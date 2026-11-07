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
from call_bridge.handoff import HandoffPolicy, resolve_handoff
from call_bridge.script import CallScript
from call_bridge.sip.mock import MockInvitePlan, MockSipEndpoint
from call_bridge.sip.protocol import PCMA_FRAME_BYTES
from call_bridge.voice.provider import VoiceAgentProvider, VoiceEvent, VoiceSessionConfig
from call_bridge.voice.tools import handoff_call_tools, tool_route

SAMPLE = json.loads(Path("examples/script.sample.json").read_text())
HANDOFF_SAMPLE = json.loads(Path("examples/script.handoff.json").read_text())
OWNER = "+15550001111"
MARKER = bytes([0x11]) * PCMA_FRAME_BYTES


def _script(**handoff: Any) -> CallScript:
    body = json.loads(json.dumps(SAMPLE))
    body["handoff"] = handoff
    body["voice"]["max_duration_seconds"] = 60
    return CallScript.model_validate(body)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "bridge_mode": "mock",
        "call_idle_timeout_seconds": 0,
        "hangup_grace_seconds": 0,
        "handoff_owner_number": OWNER,
        "handoff_owner_name": "the owner",
        "handoff_max_hold_seconds": 30,
        "handoff_owner_ring_seconds": 2,
        "handoff_owner_retries": 1,
        "handoff_lead_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


class ScriptedVoice(VoiceAgentProvider):
    """Voice double. speak_verbatim can block so tests can see overlapping work."""

    def __init__(self, *, gate_speak: bool = False) -> None:
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self.config: VoiceSessionConfig | None = None
        self.spoken: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.guidelines: list[str] = []
        self.audio_frames = 0
        self.gate_speak = gate_speak
        self.speak_started = asyncio.Event()
        self.release_speak = asyncio.Event()

    @property
    def name(self) -> str:
        return "mock"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.config = config
        await self._events.put(VoiceEvent(type="session.ready", payload={"provider": "scripted"}))

    async def close(self) -> None:
        self.release_speak.set()
        await self._events.put(VoiceEvent(type="closed"))

    async def send_audio(self, frame: bytes) -> None:
        if frame:
            self.audio_frames += 1

    async def inject_guideline(self, text: str) -> None:
        self.guidelines.append(text)

    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        self.spoken.append(text)
        self.speak_started.set()
        if self.gate_speak:
            await self.release_speak.wait()
        await self._events.put(
            VoiceEvent(type="transcript", payload={"role": "assistant", "text": text, "final": True})
        )
        await self._events.put(VoiceEvent(type="audio.out", payload={"pcm": bytes([0xD5]) * 160 * 2}))

    async def submit_tool_result(
        self,
        call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
    ) -> None:
        self.results.append({"call_id": call_id, "output": output, "continue": continue_response})

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

    def push(self, event: VoiceEvent) -> None:
        self._events.put_nowait(event)


def _stages(manager, call_id: str) -> list[str]:
    return [
        event.payload["stage"]
        for event in manager.bus.history(call_id)
        if event.type == "handoff"
    ]


async def _wait_stage(record, stage: str, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if record.handoff_stage == stage or record.state in {"ended", "failed"}:
            if record.handoff_stage == stage or stage in {"ended", "hold_timeout", "owner_unavailable"}:
                if record.handoff_stage == stage:
                    return
                if record.state in {"ended", "failed"} and stage == "ended":
                    return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for handoff stage {stage}; "
        f"got {record.handoff_stage} state={record.state} reason={record.hangup_reason}"
    )


async def _start(settings: Settings, script: CallScript, voice: ScriptedVoice, sip: MockSipEndpoint):
    from call_bridge.session import CallManager

    await sip.start()
    manager = CallManager(settings, sip, EventBus())
    with patch("call_bridge.session.create_voice_provider", return_value=voice):
        record = await manager.start_call(script, provider_name="mock")
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        if record.state in {"bridged", "ended", "failed"}:
            break
        await asyncio.sleep(0.02)
    return manager, record


def test_resolve_prefers_script_number_over_env():
    settings = _settings()
    policy = resolve_handoff(
        HandoffPolicy(to="+15550002222", owner_name="Ada"),
        settings,
        language="en",
    )
    assert policy.to == "+15550002222"
    assert policy.owner_name == "Ada"
    assert policy.connect_line.startswith("One moment")
    assert "Ada" in policy.connect_line


def test_resolve_french_defaults_and_env_number():
    settings = _settings(handoff_owner_number="0033123456789")
    policy = resolve_handoff(HandoffPolicy(), settings, language="fr-FR")
    assert policy.to == "0033123456789"
    assert "Un instant" in policy.connect_line
    assert policy.owner_retries == 1


def test_handoff_to_alias_enables_policy():
    script = CallScript.model_validate(
        {
            "call": {"to": "+33XXXXXXXXX", "language": "en"},
            "mission": {
                "disclosure": "I am an automated assistant calling about your account.",
                "goals": ["Connect the owner"],
            },
            "handoff_to": "+1 555 000 1111",
        }
    )
    assert script.requests_handoff()
    assert script.handoff is not None
    assert script.handoff.to == "+15550001111"


def test_sample_instructions_keep_hold_music_hangup_without_handoff():
    text = CallScript.model_validate(SAMPLE).build_instructions()
    assert "If you hear hold music or a voicemail beep" in text
    assert "human_answered" not in text


def test_handoff_instructions_override_hold_music_hangup():
    text = CallScript.model_validate(HANDOFF_SAMPLE).build_instructions()
    assert "human_answered" in text
    assert "dial_digits" in text
    assert "WARM HANDOFF" in text
    assert "If you hear hold music or a voicemail beep" not in text
    assert "Un instant, je vous mets en relation" in text


def test_handoff_tools_are_local_and_absent_from_default_calls():
    names = {tool.name for tool in handoff_call_tools()}
    assert names == {"hangup", "ask_orchestrator", "human_answered", "bring_in_owner", "dial_digits"}
    assert tool_route("human_answered") == "local"
    assert tool_route("dial_digits") == "local"
    assert tool_route("ask_orchestrator") == "orchestrator"
    assert tool_route("nope") == "unknown"


@pytest.mark.asyncio
async def test_human_answered_relays_owner_and_mutes_voice():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    manager, record = await _start(_settings(), _script(), voice, sip)
    try:
        assert record.handoff_stage == "holding"
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "h-1",
                    "arguments": {"summary": "Agent said hello", "party": "support"},
                },
            )
        )
        await _wait_stage(record, "merged")
        stages = _stages(manager, record.id)
        assert stages.index("announcing") < stages.index("dialing_owner")
        assert "owner_answered" in stages
        assert "owner_context" in stages
        assert stages[-1] == "merged" or "merged" in stages
        assert sip.invite_log.count(OWNER) == 1
        assert f"{record.id}:owner" in sip._dialogs
        assert any("Un instant" in line or "One moment" in line for line in voice.spoken)
        assert voice.config is not None
        assert "human_answered" in {tool.name for tool in voice.config.tools}
        before = voice.audio_frames
        await asyncio.sleep(0.08)
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            sip.push_audio(f"{record.id}:owner", MARKER)
            if MARKER in sip.notable_audio(record.id):
                break
            await asyncio.sleep(0.02)
        assert MARKER in sip.notable_audio(record.id)
        after = voice.audio_frames
        await asyncio.sleep(0.12)
        assert voice.audio_frames == after
        assert after - before <= 2
        assert record.state == "bridged"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_lead_starts_owner_during_the_connecting_line():
    sip = MockSipEndpoint()
    voice = ScriptedVoice(gate_speak=True)
    script = _script(lead_seconds=5)
    manager, record = await _start(_settings(), script, voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "lead-1",
                    "arguments": {"summary": "A person just greeted me"},
                },
            )
        )
        await asyncio.wait_for(voice.speak_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert OWNER in sip.invite_log
        voice.release_speak.set()
        await _wait_stage(record, "merged")
    finally:
        voice.release_speak.set()
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_zero_lead_does_not_dial_until_the_line_finishes():
    sip = MockSipEndpoint()
    voice = ScriptedVoice(gate_speak=True)
    manager, record = await _start(_settings(), _script(lead_seconds=0), voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "nolead",
                    "arguments": {"summary": "Hello, this is support"},
                },
            )
        )
        await asyncio.wait_for(voice.speak_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert OWNER not in sip.invite_log
        voice.release_speak.set()
        await _wait_stage(record, "merged")
        assert OWNER in sip.invite_log
    finally:
        voice.release_speak.set()
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_bring_in_owner_rejected_without_lead():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    manager, record = await _start(_settings(), _script(lead_seconds=0), voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={"tool": "bring_in_owner", "tool_call_id": "early", "arguments": {}},
            )
        )
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline and not voice.results:
            await asyncio.sleep(0.02)
        assert voice.results
        assert voice.results[-1]["output"]["error"] == "lead_disabled"
        assert OWNER not in sip.invite_log
        assert record.handoff_stage == "holding"
        assert record.state == "bridged"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_early_owner_is_parked_until_the_human_answers():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    script = _script(lead_seconds=8)
    manager, record = await _start(_settings(), script, voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "bring_in_owner",
                    "tool_call_id": "park",
                    "arguments": {"reason": "lead"},
                },
            )
        )
        await _wait_stage(record, "owner_parked")
        assert record.state == "bridged"
        assert "merged" not in _stages(manager, record.id)
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "now",
                    "arguments": {"summary": "The agent picked up"},
                },
            )
        )
        await _wait_stage(record, "merged")
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_owner_no_answer_retries_then_tells_the_agent():
    sip = MockSipEndpoint()
    sip.plan_invite(OWNER, MockInvitePlan(mode="fail", reason="486 Busy Here"))
    voice = ScriptedVoice()
    script = _script(owner_retries=1, on_unavailable="tell", owner_ring_seconds=0.2)
    manager, record = await _start(_settings(), script, voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "miss",
                    "arguments": {"summary": "Agent is here"},
                },
            )
        )
        await _wait_stage(record, "owner_unavailable")
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if any("didn't pick up" in line or "ne décroche pas" in line for line in voice.spoken):
                break
            await asyncio.sleep(0.02)
        assert sip.invite_log.count(OWNER) == 2
        stages = _stages(manager, record.id)
        assert "owner_retry" in stages
        assert record.state == "bridged"
        assert any("didn't pick up" in line or "ne décroche pas" in line for line in voice.spoken)
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_owner_ring_timeout_hangs_up_when_configured():
    sip = MockSipEndpoint()
    sip.plan_invite(OWNER, MockInvitePlan(mode="ring"))
    voice = ScriptedVoice()
    script = _script(owner_retries=0, on_unavailable="hangup", owner_ring_seconds=0.2)
    manager, record = await _start(_settings(), script, voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "human_answered",
                    "tool_call_id": "ring",
                    "arguments": {"summary": "Agent is here"},
                },
            )
        )
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if record.state in {"ended", "failed"}:
                break
            await asyncio.sleep(0.02)
        assert record.state == "ended"
        assert record.hangup_reason == "owner_unavailable"
        assert sip.invite_log.count(OWNER) == 1
        assert f"{record.id}:owner" not in sip._dialogs
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_hold_timeout_hangs_up_without_dialing_the_owner():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    script = _script(max_hold_seconds=0.3)
    manager, record = await _start(_settings(handoff_max_hold_seconds=30), script, voice, sip)
    try:
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if record.state in {"ended", "failed"}:
                break
            await asyncio.sleep(0.02)
        assert record.state == "ended"
        assert record.hangup_reason == "hold_timeout"
        assert "hold_timeout" in _stages(manager, record.id)
        assert OWNER not in sip.invite_log
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_dial_digits_sends_inband_tones_to_the_target():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    manager, record = await _start(_settings(), _script(), voice, sip)
    try:
        voice.push(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": "dial_digits",
                    "tool_call_id": "digits",
                    "arguments": {"digits": "1#"},
                },
            )
        )
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if sip.notable_audio(record.id):
                break
            await asyncio.sleep(0.02)
        assert sip.notable_audio(record.id)
        assert record.state == "bridged"
        assert OWNER not in sip.invite_log
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


@pytest.mark.asyncio
async def test_plain_call_does_not_open_a_second_leg():
    sip = MockSipEndpoint()
    voice = ScriptedVoice()
    script = CallScript.model_validate(SAMPLE)
    manager, record = await _start(_settings(call_idle_timeout_seconds=0), script, voice, sip)
    try:
        await asyncio.sleep(0.2)
        assert record.handoff is None
        assert record.handoff_stage is None
        assert sip.invite_log == [script.call.to]
        assert record.state == "bridged"
    finally:
        if record.state not in {"ended", "failed"}:
            await manager.hangup(record.id, reason="cleanup")


def test_api_rejects_handoff_without_a_number(client):
    body = {"script": _script().model_dump(), "provider": "mock"}
    res = client.post("/v1/calls", json=body)
    assert res.status_code == 409
    assert "owner number" in res.json()["detail"]


def test_api_handoff_action_reaches_merged(client):
    payload = _script(to=OWNER).model_dump()
    res = client.post("/v1/calls", json={"script": payload, "provider": "mock"})
    assert res.status_code == 200, res.text
    call_id = res.json()["id"]
    denied = client.post(
        "/v1/calls/missing/handoff",
        json={"action": "human_answered", "summary": "hello"},
    )
    assert denied.status_code == 404
    started = client.post(
        f"/v1/calls/{call_id}/handoff",
        json={"action": "human_answered", "summary": "The agent said hello"},
    )
    assert started.status_code == 200
    deadline = __import__("time").time() + 4
    stage = None
    while __import__("time").time() < deadline:
        body = client.get(f"/v1/calls/{call_id}").json()
        stage = (body.get("handoff") or {}).get("stage")
        if stage == "merged":
            break
        __import__("time").sleep(0.05)
    assert stage == "merged"
    plain = client.post("/v1/calls", json={"script": SAMPLE, "provider": "mock"})
    assert plain.status_code == 409
    client.post(f"/v1/calls/{call_id}/hangup", json={"reason": "cleanup"})
