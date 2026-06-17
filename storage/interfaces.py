"""Storage role contracts for the mixed Memory/SQLite/PostgreSQL design."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


JsonMap = Mapping[str, Any]


class StoreRole(str, Enum):
    MEMORY_HOT = "memory_hot"
    SQLITE_OPERATIONAL = "sqlite_operational"
    POSTGRES_WAREHOUSE = "postgres_warehouse"


@dataclass(frozen=True)
class StoreHealth:
    role: StoreRole
    healthy: bool
    message: str = ""
    details: JsonMap = field(default_factory=dict)


@dataclass(frozen=True)
class OperationalRecoveryState:
    trade_date: str
    gateway_commands: Sequence[JsonMap] = field(default_factory=tuple)
    dedupe_keys: Sequence[JsonMap] = field(default_factory=tuple)
    order_results: Sequence[JsonMap] = field(default_factory=tuple)
    executions: Sequence[JsonMap] = field(default_factory=tuple)
    open_positions: Sequence[JsonMap] = field(default_factory=tuple)
    watchset: Sequence[JsonMap] = field(default_factory=tuple)
    candidates: Sequence[JsonMap] = field(default_factory=tuple)
    decisions: Sequence[JsonMap] = field(default_factory=tuple)
    latest_theme_rank_snapshot: JsonMap = field(default_factory=dict)
    kill_switch_state: JsonMap = field(default_factory=dict)


@runtime_checkable
class MemoryHotStoreProtocol(Protocol):
    """In-process state used by intraday strategy and risk decisions."""

    def update_tick(self, code: str, tick: JsonMap) -> None: ...

    def latest_tick(self, code: str) -> JsonMap | None: ...

    def update_candle(self, code: str, interval: str, candle: JsonMap) -> None: ...

    def candles(self, code: str, interval: str, limit: int = 100) -> Sequence[JsonMap]: ...

    def update_theme_board(self, snapshot: JsonMap) -> None: ...

    def update_market_regime(self, snapshot: JsonMap) -> None: ...

    def update_open_position_view(self, position_id: str, snapshot: JsonMap) -> None: ...

    def runtime_view(self, code: str) -> JsonMap: ...


@runtime_checkable
class OperationalStoreProtocol(Protocol):
    """Local SQLite-backed store required for same-day recovery and order safety."""

    def health(self) -> StoreHealth: ...

    def load_recovery_state(self, trade_date: str) -> OperationalRecoveryState: ...

    def append_event_journal(self, stream: str, event_type: str, key: str, payload: JsonMap) -> str: ...

    def append_outbox_event(self, topic: str, key: str, payload: JsonMap, metadata: JsonMap | None = None) -> str: ...

    def close(self) -> None: ...


@runtime_checkable
class WarehouseStoreProtocol(Protocol):
    """Long-term analytical store. It must not be a synchronous order-path dependency."""

    def health(self) -> StoreHealth: ...

    def write_rows(self, dataset: str, rows: Sequence[JsonMap]) -> int: ...


@runtime_checkable
class PostgresWriterProtocol(Protocol):
    """Async writer that drains SQLite outbox rows into PostgreSQL Warehouse tables."""

    def health(self) -> StoreHealth: ...

    def publish_batch(self, events: Sequence[Any]) -> Any: ...
