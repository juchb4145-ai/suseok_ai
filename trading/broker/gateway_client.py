from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterable

from trading.broker.models import GatewayEvent


@dataclass
class GatewayEventQueue:
    """Small gateway-side queue that can coalesce noisy quote ticks before flush."""

    max_size: int = 1000
    coalesce_price_ticks: bool = True
    _events: deque[GatewayEvent] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._lock = Lock()

    def put(self, event: GatewayEvent) -> None:
        with self._lock:
            self._events.append(event)
            while len(self._events) > self.max_size:
                self._events.popleft()

    def extend(self, events: Iterable[GatewayEvent]) -> None:
        for event in events:
            self.put(event)

    def drain(self, limit: int = 100) -> list[GatewayEvent]:
        drained: list[GatewayEvent] = []
        with self._lock:
            for _ in range(min(max(0, int(limit)), len(self._events))):
                drained.append(self._events.popleft())
        if not self.coalesce_price_ticks:
            return drained
        return _coalesce_ticks(drained)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def _coalesce_ticks(events: list[GatewayEvent]) -> list[GatewayEvent]:
    tick_indexes: OrderedDict[str, int] = OrderedDict()
    result: list[GatewayEvent] = []
    for event in events:
        if event.type != "price_tick":
            result.append(event)
            continue
        code = str(event.payload.get("code") or "")
        if not code:
            result.append(event)
            continue
        if code in tick_indexes:
            result[tick_indexes[code]] = event
        else:
            tick_indexes[code] = len(result)
            result.append(event)
    return result
