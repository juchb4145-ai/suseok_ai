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
PAUSE_DISABLED = "DISABLED"
PAUSE_NOT_OBSERVE_MODE = "NOT_OBSERVE_MODE"
PAUSE_READY_EXISTS = "READY_EXISTS"
PAUSE_ORDER_PENDING = "ORDER_PENDING"
PAUSE_NON_BACKFILL_PENDING = "NON_BACKFILL_PENDING"
PAUSE_GATEWAY_UNHEALTHY = "GATEWAY_UNHEALTHY"
PAUSE_PENDING_LIMIT = "PENDING_LIMIT"
PAUSE_OPT10081_DISABLED = "OPT10081_DISABLED"
PAUSE_REGULAR_SESSION_DISABLED = "REGULAR_SESSION_DISABLED"


@dataclass(frozen=True)
class ThemeBackfillConfig:
    enabled: bool = False
    trading_mode: str = "OBSERVE"
    observe_only: bool = True
    max_per_cycle: int = 3
    max_pending: int = 5
    ttl_sec: int = 90
    opt10001_bucket_sec: int = 300
    opt10081_bucket_sec: int = 1800
    allow_opt10081: bool = False
    allow_regular_session: bool = True
    max_themes: int = 0
    max_hits_per_theme: int = 0
    cache_enabled: bool = True
    cache_ttl_sec: int = 21600
    cache_limit: int = 500

    @classmethod
    def from_env(cls, *, trading_mode: str | None = None) -> "ThemeBackfillConfig":
        return cls(
            enabled=_bool_env("TRADING_THEME_BACKFILL_ENABLED", False),
            trading_mode=str(trading_mode or os.environ.get("TRADING_MODE", "OBSERVE") or "OBSERVE").strip().upper() or "OBSERVE",
            observe_only=_bool_env("TRADING_THEME_BACKFILL_OBSERVE_ONLY", True),
            max_per_cycle=_int_env("TRADING_THEME_BACKFILL_MAX_PER_CYCLE", 3),
            max_pending=_int_env("TRADING_THEME_BACKFILL_MAX_PENDING", 5),
            ttl_sec=_int_env("TRADING_THEME_BACKFILL_TTL_SEC", 90),
            opt10001_bucket_sec=_int_env("TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC", 300),
            opt10081_bucket_sec=_int_env("TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC", 1800),
            allow_opt10081=_bool_env("TRADING_THEME_BACKFILL_ALLOW_OPT10081", False),
            allow_regular_session=_bool_env("TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION", True),
            max_themes=max(0, _int_env("TRADING_THEME_BACKFILL_MAX_THEMES", 0)),
            max_hits_per_theme=max(0, _int_env("TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME", 0)),
            cache_enabled=_bool_env("TRADING_THEME_BACKFILL_CACHE_ENABLED", True),
            cache_ttl_sec=max(0, _int_env("TRADING_THEME_BACKFILL_CACHE_TTL_SEC", 21600)),
            cache_limit=max(0, _int_env("TRADING_THEME_BACKFILL_CACHE_LIMIT", 500)),
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
    hit_rank: int = 999999


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
        self.last_cache_summary = _empty_cache_summary(self.config)

    def hydrate_cache(self, market_data: Any, now: datetime) -> dict[str, Any]:
        summary = _empty_cache_summary(self.config)
        if not self.config.cache_enabled or self.config.cache_limit <= 0:
            self.last_cache_summary = summary
            return summary
        seen_codes: set[str] = set()
        trade_date = _trade_date(now)
        for record in self.gateway_state.list_commands(
            status=CommandStatus.ACKED.value,
            limit=self.config.cache_limit,
            include_finished=True,
            command_type="tr_request",
        ):
            payload = _payload(record)
            if str(payload.get("purpose") or "") != THEME_BACKFILL_PURPOSE:
                continue
            summary["backfill_cache_record_count"] += 1
            record_trade_date = _record_trade_date(record)
            if record_trade_date and record_trade_date != trade_date:
                summary["backfill_cache_stale_count"] += 1
                continue
            if not record_trade_date and _record_age_sec(record, now) > self.config.cache_ttl_sec:
                summary["backfill_cache_stale_count"] += 1
                continue
            result_payload = _result_payload(record)
            parsed = _parsed_backfill_payload(result_payload)
            code = _normalize_code(result_payload.get("code") or payload.get("code") or parsed.get("code") or "")
            if not code or code in seen_codes or not parsed:
                summary["backfill_cache_skip_count"] += 1
                continue
            seen_codes.add(code)
            try:
                applied = bool(market_data.apply_theme_backfill(code, parsed, now=_as_utc(now).replace(tzinfo=None)))
            except Exception:
                applied = False
            if applied:
                summary["backfill_cache_applied_count"] += 1
            else:
                summary["backfill_cache_skip_count"] += 1
        self.last_cache_summary = summary
        return summary

    def plan_and_enqueue(self, result: Any, now: datetime) -> dict[str, Any]:
        cfg = self.config
        summary = _empty_summary(cfg)
        summary.update(self.last_cache_summary)
        summary.update(_result_quality_metrics(result, prefix="before"))
        candidates = build_backfill_candidates(result, cfg=cfg, now=now)
        summary["candidate_count"] = len(candidates)
        summary["theme_backfill_candidate_count"] = len(candidates)
        if not cfg.allow_opt10081:
            summary["opt10081_disabled_count"] = _missing_prev_close_hit_count(result)
        if not cfg.enabled:
            summary["paused_reason"] = PAUSE_DISABLED
            self.last_summary = summary
            return summary
        if cfg.observe_only and str(cfg.trading_mode or "").upper() != "OBSERVE":
            summary["paused_reason"] = PAUSE_NOT_OBSERVE_MODE
            self.last_summary = summary
            return summary
        if not cfg.allow_regular_session and _is_regular_session(now):
            summary["paused_reason"] = PAUSE_REGULAR_SESSION_DISABLED
            summary["backfill_paused_by_regular_session_count"] += 1
            self.last_summary = summary
            return summary
        pause_reason = enqueue_pause_reason(self.gateway_state, result)
        if pause_reason:
            summary["paused_reason"] = pause_reason
            _increment_pause(summary, pause_reason)
            self.last_summary = summary
            return summary
        pending = active_theme_backfill_records(self.gateway_state)
        summary["gateway_command_queue_depth"] = len(_active_records(self.gateway_state))
        summary["queued_count"] = len([item for item in pending if item.get("status") == CommandStatus.QUEUED.value])
        summary["dispatched_count"] = len([item for item in pending if item.get("status") == CommandStatus.DISPATCHED.value])
        if len(pending) >= cfg.max_pending:
            summary["paused_reason"] = PAUSE_PENDING_LIMIT
            self.last_summary = summary
            return summary
        enqueued = 0
        for candidate in candidates:
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
                summary["theme_backfill_enqueued_count"] += 1
            elif enqueue.reason == "DUPLICATE_COMMAND":
                summary["duplicated_bucket_count"] += 1
        summary["queued_count"] += enqueued
        self.last_summary = summary
        return summary


def build_backfill_candidates(result: Any, *, cfg: ThemeBackfillConfig, now: datetime) -> list[BackfillCandidate]:
    aggregate: dict[tuple[str, str], BackfillCandidate] = {}
    selected_theme_count = 0
    for rank, theme in enumerate(list(getattr(result, "themes", ()) or ()), start=1):
        priority = _theme_backfill_priority(theme)
        if priority not in {"HIGH", "MEDIUM"}:
            continue
        hits = _scoped_backfill_hits(theme, cfg=cfg)
        if not hits:
            continue
        if cfg.max_themes > 0 and selected_theme_count >= cfg.max_themes:
            continue
        selected_theme_count += 1
        theme_id = str(getattr(theme, "theme_id", "") or "")
        for hit_rank, hit in enumerate(hits, start=1):
            code = _normalize_code(getattr(hit, "symbol", ""))
            if not code:
                continue
            missing = _missing_fields(hit)
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
                    hit_rank=hit_rank,
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
                        hit_rank=hit_rank,
                    ),
                )
    return sorted(
        aggregate.values(),
        key=_candidate_sort_key,
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
            "backfill_theme_rank": candidate.rank,
            "backfill_hit_rank": candidate.hit_rank,
        },
    )


def enqueue_pause_reason(gateway_state: GatewayStateStore, result: Any) -> str:
    if _has_ready_like(result):
        return PAUSE_READY_EXISTS
    active = _active_records(gateway_state)
    if any(_command_type(record) in ORDER_COMMAND_TYPES for record in active):
        return PAUSE_ORDER_PENDING
    if any(not is_theme_backfill_record(record) for record in active):
        return PAUSE_NON_BACKFILL_PENDING
    snapshot = gateway_state.snapshot().to_dict()
    if not snapshot.get("connected") or not snapshot.get("heartbeat_ok") or not snapshot.get("kiwoom_logged_in"):
        return PAUSE_GATEWAY_UNHEALTHY
    return ""


def apply_dispatch_guard(
    gateway_state: GatewayStateStore,
    raw_theme_lab: dict[str, Any] | None = None,
    *,
    config: ThemeBackfillConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ThemeBackfillConfig.from_env()
    skipped: dict[str, int] = {}
    active = _active_records(gateway_state)
    backfill = [record for record in active if is_theme_backfill_record(record)]
    if not backfill:
        return {"skipped": skipped}
    for record in backfill:
        status = _status(record)
        command_id = str(record.get("command_id") or "")
        if not command_id:
            continue
        if status == CommandStatus.QUEUED.value and _is_expired_record(record):
            gateway_state.ack_command(
                command_id,
                status=CommandStatus.EXPIRED_BEFORE_DISPATCH.value,
                result_payload={"purpose": THEME_BACKFILL_PURPOSE, "skipped_reason": CommandStatus.EXPIRED_BEFORE_DISPATCH.value},
                error=CommandStatus.EXPIRED_BEFORE_DISPATCH.value,
            )
            skipped[CommandStatus.EXPIRED_BEFORE_DISPATCH.value] = skipped.get(CommandStatus.EXPIRED_BEFORE_DISPATCH.value, 0) + 1
        elif status == CommandStatus.DISPATCHED.value and _is_stale_dispatched_backfill(record, cfg):
            gateway_state.ack_command(
                command_id,
                status=CommandStatus.EXPIRED.value,
                result_payload={
                    "purpose": THEME_BACKFILL_PURPOSE,
                    "skipped_reason": "STALE_DISPATCHED_BACKFILL_CLEANUP",
                    "cleanup_reason": "DISPATCHED_WITHOUT_ACK_AFTER_TTL",
                },
                error="STALE_DISPATCHED_BACKFILL_CLEANUP",
            )
            skipped[CommandStatus.EXPIRED.value] = skipped.get(CommandStatus.EXPIRED.value, 0) + 1
    active = _active_records(gateway_state)
    backfill = [record for record in active if is_theme_backfill_record(record)]
    if not backfill:
        return {"skipped": skipped}
    reason = dispatch_pause_reason(gateway_state, raw_theme_lab or {}, active, config=cfg)
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


def dispatch_pause_reason(
    gateway_state: GatewayStateStore,
    raw_theme_lab: dict[str, Any],
    active_records: list[dict[str, Any]] | None = None,
    *,
    config: ThemeBackfillConfig | None = None,
) -> str:
    cfg = config or ThemeBackfillConfig.from_env()
    if cfg.observe_only and str(cfg.trading_mode or "").upper() != "OBSERVE":
        return CommandStatus.SKIPPED_NOT_OBSERVE_MODE.value
    if not cfg.allow_regular_session and _is_regular_session(datetime.now(timezone.utc)):
        return CommandStatus.SKIPPED_REGULAR_SESSION.value
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


def parse_theme_backfill(
    tr_code: str,
    rows: list[dict[str, str]],
    *,
    code: str,
    trade_date: str = "",
    master_prev_close: Any = None,
) -> dict[str, Any]:
    if str(tr_code).lower() == TR_OPT10081:
        return parse_opt10081_backfill(rows, code=code, trade_date=trade_date)
    return parse_opt10001_backfill(rows, code=code, master_prev_close=master_prev_close)



def parse_opt10001_backfill(rows: list[dict[str, str]], *, code: str, master_prev_close: Any = None) -> dict[str, Any]:
    row = dict(rows[0] if rows else {})
    if not row:
        return {
            "code": _normalize_code(code),
            "parser_status": "EMPTY",
            "parser_missing_fields": ["row"],
            "parsed_fields_count": 0,
        }
    base_price = _parse_price(_field_value(row, ("\uae30\uc900\uac00", "湲곗?媛")))
    master_price = _parse_price(master_prev_close)
    prev_close = base_price or master_price
    prev_source = TR_OPT10001 if base_price > 0 else ("GetMasterLastPrice" if master_price > 0 else "")
    parsed = {
        "code": _normalize_code(code),
        "stock_name": str(_field_value(row, ("\uc885\ubaa9\uba85", "醫낅ぉ紐?")) or "").strip(),
        "current_price": _parse_price(_field_value(row, ("\ud604\uc7ac\uac00", "?꾩옱媛"))),
        "change_rate": _parse_float(_field_value(row, ("\ub4f1\ub77d\uc728", "\ub4f1\ub77d\ub960", "?깅씫??", "?깅씫瑜?"))),
        "volume": int(_parse_price(_field_value(row, ("\uac70\ub798\ub7c9", "嫄곕옒??"))) or 0),
        "turnover": _parse_price(_field_value(row, ("\uac70\ub798\ub300\uae08", "嫄곕옒?湲?"))),
        "open_price": _parse_price(_field_value(row, ("\uc2dc\uac00", "?쒓?"))),
        "session_high": _parse_price(_field_value(row, ("\uace0\uac00", "怨좉?"))),
        "session_low": _parse_price(_field_value(row, ("\uc800\uac00", "?媛"))),
        "prev_close": prev_close,
        "previous_close": prev_close,
        "prev_close_source": prev_source,
        "master_prev_close_used": bool(master_price > 0 and base_price <= 0),
    }
    return _with_parser_status(parsed, ("current_price", "prev_close", "stock_name"))


def parse_opt10081_backfill(rows: list[dict[str, str]], *, code: str, trade_date: str) -> dict[str, Any]:
    clean_trade_date = str(trade_date or "").replace("-", "")
    prev_close = 0.0
    for row in rows:
        row_date = str(_field_value(row, ("\uc77c\uc790", "?쇱옄")) or "").strip()
        if clean_trade_date and row_date >= clean_trade_date:
            continue
        prev_close = _parse_price(_field_value(row, ("\ud604\uc7ac\uac00", "?꾩옱媛")))
        if prev_close > 0:
            break
    parsed = {
        "code": _normalize_code(code),
        "prev_close": prev_close,
        "previous_close": prev_close,
        "prev_close_source": TR_OPT10081 if prev_close > 0 else "",
    }
    return _with_parser_status(parsed, ("prev_close",))


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


def _scoped_backfill_hits(theme: Any, *, cfg: ThemeBackfillConfig) -> list[Any]:
    hits = []
    for hit in getattr(theme, "member_hits", ()) or ():
        if bool(getattr(hit, "excluded", False)):
            continue
        if not _normalize_code(getattr(hit, "symbol", "")):
            continue
        if not _missing_fields(hit):
            continue
        hits.append(hit)
    hits = sorted(hits, key=_backfill_hit_sort_key)
    if cfg.max_hits_per_theme > 0:
        return hits[: cfg.max_hits_per_theme]
    return hits


def _backfill_hit_sort_key(hit: Any) -> tuple[Any, ...]:
    if bool(getattr(hit, "leader_hit", False)):
        condition_level = 3
    elif bool(getattr(hit, "strong_hit", False)):
        condition_level = 2
    elif bool(getattr(hit, "alive_hit", False)):
        condition_level = 1
    else:
        condition_level = 0
    flags = set(getattr(hit, "data_quality_flags", ()) or ())
    missing_current = "MISSING_CURRENT_PRICE" in flags
    missing_prev = "MISSING_PREV_CLOSE" in flags
    return (
        -condition_level,
        0 if missing_current else 1,
        0 if missing_prev else 1,
        -abs(float(getattr(hit, "return_pct", 0.0) or 0.0)),
        -float(getattr(hit, "turnover_krw", 0.0) or 0.0),
        _normalize_code(getattr(hit, "symbol", "")),
    )


def _candidate_sort_key(item: BackfillCandidate) -> tuple[Any, ...]:
    return (
        0 if item.priority == "HIGH" else 1,
        item.rank,
        item.hit_rank,
        item.code,
        0 if item.tr_code == TR_OPT10001 else 1,
    )


def _merge_candidate(target: dict[tuple[str, str], BackfillCandidate], candidate: BackfillCandidate) -> None:
    key = (candidate.code, candidate.tr_code)
    existing = target.get(key)
    if existing is None:
        target[key] = candidate
        return
    existing.related_theme_ids = list(dict.fromkeys(existing.related_theme_ids + candidate.related_theme_ids))
    existing.missing_fields = list(dict.fromkeys(existing.missing_fields + candidate.missing_fields))
    if _candidate_sort_key(candidate) < _candidate_sort_key(existing):
        existing.priority = candidate.priority
        existing.primary_theme_id = candidate.primary_theme_id
        existing.rank = candidate.rank
        existing.hit_rank = candidate.hit_rank


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


def _result_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("result_payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _parsed_backfill_payload(result_payload: dict[str, Any]) -> dict[str, Any]:
    parsed = result_payload.get("parsed_backfill")
    if isinstance(parsed, dict):
        return dict(parsed)
    raw = result_payload.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("parsed_backfill"), dict):
        return dict(raw.get("parsed_backfill") or {})
    return {}


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


def _is_stale_dispatched_backfill(record: dict[str, Any], cfg: ThemeBackfillConfig) -> bool:
    if _status(record) != CommandStatus.DISPATCHED.value:
        return False
    base = _record_time(record, "dispatched_at") or _record_time(record, "created_at")
    if base is None:
        return _is_expired_record(record)
    grace_sec = max(1, int(cfg.ttl_sec or 90))
    return _as_utc(datetime.now(timezone.utc)) >= _as_utc(base) + timedelta(seconds=grace_sec)


def _record_time(record: dict[str, Any], key: str) -> datetime | None:
    text = str(record.get(key) or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _record_trade_date(record: dict[str, Any]) -> str:
    payload = _payload(record)
    result_payload = _result_payload(record)
    for value in (
        payload.get("trade_date"),
        result_payload.get("trade_date"),
        record.get("trade_date"),
    ):
        text = str(value or "").strip()
        if text:
            return text[:10]
    created = _record_time(record, "created_at")
    return created.date().isoformat() if created else ""


def _record_age_sec(record: dict[str, Any], now: datetime) -> int:
    base = _record_time(record, "acked_at") or _record_time(record, "finished_at") or _record_time(record, "updated_at") or _record_time(record, "created_at")
    if base is None:
        return 0
    return max(0, int((_as_utc(now) - _as_utc(base)).total_seconds()))


def _result_quality_metrics(result: Any, *, prefix: str) -> dict[str, Any]:
    total = 0
    missing_price = 0
    missing_prev = 0
    for theme in getattr(result, "themes", ()) or ():
        for hit in getattr(theme, "member_hits", ()) or ():
            if bool(getattr(hit, "excluded", False)):
                continue
            total += 1
            flags = set(getattr(hit, "data_quality_flags", ()) or ())
            if "MISSING_CURRENT_PRICE" in flags:
                missing_price += 1
            if "MISSING_PREV_CLOSE" in flags:
                missing_prev += 1
    coverage = None if total <= 0 else round((total - missing_price) / total, 4)
    return {
        f"missing_price_count_{prefix}": missing_price,
        f"missing_prev_close_count_{prefix}": missing_prev,
        f"theme_coverage_{prefix}": coverage,
    }


def _missing_prev_close_hit_count(result: Any) -> int:
    count = 0
    for theme in getattr(result, "themes", ()) or ():
        for hit in getattr(theme, "member_hits", ()) or ():
            if bool(getattr(hit, "excluded", False)):
                continue
            if "MISSING_PREV_CLOSE" in set(getattr(hit, "data_quality_flags", ()) or ()):
                count += 1
    return count


def _empty_summary(cfg: ThemeBackfillConfig) -> dict[str, Any]:
    return {
        "enabled": cfg.enabled,
        "observe_only": cfg.observe_only,
        "trading_mode": cfg.trading_mode,
        "max_per_cycle": cfg.max_per_cycle,
        "max_pending": cfg.max_pending,
        "ttl_sec": cfg.ttl_sec,
        "opt10001_bucket_sec": cfg.opt10001_bucket_sec,
        "opt10081_bucket_sec": cfg.opt10081_bucket_sec,
        "allow_opt10081": cfg.allow_opt10081,
        "allow_regular_session": cfg.allow_regular_session,
        "max_themes": cfg.max_themes,
        "max_hits_per_theme": cfg.max_hits_per_theme,
        "cache_enabled": cfg.cache_enabled,
        "cache_ttl_sec": cfg.cache_ttl_sec,
        "cache_limit": cfg.cache_limit,
        **_empty_cache_summary(cfg),
        "observe_pilot_active": bool(cfg.enabled and (not cfg.observe_only or str(cfg.trading_mode or "").upper() == "OBSERVE")),
        "history_window": "recent_500_commands",
        "paused_reason": "",
        "candidate_count": 0,
        "theme_backfill_candidate_count": 0,
        "queued_count": 0,
        "dispatched_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "skipped_count": 0,
        "expired_count": 0,
        "enqueued_count": 0,
        "theme_backfill_enqueued_count": 0,
        "theme_backfill_dispatched_count": 0,
        "theme_backfill_success_count": 0,
        "theme_backfill_failure_count": 0,
        "theme_backfill_skip_count": 0,
        "duplicated_bucket_count": 0,
        "opt10081_disabled_count": 0,
        "parser_miss_count": 0,
        "parser_miss_ratio": None,
        "missing_price_count_before": 0,
        "missing_price_count_after": 0,
        "missing_prev_close_count_before": 0,
        "missing_prev_close_count_after": 0,
        "theme_coverage_before": None,
        "theme_coverage_after": None,
        "tr_backfill_snapshot_count": 0,
        "rt_tick_snapshot_count": 0,
        "backfill_expired_before_dispatch_count": 0,
        "gateway_command_queue_depth": 0,
        "last_success_at": "",
        "last_failure_at": "",
        "last_failure_reason": "",
        "backfill_paused_by_ready_count": 0,
        "backfill_paused_by_order_count": 0,
        "backfill_paused_by_gateway_unhealthy_count": 0,
        "backfill_paused_by_regular_session_count": 0,
        "tr_backfill_caused_ready_count": 0,
    }


def _empty_cache_summary(cfg: ThemeBackfillConfig) -> dict[str, Any]:
    return {
        "backfill_cache_enabled": cfg.cache_enabled,
        "backfill_cache_ttl_sec": cfg.cache_ttl_sec,
        "backfill_cache_limit": cfg.cache_limit,
        "backfill_cache_record_count": 0,
        "backfill_cache_applied_count": 0,
        "backfill_cache_skip_count": 0,
        "backfill_cache_stale_count": 0,
    }


def _increment_pause(summary: dict[str, Any], reason: str) -> None:
    if reason in {PAUSE_READY_EXISTS, CommandStatus.SKIPPED_READY.value}:
        summary["backfill_paused_by_ready_count"] += 1
    elif reason in {PAUSE_ORDER_PENDING, CommandStatus.SKIPPED_ORDER_PENDING.value}:
        summary["backfill_paused_by_order_count"] += 1
    elif reason in {PAUSE_GATEWAY_UNHEALTHY, CommandStatus.SKIPPED_GATEWAY_UNHEALTHY.value}:
        summary["backfill_paused_by_gateway_unhealthy_count"] += 1
    elif reason in {PAUSE_REGULAR_SESSION_DISABLED, CommandStatus.SKIPPED_REGULAR_SESSION.value}:
        summary["backfill_paused_by_regular_session_count"] += 1


def _trade_date(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat() if now.tzinfo else now.date().isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _is_regular_session(value: datetime) -> bool:
    if value.tzinfo is not None:
        value = value.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
    minutes = value.hour * 60 + value.minute
    return (9 * 60) <= minutes <= (15 * 60 + 30)


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.zfill(6) if text.isdigit() and len(text) < 6 else text


def _field_value(row: dict[str, Any], aliases: Iterable[str]) -> Any:
    normalized = {_normalize_field_name(key): value for key, value in row.items()}
    for alias in aliases:
        key = _normalize_field_name(alias)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_field_name(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _with_parser_status(parsed: dict[str, Any], required_fields: Iterable[str]) -> dict[str, Any]:
    missing = [
        field
        for field in required_fields
        if parsed.get(field) in {None, "", 0, 0.0}
    ]
    parsed["parser_missing_fields"] = missing
    parsed["parsed_fields_count"] = sum(
        1
        for key, value in parsed.items()
        if key not in {"parser_missing_fields", "parsed_fields_count", "parser_status"} and value not in {None, "", 0, 0.0, False}
    )
    parsed["parser_status"] = "PARTIAL" if missing else "OK"
    return parsed


def _parse_price(value: Any) -> float:
    text = "".join(str(value or "").split()).replace(",", "").replace("+", "")
    if not text:
        return 0.0
    try:
        number = abs(float(text))
        return number if number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_float(value: Any) -> float:
    text = "".join(str(value or "").split()).replace(",", "").replace("+", "").replace("%", "")
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
