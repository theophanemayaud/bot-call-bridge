from __future__ import annotations

import asyncio
import logging
import random
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from call_bridge.config import Settings
from call_bridge.sip.endpoint import RegisterStatus, SipCall, SipEndpoint, SipState
from call_bridge.sip.protocol import (
    PCMA_FRAME_BYTES,
    PCMA_PT,
    PCMA_RATE,
    PCMA_SILENCE,
    RtpPacket,
    SipMessage,
    build_sdp,
    dial_request_uri,
    digest_response,
    format_authorization,
    header_tag,
    header_uri,
    parse_authenticate,
    parse_rtp,
    parse_sdp,
    parse_sip,
    random_token,
    replace_or_add_tag,
    sip_branch,
)
from call_bridge.sip.stun import discover_public_address

log = logging.getLogger(__name__)


def local_outbound_ip(peer_host: str, peer_port: int) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer_host, peer_port))
        return probe.getsockname()[0]
    finally:
        probe.close()


@dataclass
class _Dialog:
    local_call_id: str
    dest: str
    sip_call_id: str
    from_tag: str
    to_header: str
    from_header: str
    request_uri: str
    cseq: int
    routes: list[str] = field(default_factory=list)
    contact: str | None = None
    state: SipState = "calling"
    reason: str | None = None
    rtp_sock: asyncio.DatagramTransport | None = None
    rtp_protocol: _RtpProtocol | None = None
    far_ip: str | None = None
    far_port: int | None = None
    rtp_seq: int = 0
    rtp_ts: int = 0
    rtp_ssrc: int = 0
    rtp_in_packets: int = 0
    rtp_out_packets: int = 0
    audio_in: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    invite_via_branch: str = ""


class _SipProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: LiveSipEndpoint) -> None:
        self.owner = owner

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner._on_sip_datagram(data, addr)


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, dialog: _Dialog) -> None:
        self.dialog = dialog
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            packet = parse_rtp(data)
        except ValueError:
            return
        if not packet.payload:
            return
        if self.dialog.far_ip is None:
            self.dialog.far_ip, self.dialog.far_port = addr
        self.dialog.rtp_in_packets += 1
        if self.dialog.rtp_in_packets == 1:
            log.info(
                "first inbound RTP for %s from %s:%s (%d bytes)",
                self.dialog.local_call_id,
                addr[0],
                addr[1],
                len(packet.payload),
            )
        try:
            self.dialog.audio_in.put_nowait(packet.payload)
        except asyncio.QueueFull:
            try:
                self.dialog.audio_in.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dialog.audio_in.put_nowait(packet.payload)


class LiveSipEndpoint(SipEndpoint):
    """Minimal asyncio SIP UAC: REGISTER + INVITE + ACK + BYE, RTP PCMA."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._transport: asyncio.DatagramTransport | None = None
        self._advertise_host = settings.sip_advertise_host
        self._local_port = settings.sip_local_port
        self._register_state: SipState = "idle"
        self._register_detail = "not started"
        self._register_expires = settings.sip_register_expires
        self._register_call_id = random_token(12) + f"@{settings.sip_domain}"
        self._register_cseq = 0
        self._register_from_tag = random_token(6)
        self._pending: dict[str, asyncio.Future[SipMessage]] = {}
        self._dialogs: dict[str, _Dialog] = {}
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._refresh_task: asyncio.Task[None] | None = None
        self._nc = 0
        self._proxy_addr: tuple[str, int] | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _SipProtocol(self),
            local_addr=(self._settings.sip_local_host, self._settings.sip_local_port),
        )
        self._transport = transport
        sockname = transport.get_extra_info("sockname")
        self._local_port = int(sockname[1])
        if not self._advertise_host:
            self._advertise_host = await self._resolve_advertise_host()
        log.info(
            "SIP listening udp %s:%s advertising %s",
            self._settings.sip_local_host,
            self._local_port,
            self._advertise_host,
        )
        infos = await asyncio.get_running_loop().getaddrinfo(
            self._settings.sip_proxy_host(),
            self._settings.sip_port,
            type=socket.SOCK_DGRAM,
            proto=socket.IPPROTO_UDP,
            family=socket.AF_INET,
        )
        if not infos:
            raise RuntimeError(f"DNS failed for SIP proxy {self._settings.sip_proxy_host()}")
        self._proxy_addr = (infos[0][4][0], int(infos[0][4][1]))
        log.info("SIP proxy %s resolved to %s:%s", self._settings.sip_proxy_host(), *self._proxy_addr)
        await self._register()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="sip-refresh")

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None
        for call_id in list(self._dialogs):
            try:
                await self.hangup(call_id, reason="shutdown")
            except Exception:
                pass
        if self._register_state == "registered":
            try:
                await self._register(expires=0)
            except Exception:
                pass
        if self._transport:
            self._transport.close()
            self._transport = None

    def register_status(self) -> RegisterStatus:
        return RegisterStatus(
            state=self._register_state,
            detail=self._register_detail,
            expires=self._register_expires if self._register_state == "registered" else None,
        )

    async def invite(self, dest: str, local_call_id: str) -> SipCall:
        if self._register_state != "registered":
            raise RuntimeError(f"SIP not registered ({self._register_state}: {self._register_detail})")
        rtp_port = await self._pick_rtp_port()
        dialog = _Dialog(
            local_call_id=local_call_id,
            dest=dest,
            sip_call_id=random_token(12) + f"@{self._settings.sip_domain}",
            from_tag=random_token(6),
            to_header=f"<{dial_request_uri(dest, self._settings.sip_registrar)}>",
            from_header=self._from_header(tag=None),
            request_uri=dial_request_uri(dest, self._settings.sip_registrar),
            cseq=1,
            rtp_seq=random.randint(0, 65535),
            rtp_ts=random.randint(0, 2**31),
            rtp_ssrc=random.randint(1, 2**31),
        )
        dialog.from_header = self._from_header(tag=dialog.from_tag)
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _RtpProtocol(dialog),
            local_addr=(self._settings.sip_local_host, rtp_port),
        )
        dialog.rtp_sock = transport
        dialog.rtp_protocol = protocol
        self._dialogs[local_call_id] = dialog
        sdp = build_sdp(origin_ip=self._advertise_host, rtp_port=rtp_port)
        try:
            response = await self._invite_transaction(dialog, sdp)
        except Exception:
            self._close_rtp(dialog)
            self._dialogs.pop(local_call_id, None)
            raise
        if response.status_code() != 200:
            self._close_rtp(dialog)
            self._dialogs.pop(local_call_id, None)
            raise RuntimeError(f"INVITE failed: {response.status_code()} {response.reason()}")
        await self._ack(dialog, response)
        if response.body:
            remote = parse_sdp(response.body)
            dialog.far_ip = remote["ip"]
            dialog.far_port = remote["port"]
        dialog.to_header = response.get("to") or dialog.to_header
        dialog.contact = response.get("contact")
        dialog.routes = list(reversed(response.get_all("record-route")))
        dialog.state = "answered"
        dialog.answered.set()
        # Punch NAT: send a few silence frames immediately.
        for _ in range(3):
            await self.send_audio(local_call_id, PCMA_SILENCE)
        await self._events.put(
            (
                "sip.invite",
                {
                    "state": "answered",
                    "to": dest,
                    "far": f"{dialog.far_ip}:{dialog.far_port}" if dialog.far_ip else None,
                },
            )
        )
        return self._as_call(dialog)

    async def hangup(self, local_call_id: str, reason: str = "local") -> None:
        dialog = self._dialogs.get(local_call_id)
        if not dialog or dialog.state in {"ended", "failed"}:
            return
        dialog.reason = reason
        if dialog.state in {"answered", "ringing", "calling"}:
            try:
                await self._bye(dialog)
            except Exception as exc:
                log.warning("BYE failed: %s", exc)
        dialog.state = "ended"
        dialog.finished.set()
        self._close_rtp(dialog)
        await self._events.put(("sip.hangup", {"reason": reason, "to": dialog.dest}))

    async def send_audio(self, local_call_id: str, frame: bytes) -> None:
        dialog = self._dialogs.get(local_call_id)
        if not dialog or not dialog.rtp_sock or not dialog.far_ip or not dialog.far_port:
            return
        payload = frame[:PCMA_FRAME_BYTES].ljust(PCMA_FRAME_BYTES, b"\xd5")
        packet = RtpPacket(
            payload=payload,
            sequence=dialog.rtp_seq,
            timestamp=dialog.rtp_ts,
            ssrc=dialog.rtp_ssrc,
            payload_type=PCMA_PT,
        )
        dialog.rtp_seq = (dialog.rtp_seq + 1) & 0xFFFF
        dialog.rtp_ts = (dialog.rtp_ts + PCMA_FRAME_BYTES) & 0xFFFFFFFF
        dialog.rtp_sock.sendto(packet.to_bytes(), (dialog.far_ip, dialog.far_port))

    async def audio_in(self, local_call_id: str) -> AsyncIterator[bytes]:
        dialog = self._dialogs[local_call_id]
        while not dialog.finished.is_set():
            try:
                frame = await asyncio.wait_for(dialog.audio_in.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            yield frame

    async def events(self) -> AsyncIterator[tuple[str, dict]]:
        while True:
            yield await self._events.get()

    def _as_call(self, dialog: _Dialog) -> SipCall:
        return SipCall(
            call_id=dialog.local_call_id,
            to=dialog.dest,
            state=dialog.state,
            far_ip=dialog.far_ip,
            far_port=dialog.far_port,
            reason=dialog.reason,
        )

    def _maybe_learn_advertise_from_via(self, response: SipMessage) -> None:
        """If SDP still advertises a private IP, prefer the proxy-seen public IP."""
        via = response.get("via") or ""
        if "received=" not in via:
            return
        try:
            received = via.split("received=", 1)[1].split(";", 1)[0].strip()
        except Exception:
            return
        if not received or received.count(".") != 3:
            return
        current = self._advertise_host or ""
        if received != current:
            # Prefer the IP the SIP proxy actually saw (multi-egress boxes).
            log.info("SIP advertise host learned from Via received=%s (was %s)", received, current or "unset")
            self._advertise_host = received

    async def _resolve_advertise_host(self) -> str:
        if self._settings.stun_enabled and self._settings.stun_server:
            try:
                ip, _port = await discover_public_address(
                    self._settings.stun_server, self._settings.stun_port
                )
                log.info("STUN mapped address %s via %s", ip, self._settings.stun_server)
                return ip
            except Exception as exc:
                log.warning("STUN failed (%s); falling back to local outbound IP", exc)
        peer = self._settings.sip_proxy_host()
        return local_outbound_ip(peer, self._settings.sip_port)

    async def _refresh_loop(self) -> None:
        try:
            while True:
                wait = max(30, int(self._register_expires * 0.45))
                await asyncio.sleep(wait)
                try:
                    await self._register()
                except Exception as exc:
                    self._register_state = "register_failed"
                    self._register_detail = str(exc)
                    log.error("SIP re-REGISTER failed: %s", exc)
                    await self._events.put(("sip.register", {"state": "failed", "detail": str(exc)}))
        except asyncio.CancelledError:
            raise

    async def _register(self, expires: int | None = None) -> None:
        expires = self._settings.sip_register_expires if expires is None else expires
        self._register_state = "registering"
        registrar_uri = f"sip:{self._settings.sip_registrar}"
        contact = (
            f"<sip:{self._settings.sip_username}@{self._advertise_host}:{self._local_port}>"
        )
        msg = self._base_request(
            method="REGISTER",
            request_uri=registrar_uri,
            from_header=self._from_header(tag=self._register_from_tag),
            to_header=self._from_header(tag=None),
            call_id=self._register_call_id,
            cseq=self._next_register_cseq(),
            extra={"Contact": contact, "Expires": str(expires)},
        )
        response = await self._exchange(msg, timeout=8.0)
        if response.status_code() in {401, 407}:
            self._maybe_learn_advertise_from_via(response)
            # Rebuild Contact if advertise host just changed (private -> public).
            contact = (
                f"<sip:{self._settings.sip_username}@{self._advertise_host}:{self._local_port}>"
            )
            msg.set("contact", contact)
            # RFC 3261: authenticated retry needs a new CSeq (same as INVITE path).
            msg.set("cseq", f"{self._next_register_cseq()} REGISTER")
            msg = self._authorize(msg, response)
            response = await self._exchange(msg, timeout=8.0)
        if response.status_code() != 200:
            self._register_state = "register_failed"
            self._register_detail = f"{response.status_code()} {response.reason()}"
            await self._events.put(
                ("sip.register", {"state": "failed", "detail": self._register_detail})
            )
            raise RuntimeError(f"REGISTER failed: {self._register_detail}")
        self._register_state = "registered" if expires else "idle"
        self._register_detail = "ok" if expires else "unregistered"
        self._register_expires = expires
        if expires:
            self._maybe_learn_advertise_from_via(response)
        await self._events.put(("sip.register", {"state": self._register_state, "expires": expires}))

    async def _invite_transaction(self, dialog: _Dialog, sdp: str) -> SipMessage:
        extra = {
            "Contact": f"<sip:{self._settings.sip_username}@{self._advertise_host}:{self._local_port}>",
            "Content-Type": "application/sdp",
        }
        msg = self._base_request(
            method="INVITE",
            request_uri=dialog.request_uri,
            from_header=dialog.from_header,
            to_header=dialog.to_header,
            call_id=dialog.sip_call_id,
            cseq=dialog.cseq,
            extra=extra,
            body=sdp,
        )
        dialog.invite_via_branch = msg.get("via", "").split("branch=")[-1].split(";")[0]
        response = await self._exchange(msg, timeout=45.0, dialog=dialog)
        if response.status_code() in {401, 407}:
            dialog.cseq += 1
            msg.set("cseq", f"{dialog.cseq} INVITE")
            msg = self._authorize(msg, response)
            # new branch for the authenticated INVITE
            msg.set(
                "via",
                self._via(branch=sip_branch()),
            )
            response = await self._exchange(msg, timeout=45.0, dialog=dialog)
        while response.status_code() is not None and 100 <= response.status_code() < 200:
            if response.status_code() == 180:
                dialog.state = "ringing"
                await self._events.put(("sip.invite", {"state": "ringing", "to": dialog.dest}))
            if response.body:
                try:
                    remote = parse_sdp(response.body)
                    dialog.far_ip = remote["ip"]
                    dialog.far_port = remote["port"]
                except ValueError:
                    pass
            response = await self._wait_final_or_provisional(dialog, timeout=45.0)
        return response

    async def _ack(self, dialog: _Dialog, ok: SipMessage) -> None:
        cseq_n, _ = ok.cseq() or (dialog.cseq, "INVITE")
        extra: dict[str, str] = {}
        if dialog.routes:
            extra["Route"] = dialog.routes[0] if len(dialog.routes) == 1 else ",".join(dialog.routes)
        request_uri = header_uri(ok.get("contact") or dialog.request_uri)
        ack = self._base_request(
            method="ACK",
            request_uri=request_uri,
            from_header=dialog.from_header,
            to_header=ok.get("to") or dialog.to_header,
            call_id=dialog.sip_call_id,
            cseq=cseq_n,
            extra=extra,
        )
        # ACK is not transaction-matched the same way; fire and forget.
        self._send(ack)

    async def _bye(self, dialog: _Dialog) -> None:
        dialog.cseq += 1
        extra: dict[str, str] = {}
        if dialog.routes:
            extra["Route"] = ",".join(dialog.routes)
        request_uri = header_uri(dialog.contact or dialog.request_uri)
        bye = self._base_request(
            method="BYE",
            request_uri=request_uri,
            from_header=dialog.from_header,
            to_header=dialog.to_header,
            call_id=dialog.sip_call_id,
            cseq=dialog.cseq,
            extra=extra,
        )
        try:
            response = await self._exchange(bye, timeout=5.0)
            if response.status_code() in {401, 407}:
                dialog.cseq += 1
                bye.set("cseq", f"{dialog.cseq} BYE")
                bye = self._authorize(bye, response)
                await self._exchange(bye, timeout=5.0)
        except TimeoutError:
            log.warning("BYE timed out")

    def _base_request(
        self,
        *,
        method: str,
        request_uri: str,
        from_header: str,
        to_header: str,
        call_id: str,
        cseq: int,
        extra: dict[str, str] | None = None,
        body: str = "",
    ) -> SipMessage:
        msg = SipMessage(start_line=f"{method} {request_uri} SIP/2.0")
        msg.set("via", self._via(branch=sip_branch()))
        msg.set("max-forwards", "70")
        msg.set("from", from_header)
        msg.set("to", to_header)
        msg.set("call-id", call_id)
        msg.set("cseq", f"{cseq} {method}")
        msg.set("user-agent", self._settings.sip_user_agent)
        msg.set("allow", "INVITE, ACK, BYE, CANCEL, OPTIONS")
        for key, value in (extra or {}).items():
            if key.lower() == "route":
                for route in value.split(","):
                    msg.add("route", route.strip())
            else:
                msg.set(key, value)
        msg.body = body
        return msg

    def _from_header(self, tag: str | None) -> str:
        display = self._settings.sip_from_display()
        uri = f"sip:{self._settings.sip_username}@{self._settings.sip_domain}"
        value = f'"{display}" <{uri}>'
        if tag:
            value = f"{value};tag={tag}"
        return value

    def _via(self, branch: str) -> str:
        return (
            f"SIP/2.0/UDP {self._advertise_host}:{self._local_port};branch={branch};rport"
        )

    def _next_register_cseq(self) -> int:
        self._register_cseq += 1
        return self._register_cseq

    def _authorize(self, request: SipMessage, challenge_msg: SipMessage) -> SipMessage:
        header_name = (
            "proxy-authenticate" if challenge_msg.status_code() == 407 else "www-authenticate"
        )
        raw = challenge_msg.get(header_name)
        if not raw:
            raise RuntimeError("SIP challenge missing authenticate header")
        challenge = parse_authenticate(raw)
        method = request.method() or ""
        uri = request.start_line.split(" ", 2)[1]
        self._nc += 1
        params = digest_response(
            username=self._settings.sip_username,
            password=self._settings.sip_password,
            method=method,
            uri=uri,
            challenge=challenge,
            nc=f"{self._nc:08x}",
        )
        auth_header = (
            "proxy-authorization" if challenge_msg.status_code() == 407 else "authorization"
        )
        request.set(auth_header, format_authorization(params))
        return request

    def _send(self, msg: SipMessage) -> None:
        if not self._transport:
            raise RuntimeError("SIP transport is down")
        if not self._proxy_addr:
            raise RuntimeError("SIP proxy address not resolved")
        self._transport.sendto(msg.to_bytes(), self._proxy_addr)

    async def _exchange(
        self,
        msg: SipMessage,
        timeout: float,
        dialog: _Dialog | None = None,
    ) -> SipMessage:
        key = self._tx_key_from_request(msg)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SipMessage] = loop.create_future()
        self._pending[key] = future
        self._send(msg)
        try:
            while True:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
                code = response.status_code()
                # 1xx is provisional for REGISTER and INVITE alike — keep waiting.
                if code is not None and 100 <= code < 200:
                    future = loop.create_future()
                    self._pending[key] = future
                    if dialog:
                        if code == 180:
                            dialog.state = "ringing"
                            await self._events.put(
                                ("sip.invite", {"state": "ringing", "to": dialog.dest})
                            )
                        if response.body:
                            try:
                                remote = parse_sdp(response.body)
                                dialog.far_ip = remote["ip"]
                                dialog.far_port = remote["port"]
                            except ValueError:
                                pass
                    continue
                return response
        except TimeoutError:
            self._pending.pop(key, None)
            raise TimeoutError(f"SIP timeout waiting for response to {msg.method()}")
        finally:
            if self._pending.get(key) is future:
                self._pending.pop(key, None)

    async def _wait_final_or_provisional(self, dialog: _Dialog, timeout: float) -> SipMessage:
        # After a provisional, the INVITE transaction key is still the original branch+CSeq.
        # _exchange already loops; this helper is used if we need another wait.
        loop = asyncio.get_running_loop()
        key = f"{dialog.sip_call_id}:{dialog.cseq}:INVITE"
        future: asyncio.Future[SipMessage] = loop.create_future()
        self._pending[key] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            if self._pending.get(key) is future:
                self._pending.pop(key, None)

    def _tx_key_from_request(self, msg: SipMessage) -> str:
        cseq = msg.cseq()
        method = cseq[1] if cseq else (msg.method() or "")
        number = cseq[0] if cseq else 0
        return f"{msg.get('call-id')}:{number}:{method}"

    def _tx_key_from_response(self, msg: SipMessage) -> str:
        cseq = msg.cseq()
        if not cseq:
            return ""
        return f"{msg.get('call-id')}:{cseq[0]}:{cseq[1]}"

    def _on_sip_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = parse_sip(data)
        except ValueError:
            log.debug("unparseable SIP from %s", addr)
            return
        if msg.status_code() is not None:
            key = self._tx_key_from_response(msg)
            future = self._pending.get(key)
            if future and not future.done():
                future.set_result(msg)
            return
        method = msg.method()
        if method == "OPTIONS":
            self._reply(msg, 200, "OK", addr)
        elif method == "BYE":
            self._handle_remote_bye(msg, addr)
        elif method == "ACK":
            return
        else:
            self._reply(msg, 405, "Method Not Allowed", addr)

    def _handle_remote_bye(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        call_id = msg.get("call-id")
        for dialog in list(self._dialogs.values()):
            if dialog.sip_call_id == call_id:
                self._reply(msg, 200, "OK", addr)
                dialog.state = "ended"
                dialog.reason = "remote"
                dialog.finished.set()
                self._close_rtp(dialog)
                self._events.put_nowait(("sip.hangup", {"reason": "remote", "to": dialog.dest}))
                return
        self._reply(msg, 481, "Call/Transaction Does Not Exist", addr)

    def _reply(self, request: SipMessage, code: int, reason: str, addr: tuple[str, int]) -> None:
        if not self._transport:
            return
        to = request.get("to") or ""
        if not header_tag(to):
            to = replace_or_add_tag(to, random_token(6))
        reply = SipMessage(start_line=f"SIP/2.0 {code} {reason}")
        for via in request.get_all("via"):
            reply.add("via", via)
        reply.set("from", request.get("from") or "")
        reply.set("to", to)
        reply.set("call-id", request.get("call-id") or "")
        reply.set("cseq", request.get("cseq") or "")
        reply.set("user-agent", self._settings.sip_user_agent)
        self._transport.sendto(reply.to_bytes(), addr)

    def _close_rtp(self, dialog: _Dialog) -> None:
        if dialog.rtp_sock:
            dialog.rtp_sock.close()
            dialog.rtp_sock = None

    async def _pick_rtp_port(self) -> int:
        start = self._settings.sip_rtp_port_start
        end = self._settings.sip_rtp_port_end
        last_error: Exception | None = None
        for port in range(start, end + 1, 2):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self._settings.sip_local_host, port))
                sock.close()
                return port
            except OSError as exc:
                last_error = exc
        raise RuntimeError(f"no free RTP port in {start}-{end}: {last_error}")
