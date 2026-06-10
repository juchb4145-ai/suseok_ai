from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote
from uuid import uuid4

from storage.db import TradingDatabase
from trading.strategy.replay import TickReplayRunner
from trading_app.intraday_outcomes import IntradayOutcomeLabeler
from trading_app.shadow_strategy import ShadowStrategyEvaluator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "reports" / "strategy_replay" / "bundles"
DEFAULT_REPLAY_DB_ROOT = PROJECT_ROOT / "data" / "replay"
REPLAY_MODES = {"data_only", "decision_led", "full_runtime"}
REQUIRED_PRIOR_TABLES = (
    "strategy_decision_events",
    "strategy_decision_outcomes",
    "shadow_strategy_evaluations",
)
TICK_COLUMNS = [
    "timestamp",
    "code",
    "price",
    "change_rate",
    "cum_volume",
    "trade_value",
    "execution_strength",
    "best_bid",
    "best_ask",
    "spread_ticks",
    "source",
    "row_type",
]


@dataclass(frozen=True)
class StrategyReplayManifest:
    replay_id: str
    trade_date: str
    source_db_path: str
    created_at: str
    session_start: str
    session_end: str
    codes: list[str] = field(default_factory=list)
    theme_names: list[str] = field(default_factory=list)
    runtime_config_hash: str = ""
    strategy_version: str = ""
    data_files: dict[str, str] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    status: str = "READY"

    @classmethod
    def from_path(cls, path: str | Path) -> "StrategyReplayManifest":
        payload = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
        return cls(**{key: payload.get(key) for key in cls.__dataclass_fields__.keys()})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyReplayBundle:
    path: Path
    manifest: StrategyReplayManifest

    @property
    def ticks_path(self) -> Path:
        return self.path / self.manifest.data_files.get("ticks", "ticks.csv")

    @property
    def candidate_events_path(self) -> Path:
        return self.path / self.manifest.data_files.get("candidate_events", "candidate_events.jsonl")

    @property
    def theme_snapshots_path(self) -> Path:
        return self.path / self.manifest.data_files.get("theme_snapshots", "theme_snapshots.jsonl")

    @property
    def market_status_path(self) -> Path:
        return self.path / self.manifest.data_files.get("market_status", "market_status.jsonl")

    @property
    def decision_events_path(self) -> Path:
        return self.path / self.manifest.data_files.get("decision_events", "decision_events.jsonl")


@dataclass
class ReplayResult:
    replay_id: str
    trade_date: str
    mode: str
    status: str
    replay_db_path: str
    source_bundle_path: str
    summary: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ReplayComparisonReport:
    report_id: str
    replay_id: str
    trade_date: str
    mode: str
    summary: dict[str, Any]
    funnel: dict[str, Any]
    outcome_summary: dict[str, Any]
    shadow_summary: dict[str, Any]
    diff_summary: dict[str, Any]
    recommendations: list[dict[str, Any]]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReplayReadinessError(RuntimeError):
    def __init__(self, missing_tables: Iterable[str]) -> None:
        self.missing_tables = list(missing_tables)
        super().__init__(f"missing required replay tables: {', '.join(self.missing_tables)}")


class StrategyReplayBundleExporter:
    def __init__(
        self,
        source_db_path: str | Path,
        *,
        output_root: str | Path = DEFAULT_BUNDLE_ROOT,
        now_provider: Optional[callable] = None,
    ) -> None:
        self.source_db_path = Path(source_db_path).expanduser()
        self.output_root = Path(output_root).expanduser()
        self.now_provider = now_provider or (lambda: datetime.now().replace(microsecond=0))

    def export_bundle(
        self,
        trade_date: str,
        *,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        codes: Optional[Iterable[str]] = None,
        theme_names: Optional[Iterable[str]] = None,
        force: bool = False,
    ) -> StrategyReplayBundle:
        replay_id = f"replay_{trade_date}_{uuid4().hex[:10]}"
        bundle_path = self.output_root / f"{trade_date}_{replay_id}"
        if bundle_path.exists() and not force:
            raise FileExistsError(str(bundle_path))
        bundle_path.mkdir(parents=True, exist_ok=True)
        filters = _ExportFilters(
            trade_date=str(trade_date),
            start_time=start_time or "09:00:00",
            end_time=end_time or "15:30:00",
            codes=sorted({_clean_code(code) for code in codes or [] if _clean_code(code)}),
            theme_names=sorted({str(theme) for theme in theme_names or [] if str(theme or "").strip()}),
        )
        warnings: list[str] = []
        data_quality: dict[str, Any] = {}
        with _connect_readonly(self.source_db_path) as conn:
            missing = [name for name in REQUIRED_PRIOR_TABLES if not _table_exists(conn, name)]
            if missing:
                warnings.append(f"READINESS_MISSING_TABLES:{','.join(missing)}")
            ticks = self.export_ticks(conn, bundle_path, filters, warnings, data_quality)
            candidate_events = self.export_candidate_events(conn, bundle_path, filters, warnings, data_quality)
            theme_snapshots = self.export_theme_snapshots(conn, bundle_path, filters, warnings, data_quality)
            market_status = self.export_market_status(conn, bundle_path, filters, warnings, data_quality)
            decision_events = self.export_decision_events(conn, bundle_path, filters, warnings, data_quality)
            runtime_config = self.export_runtime_config(conn, bundle_path, warnings)

        discovered_codes = sorted(
            str(code)
            for code in {*(row.get("code") for row in ticks), *(row.get("code") for row in candidate_events), *filters.codes}
            if str(code or "")
        )
        discovered_themes = sorted(
            str(theme)
            for theme in {*(row.get("theme_name") for row in candidate_events), *(row.get("theme_name") for row in theme_snapshots), *filters.theme_names}
            if str(theme or "")
        )
        runtime_config_hash = _hash_payload(runtime_config)
        data_files = {
            "manifest": "manifest.json",
            "ticks": "ticks.csv",
            "candidate_events": "candidate_events.jsonl",
            "theme_snapshots": "theme_snapshots.jsonl",
            "market_status": "market_status.jsonl",
            "decision_events": "decision_events.jsonl",
            "runtime_config": "runtime_config.json",
        }
        if not ticks:
            warnings.append("MISSING_TICK_HISTORY")
        if not candidate_events:
            warnings.append("MISSING_CANDIDATE_EVENTS")
        status = "READY" if ticks and candidate_events else "PARTIAL_BUNDLE"
        data_quality.update(
            {
                "status": status,
                "tick_count": len(ticks),
                "candidate_event_count": len(candidate_events),
                "theme_snapshot_count": len(theme_snapshots),
                "market_status_count": len(market_status),
                "decision_event_count": len(decision_events),
            }
        )
        manifest = StrategyReplayManifest(
            replay_id=replay_id,
            trade_date=trade_date,
            source_db_path=str(self.source_db_path),
            created_at=self.now_provider().isoformat(timespec="seconds"),
            session_start=f"{trade_date}T{filters.start_time}",
            session_end=f"{trade_date}T{filters.end_time}",
            codes=discovered_codes,
            theme_names=discovered_themes,
            runtime_config_hash=runtime_config_hash,
            strategy_version=str(runtime_config.get("strategy_version") or ""),
            data_files=data_files,
            data_quality=data_quality,
            warnings=sorted(set(warnings)),
            status=status,
        )
        (bundle_path / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return StrategyReplayBundle(path=bundle_path, manifest=manifest)

    def export_ticks(
        self,
        conn: sqlite3.Connection,
        bundle_path: Path,
        filters: "_ExportFilters",
        warnings: list[str],
        data_quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table_name in ("gateway_price_ticks", "realtime_tick_history", "price_ticks", "market_ticks"):
            if not _table_exists(conn, table_name):
                continue
            columns = _columns(conn, table_name)
            timestamp_col = _first_existing(columns, ("timestamp", "created_at", "received_at", "event_time"))
            code_col = _first_existing(columns, ("code", "stock_code", "symbol"))
            price_col = _first_existing(columns, ("price", "current_price", "last_price"))
            if not timestamp_col or not code_col or not price_col:
                continue
            query, params = _time_code_query(table_name, timestamp_col, code_col, filters)
            selected = conn.execute(query, params).fetchall()
            for row in selected:
                payload = dict(row)
                rows.append(_tick_from_row(payload, timestamp_col, code_col, price_col, source=table_name))
            if rows:
                break
        if not rows and _table_exists(conn, "strategy_decision_events"):
            decision_rows = self._query_decision_events(conn, filters)
            for row in decision_rows:
                if row.get("price") in (None, "", 0):
                    continue
                rows.append(
                    {
                        "timestamp": row.get("decision_at") or row.get("created_at") or "",
                        "code": row.get("code") or "",
                        "price": row.get("price"),
                        "change_rate": row.get("change_rate"),
                        "cum_volume": "",
                        "trade_value": row.get("trade_value"),
                        "execution_strength": row.get("execution_strength"),
                        "best_bid": "",
                        "best_ask": "",
                        "spread_ticks": "",
                        "source": "strategy_decision_events",
                        "row_type": "tick",
                    }
                )
            if rows:
                warnings.append("TICK_HISTORY_RECONSTRUCTED_FROM_DECISIONS")
        rows = _dedupe_rows(rows, key_fields=("timestamp", "code", "price"))
        rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row.get("code") or "")))
        _write_csv(bundle_path / "ticks.csv", TICK_COLUMNS, rows)
        data_quality["tick_source"] = rows[0]["source"] if rows else "missing"
        return rows

    def export_candidate_events(
        self,
        conn: sqlite3.Connection,
        bundle_path: Path,
        filters: "_ExportFilters",
        warnings: list[str],
        data_quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if _table_exists(conn, "candidate_events") and _table_exists(conn, "candidates"):
            clauses = ["c.trade_date = ?"]
            params: list[Any] = [filters.trade_date]
            if filters.codes:
                clauses.append(f"c.code IN ({','.join('?' for _ in filters.codes)})")
                params.extend(filters.codes)
            if filters.start_time:
                clauses.append("substr(e.created_at, 12, 8) >= ?")
                params.append(filters.start_time)
            if filters.end_time:
                clauses.append("substr(e.created_at, 12, 8) <= ?")
                params.append(filters.end_time)
            selected = conn.execute(
                f"""
                SELECT e.*, c.trade_date, c.code, c.name, c.condition_names_json,
                       c.theme_ids_json, c.metadata_json
                FROM candidate_events e
                JOIN candidates c ON c.id = e.candidate_id
                WHERE {' AND '.join(clauses)}
                ORDER BY e.created_at ASC, e.id ASC
                """,
                tuple(params),
            ).fetchall()
            for row in selected:
                rows.append(_candidate_event_from_row(dict(row)))
        if not rows and _table_exists(conn, "strategy_decision_events"):
            seen: set[str] = set()
            for row in self._query_decision_events(conn, filters):
                key = str(row.get("candidate_instance_id") or row.get("candidate_id") or row.get("code") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "timestamp": row.get("decision_at") or row.get("created_at") or "",
                        "code": row.get("code") or "",
                        "name": row.get("name") or "",
                        "candidate_id": row.get("candidate_id"),
                        "candidate_instance_id": row.get("candidate_instance_id") or "",
                        "candidate_generation_seq": row.get("candidate_generation_seq") or 0,
                        "theme_name": row.get("theme_name") or "",
                        "source": "strategy_decision_events",
                        "condition_name": "",
                        "reason": row.get("gate_reason") or "",
                        "metadata": {"fallback": True, "decision_id": row.get("decision_id")},
                    }
                )
            if rows:
                warnings.append("CANDIDATE_EVENTS_RECONSTRUCTED_FROM_DECISIONS")
        rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row.get("code") or "")))
        _write_jsonl(bundle_path / "candidate_events.jsonl", rows)
        data_quality["candidate_event_source"] = rows[0]["source"] if rows else "missing"
        return rows

    def export_theme_snapshots(
        self,
        conn: sqlite3.Connection,
        bundle_path: Path,
        filters: "_ExportFilters",
        warnings: list[str],
        data_quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if _table_exists(conn, "theme_activity_snapshots"):
            clauses = ["substr(created_at, 1, 10) = ?"]
            params: list[Any] = [filters.trade_date]
            if filters.theme_names:
                clauses.append(f"theme_name IN ({','.join('?' for _ in filters.theme_names)})")
                params.extend(filters.theme_names)
            selected = conn.execute(
                f"""
                SELECT * FROM theme_activity_snapshots
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC, rank ASC, id ASC
                """,
                tuple(params),
            ).fetchall()
            for row in selected:
                rows.append(_theme_snapshot_from_row(dict(row)))
        if not rows and _table_exists(conn, "strategy_decision_events"):
            seen: set[tuple[str, str]] = set()
            for row in self._query_decision_events(conn, filters):
                theme_name = str(row.get("theme_name") or "")
                if not theme_name:
                    continue
                key = (str(row.get("decision_at") or ""), theme_name)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "timestamp": row.get("decision_at") or "",
                        "theme_name": theme_name,
                        "theme_score": row.get("theme_score"),
                        "breadth_pct": None,
                        "leader_codes": [row.get("code")] if row.get("code") else [],
                        "co_leader_codes": [],
                        "watch_codes": [],
                        "data_quality_flags": ["RECONSTRUCTED_FROM_DECISION_EVENT"],
                        "raw": {"decision_id": row.get("decision_id")},
                    }
                )
            if rows:
                warnings.append("THEME_SNAPSHOTS_RECONSTRUCTED_FROM_DECISIONS")
        rows.sort(key=lambda row: (str(row.get("timestamp") or ""), str(row.get("theme_name") or "")))
        _write_jsonl(bundle_path / "theme_snapshots.jsonl", rows)
        data_quality["theme_snapshot_source"] = "theme_activity_snapshots" if rows else "missing"
        return rows

    def export_market_status(
        self,
        conn: sqlite3.Connection,
        bundle_path: Path,
        filters: "_ExportFilters",
        warnings: list[str],
        data_quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if _table_exists(conn, "theme_lab_flow_snapshots"):
            selected = conn.execute(
                """
                SELECT calculated_at, created_at, market_status_json, data_quality_json, payload_json
                FROM theme_lab_flow_snapshots
                WHERE substr(COALESCE(NULLIF(calculated_at, ''), created_at), 1, 10) = ?
                ORDER BY COALESCE(NULLIF(calculated_at, ''), created_at) ASC, id ASC
                """,
                (filters.trade_date,),
            ).fetchall()
            for row in selected:
                rows.append(_market_status_from_row(dict(row)))
        if not rows:
            warnings.append("MISSING_MARKET_STATUS")
        _write_jsonl(bundle_path / "market_status.jsonl", rows)
        data_quality["market_status_source"] = "theme_lab_flow_snapshots" if rows else "missing"
        return rows

    def export_decision_events(
        self,
        conn: sqlite3.Connection,
        bundle_path: Path,
        filters: "_ExportFilters",
        warnings: list[str],
        data_quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = self._query_decision_events(conn, filters) if _table_exists(conn, "strategy_decision_events") else []
        _write_jsonl(bundle_path / "decision_events.jsonl", rows)
        data_quality["decision_event_source"] = "strategy_decision_events" if rows else "missing"
        return rows

    def export_runtime_config(self, conn: sqlite3.Connection, bundle_path: Path, warnings: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": "REPLAY",
            "TRADING_REPLAY_MODE": True,
            "live_order_enabled": False,
            "runtime_allow_live_orders": False,
            "runtime_allow_dry_run_orders": True,
            "external_network_enabled": False,
            "strategy_version": "",
            "settings": {},
        }
        if _table_exists(conn, "strategy_runtime_settings"):
            rows = conn.execute(
                """
                SELECT * FROM strategy_runtime_settings
                WHERE enabled = 1
                ORDER BY config_version DESC, updated_at DESC
                LIMIT 5
                """
            ).fetchall()
            payload["settings"] = [dict(row) for row in rows]
            if rows:
                payload["strategy_version"] = str(rows[0]["profile_version"] or rows[0]["config_version"] or "")
        if not payload.get("settings"):
            warnings.append("RUNTIME_CONFIG_DEFAULTED")
        (bundle_path / "runtime_config.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return payload

    def validate_bundle(self, bundle_path: str | Path) -> dict[str, Any]:
        path = Path(bundle_path)
        errors: list[str] = []
        warnings: list[str] = []
        if not (path / "manifest.json").exists():
            errors.append("MISSING_MANIFEST")
            return {"ok": False, "errors": errors, "warnings": warnings}
        manifest = StrategyReplayManifest.from_path(path)
        for key, file_name in manifest.data_files.items():
            if key == "manifest":
                continue
            if not (path / file_name).exists():
                errors.append(f"MISSING_FILE:{key}:{file_name}")
        if manifest.status == "PARTIAL_BUNDLE":
            warnings.extend(manifest.warnings)
        return {"ok": not errors, "errors": errors, "warnings": warnings, "manifest": manifest.to_dict()}

    def _query_decision_events(self, conn: sqlite3.Connection, filters: "_ExportFilters") -> list[dict[str, Any]]:
        clauses = ["trade_date = ?"]
        params: list[Any] = [filters.trade_date]
        if filters.codes:
            clauses.append(f"code IN ({','.join('?' for _ in filters.codes)})")
            params.extend(filters.codes)
        if filters.theme_names:
            clauses.append(f"theme_name IN ({','.join('?' for _ in filters.theme_names)})")
            params.extend(filters.theme_names)
        if filters.start_time:
            clauses.append("substr(decision_at, 12, 8) >= ?")
            params.append(filters.start_time)
        if filters.end_time:
            clauses.append("substr(decision_at, 12, 8) <= ?")
            params.append(filters.end_time)
        rows = conn.execute(
            f"""
            SELECT * FROM strategy_decision_events
            WHERE {' AND '.join(clauses)}
            ORDER BY decision_at ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
        return [_decision_row(dict(row)) for row in rows]


class ReplayClock:
    def __init__(self, start_at: str | datetime, *, speed: float = 1.0) -> None:
        self.current = _parse_dt(start_at)
        self.speed = max(0.0, float(speed or 1.0))

    def now(self) -> datetime:
        return self.current

    def advance_to(self, target: str | datetime) -> datetime:
        target_dt = _parse_dt(target)
        if target_dt < self.current:
            return self.current
        self.current = target_dt
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current = self.current + timedelta(seconds=max(0.0, float(seconds or 0.0)) * (self.speed or 1.0))
        return self.current


class ReplayGatewayEventFeeder:
    def __init__(self, bundle: StrategyReplayBundle) -> None:
        self.bundle = bundle
        self.events = sorted(
            [
                *({"type": "tick", **row} for row in _read_ticks(bundle.ticks_path)),
                *({"type": "candidate_event", **row} for row in _read_jsonl(bundle.candidate_events_path)),
                *({"type": "theme_snapshot", **row} for row in _read_jsonl(bundle.theme_snapshots_path)),
                *({"type": "market_status", **row} for row in _read_jsonl(bundle.market_status_path)),
            ],
            key=lambda item: (_event_time(item), str(item.get("type") or "")),
        )
        self._index = 0

    def next_timestamp(self) -> Optional[datetime]:
        if self._index >= len(self.events):
            return None
        return _event_time(self.events[self._index])

    def available_events(self, clock: ReplayClock) -> list[dict[str, Any]]:
        now = clock.now()
        ready: list[dict[str, Any]] = []
        while self._index < len(self.events) and _event_time(self.events[self._index]) <= now:
            ready.append(self.events[self._index])
            self._index += 1
        return ready

    def remaining_count(self) -> int:
        return max(0, len(self.events) - self._index)


class StrategyRuntimeReplayRunner:
    def __init__(
        self,
        *,
        source_db_path: str | Path,
        replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT,
        bundle_root: str | Path = DEFAULT_BUNDLE_ROOT,
        now_provider: Optional[callable] = None,
    ) -> None:
        self.source_db_path = Path(source_db_path).expanduser()
        self.replay_db_root = Path(replay_db_root).expanduser()
        self.bundle_root = Path(bundle_root).expanduser()
        self.now_provider = now_provider or (lambda: datetime.now().replace(microsecond=0))

    def run(
        self,
        *,
        bundle_path: Optional[str | Path] = None,
        trade_date: Optional[str] = None,
        mode: str = "decision_led",
        cycle_interval_sec: Optional[float] = None,
        speed: float = 1.0,
        replay_db: Optional[str | Path] = None,
        force: bool = False,
        limit: Optional[int] = None,
        export_report: bool = True,
    ) -> ReplayResult:
        if mode not in REPLAY_MODES:
            raise ValueError(f"unsupported replay mode: {mode}")
        if bundle_path is None:
            if not trade_date:
                raise ValueError("trade_date or bundle_path is required")
            bundle = StrategyReplayBundleExporter(self.source_db_path, output_root=self.bundle_root).export_bundle(
                trade_date,
                force=force,
            )
        else:
            bundle_path = Path(bundle_path)
            bundle = StrategyReplayBundle(path=bundle_path, manifest=StrategyReplayManifest.from_path(bundle_path))
        replay_id = _run_replay_id(bundle.manifest.replay_id, mode)
        replay_db_path = Path(replay_db).expanduser() if replay_db else self.replay_db_root / f"replay_{bundle.manifest.trade_date}_{replay_id}.sqlite3"
        if replay_db_path.exists() and force:
            _safe_unlink_replay_db(replay_db_path, self.replay_db_root)
        replay_db_path.parent.mkdir(parents=True, exist_ok=True)
        warnings = list(bundle.manifest.warnings or [])
        started_at = self.now_provider().isoformat(timespec="seconds")
        status = "OK"
        error = ""
        report: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        os.environ["TRADING_REPLAY_MODE"] = "true"
        os.environ["TRADING_RUNTIME_ALLOW_LIVE_ORDERS"] = "0"
        os.environ["TRADING_ALLOW_LIVE"] = "0"
        db = TradingDatabase(str(replay_db_path))
        try:
            _assert_required_tables(db.conn)
            _save_replay_source_metadata(db, bundle, mode)
            data_result = self._run_data_only(bundle) if mode in {"data_only", "full_runtime"} else {}
            decision_count = 0
            outcome_result: dict[str, Any] = {}
            shadow_result: dict[str, Any] = {}
            if mode in {"decision_led", "full_runtime"}:
                decision_count = self._import_decision_events(db, bundle, limit=limit)
                if decision_count:
                    outcome_result = self._run_outcomes(db, bundle, limit=limit)
                    shadow_result = self._run_shadow(db, bundle, limit=limit)
                elif mode == "decision_led":
                    status = "PARTIAL_REPLAY"
                    warnings.append("NO_DECISION_EVENTS_FOR_DECISION_LED_REPLAY")
            if mode == "full_runtime":
                full_status, full_warnings = self._run_full_runtime_guarded(bundle, cycle_interval_sec=cycle_interval_sec, speed=speed)
                if full_status != "OK":
                    status = full_status
                    warnings.extend(full_warnings)
            summary = self._build_summary(
                db,
                bundle,
                mode=mode,
                data_result=data_result,
                decision_event_count=decision_count,
                warnings=warnings,
            )
            if export_report:
                report = self._build_and_save_report(
                    db,
                    bundle,
                    replay_id=replay_id,
                    mode=mode,
                    summary=summary,
                    status=status,
                )
            finished_at = self.now_provider().isoformat(timespec="seconds")
            db.save_strategy_replay_run(
                {
                    "replay_id": replay_id,
                    "trade_date": bundle.manifest.trade_date,
                    "mode": mode,
                    "source_bundle_path": str(bundle.path),
                    "replay_db_path": str(replay_db_path),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": status,
                    "runtime_config_hash": bundle.manifest.runtime_config_hash,
                    "strategy_version": bundle.manifest.strategy_version,
                    "processed_tick_count": int(summary.get("processed_tick_count") or 0),
                    "processed_candidate_event_count": int(summary.get("candidate_count") or 0),
                    "processed_theme_snapshot_count": int(bundle.manifest.data_quality.get("theme_snapshot_count") or 0),
                    "cycle_count": int(summary.get("cycle_count") or 0),
                    "error": error,
                    "warnings": warnings,
                    "metadata": {
                        "source_bundle_hash": summary.get("source_bundle_hash"),
                        "TRADING_REPLAY_MODE": True,
                        "live_order_enabled": False,
                        "runtime_allow_live_orders": False,
                        "gateway_command_enqueue_allowed": False,
                        "kiwoom_gateway_start_allowed": False,
                        "external_http_sync_allowed": False,
                        "summary": summary,
                        "outcome_result": outcome_result,
                        "shadow_result": shadow_result,
                    },
                }
            )
        except ReplayReadinessError as exc:
            status = "READINESS_ERROR"
            error = str(exc)
            warnings.extend(f"MISSING_TABLE:{name}" for name in exc.missing_tables)
            finished_at = self.now_provider().isoformat(timespec="seconds")
            db.save_strategy_replay_run(
                {
                    "replay_id": replay_id,
                    "trade_date": bundle.manifest.trade_date,
                    "mode": mode,
                    "source_bundle_path": str(bundle.path),
                    "replay_db_path": str(replay_db_path),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "status": status,
                    "runtime_config_hash": bundle.manifest.runtime_config_hash,
                    "strategy_version": bundle.manifest.strategy_version,
                    "error": error,
                    "warnings": warnings,
                }
            )
        finally:
            db.close()
        return ReplayResult(
            replay_id=replay_id,
            trade_date=bundle.manifest.trade_date,
            mode=mode,
            status=status,
            replay_db_path=str(replay_db_path),
            source_bundle_path=str(bundle.path),
            summary=summary,
            report=report,
            warnings=sorted(set(warnings)),
            error=error,
        )

    def _run_data_only(self, bundle: StrategyReplayBundle) -> dict[str, Any]:
        result = TickReplayRunner().replay_rows(_read_ticks(bundle.ticks_path))
        return {
            "processed_tick_count": result.processed_ticks,
            "ignored_rows": result.ignored_rows,
            "generated_candle_count": result.completed_1m_count,
            "codes": result.details.get("codes", []),
        }

    def _import_decision_events(self, db: TradingDatabase, bundle: StrategyReplayBundle, *, limit: Optional[int] = None) -> int:
        rows = _read_jsonl(bundle.decision_events_path)
        if limit is not None:
            rows = rows[: max(1, int(limit or 1))]
        return db.save_strategy_decision_events(rows)

    def _run_outcomes(self, db: TradingDatabase, bundle: StrategyReplayBundle, *, limit: Optional[int] = None) -> dict[str, Any]:
        ticks = _read_ticks(bundle.ticks_path)
        provider = _bundle_price_provider(ticks)
        now = _parse_dt(bundle.manifest.session_end) + timedelta(seconds=3600)
        return IntradayOutcomeLabeler(db, price_provider=provider).rebuild(
            trade_date=bundle.manifest.trade_date,
            limit=limit or 10000,
            force=True,
            persist=True,
            now=now,
        )

    def _run_shadow(self, db: TradingDatabase, bundle: StrategyReplayBundle, *, limit: Optional[int] = None) -> dict[str, Any]:
        return ShadowStrategyEvaluator(db).rebuild(
            trade_date=bundle.manifest.trade_date,
            limit=limit or 10000,
            force=True,
            persist=True,
        )

    def _run_full_runtime_guarded(
        self,
        bundle: StrategyReplayBundle,
        *,
        cycle_interval_sec: Optional[float],
        speed: float,
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        quality = bundle.manifest.data_quality or {}
        required_counts = {
            "tick_count": int(quality.get("tick_count") or 0),
            "candidate_event_count": int(quality.get("candidate_event_count") or 0),
            "theme_snapshot_count": int(quality.get("theme_snapshot_count") or 0),
            "market_status_count": int(quality.get("market_status_count") or 0),
        }
        missing = [key for key, value in required_counts.items() if value <= 0]
        if missing:
            warnings.append(f"FULL_RUNTIME_DATA_INSUFFICIENT:{','.join(missing)}")
            return "PARTIAL_REPLAY", warnings
        feeder = ReplayGatewayEventFeeder(bundle)
        clock = ReplayClock(bundle.manifest.session_start, speed=speed)
        cycles = 0
        interval = float(cycle_interval_sec or 5.0)
        while feeder.remaining_count() and cycles < 100000:
            next_ts = feeder.next_timestamp()
            if next_ts is None:
                break
            clock.advance_to(next_ts)
            feeder.available_events(clock)
            cycles += 1
            clock.advance(interval)
        warnings.append("FULL_RUNTIME_ENGINE_NOT_WIRED_NO_ORDER_OR_GATEWAY_SIDE_EFFECTS")
        return "PARTIAL_REPLAY", warnings

    def _build_summary(
        self,
        db: TradingDatabase,
        bundle: StrategyReplayBundle,
        *,
        mode: str,
        data_result: dict[str, Any],
        decision_event_count: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        decision_summary = db.strategy_decision_summary(trade_date=bundle.manifest.trade_date)
        outcome_summary = db.strategy_decision_outcome_summary(trade_date=bundle.manifest.trade_date)
        shadow_summary = db.shadow_strategy_summary(trade_date=bundle.manifest.trade_date)
        quality = dict(bundle.manifest.data_quality or {})
        funnel = _replay_funnel(decision_summary, outcome_summary, db)
        order_rejected = _count_decision_events(db, bundle.manifest.trade_date, "ENTRY_ORDER_INTENT", "REJECTED")
        accepted_orders = _count_decision_events(db, bundle.manifest.trade_date, "ENTRY_ORDER_INTENT", "ACCEPTED")
        rejected_orders = order_rejected
        data_insufficient = int(outcome_summary.get("insufficient_count") or 0)
        data_insufficient += sum(int(row.get("count") or 0) for row in decision_summary.get("top_data_quality_issues") or [])
        return {
            "trade_date": bundle.manifest.trade_date,
            "mode": mode,
            "status": quality.get("status") or bundle.manifest.status,
            "processed_tick_count": int(data_result.get("processed_tick_count") or quality.get("tick_count") or 0),
            "generated_candle_count": int(data_result.get("generated_candle_count") or 0),
            "candidate_count": int(quality.get("candidate_event_count") or decision_summary.get("funnel", {}).get("detected") or 0),
            "evaluated_candidate_count": int(decision_summary.get("funnel", {}).get("evaluated") or 0),
            "ready_count": int(decision_summary.get("funnel", {}).get("ready") or 0),
            "wait_count": int(decision_summary.get("funnel", {}).get("wait") or 0),
            "blocked_count": int(decision_summary.get("funnel", {}).get("blocked") or 0),
            "observe_count": _count_gate_status(db, bundle.manifest.trade_date, "OBSERVE"),
            "entry_plan_count": int(decision_summary.get("funnel", {}).get("entry_plan") or 0),
            "order_intent_count": int(decision_summary.get("funnel", {}).get("order_intent") or 0),
            "accepted_order_intent_count": accepted_orders,
            "rejected_order_intent_count": rejected_orders,
            "virtual_order_count": _table_count(db, "virtual_orders"),
            "virtual_position_count": _table_count(db, "virtual_positions"),
            "exit_decision_count": int(decision_summary.get("exit_decision_count") or 0),
            "closed_position_count": _closed_position_count(db),
            "outcome_labeled_count": int(outcome_summary.get("labeled_count") or 0),
            "shadow_evaluation_count": int(shadow_summary.get("total_evaluations") or 0),
            "data_insufficient_count": data_insufficient,
            "cycle_count": max(
                int(quality.get("tick_count") or 0),
                int(quality.get("candidate_event_count") or 0),
                decision_event_count,
            ),
            "runtime_config_hash": bundle.manifest.runtime_config_hash,
            "source_bundle_hash": bundle_hash(bundle.path),
            "warnings": sorted(set(warnings)),
            "replay_guardrails": {
                "TRADING_REPLAY_MODE": True,
                "live_order_enabled": False,
                "runtime_allow_live_orders": False,
                "gateway_command_enqueue_allowed": False,
                "kiwoom_gateway_start_allowed": False,
                "external_http_sync_allowed": False,
            },
            "funnel": funnel,
        }

    def _build_and_save_report(
        self,
        db: TradingDatabase,
        bundle: StrategyReplayBundle,
        *,
        replay_id: str,
        mode: str,
        summary: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        outcome_summary = db.strategy_decision_outcome_summary(trade_date=bundle.manifest.trade_date)
        shadow_summary = db.shadow_strategy_summary(trade_date=bundle.manifest.trade_date)
        diff_summary = _build_diff_summary(db, bundle, status=status)
        recommendations = _build_recommendations(
            shadow_summary,
            data_quality=bundle.manifest.data_quality,
            warnings=summary.get("warnings") or [],
        )
        report = ReplayComparisonReport(
            report_id=f"replay_report_{replay_id}",
            replay_id=replay_id,
            trade_date=bundle.manifest.trade_date,
            mode=mode,
            summary=summary,
            funnel=summary.get("funnel") or {},
            outcome_summary=outcome_summary,
            shadow_summary=shadow_summary,
            diff_summary=diff_summary,
            recommendations=recommendations,
            created_at=self.now_provider().isoformat(timespec="seconds"),
        )
        return db.save_strategy_replay_report(report.to_dict())


def list_replay_bundles(bundle_root: str | Path = DEFAULT_BUNDLE_ROOT, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    root = Path(bundle_root)
    manifests = sorted(root.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    items: list[dict[str, Any]] = []
    for manifest_path in manifests[max(0, int(offset or 0)) : max(0, int(offset or 0)) + max(1, int(limit or 100))]:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload["bundle_path"] = str(manifest_path.parent)
        payload["source_bundle_hash"] = bundle_hash(manifest_path.parent)
        items.append(payload)
    return items


def scan_replay_runs(
    replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT,
    *,
    trade_date: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for db_path in _replay_db_paths(replay_db_root):
        db = TradingDatabase(str(db_path))
        try:
            rows.extend(db.list_strategy_replay_runs(trade_date=trade_date, mode=mode, limit=10000))
        except sqlite3.Error:
            continue
        finally:
            db.close()
    rows.sort(key=lambda row: str(row.get("started_at") or row.get("created_at") or ""), reverse=True)
    return rows[max(0, int(offset or 0)) : max(0, int(offset or 0)) + max(1, int(limit or 50))]


def scan_replay_reports(
    replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT,
    *,
    trade_date: Optional[str] = None,
    replay_id: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for db_path in _replay_db_paths(replay_db_root):
        db = TradingDatabase(str(db_path))
        try:
            for row in db.list_strategy_replay_reports(
                trade_date=trade_date,
                replay_id=replay_id,
                mode=mode,
                limit=10000,
            ):
                row.setdefault("replay_db_path", str(db_path))
                rows.append(row)
        except sqlite3.Error:
            continue
        finally:
            db.close()
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[max(0, int(offset or 0)) : max(0, int(offset or 0)) + max(1, int(limit or 50))]


def get_replay_run_detail(replay_id: str, replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT) -> dict[str, Any]:
    for db_path in _replay_db_paths(replay_db_root):
        db = TradingDatabase(str(db_path))
        try:
            run = db.get_strategy_replay_run(replay_id)
            if run:
                run["replay_db_path"] = str(db_path)
                return {"found": True, "run": run, "latest_report": db.latest_strategy_replay_report(replay_id)}
        finally:
            db.close()
    return {"found": False, "replay_id": replay_id}


def get_replay_report_detail(report_id: str, replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT) -> dict[str, Any]:
    for db_path in _replay_db_paths(replay_db_root):
        db = TradingDatabase(str(db_path))
        try:
            report = db.get_strategy_replay_report(report_id)
            if report:
                report["replay_db_path"] = str(db_path)
                return {"found": True, "report": report}
        finally:
            db.close()
    return {"found": False, "report_id": report_id}


def bundle_hash(bundle_path: str | Path) -> str:
    path = Path(bundle_path)
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        if file_path.name == "manifest.json":
            continue
        digest.update(str(file_path.relative_to(path)).encode("utf-8"))
        digest.update(file_path.read_bytes())
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class _ExportFilters:
    trade_date: str
    start_time: str
    end_time: str
    codes: list[str]
    theme_names: list[str]


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    uri_path = quote(path.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _assert_required_tables(conn: sqlite3.Connection) -> None:
    missing = [name for name in REQUIRED_PRIOR_TABLES if not _table_exists(conn, name)]
    if missing:
        raise ReplayReadinessError(missing)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except sqlite3.Error:
        return set()


def _first_existing(columns: set[str], names: Iterable[str]) -> str:
    for name in names:
        if name in columns:
            return name
    return ""


def _time_code_query(table_name: str, timestamp_col: str, code_col: str, filters: _ExportFilters) -> tuple[str, tuple[Any, ...]]:
    clauses = [f"substr({timestamp_col}, 1, 10) = ?"]
    params: list[Any] = [filters.trade_date]
    if filters.codes:
        clauses.append(f"{code_col} IN ({','.join('?' for _ in filters.codes)})")
        params.extend(filters.codes)
    if filters.start_time:
        clauses.append(f"substr({timestamp_col}, 12, 8) >= ?")
        params.append(filters.start_time)
    if filters.end_time:
        clauses.append(f"substr({timestamp_col}, 12, 8) <= ?")
        params.append(filters.end_time)
    return (
        f"""
        SELECT * FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY {timestamp_col} ASC
        """,
        tuple(params),
    )


def _tick_from_row(row: dict[str, Any], timestamp_col: str, code_col: str, price_col: str, *, source: str) -> dict[str, Any]:
    return {
        "timestamp": row.get(timestamp_col) or "",
        "code": _clean_code(row.get(code_col)),
        "price": row.get(price_col),
        "change_rate": row.get("change_rate"),
        "cum_volume": row.get("cum_volume") or row.get("volume") or "",
        "trade_value": row.get("trade_value") or row.get("turnover") or "",
        "execution_strength": row.get("execution_strength") or row.get("turnover_strength") or "",
        "best_bid": row.get("best_bid") or row.get("bid") or "",
        "best_ask": row.get("best_ask") or row.get("ask") or "",
        "spread_ticks": row.get("spread_ticks") or "",
        "source": source,
        "row_type": "tick",
    }


def _candidate_event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = _loads(row.get("payload_json"), {})
    metadata = _loads(row.get("metadata_json"), {})
    condition_names = _loads(row.get("condition_names_json"), [])
    theme_ids = _loads(row.get("theme_ids_json"), [])
    return {
        "timestamp": row.get("created_at") or "",
        "code": _clean_code(row.get("code")),
        "name": row.get("name") or "",
        "candidate_id": row.get("candidate_id"),
        "candidate_instance_id": metadata.get("candidate_instance_id") or payload.get("candidate_instance_id") or "",
        "candidate_generation_seq": metadata.get("candidate_generation_seq") or payload.get("candidate_generation_seq") or 0,
        "theme_name": payload.get("theme_name") or (theme_ids[0] if theme_ids else ""),
        "source": row.get("source") or payload.get("source") or "candidate_events",
        "condition_name": payload.get("condition_name") or (condition_names[0] if condition_names else ""),
        "reason": row.get("reason") or payload.get("reason") or row.get("event_type") or "",
        "metadata": {**metadata, "event_type": row.get("event_type"), "payload": payload},
    }


def _theme_snapshot_from_row(row: dict[str, Any]) -> dict[str, Any]:
    details = _loads(row.get("details_json"), {})
    leader = row.get("leader_code") or ""
    return {
        "timestamp": row.get("created_at") or "",
        "theme_name": row.get("theme_name") or row.get("theme_id") or "",
        "theme_score": row.get("theme_score"),
        "breadth_pct": row.get("breadth"),
        "leader_codes": [leader] if leader else [],
        "co_leader_codes": details.get("co_leader_codes") or [],
        "watch_codes": details.get("watch_codes") or [],
        "data_quality_flags": details.get("data_quality_flags") or [],
        "raw": row,
    }


def _market_status_from_row(row: dict[str, Any]) -> dict[str, Any]:
    market = _loads(row.get("market_status_json"), {})
    data_quality = _loads(row.get("data_quality_json"), {})
    timestamp = row.get("calculated_at") or row.get("created_at") or ""
    return {
        "timestamp": timestamp,
        "kospi_return_pct": market.get("kospi_return_pct") or market.get("kospi_return"),
        "kosdaq_return_pct": market.get("kosdaq_return_pct") or market.get("kosdaq_return"),
        "market_status": market.get("market_status") or market.get("status") or "",
        "kospi_market_status": market.get("kospi_market_status") or "",
        "kosdaq_market_status": market.get("kosdaq_market_status") or "",
        "advancers": market.get("advancers"),
        "decliners": market.get("decliners"),
        "data_quality_flags": data_quality.get("flags") or data_quality.get("issues") or [],
        "raw": market,
    }


def _decision_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["reason_codes"] = _loads(payload.pop("reason_codes_json", "[]"), [])
    payload["data_quality_issues"] = _loads(payload.pop("data_quality_issues_json", "[]"), [])
    payload["details"] = _loads(payload.pop("details_json", "{}"), {})
    return payload


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _read_ticks(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _dedupe_rows(rows: list[dict[str, Any]], *, key_fields: Iterable[str]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get(field) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _clean_code(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits and len(digits) <= 6 else text


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text).replace(microsecond=0)
    except ValueError:
        if len(text) == 8 and text.count(":") == 2:
            return datetime.fromisoformat(f"1970-01-01T{text}")
    return datetime.min


def _event_time(event: dict[str, Any]) -> datetime:
    return _parse_dt(event.get("timestamp") or event.get("decision_at") or event.get("created_at"))


def _bundle_price_provider(ticks: list[dict[str, Any]]):
    by_code: dict[str, list[dict[str, Any]]] = {}
    for tick in ticks:
        code = _clean_code(tick.get("code"))
        if not code:
            continue
        by_code.setdefault(code, []).append(tick)
    for values in by_code.values():
        values.sort(key=lambda row: _parse_dt(row.get("timestamp")))

    def provider(decision: dict[str, Any], horizon_sec: int, _now: datetime) -> dict[str, Any]:
        code = _clean_code(decision.get("code"))
        decision_at = _parse_dt(decision.get("decision_at"))
        end_at = decision_at + timedelta(seconds=max(1, int(horizon_sec or 1)))
        samples = []
        for tick in by_code.get(code, []):
            ts = _parse_dt(tick.get("timestamp"))
            if decision_at <= ts <= end_at:
                samples.append({"at": ts.isoformat(timespec="seconds"), "price": tick.get("price"), "source": tick.get("source") or "replay_bundle"})
        return {"source": "replay_bundle_ticks", "samples": samples}

    return provider


def _save_replay_source_metadata(db: TradingDatabase, bundle: StrategyReplayBundle, mode: str) -> None:
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS replay_source_metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    metadata = {
        "bundle_path": str(bundle.path),
        "manifest": bundle.manifest.to_dict(),
        "mode": mode,
        "TRADING_REPLAY_MODE": True,
        "live_order_enabled": False,
        "runtime_allow_live_orders": False,
    }
    with db.conn:
        db.conn.execute(
            """
            INSERT INTO replay_source_metadata(key, value_json, updated_at)
            VALUES('source', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str), datetime.now().isoformat(timespec="seconds")),
        )


def _replay_funnel(decision_summary: dict[str, Any], outcome_summary: dict[str, Any], db: TradingDatabase) -> dict[str, int]:
    decision_funnel = dict(decision_summary.get("funnel") or {})
    return {
        "candidate_detected": int(decision_funnel.get("detected") or 0),
        "theme_mapped": int(decision_funnel.get("detected") or 0),
        "realtime_data_ready": int(decision_funnel.get("evaluated") or 0),
        "gate_evaluated": int(decision_funnel.get("evaluated") or 0),
        "ready": int(decision_funnel.get("ready") or 0),
        "entry_plan_created": int(decision_funnel.get("entry_plan") or 0),
        "order_intent_created": int(decision_funnel.get("order_intent") or 0),
        "virtual_filled": _filled_virtual_order_count(db),
        "position_opened": int(decision_funnel.get("open_position") or _table_count(db, "virtual_positions")),
        "exit_decision_created": int(decision_funnel.get("exit_decision") or 0),
        "position_closed": _closed_position_count(db),
        "outcome_labeled": int(outcome_summary.get("labeled_count") or 0),
    }


def _build_diff_summary(db: TradingDatabase, bundle: StrategyReplayBundle, *, status: str) -> dict[str, Any]:
    bundled = _read_jsonl(bundle.decision_events_path)
    replayed = db.list_strategy_decision_events(trade_date=bundle.manifest.trade_date, limit=10000)
    bundled_by_id = {str(item.get("decision_id") or ""): item for item in bundled}
    diffs: list[dict[str, Any]] = []
    for item in replayed:
        original = bundled_by_id.get(str(item.get("decision_id") or ""))
        if not original:
            continue
        for field in ("gate_status", "action_type", "action_result"):
            if str(original.get(field) or "") != str(item.get(field) or ""):
                diffs.append(
                    {
                        "decision_id": item.get("decision_id"),
                        "candidate_instance_id": item.get("candidate_instance_id"),
                        "field": field,
                        "source": original.get(field),
                        "replay": item.get(field),
                    }
                )
    return {
        "status": "NO_RUNTIME_DIFF" if not diffs else "DIFF_FOUND",
        "partial_replay": status == "PARTIAL_REPLAY",
        "diff_count": len(diffs),
        "examples": diffs[:20],
        "notes": ["full_runtime currently emits PARTIAL_REPLAY unless a safe runtime adapter is available"] if status == "PARTIAL_REPLAY" else [],
    }


def _build_recommendations(
    shadow_summary: dict[str, Any],
    *,
    data_quality: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    low_quality = data_quality.get("status") != "READY" or any("MISSING" in str(warning) for warning in warnings)
    rows: list[dict[str, Any]] = []
    for policy in shadow_summary.get("policy_ranking") or []:
        grade = str(policy.get("recommendation_grade") or "DATA_INSUFFICIENT")
        if low_quality and grade == "STRONG_CANDIDATE":
            grade = "WATCH_CANDIDATE"
        if int(policy.get("total_count") or 0) < 3:
            grade = "DATA_INSUFFICIENT"
        if int(policy.get("false_positive_increase_count") or 0) >= 2:
            grade = "DO_NOT_APPLY"
        if int(policy.get("false_positive_increase_count") or 0) == 1 and grade == "STRONG_CANDIDATE":
            grade = "RISKY_CANDIDATE"
        policy_id = str(policy.get("policy_id") or "").lower()
        policy_name = str(policy.get("policy_name") or "").lower()
        if ("vi" in policy_id or "upper_limit" in policy_id or "vi" in policy_name or "upper" in policy_name) and grade == "STRONG_CANDIDATE":
            grade = "WATCH_CANDIDATE"
        rows.append(
            {
                "policy_id": policy.get("policy_id") or "",
                "policy_name": policy.get("policy_name") or "",
                "changed_decision_count": int(policy.get("changed_decision_count") or 0),
                "ready_delta": int(policy.get("ready_delta") or 0),
                "blocked_delta": -int(policy.get("ready_delta") or 0),
                "estimated_opportunity_loss_reduced_count": int(policy.get("opportunity_loss_reduced_count") or 0),
                "estimated_false_positive_increase_count": int(policy.get("false_positive_increase_count") or 0),
                "risk_block_effective_count": int(policy.get("risk_block_effective_count") or 0),
                "exit_too_late_reduced_count": int(policy.get("exit_too_late_reduced_count") or 0),
                "net_benefit_score": float(policy.get("estimated_net_benefit_score") or 0.0),
                "confidence": float(policy.get("confidence") or 0.0),
                "recommendation_grade": grade,
                "guardrail_notes": [
                    "single_day_replay_is_not_auto_apply_evidence",
                    *("low_replay_data_quality_blocks_strong_candidate" for _ in [0] if low_quality),
                ],
            }
        )
    return rows


def _count_decision_events(db: TradingDatabase, trade_date: str, action_type: str, action_result: str) -> int:
    return db.strategy_decision_event_count(
        trade_date=trade_date,
        action_type=action_type,
        action_result=action_result,
    )


def _count_gate_status(db: TradingDatabase, trade_date: str, gate_status: str) -> int:
    return db.strategy_decision_event_count(trade_date=trade_date, gate_status=gate_status)


def _table_count(db: TradingDatabase, table_name: str) -> int:
    if not _table_exists(db.conn, table_name):
        return 0
    row = db.conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"] or 0) if row else 0


def _filled_virtual_order_count(db: TradingDatabase) -> int:
    if not _table_exists(db.conn, "virtual_orders"):
        return 0
    row = db.conn.execute(
        """
        SELECT COUNT(*) AS count FROM virtual_orders
        WHERE status IN ('FILLED', 'PARTIALLY_FILLED')
        """
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _closed_position_count(db: TradingDatabase) -> int:
    if not _table_exists(db.conn, "virtual_positions"):
        return 0
    columns = _columns(db.conn, "virtual_positions")
    if "closed_at" in columns:
        row = db.conn.execute("SELECT COUNT(*) AS count FROM virtual_positions WHERE closed_at <> ''").fetchone()
    elif "status" in columns:
        row = db.conn.execute("SELECT COUNT(*) AS count FROM virtual_positions WHERE status = 'CLOSED'").fetchone()
    else:
        return 0
    return int(row["count"] or 0) if row else 0


def _safe_unlink_replay_db(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if root_resolved not in resolved.parents and resolved != root_resolved:
        raise ValueError(f"refusing to remove replay db outside {root_resolved}")
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(resolved) + suffix)
        if target.exists():
            target.unlink()


def _run_replay_id(bundle_replay_id: str, mode: str) -> str:
    return f"{bundle_replay_id}_{mode}"


def _replay_db_paths(root: str | Path) -> list[Path]:
    return sorted(Path(root).glob("replay_*.sqlite3"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
