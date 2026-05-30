from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Optional

from trading.strategy.candles import Candle, minute_start
from trading.strategy.hybrid_gate import HybridGateDecision
from trading.strategy.models import Candidate
from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings


class HybridOutcomeLabel(str, Enum):
    GOOD_READY = "good_ready"
    BAD_READY = "bad_ready"
    GOOD_BLOCK = "good_block"
    FALSE_BLOCK = "false_block"
    GOOD_WAIT = "good_wait"
    MISSED_WAIT = "missed_wait"
    OBSERVE_TO_READY_OPPORTUNITY = "observe_to_ready_opportunity"
    INSUFFICIENT = "insufficient"
    NEUTRAL = "neutral"


@dataclass
class HybridValidationEvent:
    ts: str
    trade_date: str
    stock_code: str
    stock_name: str = ""
    candidate_source: str = ""
    hybrid_status: str = ""
    hybrid_score: float = 0.0
    hybrid_position_tier: str = ""
    hybrid_primary_reason: str = ""
    hybrid_reason_codes: list[str] = field(default_factory=list)
    theme_id: str = ""
    theme_name: str = ""
    theme_status: str = ""
    theme_score: float = 0.0
    theme_rank: int = 0
    theme_rank_delta_1m: int = 0
    theme_rank_delta_5m: int = 0
    theme_breadth: float = 0.0
    rising_count: int = 0
    total_count: int = 0
    leader_gap: float = 0.0
    top3_concentration: float = 0.0
    rank_in_theme: int = 0
    leader_type: str = ""
    membership_score: float = 0.0
    relation_type: str = ""
    source_count: int = 0
    entry_timing_score: float = 0.0
    chase_risk: str = ""
    market_score: float = 0.0
    risk_score: float = 0.0
    position_tier: str = ""
    details_json: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["hybrid_reason_codes"] = list(self.hybrid_reason_codes or [])
        payload["details_json"] = dict(self.details_json or {})
        return payload


@dataclass
class HybridOutcomeMetrics:
    max_return_5m: Optional[float] = None
    max_return_10m: Optional[float] = None
    max_return_25m: Optional[float] = None
    max_return_60m: Optional[float] = None
    mae_5m: Optional[float] = None
    mae_10m: Optional[float] = None
    mae_25m: Optional[float] = None
    mae_60m: Optional[float] = None
    close_return: Optional[float] = None
    time_to_peak_m: Optional[int] = None
    time_to_drawdown_m: Optional[int] = None
    would_hit_take_profit_5pct: bool = False
    would_hit_stop_loss: bool = False
    missed_opportunity: bool = False
    bad_ready: bool = False
    good_block: bool = False
    false_block: bool = False
    good_wait: bool = False
    bad_wait: bool = False
    outcome_label: str = HybridOutcomeLabel.NEUTRAL.value
    outcome_data_quality: str = "ready"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HybridReasonPerformance:
    reason_code: str
    count: int
    avg_max_return_25m: Optional[float]
    avg_mae_25m: Optional[float]
    win_rate_25m: float
    false_block_rate: float
    bad_ready_rate: float
    sample_stocks: list[str] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HybridScoreBandPerformance:
    band: str
    count: int
    ready_count: int = 0
    wait_count: int = 0
    blocked_count: int = 0
    avg_max_return_25m: Optional[float] = None
    avg_mae_25m: Optional[float] = None
    win_rate_25m: float = 0.0
    false_block_rate: float = 0.0
    trade_eligible_success_rate: float = 0.0
    recommended_min_ready_score_candidate: Optional[float] = None
    recommended_min_membership_score_candidate: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HybridWatchPolicyPerformance:
    policy: str
    candidate_count: int
    avg_max_return_25m: Optional[float]
    avg_mae_25m: Optional[float]
    win_rate_25m: float
    stop_risk_rate: float
    missed_opportunity_reduction: float
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HybridValidationSummary:
    trade_date: str
    event_count: int
    status_performance: list[dict[str, Any]] = field(default_factory=list)
    reason_performance: list[HybridReasonPerformance] = field(default_factory=list)
    theme_score_bands: list[HybridScoreBandPerformance] = field(default_factory=list)
    membership_score_bands: list[HybridScoreBandPerformance] = field(default_factory=list)
    watch_policy_performance: list[HybridWatchPolicyPerformance] = field(default_factory=list)
    wait_quality: dict[str, Any] = field(default_factory=dict)
    high_score_failure_cases: list[dict[str, Any]] = field(default_factory=list)
    threshold_relaxation_candidates: list[dict[str, Any]] = field(default_factory=list)
    new_theme_membership_relaxation_candidates: list[dict[str, Any]] = field(default_factory=list)
    false_block_candidates: list[dict[str, Any]] = field(default_factory=list)
    calibration_recommendations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "event_count": self.event_count,
            "status_performance": self.status_performance,
            "reason_performance": [item.to_dict() for item in self.reason_performance],
            "theme_score_bands": [item.to_dict() for item in self.theme_score_bands],
            "membership_score_bands": [item.to_dict() for item in self.membership_score_bands],
            "watch_policy_performance": [item.to_dict() for item in self.watch_policy_performance],
            "wait_quality": dict(self.wait_quality),
            "high_score_failure_cases": list(self.high_score_failure_cases),
            "threshold_relaxation_candidates": list(self.threshold_relaxation_candidates),
            "new_theme_membership_relaxation_candidates": list(self.new_theme_membership_relaxation_candidates),
            "false_block_candidates": list(self.false_block_candidates),
            "calibration_recommendations": dict(self.calibration_recommendations),
        }


@dataclass
class HybridValidationConfig:
    enabled: bool = True
    outcome_windows: list[int] = field(default_factory=lambda: [5, 10, 25, 60])
    good_ready_return_threshold: float = 3.0
    bad_ready_mae_threshold: float = -2.5
    false_block_return_threshold: float = 3.0
    wait_missed_return_threshold: float = 3.0
    score_bands: list[tuple[str, float, float]] = field(default_factory=lambda: [
        ("0_50", 0.0, 50.0),
        ("50_65", 50.0, 65.0),
        ("65_75", 65.0, 75.0),
        ("75_85", 75.0, 85.0),
        ("85_100", 85.0, 100.0001),
    ])
    membership_score_bands: list[tuple[str, float, float]] = field(default_factory=lambda: [
        ("0_0_55", 0.0, 0.55),
        ("0_55_0_65", 0.55, 0.65),
        ("0_65_0_80", 0.65, 0.80),
        ("0_80_1_00", 0.80, 1.0001),
    ])
    watch_policy_shadow_test_enabled: bool = True
    calibration_min_sample_size: int = 20
    calibration_auto_apply: bool = False

    @classmethod
    def from_settings(cls, settings: Optional[StrategyRuntimeSettings] = None) -> "HybridValidationConfig":
        active = settings or legacy_strategy_runtime_settings()
        return cls(
            enabled=_bool_setting(active, "hybrid_validation.enabled", True),
            outcome_windows=[int(value) for value in active.list_value("hybrid_validation.outcome_windows", [5, 10, 25, 60])],
            good_ready_return_threshold=active.number("hybrid_validation.good_ready_return_threshold", 3.0),
            bad_ready_mae_threshold=active.number("hybrid_validation.bad_ready_mae_threshold", -2.5),
            false_block_return_threshold=active.number("hybrid_validation.false_block_return_threshold", 3.0),
            wait_missed_return_threshold=active.number("hybrid_validation.wait_missed_return_threshold", 3.0),
            watch_policy_shadow_test_enabled=_bool_setting(active, "hybrid_validation.watch_policy_shadow_test_enabled", True),
            calibration_min_sample_size=active.integer("hybrid_validation.calibration_min_sample_size", 20),
            calibration_auto_apply=_bool_setting(active, "hybrid_validation.calibration_auto_apply", False),
        )


class HybridValidationRepository:
    def __init__(self, db) -> None:
        self.db = db
        self.conn = db.conn

    def save_event(self, event: HybridValidationEvent) -> HybridValidationEvent:
        cursor = self.conn.execute(
            """
            INSERT INTO hybrid_gate_validation_events(
                created_at, trade_date, stock_code, stock_name, candidate_source,
                hybrid_status, hybrid_score, hybrid_position_tier, hybrid_primary_reason,
                hybrid_reason_codes_json, theme_id, theme_name, theme_status, theme_score,
                theme_rank, theme_rank_delta_1m, theme_rank_delta_5m, theme_breadth,
                rising_count, total_count, leader_gap, top3_concentration, rank_in_theme,
                leader_type, membership_score, relation_type, source_count,
                entry_timing_score, chase_risk, market_score, risk_score, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _event_params(event),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM hybrid_gate_validation_events WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return _row_to_event(row)

    def list_events(
        self,
        *,
        trade_date: Optional[str] = None,
        stock_code: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[HybridValidationEvent]:
        query = "SELECT * FROM hybrid_gate_validation_events"
        clauses = []
        params: list[Any] = []
        if trade_date:
            clauses.append("trade_date = ?")
            params.append(trade_date)
        if stock_code:
            clauses.append("stock_code = ?")
            params.append(stock_code)
        if status:
            clauses.append("hybrid_status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        rows = self.conn.execute(query, params).fetchall()
        return [_row_to_event(row) for row in rows]


def build_validation_event(
    *,
    candidate: Candidate,
    decision: HybridGateDecision,
    ts: str = "",
) -> HybridValidationEvent:
    payload = decision.to_dict()
    details = dict(payload.get("details") or {})
    dynamic_component = dict(payload.get("dynamic_theme_component") or {})
    dynamic_details = dict(dynamic_component.get("details") or {})
    leadership_component = dict(payload.get("stock_leadership_component") or {})
    leadership_details = dict(leadership_component.get("details") or {})
    market_component = dict(payload.get("market_component") or {})
    risk_component = dict(payload.get("risk_component") or {})
    source_values = [
        source.value if hasattr(source, "value") else str(source)
        for source in list(candidate.sources or [])
    ]
    return HybridValidationEvent(
        ts=ts or details.get("ts") or datetime.now().replace(microsecond=0).isoformat(),
        trade_date=candidate.trade_date or _trade_date_from_ts(ts or ""),
        stock_code=candidate.code,
        stock_name=candidate.name,
        candidate_source=",".join(source_values),
        hybrid_status=str(payload.get("status") or ""),
        hybrid_score=float(payload.get("score") or 0.0),
        hybrid_position_tier=str(payload.get("position_tier") or ""),
        hybrid_primary_reason=str(payload.get("primary_reason") or ""),
        hybrid_reason_codes=list(payload.get("reason_codes") or []),
        theme_id=str(details.get("dynamic_theme_id") or dynamic_details.get("theme_id") or ""),
        theme_name=str(details.get("dynamic_theme_name") or dynamic_details.get("theme_name") or ""),
        theme_status=str(details.get("dynamic_theme_status") or dynamic_details.get("theme_status") or ""),
        theme_score=float(details.get("dynamic_theme_score") or dynamic_component.get("score") or 0.0),
        theme_rank=int(details.get("dynamic_theme_rank") or dynamic_details.get("theme_rank") or 0),
        theme_rank_delta_1m=int(details.get("theme_rank_delta_1m") or 0),
        theme_rank_delta_5m=int(details.get("theme_rank_delta_5m") or 0),
        theme_breadth=float(details.get("theme_breadth") or dynamic_details.get("breadth") or 0.0),
        rising_count=int(details.get("rising_count") or dynamic_details.get("rising_count") or 0),
        total_count=int(details.get("total_count") or dynamic_details.get("total_count") or 0),
        leader_gap=float(details.get("leader_gap") or dynamic_details.get("leader_gap") or 0.0),
        top3_concentration=float(details.get("top3_concentration") or dynamic_details.get("top3_concentration") or 0.0),
        rank_in_theme=int(details.get("rank_in_theme") or leadership_details.get("rank_in_theme") or 0),
        leader_type=str(details.get("leader_type") or leadership_details.get("leader_type") or ""),
        membership_score=float(details.get("membership_score") or leadership_details.get("membership_score") or 0.0),
        relation_type=str(details.get("relation_type") or leadership_details.get("relation_type") or ""),
        source_count=int(details.get("source_count") or leadership_details.get("source_count") or 0),
        entry_timing_score=float(details.get("entry_timing_score") or 0.0),
        chase_risk=str(details.get("chase_risk") or ""),
        market_score=float(market_component.get("score") or 0.0),
        risk_score=float(risk_component.get("score") or 0.0),
        position_tier=str(payload.get("position_tier") or ""),
        details_json={
            "hybrid_result": payload,
            "base_price": details.get("base_price") or details.get("price"),
            "legacy_result": details.get("legacy_result"),
        },
    )


def build_validation_event_from_details(candidate: Candidate, details: dict[str, Any], ts: str = "") -> HybridValidationEvent:
    result = details.get("hybrid_result")
    if not isinstance(result, dict):
        raise ValueError("hybrid_result is required to build HybridValidationEvent")
    event = build_validation_event(
        candidate=candidate,
        decision=_decision_from_payload(result),
        ts=ts,
    )
    event.stock_code = candidate.code
    event.stock_name = candidate.name
    event.trade_date = candidate.trade_date or _trade_date_from_ts(ts)
    event.details_json.update({"pipeline_details": dict(details)})
    return event


def label_event_outcome(
    event: HybridValidationEvent,
    candles: list[Candle],
    config: Optional[HybridValidationConfig] = None,
) -> HybridValidationEvent:
    active_config = config or HybridValidationConfig()
    metrics = calculate_outcome_metrics(event, candles, active_config)
    labeled = HybridValidationEvent(**event.to_dict())
    labeled.details_json = dict(event.details_json or {})
    labeled.details_json["outcome"] = metrics.to_dict()
    labeled.details_json["outcome_label"] = metrics.outcome_label
    labeled.details_json["outcome_data_quality"] = metrics.outcome_data_quality
    return labeled


def calculate_outcome_metrics(
    event: HybridValidationEvent,
    candles: list[Candle],
    config: Optional[HybridValidationConfig] = None,
) -> HybridOutcomeMetrics:
    active_config = config or HybridValidationConfig()
    event_time = _parse_time(event.ts)
    selected = _future_candles(candles, event_time)
    if not event_time or not selected:
        return HybridOutcomeMetrics(outcome_label=HybridOutcomeLabel.INSUFFICIENT.value, outcome_data_quality="insufficient")
    base_price = _base_price(event, selected)
    if base_price <= 0:
        return HybridOutcomeMetrics(outcome_label=HybridOutcomeLabel.INSUFFICIENT.value, outcome_data_quality="insufficient")
    metrics = HybridOutcomeMetrics()
    for window in active_config.outcome_windows:
        bucket = _window_candles(selected, event_time, window)
        max_return = _max_return(bucket, base_price)
        mae = _mae(bucket, base_price)
        setattr(metrics, f"max_return_{window}m", max_return)
        setattr(metrics, f"mae_{window}m", mae)
    metrics.close_return = _return_pct(selected[-1].close, base_price) if selected else None
    peak = _peak_candle(selected)
    drawdown = _drawdown_candle(selected)
    metrics.time_to_peak_m = _minutes_after(event_time, peak.start_at) if peak else None
    metrics.time_to_drawdown_m = _minutes_after(event_time, drawdown.start_at) if drawdown else None
    metrics.would_hit_take_profit_5pct = _none_safe(metrics.max_return_25m) >= 5.0 or _none_safe(metrics.max_return_60m) >= 5.0
    metrics.would_hit_stop_loss = _none_safe(metrics.mae_25m, 999.0) <= active_config.bad_ready_mae_threshold
    _label_metrics(metrics, event, selected, base_price, active_config)
    return metrics


def build_validation_summary(
    events: list[HybridValidationEvent],
    config: Optional[HybridValidationConfig] = None,
    *,
    trade_date: str = "",
) -> HybridValidationSummary:
    active_config = config or HybridValidationConfig()
    labeled = [event for event in events if _outcome(event).outcome_data_quality != "insufficient"]
    summary = HybridValidationSummary(
        trade_date=trade_date or (events[0].trade_date if events else ""),
        event_count=len(events),
        status_performance=_status_performance(events),
        reason_performance=_reason_performance(events, active_config),
        theme_score_bands=_score_band_performance(events, active_config.score_bands, "theme_score", active_config),
        membership_score_bands=_score_band_performance(events, active_config.membership_score_bands, "membership_score", active_config),
        watch_policy_performance=_watch_policy_performance(events, active_config),
        wait_quality=_wait_quality(events),
        high_score_failure_cases=_high_score_failure_cases(labeled),
        threshold_relaxation_candidates=_threshold_relaxation_candidates(labeled),
        new_theme_membership_relaxation_candidates=_new_theme_membership_relaxation_candidates(labeled),
        false_block_candidates=_false_block_candidates(labeled),
    )
    summary.calibration_recommendations = generate_calibration_recommendations(summary, active_config)
    return summary


def generate_calibration_recommendations(
    summary: HybridValidationSummary,
    config: Optional[HybridValidationConfig] = None,
) -> dict[str, Any]:
    active_config = config or HybridValidationConfig()
    recommendations: dict[str, Any] = {
        "trade_date": summary.trade_date,
        "auto_apply": active_config.calibration_auto_apply,
        "recommendations": {},
    }
    total = summary.event_count
    low_confidence = total < active_config.calibration_min_sample_size
    confidence = _confidence(total, active_config.calibration_min_sample_size)
    best_theme_band = _best_band(summary.theme_score_bands)
    if best_theme_band and best_theme_band.band == "65_75" and best_theme_band.count > 0:
        recommendations["recommendations"]["hybrid_min_ready_score"] = {
            "current": 75,
            "recommended": 72,
            "reason": "65_75 theme_score band showed positive 25m return with controlled MAE",
            "confidence": confidence,
            "low_sample_size": low_confidence,
        }
    best_membership_band = _best_band(summary.membership_score_bands)
    if best_membership_band and best_membership_band.band == "0_55_0_65":
        recommendations["recommendations"]["min_membership_score"] = {
            "current": 0.55,
            "recommended": 0.52,
            "reason": "0.55-0.65 membership band showed acceptable 25m outcome",
            "confidence": confidence,
            "low_sample_size": low_confidence,
        }
    policy_b = next((item for item in summary.watch_policy_performance if item.policy == "Policy B"), None)
    if policy_b and policy_b.candidate_count and policy_b.win_rate_25m >= 0.55 and policy_b.stop_risk_rate <= 0.25:
        recommendations["recommendations"]["watch_theme_allows_small_entry"] = {
            "current": False,
            "recommended": True,
            "reason": "WATCH Policy B reduced missed opportunities with acceptable MAE",
            "confidence": confidence,
            "low_sample_size": low_confidence,
        }
    low_breadth_reason = next((item for item in summary.reason_performance if item.reason_code == "LOW_BREADTH"), None)
    if low_breadth_reason and low_breadth_reason.win_rate_25m >= 0.55:
        recommendations["recommendations"]["min_theme_breadth"] = {
            "current": 0.35,
            "recommended": 0.30,
            "reason": "LOW_BREADTH candidates still produced favorable 25m outcomes",
            "confidence": confidence,
            "low_sample_size": low_confidence,
        }
    return recommendations


class HybridValidationReportExporter:
    def build_summary(
        self,
        events: list[HybridValidationEvent],
        config: Optional[HybridValidationConfig] = None,
        *,
        trade_date: str = "",
    ) -> HybridValidationSummary:
        return build_validation_summary(events, config, trade_date=trade_date)

    def export_csv(self, events: list[HybridValidationEvent], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "ts",
            "trade_date",
            "stock_code",
            "stock_name",
            "hybrid_status",
            "hybrid_score",
            "hybrid_position_tier",
            "hybrid_primary_reason",
            "theme_id",
            "theme_score",
            "membership_score",
            "rank_in_theme",
            "outcome_label",
            "max_return_25m",
            "mae_25m",
        ]
        with _temp_path(target, encoding="utf-8-sig", newline="") as temp:
            writer = csv.DictWriter(temp.handle, fieldnames=columns)
            writer.writeheader()
            for event in events:
                outcome = _outcome(event)
                row = event.to_dict()
                row.update(outcome.to_dict())
                writer.writerow({column: _cell(row.get(column)) for column in columns})
        return target

    def export_json(
        self,
        events: list[HybridValidationEvent],
        path: str | Path,
        config: Optional[HybridValidationConfig] = None,
        *,
        trade_date: str = "",
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(events, config, trade_date=trade_date)
        with _temp_path(target, encoding="utf-8", newline="\n") as temp:
            json.dump(summary.to_dict(), temp.handle, ensure_ascii=False, indent=2)
            temp.handle.write("\n")
        return target

    def export_markdown(
        self,
        events: list[HybridValidationEvent],
        path: str | Path,
        config: Optional[HybridValidationConfig] = None,
        *,
        trade_date: str = "",
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(events, config, trade_date=trade_date)
        lines = [
            "# Hybrid Gate Validation Summary",
            "",
            f"- Trade date: {summary.trade_date}",
            f"- Events: {summary.event_count}",
            "",
            "## Hybrid Status Performance",
            "",
        ]
        lines.extend(_status_table(summary.status_performance))
        lines.extend(["", "## Hybrid Reason Code Performance", ""])
        lines.extend(_reason_table(summary.reason_performance))
        lines.extend(["", "## Theme Score Band Performance", ""])
        lines.extend(_band_table(summary.theme_score_bands))
        lines.extend(["", "## Membership Score Band Performance", ""])
        lines.extend(_band_table(summary.membership_score_bands))
        lines.extend(["", "## WATCH Theme Small Entry Policy Review", ""])
        lines.extend(_watch_policy_table(summary.watch_policy_performance))
        lines.extend(["", "## WAIT Quality Review", ""])
        lines.extend(_dict_rows(summary.wait_quality))
        lines.extend(["", "## High Score Failure Cases", ""])
        lines.extend(_case_rows(summary.high_score_failure_cases))
        lines.extend(["", "## False Block Candidates", ""])
        lines.extend(_case_rows(summary.false_block_candidates))
        lines.extend(["", "## Calibration Recommendations", ""])
        lines.extend(_recommendation_rows(summary.calibration_recommendations))
        with _temp_path(target, encoding="utf-8", newline="\n") as temp:
            temp.handle.write("\n".join(lines) + "\n")
        return target

    def export_recommendations_json(
        self,
        events: list[HybridValidationEvent],
        path: str | Path,
        config: Optional[HybridValidationConfig] = None,
        *,
        trade_date: str = "",
    ) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(events, config, trade_date=trade_date)
        with _temp_path(target, encoding="utf-8", newline="\n") as temp:
            json.dump(summary.calibration_recommendations, temp.handle, ensure_ascii=False, indent=2)
            temp.handle.write("\n")
        return target


def _label_metrics(
    metrics: HybridOutcomeMetrics,
    event: HybridValidationEvent,
    candles: list[Candle],
    base_price: float,
    config: HybridValidationConfig,
) -> None:
    status = event.hybrid_status.upper()
    max25 = _none_safe(metrics.max_return_25m)
    max10 = _none_safe(metrics.max_return_10m)
    mae25 = _none_safe(metrics.mae_25m, 999.0)
    metrics.would_hit_stop_loss = mae25 <= config.bad_ready_mae_threshold
    if status == "READY":
        if max25 >= config.good_ready_return_threshold and mae25 > -2.0:
            metrics.outcome_label = HybridOutcomeLabel.GOOD_READY.value
        elif mae25 <= config.bad_ready_mae_threshold and max25 < 1.0:
            metrics.bad_ready = True
            metrics.outcome_label = HybridOutcomeLabel.BAD_READY.value
        return
    if status == "BLOCKED":
        if max25 >= config.false_block_return_threshold:
            metrics.false_block = True
            metrics.outcome_label = HybridOutcomeLabel.FALSE_BLOCK.value
        elif max25 < 1.0 or mae25 <= -2.0:
            metrics.good_block = True
            metrics.outcome_label = HybridOutcomeLabel.GOOD_BLOCK.value
        return
    if status == "WAIT":
        if max10 >= config.wait_missed_return_threshold and _none_safe(metrics.mae_10m, 999.0) > -1.0:
            metrics.missed_opportunity = True
            metrics.bad_wait = True
            metrics.outcome_label = HybridOutcomeLabel.MISSED_WAIT.value
        elif _has_better_pullback_then_rebound(candles, base_price) or mae25 <= -2.0:
            metrics.good_wait = True
            metrics.outcome_label = HybridOutcomeLabel.GOOD_WAIT.value
        return
    if status == "OBSERVE" and max25 >= config.false_block_return_threshold and str(event.theme_status).upper() == "WATCH":
        metrics.outcome_label = HybridOutcomeLabel.OBSERVE_TO_READY_OPPORTUNITY.value


def _status_performance(events: list[HybridValidationEvent]) -> list[dict[str, Any]]:
    rows = []
    for status in ["READY", "WAIT", "BLOCKED", "OBSERVE"]:
        items = [event for event in events if event.hybrid_status.upper() == status]
        valid = [event for event in items if _outcome(event).outcome_data_quality != "insufficient"]
        rows.append({
            "status": status,
            "count": len(items),
            "avg_max_return_5m": _avg([_outcome(event).max_return_5m for event in valid]),
            "avg_max_return_10m": _avg([_outcome(event).max_return_10m for event in valid]),
            "avg_max_return_25m": _avg([_outcome(event).max_return_25m for event in valid]),
            "avg_max_return_60m": _avg([_outcome(event).max_return_60m for event in valid]),
            "avg_mae_25m": _avg([_outcome(event).mae_25m for event in valid]),
            "win_rate_25m": _rate(sum(1 for event in valid if _none_safe(_outcome(event).max_return_25m) >= 3.0), len(valid)),
            "good_ready_count": _label_count(items, HybridOutcomeLabel.GOOD_READY),
            "bad_ready_count": _label_count(items, HybridOutcomeLabel.BAD_READY),
            "good_block_count": _label_count(items, HybridOutcomeLabel.GOOD_BLOCK),
            "false_block_count": _label_count(items, HybridOutcomeLabel.FALSE_BLOCK),
            "good_wait_count": _label_count(items, HybridOutcomeLabel.GOOD_WAIT),
            "missed_wait_count": _label_count(items, HybridOutcomeLabel.MISSED_WAIT),
            "insufficient_data_count": len(items) - len(valid),
        })
    return rows


def _reason_performance(events: list[HybridValidationEvent], config: HybridValidationConfig) -> list[HybridReasonPerformance]:
    reason_codes = [
        "LOW_BREADTH",
        "LEADER_ONLY_THEME",
        "LEADER_ONLY_THEME_LAGGARD_BLOCK",
        "LATE_LAGGARD",
        "CHASE_RISK",
        "LOW_MEMBERSHIP_SCORE",
        "NO_ACTIVE_THEME",
        "THEME_CONTEXT_NOT_READY",
        "STRONG_ACTIVE_THEME",
        "WATCH_THEME_EARLY",
        "WATCH_THEME_OBSERVE_ONLY",
        "STRONG_THEME_ENTRY_NOT_READY",
    ]
    rows = []
    for reason in reason_codes:
        items = [event for event in events if reason in event.hybrid_reason_codes]
        valid = [event for event in items if _outcome(event).outcome_data_quality != "insufficient"]
        if not items:
            continue
        false_block_rate = _rate(_label_count(valid, HybridOutcomeLabel.FALSE_BLOCK), len(valid))
        bad_ready_rate = _rate(_label_count(valid, HybridOutcomeLabel.BAD_READY), len(valid))
        rows.append(
            HybridReasonPerformance(
                reason_code=reason,
                count=len(items),
                avg_max_return_25m=_avg([_outcome(event).max_return_25m for event in valid]),
                avg_mae_25m=_avg([_outcome(event).mae_25m for event in valid]),
                win_rate_25m=_rate(sum(1 for event in valid if _none_safe(_outcome(event).max_return_25m) >= config.good_ready_return_threshold), len(valid)),
                false_block_rate=false_block_rate,
                bad_ready_rate=bad_ready_rate,
                sample_stocks=_samples(items),
                recommendation=_reason_recommendation(reason, valid, config),
            )
        )
    return rows


def _score_band_performance(
    events: list[HybridValidationEvent],
    bands: list[tuple[str, float, float]],
    field_name: str,
    config: HybridValidationConfig,
) -> list[HybridScoreBandPerformance]:
    rows = []
    for label, low, high in bands:
        items = [event for event in events if low <= float(getattr(event, field_name) or 0.0) < high]
        valid = [event for event in items if _outcome(event).outcome_data_quality != "insufficient"]
        rows.append(
            HybridScoreBandPerformance(
                band=label,
                count=len(items),
                ready_count=sum(1 for event in items if event.hybrid_status == "READY"),
                wait_count=sum(1 for event in items if event.hybrid_status == "WAIT"),
                blocked_count=sum(1 for event in items if event.hybrid_status == "BLOCKED"),
                avg_max_return_25m=_avg([_outcome(event).max_return_25m for event in valid]),
                avg_mae_25m=_avg([_outcome(event).mae_25m for event in valid]),
                win_rate_25m=_rate(sum(1 for event in valid if _none_safe(_outcome(event).max_return_25m) >= config.good_ready_return_threshold), len(valid)),
                false_block_rate=_rate(_label_count(valid, HybridOutcomeLabel.FALSE_BLOCK), len(valid)),
                trade_eligible_success_rate=_rate(sum(1 for event in valid if _none_safe(_outcome(event).max_return_25m) >= config.good_ready_return_threshold), len(valid)),
                recommended_min_ready_score_candidate=low if field_name == "theme_score" and label in {"65_75", "75_85", "85_100"} else None,
                recommended_min_membership_score_candidate=low if field_name == "membership_score" and label != "0_0_55" else None,
            )
        )
    return rows


def _watch_policy_performance(events: list[HybridValidationEvent], config: HybridValidationConfig) -> list[HybridWatchPolicyPerformance]:
    watch = [event for event in events if event.theme_status.upper() == "WATCH"]
    policy_a = [event for event in watch if event.hybrid_position_tier == "observe_only" or event.hybrid_status == "OBSERVE"]
    policy_b = [
        event for event in watch
        if event.leader_type in {"leader", "co_leader"} and event.entry_timing_score >= 65 and str(event.chase_risk).lower() != "true"
    ]
    policy_c = [
        event for event in watch
        if event.theme_rank_delta_5m > 0 and event.theme_breadth >= 0.35 and str(event.chase_risk).lower() != "true"
    ]
    return [
        _watch_policy_row("Policy A", policy_a, watch, config, "Keep WATCH themes observe-only."),
        _watch_policy_row("Policy B", policy_b, watch, config, "Allow WATCH leaders with clean entry as small-first-entry shadow candidates."),
        _watch_policy_row("Policy C", policy_c, watch, config, "Allow WATCH themes only when rank and breadth are improving."),
    ]


def _watch_policy_row(
    name: str,
    items: list[HybridValidationEvent],
    baseline: list[HybridValidationEvent],
    config: HybridValidationConfig,
    recommendation: str,
) -> HybridWatchPolicyPerformance:
    valid = [event for event in items if _outcome(event).outcome_data_quality != "insufficient"]
    baseline_missed = sum(1 for event in baseline if _outcome(event).missed_opportunity)
    missed = sum(1 for event in items if _outcome(event).missed_opportunity)
    reduction = max(0.0, (baseline_missed - missed) / baseline_missed) if baseline_missed else 0.0
    return HybridWatchPolicyPerformance(
        policy=name,
        candidate_count=len(items),
        avg_max_return_25m=_avg([_outcome(event).max_return_25m for event in valid]),
        avg_mae_25m=_avg([_outcome(event).mae_25m for event in valid]),
        win_rate_25m=_rate(sum(1 for event in valid if _none_safe(_outcome(event).max_return_25m) >= config.good_ready_return_threshold), len(valid)),
        stop_risk_rate=_rate(sum(1 for event in valid if _outcome(event).would_hit_stop_loss), len(valid)),
        missed_opportunity_reduction=round(reduction, 4),
        recommendation=recommendation,
    )


def _wait_quality(events: list[HybridValidationEvent]) -> dict[str, Any]:
    wait = [event for event in events if event.hybrid_status == "WAIT"]
    valid = [event for event in wait if _outcome(event).outcome_data_quality != "insufficient"]
    return {
        "wait_count": len(wait),
        "wait_then_better_pullback_count": _label_count(valid, HybridOutcomeLabel.GOOD_WAIT),
        "wait_then_immediate_breakout_count": _label_count(valid, HybridOutcomeLabel.MISSED_WAIT),
        "wait_then_breakdown_count": sum(1 for event in valid if _none_safe(_outcome(event).mae_25m, 999.0) <= -2.5),
        "avg_time_to_better_entry": _avg([_outcome(event).time_to_drawdown_m for event in valid if _outcome(event).good_wait]),
        "missed_wait_count": _label_count(valid, HybridOutcomeLabel.MISSED_WAIT),
        "good_wait_count": _label_count(valid, HybridOutcomeLabel.GOOD_WAIT),
    }


def _high_score_failure_cases(events: list[HybridValidationEvent]) -> list[dict[str, Any]]:
    return [
        _case(event)
        for event in events
        if event.theme_score >= 85 and _outcome(event).outcome_label in {HybridOutcomeLabel.BAD_READY.value, HybridOutcomeLabel.MISSED_WAIT.value}
    ]


def _threshold_relaxation_candidates(events: list[HybridValidationEvent]) -> list[dict[str, Any]]:
    return [
        _case(event)
        for event in events
        if 65 <= event.theme_score < 75 and _none_safe(_outcome(event).max_return_25m) >= 3.0 and _none_safe(_outcome(event).mae_25m, 999.0) > -2.0
    ]


def _new_theme_membership_relaxation_candidates(events: list[HybridValidationEvent]) -> list[dict[str, Any]]:
    return [
        _case(event)
        for event in events
        if event.source_count <= 1 and 0.45 <= event.membership_score < 0.65 and _none_safe(_outcome(event).max_return_25m) >= 3.0
    ]


def _false_block_candidates(events: list[HybridValidationEvent]) -> list[dict[str, Any]]:
    return [_case(event) for event in events if _outcome(event).outcome_label == HybridOutcomeLabel.FALSE_BLOCK.value]


def _reason_recommendation(reason: str, events: list[HybridValidationEvent], config: HybridValidationConfig) -> str:
    if not events:
        return "insufficient sample"
    avg_return = _avg([_outcome(event).max_return_25m for event in events]) or 0.0
    avg_mae = _avg([_outcome(event).mae_25m for event in events]) or 0.0
    if reason in {"LOW_BREADTH", "LEADER_ONLY_THEME", "LEADER_ONLY_THEME_LAGGARD_BLOCK", "LATE_LAGGARD"}:
        if avg_return < 1.0 or avg_mae <= -2.0:
            return f"{reason} block/wait rule 유지"
        return f"{reason} threshold 완화 후보"
    if reason == "CHASE_RISK":
        if avg_return >= config.good_ready_return_threshold:
            return "chase_risk 기준 과도 가능성"
        return "chase_risk 방어 유지"
    if reason == "LOW_MEMBERSHIP_SCORE":
        if avg_return >= config.good_ready_return_threshold:
            return "min_membership_score 완화 후보"
        return "membership 차단 유지"
    return "observe and accumulate sample"


def _outcome(event: HybridValidationEvent) -> HybridOutcomeMetrics:
    raw = dict(event.details_json or {}).get("outcome")
    if isinstance(raw, dict):
        return HybridOutcomeMetrics(**{key: raw.get(key) for key in HybridOutcomeMetrics.__dataclass_fields__ if key in raw})
    return HybridOutcomeMetrics(outcome_label=HybridOutcomeLabel.INSUFFICIENT.value, outcome_data_quality="insufficient")


def _event_params(event: HybridValidationEvent) -> tuple:
    return (
        event.created_at or event.ts,
        event.trade_date,
        event.stock_code,
        event.stock_name,
        event.candidate_source,
        event.hybrid_status,
        float(event.hybrid_score),
        event.hybrid_position_tier,
        event.hybrid_primary_reason,
        json.dumps(event.hybrid_reason_codes, ensure_ascii=False),
        event.theme_id,
        event.theme_name,
        event.theme_status,
        float(event.theme_score),
        int(event.theme_rank),
        int(event.theme_rank_delta_1m),
        int(event.theme_rank_delta_5m),
        float(event.theme_breadth),
        int(event.rising_count),
        int(event.total_count),
        float(event.leader_gap),
        float(event.top3_concentration),
        int(event.rank_in_theme),
        event.leader_type,
        float(event.membership_score),
        event.relation_type,
        int(event.source_count),
        float(event.entry_timing_score),
        str(event.chase_risk),
        float(event.market_score),
        float(event.risk_score),
        json.dumps(event.details_json, ensure_ascii=False),
    )


def _row_to_event(row) -> HybridValidationEvent:
    return HybridValidationEvent(
        id=int(row["id"]),
        created_at=row["created_at"],
        ts=row["created_at"],
        trade_date=row["trade_date"],
        stock_code=row["stock_code"],
        stock_name=row["stock_name"],
        candidate_source=row["candidate_source"],
        hybrid_status=row["hybrid_status"],
        hybrid_score=float(row["hybrid_score"]),
        hybrid_position_tier=row["hybrid_position_tier"],
        hybrid_primary_reason=row["hybrid_primary_reason"],
        hybrid_reason_codes=list(json.loads(row["hybrid_reason_codes_json"] or "[]")),
        theme_id=row["theme_id"],
        theme_name=row["theme_name"],
        theme_status=row["theme_status"],
        theme_score=float(row["theme_score"]),
        theme_rank=int(row["theme_rank"]),
        theme_rank_delta_1m=int(row["theme_rank_delta_1m"]),
        theme_rank_delta_5m=int(row["theme_rank_delta_5m"]),
        theme_breadth=float(row["theme_breadth"]),
        rising_count=int(row["rising_count"]),
        total_count=int(row["total_count"]),
        leader_gap=float(row["leader_gap"]),
        top3_concentration=float(row["top3_concentration"]),
        rank_in_theme=int(row["rank_in_theme"]),
        leader_type=row["leader_type"],
        membership_score=float(row["membership_score"]),
        relation_type=row["relation_type"],
        source_count=int(row["source_count"]),
        entry_timing_score=float(row["entry_timing_score"]),
        chase_risk=row["chase_risk"],
        market_score=float(row["market_score"]),
        risk_score=float(row["risk_score"]),
        position_tier=row["hybrid_position_tier"],
        details_json=dict(json.loads(row["details_json"] or "{}")),
    )


def _decision_from_payload(payload: dict[str, Any]) -> HybridGateDecision:
    from trading.strategy.hybrid_gate import HybridGateComponent

    def component(name: str) -> HybridGateComponent:
        raw = dict(payload.get(name) or {})
        return HybridGateComponent(
            name=str(raw.get("name") or name),
            score=float(raw.get("score") or 0.0),
            status=str(raw.get("status") or ""),
            reason_codes=list(raw.get("reason_codes") or []),
            details=dict(raw.get("details") or {}),
        )

    return HybridGateDecision(
        status=str(payload.get("status") or ""),
        score=float(payload.get("score") or 0.0),
        position_tier=str(payload.get("position_tier") or ""),
        primary_reason=str(payload.get("primary_reason") or ""),
        reason_codes=list(payload.get("reason_codes") or []),
        dynamic_theme_component=component("dynamic_theme_component"),
        stock_leadership_component=component("stock_leadership_component"),
        entry_timing_component=component("entry_timing_component"),
        market_component=component("market_component"),
        risk_component=component("risk_component"),
        details=dict(payload.get("details") or {}),
    )


def _future_candles(candles: list[Candle], event_time: Optional[datetime]) -> list[Candle]:
    if event_time is None:
        return []
    start = minute_start(event_time)
    return sorted([candle for candle in candles if candle.start_at > start], key=lambda item: item.start_at)


def _window_candles(candles: list[Candle], event_time: datetime, minutes: int) -> list[Candle]:
    end = event_time + timedelta(minutes=minutes)
    return [candle for candle in candles if candle.start_at <= end]


def _base_price(event: HybridValidationEvent, candles: list[Candle]) -> float:
    details = dict(event.details_json or {})
    for key in ["base_price", "price", "entry_price"]:
        try:
            value = float(details.get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return float(candles[0].open) if candles else 0.0


def _max_return(candles: list[Candle], base_price: float) -> Optional[float]:
    if not candles or base_price <= 0:
        return None
    return round(((max(candle.high for candle in candles) - base_price) / base_price) * 100.0, 6)


def _mae(candles: list[Candle], base_price: float) -> Optional[float]:
    if not candles or base_price <= 0:
        return None
    return round(((min(candle.low for candle in candles) - base_price) / base_price) * 100.0, 6)


def _return_pct(price: float, base_price: float) -> float:
    if base_price <= 0:
        return 0.0
    return round(((price - base_price) / base_price) * 100.0, 6)


def _peak_candle(candles: list[Candle]) -> Optional[Candle]:
    return max(candles, key=lambda candle: candle.high) if candles else None


def _drawdown_candle(candles: list[Candle]) -> Optional[Candle]:
    return min(candles, key=lambda candle: candle.low) if candles else None


def _minutes_after(start: datetime, value: datetime) -> int:
    return max(0, int((value - start).total_seconds() // 60))


def _has_better_pullback_then_rebound(candles: list[Candle], base_price: float) -> bool:
    if not candles or base_price <= 0:
        return False
    lowest_index = min(range(len(candles)), key=lambda index: candles[index].low)
    pullback = _return_pct(candles[lowest_index].low, base_price)
    if pullback > -1.0:
        return False
    later = candles[lowest_index + 1 :]
    return bool(later) and _max_return(later, base_price) is not None and _max_return(later, base_price) >= 1.0


def _parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _trade_date_from_ts(value: str) -> str:
    parsed = _parse_time(value)
    return parsed.date().isoformat() if parsed else ""


def _bool_setting(settings: StrategyRuntimeSettings, path: str, default: bool) -> bool:
    value = settings.value(path, default)
    return value if type(value) is bool else bool(default)


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    numbers = [float(value) for value in values if value is not None]
    return round(sum(numbers) / len(numbers), 6) if numbers else None


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _none_safe(value: Optional[float], fallback: float = 0.0) -> float:
    return fallback if value is None else float(value)


def _label_count(events: list[HybridValidationEvent], label: HybridOutcomeLabel) -> int:
    return sum(1 for event in events if _outcome(event).outcome_label == label.value)


def _samples(events: list[HybridValidationEvent]) -> list[str]:
    result = []
    for event in events:
        if event.stock_code and event.stock_code not in result:
            result.append(event.stock_code)
        if len(result) >= 5:
            break
    return result


def _case(event: HybridValidationEvent) -> dict[str, Any]:
    outcome = _outcome(event)
    return {
        "stock_code": event.stock_code,
        "stock_name": event.stock_name,
        "hybrid_status": event.hybrid_status,
        "theme_id": event.theme_id,
        "theme_score": event.theme_score,
        "membership_score": event.membership_score,
        "reason_codes": list(event.hybrid_reason_codes),
        "outcome_label": outcome.outcome_label,
        "max_return_25m": outcome.max_return_25m,
        "mae_25m": outcome.mae_25m,
    }


def _best_band(bands: list[HybridScoreBandPerformance]) -> Optional[HybridScoreBandPerformance]:
    candidates = [band for band in bands if band.count > 0 and band.avg_max_return_25m is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda band: (band.win_rate_25m, band.avg_max_return_25m or 0.0, -(band.avg_mae_25m or 0.0)))


def _confidence(count: int, min_sample_size: int) -> float:
    if min_sample_size <= 0:
        return 1.0
    return round(min(0.85, max(0.15, count / (min_sample_size * 2))), 4)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _status_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Status | Count | Avg 25m High | Avg 25m MAE | Win Rate | Good Ready | Bad Ready | Good Block | False Block | Good Wait | Missed Wait | Insufficient |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _cell(row.get(key))
                for key in [
                    "status",
                    "count",
                    "avg_max_return_25m",
                    "avg_mae_25m",
                    "win_rate_25m",
                    "good_ready_count",
                    "bad_ready_count",
                    "good_block_count",
                    "false_block_count",
                    "good_wait_count",
                    "missed_wait_count",
                    "insufficient_data_count",
                ]
            )
            + " |"
        )
    return lines


def _reason_table(rows: list[HybridReasonPerformance]) -> list[str]:
    if not rows:
        return ["_None._"]
    lines = [
        "| Reason | Count | Avg 25m High | Avg 25m MAE | Win Rate | False Block | Bad Ready | Samples | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {_cell(row.reason_code)} | {_cell(row.count)} | {_cell(row.avg_max_return_25m)} | {_cell(row.avg_mae_25m)} | {_cell(row.win_rate_25m)} | {_cell(row.false_block_rate)} | {_cell(row.bad_ready_rate)} | {_cell(', '.join(row.sample_stocks))} | {_cell(row.recommendation)} |"
        )
    return lines


def _band_table(rows: list[HybridScoreBandPerformance]) -> list[str]:
    lines = [
        "| Band | Count | READY | WAIT | BLOCKED | Avg 25m High | Avg 25m MAE | Win Rate | False Block |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {_cell(row.band)} | {_cell(row.count)} | {_cell(row.ready_count)} | {_cell(row.wait_count)} | {_cell(row.blocked_count)} | {_cell(row.avg_max_return_25m)} | {_cell(row.avg_mae_25m)} | {_cell(row.win_rate_25m)} | {_cell(row.false_block_rate)} |"
        )
    return lines


def _watch_policy_table(rows: list[HybridWatchPolicyPerformance]) -> list[str]:
    lines = [
        "| Policy | Count | Avg 25m High | Avg 25m MAE | Win Rate | Stop Risk | Missed Reduction | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {_cell(row.policy)} | {_cell(row.candidate_count)} | {_cell(row.avg_max_return_25m)} | {_cell(row.avg_mae_25m)} | {_cell(row.win_rate_25m)} | {_cell(row.stop_risk_rate)} | {_cell(row.missed_opportunity_reduction)} | {_cell(row.recommendation)} |"
        )
    return lines


def _dict_rows(values: dict[str, Any]) -> list[str]:
    return [f"- {key}: {_cell(value)}" for key, value in values.items()] if values else ["_None._"]


def _case_rows(cases: list[dict[str, Any]]) -> list[str]:
    if not cases:
        return ["_None._"]
    lines = ["| Code | Theme | Status | Theme Score | Membership | Outcome | 25m High | 25m MAE | Reasons |", "|---|---|---|---:|---:|---|---:|---:|---|"]
    for case in cases[:20]:
        lines.append(
            f"| {_cell(case.get('stock_code'))} | {_cell(case.get('theme_id'))} | {_cell(case.get('hybrid_status'))} | {_cell(case.get('theme_score'))} | {_cell(case.get('membership_score'))} | {_cell(case.get('outcome_label'))} | {_cell(case.get('max_return_25m'))} | {_cell(case.get('mae_25m'))} | {_cell(', '.join(case.get('reason_codes') or []))} |"
        )
    return lines


def _recommendation_rows(payload: dict[str, Any]) -> list[str]:
    recommendations = dict(payload.get("recommendations") or {}) if payload else {}
    if not recommendations:
        return ["_No recommendation. Keep observing._"]
    lines = ["| Setting | Current | Recommended | Confidence | Reason |", "|---|---:|---:|---:|---|"]
    for key, item in recommendations.items():
        lines.append(
            f"| {_cell(key)} | {_cell(item.get('current'))} | {_cell(item.get('recommended'))} | {_cell(item.get('confidence'))} | {_cell(item.get('reason'))} |"
        )
    return lines


class _temp_path:
    def __init__(self, target: Path, encoding: str, newline: str) -> None:
        self.target = target
        self.encoding = encoding
        self.newline = newline
        self.handle = None
        self.path = None

    def __enter__(self):
        temp = NamedTemporaryFile(
            "w",
            delete=False,
            dir=self.target.parent,
            prefix=f".{self.target.name}.",
            suffix=".tmp",
            encoding=self.encoding,
            newline=self.newline,
        )
        self.handle = temp
        self.path = Path(temp.name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.handle is not None
        assert self.path is not None
        self.handle.close()
        if exc_type is not None:
            self.path.unlink(missing_ok=True)
            return
        self.path.replace(self.target)
