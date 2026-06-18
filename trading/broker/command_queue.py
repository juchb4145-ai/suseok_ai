from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable

from trading.broker.models import GatewayCommand, utc_timestamp


class CommandStatus(str, Enum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    ACKED = "ACKED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SKIPPED_READY = "SKIPPED_READY"
    SKIPPED_ORDER_PENDING = "SKIPPED_ORDER_PENDING"
    SKIPPED_GATEWAY_UNHEALTHY = "SKIPPED_GATEWAY_UNHEALTHY"
    SKIPPED_NON_BACKFILL_PENDING = "SKIPPED_NON_BACKFILL_PENDING"
    SKIPPED_NOT_OBSERVE_MODE = "SKIPPED_NOT_OBSERVE_MODE"
    SKIPPED_REGULAR_SESSION = "SKIPPED_REGULAR_SESSION"
    EXPIRED_BEFORE_DISPATCH = "EXPIRED_BEFORE_DISPATCH"
    DUPLICATED_BUCKET = "DUPLICATED_BUCKET"


class CommandPriority(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


FINISHED_STATUSES = {
    CommandStatus.ACKED,
    CommandStatus.REJECTED,
    CommandStatus.FAILED,
    CommandStatus.EXPIRED,
    CommandStatus.CANCELLED,
    CommandStatus.SKIPPED_READY,
    CommandStatus.SKIPPED_ORDER_PENDING,
    CommandStatus.SKIPPED_GATEWAY_UNHEALTHY,
    CommandStatus.SKIPPED_NON_BACKFILL_PENDING,
    CommandStatus.SKIPPED_NOT_OBSERVE_MODE,
    CommandStatus.SKIPPED_REGULAR_SESSION,
    CommandStatus.EXPIRED_BEFORE_DISPATCH,
    CommandStatus.DUPLICATED_BUCKET,
}
ACTIVE_DEDUPE_STATUSES = {
    CommandStatus.QUEUED,
    CommandStatus.DISPATCHED,
    CommandStatus.ACKED,
}
ORDER_COMMAND_TYPES = {"send_order", "cancel_order", "modify_order"}
COMMAND_CLASS_RANK = {
    "ORDER": 0,
    "BROKER_RECONCILE": 1,
    "CONTROL": 2,
    "HYDRATION": 3,
    "BACKFILL": 4,
}
DEFAULT_TTL_SEC = {
    "send_order": 30,
    "cancel_order": 30,
    "modify_order": 30,
    "tr_request": 20,
    "send_condition": 60,
    "register_realtime": 60,
    "remove_realtime": 60,
    "remove_all_realtime": 60,
    "stop_condition": 60,
    "load_conditions": 120,
    "login": 120,
}
DEFAULT_MAX_ATTEMPTS = {
    "send_order": 1,
    "cancel_order": 1,
    "modify_order": 1,
    "tr_request": 2,
    "send_condition": 2,
    "register_realtime": 2,
    "remove_realtime": 2,
    "remove_all_realtime": 2,
    "stop_condition": 2,
    "load_conditions": 2,
    "login": 2,
}


@dataclass
class CommandRecord:
    command: GatewayCommand
    status: CommandStatus = CommandStatus.QUEUED
    priority: CommandPriority = CommandPriority.NORMAL
    created_at: str = field(default_factory=utc_timestamp)
    dispatched_at: str = ""
    acked_at: str = ""
    finished_at: str = ""
    expires_at: str = ""
    attempts: int = 0
    max_attempts: int = 1
    last_error: str = ""
    result_payload: dict[str, Any] = field(default_factory=dict)
    command_type: str = ""
    command_id: str = ""
    idempotency_key: str = ""
    dedupe_key: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        command: GatewayCommand,
        *,
        priority: CommandPriority | str | None = None,
        ttl_sec: int | None = None,
        max_attempts: int | None = None,
        now: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "CommandRecord":
        current = _clean_time(now)
        command_type = str(command.type or "")
        resolved_priority = CommandPriority(priority or default_priority(command_type))
        resolved_ttl = int(ttl_sec if ttl_sec is not None else DEFAULT_TTL_SEC.get(command_type, 60))
        resolved_attempts = int(max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS.get(command_type, 2))
        return cls(
            command=command,
            status=CommandStatus.QUEUED,
            priority=resolved_priority,
            created_at=_format_time(current),
            expires_at=_format_time(current + timedelta(seconds=max(1, resolved_ttl))),
            max_attempts=max(1, resolved_attempts),
            command_type=command_type,
            command_id=str(command.command_id or ""),
            idempotency_key=str(command.idempotency_key or ""),
            dedupe_key=dedupe_key_for_command(command),
            source=str(command.source or ""),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command.to_dict(),
            "status": self.status.value,
            "priority": self.priority.value,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
            "acked_at": self.acked_at,
            "finished_at": self.finished_at,
            "expires_at": self.expires_at,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "result_payload": dict(self.result_payload or {}),
            "command_type": self.command_type,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "dedupe_key": self.dedupe_key,
            "source": self.source,
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandRecord":
        raw_command = _loads_mapping(data.get("command") or data.get("command_json"))
        raw_payload = _loads_mapping(data.get("payload") or data.get("payload_json"))
        if not raw_command:
            raw_command = {
                "type": data.get("command_type") or data.get("type") or "",
                "command_id": data.get("command_id") or "",
                "request_id": data.get("request_id") or "",
                "source": data.get("source") or "core",
                "idempotency_key": data.get("idempotency_key") or "",
                "payload": raw_payload,
            }
        elif raw_payload and not raw_command.get("payload"):
            raw_command["payload"] = raw_payload
        command = GatewayCommand.from_dict(raw_command)
        command_type = str(data.get("command_type") or command.type or "")
        command_id = str(data.get("command_id") or command.command_id or "")
        idempotency_key = str(data.get("idempotency_key") or command.idempotency_key or "")
        return cls(
            command=command,
            status=_coerce_status(data.get("status")),
            priority=_coerce_priority(data.get("priority")),
            created_at=str(data.get("created_at") or command.timestamp or utc_timestamp()),
            dispatched_at=str(data.get("dispatched_at") or ""),
            acked_at=str(data.get("acked_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            attempts=_safe_int(data.get("attempts"), 0),
            max_attempts=max(1, _safe_int(data.get("max_attempts"), 1)),
            last_error=str(data.get("last_error") or ""),
            result_payload=_loads_mapping(data.get("result_payload") or data.get("result_payload_json")),
            command_type=command_type,
            command_id=command_id,
            idempotency_key=idempotency_key,
            dedupe_key=str(data.get("dedupe_key") or dedupe_key_for_command(command)),
            source=str(data.get("source") or command.source or ""),
            metadata=_loads_mapping(data.get("metadata") or data.get("metadata_json")),
        )

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATUSES


@dataclass(frozen=True)
class EnqueueResult:
    accepted: bool
    record: CommandRecord | None = None
    reason: str = ""
    duplicate_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
            "record": self.record.to_dict() if self.record else None,
        }


class CommandQueue:
    def __init__(self) -> None:
        self._records: dict[str, CommandRecord] = {}
        self._sequence: list[str] = []
        self.duplicate_rejected_count = 0
        self.rate_limited_count = 0
        self.last_command_at = ""
        self.last_order_command_at = ""

    def enqueue(
        self,
        command: GatewayCommand,
        *,
        priority: CommandPriority | str | None = None,
        ttl_sec: int | None = None,
        max_attempts: int | None = None,
        now: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EnqueueResult:
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
        duplicate = self._active_duplicate(record.dedupe_key)
        if duplicate is not None:
            self.duplicate_rejected_count += 1
            return EnqueueResult(
                False,
                reason="DUPLICATE_COMMAND",
                duplicate_of=duplicate.command_id,
                record=duplicate,
            )
        if record.command_id in self._records:
            self.duplicate_rejected_count += 1
            return EnqueueResult(False, reason="DUPLICATE_COMMAND_ID", duplicate_of=record.command_id, record=self._records[record.command_id])
        self._records[record.command_id] = record
        self._sequence.append(record.command_id)
        self.last_command_at = record.created_at
        if record.command_type in ORDER_COMMAND_TYPES:
            self.last_order_command_at = record.created_at
        return EnqueueResult(True, record=record)

    def restore(self, record: CommandRecord) -> None:
        if not record.command_id:
            return
        if record.command_id not in self._records:
            self._sequence.append(record.command_id)
        self._records[record.command_id] = record
        if not self.last_command_at or record.created_at > self.last_command_at:
            self.last_command_at = record.created_at
        if record.command_type in ORDER_COMMAND_TYPES and (
            not self.last_order_command_at or record.created_at > self.last_order_command_at
        ):
            self.last_order_command_at = record.created_at

    def dispatch(self, limit: int = 20, now: datetime | None = None) -> list[GatewayCommand]:
        current = _clean_time(now)
        self.expire_old(current)
        selected: list[CommandRecord] = []
        for record in self._dispatch_candidates():
            if len(selected) >= max(0, int(limit)):
                break
            if record.attempts >= record.max_attempts:
                self._finish(record, CommandStatus.FAILED, current, error="MAX_ATTEMPTS_EXCEEDED")
                continue
            record.status = CommandStatus.DISPATCHED
            record.dispatched_at = _format_time(current)
            record.attempts += 1
            selected.append(record)
        return [record.command for record in selected]

    def ack(
        self,
        command_id: str,
        *,
        status: CommandStatus | str = CommandStatus.ACKED,
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        record = self._records.get(str(command_id or ""))
        if record is None:
            return False
        target = CommandStatus(status)
        if target == CommandStatus.DISPATCHED:
            record.status = CommandStatus.DISPATCHED
            if not record.dispatched_at:
                record.dispatched_at = _format_time(_clean_time(now))
            record.result_payload = dict(result_payload or {})
            return True
        if target == CommandStatus.ACKED:
            record.acked_at = _format_time(_clean_time(now))
        self._finish(record, target, _clean_time(now), error=error or "", result_payload=result_payload or {})
        return True

    def fail(
        self,
        command_id: str,
        error: str,
        *,
        retryable: bool = True,
        now: datetime | None = None,
    ) -> bool:
        record = self._records.get(str(command_id or ""))
        if record is None:
            return False
        current = _clean_time(now)
        record.last_error = str(error or "")
        if retryable and record.attempts < record.max_attempts and not _is_expired(record, current):
            record.status = CommandStatus.QUEUED
            return True
        self._finish(record, CommandStatus.FAILED, current, error=error)
        return True

    def cancel(self, command_id: str, now: datetime | None = None) -> bool:
        record = self._records.get(str(command_id or ""))
        if record is None or record.status != CommandStatus.QUEUED:
            return False
        self._finish(record, CommandStatus.CANCELLED, _clean_time(now))
        return True

    def expire_old(self, now: datetime | None = None) -> int:
        current = _clean_time(now)
        expired = 0
        for record in self._records.values():
            if record.finished:
                continue
            if _is_expired(record, current):
                self._finish(record, CommandStatus.EXPIRED, current, error="COMMAND_TTL_EXPIRED")
                expired += 1
        return expired

    def prune(self, *, older_than_sec: int = 3600, now: datetime | None = None) -> int:
        current = _clean_time(now)
        kept: list[str] = []
        removed = 0
        for command_id in self._sequence:
            record = self._records.get(command_id)
            if record is None:
                continue
            if record.finished and record.finished_at:
                try:
                    finished_at = _parse_time(record.finished_at)
                except ValueError:
                    finished_at = current
                if current >= finished_at + timedelta(seconds=max(0, int(older_than_sec))):
                    self._records.pop(command_id, None)
                    removed += 1
                    continue
            kept.append(command_id)
        self._sequence = kept
        return removed

    def list(
        self,
        *,
        status: CommandStatus | str | None = None,
        limit: int = 100,
        include_finished: bool = False,
    ) -> list[CommandRecord]:
        target = CommandStatus(status) if status else None
        records: list[CommandRecord] = []
        for command_id in reversed(self._sequence):
            record = self._records.get(command_id)
            if record is None:
                continue
            if target is not None and record.status != target:
                continue
            if not include_finished and record.finished:
                continue
            records.append(record)
            if len(records) >= max(0, int(limit)):
                break
        return records

    def snapshot(self) -> dict[str, Any]:
        counts = Counter(record.status for record in self._records.values())
        return {
            "queued_count": counts.get(CommandStatus.QUEUED, 0),
            "dispatched_count": counts.get(CommandStatus.DISPATCHED, 0),
            "acked_count": counts.get(CommandStatus.ACKED, 0),
            "failed_count": counts.get(CommandStatus.FAILED, 0),
            "expired_count": counts.get(CommandStatus.EXPIRED, 0),
            "rejected_count": counts.get(CommandStatus.REJECTED, 0),
            "cancelled_count": counts.get(CommandStatus.CANCELLED, 0),
            "skipped_count": sum(
                counts.get(status, 0)
                for status in {
                    CommandStatus.SKIPPED_READY,
                    CommandStatus.SKIPPED_ORDER_PENDING,
                        CommandStatus.SKIPPED_GATEWAY_UNHEALTHY,
                        CommandStatus.SKIPPED_NON_BACKFILL_PENDING,
                        CommandStatus.SKIPPED_NOT_OBSERVE_MODE,
                    }
            ),
            "expired_before_dispatch_count": counts.get(CommandStatus.EXPIRED_BEFORE_DISPATCH, 0),
            "duplicate_rejected_count": self.duplicate_rejected_count,
            "last_command_at": self.last_command_at,
            "last_order_command_at": self.last_order_command_at,
            "rate_limited_count": self.rate_limited_count,
            "total_count": len(self._records),
        }

    def has_duplicate(self, dedupe_key: str) -> bool:
        return self._active_duplicate(dedupe_key) is not None

    def duplicate_of(self, dedupe_key: str) -> str:
        record = self._active_duplicate(dedupe_key)
        return record.command_id if record else ""

    def record_rate_limited(self) -> None:
        self.rate_limited_count += 1

    def get(self, command_id: str) -> CommandRecord | None:
        return self._records.get(str(command_id or ""))

    def _active_duplicate(self, dedupe_key: str) -> CommandRecord | None:
        if not dedupe_key:
            return None
        for record in self._records.values():
            if record.dedupe_key == dedupe_key and record.status in ACTIVE_DEDUPE_STATUSES:
                return record
        return None

    def _dispatch_candidates(self) -> Iterable[CommandRecord]:
        priority_rank = {
            CommandPriority.HIGH: 0,
            CommandPriority.NORMAL: 1,
            CommandPriority.LOW: 2,
        }
        candidates = [
            self._records[command_id]
            for command_id in self._sequence
            if self._records.get(command_id) is not None and self._records[command_id].status == CommandStatus.QUEUED
        ]
        return sorted(candidates, key=lambda record: (priority_rank[record.priority], command_class_rank(record), record.created_at))

    def _finish(
        self,
        record: CommandRecord,
        status: CommandStatus,
        now: datetime,
        *,
        error: str = "",
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        record.status = status
        record.finished_at = _format_time(now)
        if error:
            record.last_error = str(error)
        if result_payload is not None:
            record.result_payload = dict(result_payload)


def default_priority(command_type: str) -> CommandPriority:
    if command_type in ORDER_COMMAND_TYPES:
        return CommandPriority.HIGH
    if command_type in {"login", "load_conditions"}:
        return CommandPriority.LOW
    return CommandPriority.NORMAL


def command_class_rank(record: CommandRecord) -> int:
    command_class = str((record.metadata or {}).get("command_class") or dict(record.command.payload or {}).get("command_class") or "").upper()
    if not command_class:
        payload = dict(record.command.payload or {})
        purpose = str(payload.get("purpose") or "")
        if record.command_type in ORDER_COMMAND_TYPES:
            command_class = "ORDER"
        elif purpose == "broker_reconcile":
            command_class = "BROKER_RECONCILE"
        elif purpose in {"candidate_hydration"}:
            command_class = "HYDRATION"
        elif purpose:
            command_class = "BACKFILL"
        elif record.command_type in {"login", "load_conditions", "send_condition", "register_realtime", "remove_realtime", "remove_all_realtime", "stop_condition"}:
            command_class = "CONTROL"
    return COMMAND_CLASS_RANK.get(command_class, 3)


def dedupe_key_for_command(command: GatewayCommand) -> str:
    if command.idempotency_key:
        return str(command.idempotency_key)
    payload = dict(command.payload or {})
    command_type = str(command.type or "")
    if command_type == "send_order":
        return "order:{account}:{code}:{side}:{quantity}:{price}:{tag}:{strategy_order_id}".format(
            account=payload.get("account", ""),
            code=payload.get("code", ""),
            side=payload.get("side", ""),
            quantity=payload.get("quantity", ""),
            price=payload.get("price", ""),
            tag=payload.get("tag", ""),
            strategy_order_id=payload.get("strategy_order_id") or payload.get("candidate_id") or "",
        )
    if command_type == "cancel_order":
        return "cancel:{account}:{code}:{original_order_no}".format(
            account=payload.get("account", ""),
            code=payload.get("code", ""),
            original_order_no=payload.get("original_order_no", ""),
        )
    if command_type == "modify_order":
        return "modify:{account}:{code}:{original_order_no}:{quantity}:{price}".format(
            account=payload.get("account", ""),
            code=payload.get("code", ""),
            original_order_no=payload.get("original_order_no", ""),
            quantity=payload.get("quantity", ""),
            price=payload.get("price", ""),
        )
    if command_type == "tr_request":
        return "tr:{rq_name}:{tr_code}:{screen_no}:{request_id}".format(
            rq_name=payload.get("rq_name", ""),
            tr_code=payload.get("tr_code", ""),
            screen_no=payload.get("screen_no", ""),
            request_id=command.request_id or payload.get("request_id", ""),
        )
    raw = json.dumps({"type": command_type, "payload": payload}, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{command_type}:{digest}"


def _is_expired(record: CommandRecord, now: datetime) -> bool:
    try:
        return now >= _parse_time(record.expires_at)
    except ValueError:
        return False


def _clean_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    return _clean_time(value).isoformat(timespec="seconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _loads_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _coerce_status(value: Any) -> CommandStatus:
    try:
        return CommandStatus(str(value or CommandStatus.QUEUED.value))
    except ValueError:
        return CommandStatus.QUEUED


def _coerce_priority(value: Any) -> CommandPriority:
    try:
        return CommandPriority(str(value or CommandPriority.NORMAL.value))
    except ValueError:
        return CommandPriority.NORMAL


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
