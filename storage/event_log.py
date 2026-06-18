from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Sequence

from trading.broker.models import GatewayEvent
from trading.runtime_ports import EventLogAppendResult, EventLogRecord


@dataclass(frozen=True)
class EventLogConfig:
    enabled: bool = True
    price_tick_enabled: bool = False
    heartbeat_enabled: bool = False
    max_pending_replay: int = 500
    processing_lease_sec: int = 30
    max_attempts: int = 5

    @classmethod
    def from_env(cls) -> "EventLogConfig":
        return cls(
            enabled=_env_bool("TRADING_EVENT_LOG_ENABLED", True),
            price_tick_enabled=_env_bool("TRADING_EVENT_LOG_PRICE_TICK_ENABLED", False),
            heartbeat_enabled=_env_bool("TRADING_EVENT_LOG_HEARTBEAT_ENABLED", False),
            max_pending_replay=max(1, _env_int("TRADING_EVENT_LOG_MAX_PENDING_REPLAY", 500)),
            processing_lease_sec=max(1, _env_int("TRADING_EVENT_PROCESSING_LEASE_SEC", 30)),
            max_attempts=max(1, _env_int("TRADING_EVENT_MAX_ATTEMPTS", 5)),
        )


class EventLogRepository:
    def __init__(
        self,
        db_path: str | Path,
        *,
        config: EventLogConfig | None = None,
    ) -> None:
        self.path = Path(db_path).expanduser()
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or EventLogConfig.from_env()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._lock = RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def append_gateway_event(self, event: GatewayEvent, *, dedupe_key: str = "") -> EventLogAppendResult:
        if not self.config.enabled:
            return EventLogAppendResult(False, ignored=True, reason="EVENT_LOG_DISABLED")
        if event.type == "price_tick" and not self.config.price_tick_enabled:
            return EventLogAppendResult(False, ignored=True, reason="PRICE_TICK_LOGGING_DISABLED")
        if event.type == "heartbeat" and not self.config.heartbeat_enabled:
            return EventLogAppendResult(False, ignored=True, reason="HEARTBEAT_LOGGING_DISABLED")

        key = str(dedupe_key or dedupe_key_for_gateway_event(event) or "").strip()
        if not key:
            key = str(event.event_id or "")
        payload_json, serialization_error = _event_payload_json(event)
        status = "FAILED" if serialization_error else "PENDING"
        error = serialization_error
        received_at = str(event.timestamp or _format_time(_now()))
        code = _event_code(event)
        trade_date = _trade_date(received_at)

        with self._lock, self.conn:
            duplicate = self.find_by_dedupe_key(key)
            if duplicate is not None:
                return EventLogAppendResult(
                    False,
                    record=duplicate,
                    duplicate=True,
                    reason="DUPLICATE_DEDUPE_KEY",
                )
            try:
                cursor = self.conn.execute(
                    """
                    INSERT INTO gateway_event_log(
                        event_id, event_type, dedupe_key, source, command_id, code,
                        trade_date, payload_json, received_at, processed_at,
                        processing_status, error, created_at, processing_result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, '{}')
                    """,
                    (
                        str(event.event_id or ""),
                        str(event.type or ""),
                        key,
                        str(event.source or ""),
                        str(event.command_id or ""),
                        code,
                        trade_date,
                        payload_json,
                        received_at,
                        status,
                        error,
                        _format_time(_now()),
                    ),
                )
            except sqlite3.IntegrityError:
                duplicate = self.find_by_dedupe_key(key)
                return EventLogAppendResult(
                    False,
                    record=duplicate,
                    duplicate=True,
                    reason="DUPLICATE_DEDUPE_KEY",
                )
            record = self._get_by_id_no_lock(int(cursor.lastrowid))
            return EventLogAppendResult(
                appended=record is not None,
                record=record,
                warning=serialization_error,
                reason=status,
            )

    def pending_gateway_events(self, *, limit: int = 100, event_type: str | None = None) -> list[EventLogRecord]:
        resolved_limit = min(max(0, int(limit)), self.config.max_pending_replay)
        where = ["processing_status = ?"]
        params: list[Any] = ["PENDING"]
        if event_type:
            where.append("event_type = ?")
            params.append(str(event_type))
        query = (
            "SELECT * FROM gateway_event_log WHERE "
            + " AND ".join(where)
            + " ORDER BY received_at ASC, id ASC LIMIT ?"
        )
        params.append(resolved_limit)
        with self._lock:
            rows = self.conn.execute(query, tuple(params)).fetchall()
            return [_row_to_record(row) for row in rows]

    def claim_pending_events(
        self,
        *,
        limit: int,
        event_types: Sequence[str] | None = None,
        worker_id: str = "",
        lease_sec: int | None = None,
        now: datetime | str | None = None,
    ) -> list[EventLogRecord]:
        resolved_limit = min(max(0, int(limit)), self.config.max_pending_replay)
        if resolved_limit <= 0:
            return []
        current = _coerce_time(now)
        current_text = _format_time(current)
        event_type_filter = [str(item) for item in (event_types or ()) if str(item or "")]
        statuses = ("PENDING", "RETRY_WAIT")
        where = ["processing_status IN (?, ?)"]
        params: list[Any] = list(statuses)
        if event_type_filter:
            placeholders = ",".join("?" for _ in event_type_filter)
            where.append(f"event_type IN ({placeholders})")
            params.extend(event_type_filter)
        where.append("(processing_status != 'RETRY_WAIT' OR next_retry_at = '' OR next_retry_at <= ?)")
        params.append(current_text)
        query = (
            "SELECT id FROM gateway_event_log WHERE "
            + " AND ".join(where)
            + " ORDER BY received_at ASC, id ASC LIMIT ?"
        )
        params.append(resolved_limit)
        claimed: list[EventLogRecord] = []
        with self._lock, self.conn:
            rows = self.conn.execute(query, tuple(params)).fetchall()
            lease_until = _format_time(current + timedelta(seconds=max(1, int(lease_sec or self.config.processing_lease_sec))))
            for row in rows:
                row_id = int(row["id"])
                cursor = self.conn.execute(
                    """
                    UPDATE gateway_event_log
                    SET processing_status = 'PROCESSING',
                        processing_attempts = processing_attempts + 1,
                        claimed_at = ?,
                        claimed_by = ?,
                        next_retry_at = ?,
                        last_attempt_at = ?
                    WHERE id = ?
                      AND (
                          processing_status = 'PENDING'
                          OR (processing_status = 'RETRY_WAIT' AND (next_retry_at = '' OR next_retry_at <= ?))
                      )
                    """,
                    (current_text, str(worker_id or ""), lease_until, current_text, row_id, current_text),
                )
                if cursor.rowcount:
                    record = self._get_by_id_no_lock(row_id)
                    if record is not None:
                        claimed.append(record)
        return claimed

    def claim_event(
        self,
        event_log_id: int | str,
        *,
        worker_id: str = "",
        lease_sec: int | None = None,
        now: datetime | str | None = None,
    ) -> EventLogRecord | None:
        current = _coerce_time(now)
        current_text = _format_time(current)
        lease_until = _format_time(current + timedelta(seconds=max(1, int(lease_sec or self.config.processing_lease_sec))))
        with self._lock, self.conn:
            row = self._get_row_by_identifier_no_lock(event_log_id)
            if row is None:
                return None
            status = str(row["processing_status"] or "")
            if status in {"PROCESSED", "IGNORED", "DEAD_LETTER"}:
                return _row_to_record(row)
            cursor = self.conn.execute(
                """
                UPDATE gateway_event_log
                SET processing_status = 'PROCESSING',
                    processing_attempts = processing_attempts + 1,
                    claimed_at = ?,
                    claimed_by = ?,
                    next_retry_at = ?,
                    last_attempt_at = ?
                WHERE id = ?
                  AND processing_status NOT IN ('PROCESSED', 'IGNORED', 'DEAD_LETTER')
                """,
                (current_text, str(worker_id or ""), lease_until, current_text, int(row["id"])),
            )
            if not cursor.rowcount:
                return None
            return self._get_by_id_no_lock(int(row["id"]))

    def mark_processed(
        self,
        event_log_id: int | str,
        *,
        processed_at: str | None = None,
        core_events: Any = (),
    ) -> None:
        current = str(processed_at or _format_time(_now()))
        with self._lock, self.conn:
            result = {"status": "PROCESSED", "core_events": core_events}
            self._update_status_no_lock(
                event_log_id,
                "PROCESSED",
                processed_at=current,
                error="",
                processing_result_json=_json_payload(result),
            )

    def mark_failed(self, event_log_id: int | str, *, error: str) -> None:
        with self._lock, self.conn:
            self._update_status_no_lock(event_log_id, "FAILED", processed_at="", error=str(error or ""))

    def mark_processing_result(
        self,
        event_log_id: int | str,
        *,
        status: str = "PROCESSED",
        result: dict[str, Any] | None = None,
        processed_at: str | None = None,
        handler_name: str = "",
        handler_version: str = "",
    ) -> None:
        resolved = str(status or "PROCESSED").upper()
        current = str(processed_at or _format_time(_now()))
        with self._lock, self.conn:
            self._update_status_no_lock(
                event_log_id,
                resolved,
                processed_at=current if resolved in {"PROCESSED", "IGNORED"} else "",
                error="" if resolved in {"PROCESSED", "IGNORED"} else str((result or {}).get("error") or ""),
                processing_result_json=_json_payload(result or {"status": resolved}),
                handler_name=handler_name,
                handler_version=handler_version,
            )

    def mark_retry_wait(self, event_log_id: int | str, *, error: str, next_retry_at: str) -> None:
        with self._lock, self.conn:
            self._update_status_no_lock(
                event_log_id,
                "RETRY_WAIT",
                processed_at="",
                error=str(error or ""),
                next_retry_at=str(next_retry_at or ""),
            )

    def mark_dead_letter(self, event_log_id: int | str, *, error: str) -> None:
        current = _format_time(_now())
        with self._lock, self.conn:
            self._update_status_no_lock(
                event_log_id,
                "DEAD_LETTER",
                processed_at="",
                error=str(error or ""),
                dead_lettered_at=current,
            )

    def mark_ignored(self, event_log_id: int | str, *, reason: str) -> None:
        current = _format_time(_now())
        with self._lock, self.conn:
            self._update_status_no_lock(
                event_log_id,
                "IGNORED",
                processed_at=current,
                error="",
                processing_result_json=_json_payload({"status": "IGNORED", "reason": str(reason or "")}),
            )

    def find_by_dedupe_key(self, dedupe_key: str) -> EventLogRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gateway_event_log WHERE dedupe_key = ?",
                (str(dedupe_key or ""),),
            ).fetchone()
            return _row_to_record(row) if row else None

    def get_event(self, event_log_id: int | str) -> EventLogRecord | None:
        with self._lock:
            row = self._get_row_by_identifier_no_lock(event_log_id)
            return _row_to_record(row) if row else None

    def get_by_event_id(self, event_id: str) -> EventLogRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gateway_event_log WHERE event_id = ? ORDER BY id DESC LIMIT 1",
                (str(event_id or ""),),
            ).fetchone()
            return _row_to_record(row) if row else None

    def recover_stale_claims(self, *, now: datetime | str | None = None) -> int:
        current_text = _format_time(_coerce_time(now))
        with self._lock, self.conn:
            cursor = self.conn.execute(
                """
                UPDATE gateway_event_log
                SET processing_status = 'PENDING',
                    claimed_at = '',
                    claimed_by = '',
                    next_retry_at = '',
                    processing_result_json = ?
                WHERE processing_status = 'PROCESSING'
                  AND next_retry_at != ''
                  AND next_retry_at <= ?
                """,
                (_json_payload({"status": "PENDING", "recovered_from_stale_claim_at": current_text}), current_text),
            )
            return int(cursor.rowcount or 0)

    def critical_backlog_snapshot(self) -> dict[str, Any]:
        critical_types = tuple(sorted(REPLAYABLE_GATEWAY_EVENT_TYPES))
        placeholders = ",".join("?" for _ in critical_types)
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT processing_status, COUNT(*) AS count
                FROM gateway_event_log
                WHERE event_type IN ({placeholders})
                GROUP BY processing_status
                """,
                critical_types,
            ).fetchall()
            oldest = self.conn.execute(
                f"""
                SELECT MIN(received_at) AS value
                FROM gateway_event_log
                WHERE event_type IN ({placeholders})
                  AND processing_status IN ('PENDING', 'PROCESSING', 'RETRY_WAIT')
                """,
                critical_types,
            ).fetchone()["value"]
            counts = {str(row["processing_status"]): int(row["count"]) for row in rows}
            return {
                "critical_event_types": list(critical_types),
                "pending_event_count": counts.get("PENDING", 0),
                "processing_count": counts.get("PROCESSING", 0),
                "retry_wait_count": counts.get("RETRY_WAIT", 0),
                "failed_count": counts.get("FAILED", 0),
                "dead_letter_count": counts.get("DEAD_LETTER", 0),
                "ignored_count": counts.get("IGNORED", 0),
                "processed_count": counts.get("PROCESSED", 0),
                "oldest_pending_at": str(oldest or ""),
                "order_lifecycle_ready": (
                    counts.get("PENDING", 0) == 0
                    and counts.get("PROCESSING", 0) == 0
                    and counts.get("RETRY_WAIT", 0) == 0
                    and counts.get("DEAD_LETTER", 0) == 0
                ),
            }

    def event_log_snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT processing_status, COUNT(*) AS count FROM gateway_event_log GROUP BY processing_status"
            ).fetchall()
            type_rows = self.conn.execute(
                "SELECT event_type, COUNT(*) AS count FROM gateway_event_log GROUP BY event_type"
            ).fetchall()
            last_received = self.conn.execute(
                "SELECT MAX(received_at) AS value FROM gateway_event_log"
            ).fetchone()["value"]
            counts = {str(row["processing_status"]): int(row["count"]) for row in rows}
            critical = self.critical_backlog_snapshot()
            return {
                "enabled": self.config.enabled,
                "price_tick_enabled": self.config.price_tick_enabled,
                "heartbeat_enabled": self.config.heartbeat_enabled,
                "max_pending_replay": self.config.max_pending_replay,
                "pending_count": counts.get("PENDING", 0),
                "processed_count": counts.get("PROCESSED", 0),
                "failed_count": counts.get("FAILED", 0),
                "processing_count": counts.get("PROCESSING", 0),
                "retry_wait_count": counts.get("RETRY_WAIT", 0),
                "dead_letter_count": counts.get("DEAD_LETTER", 0),
                "ignored_count": counts.get("IGNORED", 0),
                "total_count": sum(counts.values()),
                "event_type_counts": {str(row["event_type"]): int(row["count"]) for row in type_rows},
                "last_received_at": str(last_received or ""),
                "critical_backlog": critical,
            }

    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gateway_event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    command_id TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    received_at TEXT NOT NULL,
                    processed_at TEXT NOT NULL DEFAULT '',
                    processing_status TEXT NOT NULL DEFAULT 'PENDING',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processing_attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_at TEXT NOT NULL DEFAULT '',
                    claimed_by TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    handler_name TEXT NOT NULL DEFAULT '',
                    handler_version TEXT NOT NULL DEFAULT '',
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    dead_lettered_at TEXT NOT NULL DEFAULT '',
                    processing_result_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_event_type
                    ON gateway_event_log(event_type);
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_processing_status
                    ON gateway_event_log(processing_status);
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_received_at
                    ON gateway_event_log(received_at);
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_trade_date_code
                    ON gateway_event_log(trade_date, code);
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_command_id
                    ON gateway_event_log(command_id);
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_event_id
                    ON gateway_event_log(event_id);
                """
            )
            for column, ddl in (
                ("processing_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("claimed_at", "TEXT NOT NULL DEFAULT ''"),
                ("claimed_by", "TEXT NOT NULL DEFAULT ''"),
                ("next_retry_at", "TEXT NOT NULL DEFAULT ''"),
                ("handler_name", "TEXT NOT NULL DEFAULT ''"),
                ("handler_version", "TEXT NOT NULL DEFAULT ''"),
                ("last_attempt_at", "TEXT NOT NULL DEFAULT ''"),
                ("dead_lettered_at", "TEXT NOT NULL DEFAULT ''"),
                ("processing_result_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                _ensure_column(self.conn, "gateway_event_log", column, ddl)
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gateway_event_log_claim
                    ON gateway_event_log(processing_status, next_retry_at, received_at, id)
                """
            )

    def _get_by_id_no_lock(self, row_id: int) -> EventLogRecord | None:
        row = self.conn.execute(
            "SELECT * FROM gateway_event_log WHERE id = ?",
            (int(row_id),),
        ).fetchone()
        return _row_to_record(row) if row else None

    def _get_row_by_identifier_no_lock(self, event_log_id: int | str) -> sqlite3.Row | None:
        if isinstance(event_log_id, int) or str(event_log_id).isdigit():
            return self.conn.execute(
                "SELECT * FROM gateway_event_log WHERE id = ?",
                (int(event_log_id),),
            ).fetchone()
        return self.conn.execute(
            "SELECT * FROM gateway_event_log WHERE event_id = ? ORDER BY id DESC LIMIT 1",
            (str(event_log_id or ""),),
        ).fetchone()

    def _update_status_no_lock(
        self,
        event_log_id: int | str,
        status: str,
        *,
        processed_at: str,
        error: str,
        processing_result_json: str | None = None,
        next_retry_at: str | None = None,
        dead_lettered_at: str | None = None,
        handler_name: str | None = None,
        handler_version: str | None = None,
    ) -> None:
        updates = [
            "processing_status = ?",
            "processed_at = ?",
            "error = ?",
            "claimed_at = ''",
            "claimed_by = ''",
        ]
        params: list[Any] = [status, processed_at, error]
        if processing_result_json is not None:
            updates.append("processing_result_json = ?")
            params.append(processing_result_json)
        if next_retry_at is not None:
            updates.append("next_retry_at = ?")
            params.append(next_retry_at)
        elif status not in {"PROCESSING", "RETRY_WAIT"}:
            updates.append("next_retry_at = ''")
        if dead_lettered_at is not None:
            updates.append("dead_lettered_at = ?")
            params.append(dead_lettered_at)
        if handler_name is not None:
            updates.append("handler_name = ?")
            params.append(handler_name)
        if handler_version is not None:
            updates.append("handler_version = ?")
            params.append(handler_version)
        if isinstance(event_log_id, int) or str(event_log_id).isdigit():
            params.append(int(event_log_id))
            self.conn.execute(
                f"UPDATE gateway_event_log SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
            return
        params.append(str(event_log_id or ""))
        self.conn.execute(
            f"UPDATE gateway_event_log SET {', '.join(updates)} WHERE event_id = ?",
            tuple(params),
        )


REPLAYABLE_GATEWAY_EVENT_TYPES = {
    "command_ack",
    "command_failed",
    "command_timeout",
    "command_expired",
    "order_ack",
    "order_reject",
    "order_fill",
    "execution",
    "execution_event",
    "fill",
    "cancel_ack",
    "order_cancel",
    "order_cancelled",
    "order_status_snapshot",
    "balance_snapshot",
    "position_snapshot",
    "kiwoom_order_chejan",
    "kiwoom_balance_chejan",
    "kiwoom_special_chejan",
}


def dedupe_key_for_gateway_event(event: GatewayEvent) -> str:
    if event.event_id:
        return f"event:{event.event_id}"
    payload = dict(event.payload or {})
    event_type = str(event.type or "")
    command_id = str(event.command_id or payload.get("command_id") or "")
    if event_type == "command_ack":
        return "command_ack:{command_id}:{status}".format(
            command_id=command_id,
            status=str(payload.get("status") or payload.get("command_status") or ""),
        )
    if event_type in {"kiwoom_order_chejan", "kiwoom_balance_chejan"} and payload.get("broker_event_key"):
        return f"kiwoom-chejan:{payload.get('broker_event_key')}"
    if event_type in {"order_ack", "order_fill", "execution", "execution_event", "fill"}:
        return "order:{account}:{order_no}:{execution_id}".format(
            account=str(payload.get("account") or ""),
            order_no=str(payload.get("order_no") or payload.get("original_order_no") or ""),
            execution_id=str(payload.get("execution_id") or payload.get("fill_id") or payload.get("chejan_id") or ""),
        )
    if event_type in {"condition_event", "condition_include", "condition_remove"}:
        return "condition:{name}:{index}:{code}:{bucket}".format(
            name=str(payload.get("condition_name") or ""),
            index=str(payload.get("condition_index") or ""),
            code=_event_code(event),
            bucket=_timestamp_bucket(str(event.timestamp or payload.get("timestamp") or "")),
        )
    if event_type == "tr_response":
        request_id = str(event.request_id or payload.get("request_id") or "")
        return f"tr:{command_id or request_id}"
    return f"{event_type}:{command_id or _event_code(event) or _timestamp_bucket(str(event.timestamp or ''))}"


def _event_payload_json(event: GatewayEvent) -> tuple[str, str]:
    try:
        return json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, default=str), ""
    except Exception as exc:
        fallback = {
            "event_id": str(getattr(event, "event_id", "") or ""),
            "type": str(getattr(event, "type", "") or ""),
            "source": str(getattr(event, "source", "") or ""),
            "command_id": str(getattr(event, "command_id", "") or ""),
            "serialization_error": str(exc),
        }
        return json.dumps(fallback, ensure_ascii=False, sort_keys=True, default=str), f"PAYLOAD_SERIALIZATION_FAILED:{exc}"


def _row_to_record(row: sqlite3.Row) -> EventLogRecord:
    return EventLogRecord(
        id=int(row["id"] or 0),
        event_id=str(row["event_id"] or ""),
        event_type=str(row["event_type"] or ""),
        dedupe_key=str(row["dedupe_key"] or ""),
        source=str(row["source"] or ""),
        command_id=str(row["command_id"] or ""),
        code=str(row["code"] or ""),
        trade_date=str(row["trade_date"] or ""),
        payload_json=str(row["payload_json"] or "{}"),
        received_at=str(row["received_at"] or ""),
        processed_at=str(row["processed_at"] or ""),
        processing_status=str(row["processing_status"] or ""),
        error=str(row["error"] or ""),
        created_at=str(row["created_at"] or ""),
        processing_attempts=int(row["processing_attempts"] or 0) if "processing_attempts" in row.keys() else 0,
        claimed_at=str(row["claimed_at"] or "") if "claimed_at" in row.keys() else "",
        claimed_by=str(row["claimed_by"] or "") if "claimed_by" in row.keys() else "",
        next_retry_at=str(row["next_retry_at"] or "") if "next_retry_at" in row.keys() else "",
        handler_name=str(row["handler_name"] or "") if "handler_name" in row.keys() else "",
        handler_version=str(row["handler_version"] or "") if "handler_version" in row.keys() else "",
        last_attempt_at=str(row["last_attempt_at"] or "") if "last_attempt_at" in row.keys() else "",
        dead_lettered_at=str(row["dead_lettered_at"] or "") if "dead_lettered_at" in row.keys() else "",
        processing_result_json=str(row["processing_result_json"] or "{}") if "processing_result_json" in row.keys() else "{}",
    )


def _event_code(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    text = str(payload.get("code") or payload.get("stock_code") or payload.get("symbol") or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _timestamp_bucket(timestamp: str, bucket_sec: int = 1) -> str:
    text = str(timestamp or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:19]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = int(parsed.timestamp())
    bucket = epoch - (epoch % max(1, int(bucket_sec)))
    return str(bucket)


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


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")


def _coerce_time(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).replace(microsecond=0)
        return value.astimezone(timezone.utc).replace(microsecond=0)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _now()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    return _now()


def _json_payload(payload: Any) -> str:
    try:
        return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception as exc:
        return json.dumps({"serialization_error": str(exc)}, ensure_ascii=False, sort_keys=True)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(str(row["name"]) == column for row in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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


__all__ = [
    "EventLogConfig",
    "EventLogRepository",
    "REPLAYABLE_GATEWAY_EVENT_TYPES",
    "dedupe_key_for_gateway_event",
]
