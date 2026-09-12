from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from call_bridge.voice.provider import VoiceAgentProvider, VoiceEvent, VoiceSessionConfig

# 20 ms of PCMA silence (a-law 0)
_SILENCE_FRAME = bytes([0xD5]) * 160


class MockVoiceProvider(VoiceAgentProvider):
    """Deterministic voice side for CI / dry-run. Emits a short scripted dialogue."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._config: VoiceSessionConfig | None = None
        self._closed = False
        self._audio_in = 0
        self._script_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return "mock"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self._config = config
        self._closed = False
        await self._events.put(VoiceEvent(type="session.ready", payload={"provider": "mock"}))
        self._script_task = asyncio.create_task(self._run_script(), name="mock-voice-script")

    async def close(self) -> None:
        self._closed = True
        if self._script_task:
            self._script_task.cancel()
            try:
                await self._script_task
            except (asyncio.CancelledError, Exception):
                pass
            self._script_task = None
        await self._events.put(VoiceEvent(type="closed"))

    async def send_audio(self, frame: bytes) -> None:
        self._audio_in += len(frame)

    async def inject_guideline(self, text: str) -> None:
        await self._events.put(
            VoiceEvent(type="transcript", payload={"role": "system", "text": text, "final": True})
        )
        await self._events.put(
            VoiceEvent(
                type="transcript",
                payload={
                    "role": "assistant",
                    "text": f"(acknowledging steer) {text}",
                    "final": True,
                },
            )
        )

    async def speak_verbatim(self, text: str, *, interruptible: bool = False) -> None:
        await self._events.put(
            VoiceEvent(type="transcript", payload={"role": "assistant", "text": text, "final": True})
        )
        await self._events.put(VoiceEvent(type="audio.out", payload={"pcm": _SILENCE_FRAME * 10}))

    async def submit_tool_result(
        self,
        call_id: str,
        output: dict[str, Any],
        *,
        continue_response: bool = True,
    ) -> None:
        await self._events.put(
            VoiceEvent(
                type="transcript",
                payload={
                    "role": "assistant",
                    "text": f"(tool result {call_id}) {output}",
                    "final": True,
                },
            )
        )
        if continue_response:
            await self._events.put(VoiceEvent(type="response.done", payload={}))

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            yield event
            if event.type == "closed":
                return

    async def _run_script(self) -> None:
        try:
            if self._config and self._config.speak_first:
                await asyncio.sleep(0.05)
                await self.speak_verbatim(self._config.speak_first, interruptible=False)
            await asyncio.sleep(0.08)
            await self._events.put(VoiceEvent(type="speech.started", payload={}))
            await self._events.put(
                VoiceEvent(
                    type="transcript",
                    payload={"role": "user", "text": "Hello? Who is this?", "final": True},
                )
            )
            await self._events.put(VoiceEvent(type="speech.stopped", payload={}))
            await asyncio.sleep(0.05)
            await self._events.put(
                VoiceEvent(
                    type="transcript",
                    payload={
                        "role": "assistant",
                        "text": "I'm calling about the appointment. One moment while I confirm a detail.",
                        "final": True,
                    },
                )
            )
            await self._events.put(
                VoiceEvent(
                    type="tool_call",
                    payload={
                        "tool": "ask_orchestrator",
                        "tool_call_id": "mock-tool-1",
                        "arguments": {
                            "question": "Which appointment slot should I confirm?",
                            "context": "Callee answered and asked who is calling.",
                            "urgency": "normal",
                        },
                    },
                )
            )
        except asyncio.CancelledError:
            raise
