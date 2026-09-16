from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from call_bridge.config import Settings
from call_bridge.events import EventBus
from call_bridge.script import CallScript
from call_bridge.session import CallManager, voice_event_is_activity
from call_bridge.sip.mock import MockSipEndpoint
from call_bridge.voice.provider import VoiceAgentProvider, VoiceEvent, VoiceSessionConfig

SAMPLE = json.loads(Path("examples/script.sample.json").read_text())


def test_idle_timeout_default_is_30(monkeypatch):
    monkeypatch.delenv("CALL_IDLE_TIMEOUT_SECONDS", raising=False)
    assert Settings.model_fields["call_idle_timeout_seconds"].default == 30.0
    assert Settings().call_idle_timeout_seconds == 30.0


def test_idle_timeout_from_env(monkeypatch):
    monkeypatch.setenv("CALL_IDLE_TIMEOUT_SECONDS", "0")
    assert Settings().call_idle_timeout_seconds == 0.0


def test_voice_event_is_activity_ignores_empty_and_non_speech():
    assert voice_event_is_activity(VoiceEvent(type="audio.out", payload={"pcm": b"\x00"}))
    assert not voice_event_is_activity(VoiceEvent(type="audio.out", payload={"pcm": b""}))
    assert voice_event_is_activity(VoiceEvent(type="speech.started", payload={}))
    assert voice_event_is_activity(
        VoiceEvent(type="transcript", payload={"role": "user", "text": "allo"})
    )
    assert voice_event_is_activity(
        VoiceEvent(type="transcript", payload={"role": "user", "event": "speech_started"})
    )
    assert not voice_event_is_activity(
        VoiceEvent(type="transcript", payload={"role": "assistant", "text": ""})
    )
    assert not voice_event_is_activity(VoiceEvent(type="tool_call", payload={"tool": "hangup"}))
    assert not voice_event_is_activity(VoiceEvent(type="closed"))


class QuietVoice(VoiceAgentProvider):
    """Voice side that stays silent unless the test pushes an event."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()

    @property
    def name(self) -> str:
        return "mock"

    async def connect(self, config: VoiceSessionConfig) -> None:
        await self._events.put(VoiceEvent(type="session.ready", payload={"provider": "quiet"}))

    async def close(self) -> None:
        await self._events.put(VoiceEvent(type="closed"))

    async def send_audio(self, frame: bytes) -> None:
        return

    async def inject_guideline(self, text: str) -> None:
        return

    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        return

    async def submit_tool_result(
        self,
        call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
    ) -> None:
        return

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

    def push(self, event: VoiceEvent) -> None:
        self._events.put_nowait(event)


async def _wait_record(record, *, states: set[str], timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if record.state in states:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {states}; got {record.state}/{record.hangup_reason}")


async def _start_quiet_call(
    *,
    idle_seconds: float,
    voice: QuietVoice | None = None,
) -> tuple[CallManager, CallScript, QuietVoice, Any]:
    settings = Settings(
        bridge_mode="mock",
        call_idle_timeout_seconds=idle_seconds,
        hangup_grace_seconds=0.0,
    )
    sip = MockSipEndpoint()
    await sip.start()
    bus = EventBus()
    manager = CallManager(settings, sip, bus)
    quiet = voice or QuietVoice()
    script = CallScript.model_validate(SAMPLE)
    with patch("call_bridge.session.create_voice_provider", return_value=quiet):
        record = await manager.start_call(script, provider_name="mock")
    await _wait_record(record, states={"bridged", "ended", "failed"})
    return manager, script, quiet, record


@pytest.mark.asyncio
async def test_idle_timeout_hangs_up_without_voice_activity():
    manager, _script, _voice, record = await _start_quiet_call(idle_seconds=0.25)
    try:
        await _wait_record(record, states={"ended", "failed"}, timeout=2.0)
        assert record.state == "ended"
        assert record.hangup_reason == "idle_timeout"
        ended = [
            event
            for event in manager.bus.history(record.id)
            if event.type == "call.state" and event.payload.get("state") == "ended"
        ]
        assert ended
        assert ended[-1].payload.get("reason") == "idle_timeout"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_voice_activity_resets_idle_timer():
    manager, _script, voice, record = await _start_quiet_call(idle_seconds=0.4)
    try:
        await asyncio.sleep(0.22)
        assert record.state == "bridged"
        voice.push(
            VoiceEvent(
                type="transcript",
                payload={"role": "assistant", "text": "Bonjour, je rappelle.", "final": True},
            )
        )
        await asyncio.sleep(0.25)
        assert record.state == "bridged"
        assert record.hangup_reason is None
        await _wait_record(record, states={"ended", "failed"}, timeout=2.0)
        assert record.state == "ended"
        assert record.hangup_reason == "idle_timeout"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_idle_timeout_disabled_when_zero():
    manager, _script, _voice, record = await _start_quiet_call(idle_seconds=0)
    try:
        await asyncio.sleep(0.4)
        assert record.state == "bridged"
        assert record.hangup_reason is None
        await manager.hangup(record.id, reason="cleanup")
        await _wait_record(record, states={"ended", "failed"})
        assert record.hangup_reason == "cleanup"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_hangup_tool_call_ends_sip_call():
    manager, _script, voice, record = await _start_quiet_call(idle_seconds=0)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "hangup",
                    "tool_call_id": "h1",
                    "arguments": {"reason": "agent", "summary": "Take care, bye"},
                },
            )
        )
        await _wait_record(record, states={"ended", "failed"}, timeout=6.0)
        assert record.state == "ended"
        assert record.hangup_reason == "agent"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_hangup_not_blocked_by_pending_orchestrator():
    manager, _script, voice, record = await _start_quiet_call(idle_seconds=0)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "ask_orchestrator",
                    "tool_call_id": "orch_1",
                    "arguments": {"question": "What next?"},
                },
            )
        )
        await _wait_record(record, states={"waiting_orchestrator"}, timeout=2.0)
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "hangup",
                    "tool_call_id": "h2",
                    "arguments": {"reason": "agent", "summary": "Bye, I'll hang up now."},
                },
            )
        )
        await _wait_record(record, states={"ended", "failed"}, timeout=6.0)
        assert record.state == "ended"
        assert record.hangup_reason == "agent"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")
