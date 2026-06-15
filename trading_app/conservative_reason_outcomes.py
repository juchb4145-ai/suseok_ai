from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable as IterableABC
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "conservative_reason_outcomes"

REASON_GROUP_PRIORITY = (
    "MARKET_RISK",
    "BREADTH_RISK",
    "CHASE_RISK",
    "DATA_QUALITY_RISK",
    "PRICE_LOCATION_WAIT",
    "THEME_WEAKNESS",
    "OTHER",
)

REASON_GROUP_PATTERNS: dict[str, tuple[str, ...]] = {
    "MARKET_RISK": (
        "RISK_OFF",
        "MARKET_RISK_OFF",
        "GLOBAL_MARKET_RISK_OFF",
        "CANDIDATE_MARKET_RISK_OFF",
        "KOSDAQ_MARKET_RISK_OFF",
        "KOSPI_MARKET_RISK_OFF",
        "WAIT_MARKET_CONFIRMATION_PENDING",
        "WAIT_MARKET_RECOVERY_PENDING",
        "MARKET_WAIT_HYSTERESIS_HOLD",
    ),
    "BREADTH_RISK": (
        "LOW_BREADTH",
        "SIDE_BREADTH_LOW_TRUST",
        "SIDE_BREADTH_SAMPLE_TOO_SMALL",
        "SIDE_BREADTH_VALID_QUOTE_RATIO_LOW",
        "SIDE_BREADTH_SOURCE_CONFLICT",
        "LEADER_ONLY_THEME",
        "LEADER_ONLY_THEME_LAGGARD_BLOCK",
    ),
    "CHASE_RISK": (
        "LATE_CHASE",
        "LATE_CHASE_TEMP_WAIT",
        "CHASE_RISK",
        "CHASE_HIGH",
        "HIGH_CHASE_RISK",
        "VWAP_OVEREXTENDED",
        "BREAKOUT_CONTINUATION",
        "UPPER_LIMIT_NEAR",
        "UPPER_LIMIT_HARD_NEAR",
        "HIGH_RETURN_LEADER",
        "HIGH_RETURN_CO_LEADER",
        "HIGH_RETURN_FOLLOWER",
    ),
    "DATA_QUALITY_RISK": (
        "DATA_INSUFFICIENT",
        "CORE_BLOCKING",
        "ENTRY_BLOCKING",
        "WARMUP_OPTIONAL",
        "BACKFILL_ONLY_OBSERVE",
        "WAIT_DATA",
        "WAIT_DATA_SUPPORT_NOT_READY",
        "WAIT_DATA_REALTIME_RELIABILITY_LOW",
        "LATEST_TICK_STALE",
        "MISSING_CURRENT_PRICE",
        "SUPPORT_NOT_READY",
        "VWAP_MISSING",
        "MISSING_VWAP",
    ),
    "THEME_WEAKNESS": (
        "THEME_WEAK",
        "WEAK_THEME",
        "THEME_STALE",
        "LOW_MEMBERSHIP_SCORE",
        "THEME_MEMBER_NOT_TRADE_ELIGIBLE",
    ),
    "PRICE_LOCATION_WAIT": (
        "WAIT_PRICE_LOCATION_DATA",
        "WAIT_PRICE_LOCATION_WARMUP",
        "WAIT_PRICE_LOCATION_PROVISIONAL",
        "WAIT_PRICE_LOCATION_UNKNOWN",
        "PRICE_LOCATION_WARMUP",
        "PRICE_LOCATION_PROVISIONAL",
        "PRICE_LOCATION_UNKNOWN",
        "FAILED_BREAKOUT",
        "DEEP_PULLBACK",
        "WAIT_FAILED_BREAKOUT",
        "WAIT_DEEP_PULLBACK",
    ),
}

DATA_QUALITY_BUCKETS = {
    "CORE_BLOCKING",
    "ENTRY_BLOCKING",
    "WARMUP_OPTIONAL",
    "BACKFILL_ONLY_OBSERVE",
    "OK",
    "UNKNOWN",
}

CSV_COLUMNS = [
    "trade_date",
    "observed_at",
    "code",
    "name",
    "status",
    "primary_group",
    "reason_codes",
    "base_price",
    "return_5m_pct",
    "return_15m_pct",
    "return_30m_pct",
    "mfe_15m_pct",
    "mae_15m_pct",
    "outcome_label",
    "recommendation",
    "theme_name",
    "stock_role",
    "price_location_status",
    "risk_level",
    "data_quality_bucket",
    "early_small_candidate",
    "operator_message_ko",
]


@dataclass(frozen=True)
class ConservativeReasonOutcomeConfig:
    missed_opportunity_mfe_15m_pct: float = 3.0
    strong_missed_opportunity_mfe_15m_pct: float = 5.0
    good_block_max_mfe_15m_pct: float = 1.0
    risk_avoided_mae_15m_pct: float = -2.0
    data_quality_min_observation_count: int = 2
    min_sample_count: int = 10
    strong_sample_count: int = 30
    review_small_entry_min_missed_rate: float = 0.35
    review_small_entry_max_risk_avoided_rate: float = 0.20
    keep_block_min_good_block_rate: float = 0.35
    keep_block_min_risk_avoided_rate: float = 0.30
    thin_sample_position_size_multiplier: float = 0.10
    medium_position_size_multiplier: float = 0.15
    low_risk_position_size_multiplier: float = 0.25
    suggested_max_orders_per_cycle: int = 1


def normalize_conservative_reason_group(reason_codes: Iterable[Any], details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    details = dict(details or {})
    reason_values = [reason_codes] if isinstance(reason_codes, str) else list(reason_codes or [])
    reasons = _unique_upper([*reason_values, details.get("primary_reason")])
    bucket = str(details.get("data_quality_bucket") or "").strip().upper()
    action = str(details.get("data_quality_action") or "").strip().upper()
    status = str(details.get("candidate_market_status") or details.get("candidate_market") or "").strip().upper()
    groups: list[str] = []
    for reason in reasons:
        for group in REASON_GROUP_PRIORITY:
            if group == "OTHER":
                continue
            if _reason_matches_group(reason, group):
                _append_unique(groups, group)
    if bucket in {"CORE_BLOCKING", "ENTRY_BLOCKING", "WARMUP_OPTIONAL", "BACKFILL_ONLY_OBSERVE"}:
        _append_unique(groups, "DATA_QUALITY_RISK")
    if action.startswith("WAIT_DATA") or "DATA" in action and "BLOCK" in action:
        _append_unique(groups, "DATA_QUALITY_RISK")
    if status == "RISK_OFF":
        _append_unique(groups, "MARKET_RISK")
    ordered = [group for group in REASON_GROUP_PRIORITY if group in groups]
    if not ordered:
        ordered = ["OTHER"]
    return {"primary_group": ordered[0], "all_groups": ordered}


class ConservativeReasonOutcomeAnalyzer:
    def __init__(
        self,
        db,
        *,
        config: Optional[ConservativeReasonOutcomeConfig] = None,
        report_root: Optional[Path] = None,
        base_analyzer: Optional[ThemeLabGateReasonOutcomeAnalyzer] = None,
    ) -> None:
        self.db = db
        self.config = config or ConservativeReasonOutcomeConfig()
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT
        self.base_analyzer = base_analyzer or ThemeLabGateReasonOutcomeAnalyzer(db)

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 10000,
        offset: int = 0,
        source_report: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source = source_report if source_report is not None else self.base_analyzer.build_report(trade_date=trade_date, limit=limit, offset=offset)
        items = [self._item_from_theme_lab(item) for item in source.get("items") or []]
        group_summary = self._group_summary(items)
        reason_summary = self._reason_code_summary(items, group_summary=group_summary)
        data_quality_summary = self._data_quality_bucket_summary(items)
        review_small_entry = self._review_for_small_entry(items, group_summary=group_summary, reason_summary=reason_summary)
        summary = self._summary(items)
        generated_at = datetime.now().isoformat(timespec="seconds")
        status = "NO_DATA" if not items else "READY"
        report = {
            "available": bool(items),
            "status": status,
            "report_id": f"conservative_reason_outcomes:{source.get('trade_date') or trade_date or 'all'}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "source_report_id": source.get("report_id") or "",
            "trade_date": source.get("trade_date") or trade_date or "",
            "generated_at": generated_at,
            "last_updated_at": generated_at,
            "config": asdict(self.config),
            "summary": summary,
            "by_group": group_summary,
            "by_reason_code": reason_summary,
            "data_quality_bucket_summary": data_quality_summary,
            "review_for_small_entry": review_small_entry,
            "top_missed_opportunity_reasons": [row for row in reason_summary if row.get("missed_opportunity_count", 0) > 0][:10],
            "top_good_block_reasons": [row for row in reason_summary if row.get("good_block_count", 0) > 0][:10],
            "top_missed_opportunity_stocks": _top_items(items, key="mfe_15m_pct", predicate=lambda item: bool(item.get("missed_opportunity"))),
            "top_good_block_stocks": _top_items(items, key="mae_15m_pct", reverse=False, predicate=lambda item: bool(item.get("good_block"))),
            "warmup_optional_items": [item for item in items if item.get("data_quality_bucket") == "WARMUP_OPTIONAL"][:50],
            "items": items,
            "notes": [
                "read_only_observability_report",
                "does_not_modify_gate_thresholds_or_order_logic",
                "does_not_enable_live_real_or_live_sim_orders",
                "uses_existing_theme_lab_gate_reason_outcome_observations",
                "small_entry_recommendations_are_review_signals_only",
            ],
            "disclaimer_ko": "이 리포트는 주문 설정을 자동 변경하지 않습니다. 장중에는 5/15/30분 결과가 아직 확정되지 않을 수 있습니다.",
        }
        return report

    def filter_items(
        self,
        report: dict[str, Any],
        *,
        reason_group: str = "",
        reason_code: str = "",
        recommendation: str = "",
        code: str = "",
    ) -> list[dict[str, Any]]:
        group = str(reason_group or "").strip().upper()
        reason = str(reason_code or "").strip().upper()
        rec = str(recommendation or "").strip().upper()
        symbol = str(code or "").strip()
        rows = list(report.get("items") or [])
        if group:
            rows = [item for item in rows if group in set(item.get("all_groups") or []) or item.get("primary_group") == group]
        if reason:
            rows = [item for item in rows if reason in _upper_set(item.get("reason_codes") or [])]
        if rec:
            rows = [item for item in rows if str(item.get("recommendation") or "").upper() == rec]
        if symbol:
            rows = [item for item in rows if str(item.get("code") or "") == symbol]
        return rows

    def export_json(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for item in report.get("items") or []:
                writer.writerow({column: _csv_value(item.get(column)) for column in CSV_COLUMNS})
        return path

    def export_markdown(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = report.get("summary") or {}
        lines = [
            f"# Conservative Reason Outcomes ({report.get('trade_date') or 'all'})",
            "",
            "Read-only observability report. It does not change thresholds, guards, LIVE_SIM, or LIVE_REAL settings.",
            "",
            "## Summary",
            f"- event_count: {summary.get('event_count', 0)}",
            f"- labeled_event_count: {summary.get('labeled_event_count', 0)}",
            f"- missed_opportunity_rate: {summary.get('missed_opportunity_rate', 0)}",
            f"- good_block_rate: {summary.get('good_block_rate', 0)}",
            f"- risk_avoided_rate: {summary.get('risk_avoided_rate', 0)}",
            f"- review_for_small_entry_count: {(report.get('review_for_small_entry') or {}).get('summary', {}).get('candidate_count', 0)}",
            "",
            "## Group Outcomes",
        ]
        for row in report.get("by_group") or []:
            lines.append(
                f"- {row.get('group')}: events={row.get('event_count')}, missed={row.get('missed_opportunity_rate')}, "
                f"good_block={row.get('good_block_rate')}, recommendation={row.get('recommendation')}"
            )
        lines.extend(["", "## Review For Small Entry"])
        for row in ((report.get("review_for_small_entry") or {}).get("by_reason_code") or [])[:10]:
            lines.append(
                f"- {row.get('reason_code')}: candidates={row.get('candidate_count')}, "
                f"avg_mfe_15m_pct={row.get('avg_mfe_15m_pct')}, multiplier={row.get('suggested_position_size_multiplier')}"
            )
        lines.extend(
            [
                "",
                "## Notes",
                "- 장중에는 5/15/30분 outcome이 아직 확정되지 않을 수 있습니다.",
                "- 소액 진입 검토 후보는 관측 신호이며 주문 설정을 변경하지 않습니다.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict[str, Any], *, report_dir: Optional[Path] = None, stem: Optional[str] = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root / str(report.get("trade_date") or "all")
        stem = stem or f"conservative_reason_outcomes_{report.get('trade_date') or 'all'}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def export_report(self, report: dict[str, Any], *, fmt: str = "json") -> dict[str, str]:
        normalized = "md" if fmt == "markdown" else str(fmt or "json").lower()
        target = self.report_root / str(report.get("trade_date") or "all")
        stem = f"conservative_reason_outcomes_{report.get('trade_date') or 'all'}_{datetime.now().strftime('%H%M%S')}"
        if normalized == "all":
            return self.export_all(report, report_dir=target, stem=stem)
        if normalized == "csv":
            return {"csv": str(self.export_csv(report, target / f"{stem}.csv"))}
        if normalized == "md":
            return {"md": str(self.export_markdown(report, target / f"{stem}.md"))}
        return {"json": str(self.export_json(report, target / f"{stem}.json"))}

    def _item_from_theme_lab(self, item: dict[str, Any]) -> dict[str, Any]:
        source = dict(item or {})
        reason_codes = _unique_upper(source.get("reason_codes") or [source.get("primary_reason")])
        groups = normalize_conservative_reason_group(reason_codes, source)
        label_payload = self._label_item(source, groups)
        enriched = {
            **source,
            "primary_group": groups["primary_group"],
            "all_groups": groups["all_groups"],
            "theme_name": source.get("theme_name") or source.get("primary_theme") or "",
            "reason_codes": reason_codes,
            **label_payload,
        }
        enriched["recommendation"] = _item_recommendation(enriched)
        enriched["operator_message_ko"] = _operator_message_ko(enriched)
        return enriched

    def _label_item(self, item: dict[str, Any], groups: dict[str, Any]) -> dict[str, Any]:
        observation_count = int(_number(item.get("observation_count_15m")) or 0)
        labeled = item.get("outcome_data_quality") == "ready" and observation_count >= int(self.config.data_quality_min_observation_count)
        mfe = _number(item.get("mfe_15m_pct"))
        mae = _number(item.get("mae_15m_pct"))
        status = str(item.get("status") or "").upper()
        missed = bool(labeled and mfe is not None and mfe >= float(self.config.missed_opportunity_mfe_15m_pct))
        strong_missed = bool(labeled and mfe is not None and mfe >= float(self.config.strong_missed_opportunity_mfe_15m_pct))
        risk_avoided = bool(labeled and mae is not None and mae <= float(self.config.risk_avoided_mae_15m_pct))
        good_block = bool(
            labeled
            and mfe is not None
            and mae is not None
            and mfe < float(self.config.good_block_max_mfe_15m_pct)
            and mae <= float(self.config.risk_avoided_mae_15m_pct)
        )
        chase_protected = bool("CHASE_RISK" in groups.get("all_groups", []) and risk_avoided and not strong_missed)
        wait_resolved = bool(item.get("would_have_triggered_ready"))
        minutes_to_ready = _number(item.get("minutes_to_ready"))
        late_ready = bool(wait_resolved and minutes_to_ready is not None and minutes_to_ready > 5.0)
        false_block = bool(missed and not risk_avoided and status in {"WAIT", "OBSERVE", "BLOCKED"})
        effective_block = bool((good_block or risk_avoided or chase_protected) and not missed)
        labels: list[str] = []
        if not labeled:
            labels.append("DATA_QUALITY_UNKNOWN")
        if strong_missed:
            labels.append("STRONG_MISSED_OPPORTUNITY")
        if missed:
            labels.append("MISSED_OPPORTUNITY")
        if good_block:
            labels.append("GOOD_BLOCK")
        if risk_avoided:
            labels.append("RISK_AVOIDED")
        if chase_protected:
            labels.append("CHASE_PROTECTED")
        if wait_resolved:
            labels.append("WAIT_RESOLVED_TO_READY")
        if late_ready:
            labels.append("LATE_READY")
        if false_block:
            labels.append("FALSE_BLOCK_CANDIDATE")
        if effective_block:
            labels.append("EFFECTIVE_BLOCK")
        if not labels:
            labels.append("NEUTRAL")
        primary = _primary_label(labels)
        return {
            "labeled": labeled,
            "outcome_label": primary,
            "outcome_labels": labels,
            "missed_opportunity": missed,
            "strong_missed_opportunity": strong_missed,
            "good_block": good_block,
            "risk_avoided": risk_avoided,
            "chase_protected": chase_protected,
            "data_quality_unknown": not labeled,
            "wait_resolved_to_ready": wait_resolved,
            "late_ready": late_ready,
            "false_block_candidate": false_block,
            "effective_block": effective_block,
        }

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [item for item in items if item.get("labeled")]
        return {
            "event_count": len(items),
            "labeled_event_count": len(labeled),
            "unlabeled_event_count": len(items) - len(labeled),
            "missed_opportunity_count": _count(items, "missed_opportunity"),
            "strong_missed_opportunity_count": _count(items, "strong_missed_opportunity"),
            "good_block_count": _count(items, "good_block"),
            "risk_avoided_count": _count(items, "risk_avoided"),
            "false_block_candidate_count": _count(items, "false_block_candidate"),
            "effective_block_count": _count(items, "effective_block"),
            "data_quality_unknown_count": _count(items, "data_quality_unknown"),
            "missed_opportunity_rate": _ratio(_count(items, "missed_opportunity"), len(labeled)),
            "good_block_rate": _ratio(_count(items, "good_block"), len(labeled)),
            "risk_avoided_rate": _ratio(_count(items, "risk_avoided"), len(labeled)),
            "avg_return_5m_pct": _avg(item.get("return_5m_pct") for item in labeled),
            "avg_return_15m_pct": _avg(item.get("return_15m_pct") for item in labeled),
            "avg_return_30m_pct": _avg(item.get("return_30m_pct") for item in labeled),
            "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
            "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
        }

    def _group_summary(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            for group in item.get("all_groups") or ["OTHER"]:
                grouped[str(group or "OTHER")].append(item)
        rows = [_performance_row(group, values, key_name="group", config=self.config) for group, values in grouped.items()]
        return sorted(rows, key=lambda row: (row["recommendation_priority"], row["missed_opportunity_count"], row["event_count"]), reverse=True)

    def _reason_code_summary(self, items: list[dict[str, Any]], *, group_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
        group_recommendations = {str(row.get("group") or ""): row.get("recommendation") for row in group_summary}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            reasons = list(item.get("reason_codes") or []) or [str(item.get("primary_reason") or "UNKNOWN")]
            for reason in reasons:
                grouped[str(reason or "UNKNOWN").upper()].append(item)
        rows: list[dict[str, Any]] = []
        for reason, values in grouped.items():
            groups = normalize_conservative_reason_group([reason], values[0] if values else {})
            row = _performance_row(reason, values, key_name="reason_code", config=self.config)
            row["group"] = groups["primary_group"]
            row["sample_quality"] = _sample_quality(row["labeled_count"], config=self.config)
            if row["recommendation"] == "KEEP_WAIT" and group_recommendations.get(row["group"]) == "REVIEW_FOR_SMALL_ENTRY":
                row["recommendation"] = "REVIEW_FOR_SMALL_ENTRY"
            rows.append(row)
        return sorted(rows, key=lambda row: (row["recommendation_priority"], row["missed_opportunity_count"], row["event_count"], row["reason_code"]), reverse=True)

    def _data_quality_bucket_summary(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            bucket = str(item.get("data_quality_bucket") or "UNKNOWN").upper()
            if bucket not in DATA_QUALITY_BUCKETS:
                bucket = bucket or "UNKNOWN"
            grouped[bucket].append(item)
        rows: list[dict[str, Any]] = []
        for bucket, values in grouped.items():
            row = _performance_row(bucket, values, key_name="data_quality_bucket", config=self.config)
            row["early_small_candidate_count"] = sum(1 for item in values if item.get("early_small_candidate") or item.get("shadow_small_entry_candidate"))
            if bucket == "WARMUP_OPTIONAL" and row["recommendation"] == "REVIEW_FOR_SMALL_ENTRY":
                row["operator_message_ko"] = "WARMUP_OPTIONAL_ONLY 후보는 소액 진입 검토 가치가 있지만 order_enabled=false 관찰 신호입니다."
            elif bucket in {"CORE_BLOCKING", "BACKFILL_ONLY_OBSERVE"}:
                row["operator_message_ko"] = "핵심 데이터 부족 또는 backfill-only 상태는 주문 완화 대상이 아닙니다."
            else:
                row["operator_message_ko"] = "데이터 품질 bucket별 outcome 관측 요약입니다."
            rows.append(row)
        return sorted(rows, key=lambda row: (row["event_count"], row["data_quality_bucket"]), reverse=True)

    def _review_for_small_entry(
        self,
        items: list[dict[str, Any]],
        *,
        group_summary: list[dict[str, Any]],
        reason_summary: list[dict[str, Any]],
    ) -> dict[str, Any]:
        review_groups = {str(row.get("group") or "") for row in group_summary if row.get("recommendation") == "REVIEW_FOR_SMALL_ENTRY"}
        review_reasons = {str(row.get("reason_code") or "") for row in reason_summary if row.get("recommendation") == "REVIEW_FOR_SMALL_ENTRY"}
        candidates = [
            {**item, **self._small_entry_suggestion(item)}
            for item in items
            if _small_entry_candidate(item, review_groups=review_groups, review_reasons=review_reasons)
        ]
        labeled = [item for item in candidates if item.get("labeled")]
        summary = {
            "candidate_count": len(candidates),
            "labeled_count": len(labeled),
            "by_reason_code": _candidate_group_rows(candidates, "primary_reason", config=self.config),
            "by_group": _candidate_group_rows(candidates, "primary_group", config=self.config),
            "by_stock_role": _candidate_group_rows(candidates, "stock_role", config=self.config),
            "by_price_location": _candidate_group_rows(candidates, "price_location_status", config=self.config),
            "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
            "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
            "suggested_position_size_multiplier": _suggested_multiplier(labeled, config=self.config),
            "suggested_max_orders_per_cycle": int(self.config.suggested_max_orders_per_cycle),
            "suggested_guard_reason_codes": sorted(_SMALL_ENTRY_EXCLUDED_REASONS),
            "operator_message_ko": "소액 진입 검토 후보입니다. 실제 주문 설정은 변경하지 않았고 REVIEW_READY 근거로만 사용합니다.",
        }
        return {
            "summary": summary,
            "candidates": sorted(candidates, key=lambda item: float(item.get("mfe_15m_pct") or -999.0), reverse=True)[:50],
            "by_reason_code": summary["by_reason_code"],
            "by_group": summary["by_group"],
            "by_stock_role": summary["by_stock_role"],
            "by_price_location": summary["by_price_location"],
        }

    def _small_entry_suggestion(self, item: dict[str, Any]) -> dict[str, Any]:
        multiplier = _suggested_multiplier([item] if item.get("labeled") else [], config=self.config)
        return {
            "early_small_candidate": True,
            "suggested_position_size_multiplier": multiplier,
            "suggested_max_orders_per_cycle": int(self.config.suggested_max_orders_per_cycle),
            "suggested_guard_reason_codes": sorted(_SMALL_ENTRY_EXCLUDED_REASONS),
        }


def snapshot_payload(report: dict[str, Any]) -> dict[str, Any]:
    review = report.get("review_for_small_entry") or {}
    review_summary = review.get("summary") or {}
    return {
        "available": bool(report.get("available")),
        "status": report.get("status") or "NO_DATA",
        "trade_date": report.get("trade_date") or "",
        "generated_at": report.get("generated_at") or "",
        "summary": report.get("summary") or {},
        "top_missed_opportunity_reasons": list(report.get("top_missed_opportunity_reasons") or [])[:5],
        "top_good_block_reasons": list(report.get("top_good_block_reasons") or [])[:5],
        "review_for_small_entry": {
            "summary": review_summary,
            "by_reason_code": list(review.get("by_reason_code") or [])[:5],
            "candidates": list(review.get("candidates") or [])[:10],
        },
        "top_missed_opportunity_stocks": list(report.get("top_missed_opportunity_stocks") or [])[:10],
        "top_good_block_stocks": list(report.get("top_good_block_stocks") or [])[:10],
        "warmup_optional_items": list(report.get("warmup_optional_items") or [])[:10],
        "data_quality_bucket_summary": list(report.get("data_quality_bucket_summary") or [])[:8],
        "by_group": list(report.get("by_group") or [])[:8],
        "by_reason_code": list(report.get("by_reason_code") or [])[:10],
        "last_updated_at": report.get("last_updated_at") or report.get("generated_at") or "",
        "disclaimer_ko": report.get("disclaimer_ko") or "",
    }


def empty_payload(error: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "status": "ERROR" if error else "NO_DATA",
        "trade_date": "",
        "generated_at": "",
        "summary": {},
        "top_missed_opportunity_reasons": [],
        "top_good_block_reasons": [],
        "review_for_small_entry": {"summary": {"candidate_count": 0}, "by_reason_code": [], "candidates": []},
        "data_quality_bucket_summary": [],
        "by_group": [],
        "by_reason_code": [],
        "last_updated_at": "",
        "disclaimer_ko": "이 리포트는 주문 설정을 자동 변경하지 않습니다.",
        **({"error": error} if error else {}),
    }


def _reason_matches_group(reason: str, group: str) -> bool:
    reason = str(reason or "").strip().upper()
    if not reason:
        return False
    for pattern in REASON_GROUP_PATTERNS.get(group, ()):
        if reason == pattern or reason.startswith(f"{pattern}_") or pattern in reason:
            return True
    return False


def _performance_row(name: str, values: list[dict[str, Any]], *, key_name: str, config: ConservativeReasonOutcomeConfig) -> dict[str, Any]:
    labeled = [item for item in values if item.get("labeled")]
    missed_count = _count(values, "missed_opportunity")
    strong_count = _count(values, "strong_missed_opportunity")
    good_count = _count(values, "good_block")
    risk_count = _count(values, "risk_avoided")
    false_count = _count(values, "false_block_candidate")
    effective_count = _count(values, "effective_block")
    recommendation = _recommendation(
        labeled_count=len(labeled),
        missed_rate=_ratio(missed_count, len(labeled)),
        good_block_rate=_ratio(good_count, len(labeled)),
        risk_avoided_rate=_ratio(risk_count, len(labeled)),
        config=config,
    )
    confidence = _confidence(len(labeled), config=config)
    row = {
        key_name: name,
        "event_count": len(values),
        "labeled_count": len(labeled),
        "missed_opportunity_count": missed_count,
        "missed_opportunity_rate": _ratio(missed_count, len(labeled)),
        "strong_missed_opportunity_count": strong_count,
        "good_block_count": good_count,
        "good_block_rate": _ratio(good_count, len(labeled)),
        "risk_avoided_count": risk_count,
        "risk_avoided_rate": _ratio(risk_count, len(labeled)),
        "false_block_candidate_count": false_count,
        "effective_block_count": effective_count,
        "avg_return_5m_pct": _avg(item.get("return_5m_pct") for item in labeled),
        "avg_return_15m_pct": _avg(item.get("return_15m_pct") for item in labeled),
        "avg_return_30m_pct": _avg(item.get("return_30m_pct") for item in labeled),
        "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
        "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
        "recommendation": recommendation,
        "recommendation_priority": _recommendation_priority(recommendation),
        "confidence": confidence,
        "operator_message_ko": _recommendation_message(recommendation, name),
        "sample_codes": sorted({str(item.get("code") or "") for item in values if item.get("code")})[:5],
    }
    return row


def _recommendation(
    *,
    labeled_count: int,
    missed_rate: float,
    good_block_rate: float,
    risk_avoided_rate: float,
    config: ConservativeReasonOutcomeConfig,
) -> str:
    if labeled_count <= 0:
        return "DISABLE_ANALYSIS_LOW_QUALITY"
    if labeled_count < int(config.min_sample_count):
        return "DATA_INSUFFICIENT_MORE_SAMPLES"
    if good_block_rate >= float(config.keep_block_min_good_block_rate) or risk_avoided_rate >= float(config.keep_block_min_risk_avoided_rate):
        return "KEEP_BLOCK"
    if missed_rate >= float(config.review_small_entry_min_missed_rate) and risk_avoided_rate <= float(config.review_small_entry_max_risk_avoided_rate):
        return "REVIEW_FOR_SMALL_ENTRY"
    if missed_rate >= float(config.review_small_entry_min_missed_rate):
        return "REVIEW_FOR_RELAXATION"
    return "KEEP_WAIT"


def _item_recommendation(item: dict[str, Any]) -> str:
    if item.get("data_quality_unknown"):
        return "DATA_INSUFFICIENT_MORE_SAMPLES"
    if item.get("good_block") or item.get("chase_protected"):
        return "KEEP_BLOCK"
    if item.get("false_block_candidate"):
        return "REVIEW_FOR_SMALL_ENTRY"
    if item.get("missed_opportunity"):
        return "REVIEW_FOR_RELAXATION"
    return "KEEP_WAIT"


def _operator_message_ko(item: dict[str, Any]) -> str:
    reason_text = " ".join(item.get("reason_codes") or []).upper()
    group = str(item.get("primary_group") or "")
    if item.get("data_quality_unknown"):
        return "아직 outcome 관측 데이터가 부족합니다."
    if item.get("false_block_candidate"):
        return "차단 이후 15분 MFE가 높아 소액 진입 검토 후보입니다."
    if item.get("good_block"):
        return "상승 여지가 작고 MAE가 커서 좋은 차단으로 보입니다."
    if item.get("chase_protected"):
        return "추격 위험 차단이 이후 하락 리스크를 피한 것으로 보입니다."
    if group == "DATA_QUALITY_RISK" or "WARMUP_OPTIONAL" in reason_text:
        return "데이터 품질 사유는 주문 완화가 아니라 관측 샘플을 먼저 늘려야 합니다."
    return "보수적 차단 사유 outcome 관측 결과입니다."


_SMALL_ENTRY_ALLOWED_PRICE_LOCATIONS = {"GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM"}
_SMALL_ENTRY_ALLOWED_ROLES = {"LEADER", "CO_LEADER"}
_SMALL_ENTRY_ALLOWED_RISKS = {"PASS", "RISK_ADJUST"}
_SMALL_ENTRY_EXCLUDED_REASONS = {
    "CHASE_HIGH",
    "VWAP_OVEREXTENDED",
    "VI_ACTIVE",
    "UPPER_LIMIT_HARD_NEAR",
    "CORE_BLOCKING",
    "BACKFILL_ONLY_OBSERVE",
    "GLOBAL_MARKET_RISK_OFF",
    "MARKET_RISK_OFF",
}


def _small_entry_candidate(item: dict[str, Any], *, review_groups: set[str], review_reasons: set[str]) -> bool:
    groups = set(item.get("all_groups") or [])
    reasons = _upper_set(item.get("reason_codes") or [])
    if not (groups & review_groups or reasons & review_reasons or item.get("recommendation") == "REVIEW_FOR_SMALL_ENTRY"):
        return False
    if str(item.get("stock_role") or "").upper() not in _SMALL_ENTRY_ALLOWED_ROLES:
        return False
    if str(item.get("price_location_status") or "").upper() not in _SMALL_ENTRY_ALLOWED_PRICE_LOCATIONS:
        return False
    if str(item.get("risk_level") or "").upper() not in _SMALL_ENTRY_ALLOWED_RISKS:
        return False
    if reasons & _SMALL_ENTRY_EXCLUDED_REASONS:
        return False
    if str(item.get("candidate_market_status") or "").upper() in {"RISK_OFF", "GLOBAL_RISK_OFF", "EXTREME_RISK_OFF"}:
        return False
    return True


def _candidate_group_rows(items: list[dict[str, Any]], field: str, *, config: ConservativeReasonOutcomeConfig) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[str(item.get(field) or "UNKNOWN")].append(item)
    rows: list[dict[str, Any]] = []
    for key, values in grouped.items():
        labeled = [item for item in values if item.get("labeled")]
        rows.append(
            {
                field if field != "primary_reason" else "reason_code": key,
                "group": str(values[0].get("primary_group") or "") if values else "",
                "candidate_count": len(values),
                "labeled_count": len(labeled),
                "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
                "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
                "suggested_position_size_multiplier": _suggested_multiplier(labeled, config=config),
                "suggested_max_orders_per_cycle": int(config.suggested_max_orders_per_cycle),
                "recommendation": "REVIEW_FOR_SMALL_ENTRY",
                "sample_codes": sorted({str(item.get("code") or "") for item in values if item.get("code")})[:5],
            }
        )
    return sorted(rows, key=lambda row: (row["candidate_count"], row.get("avg_mfe_15m_pct") or -999), reverse=True)


def _suggested_multiplier(labeled: list[dict[str, Any]], *, config: ConservativeReasonOutcomeConfig) -> float:
    if len(labeled) < int(config.min_sample_count):
        return float(config.thin_sample_position_size_multiplier)
    risk_rate = _ratio(sum(1 for item in labeled if item.get("risk_avoided")), len(labeled))
    avg_mae = _avg(item.get("mae_15m_pct") for item in labeled)
    if risk_rate > 0.1 or (avg_mae is not None and avg_mae <= -1.0):
        return float(config.medium_position_size_multiplier)
    return float(config.low_risk_position_size_multiplier)


def _primary_label(labels: list[str]) -> str:
    order = [
        "DATA_QUALITY_UNKNOWN",
        "STRONG_MISSED_OPPORTUNITY",
        "MISSED_OPPORTUNITY",
        "CHASE_PROTECTED",
        "GOOD_BLOCK",
        "RISK_AVOIDED",
        "WAIT_RESOLVED_TO_READY",
        "LATE_READY",
        "FALSE_BLOCK_CANDIDATE",
        "EFFECTIVE_BLOCK",
        "NEUTRAL",
    ]
    for label in order:
        if label in labels:
            return label
    return labels[0] if labels else "NEUTRAL"


def _sample_quality(labeled_count: int, *, config: ConservativeReasonOutcomeConfig) -> str:
    if labeled_count <= 0:
        return "LOW"
    if labeled_count < int(config.min_sample_count):
        return "THIN"
    if labeled_count < int(config.strong_sample_count):
        return "MEDIUM"
    return "STRONG"


def _confidence(labeled_count: int, *, config: ConservativeReasonOutcomeConfig) -> float:
    if labeled_count <= 0:
        return 0.0
    if labeled_count >= int(config.strong_sample_count):
        return 0.9
    if labeled_count >= int(config.min_sample_count):
        return 0.65
    return 0.3


def _recommendation_priority(recommendation: str) -> int:
    return {
        "REVIEW_FOR_SMALL_ENTRY": 6,
        "REVIEW_FOR_RELAXATION": 5,
        "KEEP_BLOCK": 4,
        "KEEP_WAIT": 3,
        "DATA_INSUFFICIENT_MORE_SAMPLES": 2,
        "DISABLE_ANALYSIS_LOW_QUALITY": 1,
    }.get(str(recommendation or ""), 0)


def _recommendation_message(recommendation: str, name: str) -> str:
    messages = {
        "KEEP_BLOCK": "차단 사유가 손실 회피에 기여한 표본이 많습니다.",
        "KEEP_WAIT": "현재는 대기/관찰 정책 유지가 적절합니다.",
        "REVIEW_FOR_SMALL_ENTRY": "소액 진입 후보군으로 별도 검토할 가치가 있습니다.",
        "REVIEW_FOR_RELAXATION": "완화 검토 후보지만 주문 설정 자동 변경 대상은 아닙니다.",
        "DATA_INSUFFICIENT_MORE_SAMPLES": "표본이 부족해 추가 관측이 필요합니다.",
        "DISABLE_ANALYSIS_LOW_QUALITY": "관측 품질이 낮아 분석 결과를 의사결정에 쓰기 어렵습니다.",
    }
    return f"{name}: {messages.get(recommendation, '관측 결과를 확인하세요.')}"


def _top_items(items: list[dict[str, Any]], *, key: str, predicate, reverse: bool = True) -> list[dict[str, Any]]:
    filtered = [item for item in items if predicate(item) and _number(item.get(key)) is not None]
    return sorted(filtered, key=lambda item: float(item.get(key) or 0.0), reverse=reverse)[:20]


def _count(items: Iterable[dict[str, Any]], field: str) -> int:
    return sum(1 for item in items if item.get(field))


def _ratio(numerator: int, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [_number(value) for value in values]
    valid = [float(value) for value in numbers if value is not None]
    if not valid:
        return None
    return round(mean(valid), 4)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique_upper(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
        elif isinstance(value, IterableABC):
            parts = [str(part or "").strip() for part in value]
        else:
            parts = [str(value or "").strip()]
        for part in parts:
            text = part.upper()
            if text and text not in result:
                result.append(text)
    return result


def _upper_set(values: Iterable[Any]) -> set[str]:
    return set(_unique_upper(values))


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _csv_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)
