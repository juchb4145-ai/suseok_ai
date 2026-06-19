from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from trading.strategy.market_relative_strength_shadow import ACTION_TYPE


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
    "label_shadow_outcome",
]
