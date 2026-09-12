from __future__ import annotations

import hashlib
import random
import re
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from call_bridge.script import normalize_dial_user

PCMA_PT = 8
PCMA_RATE = 8000
PCMA_PTIME_MS = 20
PCMA_FRAME_BYTES = 160
PCMA_SILENCE = bytes([0xD5]) * PCMA_FRAME_BYTES


def random_token(n: int = 12) -> str:
    return secrets.token_hex(n)


def sip_branch() -> str:
    return "z9hG4bK" + random_token(8)


def quote_sip(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class SipMessage:
    start_line: str
    headers: dict[str, list[str]] = field(default_factory=dict)
    body: str = ""

    def method(self) -> str | None:
        if self.start_line.startswith("SIP/2.0"):
            return None
        return self.start_line.split(" ", 1)[0].upper()

    def status_code(self) -> int | None:
        if not self.start_line.startswith("SIP/2.0"):
            return None
        parts = self.start_line.split()
        return int(parts[1]) if len(parts) > 1 else None

    def reason(self) -> str:
        if not self.start_line.startswith("SIP/2.0"):
            return ""
        parts = self.start_line.split(" ", 2)
        return parts[2] if len(parts) > 2 else ""

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else default

    def get_all(self, name: str) -> list[str]:
        return list(self.headers.get(name.lower(), []))

    def add(self, name: str, value: str) -> None:
        self.headers.setdefault(name.lower(), []).append(value)

    def set(self, name: str, value: str) -> None:
        self.headers[name.lower()] = [value]

    def cseq(self) -> tuple[int, str] | None:
        raw = self.get("cseq")
        if not raw:
            return None
        num, _, method = raw.strip().partition(" ")
        return int(num), method.strip().upper()

    def to_bytes(self) -> bytes:
        order = [
            "via",
            "max-forwards",
            "from",
            "to",
            "call-id",
            "cseq",
            "contact",
            "expires",
            "allow",
            "user-agent",
            "authorization",
            "proxy-authorization",
            "record-route",
            "route",
            "content-type",
            "content-length",
        ]
        display = {
            "via": "Via",
            "max-forwards": "Max-Forwards",
            "from": "From",
            "to": "To",
            "call-id": "Call-ID",
            "cseq": "CSeq",
            "contact": "Contact",
            "expires": "Expires",
            "allow": "Allow",
            "user-agent": "User-Agent",
            "authorization": "Authorization",
            "proxy-authorization": "Proxy-Authorization",
            "record-route": "Record-Route",
            "route": "Route",
            "content-type": "Content-Type",
            "content-length": "Content-Length",
            "www-authenticate": "WWW-Authenticate",
            "proxy-authenticate": "Proxy-Authenticate",
        }
        lines = [self.start_line]
        seen: set[str] = set()
        for key in order:
            for value in self.headers.get(key, []):
                lines.append(f"{display.get(key, key)}: {value}")
            seen.add(key)
        for key, values in self.headers.items():
            if key in seen:
                continue
            for value in values:
                lines.append(f"{display.get(key, key.title())}: {value}")
        body_bytes = self.body.encode("utf-8") if self.body else b""
        # Always rewrite Content-Length to match the body we send.
        filtered = [line for line in lines if not line.lower().startswith("content-length:")]
        filtered.append(f"Content-Length: {len(body_bytes)}")
        header = "\r\n".join(filtered) + "\r\n\r\n"
        return header.encode("utf-8") + body_bytes


def parse_sip(data: bytes) -> SipMessage:
    text = data.decode("utf-8", errors="replace")
    header_part, sep, body = text.partition("\r\n\r\n")
    if not sep:
        header_part, sep, body = text.partition("\n\n")
    raw_lines = header_part.splitlines()
    if not raw_lines:
        raise ValueError("empty SIP message")
    lines: list[str] = []
    for line in raw_lines:
        if lines and (line.startswith(" ") or line.startswith("\t")):
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    start = lines[0]
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return SipMessage(start_line=start, headers=headers, body=body)


_AUTH_PARAM = re.compile(r'(\w+)=("(?:\\.|[^"])*"|[^,]+)')


def parse_authenticate(header: str) -> dict[str, str]:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "digest":
        raise ValueError(f"unsupported auth scheme: {scheme}")
    params: dict[str, str] = {}
    for match in _AUTH_PARAM.finditer(rest):
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
        params[match.group(1)] = value
    return params


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def digest_response(
    *,
    username: str,
    password: str,
    method: str,
    uri: str,
    challenge: dict[str, str],
    cnonce: str | None = None,
    nc: str = "00000001",
) -> dict[str, str]:
    realm = challenge["realm"]
    nonce = challenge["nonce"]
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    if algorithm not in {"MD5", "MD5-SESS"}:
        raise ValueError(f"unsupported digest algorithm: {algorithm}")
    ha1 = md5_hex(f"{username}:{realm}:{password}")
    qop_raw = challenge.get("qop")
    qop = None
    if qop_raw:
        qops = [part.strip() for part in qop_raw.split(",")]
        if "auth" in qops:
            qop = "auth"
    if algorithm == "MD5-SESS":
        cnonce = cnonce or random_token(8)
        ha1 = md5_hex(f"{ha1}:{nonce}:{cnonce}")
    ha2 = md5_hex(f"{method}:{uri}")
    if qop:
        cnonce = cnonce or random_token(8)
        response = md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    else:
        response = md5_hex(f"{ha1}:{nonce}:{ha2}")
    out = {
        "username": username,
        "realm": realm,
        "nonce": nonce,
        "uri": uri,
        "response": response,
        "algorithm": algorithm,
    }
    if "opaque" in challenge:
        out["opaque"] = challenge["opaque"]
    if qop:
        out["qop"] = qop
        out["nc"] = nc
        out["cnonce"] = cnonce or random_token(8)
    return out


def format_authorization(params: dict[str, str]) -> str:
    quoted = {"username", "realm", "nonce", "uri", "response", "opaque", "cnonce"}
    parts: list[str] = []
    for key, value in params.items():
        if key in quoted:
            parts.append(f"{key}={quote_sip(value)}")
        else:
            parts.append(f"{key}={value}")
    return "Digest " + ", ".join(parts)


def parse_sip_uri(uri: str) -> tuple[str, str, int]:
    raw = uri.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    raw = raw.split(";")[0]
    if raw.lower().startswith("sip:"):
        raw = raw[4:]
    user = ""
    hostport = raw
    if "@" in raw:
        user, hostport = raw.split("@", 1)
        user = unquote(user)
    host, _, port_s = hostport.partition(":")
    port = int(port_s) if port_s else 5060
    return user, host, port


def header_uri(value: str) -> str:
    match = re.search(r"<([^>]+)>", value)
    if match:
        return match.group(1)
    return value.split(";")[0].strip()


def header_tag(value: str) -> str | None:
    match = re.search(r";\s*tag=([^;\s]+)", value, re.I)
    return match.group(1) if match else None


def replace_or_add_tag(value: str, tag: str) -> str:
    if re.search(r";\s*tag=", value, re.I):
        return re.sub(r";\s*tag=[^;\s]+", f";tag={tag}", value, flags=re.I)
    return f"{value};tag={tag}"


def build_sdp(*, origin_ip: str, rtp_port: int, session_id: int | None = None) -> str:
    sess = session_id or random.randint(1, 2**31 - 1)
    return (
        "v=0\r\n"
        f"o=- {sess} {sess} IN IP4 {origin_ip}\r\n"
        "s=call-bridge\r\n"
        f"c=IN IP4 {origin_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP {PCMA_PT}\r\n"
        f"a=rtpmap:{PCMA_PT} PCMA/{PCMA_RATE}\r\n"
        f"a=ptime:{PCMA_PTIME_MS}\r\n"
        "a=sendrecv\r\n"
    )


def parse_sdp(body: str) -> dict[str, Any]:
    ip = None
    port = None
    payload = PCMA_PT
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("c=") and "IP4" in line:
            ip = line.split()[-1]
        elif line.startswith("m=audio"):
            parts = line.split()
            port = int(parts[1])
            if len(parts) >= 4:
                payload = int(parts[3])
    if not ip or not port:
        raise ValueError("SDP missing connection or media")
    return {"ip": ip, "port": port, "payload": payload}


@dataclass
class RtpPacket:
    payload: bytes
    sequence: int
    timestamp: int
    ssrc: int
    payload_type: int = PCMA_PT
    marker: bool = False

    def to_bytes(self) -> bytes:
        b0 = 0x80
        b1 = self.payload_type & 0x7F
        if self.marker:
            b1 |= 0x80
        header = struct.pack("!BBHII", b0, b1, self.sequence & 0xFFFF, self.timestamp & 0xFFFFFFFF, self.ssrc)
        return header + self.payload


def parse_rtp(data: bytes) -> RtpPacket:
    if len(data) < 12:
        raise ValueError("RTP packet too short")
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
    csrc_count = b0 & 0x0F
    offset = 12 + 4 * csrc_count
    if b0 & 0x10:
        if len(data) < offset + 4:
            raise ValueError("RTP extension truncated")
        ext_len = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        offset += 4 + 4 * ext_len
    return RtpPacket(
        payload=data[offset:],
        sequence=seq,
        timestamp=ts,
        ssrc=ssrc,
        payload_type=b1 & 0x7F,
        marker=bool(b1 & 0x80),
    )


def dial_request_uri(number: str, registrar: str) -> str:
    return f"sip:{normalize_dial_user(number)}@{registrar}"

