from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Optional

from storage.db import TradingDatabase


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "market_gate_review"

MARKET_WAIT_STATUSES = {
    "WAIT_MARKET_CONFIRMATION_PENDING",
    "WAIT_CANDIDATE_MARKET_WEAK",
    "WAIT_CANDIDATE_MARKET_RISK_OFF",
    "WAIT_MARKET_RECOVERY_PENDING",
    "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK",
}
RESTORE_RESET_TRANSITION_TYPES = {
    "RESET_FORCE",
    "RESET_ON_TRADE_DATE_CHANGE",
    "RESET_ON_SESSION_BOUNDARY",
    "RESET_ON_MARKET_CLOSE",
    "RESET_STALE",
    "RESET_EXPIRED",
    "STATE_RESET",
    "SCHEDULE_UNKNOWN_CONSERVATIVE",
}
SESSION_RESET_TRANSITION_TYPES = {
    "RESET_ON_TRADE_DATE_CHANGE",
    "RESET_ON_SESSION_BOUNDARY",
    "RESET_ON_MARKET_CLOSE",
    "SESSION_CLOSE",
    "SCHEDULE_UNKNOWN_CONSERVATIVE",
}
DEFAULT_MAX_RESTORE_AGE_SEC_REGULAR = 900

REVIEW_COLUMNS = [
    "trade_date",
    "cycle_id",
    "code",
    "name",
    "candidate_instance_id",
    "attribution_confidence",
    "candidate_market",
    "theme_name",
    "theme_score",
    "stock_role",
    "price_location",
    "final_status",
    "display_status",
    "normalized_status",
    "strategy_eligible",
    "order_eligibility",
    "entry_profile",
    "ready_type",
    "chase_risk",
    "chase_risk_reason",
    "late_chase_level",
    "late_chase_score",
    "late_chase_block_type",
    "late_chase_temp_wait",
    "late_chase_recoverable",
    "late_chase_recheck_after_sec",
    "price_location_block_reason",
    "support_source",
    "support_price",
    "support_ready",
    "support_ready_reason",
    "latest_tick_ready",
    "latest_tick_age_sec",
    "base_line_120_ready",
    "base_line_120_candle_count",
    "vwap_ready",
    "recent_support_ready",
    "market_raw_status",
    "market_confirmed_status",
    "market_previous_confirmed_status",
    "market_confirmation_pending",
    "market_recovery_pending",
    "market_weak_consecutive_cycles",
    "market_risk_off_consecutive_cycles",
    "market_healthy_consecutive_cycles",
    "market_wait_reason",
    "market_wait_started_at",
    "market_wait_cycle_id",
    "market_wait_recheck_after_sec",
    "market_wait_recovered_at",
    "market_wait_cycles_to_recover",
    "market_confirmation_state_source",
    "market_confirmation_state_restored",
    "market_confirmation_state_persisted",
    "market_confirmation_state_age_sec",
    "market_confirmation_state_max_restore_age_sec",
    "market_confirmation_state_restore_reason",
    "market_confirmation_state_reset_reason",
    "market_session_id",
    "market_session_type",
    "market_trade_date",
    "market_restore_allowed",
    "market_reset_required",
    "market_side_breadth_pct",
    "market_side_index_return_pct",
    "market_side_turnover_weighted_return_pct",
    "market_side_breadth_source",
    "market_side_breadth_trust_level",
    "market_side_breadth_gate_usable",
    "market_side_source_conflict",
    "market_side_source_conflict_delta",
    "market_side_valid_quote_ratio",
    "market_side_sample_count",
    "entry_plan_created",
    "diagnostic_only",
    "submittable",
    "blocked_reason",
    "blocked_reason_codes",
    "runtime_order_intent_created",
    "virtual_order_created",
    "live_order_enabled",
    "live_order_guard_passed",
    "false_block_candidate",
    "false_block_reason",
    "post_block_return_3m_pct",
    "post_block_return_5m_pct",
    "post_block_return_10m_pct",
    "post_block_return_30m_pct",
    "max_favorable_excursion_after_block_pct",
    "max_adverse_excursion_after_block_pct",
    "review_reason_codes",
]


class MarketGateReviewAnalyzer:
    def __init__(self, db: TradingDatabase, *, report_root: Optional[Path] = None) -> None:
        self.db = db
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def build_report(self, *, trade_date: str | None = None, limit: int = 1000) -> dict[str, Any]:
        candidates = self.db.list_candidates(trade_date=trade_date)
        rows: list[dict[str, Any]] = []
        for candidate in candidates[: max(1, int(limit or 1000))]:
            rows.extend(self._candidate_rows(candidate))
        if trade_date is None:
            trade_date = _latest_trade_date(rows)
        transitions = self.db.list_market_side_confirmation_transitions(trade_date=trade_date or "", limit=max(100, int(limit or 1000)))
        summary = self._summary(rows)
        transition_summary = self._transition_summary(transitions)
        restore_age = self._restore_age_summary(rows, transitions)
        session_reset = self._session_reset_summary(rows, transitions)
        false_block = self._false_block_summary(rows)
        report = {
            "report_id": f"market_gate_review:{trade_date or 'all'}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "trade_date": trade_date or "",
            "status": "READY",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                **summary,
                **restore_age,
                **session_reset,
                **false_block,
            },
            "by_market_side": self._by_market_side(rows),
            "transition_summary": transition_summary,
            "confirmation_pending_summary": self._confirmation_pending_summary(rows, transitions),
            "recovery_pending_summary": self._recovery_pending_summary(rows, transitions),
            "source_conflict_summary": self._source_conflict_summary(rows, transitions),
            "rows": rows,
            "notes": [
                "read_only_observability_report",
                "does_not_modify_gate_thresholds_or_order_logic",
                "false_block_candidate_is_opportunity_review_not_confirmed_pnl",
            ],
        }
        return report

    def export_json(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
            writer.writeheader()
            for row in report.get("rows") or []:
                writer.writerow({column: _csv_value(row.get(column)) for column in REVIEW_COLUMNS})
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        transition = dict(report.get("transition_summary") or {})
        lines = [
            f"# Market Gate Transition Review ({report.get('trade_date') or 'all'})",
            "",
            "This is a read-only observability report. It does not change buy logic, thresholds, or live order settings.",
            "",
            "## Summary",
            f"- total_candidates_seen: {summary.get('total_candidates_seen', 0)}",
            f"- total_market_wait_count: {summary.get('total_market_wait_count', 0)}",
            f"- market_wait_recovered_count: {summary.get('market_wait_recovered_count', 0)}",
            f"- market_wait_recovery_ratio: {summary.get('market_wait_recovery_ratio', 0)}",
            f"- false_block_candidate_count: {summary.get('false_block_candidate_count', 0)}",
            f"- restore_age_sec_p90: {summary.get('restore_age_sec_p90')}",
            f"- session_reset_count: {summary.get('session_reset_count', 0)}",
            "",
            "## Transition Summary",
            f"- transition_count: {transition.get('transition_count', 0)}",
            f"- market_wait_confirmation_pending_count: {transition.get('market_wait_confirmation_pending_count', 0)}",
            f"- market_wait_confirmed_weak_count: {transition.get('market_wait_confirmed_weak_count', 0)}",
            f"- market_wait_recovered_count: {transition.get('market_wait_recovered_count', 0)}",
            f"- source_conflict_count: {transition.get('source_conflict_count', 0)}",
            "",
            "## Notes",
            "- false_block_candidate means opportunity-review candidate, not confirmed strategy error.",
            "- Missing price data leaves return fields blank and adds MARKET_GATE_REVIEW_PRICE_DATA_MISSING.",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict, *, report_dir: Path | None = None, stem: str | None = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root
        stem = stem or f"market_gate_review_{report.get('trade_date') or 'all'}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def export_report(self, report: dict, *, fmt: str = "json") -> dict[str, str]:
        normalized = "md" if fmt == "markdown" else str(fmt or "json").lower()
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        target = self.report_root / trade_date
        stem = f"market_gate_review_{trade_date}_{datetime.now().strftime('%H%M%S')}"
        if normalized == "all":
            return self.export_all(report, report_dir=target, stem=stem)
        if normalized == "json":
            return {"json": str(self.export_json(report, target / f"{stem}.json"))}
        if normalized == "csv":
            return {"csv": str(self.export_csv(report, target / f"{stem}.csv"))}
        if normalized == "md":
            return {"md": str(self.export_markdown(report, target / f"{stem}.md"))}
        return {"json": str(self.export_json(report, target / f"{stem}.json"))}

    def _candidate_rows(self, candidate: Any) -> list[dict[str, Any]]:
        metadata = dict(getattr(candidate, "metadata", {}) or {})
        gate_results = dict(metadata.get("gate_results_by_theme") or {})
        if not gate_results:
            return []
        rows = []
        entry_plan_created = bool(getattr(candidate, "id", None) and self.db.list_entry_plans(candidate.id))
        virtual_order_created = bool(getattr(candidate, "id", None) and self.db.list_virtual_orders(candidate.id))
        runtime_intent_created = bool(getattr(candidate, "id", None) and self.db.list_runtime_order_intents(candidate_id=candidate.id))
        for theme_id, details in gate_results.items():
            details = dict(details or {})
            row = self._row_from_details(candidate, str(theme_id), details)
            row["entry_plan_created"] = entry_plan_created or bool(row.get("entry_plan_created"))
            row["virtual_order_created"] = virtual_order_created or bool(row.get("virtual_order_created"))
            row["runtime_order_intent_created"] = runtime_intent_created or bool(row.get("runtime_order_intent_created"))
            rows.append(row)
        return rows

    def _row_from_details(self, candidate: Any, theme_id: str, details: dict[str, Any]) -> dict[str, Any]:
        candidate_instance_id = str(details.get("candidate_instance_id") or dict(getattr(candidate, "metadata", {}) or {}).get("candidate_instance_id") or "")
        normalized = str(details.get("normalized_status") or _normalize_display_status(details))
        reason_codes = list(details.get("reason_codes") or [])
        review_codes = ["MARKET_GATE_REVIEW_ROW_CREATED", "DISPLAY_STATUS_NORMALIZED"]
        attribution = "HIGH" if candidate_instance_id else "LOW"
        if attribution == "LOW":
            review_codes.append("MARKET_GATE_REVIEW_ATTRIBUTION_LOW_CONFIDENCE")
        price_fields = _price_observation_fields(details)
        if price_fields["price_data_missing"]:
            review_codes.append("MARKET_GATE_REVIEW_PRICE_DATA_MISSING")
        false_block = _is_false_block_candidate(normalized, details)
        if false_block:
            review_codes.append("MARKET_GATE_REVIEW_FALSE_BLOCK_CANDIDATE")
        if details.get("market_confirmation_state_restored"):
            review_codes.append("MARKET_GATE_REVIEW_RESTORED_STATE_ANALYZED")
        if details.get("market_reset_required") or details.get("market_confirmation_state_reset_reason"):
            review_codes.append("MARKET_GATE_REVIEW_SESSION_RESET_ANALYZED")
        return {
            "trade_date": getattr(candidate, "trade_date", ""),
            "cycle_id": details.get("market_side_cycle_id") or details.get("decision_cycle_id") or details.get("evaluated_at") or "",
            "code": getattr(candidate, "code", ""),
            "name": getattr(candidate, "name", ""),
            "candidate_instance_id": candidate_instance_id,
            "attribution_confidence": attribution,
            "candidate_market": details.get("candidate_market") or getattr(candidate, "market", ""),
            "theme_name": details.get("theme_name") or theme_id,
            "theme_score": details.get("theme_score", details.get("dynamic_theme_score")),
            "stock_role": details.get("stock_role", ""),
            "price_location": details.get("price_location_status", ""),
            "final_status": details.get("final_gate_status") or details.get("sub_status") or "",
            "display_status": details.get("display_status") or normalized,
            "normalized_status": normalized,
            "strategy_eligible": bool(details.get("strategy_eligible")),
            "order_eligibility": details.get("order_eligibility", ""),
            "entry_profile": details.get("profile") or details.get("entry_profile") or "",
            "ready_type": details.get("ready_type", ""),
            "chase_risk": bool(details.get("chase_risk")),
            "chase_risk_reason": details.get("chase_risk_reason", ""),
            "late_chase_level": details.get("late_chase_level", ""),
            "late_chase_score": details.get("late_chase_score"),
            "late_chase_block_type": details.get("late_chase_block_type", ""),
            "late_chase_temp_wait": bool(details.get("late_chase_temp_wait") or normalized == "LATE_CHASE_TEMP_WAIT"),
            "late_chase_recoverable": bool(details.get("late_chase_recoverable")),
            "late_chase_recheck_after_sec": details.get("late_chase_recheck_after_sec", 0),
            "price_location_block_reason": details.get("price_location_block_reason", ""),
            "support_source": details.get("selected_support_source") or details.get("nearest_support") or "",
            "support_price": details.get("selected_support_price") or details.get("nearest_support_price") or details.get("support_price"),
            "support_ready": bool(details.get("support_ready")),
            "support_ready_reason": details.get("support_ready_reason", ""),
            "latest_tick_ready": bool(details.get("latest_tick_ready", True)),
            "latest_tick_age_sec": details.get("latest_tick_age_sec"),
            "base_line_120_ready": bool(details.get("base_line_120_ready")),
            "base_line_120_candle_count": details.get("base_line_120_candle_count", 0),
            "vwap_ready": bool(details.get("vwap_ready")),
            "recent_support_ready": bool(details.get("recent_support_ready")),
            "market_raw_status": details.get("candidate_market_raw_status", ""),
            "market_confirmed_status": details.get("candidate_market_confirmed_status") or details.get("candidate_market_status", ""),
            "market_previous_confirmed_status": details.get("market_previous_confirmed_status", ""),
            "market_confirmation_pending": bool(details.get("candidate_market_confirmation_pending")),
            "market_recovery_pending": bool(details.get("candidate_market_recovery_pending")),
            "market_weak_consecutive_cycles": int(details.get("market_side_weak_consecutive_cycles") or 0),
            "market_risk_off_consecutive_cycles": int(details.get("market_side_risk_off_consecutive_cycles") or 0),
            "market_healthy_consecutive_cycles": int(details.get("market_side_healthy_consecutive_cycles") or 0),
            "market_wait_reason": details.get("market_wait_reason", ""),
            "market_wait_started_at": details.get("market_side_wait_started_at", ""),
            "market_wait_cycle_id": details.get("market_side_cycle_id", ""),
            "market_wait_recheck_after_sec": details.get("market_side_recheck_after_sec", 0),
            "market_wait_recovered_at": details.get("market_side_recovered_at", ""),
            "market_wait_cycles_to_recover": int(details.get("market_side_cycles_to_recover") or 0),
            "market_confirmation_state_source": details.get("market_confirmation_state_source", ""),
            "market_confirmation_state_restored": bool(details.get("market_confirmation_state_restored")),
            "market_confirmation_state_persisted": bool(details.get("market_confirmation_state_persisted")),
            "market_confirmation_state_age_sec": _float_or_none(details.get("market_confirmation_state_age_sec")),
            "market_confirmation_state_max_restore_age_sec": details.get("market_confirmation_state_max_restore_age_sec"),
            "market_confirmation_state_restore_reason": details.get("market_confirmation_state_restore_reason", ""),
            "market_confirmation_state_reset_reason": details.get("market_confirmation_state_reset_reason", ""),
            "market_session_id": details.get("market_session_id", ""),
            "market_session_type": details.get("market_session_type", ""),
            "market_trade_date": details.get("market_trade_date", ""),
            "market_restore_allowed": bool(details.get("market_restore_allowed", True)),
            "market_reset_required": bool(details.get("market_reset_required", False)),
            "market_side_breadth_pct": details.get("candidate_breadth_pct"),
            "market_side_index_return_pct": details.get("candidate_index_return_pct"),
            "market_side_turnover_weighted_return_pct": details.get("market_side_turnover_weighted_return_pct"),
            "market_side_breadth_source": details.get("candidate_breadth_source", ""),
            "market_side_breadth_trust_level": details.get("candidate_breadth_trust_level", ""),
            "market_side_breadth_gate_usable": bool(details.get("candidate_breadth_gate_usable")),
            "market_side_source_conflict": "SIDE_BREADTH_SOURCE_CONFLICT" in set(details.get("market_side_reason_codes") or []),
            "market_side_source_conflict_delta": details.get("market_side_source_conflict_delta"),
            "market_side_valid_quote_ratio": details.get("candidate_valid_quote_ratio"),
            "market_side_sample_count": details.get("candidate_breadth_sample_count", 0),
            "entry_plan_created": bool(details.get("entry_plan_created")),
            "diagnostic_only": bool(details.get("diagnostic_only")),
            "submittable": bool(details.get("submittable")),
            "blocked_reason": details.get("blocked_reason", ""),
            "blocked_reason_codes": list(details.get("reason_codes") or reason_codes),
            "runtime_order_intent_created": bool(details.get("runtime_order_intent_created")),
            "virtual_order_created": bool(details.get("virtual_order_created")),
            "live_order_enabled": bool(details.get("live_order_enabled")),
            "live_order_guard_passed": bool(details.get("live_order_guard_passed")),
            "false_block_candidate": false_block,
            "false_block_reason": normalized if false_block else "",
            **{key: price_fields.get(key) for key in price_fields if key != "price_data_missing"},
            "review_reason_codes": review_codes,
        }

    def _summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        waits = [row for row in rows if row["normalized_status"] in MARKET_WAIT_STATUSES]
        cycles = [int(row.get("market_wait_cycles_to_recover") or 0) for row in waits if int(row.get("market_wait_cycles_to_recover") or 0) > 0]
        recovered = [row for row in waits if row.get("market_wait_recovered_at") or row.get("normalized_status") in {"READY", "READY_SMALL"}]
        return {
            "total_candidates_seen": len(rows),
            "total_market_wait_count": len(waits),
            "market_wait_confirmation_pending_count": _count_status(rows, "WAIT_MARKET_CONFIRMATION_PENDING"),
            "market_wait_confirmed_weak_count": _count_status(rows, "WAIT_CANDIDATE_MARKET_WEAK"),
            "market_wait_confirmed_risk_off_count": _count_status(rows, "WAIT_CANDIDATE_MARKET_RISK_OFF"),
            "market_wait_recovery_pending_count": _count_status(rows, "WAIT_MARKET_RECOVERY_PENDING"),
            "market_wait_recovered_count": len(recovered),
            "market_wait_never_recovered_count": sum(1 for row in waits if not row.get("market_wait_recovered_at")),
            "market_wait_recovery_ratio": _ratio(len(recovered), len(waits)),
            "market_wait_avg_cycles_to_recover": _avg(cycles),
            "market_wait_median_cycles_to_recover": median(cycles) if cycles else None,
            "market_wait_p90_cycles_to_recover": _percentile(cycles, 90),
            "market_wait_buy_intent_blocked_count": sum(1 for row in waits if not row.get("runtime_order_intent_created")),
        }

    def _by_market_side(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("candidate_market") or "UNKNOWN")].append(row)
        result = {}
        for side, values in grouped.items():
            waits = [row for row in values if row["normalized_status"] in MARKET_WAIT_STATUSES]
            cycles = [int(row.get("market_wait_cycles_to_recover") or 0) for row in waits if int(row.get("market_wait_cycles_to_recover") or 0) > 0]
            recovered = [row for row in waits if row.get("market_wait_recovered_at")]
            result[side] = {
                "market_wait_count": len(waits),
                "recovery_ratio": _ratio(len(recovered), len(waits)),
                "avg_cycles_to_recover": _avg(cycles),
            }
        for side in ("KOSPI", "KOSDAQ", "UNKNOWN"):
            result.setdefault(side, {"market_wait_count": 0, "recovery_ratio": 0.0, "avg_cycles_to_recover": None})
        return result

    def _transition_summary(self, transitions: list[dict[str, Any]]) -> dict[str, Any]:
        types = Counter(str(item.get("transition_type") or "") for item in transitions)
        return {
            "transition_count": len(transitions),
            "by_transition_type": dict(types),
            "market_wait_confirmation_pending_count": types.get("WEAK_PENDING", 0) + types.get("RISK_OFF_PENDING", 0),
            "market_wait_confirmed_weak_count": types.get("WEAK_CONFIRMED", 0),
            "market_wait_confirmed_risk_off_count": types.get("RISK_OFF_CONFIRMED", 0),
            "market_wait_recovery_pending_count": types.get("RECOVERY_PENDING", 0),
            "market_wait_recovered_count": types.get("RECOVERY_CONFIRMED", 0),
            "source_conflict_count": types.get("SOURCE_CONFLICT", 0),
        }

    def _confirmation_pending_summary(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> dict[str, Any]:
        types = Counter(str(item.get("transition_type") or "") for item in transitions)
        pending = _count_status(rows, "WAIT_MARKET_CONFIRMATION_PENDING") + types.get("WEAK_PENDING", 0) + types.get("RISK_OFF_PENDING", 0)
        return {
            "confirmation_pending_count": pending,
            "confirmation_pending_to_confirmed_weak_count": types.get("WEAK_CONFIRMED", 0),
            "confirmation_pending_to_recovered_count": types.get("RECOVERY_CONFIRMED", 0),
            "confirmation_pending_to_ready_count": sum(1 for row in rows if row.get("market_wait_recovered_at")),
            "confirmation_pending_never_resolved_count": max(0, pending - types.get("WEAK_CONFIRMED", 0) - types.get("RECOVERY_CONFIRMED", 0)),
            "confirmation_pending_avg_cycles_to_resolution": _avg([row.get("market_wait_cycles_to_recover") for row in rows]),
        }

    def _recovery_pending_summary(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> dict[str, Any]:
        types = Counter(str(item.get("transition_type") or "") for item in transitions)
        pending = _count_status(rows, "WAIT_MARKET_RECOVERY_PENDING") + types.get("RECOVERY_PENDING", 0)
        return {
            "recovery_pending_count": pending,
            "recovery_pending_to_ready_count": types.get("RECOVERY_CONFIRMED", 0),
            "recovery_pending_back_to_weak_count": types.get("WEAK_CONFIRMED", 0),
            "recovery_pending_never_resolved_count": max(0, pending - types.get("RECOVERY_CONFIRMED", 0)),
            "recovery_pending_avg_cycles_to_resolution": _avg([row.get("market_wait_cycles_to_recover") for row in rows if row.get("normalized_status") == "WAIT_MARKET_RECOVERY_PENDING"]),
        }

    def _source_conflict_summary(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> dict[str, Any]:
        conflict_rows = [row for row in rows if row.get("market_side_source_conflict")]
        conflict_transitions = [item for item in transitions if str(item.get("transition_type") or "") == "SOURCE_CONFLICT"]
        by_side = Counter(str(row.get("candidate_market") or "UNKNOWN") for row in conflict_rows)
        return {
            "source_conflict_count": len(conflict_rows) + len(conflict_transitions),
            "source_conflict_blocked_count": sum(1 for row in conflict_rows if not row.get("runtime_order_intent_created")),
            "source_conflict_resolved_count": sum(1 for row in conflict_rows if row.get("market_wait_recovered_at")),
            "source_conflict_to_ready_count": sum(1 for row in conflict_rows if row.get("market_wait_recovered_at")),
            "source_conflict_never_resolved_count": sum(1 for row in conflict_rows if not row.get("market_wait_recovered_at")),
            "source_conflict_avg_cycles_to_resolution": _avg([row.get("market_wait_cycles_to_recover") for row in conflict_rows]),
            "source_conflict_by_side": dict(by_side),
            "source_conflict_by_source_pair": {},
        }

    def _restore_age_summary(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> dict[str, Any]:
        ages = [float(row["market_confirmation_state_age_sec"]) for row in rows if row.get("market_confirmation_state_age_sec") is not None]
        transition_types = Counter(str(item.get("transition_type") or "") for item in transitions)
        transition_reset_count = sum(transition_types.get(item, 0) for item in RESTORE_RESET_TRANSITION_TYPES)
        transition_reasons = Counter(
            reason
            for item in transitions
            for reason in _market_confirmation_reasons(item.get("transition_reason_codes") or [])
        )
        max_restore_values = [
            int(row.get("market_confirmation_state_max_restore_age_sec") or 0)
            for row in rows
            if int(row.get("market_confirmation_state_max_restore_age_sec") or 0) > 0
        ]
        max_restore_reference = max_restore_values[0] if max_restore_values else DEFAULT_MAX_RESTORE_AGE_SEC_REGULAR
        return {
            "restore_attempt_count": sum(1 for row in rows if row.get("market_confirmation_state_source")) + transition_types.get("RESTORE_ALLOWED", 0) + transition_types.get("RESTORE_SKIPPED", 0),
            "restore_success_count": sum(1 for row in rows if row.get("market_confirmation_state_restored")) + transition_types.get("RESTORE_ALLOWED", 0),
            "restore_skipped_count": sum(1 for row in rows if row.get("market_confirmation_state_restore_reason") and not row.get("market_confirmation_state_restored")) + transition_types.get("RESTORE_SKIPPED", 0),
            "restore_failed_count": sum(1 for row in rows if row.get("market_confirmation_state_restore_reason") == "MARKET_CONFIRMATION_STATE_DB_ERROR")
            + transition_reasons.get("MARKET_CONFIRMATION_STATE_DB_ERROR", 0),
            "restore_age_sec_avg": _avg(ages),
            "restore_age_sec_median": median(ages) if ages else None,
            "restore_age_sec_p90": _percentile(ages, 90),
            "restore_age_sec_max": max(ages) if ages else None,
            "restore_age_bucket_count": _age_buckets(ages),
            "restore_age_900s_plus_count": _age_buckets(ages).get("900s_plus", 0),
            "restore_age_over_max_count": sum(
                1
                for row in rows
                if row.get("market_confirmation_state_age_sec") is not None
                and float(row.get("market_confirmation_state_age_sec") or 0) > int(row.get("market_confirmation_state_max_restore_age_sec") or max_restore_reference)
            ),
            "max_restore_age_sec_regular_reference": max_restore_reference,
            "restore_age_exceeded_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") == "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED")
            + transition_reasons.get("MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED", 0),
            "state_stale_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") in {"MARKET_CONFIRMATION_STATE_STALE", "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED"}),
            "state_expired_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") == "MARKET_CONFIRMATION_STATE_EXPIRED"),
            "reset_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason")) + transition_reset_count,
            "reset_by_reason": dict(
                Counter(str(row.get("market_confirmation_state_reset_reason") or "") for row in rows if row.get("market_confirmation_state_reset_reason"))
                + transition_reasons
            ),
            "conservative_fallback_count": sum(1 for row in rows if row.get("normalized_status") == "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"),
            "conservative_fallback_blocked_ready_count": sum(1 for row in rows if row.get("normalized_status") == "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK" and not row.get("runtime_order_intent_created")),
            "restore_age_exceeded_but_next_cycle_same_status_count": 0,
            "restore_age_exceeded_but_market_still_weak_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") == "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED" and row.get("market_confirmed_status") == "WEAK"),
            "restore_age_exceeded_then_ready_within_2_cycles_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") == "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED" and int(row.get("market_wait_cycles_to_recover") or 0) <= 2 and row.get("market_wait_recovered_at")),
            "stale_reset_then_reconfirmed_weak_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") in {"MARKET_CONFIRMATION_STATE_STALE", "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED"} and row.get("market_confirmed_status") == "WEAK"),
            "stale_reset_then_ready_count": sum(1 for row in rows if row.get("market_confirmation_state_reset_reason") in {"MARKET_CONFIRMATION_STATE_STALE", "MARKET_CONFIRMATION_STATE_RESTORE_AGE_EXCEEDED"} and row.get("market_wait_recovered_at")),
        }

    def _session_reset_summary(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> dict[str, Any]:
        session_reset_reasons = {
            "MARKET_CONFIRMATION_STATE_RESET_ON_SESSION_CHANGE",
            "MARKET_CONFIRMATION_STATE_RESET_ON_TRADE_DATE_CHANGE",
            "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
            "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN",
            "MARKET_CONFIRMATION_STATE_SESSION_MISMATCH",
            "MARKET_CONFIRMATION_STATE_DATE_MISMATCH",
        }
        session_rows = [row for row in rows if row.get("market_confirmation_state_reset_reason") in session_reset_reasons or row.get("market_reset_required")]
        by_reason = Counter(str(row.get("market_confirmation_state_reset_reason") or row.get("market_reset_reason") or "") for row in session_rows)
        transition_types = Counter(str(item.get("transition_type") or "") for item in transitions)
        transition_session_reset_count = sum(transition_types.get(item, 0) for item in SESSION_RESET_TRANSITION_TYPES)
        transition_reasons = Counter(
            reason
            for item in transitions
            if str(item.get("transition_type") or "") in SESSION_RESET_TRANSITION_TYPES
            for reason in _market_confirmation_reasons(item.get("transition_reason_codes") or [])
        )
        return {
            "session_reset_count": len(session_rows) + transition_session_reset_count,
            "session_reset_by_reason": dict(by_reason + transition_reasons),
            "session_reset_then_market_wait_count": sum(1 for row in session_rows if row.get("normalized_status") in MARKET_WAIT_STATUSES),
            "session_reset_then_ready_within_1_cycle_count": sum(1 for row in session_rows if row.get("market_wait_recovered_at") and int(row.get("market_wait_cycles_to_recover") or 0) <= 1),
            "session_reset_then_ready_within_2_cycles_count": sum(1 for row in session_rows if row.get("market_wait_recovered_at") and int(row.get("market_wait_cycles_to_recover") or 0) <= 2),
            "session_reset_then_blocked_buy_intent_count": sum(1 for row in session_rows if not row.get("runtime_order_intent_created")),
            "session_reset_unknown_schedule_count": by_reason.get("MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN", 0)
            + transition_reasons.get("MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN", 0),
            "session_reset_conservative_fallback_count": sum(1 for row in session_rows if row.get("normalized_status") == "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"),
            "pre_open_restore_skipped_count": sum(1 for row in rows if row.get("market_session_type") == "pre_open" and row.get("market_confirmation_state_restore_reason"))
            + sum(1 for item in transitions if str(item.get("transition_type") or "") == "RESTORE_SKIPPED" and str(item.get("session_id") or "").endswith(":pre_open")),
            "post_close_restore_skipped_count": sum(1 for row in rows if row.get("market_session_type") == "post_close" and row.get("market_confirmation_state_restore_reason"))
            + sum(1 for item in transitions if str(item.get("transition_type") or "") == "RESTORE_SKIPPED" and str(item.get("session_id") or "").endswith(":post_close")),
            "trade_date_change_reset_count": by_reason.get("MARKET_CONFIRMATION_STATE_RESET_ON_TRADE_DATE_CHANGE", 0)
            + by_reason.get("MARKET_CONFIRMATION_STATE_DATE_MISMATCH", 0)
            + transition_reasons.get("MARKET_CONFIRMATION_STATE_RESET_ON_TRADE_DATE_CHANGE", 0)
            + transition_reasons.get("MARKET_CONFIRMATION_STATE_DATE_MISMATCH", 0),
        }

    def _false_block_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        false_rows = [row for row in rows if row.get("false_block_candidate")]
        cycles = [int(row.get("market_wait_cycles_to_recover") or 0) for row in false_rows if int(row.get("market_wait_cycles_to_recover") or 0) > 0]
        return {
            "false_block_candidate_count": len(false_rows),
            "false_block_candidate_ratio": _ratio(len(false_rows), len(rows)),
            "false_block_by_reason": dict(Counter(str(row.get("false_block_reason") or "") for row in false_rows)),
            "false_block_by_market_side": dict(Counter(str(row.get("candidate_market") or "UNKNOWN") for row in false_rows)),
            "false_block_by_source": dict(Counter(str(row.get("market_confirmation_state_source") or "") for row in false_rows)),
            "false_block_recovered_to_ready_count": sum(1 for row in false_rows if row.get("market_wait_recovered_at")),
            "false_block_recovered_to_ready_ratio": _ratio(sum(1 for row in false_rows if row.get("market_wait_recovered_at")), len(false_rows)),
            "false_block_avg_cycles_to_ready": _avg(cycles),
            "false_block_median_cycles_to_ready": median(cycles) if cycles else None,
        }


def _normalize_display_status(details: dict[str, Any]) -> str:
    reasons = {str(reason or "") for reason in details.get("reason_codes") or []}
    market_reasons = {str(reason or "") for reason in details.get("market_side_reason_codes") or []}
    all_reasons = reasons | market_reasons
    market_status = str(details.get("candidate_market_confirmed_status") or details.get("candidate_market_status") or "")
    if details.get("chase_risk") or "CHASE_RISK" in all_reasons:
        return "CHASE_RISK_BLOCKED"
    if details.get("late_chase_level") == "soft_block" or "LATE_CHASE_TEMP_WAIT" in all_reasons:
        return "LATE_CHASE_TEMP_WAIT"
    if "MARKET_CONFIRMATION_STATE_CONSERVATIVE_FALLBACK" in all_reasons:
        return "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"
    if details.get("candidate_market_recovery_pending"):
        return "WAIT_MARKET_RECOVERY_PENDING"
    if details.get("candidate_market_confirmation_pending"):
        return "WAIT_MARKET_CONFIRMATION_PENDING"
    if market_status == "RISK_OFF":
        return "WAIT_CANDIDATE_MARKET_RISK_OFF"
    if market_status == "WEAK":
        return "WAIT_CANDIDATE_MARKET_WEAK"
    if details.get("support_ready_reason") or details.get("support_missing_reason"):
        return "WAIT_DATA_SUPPORT_NOT_READY"
    if details.get("latest_tick_ready") is False:
        return "WAIT_DATA_LATEST_TICK_STALE"
    return str(details.get("final_gate_status") or details.get("sub_status") or "")


def _is_false_block_candidate(normalized: str, details: dict[str, Any]) -> bool:
    if normalized not in MARKET_WAIT_STATUSES:
        return False
    return bool(details.get("market_side_recovered_to_ready") or details.get("market_side_recovered_at") or details.get("market_side_cycles_to_recover"))


def _price_observation_fields(details: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "post_block_return_3m_pct",
        "post_block_return_5m_pct",
        "post_block_return_10m_pct",
        "post_block_return_30m_pct",
        "max_favorable_excursion_after_block_pct",
        "max_adverse_excursion_after_block_pct",
    ]
    values = {key: _float_or_none(details.get(key)) for key in keys}
    values["price_data_missing"] = all(value is None for value in values.values())
    return values


def _market_confirmation_reasons(values: list[Any]) -> list[str]:
    return [str(value) for value in values if str(value or "").startswith("MARKET_CONFIRMATION_STATE_")]


def _count_status(rows: list[dict[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row.get("normalized_status") == status)


def _avg(values: list[Any]) -> float | None:
    numbers = [float(value) for value in values if _float_or_none(value) is not None]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def _ratio(part: int, total: int) -> float:
    return round(part / total, 4) if total else 0.0


def _percentile(values: list[Any], pct: int) -> float | None:
    numbers = sorted(float(value) for value in values if _float_or_none(value) is not None)
    if not numbers:
        return None
    index = min(len(numbers) - 1, max(0, int(round((pct / 100) * (len(numbers) - 1)))))
    return numbers[index]


def _age_buckets(values: list[float]) -> dict[str, int]:
    buckets = {"0_60s": 0, "60_180s": 0, "180_300s": 0, "300_600s": 0, "600_900s": 0, "900s_plus": 0}
    for value in values:
        if value < 60:
            buckets["0_60s"] += 1
        elif value < 180:
            buckets["60_180s"] += 1
        elif value < 300:
            buckets["180_300s"] += 1
        elif value < 600:
            buckets["300_600s"] += 1
        elif value < 900:
            buckets["600_900s"] += 1
        else:
            buckets["900s_plus"] += 1
    return buckets


def _latest_trade_date(rows: list[dict[str, Any]]) -> str:
    dates = sorted({str(row.get("trade_date") or "") for row in rows if row.get("trade_date")})
    return dates[-1] if dates else ""


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value
