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

    @property
    def live_order_enabled(self) -> bool:
        return self.mode == "LIVE" and self.allow_live


def get_settings() -> CoreSettings:
    mode = os.environ.get("TRADING_MODE", "OBSERVE").strip().upper() or "OBSERVE"
    if mode not in {"OBSERVE", "DRY_RUN", "LIVE"}:
        mode = "OBSERVE"
    return CoreSettings(
        db_path=Path(os.environ.get("TRADING_DB_PATH", str(DEFAULT_DB_PATH))).expanduser(),
        local_token=os.environ.get("TRADING_CORE_TOKEN", DEFAULT_LOCAL_TOKEN),
        mode=mode,
        allow_live=os.environ.get("TRADING_ALLOW_LIVE", "0") == "1",
        max_order_amount=_int_env("TRADING_MAX_ORDER_AMOUNT", 3_000_000),
        max_daily_orders_per_code=_int_env("TRADING_MAX_DAILY_ORDERS_PER_CODE", 5),
        command_ttl_sec=_int_env("TRADING_ORDER_COMMAND_TTL_SEC", 30),
        command_max_attempts=_int_env("TRADING_ORDER_COMMAND_MAX_ATTEMPTS", 1),
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
