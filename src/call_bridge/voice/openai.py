from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from call_bridge.config import Settings
from call_bridge.voice.provider import (
    AudioFormat,
    VoiceAgentProvider,
    VoiceEvent,
    VoiceSessionConfig,
)
from call_bridge.voice.tools import (
    ORCHESTRATOR_TOOL_NAMES,
    default_call_tools,
    tools_as_openai_functions,
)

log = logging.getLogger(__name__)

# Live commentary/instructions appends are capped at ~500 tokens.
_APPEND_CHAR_LIMIT = 1800
_CONNECT_TIMEOUT_SECONDS = 15.0
_CLOSE_TIMEOUT_SECONDS = 2.0

_LIVE_INSTRUCTION_PREFIX = """You are a calm, friendly voice agent on a live outbound phone call.
Speak naturally, at an unhurried pace. Be clear and direct. If the callee is frustrated, acknowledge it briefly and focus on the next helpful step.
Prefer the callee's language when they speak; default to the SCRIPT language.

Backchannel policy: Use moderate backchannels (mm-hmm, oui, d'accord, I see). Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the callee interrupts. Listen to what they say.

Silence and noise policy: Keep listening while the callee pauses to think. Do not treat a cough, hold music, a ringtone, or nearby conversation as a new request.

Delegation policy:
A backend selects named tools. You do not call tools yourself. Where the SCRIPT says to hang up or ask the orchestrator, delegate so the backend calls that tool.
Do not mention tools, backend, or delegation to the callee.
- ask_orchestrator: facts, decisions, and next steps you do not have. Delegate as soon as you need them. The orchestrator is checking, and it may take a few seconds. You can let the person on the line know if you want, for example that you are looking it up. Never invent those facts. Never delegate this when the conversation is already finished or you are about to hang up.
- hangup: end the phone call. Speak a short audible goodbye first, then delegate in the same turn so the backend calls hangup.

When the goals are done, the callee says goodbye, or a fallback says to disconnect:
1. Speak a short goodbye (for example "Thanks, take care, bye" / "Je te laisse, au revoir" / "Takk, ha det bra").
2. Then immediately delegate so the backend calls hangup. Do not ask what to do next. Do not wait for another user turn after you have said goodbye.
3. If you say you will hang up, you must delegate in that same turn. Saying goodbye without delegating leaves the callee on the line.

Voicemail: If you reach voicemail or an answering machine, leave a short message (who you are and why), say goodbye, then delegate so the backend calls hangup. Do not sit in silence after the greeting.

Delegate before giving an answer that depends on backend work.
Do not invent facts while waiting.
"""

# Stable prefix so Live can cache the backend prompt across delegations.
LIVE_BACKEND_INSTRUCTIONS = """You select one function for a live phone call. You do not speak to the callee.

If this request has no tool result yet, call exactly one function. Do not invent a function name.
- ask_orchestrator: a fact, decision, or next step is missing. Put it in question. Do not invent the fact.
- hangup: goodbye was already said, the callee asked to stop, or a voicemail was left. Do not use ask_orchestrator to end the call.

If a tool result is already present, do not call any function. Return one short final message the voice model should say. If the result says to hang up, include a short goodbye in that message. Do not ask for confirmation.
If the result has ok=false or error=orchestrator_timeout, the message is that fallback only. Do not invent facts.
"""


def _format_type(fmt: AudioFormat) -> str:
    return {
        "pcma": "audio/pcma",
        "pcmu": "audio/pcmu",
        "pcm16": "audio/pcm",
    }[fmt.encoding]


def live_audio_format(fmt: AudioFormat) -> dict[str, Any]:
    """Shared Live input/output format. G.711 MUST include rate (unlike old Realtime)."""
    return {"type": _format_type(fmt), "rate": fmt.sample_rate_hz}


def compose_live_instructions(config: VoiceSessionConfig) -> str:
    """Short Live policies + SCRIPT mission. Keep policy labels from the Live prompting guide."""
    script = (config.instructions or "").strip()
    if script:
        return f"{_LIVE_INSTRUCTION_PREFIX.rstrip()}\n\nSCRIPT\n{script}"
    return _LIVE_INSTRUCTION_PREFIX.strip()


def build_live_session_payload(
    config: VoiceSessionConfig,
    *,
    model: str,
    default_voice: str,
    backend_model: str,
    reasoning_effort: str,
    service_tier: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    """GPT-Live session.start body (responses delegation, named tools, shared PCMA format)."""
    fmt = config.audio_in
    tools = config.tools or default_call_tools()
    return {
        "model": model,
        "instructions": compose_live_instructions(config),
        "audio": {
            "format": live_audio_format(fmt),
            "output": {"voice": config.voice or default_voice},
        },
        "delegation": {
            "type": "responses",
            "responses": {
                "model": backend_model,
                "instructions": LIVE_BACKEND_INSTRUCTIONS.strip(),
                "tools": tools_as_openai_functions(tools),
                "tool_choice": "required",
                "parallel_tool_calls": False,
                "reasoning": {"effort": reasoning_effort},
                "service_tier": service_tier,
                "max_output_tokens": max_output_tokens,
            },
        },
    }


@dataclass(frozen=True, slots=True)
class ResponsesFunctionCall:
    """One completed function call unwrapped from a Live ``response.event``."""

    name: str
    call_id: str
    arguments: dict[str, Any]
    delegation_id: str | None
    response_id: str | None


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Responses sends ``arguments`` as a JSON string. Accept an object too."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"_unparsed": text}
        if isinstance(parsed, dict):
            return parsed
        return {"_unparsed": text}
    if raw is None:
        return {}
    return {"_unparsed": str(raw)}


def parse_responses_function_call(event: dict[str, Any]) -> ResponsesFunctionCall | None:
    """Return a finished function call nested in ``response.event``.

    Only ``response.output_item.done`` items of type ``function_call`` count.
    ``response.function_call_arguments.done`` has no name or call id, so it is ignored.
    """
    if event.get("type") != "response.event":
        return None
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "response.output_item.done":
        return None
    item = inner.get("item")
    if not isinstance(item, dict):
        item = inner.get("output_item")
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return None
    name = item.get("name")
    call_id = item.get("call_id")
    if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
        return None
    response = inner.get("response") if isinstance(inner.get("response"), dict) else {}
    response_id = inner.get("response_id") or response.get("id") or item.get("response_id")
    delegation_id = event.get("delegation_id")
    if not isinstance(delegation_id, str) or not delegation_id:
        delegation_id = None
    if not isinstance(response_id, str) or not response_id:
        response_id = None
    return ResponsesFunctionCall(
        name=name,
        call_id=call_id,
        arguments=parse_tool_arguments(item.get("arguments")),
        delegation_id=delegation_id,
        response_id=response_id,
    )


def _speakable_tool_output(output: dict[str, Any]) -> str:
    for key in ("answer", "instruction", "summary"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(output, ensure_ascii=False)


def _clip(text: str, limit: int = _APPEND_CHAR_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _backend_failure_message(inner: dict[str, Any]) -> str | None:
    """Terminal Responses events nested in ``response.event``. Empty if this is not one."""
    kind = inner.get("type")
    if kind == "error":
        return _error_message(inner)
    if kind not in {"response.failed", "response.incomplete"}:
        return None
    response = inner.get("response") if isinstance(inner.get("response"), dict) else inner
    error = response.get("error") or response.get("incomplete_details") or kind
    if isinstance(error, dict):
        return str(error.get("message") or error.get("reason") or kind)
    return str(error)


def _error_message(event: dict[str, Any]) -> str:
    err = event.get("error") or event
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


def _loop_time() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


class OpenAILiveProvider(VoiceAgentProvider):
    """OpenAI GPT-Live-1 WebSocket (responses delegation / named tools / PCMA 8 kHz)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ws: ClientConnection | None = None
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[dict[str, Any]] | None = None
        self._server_closed = asyncio.Event()
        self._config: VoiceSessionConfig | None = None
        self._session_id: str | None = None
        self._closing = False
        self._user_speaking = False
        self._seen_call_ids: set[str] = set()
        self._call_names: dict[str, str] = {}
        self._pending_calls: dict[str, str] = {}
        self._groups: dict[str, set[str]] = {}
        self._delegation_started: dict[str, float] = {}
        self._delegation_tool_count: dict[str, int] = {}
        # Last orchestrator output per delegation. A later function call on that
        # same id is settled locally so it cannot sit unanswered.
        self._answered: dict[str, dict[str, Any]] = {}
        self._spoken_for: set[str] = set()
        self._checking_for: set[str] = set()
        self._repeat_continues: dict[str, int] = {}
        self._awaiting_final: set[str] = set()
        self._tool_choice = "required"
        self._failure_spoken: set[str] = set()
        self._side_tasks: set[asyncio.Task[None]] = set()

    @property
    def name(self) -> str:
        return "openai"

    async def connect(self, config: VoiceSessionConfig) -> None:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is empty")
        self._config = config
        self._closing = False
        self._server_closed.clear()
        self._user_speaking = False
        self._seen_call_ids.clear()
        self._call_names.clear()
        self._pending_calls.clear()
        self._groups.clear()
        self._delegation_started.clear()
        self._delegation_tool_count.clear()
        self._answered.clear()
        self._spoken_for.clear()
        self._checking_for.clear()
        self._repeat_continues.clear()
        self._awaiting_final.clear()
        self._tool_choice = "required"
        self._failure_spoken.clear()
        for task in list(self._side_tasks):
            task.cancel()
        self._side_tasks.clear()
        url = self._settings.openai_live_url
        log.info(
            "connecting openai gpt-live %s backend=%s reasoning=%s",
            self._settings.openai_live_model,
            self._settings.openai_live_backend_model,
            self._settings.openai_live_reasoning_effort,
        )
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self._settings.openai_api_key}",
            },
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._reader = asyncio.create_task(self._read_loop(), name="openai-live-reader")
        await self._send(
            {
                "type": "session.start",
                "event_id": _event_id("start"),
                "session": build_live_session_payload(
                    config,
                    model=self._settings.openai_live_model,
                    default_voice=self._settings.openai_voice,
                    backend_model=self._settings.openai_live_backend_model,
                    reasoning_effort=self._settings.openai_live_reasoning_effort,
                    service_tier=self._settings.openai_live_service_tier,
                    max_output_tokens=self._settings.openai_live_max_output_tokens,
                ),
            }
        )
        try:
            started = await asyncio.wait_for(asyncio.shield(self._ready), timeout=_CONNECT_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise RuntimeError("GPT-Live session.started timed out") from exc
        session = started.get("session") or {}
        self._session_id = session.get("id")
        if config.speak_first:
            await self.speak_verbatim(config.speak_first, interruptible=False)

    async def close(self) -> None:
        self._closing = True
        for task in list(self._side_tasks):
            task.cancel()
        if self._ws is not None and self._live_started() and not self._server_closed.is_set():
            try:
                await self._send({"type": "session.close", "event_id": _event_id("close")})
                await asyncio.wait_for(self._server_closed.wait(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        await self._events.put(VoiceEvent(type="closed"))

    async def send_audio(self, frame: bytes) -> None:
        if not frame or self._ws is None or self._closing:
            return
        if not self._live_started():
            return
        await self._send(
            {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }
        )

    async def inject_guideline(self, text: str) -> None:
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        await self._append(
            "session.instructions.append",
            f"[ORCHESTRATOR STEER — do not read this aloud] {text}",
        )

    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        """Request a fixed spoken line. Live has no force_message; instructions.append is the disclosure path."""
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        del interruptible  # Live duplex yield is model + prompt; no interrupt_response flag.
        await self._append(
            "session.instructions.append",
            (
                "Immediately say the following text exactly and in full, then pause and listen. "
                "Do not add a greeting or extra words:\n"
                f"{text}"
            ),
        )

    async def submit_tool_result(
        self,
        call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
    ) -> None:
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        group = self._pending_calls.pop(call_id, None)
        if group is None:
            log.warning("openai live tool result for unknown call_id=%s", call_id)
            return
        pending = self._groups.get(group)
        if pending is not None:
            pending.discard(call_id)
            if not pending:
                self._groups.pop(group, None)
        name = self._call_names.get(call_id, "")
        if continue_response and name in ORCHESTRATOR_TOOL_NAMES:
            # Remember before any await so a repeat on this delegation cannot
            # open another orchestrator wait.
            self._answered[group] = output
            if group not in self._spoken_for:
                self._spoken_for.add(group)
                await self._append(
                    "session.commentary.append",
                    (
                        "Tell the callee the following now, in their language, then pause and listen. "
                        "Do not mention tools:\n"
                        f"{_speakable_tool_output(output)}"
                    ),
                )
            # tool_choice stays "required" on the session so the next delegation
            # still picks a named tool. This continuation must be a final message,
            # otherwise the backend calls ask_orchestrator again and Live stays silent.
            await self._set_tool_choice("none")
        await self._send(
            {
                "type": "response.item.create",
                "event_id": _event_id("tool"),
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                },
            }
        )
        # Hangup must not start another backend turn (that can talk over the goodbye).
        # Other still-open calls on this delegation keep the response parked.
        group_open = bool(self._groups.get(group))
        if not continue_response or group_open:
            return
        self._awaiting_final.add(group)
        await self._send({"type": "response.create", "event_id": _event_id("cont")})

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

    async def _set_tool_choice(self, choice: str) -> None:
        if self._ws is None or self._tool_choice == choice:
            return
        self._tool_choice = choice
        log.info("openai live backend tool_choice=%s", choice)
        await self._send(
            {
                "type": "session.update",
                "event_id": _event_id("upd"),
                "session": {
                    "delegation": {
                        "responses": {"tool_choice": choice},
                    },
                },
            }
        )

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_done)

    def _side_done(self, task: asyncio.Task[None]) -> None:
        self._side_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("openai live side task failed: %s", exc)

    async def _append(self, type_: str, content: str) -> None:
        # Responses-mode updates use a null delegation id. A client delegation id
        # is rejected on this session.
        await self._send(
            {
                "type": type_,
                "event_id": _event_id("append"),
                "delegation_id": None,
                "content": _clip(content),
            }
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        await self._ws.send(json.dumps(payload))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    await self._events.put(VoiceEvent(type="audio.out", payload={"pcm": raw}))
                    continue
                event = json.loads(raw)
                await self._handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("openai live websocket ended: %s", exc)
            self._fail_ready(exc)
            await self._events.put(VoiceEvent(type="error", payload={"message": str(exc)}))
        finally:
            self._server_closed.set()
            await self._events.put(VoiceEvent(type="closed"))

    def _live_started(self) -> bool:
        return (
            self._ready is not None
            and self._ready.done()
            and self._ready.exception() is None
        )

    def _fail_ready(self, exc: BaseException) -> None:
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(exc)

    def _note_delegation(self, event: dict[str, Any]) -> None:
        """Record when Live handed work to the backend. Does not choose a tool."""
        delegation = event.get("delegation") if isinstance(event.get("delegation"), dict) else {}
        delegation_id = delegation.get("id") or event.get("delegation_id")
        if not isinstance(delegation_id, str) or not delegation_id:
            log.warning("openai live delegation missing id: %s", event.get("type"))
            return
        target = delegation.get("target") or event.get("target") or "responses"
        response_id = delegation.get("response_id") or event.get("response_id")
        if delegation_id not in self._delegation_started:
            self._delegation_started[delegation_id] = _loop_time()
            self._delegation_tool_count.setdefault(delegation_id, 0)
            log.info(
                "openai live delegation id=%s target=%s response_id=%s",
                delegation_id,
                target,
                response_id or "",
            )

    def _delegation_ms(self, delegation_id: str | None) -> str:
        if not delegation_id:
            return "na"
        started = self._delegation_started.get(delegation_id)
        if started is None:
            return "na"
        return f"{(_loop_time() - started) * 1000:.0f}"

    async def _emit_tool_call(
        self, tool: str, tool_call_id: str, arguments: dict[str, Any]
    ) -> None:
        await self._events.put(
            VoiceEvent(
                type="tool_call",
                payload={
                    "tool": tool,
                    "tool_call_id": tool_call_id,
                    "arguments": arguments,
                },
            )
        )

    async def _handle_response_event(self, event: dict[str, Any]) -> None:
        """Unwrap one backend event. Never waits on the orchestrator or on audio."""
        call = parse_responses_function_call(event)
        if call is not None:
            await self._emit_function_call(call)
            return
        inner = event.get("event")
        if not isinstance(inner, dict):
            return
        failure = _backend_failure_message(inner)
        if failure:
            delegation_id = event.get("delegation_id")
            if not isinstance(delegation_id, str):
                delegation_id = None
            log.warning(
                "openai live backend failure delegation_id=%s message=%s",
                delegation_id or "",
                failure,
            )
            await self._events.put(VoiceEvent(type="error", payload={"message": failure}))
            key = delegation_id or ""
            if self._ws is not None and key not in self._failure_spoken:
                self._failure_spoken.add(key)
                self._spawn(self._say_backend_failure())
            if self._tool_choice == "none":
                self._spawn(self._set_tool_choice("required"))
            return
        if inner.get("type") != "response.completed":
            return
        delegation_id = event.get("delegation_id")
        if not isinstance(delegation_id, str):
            delegation_id = None
        if delegation_id and delegation_id in self._awaiting_final:
            self._awaiting_final.discard(delegation_id)
            # Next delegation must be able to call a named tool again.
            self._spawn(self._set_tool_choice("required"))
            return
        count = self._delegation_tool_count.get(delegation_id or "", 0)
        if count == 0:
            log.warning(
                "openai live delegation id=%s completed with no function call delegation_ms=%s",
                delegation_id or "",
                self._delegation_ms(delegation_id),
            )

    def _remember_call(self, call: ResponsesFunctionCall) -> str:
        group = call.delegation_id or call.response_id or call.call_id
        self._call_names[call.call_id] = call.name
        self._pending_calls[call.call_id] = group
        self._groups.setdefault(group, set()).add(call.call_id)
        if call.delegation_id:
            self._delegation_tool_count[call.delegation_id] = (
                self._delegation_tool_count.get(call.delegation_id, 0) + 1
            )
        return group

    async def _say_backend_failure(self) -> None:
        await self._append(
            "session.commentary.append",
            (
                "Say one short sentence that you could not check that just now, "
                "then use a SCRIPT fallback. Do not invent the fact."
            ),
        )

    async def _say_checking(self, delegation_id: str | None) -> None:
        key = delegation_id or ""
        if key in self._checking_for or self._ws is None:
            return
        self._checking_for.add(key)
        await self._append(
            "session.commentary.append",
            (
                "The orchestrator is checking this. It may take a few seconds. "
                "You can let the person on the line know if you want, for example that you are looking it up. "
                "Do not invent the fact."
            ),
        )

    async def _settle_repeat(self, call: ResponsesFunctionCall, group: str) -> None:
        """Answer a second function call on a delegation that already has a result."""
        previous = self._answered.get(group) or {}
        speakable = _speakable_tool_output(previous)
        output = {
            "ok": True,
            "already_answered": True,
            "answer": speakable,
            "instruction": (
                "Already answered on this delegation. Speak that answer to the callee now. "
                "Do not call a tool and do not ask for confirmation."
            ),
        }
        # One continuation after the repeat, with tools disabled. Further calls
        # only receive the output so the backend cannot loop on the orchestrator.
        repeats = self._repeat_continues.get(group, 0)
        self._repeat_continues[group] = repeats + 1
        log.info(
            "openai live repeat %s call_id=%s delegation_id=%s settled locally",
            call.name,
            call.call_id,
            group,
        )
        await self.submit_tool_result(
            call.call_id,
            output,
            continue_response=repeats == 0,
        )

    async def _emit_function_call(self, call: ResponsesFunctionCall) -> None:
        if call.call_id in self._seen_call_ids:
            log.info("openai live duplicate function call call_id=%s", call.call_id)
            return
        self._seen_call_ids.add(call.call_id)
        group = call.delegation_id or call.response_id or call.call_id
        if (
            call.name in ORCHESTRATOR_TOOL_NAMES
            and group in self._answered
        ):
            self._remember_call(call)
            self._spawn(self._settle_repeat(call, group))
            return
        self._remember_call(call)
        delay_ms = self._delegation_ms(call.delegation_id)
        log.info(
            "openai live tool %s call_id=%s delegation_id=%s delegation_ms=%s",
            call.name,
            call.call_id,
            call.delegation_id or "",
            delay_ms,
        )
        if call.name in ORCHESTRATOR_TOOL_NAMES and self._ws is not None:
            self._spawn(self._say_checking(call.delegation_id))
        await self._emit_tool_call(call.name, call.call_id, call.arguments)

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.started":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(event)
            await self._events.put(VoiceEvent(type="session.ready", payload=event))
            return
        if kind == "session.closed":
            self._server_closed.set()
            usage = event.get("usage")
            if usage:
                log.info("openai live session closed usage=%s reason=%s", usage, event.get("reason"))
            return
        if kind == "session.output_audio.delta":
            delta = event.get("delta") or event.get("audio")
            if delta:
                self._user_speaking = False
                await self._events.put(
                    VoiceEvent(type="audio.out", payload={"pcm": base64.b64decode(delta)})
                )
            return
        if kind == "session.input_transcript.delta":
            text = event.get("delta") or event.get("text") or ""
            if not self._user_speaking:
                self._user_speaking = True
                # Flush queued assistant playout on barge-in (Live has no speech.started).
                await self._events.put(VoiceEvent(type="speech.started", payload=event))
            await self._events.put(
                VoiceEvent(
                    type="transcript",
                    payload={"role": "user", "text": text, "final": False},
                )
            )
            return
        if kind == "session.output_transcript.delta":
            self._user_speaking = False
            text = event.get("delta") or event.get("text") or ""
            await self._events.put(
                VoiceEvent(
                    type="transcript",
                    payload={"role": "assistant", "text": text, "final": False},
                )
            )
            return
        if kind == "session.delegation.created":
            self._note_delegation(event)
            return
        if kind == "response.event":
            await self._handle_response_event(event)
            return
        if kind == "error":
            message = _error_message(event)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(message))
            await self._events.put(VoiceEvent(type="error", payload={"message": message}))
            return


def _event_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
