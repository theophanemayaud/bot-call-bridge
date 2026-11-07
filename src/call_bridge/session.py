from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from call_bridge.config import Settings
from call_bridge.events import BridgeEvent, EventBus
from call_bridge.handoff import ResolvedHandoff, resolve_handoff
from call_bridge.script import CallScript
from call_bridge.sip.dtmf import dtmf_frames
from call_bridge.sip.endpoint import SipEndpoint
from call_bridge.sip.protocol import PCMA_FRAME_BYTES, PCMA_SILENCE
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.provider import VoiceAgentProvider, VoiceEvent, VoiceSessionConfig
from call_bridge.voice.tools import (
    HANDOFF_TOOL_NAMES,
    default_call_tools,
    handoff_call_tools,
    tool_route,
)

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

MediaMode = Literal["voice", "context_owner", "relay"]


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
    handoff: ResolvedHandoff | None = None
    handoff_stage: str | None = None

    def as_dict(self) -> dict[str, Any]:
        handoff = None
        if self.handoff is not None:
            handoff = {
                "stage": self.handoff_stage,
                "to": self.handoff.to,
                "owner_name": self.handoff.owner_name,
                "party": self.handoff.party,
                "lead_seconds": self.handoff.lead_seconds,
                "max_hold_seconds": self.handoff.max_hold_seconds,
                "on_unavailable": self.handoff.on_unavailable,
            }
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
            "handoff": handoff,
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
        handoff = self._resolve_handoff(script)
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
            handoff=handoff,
        )
        self.calls[call_id] = record
        live = _LiveCall(self, record, provider)
        self._active[call_id] = live
        live.task = asyncio.create_task(live.run(), name=f"call-{call_id}")
        self._emit(record, "call.state", {"state": record.state, "to": script.call.to})
        return record

    def _resolve_handoff(self, script: CallScript) -> ResolvedHandoff | None:
        if not script.requests_handoff() or script.handoff is None:
            return None
        try:
            policy = resolve_handoff(
                script.handoff,
                self.settings,
                language=script.call.language,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if not policy.to:
            raise RuntimeError(
                "handoff requires an owner number: set HANDOFF_OWNER_NUMBER "
                "or script handoff.to / handoff_to"
            )
        return policy

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

    async def request_handoff(
        self,
        call_id: str,
        action: Literal["human_answered", "bring_in_owner"],
        *,
        summary: str = "",
        party: str = "",
    ) -> None:
        if call_id not in self.calls:
            raise KeyError(call_id)
        live = self._require_live(call_id)
        await live.request_handoff(action, summary=summary, party=party)

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
        self._orchestrator_tasks: set[asyncio.Task[None]] = set()
        self._local_tool_tasks: set[asyncio.Task[None]] = set()
        self._handoff = record.handoff
        self._suppress_idle = record.handoff is not None
        self._media_mode: MediaMode = "voice"
        self._human_ready = False
        self._merged = False
        self._announced = False
        self._owner_up = False
        self._owner_parked = False
        self._owner_gave_up = False
        self._owner_attempts = 0
        self._owner_generation = 0
        self._owner_leg_id = f"{record.id}:owner"
        self._owner_streaming = asyncio.Event()
        self._dial_task: asyncio.Task[bool] | None = None
        self._handoff_lock = asyncio.Lock()
        self._target_buf = bytearray()
        self._owner_buf = bytearray()
        self._dtmf: deque[bytes] = deque()

    async def run(self) -> None:
        record = self.record
        script = record.script
        try:
            record.state = "dialing"
            self.manager._emit(record, "call.state", {"state": "dialing", "to": script.call.to})
            sip_call = await self.manager.sip.invite(script.call.to, record.id)
            record.state = "ringing" if sip_call.state == "ringing" else "bridged"
            self.manager._emit(record, "call.state", {"state": record.state, "sip": sip_call.state})

            policy = record.handoff
            voice_config = VoiceSessionConfig(
                instructions=script.build_instructions(handoff=policy),
                voice=script.voice.voice,
                language=script.call.language,
                keyterms=script.voice.keyterms,
                tools=handoff_call_tools() if policy else default_call_tools(),
                speak_first=script.mission.disclosure if script.mission.speak_disclosure_first else None,
                transcribe_model=self.manager.settings.xai_transcribe_model,
                enable_web_search=script.voice.enable_web_search,
            )
            await self.provider.connect(voice_config)
            record.state = "bridged"
            self.manager._emit(record, "call.state", {"state": "bridged"})
            if policy is not None:
                self._emit_handoff(
                    "holding",
                    to=policy.to,
                    lead_seconds=policy.lead_seconds,
                    max_hold_seconds=policy.max_hold_seconds,
                )

            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._pump_rtp_to_voice(), name="rtp-up")
                    group.create_task(self._pump_voice_to_rtp(), name="rtp-down")
                    group.create_task(self._consume_voice_events(), name="voice-events")
                    group.create_task(
                        self._watch_hangup_or_timeout(script.voice.max_duration_seconds),
                        name="watch",
                    )
                    if policy is not None:
                        group.create_task(self._pump_owner_rtp(), name="owner-rtp")
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

    async def request_handoff(
        self,
        action: Literal["human_answered", "bring_in_owner"],
        *,
        summary: str = "",
        party: str = "",
    ) -> None:
        if self._handoff is None:
            raise RuntimeError("call has no handoff policy")
        payload = {
            "tool": action,
            "tool_call_id": f"orch-{uuid.uuid4().hex[:8]}",
            "arguments": {"summary": summary, "party": party, "source": "orchestrator"},
        }
        task = asyncio.create_task(self._run_local_tool(payload), name=f"orch-{action}")
        self._local_tool_tasks.add(task)
        task.add_done_callback(self._local_tool_tasks.discard)

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
        hold_limit = self._handoff.max_hold_seconds if self._handoff is not None else 0.0
        hold_deadline = (loop.time() + hold_limit) if hold_limit > 0 else None
        self._last_activity = loop.time()
        while not self._hangup.is_set():
            now = loop.time()
            remaining_max = deadline - now
            if remaining_max <= 0:
                self._hangup_reason = "max_duration"
                self._hangup.set()
                break
            if (
                hold_deadline is not None
                and not self._human_ready
                and not self._merged
                and now >= hold_deadline
            ):
                self._hangup_reason = "hold_timeout"
                self.record.hangup_reason = "hold_timeout"
                self._emit_handoff("hold_timeout")
                self._hangup.set()
                log.info("call %s hold timeout after %.1fs", self.record.id, hold_limit)
                break
            timeout = remaining_max
            if hold_deadline is not None and not self._human_ready and not self._merged:
                timeout = min(timeout, max(0.0, hold_deadline - now))
            idle_seconds = (
                0.0
                if self._suppress_idle
                else float(self.manager.settings.call_idle_timeout_seconds)
            )
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
            # Relay / owner-context keeps the single-leg path free of a jitter buffer.
            # Frames are forwarded on the same 20 ms tick, capped at two frames.
            if self._media_mode in {"relay", "context_owner"}:
                self._push_leg(self._target_buf, frame)
            else:
                await self.provider.send_audio(frame)
        if not self._hangup.is_set():
            self._hangup_reason = "remote"
            self._hangup.set()

    async def _pump_owner_rtp(self) -> None:
        while not self._hangup.is_set():
            await self._owner_streaming.wait()
            if self._hangup.is_set():
                return
            generation = self._owner_generation
            ended = False
            try:
                async for frame in self.manager.sip.audio_in(self._owner_leg_id):
                    if self._hangup.is_set() or generation != self._owner_generation:
                        break
                    self._push_leg(self._owner_buf, frame)
                else:
                    ended = True
            except KeyError:
                ended = True
            except asyncio.CancelledError:
                raise
            if (
                ended
                and generation == self._owner_generation
                and not self._hangup.is_set()
                and (self._merged or self._owner_up)
            ):
                self._hangup_reason = "remote"
                self._hangup.set()
                return
            if generation == self._owner_generation:
                self._owner_streaming.clear()

    async def _pump_voice_to_rtp(self) -> None:
        interval = 0.02
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._hangup.is_set():
            now = loop.time()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
            next_tick += interval
            await self._send_media_tick()

    async def _send_media_tick(self) -> None:
        mode = self._media_mode
        owner_up = self._owner_up
        dtmf = self._dtmf.popleft() if self._dtmf else None
        if mode == "relay" and owner_up:
            if dtmf is not None:
                to_target = dtmf
            else:
                to_target = self._pop_frame(self._owner_buf) or PCMA_SILENCE
            to_owner = self._pop_frame(self._target_buf) or PCMA_SILENCE
            await self.manager.sip.send_audio(self.record.id, to_target)
            await self.manager.sip.send_audio(self._owner_leg_id, to_owner)
            return
        if dtmf is not None:
            await self.manager.sip.send_audio(self.record.id, dtmf)
            if owner_up:
                await self.manager.sip.send_audio(self._owner_leg_id, PCMA_SILENCE)
            return
        # Single-leg path: one 20 ms frame, silence when the model is quiet.
        # No steady-state prebuffer. A parked owner leg only gets comfort silence.
        voice = self._pop_voice_frame() or PCMA_SILENCE
        if mode == "context_owner" and owner_up:
            await self.manager.sip.send_audio(self._owner_leg_id, voice)
            await self.manager.sip.send_audio(self.record.id, PCMA_SILENCE)
            return
        await self.manager.sip.send_audio(self.record.id, voice)
        if owner_up:
            await self.manager.sip.send_audio(self._owner_leg_id, PCMA_SILENCE)

    def _pop_voice_frame(self) -> bytes | None:
        while len(self._play_buf) < PCMA_FRAME_BYTES:
            try:
                chunk = self._downlink.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._play_buf.extend(chunk)
        if len(self._play_buf) < PCMA_FRAME_BYTES:
            return None
        frame = bytes(self._play_buf[:PCMA_FRAME_BYTES])
        del self._play_buf[:PCMA_FRAME_BYTES]
        return frame

    def _push_leg(self, buf: bytearray, frame: bytes) -> None:
        buf.extend(frame)
        cap = 2 * PCMA_FRAME_BYTES
        if len(buf) > cap:
            del buf[: len(buf) - cap]

    def _pop_frame(self, buf: bytearray) -> bytes | None:
        if len(buf) < PCMA_FRAME_BYTES:
            return None
        frame = bytes(buf[:PCMA_FRAME_BYTES])
        del buf[:PCMA_FRAME_BYTES]
        return frame

    async def _consume_voice_events(self) -> None:
        async for event in self.provider.events():
            if voice_event_is_activity(event):
                self._mark_activity()
            if event.type == "audio.out":
                if self._media_mode == "relay":
                    continue
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
                await self._dispatch_tool_call(event.payload)
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

    def _cancel_pending_orchestrator(self) -> None:
        for pending in list(self.record.pending_tools.values()):
            if not pending.future.done():
                pending.future.cancel()
        for task in list(self._orchestrator_tasks):
            task.cancel()

    async def _dispatch_tool_call(self, payload: dict[str, Any]) -> None:
        """Hangup must not wait behind ask_orchestrator (Live can emit both).

        Handoff tools run off this consumer so their audio can drain on the RTP tick.
        A future `tool_route` from PRs #12/#13 should keep these names local, next to hangup.
        """
        tool = str(payload.get("tool") or "")
        if tool == "hangup":
            self._cancel_pending_orchestrator()
            await self._on_tool_call(payload)
            return
        if tool in HANDOFF_TOOL_NAMES or (tool_route(tool) == "local" and tool != "hangup"):
            task = asyncio.create_task(self._run_local_tool(payload), name=f"tool-{tool}")
            self._local_tool_tasks.add(task)
            task.add_done_callback(self._local_tool_tasks.discard)
            return
        if tool_route(tool) == "orchestrator":
            task = asyncio.create_task(self._on_tool_call(payload), name="ask-orchestrator")
            self._orchestrator_tasks.add(task)
            task.add_done_callback(self._orchestrator_tasks.discard)
            return
        await self._on_tool_call(payload)

    async def _run_local_tool(self, payload: dict[str, Any]) -> None:
        try:
            await self._on_tool_call(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "local tool %s failed for call %s",
                payload.get("tool"),
                self.record.id,
            )

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
            if self._hangup.is_set():
                return
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
        if tool in HANDOFF_TOOL_NAMES:
            await self._on_handoff_tool(tool, tool_call_id, arguments)
            return
        if tool_route(tool) == "orchestrator":
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
            except asyncio.CancelledError:
                self.record.pending_tools.pop(tool_call_id, None)
                return
            self.record.pending_tools.pop(tool_call_id, None)
            if self._hangup.is_set():
                return
            if self.record.state == "waiting_orchestrator":
                self.record.state = "bridged"
            await self.provider.submit_tool_result(tool_call_id, result, continue_response=True)
            return
        await self.provider.submit_tool_result(
            tool_call_id,
            {"ok": False, "error": f"unknown tool {tool}"},
            continue_response=True,
        )

    async def _on_handoff_tool(
        self,
        tool: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> None:
        if tool == "dial_digits":
            await self._tool_dial_digits(tool_call_id, arguments)
            return
        if tool == "human_answered":
            await self._tool_human_answered(tool_call_id, arguments)
            return
        if tool == "bring_in_owner":
            await self._tool_bring_in_owner(tool_call_id, arguments)

    async def _tool_result(
        self,
        tool_call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool,
    ) -> None:
        try:
            await self.provider.submit_tool_result(
                tool_call_id,
                output,
                continue_response=continue_response,
            )
        except Exception:
            log.warning("tool result failed for %s", tool_call_id)

    def _emit_handoff(self, stage: str, **extra: Any) -> None:
        if stage != "dtmf":
            self.record.handoff_stage = stage
        payload: dict[str, Any] = {"stage": stage}
        if self._handoff is not None:
            payload["owner_name"] = self._handoff.owner_name
        payload.update(extra)
        self.manager._emit(self.record, "handoff", payload)

    async def _tool_dial_digits(self, tool_call_id: str, arguments: dict[str, Any]) -> None:
        if self._handoff is None:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": "handoff_disabled"},
                continue_response=True,
            )
            return
        if self._merged:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": "already_merged"},
                continue_response=False,
            )
            return
        digits = "".join(str(arguments.get("digits") or "").split())
        try:
            frames = dtmf_frames(digits)
        except ValueError as exc:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": str(exc)},
                continue_response=True,
            )
            return
        if len(self._dtmf) + len(frames) > 500:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": "too_many_digits"},
                continue_response=True,
            )
            return
        self._dtmf.extend(frames)
        self._emit_handoff("dtmf", digits=digits)
        await self._tool_result(
            tool_call_id,
            {
                "ok": True,
                "digits": digits,
                "instruction": "Digits are playing. Keep listening.",
            },
            continue_response=True,
        )

    async def _tool_human_answered(self, tool_call_id: str, arguments: dict[str, Any]) -> None:
        if self._handoff is None:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": "handoff_disabled"},
                continue_response=True,
            )
            return
        await self._tool_result(
            tool_call_id,
            {
                "ok": True,
                "action": "connecting",
                "instruction": "Stay silent. The bridge is connecting the owner. Do not speak.",
            },
            continue_response=False,
        )
        async with self._handoff_lock:
            if self._hangup.is_set() or self._merged or self._human_ready:
                return
            self._human_ready = True
            self._emit_handoff(
                "human_answered",
                summary=str(arguments.get("summary") or ""),
                party=str(arguments.get("party") or ""),
            )
            overlap: asyncio.Task[bool] | None = None
            if self._handoff.lead_seconds > 0 and not self._owner_up:
                overlap = asyncio.create_task(self._ensure_owner_dialed(), name="owner-lead")
            try:
                if not self._announced:
                    self._announced = True
                    self._media_mode = "voice"
                    self._emit_handoff("announcing", text=self._handoff.connect_line)
                    await self._speak_and_wait(self._handoff.connect_line)
                if self._hangup.is_set():
                    return
                ok = await overlap if overlap is not None else await self._ensure_owner_dialed()
            except asyncio.CancelledError:
                if overlap is not None and not overlap.done():
                    overlap.cancel()
                raise
            if not ok:
                await self._finish_owner_unavailable()
                return
            await self._merge_humans()

    async def _tool_bring_in_owner(self, tool_call_id: str, arguments: dict[str, Any]) -> None:
        if self._handoff is None:
            await self._tool_result(
                tool_call_id,
                {"ok": False, "error": "handoff_disabled"},
                continue_response=True,
            )
            return
        async with self._handoff_lock:
            if self._hangup.is_set() or self._merged:
                await self._tool_result(
                    tool_call_id,
                    {"ok": True, "duplicate": True},
                    continue_response=False,
                )
                return
            if not self._human_ready and self._handoff.lead_seconds <= 0:
                await self._tool_result(
                    tool_call_id,
                    {
                        "ok": False,
                        "error": "lead_disabled",
                        "instruction": (
                            "Wait until a live person is on the line, then call human_answered."
                        ),
                    },
                    continue_response=True,
                )
                return
            await self._tool_result(
                tool_call_id,
                {"ok": True, "instruction": "Stay silent while the owner is called."},
                continue_response=False,
            )
            self._emit_handoff("bring_in_owner", reason=str(arguments.get("reason") or ""))
            ok = await self._ensure_owner_dialed()
            if self._hangup.is_set():
                return
            if not ok:
                if self._human_ready:
                    await self._finish_owner_unavailable()
                else:
                    self._emit_handoff("owner_unavailable", action="keep_holding")
                    with suppress(Exception):
                        await self.provider.inject_guideline(
                            "The owner did not answer. Keep waiting for a live person. "
                            "Do not invent that they are on the line."
                        )
                return
            if self._human_ready:
                if not self._announced:
                    self._announced = True
                    self._media_mode = "voice"
                    self._emit_handoff("announcing", text=self._handoff.connect_line)
                    await self._speak_and_wait(self._handoff.connect_line)
                await self._merge_humans()
            else:
                await self._park_owner()

    async def _ensure_owner_dialed(self) -> bool:
        if self._owner_up:
            return True
        if self._handoff is None:
            return False
        task = self._dial_task
        if task is not None and not task.done():
            return await task
        if self._owner_attempts >= 1 + self._handoff.owner_retries:
            return False
        self._dial_task = asyncio.create_task(
            self._dial_owner_with_retries(),
            name="owner-dial",
        )
        try:
            return await self._dial_task
        except asyncio.CancelledError:
            if self._dial_task is not None and not self._dial_task.done():
                self._dial_task.cancel()
            raise

    async def _dial_owner_with_retries(self) -> bool:
        if self._handoff is None:
            return False
        limit = 1 + self._handoff.owner_retries
        while self._owner_attempts < limit:
            if self._hangup.is_set():
                return False
            self._owner_attempts += 1
            if self._owner_attempts > 1:
                self._emit_handoff("owner_retry", attempt=self._owner_attempts)
            if await self._dial_owner_once(self._owner_attempts):
                return True
        return False

    async def _dial_owner_once(self, attempt: int) -> bool:
        if self._handoff is None:
            return False
        self._owner_generation += 1
        generation = self._owner_generation
        self._owner_streaming.clear()
        self._owner_up = False
        with suppress(Exception):
            await self.manager.sip.hangup(self._owner_leg_id, reason="redial")
        if self._hangup.is_set():
            return False
        self._emit_handoff("dialing_owner", attempt=attempt)
        try:
            await asyncio.wait_for(
                self.manager.sip.invite(self._handoff.to, self._owner_leg_id),
                timeout=self._handoff.owner_ring_seconds,
            )
        except asyncio.CancelledError:
            with suppress(Exception):
                await self.manager.sip.hangup(self._owner_leg_id, reason="cancelled")
            raise
        except Exception as exc:
            log.info("owner invite failed for call %s: %s", self.record.id, exc)
            with suppress(Exception):
                await self.manager.sip.hangup(self._owner_leg_id, reason="no_answer")
            self._emit_handoff("owner_no_answer", attempt=attempt, error=str(exc)[:200])
            return False
        if generation != self._owner_generation or self._hangup.is_set():
            return False
        self._owner_buf.clear()
        self._owner_up = True
        self._owner_streaming.set()
        self._emit_handoff("owner_answered", attempt=attempt)
        return True

    async def _park_owner(self) -> None:
        if self._owner_parked or not self._owner_up or self._handoff is None:
            return
        self._owner_parked = True
        self._media_mode = "context_owner"
        self._emit_handoff("owner_parked")
        self._flush_downlink()
        await self._speak_and_wait(self._handoff.park_line)
        if not self._human_ready and not self._merged and not self._hangup.is_set():
            self._media_mode = "voice"

    async def _merge_humans(self) -> None:
        if self._merged or not self._owner_up or self._handoff is None or self._hangup.is_set():
            return
        self._emit_handoff("owner_context", text=self._handoff.context)
        self._media_mode = "context_owner"
        self._flush_downlink()
        await self._speak_and_wait(self._handoff.context)
        if self._hangup.is_set() or not self._owner_up:
            return
        self._flush_downlink()
        self._target_buf.clear()
        self._owner_buf.clear()
        self._media_mode = "relay"
        self._merged = True
        self._suppress_idle = True
        self._emit_handoff("merged")
        with suppress(Exception):
            await self.provider.inject_guideline(
                "The owner and the other person are now connected. You are muted. "
                "Do not speak. Do not call tools."
            )

    async def _finish_owner_unavailable(self) -> None:
        if self._owner_gave_up or self._handoff is None:
            return
        self._owner_gave_up = True
        self._owner_up = False
        self._owner_streaming.clear()
        self._media_mode = "voice"
        action = self._handoff.on_unavailable
        self._emit_handoff("owner_unavailable", action=action)
        if action == "hangup":
            await self._speak_and_wait(self._handoff.apology_hangup)
            if not self._hangup.is_set():
                self._hangup_reason = "owner_unavailable"
                self.record.hangup_reason = self._hangup_reason
                self._hangup.set()
            return
        self._suppress_idle = False
        self._mark_activity()
        await self._speak_and_wait(self._handoff.apology_tell)
        with suppress(Exception):
            await self.provider.inject_guideline(
                "The owner did not answer. Continue this call yourself. "
                "Do not claim the owner is on the line."
            )

    async def _speak_and_wait(self, text: str) -> None:
        if not text or self._hangup.is_set():
            return
        self._flush_downlink()
        try:
            await self.provider.speak_verbatim(text, interruptible=False)
        except Exception:
            log.exception("handoff speak failed for call %s", self.record.id)
            return
        await self._wait_for_playout()

    async def _wait_for_playout(
        self, *, max_seconds: float = 4.0, idle_grace: float = 0.08
    ) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max_seconds
        saw = False
        quiet_since: float | None = None
        while loop.time() < deadline:
            if self._hangup.is_set():
                return
            queued = (
                not self._downlink.empty()
                or len(self._play_buf) >= PCMA_FRAME_BYTES
                or bool(self._dtmf)
            )
            if queued:
                saw = True
                quiet_since = None
            elif saw:
                if quiet_since is None:
                    quiet_since = loop.time()
                elif loop.time() - quiet_since >= idle_grace:
                    return
            elif loop.time() - started >= 0.12:
                return
            await asyncio.sleep(0.02)

    async def _cancel_background(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        dial = self._dial_task
        if dial is not None and not dial.done():
            dial.cancel()
            tasks.append(dial)
        for task in list(self._local_tool_tasks):
            if not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _teardown(self) -> None:
        reason = self._hangup_reason
        self._cancel_pending_orchestrator()
        await self._cancel_background()
        try:
            await self.manager.sip.hangup(self.record.id, reason=reason)
        except Exception as exc:
            log.warning("sip hangup: %s", exc)
        if self._handoff is not None:
            try:
                await self.manager.sip.hangup(self._owner_leg_id, reason=reason)
            except Exception as exc:
                log.warning("owner sip hangup: %s", exc)
        try:
            await self.provider.close()
        except Exception as exc:
            log.warning("voice close: %s", exc)
        if self.record.state != "failed":
            self.record.state = "ended"
        self.record.hangup_reason = reason
        self.record.ended_at = time.time()
        self.manager._active.pop(self.record.id, None)
        if self._handoff is not None and self.record.handoff_stage != "ended":
            self._emit_handoff("ended", reason=reason)
        self.manager._emit(self.record, "call.state", {"state": self.record.state, "reason": reason})
        self.manager._emit(self.record, "sip.hangup", {"reason": reason})


class _CallFinished(Exception):
    """Ends the TaskGroup without treating hangup as a failure."""
