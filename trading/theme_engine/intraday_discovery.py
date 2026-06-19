from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, new_message_id
from trading.theme_engine.opening_runtime import (
    OPENING_RQ_NAME,
    OPENING_SCREEN_NO,
    OPENING_TR_CODE,
    OPT10032_FIELDS,
    OpeningBurstRuntimeConfig,
    ParsedOpeningSeedRow,
    opt10032_seed_inputs,
    parse_opt10032_seed_rows,
)


INTRADAY_TURNOVER_SEED_PURPOSE = "intraday_turnover_seed"
INTRADAY_OUTPUT_MODE = "OBSERVE"
KST = timezone(timedelta(hours=9), "KST")


class IntradayDiscoveryPhase(str, Enum):
    MORNING = "MORNING"
    MIDDAY = "MIDDAY"
    ROTATION = "ROTATION"
    LATE = "LATE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class IntradayDiscoveryConfig:
    enabled: bool = False
    observe_only: bool = True
    trading_mode: str = "OBSERVE"
    start: str = "09:20"
    end: str = "15:00"
    morning_interval_sec: int = 300
    midday_interval_sec: int = 600
    rotation_interval_sec: int = 300
    late_interval_sec: int = 600
    top_n: int = 100
    signal_ttl_sec: int = 600
    max_pending_commands: int = 1
    queue_depth_limit: int = 5
    tr_ttl_sec: int = 60
    screen_no: str = OPENING_SCREEN_NO
    opt10032_market_code: str = "000"
    opt10032_include_management: str = "0"
    opt10032_exchange_code: str = "3"

    @classmethod
    def from_env(cls, *, trading_mode: str | None = None) -> "IntradayDiscoveryConfig":
        return cls(
            enabled=_env_bool("TRADING_INTRADAY_THEME_DISCOVERY_ENABLED", True),
            observe_only=True,
            trading_mode=str(trading_mode or os.getenv("TRADING_MODE", "OBSERVE") or "OBSERVE").upper(),
            start=str(os.getenv("TRADING_INTRADAY_THEME_DISCOVERY_START", "09:20") or "09:20"),
            end=str(os.getenv("TRADING_INTRADAY_THEME_DISCOVERY_END", "15:00") or "15:00"),
            morning_interval_sec=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_MORNING_INTERVAL_SEC", 300)),
            midday_interval_sec=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_MIDDAY_INTERVAL_SEC", 600)),
            rotation_interval_sec=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_ROTATION_INTERVAL_SEC", 300)),
            late_interval_sec=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_LATE_INTERVAL_SEC", 600)),
            top_n=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_TOP_N", 100)),
            signal_ttl_sec=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_SIGNAL_TTL_SEC", 600)),
            max_pending_commands=max(0, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_MAX_PENDING_COMMANDS", 1)),
            queue_depth_limit=max(1, _env_int("TRADING_INTRADAY_THEME_DISCOVERY_QUEUE_DEPTH_LIMIT", 5)),
        )

    def opening_compatible_config(self) -> OpeningBurstRuntimeConfig:
        return OpeningBurstRuntimeConfig(
            enabled=True,
            observe_only=True,
            trading_mode=self.trading_mode,
            top_n_per_call=self.top_n,
            tr_screen_no=self.screen_no,
            opt10032_market_code=self.opt10032_market_code,
            opt10032_include_management=self.opt10032_include_management,
            opt10032_exchange_code=self.opt10032_exchange_code,
        )


@dataclass(frozen=True)
class IntradayDiscoveryRequest:
    trade_date: str
    bucket: str
    phase: str
    idempotency_key: str
    top_n: int = 100


@dataclass(frozen=True)
class IntradayDiscoveryRow:
    stock_code: str
    stock_name: str = ""
    rank: int = 0
    current_turnover_krw: float = 0.0
    change_rate_pct: float = 0.0
    observed_at: str = ""
    session_phase: str = ""
    raw_json: dict[str, Any] | None = None
    parser_status: str = "OK"


@dataclass(frozen=True)
class IntradayDiscoveryBatch:
    trade_date: str
    observed_at: str
    session_phase: str
    batch_id: str = ""
    rows: tuple[IntradayDiscoveryRow, ...] = ()
    parser_status: str = "OK"


class IntradayDiscoveryScheduler:
    def __init__(self, gateway_state: GatewayStateStore, *, config: IntradayDiscoveryConfig | None = None) -> None:
        self.gateway_state = gateway_state
        self.config = config or IntradayDiscoveryConfig.from_env()
        self._last_bucket_by_phase: dict[str, str] = {}

    def enqueue_if_due(self, now: datetime) -> dict[str, Any]:
        current = _as_kst(now)
        phase = _phase(current)
        summary = {
            "enabled": self.config.enabled,
            "status": "WAITING",
            "scheduled": False,
            "enqueued": False,
            "duplicate": False,
            "paused_reason": "",
            "phase": phase.value,
            "bucket": "",
            "ready_allowed": False,
            "order_intent_allowed": False,
            "output_mode": INTRADAY_OUTPUT_MODE,
        }
        if not self.config.enabled:
            summary["status"] = "DISABLED"
            summary["paused_reason"] = "DISABLED"
            return summary
        if self.config.observe_only and self.config.trading_mode != "OBSERVE":
            summary["status"] = "SKIPPED"
            summary["paused_reason"] = "NOT_OBSERVE_MODE"
            return summary
        if phase == IntradayDiscoveryPhase.CLOSED:
            summary["status"] = "SKIPPED"
            summary["paused_reason"] = "OUTSIDE_DISCOVERY_WINDOW"
            return summary
        if self._pending_count() >= self.config.max_pending_commands:
            summary["status"] = "SKIPPED"
            summary["paused_reason"] = "PENDING_DISCOVERY_COMMAND_EXISTS"
            return summary
        if self._queue_depth() >= self.config.queue_depth_limit:
            summary["status"] = "SKIPPED"
            summary["paused_reason"] = "COMMAND_QUEUE_DEPTH_LIMIT"
            return summary
        bucket = _bucket(current, _interval_for_phase(phase, self.config))
        summary["bucket"] = bucket
        if self._last_bucket_by_phase.get(phase.value) == bucket:
            summary["status"] = "SKIPPED"
            summary["paused_reason"] = "BUCKET_ALREADY_REQUESTED"
            return summary
        request = self.request_for(current, phase=phase, bucket=bucket)
        if self.gateway_state.has_duplicate(request.idempotency_key):
            summary["status"] = "SKIPPED"
            summary["duplicate"] = True
            summary["paused_reason"] = "DUPLICATE_BUCKET"
            return summary
        command = intraday_discovery_tr_command(self.config, request)
        result = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.NORMAL,
            ttl_sec=self.config.tr_ttl_sec,
            max_attempts=1,
            metadata={"runtime": "intraday_theme_discovery", "purpose": INTRADAY_TURNOVER_SEED_PURPOSE},
            now=now,
        )
        self._last_bucket_by_phase[phase.value] = bucket
        summary["scheduled"] = True
        summary["enqueued"] = bool(result.accepted)
        summary["status"] = "QUEUED" if result.accepted else "REJECTED"
        summary["idempotency_key"] = request.idempotency_key
        if not result.accepted:
            summary["paused_reason"] = str(result.reason or "ENQUEUE_REJECTED")
        return summary

    def request_for(self, now: datetime, *, phase: IntradayDiscoveryPhase, bucket: str) -> IntradayDiscoveryRequest:
        trade_date = _as_kst(now).date().isoformat()
        key = f"intraday_theme_discovery:seed:{trade_date}:{phase.value}:{bucket}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        return IntradayDiscoveryRequest(
            trade_date=trade_date,
            bucket=bucket,
            phase=phase.value,
            idempotency_key=f"{key}:{digest}",
            top_n=self.config.top_n,
        )

    def _pending_count(self) -> int:
        return len(self.gateway_state.list_commands(status="QUEUED", command_type="tr_request", include_finished=False, limit=20))

    def _queue_depth(self) -> int:
        return int(dict(self.gateway_state.command_snapshot() or {}).get("queued_count") or 0)


class IntradayDiscoveryRuntimePipeline:
    def __init__(self, *, gateway_state: GatewayStateStore, config: IntradayDiscoveryConfig | None = None) -> None:
        self.gateway_state = gateway_state
        self.config = config or IntradayDiscoveryConfig.from_env()
        self.scheduler = IntradayDiscoveryScheduler(gateway_state, config=self.config)
        self.last_summary: dict[str, Any] = {"enabled": self.config.enabled, "status": "IDLE"}

    def run_if_due(self, now: datetime) -> dict[str, Any]:
        self.last_summary = self.scheduler.enqueue_if_due(now)
        return dict(self.last_summary)


def intraday_discovery_tr_command(config: IntradayDiscoveryConfig, request: IntradayDiscoveryRequest) -> GatewayCommand:
    cfg = config.opening_compatible_config()
    return GatewayCommand(
        type="tr_request",
        command_id=new_message_id("cmd_intraday_seed"),
        idempotency_key=request.idempotency_key,
        source="strategy_runtime",
        payload={
            "purpose": INTRADAY_TURNOVER_SEED_PURPOSE,
            "response_mode": "capture",
            "tr_code": OPENING_TR_CODE,
            "rq_name": f"{OPENING_RQ_NAME}_Intraday",
            "screen_no": config.screen_no,
            "inputs": opt10032_seed_inputs(cfg),
            "fields": list(OPT10032_FIELDS),
            "trade_date": request.trade_date,
            "seed_scope": "INTRADAY",
            "session_phase": request.phase,
            "bucket": request.bucket,
            "top_n": request.top_n,
            "ready_allowed": False,
            "order_intent_allowed": False,
        },
    )


def parse_intraday_discovery_rows(rows: Iterable[Mapping[str, Any]], *, observed_at: str, session_phase: str) -> IntradayDiscoveryBatch:
    parsed = parse_opt10032_seed_rows(rows, batch_time=observed_at)
    return IntradayDiscoveryBatch(
        trade_date=observed_at[:10],
        observed_at=observed_at,
        session_phase=session_phase,
        rows=tuple(_row_from_parsed(row, observed_at=observed_at, session_phase=session_phase) for row in parsed.rows),
        parser_status=parsed.parser_status,
    )


def _row_from_parsed(row: ParsedOpeningSeedRow, *, observed_at: str, session_phase: str) -> IntradayDiscoveryRow:
    return IntradayDiscoveryRow(
        stock_code=row.seed.stock_code,
        stock_name=row.seed.stock_name,
        rank=row.seed.seed_rank,
        current_turnover_krw=row.seed.turnover_krw,
        change_rate_pct=row.seed.change_rate_pct,
        observed_at=observed_at,
        session_phase=session_phase,
        raw_json=dict(row.seed.raw or {}),
        parser_status=row.parser_status,
    )


def _phase(now: datetime) -> IntradayDiscoveryPhase:
    current = now.time()
    if time(9, 20) <= current < time(11, 0):
        return IntradayDiscoveryPhase.MORNING
    if time(11, 0) <= current < time(13, 20):
        return IntradayDiscoveryPhase.MIDDAY
    if time(13, 20) <= current < time(14, 30):
        return IntradayDiscoveryPhase.ROTATION
    if time(14, 30) <= current < time(15, 0):
        return IntradayDiscoveryPhase.LATE
    return IntradayDiscoveryPhase.CLOSED


def _interval_for_phase(phase: IntradayDiscoveryPhase, config: IntradayDiscoveryConfig) -> int:
    return {
        IntradayDiscoveryPhase.MORNING: config.morning_interval_sec,
        IntradayDiscoveryPhase.MIDDAY: config.midday_interval_sec,
        IntradayDiscoveryPhase.ROTATION: config.rotation_interval_sec,
        IntradayDiscoveryPhase.LATE: config.late_interval_sec,
    }.get(phase, 999999)


def _bucket(now: datetime, interval_sec: int) -> str:
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    bucket_seconds = seconds - (seconds % max(1, interval_sec))
    hour = bucket_seconds // 3600
    minute = (bucket_seconds % 3600) // 60
    return f"{hour:02d}:{minute:02d}"


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(KST).replace(microsecond=0)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "INTRADAY_TURNOVER_SEED_PURPOSE",
    "IntradayDiscoveryBatch",
    "IntradayDiscoveryConfig",
    "IntradayDiscoveryPhase",
    "IntradayDiscoveryRequest",
    "IntradayDiscoveryRow",
    "IntradayDiscoveryRuntimePipeline",
    "IntradayDiscoveryScheduler",
    "intraday_discovery_tr_command",
    "parse_intraday_discovery_rows",
]
