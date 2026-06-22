from __future__ import annotations

import csv
import json
import os
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from trading.strategy.market_relative_strength_shadow import ACTION_TYPE
from trading_app.intraday_outcomes import IntradayOutcomeConfig, IntradayOutcomeLabeler


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "market_relative_strength"
HORIZON_NAMES = {300: "5m", 600: "10m", 1200: "20m"}
GROUP_FIELDS = (
    "shadow_scenario",
    "shadow_variant",
    "market_side",
    "side_market_regime",
    "composite_market_mode",
    "actual_market_action",
    "trade_stock_role",
    "theme_state",
    "price_location",
    "session_phase",
    "relative_strength_band",
    "theme_score_band",
    "data_quality_status",
)


@dataclass(frozen=True)
class MarketRelativeStrengthOutcomeConfig:
    horizons_sec: tuple[int, ...] = (300, 600, 1200)
    edge_mfe_10m_pct: float = 1.5
    positive_return_10m_pct: float = 0.5
    risk_mae_10m_pct: float = -1.0
    severe_risk_mae_10m_pct: float = -1.5
    weak_side_min_labeled_count: int = 30
    weak_side_min_positive_return_rate: float = 0.55
    weak_side_max_shadow_risk_case_rate: float = 0.15
    weak_side_min_avg_mae_10m_pct: float = -1.0
    weak_side_min_avg_mfe_10m_pct: float = 1.5


@dataclass(frozen=True)
class MarketRelativeStrengthOutcomeRuntimeConfig:
    enabled: bool = False
    interval_sec: int = 60
    horizons_sec: tuple[int, ...] = (300, 600, 1200)
    max_batch_size: int = 500
    min_price_samples: int = 2
    force: bool = False

    @classmethod
    def from_env(cls) -> "MarketRelativeStrengthOutcomeRuntimeConfig":
        return cls(
            enabled=_env_bool("TRADING_MARKET_RS_OUTCOME_ENABLED", default=False),
            interval_sec=_env_int("TRADING_MARKET_RS_OUTCOME_INTERVAL_SEC", default=60, minimum=1),
            horizons_sec=_env_horizons("TRADING_MARKET_RS_OUTCOME_HORIZONS_SEC", default=(300, 600, 1200)),
            max_batch_size=_env_int("TRADING_MARKET_RS_OUTCOME_MAX_BATCH_SIZE", default=500, minimum=1),
            min_price_samples=_env_int("TRADING_MARKET_RS_OUTCOME_MIN_PRICE_SAMPLES", default=2, minimum=1),
            force=_env_bool("TRADING_MARKET_RS_OUTCOME_FORCE", default=False),
        )


class MarketRelativeStrengthOutcomeRuntimePipeline:
    """Persist read-only outcome labels for the Market RS shadow stream."""

    def __init__(
        self,
        db: Any,
        *,
        config: MarketRelativeStrengthOutcomeRuntimeConfig | None = None,
        price_provider: Any | None = None,
        clock: Any = datetime.now,
    ) -> None:
        self.db = db
        self.config = config or MarketRelativeStrengthOutcomeRuntimeConfig()
        self.price_provider = price_provider if price_provider is not None else GatewayPriceTickPathProvider(db)
        self.clock = clock
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, Any] = {}

    def run_if_due(self, now: datetime | None = None, **_: Any) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            result = self._disabled_result(current, "DISABLED")
            self.last_result = result
            return result
        if self.last_run_at is not None:
            elapsed = (current - self.last_run_at).total_seconds()
            if elapsed < max(1, int(self.config.interval_sec)):
                result = dict(self.last_result or self._disabled_result(current, "WAIT_INTERVAL"))
                result["status"] = "WAIT_INTERVAL"
                result["calculated_at"] = current.isoformat()
                return result
        result = self.run(current)
        self.last_run_at = current
        self.last_result = result
        return result

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = current.date().isoformat()
        horizons = _normalize_horizons(self.config.horizons_sec)
        due = self._due_decisions(current, trade_date=trade_date, horizons_sec=horizons)
        labeler = IntradayOutcomeLabeler(
            self.db,
            config=IntradayOutcomeConfig(
                enabled=True,
                horizons_sec=horizons,
                min_price_samples=max(1, int(self.config.min_price_samples)),
                max_batch_size=max(1, int(self.config.max_batch_size)),
            ),
            price_provider=self.price_provider,
        )
        outcomes = [
            labeler.build_outcome_for_decision(decision, int(decision.get("horizon_sec") or 0), now=current)
            for decision in due
        ]
        persisted_count = labeler.persist_outcomes(outcomes, force=bool(self.config.force))
        report = MarketRelativeStrengthOutcomeAnalyzer(
            self.db,
            config=MarketRelativeStrengthOutcomeConfig(horizons_sec=horizons),
        ).build_report(trade_date=trade_date, limit=max(10000, int(self.config.max_batch_size) * len(horizons) * 4))
        summary = dict(report.get("summary") or {})
        counts = self._counts(trade_date=trade_date, now=current, horizons_sec=horizons)
        return {
            "enabled": True,
            "status": "OK",
            "trade_date": trade_date,
            "calculated_at": current.isoformat(),
            "generated_at": report.get("generated_at") or current.isoformat(),
            "action_type": ACTION_TYPE,
            "horizons_sec": horizons,
            "due_count": len(due),
            "outcome_count": len(outcomes),
            "persisted_count": int(persisted_count or 0),
            "tracked_event_count": counts["tracked_event_count"],
            "matured_pending_count": counts["matured_pending_count"],
            "persisted_outcome_count": counts["persisted_outcome_count"],
            "report_status": report.get("status") or "NO_DATA",
            "available": bool(report.get("available")),
            "summary": summary,
            "recommendations": dict(report.get("recommendations") or {}),
            "order_intent_allowed": False,
            "dry_run_order_allowed": False,
            "live_order_allowed": False,
            "notes": [
                "analysis_only_market_relative_strength_shadow_outcomes",
                "does_not_create_orders_or_entry_intents",
            ],
        }

    def _due_decisions(self, now: datetime, *, trade_date: str, horizons_sec: tuple[int, ...]) -> list[dict[str, Any]]:
        loader = getattr(self.db, "list_strategy_decision_events_due_for_outcomes", None)
        if not callable(loader):
            return []
        max_batch = max(1, int(self.config.max_batch_size))
        ordered_horizons = sorted(horizons_sec, key=lambda horizon: (abs(int(horizon) - 600), int(horizon)))
        per_horizon = max(1, (max_batch + max(1, len(ordered_horizons)) - 1) // max(1, len(ordered_horizons)))
        due: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for horizon in ordered_horizons:
            rows = list(
                loader(
                    evaluated_at=now.isoformat(),
                    horizons_sec=(horizon,),
                    trade_date=trade_date,
                    action_type=ACTION_TYPE,
                    limit=per_horizon,
                    force=bool(self.config.force),
                )
                or []
            )
            for row in rows:
                key = (str(row.get("decision_id") or ""), int(row.get("horizon_sec") or horizon))
                if key in seen:
                    continue
                seen.add(key)
                due.append(row)
                if len(due) >= max_batch:
                    return due
        return due

    def _pending_due_count(self, now: datetime, *, trade_date: str, horizons_sec: tuple[int, ...]) -> int:
        loader = getattr(self.db, "list_strategy_decision_events_due_for_outcomes", None)
        if not callable(loader):
            return 0
        return len(
            loader(
                evaluated_at=now.isoformat(),
                horizons_sec=horizons_sec,
                trade_date=trade_date,
                action_type=ACTION_TYPE,
                limit=max(1, int(self.config.max_batch_size)),
                force=bool(self.config.force),
            )
            or []
        )

    def _counts(self, *, trade_date: str, now: datetime, horizons_sec: tuple[int, ...]) -> dict[str, int]:
        tracked_event_count = 0
        event_counter = getattr(self.db, "strategy_decision_event_count", None)
        if callable(event_counter):
            tracked_event_count = int(event_counter(trade_date=trade_date, action_type=ACTION_TYPE) or 0)
        persisted_outcome_count = 0
        outcome_counter = getattr(self.db, "strategy_decision_outcome_count", None)
        if callable(outcome_counter):
            persisted_outcome_count = int(outcome_counter(trade_date=trade_date, action_type=ACTION_TYPE) or 0)
        matured_pending_count = 0
        if bool(self.config.force):
            matured_pending_count = self._pending_due_count(now, trade_date=trade_date, horizons_sec=horizons_sec)
        else:
            pending_loader = getattr(self.db, "list_strategy_decision_events_due_for_outcomes", None)
            if callable(pending_loader):
                matured_pending_count = len(
                    pending_loader(
                        evaluated_at=now.isoformat(),
                        horizons_sec=horizons_sec,
                        trade_date=trade_date,
                        action_type=ACTION_TYPE,
                        limit=max(1, int(self.config.max_batch_size)),
                        force=False,
                    )
                    or []
                )
        return {
            "tracked_event_count": tracked_event_count,
            "persisted_outcome_count": persisted_outcome_count,
            "matured_pending_count": matured_pending_count,
        }

    def _disabled_result(self, now: datetime, status: str) -> dict[str, Any]:
        return {
            "enabled": False,
            "status": status,
            "trade_date": now.date().isoformat(),
            "calculated_at": now.isoformat(),
            "action_type": ACTION_TYPE,
            "horizons_sec": _normalize_horizons(self.config.horizons_sec),
            "due_count": 0,
            "outcome_count": 0,
            "persisted_count": 0,
            "tracked_event_count": 0,
            "matured_pending_count": 0,
            "persisted_outcome_count": 0,
            "summary": {},
            "recommendations": {},
            "order_intent_allowed": False,
            "dry_run_order_allowed": False,
            "live_order_allowed": False,
        }


class GatewayPriceTickPathProvider:
    """Fast runtime price path provider backed by persisted gateway ticks."""

    def __init__(self, db: Any, *, max_rows: int = 300, cache_by_code: bool = False) -> None:
        self.db = db
        self.max_rows = max(1, int(max_rows or 300))
        self.cache_by_code = bool(cache_by_code)
        self._code_cache: dict[tuple[str, str], tuple[list[str], list[dict[str, Any]]]] = {}

    def __call__(self, decision: dict[str, Any], horizon_sec: int, now: datetime) -> dict[str, Any]:
        code = _normalize_code(decision.get("code"))
        decision_at = _parse_dt(decision.get("decision_at"))
        if not code or decision_at is None or not hasattr(self.db, "conn"):
            return {"source": "gateway_price_ticks", "samples": []}
        horizon_at = min(decision_at + timedelta(seconds=max(1, int(horizon_sec or 1))), now)
        if self.cache_by_code:
            rows = self._cached_rows(decision_at.date().isoformat(), code, decision_at, horizon_at)
            return {"source": "gateway_price_ticks", "samples": self._samples_from_rows(rows)}
        try:
            rows = self.db.conn.execute(
                """
                SELECT timestamp, price, source
                FROM gateway_price_ticks
                WHERE trade_date = ? AND code = ? AND timestamp >= ? AND timestamp <= ? AND price IS NOT NULL
                ORDER BY timestamp ASC, id ASC
                LIMIT ?
                """,
                (
                    decision_at.date().isoformat(),
                    code,
                    decision_at.isoformat(timespec="seconds"),
                    horizon_at.isoformat(timespec="seconds"),
                    self.max_rows,
                ),
            ).fetchall()
        except Exception:
            return {"source": "gateway_price_ticks", "samples": []}
        return {"source": "gateway_price_ticks", "samples": self._samples_from_rows([dict(row) for row in rows])}

    def _cached_rows(self, trade_date: str, code: str, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        key = (trade_date, code)
        cached = self._code_cache.get(key)
        if cached is None:
            try:
                rows = [
                    dict(row)
                    for row in self.db.conn.execute(
                        """
                        SELECT timestamp, price, source
                        FROM gateway_price_ticks
                        WHERE trade_date = ? AND code = ? AND price IS NOT NULL
                        ORDER BY timestamp ASC, id ASC
                        """,
                        (trade_date, code),
                    ).fetchall()
                ]
            except Exception:
                rows = []
            cached = ([str(row.get("timestamp") or "") for row in rows], rows)
            self._code_cache[key] = cached
        timestamps, rows = cached
        start = bisect_left(timestamps, start_at.isoformat(timespec="seconds"))
        end = bisect_right(timestamps, end_at.isoformat(timespec="seconds"))
        return rows[start:end][: self.max_rows]

    def _samples_from_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        seen: set[tuple[str, float]] = set()
        for row in rows:
            price = _positive_float(row.get("price"))
            at = str(row.get("timestamp") or "")
            if not price or not at:
                continue
            key = (at, price)
            if key in seen:
                continue
            seen.add(key)
            samples.append({"at": at, "price": price, "source": row.get("source") or "gateway_price_ticks"})
        return samples


class MarketRelativeStrengthOutcomeAnalyzer:
    def __init__(
        self,
        db: Any,
        *,
        config: MarketRelativeStrengthOutcomeConfig | None = None,
        report_root: Path | None = None,
    ) -> None:
        self.db = db
        self.config = config or MarketRelativeStrengthOutcomeConfig()
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def build_report(
        self,
        *,
        trade_date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        scenario: str | None = None,
        market_side: str | None = None,
        limit: int = 10000,
        source_items: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if source_items is None:
            outcomes = self._load_outcomes(trade_date=trade_date, limit=limit)
        else:
            outcomes = [dict(item or {}) for item in source_items]
        rows = self._rows_from_outcomes(outcomes)
        from_date_filter = str(from_date or "").strip()
        to_date_filter = str(to_date or "").strip()
        scenario_filter = str(scenario or "").strip().upper()
        market_side_filter = str(market_side or "").strip().upper()
        if from_date_filter:
            rows = [row for row in rows if str(row.get("trade_date") or "") >= from_date_filter]
        if to_date_filter:
            rows = [row for row in rows if str(row.get("trade_date") or "") <= to_date_filter]
        if scenario_filter:
            rows = [row for row in rows if str(row.get("shadow_scenario") or "").upper() == scenario_filter]
        if market_side_filter:
            rows = [row for row in rows if str(row.get("market_side") or "").upper() == market_side_filter]
        if trade_date is None:
            trade_date = _latest_trade_date(rows)
        group_summaries = {
            field: self._group_summary(rows, field)
            for field in GROUP_FIELDS
        }
        summary = self._summary(rows)
        report = {
            "available": bool(rows),
            "status": "READY" if rows else "NO_DATA",
            "report_name": "split_market_relative_strength_outcomes",
            "report_id": f"split_market_relative_strength_outcomes:{trade_date or 'all'}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "trade_date": trade_date or "",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": asdict(self.config),
            "filters": {
                "from_date": from_date_filter,
                "to_date": to_date_filter,
                "scenario": scenario_filter,
                "market_side": market_side_filter,
            },
            "summary": summary,
            "groups": group_summaries,
            "recommendations": self._recommendations(rows, summary),
            "rows": rows,
            "notes": [
                "read_only_shadow_validation_report",
                "does_not_change_entry_status_or_order_intents",
                "risk_off_side_diagnostic_never_promotes_to_auto_entry",
            ],
        }
        return report

    def export_json(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "trade_date",
            "calculated_at",
            "code",
            "name",
            "market_side",
            "shadow_scenario",
            "shadow_variant",
            "shadow_status",
            "actual_market_action",
            "actual_entry_status",
            "relative_strength_vs_index_pct",
            "price_location",
            "mfe_5m",
            "mae_5m",
            "return_5m",
            "mfe_10m",
            "mae_10m",
            "return_10m",
            "mfe_20m",
            "mae_20m",
            "return_20m",
            "shadow_outcome_label",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in report.get("rows") or []:
                writer.writerow({column: _csv_value(row.get(column)) for column in columns})
        return path

    def export_markdown(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        lines = [
            f"# Split Market Relative Strength Outcomes ({report.get('trade_date') or 'all'})",
            "",
            "This report is read-only shadow validation. It does not enable orders, alter EntryDecision, or change position sizing.",
            "",
            "## Summary",
            f"- Shadow candidates: {summary.get('shadow_candidate_count', 0)}",
            f"- Healthy-side reduced: {summary.get('healthy_side_reduced_count', 0)}",
            f"- WEAK-side strict: {summary.get('weak_side_shadow_candidate_count', 0)}",
            f"- RISK_OFF diagnostic: {summary.get('risk_off_side_diagnostic_count', 0)}",
            f"- Systemic excluded: {summary.get('systemic_excluded_count', 0)}",
            f"- 10m avg MFE/MAE: {summary.get('avg_mfe_10m')} / {summary.get('avg_mae_10m')}",
            f"- 10m edge/risk rate: {summary.get('shadow_edge_rate_10m')} / {summary.get('shadow_risk_case_rate_10m')}",
            f"- Recommendation: {dict(report.get('recommendations') or {}).get('current_recommendation', 'NO_DATA')}",
            "",
            "## By Scenario",
            "| scenario | candidates | labeled | avg_mfe_10m | avg_mae_10m | edge_rate | risk_rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in dict(report.get("groups") or {}).get("shadow_scenario", []):
            lines.append(
                f"| {row.get('key')} | {row.get('candidate_count')} | {row.get('labeled_count')} | "
                f"{row.get('avg_mfe_10m')} | {row.get('avg_mae_10m')} | {row.get('shadow_edge_rate')} | {row.get('shadow_risk_case_rate')} |"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict[str, Any], *, report_dir: Path | None = None, stem: str | None = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root / str(report.get("trade_date") or "all")
        clean_date = str(report.get("trade_date") or "all").replace("-", "")
        stem = stem or f"split_market_relative_strength_outcomes_{clean_date}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def _load_outcomes(self, *, trade_date: str | None, limit: int) -> list[dict[str, Any]]:
        loader = getattr(self.db, "list_strategy_decision_outcomes", None)
        if not callable(loader):
            return []
        return list(
            loader(
                trade_date=trade_date,
                action_type=ACTION_TYPE,
                limit=max(1, int(limit or 10000)),
            )
            or []
        )

    def _rows_from_outcomes(self, outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_decision: dict[str, dict[str, Any]] = {}
        for outcome in outcomes:
            decision_id = str(outcome.get("decision_id") or "")
            if not decision_id:
                continue
            row = by_decision.setdefault(decision_id, self._base_row(outcome))
            horizon_name = HORIZON_NAMES.get(int(outcome.get("horizon_sec") or 0))
            if horizon_name:
                row[f"mfe_{horizon_name}"] = _round(outcome.get("max_return_pct"))
                row[f"mae_{horizon_name}"] = _round(outcome.get("max_drawdown_pct"))
                row[f"return_{horizon_name}"] = _round(outcome.get("current_return_pct"))
                row[f"sample_count_{horizon_name}"] = _sample_count(outcome)
                row[f"data_status_{horizon_name}"] = str(outcome.get("data_status") or "")
        rows = list(by_decision.values())
        for row in rows:
            row["shadow_outcome_label"] = label_shadow_outcome(row, self.config)
            row["theme_score_band"] = _theme_score_band(row.get("theme_score"))
        rows.sort(key=lambda item: (str(item.get("trade_date") or ""), str(item.get("calculated_at") or ""), str(item.get("code") or "")))
        return rows

    def _base_row(self, outcome: dict[str, Any]) -> dict[str, Any]:
        details = dict(outcome.get("decision_details") or {})
        return {
            "decision_id": str(outcome.get("decision_id") or ""),
            "trade_date": str(outcome.get("trade_date") or ""),
            "calculated_at": str(details.get("calculated_at") or outcome.get("decision_at") or ""),
            "code": str(outcome.get("code") or ""),
            "name": details.get("name") or outcome.get("name") or "",
            "market_side": details.get("market_side") or "",
            "side_market_regime": details.get("side_market_regime") or "",
            "counterpart_market_regime": details.get("counterpart_market_regime") or "",
            "composite_market_mode": details.get("composite_market_mode") or "",
            "systemic_risk_off": bool(details.get("systemic_risk_off")),
            "actual_market_action": details.get("actual_market_action") or "",
            "actual_entry_status": details.get("actual_entry_status") or "",
            "actual_ready_allowed": bool(details.get("actual_ready_allowed")),
            "shadow_scenario": details.get("shadow_scenario") or "",
            "shadow_variant": details.get("shadow_variant") or "",
            "shadow_status": details.get("shadow_status") or "",
            "counterfactual_action": details.get("counterfactual_action") or "",
            "counterfactual_position_size_multiplier_hint": _round(details.get("counterfactual_position_size_multiplier_hint")),
            "trade_stock_role": details.get("trade_stock_role") or "",
            "theme_id": details.get("theme_id") or "",
            "theme_name": details.get("theme_name") or outcome.get("theme_name") or "",
            "theme_state": details.get("theme_state") or "",
            "theme_score": _round(details.get("theme_score")),
            "persistence_count": int(details.get("persistence_count") or 0),
            "relative_strength_vs_index_pct": _round(details.get("relative_strength_vs_index_pct")),
            "relative_strength_band": details.get("relative_strength_band") or _relative_strength_band(details.get("relative_strength_vs_index_pct")),
            "price_location": details.get("price_location") or "",
            "session_phase": dict(details.get("feature_snapshot") or {}).get("context", {}).get("session_phase", ""),
            "data_quality_status": details.get("data_quality_status") or "",
            "reason_codes": list(details.get("reason_codes") or []),
            "reject_reason_codes": list(details.get("reject_reason_codes") or []),
        }

    def _summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [row for row in rows if _is_labeled(row)]
        edge = [row for row in labeled if row.get("shadow_outcome_label") == "SHADOW_EDGE_CANDIDATE"]
        risk = [row for row in labeled if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE"]
        return {
            "candidate_count": len(rows),
            "labeled_count": len(labeled),
            "insufficient_count": len([row for row in rows if row.get("shadow_outcome_label") == "SHADOW_INSUFFICIENT_DATA"]),
            "shadow_candidate_count": sum(1 for row in rows if row.get("shadow_status") == "SHADOW_CANDIDATE"),
            "healthy_side_reduced_count": sum(1 for row in rows if row.get("shadow_scenario") == "HEALTHY_SIDE_REDUCED"),
            "weak_side_shadow_candidate_count": sum(1 for row in rows if row.get("shadow_scenario") == "WEAK_SIDE_STRICT_SHADOW" and row.get("shadow_status") == "SHADOW_CANDIDATE"),
            "risk_off_side_diagnostic_count": sum(1 for row in rows if row.get("shadow_scenario") == "RISK_OFF_SIDE_DIAGNOSTIC"),
            "systemic_excluded_count": sum(1 for row in rows if row.get("shadow_scenario") == "SYSTEMIC_RISK_EXCLUDED"),
            "market_side_unresolved_count": sum(1 for row in rows if row.get("shadow_scenario") == "DATA_WAIT_EXCLUDED"),
            "split_market_false_negative_candidate_count": len(edge),
            "missed_opportunity_count": len(edge),
            "good_block_count": sum(1 for row in rows if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE" and row.get("actual_market_action") in {"WAIT_MARKET", "BLOCK_NEW_ENTRY"}),
            "avg_mfe_10m": _avg(row.get("mfe_10m") for row in labeled),
            "avg_mae_10m": _avg(row.get("mae_10m") for row in labeled),
            "avg_return_10m": _avg(row.get("return_10m") for row in labeled),
            "positive_return_rate_10m": _rate((row for row in labeled if _float(row.get("return_10m")) > 0.0), labeled),
            "shadow_edge_rate_10m": _rate(edge, labeled),
            "shadow_risk_case_rate_10m": _rate(risk, labeled),
            "severe_risk_rate_10m": _rate((row for row in labeled if _float(row.get("mae_10m")) <= self.config.severe_risk_mae_10m_pct), labeled),
            "data_coverage_rate": _rate(labeled, rows),
        }

    def _group_summary(self, rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(field) or "UNKNOWN")].append(row)
        return [
            {"key": key, **self._group_metrics(items)}
            for key, items in sorted(grouped.items(), key=lambda item: item[0])
        ]

    def _group_metrics(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [row for row in rows if _is_labeled(row)]
        return {
            "candidate_count": len(rows),
            "labeled_count": len(labeled),
            "insufficient_count": len(rows) - len(labeled),
            "avg_mfe_5m": _avg(row.get("mfe_5m") for row in labeled),
            "median_mfe_5m": _median(row.get("mfe_5m") for row in labeled),
            "avg_mae_5m": _avg(row.get("mae_5m") for row in labeled),
            "median_mae_5m": _median(row.get("mae_5m") for row in labeled),
            "avg_return_5m": _avg(row.get("return_5m") for row in labeled),
            "median_return_5m": _median(row.get("return_5m") for row in labeled),
            "avg_mfe_10m": _avg(row.get("mfe_10m") for row in labeled),
            "median_mfe_10m": _median(row.get("mfe_10m") for row in labeled),
            "avg_mae_10m": _avg(row.get("mae_10m") for row in labeled),
            "median_mae_10m": _median(row.get("mae_10m") for row in labeled),
            "avg_return_10m": _avg(row.get("return_10m") for row in labeled),
            "median_return_10m": _median(row.get("return_10m") for row in labeled),
            "avg_mfe_20m": _avg(row.get("mfe_20m") for row in labeled),
            "median_mfe_20m": _median(row.get("mfe_20m") for row in labeled),
            "avg_mae_20m": _avg(row.get("mae_20m") for row in labeled),
            "median_mae_20m": _median(row.get("mae_20m") for row in labeled),
            "avg_return_20m": _avg(row.get("return_20m") for row in labeled),
            "median_return_20m": _median(row.get("return_20m") for row in labeled),
            "positive_return_rate": _rate((row for row in labeled if _float(row.get("return_10m")) > 0.0), labeled),
            "shadow_edge_rate": _rate((row for row in labeled if row.get("shadow_outcome_label") == "SHADOW_EDGE_CANDIDATE"), labeled),
            "shadow_risk_case_rate": _rate((row for row in labeled if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE"), labeled),
            "severe_risk_rate": _rate((row for row in labeled if _float(row.get("mae_10m")) <= self.config.severe_risk_mae_10m_pct), labeled),
            "data_coverage_rate": _rate(labeled, rows),
        }

    def _recommendations(self, rows: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
        weak_rows = [row for row in rows if row.get("shadow_scenario") == "WEAK_SIDE_STRICT_SHADOW" and row.get("shadow_status") == "SHADOW_CANDIDATE"]
        weak_labeled = [row for row in weak_rows if _is_labeled(row)]
        if not rows:
            current = "NO_DATA"
        elif len(weak_labeled) < self.config.weak_side_min_labeled_count:
            current = "INSUFFICIENT_SAMPLE"
        elif (
            _rate((row for row in weak_labeled if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE"), weak_labeled) > 0.3
            or _avg(row.get("mae_10m") for row in weak_labeled) < -1.5
        ):
            current = "DO_NOT_PROMOTE"
        elif (
            _rate((row for row in weak_labeled if _float(row.get("return_10m")) > 0.0), weak_labeled) >= self.config.weak_side_min_positive_return_rate
            and _rate((row for row in weak_labeled if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE"), weak_labeled) <= self.config.weak_side_max_shadow_risk_case_rate
            and _avg(row.get("mae_10m") for row in weak_labeled) >= self.config.weak_side_min_avg_mae_10m_pct
            and _avg(row.get("mfe_10m") for row in weak_labeled) >= self.config.weak_side_min_avg_mfe_10m_pct
            and not _has_concentration_dominance(weak_labeled)
            and _sample_distribution_ok(weak_labeled)
        ):
            current = "REVIEW_WEAK_SIDE_SMALL_CANARY"
        else:
            current = "WATCH_MORE"
        return {
            "current_recommendation": current,
            "healthy_side_reduced": "REVIEW_HEALTHY_SIDE_MULTIPLIER_LATER" if summary.get("healthy_side_reduced_count") else "NO_DATA",
            "weak_side": current,
            "risk_off_side": "RISK_OFF_OBSERVE_ONLY_NO_PROMOTION" if summary.get("risk_off_side_diagnostic_count") else "NO_DATA",
            "weak_side_checks": {
                "labeled_count": len(weak_labeled),
                "positive_return_rate_10m": _rate((row for row in weak_labeled if _float(row.get("return_10m")) > 0.0), weak_labeled),
                "shadow_risk_case_rate_10m": _rate((row for row in weak_labeled if row.get("shadow_outcome_label") == "SHADOW_RISK_CASE"), weak_labeled),
                "avg_mae_10m": _avg(row.get("mae_10m") for row in weak_labeled),
                "avg_mfe_10m": _avg(row.get("mfe_10m") for row in weak_labeled),
                "concentration_dominance": _has_concentration_dominance(weak_labeled),
                "sample_distribution_ok": _sample_distribution_ok(weak_labeled),
            },
            "notes": [
                "automatic_policy_change_forbidden",
                "risk_off_side_never_promotes_to_entry",
                "healthy_side_multiplier_not_auto_adjusted",
            ],
        }


def label_shadow_outcome(row: dict[str, Any], config: MarketRelativeStrengthOutcomeConfig | None = None) -> str:
    config = config or MarketRelativeStrengthOutcomeConfig()
    if row.get("data_status_10m") == "INSUFFICIENT" or row.get("mfe_10m") in (None, ""):
        return "SHADOW_INSUFFICIENT_DATA"
    mfe = _float(row.get("mfe_10m"))
    mae = _float(row.get("mae_10m"))
    ret = _float(row.get("return_10m"))
    if mfe >= config.edge_mfe_10m_pct and ret >= config.positive_return_10m_pct and mae > config.risk_mae_10m_pct:
        return "SHADOW_EDGE_CANDIDATE"
    if mae <= config.risk_mae_10m_pct or ret < 0:
        return "SHADOW_RISK_CASE"
    return "SHADOW_NEUTRAL"


def _is_labeled(row: dict[str, Any]) -> bool:
    return row.get("shadow_outcome_label") not in {"", None, "SHADOW_INSUFFICIENT_DATA"}


def _sample_count(outcome: dict[str, Any]) -> int:
    details = dict(outcome.get("details") or {})
    metrics = dict(details.get("metrics") or {})
    return int(metrics.get("sample_count") or details.get("sample_count") or 0)


def _latest_trade_date(rows: list[dict[str, Any]]) -> str:
    values = [str(row.get("trade_date") or "") for row in rows if row.get("trade_date")]
    return max(values) if values else ""


def _theme_score_band(value: Any) -> str:
    number = _float(value)
    if number >= 85:
        return "GE_85"
    if number >= 70:
        return "70_TO_85"
    if number > 0:
        return "LT_70"
    return "UNKNOWN"


def _relative_strength_band(value: Any) -> str:
    number = _float(value)
    if number < 2:
        return "LT_2"
    if number < 4:
        return "2_TO_4"
    if number < 6:
        return "4_TO_6"
    return "GE_6"


def _avg(values: Iterable[Any]) -> float:
    items = [_float(value) for value in values if value not in (None, "")]
    return _round(mean(items)) if items else 0.0


def _median(values: Iterable[Any]) -> float:
    items = [_float(value) for value in values if value not in (None, "")]
    return _round(median(items)) if items else 0.0


def _rate(numerator: Iterable[Any], denominator: Iterable[Any]) -> float:
    den = len(list(denominator))
    if den <= 0:
        return 0.0
    return _round(len(list(numerator)) / den)


def _has_concentration_dominance(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    for field in ("code", "theme_name"):
        counts = Counter(str(row.get(field) or "") for row in rows)
        if counts and counts.most_common(1)[0][1] / len(rows) > 0.6:
            return True
    return False


def _sample_distribution_ok(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 1:
        return False
    codes = {str(row.get("code") or "") for row in rows if row.get("code")}
    trade_dates = {str(row.get("trade_date") or "") for row in rows if row.get("trade_date")}
    if len(rows) >= 30:
        return len(codes) >= 5 and len(trade_dates) >= 2
    return len(codes) >= 1 and len(trade_dates) >= 1


def _normalize_horizons(values: Iterable[Any]) -> tuple[int, ...]:
    horizons: set[int] = set()
    for value in values:
        try:
            horizon = int(value or 0)
        except (TypeError, ValueError):
            continue
        if horizon > 0:
            horizons.add(horizon)
    return tuple(sorted(horizons) or (300, 600, 1200))


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, *, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, int(default))
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _env_horizons(name: str, *, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None:
        return _normalize_horizons(default)
    parts = str(raw).replace(";", ",").split(",")
    values: list[int] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            continue
    return _normalize_horizons(values or default)


def _normalize_code(value: Any) -> str:
    text = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if not text:
        return ""
    return text[-6:].zfill(6)


def _positive_float(value: Any) -> float:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _round(value: Any, digits: int = 4) -> float:
    return round(_float(value), digits)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


__all__ = [
    "MarketRelativeStrengthOutcomeAnalyzer",
    "MarketRelativeStrengthOutcomeConfig",
    "MarketRelativeStrengthOutcomeRuntimeConfig",
    "MarketRelativeStrengthOutcomeRuntimePipeline",
    "GatewayPriceTickPathProvider",
    "label_shadow_outcome",
]
