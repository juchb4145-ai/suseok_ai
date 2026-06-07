from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kiwoom.client import MockKiwoomClient
from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candidates import CandidateCollector
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
from trading.strategy.holding import StaticHoldingProvider
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import IndexTick, MarketIndexStore
from trading.strategy.models import BlockType, CandidateState
from trading.strategy.pipeline import GatePipeline
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.review import TradeReviewService
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeConfig
from trading.strategy.runtime_settings import legacy_strategy_runtime_settings
from trading.strategy.virtual_orders import VirtualOrderService
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.lab import (
    LabGateDecision,
    LabGateStatus,
    MarketStatus,
    MarketSide,
    MarketStrengthSnapshot,
    PriceLocationStatus,
    StockRole,
    ThemeConditionSnapshot,
    ThemeLabFlowResult,
    ThemeLabThemeStatus,
    TradeabilityRiskLevel,
    WatchSetSnapshot,
)
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.dependencies import CoreSettings
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, NoopRuntimeOrderSink


NOW = datetime(2026, 6, 1, 9, 5, 0)


def test_ready_good_pullback_creates_dry_run_intent_without_gateway_command(tmp_path):
    runtime, db, gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    snapshot = runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    intents = db.list_runtime_order_intents(candidate_id=candidate.id)

    assert candidate.state == CandidateState.READY
    assert len(db.list_entry_plans(candidate.id)) == 1
    assert len(db.list_virtual_orders(candidate.id)) == 1
    assert len(intents) == 1
    assert intents[0]["source"] == "themelab_flow"
    assert intents[0]["idempotency_key"] == f"themelab_flow:2026-06-01:000001:{candidate.metadata['candidate_instance_id']}:entry:1"
    assert intents[0]["metadata"]["candidate_instance_id"] == candidate.metadata["candidate_instance_id"]
    assert intents[0]["metadata"]["decision_cycle_id"].startswith("themelab_flow:2026-06-01:")
    assert intents[0]["metadata"]["price_location_status"] == "GOOD_PULLBACK"
    assert intents[0]["metadata"]["support_price"] == 9950
    assert intents[0]["metadata"]["limit_price"] > 0
    assert intents[0]["metadata"]["split_leg"] == 1
    assert gateway_state.command_snapshot()["queued_count"] == 0
    assert snapshot.dry_run_entry_order_intent_count >= 1


def test_ready_pullback_reclaim_creates_dry_run_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.PULLBACK_RECLAIM),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert db.list_runtime_order_intents(candidate_id=candidate.id)[0]["metadata"]["price_location_status"] == "PULLBACK_RECLAIM"


def test_ready_small_leader_good_pullback_creates_scaled_small_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.READY_SMALL,
            PriceLocationStatus.GOOD_PULLBACK,
            role=StockRole.LEADER,
            risk=TradeabilityRiskLevel.RISK_ADJUST,
            multiplier=0.5,
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    intent = db.list_runtime_order_intents(candidate_id=candidate.id)[0]
    assert intent["metadata"]["order_eligibility"] == "BUY_ELIGIBLE_SMALL_PULLBACK"
    assert intent["metadata"]["weight_pct"] == 25.0


def test_risk_off_small_entry_creates_one_scaled_dry_run_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.READY_SMALL,
            PriceLocationStatus.VWAP_RECLAIM,
            role=StockRole.LEADER,
            reasons=("READY_RISK_OFF_SMALL", "RISK_OFF_SMALL_ENTRY", "RISK_OFF_RELATIVE_STRENGTH", "RISK_OFF_BREADTH_FILTER_PASS"),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.RISK_OFF.value,
            candidate_market_raw_status=MarketStatus.RISK_OFF.value,
            candidate_market_confirmed_status=MarketStatus.RISK_OFF.value,
            candidate_breadth_pct=0.50,
            candidate_breadth_ready=True,
            candidate_breadth_sample_count=140,
            candidate_breadth_gate_usable=True,
            multiplier=0.25,
            risk_off_entry_details=_risk_off_entry_details(observe_only=False),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    plan = db.list_entry_plans(candidate.id)[0]
    intent = db.list_runtime_order_intents(candidate_id=candidate.id)[0]

    assert details["sub_status"] == "READY_RISK_OFF_SMALL"
    assert details["order_eligibility"] == "BUY_ELIGIBLE_RISK_OFF_SMALL"
    assert details["risk_off_entry_allowed"] is True
    assert plan.cancel_condition["ready_type"] == "READY_RISK_OFF_SMALL"
    assert plan.cancel_condition["risk_off_entry_allowed"] is True
    assert plan.split_plan[0]["submittable"] is True
    assert plan.split_plan[0]["weight_pct"] <= 25.0
    assert plan.split_plan[1]["submittable"] is False
    assert plan.split_plan[1]["reason"] == "risk_off_small_later_leg_pending"
    assert intent["metadata"]["order_eligibility"] == "BUY_ELIGIBLE_RISK_OFF_SMALL"
    assert intent["metadata"]["weight_pct"] <= 25.0
    assert intent["metadata"]["risk_off_exit_hint"]["max_hold_minutes"] == 20


def test_risk_off_small_entry_observe_only_does_not_create_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.OBSERVE,
            PriceLocationStatus.VWAP_RECLAIM,
            role=StockRole.LEADER,
            reasons=("OBSERVE_RISK_OFF_SMALL_ENTRY", "RISK_OFF_SMALL_ENTRY", "RISK_OFF_RELATIVE_STRENGTH", "RISK_OFF_BREADTH_FILTER_PASS"),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.RISK_OFF.value,
            candidate_market_raw_status=MarketStatus.RISK_OFF.value,
            candidate_market_confirmed_status=MarketStatus.RISK_OFF.value,
            candidate_breadth_pct=0.50,
            candidate_breadth_ready=True,
            candidate_breadth_sample_count=140,
            candidate_breadth_gate_usable=True,
            multiplier=0.25,
            risk_off_entry_details=_risk_off_entry_details(observe_only=True),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert details["sub_status"] == "OBSERVE_RISK_OFF_SMALL_ENTRY"
    assert details["risk_off_entry_allowed"] is True
    assert details["risk_off_entry_observe_only"] is True
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_themelab_good_pullback_soft_block_waits_without_entry_plan_or_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.READY,
            PriceLocationStatus.GOOD_PULLBACK,
            risk=TradeabilityRiskLevel.SOFT_BLOCK,
            reasons=("HIGH_CHASE_RISK",),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert candidate.can_recover is True
    assert details["sub_status"] == "LATE_CHASE_TEMP_WAIT"
    assert details["risk_level"] == "SOFT_BLOCK"
    assert details["late_chase_level"] == "soft_block"
    assert details["late_chase_block_type"] == "temporary_wait"
    assert "SOFT_BLOCK_ONLY" in details["reason_codes"]
    assert "LATE_CHASE_TEMP_WAIT" in details["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_themelab_generic_soft_block_does_not_pollute_late_chase_bucket(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.READY,
            PriceLocationStatus.GOOD_PULLBACK,
            risk=TradeabilityRiskLevel.SOFT_BLOCK,
            reasons=("HIGH_RETURN_FOLLOWER",),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert details["sub_status"] == "RISK_SOFT_BLOCK_TEMP_WAIT"
    assert details["risk_soft_block"] is True
    assert details["late_chase_level"] == ""
    assert "RISK_SOFT_BLOCK_TEMP_WAIT" in details["reason_codes"]
    assert "LATE_CHASE_TEMP_WAIT" not in details["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_candidate_market_weak_wait_is_recoverable_without_entry_plan_or_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=("CANDIDATE_MARKET_WEAK", "KOSDAQ_MARKET_WEAK", "WAIT_MARKET_RECOVERY"),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.WEAK.value,
            market_side_reason_codes=("KOSDAQ_SIDE_BREADTH_WEAK", "SIDE_BREADTH_WEAK_INDEX_OK"),
            candidate_breadth_pct=0.25,
            candidate_breadth_ready=True,
            candidate_breadth_sample_count=120,
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.market == MarketSide.KOSDAQ.value
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert candidate.can_recover is True
    assert details["sub_status"] == "WAIT_CANDIDATE_MARKET_WEAK"
    assert details["order_eligibility"] == "NOT_ELIGIBLE_MARKET"
    assert details["candidate_market"] == MarketSide.KOSDAQ.value
    assert details["candidate_market_status"] == MarketStatus.WEAK.value
    assert details["candidate_breadth_pct"] == 0.25
    assert details["candidate_breadth_ready"] is True
    assert details["candidate_breadth_sample_count"] == 120
    assert "KOSDAQ_MARKET_WEAK" in details["reason_codes"]
    assert "KOSDAQ_SIDE_BREADTH_WEAK" in details["market_side_reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_restored_market_weak_state_does_not_create_dry_run_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=("CANDIDATE_MARKET_WEAK", "KOSDAQ_MARKET_WEAK", "MARKET_WEAK_CONFIRMED", "WAIT_CANDIDATE_MARKET_WEAK"),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.WEAK.value,
            candidate_market_raw_status=MarketStatus.WEAK.value,
            candidate_market_confirmed_status=MarketStatus.WEAK.value,
            market_side_reason_codes=("MARKET_CONFIRMATION_STATE_RESTORED", "MARKET_WEAK_CONFIRMED"),
            market_confirmation_state_restored=True,
            market_confirmation_state_persisted=True,
            market_confirmation_state_restore_reason="MARKET_CONFIRMATION_STATE_RESTORED",
            market_confirmation_state_source="restored_db",
            market_confirmation_state_version=1,
            market_confirmation_state_last_updated_at=NOW.isoformat(),
            market_confirmation_transition_type="WEAK_CONFIRMED",
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert details["sub_status"] == "WAIT_CANDIDATE_MARKET_WEAK"
    assert details["market_confirmation_state_restored"] is True
    assert details["market_confirmation_state_source"] == "restored_db"
    assert "MARKET_CONFIRMATION_STATE_RESTORED" in details["market_side_reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_virtual_orders(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_market_confirmation_pending_wait_is_recoverable_without_entry_plan_or_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=(
                "WAIT_MARKET_CONFIRMATION_PENDING",
                "MARKET_WEAK_CONFIRMATION_PENDING",
                "CANDIDATE_MARKET_WEAK_UNCONFIRMED",
            ),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.CHOPPY.value,
            candidate_market_raw_status=MarketStatus.WEAK.value,
            candidate_market_confirmed_status=MarketStatus.CHOPPY.value,
            candidate_market_confirmation_pending=True,
            market_side_reason_codes=(
                "WAIT_MARKET_CONFIRMATION_PENDING",
                "MARKET_WEAK_CONFIRMATION_PENDING",
                "CANDIDATE_MARKET_WEAK_UNCONFIRMED",
            ),
            candidate_breadth_pct=0.25,
            candidate_breadth_ready=True,
            candidate_breadth_sample_count=120,
            candidate_breadth_trust_level="HIGH",
            candidate_breadth_gate_usable=True,
            market_side_weak_consecutive_cycles=1,
            market_side_wait_started_at=NOW.isoformat(),
            market_side_cycle_id=NOW.isoformat(),
            market_side_never_recovered=True,
            market_side_blocked_buy_intent_count=1,
            market_side_recheck_after_sec=60,
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert candidate.can_recover is True
    assert details["sub_status"] == "WAIT_MARKET_CONFIRMATION_PENDING"
    assert details["order_eligibility"] == "NOT_ELIGIBLE_MARKET"
    assert details["candidate_market_confirmation_pending"] is True
    assert details["candidate_market_raw_status"] == MarketStatus.WEAK.value
    assert details["candidate_market_confirmed_status"] == MarketStatus.CHOPPY.value
    assert details["market_side_weak_consecutive_cycles"] == 1
    assert details["market_side_cycle_id"] == NOW.isoformat()
    assert details["market_side_never_recovered"] is True
    assert details["market_side_blocked_buy_intent_count"] == 1
    assert "CANDIDATE_MARKET_WEAK_UNCONFIRMED" in details["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_session_boundary_wait_does_not_create_dry_run_or_virtual_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=(
                "WAIT_MARKET_CONFIRMATION_PENDING",
                "MARKET_CONFIRMATION_STATE_RESTORE_SKIPPED",
                "MARKET_SESSION_POST_CLOSE",
            ),
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_status=MarketStatus.EXPANSION.value,
            candidate_market_raw_status=MarketStatus.EXPANSION.value,
            candidate_market_confirmed_status=MarketStatus.EXPANSION.value,
            candidate_market_confirmation_pending=True,
            market_side_reason_codes=(
                "MARKET_SESSION_BOUNDARY_DETECTED",
                "MARKET_SESSION_POST_CLOSE",
                "MARKET_CONFIRMATION_STATE_RESTORE_SKIPPED",
                "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
                "WAIT_MARKET_CONFIRMATION_PENDING",
            ),
            market_confirmation_state_restore_skipped=True,
            market_confirmation_state_restore_reason="MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
            market_confirmation_state_source="session_boundary_memory_fallback",
            market_confirmation_state_reset_reason="MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
            market_confirmation_state_reset_count=1,
            market_confirmation_state_max_restore_age_sec=0,
            market_session_id="2026-06-01:post_close",
            market_session_type="post_close",
            market_trade_date="2026-06-01",
            market_timezone="Asia/Seoul",
            market_schedule_source="runtime_settings",
            market_schedule_known=True,
            market_is_regular_session=False,
            market_restore_allowed=False,
            market_reset_required=True,
            market_reset_reason="MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE",
            market_session_reason_codes=("MARKET_SESSION_BOUNDARY_DETECTED", "MARKET_SESSION_POST_CLOSE"),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.TEMPORARY
    assert details["sub_status"] == "WAIT_MARKET_CONFIRMATION_PENDING"
    assert details["market_session_type"] == "post_close"
    assert details["market_restore_allowed"] is False
    assert details["market_confirmation_state_restore_skipped"] is True
    assert "MARKET_SESSION_POST_CLOSE" in details["market_side_reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_virtual_orders(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_session_boundary_wait_with_noop_sink_never_queues_gateway_command(tmp_path):
    runtime, db, gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=("WAIT_MARKET_CONFIRMATION_PENDING", "MARKET_CONFIRMATION_STATE_RESTORE_SKIPPED"),
            candidate_market_confirmation_pending=True,
            market_side_reason_codes=(
                "MARKET_SESSION_BOUNDARY_DETECTED",
                "MARKET_CONFIRMATION_STATE_RESTORE_SKIPPED",
                "WAIT_MARKET_CONFIRMATION_PENDING",
                "LIVE_PATH_UNCHANGED",
                "LIVE_ORDER_GUARD_NOT_BYPASSED",
            ),
            market_confirmation_state_restore_skipped=True,
            market_confirmation_state_source="session_boundary_memory_fallback",
            market_session_id="2026-06-01:post_close",
            market_session_type="post_close",
            market_restore_allowed=False,
            market_reset_required=True,
            market_session_reason_codes=("MARKET_SESSION_BOUNDARY_DETECTED", "MARKET_SESSION_POST_CLOSE"),
        ),
        dry_run_orders=False,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata["gate_results_by_theme"]["ai"]
    assert candidate.state == CandidateState.BLOCKED
    assert details["market_session_type"] == "post_close"
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_virtual_orders(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []
    assert gateway_state.command_snapshot()["queued_count"] == 0


@pytest.mark.parametrize(
    ("status", "price_location", "expected_final"),
    [
        (LabGateStatus.READY_SMALL, PriceLocationStatus.CHASE_HIGH, "OBSERVE_CHASE"),
        (LabGateStatus.READY_SMALL, PriceLocationStatus.BREAKOUT_CONTINUATION, "OBSERVE_BREAKOUT_CONTINUATION"),
        (LabGateStatus.READY, PriceLocationStatus.VWAP_OVEREXTENDED, "OBSERVE_VWAP_OVEREXTENDED"),
    ],
)
def test_chase_and_extension_locations_do_not_create_intents(tmp_path, status, price_location, expected_final):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(status, price_location, risk=TradeabilityRiskLevel.RISK_ADJUST),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []
    assert candidate.metadata["gate_results_by_theme"]["ai"]["sub_status"] == expected_final


def test_data_insufficient_waits_without_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.UNKNOWN,
            reasons=("DATA_INSUFFICIENT", "INDICATOR_DATA_INSUFFICIENT"),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert candidate.state == CandidateState.WATCHING
    assert candidate.metadata["gate_results_by_theme"]["ai"]["sub_status"] == "WAIT_DATA"
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_base_line_120_insufficient_candles_waits_without_plan(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        tick_metadata={
            "prev_close": 10000,
            "base_line_120": 9950,
            "base_line_120_ready": False,
            "base_line_120_candle_count": 80,
        },
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert candidate.metadata["gate_results_by_theme"]["ai"]["sub_status"] == "WAIT_DATA"
    assert "BASE_LINE_120_INSUFFICIENT_CANDLES" in candidate.metadata["gate_results_by_theme"]["ai"]["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_base_line_120_insufficient_but_vwap_ready_allows_early_small_first_leg(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        tick_metadata={
            "prev_close": 10000,
            "vwap": 9950,
            "vwap_ready": True,
            "base_line_120": 9800,
            "base_line_120_ready": False,
            "base_line_120_candle_count": 80,
        },
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    plan = db.list_entry_plans(candidate.id)[0]
    assert candidate.state == CandidateState.READY
    assert plan.cancel_condition["ready_type"] == "READY_EARLY_SMALL"
    assert plan.split_plan[0]["submittable"] is True
    assert plan.split_plan[1]["submittable"] is False
    assert plan.split_plan[2]["reason"] == "early_small_later_leg_pending"
    assert len(db.list_runtime_order_intents(candidate_id=candidate.id)) == 1


@pytest.mark.parametrize(
    ("metadata", "expected_reason"),
    [
        (
            {"prev_close": 10000, "vwap": 9950, "vwap_ready": False},
            "VWAP_NOT_READY",
        ),
        (
            {
                "prev_close": 10000,
                "recent_swing_low": 9950,
                "recent_swing_low_ready": True,
                "recent_swing_low_candle_count": 2,
            },
            "RECENT_SWING_LOW_NOT_READY",
        ),
        (
            {"prev_close": 10000, "opening_range": 9950, "opening_range_ready": False},
            "OPENING_RANGE_NOT_READY",
        ),
        (
            {"prev_close": 10000, "prev_day_level": 9950, "prev_day_level_ready": False},
            "PREV_DAY_LEVEL_NOT_READY",
        ),
    ],
)
def test_unready_support_sources_wait_without_intent(tmp_path, metadata, expected_reason):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        tick_metadata=metadata,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata.get("gate_results_by_theme", {}).get("ai") or runtime._theme_lab_bridge_results[0].details
    assert details["sub_status"] == "WAIT_DATA"
    assert expected_reason in details["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


@pytest.mark.parametrize(
    ("runtime_kwargs", "expected_reason"),
    [
        ({"with_tick": False}, "LATEST_TICK_MISSING"),
        ({"tick_timestamp": NOW - timedelta(seconds=40)}, "LATEST_TICK_STALE"),
    ],
)
def test_latest_tick_missing_or_stale_waits_without_intent(tmp_path, runtime_kwargs, expected_reason):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        **runtime_kwargs,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    details = candidate.metadata.get("gate_results_by_theme", {}).get("ai") or runtime._theme_lab_bridge_results[0].details
    assert details["sub_status"] == "WAIT_DATA"
    assert expected_reason in details["reason_codes"]
    assert db.list_entry_plans(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_theme_weak_blocks_without_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.BLOCKED,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=("THEME_WEAK",),
            theme_status=ThemeLabThemeStatus.WEAK_THEME,
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert candidate.state == CandidateState.BLOCKED
    assert candidate.block_type == BlockType.FINAL
    assert candidate.metadata["sub_status"] == "BLOCK_THEME"
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


@pytest.mark.parametrize(
    ("metadata", "expected_reason"),
    [
        ({"prev_close": 10000}, "SUPPORT_DATA_MISSING"),
        (
            {
                "prev_close": 10000,
                "vwap": 9950,
                "vwap_ready": True,
                "vwap_stale": True,
            },
            "SUPPORT_STALE_VWAP",
        ),
        (
            {
                "prev_close": 10000,
                "recent_support_price": 9000,
                "recent_support_ready": True,
                "recent_support_candle_count": 3,
                "base_line_120": 8800,
                "base_line_120_ready": True,
                "base_line_120_candle_count": 120,
            },
            "max_chase_exceeded",
        ),
    ],
)
def test_entry_plan_diagnostic_only_blocks_intent_for_missing_support_or_chase(tmp_path, metadata, expected_reason):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        tick_metadata=metadata,
        price=11000 if expected_reason == "max_chase_exceeded" else 10000,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    plans = db.list_entry_plans(candidate.id)
    if expected_reason in {"SUPPORT_DATA_MISSING", "SUPPORT_STALE_VWAP"}:
        assert plans == []
        assert candidate.metadata["gate_results_by_theme"]["ai"]["sub_status"] == "WAIT_DATA"
        assert expected_reason in candidate.metadata["gate_results_by_theme"]["ai"]["reason_codes"]
    else:
        assert plans[0].cancel_condition["diagnostic_only"] is True
        assert plans[0].cancel_condition["reason"] == expected_reason
    assert db.list_virtual_orders(candidate.id) == []
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_observe_sink_records_lifecycle_but_no_buy_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=False,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert candidate.state == CandidateState.READY
    assert db.list_virtual_orders(candidate.id)
    assert db.list_runtime_order_intents(candidate_id=candidate.id) == []


def test_second_cycle_dedupes_same_themelab_intent(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))
    runtime.cycle(NOW + timedelta(seconds=6))

    candidate = db.load_candidate("2026-06-01", "000001")
    assert len(db.list_runtime_order_intents(candidate_id=candidate.id)) == 1
    events = db.list_runtime_order_intent_events(db.list_runtime_order_intents(candidate_id=candidate.id)[0]["intent_id"])
    assert any(event["event_type"] == "duplicate_rejected" for event in events)


def test_bridge_disabled_keeps_themelab_scanner_only(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        bridge_enabled=False,
    )

    runtime.start(NOW)
    snapshot = runtime.cycle(NOW + timedelta(seconds=3))

    assert db.list_candidates("2026-06-01") == []
    assert "THEME_LAB_DRY_RUN_BRIDGE_DISABLED" in snapshot.warnings


def test_legacy_mode_ignores_theme_lab_bridge(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
        legacy=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    assert db.list_candidates("2026-06-01") == []


def test_same_code_theme_change_creates_new_candidate_instance_generation(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))
    first = db.load_candidate("2026-06-01", "000001")
    first_instance = first.metadata["candidate_instance_id"]

    runtime.theme_lab_pipeline.result = _flow_result(
        LabGateStatus.READY,
        PriceLocationStatus.GOOD_PULLBACK,
        theme_id="robotics",
        theme_name="Robotics",
    )
    runtime.cycle(NOW + timedelta(minutes=25))
    second = db.load_candidate("2026-06-01", "000001")

    assert second.id == first.id
    assert second.metadata["candidate_instance_id"] != first_instance
    assert second.metadata["candidate_generation_seq"] == 2
    assert second.metadata["candidate_generation_reason"] == "theme_changed"


class StaticThemeLabPipeline:
    def __init__(self, result: ThemeLabFlowResult) -> None:
        self.result = result
        self.last_result = None
        self.last_run_at = None
        self.interval_sec = 3

    def run_if_due(self, now: datetime):
        self.last_run_at = now.replace(microsecond=0)
        self.last_result = self.result
        return self.result

    def drain_warnings(self):
        return []

    def watchset_codes(self):
        return [item.symbol for item in self.result.watchset]


def _runtime(
    tmp_path,
    result: ThemeLabFlowResult,
    *,
    dry_run_orders: bool,
    bridge_enabled: bool = True,
    legacy: bool = False,
    tick_metadata: dict | None = None,
    price: int = 10000,
    tick_timestamp: datetime = NOW,
    with_tick: bool = True,
):
    db_path = Path(tmp_path) / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    client = MockKiwoomClient()
    settings = legacy_strategy_runtime_settings()
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    market_index_store = MarketIndexStore()
    market_index_store.update_index_tick(IndexTick.from_realtime("KOSPI", "KOSPI", 2500, 0.1, timestamp=NOW))
    market_index_store.update_index_tick(IndexTick.from_realtime("KOSDAQ", "KOSDAQ", 850, 0.2, timestamp=NOW))
    metadata = (
        {
            "prev_close": 10000,
            "recent_support_price": 9950,
            "recent_support_ready": True,
            "recent_support_candle_count": 3,
            "vwap": 9950,
            "vwap_ready": True,
            "base_line_120": 9800,
            "base_line_120_ready": True,
            "base_line_120_candle_count": 120,
        }
        if tick_metadata is None
        else dict(tick_metadata)
    )
    if with_tick:
        market_data.update_tick(
            StrategyTick.from_realtime(
                "000001",
                price,
                change_rate=5.0,
                cum_volume=10_000,
                trade_value=100_000_000,
                execution_strength=120.0,
                timestamp=tick_timestamp,
                metadata=metadata,
            )
        )
    _seed_theme(db)
    gateway_state = GatewayStateStore()
    core_settings = CoreSettings(
        db_path=db_path,
        local_token="test-token",
        mode="OBSERVE",
        runtime_mode="DRY_RUN" if dry_run_orders else "OBSERVE",
        runtime_allow_dry_run_orders=dry_run_orders,
        runtime_dry_run_position_amount=1_000_000,
    )
    order_sink = (
        DryRunRuntimeOrderSink(
            settings=core_settings,
            service=OrderEnqueueService(settings=core_settings, gateway_state=gateway_state, db_path=db_path),
        )
        if dry_run_orders
        else NoopRuntimeOrderSink()
    )
    theme_context_provider = DynamicThemeContextProvider(ThemeEngineRepository(db))
    gate_pipeline = GatePipeline(
        theme_context_provider,
        market_data,
        candle_builder,
        IndicatorCalculator(market_data, candle_builder),
        IntradayStateTracker(settings),
        market_index_store,
        settings,
    )
    config = StrategyRuntimeConfig(
        theme_engine_mode="legacy" if legacy else "themelab_flow",
        theme_lab_dry_run_bridge_enabled=bridge_enabled,
    )
    runtime = StrategyRuntime(
        db=db,
        candidate_collector=CandidateCollector(
            db,
            client=None,
            trade_date_provider=lambda: "2026-06-01",
            default_ttl_minutes=30,
        ),
        subscription_manager=RealTimeSubscriptionManager(client, max_codes=80),
        candle_builder=candle_builder,
        gate_pipeline=gate_pipeline,
        entry_plan_builder=EntryPlanBuilder(settings=settings),
        virtual_order_service=VirtualOrderService(db=db, settings=settings),
        virtual_position_service=VirtualPositionService(db=db),
        exit_decision_engine=ExitDecisionEngine(settings),
        trade_review_service=TradeReviewService(settings),
        config=config,
        holding_provider=StaticHoldingProvider(),
        order_sink=order_sink,
        theme_lab_pipeline=StaticThemeLabPipeline(result),
    )
    return runtime, db, gateway_state


def _flow_result(
    status: LabGateStatus,
    price_location: PriceLocationStatus,
    *,
    role: StockRole = StockRole.LEADER,
    risk: TradeabilityRiskLevel = TradeabilityRiskLevel.PASS,
    reasons: tuple[str, ...] = (),
    theme_status: ThemeLabThemeStatus = ThemeLabThemeStatus.LEADING_THEME,
    multiplier: float = 1.0,
    theme_id: str = "ai",
    theme_name: str = "AI",
    candidate_market: str = MarketSide.KOSDAQ.value,
    candidate_market_status: str = MarketStatus.EXPANSION.value,
    candidate_market_raw_status: str = "",
    candidate_market_confirmed_status: str = "",
    candidate_market_confirmation_pending: bool = False,
    candidate_market_recovery_pending: bool = False,
    market_side_reason_codes: tuple[str, ...] | None = None,
    candidate_breadth_pct: float | None = None,
    candidate_breadth_ready: bool = False,
    candidate_breadth_sample_count: int = 0,
    candidate_breadth_trust_level: str = "",
    candidate_breadth_gate_usable: bool = False,
    candidate_breadth_diagnostic_only: bool = False,
    market_side_weak_consecutive_cycles: int = 0,
    market_side_risk_off_consecutive_cycles: int = 0,
    market_side_healthy_consecutive_cycles: int = 0,
    market_side_wait_started_at: str = "",
    market_side_cycle_id: str = "",
    market_side_last_confirmed_at: str = "",
    market_side_last_recovered_at: str = "",
    market_side_recovered_at: str = "",
    market_side_cycles_to_recover: int = 0,
    market_side_recovered_to_ready: bool = False,
    market_side_never_recovered: bool = False,
    market_side_blocked_buy_intent_count: int = 0,
    market_side_recheck_after_sec: int = 0,
    market_confirmation_state_persisted: bool = False,
    market_confirmation_state_restored: bool = False,
    market_confirmation_state_restore_reason: str = "",
    market_confirmation_state_last_updated_at: str = "",
    market_confirmation_state_age_sec: float | None = None,
    market_confirmation_state_version: int = 0,
    market_confirmation_state_source: str = "memory",
    market_confirmation_state_reset_reason: str = "",
    market_confirmation_state_restore_skipped: bool = False,
    market_confirmation_state_max_restore_age_sec: int = 0,
    market_confirmation_state_expires_at: str = "",
    market_confirmation_state_reset_count: int = 0,
    market_confirmation_transition_type: str = "",
    market_session_id: str = "",
    market_session_type: str = "",
    market_trade_date: str = "",
    market_timezone: str = "",
    market_schedule_source: str = "",
    market_schedule_known: bool = True,
    market_is_regular_session: bool = True,
    market_restore_allowed: bool = True,
    market_reset_required: bool = False,
    market_reset_reason: str = "",
    market_session_reason_codes: tuple[str, ...] = (),
    market_confirmation_metrics: dict | None = None,
    risk_off_entry_details: dict | None = None,
) -> ThemeLabFlowResult:
    theme = ThemeConditionSnapshot(
        calculated_at=NOW.isoformat(),
        theme_id=theme_id,
        theme_name=theme_name,
        raw_total_members=1,
        eligible_total_members=1,
        alive_count=1,
        strong_count=1,
        leader_count=1,
        alive_ratio=1.0,
        strong_ratio=1.0,
        leader_ratio=1.0,
        condition_score=85.0,
        theme_status=theme_status,
    )
    watch = WatchSetSnapshot(
        calculated_at=NOW.isoformat(),
        symbol="000001",
        name="leader",
        themes=(theme_id,),
        primary_theme=theme_id,
        return_pct=5.0,
        turnover_krw=100_000_000,
        condition_level=3,
        stock_role=role,
        gate_status=status,
        final_gate_status=status,
        risk_level=risk,
        risk_reason_codes=reasons,
        position_size_multiplier=multiplier,
        price_location_status=price_location,
        price_location_score=80.0,
        price_location_reason_codes=(price_location.value,),
        candidate_market=candidate_market,
        candidate_market_source="test",
        candidate_market_status=candidate_market_status,
        candidate_market_action="TEMPORARY_WAIT" if status == LabGateStatus.WAIT and "MARKET" in " ".join(reasons) else "PASS",
        candidate_index_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
        global_market_status=MarketStatus.SELECTIVE.value,
        kospi_market_status=MarketStatus.SELECTIVE.value,
        kosdaq_market_status=candidate_market_status if candidate_market == MarketSide.KOSDAQ.value else MarketStatus.SELECTIVE.value,
        kospi_return_pct=0.1,
        kosdaq_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
        candidate_breadth_pct=candidate_breadth_pct,
        candidate_breadth_ready=candidate_breadth_ready,
        candidate_breadth_sample_count=candidate_breadth_sample_count,
        candidate_breadth_source="SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE" if candidate_breadth_ready else "",
        candidate_valid_quote_ratio=1.0 if candidate_breadth_ready else None,
        candidate_breadth_trust_level=candidate_breadth_trust_level,
        candidate_breadth_gate_usable=candidate_breadth_gate_usable,
        candidate_breadth_diagnostic_only=candidate_breadth_diagnostic_only,
        candidate_market_raw_status=candidate_market_raw_status,
        candidate_market_confirmed_status=candidate_market_confirmed_status,
        candidate_market_confirmation_pending=candidate_market_confirmation_pending,
        candidate_market_recovery_pending=candidate_market_recovery_pending,
        market_side_weak_consecutive_cycles=market_side_weak_consecutive_cycles,
        market_side_risk_off_consecutive_cycles=market_side_risk_off_consecutive_cycles,
        market_side_healthy_consecutive_cycles=market_side_healthy_consecutive_cycles,
        market_side_wait_started_at=market_side_wait_started_at,
        market_side_cycle_id=market_side_cycle_id,
        market_side_last_confirmed_at=market_side_last_confirmed_at,
        market_side_last_recovered_at=market_side_last_recovered_at,
        market_side_recovered_at=market_side_recovered_at,
        market_side_cycles_to_recover=market_side_cycles_to_recover,
        market_side_recovered_to_ready=market_side_recovered_to_ready,
        market_side_never_recovered=market_side_never_recovered,
        market_side_blocked_buy_intent_count=market_side_blocked_buy_intent_count,
        market_side_recheck_after_sec=market_side_recheck_after_sec,
        market_confirmation_state_persisted=market_confirmation_state_persisted,
        market_confirmation_state_restored=market_confirmation_state_restored,
        market_confirmation_state_restore_reason=market_confirmation_state_restore_reason,
        market_confirmation_state_last_updated_at=market_confirmation_state_last_updated_at,
        market_confirmation_state_age_sec=market_confirmation_state_age_sec,
        market_confirmation_state_version=market_confirmation_state_version,
        market_confirmation_state_source=market_confirmation_state_source,
        market_confirmation_state_reset_reason=market_confirmation_state_reset_reason,
        market_confirmation_state_restore_skipped=market_confirmation_state_restore_skipped,
        market_confirmation_state_max_restore_age_sec=market_confirmation_state_max_restore_age_sec,
        market_confirmation_state_expires_at=market_confirmation_state_expires_at,
        market_confirmation_state_reset_count=market_confirmation_state_reset_count,
        market_confirmation_transition_type=market_confirmation_transition_type,
        market_session_id=market_session_id,
        market_session_type=market_session_type,
        market_trade_date=market_trade_date,
        market_timezone=market_timezone,
        market_schedule_source=market_schedule_source,
        market_schedule_known=market_schedule_known,
        market_is_regular_session=market_is_regular_session,
        market_restore_allowed=market_restore_allowed,
        market_reset_required=market_reset_required,
        market_reset_reason=market_reset_reason,
        market_session_reason_codes=market_session_reason_codes,
        market_confirmation_metrics=dict(market_confirmation_metrics or {}),
        market_side_reason_codes=market_side_reason_codes
        if market_side_reason_codes is not None
        else (reasons if any("MARKET" in reason for reason in reasons) else ()),
    )
    decision = LabGateDecision(
        symbol="000001",
        status=status,
        reason_codes=reasons,
        blocked_reason="THEME_WEAK" if theme_status == ThemeLabThemeStatus.WEAK_THEME else "",
        risk_level=risk,
        risk_reason_codes=reasons,
        position_size_multiplier=multiplier,
        recheck_after_sec=30,
        price_location_status=price_location,
        price_location_score=80.0,
        price_location_reason_codes=(price_location.value,),
        candidate_market=candidate_market,
        candidate_market_source="test",
        candidate_market_status=candidate_market_status,
        candidate_market_action="TEMPORARY_WAIT" if status == LabGateStatus.WAIT and "MARKET" in " ".join(reasons) else "PASS",
        candidate_index_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
        global_market_status=MarketStatus.SELECTIVE.value,
        kospi_market_status=MarketStatus.SELECTIVE.value,
        kosdaq_market_status=candidate_market_status if candidate_market == MarketSide.KOSDAQ.value else MarketStatus.SELECTIVE.value,
        kospi_return_pct=0.1,
        kosdaq_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
        candidate_breadth_pct=candidate_breadth_pct,
        candidate_breadth_ready=candidate_breadth_ready,
        candidate_breadth_sample_count=candidate_breadth_sample_count,
        candidate_breadth_source="SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE" if candidate_breadth_ready else "",
        candidate_valid_quote_ratio=1.0 if candidate_breadth_ready else None,
        candidate_breadth_trust_level=candidate_breadth_trust_level,
        candidate_breadth_gate_usable=candidate_breadth_gate_usable,
        candidate_breadth_diagnostic_only=candidate_breadth_diagnostic_only,
        candidate_market_raw_status=candidate_market_raw_status,
        candidate_market_confirmed_status=candidate_market_confirmed_status,
        candidate_market_confirmation_pending=candidate_market_confirmation_pending,
        candidate_market_recovery_pending=candidate_market_recovery_pending,
        market_side_weak_consecutive_cycles=market_side_weak_consecutive_cycles,
        market_side_risk_off_consecutive_cycles=market_side_risk_off_consecutive_cycles,
        market_side_healthy_consecutive_cycles=market_side_healthy_consecutive_cycles,
        market_side_wait_started_at=market_side_wait_started_at,
        market_side_cycle_id=market_side_cycle_id,
        market_side_last_confirmed_at=market_side_last_confirmed_at,
        market_side_last_recovered_at=market_side_last_recovered_at,
        market_side_recovered_at=market_side_recovered_at,
        market_side_cycles_to_recover=market_side_cycles_to_recover,
        market_side_recovered_to_ready=market_side_recovered_to_ready,
        market_side_never_recovered=market_side_never_recovered,
        market_side_blocked_buy_intent_count=market_side_blocked_buy_intent_count,
        market_side_recheck_after_sec=market_side_recheck_after_sec,
        market_confirmation_state_persisted=market_confirmation_state_persisted,
        market_confirmation_state_restored=market_confirmation_state_restored,
        market_confirmation_state_restore_reason=market_confirmation_state_restore_reason,
        market_confirmation_state_last_updated_at=market_confirmation_state_last_updated_at,
        market_confirmation_state_age_sec=market_confirmation_state_age_sec,
        market_confirmation_state_version=market_confirmation_state_version,
        market_confirmation_state_source=market_confirmation_state_source,
        market_confirmation_state_reset_reason=market_confirmation_state_reset_reason,
        market_confirmation_state_restore_skipped=market_confirmation_state_restore_skipped,
        market_confirmation_state_max_restore_age_sec=market_confirmation_state_max_restore_age_sec,
        market_confirmation_state_expires_at=market_confirmation_state_expires_at,
        market_confirmation_state_reset_count=market_confirmation_state_reset_count,
        market_confirmation_transition_type=market_confirmation_transition_type,
        market_session_id=market_session_id,
        market_session_type=market_session_type,
        market_trade_date=market_trade_date,
        market_timezone=market_timezone,
        market_schedule_source=market_schedule_source,
        market_schedule_known=market_schedule_known,
        market_is_regular_session=market_is_regular_session,
        market_restore_allowed=market_restore_allowed,
        market_reset_required=market_reset_required,
        market_reset_reason=market_reset_reason,
        market_session_reason_codes=market_session_reason_codes,
        market_confirmation_metrics=dict(market_confirmation_metrics or {}),
        market_side_reason_codes=market_side_reason_codes
        if market_side_reason_codes is not None
        else (reasons if any("MARKET" in reason for reason in reasons) else ()),
        risk_off_entry_details=dict(risk_off_entry_details or {}),
    )
    return ThemeLabFlowResult(
        market=MarketStrengthSnapshot(
            MarketStatus.SELECTIVE,
            kospi_return_pct=0.1,
            kosdaq_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
            kospi_status=MarketStatus.SELECTIVE,
            kosdaq_status=MarketStatus.WEAK if candidate_market_status == MarketStatus.WEAK.value else MarketStatus.EXPANSION,
            kospi_index_return_pct=0.1,
            kosdaq_index_return_pct=-1.2 if candidate_market_status == MarketStatus.WEAK.value else 0.2,
        ),
        themes=(theme,),
        watchset=(watch,),
        gate_decisions=(decision,),
        data_quality={},
    )


def _risk_off_entry_details(*, observe_only: bool) -> dict:
    return {
        "risk_off_entry_enabled": True,
        "risk_off_entry_observe_only": observe_only,
        "risk_off_entry_allowed": True,
        "risk_off_entry_rejected_reason": "",
        "risk_off_relative_strength_pct": 8.2,
        "risk_off_candidate_breadth_pct": 0.50,
        "risk_off_candidate_index_return_pct": -3.2,
        "risk_off_max_position_size_multiplier": 0.25,
        "risk_off_exit_hint": {
            "stop_loss_pct": -1.2,
            "take_profit_pct": 1.8,
            "max_hold_minutes": 20,
        },
    }


def _seed_theme(db: TradingDatabase) -> None:
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id="ai",
            canonical_name="AI",
            display_name="AI",
            status=ThemeStatus.ACTIVE,
            trade_eligible=True,
        )
    )
    repo.upsert_current_membership(
        ThemeMembership(
            theme_id="ai",
            stock_code="000001",
            stock_name="leader",
            membership_score=1.0,
            active=True,
            trade_eligible=True,
        )
    )
