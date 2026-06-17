"""Outbox event models for future SQLite-to-PostgreSQL replication."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERED = "DELIVERED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True)
class OutboxEvent:
    topic: str
    key: str
    payload: Mapping[str, Any]
    event_type: str = ""
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=utc_now)
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    next_attempt_at: str = ""
    delivered_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def mark_in_flight(self) -> "OutboxEvent":
        return replace(self, status=OutboxStatus.IN_FLIGHT, attempts=self.attempts + 1)

    def mark_delivered(self, delivered_at: str | None = None) -> "OutboxEvent":
        return replace(
            self,
            status=OutboxStatus.DELIVERED,
            delivered_at=delivered_at or utc_now(),
        )

    def mark_retryable_failure(self, next_attempt_at: str, error: str = "") -> "OutboxEvent":
        metadata = {**dict(self.metadata), "last_error": error}
        return replace(
            self,
            status=OutboxStatus.FAILED_RETRYABLE,
            next_attempt_at=next_attempt_at,
            metadata=metadata,
        )

    def mark_dead_letter(self, error: str = "") -> "OutboxEvent":
        metadata = {**dict(self.metadata), "last_error": error}
        return replace(self, status=OutboxStatus.DEAD_LETTER, metadata=metadata)


@dataclass(frozen=True)
class OutboxPublishResult:
    accepted: int = 0
    deferred: int = 0
    failed: int = 0
    error: str = ""


class InMemoryOutboxBuffer:
    """Tiny test/development buffer; production outbox belongs in SQLite."""

    def __init__(self, events: Sequence[OutboxEvent] | None = None) -> None:
        self._events = list(events or [])

    def append(self, event: OutboxEvent) -> str:
        self._events.append(event)
        return event.event_id

    def pending(self, limit: int = 100) -> list[OutboxEvent]:
        return [
            event
            for event in self._events
            if event.status in {OutboxStatus.PENDING, OutboxStatus.FAILED_RETRYABLE}
        ][: max(0, int(limit))]

    def replace(self, event: OutboxEvent) -> None:
        for index, existing in enumerate(self._events):
            if existing.event_id == event.event_id:
                self._events[index] = event
                return
        self._events.append(event)

    def all_events(self) -> list[OutboxEvent]:
        return list(self._events)
