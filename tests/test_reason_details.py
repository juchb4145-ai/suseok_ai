from datetime import datetime

from trading.strategy.models import BlockType, GateDecision
from trading.strategy.reason_codes import (
    COMPARISON_MODE_LEGACY_ONLY,
    P1_REASON_CODES,
    REASON_DETAILS_FEATURE_VERSION,
    STRATEGY_FEATURE_VERSION,
    ReasonCode,
    standardize_details,
)
from trading.strategy.session import session_bucket_at


def test_p1_reason_codes_are_centralized():
    assert {
        "MARKET_BREADTH_WEAK",
        "INDEX_SLOPE_WEAK",
        "OPEN_GAP_RISK",
        "THEME_SYNC_WEAK",
        "LEADER_FOLLOWER_GAP",
        "LEADER_REPLACED",
        "LATE_CHASE",
        "FILL_LIQUIDITY_WEAK",
        "SPREAD_TOO_WIDE",
        "SESSION_PROFILE_RESTRICTED",
        "INPUT_MISSING",
        "BREADTH_SCOPE_LIMITED",
        "FILL_INPUT_INSUFFICIENT",
        "SOFT_BLOCK_ONLY",
    }.issubset(P1_REASON_CODES)
    assert ReasonCode.LATE_CHASE.value in P1_REASON_CODES


def test_session_bucket_boundaries():
    assert session_bucket_at(datetime(2026, 5, 29, 9, 0)) == "OPEN_0_10"
    assert session_bucket_at(datetime(2026, 5, 29, 9, 10)) == "OPEN_10_90"
    assert session_bucket_at(datetime(2026, 5, 29, 10, 30)) == "MIDDAY"
    assert session_bucket_at(datetime(2026, 5, 29, 13, 30)) == "LATE"


def test_gate_decision_details_get_standard_reason_schema_without_decision_change():
    decision = GateDecision(
        gate_name="MarketIndexGate",
        passed=False,
        score=20.0,
        block_type=BlockType.TEMPORARY,
        reason_codes=["INDEX_WEAK", "OPEN_GAP_RISK"],
        details={"sub_status": "MARKET_WAIT"},
        created_at="2026-05-29T09:05:00",
    )

    assert decision.passed is False
    assert decision.score == 20.0
    assert decision.reason_codes == ["INDEX_WEAK", "OPEN_GAP_RISK"]
    assert decision.details["feature_version"] == REASON_DETAILS_FEATURE_VERSION
    assert decision.details["strategy_feature_version"] == STRATEGY_FEATURE_VERSION
    assert decision.details["session_bucket"] == "OPEN_0_10"
    assert decision.details["primary_reason_code"] == "INDEX_WEAK"
    assert decision.details["secondary_reason_codes"] == ["OPEN_GAP_RISK"]
    assert decision.details["legacy_result"] is False
    assert decision.details["new_result"] is False
    assert decision.details["legacy_score"] == 20.0
    assert decision.details["new_score"] == 20.0
    assert decision.details["comparison_mode"] == COMPARISON_MODE_LEGACY_ONLY


def test_standard_details_extracts_missing_inputs_from_existing_details():
    details = standardize_details(
        {"insufficient_reason": ["tick_missing", "turnover_missing", "volume_low"]},
        ["DATA_INSUFFICIENT"],
        passed=False,
    )

    assert details["primary_reason_code"] == "DATA_INSUFFICIENT"
    assert details["input_missing_fields"] == ["tick_missing", "turnover_missing"]
