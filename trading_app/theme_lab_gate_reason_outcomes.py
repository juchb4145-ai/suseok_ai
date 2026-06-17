from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

from trading.strategy.candidates import normalize_code


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "theme_lab_gate_reason_outcomes"

DEFAULT_WINDOWS_MIN = (5, 15, 30)
READY_STATUSES = {"READY", "READY_SMALL"}
TRACKED_STATUSES = {"WAIT", "OBSERVE", "BLOCKED"}

CSV_COLUMNS = [
    "trade_date",
    "observed_at",
    "code",
    "name",
    "status",
    "primary_reason",
    "reason_codes",
    "base_price",
    "return_5m_pct",
    "return_15m_pct",
    "return_30m_pct",
    "mfe_15m_pct",
    "mae_15m_pct",
    "would_have_triggered_ready",
    "minutes_to_ready",
    "missed_opportunity",
    "good_block",
    "outcome_label",
    "outcome_data_quality",
    "primary_theme",
    "stock_role",
    "price_location_status",
    "price_location_readiness",
    "candidate_market",
    "condition_level",
    "risk_level",
    "candidate_market_status",
    "data_quality_bucket",
    "data_quality_action",
    "early_small_candidate",
    "early_small_order_enabled",
    "shadow_small_entry_candidate",
    "shadow_small_entry_reason",
    "shadow_small_entry_win_15m",
    "shadow_small_entry_risk_15m",
    "shadow_position_size_multiplier",
]


@dataclass(frozen=True)
class ShadowSmallEntryScenario:
    scenario_id: str
    label: str
    statuses: tuple[str, ...] = ("WAIT", "OBSERVE")
    roles: tuple[str, ...] = ("LEADER", "CO_LEADER")
    min_condition_level: int = 2
    allowed_risks: tuple[str, ...] = ("PASS", "RISK_ADJUST")
    allowed_market_statuses: tuple[str, ...] = ()
    excluded_market_statuses: tuple[str, ...] = ("RISK_OFF",)
    position_size_multiplier: float = 0.25
    return_threshold_pct: float = 3.0
    risk_mae_threshold_pct: float = -2.0
    description: str = ""


def _default_shadow_small_entry_ab_scenarios() -> tuple[ShadowSmallEntryScenario, ...]:
    return (
        ShadowSmallEntryScenario(
            scenario_id="leader_pass_l3_x10",
            label="LEADER PASS L3 x0.10",
            roles=("LEADER",),
            min_condition_level=3,
            allowed_risks=("PASS",),
            position_size_multiplier=0.10,
            description="Strict leader-only, PASS risk, CONDITION3.",
        ),
        ShadowSmallEntryScenario(
            scenario_id="leader_co_pass_l3_x15",
            label="LEADER/CO PASS L3 x0.15",
            roles=("LEADER", "CO_LEADER"),
            min_condition_level=3,
            allowed_risks=("PASS",),
            position_size_multiplier=0.15,
            description="Leader-like names only, PASS risk, CONDITION3.",
        ),
        ShadowSmallEntryScenario(
            scenario_id="leader_co_pass_risk_l2_x10",
            label="LEADER/CO PASS/RISK L2 x0.10",
            roles=("LEADER", "CO_LEADER"),
            min_condition_level=2,
            allowed_risks=("PASS", "RISK_ADJUST"),
            position_size_multiplier=0.10,
            description="Balanced filter with very small pilot sizing.",
        ),
        ShadowSmallEntryScenario(
            scenario_id="leader_co_pass_risk_l2_x15",
            label="LEADER/CO PASS/RISK L2 x0.15",
            roles=("LEADER", "CO_LEADER"),
            min_condition_level=2,
            allowed_risks=("PASS", "RISK_ADJUST"),
            position_size_multiplier=0.15,
            description="Balanced filter with small pilot sizing.",
        ),
        ShadowSmallEntryScenario(
            scenario_id="leader_co_pass_risk_l2_x25",
            label="LEADER/CO PASS/RISK L2 x0.25",
            roles=("LEADER", "CO_LEADER"),
            min_condition_level=2,
            allowed_risks=("PASS", "RISK_ADJUST"),
            position_size_multiplier=0.25,
            description="Current baseline PROVISIONAL shadow policy.",
        ),
        ShadowSmallEntryScenario(
            scenario_id="healthy_leader_co_pass_risk_l2_x15",
            label="HEALTHY LEADER/CO PASS/RISK L2 x0.15",
            roles=("LEADER", "CO_LEADER"),
            min_condition_level=2,
            allowed_risks=("PASS", "RISK_ADJUST"),
            allowed_market_statuses=("HEALTHY",),
            position_size_multiplier=0.15,
            description="Balanced filter, but only when candidate market is healthy.",
        ),
    )


@dataclass(frozen=True)
class ThemeLabGateReasonOutcomeConfig:
    windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN
    label_horizon_min: int = 15
    missed_opportunity_return_threshold_pct: float = 3.0
    good_block_return_threshold_pct: float = 1.0
    dedupe_window_sec: int = 60
    shadow_small_entry_enabled: bool = True
    shadow_small_entry_statuses: tuple[str, ...] = ("WAIT", "OBSERVE")
    shadow_small_entry_roles: tuple[str, ...] = ("LEADER", "CO_LEADER")
    shadow_small_entry_min_condition_level: int = 2
    shadow_small_entry_allowed_risks: tuple[str, ...] = ("PASS", "RISK_ADJUST")
    shadow_small_entry_excluded_market_statuses: tuple[str, ...] = ("RISK_OFF",)
    shadow_small_entry_return_threshold_pct: float = 3.0
    shadow_small_entry_risk_mae_threshold_pct: float = -2.0
    shadow_small_entry_position_size_multiplier: float = 0.25
    shadow_small_entry_ab_scenarios: tuple[ShadowSmallEntryScenario, ...] = field(default_factory=_default_shadow_small_entry_ab_scenarios)


@dataclass
class _Observation:
    at: datetime
    code: str
    price: float
    status: str = ""
    source: str = ""


@dataclass
class _Event:
    observed_at: datetime
    trade_date: str
    code: str
    name: str = ""
    status: str = ""
    primary_reason: str = ""
    reason_codes: list[str] = field(default_factory=list)
    base_price: float = 0.0
    primary_theme: str = ""
    stock_role: str = ""
    price_location_status: str = ""
    price_location_readiness: str = ""
    price_location_provisional: bool = False
    candidate_market: str = ""
    candidate_market_status: str = ""
    condition_level: int = 0
    risk_level: str = ""
    data_quality_bucket: str = ""
    data_quality_action: str = ""
    early_small_candidate: bool = False
    early_small_order_enabled: bool = False
    trade_setup_type: str = ""
    trade_setup_action: str = ""
    trade_setup_confidence_score: Optional[float] = None
    trade_setup_position_size_multiplier: Optional[float] = None
    trade_setup_reason_codes: list[str] = field(default_factory=list)
    trade_setup_operator_message_ko: str = ""
    source_snapshot_id: int = 0


class ThemeLabGateReasonOutcomeAnalyzer:
    def __init__(
        self,
        db,
        *,
        config: Optional[ThemeLabGateReasonOutcomeConfig] = None,
        report_root: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.config = config or ThemeLabGateReasonOutcomeConfig()
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 10000,
        offset: int = 0,
    ) -> dict[str, Any]:
        snapshots = self._load_snapshots(trade_date=trade_date, limit=limit, offset=offset)
        if trade_date is None:
            trade_date = _latest_trade_date(snapshots)
            if trade_date:
                snapshots = [item for item in snapshots if str(item.get("calculated_at") or "").startswith(trade_date)]
        events, observations = self._events_and_snapshot_observations(snapshots)
        observations.extend(self._load_outcome_observations(trade_date, events))
        observations_by_code = _observations_by_code(observations)
        labeled = [self._label_event(event, observations_by_code.get(event.code, [])) for event in self._dedupe_events(events)]
        summary = self._summary(labeled, snapshots, observations)
        report = {
            "report_id": f"theme_lab_gate_reason_outcomes:{trade_date or 'all'}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "trade_date": trade_date or "",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "READY",
            "config": asdict(self.config),
            "summary": summary,
            "by_reason": self._by_reason(labeled),
            "by_status": self._by_status(labeled),
            "top_missed_opportunity_reasons": self._top_missed_opportunity_reasons(labeled),
            "shadow_small_entry": self._shadow_small_entry(labeled),
            "shadow_small_entry_ab": self._shadow_small_entry_ab(labeled),
            "items": labeled,
            "notes": [
                "read_only_observability_report",
                "does_not_modify_gate_thresholds_or_order_logic",
                "uses_theme_lab_snapshots_plus_theme_lab_outcome_tracking_observations",
                "shadow_small_entry_is_simulation_only",
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
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for item in report.get("items") or []:
                writer.writerow({column: _csv_value(item.get(column)) for column in CSV_COLUMNS})
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        lines = [
            f"# ThemeLab Gate Reason Outcomes ({report.get('trade_date') or 'all'})",
            "",
            "This is a read-only observability report. It does not change buy logic, thresholds, or live order settings.",
            "",
            "## Summary",
            f"- event_count: {summary.get('event_count', 0)}",
            f"- labeled_event_count: {summary.get('labeled_event_count', 0)}",
            f"- missed_opportunity_count: {summary.get('missed_opportunity_count', 0)}",
            f"- missed_opportunity_rate: {summary.get('missed_opportunity_rate', 0)}",
            f"- good_block_count: {summary.get('good_block_count', 0)}",
            f"- ready_later_count: {summary.get('ready_later_count', 0)}",
            f"- outcome_observation_count: {summary.get('outcome_observation_count', 0)}",
            "",
            "## Top Missed Opportunity Reasons",
        ]
        for row in report.get("top_missed_opportunity_reasons") or []:
            lines.append(
                f"- {row.get('reason_code')}: {row.get('missed_opportunity_count')}/{row.get('event_count')} "
                f"(avg_mfe_15m_pct={row.get('avg_mfe_15m_pct')})"
            )
        shadow = dict(report.get("shadow_small_entry") or {})
        shadow_summary = dict(shadow.get("summary") or {})
        lines.extend(
            [
                "",
                "## PROVISIONAL Small Entry Shadow",
                f"- candidate_count: {shadow_summary.get('candidate_count', 0)}",
                f"- labeled_count: {shadow_summary.get('labeled_count', 0)}",
                f"- win_rate_15m: {shadow_summary.get('win_rate_15m', 0)}",
                f"- risk_case_rate_15m: {shadow_summary.get('risk_case_rate_15m', 0)}",
                f"- missed_opportunity_capture_count: {shadow_summary.get('missed_opportunity_capture_count', 0)}",
                f"- missed_opportunity_reduction_estimate: {shadow_summary.get('missed_opportunity_reduction_estimate', 0)}",
            ]
        )
        ab = dict(report.get("shadow_small_entry_ab") or {})
        best = list(ab.get("best_scenarios") or [])
        lines.extend(["", "## PROVISIONAL Shadow A/B"])
        if best:
            for row in best[:5]:
                lines.append(
                    f"- {row.get('scenario_id')}: score={row.get('net_shadow_score')}, "
                    f"candidates={row.get('candidate_count')}, win_rate_15m={row.get('win_rate_15m')}, "
                    f"risk_case_rate_15m={row.get('risk_case_rate_15m')}, recommendation={row.get('recommendation')}"
                )
        else:
            lines.append("- no_scenarios_with_candidates")
        lines.extend(
            [
                "",
                "## Notes",
                "- outcome labels are opportunity-review signals, not confirmed strategy errors.",
                "- insufficient rows usually mean the symbol did not have enough future observations within the horizon.",
                "- shadow_small_entry rows are simulated only and do not submit orders.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict, *, report_dir: Path | None = None, stem: str | None = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root / str(report.get("trade_date") or "all")
        stem = stem or f"theme_lab_gate_reason_outcomes_{report.get('trade_date') or 'all'}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def export_report(self, report: dict, *, fmt: str = "json") -> dict[str, str]:
        normalized = "md" if fmt == "markdown" else str(fmt or "json").lower()
        target = self.report_root / str(report.get("trade_date") or "all")
        stem = f"theme_lab_gate_reason_outcomes_{report.get('trade_date') or 'all'}_{datetime.now().strftime('%H%M%S')}"
        if normalized == "all":
            return self.export_all(report, report_dir=target, stem=stem)
        if normalized == "csv":
            return {"csv": str(self.export_csv(report, target / f"{stem}.csv"))}
        if normalized == "md":
            return {"md": str(self.export_markdown(report, target / f"{stem}.md"))}
        return {"json": str(self.export_json(report, target / f"{stem}.json"))}

    def _load_snapshots(self, *, trade_date: Optional[str], limit: int, offset: int) -> list[dict[str, Any]]:
        list_results = getattr(self.db, "list_theme_lab_flow_results", None)
        if callable(list_results):
            return list_results(trade_date=trade_date, limit=max(1, int(limit or 10000)), offset=max(0, int(offset or 0)))
        return []

    def _load_outcome_observations(self, trade_date: str | None, events: list[_Event]) -> list[_Observation]:
        codes = sorted({event.code for event in events if event.code})
        if not codes:
            return []
        list_observations = getattr(self.db, "list_theme_lab_outcome_observations", None)
        if not callable(list_observations):
            return []
        rows = list_observations(trade_date=trade_date, codes=codes, limit=100000)
        result: list[_Observation] = []
        for row in rows:
            at = _parse_time(row.get("observed_at"))
            code = normalize_code(row.get("stock_code") or row.get("code") or "")
            price = _float_or_zero(row.get("price"))
            if at is None or not code or price <= 0:
                continue
            result.append(_Observation(at=at, code=code, price=price, source=str(row.get("source") or "outcome_tracking")))
        return result

    def _events_and_snapshot_observations(self, snapshots: list[dict[str, Any]]) -> tuple[list[_Event], list[_Observation]]:
        events: list[_Event] = []
        observations: list[_Observation] = []
        for snapshot in snapshots:
            snapshot_at = _parse_time(snapshot.get("calculated_at") or snapshot.get("created_at"))
            if snapshot_at is None:
                continue
            decisions = _by_code(snapshot.get("gate_decisions") or [])
            watchset = [dict(item or {}) for item in snapshot.get("watchset_snapshots") or []]
            seen_codes: set[str] = set()
            for watch in watchset:
                code = normalize_code(watch.get("symbol") or watch.get("code") or "")
                if not code:
                    continue
                seen_codes.add(code)
                merged = {**watch, **dict(decisions.get(code) or {})}
                event = _event_from_details(snapshot, snapshot_at, code, merged)
                if event.base_price > 0:
                    observations.append(_Observation(at=snapshot_at, code=code, price=event.base_price, status=event.status, source="theme_lab_snapshot"))
                if _should_track_event(event):
                    events.append(event)
            for code, decision in decisions.items():
                if code in seen_codes:
                    continue
                event = _event_from_details(snapshot, snapshot_at, code, dict(decision or {}))
                if _should_track_event(event):
                    events.append(event)
        return events, observations

    def _dedupe_events(self, events: list[_Event]) -> list[_Event]:
        dedupe_sec = max(0, int(self.config.dedupe_window_sec or 0))
        result: list[_Event] = []
        last_seen: dict[tuple[str, str, str], datetime] = {}
        for event in sorted(events, key=lambda item: (item.observed_at, item.code, item.primary_reason)):
            key = (event.code, event.status, event.primary_reason)
            previous = last_seen.get(key)
            if previous is not None and (event.observed_at - previous).total_seconds() < dedupe_sec:
                continue
            result.append(event)
            last_seen[key] = event.observed_at
        return result

    def _label_event(self, event: _Event, observations: list[_Observation]) -> dict[str, Any]:
        windows = tuple(sorted({int(value) for value in self.config.windows_min if int(value) > 0}))
        followups = [obs for obs in observations if obs.at > event.observed_at and obs.price > 0]
        payload = {
            "event_id": _event_id(event),
            "trade_date": event.trade_date,
            "observed_at": event.observed_at.isoformat(),
            "code": event.code,
            "name": event.name,
            "status": event.status,
            "primary_reason": event.primary_reason,
            "reason_codes": list(event.reason_codes),
            "base_price": event.base_price or None,
            "primary_theme": event.primary_theme,
            "stock_role": event.stock_role,
            "price_location_status": event.price_location_status,
            "price_location_readiness": event.price_location_readiness,
            "price_location_provisional": event.price_location_provisional,
            "candidate_market": event.candidate_market,
            "candidate_market_status": event.candidate_market_status,
            "condition_level": event.condition_level,
            "risk_level": event.risk_level,
            "data_quality_bucket": event.data_quality_bucket,
            "data_quality_action": event.data_quality_action,
            "early_small_candidate": event.early_small_candidate,
            "early_small_order_enabled": event.early_small_order_enabled,
            "trade_setup_type": event.trade_setup_type,
            "trade_setup_action": event.trade_setup_action,
            "trade_setup_confidence_score": event.trade_setup_confidence_score,
            "trade_setup_position_size_multiplier": event.trade_setup_position_size_multiplier,
            "trade_setup_reason_codes": list(event.trade_setup_reason_codes),
            "trade_setup_operator_message_ko": event.trade_setup_operator_message_ko,
            "source_snapshot_id": event.source_snapshot_id,
        }
        if event.base_price <= 0:
            insufficient = {
                **payload,
                "outcome_label": "INSUFFICIENT",
                "outcome_data_quality": "missing_base_price",
                "missed_opportunity": False,
                "good_block": False,
                "would_have_triggered_ready": False,
                "minutes_to_ready": None,
            }
            insufficient.update(self._shadow_small_entry_fields(event, insufficient, {"mfe_pct": None, "mae_pct": None}))
            return insufficient
        if not followups:
            insufficient = {
                **payload,
                "outcome_label": "INSUFFICIENT",
                "outcome_data_quality": "missing_followup_price",
                "missed_opportunity": False,
                "good_block": False,
                "would_have_triggered_ready": False,
                "minutes_to_ready": None,
            }
            insufficient.update(self._shadow_small_entry_fields(event, insufficient, {"mfe_pct": None, "mae_pct": None}))
            return insufficient
        for window in windows:
            metrics = _window_metrics(event, followups, window)
            payload[f"return_{window}m_pct"] = metrics["return_pct"]
            payload[f"mfe_{window}m_pct"] = metrics["mfe_pct"]
            payload[f"mae_{window}m_pct"] = metrics["mae_pct"]
            payload[f"observation_count_{window}m"] = metrics["observation_count"]
        horizon = int(self.config.label_horizon_min)
        horizon_metrics = _window_metrics(event, followups, horizon)
        ready_at = _first_ready_at(event, followups, horizon)
        mfe = horizon_metrics["mfe_pct"]
        status = event.status.upper()
        missed = status in TRACKED_STATUSES and mfe is not None and mfe >= float(self.config.missed_opportunity_return_threshold_pct)
        good_block = status == "BLOCKED" and mfe is not None and mfe < float(self.config.good_block_return_threshold_pct)
        label = "NEUTRAL"
        if missed:
            label = "MISSED_OPPORTUNITY"
        elif good_block:
            label = "GOOD_BLOCK"
        elif ready_at is not None and status in {"WAIT", "OBSERVE"}:
            label = "WAIT_RESOLVED_TO_READY"
        elif status in READY_STATUSES:
            label = "READY_BASELINE"
        payload.update(
            {
                "max_favorable_excursion_pct": mfe,
                "max_adverse_excursion_pct": horizon_metrics["mae_pct"],
                "would_have_triggered_ready": ready_at is not None,
                "minutes_to_ready": round((ready_at - event.observed_at).total_seconds() / 60.0, 4) if ready_at else None,
                "missed_opportunity": missed,
                "good_block": good_block,
                "outcome_label": label,
                "outcome_data_quality": "ready" if horizon_metrics["observation_count"] else "insufficient_horizon",
            }
        )
        payload.update(self._shadow_small_entry_fields(event, payload, horizon_metrics))
        return payload

    def _summary(self, items: list[dict[str, Any]], snapshots: list[dict[str, Any]], observations: list[_Observation]) -> dict[str, Any]:
        labeled = [item for item in items if item.get("outcome_data_quality") == "ready"]
        return {
            "snapshot_count": len(snapshots),
            "event_count": len(items),
            "labeled_event_count": len(labeled),
            "insufficient_count": len(items) - len(labeled),
            "missed_opportunity_count": sum(1 for item in items if item.get("missed_opportunity")),
            "missed_opportunity_rate": _ratio(sum(1 for item in items if item.get("missed_opportunity")), len(labeled)),
            "good_block_count": sum(1 for item in items if item.get("good_block")),
            "ready_later_count": sum(1 for item in items if item.get("would_have_triggered_ready")),
            "snapshot_observation_count": sum(1 for obs in observations if obs.source == "theme_lab_snapshot"),
            "outcome_observation_count": sum(1 for obs in observations if obs.source != "theme_lab_snapshot"),
            "dedupe_window_sec": self.config.dedupe_window_sec,
            "by_data_quality_bucket": self._by_data_quality_bucket(items),
        }

    def _by_data_quality_bucket(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            bucket = str(item.get("data_quality_bucket") or "UNKNOWN")
            grouped[bucket].append(item)
        return sorted(
            (_performance_row(bucket, values, key_name="data_quality_bucket") for bucket, values in grouped.items()),
            key=lambda row: (row["missed_opportunity_count"], row["event_count"], row["data_quality_bucket"]),
            reverse=True,
        )

    def _shadow_small_entry_fields(self, event: _Event, item: dict[str, Any], horizon_metrics: dict[str, Any]) -> dict[str, Any]:
        candidate, reason = _shadow_small_entry_candidate(event, self.config)
        mfe = horizon_metrics.get("mfe_pct")
        mae = horizon_metrics.get("mae_pct")
        labeled = item.get("outcome_data_quality") == "ready"
        win = bool(candidate and labeled and mfe is not None and float(mfe) >= float(self.config.shadow_small_entry_return_threshold_pct))
        risk = bool(candidate and labeled and mae is not None and float(mae) <= float(self.config.shadow_small_entry_risk_mae_threshold_pct))
        return {
            "shadow_small_entry_candidate": candidate,
            "shadow_small_entry_reason": reason,
            "shadow_small_entry_win_15m": win,
            "shadow_small_entry_risk_15m": risk,
            "shadow_position_size_multiplier": float(self.config.shadow_small_entry_position_size_multiplier),
        }

    def _by_reason(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            reasons = list(item.get("reason_codes") or []) or [str(item.get("primary_reason") or "UNKNOWN")]
            for reason in reasons:
                grouped[str(reason or "UNKNOWN")].append(item)
        rows = [_performance_row(reason, values) for reason, values in grouped.items()]
        return sorted(rows, key=lambda row: (row["missed_opportunity_count"], row["event_count"], row["reason_code"]), reverse=True)

    def _by_status(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            grouped[str(item.get("status") or "UNKNOWN")].append(item)
        return sorted((_performance_row(status, values, key_name="status") for status, values in grouped.items()), key=lambda row: row["event_count"], reverse=True)

    def _top_missed_opportunity_reasons(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in self._by_reason(items) if row["missed_opportunity_count"] > 0][:10]

    def _shadow_small_entry(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = [item for item in items if item.get("shadow_small_entry_candidate")]
        labeled = [item for item in candidates if item.get("outcome_data_quality") == "ready"]
        wins = [item for item in labeled if item.get("shadow_small_entry_win_15m")]
        risk_cases = [item for item in labeled if item.get("shadow_small_entry_risk_15m")]
        missed_total = sum(1 for item in items if item.get("missed_opportunity"))
        captured = [item for item in candidates if item.get("missed_opportunity")]
        summary = {
            "candidate_count": len(candidates),
            "labeled_count": len(labeled),
            "win_count_15m": len(wins),
            "win_rate_15m": _ratio(len(wins), len(labeled)),
            "risk_case_count_15m": len(risk_cases),
            "risk_case_rate_15m": _ratio(len(risk_cases), len(labeled)),
            "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
            "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
            "avg_return_15m_pct": _avg(item.get("return_15m_pct") for item in labeled),
            "missed_opportunity_capture_count": len(captured),
            "missed_opportunity_reduction_estimate": _ratio(len(captured), missed_total),
            "position_size_multiplier": float(self.config.shadow_small_entry_position_size_multiplier),
        }
        return {
            "summary": summary,
            "by_reason": _shadow_group_rows(candidates, "primary_reason"),
            "by_role": _shadow_group_rows(candidates, "stock_role"),
            "by_market_status": _shadow_group_rows(candidates, "candidate_market_status"),
            "top_candidates": sorted(
                candidates,
                key=lambda item: (
                    float(item.get("mfe_15m_pct") or -999.0),
                    float(item.get("return_15m_pct") or -999.0),
                    str(item.get("code") or ""),
                ),
                reverse=True,
            )[:20],
            "rejected_reason_counts": dict(Counter(str(item.get("shadow_small_entry_reason") or "") for item in items if not item.get("shadow_small_entry_candidate"))),
        }

    def _shadow_small_entry_ab(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        rows = [_shadow_scenario_row(items, scenario, self.config) for scenario in self.config.shadow_small_entry_ab_scenarios]
        rows = sorted(rows, key=lambda row: (row["net_shadow_score"], row["labeled_count"], row["scenario_id"]), reverse=True)
        return {
            "scenario_count": len(rows),
            "scenarios": rows,
            "best_scenarios": [row for row in rows if row["candidate_count"] > 0][:5],
            "matrix": {
                "by_multiplier": _scenario_matrix(rows, "position_size_multiplier"),
                "by_min_condition_level": _scenario_matrix(rows, "min_condition_level"),
                "by_roles": _scenario_matrix(rows, "roles_key"),
                "by_risk_set": _scenario_matrix(rows, "allowed_risks_key"),
            },
            "notes": [
                "shadow_ab_is_simulation_only",
                "net_shadow_score_rewards_win_rate_and_missed_opportunity_capture_while_penalizing_mae_risk",
                "low_sample_recommendations_should_not_be_promoted_to_dry_run",
            ],
        }


def _event_from_details(snapshot: dict[str, Any], snapshot_at: datetime, code: str, details: dict[str, Any]) -> _Event:
    reasons = _reason_codes(details)
    status = _status(details)
    trade_setup_reason_codes = _list_upper(details.get("trade_setup_reason_codes") or [])
    return _Event(
        observed_at=snapshot_at,
        trade_date=str(details.get("market_trade_date") or snapshot_at.date().isoformat()),
        code=code,
        name=str(details.get("name") or details.get("stock_name") or ""),
        status=status,
        primary_reason=_primary_reason(reasons, status),
        reason_codes=reasons,
        base_price=_first_positive(
            details.get("current_price"),
            details.get("price"),
            details.get("last_price"),
            details.get("entry_price"),
        ),
        primary_theme=str(details.get("primary_theme") or details.get("theme_name") or details.get("theme_id") or ""),
        stock_role=str(details.get("stock_role") or ""),
        price_location_status=str(details.get("price_location_status") or ""),
        price_location_readiness=str(details.get("price_location_readiness") or ""),
        price_location_provisional=bool(details.get("price_location_provisional")),
        candidate_market=str(details.get("candidate_market") or ""),
        candidate_market_status=str(
            details.get("candidate_market_confirmed_status")
            or details.get("candidate_market_status")
            or details.get("candidate_market_raw_status")
            or ""
        ),
        condition_level=int(_float_or_zero(details.get("condition_level"))),
        risk_level=str(details.get("risk_level") or ""),
        data_quality_bucket=str(details.get("data_quality_bucket") or ""),
        data_quality_action=str(details.get("data_quality_action") or ""),
        early_small_candidate=bool(details.get("early_small_candidate")),
        early_small_order_enabled=bool(details.get("early_small_order_enabled")),
        trade_setup_type=str(details.get("trade_setup_type") or "").strip().upper(),
        trade_setup_action=str(details.get("trade_setup_action") or "").strip().upper(),
        trade_setup_confidence_score=_float_or_none(details.get("trade_setup_confidence_score")),
        trade_setup_position_size_multiplier=_float_or_none(details.get("trade_setup_position_size_multiplier")),
        trade_setup_reason_codes=trade_setup_reason_codes,
        trade_setup_operator_message_ko=str(details.get("trade_setup_operator_message_ko") or ""),
        source_snapshot_id=int(snapshot.get("id") or 0),
    )


def _should_track_event(event: _Event) -> bool:
    if event.trade_setup_type and event.trade_setup_type != "UNKNOWN":
        return True
    if event.status.upper() in TRACKED_STATUSES:
        return True
    reasons = {reason.upper() for reason in event.reason_codes}
    return any(reason.startswith(("PRICE_LOCATION_", "WAIT_", "MARKET_", "SIDE_BREADTH_", "MISSING_")) for reason in reasons)


def _status(details: dict[str, Any]) -> str:
    for key in ("status", "final_gate_status", "gate_status", "display_status", "sub_status"):
        value = str(details.get(key) or "").strip().upper()
        if value:
            return value
    return "UNKNOWN"


def _reason_codes(details: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in (
        "reason_codes",
        "price_location_readiness_reason_codes",
        "price_location_reason_codes",
        "market_side_reason_codes",
        "market_session_reason_codes",
        "risk_reason_codes",
        "trade_setup_reason_codes",
        "price_location_data_quality_flags",
        "missing_core_fields",
        "missing_entry_fields",
        "missing_optional_fields",
    ):
        values = details.get(key) or []
        if isinstance(values, str):
            values = [part.strip() for part in values.split(",")]
        for value in values:
            text = str(value or "").strip().upper()
            if text and text not in result:
                result.append(text)
    readiness = str(details.get("price_location_readiness") or "").strip().upper()
    if readiness and readiness not in {"READY", "UNKNOWN"}:
        token = f"PRICE_LOCATION_{readiness}"
        if token not in result:
            result.append(token)
    return result


def _list_upper(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    elif isinstance(values, dict):
        values = values.values()
    else:
        try:
            values = list(values or [])
        except TypeError:
            values = [values]
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip().upper()
        if text and text not in result:
            result.append(text)
    return result


def _primary_reason(reasons: list[str], status: str) -> str:
    if reasons:
        return reasons[0]
    return status or "UNKNOWN"


def _by_code(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        row = dict(row or {})
        code = normalize_code(row.get("symbol") or row.get("code") or "")
        if code:
            result[code] = row
    return result


def _observations_by_code(observations: list[_Observation]) -> dict[str, list[_Observation]]:
    grouped: dict[str, list[_Observation]] = defaultdict(list)
    dedupe: set[tuple[str, datetime, str]] = set()
    for obs in observations:
        key = (obs.code, obs.at, obs.source)
        if key in dedupe:
            continue
        dedupe.add(key)
        grouped[obs.code].append(obs)
    for code in list(grouped):
        grouped[code] = sorted(grouped[code], key=lambda item: item.at)
    return grouped


def _window_metrics(event: _Event, followups: list[_Observation], window_min: int) -> dict[str, Any]:
    end_at = event.observed_at + timedelta(minutes=max(1, int(window_min)))
    values = [obs for obs in followups if obs.at <= end_at]
    if not values:
        return {"return_pct": None, "mfe_pct": None, "mae_pct": None, "observation_count": 0}
    last_price = values[-1].price
    high = max(obs.price for obs in values)
    low = min(obs.price for obs in values)
    return {
        "return_pct": _pct(last_price, event.base_price),
        "mfe_pct": _pct(high, event.base_price),
        "mae_pct": _pct(low, event.base_price),
        "observation_count": len(values),
    }


def _first_ready_at(event: _Event, followups: list[_Observation], horizon_min: int) -> datetime | None:
    end_at = event.observed_at + timedelta(minutes=max(1, int(horizon_min)))
    for obs in followups:
        if obs.at > end_at:
            break
        if obs.status.upper() in READY_STATUSES:
            return obs.at
    return None


def _performance_row(name: str, values: list[dict[str, Any]], *, key_name: str = "reason_code") -> dict[str, Any]:
    labeled = [item for item in values if item.get("outcome_data_quality") == "ready"]
    missed = [item for item in values if item.get("missed_opportunity")]
    good_blocks = [item for item in values if item.get("good_block")]
    ready_later = [item for item in values if item.get("would_have_triggered_ready")]
    row = {
        key_name: name,
        "event_count": len(values),
        "labeled_event_count": len(labeled),
        "missed_opportunity_count": len(missed),
        "missed_opportunity_rate": _ratio(len(missed), len(labeled)),
        "good_block_count": len(good_blocks),
        "good_block_rate": _ratio(len(good_blocks), len(labeled)),
        "ready_later_count": len(ready_later),
        "ready_later_rate": _ratio(len(ready_later), len(labeled)),
        "avg_return_5m_pct": _avg(item.get("return_5m_pct") for item in labeled),
        "avg_return_15m_pct": _avg(item.get("return_15m_pct") for item in labeled),
        "avg_return_30m_pct": _avg(item.get("return_30m_pct") for item in labeled),
        "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
        "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
        "sample_codes": sorted({str(item.get("code") or "") for item in values if item.get("code")})[:5],
    }
    return row


def _shadow_small_entry_candidate(event: _Event, config: ThemeLabGateReasonOutcomeConfig) -> tuple[bool, str]:
    if not config.shadow_small_entry_enabled:
        return False, "SHADOW_SMALL_ENTRY_DISABLED"
    status = event.status.upper()
    if status not in _upper_set(config.shadow_small_entry_statuses):
        return False, "STATUS_NOT_ELIGIBLE"
    readiness = event.price_location_readiness.upper()
    if readiness != "PROVISIONAL" and not event.price_location_provisional:
        return False, "PRICE_LOCATION_NOT_PROVISIONAL"
    if event.stock_role.upper() not in _upper_set(config.shadow_small_entry_roles):
        return False, "ROLE_NOT_LEADER_LIKE"
    if int(event.condition_level or 0) < int(config.shadow_small_entry_min_condition_level or 0):
        return False, "CONDITION_LEVEL_LOW"
    risk = event.risk_level.upper()
    if risk not in _upper_set(config.shadow_small_entry_allowed_risks):
        return False, "RISK_NOT_ALLOWED"
    market_status = event.candidate_market_status.upper()
    if market_status and market_status in _upper_set(config.shadow_small_entry_excluded_market_statuses):
        return False, "MARKET_STATUS_EXCLUDED"
    return True, "PROVISIONAL_SMALL_ENTRY_SHADOW"


def _shadow_group_rows(items: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[str(item.get(field) or "UNKNOWN")].append(item)
    rows: list[dict[str, Any]] = []
    for key, values in grouped.items():
        labeled = [item for item in values if item.get("outcome_data_quality") == "ready"]
        wins = [item for item in labeled if item.get("shadow_small_entry_win_15m")]
        risks = [item for item in labeled if item.get("shadow_small_entry_risk_15m")]
        rows.append(
            {
                field: key,
                "candidate_count": len(values),
                "labeled_count": len(labeled),
                "win_count_15m": len(wins),
                "win_rate_15m": _ratio(len(wins), len(labeled)),
                "risk_case_count_15m": len(risks),
                "risk_case_rate_15m": _ratio(len(risks), len(labeled)),
                "avg_mfe_15m_pct": _avg(item.get("mfe_15m_pct") for item in labeled),
                "avg_mae_15m_pct": _avg(item.get("mae_15m_pct") for item in labeled),
                "sample_codes": sorted({str(item.get("code") or "") for item in values if item.get("code")})[:5],
            }
        )
    return sorted(rows, key=lambda row: (row["candidate_count"], row.get(field, "")), reverse=True)


def _shadow_scenario_row(
    items: list[dict[str, Any]],
    scenario: ShadowSmallEntryScenario,
    config: ThemeLabGateReasonOutcomeConfig,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    reject_reasons: Counter[str] = Counter()
    for item in items:
        selected, reason = _shadow_scenario_candidate(item, scenario)
        if selected:
            candidates.append(item)
        else:
            reject_reasons[reason] += 1
    labeled = [item for item in candidates if item.get("outcome_data_quality") == "ready"]
    wins = [
        item
        for item in labeled
        if _float_or_none(item.get("mfe_15m_pct")) is not None
        and float(item.get("mfe_15m_pct") or 0.0) >= float(scenario.return_threshold_pct)
    ]
    risk_cases = [
        item
        for item in labeled
        if _float_or_none(item.get("mae_15m_pct")) is not None
        and float(item.get("mae_15m_pct") or 0.0) <= float(scenario.risk_mae_threshold_pct)
    ]
    missed_total = sum(1 for item in items if item.get("missed_opportunity"))
    captured = [item for item in candidates if item.get("missed_opportunity")]
    avg_return = _avg(item.get("return_15m_pct") for item in labeled)
    avg_mfe = _avg(item.get("mfe_15m_pct") for item in labeled)
    avg_mae = _avg(item.get("mae_15m_pct") for item in labeled)
    win_rate = _ratio(len(wins), len(labeled))
    risk_rate = _ratio(len(risk_cases), len(labeled))
    capture_rate = _ratio(len(captured), missed_total)
    net_score = _shadow_net_score(
        labeled_count=len(labeled),
        win_rate=win_rate,
        risk_rate=risk_rate,
        capture_rate=capture_rate,
        avg_return_15m=avg_return,
        avg_mae_15m=avg_mae,
    )
    multiplier = float(scenario.position_size_multiplier)
    row = {
        "scenario_id": scenario.scenario_id,
        "label": scenario.label,
        "description": scenario.description,
        "candidate_count": len(candidates),
        "labeled_count": len(labeled),
        "win_count_15m": len(wins),
        "win_rate_15m": win_rate,
        "risk_case_count_15m": len(risk_cases),
        "risk_case_rate_15m": risk_rate,
        "avg_return_15m_pct": avg_return,
        "avg_mfe_15m_pct": avg_mfe,
        "avg_mae_15m_pct": avg_mae,
        "scaled_avg_return_15m_pct": _scale_metric(avg_return, multiplier),
        "scaled_avg_mfe_15m_pct": _scale_metric(avg_mfe, multiplier),
        "scaled_avg_mae_15m_pct": _scale_metric(avg_mae, multiplier),
        "missed_opportunity_capture_count": len(captured),
        "missed_opportunity_reduction_estimate": capture_rate,
        "net_shadow_score": net_score,
        "recommendation": _shadow_recommendation(
            candidate_count=len(candidates),
            labeled_count=len(labeled),
            win_rate=win_rate,
            risk_rate=risk_rate,
            net_score=net_score,
            config=config,
        ),
        "position_size_multiplier": multiplier,
        "statuses": list(scenario.statuses),
        "roles": list(scenario.roles),
        "roles_key": "+".join(scenario.roles),
        "min_condition_level": int(scenario.min_condition_level),
        "allowed_risks": list(scenario.allowed_risks),
        "allowed_risks_key": "+".join(scenario.allowed_risks),
        "allowed_market_statuses": list(scenario.allowed_market_statuses),
        "excluded_market_statuses": list(scenario.excluded_market_statuses),
        "reject_reason_counts": dict(reject_reasons),
        "sample_codes": sorted({str(item.get("code") or "") for item in candidates if item.get("code")})[:5],
    }
    return row


def _shadow_scenario_candidate(item: dict[str, Any], scenario: ShadowSmallEntryScenario) -> tuple[bool, str]:
    status = str(item.get("status") or "").strip().upper()
    if status not in _upper_set(scenario.statuses):
        return False, "STATUS_NOT_ELIGIBLE"
    readiness = str(item.get("price_location_readiness") or "").strip().upper()
    if readiness != "PROVISIONAL" and not bool(item.get("price_location_provisional")):
        return False, "PRICE_LOCATION_NOT_PROVISIONAL"
    if str(item.get("stock_role") or "").strip().upper() not in _upper_set(scenario.roles):
        return False, "ROLE_NOT_ELIGIBLE"
    if int(_float_or_zero(item.get("condition_level"))) < int(scenario.min_condition_level or 0):
        return False, "CONDITION_LEVEL_LOW"
    if str(item.get("risk_level") or "").strip().upper() not in _upper_set(scenario.allowed_risks):
        return False, "RISK_NOT_ALLOWED"
    market_status = str(item.get("candidate_market_status") or "").strip().upper()
    allowed_market_statuses = _upper_set(scenario.allowed_market_statuses)
    if allowed_market_statuses and market_status not in allowed_market_statuses:
        return False, "MARKET_STATUS_NOT_ALLOWED"
    if market_status and market_status in _upper_set(scenario.excluded_market_statuses):
        return False, "MARKET_STATUS_EXCLUDED"
    return True, "SCENARIO_MATCH"


def _scenario_matrix(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(field) or "UNKNOWN")].append(row)
    result: list[dict[str, Any]] = []
    for key, values in grouped.items():
        result.append(
            {
                field: key,
                "scenario_count": len(values),
                "candidate_count": sum(int(row.get("candidate_count") or 0) for row in values),
                "labeled_count": sum(int(row.get("labeled_count") or 0) for row in values),
                "avg_win_rate_15m": _avg(row.get("win_rate_15m") for row in values),
                "avg_risk_case_rate_15m": _avg(row.get("risk_case_rate_15m") for row in values),
                "avg_net_shadow_score": _avg(row.get("net_shadow_score") for row in values),
                "best_scenario_id": max(values, key=lambda row: float(row.get("net_shadow_score") or 0.0)).get("scenario_id", ""),
            }
        )
    return sorted(result, key=lambda row: float(row.get("avg_net_shadow_score") or 0.0), reverse=True)


def _shadow_net_score(
    *,
    labeled_count: int,
    win_rate: float,
    risk_rate: float,
    capture_rate: float,
    avg_return_15m: float | None,
    avg_mae_15m: float | None,
) -> float:
    if labeled_count <= 0:
        return 0.0
    return_component = max(-5.0, min(8.0, float(avg_return_15m or 0.0))) * 4.0
    mae_penalty = abs(min(0.0, float(avg_mae_15m or 0.0))) * 3.0
    sample_discount = min(1.0, labeled_count / 10.0)
    score = ((win_rate * 70.0) + (capture_rate * 35.0) + return_component - (risk_rate * 85.0) - mae_penalty) * sample_discount
    return round(score, 4)


def _shadow_recommendation(
    *,
    candidate_count: int,
    labeled_count: int,
    win_rate: float,
    risk_rate: float,
    net_score: float,
    config: ThemeLabGateReasonOutcomeConfig,
) -> str:
    if candidate_count <= 0:
        return "NO_CANDIDATES"
    if labeled_count < 10:
        return "INSUFFICIENT_SAMPLE"
    if risk_rate >= 0.25:
        return "RISK_TOO_HIGH"
    if win_rate >= 0.55 and net_score >= 35.0:
        return "PROMISING_SHADOW"
    if win_rate >= 0.45 and risk_rate <= 0.15 and net_score >= 20.0:
        return "OBSERVE_MORE"
    return "DO_NOT_PROMOTE"


def _scale_metric(value: float | None, multiplier: float) -> float | None:
    if value is None:
        return None
    return round(float(value) * float(multiplier), 4)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _upper_set(values: Iterable[Any]) -> set[str]:
    return {str(value or "").strip().upper() for value in values}


def _event_id(event: _Event) -> str:
    return f"{event.trade_date}:{event.observed_at.isoformat()}:{event.code}:{event.primary_reason}"


def _latest_trade_date(snapshots: list[dict[str, Any]]) -> str:
    dates = sorted({str(item.get("calculated_at") or "")[:10] for item in snapshots if str(item.get("calculated_at") or "")[:10]})
    return dates[-1] if dates else ""


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def _first_positive(*values: Any) -> float:
    for value in values:
        number = _float_or_zero(value)
        if number > 0:
            return number
    return 0.0


def _float_or_zero(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _pct(value: float, base: float) -> float | None:
    if base <= 0:
        return None
    return round((float(value) / float(base) - 1.0) * 100.0, 4)


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if value not in (None, "")]
    return round(mean(numbers), 4) if numbers else None


def _ratio(part: int, total: int) -> float:
    return round(float(part) / float(total), 4) if total else 0.0


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value
