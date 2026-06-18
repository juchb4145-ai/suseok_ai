from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from trading.broker.models import utc_timestamp


class ReconcileSourceType(str, Enum):
    OPEN_ORDERS = "OPEN_ORDERS"
    ACCOUNT_POSITIONS = "ACCOUNT_POSITIONS"
    ACCOUNT_CASH = "ACCOUNT_CASH"


class ReconcileTrValidationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
    SIMULATION_CAPTURED = "SIMULATION_CAPTURED"
    PASS = "PASS"
    HOLD = "HOLD"
    INVALID = "INVALID"


class ReconcileRunStatus(str, Enum):
    DISABLED = "DISABLED"
    WAIT_GATEWAY = "WAIT_GATEWAY"
    WAIT_ACCOUNT = "WAIT_ACCOUNT"
    WAIT_CREDENTIAL = "WAIT_CREDENTIAL"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    CLEAN = "CLEAN"
    DISCREPANCY = "DISCREPANCY"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    FAILED = "FAILED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class ReconcileSourceStatus(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    CAPTURED = "CAPTURED"
    PARSED = "PARSED"
    VALID_EMPTY = "VALID_EMPTY"
    INVALID = "INVALID"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ReconcileParserStatus(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"
    VALID_EMPTY = "VALID_EMPTY"


class ReconcileDiscrepancySeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    STOP_NEW_BUY = "STOP_NEW_BUY"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH_RECOMMENDED = "KILL_SWITCH_RECOMMENDED"


@dataclass(frozen=True)
class ParsedNumber:
    raw_value: str = ""
    parsed_value: int | float | None = None
    field_present: bool = False
    parse_warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class BrokerOpenOrderSnapshot:
    account_token: str
    order_no: str
    original_order_no: str = ""
    code: str = ""
    side: str = ""
    order_quantity: int = 0
    order_price: int = 0
    filled_quantity: int = 0
    remaining_quantity: int = 0
    order_status: str = ""
    order_time: str = ""
    source: str = ReconcileSourceType.OPEN_ORDERS.value
    source_run_id: str = ""
    source_event_id: str = ""
    field_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    account_token: str
    code: str
    quantity: int = 0
    orderable_quantity: int = 0
    average_price: float = 0.0
    total_buy_amount: float = 0.0
    current_price: float = 0.0
    evaluation_amount: float = 0.0
    evaluation_pnl: float = 0.0
    profit_rate: float = 0.0
    source: str = ReconcileSourceType.ACCOUNT_POSITIONS.value
    source_run_id: str = ""
    source_event_id: str = ""
    field_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class BrokerCashSnapshot:
    account_token: str
    deposit: int = 0
    orderable_cash: int = 0
    withdrawable_cash: int = 0
    d1_estimated_deposit: int = 0
    d2_estimated_deposit: int = 0
    source: str = ReconcileSourceType.ACCOUNT_CASH.value
    source_run_id: str = ""
    source_event_id: str = ""
    field_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ReconcileTrParseResult:
    run_id: str
    logical_source: str
    parser_version: str
    parser_status: ReconcileParserStatus | str
    tr_code: str = ""
    rq_name: str = ""
    complete: bool = False
    valid_empty: bool = False
    page_count: int = 0
    row_count: int = 0
    open_orders: tuple[BrokerOpenOrderSnapshot, ...] = ()
    positions: tuple[BrokerPositionSnapshot, ...] = ()
    cash: BrokerCashSnapshot | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    raw_checksum: str = ""
    captured_at: str = field(default_factory=utc_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "logical_source": self.logical_source,
            "tr_code": self.tr_code,
            "rq_name": self.rq_name,
            "parser_version": self.parser_version,
            "parser_status": str(self.parser_status.value if isinstance(self.parser_status, Enum) else self.parser_status),
            "complete": self.complete,
            "valid_empty": self.valid_empty,
            "page_count": self.page_count,
            "row_count": self.row_count,
            "open_orders": [item.to_dict() for item in self.open_orders],
            "positions": [item.to_dict() for item in self.positions],
            "cash": self.cash.to_dict() if self.cash else None,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "raw_checksum": self.raw_checksum,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class BrokerReconcileDiscrepancy:
    category: str
    severity: ReconcileDiscrepancySeverity | str
    entity_type: str
    entity_key: str
    account_token: str = ""
    code: str = ""
    order_no: str = ""
    local: dict[str, Any] = field(default_factory=dict)
    chejan_projection: dict[str, Any] = field(default_factory=dict)
    tr_snapshot: dict[str, Any] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    first_detected_at: str = field(default_factory=utc_timestamp)
    last_detected_at: str = field(default_factory=utc_timestamp)
    status: str = "OPEN"
    operator_action_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": str(self.severity.value if isinstance(self.severity, Enum) else self.severity),
            "entity_type": self.entity_type,
            "entity_key": self.entity_key,
            "account_token": self.account_token,
            "code": self.code,
            "order_no": self.order_no,
            "local": dict(self.local),
            "chejan_projection": dict(self.chejan_projection),
            "tr_snapshot": dict(self.tr_snapshot),
            "reason_codes": list(self.reason_codes),
            "first_detected_at": self.first_detected_at,
            "last_detected_at": self.last_detected_at,
            "status": self.status,
            "operator_action_required": self.operator_action_required,
        }


def account_token(account: str) -> str:
    text = str(account or "").strip()
    if not text:
        return ""
    return "ACC_TOKEN_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def payload_checksum(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
