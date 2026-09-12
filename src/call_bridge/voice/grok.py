from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection

from call_bridge.config import Settings
from call_bridge.voice.provider import (
    AudioFormat,
    VoiceAgentProvider,
    VoiceEvent,
    VoiceSessionConfig,
)
from call_bridge.voice.tools import tools_as_openai_functions

log = logging.getLogger(__name__)

_ASSISTANT_TRANSCRIPT_EVENTS = {
    "response.output_audio_transcript.delta",
    "response.output_audio_transcript.done",
    "response.audio_transcript.delta",
    "response.audio_transcript.done",
    "response.text.delta",
    "response.text.done",
}


def _format_type(fmt: AudioFormat) -> str:
    return {
        "pcma": "audio/pcma",
        "pcmu": "audio/pcmu",
        "pcm16": "audio/pcm",
    }[fmt.encoding]


def _audio_block(fmt: AudioFormat, *, transcribe_model: str | None, language: str | None, keyterms: list[str]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "format": {"type": _format_type(fmt)},
        "transport": fmt.transport,
    }
    if fmt.encoding == "pcm16":
        block["format"]["rate"] = fmt.sample_rate_hz
    transcription: dict[str, Any] = {}
    if transcribe_model:
        transcription["model"] = transcribe_model
    if language:
        transcription["language_hint"] = language
    if keyterms:
        transcription["keyterms"] = keyterms[:100]
    if transcription:
        block["transcription"] = transcription
    return block


class GrokVoiceProvider(VoiceAgentProvider):
    """xAI Grok Voice Realtime (WSS / session.update / server_vad / function tools)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ws: ClientConnection | None = None
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._pending_tool_calls = 0
        self._config: VoiceSessionConfig | None = None

    @property
    def name(self) -> str:
        return "grok"

    async def connect(self, config: VoiceSessionConfig) -> None:
        if not self._settings.xai_api_key:
            raise RuntimeError("XAI_API_KEY is empty")
        self._config = config
        query = urlencode({"model": self._settings.xai_voice_model})
        url = f"{self._settings.xai_realtime_url}?{query}"
        log.info("connecting grok voice %s", self._settings.xai_voice_model)
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self._settings.xai_api_key}"},
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        self._closed.clear()
        self._reader = asyncio.create_task(self._read_loop(), name="grok-reader")
        await self._send({"type": "session.update", "session": self._session_payload(config)})
        if config.speak_first:
            await self.speak_verbatim(config.speak_first, interruptible=False)

    def _session_payload(self, config: VoiceSessionConfig) -> dict[str, Any]:
        turn: dict[str, Any] | None
        if config.turn_detection.kind == "server_vad":
            turn = {"type": "server_vad"}
            if config.turn_detection.threshold is not None:
                turn["threshold"] = config.turn_detection.threshold
            if config.turn_detection.silence_duration_ms is not None:
                turn["silence_duration_ms"] = config.turn_detection.silence_duration_ms
            if config.turn_detection.prefix_padding_ms is not None:
                turn["prefix_padding_ms"] = config.turn_detection.prefix_padding_ms
            if config.turn_detection.idle_timeout_ms is not None:
                turn["idle_timeout_ms"] = config.turn_detection.idle_timeout_ms
        else:
            turn = None
        return {
            "voice": config.voice or self._settings.xai_voice,
            "instructions": config.instructions,
            "turn_detection": turn,
            "audio": {
                "input": _audio_block(
                    config.audio_in,
                    transcribe_model=config.transcribe_model or self._settings.xai_transcribe_model,
                    language=config.language,
                    keyterms=config.keyterms,
                ),
                "output": _audio_block(
                    config.audio_out,
                    transcribe_model=None,
                    language=None,
                    keyterms=[],
                ),
            },
            "tools": tools_as_openai_functions(config.tools)
            + ([{"type": "web_search"}] if config.enable_web_search else []),
        }

    async def close(self) -> None:
        self._closed.set()
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
        if not frame or self._ws is None:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }
        )

    async def inject_guideline(self, text: str) -> None:
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[ORCHESTRATOR STEER — do not read this aloud] {text}",
                        }
                    ],
                },
            }
        )
        await self._send(
            {
                "type": "response.create",
                "response": {"instructions": text},
            }
        )

    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        if self._ws is None:
            raise RuntimeError("voice session is not connected")
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "interruptible": interruptible,
                    "content": [{"type": "output_text", "text": text}],
                },
            }
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
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output),
                },
            }
        )
        self._pending_tool_calls = max(0, self._pending_tool_calls - 1)
        if continue_response and self._pending_tool_calls == 0:
            await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

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
            log.warning("grok websocket ended: %s", exc)
            await self._events.put(VoiceEvent(type="error", payload={"message": str(exc)}))
        finally:
            await self._events.put(VoiceEvent(type="closed"))

    async def _handle_server_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")
        if kind == "session.updated" or kind == "session.created":
            await self._events.put(VoiceEvent(type="session.ready", payload=event))
            return
        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            delta = event.get("delta") or event.get("audio")
            if delta:
                await self._events.put(
                    VoiceEvent(type="audio.out", payload={"pcm": base64.b64decode(delta)})
                )
            return
        if kind == "input_audio_buffer.speech_started":
            await self._events.put(VoiceEvent(type="speech.started", payload=event))
            return
        if kind == "input_audio_buffer.speech_stopped":
            await self._events.put(VoiceEvent(type="speech.stopped", payload=event))
            return
        if kind in {
            "conversation.item.input_audio_transcription.updated",
            "conversation.item.input_audio_transcription.completed",
        }:
            transcript = event.get("transcript") or event.get("text") or ""
            final = kind.endswith("completed")
            await self._events.put(
                VoiceEvent(
                    type="transcript",
                    payload={"role": "user", "text": transcript, "final": final},
                )
            )
            return
        if kind in _ASSISTANT_TRANSCRIPT_EVENTS:
            text = event.get("delta") or event.get("transcript") or event.get("text") or ""
            final = kind.endswith(".done")
            if text or final:
                await self._events.put(
                    VoiceEvent(
                        type="transcript",
                        payload={"role": "assistant", "text": text, "final": final},
                    )
                )
            return
        if kind == "response.function_call_arguments.done":
            self._pending_tool_calls += 1
            arguments = event.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    parsed = {"raw": arguments}
            else:
                parsed = arguments
            await self._events.put(
                VoiceEvent(
                    type="tool_call",
                    payload={
                        "tool": event.get("name"),
                        "tool_call_id": event.get("call_id"),
                        "arguments": parsed,
                    },
                )
            )
            return
        if kind == "response.done":
            await self._events.put(VoiceEvent(type="response.done", payload=event))
            return
        if kind == "error":
            await self._events.put(
                VoiceEvent(type="error", payload={"message": event.get("error") or event})
            )
            return
