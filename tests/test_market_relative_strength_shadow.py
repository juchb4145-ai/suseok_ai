from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from storage.db import TradingDatabase
from trading.strategy.market_relative_strength_shadow import (
    ACTION_TYPE,
    MarketRelativeStrengthCounterfactualAction,
    MarketRelativeStrengthShadowConfig,
    MarketRelativeStrengthShadowEvaluator,
    MarketRelativeStrengthShadowRuntimePipeline,
    MarketRelativeStrengthShadowScenario,
    MarketRelativeStrengthShadowStatus,
    MarketRelativeStrengthShadowVariant,
)
from trading.strategy.models import Candidate, CandidateState


def test_healthy_side_reduced_records_observe_candidate_without_entry_mutation() -> None:
    candidate = _candidate(
        side_regime="EXPANSION",
        market_action="ALLOW_REDUCED",
        market_reasons=["SPLIT_MARKET_HEALTHY_SIDE_REDUCED"],
        rs=4.2,
        entry_status="OBSERVE_READY",
        ready_allowed=True,
    )
    before_entry = dict(candidate.metadata["entry_decision"])

    decision = _evaluate(candidate)

    assert decision.shadow_scenario == MarketRelativeStrengthShadowScenario.HEALTHY_SIDE_REDUCED.value
    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE.value
    assert decision.shadow_variant == MarketRelativeStrengthShadowVariant.STRICT.value
    assert decision.counterfactual_action == MarketRelativeStrengthCounterfactualAction.OBSERVE_ONLY.value
    assert decision.counterfactual_position_size_multiplier_hint == 0.6
    assert decision.promotion_eligible is False
    assert candidate.metadata["entry_decision"] == before_entry


def test_weak_side_strict_shadow_keeps_actual_wait_market_contract() -> None:
    candidate = _candidate(side_regime="WEAK", market_action="WAIT_MARKET", rs=4.0, entry_status="MARKET_WAIT")

    decision = _evaluate(candidate)

    assert decision.shadow_scenario == MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW.value
    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE.value
    assert decision.shadow_variant == MarketRelativeStrengthShadowVariant.STRICT.value
    assert decision.counterfactual_action == MarketRelativeStrengthCounterfactualAction.OBSERVE_SMALL.value
    assert decision.actual_market_action == "WAIT_MARKET"
    assert decision.actual_entry_status == "MARKET_WAIT"
    assert decision.actual_ready_allowed is False
    assert decision.actual_dry_run_intent_allowed is False
    assert decision.shadow_filter_passed is True
    assert decision.review_candidate is True
    assert decision.promotion_eligible is False


def test_risk_off_side_diagnostic_is_observe_only_and_never_promotion_eligible() -> None:
    candidate = _candidate(side_regime="RISK_OFF", market_action="BLOCK_NEW_ENTRY", rs=5.0, entry_status="HARD_BLOCK")

    decision = _evaluate(candidate)

    assert decision.shadow_scenario == MarketRelativeStrengthShadowScenario.RISK_OFF_SIDE_DIAGNOSTIC.value
    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE.value
    assert decision.counterfactual_action == MarketRelativeStrengthCounterfactualAction.OBSERVE_ONLY.value
    assert decision.counterfactual_position_size_multiplier_hint == 0.0
    assert decision.promotion_eligible is False
    assert decision.actual_market_action == "BLOCK_NEW_ENTRY"
    assert decision.actual_entry_status == "HARD_BLOCK"


def test_systemic_risk_is_excluded_not_shadow_candidate() -> None:
    candidate = _candidate(side_regime="RISK_OFF", market_action="BLOCK_NEW_ENTRY", rs=7.0, systemic=True)

    decision = _evaluate(candidate)

    assert decision.shadow_scenario == MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED.value
    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SYSTEMIC_EXCLUDED.value
    assert decision.counterfactual_action == MarketRelativeStrengthCounterfactualAction.EXCLUDED.value
    assert "SYSTEMIC_RISK_SHADOW_EXCLUDED" in decision.reason_codes
    assert decision.promotion_eligible is False


def test_unknown_market_or_data_wait_is_excluded() -> None:
    candidate = _candidate(market_side="UNKNOWN", side_regime="DATA_WAIT", market_action="DATA_WAIT", rs=8.0)

    decision = _evaluate(candidate)

    assert decision.shadow_scenario == MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED.value
    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.DATA_WAIT.value
    assert "MARKET_SIDE_UNRESOLVED" in decision.reject_reason_codes
    assert "MARKET_RS_CONTEXT_NOT_READY" in decision.reason_codes


def test_stale_data_rejects_otherwise_valid_weak_shadow() -> None:
    candidate = _candidate(side_regime="WEAK", market_action="WAIT_MARKET", rs=5.0, realtime_tick_fresh=False)

    decision = _evaluate(candidate)

    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_REJECT.value
    assert "MARKET_RS_STALE_DATA" in decision.reject_reason_codes


def test_role_and_leader_only_follower_are_rejected() -> None:
    follower = _candidate(
        side_regime="WEAK",
        market_action="WAIT_MARKET",
        rs=5.0,
        role="FOLLOWER",
        raw_role="FOLLOWER",
        theme_state="LEADER_ONLY_THEME",
    )

    decision = _evaluate(follower)

    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_REJECT.value
    assert "MARKET_RS_ROLE_NOT_ALLOWED" in decision.reject_reason_codes


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"vi_active": True}, "MARKET_RS_VI_BLOCK"),
        ({"upper_limit_near": True}, "MARKET_RS_OVERHEAT_BLOCK"),
        ({"overheated": True}, "MARKET_RS_OVERHEAT_BLOCK"),
        ({"chase_risk": True}, "MARKET_RS_CHASE_BLOCK"),
    ],
)
def test_vi_upper_overheat_and_chase_guards_reject(kwargs: dict[str, bool], reason: str) -> None:
    candidate = _candidate(side_regime="WEAK", market_action="WAIT_MARKET", rs=5.0, **kwargs)

    decision = _evaluate(candidate)

    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_REJECT.value
    assert reason in decision.reject_reason_codes


@pytest.mark.parametrize(
    ("side_regime", "market_action", "rs", "expected_variant"),
    [
        ("WEAK", "WAIT_MARKET", 3.99, "BALANCED"),
        ("WEAK", "WAIT_MARKET", 4.0, "STRICT"),
        ("RISK_OFF", "BLOCK_NEW_ENTRY", 4.99, "BALANCED"),
        ("RISK_OFF", "BLOCK_NEW_ENTRY", 5.0, "STRICT"),
    ],
)
def test_threshold_boundaries_create_balanced_or_strict_variant(
    side_regime: str,
    market_action: str,
    rs: float,
    expected_variant: str,
) -> None:
    candidate = _candidate(side_regime=side_regime, market_action=market_action, rs=rs)

    decision = _evaluate(candidate)

    assert decision.shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE.value
    assert decision.shadow_variant == expected_variant


def test_pipeline_persists_events_dedupes_material_state_and_never_creates_orders(tmp_path) -> None:
    db = TradingDatabase(str(tmp_path / "market-rs-shadow.db"))
    now = datetime(2026, 6, 19, 9, 30)
    candidate = db.save_candidate(_candidate(side_regime="WEAK", market_action="WAIT_MARKET", rs=4.5, trade_date=now.date().isoformat()))
    pipeline = MarketRelativeStrengthShadowRuntimePipeline(
        db=db,
        config=MarketRelativeStrengthShadowConfig(enabled=True, interval_sec=0, dedupe_sec=60),
    )

    first = pipeline.run(now)
    second = pipeline.run(now + timedelta(seconds=30))
    candidate.metadata["entry_decision"]["price_location"] = "VWAP_RECLAIM"
    db.save_candidate(candidate)
    third = pipeline.run(now + timedelta(seconds=40))

    events = db.list_strategy_decision_events(trade_date=now.date().isoformat(), action_type=ACTION_TYPE, limit=10)
    saved = db.load_candidate(now.date().isoformat(), candidate.code)
    assert first["persisted_count"] == 1
    assert second["duplicate_suppressed_count"] == 1
    assert third["persisted_count"] == 1
    assert len(events) == 2
    assert saved is not None
    assert saved.state == CandidateState.WATCHING
    assert db.list_runtime_order_intents(limit=10) == []


def _evaluate(candidate: Candidate):
    evaluator = MarketRelativeStrengthShadowEvaluator(
        config=MarketRelativeStrengthShadowConfig(enabled=True),
    )
    return evaluator.evaluate_candidate(candidate, now=datetime(2026, 6, 19, 9, 30))


def _candidate(
    *,
    trade_date: str = "2026-06-19",
    code: str = "000001",
    market_side: str = "KOSPI",
    side_regime: str,
    market_action: str,
    market_reasons: list[str] | None = None,
    rs: float,
    systemic: bool = False,
    role: str = "LEADER_CONFIRMED",
    raw_role: str = "LEADER",
    theme_state: str = "LEADING_THEME",
    persistence_count: int = 2,
    price_location: str = "GOOD_PULLBACK",
    entry_status: str = "MARKET_WAIT",
    ready_allowed: bool = False,
    realtime_tick_fresh: bool = True,
    vi_active: bool = False,
    upper_limit_near: bool = False,
    overheated: bool = False,
    chase_risk: bool = False,
) -> Candidate:
    context = {
        "schema_version": "strategy_context_v3",
        "context_id": f"ctx-{code}-{side_regime}-{market_action}",
        "context_fresh": True,
        "session_phase": "MORNING_TREND",
        "market": {
            "market_side": market_side,
            "market_side_resolution_status": "RESOLVED" if market_side in {"KOSPI", "KOSDAQ"} else "UNRESOLVED",
            "side_market_regime": side_regime,
            "counterpart_market_side": "KOSDAQ" if market_side == "KOSPI" else "KOSPI",
            "counterpart_market_regime": "RISK_OFF" if side_regime == "EXPANSION" else "EXPANSION",
            "composite_market_mode": "SPLIT_KOSPI_ON",
            "systemic_risk_off": systemic,
            "global_market_regime": "RISK_OFF" if side_regime in {"WEAK", "RISK_OFF"} else "SELECTIVE",
            "market_action": market_action,
            "position_size_multiplier_hint": 0.6 if market_action == "ALLOW_REDUCED" else 0.0,
            "index_return_pct": -0.5,
            "counterpart_index_return_pct": -2.0,
            "risk_score": 0.2,
            "counterpart_risk_score": 0.8,
            "reason_codes": market_reasons or [],
        },
        "theme": {
            "theme_id": "theme-ai",
            "theme_name": "AI",
            "theme_state": theme_state,
            "theme_score": 88.0,
            "persistence_count": persistence_count,
            "reason_codes": [],
        },
        "stock": {
            "trade_stock_role": role,
            "raw_stock_role": raw_role,
            "relative_strength_vs_index_pct": rs,
            "relative_strength_band": "4_TO_6" if rs < 6 else "GE_6",
            "change_rate_pct": rs - 0.5,
            "turnover_krw": 12_000_000_000,
            "turnover_speed": 900_000_000,
            "execution_strength": 160.0,
            "momentum_1m": 0.2,
            "momentum_3m": 0.4,
            "momentum_5m": 0.6,
            "vwap": 9900,
            "price_vs_vwap_pct": 1.01,
            "pullback_from_high_pct": 2.5,
            "vi_active": vi_active,
            "upper_limit_near": upper_limit_near,
            "overheated": overheated,
            "stock_data_quality_status": "OK",
        },
        "data": {
            "realtime_tick_available": True,
            "realtime_tick_age_sec": 1.0,
            "realtime_tick_fresh": realtime_tick_fresh,
            "market_context_fresh": market_action != "DATA_WAIT",
            "theme_context_fresh": True,
            "data_quality_status": "OK",
        },
        "risk": {
            "vi_block": vi_active,
            "overheat_block": overheated,
            "chase_risk": chase_risk,
            "stale_data_block": not realtime_tick_fresh,
            "reason_codes": [],
        },
    }
    return Candidate(
        trade_date=trade_date,
        code=code,
        name=f"Stock {code}",
        state=CandidateState.WATCHING,
        detected_at=f"{trade_date}T09:00:00",
        last_seen_at=f"{trade_date}T09:30:00",
        metadata={
            "candidate_instance_id": f"ci-{code}",
            "strategy_context_v3": context,
            "entry_decision": {
                "entry_status": entry_status,
                "ready_allowed": ready_allowed,
                "dry_run_intent_allowed": False,
                "price_location": price_location,
                "current_price": 10000,
                "vwap": 9900,
            },
        },
    )
