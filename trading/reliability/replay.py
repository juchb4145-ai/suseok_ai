from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from storage.db import TradingDatabase
from storage.event_log import EventLogRepository
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading_app.gateway_event_consumer import GatewayEventConsumerConfig, GatewayEventDispatcher, OrderLifecycleEventConsumer


DIGEST_TABLES = (
    "gateway_event_log",
    "order_gateway_event_receipts",
    "managed_orders",
    "managed_order_intents",
    "broker_order_state",
    "broker_position_state",
    "order_kill_switch_state",
    "dashboard_read_models",
)
DYNAMIC_FIELDS = {
    "id",
    "created_at",
    "updated_at",
    "received_at",
    "processed_at",
    "claimed_at",
    "claimed_by",
    "next_retry_at",
    "last_attempt_at",
    "dead_lettered_at",
    "applied_at",
    "snapshot_at",
    "generated_at",
    "build_duration_ms",
    "processing_duration_ms",
    "handler_name",
    "worker_id",
}


@dataclass
class ReplayDigest:
    digest: str
    sections: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "sections": self.sections}


@dataclass
class DeterministicReplayResult:
    status: str
    repeat: int
    digests: list[ReplayDigest]
    mismatch: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "repeat": self.repeat,
            "digests": [item.to_dict() for item in self.digests],
            "mismatch": self.mismatch,
            "warnings": self.warnings,
        }


class DeterministicReplayVerifier:
    def __init__(self, *, output_dir: str | Path, seed: int = 20260618) -> None:
        self.output_dir = Path(output_dir)
        self.seed = int(seed)

    def verify_events(self, events: Iterable[GatewayEvent], *, repeat: int = 2) -> DeterministicReplayResult:
        event_list = list(events)
        digests: list[ReplayDigest] = []
        replay_root = self.output_dir / "deterministic_replay"
        if replay_root.exists():
            shutil.rmtree(replay_root)
        replay_root.mkdir(parents=True, exist_ok=True)
        for index in range(max(1, int(repeat))):
            db_path = replay_root / f"run_{index + 1}.sqlite3"
            self._run_events(db_path, event_list)
            digests.append(digest_database(db_path))
        mismatch = first_digest_mismatch(digests)
        return DeterministicReplayResult(
            status="PASS" if not mismatch else "FAIL",
            repeat=len(digests),
            digests=digests,
            mismatch=mismatch,
        )

    def _run_events(self, db_path: Path, events: list[GatewayEvent]) -> None:
        db = TradingDatabase(str(db_path))
        db.close()
        event_log = EventLogRepository(db_path)
        gateway_state = GatewayStateStore(event_log_store=event_log)
        consumer = OrderLifecycleEventConsumer(
            db_path=db_path,
            gateway_state=gateway_state,
            config=GatewayEventConsumerConfig(),
        )
        dispatcher = GatewayEventDispatcher(event_log=event_log, order_consumer=consumer, config=GatewayEventConsumerConfig())
        dispatcher.start()
        try:
            for event in events:
                accepted = gateway_state.record_event(event)
                if accepted and event.type not in {"price_tick", "heartbeat"}:
                    dispatcher.consume_live_event(event)
            dispatcher.replay_pending(limit=500)
        finally:
            event_log.close()


def digest_database(db_path: str | Path, *, tables: Iterable[str] = DIGEST_TABLES) -> ReplayDigest:
    path = Path(db_path)
    sections: dict[str, Any] = {}
    if not path.exists():
        return ReplayDigest(digest="", sections={"missing": str(path)})
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for table in tables:
            if not _table_exists(conn, table):
                continue
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            normalized = [_normalize_row(dict(row)) for row in rows]
            normalized.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
            sections[table] = normalized
    finally:
        conn.close()
    raw = json.dumps(sections, ensure_ascii=False, sort_keys=True, default=str)
    return ReplayDigest(hashlib.sha256(raw.encode("utf-8")).hexdigest(), sections)


def first_digest_mismatch(digests: list[ReplayDigest]) -> dict[str, Any]:
    if len(digests) <= 1:
        return {}
    expected = digests[0]
    for index, digest in enumerate(digests[1:], start=2):
        if digest.digest == expected.digest:
            continue
        section = _first_differing_section(expected.sections, digest.sections)
        return {
            "run_index": index,
            "expected_digest": expected.digest,
            "actual_digest": digest.digest,
            "differing_section": section.get("section", ""),
            "expected": section.get("expected"),
            "actual": section.get("actual"),
        }
    return {}


def _first_differing_section(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(left) | set(right))
    for key in keys:
        if left.get(key) != right.get(key):
            return {"section": key, "expected": left.get(key), "actual": right.get(key)}
    return {}


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key in DYNAMIC_FIELDS:
            continue
        if str(key).endswith("_at"):
            continue
        if isinstance(value, str) and value.startswith("rel_"):
            continue
        if str(key).endswith("_json"):
            normalized[key] = _normalize_json(value)
        else:
            normalized[key] = value
    return normalized


def _normalize_json(value: Any) -> Any:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return value
    return _strip_dynamic(parsed)


def _strip_dynamic(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_dynamic(item) for key, item in sorted(value.items()) if key not in DYNAMIC_FIELDS and not str(key).endswith("_at")}
    if isinstance(value, list):
        return [_strip_dynamic(item) for item in value]
    return value


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (str(table),)).fetchone()
    return row is not None


__all__ = [
    "DIGEST_TABLES",
    "DeterministicReplayResult",
    "DeterministicReplayVerifier",
    "ReplayDigest",
    "digest_database",
    "first_digest_mismatch",
]
