from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

from trading.broker.command_persistence import CommandStoreProtocol
from trading.broker.command_queue import ORDER_COMMAND_TYPES, CommandPriority, CommandQueue, CommandRecord, CommandStatus, EnqueueResult
from trading.broker.models import GatewayCommand, GatewayEvent, utc_timestamp


@dataclass
class GatewayStatusSnapshot:
    connection_state: str = "DISCONNECTED"
    connected: bool = False
    kiwoom_logged_in: bool = False
    orderable: bool = False
    mode: str = "OBSERVE"
    account: str = ""
    last_heartbeat_at: str = ""
    last_event_at: str = ""
    last_error: str = ""
    heartbeat_timeout_sec: int = 15
    heartbeat_age_sec: float | None = None
    heartbeat_ok: bool = False
    pending_command_count: int = 0
    received_event_count: int = 0
    deduped_event_count: int = 0
    reconnect_count: int = 0
    gateway_client_id: str = ""
    last_heartbeat_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class GatewayStateStore:
    heartbeat_timeout_sec: int = 15
    max_recent_events: int = 200
    command_store: CommandStoreProtocol | None = None
    expire_stale_dispatched_on_recovery: bool = True
    status: GatewayStatusSnapshot = field(default_factory=GatewayStatusSnapshot)

    def __post_init__(self) -> None:
        self.status.heartbeat_timeout_sec = self.heartbeat_timeout_sec
        self._lock = RLock()
        self._command_queue = CommandQueue()
        self._recent_events: list[GatewayEvent] = []
        self._seen_event_ids: set[str] = set()
        self._latest_ticks: dict[str, dict[str, Any]] = {}
        self.recovered_queued_count = 0
        self.recover_from_store()

    def recover_from_store(self, now: datetime | None = None) -> int:
        if self.command_store is None:
            return 0
        with self._lock:
            expire_old = getattr(self.command_store, "expire_old_records", None)
            if callable(expire_old):
                expire_old(now, include_dispatched=self.expire_stale_dispatched_on_recovery)
            recovered = self.command_store.load_recoverable_records(now=now)
            self._command_queue = CommandQueue()
            for record in recovered:
                self._command_queue.restore(record)
            self.recovered_queued_count = len(recovered)
            self.status.pending_command_count = len(recovered)
            return len(recovered)

    def record_event(self, event: GatewayEvent) -> bool:
        with self._lock:
            if event.event_id in self._seen_event_ids:
                self.status.deduped_event_count += 1
                return False
            self._seen_event_ids.add(event.event_id)
            self.status.received_event_count += 1
            self.status.connected = True
            self.status.connection_state = "CONNECTED"
            self.status.last_event_at = event.timestamp or utc_timestamp()
            self.status.gateway_client_id = event.source or self.status.gateway_client_id

            if event.type == "heartbeat":
                self._apply_heartbeat_status_locked(event)
            elif event.type == "login_status":
                self.status.kiwoom_logged_in = bool(event.payload.get("logged_in"))
                self.status.last_error = str(event.payload.get("message") or "")
            elif event.type == "orderability":
                self.status.orderable = bool(event.payload.get("orderable"))
                self.status.account = str(event.payload.get("account") or self.status.account)
                self.status.mode = str(event.payload.get("mode") or self.status.mode or "OBSERVE")
            elif event.type == "price_tick":
                code = str(event.payload.get("code") or "")
                if code:
                    self._latest_ticks[code] = dict(event.payload)
            elif event.type in {"gateway_error", "error"}:
                self.status.last_error = str(event.payload.get("message") or event.payload.get("error") or "")
            elif event.type == "rate_limited":
                self._command_queue.record_rate_limited()
                if self.command_store is not None:
                    self.command_store.record_rate_limited(
                        str(event.payload.get("command_id") or event.command_id or ""),
                        str(event.payload.get("command_type") or ""),
                        float(event.payload.get("wait_time_sec") or 0.0),
                        payload=dict(event.payload or {}),
                    )

            self._recent_events.append(event)
            if len(self._recent_events) > self.max_recent_events:
                self._recent_events = self._recent_events[-self.max_recent_events :]
            return True

    def record_heartbeat_hint(self, event: GatewayEvent) -> None:
        if event.type != "heartbeat":
            return
        with self._lock:
            self.status.connected = True
            self.status.connection_state = "CONNECTED"
            self.status.last_event_at = event.timestamp or utc_timestamp()
            self.status.gateway_client_id = event.source or self.status.gateway_client_id
            self._apply_heartbeat_status_locked(event)

    def _apply_heartbeat_status_locked(self, event: GatewayEvent) -> None:
        self.status.last_heartbeat_at = event.timestamp or utc_timestamp()
        self.status.last_heartbeat_payload = dict(event.payload or {})
        self.status.last_error = str(event.payload.get("last_error") or "")
        self.status.kiwoom_logged_in = bool(event.payload.get("kiwoom_logged_in", self.status.kiwoom_logged_in))
        self.status.orderable = bool(event.payload.get("orderable", self.status.orderable))
        self.status.account = str(event.payload.get("account") or self.status.account)
        self.status.mode = str(event.payload.get("mode") or self.status.mode or "OBSERVE")
        reconnect_count = event.payload.get("reconnect_count")
        if reconnect_count is not None:
            self.status.reconnect_count = int(reconnect_count or 0)

    def enqueue_command(
        self,
        command: GatewayCommand,
        priority: CommandPriority | str | None = None,
        ttl_sec: int | None = None,
        max_attempts: int | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> EnqueueResult:
        with self._lock:
            if self.command_store is not None:
                if not command.command_id:
                    return EnqueueResult(False, reason="COMMAND_ID_REQUIRED")
                record = CommandRecord.create(
                    command,
                    priority=priority,
                    ttl_sec=ttl_sec,
                    max_attempts=max_attempts,
                    now=now,
                    metadata=metadata,
                )
                enqueue_record = getattr(self.command_store, "enqueue_record", None)
                if callable(enqueue_record):
                    result = enqueue_record(record)
                else:
                    duplicate = self.command_store.has_active_or_retained_dedupe(record.dedupe_key, now=now)
                    if duplicate:
                        self.command_store.mark_duplicate_rejected(record.dedupe_key, record.command_id, "")
                        result = EnqueueResult(False, reason="DUPLICATE_COMMAND", record=record)
                    else:
                        self.command_store.upsert_record(record)
                        result = EnqueueResult(True, record=record)
                if result.accepted and result.record is not None:
                    self._command_queue.restore(result.record)
                self.status.pending_command_count = self.command_snapshot()["queued_count"]
                return result
            result = self._command_queue.enqueue(
                command,
                priority=priority,
                ttl_sec=ttl_sec,
                max_attempts=max_attempts,
                now=now,
                metadata=metadata,
            )
            self.status.pending_command_count = self._command_queue.snapshot()["queued_count"]
            return result

    def dispatch_commands(self, limit: int = 20, now: datetime | None = None) -> list[GatewayCommand]:
        with self._lock:
            if self.command_store is not None:
                expire_old = getattr(self.command_store, "expire_old_records", None)
                if callable(expire_old):
                    expire_old(now)
            commands = self._command_queue.dispatch(limit=limit, now=now)
            if self.command_store is not None:
                for command in commands:
                    record = self._command_queue.get(command.command_id)
                    if record is not None:
                        self.command_store.upsert_record(record)
                        self.command_store.append_event(
                            record.command_id,
                            "dispatch",
                            status_from=CommandStatus.QUEUED.value,
                            status_to=CommandStatus.DISPATCHED.value,
                            message="dispatched to gateway polling response",
                            payload=record.to_dict(),
                            created_at=record.dispatched_at,
                        )
            self.status.pending_command_count = self._command_queue.snapshot()["queued_count"]
            return list(commands)

    def pop_commands(self, limit: int = 20) -> list[GatewayCommand]:
        return self.dispatch_commands(limit=limit)

    def ack_command(
        self,
        command_id: str,
        status: str = "ACKED",
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        with self._lock:
            acknowledged = self._command_queue.ack(
                command_id,
                status=CommandStatus(status),
                result_payload=result_payload,
                error=error,
            )
            if self.command_store is not None:
                stored = self.command_store.update_record_status(
                    command_id,
                    status,
                    result_payload=result_payload,
                    error=error,
                    event_type="command_ack" if status != CommandStatus.DISPATCHED.value else "command_started",
                    message=error or str((result_payload or {}).get("message") or ""),
                )
                return acknowledged or stored
            return acknowledged

    def fail_command(self, command_id: str, error: str, retryable: bool = True) -> bool:
        with self._lock:
            failed = self._command_queue.fail(command_id, error, retryable=retryable)
            if self.command_store is not None:
                record = self._command_queue.get(command_id)
                if failed and record is not None:
                    self.command_store.upsert_record(record)
                    self.command_store.append_event(
                        command_id,
                        "command_failed",
                        status_to=record.status.value,
                        message=error,
                        payload={"retryable": retryable},
                    )
                    return True
                stored = self.command_store.update_record_status(
                    command_id,
                    CommandStatus.FAILED.value,
                    error=error,
                    event_type="command_failed",
                    message=error,
                )
                return failed or stored
            return failed

    def cancel_command(self, command_id: str) -> bool:
        with self._lock:
            cancelled = self._command_queue.cancel(command_id)
            if self.command_store is not None:
                record = self._command_queue.get(command_id)
                if cancelled and record is not None:
                    self.command_store.upsert_record(record)
                    self.command_store.append_event(
                        command_id,
                        "cancelled",
                        status_to=CommandStatus.CANCELLED.value,
                        message="cancelled before dispatch",
                    )
                    return True
                stored_record = self.command_store.get_record(command_id)
                if stored_record and stored_record.status == CommandStatus.QUEUED:
                    return self.command_store.update_record_status(
                        command_id,
                        CommandStatus.CANCELLED.value,
                        event_type="cancelled",
                        message="cancelled before dispatch",
                    )
                return False
            return cancelled

    def expire_old_commands(self, now: datetime | None = None) -> int:
        with self._lock:
            count = self._command_queue.expire_old(now)
            if self.command_store is not None:
                store_count = getattr(self.command_store, "expire_old_records", lambda _now=None: 0)(now)
                for record in self._command_queue.list(limit=10000, include_finished=True):
                    if record.status == CommandStatus.EXPIRED:
                        self.command_store.upsert_record(record)
                count = max(count, int(store_count or 0))
            self.status.pending_command_count = self._command_queue.snapshot()["queued_count"]
            return count

    def prune_commands(self, older_than_sec: int = 3600) -> int:
        with self._lock:
            if self.command_store is not None:
                removed = self.command_store.prune_finished(older_than_sec=older_than_sec)
                self.command_store.prune_dedupe_keys()
                return removed
            return self._command_queue.prune(older_than_sec=older_than_sec)

    def list_commands(
        self,
        status: str | None = None,
        limit: int = 100,
        include_finished: bool = False,
        command_type: str | None = None,
        trade_date: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self.command_store is not None:
                return [
                    record.to_dict()
                    for record in self.command_store.list_records(
                        status=status,
                        command_type=command_type,
                        limit=limit,
                        offset=offset,
                        include_finished=include_finished,
                        trade_date=trade_date,
                    )
                ]
            return [
                record.to_dict()
                for record in self._command_queue.list(status=status, limit=limit, include_finished=include_finished)
            ]

    def command_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self.command_store is not None:
                snapshot = self.command_store.snapshot()
                snapshot["recovered_queued_count"] = self.recovered_queued_count
                return snapshot
            snapshot = self._command_queue.snapshot()
            snapshot["stale_dispatched_count"] = snapshot.get("dispatched_count", 0)
            snapshot["recovered_queued_count"] = 0
            return snapshot

    def daily_order_command_count(
        self,
        *,
        trade_date: str,
        code: str,
        side: str,
        tag: str = "",
        order_type: int | None = None,
    ) -> int:
        with self._lock:
            if self.command_store is not None:
                counter = getattr(self.command_store, "count_order_commands", None)
                if callable(counter):
                    return int(
                        counter(
                            trade_date=trade_date,
                            code=code,
                            side=side,
                            tag=tag,
                            order_type=order_type,
                        )
                        or 0
                    )
            return _in_memory_order_command_count(
                self._command_queue.list(limit=10000, include_finished=True),
                trade_date=trade_date,
                code=code,
                side=side,
                tag=tag,
                order_type=order_type,
            )

    def has_duplicate(self, dedupe_key: str) -> bool:
        with self._lock:
            if self.command_store is not None:
                return self.command_store.has_active_or_retained_dedupe(dedupe_key)
            return self._command_queue.has_duplicate(dedupe_key)

    def duplicate_of(self, dedupe_key: str) -> str:
        with self._lock:
            if self.command_store is not None:
                finder = getattr(self.command_store, "find_active_or_retained_dedupe", None)
                if callable(finder):
                    row = finder(dedupe_key)
                    return str((row or {}).get("command_id") or "")
            return self._command_queue.duplicate_of(dedupe_key)

    def get_command(self, command_id: str) -> CommandRecord | None:
        with self._lock:
            if self.command_store is not None:
                return self.command_store.get_record(command_id)
            return self._command_queue.get(command_id)

    def command_events(self, command_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if self.command_store is None:
                return []
            return self.command_store.list_events(command_id, limit=limit)

    def append_command_event(
        self,
        command_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if self.command_store is not None:
                self.command_store.append_event(
                    command_id,
                    event_type,
                    status_from=status_from,
                    status_to=status_to,
                    message=message,
                    payload=payload,
                )

    def snapshot(self) -> GatewayStatusSnapshot:
        with self._lock:
            snapshot = GatewayStatusSnapshot(**self.status.to_dict())
            snapshot.pending_command_count = self._command_queue.snapshot()["queued_count"]
            snapshot.heartbeat_age_sec = _age_seconds(snapshot.last_heartbeat_at)
            snapshot.heartbeat_ok = (
                snapshot.connected
                and snapshot.heartbeat_age_sec is not None
                and snapshot.heartbeat_age_sec <= snapshot.heartbeat_timeout_sec
            )
            if snapshot.connected and not snapshot.heartbeat_ok:
                snapshot.connection_state = "STALE"
            return snapshot

    def recent_events(self, limit: int = 50) -> list[GatewayEvent]:
        with self._lock:
            return list(self._recent_events[-max(0, int(limit)) :])

    def latest_ticks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._latest_ticks.values())[-max(0, int(limit)) :]


def _age_seconds(timestamp: str) -> float | None:
    if not timestamp:
        return None
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())


def _in_memory_order_command_count(
    records: list[CommandRecord],
    *,
    trade_date: str,
    code: str,
    side: str,
    tag: str = "",
    order_type: int | None = None,
) -> int:
    count = 0
    for record in records:
        if record.command_type not in ORDER_COMMAND_TYPES:
            continue
        if _kst_trade_date(record.created_at) != trade_date:
            continue
        payload = dict(record.command.payload or {})
        if str(payload.get("code") or "") != str(code or ""):
            continue
        if str(payload.get("side") or "") != str(side or ""):
            continue
        if tag and str(payload.get("tag") or "") != str(tag):
            continue
        if order_type is not None:
            try:
                payload_order_type = int(payload.get("order_type") or 0)
            except (TypeError, ValueError):
                payload_order_type = -1
            if payload_order_type != int(order_type):
                continue
        count += 1
    return count


def _kst_trade_date(timestamp: str) -> str:
    text = str(timestamp or "")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=9))).date().isoformat()
