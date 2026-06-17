"""PostgreSQL writer skeleton.

No PostgreSQL driver is imported here. The production writer should drain
SQLite outbox rows asynchronously and must never run inline in the order path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from storage.interfaces import StoreHealth, StoreRole
from storage.outbox import OutboxEvent, OutboxPublishResult


@dataclass(frozen=True)
class PostgresWriterConfig:
    enabled: bool = False
    dsn_env: str = "TRADING_POSTGRES_DSN"
    batch_size: int = 500
    connect_timeout_sec: float = 2.0
    statement_timeout_sec: float = 5.0


class DisabledPostgresWriter:
    """Default writer for the design phase."""

    role = StoreRole.POSTGRES_WAREHOUSE

    def __init__(self, config: PostgresWriterConfig | None = None) -> None:
        self.config = config or PostgresWriterConfig(enabled=False)

    @property
    def enabled(self) -> bool:
        return False

    def health(self) -> StoreHealth:
        return StoreHealth(
            role=self.role,
            healthy=False,
            message="PostgreSQL writer is disabled; outbox events remain pending for future async replication.",
            details={"configured_enabled": self.config.enabled},
        )

    def publish_batch(self, events: Sequence[OutboxEvent]) -> OutboxPublishResult:
        return OutboxPublishResult(deferred=len(events))
