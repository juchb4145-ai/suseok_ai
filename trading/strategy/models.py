from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Optional, TypeVar, Union, get_args, get_origin, get_type_hints

from trading.strategy.reason_codes import normalize_reason_codes, standardize_details


class CandidateState(str, Enum):
    DETECTED = "DETECTED"
    WATCHING = "WATCHING"
    READY = "READY"
    ORDER_DECIDED = "ORDER_DECIDED"
    ORDER_SENT = "ORDER_SENT"
    FILLED = "FILLED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    REMOVED = "REMOVED"
    CANCELLED = "CANCELLED"


class CandidateSourceType(str, Enum):
    CONDITION = "condition"
    THEME_WATCH = "theme_watch"
    LEADING_STOCK = "leading_stock"
    MANUAL_DEBUG = "manual_debug"


class BlockType(str, Enum):
    NONE = "none"
    TEMPORARY = "temporary"
    FINAL = "final"


class StrategyProfile(str, Enum):
    KOSDAQ_THEME_PROFILE = "KOSDAQ_THEME_PROFILE"
    KOSPI_LEADER_PROFILE = "KOSPI_LEADER_PROFILE"
    SEMICONDUCTOR_SIGNAL_PROFILE = "SEMICONDUCTOR_SIGNAL_PROFILE"
    THEME_DISCOVERY_PROFILE = "THEME_DISCOVERY_PROFILE"


class OrderMode(str, Enum):
    OBSERVE = "OBSERVE"
    CONFIRM_A = "CONFIRM_A"
    AUTO_A = "AUTO_A"
    HYBRID = "HYBRID"


class VirtualOrderStatus(str, Enum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    FILLED = "filled"
    UNFILLED = "unfilled"
    CANCELLED = "cancelled"


class FillPolicy(str, Enum):
    OPTIMISTIC = "optimistic"
    NORMAL = "normal"
    CONSERVATIVE = "conservative"


class ReviewFinalStatus(str, Enum):
    BLOCKED_TEMP = "BLOCKED_TEMP"
    BLOCKED_FINAL = "BLOCKED_FINAL"
    EXPIRED = "EXPIRED"
    PLAN_NOT_CREATED = "PLAN_NOT_CREATED"
    VIRTUAL_SUBMITTED = "VIRTUAL_SUBMITTED"
    VIRTUAL_FILLED = "VIRTUAL_FILLED"
    VIRTUAL_UNFILLED = "VIRTUAL_UNFILLED"
    VIRTUAL_CANCELLED = "VIRTUAL_CANCELLED"
    VIRTUAL_PARTIAL_TAKE_PROFIT = "VIRTUAL_PARTIAL_TAKE_PROFIT"
    VIRTUAL_CLOSED_TAKE_PROFIT = "VIRTUAL_CLOSED_TAKE_PROFIT"
    VIRTUAL_CLOSED_SUPPORT_LOSS = "VIRTUAL_CLOSED_SUPPORT_LOSS"
    VIRTUAL_CLOSED_TIME_EXIT = "VIRTUAL_CLOSED_TIME_EXIT"
    VIRTUAL_CLOSED_TRAILING_STOP = "VIRTUAL_CLOSED_TRAILING_STOP"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


T = TypeVar("T", bound="SerializableDataclass")


class SerializableDataclass:
    def to_dict(self) -> dict[str, Any]:
        return _serialize_value(asdict(self))

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        hints = get_type_hints(cls)
        values: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in data:
                continue
            values[item.name] = _deserialize_value(hints.get(item.name, item.type), data[item.name])
        return cls(**values)


@dataclass
class Candidate(SerializableDataclass):
    id: Optional[int] = None
    trade_date: str = ""
    code: str = ""
    name: str = ""
    market: str = ""
    strategy_profile: Optional[StrategyProfile] = None
    sources: list[CandidateSourceType] = field(default_factory=list)
    priority: int = 0
    theme_ids: list[str] = field(default_factory=list)
    state: CandidateState = CandidateState.DETECTED
    detected_at: str = ""
    last_seen_at: str = ""
    expires_at: str = ""
    condition_names: list[str] = field(default_factory=list)
    block_type: BlockType = BlockType.NONE
    recheck_after_sec: int = 0
    can_recover: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateEvent(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    event_type: str = ""
    from_state: Optional[CandidateState] = None
    to_state: Optional[CandidateState] = None
    source: Optional[CandidateSourceType] = None
    reason: str = ""
    created_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class IndicatorSnapshot(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    code: str = ""
    created_at: str = ""
    price: int = 0
    vwap: Optional[float] = None
    ema20_5m: Optional[float] = None
    base_line_120: Optional[float] = None
    envelope_mid: Optional[float] = None
    day_high: int = 0
    day_low: int = 0
    day_mid: Optional[float] = None
    prev_high: int = 0
    prev_low: int = 0
    pullback_pct: Optional[float] = None
    volume_reaccel: bool = False
    failed_low_break_rebound: bool = False
    chase_risk: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateDecision(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    gate_name: str = ""
    passed: bool = False
    score: float = 0.0
    grade: str = ""
    block_type: BlockType = BlockType.NONE
    can_recover: bool = False
    recheck_after_sec: int = 0
    reason_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.reason_codes = normalize_reason_codes(self.reason_codes)
        self.details = standardize_details(
            self.details,
            self.reason_codes,
            passed=self.passed,
            score=self.score,
            created_at=self.created_at,
        )


@dataclass
class EntryPlan(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    entry_type: str = ""
    base_price_source: str = ""
    limit_price: int = 0
    tick_offset: int = 0
    max_chase_pct: float = 0.0
    split_plan: list[dict[str, Any]] = field(default_factory=list)
    order_timeout_sec: int = 0
    cancel_condition: dict[str, Any] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=dict)
    confirmation_signal: list[str] = field(default_factory=list)
    fill_policy: FillPolicy = FillPolicy.NORMAL
    created_at: str = ""


@dataclass
class VirtualOrder(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    entry_plan_id: Optional[int] = None
    leg_index: int = 1
    weight_pct: float = 100.0
    status: VirtualOrderStatus = VirtualOrderStatus.PLANNED
    limit_price: int = 0
    virtual_fill_price: int = 0
    fill_policy: FillPolicy = FillPolicy.NORMAL
    submitted_at: str = ""
    filled_at: str = ""
    cancelled_at: str = ""
    unfilled_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VirtualPosition(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    virtual_order_id: Optional[int] = None
    entry_price: int = 0
    quantity: int = 0
    opened_at: str = ""
    closed_at: str = ""
    close_price: int = 0
    close_reason: str = ""
    max_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    realized_return_pct: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitDecision(SerializableDataclass):
    id: Optional[int] = None
    virtual_position_id: Optional[int] = None
    decision_type: str = ""
    trigger_price: int = 0
    filled: bool = False
    fill_policy: FillPolicy = FillPolicy.NORMAL
    reason_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.reason_codes = normalize_reason_codes(self.reason_codes)
        self.details = standardize_details(
            self.details,
            self.reason_codes,
            passed=self.filled,
            created_at=self.created_at,
        )


@dataclass
class TradeReview(SerializableDataclass):
    id: Optional[int] = None
    candidate_id: Optional[int] = None
    trade_date: str = ""
    code: str = ""
    name: str = ""
    market: str = ""
    theme_id: str = ""
    theme_name: str = ""
    strategy_profile: str = ""
    gate_result_key: str = ""
    review_key: str = ""
    entry_plan_id: Optional[int] = None
    virtual_order_id: Optional[int] = None
    virtual_position_id: Optional[int] = None
    final_grade: str = ""
    final_status: str = ""
    virtual_order_status: str = ""
    exit_reason: str = ""
    entry_price: int = 0
    exit_price: int = 0
    max_return_5m: Optional[float] = None
    max_return_10m: Optional[float] = None
    max_return_20m: Optional[float] = None
    max_drawdown_20m: Optional[float] = None
    missed_reason: str = ""
    false_negative_flag: bool = False
    false_positive_flag: bool = False
    expired_but_later_rallied: bool = False
    blocked_but_later_rallied: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    return value


def _deserialize_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    enum_type = _optional_inner(annotation)
    if enum_type in {
        CandidateState,
        CandidateSourceType,
        BlockType,
        StrategyProfile,
        VirtualOrderStatus,
        FillPolicy,
        ReviewFinalStatus,
    }:
        return enum_type(value)
    if _is_list_of(annotation, CandidateSourceType):
        return [CandidateSourceType(item) for item in value]
    return value


def _optional_inner(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_list_of(annotation: Any, expected_item_type: Any) -> bool:
    return get_origin(annotation) is list and get_args(annotation) == (expected_item_type,)
