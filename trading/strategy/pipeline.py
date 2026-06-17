from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from trading.strategy.candidates import candidate_is_discovery_only
from trading.strategy.candles import CandleBuilder
from trading.strategy.gates import (
    ACTIVE_STATES,
    ENTRY_RISK_BLOCK_SUB_STATUSES,
    ENTRY_RISK_FINAL_BLOCK,
    ENTRY_RISK_TEMP_WAIT,
    MarketIndexGate,
    StockLeadershipGate,
    StockPullbackEntryGate,
    ThemePullbackGate,
    ThemeStrengthGate,
)
from trading.strategy.hybrid_gate import HybridDynamicThemeGate, HybridGateConfig, HybridGateStatus, hybrid_decision_flat_fields
from trading.strategy.hybrid_validation import (
    HybridValidationConfig,
    attach_validation_event_details,
    build_validation_event,
    should_save_validation_event,
)
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import BlockType, Candidate, GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.reason_codes import normalize_reason_codes, standardize_details
from trading.strategy.runtime_settings import (
    StrategyRuntimeSettings,
    attach_settings_details,
    legacy_strategy_runtime_settings,
)
from trading.strategy.trade_setup_classifier import attach_trade_setup_details
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.models import StockLeadershipResult, ThemeContext, ThemeStrengthResult


@dataclass
class GatePipelineResult:
    candidate_id: Optional[int]
    code: str
    theme_id: str
    final_grade: str = "C"
    final_score: float = 0.0
    strategy_eligible: bool = False
    block_type: BlockType = BlockType.NONE
    can_recover: bool = False
    recheck_after_sec: int = 0
    decisions: list[GateDecision] = field(default_factory=list)
    snapshot: Optional[IndicatorSnapshot] = None
    details: dict = field(default_factory=dict)


class GatePipeline:
    def __init__(
        self,
        theme_context_provider: DynamicThemeContextProvider,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        indicator_calculator: IndicatorCalculator,
        intraday_tracker: IntradayStateTracker,
        market_index_store: MarketIndexStore,
        settings: Optional[StrategyRuntimeSettings] = None,
        hybrid_validation_repository=None,
    ) -> None:
        self.theme_context_provider = theme_context_provider
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.indicator_calculator = indicator_calculator
        self.intraday_tracker = intraday_tracker
        self.market_index_store = market_index_store
        self.settings = settings or legacy_strategy_runtime_settings()
        self.hybrid_gate = HybridDynamicThemeGate(HybridGateConfig.from_settings(self.settings))
        self.hybrid_validation_config = HybridValidationConfig.from_settings(self.settings)
        self.hybrid_validation_repository = hybrid_validation_repository

    def evaluate(
        self,
        candidates: list[Candidate],
        *,
        entry_candidates: Optional[list[Candidate]] = None,
    ) -> list[GatePipelineResult]:
        refresh_context = getattr(self.theme_context_provider, "refresh_cache", None)
        if callable(refresh_context):
            refresh_context()
        active_candidates = [candidate for candidate in candidates if candidate.state in ACTIVE_STATES]
        enriched_candidates = [self.theme_context_provider.enrich_candidate(candidate) for candidate in active_candidates]
        entry_source = entry_candidates if entry_candidates is not None else active_candidates
        active_entry_candidates = [
            candidate
            for candidate in entry_source
            if candidate.state in ACTIVE_STATES and not candidate_is_discovery_only(candidate)
        ]
        enriched_entry_candidates = [
            self.theme_context_provider.enrich_candidate(candidate) for candidate in active_entry_candidates
        ]
        theme_results = {
            result.theme_id: result
            for result in ThemeStrengthGate(
                self.theme_context_provider,
                self.market_data,
                self.candle_builder,
                self.settings,
            ).evaluate(enriched_candidates)
        }
        leadership_results = {
            (result.code, result.theme_id): result
            for result in StockLeadershipGate(
                self.theme_context_provider,
                self.market_data,
                self.candle_builder,
                self.market_index_store,
                self.settings,
            ).evaluate_all(enriched_candidates)
        }
        market_gate = MarketIndexGate(self.market_index_store, self.settings)
        theme_pullback_gate = ThemePullbackGate(
            self.indicator_calculator,
            self.intraday_tracker,
            self.candle_builder,
            self.market_data,
            self.settings,
        )
        stock_pullback_gate = StockPullbackEntryGate(
            self.indicator_calculator,
            self.intraday_tracker,
            self.candle_builder,
            self.market_data,
            self.settings,
        )

        results: list[GatePipelineResult] = []
        for candidate in enriched_entry_candidates:
            for mapping in self.theme_context_provider.themes_for_code(candidate.code):
                theme_result = theme_results.get(mapping.theme_id)
                if theme_result is None:
                    continue
                leadership_result = leadership_results.get((candidate.code, mapping.theme_id))
                if leadership_result is None:
                    continue
                results.append(
                    self._evaluate_candidate_theme(
                        candidate,
                        mapping,
                        theme_result,
                        leadership_result,
                        market_gate,
                        theme_pullback_gate,
                        stock_pullback_gate,
                    )
                )
        return results

    def _evaluate_candidate_theme(
        self,
        candidate: Candidate,
        mapping: ThemeContext,
        theme_result: ThemeStrengthResult,
        leadership_result: StockLeadershipResult,
        market_gate: MarketIndexGate,
        theme_pullback_gate: ThemePullbackGate,
        stock_pullback_gate: StockPullbackEntryGate,
    ) -> GatePipelineResult:
        market_decision = market_gate.evaluate(candidate, mapping)
        theme_strength_decision = _theme_strength_decision(theme_result)
        theme_pullback_decision = theme_pullback_gate.evaluate(theme_result)
        leadership_decision = _leadership_decision(leadership_result)
        stock_pullback_decision, snapshot = stock_pullback_gate.evaluate(
            candidate,
            theme_result,
            leadership_result,
            market_decision,
        )
        decisions = [
            market_decision,
            theme_strength_decision,
            theme_pullback_decision,
            leadership_decision,
            stock_pullback_decision,
        ]
        hybrid_decision = self.hybrid_gate.evaluate(
            candidate=candidate,
            theme_context=mapping,
            theme_result=theme_result,
            leadership_result=leadership_result,
            market_decision=market_decision,
            theme_strength_decision=theme_strength_decision,
            theme_pullback_decision=theme_pullback_decision,
            leadership_decision=leadership_decision,
            stock_pullback_decision=stock_pullback_decision,
        )
        comparison_reason_codes = _comparison_reason_codes(decisions)
        legacy_final_score = _final_score(decisions, self.settings)
        legacy_grade, legacy_strategy_eligible, legacy_block_type, legacy_can_recover, legacy_recheck_after_sec, legacy_cap_rules, legacy_sub_status = _final_grade(
            candidate,
            mapping,
            theme_result,
            leadership_result,
            decisions,
            legacy_final_score,
            self.settings,
        )
        grade = legacy_grade
        strategy_eligible = legacy_strategy_eligible
        block_type = legacy_block_type
        can_recover = legacy_can_recover
        recheck_after_sec = legacy_recheck_after_sec
        cap_rules = list(legacy_cap_rules)
        sub_status = legacy_sub_status
        final_score = legacy_final_score
        hybrid_live_applied = False
        if self.hybrid_gate.config.hybrid_gate_enabled and not self.hybrid_gate.config.hybrid_gate_observe_only:
            grade, strategy_eligible, block_type, can_recover, recheck_after_sec, cap_rules, sub_status = _hybrid_live_result(
                hybrid_decision,
                fallback_grade=legacy_grade,
            )
            final_score = hybrid_decision.score
            hybrid_live_applied = True
        weights = _gate_weights(self.settings)
        hybrid_payload = hybrid_decision.to_dict()
        hybrid_flat = hybrid_decision_flat_fields(hybrid_decision)
        details = attach_settings_details({
            "theme_id": mapping.theme_id,
            "theme_name": mapping.theme_name,
            "theme_score": hybrid_flat.get("dynamic_theme_score", 0.0),
            "score_components": {
                "MarketIndexGate": market_decision.score * weights["MarketIndexGate"],
                "ThemeStrengthGate": theme_strength_decision.score * weights["ThemeStrengthGate"],
                "ThemePullbackGate": theme_pullback_decision.score * weights["ThemePullbackGate"],
                "StockLeadershipGate": leadership_decision.score * weights["StockLeadershipGate"],
                "StockPullbackEntryGate": stock_pullback_decision.score * weights["StockPullbackEntryGate"],
            },
            "cap_rules_applied": cap_rules,
            "sub_status": sub_status,
            "actual_order_allowed": False,
            "entry_plan_created": False,
            "theme_diagnostics_v2": theme_strength_decision.details.get("theme_diagnostics_v2", {}),
            "leadership_diagnostics_v2": leadership_decision.details.get("leadership_diagnostics_v2", {}),
            "late_chase_diagnostics": stock_pullback_decision.details.get("late_chase_diagnostics", {}),
            "late_chase_level": stock_pullback_decision.details.get("late_chase_level", ""),
            "late_chase_score": stock_pullback_decision.details.get("late_chase_score"),
            "late_chase_block_type": stock_pullback_decision.details.get("late_chase_block_type", ""),
            "late_chase_recoverable": bool(stock_pullback_decision.details.get("late_chase_recoverable")),
            "late_chase_recheck_after_sec": stock_pullback_decision.details.get("late_chase_recheck_after_sec", 0),
            "late_chase_recovery_conditions": list(stock_pullback_decision.details.get("late_chase_recovery_conditions") or []),
            "entry_risk_diagnostics": dict(stock_pullback_decision.details.get("entry_risk_diagnostics") or {}),
            "entry_risk_feature_version": stock_pullback_decision.details.get("entry_risk_feature_version", ""),
            "entry_risk_level": stock_pullback_decision.details.get("entry_risk_level", ""),
            "entry_risk_score": stock_pullback_decision.details.get("entry_risk_score"),
            "entry_risk_action": stock_pullback_decision.details.get("entry_risk_action", ""),
            "entry_risk_reason_codes": list(stock_pullback_decision.details.get("entry_risk_reason_codes") or []),
            "entry_risk_recovery_checks": dict(stock_pullback_decision.details.get("entry_risk_recovery_checks") or {}),
            "vi_status": stock_pullback_decision.details.get("vi_status", "UNKNOWN"),
            "vi_signal_source": stock_pullback_decision.details.get("vi_signal_source", "unknown"),
            "seconds_since_vi_release": stock_pullback_decision.details.get("seconds_since_vi_release"),
            "upper_limit_price": stock_pullback_decision.details.get("upper_limit_price"),
            "upper_limit_gap_pct": stock_pullback_decision.details.get("upper_limit_gap_pct"),
            "change_rate": stock_pullback_decision.details.get("change_rate"),
            "pullback_from_high_pct": stock_pullback_decision.details.get("pullback_from_high_pct"),
            "leadership_role": stock_pullback_decision.details.get("leadership_role", leadership_result.leadership_role),
            "stock_role": stock_pullback_decision.details.get("stock_role", ""),
            "position_size_multiplier": stock_pullback_decision.details.get("position_size_multiplier", 1.0),
            "comparison_reason_codes": comparison_reason_codes,
            "secondary_reason_codes": comparison_reason_codes,
            "hybrid_result": hybrid_payload,
            "hybrid_live_applied": hybrid_live_applied,
            "legacy_final_grade": legacy_grade,
            "legacy_final_score": legacy_final_score,
            "legacy_strategy_eligible": legacy_strategy_eligible,
            "legacy_block_type": legacy_block_type.value,
            "legacy_sub_status": legacy_sub_status,
            "base_price": snapshot.price if snapshot else 0,
            **hybrid_flat,
        }, self.settings)
        details = standardize_details(
            details,
            cap_rules,
            passed=strategy_eligible,
            score=final_score,
            created_at=snapshot.created_at if snapshot else "",
            legacy_result=strategy_eligible,
            new_result=strategy_eligible,
            legacy_score=legacy_final_score,
            new_score=final_score,
        )
        details = attach_trade_setup_details(details)
        if self.hybrid_validation_repository is not None and self.hybrid_validation_config.enabled:
            try:
                event_ts = snapshot.created_at if snapshot else ""
                if should_save_validation_event(
                    candidate,
                    hybrid_decision,
                    config=self.hybrid_validation_config,
                    ts=event_ts,
                ):
                    event = build_validation_event(candidate=candidate, decision=hybrid_decision, ts=event_ts)
                    attach_validation_event_details(
                        event,
                        base_price=snapshot.price if snapshot else 0,
                        pipeline_details=details,
                        config=self.hybrid_validation_config,
                    )
                    self.hybrid_validation_repository.save_event(event)
                    details["hybrid_validation_event_saved"] = True
                else:
                    details["hybrid_validation_event_saved"] = False
                    details["hybrid_validation_event_skip_reason"] = "sampled_out"
            except Exception as exc:
                details["hybrid_validation_event_saved"] = False
                details["hybrid_validation_event_error"] = str(exc)
        final_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="FinalGrade",
            passed=strategy_eligible,
            score=final_score,
            grade=grade,
            block_type=block_type,
            can_recover=can_recover,
            recheck_after_sec=recheck_after_sec,
            reason_codes=cap_rules,
            details=details,
            created_at=snapshot.created_at if snapshot else "",
        )
        all_decisions = decisions + [final_decision]
        return GatePipelineResult(
            candidate_id=candidate.id,
            code=candidate.code,
            theme_id=mapping.theme_id,
            final_grade=grade,
            final_score=final_score,
            strategy_eligible=strategy_eligible,
            block_type=block_type,
            can_recover=can_recover,
            recheck_after_sec=recheck_after_sec,
            decisions=all_decisions,
            snapshot=snapshot,
            details=details,
        )


def _theme_strength_decision(theme_result: ThemeStrengthResult) -> GateDecision:
    passed = theme_result.grade != "C"
    block_type = BlockType.NONE if passed else BlockType.FINAL
    details = dict(theme_result.details)
    details["theme_grade"] = theme_result.grade
    insufficient = bool(details.get("insufficient_reason"))
    reason_codes = []
    if not passed:
        reason_codes.append("THEME_STRENGTH_C")
    if insufficient:
        reason_codes.append("DATA_INSUFFICIENT")
    return GateDecision(
        gate_name="ThemeStrengthGate",
        passed=passed,
        score=theme_result.score,
        grade=theme_result.grade,
        block_type=BlockType.TEMPORARY if insufficient and not passed else block_type,
        can_recover=insufficient and not passed,
        recheck_after_sec=60 if insufficient and not passed else 0,
        reason_codes=reason_codes,
        details=details,
    )


def _leadership_decision(result: StockLeadershipResult) -> GateDecision:
    passed = result.leadership_role in {
        "leader",
        "co_leader",
    }
    return GateDecision(
        candidate_id=result.candidate_id,
        gate_name="StockLeadershipGate",
        passed=passed,
        score=result.score,
        grade=result.leadership_role,
        block_type=BlockType.NONE if passed else BlockType.TEMPORARY,
        can_recover=not passed,
        recheck_after_sec=60 if not passed else 0,
        reason_codes=[] if passed else ["LEADERSHIP_WEAK"],
        details=dict(result.details),
    )


def _comparison_reason_codes(decisions: list[GateDecision]) -> list[str]:
    values: list[str] = []
    for decision in decisions:
        values.extend(str(code) for code in decision.details.get("comparison_reason_codes", []))
    return normalize_reason_codes(values)


def _gate_weights(settings: Optional[StrategyRuntimeSettings] = None) -> dict[str, float]:
    active_settings = settings or legacy_strategy_runtime_settings()
    return {
        "MarketIndexGate": active_settings.number("gate_weights.market", 0.15),
        "ThemeStrengthGate": active_settings.number("gate_weights.theme_strength", 0.30),
        "ThemePullbackGate": active_settings.number("gate_weights.theme_pullback", 0.15),
        "StockLeadershipGate": active_settings.number("gate_weights.stock_leadership", 0.20),
        "StockPullbackEntryGate": active_settings.number("gate_weights.stock_pullback", 0.20),
    }


def _final_score(decisions: list[GateDecision], settings: Optional[StrategyRuntimeSettings] = None) -> float:
    weights = _gate_weights(settings)
    score = sum(decision.score * weights.get(decision.gate_name, 0.0) for decision in decisions)
    return round(score, 4)


def _hybrid_live_result(
    hybrid_decision,
    *,
    fallback_grade: str,
) -> tuple[str, bool, BlockType, bool, int, list[str], str]:
    status = hybrid_decision.status.value if hasattr(hybrid_decision.status, "value") else str(hybrid_decision.status)
    position_tier = (
        hybrid_decision.position_tier.value
        if hasattr(hybrid_decision.position_tier, "value")
        else str(hybrid_decision.position_tier)
    )
    reason_codes = list(hybrid_decision.reason_codes)
    if status == HybridGateStatus.READY.value:
        grade = "A" if position_tier == "normal_first_entry" else "B+"
        return grade, True, BlockType.NONE, False, 0, reason_codes, "HYBRID_READY"
    if status == HybridGateStatus.WAIT.value:
        return fallback_grade if fallback_grade != "C" else "B", False, BlockType.TEMPORARY, True, 60, reason_codes, "HYBRID_WAIT"
    if status == HybridGateStatus.BLOCKED.value:
        return "C", False, BlockType.FINAL, False, 0, reason_codes, "HYBRID_BLOCKED"
    return fallback_grade if fallback_grade != "A" else "B", False, BlockType.NONE, False, 0, reason_codes, "HYBRID_OBSERVE"


def _final_grade(
    candidate: Candidate,
    mapping: ThemeContext,
    theme_result: ThemeStrengthResult,
    leadership_result: StockLeadershipResult,
    decisions: list[GateDecision],
    final_score: float,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> tuple[str, bool, BlockType, bool, int, list[str], str]:
    active_settings = settings or legacy_strategy_runtime_settings()
    decision_by_name = {decision.gate_name: decision for decision in decisions}
    cap_rules: list[str] = []
    temporary = any(decision.block_type == BlockType.TEMPORARY for decision in decisions)
    final_block = any(decision.block_type == BlockType.FINAL for decision in decisions)
    stock_pullback = decision_by_name["StockPullbackEntryGate"]
    market = decision_by_name["MarketIndexGate"]
    theme_pullback = decision_by_name["ThemePullbackGate"]

    if stock_pullback.details.get("sub_status") in ENTRY_RISK_BLOCK_SUB_STATUSES:
        entry_codes = normalize_reason_codes(
            list(stock_pullback.reason_codes)
            + list(stock_pullback.details.get("entry_risk_reason_codes") or [])
        )
        cap_rules.extend(entry_codes)
        if stock_pullback.details.get("sub_status") == ENTRY_RISK_FINAL_BLOCK:
            if ENTRY_RISK_FINAL_BLOCK not in cap_rules:
                cap_rules.append(ENTRY_RISK_FINAL_BLOCK)
            return "C", False, BlockType.FINAL, False, 0, cap_rules, ENTRY_RISK_FINAL_BLOCK
        if ENTRY_RISK_TEMP_WAIT not in cap_rules:
            cap_rules.append(ENTRY_RISK_TEMP_WAIT)
        recheck_after_sec = int(
            stock_pullback.recheck_after_sec
            or stock_pullback.details.get("entry_risk_diagnostics", {}).get("recheck_after_sec")
            or active_settings.integer("entry_risk_gate.risk_recheck_after_sec", 30)
        )
        return "B", False, BlockType.TEMPORARY, bool(stock_pullback.can_recover), recheck_after_sec, cap_rules, ENTRY_RISK_TEMP_WAIT
    if stock_pullback.details.get("chase_risk"):
        cap_rules.append("CHASE_RISK_CAP")
        return "C", False, BlockType.FINAL, False, 0, cap_rules, "CHASE_RISK"
    if market.block_type == BlockType.TEMPORARY:
        cap_rules.append("MARKET_INDEX_TEMPORARY_CAP")
        return "C", False, BlockType.TEMPORARY, True, 60, cap_rules, market.details.get("sub_status", "MARKET_WAIT")
    if _has_data_insufficient(decisions):
        cap_rules.append("DATA_INSUFFICIENT_CAP")
        return "C", False, BlockType.TEMPORARY, True, 60, cap_rules, "DATA_INSUFFICIENT"
    if theme_result.grade == "C":
        cap_rules.append("THEME_STRENGTH_C_CAP")
        return "C", False, BlockType.FINAL, False, 0, cap_rules, "THEME_WEAK"
    if theme_pullback.block_type == BlockType.FINAL:
        cap_rules.append("THEME_PULLBACK_FINAL_CAP")
        return "C", False, BlockType.FINAL, False, 0, cap_rules, theme_pullback.details.get("sub_status", "THEME_PULLBACK_FAIL")
    if final_block:
        cap_rules.append("FINAL_BLOCK_CAP")
        return "C", False, BlockType.FINAL, False, 0, cap_rules, "FINAL_BLOCK"
    if stock_pullback.block_type == BlockType.TEMPORARY:
        if stock_pullback.details.get("sub_status") == "LATE_CHASE_TEMP_WAIT":
            cap_rules.extend(
                normalize_reason_codes(
                    list(stock_pullback.reason_codes)
                    + ["LATE_CHASE", "SOFT_BLOCK_ONLY", "LATE_CHASE_TEMP_WAIT"]
                )
            )
            recheck_after_sec = int(stock_pullback.recheck_after_sec or stock_pullback.details.get("late_chase_recheck_after_sec") or 60)
            return "B", False, BlockType.TEMPORARY, bool(stock_pullback.can_recover), recheck_after_sec, cap_rules, "LATE_CHASE_TEMP_WAIT"
        cap_rules.append("STOCK_PULLBACK_WAIT_CAP")
        return "B", False, BlockType.TEMPORARY, True, 60, cap_rules, "WAIT_PULLBACK_CONFIRMATION"

    kosdaq_signal_wait = (
        theme_result.grade == "A_SIGNAL"
        and mapping.strategy_profile == StrategyProfile.KOSDAQ_THEME_PROFILE
        and not _kosdaq_signal_can_promote(theme_result, leadership_result, stock_pullback)
    )
    if kosdaq_signal_wait:
        cap_rules.append("A_SIGNAL_KOSDAQ_WAIT_CAP")
        return "B+", False, BlockType.TEMPORARY, True, 60, cap_rules, "A_SIGNAL_WAIT"

    a_allowed = (
        final_score >= active_settings.number("theme_thresholds.grade_a_score", 75.0)
        and all(decision.passed for decision in decisions)
        and _theme_allows_a(candidate, mapping, theme_result, leadership_result, stock_pullback)
    )
    if a_allowed:
        return "A", True, BlockType.NONE, False, 0, cap_rules, "PASS"
    if final_score >= active_settings.number("theme_thresholds.grade_b_plus_score", 70.0) and not temporary:
        return "B+", True, BlockType.NONE, False, 0, cap_rules, "PASS"
    if final_score >= active_settings.number("theme_thresholds.grade_b_score", 55.0) and not temporary:
        return "B", True, BlockType.NONE, False, 0, cap_rules, "PASS"
    cap_rules.append("LOW_SCORE_CAP")
    return "C", False, BlockType.NONE, False, 0, cap_rules, "LOW_SCORE"


def _theme_allows_a(
    candidate: Candidate,
    mapping: ThemeContext,
    theme_result: ThemeStrengthResult,
    leadership_result: StockLeadershipResult,
    stock_pullback: GateDecision,
) -> bool:
    if theme_result.grade == "A":
        return leadership_result.leadership_role in {"leader", "co_leader"}
    if theme_result.grade == "A_SIGNAL" and mapping.strategy_profile in {
        StrategyProfile.KOSPI_LEADER_PROFILE,
        StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE,
    }:
        return leadership_result.leadership_role in {"leader", "co_leader"}
    if theme_result.grade == "A_SIGNAL" and mapping.strategy_profile == StrategyProfile.KOSDAQ_THEME_PROFILE:
        return _kosdaq_signal_can_promote(theme_result, leadership_result, stock_pullback)
    return False


def _kosdaq_signal_can_promote(
    theme_result: ThemeStrengthResult,
    leadership_result: StockLeadershipResult,
    stock_pullback: GateDecision,
) -> bool:
    non_signal_scope_count = len(leadership_result.details.get("scope_candidate_codes", []))
    return (
        non_signal_scope_count >= 3
        and leadership_result.leadership_role in {"leader", "co_leader"}
        and stock_pullback.passed
    )


def _has_data_insufficient(decisions: list[GateDecision]) -> bool:
    for decision in decisions:
        if "DATA_INSUFFICIENT" in decision.reason_codes:
            return True
        if decision.details.get("sub_status") == "DATA_INSUFFICIENT":
            return True
    return False
