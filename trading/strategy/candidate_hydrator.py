from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from trading.broker.command_queue import CommandPriority, CommandStatus
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent, new_message_id
from trading.strategy.candidates import normalize_code
from trading.strategy.candidate_fsm import CandidateBlockingStage, CandidateFsmService, CandidateReasonCode
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import Candidate, CandidateEvent, CandidateState
from trading.theme_engine.backfill import OPT10001_FIELDS, parse_opt10001_backfill


CANDIDATE_HYDRATION_PURPOSE = "candidate_hydration"
CANDIDATE_HYDRATION_TR_CODE = "opt10001"
CANDIDATE_HYDRATION_RQ_NAME = "CandidateHydration_opt10001"
CANDIDATE_HYDRATION_SCREEN_NO = "8730"
CANDIDATE_HYDRATION_BUCKET = "basic"


@dataclass(frozen=True)
class TrFieldSpec:
    broker_field: str
    normalized_field: str
    aliases: tuple[str, ...] = ()


CANDIDATE_HYDRATION_FIELD_SPECS = (
    TrFieldSpec("종목명", "stock_name", ("종목명", "stock_name", "name")),
    TrFieldSpec("현재가", "current_price", ("현재가", "current_price", "price")),
    TrFieldSpec("등락율", "change_rate", ("등락율", "등락률", "change_rate")),
    TrFieldSpec("거래량", "volume", ("거래량", "volume")),
    TrFieldSpec("거래대금", "trade_value", ("거래대금", "trade_value", "turnover", "turnover_krw")),
    TrFieldSpec("시가", "open_price", ("시가", "open_price", "open")),
    TrFieldSpec("고가", "day_high", ("고가", "day_high", "session_high", "high")),
    TrFieldSpec("저가", "day_low", ("저가", "day_low", "session_low", "low")),
    TrFieldSpec("기준가", "prev_close", ("기준가", "prev_close", "previous_close", "base_price")),
)
CANDIDATE_HYDRATION_FIELDS = list(OPT10001_FIELDS)


@dataclass(frozen=True)
class CandidateHydrationConfig:
    enabled: bool = True
    max_per_cycle: int = 5
    max_pending: int = 10
    ttl_sec: int = 90
    max_attempts: int = 3
    retry_base_sec: int = 15
    retry_max_sec: int = 300
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
            max_attempts=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_MAX_ATTEMPTS", 3)),
            retry_base_sec=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_RETRY_BASE_SEC", 15)),
            retry_max_sec=max(1, _int_env("TRADING_CANDIDATE_HYDRATION_RETRY_MAX_SEC", 300)),
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
        self.fsm = CandidateFsmService(db, clock=self.clock)

    def enqueue_due_candidates(self, *, trade_date: str | None = None) -> list[CandidateHydrationEnqueueResult]:
        if not self.config.enabled:
            return []
        self.recover_from_command_history()
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
        generation = self._retry_generation(candidate)
        bucket = self.config.bucket if generation <= 0 else f"{self.config.bucket}:retry{generation}"
        key = hydration_idempotency_key(
            trade_date=candidate.trade_date,
            code=candidate.code,
            tr_code=self.config.tr_code,
            bucket=bucket,
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
                "inputs": {"종목코드": candidate.code},
                "fields": list(CANDIDATE_HYDRATION_FIELDS),
                "field_specs": [field.__dict__ for field in CANDIDATE_HYDRATION_FIELD_SPECS],
                "bucket": self.config.bucket,
                "retry_generation": generation,
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
        if event.type not in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
            return False
        payload = dict(event.payload or {})
        command_id = str(payload.get("command_id") or event.command_id or "")
        command_payload = self._command_payload(command_id)
        if str(payload.get("purpose") or command_payload.get("purpose") or "") != CANDIDATE_HYDRATION_PURPOSE:
            return False
        if event.type != "command_ack":
            reason = {
                "command_failed": "COMMAND_FAILED",
                "command_timeout": "TIMEOUT",
                "command_expired": "EXPIRED",
            }.get(event.type, event.type.upper())
            self.mark_failure(payload, event=event, reason=reason)
            return True
        status = str(payload.get("status") or payload.get("command_status") or CommandStatus.ACKED.value).upper()
        result_code = _int(payload.get("result_code") or payload.get("code"))
        if status != CommandStatus.ACKED.value or result_code < 0:
            self.mark_failure(payload, event=event, reason=status if status != CommandStatus.ACKED.value else "REJECTED")
            return True
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
            parsed = parse_candidate_hydration_rows(rows, code=code)
        failure_reason = _hydration_parse_failure_reason(parsed, _ack_rows(raw_payload))
        if failure_reason:
            return self.mark_failure(raw_payload, event=event, reason=failure_reason, parsed=parsed)
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
                "failure_status": "",
                "command_id": command_id,
                "idempotency_key": str(raw_payload.get("idempotency_key") or command_payload.get("idempotency_key") or ""),
                "merged_at": _format_time(self.clock()),
                "parsed": dict(parsed),
                "raw_rows": _ack_rows(raw_payload)[:5],
                "last_error": "",
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
        self.fsm.on_hydration_result(
            saved,
            {
                "command_id": command_id,
                "market_data_merged": market_merged,
                "parsed": parsed,
                "source_event_id": event.event_id if event else "",
            },
        )
        saved = self.db.save_candidate(saved)
        updater = getattr(self.db, "update_candidate_hydration_request_status", None)
        if callable(updater) and command_id:
            updater(command_id=command_id, status="ACKED", result_status="MERGED")
        return saved

    def mark_failure(
        self,
        payload: Mapping[str, Any],
        *,
        event: GatewayEvent | None = None,
        reason: str,
        parsed: Mapping[str, Any] | None = None,
    ) -> Candidate | None:
        raw_payload = dict(payload or {})
        command_id = str(raw_payload.get("command_id") or (event.command_id if event else "") or "")
        command_payload = self._command_payload(command_id)
        trade_date = str(raw_payload.get("trade_date") or command_payload.get("trade_date") or self.clock().date().isoformat())
        code = normalize_code(str(raw_payload.get("code") or command_payload.get("code") or ""))
        candidate_id = _int(raw_payload.get("candidate_id") or command_payload.get("candidate_id"))
        candidate = self.db.load_candidate_by_id(candidate_id) if candidate_id else None
        if candidate is None and trade_date and code:
            candidate = self.db.load_candidate(trade_date, code)
        parsed_payload = dict(parsed or _parsed_hydration_payload(raw_payload) or {})
        if candidate is None:
            self._save_result(None, raw_payload, parsed_payload, status="ORPHAN_FAILURE", reason=reason)
            return None
        now = self.clock().replace(microsecond=0)
        previous_state = candidate.state
        if candidate.state in {CandidateState.DETECTED, CandidateState.HYDRATING, CandidateState.WAIT_DATA}:
            candidate.state = CandidateState.WAIT_DATA
        metadata = dict(candidate.metadata or {})
        hydration = dict(metadata.get("candidate_hydration") or {})
        retry_count = int(hydration.get("retry_count") or 0) + 1
        retry_generation = int(hydration.get("retry_generation") or 0) + 1
        retryable = retry_count < max(1, self.config.max_attempts)
        retry_after_at = now + timedelta(seconds=self._retry_delay_sec(retry_count)) if retryable else None
        hydration.update(
            {
                "status": "RETRY_WAIT" if retryable else "FAILED",
                "failure_status": str(reason or "FAILED"),
                "command_id": command_id,
                "idempotency_key": str(raw_payload.get("idempotency_key") or command_payload.get("idempotency_key") or ""),
                "last_error": str(raw_payload.get("error") or raw_payload.get("message") or reason or "FAILED"),
                "retry_count": retry_count,
                "retry_generation": retry_generation,
                "retry_after_at": retry_after_at.isoformat() if retry_after_at else "",
                "failed_at": now.isoformat(),
                "parsed": parsed_payload,
                "raw_rows": _ack_rows(raw_payload)[:5],
            }
        )
        reason_codes = _dedupe([*list(metadata.get("reason_codes") or []), "WAIT_DATA", str(reason or "FAILED")])
        metadata["reason_codes"] = reason_codes
        metadata["candidate_hydration"] = hydration
        candidate.metadata = metadata
        candidate.last_seen_at = now.isoformat()
        saved = self.db.save_candidate_with_events(
            candidate,
            [
                CandidateEvent(
                    candidate_id=candidate.id,
                    event_type="candidate_hydration_failed",
                    from_state=previous_state,
                    to_state=candidate.state,
                    source=None,
                    reason=str(reason or "candidate hydration failed"),
                    created_at=candidate.last_seen_at,
                    payload={
                        "command_id": command_id,
                        "code": candidate.code,
                        "reason": reason,
                        "retry_count": retry_count,
                        "retry_after_at": retry_after_at.isoformat() if retry_after_at else "",
                    },
                )
            ],
        )
        self._save_result(saved, raw_payload, parsed_payload, status="FAILED", reason=reason)
        self.fsm.apply_blocking_reason(
            saved,
            CandidateBlockingStage.DATA,
            reason or CandidateReasonCode.HYDRATION_PENDING.value,
            details={"command_id": command_id, "reason": reason, "retry_count": retry_count},
            source_event_id=event.event_id if event else "",
            source_event_type="hydration_failure",
            source_component="CandidateHydrator",
        )
        saved = self.db.save_candidate(saved)
        updater = getattr(self.db, "update_candidate_hydration_request_status", None)
        if callable(updater) and command_id:
            updater(command_id=command_id, status="FAILED", result_status=reason)
        return saved

    def recover_from_command_history(self) -> dict[str, int]:
        self.gateway_state.expire_old_commands(self.clock())
        summary = {"processed": 0, "expired": 0, "failed": 0}
        for record in self.gateway_state.list_commands(include_finished=True, command_type="tr_request", limit=200):
            payload = _record_payload(record)
            result_payload = _record_result_payload(record)
            if str(payload.get("purpose") or result_payload.get("purpose") or "") != CANDIDATE_HYDRATION_PURPOSE:
                continue
            status = str(record.get("status") or "").upper()
            command_id = str(record.get("command_id") or payload.get("command_id") or "")
            candidate = self._candidate_for_command_payload(payload, result_payload)
            if candidate is None or not self._candidate_waiting_on_command(candidate, command_id):
                continue
            if status == CommandStatus.ACKED.value:
                parsed = _parsed_hydration_payload(result_payload)
                if not parsed:
                    parsed = parse_candidate_hydration_rows(_ack_rows(result_payload), code=str(payload.get("code") or ""))
                reason = _hydration_parse_failure_reason(parsed, _ack_rows(result_payload))
                if reason:
                    self.mark_failure({**payload, **result_payload, "command_id": command_id}, reason=reason, parsed=parsed)
                    summary["processed"] += 1
                    summary["failed"] += 1
                continue
            if status in {
                CommandStatus.REJECTED.value,
                CommandStatus.FAILED.value,
                CommandStatus.EXPIRED.value,
                CommandStatus.CANCELLED.value,
                CommandStatus.EXPIRED_BEFORE_DISPATCH.value,
            }:
                reason = "EXPIRED" if status in {CommandStatus.EXPIRED.value, CommandStatus.EXPIRED_BEFORE_DISPATCH.value} else status
                self.mark_failure({**payload, **result_payload, "command_id": command_id}, reason=reason)
                summary["processed"] += 1
                if reason == "EXPIRED":
                    summary["expired"] += 1
                else:
                    summary["failed"] += 1
        return summary

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
            "retry_generation": _int((command.payload or {}).get("retry_generation")),
        }
        candidate.metadata = metadata
        candidate.last_seen_at = _format_time(self.clock())
        saved = self.db.save_candidate_with_events(
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
        self.fsm.on_hydration_requested(saved)
        return self.db.save_candidate(saved)

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
            return bool(self.market_data.apply_theme_backfill(code, payload, now=self.clock()))
        except Exception:
            return False

    def _needs_hydration(self, candidate: Candidate) -> bool:
        metadata = dict(candidate.metadata or {})
        hydration = dict(metadata.get("candidate_hydration") or {})
        status = str(hydration.get("status") or "").upper()
        if status in {"ACKED", "MERGED", "OK"}:
            return False
        if status == "PENDING":
            return False
        if status == "FAILED" and int(hydration.get("retry_count") or 0) >= max(1, self.config.max_attempts):
            return False
        if status == "RETRY_WAIT":
            retry_after_at = _parse_time(hydration.get("retry_after_at"))
            if retry_after_at is not None and self.clock().replace(microsecond=0) < retry_after_at:
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

    def _candidate_for_command_payload(self, payload: Mapping[str, Any], result_payload: Mapping[str, Any]) -> Candidate | None:
        candidate_id = _int(result_payload.get("candidate_id") or payload.get("candidate_id"))
        candidate = self.db.load_candidate_by_id(candidate_id) if candidate_id else None
        if candidate is not None:
            return candidate
        trade_date = str(result_payload.get("trade_date") or payload.get("trade_date") or "")
        code = normalize_code(str(result_payload.get("code") or payload.get("code") or ""))
        if trade_date and code:
            return self.db.load_candidate(trade_date, code)
        return None

    @staticmethod
    def _candidate_waiting_on_command(candidate: Candidate, command_id: str) -> bool:
        hydration = dict(dict(candidate.metadata or {}).get("candidate_hydration") or {})
        status = str(hydration.get("status") or "").upper()
        return bool(command_id and str(hydration.get("command_id") or "") == command_id and status == "PENDING")

    def _retry_generation(self, candidate: Candidate) -> int:
        hydration = dict(dict(candidate.metadata or {}).get("candidate_hydration") or {})
        return max(_int(hydration.get("retry_generation")), _int(hydration.get("retry_count")))

    def _retry_delay_sec(self, retry_count: int) -> int:
        attempt_index = max(0, int(retry_count or 1) - 1)
        delay = int(self.config.retry_base_sec) * (2**attempt_index)
        return max(1, min(int(self.config.retry_max_sec), delay))


def hydration_idempotency_key(*, trade_date: str, code: str, tr_code: str = CANDIDATE_HYDRATION_TR_CODE, bucket: str = CANDIDATE_HYDRATION_BUCKET) -> str:
    return f"candidate_hydration:{trade_date}:{normalize_code(code)}:{tr_code}:{bucket}"


def parse_candidate_hydration_rows(rows: Iterable[Mapping[str, Any]], *, code: str = "") -> dict[str, Any]:
    raw_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    parsed = parse_opt10001_backfill(raw_rows, code=code)
    if parsed:
        if parsed.get("turnover") is not None and parsed.get("trade_value") is None:
            parsed["trade_value"] = parsed.get("turnover")
        if parsed.get("session_high") is not None and parsed.get("day_high") is None:
            parsed["day_high"] = parsed.get("session_high")
        if parsed.get("session_low") is not None and parsed.get("day_low") is None:
            parsed["day_low"] = parsed.get("session_low")
        parsed["raw"] = raw_rows[0] if raw_rows else {}
        if parsed.get("parser_status") != "EMPTY":
            return parsed
    for raw in raw_rows:
        normalized = {_normalize_field_name(key): value for key, value in dict(raw).items()}
        fallback = {
            "code": normalize_code(str(code or _field_value(normalized, _CODE_FIELDS) or "")),
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
        if fallback["code"] or fallback["current_price"] > 0 or fallback["stock_name"]:
            return fallback
    return parsed or {}


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


def _hydration_parse_failure_reason(parsed: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> str:
    row_list = list(rows or [])
    status = str(parsed.get("parser_status") or "").upper()
    if status == "EMPTY" or not row_list:
        return "TR_EMPTY"
    missing = {str(item) for item in list(parsed.get("parser_missing_fields") or [])}
    if not parsed or ("current_price" in missing and _float(parsed.get("current_price")) <= 0):
        return "PARSE_ERROR"
    return ""


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    command = dict(record.get("command") or {})
    payload = command.get("payload")
    if isinstance(payload, Mapping):
        return dict(payload)
    payload = record.get("payload")
    return dict(payload or {}) if isinstance(payload, Mapping) else {}


def _record_result_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = record.get("result_payload")
    return dict(payload or {}) if isinstance(payload, Mapping) else {}


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


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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
    "TrFieldSpec",
    "hydration_idempotency_key",
    "parse_candidate_hydration_rows",
]
