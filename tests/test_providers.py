from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from call_bridge.config import Settings
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.grok import GrokVoiceProvider
from call_bridge.voice.mock import MockVoiceProvider
from call_bridge.voice.openai import OpenAIRealtimeProvider, build_openai_session_payload
from call_bridge.voice.provider import AudioFormat, TurnDetection, VoiceAgentProvider, VoiceSessionConfig
from call_bridge.voice.tools import default_call_tools, tools_as_openai_functions


def test_factory_mock_and_grok():
    settings = Settings(bridge_mode="mock")
    assert isinstance(create_voice_provider(settings), MockVoiceProvider)
    assert isinstance(create_voice_provider(settings, name="grok"), GrokVoiceProvider)
    assert isinstance(create_voice_provider(settings, name="openai"), OpenAIRealtimeProvider)


def test_openai_is_provider():
    provider = OpenAIRealtimeProvider(Settings())
    assert isinstance(provider, VoiceAgentProvider)
    assert provider.name == "openai"


@pytest.mark.asyncio
async def test_openai_connect_requires_api_key():
    provider = OpenAIRealtimeProvider(Settings(openai_api_key=""))
    config = VoiceSessionConfig(
        instructions="x",
        voice="marin",
        language="en",
        keyterms=[],
        tools=[],
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await provider.connect(config)


def test_openai_session_payload_pcma_and_tools():
    config = VoiceSessionConfig(
        instructions="Be brief.",
        voice="cedar",
        language="fr",
        keyterms=[],
        tools=default_call_tools(),
        audio_in=AudioFormat(encoding="pcma"),
        audio_out=AudioFormat(encoding="pcma"),
        turn_detection=TurnDetection(kind="semantic_vad", eagerness="auto"),
    )
    payload = build_openai_session_payload(
        config,
        model="gpt-realtime",
        default_voice="marin",
        transcribe_model="gpt-4o-mini-transcribe",
    )
    assert payload["type"] == "realtime"
    assert payload["model"] == "gpt-realtime"
    assert payload["output_modalities"] == ["audio"]
    assert payload["audio"]["input"]["format"]["type"] == "audio/pcma"
    assert payload["audio"]["output"]["format"]["type"] == "audio/pcma"
    assert "rate" not in payload["audio"]["input"]["format"]
    assert "rate" not in payload["audio"]["output"]["format"]
    assert payload["audio"]["output"]["voice"] == "cedar"
    assert payload["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert payload["audio"]["input"]["turn_detection"]["eagerness"] == "auto"
    assert payload["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert payload["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    names = {t["name"] for t in payload["tools"]}
    assert names == {"hangup", "ask_orchestrator"}


def test_openai_session_payload_ignores_grok_transcribe():
    config = VoiceSessionConfig(
        instructions="x",
        voice="marin",
        language="en",
        keyterms=[],
        tools=[],
        transcribe_model="grok-transcribe",
    )
    payload = build_openai_session_payload(
        config,
        model="gpt-realtime",
        default_voice="marin",
        transcribe_model=None,
    )
    assert "transcription" not in payload["audio"]["input"]


@pytest.mark.asyncio
async def test_openai_maps_server_events():
    provider = OpenAIRealtimeProvider(Settings(openai_api_key="test-key"))
    # Feed events through the private handler without a live socket.
    await provider._handle_server_event({"type": "session.updated", "session": {"id": "s1"}})
    await provider._handle_server_event(
        {
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(b"\x00" * 4).decode("ascii"),
        }
    )
    await provider._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "hangup",
            "call_id": "call_1",
            "arguments": '{"reason":"completed"}',
        }
    )
    await provider._handle_server_event({"type": "response.done"})

    types = []
    for _ in range(4):
        event = await provider._events.get()
        types.append(event.type)
        if event.type == "audio.out":
            assert event.payload["pcm"] == b"\x00" * 4
        if event.type == "tool_call":
            assert event.payload["tool"] == "hangup"
            assert event.payload["tool_call_id"] == "call_1"
            assert event.payload["arguments"]["reason"] == "completed"
    assert types == ["session.ready", "audio.out", "tool_call", "response.done"]


@pytest.mark.asyncio
async def test_openai_speak_and_tool_result_send_shapes():
    provider = OpenAIRealtimeProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()  # mark connected
    provider._send = fake_send  # type: ignore[method-assign]

    await provider.speak_verbatim("Bonjour.", interruptible=False)
    await provider.submit_tool_result("call_9", {"ok": True}, continue_response=True)

    assert sent[0]["type"] == "response.create"
    assert sent[0]["response"]["input"] == []
    assert "Bonjour." in sent[0]["response"]["instructions"]
    assert sent[1]["type"] == "conversation.item.create"
    assert sent[1]["item"]["type"] == "function_call_output"
    assert sent[1]["item"]["call_id"] == "call_9"
    assert sent[2]["type"] == "response.create"


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
