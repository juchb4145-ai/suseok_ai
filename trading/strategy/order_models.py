from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CANCEL_BUY = "CANCEL_BUY"
    CANCEL_SELL = "CANCEL_SELL"


class OrderIntentSource(str, Enum):
    REBOOT_ENTRY_ENGINE = "REBOOT_ENTRY_ENGINE"
    REBOOT_EXIT_ENGINE = "REBOOT_EXIT_ENGINE"
    MANUAL_OPERATOR = "MANUAL_OPERATOR"
    TEST_ONLY = "TEST_ONLY"


class OrderIntentStatus(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    COMMAND_QUEUED = "COMMAND_QUEUED"
    COMMAND_ACKED = "COMMAND_ACKED"
    COMMAND_REJECTED = "COMMAND_REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class ManagedOrderStatus(str, Enum):
    PENDING_LOCAL = "PENDING_LOCAL"
    QUEUED_TO_GATEWAY = "QUEUED_TO_GATEWAY"
    ACKED_BY_GATEWAY = "ACKED_BY_GATEWAY"
    REJECTED_BY_GATEWAY = "REJECTED_BY_GATEWAY"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class OrderRiskResult(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    WAIT = "WAIT"
    KILL_SWITCH = "KILL_SWITCH"


class OrderKillSwitchState(str, Enum):
    NORMAL = "NORMAL"
    STOP_NEW_BUY = "STOP_NEW_BUY"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"


@dataclass(frozen=True)
class OrderManagerConfig:
    enabled: bool = False
    mode: str = "OBSERVE"
    allow_live_sim_orders: bool = False
    require_simulation_broker: bool = True
    block_real_broker: bool = True
    live_sim_account_whitelist: tuple[str, ...] = ()
    max_order_quantity: int = 1
    max_order_amount: int = 100_000
    max_daily_buy_orders: int = 3
    max_daily_sell_orders: int = 10
    max_daily_orders_per_code: int = 1
    max_open_positions: int = 3
    max_theme_exposure_count: int = 2
    cancel_unfilled_after_sec: int = 45
    order_hoga: str = "00"
    use_limit_price: bool = True
    allow_market_order: bool = False
    daily_loss_limit_pct: float = -2.0
    daily_loss_limit_krw: int = 50_000
    consecutive_loss_limit: int = 2
    kill_switch_enabled: bool = True
    observe_only: bool = True
    decision_stale_after_sec: int = 180
    quote_stale_after_sec: int = 45
    max_spread_ticks: int = 5
    block_pyramiding: bool = True
    command_ttl_sec: int = 30
    command_max_attempts: int = 1
    cycle_bucket_sec: int = 30
    run_interval_sec: float = 1.0

    @classmethod
    def from_env(cls) -> "OrderManagerConfig":
        whitelist = tuple(
            item.strip()
            for item in os.getenv("TRADING_LIVE_SIM_ACCOUNT_WHITELIST", "").split(",")
            if item.strip()
        )
        return cls(
            enabled=_env_bool("TRADING_ORDER_MANAGER_ENABLED", False),
            mode=_env_choice("TRADING_ORDER_MANAGER_MODE", "OBSERVE", {"OBSERVE", "DRY_RUN", "LIVE_SIM"}),
            allow_live_sim_orders=_env_bool("TRADING_ALLOW_LIVE_SIM_ORDERS", False),
            require_simulation_broker=_env_bool("TRADING_REQUIRE_SIMULATION_BROKER", True),
            block_real_broker=_env_bool("TRADING_BLOCK_REAL_BROKER", True),
            live_sim_account_whitelist=whitelist,
            max_order_quantity=_env_int("TRADING_LIVE_SIM_MAX_ORDER_QUANTITY", 1),
            max_order_amount=_env_int("TRADING_LIVE_SIM_MAX_ORDER_AMOUNT", 100_000),
            max_daily_buy_orders=_env_int("TRADING_LIVE_SIM_MAX_DAILY_BUY_ORDERS", 3),
            max_daily_sell_orders=_env_int("TRADING_LIVE_SIM_MAX_DAILY_SELL_ORDERS", 10),
            max_daily_orders_per_code=_env_int("TRADING_LIVE_SIM_MAX_DAILY_ORDERS_PER_CODE", 1),
            max_open_positions=_env_int("TRADING_LIVE_SIM_MAX_OPEN_POSITIONS", 3),
            max_theme_exposure_count=_env_int("TRADING_LIVE_SIM_MAX_THEME_EXPOSURE_COUNT", 2),
            cancel_unfilled_after_sec=_env_int("TRADING_LIVE_SIM_CANCEL_UNFILLED_AFTER_SEC", 45),
            order_hoga=os.getenv("TRADING_LIVE_SIM_ORDER_HOGA", "00"),
            use_limit_price=_env_bool("TRADING_LIVE_SIM_USE_LIMIT_PRICE", True),
            allow_market_order=_env_bool("TRADING_LIVE_SIM_ALLOW_MARKET_ORDER", False),
            daily_loss_limit_pct=_env_float("TRADING_LIVE_SIM_DAILY_LOSS_LIMIT_PCT", -2.0),
            daily_loss_limit_krw=_env_int("TRADING_LIVE_SIM_DAILY_LOSS_LIMIT_KRW", 50_000),
            consecutive_loss_limit=_env_int("TRADING_LIVE_SIM_CONSECUTIVE_LOSS_LIMIT", 2),
            kill_switch_enabled=_env_bool("TRADING_LIVE_SIM_KILL_SWITCH_ENABLED", True),
            observe_only=_env_bool("TRADING_ORDER_MANAGER_OBSERVE_ONLY", True),
            decision_stale_after_sec=_env_int("TRADING_LIVE_SIM_DECISION_STALE_AFTER_SEC", 180),
            quote_stale_after_sec=_env_int("TRADING_LIVE_SIM_QUOTE_STALE_AFTER_SEC", 45),
            max_spread_ticks=_env_int("TRADING_LIVE_SIM_MAX_SPREAD_TICKS", 5),
            command_ttl_sec=_env_int("TRADING_LIVE_SIM_COMMAND_TTL_SEC", 30),
            command_max_attempts=_env_int("TRADING_LIVE_SIM_COMMAND_MAX_ATTEMPTS", 1),
            cycle_bucket_sec=_env_int("TRADING_LIVE_SIM_CYCLE_BUCKET_SEC", 30),
            run_interval_sec=_env_float("TRADING_ORDER_MANAGER_INTERVAL_SEC", 1.0),
        )


@dataclass(frozen=True)
class OrderRiskDecision:
    decision: str
    side: str
    code: str
    idempotency_key: str
    reason_codes: tuple[str, ...] = ()
    operator_message_ko: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == OrderRiskResult.PASS.value

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class ManagedOrderIntent:
    trade_date: str
    source: str
    side: str
    code: str
    quantity: int
    price: int
    idempotency_key: str
    created_at: str
    account: str = ""
    name: str = ""
    hoga: str = "00"
    status: str = OrderIntentStatus.CREATED.value
    intent_id: int | None = None
    candidate_id: int | None = None
    position_id: str = ""
    decision_id: int | None = None
    theme_id: str = ""
    theme_name: str = ""
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class ManagedOrder:
    intent_id: int
    trade_date: str
    side: str
    code: str
    quantity: int
    price: int
    idempotency_key: str
    created_at: str
    account: str = ""
    hoga: str = "00"
    status: str = ManagedOrderStatus.PENDING_LOCAL.value
    order_id: int | None = None
    source: str = ""
    candidate_id: int | None = None
    position_id: str = ""
    command_id: str = ""
    order_no: str = ""
    original_order_no: str = ""
    filled_quantity: int = 0
    remaining_quantity: int = 0
    avg_fill_price: float = 0.0
    sent_at: str = ""
    acked_at: str = ""
    updated_at: str = ""
    cancel_after_sec: int = 45
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class OrderExecutionReconcileResult:
    matched: bool
    order_id: int | None = None
    status: str = ""
    filled_quantity: int = 0
    remaining_quantity: int = 0
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class UnfilledCancelDecision:
    should_cancel: bool
    order_id: int | None = None
    code: str = ""
    order_no: str = ""
    remaining_quantity: int = 0
    idempotency_key: str = ""
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True)
class OrderManagerSnapshot:
    status: str
    mode: str
    enabled: bool
    live_sim_orders_allowed: bool
    broker_env: str = "UNKNOWN"
    account: str = ""
    account_whitelisted: bool = False
    risk_state: str = "UNKNOWN"
    kill_switch_state: str = OrderKillSwitchState.NORMAL.value
    today_buy_order_count: int = 0
    today_sell_order_count: int = 0
    open_order_count: int = 0
    pending_cancel_count: int = 0
    rejected_order_count: int = 0
    created_intent_count: int = 0
    queued_command_count: int = 0
    rejected_intent_count: int = 0
    last_order_at: str = ""
    last_reject_reason: str = ""
    warnings: tuple[str, ...] = ()
    recent_orders: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_to_dict(item) for item in value]
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = str(os.getenv(name, default) or default).strip().upper()
    return raw if raw in allowed else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, default)).strip()))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return float(default)
