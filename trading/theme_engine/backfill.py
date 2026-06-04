from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from trading.broker.command_queue import CommandPriority, CommandStatus, ORDER_COMMAND_TYPES
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, new_message_id


THEME_BACKFILL_PURPOSE = "theme_data_backfill"
TR_OPT10001 = "opt10001"
TR_OPT10081 = "opt10081"
OPT10001_FIELDS = ["종목명", "현재가", "등락율", "거래량", "거래대금", "시가", "고가", "저가", "기준가"]
OPT10081_FIELDS = ["일자", "현재가", "시가", "고가", "저가", "거래량"]
ACTIVE_STATUSES = {CommandStatus.QUEUED.value, CommandStatus.DISPATCHED.value}


@dataclass(frozen=True)
class ThemeBackfillConfig:
    enabled: bool = False
    max_per_cycle: int = 3
    max_pending: int = 5
    ttl_sec: int = 90
    opt10001_bucket_sec: int = 300
    opt10081_bucket_sec: int = 1800
    allow_opt10081: bool = False
    allow_regular_session: bool = True

    @classmethod
    def from_env(cls) -> "ThemeBackfillConfig":
        return cls(
            enabled=_bool_env("TRADING_THEME_BACKFILL_ENABLED", False),
            max_per_cycle=_int_env("TRADING_THEME_BACKFILL_MAX_PER_CYCLE", 3),
            max_pending=_int_env("TRADING_THEME_BACKFILL_MAX_PENDING", 5),
            ttl_sec=_int_env("TRADING_THEME_BACKFILL_TTL_SEC", 90),
            opt10001_bucket_sec=_int_env("TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC", 300),
            opt10081_bucket_sec=_int_env("TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC", 1800),
            allow_opt10081=_bool_env("TRADING_THEME_BACKFILL_ALLOW_OPT10081", False),
            allow_regular_session=_bool_env("TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION", True),
        )


@dataclass
class BackfillCandidate:
    code: str
    tr_code: str
    primary_theme_id: str
    related_theme_ids: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    priority: str = "MEDIUM"
    rank: int = 999999


class ThemeBackfillService:
    def __init__(
        self,
        gateway_state: GatewayStateStore,
        *,
        config: ThemeBackfillConfig | None = None,
    ) -> None:
        self.gateway_state = gateway_state
        self.config = config or ThemeBackfillConfig.from_env()
        self.last_summary = _empty_summary(self.config)

    def plan_and_enqueue(self, result: Any, now: datetime) -> dict[str, Any]:
        cfg = self.config
        summary = _empty_summary(cfg)
        summary["candidate_count"] = len(build_backfill_candidates(result, cfg=cfg, now=now))
        if not cfg.enabled:
            summary["paused_reason"] = "DISABLED"
            self.last_summary = summary
            return summary
        pause_reason = enqueue_pause_reason(self.gateway_state, result)
        if pause_reason:
            summary["paused_reason"] = pause_reason
            _increment_pause(summary, pause_reason)
            self.last_summary = summary
            return summary
        pending = active_theme_backfill_records(self.gateway_state)
        summary["queued_count"] = len([item for item in pending if item.get("status") == CommandStatus.QUEUED.value])
        summary["dispatched_count"] = len([item for item in pending if item.get("status") == CommandStatus.DISPATCHED.value])
        if len(pending) >= cfg.max_pending:
            summary["paused_reason"] = "MAX_PENDING"
            self.last_summary = summary
            return summary
        enqueued = 0
        for candidate in build_backfill_candidates(result, cfg=cfg, now=now):
            if enqueued >= cfg.max_per_cycle or len(pending) + enqueued >= cfg.max_pending:
                break
            command = command_for_candidate(candidate, cfg=cfg, now=now)
            if self.gateway_state.has_duplicate(command.idempotency_key):
                summary["duplicated_bucket_count"] += 1
                continue
            enqueue = self.gateway_state.enqueue_command(
                command,
                priority=CommandPriority.LOW,
                ttl_sec=cfg.ttl_sec,
                max_attempts=1,
                metadata={"purpose": THEME_BACKFILL_PURPOSE, "priority": candidate.priority},
                now=_as_utc(now),
            )
            if enqueue.accepted:
                enqueued += 1
                summary["enqueued_count"] += 1
            elif enqueue.reason == "DUPLICATE_COMMAND":
                summary["duplicated_bucket_count"] += 1
        summary["queued_count"] += enqueued
        self.last_summary = summary
        return summary


def build_backfill_candidates(result: Any, *, cfg: ThemeBackfillConfig, now: datetime) -> list[BackfillCandidate]:
    aggregate: dict[tuple[str, str], BackfillCandidate] = {}
    for rank, theme in enumerate(list(getattr(result, "themes", ()) or ()), start=1):
        priority = _theme_backfill_priority(theme)
        if priority not in {"HIGH", "MEDIUM"}:
            continue
        theme_id = str(getattr(theme, "theme_id", "") or "")
        for hit in getattr(theme, "member_hits", ()) or ():
            if bool(getattr(hit, "excluded", False)):
                continue
            code = _normalize_code(getattr(hit, "symbol", ""))
            if not code:
                continue
            missing = _missing_fields(hit)
            if not missing:
                continue
            _merge_candidate(
                aggregate,
                BackfillCandidate(
                    code=code,
                    tr_code=TR_OPT10001,
                    primary_theme_id=theme_id,
                    related_theme_ids=[theme_id] if theme_id else [],
                    missing_fields=missing,
                    priority=priority,
                    rank=rank,
                ),
            )
            if cfg.allow_opt10081 and "prev_close" in missing:
                _merge_candidate(
                    aggregate,
                    BackfillCandidate(
                        code=code,
                        tr_code=TR_OPT10081,
                        primary_theme_id=theme_id,
                        related_theme_ids=[theme_id] if theme_id else [],
                        missing_fields=["prev_close"],
                        priority=priority,
                        rank=rank,
                    ),
                )
    return sorted(
        aggregate.values(),
        key=lambda item: (0 if item.priority == "HIGH" else 1, item.rank, item.code, 0 if item.tr_code == TR_OPT10001 else 1),
    )


def command_for_candidate(candidate: BackfillCandidate, *, cfg: ThemeBackfillConfig, now: datetime) -> GatewayCommand:
    bucket = bucket_for(candidate.tr_code, now, cfg)
    idempotency_key = f"theme_backfill:{_trade_date(now)}:{candidate.code}:{candidate.tr_code}:{bucket}"
    if candidate.tr_code == TR_OPT10081:
        inputs = {"종목코드": candidate.code, "기준일자": _trade_date(now).replace("-", ""), "수정주가구분": "1"}
        fields = OPT10081_FIELDS
        rq_name = "ThemeBackfill_opt10081"
    else:
        inputs = {"종목코드": candidate.code}
        fields = OPT10001_FIELDS
        rq_name = "ThemeBackfill_opt10001"
    return GatewayCommand(
        type="tr_request",
        command_id=new_message_id("cmd"),
        idempotency_key=idempotency_key,
        payload={
            "purpose": THEME_BACKFILL_PURPOSE,
            "response_mode": "capture",
            "code": candidate.code,
            "primary_theme_id": candidate.primary_theme_id,
            "related_theme_ids": list(dict.fromkeys(candidate.related_theme_ids)),
            "missing_fields": list(dict.fromkeys(candidate.missing_fields)),
            "tr_code": candidate.tr_code,
            "rq_name": rq_name,
            "screen_no": "8700",
            "inputs": inputs,
            "fields": fields,
            "trade_date": _trade_date(now),
            "bucket": bucket,
            "backfill_priority": candidate.priority,
        },
    )


def enqueue_pause_reason(gateway_state: GatewayStateStore, result: Any) -> str:
    if _has_ready_like(result):
        return CommandStatus.SKIPPED_READY.value
    active = _active_records(gateway_state)
    if any(_command_type(record) in ORDER_COMMAND_TYPES for record in active):
        return CommandStatus.SKIPPED_ORDER_PENDING.value
    if any(not is_theme_backfill_record(record) for record in active):
        return CommandStatus.SKIPPED_NON_BACKFILL_PENDING.value
    snapshot = gateway_state.snapshot().to_dict()
    if not snapshot.get("connected") or not snapshot.get("heartbeat_ok") or not snapshot.get("kiwoom_logged_in"):
        return CommandStatus.SKIPPED_GATEWAY_UNHEALTHY.value
    return ""


def apply_dispatch_guard(gateway_state: GatewayStateStore, raw_theme_lab: dict[str, Any] | None = None) -> dict[str, Any]:
    skipped: dict[str, int] = {}
    active = _active_records(gateway_state)
    backfill = [record for record in active if is_theme_backfill_record(record)]
    if not backfill:
        return {"skipped": skipped}
    for record in backfill:
        if _status(record) == CommandStatus.QUEUED.value and _is_expired_record(record):
            command_id = str(record.get("command_id") or "")
            if command_id:
                gateway_state.ack_command(
                    command_id,
                    status=CommandStatus.EXPIRED_BEFORE_DISPATCH.value,
                    result_payload={"purpose": THEME_BACKFILL_PURPOSE, "skipped_reason": CommandStatus.EXPIRED_BEFORE_DISPATCH.value},
                    error=CommandStatus.EXPIRED_BEFORE_DISPATCH.value,
                )
                skipped[CommandStatus.EXPIRED_BEFORE_DISPATCH.value] = skipped.get(CommandStatus.EXPIRED_BEFORE_DISPATCH.value, 0) + 1
    active = _active_records(gateway_state)
    backfill = [record for record in active if is_theme_backfill_record(record)]
    if not backfill:
        return {"skipped": skipped}
    reason = dispatch_pause_reason(gateway_state, raw_theme_lab or {}, active)
    if not reason:
        return {"skipped": skipped}
    for record in backfill:
        if _status(record) != CommandStatus.QUEUED.value:
            continue
        command_id = str(record.get("command_id") or "")
        if not command_id:
            continue
        gateway_state.ack_command(
            command_id,
            status=reason,
            result_payload={"purpose": THEME_BACKFILL_PURPOSE, "skipped_reason": reason},
            error=reason,
        )
        skipped[reason] = skipped.get(reason, 0) + 1
    return {"skipped": skipped}


def dispatch_pause_reason(gateway_state: GatewayStateStore, raw_theme_lab: dict[str, Any], active_records: list[dict[str, Any]] | None = None) -> str:
    if _raw_has_ready_like(raw_theme_lab):
        return CommandStatus.SKIPPED_READY.value
    active = active_records if active_records is not None else _active_records(gateway_state)
    if any(_command_type(record) in ORDER_COMMAND_TYPES for record in active):
        return CommandStatus.SKIPPED_ORDER_PENDING.value
    if any(not is_theme_backfill_record(record) for record in active):
        return CommandStatus.SKIPPED_NON_BACKFILL_PENDING.value
    snapshot = gateway_state.snapshot().to_dict()
    if not snapshot.get("connected") or not snapshot.get("heartbeat_ok") or not snapshot.get("kiwoom_logged_in"):
        return CommandStatus.SKIPPED_GATEWAY_UNHEALTHY.value
    return ""


def active_theme_backfill_records(gateway_state: GatewayStateStore) -> list[dict[str, Any]]:
    return [record for record in _active_records(gateway_state) if is_theme_backfill_record(record)]


def is_theme_backfill_record(record: dict[str, Any]) -> bool:
    payload = _payload(record)
    return str(payload.get("purpose") or "") == THEME_BACKFILL_PURPOSE


def is_theme_backfill_command(command: GatewayCommand) -> bool:
    return str((command.payload or {}).get("purpose") or "") == THEME_BACKFILL_PURPOSE


def parse_theme_backfill(tr_code: str, rows: list[dict[str, str]], *, code: str, trade_date: str = "") -> dict[str, Any]:
    if str(tr_code).lower() == TR_OPT10081:
        return parse_opt10081_backfill(rows, code=code, trade_date=trade_date)
    return parse_opt10001_backfill(rows, code=code)


def parse_opt10001_backfill(rows: list[dict[str, str]], *, code: str) -> dict[str, Any]:
    row = dict(rows[0] if rows else {})
    if not row:
        return {}
    base_price = _parse_price(row.get("기준가"))
    return {
        "code": _normalize_code(code),
        "stock_name": str(row.get("종목명") or "").strip(),
        "current_price": _parse_price(row.get("현재가")),
        "change_rate": _parse_float(row.get("등락율")),
        "volume": int(_parse_price(row.get("거래량")) or 0),
        "turnover": _parse_price(row.get("거래대금")),
        "open_price": _parse_price(row.get("시가")),
        "session_high": _parse_price(row.get("고가")),
        "session_low": _parse_price(row.get("저가")),
        "prev_close": base_price,
        "previous_close": base_price,
        "prev_close_source": TR_OPT10001 if base_price > 0 else "",
    }


def parse_opt10081_backfill(rows: list[dict[str, str]], *, code: str, trade_date: str) -> dict[str, Any]:
    clean_trade_date = str(trade_date or "").replace("-", "")
    prev_close = 0.0
    for row in rows:
        row_date = str(row.get("일자") or "").strip()
        if clean_trade_date and row_date >= clean_trade_date:
            continue
        prev_close = _parse_price(row.get("현재가"))
        if prev_close > 0:
            break
    return {
        "code": _normalize_code(code),
        "prev_close": prev_close,
        "previous_close": prev_close,
        "prev_close_source": TR_OPT10081 if prev_close > 0 else "",
    }


def bucket_for(tr_code: str, now: datetime, cfg: ThemeBackfillConfig) -> int:
    seconds = cfg.opt10081_bucket_sec if str(tr_code).lower() == TR_OPT10081 else cfg.opt10001_bucket_sec
    timestamp = int(_as_utc(now).timestamp())
    return timestamp // max(1, int(seconds or 1))


def _theme_backfill_priority(theme: Any) -> str:
    total = int(getattr(theme, "eligible_total_members", 0) or 0)
    hits = [hit for hit in getattr(theme, "member_hits", ()) or () if not bool(getattr(hit, "excluded", False))]
    if not total:
        total = len(hits)
    missing_current = sum(1 for hit in hits if "MISSING_CURRENT_PRICE" in set(getattr(hit, "data_quality_flags", ()) or ()))
    missing_prev = sum(1 for hit in hits if "MISSING_PREV_CLOSE" in set(getattr(hit, "data_quality_flags", ()) or ()))
    priced = max(0, total - missing_current)
    price_ratio = priced / total if total else 0.0
    flags = set(getattr(theme, "data_quality_flags", ()) or ())
    if total and (price_ratio < 0.3 or missing_current >= max(2, total * 0.7)):
        return "HIGH"
    if flags or missing_current or missing_prev or (total and price_ratio < 0.6):
        return "MEDIUM"
    return "LOW"


def _missing_fields(hit: Any) -> list[str]:
    flags = set(getattr(hit, "data_quality_flags", ()) or ())
    fields: list[str] = []
    if "MISSING_CURRENT_PRICE" in flags:
        fields.append("current_price")
    if "MISSING_PREV_CLOSE" in flags:
        fields.append("prev_close")
    if not str(getattr(hit, "name", "") or "").strip():
        fields.append("stock_name")
    if float(getattr(hit, "turnover_krw", 0.0) or 0.0) <= 0:
        fields.append("turnover")
    return list(dict.fromkeys(fields))


def _merge_candidate(target: dict[tuple[str, str], BackfillCandidate], candidate: BackfillCandidate) -> None:
    key = (candidate.code, candidate.tr_code)
    existing = target.get(key)
    if existing is None:
        target[key] = candidate
        return
    existing.related_theme_ids = list(dict.fromkeys(existing.related_theme_ids + candidate.related_theme_ids))
    existing.missing_fields = list(dict.fromkeys(existing.missing_fields + candidate.missing_fields))
    if existing.priority != "HIGH" and candidate.priority == "HIGH":
        existing.priority = "HIGH"
        existing.primary_theme_id = candidate.primary_theme_id
        existing.rank = min(existing.rank, candidate.rank)


def _active_records(gateway_state: GatewayStateStore) -> list[dict[str, Any]]:
    return [
        record
        for record in gateway_state.list_commands(limit=1000, include_finished=False)
        if _status(record) in ACTIVE_STATUSES
    ]


def _has_ready_like(result: Any) -> bool:
    for watch in getattr(result, "watchset", ()) or ():
        status = str(getattr(watch, "final_gate_status", "") or getattr(watch, "gate_status", "") or "")
        if status in {"READY", "READY_SMALL"}:
            return True
    return False


def _raw_has_ready_like(raw: dict[str, Any]) -> bool:
    for item in list(raw.get("watchset_snapshots") or []):
        status = str(item.get("final_gate_status") or item.get("gate_status") or "")
        if status in {"READY", "READY_SMALL"}:
            return True
    return False


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record.get("command", {}) or {}).get("payload")
    if isinstance(payload, dict):
        return payload
    payload = record.get("payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _command_type(record: dict[str, Any]) -> str:
    return str(record.get("command_type") or dict(record.get("command", {}) or {}).get("type") or "")


def _status(record: dict[str, Any]) -> str:
    return str(record.get("status") or "")


def _is_expired_record(record: dict[str, Any]) -> bool:
    text = str(record.get("expires_at") or "")
    if not text:
        return False
    try:
        expires_at = datetime.fromisoformat(text)
    except ValueError:
        return False
    return _as_utc(datetime.now(timezone.utc)) >= _as_utc(expires_at)


def _empty_summary(cfg: ThemeBackfillConfig) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "paused_reason": "",
        "candidate_count": 0,
        "queued_count": 0,
        "dispatched_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "skipped_count": 0,
        "expired_count": 0,
        "enqueued_count": 0,
        "duplicated_bucket_count": 0,
        "last_success_at": "",
        "last_failure_at": "",
        "last_failure_reason": "",
        "backfill_paused_by_ready_count": 0,
        "backfill_paused_by_order_count": 0,
        "backfill_paused_by_gateway_unhealthy_count": 0,
        "tr_backfill_caused_ready_count": 0,
    }


def _increment_pause(summary: dict[str, Any], reason: str) -> None:
    if reason == CommandStatus.SKIPPED_READY.value:
        summary["backfill_paused_by_ready_count"] += 1
    elif reason == CommandStatus.SKIPPED_ORDER_PENDING.value:
        summary["backfill_paused_by_order_count"] += 1
    elif reason == CommandStatus.SKIPPED_GATEWAY_UNHEALTHY.value:
        summary["backfill_paused_by_gateway_unhealthy_count"] += 1


def _trade_date(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat() if now.tzinfo else now.date().isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


def _parse_price(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("+", "")
    if not text:
        return 0.0
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return 0.0


def _parse_float(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
