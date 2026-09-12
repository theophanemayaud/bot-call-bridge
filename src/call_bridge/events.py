from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal


EventKind = Literal[
    "bridge.status",
    "sip.register",
    "sip.invite",
    "sip.hangup",
    "call.state",
    "transcript",
    "guideline",
    "tool_call",
    "tool_result",
    "voice.error",
    "audio",
]


@dataclass(slots=True)
class BridgeEvent:
    type: EventKind
    call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "type": self.type,
            "call_id": self.call_id,
            **self.payload,
        }


class EventBus:
    def __init__(self, history: int = 200) -> None:
        self._subs: set[asyncio.Queue[BridgeEvent]] = set()
        self._history: deque[BridgeEvent] = deque(maxlen=history)
        self._per_call: dict[str, deque[BridgeEvent]] = defaultdict(
            lambda: deque(maxlen=history)
        )

    def publish(self, event: BridgeEvent) -> None:
        self._history.append(event)
        if event.call_id:
            self._per_call[event.call_id].append(event)
        for queue in list(self._subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)

    def history(self, call_id: str | None = None) -> list[BridgeEvent]:
        if call_id:
            return list(self._per_call.get(call_id, ()))
        return list(self._history)

    def subscribe(self) -> asyncio.Queue[BridgeEvent]:
        queue: asyncio.Queue[BridgeEvent] = asyncio.Queue(maxsize=256)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[BridgeEvent]) -> None:
        self._subs.discard(queue)
