"""Warehouse store skeleton.

The real PostgreSQL implementation is intentionally deferred. The disabled
store makes the non-goal explicit: callers must not rely on synchronous
Warehouse writes for order execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from storage.interfaces import JsonMap, StoreHealth, StoreRole
from storage.outbox import OutboxEvent


@dataclass(frozen=True)
class WarehouseWritePlan:
    dataset: str
    rows: Sequence[JsonMap]
    idempotency_key: str = ""


class DeferredWarehouseStore:
    """No-op Warehouse adapter used until PostgreSQL writer is enabled."""

    role = StoreRole.POSTGRES_WAREHOUSE

    def health(self) -> StoreHealth:
        return StoreHealth(
            role=self.role,
            healthy=False,
            message="PostgreSQL warehouse is not configured; SQLite operational path remains authoritative.",
        )

    def write_rows(self, dataset: str, rows: Sequence[JsonMap]) -> int:
        return 0

    def to_outbox_event(self, plan: WarehouseWritePlan) -> OutboxEvent:
        return OutboxEvent(
            topic=plan.dataset,
            key=plan.idempotency_key or plan.dataset,
            event_type="warehouse_write_requested",
            payload={"dataset": plan.dataset, "rows": list(plan.rows)},
        )
