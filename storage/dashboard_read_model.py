from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class DashboardReadModelRecord:
    id: int = 0
    view_name: str = "main"
    schema_version: str = "dashboard_v2.read_model.v1"
    trade_date: str = ""
    generation: int = 0
    snapshot: dict[str, Any] | None = None
    checksum: str = ""
    status: str = "OK"
    snapshot_at: str = ""
    source_runtime_cycle_at: str = ""
    source_runtime_cycle_count: int = 0
    source_event_watermark: str = ""
    stale_after_sec: int = 5
    build_duration_ms: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    last_error: str = ""
    persisted: bool = True
    unchanged: bool = False
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "view_name": self.view_name,
            "schema_version": self.schema_version,
            "trade_date": self.trade_date,
            "generation": self.generation,
            "snapshot": dict(self.snapshot or {}),
            "checksum": self.checksum,
            "status": self.status,
            "snapshot_at": self.snapshot_at,
            "source_runtime_cycle_at": self.source_runtime_cycle_at,
            "source_runtime_cycle_count": self.source_runtime_cycle_count,
            "source_event_watermark": self.source_event_watermark,
            "stale_after_sec": self.stale_after_sec,
            "build_duration_ms": self.build_duration_ms,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
            "persisted": self.persisted,
            "unchanged": self.unchanged,
            "recovered": self.recovered,
        }


class DashboardReadModelRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser()
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def save_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        view_name: str = "main",
        schema_version: str = "dashboard_v2.read_model.v1",
        trade_date: str = "",
        generation: int = 0,
        snapshot_at: str = "",
        source_runtime_cycle_at: str = "",
        source_runtime_cycle_count: int = 0,
        source_event_watermark: str = "",
        stale_after_sec: int = 5,
        build_duration_ms: float = 0.0,
        status: str = "OK",
        last_error: str = "",
        checksum: str = "",
        skip_unchanged: bool = True,
    ) -> DashboardReadModelRecord:
        view = str(view_name or "main")
        current_at = snapshot_at or _format_time(_now())
        current_trade_date = trade_date or _trade_date(current_at)
        payload = dict(snapshot or {})
        resolved_checksum = checksum or checksum_snapshot(payload)
        snapshot_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock, self.conn:
            existing = self._read_record_no_lock(view)
            if existing is not None and skip_unchanged and existing.checksum == resolved_checksum:
                return replace(existing, unchanged=True, persisted=True)
            next_generation = int(generation or 0)
            if next_generation <= 0:
                next_generation = int(existing.generation + 1 if existing else 1)
            now_text = _format_time(_now())
            self.conn.execute(
                """
                INSERT INTO dashboard_read_models(
                    view_name, schema_version, trade_date, generation, snapshot_json,
                    checksum, status, snapshot_at, source_runtime_cycle_at,
                    source_runtime_cycle_count, source_event_watermark, stale_after_sec,
                    build_duration_ms, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(view_name) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    trade_date = excluded.trade_date,
                    generation = excluded.generation,
                    snapshot_json = excluded.snapshot_json,
                    checksum = excluded.checksum,
                    status = excluded.status,
                    snapshot_at = excluded.snapshot_at,
                    source_runtime_cycle_at = excluded.source_runtime_cycle_at,
                    source_runtime_cycle_count = excluded.source_runtime_cycle_count,
                    source_event_watermark = excluded.source_event_watermark,
                    stale_after_sec = excluded.stale_after_sec,
                    build_duration_ms = excluded.build_duration_ms,
                    updated_at = excluded.updated_at,
                    last_error = excluded.last_error
                """,
                (
                    view,
                    str(schema_version or ""),
                    current_trade_date,
                    next_generation,
                    snapshot_json,
                    resolved_checksum,
                    str(status or "OK"),
                    current_at,
                    str(source_runtime_cycle_at or ""),
                    int(source_runtime_cycle_count or 0),
                    str(source_event_watermark or ""),
                    max(1, int(stale_after_sec or 5)),
                    float(build_duration_ms or 0.0),
                    now_text,
                    now_text,
                    str(last_error or ""),
                ),
            )
            record = self._read_record_no_lock(view)
            if record is None:
                raise RuntimeError(f"dashboard read model save failed: {view}")
            return record

    def read_main_snapshot(self) -> DashboardReadModelRecord | None:
        return self.read_snapshot("main")

    def read_snapshot(self, view_name: str) -> DashboardReadModelRecord | None:
        with self._lock:
            return self._read_record_no_lock(str(view_name or "main"))

    def recover_latest_snapshot(self, view_name: str = "main") -> DashboardReadModelRecord | None:
        record = self.read_snapshot(view_name)
        if record is None:
            return None
        return replace(record, recovered=True)

    def snapshot_status(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT view_name, status, generation, snapshot_at, updated_at, last_error FROM dashboard_read_models"
            ).fetchall()
            return {
                "view_count": len(rows),
                "views": [dict(row) for row in rows],
                "latest_snapshot_at": max([str(row["snapshot_at"] or "") for row in rows], default=""),
            }

    def _migrate(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dashboard_read_models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    view_name TEXT NOT NULL,
                    schema_version TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    checksum TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'OK',
                    snapshot_at TEXT NOT NULL DEFAULT '',
                    source_runtime_cycle_at TEXT NOT NULL DEFAULT '',
                    source_runtime_cycle_count INTEGER NOT NULL DEFAULT 0,
                    source_event_watermark TEXT NOT NULL DEFAULT '',
                    stale_after_sec INTEGER NOT NULL DEFAULT 5,
                    build_duration_ms REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(view_name)
                );
                CREATE INDEX IF NOT EXISTS idx_dashboard_read_models_view_trade_date
                    ON dashboard_read_models(view_name, trade_date);
                CREATE INDEX IF NOT EXISTS idx_dashboard_read_models_snapshot_at
                    ON dashboard_read_models(snapshot_at);
                CREATE INDEX IF NOT EXISTS idx_dashboard_read_models_status
                    ON dashboard_read_models(status);
                """
            )

    def _read_record_no_lock(self, view_name: str) -> DashboardReadModelRecord | None:
        row = self.conn.execute(
            "SELECT * FROM dashboard_read_models WHERE view_name = ?",
            (str(view_name or "main"),),
        ).fetchone()
        if row is None:
            return None
        snapshot: dict[str, Any] = {}
        status = str(row["status"] or "OK")
        error = str(row["last_error"] or "")
        try:
            parsed = json.loads(str(row["snapshot_json"] or "{}"))
            snapshot = dict(parsed or {}) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError as exc:
            status = "CORRUPT"
            error = f"SNAPSHOT_JSON_CORRUPT:{exc}"
        return DashboardReadModelRecord(
            id=int(row["id"] or 0),
            view_name=str(row["view_name"] or ""),
            schema_version=str(row["schema_version"] or ""),
            trade_date=str(row["trade_date"] or ""),
            generation=int(row["generation"] or 0),
            snapshot=snapshot,
            checksum=str(row["checksum"] or ""),
            status=status,
            snapshot_at=str(row["snapshot_at"] or ""),
            source_runtime_cycle_at=str(row["source_runtime_cycle_at"] or ""),
            source_runtime_cycle_count=int(row["source_runtime_cycle_count"] or 0),
            source_event_watermark=str(row["source_event_watermark"] or ""),
            stale_after_sec=int(row["stale_after_sec"] or 5),
            build_duration_ms=float(row["build_duration_ms"] or 0.0),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            last_error=error,
        )


def checksum_snapshot(snapshot: dict[str, Any]) -> str:
    import hashlib

    stable = _stable_for_checksum(snapshot)
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_for_checksum(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            if text in {
                "generated_at",
                "snapshot_age_sec",
                "source_runtime_cycle_age_sec",
                "build_duration_ms",
                "updated_at",
                "last_write_at",
            }:
                continue
            if text == "read_model":
                metadata = dict(item or {}) if isinstance(item, dict) else {}
                result[text] = {
                    key: _stable_for_checksum(metadata.get(key))
                    for key in (
                        "enabled",
                        "source",
                        "view_name",
                        "schema_version",
                        "generation",
                        "status",
                        "stale",
                        "stale_after_sec",
                        "source_runtime_cycle_at",
                        "source_runtime_cycle_count",
                        "fallback_used",
                        "persisted",
                        "warnings",
                    )
                    if key in metadata
                }
                continue
            result[text] = _stable_for_checksum(item)
        return result
    if isinstance(value, list):
        return [_stable_for_checksum(item) for item in value]
    return value


def _trade_date(timestamp: str) -> str:
    text = str(timestamp or "")
    if not text:
        return _now().astimezone(timezone(timedelta(hours=9))).date().isoformat()
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


__all__ = [
    "DashboardReadModelRecord",
    "DashboardReadModelRepository",
    "checksum_snapshot",
]
