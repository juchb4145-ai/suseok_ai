from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

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
        self._commands: list[GatewayCommand] = []
        self._recent_events: list[GatewayEvent] = []
        self._seen_event_ids: set[str] = set()
        self._seen_command_keys: set[str] = set()
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

            self._recent_events.append(event)
            if len(self._recent_events) > self.max_recent_events:
                self._recent_events = self._recent_events[-self.max_recent_events :]
            return True

    def enqueue_command(self, command: GatewayCommand) -> bool:
        key = command.idempotency_key or command.command_id
        with self._lock:
            if key and key in self._seen_command_keys:
                return False
            if key:
                self._seen_command_keys.add(key)
            self._commands.append(command)
            self.status.pending_command_count = len(self._commands)
            return True

    def pop_commands(self, limit: int = 20) -> list[GatewayCommand]:
        with self._lock:
            count = max(0, min(int(limit), len(self._commands)))
            commands = self._commands[:count]
            self._commands = self._commands[count:]
            self.status.pending_command_count = len(self._commands)
            return list(commands)

    def snapshot(self) -> GatewayStatusSnapshot:
        with self._lock:
            snapshot = GatewayStatusSnapshot(**self.status.to_dict())
            snapshot.pending_command_count = len(self._commands)
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
