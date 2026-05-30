from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from storage.db import TradingDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"
DEFAULT_LOCAL_TOKEN = "local-dev-token"


@dataclass(frozen=True)
class CoreSettings:
    db_path: Path
    local_token: str
    mode: str = "OBSERVE"
    allow_live: bool = False
    max_order_amount: int = 3_000_000
    max_daily_orders_per_code: int = 5
    command_ttl_sec: int = 30
    command_max_attempts: int = 1
    command_dedupe_retention_sec: int = 86400
    command_history_retention_sec: int = 604800
    command_recovery_expire_stale_dispatched: bool = True
    runtime_enabled: bool = False
    runtime_auto_start: bool = False
    runtime_mode: str = "OBSERVE"
    runtime_evaluation_interval_sec: int = 5
    runtime_cycle_timeout_sec: int = 30
    runtime_allow_dry_run_orders: bool = False
    runtime_allow_live_orders: bool = False
    runtime_require_gateway_heartbeat: bool = True
    runtime_require_kiwoom_login: bool = True
    runtime_require_orderable_for_order: bool = True
    runtime_dry_run_account: str = ""
    runtime_dry_run_position_amount: int = 1_000_000
    runtime_dry_run_min_quantity: int = 1
    runtime_dry_run_hoga: str = "00"
    runtime_dry_run_order_type_buy: int = 1
    runtime_dry_run_order_type_sell: int = 2
    runtime_dry_run_require_account: bool = False
    runtime_dry_run_respect_weight_pct: bool = True

    @property
    def live_order_enabled(self) -> bool:
        return self.mode == "LIVE" and self.allow_live


def get_settings() -> CoreSettings:
    mode = os.environ.get("TRADING_MODE", "OBSERVE").strip().upper() or "OBSERVE"
    if mode not in {"OBSERVE", "DRY_RUN", "LIVE"}:
        mode = "OBSERVE"
    runtime_mode = os.environ.get("TRADING_RUNTIME_MODE", "OBSERVE").strip().upper() or "OBSERVE"
    if runtime_mode not in {"OBSERVE", "DRY_RUN"}:
        runtime_mode = "OBSERVE"
    return CoreSettings(
        db_path=Path(os.environ.get("TRADING_DB_PATH", str(DEFAULT_DB_PATH))).expanduser(),
        local_token=os.environ.get("TRADING_CORE_TOKEN", DEFAULT_LOCAL_TOKEN),
        mode=mode,
        allow_live=os.environ.get("TRADING_ALLOW_LIVE", "0") == "1",
        max_order_amount=_int_env("TRADING_MAX_ORDER_AMOUNT", 3_000_000),
        max_daily_orders_per_code=_int_env("TRADING_MAX_DAILY_ORDERS_PER_CODE", 5),
        command_ttl_sec=_int_env("TRADING_ORDER_COMMAND_TTL_SEC", 30),
        command_max_attempts=_int_env("TRADING_ORDER_COMMAND_MAX_ATTEMPTS", 1),
        command_dedupe_retention_sec=_int_env("TRADING_COMMAND_DEDUPE_RETENTION_SEC", 86400),
        command_history_retention_sec=_int_env("TRADING_COMMAND_HISTORY_RETENTION_SEC", 604800),
        command_recovery_expire_stale_dispatched=os.environ.get(
            "TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED", "1"
        )
        != "0",
        runtime_enabled=_bool_env("TRADING_RUNTIME_ENABLED", False),
        runtime_auto_start=_bool_env("TRADING_RUNTIME_AUTO_START", False),
        runtime_mode=runtime_mode,
        runtime_evaluation_interval_sec=_int_env("TRADING_RUNTIME_EVALUATION_INTERVAL_SEC", 5),
        runtime_cycle_timeout_sec=_int_env("TRADING_RUNTIME_CYCLE_TIMEOUT_SEC", 30),
        runtime_allow_dry_run_orders=_bool_env("TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS", False),
        runtime_allow_live_orders=_bool_env("TRADING_RUNTIME_ALLOW_LIVE_ORDERS", False),
        runtime_require_gateway_heartbeat=_bool_env("TRADING_RUNTIME_REQUIRE_GATEWAY_HEARTBEAT", True),
        runtime_require_kiwoom_login=_bool_env("TRADING_RUNTIME_REQUIRE_KIWOOM_LOGIN", True),
        runtime_require_orderable_for_order=_bool_env("TRADING_RUNTIME_REQUIRE_ORDERABLE_FOR_ORDER", True),
        runtime_dry_run_account=os.environ.get("TRADING_RUNTIME_DRY_RUN_ACCOUNT", ""),
        runtime_dry_run_position_amount=_int_env("TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT", 1_000_000),
        runtime_dry_run_min_quantity=_int_env("TRADING_RUNTIME_DRY_RUN_MIN_QUANTITY", 1),
        runtime_dry_run_hoga=os.environ.get("TRADING_RUNTIME_DRY_RUN_HOGA", "00"),
        runtime_dry_run_order_type_buy=_int_env("TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_BUY", 1),
        runtime_dry_run_order_type_sell=_int_env("TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_SELL", 2),
        runtime_dry_run_require_account=_bool_env("TRADING_RUNTIME_DRY_RUN_REQUIRE_ACCOUNT", False),
        runtime_dry_run_respect_weight_pct=_bool_env("TRADING_RUNTIME_DRY_RUN_RESPECT_WEIGHT_PCT", True),
    )


def open_database() -> TradingDatabase:
    return TradingDatabase(str(get_settings().db_path))


def close_database(db: TradingDatabase) -> None:
    try:
        db.close()
    except Exception:
        pass


def extract_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_local_token:
        return x_local_token.strip()
    query_token = request.query_params.get("token")
    return str(query_token or "").strip()


def verify_gateway_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> None:
    expected = get_settings().local_token
    provided = extract_token(request, authorization=authorization, x_local_token=x_local_token)
    if not expected or provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid local gateway token",
        )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
