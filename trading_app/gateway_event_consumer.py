from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from storage.db import TradingDatabase
from storage.event_log import EventLogRepository, REPLAYABLE_GATEWAY_EVENT_TYPES, dedupe_key_for_gateway_event
from trading.broker.models import GatewayEvent
from trading.strategy.order_manager import OrderManagerRuntimePipeline
from trading.strategy.order_models import OrderKillSwitchState, OrderManagerConfig


CANONICAL_ORDER_EVENT_TYPES = {
    "COMMAND_ACK",
    "COMMAND_FAILED",
    "ORDER_ACCEPTED",
    "ORDER_REJECTED",
    "ORDER_PARTIALLY_FILLED",
    "ORDER_FILLED",
    "ORDER_CANCEL_ACCEPTED",
    "ORDER_CANCELLED",
    "ORDER_STATUS_SNAPSHOT",
    "BALANCE_SNAPSHOT",
    "POSITION_SNAPSHOT",
    "RECONCILE_SNAPSHOT",
}
ORDER_COMMAND_TYPES = {"send_order", "cancel_order", "modify_order"}
FILL_EVENT_TYPES = {"order_fill", "execution", "execution_event", "fill"}
IGNORED_REPLAY_TYPES = {"price_tick", "heartbeat"}


@dataclass(frozen=True)
class GatewayEventConsumerConfig:
    core_enabled: bool = True
    order_enabled: bool = True
    replay_enabled: bool = True
    replay_on_startup: bool = True
    replay_interval_sec: float = 1.0
    replay_batch_size: int = 100
    processing_lease_sec: int = 30
    max_attempts: int = 5
    retry_base_sec: float = 1.0
    retry_max_sec: float = 30.0
    fail_closed: bool = True
    dead_letter_blocks_buy: bool = True
    pending_max_age_sec: int = 10
    live_queue_max: int = 1000
    replay_price_tick_enabled: bool = False
    replay_heartbeat_enabled: bool = False
    receipt_enabled: bool = True
    handler_version: str = "order_lifecycle_v1"

    @classmethod
    def from_env(cls) -> "GatewayEventConsumerConfig":
        return cls(
            core_enabled=_env_bool("TRADING_CORE_EVENT_CONSUMER_ENABLED", True),
            order_enabled=_env_bool("TRADING_ORDER_EVENT_CONSUMER_ENABLED", True),
            replay_enabled=_env_bool("TRADING_EVENT_REPLAY_ENABLED", True),
            replay_on_startup=_env_bool("TRADING_EVENT_REPLAY_ON_STARTUP", True),
            replay_interval_sec=max(0.1, _env_float("TRADING_EVENT_REPLAY_INTERVAL_SEC", 1.0)),
            replay_batch_size=max(1, _env_int("TRADING_EVENT_REPLAY_BATCH_SIZE", 100)),
            processing_lease_sec=max(1, _env_int("TRADING_EVENT_PROCESSING_LEASE_SEC", 30)),
            max_attempts=max(1, _env_int("TRADING_EVENT_MAX_ATTEMPTS", 5)),
            retry_base_sec=max(0.1, _env_float("TRADING_EVENT_RETRY_BASE_SEC", 1.0)),
            retry_max_sec=max(1.0, _env_float("TRADING_EVENT_RETRY_MAX_SEC", 30.0)),
            fail_closed=_env_bool("TRADING_ORDER_EVENT_FAIL_CLOSED", True),
            dead_letter_blocks_buy=_env_bool("TRADING_ORDER_EVENT_DEAD_LETTER_BLOCKS_BUY", True),
            pending_max_age_sec=max(1, _env_int("TRADING_ORDER_EVENT_PENDING_MAX_AGE_SEC", 10)),
            live_queue_max=max(1, _env_int("TRADING_ORDER_EVENT_LIVE_QUEUE_MAX", 1000)),
            replay_price_tick_enabled=_env_bool("TRADING_EVENT_REPLAY_PRICE_TICK_ENABLED", False),
            replay_heartbeat_enabled=_env_bool("TRADING_EVENT_REPLAY_HEARTBEAT_ENABLED", False),
            receipt_enabled=_env_bool("TRADING_ORDER_EVENT_RECEIPT_ENABLED", True),
        )


@dataclass(frozen=True)
class CanonicalGatewayEvent:
    canonical_type: str
    payload: dict[str, Any]
    raw_event_type: str
    source_event_id: str
    dedupe_key: str
    critical: bool = True
    ignored: bool = False
    ignore_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventProcessingResult:
    status: str
    source_event_id: str = ""
    raw_event_type: str = ""
    canonical_event_type: str = ""
    dedupe_key: str = ""
    managed_order_id: int | None = None
    managed_intent_id: int | None = None
    core_events: tuple[dict[str, Any], ...] = ()
    retryable: bool = False
    reconcile_required: bool = False
    stop_new_buy: bool = False
    error: str = ""
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_event_id": self.source_event_id,
            "raw_event_type": self.raw_event_type,
            "canonical_event_type": self.canonical_event_type,
            "dedupe_key": self.dedupe_key,
            "managed_order_id": self.managed_order_id,
            "managed_intent_id": self.managed_intent_id,
            "core_events": list(self.core_events),
            "retryable": self.retryable,
            "reconcile_required": self.reconcile_required,
            "stop_new_buy": self.stop_new_buy,
            "error": self.error,
            "reason": self.reason,
            "details": dict(self.details or {}),
        }


@dataclass(frozen=True)
class EventConsumerHealthSnapshot:
    status: str
    consumer_enabled: bool
    consumer_running: bool
    order_lifecycle_ready: bool
    live_queue_depth: int = 0
    pending_event_count: int = 0
    retry_wait_count: int = 0
    failed_count: int = 0
    dead_letter_count: int = 0
    oldest_pending_age_sec: float = 0.0
    processed_count: int = 0
    duplicate_applied_count: int = 0
    unmatched_event_count: int = 0
    reconcile_required_count: int = 0
    last_event_type: str = ""
    last_event_at: str = ""
    last_processed_at: str = ""
    last_error: str = ""
    replay_status: str = "IDLE"
    replay_duration_ms: float = 0.0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class GatewayEventCodec:
    def __init__(self, config: GatewayEventConsumerConfig | None = None) -> None:
        self.config = config or GatewayEventConsumerConfig.from_env()

    def decode(self, event: GatewayEvent, *, source_event_id: str = "") -> CanonicalGatewayEvent:
        raw_type = str(event.type or "")
        payload = dict(event.payload or {})
        if event.command_id and not payload.get("command_id"):
            payload["command_id"] = event.command_id
        payload.setdefault("raw_event_type", raw_type)
        resolved_source_event_id = str(source_event_id or event.event_id or "")
        if raw_type == "price_tick" and not self.config.replay_price_tick_enabled:
            return self._ignored(event, payload, resolved_source_event_id, "PRICE_TICK_REPLAY_DISABLED")
        if raw_type == "heartbeat" and not self.config.replay_heartbeat_enabled:
            return self._ignored(event, payload, resolved_source_event_id, "HEARTBEAT_REPLAY_DISABLED")
        if str(payload.get("purpose") or "") == "broker_reconcile":
            return self._ignored(event, payload, resolved_source_event_id, "BROKER_RECONCILE_EVENT_ROUTED_TO_RECONCILE_CONSUMER")
        if raw_type in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
            return self._decode_command_event(event, payload, resolved_source_event_id)
        if raw_type == "kiwoom_order_chejan":
            return self._decode_kiwoom_order_chejan(event, payload, resolved_source_event_id)
        if raw_type == "kiwoom_balance_chejan":
            return self._canonical(event, payload, resolved_source_event_id, "POSITION_SNAPSHOT")
        if raw_type == "kiwoom_special_chejan":
            return self._ignored(event, payload, resolved_source_event_id, "KIWOOM_SPECIAL_CHEJAN_DIAGNOSTIC_ONLY")
        if raw_type in FILL_EVENT_TYPES:
            canonical = "ORDER_FILLED" if _remaining_quantity(payload) <= 0 else "ORDER_PARTIALLY_FILLED"
            return self._canonical(event, payload, resolved_source_event_id, canonical)
        mapping = {
            "order_ack": "ORDER_ACCEPTED",
            "order_reject": "ORDER_REJECTED",
            "cancel_ack": "ORDER_CANCEL_ACCEPTED",
            "order_cancel": "ORDER_CANCELLED",
            "order_cancelled": "ORDER_CANCELLED",
            "order_status_snapshot": "ORDER_STATUS_SNAPSHOT",
            "balance_snapshot": "BALANCE_SNAPSHOT",
            "position_snapshot": "POSITION_SNAPSHOT",
            "reconcile_snapshot": "RECONCILE_SNAPSHOT",
        }
        canonical = mapping.get(raw_type)
        if canonical:
            return self._canonical(event, payload, resolved_source_event_id, canonical)
        return self._ignored(event, payload, resolved_source_event_id, "NON_ORDER_EVENT_HANDLED_BY_LEGACY")

    def _decode_kiwoom_order_chejan(
        self,
        event: GatewayEvent,
        payload: dict[str, Any],
        source_event_id: str,
    ) -> CanonicalGatewayEvent:
        parser_status = str(payload.get("parser_status") or "").upper()
        if parser_status == "INVALID":
            data = {**payload, "status": "REJECTED", "reason": "KIWOOM_CHEJAN_PARSER_INVALID"}
            return self._canonical(event, data, source_event_id, "ORDER_REJECTED")
        event_kind = str(payload.get("event_kind") or "").lower()
        reject_reason = str(payload.get("reject_reason") or "")
        order_status = str(payload.get("order_status") or "")
        has_fill_identity = bool(payload.get("execution_id"))
        has_fill_quantity = _safe_int(payload.get("incremental_execution_quantity") or payload.get("execution_quantity"), 0) > 0
        if event_kind == "order_fill" and (has_fill_identity or has_fill_quantity):
            canonical = "ORDER_FILLED" if _remaining_quantity(payload) <= 0 else "ORDER_PARTIALLY_FILLED"
            return self._canonical(event, payload, source_event_id, canonical)
        if event_kind == "order_rejected" or reject_reason or "거부" in order_status or "거절" in order_status:
            data = {**payload, "status": "REJECTED"}
            return self._canonical(event, data, source_event_id, "ORDER_REJECTED")
        if event_kind == "order_cancelled":
            return self._canonical(event, payload, source_event_id, "ORDER_CANCELLED")
        if event_kind == "order_cancel_accepted":
            return self._canonical(event, payload, source_event_id, "ORDER_CANCEL_ACCEPTED")
        if event_kind == "order_accepted" and payload.get("order_no"):
            return self._canonical(event, payload, source_event_id, "ORDER_ACCEPTED")
        return self._canonical(event, payload, source_event_id, "ORDER_STATUS_SNAPSHOT")

    def _decode_command_event(
        self,
        event: GatewayEvent,
        payload: dict[str, Any],
        source_event_id: str,
    ) -> CanonicalGatewayEvent:
        raw_type = str(event.type or "")
        command_type = str(payload.get("command_type") or "")
        status = str(payload.get("status") or "").upper()
        result_code = _safe_int(payload.get("result_code") or dict(payload.get("order_result") or {}).get("result_code"), 0)
        order_no = _order_no(payload)
        if raw_type != "command_ack":
            return self._canonical(event, payload, source_event_id, "COMMAND_FAILED")
        if status in {"FAILED", "REJECTED"} or result_code != 0:
            canonical = "ORDER_REJECTED" if command_type in ORDER_COMMAND_TYPES else "COMMAND_FAILED"
            return self._canonical(event, payload, source_event_id, canonical)
        if command_type == "cancel_order" and order_no:
            return self._canonical(event, payload, source_event_id, "ORDER_CANCEL_ACCEPTED")
        if command_type in {"send_order", "modify_order"} and order_no:
            return self._canonical(event, payload, source_event_id, "ORDER_ACCEPTED")
        return self._canonical(event, payload, source_event_id, "COMMAND_ACK")

    def _canonical(
        self,
        event: GatewayEvent,
        payload: dict[str, Any],
        source_event_id: str,
        canonical_type: str,
    ) -> CanonicalGatewayEvent:
        payload = _normalized_payload(payload)
        dedupe_key = _canonical_dedupe_key(canonical_type, payload, source_event_id or event.event_id, event)
        return CanonicalGatewayEvent(
            canonical_type=canonical_type,
            payload=payload,
            raw_event_type=str(event.type or ""),
            source_event_id=str(source_event_id or event.event_id or dedupe_key),
            dedupe_key=dedupe_key,
            critical=True,
            metadata={"raw_event_type": str(event.type or ""), "gateway_event_id": str(event.event_id or "")},
        )

    def _ignored(
        self,
        event: GatewayEvent,
        payload: dict[str, Any],
        source_event_id: str,
        reason: str,
    ) -> CanonicalGatewayEvent:
        return CanonicalGatewayEvent(
            canonical_type="IGNORED",
            payload=_normalized_payload(payload),
            raw_event_type=str(event.type or ""),
            source_event_id=str(source_event_id or event.event_id or ""),
            dedupe_key=dedupe_key_for_gateway_event(event),
            critical=False,
            ignored=True,
            ignore_reason=reason,
            metadata={"raw_event_type": str(event.type or ""), "gateway_event_id": str(event.event_id or "")},
        )


class OrderLifecycleEventConsumer:
    def __init__(
        self,
        *,
        db_path: str | Path,
        gateway_state: Any,
        config: GatewayEventConsumerConfig | None = None,
        codec: GatewayEventCodec | None = None,
        dirty_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.gateway_state = gateway_state
        self.config = config or GatewayEventConsumerConfig.from_env()
        self.codec = codec or GatewayEventCodec(self.config)
        self.dirty_callback = dirty_callback

    def dispatch(self, event: GatewayEvent, *, source_event_id: str = "") -> EventProcessingResult:
        canonical = self.codec.decode(event, source_event_id=source_event_id)
        if canonical.ignored:
            return EventProcessingResult(
                status="IGNORED",
                source_event_id=canonical.source_event_id,
                raw_event_type=canonical.raw_event_type,
                canonical_event_type=canonical.canonical_type,
                dedupe_key=canonical.dedupe_key,
                reason=canonical.ignore_reason,
            )
        if not self.config.order_enabled:
            return EventProcessingResult(
                status="IGNORED",
                source_event_id=canonical.source_event_id,
                raw_event_type=canonical.raw_event_type,
                canonical_event_type=canonical.canonical_type,
                dedupe_key=canonical.dedupe_key,
                reason="ORDER_EVENT_CONSUMER_DISABLED",
            )
        db = TradingDatabase(self.db_path)
        try:
            if self.config.receipt_enabled:
                receipt = db.find_order_gateway_event_receipt(
                    source_event_id=canonical.source_event_id,
                    dedupe_key=canonical.dedupe_key,
                )
                if receipt:
                    return EventProcessingResult(
                        status="DUPLICATE_ALREADY_APPLIED",
                        source_event_id=canonical.source_event_id,
                        raw_event_type=canonical.raw_event_type,
                        canonical_event_type=canonical.canonical_type,
                        dedupe_key=canonical.dedupe_key,
                        managed_order_id=receipt.get("managed_order_id"),
                        managed_intent_id=receipt.get("managed_intent_id"),
                        reason="RECEIPT_EXISTS",
                        details={"receipt": receipt},
                    )
            pipeline = OrderManagerRuntimePipeline(
                db=db,
                gateway_state=self.gateway_state,
                config=OrderManagerConfig.from_env(),
            )
            result = pipeline.apply_canonical_order_event(
                canonical.canonical_type,
                canonical.payload,
                source_event_id=canonical.source_event_id,
            )
            self._update_projection(db, canonical, result)
            receipt = self._save_receipt(db, canonical, result)
            if self.dirty_callback is not None:
                self.dirty_callback(f"order_lifecycle:{canonical.canonical_type}")
            reconcile_required = _result_reconcile_required(result)
            status = "APPLIED" if not reconcile_required else "RECONCILE_REQUIRED"
            return EventProcessingResult(
                status=status,
                source_event_id=canonical.source_event_id,
                raw_event_type=canonical.raw_event_type,
                canonical_event_type=canonical.canonical_type,
                dedupe_key=canonical.dedupe_key,
                managed_order_id=receipt.get("managed_order_id"),
                managed_intent_id=receipt.get("managed_intent_id"),
                core_events=tuple(_core_events_for(canonical, result)),
                reconcile_required=reconcile_required,
                stop_new_buy=reconcile_required,
                reason=str(result.get("reason") or result.get("status") or ""),
                details={"result": result, "receipt": receipt},
            )
        finally:
            db.close()

    def _save_receipt(self, db: TradingDatabase, canonical: CanonicalGatewayEvent, result: dict[str, Any]) -> dict:
        receipt = {
            "source_event_id": canonical.source_event_id,
            "dedupe_key": canonical.dedupe_key,
            "raw_event_type": canonical.raw_event_type,
            "canonical_event_type": canonical.canonical_type,
            "account": str(canonical.payload.get("account") or ""),
            "command_id": str(canonical.payload.get("command_id") or ""),
            "order_no": str(canonical.payload.get("order_no") or ""),
            "original_order_no": str(canonical.payload.get("original_order_no") or ""),
            "execution_id": str(_execution_id(canonical.payload) or ""),
            "code": str(canonical.payload.get("code") or ""),
            "side": str(canonical.payload.get("side") or "").upper(),
            "managed_order_id": result.get("order_id") or result.get("managed_order_id"),
            "managed_intent_id": result.get("intent_id") or result.get("managed_intent_id"),
            "apply_status": "RECONCILE_REQUIRED" if _result_reconcile_required(result) else "APPLIED",
            "payload_checksum": _payload_checksum(canonical.payload),
            "applied_at": _now_text(),
            "details": {"payload": canonical.payload, "result": result},
        }
        return db.save_order_gateway_event_receipt(receipt)

    def _update_projection(self, db: TradingDatabase, canonical: CanonicalGatewayEvent, result: dict[str, Any]) -> None:
        payload = canonical.payload
        order_no = str(payload.get("order_no") or "")
        if canonical.canonical_type in {
            "ORDER_ACCEPTED",
            "ORDER_PARTIALLY_FILLED",
            "ORDER_FILLED",
            "ORDER_CANCEL_ACCEPTED",
            "ORDER_CANCELLED",
            "ORDER_STATUS_SNAPSHOT",
        } and order_no:
            db.upsert_broker_order_state(
                {
                    "account": payload.get("account") or "",
                    "order_no": order_no,
                    "original_order_no": payload.get("original_order_no") or "",
                    "command_id": payload.get("command_id") or "",
                    "code": payload.get("code") or "",
                    "side": payload.get("side") or "",
                    "order_qty": payload.get("order_qty") or payload.get("quantity") or result.get("quantity") or 0,
                    "filled_qty": payload.get("filled_qty") or payload.get("filled_quantity") or result.get("filled_quantity") or 0,
                    "remaining_qty": payload.get("remaining_qty") or payload.get("remaining_quantity") or result.get("remaining_quantity") or 0,
                    "avg_fill_price": payload.get("avg_fill_price") or payload.get("price") or result.get("avg_fill_price") or 0,
                    "broker_status": canonical.canonical_type,
                    "last_event_id": canonical.source_event_id,
                    "last_event_at": payload.get("timestamp") or _now_text(),
                    "details": {"payload": payload, "result": result},
                }
            )
        if canonical.canonical_type in {"BALANCE_SNAPSHOT", "POSITION_SNAPSHOT"}:
            positions = payload.get("positions") if isinstance(payload.get("positions"), list) else [payload]
            for item in positions:
                if not isinstance(item, dict):
                    continue
                db.upsert_broker_position_state(
                    {
                        "account": item.get("account") or payload.get("account") or "",
                        "code": item.get("code") or payload.get("code") or "",
                        "quantity": item.get("quantity") or item.get("qty") or 0,
                        "available_quantity": item.get("available_quantity") or item.get("available_qty") or 0,
                        "avg_price": item.get("avg_price") or item.get("average_price") or 0,
                        "last_snapshot_id": canonical.source_event_id,
                        "snapshot_at": payload.get("timestamp") or _now_text(),
                        "details": {"payload": payload},
                    }
                )


class GatewayEventDispatcher:
    def __init__(
        self,
        *,
        event_log: EventLogRepository,
        order_consumer: OrderLifecycleEventConsumer,
        config: GatewayEventConsumerConfig | None = None,
    ) -> None:
        self.event_log = event_log
        self.order_consumer = order_consumer
        self.config = config or GatewayEventConsumerConfig.from_env()
        self.worker_id = f"order-consumer-{uuid4().hex[:8]}"
        self.running = False
        self.metrics: dict[str, Any] = {
            "live_received_count": 0,
            "claimed_count": 0,
            "processed_count": 0,
            "replayed_count": 0,
            "duplicate_count": 0,
            "ignored_count": 0,
            "retry_count": 0,
            "failed_count": 0,
            "dead_letter_count": 0,
            "unmatched_count": 0,
            "queue_overflow_count": 0,
            "processing_duration_ms": 0.0,
            "max_processing_duration_ms": 0.0,
            "last_success_at": "",
            "last_error": "",
            "last_event_type": "",
            "last_event_at": "",
            "last_processed_at": "",
            "replay_status": "IDLE",
            "replay_duration_ms": 0.0,
        }

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def consume_live_event(self, event: GatewayEvent) -> EventProcessingResult:
        self.metrics["live_received_count"] = int(self.metrics.get("live_received_count") or 0) + 1
        self.metrics["last_event_type"] = str(event.type or "")
        self.metrics["last_event_at"] = str(event.timestamp or _now_text())
        if event.type in IGNORED_REPLAY_TYPES:
            return EventProcessingResult(status="IGNORED", raw_event_type=event.type, reason=f"{event.type.upper()}_REPLAY_DISABLED")
        record = self.event_log.get_by_event_id(event.event_id)
        if record is None:
            record = self.event_log.find_by_dedupe_key(dedupe_key_for_gateway_event(event))
        if record is None:
            if self.order_consumer.codec.decode(event).critical and self.config.fail_closed:
                self._fail_closed("EVENT_LOG_UNAVAILABLE", {"event": event.to_dict()})
            return self._record_result(
                self.order_consumer.dispatch(event, source_event_id=event.event_id),
                processing_started=time.perf_counter(),
            )
        claimed = self.event_log.claim_event(
            record.id,
            worker_id=self.worker_id,
            lease_sec=self.config.processing_lease_sec,
        )
        if claimed is None:
            return EventProcessingResult(
                status="RETRY_WAIT",
                source_event_id=record.event_id,
                raw_event_type=record.event_type,
                reason="EVENT_ALREADY_CLAIMED_OR_TERMINAL",
            )
        if str(getattr(claimed, "processing_status", "") or "") in {"PROCESSED", "IGNORED", "DEAD_LETTER"}:
            return EventProcessingResult(
                status=str(claimed.processing_status),
                source_event_id=claimed.event_id,
                raw_event_type=claimed.event_type,
                reason="EVENT_LOG_ALREADY_TERMINAL",
            )
        result = self.consume_event_log_record(claimed)
        return result

    def consume_event_log_record(self, record: Any) -> EventProcessingResult:
        started = time.perf_counter()
        try:
            event = _event_from_record(record)
            result = self.order_consumer.dispatch(event, source_event_id=record.event_id or f"log:{record.id}")
        except Exception as exc:
            result = EventProcessingResult(
                status="FAILED",
                source_event_id=str(getattr(record, "event_id", "") or ""),
                raw_event_type=str(getattr(record, "event_type", "") or ""),
                retryable=_is_retryable_error(exc),
                error=str(exc),
            )
        self._finish_record(record, result)
        return self._record_result(result, processing_started=started)

    def dispatch(self, event: GatewayEvent, *, source_event_id: str = "") -> EventProcessingResult:
        return self.order_consumer.dispatch(event, source_event_id=source_event_id)

    def replay_pending(self, *, limit: int | None = None) -> dict[str, Any]:
        if not self.config.replay_enabled:
            return {"status": "DISABLED", "processed_count": 0}
        started = time.perf_counter()
        self.metrics["replay_status"] = "RUNNING"
        claimed = self.event_log.claim_pending_events(
            limit=int(limit or self.config.replay_batch_size),
            event_types=sorted(REPLAYABLE_GATEWAY_EVENT_TYPES),
            worker_id=self.worker_id,
            lease_sec=self.config.processing_lease_sec,
        )
        counts = {
            "claimed_count": len(claimed),
            "processed_count": 0,
            "duplicate_count": 0,
            "ignored_count": 0,
            "retry_count": 0,
            "failed_count": 0,
            "dead_letter_count": 0,
            "reconcile_required_count": 0,
        }
        for record in claimed:
            result = self.consume_event_log_record(record)
            if result.status in {"APPLIED", "PROCESSED"}:
                counts["processed_count"] += 1
            elif result.status == "DUPLICATE_ALREADY_APPLIED":
                counts["duplicate_count"] += 1
            elif result.status == "IGNORED":
                counts["ignored_count"] += 1
            elif result.status == "RETRY_WAIT":
                counts["retry_count"] += 1
            elif result.status == "DEAD_LETTER":
                counts["dead_letter_count"] += 1
            elif result.status == "FAILED":
                counts["failed_count"] += 1
            if result.reconcile_required:
                counts["reconcile_required_count"] += 1
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.metrics["replay_duration_ms"] = duration_ms
        self.metrics["replay_status"] = "OK"
        self.metrics["replayed_count"] = int(self.metrics.get("replayed_count") or 0) + len(claimed)
        return {
            "status": "OK",
            "replay_started_at": _now_text(),
            "claimed_count": len(claimed),
            **counts,
            "duration_ms": duration_ms,
            "order_lifecycle_ready": self.consumer_health().get("order_lifecycle_ready", False),
        }

    def recover_stale_claims(self) -> int:
        return self.event_log.recover_stale_claims()

    def consumer_health(self) -> dict[str, Any]:
        backlog = self.event_log.critical_backlog_snapshot()
        snapshot = self.event_log.event_log_snapshot()
        oldest_age = _age_sec(str(backlog.get("oldest_pending_at") or ""))
        warnings: list[str] = []
        if int(backlog.get("dead_letter_count") or 0) > 0:
            warnings.append("ORDER_EVENT_DEAD_LETTER_PRESENT")
        if int(backlog.get("pending_event_count") or 0) > 0:
            warnings.append("ORDER_EVENT_REPLAY_PENDING")
        if oldest_age > self.config.pending_max_age_sec:
            warnings.append("ORDER_EVENT_PENDING_AGE_EXCEEDED")
        ready = bool(backlog.get("order_lifecycle_ready")) and not warnings
        return EventConsumerHealthSnapshot(
            status="READY" if ready else "DEGRADED",
            consumer_enabled=self.config.order_enabled,
            consumer_running=self.running,
            order_lifecycle_ready=ready,
            pending_event_count=int(backlog.get("pending_event_count") or 0),
            retry_wait_count=int(backlog.get("retry_wait_count") or 0),
            failed_count=int(backlog.get("failed_count") or 0),
            dead_letter_count=int(backlog.get("dead_letter_count") or 0),
            oldest_pending_age_sec=oldest_age,
            processed_count=int(snapshot.get("processed_count") or 0),
            duplicate_applied_count=int(self.metrics.get("duplicate_count") or 0),
            unmatched_event_count=int(self.metrics.get("unmatched_count") or 0),
            reconcile_required_count=int(self.metrics.get("unmatched_count") or 0),
            last_event_type=str(self.metrics.get("last_event_type") or ""),
            last_event_at=str(self.metrics.get("last_event_at") or ""),
            last_processed_at=str(self.metrics.get("last_processed_at") or ""),
            last_error=str(self.metrics.get("last_error") or ""),
            replay_status=str(self.metrics.get("replay_status") or "IDLE"),
            replay_duration_ms=float(self.metrics.get("replay_duration_ms") or 0.0),
            warnings=tuple(warnings),
        ).to_dict()

    def _finish_record(self, record: Any, result: EventProcessingResult) -> None:
        if result.status in {"APPLIED", "DUPLICATE_ALREADY_APPLIED", "PROCESSED"}:
            self.event_log.mark_processing_result(
                record.id,
                status="PROCESSED",
                result=result.to_dict(),
                handler_name="OrderLifecycleEventConsumer",
                handler_version=self.config.handler_version,
            )
            return
        if result.status == "IGNORED":
            self.event_log.mark_ignored(record.id, reason=result.reason or "IGNORED")
            return
        if result.status == "RECONCILE_REQUIRED":
            self.event_log.mark_processing_result(
                record.id,
                status="PROCESSED",
                result=result.to_dict(),
                handler_name="OrderLifecycleEventConsumer",
                handler_version=self.config.handler_version,
            )
            return
        attempts = int(getattr(record, "processing_attempts", 0) or 0)
        if not result.retryable or attempts >= self.config.max_attempts:
            self.event_log.mark_dead_letter(record.id, error=result.error or result.reason or "EVENT_PROCESSING_FAILED")
            if self.config.dead_letter_blocks_buy:
                self._fail_closed("ORDER_EVENT_DEAD_LETTER_PRESENT", result.to_dict())
            return
        delay = min(self.config.retry_max_sec, self.config.retry_base_sec * (2 ** max(0, attempts - 1)))
        self.event_log.mark_retry_wait(
            record.id,
            error=result.error or result.reason or "EVENT_PROCESSING_RETRY",
            next_retry_at=_format_time(datetime.now(timezone.utc) + timedelta(seconds=delay)),
        )

    def _record_result(self, result: EventProcessingResult, *, processing_started: float) -> EventProcessingResult:
        duration_ms = (time.perf_counter() - processing_started) * 1000.0
        self.metrics["processing_duration_ms"] = duration_ms
        self.metrics["max_processing_duration_ms"] = max(float(self.metrics.get("max_processing_duration_ms") or 0.0), duration_ms)
        self.metrics["last_processed_at"] = _now_text()
        if result.status in {"APPLIED", "PROCESSED"}:
            self.metrics["processed_count"] = int(self.metrics.get("processed_count") or 0) + 1
            self.metrics["last_success_at"] = _now_text()
        elif result.status == "DUPLICATE_ALREADY_APPLIED":
            self.metrics["duplicate_count"] = int(self.metrics.get("duplicate_count") or 0) + 1
        elif result.status == "IGNORED":
            self.metrics["ignored_count"] = int(self.metrics.get("ignored_count") or 0) + 1
        elif result.status == "RETRY_WAIT":
            self.metrics["retry_count"] = int(self.metrics.get("retry_count") or 0) + 1
        elif result.status == "DEAD_LETTER":
            self.metrics["dead_letter_count"] = int(self.metrics.get("dead_letter_count") or 0) + 1
        elif result.status == "FAILED":
            self.metrics["failed_count"] = int(self.metrics.get("failed_count") or 0) + 1
            self.metrics["last_error"] = result.error or result.reason
        if result.reconcile_required:
            self.metrics["unmatched_count"] = int(self.metrics.get("unmatched_count") or 0) + 1
        return result

    def _fail_closed(self, reason: str, payload: dict[str, Any]) -> None:
        if not self.config.fail_closed:
            return
        db = TradingDatabase(self.order_consumer.db_path)
        try:
            db.save_order_kill_switch_state(
                {
                    "trade_date": datetime.now(timezone.utc).date().isoformat(),
                    "state": OrderKillSwitchState.STOP_NEW_BUY.value,
                    "reason_codes": [reason],
                    "details": payload,
                    "updated_at": _now_text(),
                }
            )
        finally:
            db.close()


class EventLogReplayWorker:
    def __init__(self, dispatcher: GatewayEventDispatcher) -> None:
        self.dispatcher = dispatcher

    def replay_pending(self, *, limit: int | None = None) -> dict[str, Any]:
        return self.dispatcher.replay_pending(limit=limit)

    def recover_stale_claims(self) -> int:
        return self.dispatcher.recover_stale_claims()

    def consumer_health(self) -> dict[str, Any]:
        return self.dispatcher.consumer_health()

    def start(self) -> None:
        self.dispatcher.start()

    def stop(self) -> None:
        self.dispatcher.stop()


def _event_from_record(record: Any) -> GatewayEvent:
    payload = json.loads(str(record.payload_json or "{}"))
    if isinstance(payload, dict) and "type" in payload:
        return GatewayEvent.from_dict(payload)
    return GatewayEvent(
        type=str(record.event_type or ""),
        event_id=str(record.event_id or ""),
        command_id=str(record.command_id or ""),
        source=str(record.source or ""),
        payload=payload if isinstance(payload, dict) else {"payload": payload},
        timestamp=str(record.received_at or ""),
    )


def _normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    order_result = data.get("order_result") if isinstance(data.get("order_result"), dict) else {}
    request = order_result.get("request") if isinstance(order_result.get("request"), dict) else {}
    for key in ("account", "code", "side", "quantity", "price", "idempotency_key"):
        if key not in data or data.get(key) in {None, ""}:
            data[key] = request.get(key, data.get(key, ""))
    data["order_no"] = _order_no(data)
    data["original_order_no"] = str(data.get("original_order_no") or data.get("orig_order_no") or "")
    data["execution_id"] = _execution_id(data)
    if data.get("side"):
        data["side"] = str(data.get("side") or "").upper()
    return data


def _canonical_dedupe_key(canonical_type: str, payload: dict[str, Any], source_event_id: str, event: GatewayEvent) -> str:
    if canonical_type in {"ORDER_PARTIALLY_FILLED", "ORDER_FILLED"}:
        if payload.get("broker_event_key"):
            return f"kiwoom-fill:{payload.get('broker_event_key')}"
        execution_id = str(_execution_id(payload) or "")
        if execution_id:
            return "fill:{account}:{order_no}:{execution_id}".format(
                account=str(payload.get("account") or ""),
                order_no=str(payload.get("order_no") or ""),
                execution_id=execution_id,
            )
    if canonical_type in {"ORDER_ACCEPTED", "ORDER_REJECTED", "ORDER_CANCEL_ACCEPTED", "ORDER_CANCELLED", "COMMAND_ACK", "COMMAND_FAILED"}:
        if payload.get("broker_event_key"):
            return f"kiwoom-order:{payload.get('broker_event_key')}"
        command_id = str(payload.get("command_id") or event.command_id or "")
        if command_id:
            return "order-command:{command_id}:{status}:{order_no}".format(
                command_id=command_id,
                status=str(payload.get("status") or canonical_type),
                order_no=str(payload.get("order_no") or ""),
            )
    if source_event_id:
        return f"source:{source_event_id}"
    return dedupe_key_for_gateway_event(event)


def _core_events_for(canonical: CanonicalGatewayEvent, result: dict[str, Any]) -> list[dict[str, Any]]:
    if canonical.canonical_type in {"ORDER_PARTIALLY_FILLED", "ORDER_FILLED"}:
        event_type = "ORDER_FILL_APPLIED"
    elif _result_reconcile_required(result):
        event_type = "ORDER_RECONCILE_REQUIRED"
    elif canonical.canonical_type in {"BALANCE_SNAPSHOT", "POSITION_SNAPSHOT"}:
        event_type = "POSITION_STATE_CHANGED"
    else:
        event_type = "ORDER_STATE_CHANGED"
    return [
        {
            "type": event_type,
            "source_event_id": canonical.source_event_id,
            "canonical_event_type": canonical.canonical_type,
            "payload": {"result": result, "event": canonical.payload},
            "occurred_at": _now_text(),
        }
    ]


def _result_reconcile_required(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "").upper()
    reason = str(result.get("reason") or "").upper()
    return (
        status == "RECONCILE_REQUIRED"
        or "RECONCILE_REQUIRED" in reason
        or bool(result.get("reconcile_required"))
        or result.get("matched") is False
    )


def _remaining_quantity(payload: dict[str, Any]) -> int:
    if "remaining_quantity" in payload:
        return max(0, _safe_int(payload.get("remaining_quantity"), 0))
    if "remaining_qty" in payload:
        return max(0, _safe_int(payload.get("remaining_qty"), 0))
    quantity = _safe_int(payload.get("quantity") or payload.get("order_qty"), 0)
    filled = _safe_int(payload.get("filled_quantity") or payload.get("filled_qty"), 0)
    if quantity > 0:
        return max(0, quantity - filled)
    return 0


def _order_no(payload: dict[str, Any]) -> str:
    order_result = payload.get("order_result") if isinstance(payload.get("order_result"), dict) else {}
    return str(payload.get("order_no") or payload.get("broker_order_id") or order_result.get("order_no") or "")


def _execution_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("execution_id")
        or payload.get("fill_id")
        or payload.get("chejan_id")
        or payload.get("execution_no")
        or payload.get("체결번호")
        or ""
    )


def _payload_checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("locked", "busy", "temporar", "timeout", "unavailable"))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _age_sec(timestamp: str) -> float:
    if not timestamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "CANONICAL_ORDER_EVENT_TYPES",
    "CanonicalGatewayEvent",
    "EventConsumerHealthSnapshot",
    "EventLogReplayWorker",
    "EventProcessingResult",
    "GatewayEventCodec",
    "GatewayEventConsumerConfig",
    "GatewayEventDispatcher",
    "OrderLifecycleEventConsumer",
]
