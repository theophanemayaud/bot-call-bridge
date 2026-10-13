from __future__ import annotations

import asyncio
import base64
import json
import logging
from unittest.mock import MagicMock

import pytest

from call_bridge.config import Settings
from call_bridge.voice.factory import create_voice_provider
from call_bridge.voice.grok import GrokVoiceProvider
from call_bridge.voice.mock import MockVoiceProvider
from call_bridge.voice.openai import (
    LIVE_BACKEND_INSTRUCTIONS,
    OpenAILiveProvider,
    build_live_session_payload,
    compose_live_instructions,
    live_audio_format,
    parse_responses_function_call,
    parse_tool_arguments,
)
from call_bridge.voice.provider import AudioFormat, VoiceAgentProvider, VoiceSessionConfig
from call_bridge.voice.tools import default_call_tools, tool_route, tools_as_openai_functions


def _session_config(**overrides) -> VoiceSessionConfig:
    data = dict(
        instructions="Be brief.",
        voice="cedar",
        language="fr",
        keyterms=[],
        tools=default_call_tools(),
        audio_in=AudioFormat(encoding="pcma", sample_rate_hz=8000),
        audio_out=AudioFormat(encoding="pcma", sample_rate_hz=8000),
    )
    data.update(overrides)
    return VoiceSessionConfig(**data)


def _function_event(
    *,
    name: str,
    call_id: str,
    arguments: dict | str,
    delegation_id: str = "item_1",
    response_id: str | None = None,
) -> dict:
    item: dict = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
    }
    inner: dict = {"type": "response.output_item.done", "item": item}
    if response_id:
        inner["response_id"] = response_id
    return {
        "type": "response.event",
        "delegation_id": delegation_id,
        "event": inner,
    }


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
    assert settings.openai_live_backend_model == "gpt-6-luna"
    assert settings.openai_live_reasoning_effort == "none"
    assert settings.openai_live_service_tier == "auto"
    assert settings.openai_live_max_output_tokens == 256


@pytest.mark.asyncio
async def test_openai_connect_requires_api_key():
    provider = OpenAILiveProvider(Settings(openai_api_key=""))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await provider.connect(_session_config())


def test_openai_session_payload_uses_responses_tools():
    payload = build_live_session_payload(
        _session_config(),
        model="gpt-live-1",
        default_voice="marin",
        backend_model="gpt-6-luna",
        reasoning_effort="none",
        service_tier="auto",
        max_output_tokens=256,
    )
    assert payload["model"] == "gpt-live-1"
    assert "type" not in payload
    assert payload["audio"]["format"] == {"type": "audio/pcma", "rate": 8000}
    assert payload["audio"]["output"]["voice"] == "cedar"
    assert "tools" not in payload
    assert "turn_detection" not in payload
    assert "input" not in payload["audio"]
    delegation = payload["delegation"]
    assert delegation["type"] == "responses"
    responses = delegation["responses"]
    assert responses["model"] == "gpt-6-luna"
    assert responses["tool_choice"] == "required"
    assert responses["parallel_tool_calls"] is False
    assert responses["reasoning"] == {"effort": "none"}
    assert responses["service_tier"] == "auto"
    assert responses["max_output_tokens"] == 256
    assert responses["instructions"] == LIVE_BACKEND_INSTRUCTIONS.strip()
    names = {item["name"] for item in responses["tools"]}
    assert names == {"hangup", "ask_orchestrator"}
    assert "dial_digits" not in names
    ask = next(item for item in responses["tools"] if item["name"] == "ask_orchestrator")
    assert ask["type"] == "function"
    assert "question" in ask["parameters"]["required"]
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
    text = compose_live_instructions(
        _session_config(instructions="Mission: confirm Tuesday.", voice="marin")
    )
    assert "Backchannel policy:" in text
    assert "Interruption policy:" in text
    assert "Delegation policy:" in text
    assert "ask_orchestrator" in text
    assert "hangup" in text
    assert "Mission: confirm Tuesday." in text
    assert "voicemail" in text.lower()
    assert "short message" in text
    assert "delegate so the backend calls hangup" in text
    assert "Do not sit in silence after the greeting." in text
    assert "Never delegate this when the conversation is already finished" in text
    assert "immediately delegate so the backend calls hangup" in text
    assert "Do not go silent while you wait." in text
    assert "do not call any function" in LIVE_BACKEND_INSTRUCTIONS.lower()
    assert "do not ask for confirmation" in LIVE_BACKEND_INSTRUCTIONS.lower()


def test_parse_tool_arguments_json_string_and_object():
    assert parse_tool_arguments('{"question": "Tuesday?"}') == {"question": "Tuesday?"}
    assert parse_tool_arguments({"reason": "completed"}) == {"reason": "completed"}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments(None) == {}
    assert parse_tool_arguments("not-json") == {"_unparsed": "not-json"}
    assert parse_tool_arguments("[1]") == {"_unparsed": "[1]"}


def test_parse_responses_function_call_reads_nested_item():
    event = _function_event(
        name="ask_orchestrator",
        call_id="call_q",
        arguments={"question": "Confirm Tuesday 10:00?"},
        delegation_id="item_orch",
        response_id="resp_1",
    )
    parsed = parse_responses_function_call(event)
    assert parsed is not None
    assert parsed.name == "ask_orchestrator"
    assert parsed.call_id == "call_q"
    assert parsed.arguments == {"question": "Confirm Tuesday 10:00?"}
    assert parsed.delegation_id == "item_orch"
    assert parsed.response_id == "resp_1"


def test_parse_responses_function_call_ignores_arguments_done_and_prose():
    assert (
        parse_responses_function_call(
            {
                "type": "response.event",
                "delegation_id": "item_1",
                "event": {
                    "type": "response.function_call_arguments.done",
                    "arguments": '{"reason":"completed"}',
                },
            }
        )
        is None
    )
    assert (
        parse_responses_function_call(
            {
                "type": "response.event",
                "delegation_id": "item_1",
                "event": {
                    "type": "response.output_item.done",
                    "item": {"type": "message", "content": []},
                },
            }
        )
        is None
    )
    assert parse_responses_function_call({"type": "session.delegation.created"}) is None


@pytest.mark.asyncio
async def test_openai_routes_named_tools_and_keeps_audio_moving():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    audio = base64.b64encode(b"\x2a" * 4).decode("ascii")
    await provider._handle_server_event({"type": "session.started", "session": {"id": "sess_1"}})
    await provider._handle_server_event({"type": "session.output_audio.delta", "delta": audio})
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {
                "id": "item_orch_1",
                "type": "delegation",
                "target": "responses",
                "response_id": "resp_orch",
            },
        }
    )
    await provider._handle_server_event({"type": "session.output_audio.delta", "delta": audio})
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_orch",
            arguments={"question": "Which day?"},
            delegation_id="item_orch_1",
        )
    )
    await provider._handle_server_event(
        {"type": "session.output_transcript.delta", "delta": "Thanks. Take care. Bye."}
    )
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_hang_1", "type": "delegation", "target": "responses"},
        }
    )
    await provider._handle_server_event(
        _function_event(
            name="hangup",
            call_id="call_hang",
            arguments={"reason": "completed", "summary": "Said goodbye."},
            delegation_id="item_hang_1",
        )
    )
    await provider._handle_server_event({"type": "session.output_audio.delta", "delta": audio})

    seen: list = []
    while not provider._events.empty():
        seen.append(await provider._events.get())

    types = [event.type for event in seen]
    assert types == [
        "session.ready",
        "audio.out",
        "audio.out",
        "tool_call",
        "transcript",
        "tool_call",
        "audio.out",
    ]
    assert seen[1].payload["pcm"] == b"\x2a" * 4
    assert seen[3].payload == {
        "tool": "ask_orchestrator",
        "tool_call_id": "call_orch",
        "arguments": {"question": "Which day?"},
    }
    assert seen[4].payload["role"] == "assistant"
    assert seen[4].payload["text"] == "Thanks. Take care. Bye."
    assert seen[5].payload["tool"] == "hangup"
    assert seen[5].payload["tool_call_id"] == "call_hang"
    assert seen[5].payload["arguments"]["reason"] == "completed"


@pytest.mark.asyncio
async def test_spoken_goodbye_does_not_emit_hangup():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event(
        {"type": "session.output_transcript.delta", "delta": "Bye, I'll hang up now."}
    )
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_only", "type": "delegation", "target": "responses"},
        }
    )
    await asyncio.sleep(0.05)
    seen: list = []
    while not provider._events.empty():
        seen.append(await provider._events.get())
    assert [event.type for event in seen] == ["transcript"]
    assert all(event.type != "tool_call" for event in seen)


@pytest.mark.asyncio
async def test_arguments_done_and_empty_completion_do_not_guess_a_tool(caplog):
    caplog.set_level(logging.WARNING, logger="call_bridge.voice.openai")
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_empty", "target": "responses"},
        }
    )
    await provider._handle_server_event(
        {
            "type": "response.event",
            "delegation_id": "item_empty",
            "event": {
                "type": "response.function_call_arguments.done",
                "arguments": '{"reason":"completed"}',
            },
        }
    )
    await provider._handle_server_event(
        {
            "type": "response.event",
            "delegation_id": "item_empty",
            "event": {"type": "response.completed", "response": {"id": "resp_empty", "output": []}},
        }
    )
    assert provider._events.empty()
    assert "no function call" in caplog.text


@pytest.mark.asyncio
async def test_duplicate_function_call_is_ignored():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    event = _function_event(
        name="hangup",
        call_id="call_once",
        arguments={"reason": "completed"},
        delegation_id="item_h",
    )
    await provider._handle_server_event(event)
    await provider._handle_server_event(event)
    tools = []
    while not provider._events.empty():
        item = await provider._events.get()
        if item.type == "tool_call":
            tools.append(item)
    assert len(tools) == 1


@pytest.mark.asyncio
async def test_delegation_to_tool_delay_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="call_bridge.voice.openai")
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {"id": "item_timed", "target": "responses", "response_id": "resp_t"},
        }
    )
    await asyncio.sleep(0.02)
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_timed",
            arguments={"question": "What time?"},
            delegation_id="item_timed",
        )
    )
    matched = [
        record
        for record in caplog.records
        if record.name == "call_bridge.voice.openai" and "delegation_ms=" in record.message
    ]
    assert matched
    message = matched[-1].getMessage()
    assert "ask_orchestrator" in message
    assert "call_id=call_timed" in message
    assert "delegation_id=item_timed" in message
    millis = message.split("delegation_ms=", 1)[1]
    assert millis != "na"
    assert int(millis) >= 0


@pytest.mark.asyncio
async def test_openai_input_transcript_emits_speech_started():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    await provider._handle_server_event({"type": "session.input_transcript.delta", "delta": "allo"})
    await provider._handle_server_event({"type": "session.input_transcript.delta", "delta": " ?"})
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
async def test_openai_tool_results_continue_the_responses_backend():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_a",
            arguments={"question": "Tuesday?"},
            delegation_id="item_a",
        )
    )
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_b",
            arguments={"question": "Thursday?"},
            delegation_id="item_b",
        )
    )
    await _drain_side_tasks(provider)
    await provider.speak_verbatim("Bonjour.", interruptible=False)
    await provider.inject_guideline("Offer Thursday 14:00.")
    await provider.submit_tool_result(
        "call_a",
        {"answer": "Confirm Tuesday 10:00."},
        continue_response=True,
    )
    await provider.submit_tool_result(
        "call_b",
        {"ok": False, "error": "orchestrator_timeout", "instruction": "Use a fallback."},
        continue_response=True,
    )
    await provider.submit_tool_result(
        "call_missing",
        {"ok": True, "hanging_up": True},
        continue_response=False,
    )

    instructions = [item for item in sent if item["type"] == "session.instructions.append"]
    assert instructions[0]["delegation_id"] is None
    assert "Bonjour." in instructions[0]["content"]
    assert "ORCHESTRATOR STEER" in instructions[1]["content"]
    outputs = [item for item in sent if item["type"] == "response.item.create"]
    assert [item["item"]["call_id"] for item in outputs] == ["call_a", "call_b"]
    assert json.loads(outputs[0]["item"]["output"])["answer"] == "Confirm Tuesday 10:00."
    creates = [item for item in sent if item["type"] == "response.create"]
    assert len(creates) == 2
    spoken = [
        item["content"]
        for item in sent
        if item["type"] == "session.commentary.append" and "Confirm Tuesday" in item["content"]
    ]
    assert spoken
    assert all(item.get("delegation_id") is None for item in sent if item["type"] == "session.commentary.append")
    updates = [item for item in sent if item["type"] == "session.update"]
    assert updates
    assert updates[0]["session"]["delegation"]["responses"]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_hangup_result_does_not_continue_the_backend():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    await provider._handle_server_event(
        _function_event(
            name="hangup",
            call_id="call_hang",
            arguments={"reason": "completed"},
            delegation_id="item_hang",
        )
    )
    await provider.submit_tool_result(
        "call_hang",
        {"ok": True, "hanging_up": True},
        continue_response=False,
    )
    assert [item["type"] for item in sent] == ["response.item.create"]
    assert sent[0]["item"]["type"] == "function_call_output"
    assert sent[0]["item"]["call_id"] == "call_hang"


@pytest.mark.asyncio
async def test_openai_send_audio_live_shape():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    provider._ready = loop.create_future()
    provider._ready.set_result({"type": "session.started"})

    frame = b"\xd5" * 160
    await provider.send_audio(frame)
    assert sent[0]["type"] == "session.input_audio.append"
    assert sent[0]["audio"] == base64.b64encode(frame).decode("ascii")


async def _drain_side_tasks(provider: OpenAILiveProvider) -> None:
    while provider._side_tasks:
        await asyncio.gather(*list(provider._side_tasks))


@pytest.mark.asyncio
async def test_answered_delegation_is_spoken_and_repeat_is_not_forwarded():
    """Same shape as a live call: one delegation, an orchestrator answer, then another ask."""
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    delegation_id = "item_same"
    await provider._handle_server_event(
        {
            "type": "session.delegation.created",
            "delegation": {
                "id": delegation_id,
                "type": "delegation",
                "target": "responses",
                "response_id": "resp_same",
            },
        }
    )
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_1",
            arguments={"question": "What is on the calendar tomorrow?", "urgency": "normal"},
            delegation_id=delegation_id,
        )
    )
    await _drain_side_tasks(provider)
    tools = []
    while not provider._events.empty():
        event = await provider._events.get()
        if event.type == "tool_call":
            tools.append(event)
    assert len(tools) == 1
    assert tools[0].payload["tool_call_id"] == "call_1"
    assert tools[0].payload["arguments"]["question"] == "What is on the calendar tomorrow?"
    checking = [
        item
        for item in sent
        if item["type"] == "session.commentary.append" and "checking" in item["content"]
    ]
    assert len(checking) == 1
    assert checking[0]["delegation_id"] is None

    sent.clear()
    await provider.submit_tool_result(
        "call_1",
        {"answer": "Nothing is booked tomorrow. Then say goodbye and hang up."},
        continue_response=True,
    )
    assert any(
        item["type"] == "session.commentary.append" and "Nothing is booked tomorrow" in item["content"]
        for item in sent
    )
    assert any(
        item["type"] == "session.update"
        and item["session"]["delegation"]["responses"]["tool_choice"] == "none"
        for item in sent
    )
    assert any(
        item["type"] == "response.item.create" and item["item"]["call_id"] == "call_1"
        for item in sent
    )
    assert any(item["type"] == "response.create" for item in sent)
    # Speech is requested before the backend is continued, so the line is not
    # waiting on another model pass.
    kinds = [item["type"] for item in sent]
    assert kinds.index("session.commentary.append") < kinds.index("response.create")

    sent.clear()
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_2",
            arguments={"question": "Please confirm I can say that now.", "urgency": "normal"},
            delegation_id=delegation_id,
        )
    )
    await _drain_side_tasks(provider)
    assert provider._events.empty()
    repeat_outputs = [
        item for item in sent if item["type"] == "response.item.create" and item["item"]["call_id"] == "call_2"
    ]
    assert len(repeat_outputs) == 1
    body = json.loads(repeat_outputs[0]["item"]["output"])
    assert body["already_answered"] is True
    assert "Nothing is booked tomorrow" in body["answer"]
    assert any(item["type"] == "response.create" for item in sent)

    sent.clear()
    await provider._handle_server_event(
        _function_event(
            name="ask_orchestrator",
            call_id="call_3",
            arguments={"question": "The last check timed out. What should I say?", "urgency": "normal"},
            delegation_id=delegation_id,
        )
    )
    await _drain_side_tasks(provider)
    assert provider._events.empty()
    third = [
        item for item in sent if item["type"] == "response.item.create" and item["item"]["call_id"] == "call_3"
    ]
    assert len(third) == 1
    assert not any(item["type"] == "response.create" for item in sent)

    await provider._handle_server_event(
        {
            "type": "response.event",
            "delegation_id": delegation_id,
            "event": {"type": "response.completed", "response": {"id": "resp_same", "output": []}},
        }
    )
    await _drain_side_tasks(provider)
    assert any(
        item["type"] == "session.update"
        and item["session"]["delegation"]["responses"]["tool_choice"] == "required"
        for item in sent
    )


@pytest.mark.asyncio
async def test_backend_failure_is_an_error_and_a_spoken_fallback():
    provider = OpenAILiveProvider(Settings(openai_api_key="test-key"))
    sent: list[dict] = []

    async def fake_send(payload: dict) -> None:
        sent.append(payload)

    provider._ws = MagicMock()
    provider._send = fake_send  # type: ignore[method-assign]
    provider._tool_choice = "none"
    await provider._handle_server_event(
        {
            "type": "response.event",
            "delegation_id": "item_fail",
            "event": {
                "type": "response.failed",
                "response": {"status": "failed", "error": {"message": "backend unavailable"}},
            },
        }
    )
    await _drain_side_tasks(provider)
    error = await provider._events.get()
    assert error.type == "error"
    assert error.payload["message"] == "backend unavailable"
    assert any(
        item["type"] == "session.commentary.append" and "could not check" in item["content"]
        for item in sent
    )
    assert any(
        item["type"] == "session.update"
        and item["session"]["delegation"]["responses"]["tool_choice"] == "required"
        for item in sent
    )
    sent.clear()
    await provider._handle_server_event(
        {
            "type": "response.event",
            "delegation_id": "item_fail",
            "event": {"type": "response.incomplete", "response": {"status": "incomplete"}},
        }
    )
    await _drain_side_tasks(provider)
    again = await provider._events.get()
    assert again.type == "error"
    assert not any(item["type"] == "session.commentary.append" for item in sent)


def test_shared_function_tool_shape_and_routes():
    encoded = tools_as_openai_functions(default_call_tools())
    names = {item["name"] for item in encoded}
    assert names == {"hangup", "ask_orchestrator"}
    assert all(item["type"] == "function" for item in encoded)
    assert tool_route("hangup") == "local"
    assert tool_route("ask_orchestrator") == "orchestrator"
    assert tool_route("dial_digits") == "unknown"


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
