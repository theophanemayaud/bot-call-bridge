from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from call_bridge.sip.endpoint import RegisterStatus, SipCall, SipEndpoint, SipState
from call_bridge.sip.protocol import PCMA_SILENCE


@dataclass
class _MockDialog:
    call: SipCall
    audio_in: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    feeder: asyncio.Task[None] | None = None


class MockSipEndpoint(SipEndpoint):
    """In-process SIP stand-in. REGISTER is instant; INVITE answers after a short ring."""

    def __init__(self) -> None:
        self._state: SipState = "idle"
        self._dialogs: dict[str, _MockDialog] = {}
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def start(self) -> None:
        self._state = "registered"
        await self._events.put(("sip.register", {"state": "registered", "mode": "mock"}))

    async def stop(self) -> None:
        for call_id in list(self._dialogs):
            await self.hangup(call_id, reason="shutdown")
        self._state = "idle"

    def register_status(self) -> RegisterStatus:
        return RegisterStatus(state=self._state, detail="mock", expires=3600)

    async def invite(self, dest: str, local_call_id: str) -> SipCall:
        call = SipCall(call_id=local_call_id, to=dest, state="calling")
        dialog = _MockDialog(call=call)
        self._dialogs[local_call_id] = dialog
        await self._events.put(("sip.invite", {"state": "calling", "to": dest}))
        await asyncio.sleep(0.05)
        call.state = "ringing"
        await self._events.put(("sip.invite", {"state": "ringing", "to": dest}))
        await asyncio.sleep(0.05)
        call.state = "answered"
        call.far_ip = "203.0.113.10"
        call.far_port = 20000
        dialog.feeder = asyncio.create_task(self._feed_far_end(dialog), name="mock-rtp")
        await self._events.put(("sip.invite", {"state": "answered", "to": dest}))
        return call

    async def hangup(self, local_call_id: str, reason: str = "local") -> None:
        dialog = self._dialogs.pop(local_call_id, None)
        if not dialog:
            return
        dialog.call.state = "ended"
        dialog.call.reason = reason
        dialog.finished.set()
        if dialog.feeder:
            dialog.feeder.cancel()
        await self._events.put(("sip.hangup", {"reason": reason, "to": dialog.call.to}))

    async def send_audio(self, local_call_id: str, frame: bytes) -> None:
        dialog = self._dialogs.get(local_call_id)
        if dialog:
            dialog.call.frames_out += 1

    async def audio_in(self, local_call_id: str) -> AsyncIterator[bytes]:
        dialog = self._dialogs[local_call_id]
        while not dialog.finished.is_set():
            try:
                frame = await asyncio.wait_for(dialog.audio_in.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            dialog.call.frames_in += 1
            yield frame

    async def events(self) -> AsyncIterator[tuple[str, dict]]:
        while True:
            yield await self._events.get()

    async def _feed_far_end(self, dialog: _MockDialog) -> None:
        try:
            while not dialog.finished.is_set():
                await asyncio.sleep(0.02)
                try:
                    dialog.audio_in.put_nowait(PCMA_SILENCE)
                except asyncio.QueueFull:
                    pass
        except asyncio.CancelledError:
            raise
