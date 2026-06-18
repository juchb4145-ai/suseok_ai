"""SQLite operational store skeleton.

This wrapper documents the intended boundary around the existing SQLite
TradingDatabase and SQLiteCommandStore without changing current call paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from storage.db import TradingDatabase
from storage.event_log import EventLogRepository
from storage.interfaces import OperationalRecoveryState, StoreHealth, StoreRole
from storage.outbox import InMemoryOutboxBuffer, OutboxEvent
from trading.broker.command_persistence import SQLiteCommandStore


@dataclass(frozen=True)
class SQLiteOperationalStoreConfig:
    db_path: Path
    dedupe_retention_sec: int | None = None
    history_retention_sec: int | None = None


class SQLiteOperationalStore:
    """Boundary object for same-day operational state and recovery."""

    role = StoreRole.SQLITE_OPERATIONAL

    def __init__(
        self,
        config: SQLiteOperationalStoreConfig,
        *,
        database: TradingDatabase | None = None,
        command_store: SQLiteCommandStore | None = None,
        event_log_store: EventLogRepository | None = None,
        outbox: InMemoryOutboxBuffer | None = None,
    ) -> None:
        self.config = config
        self.database = database or TradingDatabase(str(config.db_path))
        self.command_store = command_store or SQLiteCommandStore(
            config.db_path,
            dedupe_retention_sec=config.dedupe_retention_sec,
            history_retention_sec=config.history_retention_sec,
        )
        self.event_log_store = event_log_store or EventLogRepository(config.db_path)
        self.outbox = outbox or InMemoryOutboxBuffer()

    def health(self) -> StoreHealth:
        return StoreHealth(
            role=self.role,
            healthy=True,
            message="SQLite operational store is available.",
            details={"db_path": str(self.config.db_path)},
        )

    def load_recovery_state(self, trade_date: str) -> OperationalRecoveryState:
        records = self.command_store.list_records(
            trade_date=trade_date,
            include_finished=True,
            limit=10000,
        )
        return OperationalRecoveryState(
            trade_date=trade_date,
            gateway_commands=tuple(record.to_dict() for record in records),
        )

    def append_event_journal(self, stream: str, event_type: str, key: str, payload: Mapping[str, Any]) -> str:
        event = OutboxEvent(
            topic=f"operational.{stream}",
            key=key,
            event_type=event_type,
            payload=dict(payload),
        )
        return self.outbox.append(event)

    def append_outbox_event(
        self,
        topic: str,
        key: str,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        event = OutboxEvent(
            topic=topic,
            key=key,
            payload=dict(payload),
            metadata=dict(metadata or {}),
        )
        return self.outbox.append(event)

    def close(self) -> None:
        self.event_log_store.close()
        self.command_store.close()
        self.database.close()
