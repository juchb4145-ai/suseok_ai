from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from trading.broker.models import GatewayEvent
from trading.runtime_ports import EventLogAppendResult, EventLogRecord


@dataclass(frozen=True)
class EventLogConfig:
    enabled: bool = True
    price_tick_enabled: bool = False
    heartbeat_enabled: bool = False
    max_pending_replay: int = 500

    @classmethod
    def from_env(cls) -> "EventLogConfig":
        return cls(
            enabled=_env_bool("TRADING_EVENT_LOG_ENABLED", True),
            price_tick_enabled=_env_bool("TRADING_EVENT_LOG_PRICE_TICK_ENABLED", False),
            heartbeat_enabled=_env_bool("TRADING_EVENT_LOG_HEARTBEAT_ENABLED", False),
            max_pending_replay=max(1, _env_int("TRADING_EVENT_LOG_MAX_PENDING_REPLAY", 500)),
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
                        processing_status, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
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

    def mark_processed(
        self,
        event_log_id: int | str,
        *,
        processed_at: str | None = None,
        core_events: Any = (),
    ) -> None:
        current = str(processed_at or _format_time(_now()))
        with self._lock, self.conn:
            self._update_status_no_lock(event_log_id, "PROCESSED", processed_at=current, error="")

    def mark_failed(self, event_log_id: int | str, *, error: str) -> None:
        with self._lock, self.conn:
            self._update_status_no_lock(event_log_id, "FAILED", processed_at="", error=str(error or ""))

    def find_by_dedupe_key(self, dedupe_key: str) -> EventLogRecord | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM gateway_event_log WHERE dedupe_key = ?",
                (str(dedupe_key or ""),),
            ).fetchone()
            return _row_to_record(row) if row else None

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
            return {
                "enabled": self.config.enabled,
                "price_tick_enabled": self.config.price_tick_enabled,
                "heartbeat_enabled": self.config.heartbeat_enabled,
                "max_pending_replay": self.config.max_pending_replay,
                "pending_count": counts.get("PENDING", 0),
                "processed_count": counts.get("PROCESSED", 0),
                "failed_count": counts.get("FAILED", 0),
                "total_count": sum(counts.values()),
                "event_type_counts": {str(row["event_type"]): int(row["count"]) for row in type_rows},
                "last_received_at": str(last_received or ""),
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

    def _get_by_id_no_lock(self, row_id: int) -> EventLogRecord | None:
        row = self.conn.execute(
            "SELECT * FROM gateway_event_log WHERE id = ?",
            (int(row_id),),
        ).fetchone()
        return _row_to_record(row) if row else None

    def _update_status_no_lock(
        self,
        event_log_id: int | str,
        status: str,
        *,
        processed_at: str,
        error: str,
    ) -> None:
        if isinstance(event_log_id, int) or str(event_log_id).isdigit():
            self.conn.execute(
                """
                UPDATE gateway_event_log
                SET processing_status = ?, processed_at = ?, error = ?
                WHERE id = ?
                """,
                (status, processed_at, error, int(event_log_id)),
            )
            return
        self.conn.execute(
            """
            UPDATE gateway_event_log
            SET processing_status = ?, processed_at = ?, error = ?
            WHERE event_id = ?
            """,
            (status, processed_at, error, str(event_log_id or "")),
        )


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
    if event_type in {"order_ack", "order_fill", "execution", "fill"}:
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
    "dedupe_key_for_gateway_event",
]
