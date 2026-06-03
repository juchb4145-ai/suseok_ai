from __future__ import annotations

from copy import deepcopy

import trading.strategy.gates as gates
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.models import BlockType, Candidate, GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.pipeline import GatePipelineResult, _final_grade
from trading.strategy.runtime_settings import LEGACY_DEFAULT_SETTINGS, StrategyRuntimeSettings, legacy_strategy_runtime_settings
from trading.theme_engine.models import RelationType, StockLeadershipResult, ThemeActivitySnapshot, ThemeContext, ThemeStatus, ThemeStrengthResult


def test_stock_pullback_soft_block_becomes_recoverable_temp_wait(monkeypatch):
    decision, _snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block")

    assert decision.passed is False
    assert decision.block_type == BlockType.TEMPORARY
    assert decision.can_recover is True
    assert decision.recheck_after_sec == 60
    assert decision.details["sub_status"] == "LATE_CHASE_TEMP_WAIT"
    assert decision.details["late_chase_recoverable"] is True
    assert decision.details["late_chase_recheck_after_sec"] == 60
    assert "selected_support_ready" in decision.details["late_chase_recovery_conditions"]
    assert "LATE_CHASE" in decision.reason_codes
    assert "SOFT_BLOCK_ONLY" in decision.reason_codes
    assert "LATE_CHASE_TEMP_WAIT" in decision.reason_codes


def test_stock_pullback_without_soft_block_passes_existing_flow(monkeypatch):
    decision, _snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="none")

    assert decision.passed is True
    assert decision.block_type == BlockType.NONE
    assert decision.details["sub_status"] == "PASS"


def test_stock_pullback_warning_is_tag_only_and_does_not_block(monkeypatch):
    decision, _snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="warning")

    assert decision.passed is True
    assert decision.block_type == BlockType.NONE
    assert decision.details["sub_status"] == "PASS"
    assert decision.details["late_chase_block_type"] == "tag_only"
    assert "LATE_CHASE_WARNING" in decision.details["comparison_reason_codes"]


def test_snapshot_chase_risk_remains_final_chase_block(monkeypatch):
    decision, _snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block", chase_risk=True)

    assert decision.passed is False
    assert decision.block_type == BlockType.FINAL
    assert decision.can_recover is False
    assert decision.details["sub_status"] == "CHASE_RISK"
    assert decision.reason_codes == ["CHASE_RISK"]


def test_late_chase_temp_wait_blocks_entry_plan_creation(monkeypatch):
    stock_decision, snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block")
    result = _pipeline_result(stock_decision, snapshot)

    assert result.strategy_eligible is False
    assert result.block_type == BlockType.TEMPORARY
    assert result.details["sub_status"] == "LATE_CHASE_TEMP_WAIT"
    assert EntryPlanBuilder().build(result) is None


def test_ready_early_small_soft_block_does_not_create_first_leg(monkeypatch):
    stock_decision, snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block")
    result = _pipeline_result(stock_decision, snapshot)
    result.details["ready_type"] = "READY_EARLY_SMALL"

    assert result.strategy_eligible is False
    assert EntryPlanBuilder().build(result) is None


def test_late_chase_recovery_reevaluation_waits_until_recovery_conditions_clear(monkeypatch):
    first_decision, _first_snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block")
    second_decision, _second_snapshot = _evaluate_stock_gate(
        monkeypatch,
        late_chase_level="warning",
        previous_late_chase=True,
    )

    assert first_decision.details["sub_status"] == "LATE_CHASE_TEMP_WAIT"
    assert second_decision.passed is False
    assert second_decision.details["sub_status"] == "LATE_CHASE_TEMP_WAIT"
    assert second_decision.details["late_chase_recovery_status"]["pending"] is True
    assert "late_chase_score_below_warning" in second_decision.details["late_chase_recovery_status"]["missing_conditions"]
    assert "LATE_CHASE_RECOVERY_PENDING" in second_decision.reason_codes


def test_late_chase_recovery_reevaluation_can_pass_when_all_conditions_clear(monkeypatch):
    decision, _snapshot = _evaluate_stock_gate(
        monkeypatch,
        late_chase_level="none",
        previous_late_chase=True,
    )

    assert decision.passed is True
    assert decision.details["sub_status"] == "PASS"
    assert decision.details["late_chase_recovery_status"]["recovered"] is True


def test_late_chase_recheck_setting_is_configurable(monkeypatch):
    settings_json = deepcopy(LEGACY_DEFAULT_SETTINGS)
    settings_json["late_chase_policy"]["recheck_after_sec"] = 45
    settings = StrategyRuntimeSettings.from_settings_json(settings_json)

    decision, _snapshot = _evaluate_stock_gate(monkeypatch, late_chase_level="soft_block", settings=settings)

    assert decision.recheck_after_sec == 45
    assert decision.details["late_chase_recheck_after_sec"] == 45


def _evaluate_stock_gate(
    monkeypatch,
    *,
    late_chase_level: str,
    chase_risk: bool = False,
    settings: StrategyRuntimeSettings | None = None,
    previous_late_chase: bool = False,
):
    active_settings = settings or legacy_strategy_runtime_settings()
    snapshot = IndicatorSnapshot(
        candidate_id=1,
        code="000001",
        created_at="2026-06-01T09:10:00",
        price=9800,
        day_high=10000,
        day_low=9500,
        pullback_pct=-2.0,
        volume_reaccel=True,
        failed_low_break_rebound=False,
        chase_risk=chase_risk,
        metadata={"latest_tick_ready": True},
    )
    support_status = {
        "nearest_support": "vwap",
        "nearest_support_price": 9780,
        "support_distance_pct": 0.204,
        "support_touched": True,
        "support_reclaimed": True,
        "support_candidates": {"vwap": 9780},
        "selected_support_source": "vwap",
        "selected_support_price": 9780,
        "selected_support_ready": True,
        "selected_support_ready_reason": "",
        "support_ready": True,
        "support_ready_reason": "",
        "support_readiness_reason_codes": [],
    }

    monkeypatch.setattr(gates, "_snapshot_for", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(gates, "support_status_for_snapshot", lambda *args, **kwargs: dict(support_status))
    diagnostics = {
        "feature_version": "late_chase_diagnostics_v1",
        "late_chase_level": late_chase_level,
        "late_chase_score": 100.0 if late_chase_level == "soft_block" else (30.0 if late_chase_level == "warning" else 0.0),
        "reason_codes": ["LATE_CHASE", "SOFT_BLOCK_ONLY"] if late_chase_level == "soft_block" else [],
        "input_missing_fields": [],
        "support_distance_excessive": late_chase_level == "soft_block",
        "volume_reacceleration_confirmed": True,
        "after_large_3m_candle": late_chase_level == "soft_block",
        "after_large_5m_candle": False,
    }
    monkeypatch.setattr(
        gates,
        "_late_chase_diagnostics",
        lambda *args, **kwargs: dict(diagnostics),
    )
    gate = gates.StockPullbackEntryGate(
        indicator_calculator=object(),
        intraday_tracker=object(),
        candle_builder=object(),
        market_data=object(),
        settings=active_settings,
    )
    metadata = {}
    if previous_late_chase:
        metadata = {
            "sub_status": "LATE_CHASE_TEMP_WAIT",
            "last_block_result": {
                "sub_status": "LATE_CHASE_TEMP_WAIT",
                "reason_codes": ["LATE_CHASE", "SOFT_BLOCK_ONLY", "LATE_CHASE_TEMP_WAIT"],
            },
        }
    return gate.evaluate(
        Candidate(id=1, code="000001", strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE, metadata=metadata),
        _theme(),
        _leadership(),
    )


def _pipeline_result(stock_decision: GateDecision, snapshot: IndicatorSnapshot) -> GatePipelineResult:
    candidate = Candidate(id=1, code="000001", strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE)
    mapping = _context()
    theme = _theme()
    leadership = _leadership()
    market = GateDecision(gate_name="MarketIndexGate", passed=True, score=100, block_type=BlockType.NONE, details={"position_vs_mid": "ABOVE_MID"})
    theme_pullback = GateDecision(gate_name="ThemePullbackGate", passed=True, score=100, block_type=BlockType.NONE, details={"sub_status": "PASS"})
    decisions = [market, GateDecision(gate_name="ThemeStrengthGate", passed=True, score=90, grade="A", block_type=BlockType.NONE), theme_pullback, GateDecision(gate_name="StockLeadershipGate", passed=True, score=95, grade="leader", block_type=BlockType.NONE), stock_decision]
    final_grade, eligible, block_type, can_recover, recheck_after_sec, cap_rules, sub_status = _final_grade(
        candidate,
        mapping,
        theme,
        leadership,
        decisions,
        90.0,
    )
    details = {
        "theme_name": "AI",
        "sub_status": sub_status,
        "cap_rules_applied": cap_rules,
        "late_chase_level": stock_decision.details.get("late_chase_level", ""),
        "late_chase_block_type": stock_decision.details.get("late_chase_block_type", ""),
    }
    return GatePipelineResult(
        candidate_id=1,
        code="000001",
        theme_id="ai",
        final_grade=final_grade,
        final_score=90.0,
        strategy_eligible=eligible,
        block_type=block_type,
        can_recover=can_recover,
        recheck_after_sec=recheck_after_sec,
        decisions=decisions + [GateDecision(gate_name="FinalGrade", passed=eligible, score=90.0, grade=final_grade, block_type=block_type, can_recover=can_recover, recheck_after_sec=recheck_after_sec, reason_codes=cap_rules, details=details)],
        snapshot=snapshot,
        details=details,
    )


def _theme() -> ThemeStrengthResult:
    return ThemeStrengthResult(theme_id="ai", theme_name="AI", score=90, grade="A", details={})


def _leadership() -> StockLeadershipResult:
    return StockLeadershipResult(candidate_id=1, code="000001", theme_id="ai", theme_name="AI", score=95, leadership_rank=1, leadership_role="leader", details={})


def _context() -> ThemeContext:
    return ThemeContext(
        theme_id="ai",
        theme_name="AI",
        status=ThemeStatus.ACTIVE,
        activity=ThemeActivitySnapshot(theme_id="ai", theme_name="AI", theme_score=90, status=ThemeStatus.ACTIVE, trade_eligible=True),
        membership_score=1.0,
        relation_type=RelationType.INVESTOR,
        trade_eligible=True,
        rank=1,
        rank_in_theme=1,
    )
