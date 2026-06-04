from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from trading.broker.command_queue import (
    FINISHED_STATUSES,
    ORDER_COMMAND_TYPES,
    CommandRecord,
    CommandStatus,
    EnqueueResult,
)


class CommandStoreProtocol(Protocol):
    def save_record(self, record: CommandRecord) -> None: ...

    def upsert_record(self, record: CommandRecord) -> None: ...

    def get_record(self, command_id: str) -> CommandRecord | None: ...

    def list_records(
        self,
        *,
        status: str | None = None,
        command_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_finished: bool = True,
        trade_date: str | None = None,
    ) -> list[CommandRecord]: ...

    def load_recoverable_records(self, now: datetime | None = None) -> list[CommandRecord]: ...

    def update_record_status(
        self,
        command_id: str,
        status: str,
        *,
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
        event_type: str = "status_update",
        message: str = "",
    ) -> bool: ...

    def append_event(
        self,
        command_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None: ...

    def snapshot(self, *, status: str | None = None, trade_date: str | None = None) -> dict[str, Any]: ...

    def count_order_commands(
        self,
        *,
        trade_date: str,
        code: str,
        side: str,
        tag: str = "",
        order_type: int | None = None,
    ) -> int: ...

    def has_active_or_retained_dedupe(self, dedupe_key: str, now: datetime | None = None) -> bool: ...

    def register_dedupe(self, record: CommandRecord, retention_sec: int | None = None) -> bool: ...

    def mark_duplicate_rejected(
        self,
        dedupe_key: str,
        command_id: str,
        duplicate_of: str,
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    def record_rate_limited(
        self,
        command_id: str,
        command_type: str,
        wait_time_sec: float,
        payload: dict[str, Any] | None = None,
    ) -> None: ...

    def prune_finished(self, older_than_sec: int) -> int: ...

    def prune_dedupe_keys(self, now: datetime | None = None) -> int: ...

    def list_events(self, command_id: str, limit: int = 100) -> list[dict[str, Any]]: ...


class SQLiteCommandStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        dedupe_retention_sec: int | None = None,
        history_retention_sec: int | None = None,
    ) -> None:
        self.path = Path(db_path).expanduser()
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        from storage.db import TradingDatabase

        bootstrap = TradingDatabase(str(self.path))
        bootstrap.close()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._lock = RLock()
        self.dedupe_retention_sec = int(
            dedupe_retention_sec
            if dedupe_retention_sec is not None
            else _int_env("TRADING_COMMAND_DEDUPE_RETENTION_SEC", 86400)
        )
        self.history_retention_sec = int(
            history_retention_sec
            if history_retention_sec is not None
            else _int_env("TRADING_COMMAND_HISTORY_RETENTION_SEC", 604800)
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def save_record(self, record: CommandRecord) -> None:
        self.upsert_record(record)

    def upsert_record(self, record: CommandRecord) -> None:
        with self._lock, self.conn:
            self._upsert_record_no_lock(record)
            self._upsert_dedupe_no_lock(record)

    def enqueue_record(self, record: CommandRecord) -> EnqueueResult:
        if not record.command_id:
            return EnqueueResult(False, reason="COMMAND_ID_REQUIRED")
        with self._lock, self.conn:
            duplicate = self.conn.execute(
                "SELECT command_id FROM gateway_commands WHERE command_id = ?",
                (record.command_id,),
            ).fetchone()
            if duplicate:
                self._append_event_no_lock(
                    record.command_id,
                    "duplicate_rejected",
                    status_to=CommandStatus.REJECTED.value,
                    message=f"duplicate of {duplicate['command_id']}",
                    payload={
                        "dedupe_key": record.dedupe_key,
                        "duplicate_of": str(duplicate["command_id"]),
                        "reason": "DUPLICATE_COMMAND_ID",
                    },
                )
                return EnqueueResult(False, reason="DUPLICATE_COMMAND_ID", duplicate_of=record.command_id, record=record)
            retained = self._retained_dedupe_no_lock(record.dedupe_key, _now())
            if retained and retained["command_id"] != record.command_id:
                duplicate_record = self.get_record(str(retained["command_id"]))
                self._append_event_no_lock(
                    record.command_id,
                    "duplicate_rejected",
                    status_to=CommandStatus.REJECTED.value,
                    message=f"duplicate of {retained['command_id']}",
                    payload={
                        "dedupe_key": record.dedupe_key,
                        "duplicate_of": str(retained["command_id"]),
                        "reason": "DUPLICATE_COMMAND",
                        "attempted_command": record.to_dict(),
                    },
                )
                return EnqueueResult(
                    False,
                    reason="DUPLICATE_COMMAND",
                    duplicate_of=str(retained["command_id"]),
                    record=duplicate_record,
                )
            self._upsert_record_no_lock(record)
            self._upsert_dedupe_no_lock(record)
            self._append_event_no_lock(
                record.command_id,
                "enqueue",
                status_from="",
                status_to=record.status.value,
                message="command enqueued",
                payload=record.to_dict(),
                created_at=record.created_at,
            )
            return EnqueueResult(True, record=record)

    def get_record(self, command_id: str) -> CommandRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gateway_commands WHERE command_id = ?",
                (str(command_id or ""),),
            ).fetchone()
            return _row_to_record(row) if row else None

    def list_records(
        self,
        *,
        status: str | None = None,
        command_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_finished: bool = True,
        trade_date: str | None = None,
    ) -> list[CommandRecord]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(str(status))
        if command_type:
            where.append("command_type = ?")
            params.append(str(command_type))
        if trade_date:
            where.append("trade_date = ?")
            params.append(str(trade_date))
        if not include_finished:
            placeholders = ",".join("?" for _ in FINISHED_STATUSES)
            where.append(f"status NOT IN ({placeholders})")
            params.extend(sorted(status.value for status in FINISHED_STATUSES))
        query = "SELECT * FROM gateway_commands"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC, updated_at DESC LIMIT ? OFFSET ?"
        params.extend([max(0, int(limit)), max(0, int(offset))])
        with self._lock:
            rows = self.conn.execute(query, tuple(params)).fetchall()
            return [_row_to_record(row) for row in rows]

    def load_recoverable_records(self, now: datetime | None = None) -> list[CommandRecord]:
        current = _format_time(_clean_time(now))
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM gateway_commands
                WHERE status = ?
                  AND (expires_at = '' OR expires_at > ?)
                ORDER BY created_at ASC
                """,
                (CommandStatus.QUEUED.value, current),
            ).fetchall()
            return [_row_to_record(row) for row in rows]

    def expire_old_records(self, now: datetime | None = None, *, include_dispatched: bool = True) -> int:
        current = _format_time(_clean_time(now))
        active = [CommandStatus.QUEUED.value]
        if include_dispatched:
            active.append(CommandStatus.DISPATCHED.value)
        with self._lock, self.conn:
            rows = self.conn.execute(
                f"""
                SELECT command_id, status FROM gateway_commands
                WHERE status IN ({','.join('?' for _ in active)})
                  AND expires_at != ''
                  AND expires_at <= ?
                """,
                (*active, current),
            ).fetchall()
            for row in rows:
                self._update_status_no_lock(
                    str(row["command_id"]),
                    CommandStatus.EXPIRED.value,
                    error="COMMAND_TTL_EXPIRED",
                    now=current,
                    event_type="expired",
                    message="command TTL expired",
                )
            return len(rows)

    def update_record_status(
        self,
        command_id: str,
        status: str,
        *,
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
        now: datetime | None = None,
        event_type: str = "status_update",
        message: str = "",
    ) -> bool:
        with self._lock, self.conn:
            return self._update_status_no_lock(
                command_id,
                status,
                result_payload=result_payload,
                error=error,
                now=_format_time(_clean_time(now)),
                event_type=event_type,
                message=message,
            )

    def append_event(
        self,
        command_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        with self._lock, self.conn:
            self._append_event_no_lock(
                command_id,
                event_type,
                status_from=status_from,
                status_to=status_to,
                message=message,
                payload=payload,
                created_at=created_at,
            )

    def snapshot(self, *, status: str | None = None, trade_date: str | None = None) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(str(status))
        if trade_date:
            where.append("trade_date = ?")
            params.append(str(trade_date))
        suffix = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT status, COUNT(*) AS count FROM gateway_commands{suffix} GROUP BY status",
                tuple(params),
            ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            total = sum(counts.values())
            last_command_at = self.conn.execute(
                f"SELECT MAX(created_at) AS value FROM gateway_commands{suffix}",
                tuple(params),
            ).fetchone()["value"]
            last_order_command_at = self.conn.execute(
                f"""
                SELECT MAX(created_at) AS value FROM gateway_commands{suffix}
                {'AND' if where else 'WHERE'} command_type IN ({','.join('?' for _ in ORDER_COMMAND_TYPES)})
                """,
                (*params, *sorted(ORDER_COMMAND_TYPES)),
            ).fetchone()["value"]
            duplicate_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM gateway_command_events WHERE event_type = 'duplicate_rejected'"
            ).fetchone()["count"]
            rate_limited_count = self.conn.execute(
                "SELECT COUNT(*) AS count FROM gateway_command_events WHERE event_type = 'rate_limited'"
            ).fetchone()["count"]
            return {
                "queued_count": counts.get(CommandStatus.QUEUED.value, 0),
                "dispatched_count": counts.get(CommandStatus.DISPATCHED.value, 0),
                "acked_count": counts.get(CommandStatus.ACKED.value, 0),
                "failed_count": counts.get(CommandStatus.FAILED.value, 0),
                "expired_count": counts.get(CommandStatus.EXPIRED.value, 0),
                "rejected_count": counts.get(CommandStatus.REJECTED.value, 0),
                "cancelled_count": counts.get(CommandStatus.CANCELLED.value, 0),
                "skipped_count": sum(
                    counts.get(status.value, 0)
                    for status in {
                        CommandStatus.SKIPPED_READY,
                        CommandStatus.SKIPPED_ORDER_PENDING,
                        CommandStatus.SKIPPED_GATEWAY_UNHEALTHY,
                        CommandStatus.SKIPPED_NON_BACKFILL_PENDING,
                        CommandStatus.SKIPPED_NOT_OBSERVE_MODE,
                    }
                ),
                "expired_before_dispatch_count": counts.get(CommandStatus.EXPIRED_BEFORE_DISPATCH.value, 0),
                "duplicate_rejected_count": int(duplicate_count or 0),
                "last_command_at": str(last_command_at or ""),
                "last_order_command_at": str(last_order_command_at or ""),
                "rate_limited_count": int(rate_limited_count or 0),
                "stale_dispatched_count": counts.get(CommandStatus.DISPATCHED.value, 0),
                "total_count": total,
            }

    def count_order_commands(
        self,
        *,
        trade_date: str,
        code: str,
        side: str,
        tag: str = "",
        order_type: int | None = None,
    ) -> int:
        normalized_code = str(code or "")
        normalized_side = str(side or "")
        normalized_tag = str(tag or "")
        normalized_order_type = int(order_type) if order_type is not None else None
        if not trade_date or not normalized_code or not normalized_side:
            return 0
        placeholders = ",".join("?" for _ in ORDER_COMMAND_TYPES)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT payload_json FROM gateway_commands
                WHERE trade_date = ?
                  AND command_type IN ({placeholders})
                """,
                (str(trade_date), *sorted(ORDER_COMMAND_TYPES)),
            ).fetchall()
        count = 0
        for row in rows:
            payload = _loads(row["payload_json"])
            if str(payload.get("code") or "") != normalized_code:
                continue
            if str(payload.get("side") or "") != normalized_side:
                continue
            if normalized_tag and str(payload.get("tag") or "") != normalized_tag:
                continue
            if normalized_order_type is not None and _safe_int(payload.get("order_type"), -1) != normalized_order_type:
                continue
            count += 1
        return count

    def has_active_or_retained_dedupe(self, dedupe_key: str, now: datetime | None = None) -> bool:
        with self._lock:
            return self._retained_dedupe_no_lock(str(dedupe_key or ""), _clean_time(now)) is not None

    def find_active_or_retained_dedupe(self, dedupe_key: str, now: datetime | None = None) -> dict[str, Any] | None:
        with self._lock:
            row = self._retained_dedupe_no_lock(str(dedupe_key or ""), _clean_time(now))
            return dict(row) if row else None

    def register_dedupe(self, record: CommandRecord, retention_sec: int | None = None) -> bool:
        with self._lock, self.conn:
            retained = self._retained_dedupe_no_lock(record.dedupe_key, _now())
            if retained and retained["command_id"] != record.command_id:
                return False
            self._upsert_dedupe_no_lock(record, retention_sec=retention_sec)
            return True

    def mark_duplicate_rejected(
        self,
        dedupe_key: str,
        command_id: str,
        duplicate_of: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.append_event(
            command_id,
            "duplicate_rejected",
            status_to=CommandStatus.REJECTED.value,
            message=f"duplicate of {duplicate_of}",
            payload={"dedupe_key": dedupe_key, "duplicate_of": duplicate_of, **dict(payload or {})},
        )

    def record_rate_limited(
        self,
        command_id: str,
        command_type: str,
        wait_time_sec: float,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.append_event(
            command_id,
            "rate_limited",
            message=f"{command_type} delayed {wait_time_sec:.3f}s",
            payload={"command_type": command_type, "wait_time_sec": wait_time_sec, **dict(payload or {})},
        )

    def prune_finished(self, older_than_sec: int) -> int:
        cutoff = _format_time(_now() - timedelta(seconds=max(0, int(older_than_sec))))
        finished = sorted(status.value for status in FINISHED_STATUSES)
        with self._lock, self.conn:
            rows = self.conn.execute(
                f"""
                SELECT command_id FROM gateway_commands
                WHERE status IN ({','.join('?' for _ in finished)})
                  AND finished_at != ''
                  AND finished_at <= ?
                """,
                (*finished, cutoff),
            ).fetchall()
            command_ids = [str(row["command_id"]) for row in rows]
            for command_id in command_ids:
                self.conn.execute("DELETE FROM gateway_commands WHERE command_id = ?", (command_id,))
                self.conn.execute(
                    "DELETE FROM gateway_command_events WHERE command_id = ? AND event_type != 'duplicate_rejected'",
                    (command_id,),
                )
            return len(command_ids)

    def prune_dedupe_keys(self, now: datetime | None = None) -> int:
        current = _format_time(_clean_time(now))
        with self._lock, self.conn:
            cursor = self.conn.execute(
                """
                DELETE FROM gateway_command_dedupe_keys
                WHERE expires_at != '' AND expires_at <= ?
                """,
                (current,),
            )
            return int(cursor.rowcount or 0)

    def list_events(self, command_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, command_id, event_type, status_from, status_to, message,
                       payload_json, created_at
                FROM gateway_command_events
                WHERE command_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (str(command_id or ""), max(0, int(limit))),
            ).fetchall()
            return [
                {
                    **{key: row[key] for key in row.keys() if key != "payload_json"},
                    "payload": _loads(row["payload_json"]),
                }
                for row in rows
            ]

    def _upsert_record_no_lock(self, record: CommandRecord) -> None:
        now = _format_time(_now())
        self.conn.execute(
            """
            INSERT INTO gateway_commands(
                command_id, request_id, command_type, status, priority, idempotency_key,
                dedupe_key, source, payload_json, command_json, metadata_json,
                result_payload_json, last_error, created_at, updated_at, dispatched_at,
                acked_at, finished_at, expires_at, attempts, max_attempts, trade_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                request_id=excluded.request_id,
                command_type=excluded.command_type,
                status=excluded.status,
                priority=excluded.priority,
                idempotency_key=excluded.idempotency_key,
                dedupe_key=excluded.dedupe_key,
                source=excluded.source,
                payload_json=excluded.payload_json,
                command_json=excluded.command_json,
                metadata_json=excluded.metadata_json,
                result_payload_json=excluded.result_payload_json,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at,
                dispatched_at=excluded.dispatched_at,
                acked_at=excluded.acked_at,
                finished_at=excluded.finished_at,
                expires_at=excluded.expires_at,
                attempts=excluded.attempts,
                max_attempts=excluded.max_attempts,
                trade_date=excluded.trade_date
            """,
            (
                record.command_id,
                record.command.request_id,
                record.command_type,
                record.status.value,
                record.priority.value,
                record.idempotency_key,
                record.dedupe_key,
                record.source,
                json.dumps(record.command.payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(record.command.to_dict(), ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(record.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(record.result_payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                record.last_error,
                record.created_at,
                now,
                record.dispatched_at,
                record.acked_at,
                record.finished_at,
                record.expires_at,
                record.attempts,
                record.max_attempts,
                _trade_date(record.created_at),
            ),
        )

    def _upsert_dedupe_no_lock(self, record: CommandRecord, retention_sec: int | None = None) -> None:
        if not record.dedupe_key:
            return
        now = _format_time(_now())
        expires_at = _dedupe_expires_at(record, self.dedupe_retention_sec if retention_sec is None else retention_sec)
        self.conn.execute(
            """
            INSERT INTO gateway_command_dedupe_keys(
                dedupe_key, command_id, command_type, idempotency_key, status,
                trade_date, created_at, updated_at, expires_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                command_id=excluded.command_id,
                command_type=excluded.command_type,
                idempotency_key=excluded.idempotency_key,
                status=excluded.status,
                trade_date=excluded.trade_date,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at,
                metadata_json=excluded.metadata_json
            """,
            (
                record.dedupe_key,
                record.command_id,
                record.command_type,
                record.idempotency_key,
                record.status.value,
                _trade_date(record.created_at),
                record.created_at,
                now,
                expires_at,
                json.dumps(record.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )

    def _retained_dedupe_no_lock(self, dedupe_key: str, now: datetime) -> sqlite3.Row | None:
        if not dedupe_key:
            return None
        current = _format_time(now)
        return self.conn.execute(
            """
            SELECT * FROM gateway_command_dedupe_keys
            WHERE dedupe_key = ?
              AND (expires_at = '' OR expires_at > ?)
            """,
            (dedupe_key, current),
        ).fetchone()

    def _update_status_no_lock(
        self,
        command_id: str,
        status: str,
        *,
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
        now: str | None = None,
        event_type: str = "status_update",
        message: str = "",
    ) -> bool:
        row = self.conn.execute(
            "SELECT * FROM gateway_commands WHERE command_id = ?",
            (str(command_id or ""),),
        ).fetchone()
        if row is None:
            self._append_event_no_lock(
                command_id,
                event_type,
                status_from="",
                status_to=str(status or ""),
                message=message or str(error or ""),
                payload={"missing_record": True, "result_payload": result_payload or {}},
                created_at=now,
            )
            return False
        current = now or _format_time(_now())
        status_from = str(row["status"] or "")
        status_to = str(status or status_from)
        finished_at = str(row["finished_at"] or "")
        acked_at = str(row["acked_at"] or "")
        dispatched_at = str(row["dispatched_at"] or "")
        attempts = int(row["attempts"] or 0)
        if status_to == CommandStatus.DISPATCHED.value:
            dispatched_at = dispatched_at or current
            attempts = max(1, attempts)
        if status_to == CommandStatus.ACKED.value:
            acked_at = current
        if status_to in {status.value for status in FINISHED_STATUSES}:
            finished_at = current
        merged_payload = result_payload if result_payload is not None else _loads(row["result_payload_json"])
        last_error = str(error or row["last_error"] or "")
        self.conn.execute(
            """
            UPDATE gateway_commands
            SET status = ?, result_payload_json = ?, last_error = ?, updated_at = ?,
                dispatched_at = ?, acked_at = ?, finished_at = ?, attempts = ?
            WHERE command_id = ?
            """,
            (
                status_to,
                json.dumps(merged_payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                last_error,
                current,
                dispatched_at,
                acked_at,
                finished_at,
                attempts,
                command_id,
            ),
        )
        self.conn.execute(
            "UPDATE gateway_command_dedupe_keys SET status = ?, updated_at = ? WHERE command_id = ?",
            (status_to, current, command_id),
        )
        self._append_event_no_lock(
            command_id,
            event_type,
            status_from=status_from,
            status_to=status_to,
            message=message or last_error,
            payload=result_payload or {},
            created_at=current,
        )
        return True

    def _append_event_no_lock(
        self,
        command_id: str,
        event_type: str,
        *,
        status_from: str = "",
        status_to: str = "",
        message: str = "",
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO gateway_command_events(
                command_id, event_type, status_from, status_to, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(command_id or ""),
                str(event_type or ""),
                str(status_from or ""),
                str(status_to or ""),
                str(message or ""),
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                created_at or _format_time(_now()),
            ),
        )


def _row_to_record(row: sqlite3.Row) -> CommandRecord:
    return CommandRecord.from_dict({key: row[key] for key in row.keys()})


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _dedupe_expires_at(record: CommandRecord, retention_sec: int) -> str:
    if record.command_type in ORDER_COMMAND_TYPES:
        return _format_time(_now() + timedelta(seconds=max(1, int(retention_sec))))
    if record.expires_at:
        return record.expires_at
    return _format_time(_now() + timedelta(seconds=max(1, int(retention_sec))))


def _trade_date(timestamp: str) -> str:
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


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _clean_time(value: datetime | None = None) -> datetime:
    if value is None:
        return _now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    return _clean_time(value).isoformat(timespec="seconds")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default
