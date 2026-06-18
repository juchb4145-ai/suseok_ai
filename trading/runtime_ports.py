from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Protocol, Sequence

from trading.broker.models import GatewayCommand, GatewayEvent

if TYPE_CHECKING:
    from trading.strategy.entry_engine import EntryDecision
    from trading.strategy.order_models import ManagedOrder, ManagedOrderIntent
else:
    EntryDecision = Any
    ManagedOrder = Any
    ManagedOrderIntent = Any


class CoreEventType(str, Enum):
    GATEWAY_EVENT_APPENDED = "gateway_event_appended"
    MARKET_DATA_UPDATED = "market_data_updated"
    CANDIDATE_TRANSITIONED = "candidate_transitioned"
    ENTRY_DECIDED = "entry_decided"
    ORDER_INTENT_CREATED = "order_intent_created"
    ORDER_STATE_CHANGED = "order_state_changed"
    DASHBOARD_SNAPSHOT_READY = "dashboard_snapshot_ready"
    SYSTEM_HEALTH_CHANGED = "system_health_changed"


class CandidateRuntimeState(str, Enum):
    DISCOVERED = "DISCOVERED"
    HYDRATING = "HYDRATING"
    WATCHING = "WATCHING"
    SETUP_READY = "SETUP_READY"
    TIMING_READY = "TIMING_READY"
    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    ORDER_PENDING = "ORDER_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    REMOVED = "REMOVED"
    EXPIRED = "EXPIRED"


class BlockingStage(str, Enum):
    NONE = "NONE"
    WAIT_DATA = "WAIT_DATA"
    WAIT_MARKET = "WAIT_MARKET"
    WAIT_THEME = "WAIT_THEME"
    WAIT_PRICE = "WAIT_PRICE"
    BLOCK_RISK = "BLOCK_RISK"


class EntryStep(str, Enum):
    DATA_READY = "DATA_READY"
    THEME_READY = "THEME_READY"
    MARKET_ALLOWED = "MARKET_ALLOWED"
    ROLE_ALLOWED = "ROLE_ALLOWED"
    PRICE_TIMING_READY = "PRICE_TIMING_READY"
    RISK_PRECHECK = "RISK_PRECHECK"


class StepResult(str, Enum):
    PASS = "PASS"
    WAIT = "WAIT"
    DATA_WAIT = "DATA_WAIT"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class EventLogRecord:
    event_id: str
    event_type: str
    dedupe_key: str
    received_at: str = ""
    payload_json: str = "{}"
    id: int = 0
    source: str = "kiwoom_gateway"
    command_id: str = ""
    code: str = ""
    trade_date: str = ""
    processed_at: str = ""
    processing_status: str = "PENDING"
    error: str = ""
    created_at: str = ""
    processing_attempts: int = 0
    claimed_at: str = ""
    claimed_by: str = ""
    next_retry_at: str = ""
    handler_name: str = ""
    handler_version: str = ""
    last_attempt_at: str = ""
    dead_lettered_at: str = ""
    processing_result_json: str = "{}"


@dataclass(frozen=True)
class EventLogAppendResult:
    appended: bool
    record: EventLogRecord | None = None
    duplicate: bool = False
    ignored: bool = False
    reason: str = ""
    warning: str = ""


@dataclass(frozen=True)
class CoreEvent:
    type: CoreEventType | str
    event_id: str
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    source_event_id: str = ""


@dataclass(frozen=True)
class CandleSnapshot:
    interval_sec: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    started_at: str
    updated_at: str


@dataclass(frozen=True)
class MarketDataSnapshot:
    code: str
    price: int
    tick_at: str = ""
    received_at: str = ""
    name: str = ""
    change_rate: float = 0.0
    trade_value: float = 0.0
    cum_volume: int = 0
    best_ask: int = 0
    best_bid: int = 0
    open_price: int = 0
    tick_timestamp: str = ""
    tick_age_sec: float = 0.0
    freshness_status: str = "UNKNOWN"
    data_quality_status: str = "UNKNOWN"
    price_source: str = ""
    updated_at: str = ""
    source_event_id: str = ""
    is_fresh: bool = False
    tick_age_ms: int = 0
    data_quality: str = "UNKNOWN"
    reason_codes: tuple[str, ...] = ()
    vwap: float = 0.0
    turnover: float = 0.0
    execution_strength: float = 0.0
    spread_ticks: int = 0
    day_high: int = 0
    day_low: int = 0
    candles: tuple[CandleSnapshot, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateStateTransition:
    candidate_id: str
    code: str
    from_state: CandidateRuntimeState | str
    to_state: CandidateRuntimeState | str
    occurred_at: str
    reason_code: str
    transition_id: str = ""
    trade_date: str = ""
    reason_codes: tuple[str, ...] = ()
    blocking_stage: BlockingStage | str = BlockingStage.NONE
    source_event_id: str = ""
    source_event_type: str = ""
    source_component: str = ""
    next_required_action: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryEvaluationStep:
    step: EntryStep | str
    result: StepResult | str
    reason_codes: tuple[str, ...] = ()
    next_required_action: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryDecisionEnvelope:
    decision: EntryDecision
    candidate_state: CandidateRuntimeState | str
    blocking_stage: BlockingStage | str
    steps: tuple[EntryEvaluationStep, ...] = ()
    dirty_reason: str = ""
    source_event_ids: tuple[str, ...] = ()
    next_required_action: str = ""


@dataclass(frozen=True)
class OrderIntent:
    intent: ManagedOrderIntent
    decision_id: str
    candidate_id: str
    idempotency_key: str
    created_at: str
    source_event_ids: tuple[str, ...] = ()
    risk_precheck_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedOrderEnvelope:
    order: ManagedOrder
    intent_id: str
    command_id: str = ""
    gateway_order_no: str = ""
    last_gateway_event_id: str = ""
    reconcile_required: bool = False
    reason_codes: tuple[str, ...] = ()


class EventLogPort(Protocol):
    def append_gateway_event(self, event: GatewayEvent, *, dedupe_key: str = "") -> EventLogAppendResult:
        ...

    def pending_gateway_events(self, *, limit: int = 100) -> Sequence[EventLogRecord]:
        ...

    def mark_processed(self, event_id: str, *, processed_at: str, core_events: Sequence[CoreEvent] = ()) -> None:
        ...

    def mark_failed(self, event_id: str, *, error: str) -> None:
        ...

    def claim_pending_events(
        self,
        *,
        limit: int,
        event_types: Sequence[str] | None = None,
        worker_id: str = "",
        lease_sec: int = 30,
        now: Any = None,
    ) -> Sequence[EventLogRecord]:
        ...

    def claim_event(self, event_log_id: int | str, *, worker_id: str = "", lease_sec: int = 30) -> EventLogRecord | None:
        ...

    def mark_processing_result(self, event_log_id: int | str, *, status: str, result: dict[str, Any] | None = None) -> None:
        ...

    def mark_retry_wait(self, event_log_id: int | str, *, error: str, next_retry_at: str) -> None:
        ...

    def mark_dead_letter(self, event_log_id: int | str, *, error: str) -> None:
        ...

    def mark_ignored(self, event_log_id: int | str, *, reason: str) -> None:
        ...

    def recover_stale_claims(self, *, now: Any = None) -> int:
        ...

    def get_event(self, event_log_id: int | str) -> EventLogRecord | None:
        ...

    def get_by_event_id(self, event_id: str) -> EventLogRecord | None:
        ...

    def critical_backlog_snapshot(self) -> dict[str, Any]:
        ...


class GatewayEventConsumerPort(Protocol):
    def consume_live_event(self, event: GatewayEvent) -> Any:
        ...

    def consume_event_log_record(self, record: EventLogRecord) -> Any:
        ...

    def dispatch(self, event: GatewayEvent, *, source_event_id: str = "") -> Any:
        ...

    def consumer_health(self) -> dict[str, Any]:
        ...

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


class EventReplayPort(Protocol):
    def replay_pending(self, *, limit: int | None = None) -> dict[str, Any]:
        ...

    def recover_stale_claims(self) -> int:
        ...


class GatewayCommandPort(Protocol):
    def enqueue_command(self, command: GatewayCommand, *, metadata: dict[str, Any] | None = None) -> Any:
        ...

    def dispatch_commands(self, *, limit: int = 20) -> Sequence[GatewayCommand]:
        ...

    def command_snapshot(self) -> dict[str, Any]:
        ...


class MarketDataServicePort(Protocol):
    def apply_gateway_event(self, event: GatewayEvent) -> tuple[MarketDataSnapshot | None, tuple[str, ...]]:
        ...

    def latest_snapshot(self, code: str) -> MarketDataSnapshot | None:
        ...

    def dirty_codes(self, *, limit: int = 1000) -> Sequence[str]:
        ...

    def flush_batch(self) -> dict[str, Any]:
        ...


class CandidateFsmPort(Protocol):
    def apply_event(self, event: GatewayEvent, market_data: MarketDataSnapshot | None = None) -> Sequence[CandidateStateTransition]:
        ...

    def transition(self, transition: CandidateStateTransition) -> CandidateStateTransition:
        ...

    def candidates_for_codes(self, codes: Iterable[str]) -> Sequence[Any]:
        ...


class StrategyEvaluatorPort(Protocol):
    def evaluate_dirty_codes(self, codes: Iterable[str], *, now: str) -> Sequence[EntryDecisionEnvelope]:
        ...


class RiskManagerPort(Protocol):
    def precheck(self, decision: EntryDecisionEnvelope) -> EntryEvaluationStep:
        ...

    def approve_intent(self, intent: OrderIntent) -> tuple[bool, tuple[str, ...], dict[str, Any]]:
        ...


class OrderManagerPort(Protocol):
    def create_intent(self, decision: EntryDecisionEnvelope) -> OrderIntent | None:
        ...

    def enqueue_approved_intent(self, intent: OrderIntent) -> ManagedOrderEnvelope | None:
        ...

    def apply_gateway_event(self, event: GatewayEvent) -> Sequence[ManagedOrderEnvelope]:
        ...

    def reconcile(self, *, now: str) -> dict[str, Any]:
        ...


class DashboardReadModelPort(Protocol):
    def update_snapshot(self, events: Sequence[CoreEvent], *, snapshot_at: str) -> None:
        ...

    def build_from_runtime(
        self,
        runtime_snapshot: dict[str, Any] | None,
        gateway_snapshot: dict[str, Any] | None,
        command_snapshot: dict[str, Any] | None,
        core_status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        ...

    def write_if_due(self, *, now: Any = None) -> dict[str, Any]:
        ...

    def save_snapshot(self, snapshot: dict[str, Any]) -> Any:
        ...

    def read_main_snapshot(self) -> dict[str, Any]:
        ...

    def read_snapshot(self, view_name: str) -> dict[str, Any]:
        ...

    def mark_dirty(self, reason: str) -> None:
        ...

    def snapshot_status(self) -> dict[str, Any]:
        ...

    def recover_latest_snapshot(self) -> dict[str, Any]:
        ...


__all__ = [
    "BlockingStage",
    "CandidateFsmPort",
    "CandidateRuntimeState",
    "CandidateStateTransition",
    "CandleSnapshot",
    "CoreEvent",
    "CoreEventType",
    "DashboardReadModelPort",
    "EntryDecisionEnvelope",
    "EntryEvaluationStep",
    "EntryStep",
    "EventLogAppendResult",
    "EventLogPort",
    "EventLogRecord",
    "EventReplayPort",
    "GatewayCommandPort",
    "GatewayEventConsumerPort",
    "ManagedOrderEnvelope",
    "MarketDataServicePort",
    "MarketDataSnapshot",
    "OrderIntent",
    "OrderManagerPort",
    "RiskManagerPort",
    "StepResult",
    "StrategyEvaluatorPort",
]
