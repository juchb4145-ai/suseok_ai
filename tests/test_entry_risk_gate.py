from __future__ import annotations

import trading.strategy.gates as gates
from trading.strategy.models import BlockType, Candidate, IndicatorSnapshot, StrategyProfile
from trading.theme_engine.models import StockLeadershipResult, ThemeStrengthResult


def test_vi_active_is_final_block_before_support_checks(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        metadata={"vi_status": "ACTIVE", "vi_signal_source": "payload", "change_rate": 2.0},
        support_ready=False,
    )

    assert decision.passed is False
    assert decision.block_type == BlockType.FINAL
    assert decision.can_recover is False
    assert decision.details["sub_status"] == "ENTRY_RISK_FINAL_BLOCK"
    assert "VI_ACTIVE" in decision.reason_codes
    assert "ENTRY_RISK_FINAL_BLOCK" in decision.reason_codes


def test_vi_cooldown_is_recoverable_temp_wait(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        metadata={"vi_status": "COOLDOWN", "vi_signal_source": "inferred", "seconds_since_vi_release": 90, "change_rate": 2.0},
    )

    assert decision.block_type == BlockType.TEMPORARY
    assert decision.can_recover is True
    assert decision.recheck_after_sec == 30
    assert decision.details["sub_status"] == "ENTRY_RISK_TEMP_WAIT"
    assert "VI_COOLDOWN" in decision.reason_codes


def test_upper_limit_hard_near_is_final_block(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 0.8, "change_rate": 6.0},
    )

    assert decision.block_type == BlockType.FINAL
    assert "UPPER_LIMIT_HARD_NEAR" in decision.reason_codes
    assert "ENTRY_RISK_FINAL_BLOCK" in decision.reason_codes


def test_upper_limit_near_leader_is_still_temporary_wait(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        role="leader",
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 2.5, "change_rate": 10.0, "turnover_strength": 1.2},
    )

    assert decision.block_type == BlockType.TEMPORARY
    assert decision.details["sub_status"] == "ENTRY_RISK_TEMP_WAIT"
    assert "UPPER_LIMIT_NEAR" in decision.reason_codes
    assert "ENTRY_RISK_TEMP_WAIT" in decision.reason_codes


def test_high_return_thresholds_are_role_sensitive(monkeypatch):
    leader_decision, _snapshot = _evaluate(
        monkeypatch,
        role="leader",
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 10.0, "change_rate": 15.0, "pullback_from_high_pct": 2.0},
    )
    follower_decision, _snapshot = _evaluate(
        monkeypatch,
        role="follower",
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 10.0, "change_rate": 8.0, "pullback_from_high_pct": 2.0},
    )

    assert leader_decision.block_type == BlockType.TEMPORARY
    assert "HIGH_RETURN_LEADER" in leader_decision.reason_codes
    assert follower_decision.block_type == BlockType.FINAL
    assert "HIGH_RETURN_FOLLOWER" in follower_decision.reason_codes


def test_late_laggard_high_return_is_final_block(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        role="late_laggard",
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 10.0, "change_rate": 5.0, "pullback_from_high_pct": 2.0},
    )

    assert decision.block_type == BlockType.FINAL
    assert "HIGH_RETURN_LATE_LAGGARD" in decision.reason_codes


def test_vi_unknown_limit_risk_uses_upper_limit_fallback(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        role="leader",
        metadata={"vi_status": "UNKNOWN", "upper_limit_gap_pct": 2.5, "change_rate": 4.0, "pullback_from_high_pct": 2.0},
    )

    assert decision.block_type == BlockType.TEMPORARY
    assert "VI_UNKNOWN_LIMIT_RISK" in decision.reason_codes
    assert "UPPER_LIMIT_NEAR" in decision.reason_codes


def test_entry_risk_recovered_allows_existing_flow_with_size_multiplier(monkeypatch):
    decision, _snapshot = _evaluate(
        monkeypatch,
        metadata={"vi_status": "INACTIVE", "upper_limit_gap_pct": 6.0, "change_rate": 2.0, "pullback_from_high_pct": 2.0},
        previous_entry_risk=True,
    )

    assert decision.passed is True
    assert decision.block_type == BlockType.NONE
    assert decision.details["sub_status"] == "PASS"
    assert decision.details["entry_risk_action"] == "recovered"
    assert decision.details["position_size_multiplier"] == 0.25
    assert "ENTRY_RISK_RECOVERED" in decision.details["entry_risk_reason_codes"]


def _evaluate(
    monkeypatch,
    *,
    metadata: dict,
    role: str = "leader",
    support_ready: bool = True,
    previous_entry_risk: bool = False,
):
    base_metadata = {
        "upper_limit_gap_pct": 10.0,
        "upper_limit_price": 13000,
        "pullback_from_high_pct": 2.0,
        "session_high": 10000,
        "latest_tick_ready": True,
        "turnover_strength": 1.0,
    }
    base_metadata.update(metadata)
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
        chase_risk=False,
        metadata=base_metadata,
    )
    support_status = {
        "nearest_support": "vwap" if support_ready else None,
        "nearest_support_price": 9780 if support_ready else 0,
        "support_distance_pct": 0.204 if support_ready else None,
        "support_touched": support_ready,
        "support_reclaimed": support_ready,
        "support_candidates": {"vwap": 9780} if support_ready else {},
        "selected_support_source": "vwap" if support_ready else "",
        "selected_support_price": 9780 if support_ready else 0,
        "selected_support_ready": support_ready,
        "selected_support_ready_reason": "",
        "support_ready": support_ready,
        "support_ready_reason": "",
        "support_readiness_reason_codes": [],
    }
    monkeypatch.setattr(gates, "_snapshot_for", lambda *args, **kwargs: snapshot)
    monkeypatch.setattr(gates, "support_status_for_snapshot", lambda *args, **kwargs: dict(support_status))
    monkeypatch.setattr(
        gates,
        "_late_chase_diagnostics",
        lambda *args, **kwargs: {
            "feature_version": "late_chase_diagnostics_v1",
            "late_chase_level": "none",
            "late_chase_score": 0.0,
            "reason_codes": [],
            "input_missing_fields": [],
            "support_distance_excessive": False,
            "volume_reacceleration_confirmed": True,
            "after_large_3m_candle": False,
            "after_large_5m_candle": False,
        },
    )
    gate = gates.StockPullbackEntryGate(
        indicator_calculator=object(),
        intraday_tracker=object(),
        candle_builder=object(),
        market_data=object(),
    )
    candidate_metadata = {}
    if previous_entry_risk:
        candidate_metadata = {
            "last_block_result": {
                "sub_status": "ENTRY_RISK_TEMP_WAIT",
                "reason_codes": ["ENTRY_RISK_TEMP_WAIT", "VI_COOLDOWN"],
            }
        }
    return gate.evaluate(
        Candidate(id=1, code="000001", strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE, metadata=candidate_metadata),
        ThemeStrengthResult(theme_id="ai", theme_name="AI", score=90, grade="A", details={}),
        StockLeadershipResult(
            candidate_id=1,
            code="000001",
            theme_id="ai",
            theme_name="AI",
            score=95,
            leadership_rank=1,
            leadership_role=role,
            details={"turnover": 100_000_000, "change_rate": base_metadata.get("change_rate", 0.0)},
        ),
    )
