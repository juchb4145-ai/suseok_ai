from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, new_message_id
from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import Candidate, CandidateEvent, CandidateState


CANDIDATE_HYDRATION_PURPOSE = "candidate_hydration"
CANDIDATE_HYDRATION_TR_CODE = "opt10001"
CANDIDATE_HYDRATION_RQ_NAME = "CandidateHydration_opt10001"
CANDIDATE_HYDRATION_SCREEN_NO = "8730"
CANDIDATE_HYDRATION_BUCKET = "basic"

CANDIDATE_HYDRATION_FIELDS = [
    "stock_name",
    "current_price",
    "change_rate",
    "volume",
    "trade_value",
    "open_price",
    "day_high",
    "day_low",
    "prev_close",
]


@dataclass(frozen=True)
class CandidateHydrationConfig:
    enabled: bool = True
    max_per_cycle: int = 5
    max_pending: int = 10
    ttl_sec: int = 90
    tr_code: str = CANDIDATE_HYDRATION_TR_CODE
    rq_name: str = CANDIDATE_HYDRATION_RQ_NAME
    screen_no: str = CANDIDATE_HYDRATION_SCREEN_NO
    bucket: str = CANDIDATE_HYDRATION_BUCKET

    @classmethod
    def from_env(cls) -> "CandidateHydrationConfig":
        return cls(
            enabled=_bool_env("TRADING_CANDIDATE_HYDRATION_ENABLED", True),
            max_per_cycle=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_MAX_PER_CYCLE", 5)),
            max_pending=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_MAX_PENDING", 10)),
            ttl_sec=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_TTL_SEC", 90)),
        )


@dataclass(frozen=True)
class CandidateHydrationEnqueueResult:
    candidate_id: int | None
    code: str
    enqueued: bool
    duplicate: bool = False
    reason: str = ""
    command_id: str = ""
    idempotency_key: str = ""


class CandidateHydrator:
    def __init__(
        self,
        db: Any,
        gateway_state: GatewayStateStore,
        *,
        market_data: MarketDataStore | None = None,
        config: CandidateHydrationConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.market_data = market_data
        self.config = config or CandidateHydrationConfig.from_env()
        self.clock = clock or datetime.now

    def enqueue_due_candidates(self, *, trade_date: str | None = None) -> list[CandidateHydrationEnqueueResult]:
        if not self.config.enabled:
            return []
        trade_date = trade_date or self.clock().date().isoformat()
        pending = self._pending_count(trade_date)
        remaining_pending = max(0, self.config.max_pending - pending)
        limit = min(self.config.max_per_cycle, remaining_pending)
        if limit <= 0:
            return []
        candidates = [
            candidate
            for candidate in self.db.list_candidates(trade_date=trade_date)
            if candidate.state in {CandidateState.DETECTED, CandidateState.WAIT_DATA}
            and self._needs_hydration(candidate)
        ]
        candidates = sorted(candidates, key=self._hydration_sort_key, reverse=True)
        results: list[CandidateHydrationEnqueueResult] = []
        for candidate in candidates[:limit]:
            results.append(self.enqueue_candidate(candidate))
        return results

    def enqueue_candidate(self, candidate: Candidate) -> CandidateHydrationEnqueueResult:
        key = hydration_idempotency_key(
            trade_date=candidate.trade_date,
            code=candidate.code,
            tr_code=self.config.tr_code,
            bucket=self.config.bucket,
        )
        if not self.config.enabled:
            return CandidateHydrationEnqueueResult(
                candidate_id=candidate.id,
                code=candidate.code,
                enqueued=False,
                reason="HYDRATION_DISABLED",
                idempotency_key=key,
            )
        priority_name, command_priority = self._hydration_priority(candidate)
        command = GatewayCommand(
            type="tr_request",
            command_id=new_message_id("cmd_cand_hyd"),
            idempotency_key=key,
            source="strategy_runtime",
            payload={
                "purpose": CANDIDATE_HYDRATION_PURPOSE,
                "response_mode": "capture",
                "trade_date": candidate.trade_date,
                "candidate_id": candidate.id,
                "code": candidate.code,
                "tr_code": self.config.tr_code,
                "rq_name": self.config.rq_name,
                "screen_no": self.config.screen_no,
                "inputs": {"종목코드": candidate.code, "code": candidate.code},
                "fields": list(CANDIDATE_HYDRATION_FIELDS),
                "bucket": self.config.bucket,
            },
        )
        duplicate_of = self.gateway_state.duplicate_of(key)
        if duplicate_of:
            self._save_request(candidate, command, status="DUPLICATE", priority=priority_name, duplicate_of=duplicate_of)
            self._mark_hydrating(candidate, command, priority=priority_name, duplicate_of=duplicate_of)
            return CandidateHydrationEnqueueResult(
                candidate_id=candidate.id,
                code=candidate.code,
                enqueued=False,
                duplicate=True,
                reason="DUPLICATE_COMMAND",
                command_id=duplicate_of,
                idempotency_key=key,
            )
        result = self.gateway_state.enqueue_command(
            command,
            priority=command_priority,
            ttl_sec=self.config.ttl_sec,
            max_attempts=1,
            metadata={"runtime": "strategy_reboot_v2", "purpose": CANDIDATE_HYDRATION_PURPOSE, "priority": priority_name},
        )
        status = "QUEUED" if result.accepted else "REJECTED"
        duplicate = result.reason == "DUPLICATE_COMMAND"
        self._save_request(candidate, command, status=status, priority=priority_name, duplicate_of=result.duplicate_of)
        self._mark_hydrating(candidate, command, priority=priority_name, duplicate_of=result.duplicate_of)
        return CandidateHydrationEnqueueResult(
            candidate_id=candidate.id,
            code=candidate.code,
            enqueued=bool(result.accepted),
            duplicate=duplicate,
            reason=result.reason,
            command_id=result.duplicate_of or command.command_id,
            idempotency_key=key,
        )

    def handle_event(self, event: GatewayEvent) -> bool:
        if event.type != "command_ack":
            return False
        payload = dict(event.payload or {})
        if str(payload.get("purpose") or "") != CANDIDATE_HYDRATION_PURPOSE:
            return False
        self.merge_ack(payload, event=event)
        return True

    def merge_ack(self, payload: Mapping[str, Any], *, event: GatewayEvent | None = None) -> Candidate | None:
        raw_payload = dict(payload or {})
        command_id = str(raw_payload.get("command_id") or (event.command_id if event else "") or "")
        command_payload = self._command_payload(command_id)
        trade_date = str(raw_payload.get("trade_date") or command_payload.get("trade_date") or self.clock().date().isoformat())
        code = normalize_code(str(raw_payload.get("code") or command_payload.get("code") or ""))
        candidate_id = _int(raw_payload.get("candidate_id") or command_payload.get("candidate_id"))
        parsed = _parsed_hydration_payload(raw_payload)
        if not parsed:
            rows = _ack_rows(raw_payload)
            parsed = parse_candidate_hydration_rows(rows)
        if not code:
            code = normalize_code(str(parsed.get("code") or ""))
        candidate = self.db.load_candidate_by_id(candidate_id) if candidate_id else None
        if candidate is None and trade_date and code:
            candidate = self.db.load_candidate(trade_date, code)
        if candidate is None:
            self._save_result(None, raw_payload, parsed, status="ORPHAN_ACK", reason="CANDIDATE_NOT_FOUND")
            return None
        previous_state = candidate.state
        metadata = dict(candidate.metadata or {})
        current_metadata = dict(metadata.get("candidate_hydration") or {})
        current_metadata.update(
            {
                "status": "ACKED",
                "command_id": command_id,
                "idempotency_key": str(raw_payload.get("idempotency_key") or command_payload.get("idempotency_key") or ""),
                "merged_at": _format_time(self.clock()),
                "parsed": dict(parsed),
                "raw_rows": _ack_rows(raw_payload)[:5],
            }
        )
        metadata["candidate_hydration"] = current_metadata
        if parsed.get("stock_name") and not candidate.name:
            candidate.name = str(parsed.get("stock_name") or "")
        reason_codes = list(metadata.get("reason_codes") or [])
        market_merged = self._merge_market_data(candidate.code, parsed)
        metadata["candidate_hydration"]["market_data_merged"] = market_merged
        realtime_tick = self.market_data.latest_tick(candidate.code) if self.market_data is not None else None
        if realtime_tick is not None and str((realtime_tick.metadata or {}).get("price_source") or "") == "TR_BACKFILL":
            metadata["gate_usable_for_entry"] = False
            metadata["entry_timing_source"] = "WAIT_REALTIME_TICK"
        elif realtime_tick is None:
            metadata["gate_usable_for_entry"] = False
            metadata["entry_timing_source"] = "WAIT_REALTIME_TICK"
        else:
            metadata["gate_usable_for_entry"] = True
            metadata["entry_timing_source"] = "REALTIME_TICK"
        enough, wait_reasons = _minimum_watching_data(candidate, parsed, metadata)
        if enough:
            candidate.state = CandidateState.WATCHING
            reason_codes = _dedupe([*reason_codes, *wait_reasons])
        else:
            candidate.state = CandidateState.WAIT_DATA
            reason_codes = _dedupe([*reason_codes, "WAIT_DATA", *wait_reasons])
        metadata["reason_codes"] = reason_codes
        candidate.metadata = metadata
        candidate.last_seen_at = _format_time(self.clock())
        saved = self.db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_hydration_merged",
                    from_state=previous_state,
                    to_state=candidate.state,
                    source=None,
                    reason="candidate hydration ack",
                    created_at=candidate.last_seen_at,
                    payload={
                        "command_id": command_id,
                        "code": candidate.code,
                        "parsed": parsed,
                        "wait_reasons": wait_reasons,
                    },
                )
            ],
        )
        self._save_result(saved, raw_payload, parsed, status="MERGED", reason="")
        updater = getattr(self.db, "update_candidate_hydration_request_status", None)
        if callable(updater) and command_id:
            updater(command_id=command_id, status="ACKED", result_status="MERGED")
        return saved

    def _mark_hydrating(
        self,
        candidate: Candidate,
        command: GatewayCommand,
        *,
        priority: str,
        duplicate_of: str = "",
    ) -> Candidate:
        previous_state = candidate.state
        if candidate.state in {CandidateState.DETECTED, CandidateState.WAIT_DATA}:
            candidate.state = CandidateState.HYDRATING
        metadata = dict(candidate.metadata or {})
        metadata["candidate_hydration"] = {
            **dict(metadata.get("candidate_hydration") or {}),
            "status": "PENDING",
            "command_id": duplicate_of or command.command_id,
            "idempotency_key": command.idempotency_key,
            "priority": priority,
            "requested_at": _format_time(self.clock()),
            "purpose": CANDIDATE_HYDRATION_PURPOSE,
        }
        candidate.metadata = metadata
        candidate.last_seen_at = _format_time(self.clock())
        return self.db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_hydration_requested",
                    from_state=previous_state,
                    to_state=candidate.state,
                    source=None,
                    reason="candidate hydration requested",
                    created_at=candidate.last_seen_at,
                    payload={
                        "command_id": duplicate_of or command.command_id,
                        "idempotency_key": command.idempotency_key,
                        "priority": priority,
                    },
                )
            ],
        )

    def _save_request(
        self,
        candidate: Candidate,
        command: GatewayCommand,
        *,
        status: str,
        priority: str,
        duplicate_of: str = "",
    ) -> None:
        saver = getattr(self.db, "save_candidate_hydration_request", None)
        if not callable(saver):
            return
        saver(
            {
                "trade_date": candidate.trade_date,
                "candidate_id": candidate.id,
                "code": candidate.code,
                "command_id": command.command_id,
                "idempotency_key": command.idempotency_key,
                "tr_code": self.config.tr_code,
                "rq_name": self.config.rq_name,
                "bucket": self.config.bucket,
                "priority": priority,
                "status": status,
                "duplicate_of": duplicate_of,
                "request_payload": dict(command.payload or {}),
            }
        )

    def _save_result(
        self,
        candidate: Candidate | None,
        ack_payload: Mapping[str, Any],
        parsed: Mapping[str, Any],
        *,
        status: str,
        reason: str,
    ) -> None:
        saver = getattr(self.db, "save_candidate_hydration_result", None)
        if not callable(saver):
            return
        saver(
            {
                "trade_date": str((candidate.trade_date if candidate else "") or ack_payload.get("trade_date") or ""),
                "candidate_id": candidate.id if candidate else None,
                "code": str((candidate.code if candidate else "") or ack_payload.get("code") or parsed.get("code") or ""),
                "command_id": str(ack_payload.get("command_id") or ""),
                "idempotency_key": str(ack_payload.get("idempotency_key") or ""),
                "status": status,
                "reason": reason,
                "parsed_payload": dict(parsed or {}),
                "raw_payload": dict(ack_payload or {}),
            }
        )

    def _merge_market_data(self, code: str, parsed: Mapping[str, Any]) -> bool:
        if self.market_data is None:
            return False
        payload = {
            "stock_name": parsed.get("stock_name") or parsed.get("name") or "",
            "current_price": parsed.get("current_price") or parsed.get("price") or 0,
            "change_rate": parsed.get("change_rate") or 0.0,
            "volume": parsed.get("volume") or 0,
            "turnover": parsed.get("trade_value") or parsed.get("turnover") or 0.0,
            "open_price": parsed.get("open_price") or 0,
            "session_high": parsed.get("day_high") or 0,
            "session_low": parsed.get("day_low") or 0,
            "prev_close": parsed.get("prev_close") or 0,
            "backfill_source": CANDIDATE_HYDRATION_PURPOSE,
        }
        try:
            return bool(self.market_data.apply_theme_backfill(code, payload))
        except Exception:
            return False

    def _needs_hydration(self, candidate: Candidate) -> bool:
        metadata = dict(candidate.metadata or {})
        hydration = dict(metadata.get("candidate_hydration") or {})
        if str(hydration.get("status") or "") in {"ACKED", "MERGED", "OK"}:
            return False
        return True

    def _pending_count(self, trade_date: str) -> int:
        summary = {}
        loader = getattr(self.db, "candidate_hydration_summary", None)
        if callable(loader):
            summary = dict(loader(trade_date=trade_date) or {})
        return int(summary.get("pending_count") or 0)

    def _hydration_priority(self, candidate: Candidate) -> tuple[str, CommandPriority]:
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        primary_source = str(ingestion.get("primary_source") or metadata.get("primary_source") or "")
        role = str(ingestion.get("stock_role") or metadata.get("stock_role") or "").upper()
        theme_id = str(ingestion.get("primary_theme_id") or metadata.get("primary_theme_id") or metadata.get("best_theme_id") or "")
        if primary_source == "opening_burst" and role in {"LEADER", "CO_LEADER"}:
            return "HIGH", CommandPriority.HIGH
        if primary_source == "condition_search" and theme_id:
            return "MEDIUM", CommandPriority.NORMAL
        return "LOW", CommandPriority.LOW

    def _hydration_sort_key(self, candidate: Candidate) -> tuple[int, float, str]:
        priority_name, _ = self._hydration_priority(candidate)
        rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(priority_name, 0)
        score = _float(dict(candidate.metadata or {}).get("source_score"))
        return rank, score, str(candidate.last_seen_at or candidate.detected_at or "")

    def _command_payload(self, command_id: str) -> dict[str, Any]:
        if not command_id:
            return {}
        record = self.gateway_state.get_command(command_id)
        if record is None:
            return {}
        command = getattr(record, "command", None)
        if command is not None:
            return dict(command.payload or {})
        if isinstance(record, Mapping):
            command_data = dict(record.get("command") or {})
            return dict(command_data.get("payload") or record.get("payload") or {})
        return {}


def hydration_idempotency_key(*, trade_date: str, code: str, tr_code: str = CANDIDATE_HYDRATION_TR_CODE, bucket: str = CANDIDATE_HYDRATION_BUCKET) -> str:
    return f"candidate_hydration:{trade_date}:{normalize_code(code)}:{tr_code}:{bucket}"


def parse_candidate_hydration_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    for raw in rows:
        normalized = {_normalize_field_name(key): value for key, value in dict(raw).items()}
        parsed = {
            "code": normalize_code(str(_field_value(normalized, _CODE_FIELDS) or "")),
            "stock_name": str(_field_value(normalized, _NAME_FIELDS) or "").strip(),
            "current_price": abs(_float(_field_value(normalized, _PRICE_FIELDS))),
            "change_rate": _float(_field_value(normalized, _CHANGE_RATE_FIELDS)),
            "volume": int(abs(_float(_field_value(normalized, _VOLUME_FIELDS)))),
            "trade_value": abs(_float(_field_value(normalized, _TRADE_VALUE_FIELDS))),
            "open_price": abs(_float(_field_value(normalized, _OPEN_FIELDS))),
            "day_high": abs(_float(_field_value(normalized, _HIGH_FIELDS))),
            "day_low": abs(_float(_field_value(normalized, _LOW_FIELDS))),
            "prev_close": abs(_float(_field_value(normalized, _PREV_CLOSE_FIELDS))),
            "raw": dict(raw),
        }
        if parsed["code"] or parsed["current_price"] > 0 or parsed["stock_name"]:
            return parsed
    return {}


def _parsed_hydration_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("parsed_hydration", "parsed_backfill", "parsed"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            parsed = dict(value)
            if parsed:
                return parsed
    raw = payload.get("raw")
    if isinstance(raw, Mapping):
        for key in ("parsed_hydration", "parsed_backfill", "parsed"):
            value = raw.get(key)
            if isinstance(value, Mapping) and value:
                return dict(value)
    return {}


def _minimum_watching_data(candidate: Candidate, parsed: Mapping[str, Any], metadata: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    code = normalize_code(candidate.code)
    if not code:
        reasons.append("WAIT_DATA_CODE_MISSING")
    current_price = _float(parsed.get("current_price") or parsed.get("price"))
    prev_close = _float(parsed.get("prev_close"))
    change_rate = _float(parsed.get("change_rate"))
    if current_price <= 0:
        reasons.append("WAIT_DATA_PRICE_MISSING")
    if not change_rate and not (prev_close > 0 and current_price > 0):
        reasons.append("WAIT_DATA_CHANGE_RATE_MISSING")
    ingestion = dict(metadata.get("candidate_ingestion") or {})
    if not ingestion.get("active_source_types"):
        reasons.append("WAIT_DATA_SOURCE_MISSING")
    if not (ingestion.get("primary_theme_id") or metadata.get("primary_theme_id") or metadata.get("best_theme_id")):
        reasons.append("theme_unmapped")
    blocking = [reason for reason in reasons if reason != "theme_unmapped"]
    return not blocking, _dedupe(reasons)


def _ack_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("tr_rows") or payload.get("rows")
    raw = payload.get("raw")
    if not rows and isinstance(raw, Mapping):
        rows = raw.get("tr_rows") or raw.get("rows")
    return [dict(row) for row in list(rows or []) if isinstance(row, Mapping)]


def _field_value(normalized: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    for alias in aliases:
        key = _normalize_field_name(alias)
        if key in normalized:
            return normalized[key]
    return None


def _normalize_field_name(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


_CODE_FIELDS = ("code", "stock_code", "종목코드", "단축코드")
_NAME_FIELDS = ("stock_name", "name", "종목명")
_PRICE_FIELDS = ("current_price", "price", "현재가")
_CHANGE_RATE_FIELDS = ("change_rate", "change_rate_pct", "등락율", "등락률")
_VOLUME_FIELDS = ("volume", "cum_volume", "거래량")
_TRADE_VALUE_FIELDS = ("trade_value", "turnover", "turnover_krw", "거래대금")
_OPEN_FIELDS = ("open_price", "open", "시가")
_HIGH_FIELDS = ("day_high", "high", "고가")
_LOW_FIELDS = ("day_low", "low", "저가")
_PREV_CLOSE_FIELDS = ("prev_close", "previous_close", "base_price", "기준가", "전일종가")


def _format_time(value: datetime | None = None) -> str:
    return (value or datetime.now()).replace(microsecond=0).isoformat()


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").strip().replace(",", "").replace("+", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").strip().replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


__all__ = [
    "CANDIDATE_HYDRATION_PURPOSE",
    "CANDIDATE_HYDRATION_RQ_NAME",
    "CANDIDATE_HYDRATION_TR_CODE",
    "CandidateHydrationConfig",
    "CandidateHydrationEnqueueResult",
    "CandidateHydrator",
    "hydration_idempotency_key",
    "parse_candidate_hydration_rows",
]
