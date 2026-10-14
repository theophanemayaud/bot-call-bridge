from __future__ import annotations

import asyncio
import json

import pytest

from call_bridge.config import Settings
from call_bridge.voice.grok import GrokVoiceProvider, build_grok_session, grok_realtime_url
from call_bridge.voice.provider import AudioFormat, TurnDetection, VoiceSessionConfig
from call_bridge.voice.tools import ORCHESTRATOR_CHECKING_CONTEXT, default_call_tools


def _config(**overrides) -> VoiceSessionConfig:
    base = dict(
        instructions="Be brief.",
        voice="eve",
        language="fr",
        keyterms=["a" * 80, "clinic"],
        tools=default_call_tools(),
        audio_in=AudioFormat(encoding="pcma", sample_rate_hz=8000),
        audio_out=AudioFormat(encoding="pcma", sample_rate_hz=8000),
    )
    base.update(overrides)
    return VoiceSessionConfig(**base)


def test_grok_voice_defaults():
    settings = Settings()
    assert settings.xai_voice_model == "grok-voice-3.0"
    assert settings.xai_reasoning_effort == "none"
    assert settings.xai_transcribe_model == "grok-transcribe"
    assert grok_realtime_url(settings.xai_realtime_url, settings.xai_voice_model).endswith(
        "model=grok-voice-3.0"
    )


def test_grok_session_pins_voice_3_pcma_and_named_tools():
    settings = Settings()
    payload = build_grok_session(_config(), settings)
    assert payload["voice"] == "eve"
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["turn_detection"] == {"type": "server_vad"}
    assert "threshold" not in payload["turn_detection"]
    assert "silence_duration_ms" not in payload["turn_detection"]
    audio_in = payload["audio"]["input"]
    audio_out = payload["audio"]["output"]
    assert audio_in["format"] == {"type": "audio/pcma"}
    assert audio_out["format"] == {"type": "audio/pcma"}
    assert "rate" not in audio_in["format"]
    assert audio_in["transcription"]["model"] == "grok-transcribe"
    assert audio_in["transcription"]["language_hint"] == "fr"
    assert audio_in["transcription"]["keyterms"] == ["a" * 50, "clinic"]
    names = {tool["name"] for tool in payload["tools"]}
    assert names == {"hangup", "ask_orchestrator"}
    assert all(tool["type"] == "function" for tool in payload["tools"])
    ask = next(tool for tool in payload["tools"] if tool["name"] == "ask_orchestrator")
    assert "question" in ask["parameters"]["properties"]
    assert ask["parameters"]["required"] == ["question"]


def test_grok_turn_detection_none_is_null():
    settings = Settings()
    payload = build_grok_session(
        _config(
            turn_detection=TurnDetection(
                kind="none",
                silence_duration_ms=None,
                prefix_padding_ms=None,
            )
        ),
        settings,
    )
    assert payload["turn_detection"] is None


def test_grok_web_search_is_appended_not_a_function():
    settings = Settings()
    payload = build_grok_session(_config(enable_web_search=True), settings)
    assert payload["tools"][-1] == {"type": "web_search"}


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        await self._incoming.put(None)

    def push(self, event: dict) -> None:
        self._incoming.put_nowait(json.dumps(event))

    def __aiter__(self) -> _FakeSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item


@pytest.mark.asyncio
async def test_grok_connect_sends_session_update(monkeypatch):
    holder: dict = {}

    async def fake_connect(url, **kwargs):
        holder["url"] = url
        holder["headers"] = kwargs.get("additional_headers")
        holder["ws"] = _FakeSocket()
        return holder["ws"]

    monkeypatch.setattr("call_bridge.voice.grok.websockets.connect", fake_connect)
    provider = GrokVoiceProvider(Settings(xai_api_key="test-key"))
    await provider.connect(_config())
    try:
        assert "model=grok-voice-3.0" in holder["url"]
        assert holder["headers"]["Authorization"] == "Bearer test-key"
        assert holder["ws"].sent[0]["type"] == "session.update"
        assert holder["ws"].sent[0]["session"]["reasoning"]["effort"] == "none"
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_grok_function_call_is_a_tool_and_goodbye_text_is_not():
    provider = GrokVoiceProvider(Settings(xai_api_key="test-key"))
    await provider._handle_server_event(
        {
            "type": "response.output_audio_transcript.done",
            "transcript": "Bye, I'll hang up now.",
        }
    )
    await provider._handle_server_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "goodbye",
        }
    )
    await provider._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "ask_orchestrator",
            "call_id": "call_9",
            "arguments": json.dumps({"question": "Which slot?"}),
        }
    )
    await provider._handle_server_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "ask_orchestrator",
            "call_id": "call_9",
            "arguments": json.dumps({"question": "Which slot?"}),
        }
    )
    events = []
    while not provider._events.empty():
        events.append(provider._events.get_nowait())
    assert [event.type for event in events] == ["transcript", "transcript", "tool_call"]
    tool = events[-1].payload
    assert tool["tool"] == "ask_orchestrator"
    assert tool["tool_call_id"] == "call_9"
    assert tool["arguments"] == {"question": "Which slot?"}


@pytest.mark.asyncio
async def test_grok_checking_context_is_one_response_not_history():
    provider = GrokVoiceProvider(Settings(xai_api_key="test-key"))
    socket = _FakeSocket()
    provider._ws = socket
    await provider.offer_checking_context("call_9")
    await provider.offer_checking_context("call_other")
    assert [item["type"] for item in socket.sent] == ["response.create"]
    assert socket.sent[0]["response"]["instructions"] == ORCHESTRATOR_CHECKING_CONTEXT
    assert "force_message" not in json.dumps(socket.sent)
    assert "conversation.item.create" not in json.dumps(socket.sent)

    await provider._handle_server_event({"type": "response.done"})
    provider._settled_calls.add("call_9")
    socket.sent.clear()
    await provider.offer_checking_context("call_9")
    assert socket.sent == []
    await provider.offer_checking_context("call_later")
    assert [item["type"] for item in socket.sent] == ["response.create"]


@pytest.mark.asyncio
async def test_grok_tool_result_waits_out_checking_turn():
    provider = GrokVoiceProvider(Settings(xai_api_key="test-key"))
    socket = _FakeSocket()
    provider._ws = socket
    provider._pending_tool_calls = 1
    await provider.offer_checking_context("call_9")
    socket.sent.clear()
    started = asyncio.create_task(
        provider.submit_tool_result("call_9", {"answer": "Tuesday 10:00"}, continue_response=True)
    )
    await asyncio.sleep(0.05)
    assert not started.done()
    await provider._handle_server_event({"type": "response.done"})
    await asyncio.wait_for(started, timeout=1)
    assert socket.sent[0]["item"]["type"] == "function_call_output"
    assert socket.sent[0]["item"]["call_id"] == "call_9"
    assert socket.sent[-1] == {"type": "response.create"}


@pytest.mark.asyncio
async def test_grok_hangup_result_does_not_continue():
    provider = GrokVoiceProvider(Settings(xai_api_key="test-key"))
    socket = _FakeSocket()
    provider._ws = socket
    provider._pending_tool_calls = 1
    await provider.submit_tool_result(
        "hang_1",
        {"ok": True, "hanging_up": True},
        continue_response=False,
    )
    assert [item["type"] for item in socket.sent] == ["conversation.item.create"]
    assert socket.sent[0]["item"]["type"] == "function_call_output"
