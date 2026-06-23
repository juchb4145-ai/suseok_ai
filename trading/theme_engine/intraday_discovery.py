from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, new_message_id
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
        phase = _phase(current, self.config)
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
    def __init__(self, *, gateway_state: GatewayStateStore, db: Any | None = None, config: IntradayDiscoveryConfig | None = None) -> None:
        self.gateway_state = gateway_state
        self.db = db
        self.config = config or IntradayDiscoveryConfig.from_env()
        self.scheduler = IntradayDiscoveryScheduler(gateway_state, config=self.config)
        self.last_summary: dict[str, Any] = {"enabled": self.config.enabled, "status": "IDLE"}
        self.recovery_run_count = 0
        self.last_recovery_summary: dict[str, Any] = {
            "status": "IDLE",
            "recovery_status": "IDLE",
            "recovery_run_count": 0,
            "recovered_count": 0,
            "duplicate_skipped_count": 0,
            "failed_count": 0,
        }
        self._recovered_command_keys: set[str] = set()

    def run_if_due(self, now: datetime) -> dict[str, Any]:
        self.last_summary = self.scheduler.enqueue_if_due(now)
        return dict(self.last_summary)

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type not in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
            return False
        payload = dict(event.payload or {})
        if str(payload.get("purpose") or "") != INTRADAY_TURNOVER_SEED_PURPOSE:
            return False
        if self.db is None:
            self.last_summary = {
                "enabled": self.config.enabled,
                "status": "ERROR",
                "paused_reason": "DB_UNAVAILABLE",
                "ready_allowed": False,
                "order_intent_allowed": False,
                "output_mode": INTRADAY_OUTPUT_MODE,
            }
            return True
        batch = self._batch_from_event(event, payload)
        saved = self._save_batch(batch)
        self.last_summary = {
            "enabled": self.config.enabled,
            "status": batch["status"],
            "trade_date": batch["trade_date"],
            "phase": batch["session_phase"],
            "bucket": batch["bucket"],
            "last_ack_at": batch["observed_at"] if event.type == "command_ack" else "",
            "last_batch_row_count": int(batch["row_count"] or 0),
            "parser_status": batch["parser_status"],
            "failure_count": 1 if batch["status"] in {"FAILED", "TIMEOUT", "EXPIRED", "EMPTY", "PARSE_ERROR"} else 0,
            "saved_batch_id": int(dict(saved or {}).get("id") or 0),
            "ready_allowed": False,
            "order_intent_allowed": False,
            "output_mode": INTRADAY_OUTPUT_MODE,
        }
        return True

    def recover_from_command_history(self, *, limit: int = 100) -> dict[str, Any]:
        if self.db is None:
            return {"status": "DISABLED", "recovered_count": 0, "reason": "DB_UNAVAILABLE"}
        self.recovery_run_count += 1
        recovered = 0
        skipped = 0
        duplicate_skipped = 0
        failed = 0
        unique_errors = 0
        last_error = ""
        seen_this_run: set[str] = set()
        for record in self.gateway_state.list_commands(command_type="tr_request", include_finished=True, limit=limit):
            command = dict(record.get("command") or {})
            command_payload = dict(command.get("payload") or {})
            result_payload = dict(record.get("result_payload") or {})
            if str(command_payload.get("purpose") or result_payload.get("purpose") or "") != INTRADAY_TURNOVER_SEED_PURPOSE:
                skipped += 1
                continue
            payload = {**command_payload, **result_payload}
            payload.setdefault("command_id", record.get("command_id") or command.get("command_id") or "")
            payload.setdefault("idempotency_key", record.get("idempotency_key") or command.get("idempotency_key") or "")
            recovery_key = _recovery_key(record, payload)
            if recovery_key in seen_this_run or recovery_key in self._recovered_command_keys:
                duplicate_skipped += 1
                continue
            seen_this_run.add(recovery_key)
            event_type = "command_ack" if str(record.get("status") or "").upper() in {"ACKED", "SUCCEEDED", "SUCCESS"} else "command_failed"
            event = GatewayEvent(type=event_type, command_id=str(payload.get("command_id") or ""), idempotency_key=str(payload.get("idempotency_key") or ""), payload=payload)
            try:
                if self.handle_event(event):
                    recovered += 1
                    self._recovered_command_keys.add(recovery_key)
            except Exception as exc:
                failed += 1
                last_error = str(exc)
                if "UNIQUE constraint failed" in last_error:
                    unique_errors += 1
        status = "OK" if failed == 0 else "PARTIAL"
        summary = {
            "status": status,
            "recovery_status": status,
            "recovery_run_count": self.recovery_run_count,
            "recovered_count": recovered,
            "skipped_count": skipped,
            "duplicate_skipped_count": duplicate_skipped,
            "failed_count": failed,
            "unique_constraint_error_count": unique_errors,
            "last_recovery_at": datetime.now().replace(microsecond=0).isoformat(),
        }
        if last_error:
            summary["last_error"] = last_error
        self.last_recovery_summary = dict(summary)
        return summary

    def _batch_from_event(self, event: GatewayEvent, payload: Mapping[str, Any]) -> dict[str, Any]:
        command_id = str(payload.get("command_id") or event.command_id or "")
        idempotency_key = str(payload.get("idempotency_key") or event.idempotency_key or "")
        fallback = _request_fields_from_idempotency(idempotency_key)
        trade_date = str(payload.get("trade_date") or fallback.get("trade_date") or event.timestamp[:10])
        session_phase = str(payload.get("session_phase") or fallback.get("session_phase") or "")
        bucket = str(payload.get("bucket") or fallback.get("bucket") or "")
        observed_at = str(payload.get("observed_at") or payload.get("batch_time") or _observed_at(trade_date, bucket, event.timestamp))
        raw_rows = _extract_rows(payload)
        status = _event_status(event.type, raw_rows)
        error = str(payload.get("error") or payload.get("message") or "")
        if status != "OK":
            return {
                "trade_date": trade_date,
                "observed_at": observed_at,
                "session_phase": session_phase,
                "bucket": bucket,
                "command_id": command_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "parser_status": status,
                "row_count": len(raw_rows),
                "parsed_count": 0,
                "error": error,
                "raw_summary": _raw_summary(payload, raw_rows),
                "rows": [],
            }
        parsed = parse_intraday_discovery_rows(raw_rows, observed_at=observed_at, session_phase=session_phase)
        rows = [
            {
                "stock_code": row.stock_code,
                "stock_name": row.stock_name,
                "rank": row.rank,
                "current_turnover_krw": row.current_turnover_krw,
                "change_rate_pct": row.change_rate_pct,
                "observed_at": row.observed_at,
                "session_phase": row.session_phase,
                "parser_status": row.parser_status,
                "raw": row.raw_json or {},
            }
            for row in parsed.rows
        ]
        parser_status = parsed.parser_status
        save_status = "PARSE_ERROR" if parser_status not in {"OK", "PARTIAL"} and rows else "OK"
        return {
            "trade_date": trade_date,
            "observed_at": observed_at,
            "session_phase": session_phase,
            "bucket": bucket,
            "command_id": command_id,
            "idempotency_key": idempotency_key,
            "status": save_status,
            "parser_status": parser_status,
            "row_count": len(raw_rows),
            "parsed_count": len(rows),
            "error": "" if save_status == "OK" else parser_status,
            "raw_summary": _raw_summary(payload, raw_rows),
            "rows": rows,
        }

    def _save_batch(self, batch: Mapping[str, Any]) -> dict[str, Any]:
        saver = getattr(self.db, "save_intraday_theme_discovery_batch", None)
        if not callable(saver):
            return {}
        return dict(saver(dict(batch)) or {})


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


def _extract_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources: list[Any] = [
        payload.get("rows"),
        payload.get("captured_rows"),
        payload.get("merged_rows"),
        payload.get("tr_rows"),
    ]
    for container_key in ("raw", "result", "result_payload"):
        container = payload.get(container_key)
        if isinstance(container, Mapping):
            sources.extend(
                [
                    container.get("rows"),
                    container.get("captured_rows"),
                    container.get("merged_rows"),
                    container.get("tr_rows"),
                ]
            )
    for source in sources:
        if source:
            return [dict(row or {}) for row in list(source or [])]
    return []


def _event_status(event_type: str, rows: Iterable[Mapping[str, Any]]) -> str:
    if event_type == "command_failed":
        return "FAILED"
    if event_type == "command_timeout":
        return "TIMEOUT"
    if event_type == "command_expired":
        return "EXPIRED"
    return "OK" if list(rows) else "EMPTY"


def _raw_summary(payload: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(list(rows)),
        "keys": sorted(str(key) for key in payload.keys()),
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _observed_at(trade_date: str, bucket: str, event_timestamp: str) -> str:
    if trade_date and bucket:
        return f"{trade_date}T{bucket}:00"
    return str(event_timestamp or "")


def _request_fields_from_idempotency(idempotency_key: str) -> dict[str, str]:
    parts = str(idempotency_key or "").split(":")
    if len(parts) < 7 or parts[:3] != ["intraday_theme_discovery", "seed"]:
        return {}
    return {
        "trade_date": parts[3],
        "session_phase": parts[4],
        "bucket": f"{parts[5]}:{parts[6]}",
    }


def _recovery_key(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    command_id = str(payload.get("command_id") or record.get("command_id") or "")
    if command_id:
        return f"command:{command_id}"
    idempotency_key = str(payload.get("idempotency_key") or record.get("idempotency_key") or "")
    if idempotency_key:
        return f"idempotency:{idempotency_key}"
    trade_date = str(payload.get("trade_date") or "")
    session_phase = str(payload.get("session_phase") or "")
    bucket = str(payload.get("bucket") or "")
    return f"natural:{trade_date}:{session_phase}:{bucket}"


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


def _phase(now: datetime, config: IntradayDiscoveryConfig) -> IntradayDiscoveryPhase:
    current = now.time()
    start = _parse_hhmm(config.start, time(9, 20))
    end = _parse_hhmm(config.end, time(15, 0))
    if current < start or current >= end:
        return IntradayDiscoveryPhase.CLOSED
    if start <= current < time(11, 0):
        return IntradayDiscoveryPhase.MORNING
    if time(11, 0) <= current < time(13, 20):
        return IntradayDiscoveryPhase.MIDDAY
    if time(13, 20) <= current < time(14, 30):
        return IntradayDiscoveryPhase.ROTATION
    if time(14, 30) <= current < end:
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


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = str(value or "").split(":", 1)
        return time(max(0, min(23, int(hour))), max(0, min(59, int(minute))))
    except (TypeError, ValueError):
        return default


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
