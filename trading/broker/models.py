from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_message_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ConditionLoadState(str, Enum):
    IDLE = "IDLE"
    LOADING = "LOADING"
    LOADED = "LOADED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BrokerOrderRequest:
    account: str
    code: str
    quantity: int
    price: int
    side: str
    tag: str = ""
    order_type: int = 0
    hoga: str = "00"
    original_order_no: str = ""
    command_id: str = ""
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerOrderRequest":
        return cls(
            account=str(data.get("account") or ""),
            code=str(data.get("code") or ""),
            quantity=int(data.get("quantity") or 0),
            price=int(data.get("price") or 0),
            side=str(data.get("side") or ""),
            tag=str(data.get("tag") or ""),
            order_type=int(data.get("order_type") or 0),
            hoga=str(data.get("hoga") or "00"),
            original_order_no=str(data.get("original_order_no") or ""),
            command_id=str(data.get("command_id") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class BrokerOrderResult:
    ok: bool
    code: int
    message: str
    request: BrokerOrderRequest
    order_no: str = ""
    command_id: str = ""
    idempotency_key: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerOrderResult":
        request_raw = data.get("request") or {}
        request = request_raw if isinstance(request_raw, BrokerOrderRequest) else BrokerOrderRequest.from_dict(dict(request_raw))
        return cls(
            ok=bool(data.get("ok")),
            code=int(data.get("code", data.get("result_code", 0)) or 0),
            message=str(data.get("message") or ""),
            request=request,
            order_no=str(data.get("order_no") or ""),
            command_id=str(data.get("command_id") or request.command_id),
            idempotency_key=str(data.get("idempotency_key") or request.idempotency_key),
            raw=dict(data.get("raw") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class BrokerExecutionEvent:
    code: str
    order_no: str
    side: str
    quantity: int
    price: int
    filled_quantity: int
    remaining_quantity: int
    tag: str = ""
    account: str = ""
    execution_id: str = ""
    command_id: str = ""
    idempotency_key: str = ""
    timestamp: str = field(default_factory=utc_timestamp)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerExecutionEvent":
        return cls(
            code=str(data.get("code") or ""),
            order_no=str(data.get("order_no") or ""),
            side=str(data.get("side") or ""),
            quantity=int(data.get("quantity") or 0),
            price=int(data.get("price") or 0),
            filled_quantity=int(data.get("filled_quantity") or 0),
            remaining_quantity=int(data.get("remaining_quantity") or 0),
            tag=str(data.get("tag") or ""),
            account=str(data.get("account") or ""),
            execution_id=str(data.get("execution_id") or ""),
            command_id=str(data.get("command_id") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
            raw=dict(data.get("raw") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class BrokerPriceTick:
    code: str
    price: int
    change_rate: float = 0.0
    volume: int = 0
    best_ask: int = 0
    best_bid: int = 0
    trade_value: float = 0.0
    execution_strength: float = 0.0
    spread_ticks: int = 0
    trade_time: str = ""
    open_price: int = 0
    instrument_type: str = "stock"
    name: str = ""
    day_high: int = 0
    day_low: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_timestamp)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerPriceTick":
        volume = data.get("volume", data.get("cum_volume", 0))
        return cls(
            code=str(data.get("code") or ""),
            price=_abs_int(data.get("price")),
            change_rate=_float(data.get("change_rate")),
            volume=_abs_int(volume),
            best_ask=_abs_int(data.get("best_ask")),
            best_bid=_abs_int(data.get("best_bid")),
            trade_value=max(0.0, _float(data.get("trade_value"))),
            execution_strength=max(0.0, _float(data.get("execution_strength"))),
            spread_ticks=_abs_int(data.get("spread_ticks")),
            trade_time=str(data.get("trade_time") or ""),
            open_price=_abs_int(data.get("open_price")),
            instrument_type=str(data.get("instrument_type") or "stock"),
            name=str(data.get("name") or ""),
            day_high=_abs_int(data.get("day_high")),
            day_low=_abs_int(data.get("day_low")),
            metadata=dict(data.get("metadata") or {}),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class BrokerConditionEvent:
    condition_name: str
    code: str
    condition_index: int = -1
    event_type: str = "include"
    source: str = "condition"
    strategy_profile: str = ""
    purpose: str = ""
    timestamp: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerConditionEvent":
        return cls(
            condition_name=str(data.get("condition_name") or ""),
            code=str(data.get("code") or ""),
            condition_index=int(data.get("condition_index", -1) or -1),
            event_type=str(data.get("event_type") or "include"),
            source=str(data.get("source") or "condition"),
            strategy_profile=str(data.get("strategy_profile") or ""),
            purpose=str(data.get("purpose") or ""),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


ConditionCandidateEvent = BrokerConditionEvent


@dataclass(frozen=True)
class BrokerTrRequest:
    rq_name: str
    tr_code: str
    screen_no: str
    inputs: dict[str, str] = field(default_factory=dict)
    prev_next: int = 0
    request_id: str = ""
    command_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerTrRequest":
        return cls(
            rq_name=str(data.get("rq_name") or ""),
            tr_code=str(data.get("tr_code") or ""),
            screen_no=str(data.get("screen_no") or ""),
            inputs={str(key): str(value) for key, value in dict(data.get("inputs") or {}).items()},
            prev_next=int(data.get("prev_next") or 0),
            request_id=str(data.get("request_id") or ""),
            command_id=str(data.get("command_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class BrokerTrResponse:
    rq_name: str
    tr_code: str
    record_name: str = ""
    prev_next: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    error_code: str = ""
    message: str = ""
    request_id: str = ""
    timestamp: str = field(default_factory=utc_timestamp)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BrokerTrResponse":
        return cls(
            rq_name=str(data.get("rq_name") or ""),
            tr_code=str(data.get("tr_code") or ""),
            record_name=str(data.get("record_name") or ""),
            prev_next=str(data.get("prev_next") or ""),
            rows=[dict(row) for row in list(data.get("rows") or [])],
            error_code=str(data.get("error_code") or ""),
            message=str(data.get("message") or ""),
            request_id=str(data.get("request_id") or ""),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class GatewayEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: new_message_id("evt"))
    request_id: str = ""
    timestamp: str = field(default_factory=utc_timestamp)
    source: str = "kiwoom_gateway"
    command_id: str = ""
    idempotency_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GatewayEvent":
        return cls(
            type=str(data.get("type") or ""),
            payload=dict(data.get("payload") or {}),
            event_id=str(data.get("event_id") or new_message_id("evt")),
            request_id=str(data.get("request_id") or ""),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
            source=str(data.get("source") or "kiwoom_gateway"),
            command_id=str(data.get("command_id") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class GatewayCommand:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: new_message_id("cmd"))
    request_id: str = ""
    timestamp: str = field(default_factory=utc_timestamp)
    source: str = "core"
    idempotency_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GatewayCommand":
        return cls(
            type=str(data.get("type") or ""),
            payload=dict(data.get("payload") or {}),
            command_id=str(data.get("command_id") or new_message_id("cmd")),
            request_id=str(data.get("request_id") or ""),
            timestamp=str(data.get("timestamp") or utc_timestamp()),
            source=str(data.get("source") or "core"),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class ConditionInfo:
    index: int
    name: str


class Signal:
    def __init__(self) -> None:
        self._handlers: list[Callable] = []

    def connect(self, handler: Callable) -> None:
        self._handlers.append(handler)

    def emit(self, *args, **kwargs) -> None:
        for handler in list(self._handlers):
            handler(*args, **kwargs)


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    return value


def _abs_int(value: Any) -> int:
    if value is None:
        return 0
    raw = str(value).strip().replace(",", "").replace("+", "")
    if not raw:
        return 0
    try:
        return abs(int(float(raw)))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    raw = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
