from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from call_bridge.config import Settings
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.grok import GrokVoiceProvider
from call_bridge.voice.mock import MockVoiceProvider
from call_bridge.voice.openai import (
    OpenAILiveProvider,
    build_live_session_payload,
    classify_live_delegation,
    compose_live_instructions,
    live_audio_format,
)
from call_bridge.voice.provider import AudioFormat, VoiceAgentProvider, VoiceSessionConfig
from call_bridge.voice.tools import default_call_tools, tools_as_openai_functions


def test_factory_mock_and_grok():
    settings = Settings(bridge_mode="mock")
    assert isinstance(create_voice_provider(settings), MockVoiceProvider)
    assert isinstance(create_voice_provider(settings, name="grok"), GrokVoiceProvider)
    assert isinstance(create_voice_provider(settings, name="openai"), OpenAILiveProvider)


def test_openai_is_provider():
    provider = OpenAILiveProvider(Settings())
    assert isinstance(provider, VoiceAgentProvider)
    assert provider.name == "openai"


def test_openai_live_settings_defaults():
    settings = Settings()
    assert settings.openai_live_url == "wss://api.openai.com/v1/live/sessions"
    assert settings.openai_live_model == "gpt-live-1"
    assert settings.openai_voice == "marin"


@pytest.mark.asyncio
async def test_openai_connect_requires_api_key():
    provider = OpenAILiveProvider(Settings(openai_api_key=""))
    config = VoiceSessionConfig(
        instructions="x",
        voice="marin",
        language="en",
        keyterms=[],
        tools=[],
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await provider.connect(config)


def test_openai_session_payload_pcma_and_client_delegation():
    config = VoiceSessionConfig(
        instructions="Be brief.",
        voice="cedar",
        language="fr",
        keyterms=[],
        tools=default_call_tools(),
        audio_in=AudioFormat(encoding="pcma", sample_rate_hz=8000),
        audio_out=AudioFormat(encoding="pcma", sample_rate_hz=8000),
    )
    payload = build_live_session_payload(
        config,
        model="gpt-live-1",
        default_voice="marin",
    )
    assert payload["model"] == "gpt-live-1"
    assert "type" not in payload
    assert payload["audio"]["format"] == {"type": "audio/pcma", "rate": 8000}
    assert payload["audio"]["output"]["voice"] == "cedar"
    assert payload["delegation"] == {"type": "client"}
    assert "tools" not in payload
    assert "turn_detection" not in payload
    assert "input" not in payload["audio"]
    instructions = payload["instructions"]
    assert "Backchannel policy:" in instructions
    assert "Interruption policy:" in instructions
    assert "Silence and noise policy:" in instructions
    assert "Delegation policy:" in instructions
    assert "Be brief." in instructions


def test_live_audio_format_requires_rate_on_pcma():
    block = live_audio_format(AudioFormat(encoding="pcma", sample_rate_hz=8000))
    assert block == {"type": "audio/pcma", "rate": 8000}


def test_compose_live_instructions_keeps_policy_labels():
    config = VoiceSessionConfig(
        instructions="Mission: confirm Tuesday.",
        voice="marin",
        language="fr",
        keyterms=[],
        tools=[],
    )
    text = compose_live_instructions(config)
    assert "Backchannel policy:" in text
    assert "Interruption policy:" in text
    assert "Delegation policy:" in text
    assert "ask_orchestrator" in text
    assert "hangup" in text
    assert "Mission: confirm Tuesday." in text
    assert "voicemail" in text.lower()
    assert "short message" in text
    assert "delegate hangup" in text
    assert "Do not sit in silence after the greeting." in text


def test_classify_live_delegation_hangup_from_goodbye():
    tool, args = classify_live_delegation(
        last_assistant="Je te laisse, au revoir !",
        recent="user: merci\nassistant: Je te laisse, au revoir !",
    )
    assert tool == "hangup"
    assert args["reason"] == "agent"


def test_classify_live_delegation_defaults_to_orchestrator():
    tool, args = classify_live_delegation(
        last_assistant="Je vérifie ça tout de suite.",
        recent="user: Tuesday or Thursday?",
    )
    assert tool == "ask_orchestrator"
    assert "Tuesday or Thursday?" in args["context"]


@pytest.mark.asyncio
async def test_openai_maps_live_server_events():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event(
        {"type": "session.started", "session": {"id": "sess_1"}}
    )
    await provider._handle_server_event(
        {
            "type": "session.output_audio.delta",
            "delta": base64.b64encode(b"\x00" * 4).decode("ascii"),
        }
    )
    await provider._handle_server_event(
        {"type": "session.output_transcript.delta", "delta": "Je vérifie ça."}
    )
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_orch_1", "type": "delegation", "target": "client"},
        }
    )
    await provider._handle_server_event(
        {"type": "session.output_transcript.delta", "delta": "Je te laisse, au revoir !"}
    )
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_hang_1", "type": "delegation", "target": "client"},
        }
    )

    seen: list = []
    while not provider._events.empty():
        seen.append(await provider._events.get())

    types = [event.type for event in seen]
    assert types == [
        "session.ready",
        "audio.out",
        "transcript",
        "tool_call",
        "transcript",
        "tool_call",
    ]
    assert seen[1].payload["pcm"] == b"\x00" * 4
    assert seen[2].payload == {"role": "assistant", "text": "Je vérifie ça.", "final": False}
    assert seen[3].payload["tool"] == "ask_orchestrator"
    assert seen[3].payload["tool_call_id"] == "item_orch_1"
    assert seen[5].payload["tool"] == "hangup"
    assert seen[5].payload["tool_call_id"] == "item_hang_1"


@pytest.mark.asyncio
async def test_openai_input_transcript_emits_speech_started():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event(
        {"type": "session.input_transcript.delta", "delta": "allo"}
    )
    await provider._handle_server_event(
        {"type": "session.input_transcript.delta", "delta": " ?"}
    )
    first = await provider._events.get()
    second = await provider._events.get()
    third = await provider._events.get()
    assert first.type == "speech.started"
    assert second.type == "transcript"
    assert second.payload["role"] == "user"
    assert second.payload["text"] == "allo"
    assert third.type == "transcript"
    assert third.payload["text"] == " ?"
    assert provider._events.empty()


@pytest.mark.asyncio
async def test_openai_speak_steer_and_tool_result_send_shapes():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    provider._delegations["item_9"] = "ask_orchestrator"

    await provider.speak_verbatim("Bonjour.", interruptible=False)
    await provider.inject_guideline("Offer Thursday 14:00.")
    await provider.submit_tool_result("item_9", {"answer": "Confirm Tuesday 10:00."}, continue_response=True)
    await provider.submit_tool_result("item_gone", {"ok": True, "hanging_up": True}, continue_response=False)

    assert sent[0]["type"] == "session.instructions.append"
    assert sent[0]["delegation_id"] is None
    assert "Bonjour." in sent[0]["content"]
    assert sent[1]["type"] == "session.instructions.append"
    assert "ORCHESTRATOR STEER" in sent[1]["content"]
    assert sent[2]["type"] == "session.commentary.append"
    assert sent[2]["delegation_id"] == "item_9"
    assert sent[2]["content"] == "Confirm Tuesday 10:00."
    assert sent[3]["type"] == "session.instructions.append"
    assert "hanging up" in sent[3]["content"].lower()


@pytest.mark.asyncio
async def test_openai_send_audio_live_shape():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    loop = __import__("asyncio").get_running_loop()
    provider._ready = loop.create_future()
    provider._ready.set_result({"type": "session.started"})

    frame = b"\xd5" * 160
    await provider.send_audio(frame)
    assert sent[0]["type"] == "session.input_audio.append"
    assert sent[0]["audio"] == base64.b64encode(frame).decode("ascii")


def test_shared_function_tool_shape():
    encoded = tools_as_openai_functions(default_call_tools())
    names = {item["name"] for item in encoded}
    assert names == {"hangup", "ask_orchestrator"}
    assert all(item["type"] == "function" for item in encoded)


@pytest.mark.asyncio
async def test_mock_voice_emits_tool_call():
    provider = MockVoiceProvider()
    config = VoiceSessionConfig(
        instructions="test",
        voice="eve",
        language="en",
        keyterms=[],
        tools=default_call_tools(),
        speak_first="I am an AI assistant calling about an appointment.",
    )
    await provider.connect(config)
    seen: list[str] = []
    async for event in provider.events():
        seen.append(event.type)
        if event.type == "tool_call":
            assert event.payload["tool"] == "ask_orchestrator"
            break
        if event.type == "closed":
            break
    await provider.close()
    assert "session.ready" in seen
    assert "tool_call" in seen
