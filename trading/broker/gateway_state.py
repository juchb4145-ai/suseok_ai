from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from trading.broker.command_queue import CommandPriority, CommandQueue, CommandRecord, CommandStatus, EnqueueResult
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

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class GatewayStateStore:
    heartbeat_timeout_sec: int = 15
    max_recent_events: int = 200
    status: GatewayStatusSnapshot = field(default_factory=GatewayStatusSnapshot)

    def __post_init__(self) -> None:
        self.status.heartbeat_timeout_sec = self.heartbeat_timeout_sec
        self._lock = Lock()
        self._command_queue = CommandQueue()
        self._recent_events: list[GatewayEvent] = []
        self._seen_event_ids: set[str] = set()
        self._latest_ticks: dict[str, dict[str, Any]] = {}

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
                self.status.last_heartbeat_at = event.timestamp or utc_timestamp()
                self.status.last_error = str(event.payload.get("last_error") or "")
                self.status.kiwoom_logged_in = bool(event.payload.get("kiwoom_logged_in", self.status.kiwoom_logged_in))
                self.status.orderable = bool(event.payload.get("orderable", self.status.orderable))
                self.status.account = str(event.payload.get("account") or self.status.account)
                self.status.mode = str(event.payload.get("mode") or self.status.mode or "OBSERVE")
                reconnect_count = event.payload.get("reconnect_count")
                if reconnect_count is not None:
                    self.status.reconnect_count = int(reconnect_count or 0)
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

            self._recent_events.append(event)
            if len(self._recent_events) > self.max_recent_events:
                self._recent_events = self._recent_events[-self.max_recent_events :]
            return True

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
            commands = self._command_queue.dispatch(limit=limit, now=now)
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
            return self._command_queue.ack(
                command_id,
                status=CommandStatus(status),
                result_payload=result_payload,
                error=error,
            )

    def fail_command(self, command_id: str, error: str, retryable: bool = True) -> bool:
        with self._lock:
            return self._command_queue.fail(command_id, error, retryable=retryable)

    def cancel_command(self, command_id: str) -> bool:
        with self._lock:
            return self._command_queue.cancel(command_id)

    def expire_old_commands(self, now: datetime | None = None) -> int:
        with self._lock:
            count = self._command_queue.expire_old(now)
            self.status.pending_command_count = self._command_queue.snapshot()["queued_count"]
            return count

    def prune_commands(self, older_than_sec: int = 3600) -> int:
        with self._lock:
            return self._command_queue.prune(older_than_sec=older_than_sec)

    def list_commands(
        self,
        status: str | None = None,
        limit: int = 100,
        include_finished: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [
                record.to_dict()
                for record in self._command_queue.list(status=status, limit=limit, include_finished=include_finished)
            ]

    def command_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._command_queue.snapshot()

    def has_duplicate(self, dedupe_key: str) -> bool:
        with self._lock:
            return self._command_queue.has_duplicate(dedupe_key)

    def get_command(self, command_id: str) -> CommandRecord | None:
        with self._lock:
            return self._command_queue.get(command_id)

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
