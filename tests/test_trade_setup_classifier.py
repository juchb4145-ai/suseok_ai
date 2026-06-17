from __future__ import annotations

import pytest

from trading.strategy.runtime_settings import StrategyRuntimeSettings
from trading.strategy.trade_setup_classifier import TradeSetupType, classify_trade_setup


def test_strong_leader_good_pullback_pass_classifies_core_pullback():
    decision = classify_trade_setup(
        {
            "final_gate_status": "READY",
            "theme_status": "LEADING_THEME",
            "dynamic_theme_score": 86.0,
            "stock_role": "LEADER",
            "price_location_status": "GOOD_PULLBACK",
            "risk_level": "PASS",
            "position_size_multiplier": 1.0,
        }
    )

    assert decision.setup_type == TradeSetupType.CORE_PULLBACK
    assert decision.recommended_action == "NORMAL_READY"
    assert decision.recommended_position_size_multiplier == pytest.approx(1.0)


def test_strong_leader_provisional_vwap_without_hard_risk_classifies_leader_probe():
    decision = classify_trade_setup(
        {
            "final_gate_status": "WAIT",
            "theme_status": "LEADING_THEME",
            "dynamic_theme_score": 82.0,
            "stock_role": "LEADER",
            "price_location_status": "VWAP_RECLAIM",
            "price_location_readiness": "PROVISIONAL",
            "risk_level": "PASS",
            "latest_tick_ready": True,
            "reason_codes": ["WAIT_PRICE_LOCATION_PROVISIONAL"],
        }
    )

    assert decision.setup_type == TradeSetupType.LEADER_PROBE
    assert decision.recommended_action == "SMALL_OBSERVE"


def test_weak_market_leader_relative_strength_classifies_relative_strength():
    decision = classify_trade_setup(
        {
            "final_gate_status": "WAIT",
            "theme_status": "LEADING_THEME",
            "dynamic_theme_score": 88.0,
            "stock_role": "CO_LEADER",
            "price_location_status": "PULLBACK_RECLAIM",
            "risk_level": "PASS",
            "candidate_market_status": "WEAK",
            "reason_codes": ["RELATIVE_STRENGTH"],
        }
    )

    assert decision.setup_type == TradeSetupType.RELATIVE_STRENGTH
    assert decision.recommended_action == "SMALL_OBSERVE"


def test_breakout_leader_low_risk_classifies_momentum_continuation_observe():
    decision = classify_trade_setup(
        {
            "final_gate_status": "OBSERVE_BREAKOUT_CONTINUATION",
            "theme_status": "LEADING_THEME",
            "stock_role": "LEADER",
            "price_location_status": "BREAKOUT_CONTINUATION",
            "risk_level": "PASS",
            "turnover_krw": 5_000_000_000,
            "momentum_1m": 0.4,
            "momentum_3m": 0.2,
            "upper_wick_risk": False,
            "reason_codes": ["BREAKOUT_CONTINUATION", "TURNOVER_MAINTAINED"],
        }
    )

    assert decision.setup_type == TradeSetupType.MOMENTUM_CONTINUATION
    assert decision.recommended_action == "OBSERVE"


def test_spreading_theme_follower_with_flow_classifies_rotation_follower_observe():
    decision = classify_trade_setup(
        {
            "final_gate_status": "OBSERVE",
            "theme_status": "SPREADING_THEME",
            "stock_role": "FOLLOWER",
            "price_location_status": "PULLBACK_RECLAIM",
            "risk_level": "PASS",
            "turnover_krw": 2_000_000_000,
            "momentum_1m": 0.3,
            "momentum_3m": 0.1,
            "reason_codes": ["FOLLOWER_MOMENTUM"],
        }
    )

    assert decision.setup_type == TradeSetupType.ROTATION_FOLLOWER
    assert decision.recommended_action == "OBSERVE"


@pytest.mark.parametrize("reason_code", ["LATE_LAGGARD", "VI_ACTIVE", "UPPER_LIMIT_HARD_NEAR"])
def test_late_laggard_vi_or_upper_limit_hard_near_classifies_avoid(reason_code):
    decision = classify_trade_setup(
        {
            "final_gate_status": "BLOCKED",
            "theme_status": "LEADING_THEME",
            "stock_role": "LEADER",
            "price_location_status": "GOOD_PULLBACK",
            "risk_level": "PASS",
            "reason_codes": [reason_code],
        }
    )

    assert decision.setup_type == TradeSetupType.AVOID
    assert decision.recommended_action == "BLOCK"


def test_safety_defaults_remain_observe_or_order_disabled():
    settings = StrategyRuntimeSettings.legacy_default()

    assert settings.value("hybrid_gate.observe_only") is True
    assert settings.value("data_quality_early_small.order_enabled") is False
    assert settings.value("shadow_small_entry_promotion.order_enabled") is False
    assert settings.value("live_sim_hybrid_ready_canary.enabled") is False
    assert settings.value("live_sim_hybrid_ready_canary.order_enabled") is False
