from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

from call_bridge.sip.endpoint import RegisterStatus, SipCall, SipEndpoint, SipState
from call_bridge.sip.protocol import PCMA_SILENCE


@dataclass
class MockInvitePlan:
    """Per-number INVITE behaviour. Unlisted numbers answer after a short ring."""

    mode: Literal["answer", "ring", "fail"] = "answer"
    delay: float = 0.0
    reason: str = "486 Busy Here"


@dataclass
class _MockDialog:
    call: SipCall
    audio_in: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    answer_event: asyncio.Event = field(default_factory=asyncio.Event)
    feeder: asyncio.Task[None] | None = None
    notable: deque[bytes] = field(default_factory=lambda: deque(maxlen=16))


class MockSipEndpoint(SipEndpoint):
    """In-process SIP stand-in. REGISTER is instant; INVITE answers after a short ring."""

    def __init__(self) -> None:
        self._state: SipState = "idle"
        self._dialogs: dict[str, _MockDialog] = {}
        self._events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._plans: dict[str, MockInvitePlan] = {}
        self.invite_log: list[str] = []

    def plan_invite(self, number: str, plan: MockInvitePlan) -> None:
        self._plans[number] = plan

    def notable_audio(self, local_call_id: str) -> list[bytes]:
        dialog = self._dialogs.get(local_call_id)
        return list(dialog.notable) if dialog else []

    def push_audio(self, local_call_id: str, frame: bytes) -> None:
        dialog = self._dialogs[local_call_id]
        try:
            dialog.audio_in.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                dialog.audio_in.get_nowait()
            except asyncio.QueueEmpty:
                pass
            dialog.audio_in.put_nowait(frame)

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
        self.invite_log.append(dest)
        plan = self._plans.get(dest)
        call = SipCall(call_id=local_call_id, to=dest, state="calling")
        dialog = _MockDialog(call=call)
        self._dialogs[local_call_id] = dialog
        try:
            if plan is not None and plan.mode == "fail":
                raise RuntimeError(f"INVITE failed: {plan.reason}")
            await self._events.put(("sip.invite", {"state": "calling", "to": dest}))
            if plan is not None and plan.mode == "ring":
                call.state = "ringing"
                await self._events.put(("sip.invite", {"state": "ringing", "to": dest}))
                await dialog.answer_event.wait()
                if dialog.finished.is_set() and call.state != "answered":
                    raise RuntimeError("INVITE cancelled")
            else:
                # Default path matches the original short ring (two 50 ms sleeps).
                await asyncio.sleep(0.05)
                if dialog.finished.is_set():
                    raise RuntimeError("INVITE cancelled")
                call.state = "ringing"
                await self._events.put(("sip.invite", {"state": "ringing", "to": dest}))
                extra = plan.delay if plan is not None else 0.0
                await asyncio.sleep(0.05 + extra)
                if dialog.finished.is_set():
                    raise RuntimeError("INVITE cancelled")
            call.state = "answered"
            call.far_ip = "203.0.113.10"
            call.far_port = 20000
            dialog.feeder = asyncio.create_task(self._feed_far_end(dialog), name="mock-rtp")
            await self._events.put(("sip.invite", {"state": "answered", "to": dest}))
            return call
        except asyncio.CancelledError:
            dialog.call.state = "ended"
            dialog.call.reason = "cancelled"
            dialog.finished.set()
            if dialog.feeder:
                dialog.feeder.cancel()
            self._dialogs.pop(local_call_id, None)
            self._events.put_nowait(("sip.hangup", {"reason": "cancelled", "to": dest}))
            raise
        except Exception:
            dialog.finished.set()
            if dialog.feeder:
                dialog.feeder.cancel()
            self._dialogs.pop(local_call_id, None)
            raise

    async def hangup(self, local_call_id: str, reason: str = "local") -> None:
        dialog = self._dialogs.pop(local_call_id, None)
        if not dialog:
            return
        dialog.call.state = "ended"
        dialog.call.reason = reason
        dialog.finished.set()
        dialog.answer_event.set()
        if dialog.feeder:
            dialog.feeder.cancel()
        await self._events.put(("sip.hangup", {"reason": reason, "to": dialog.call.to}))

    async def send_audio(self, local_call_id: str, frame: bytes) -> None:
        dialog = self._dialogs.get(local_call_id)
        if dialog:
            dialog.call.frames_out += 1
            if frame != PCMA_SILENCE:
                dialog.notable.append(frame)

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
