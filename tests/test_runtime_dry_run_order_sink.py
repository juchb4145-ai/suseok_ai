from pathlib import Path

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateSourceType,
    CandidateState,
    EntryPlan,
    StrategyProfile,
    VirtualOrder,
    VirtualOrderStatus,
)
from trading.strategy.pipeline import GatePipelineResult
from trading_app.dependencies import CoreSettings
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, NoopRuntimeOrderSink


def _settings(tmp_path, *, runtime_mode="DRY_RUN", allow_dry=True, allow_live=False):
    return CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        mode="OBSERVE",
        allow_live=allow_live,
        runtime_mode=runtime_mode,
        runtime_allow_dry_run_orders=allow_dry,
        runtime_dry_run_account="",
        runtime_dry_run_position_amount=1_000_000,
    )


def _candidate():
    return Candidate(
        id=11,
        trade_date="2026-05-30",
        code="005930",
        name="Samsung",
        market="KOSPI",
        strategy_profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE,
        sources=[CandidateSourceType.CONDITION],
        state=CandidateState.READY,
    )


def _plan():
    return EntryPlan(
        id=21,
        candidate_id=11,
        entry_type="pullback",
        limit_price=70000,
        split_plan=[{"leg": 1, "limit_price": 70000, "weight_pct": 50}],
        cancel_condition={"theme_id": "theme-a", "gate_result_key": "g1"},
    )


def _order(**overrides):
    payload = {
        "id": 31,
        "candidate_id": 11,
        "entry_plan_id": 21,
        "leg_index": 1,
        "weight_pct": 50.0,
        "status": VirtualOrderStatus.SUBMITTED,
        "limit_price": 70000,
        "submitted_at": "2026-05-30T09:01:00",
    }
    payload.update(overrides)
    return VirtualOrder(**payload)


def _gate_result():
    return GatePipelineResult(
        candidate_id=11,
        code="005930",
        theme_id="theme-a",
        final_grade="A",
        final_score=82.5,
        strategy_eligible=True,
        block_type=BlockType.NONE,
        details={"theme_name": "AI반도체", "theme_score": 76.0, "gate_result_key": "g1"},
    )


def test_dry_run_order_sink_records_intent_and_never_enqueues_gateway_command(tmp_path):
    settings = _settings(tmp_path)
    gateway_state = GatewayStateStore()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)
    sink = DryRunRuntimeOrderSink(settings=settings, service=service)

    result = sink.on_entry_order_decision(
        candidate=_candidate(),
        gate_result=_gate_result(),
        entry_plan=_plan(),
        virtual_order=_order(),
        runtime_cycle_at="2026-05-30T09:02:00",
    )

    assert result["accepted"] is True
    assert result["command"] is None
    assert result["request"]["quantity"] == 7
    assert gateway_state.command_snapshot()["queued_count"] == 0

    db = TradingDatabase(str(settings.db_path))
    try:
        row = db.get_runtime_order_intent(result["intent_id"])
    finally:
        db.close()
    assert row["virtual_order_id"] == 31
    assert row["candidate_id"] == 11
    assert row["trade_date"] == "2026-05-30"
    assert row["code"] == "005930"
    assert row["side"] == "buy"
    assert row["order_phase"] == "entry"
    assert row["price"] == 70000
    assert row["quantity"] > 0
    assert row["strategy_name"] == "SEMICONDUCTOR_SIGNAL_PROFILE"
    assert row["metadata"]["calculated_order_amount"] == 500000


def test_rejected_dry_run_intent_keeps_reject_reason_and_decision_metadata(tmp_path):
    settings = _settings(tmp_path)
    gateway_state = GatewayStateStore()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)
    sink = DryRunRuntimeOrderSink(settings=settings, service=service)
    gate_result = _gate_result()
    gate_result.details["theme_lab_bridge"] = {
        "source": "themelab_flow",
        "trade_date": "2026-05-30",
        "candidate_id": 11,
        "theme_id": "theme-a",
        "theme_name": "AI",
        "lab_gate_status": "READY",
        "final_gate_status": "READY_PULLBACK",
        "order_eligibility": "BUY_ELIGIBLE_PULLBACK",
        "price_location_status": "GOOD_PULLBACK",
        "risk_level": "PASS",
        "reason_codes": ["READY_PULLBACK"],
    }

    result = sink.on_entry_order_decision(
        candidate=_candidate(),
        gate_result=gate_result,
        entry_plan=_plan(),
        virtual_order=_order(limit_price=0, weight_pct=50.0),
        runtime_cycle_at="2026-05-30T09:02:00",
    )

    assert result["accepted"] is False
    assert result["status"] == "DRY_RUN_REJECTED"
    assert result["reason"] in {"PRICE_INVALID", "PRICE_INVALID_OR_MARKET_ORDER_UNSUPPORTED", "QUANTITY_INVALID"}
    assert result["command"] is None

    db = TradingDatabase(str(settings.db_path))
    try:
        row = db.get_runtime_order_intent(result["intent_id"])
    finally:
        db.close()
    assert row["source"] == "themelab_flow"
    assert row["trade_date"] == "2026-05-30"
    assert row["code"] == "005930"
    assert row["side"] == "buy"
    assert row["order_phase"] == "entry"
    assert row["price"] == 70000
    assert row["quantity"] == 0
    assert row["metadata"]["order_eligibility"] == "BUY_ELIGIBLE_PULLBACK"
    assert row["metadata"]["quantity_calculation_reason"] == "PRICE_INVALID"


def test_dry_run_order_sink_dedupes_same_virtual_order(tmp_path):
    settings = _settings(tmp_path)
    gateway_state = GatewayStateStore()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)
    sink = DryRunRuntimeOrderSink(settings=settings, service=service)
    kwargs = {
        "candidate": _candidate(),
        "gate_result": _gate_result(),
        "entry_plan": _plan(),
        "virtual_order": _order(),
        "runtime_cycle_at": "2026-05-30T09:02:00",
    }

    first = sink.on_entry_order_decision(**kwargs)
    second = sink.on_entry_order_decision(**kwargs)

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["status"] == "DUPLICATE"
    assert second["duplicate_of"] == first["intent_id"]
    assert sink.snapshot()["dry_run_order_duplicate_count"] >= 1


def test_noop_runtime_order_sink_does_not_create_intent(tmp_path):
    sink = NoopRuntimeOrderSink(reason="OBSERVE_VIRTUAL_ONLY")
    result = sink.on_entry_order_decision(
        candidate=_candidate(),
        gate_result=_gate_result(),
        entry_plan=_plan(),
        virtual_order=_order(),
        runtime_cycle_at="2026-05-30T09:02:00",
    )

    assert result["status"] == "SKIPPED"
    assert sink.snapshot()["dry_run_order_sink_enabled"] is False
