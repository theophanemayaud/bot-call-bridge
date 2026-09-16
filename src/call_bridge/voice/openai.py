from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
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

log = logging.getLogger(__name__)

# Live commentary/instructions appends are capped at ~500 tokens.
_APPEND_CHAR_LIMIT = 1800
_CONNECT_TIMEOUT_SECONDS = 15.0
_CLOSE_TIMEOUT_SECONDS = 2.0

# How long to wait for output transcript when Live delegates before the goodbye
# text has arrived. Does not delay RTP; only classification of that delegation.
DELEGATION_TRANSCRIPT_GRACE_SECONDS = 0.45
# After an explicit "I'll hang up" (and no new user speech), emit hangup even if
# the model never delegated. Soft closers ("bye", "take care") do not arm this.
HANGUP_FALLBACK_SETTLE_SECONDS = 1.8

# Closers used when Live already delegated. Keep these at the *end* of a turn.
_HANGUP_MARKERS = (
    "au revoir",
    "raccroche",
    "je raccroche",
    "je vais raccrocher",
    "je te laisse",
    "je vous laisse",
    "à bientôt",
    "a bientot",
    "goodbye",
    "good bye",
    "bye-bye",
    "bye bye",
    "bye",
    "take care",
    "talk soon",
    "have a good one",
    "have a nice day",
    "have a good day",
    "hanging up",
    "hang up",
    "i'll hang up",
    "i will hang up",
    "i'm hanging up",
    "i am hanging up",
    "i'll let you go",
    "i will let you go",
    "ha det bra",
    "ha det",
    "adjø",
    "adjo",
    "farvel",
    "vi snakkes",
    "jeg legger på",
    "legger på",
    "takk for nå",
    "takk for na",
    "ciao",
)

# Spoken intent strong enough to BYE without a second delegation.
_EXPLICIT_HANGUP_MARKERS = (
    "hanging up",
    "hang up",
    "i'll hang up",
    "i will hang up",
    "i'm hanging up",
    "i am hanging up",
    "i'm going to hang up",
    "i am going to hang up",
    "i'll let you go",
    "i will let you go",
    "je raccroche",
    "je vais raccrocher",
    "jeg legger på",
    "legger på",
)

_HANGUP_NEGATIONS = (
    "don't hang up",
    "do not hang up",
    "dont hang up",
    "not hanging up",
    "don't go",
    "do not go",
    "stay on the line",
    "hold the line",
    "hold on",
    "one more thing",
)

_END_ONLY_HANGUP_MARKERS = (
    "je te laisse",
    "je vous laisse",
    "ha det",
)

_CONTINUATION_PHRASES = (
    "let me check",
    "i'll check",
    "i will check",
    "i'll look",
    "i will look",
    "let me look",
    "come back to you",
    "i'll wait",
    "i will wait",
    "i forgot",
    "before you go",
)

_LIVE_INSTRUCTION_PREFIX = """You are a calm, friendly voice agent on a live outbound phone call.
Speak naturally, at an unhurried pace. Be clear and direct. If the callee is frustrated, acknowledge it briefly and focus on the next helpful step.
Prefer the callee's language when they speak; default to the SCRIPT language.

Backchannel policy: Use moderate backchannels (mm-hmm, oui, d'accord, I see). Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the callee interrupts. Listen to what they say.

Silence and noise policy: Keep listening while the callee pauses to think. Do not treat a cough, hold music, a ringtone, or nearby conversation as a new request.

Delegation policy:
Backend tools:
- ask_orchestrator: facts, decisions, and next steps the Call/orchestrator must provide. You do not have those facts. Never use this when the conversation is already finished or you are about to hang up.
- hangup: end the phone call after you have already spoken an audible goodbye.

When the goals are done, the callee says goodbye, or a fallback says to disconnect:
1. Speak a short goodbye (for example "Thanks, take care, bye" / "Je te laisse, au revoir" / "Takk, ha det bra").
2. Then immediately delegate hangup in the same turn. Do not ask the orchestrator what to do next. Do not wait for another user turn after you have said goodbye.
3. If you say you will hang up, you must delegate hangup — saying "I'll hang up now" without delegating leaves the callee on the line.

Voicemail: If you reach voicemail or an answering machine, leave a short message (who you are and why), say goodbye, then delegate hangup. Do not sit in silence after the greeting.

Delegate to the backend when:
- You need a fact, confirmation, or next step you do not have.
- The conversation is complete, the callee asks to stop, or you just left a voicemail — speak goodbye first, then delegate hangup so the line can BYE.

Do not delegate to the backend when:
- You can answer from the SCRIPT, disclosure, or a still-current orchestrator result.
- You only need a brief clarification from the callee.
- The call is over except to hang up (use hangup, not ask_orchestrator).

Delegate before giving an answer that depends on backend work.
Do not invent facts while waiting.
Do not mention backend, tools, or delegation to the callee.
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
) -> dict[str, Any]:
    """GPT-Live session.start body (client delegation, shared PCMA format)."""
    fmt = config.audio_in
    return {
        "model": model,
        "instructions": compose_live_instructions(config),
        "audio": {
            "format": live_audio_format(fmt),
            "output": {"voice": config.voice or default_voice},
        },
        "delegation": {"type": "client"},
    }


def _normalize_speech(text: str) -> str:
    lowered = (text or "").strip().lower()
    for src in ("\u2019", "\u2018", "\u02bc", "`"):
        lowered = lowered.replace(src, "'")
    return " ".join(lowered.split())


def _closing_tail(text: str, *, max_chars: int = 180) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _phrase_in(text: str, phrase: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    return re.search(pattern, text) is not None


def _phrase_at_close(text: str, phrase: str) -> bool:
    """True when ``phrase`` is a closer, not 'je te laisse vérifier'."""
    match = None
    for found in re.finditer(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
        match = found
    if not match:
        return False
    rest = text[match.end() :].strip(" .,!?;:…-")
    if not rest:
        return True
    return any(_phrase_in(rest, marker) for marker in _HANGUP_MARKERS if marker != phrase)


def looks_like_hangup_speech(text: str, *, explicit: bool = False) -> bool:
    """True when the closing tail of an utterance is a hangup/goodbye.

    ``explicit=True`` requires "I'll hang up" / "je raccroche" / similar — used
    for the no-delegation fallback. Soft closers (bye, take care) only count
    when Live already delegated, so mid-call "take care of the booking" does not
    BYE.
    """
    tail = _closing_tail(_normalize_speech(text))
    if not tail:
        return False
    if any(_phrase_in(tail, phrase) for phrase in _CONTINUATION_PHRASES):
        return False
    if any(_phrase_in(tail, phrase) for phrase in _HANGUP_NEGATIONS):
        return False
    markers = _EXPLICIT_HANGUP_MARKERS if explicit else _HANGUP_MARKERS
    for marker in markers:
        if marker == "take care" and _phrase_in(tail, "take care of"):
            continue
        if marker in _END_ONLY_HANGUP_MARKERS and not _phrase_at_close(tail, marker):
            continue
        if _phrase_in(tail, marker):
            return True
    return False


def classify_live_delegation(
    *,
    last_assistant: str,
    recent: str,
    last_user: str = "",
) -> tuple[str, dict[str, Any]]:
    """Map a client-delegation notice onto hangup or ask_orchestrator.

    Live `session.delegation.created` has metadata only — no tool name or args.
    Prefer the current assistant closer (trailing after the last user turn). If
    that text is empty, a callee closer like "bye" still maps to hangup.
    """
    assistant = (last_assistant or "").strip()
    if looks_like_hangup_speech(assistant):
        return "hangup", {
            "reason": "agent",
            "summary": assistant[-240:],
        }
    user = (last_user or "").strip()
    if not assistant and looks_like_hangup_speech(user):
        return "hangup", {
            "reason": "agent",
            "summary": user[-240:] or (recent or "")[-240:],
        }
    return "ask_orchestrator", {
        "question": "The live model delegated. What should I do or say next?",
        "context": recent,
        "urgency": "normal",
    }


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


def _error_message(event: dict[str, Any]) -> str:
    err = event.get("error") or event
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


class OpenAILiveProvider(VoiceAgentProvider):
    """OpenAI GPT-Live-1 WebSocket (session.start / client delegation / PCMA 8 kHz)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ws: ClientConnection | None = None
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[dict[str, Any]] | None = None
        self._server_closed = asyncio.Event()
        self._config: VoiceSessionConfig | None = None
        self._session_id: str | None = None
        self._delegations: dict[str, str] = {}
        self._fragments: list[tuple[str, str]] = []
        self._user_speaking = False
        self._closing = False
        self._hangup_emitted = False
        self._fallback_task: asyncio.Task[None] | None = None
        self._classify_tasks: set[asyncio.Task[None]] = set()
        self._last_assistant_activity_at = 0.0

    @property
    def name(self) -> str:
        return "openai"

    async def connect(self, config: VoiceSessionConfig) -> None:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is empty")
        self._config = config
        self._closing = False
        self._server_closed.clear()
        self._delegations.clear()
        self._fragments.clear()
        self._user_speaking = False
        self._hangup_emitted = False
        self._last_assistant_activity_at = 0.0
        self._cancel_classify_tasks()
        self._disarm_hangup_fallback()
        url = self._settings.openai_live_url
        log.info("connecting openai gpt-live %s", self._settings.openai_live_model)
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
        self._disarm_hangup_fallback()
        self._cancel_classify_tasks()
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
            delegation_id=None,
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
            delegation_id=None,
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
        delegation_id = call_id if call_id in self._delegations else None
        self._delegations.pop(call_id, None)
        if not continue_response:
            await self._append(
                "session.instructions.append",
                "The application is hanging up the call now. Do not continue the conversation.",
                delegation_id=delegation_id,
            )
            return
        if output.get("error") == "orchestrator_timeout" or output.get("ok") is False:
            fallback = _speakable_tool_output(output)
            await self._append(
                "session.instructions.append",
                (
                    "The orchestrator could not complete this request. "
                    f"{fallback} Use a SCRIPT fallback. Do not invent facts."
                ),
                delegation_id=delegation_id,
            )
            return
        await self._append(
            "session.commentary.append",
            _speakable_tool_output(output),
            delegation_id=delegation_id,
        )

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

    async def _append(self, type_: str, content: str, *, delegation_id: str | None) -> None:
        await self._send(
            {
                "type": type_,
                "event_id": _event_id("append"),
                "delegation_id": delegation_id,
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

    def _recent_conversation(self, *, max_chars: int = 800) -> str:
        lines: list[str] = []
        for role, text in self._fragments[-24:]:
            piece = text.strip()
            if piece:
                lines.append(f"{role}: {piece}")
        blob = "\n".join(lines)
        if len(blob) > max_chars:
            return blob[-max_chars:]
        return blob

    def _trailing_role_text(self, role: str) -> str:
        """Text for ``role`` only if that role is currently speaking (no later other-role turn)."""
        chunks: list[str] = []
        for frag_role, text in reversed(self._fragments):
            if not (text or "").strip():
                continue
            if frag_role != role:
                break
            chunks.append(text)
        chunks.reverse()
        return "".join(chunks)

    def _mark_assistant_activity(self) -> None:
        try:
            self._last_assistant_activity_at = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_assistant_activity_at = 0.0

    def _cancel_classify_tasks(self) -> None:
        for task in list(self._classify_tasks):
            task.cancel()
        self._classify_tasks.clear()

    def _disarm_hangup_fallback(self) -> None:
        task = self._fallback_task
        self._fallback_task = None
        if task is not None and not task.done():
            task.cancel()

    def _arm_hangup_fallback(self) -> None:
        if self._hangup_emitted or self._closing:
            return
        self._disarm_hangup_fallback()
        try:
            self._fallback_task = asyncio.get_running_loop().create_task(
                self._hangup_fallback_watch(),
                name="openai-hangup-fallback",
            )
        except RuntimeError:
            self._fallback_task = None

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._classify_tasks.add(task)
        task.add_done_callback(self._classify_tasks.discard)

    def _on_assistant_speech(self, text: str) -> None:
        self._mark_assistant_activity()
        if not text:
            return
        trailing = self._trailing_role_text("assistant")
        if looks_like_hangup_speech(trailing, explicit=True):
            self._arm_hangup_fallback()
        elif not looks_like_hangup_speech(trailing):
            self._disarm_hangup_fallback()

    async def _note_transcript(self, role: str, text: str) -> None:
        if text:
            self._fragments.append((role, text))
        if role == "assistant":
            self._on_assistant_speech(text)
        else:
            self._disarm_hangup_fallback()
        await self._events.put(
            VoiceEvent(
                type="transcript",
                payload={"role": role, "text": text, "final": False},
            )
        )

    async def _emit_tool_call(self, tool: str, tool_call_id: str, arguments: dict[str, Any]) -> None:
        self._delegations[tool_call_id] = tool
        if tool == "hangup":
            self._hangup_emitted = True
            self._disarm_hangup_fallback()
        log.info("openai live tool %s id=%s", tool, tool_call_id)
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

    async def _classify_and_emit_delegation(self, delegation_id: str, *, wait_for_transcript: bool) -> None:
        if wait_for_transcript:
            deadline = asyncio.get_running_loop().time() + DELEGATION_TRANSCRIPT_GRACE_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                if self._trailing_role_text("assistant") or self._trailing_role_text("user"):
                    break
                await asyncio.sleep(0.05)
        if self._hangup_emitted or self._closing:
            return
        assistant = self._trailing_role_text("assistant")
        user = self._trailing_role_text("user")
        tool, arguments = classify_live_delegation(
            last_assistant=assistant,
            last_user=user,
            recent=self._recent_conversation(),
        )
        await self._emit_tool_call(tool, delegation_id, arguments)

    async def _hangup_fallback_watch(self) -> None:
        """BYE after explicit hangup speech when Live never delegates.

        Armed only on "I'll hang up" / "je raccroche" / similar — not on "bye"
        or "take care", which are common mid-call. Cancelled on user barge-in.
        """
        try:
            while True:
                await asyncio.sleep(0.05)
                if self._hangup_emitted or self._closing or self._user_speaking:
                    return
                last = self._trailing_role_text("assistant")
                if not looks_like_hangup_speech(last, explicit=True):
                    return
                idle = asyncio.get_running_loop().time() - self._last_assistant_activity_at
                if idle >= HANGUP_FALLBACK_SETTLE_SECONDS:
                    await self._emit_tool_call(
                        "hangup",
                        f"hangup_fallback_{uuid.uuid4().hex[:12]}",
                        {
                            "reason": "spoken_goodbye",
                            "summary": last[-240:],
                        },
                    )
                    return
        except asyncio.CancelledError:
            return

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
                self._mark_assistant_activity()
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
            await self._note_transcript("user", text)
            return
        if kind == "session.output_transcript.delta":
            self._user_speaking = False
            text = event.get("delta") or event.get("text") or ""
            await self._note_transcript("assistant", text)
            return
        if kind == "session.delegation.created":
            delegation = event.get("delegation") or {}
            delegation_id = delegation.get("id") or event.get("delegation_id")
            if not delegation_id:
                log.warning("openai live delegation missing id: %s", event)
                return
            assistant = self._trailing_role_text("assistant")
            user = self._trailing_role_text("user")
            # If goodbye audio/transcript is still in flight, wait off the read
            # loop so later output_transcript.delta events can arrive.
            wait = not assistant and not user
            if wait:
                self._track_task(
                    asyncio.create_task(
                        self._classify_and_emit_delegation(delegation_id, wait_for_transcript=True),
                        name="openai-classify-delegation",
                    )
                )
                return
            await self._classify_and_emit_delegation(delegation_id, wait_for_transcript=False)
            return
        if kind == "error":
            message = _error_message(event)
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(RuntimeError(message))
            await self._events.put(VoiceEvent(type="error", payload={"message": message}))
            return


def _event_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
