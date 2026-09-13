from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from call_bridge.config import Settings
from call_bridge.events import BridgeEvent, EventBus
from call_bridge.script import CallScript
from call_bridge.sip.endpoint import SipEndpoint
from call_bridge.sip.protocol import PCMA_FRAME_BYTES, PCMA_SILENCE
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.provider import VoiceAgentProvider, VoiceEvent, VoiceSessionConfig
from call_bridge.voice.tools import default_call_tools

log = logging.getLogger(__name__)


def voice_event_is_activity(event: VoiceEvent) -> bool:
    """True when a voice-layer event should reset the idle timer.

    User transcript / barge-in and assistant transcript or outbound audio count.
    Continuous SIP comfort-noise RTP does not — the session never sees those as
    voice events.
    """
    if event.type == "audio.out":
        return bool(event.payload.get("pcm"))
    if event.type == "speech.started":
        return True
    if event.type == "transcript":
        text = event.payload.get("text")
        return bool(text) or event.payload.get("event") == "speech_started"
    return False

CallState = Literal[
    "starting",
    "dialing",
    "ringing",
    "bridged",
    "waiting_orchestrator",
    "ending",
    "ended",
    "failed",
]


@dataclass
class PendingTool:
    tool: str
    tool_call_id: str
    arguments: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future[dict[str, Any]] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


@dataclass
class CallRecord:
    id: str
    script: CallScript
    provider: str
    state: CallState
    started_at: float
    ended_at: float | None = None
    error: str | None = None
    hangup_reason: str | None = None
    pending_tools: dict[str, PendingTool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "to": self.script.call.to,
            "language": self.script.call.language,
            "provider": self.provider,
            "state": self.state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "hangup_reason": self.hangup_reason,
            "pending_tools": [
                {
                    "tool": item.tool,
                    "tool_call_id": item.tool_call_id,
                    "arguments": item.arguments,
                }
                for item in self.pending_tools.values()
            ],
            "metadata": self.script.metadata,
        }


class CallManager:
    def __init__(self, settings: Settings, sip: SipEndpoint, bus: EventBus) -> None:
        self.settings = settings
        self.sip = sip
        self.bus = bus
        self.calls: dict[str, CallRecord] = {}
        self._active: dict[str, _LiveCall] = {}

    def status(self) -> dict[str, Any]:
        sip = self.sip.register_status()
        return {
            "mode": self.settings.bridge_mode,
            "voice_provider": self.settings.voice_provider,
            "sip": {"state": sip.state, "detail": sip.detail, "expires": sip.expires},
            "active_calls": [record.as_dict() for record in self.calls.values() if record.state not in {"ended", "failed"}],
            "recent_calls": [record.as_dict() for record in list(self.calls.values())[-10:]],
        }

    async def start_call(
        self,
        script: CallScript,
        *,
        provider_name: Literal["grok", "openai", "mock"] | None = None,
    ) -> CallRecord:
        active = [c for c in self.calls.values() if c.state not in {"ended", "failed"}]
        if len(active) >= self.settings.max_concurrent_calls:
            raise RuntimeError(
                f"already have {len(active)} active call(s); this bridge is one SIP registration"
            )
        if self.settings.bridge_mode == "live":
            self.settings.require_live_sip()
            chosen = provider_name or self.settings.voice_provider
            if chosen != "mock":
                self.settings.require_live_voice()
        call_id = uuid.uuid4().hex[:12]
        provider = create_voice_provider(self.settings, name=provider_name)
        record = CallRecord(
            id=call_id,
            script=script,
            provider=provider.name,
            state="starting",
            started_at=time.time(),
        )
        self.calls[call_id] = record
        live = _LiveCall(self, record, provider)
        self._active[call_id] = live
        live.task = asyncio.create_task(live.run(), name=f"call-{call_id}")
        self._emit(record, "call.state", {"state": record.state, "to": script.call.to})
        return record

    async def hangup(self, call_id: str, reason: str = "orchestrator") -> CallRecord:
        live = self._active.get(call_id)
        record = self.calls.get(call_id)
        if not record:
            raise KeyError(call_id)
        if live:
            await live.request_hangup(reason)
        else:
            record.state = "ended"
            record.hangup_reason = reason
            record.ended_at = time.time()
        return record

    async def inject_guideline(self, call_id: str, text: str, *, mode: Literal["steer", "speak"] = "steer") -> None:
        live = self._require_live(call_id)
        await live.inject_guideline(text, mode=mode)
        self._emit(live.record, "guideline", {"text": text, "mode": mode})

    async def submit_tool_result(self, call_id: str, tool_call_id: str, output: dict[str, Any]) -> None:
        live = self._require_live(call_id)
        await live.submit_orchestrator_result(tool_call_id, output)
        self._emit(live.record, "tool_result", {"tool_call_id": tool_call_id, "output": output})

    def get(self, call_id: str) -> CallRecord:
        record = self.calls.get(call_id)
        if not record:
            raise KeyError(call_id)
        return record

    def _require_live(self, call_id: str) -> _LiveCall:
        live = self._active.get(call_id)
        if not live:
            raise RuntimeError(f"call {call_id} is not active")
        return live

    def _emit(self, record: CallRecord, type_: Any, payload: dict[str, Any]) -> None:
        self.bus.publish(BridgeEvent(type=type_, call_id=record.id, payload=payload))


class _LiveCall:
    def __init__(self, manager: CallManager, record: CallRecord, provider: VoiceAgentProvider) -> None:
        self.manager = manager
        self.record = record
        self.provider = provider
        self.task: asyncio.Task[None] | None = None
        self._hangup = asyncio.Event()
        self._hangup_reason = "local"
        self._downlink: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._play_buf = bytearray()
        self._last_activity = time.monotonic()

    async def run(self) -> None:
        record = self.record
        script = record.script
        try:
            record.state = "dialing"
            self.manager._emit(record, "call.state", {"state": "dialing", "to": script.call.to})
            sip_call = await self.manager.sip.invite(script.call.to, record.id)
            record.state = "ringing" if sip_call.state == "ringing" else "bridged"
            self.manager._emit(record, "call.state", {"state": record.state, "sip": sip_call.state})

            voice_config = VoiceSessionConfig(
                instructions=script.build_instructions(),
                voice=script.voice.voice,
                language=script.call.language,
                keyterms=script.voice.keyterms,
                tools=default_call_tools(),
                speak_first=script.mission.disclosure if script.mission.speak_disclosure_first else None,
                transcribe_model=self.manager.settings.xai_transcribe_model,
                enable_web_search=script.voice.enable_web_search,
            )
            await self.provider.connect(voice_config)
            record.state = "bridged"
            self.manager._emit(record, "call.state", {"state": "bridged"})

            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._pump_rtp_to_voice(), name="rtp-up")
                    group.create_task(self._pump_voice_to_rtp(), name="rtp-down")
                    group.create_task(self._consume_voice_events(), name="voice-events")
                    group.create_task(
                        self._watch_hangup_or_timeout(script.voice.max_duration_seconds),
                        name="watch",
                    )
            except* _CallFinished:
                pass
            except* Exception as eg:
                raise RuntimeError("; ".join(str(exc) for exc in eg.exceptions)) from eg
        except _CallFinished:
            pass
        except Exception as exc:
            record.state = "failed"
            record.error = str(exc)
            self.manager._emit(record, "voice.error", {"message": record.error})
            log.exception("call %s failed", record.id)
        finally:
            await self._teardown()

    async def request_hangup(self, reason: str) -> None:
        self._hangup_reason = reason
        self._hangup.set()
        if self.task:
            # give the watch task a moment; teardown happens in finally
            for _ in range(50):
                if self.record.state in {"ended", "failed"}:
                    return
                await asyncio.sleep(0.05)

    async def inject_guideline(self, text: str, *, mode: Literal["steer", "speak"]) -> None:
        if mode == "speak":
            await self.provider.speak_verbatim(text, interruptible=True)
        else:
            await self.provider.inject_guideline(text)

    async def submit_orchestrator_result(self, tool_call_id: str, output: dict[str, Any]) -> None:
        pending = self.record.pending_tools.get(tool_call_id)
        if not pending:
            raise KeyError(tool_call_id)
        if not pending.future.done():
            pending.future.set_result(output)

    def _mark_activity(self) -> None:
        try:
            self._last_activity = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_activity = time.monotonic()

    async def _watch_hangup_or_timeout(self, max_seconds: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        idle_seconds = float(self.manager.settings.call_idle_timeout_seconds)
        self._last_activity = loop.time()
        while not self._hangup.is_set():
            now = loop.time()
            remaining_max = deadline - now
            if remaining_max <= 0:
                self._hangup_reason = "max_duration"
                self._hangup.set()
                break
            timeout = remaining_max
            if idle_seconds > 0:
                remaining_idle = idle_seconds - (now - self._last_activity)
                if remaining_idle <= 0:
                    self._hangup_reason = "idle_timeout"
                    self._hangup.set()
                    log.info(
                        "call %s idle timeout after %.1fs with no voice activity",
                        self.record.id,
                        idle_seconds,
                    )
                    break
                timeout = min(timeout, remaining_idle)
            try:
                await asyncio.wait_for(self._hangup.wait(), timeout=timeout)
            except TimeoutError:
                continue
        raise _CallFinished()

    async def _pump_rtp_to_voice(self) -> None:
        async for frame in self.manager.sip.audio_in(self.record.id):
            if self._hangup.is_set():
                return
            await self.provider.send_audio(frame)
        if not self._hangup.is_set():
            self._hangup_reason = "remote"
            self._hangup.set()

    async def _pump_voice_to_rtp(self) -> None:
        interval = 0.02
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._hangup.is_set():
            now = loop.time()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
            next_tick += interval
            while len(self._play_buf) < PCMA_FRAME_BYTES:
                try:
                    chunk = self._downlink.get_nowait()
                    self._play_buf.extend(chunk)
                except asyncio.QueueEmpty:
                    break
            if len(self._play_buf) >= PCMA_FRAME_BYTES:
                frame = bytes(self._play_buf[:PCMA_FRAME_BYTES])
                del self._play_buf[:PCMA_FRAME_BYTES]
            else:
                # Keep RTP flowing while the agent is silent. Stopping media after the
                # first TTS can make carriers inject MOH/IVR and freeze the NAT path.
                frame = PCMA_SILENCE
            await self.manager.sip.send_audio(self.record.id, frame)

    async def _consume_voice_events(self) -> None:
        async for event in self.provider.events():
            if voice_event_is_activity(event):
                self._mark_activity()
            if event.type == "audio.out":
                pcm = event.payload.get("pcm") or b""
                if pcm:
                    try:
                        self._downlink.put_nowait(pcm)
                    except asyncio.QueueFull:
                        try:
                            self._downlink.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._downlink.put_nowait(pcm)
            elif event.type == "speech.started":
                # Barge-in: drop queued assistant audio so the callee is heard.
                self._flush_downlink()
                self.manager._emit(self.record, "transcript", {"role": "user", "event": "speech_started"})
            elif event.type == "transcript":
                self.manager._emit(self.record, "transcript", event.payload)
            elif event.type == "tool_call":
                await self._on_tool_call(event.payload)
            elif event.type == "error":
                self.manager._emit(self.record, "voice.error", event.payload)
            elif event.type == "closed":
                return

    def _flush_downlink(self) -> None:
        self._play_buf.clear()
        while True:
            try:
                self._downlink.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _on_tool_call(self, payload: dict[str, Any]) -> None:
        tool = payload.get("tool")
        tool_call_id = payload.get("tool_call_id") or uuid.uuid4().hex
        arguments = payload.get("arguments") or {}
        self.manager._emit(
            self.record,
            "tool_call",
            {"tool": tool, "tool_call_id": tool_call_id, "arguments": arguments},
        )
        if tool == "hangup":
            await self.provider.submit_tool_result(
                tool_call_id,
                {"ok": True, "hanging_up": True},
                continue_response=False,
            )
            # Let any in-flight goodbye TTS reach the RTP playout (plus configured grace).
            grace = float(self.manager.settings.hangup_grace_seconds)
            deadline = asyncio.get_running_loop().time() + max(grace, 2.5)
            while asyncio.get_running_loop().time() < deadline:
                if self._downlink.empty() and len(self._play_buf) < PCMA_FRAME_BYTES:
                    # small settle so the last frame is sent
                    await asyncio.sleep(0.15)
                    if self._downlink.empty() and len(self._play_buf) < PCMA_FRAME_BYTES:
                        break
                await asyncio.sleep(0.05)
            await asyncio.sleep(grace)
            self._hangup_reason = str(arguments.get("reason") or "agent")
            self.record.hangup_reason = self._hangup_reason
            self._hangup.set()
            return
        if tool == "ask_orchestrator":
            pending = PendingTool(tool=tool, tool_call_id=tool_call_id, arguments=arguments)
            self.record.pending_tools[tool_call_id] = pending
            self.record.state = "waiting_orchestrator"
            self.manager._emit(self.record, "call.state", {"state": "waiting_orchestrator"})
            try:
                result = await asyncio.wait_for(
                    pending.future, timeout=self.manager.settings.tool_timeout_seconds
                )
            except TimeoutError:
                result = {
                    "ok": False,
                    "error": "orchestrator_timeout",
                    "instruction": "The orchestrator did not answer in time. Use a fallback and do not invent facts.",
                }
            self.record.pending_tools.pop(tool_call_id, None)
            if self.record.state == "waiting_orchestrator":
                self.record.state = "bridged"
            await self.provider.submit_tool_result(tool_call_id, result, continue_response=True)
            return
        await self.provider.submit_tool_result(
            tool_call_id,
            {"ok": False, "error": f"unknown tool {tool}"},
            continue_response=True,
        )

    async def _teardown(self) -> None:
        reason = self._hangup_reason
        try:
            await self.manager.sip.hangup(self.record.id, reason=reason)
        except Exception as exc:
            log.warning("sip hangup: %s", exc)
        try:
            await self.provider.close()
        except Exception as exc:
            log.warning("voice close: %s", exc)
        if self.record.state != "failed":
            self.record.state = "ended"
        self.record.hangup_reason = reason
        self.record.ended_at = time.time()
        self.manager._active.pop(self.record.id, None)
        self.manager._emit(self.record, "call.state", {"state": self.record.state, "reason": reason})
        self.manager._emit(self.record, "sip.hangup", {"reason": reason})


class _CallFinished(Exception):
    """Ends the TaskGroup without treating hangup as a failure."""
