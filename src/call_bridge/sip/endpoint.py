from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal


SipState = Literal[
    "idle",
    "registering",
    "registered",
    "register_failed",
    "calling",
    "ringing",
    "answered",
    "ended",
    "failed",
]


@dataclass
class SipCall:
    call_id: str
    to: str
    state: SipState = "calling"
    far_ip: str | None = None
    far_port: int | None = None
    reason: str | None = None
    frames_in: int = 0
    frames_out: int = 0


@dataclass
class RegisterStatus:
    state: SipState
    detail: str = ""
    expires: int | None = None


class SipEndpoint(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def register_status(self) -> RegisterStatus: ...

    @abstractmethod
    async def invite(self, dest: str, local_call_id: str) -> SipCall: ...

    @abstractmethod
    async def hangup(self, local_call_id: str, reason: str = "local") -> None: ...

    @abstractmethod
    async def send_audio(self, local_call_id: str, frame: bytes) -> None: ...

    @abstractmethod
    def audio_in(self, local_call_id: str) -> AsyncIterator[bytes]: ...

    @abstractmethod
    def events(self) -> AsyncIterator[tuple[str, dict]]: ...
