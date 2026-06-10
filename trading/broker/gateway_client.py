from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterable

from trading.broker.models import GatewayEvent

HIGH_PRIORITY_EVENT_TYPES = {
    "heartbeat",
    "login_status",
    "orderability",
    "condition_load_result",
    "condition_loaded",
    "condition_event",
    "command_started",
    "command_ack",
    "command_failed",
    "rate_limited",
    "gateway_error",
    "error",
}


@dataclass
class GatewayEventQueue:
    """Small gateway-side queue that coalesces noisy events before flush."""

    max_size: int = 1000
    coalesce_price_ticks: bool = True
    coalesce_condition_events: bool = True
    _events: deque[GatewayEvent] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._lock = Lock()

    def put(self, event: GatewayEvent) -> None:
        with self._lock:
            if self.coalesce_price_ticks and event.type == "price_tick":
                code = _price_tick_code(event)
                if code:
                    for index in range(len(self._events) - 1, -1, -1):
                        existing = self._events[index]
                        if existing.type == "price_tick" and _price_tick_code(existing) == code:
                            self._events[index] = event
                            return
            if self.coalesce_condition_events and event.type == "condition_event":
                key = _condition_event_key(event)
                if key:
                    for index in range(len(self._events) - 1, -1, -1):
                        existing = self._events[index]
                        if existing.type == "condition_event" and _condition_event_key(existing) == key:
                            self._events[index] = event
                            return
            self._events.append(event)
            while len(self._events) > self.max_size:
                self._drop_oldest_low_priority_event()

    def extend(self, events: Iterable[GatewayEvent]) -> None:
        for event in events:
            self.put(event)

    def drain(self, limit: int = 100) -> list[GatewayEvent]:
        drained: list[GatewayEvent] = []
        target = max(0, int(limit))
        with self._lock:
            if target <= 0:
                return []
            for event in list(self._events):
                if len(drained) >= target:
                    break
                if event.type in HIGH_PRIORITY_EVENT_TYPES:
                    self._events.remove(event)
                    drained.append(event)
            while len(drained) < target and self._events:
                drained.append(self._events.popleft())
        if self.coalesce_condition_events:
            drained = _coalesce_condition_events(drained)
        if self.coalesce_price_ticks:
            drained = _coalesce_ticks(drained)
        return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def _drop_oldest_low_priority_event(self) -> None:
        _drop_oldest_low_priority_event_from(self._events)


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


def _coalesce_condition_events(events: list[GatewayEvent]) -> list[GatewayEvent]:
    condition_indexes: OrderedDict[str, int] = OrderedDict()
    result: list[GatewayEvent] = []
    for event in events:
        if event.type != "condition_event":
            result.append(event)
            continue
        key = _condition_event_key(event)
        if not key:
            result.append(event)
            continue
        if key in condition_indexes:
            result[condition_indexes[key]] = event
        else:
            condition_indexes[key] = len(result)
            result.append(event)
    return result


def _price_tick_code(event: GatewayEvent) -> str:
    return str(event.payload.get("code") or event.payload.get("stock_code") or "").strip()


def _condition_event_key(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    code = _condition_event_code(payload)
    if not code:
        return ""
    return "|".join(
        [
            _payload_text(payload, "condition_name", "condition"),
            _payload_text(payload, "condition_index", "index"),
            code,
            _payload_text(payload, "event_type", "action").lower(),
            _payload_text(payload, "source"),
            _payload_text(payload, "strategy_profile", "profile"),
            _payload_text(payload, "purpose"),
        ]
    )


def _condition_event_code(payload: dict[str, Any]) -> str:
    value = str(payload.get("code") or payload.get("stock_code") or payload.get("symbol") or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        return value[1:]
    return value


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _low_priority_event(event: GatewayEvent) -> bool:
    return event.type == "price_tick"


def _drop_oldest_low_priority_event_from(events: deque[GatewayEvent]) -> None:
    for index, event in enumerate(events):
        if _low_priority_event(event):
            del events[index]
            return
    events.popleft()
