from pathlib import Path

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.exit import SUPPORT_LOSS, TAKE_PROFIT, THEME_WEAK_EXIT, TIME_EXIT, TRAILING_STOP
from trading.strategy.models import (
    Candidate,
    CandidateSourceType,
    CandidateState,
    ExitDecision,
    FillPolicy,
    StrategyProfile,
    VirtualPosition,
)
from trading_app.dependencies import CoreSettings
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink


def _settings(tmp_path):
    return CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        mode="OBSERVE",
        allow_live=False,
        runtime_mode="DRY_RUN",
        runtime_allow_dry_run_orders=True,
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


def _position(**overrides):
    payload = {
        "id": 41,
        "candidate_id": 11,
        "virtual_order_id": 31,
        "entry_price": 70000,
        "quantity": 10,
        "opened_at": "2026-05-30T09:01:00",
        "closed_at": "",
        "close_price": 0,
        "close_reason": "",
        "max_return_pct": 3.2,
        "max_drawdown_pct": -0.4,
        "realized_return_pct": 0.0,
        "details": {"remaining_weight_pct": 100.0, "filled_weight_pct": 100.0},
    }
    payload.update(overrides)
    return VirtualPosition(**payload)


def _decision(decision_type=TAKE_PROFIT, decision_id=51, **detail_overrides):
    details = {
        "partial_exit": decision_type == TAKE_PROFIT,
        "full_exit": decision_type != TAKE_PROFIT,
        "position_closed": decision_type != TAKE_PROFIT,
        "exit_percent": 70,
        "virtual_exit_price": 73500,
    }
    details.update(detail_overrides)
    return ExitDecision(
        id=decision_id,
        virtual_position_id=41,
        decision_type=decision_type,
        trigger_price=int(details.get("virtual_exit_price") or 0),
        filled=True,
        fill_policy=FillPolicy.NORMAL,
        reason_codes=[f"{decision_type}_CONFIRMED"],
        details=details,
        created_at="2026-05-30T09:15:00",
    )


def _sink(tmp_path):
    settings = _settings(tmp_path)
    gateway_state = GatewayStateStore()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)
    return DryRunRuntimeOrderSink(settings=settings, service=service), gateway_state, settings


def test_exit_sell_intent_is_saved_without_gateway_command(tmp_path):
    sink, gateway_state, settings = _sink(tmp_path)

    result = sink.on_exit_order_decision(
        candidate=_candidate(),
        virtual_position=_position(),
        exit_decision=_decision(),
        runtime_cycle_at="2026-05-30T09:16:00",
    )

    assert result["accepted"] is True
    assert result["command"] is None
    assert result["request"]["side"] == "sell"
    assert result["request"]["order_phase"] == "exit"
    assert result["request"]["quantity"] == 7
    assert gateway_state.command_snapshot()["queued_count"] == 0

    db = TradingDatabase(str(settings.db_path))
    try:
        row = db.get_runtime_order_intent(result["intent_id"])
        summary = db.runtime_order_intent_summary()
    finally:
        db.close()
    assert row["side"] == "sell"
    assert row["order_phase"] == "exit"
    assert row["exit_decision_id"] == 51
    assert row["exit_decision_type"] == TAKE_PROFIT
    assert row["exit_quantity"] == 7
    assert summary["sell_total"] == 1
    assert summary["exit_by_decision_type"][0]["decision_type"] == TAKE_PROFIT


def test_full_exit_decision_types_sell_full_quantity(tmp_path):
    for decision_type in [SUPPORT_LOSS, TIME_EXIT, TRAILING_STOP, THEME_WEAK_EXIT]:
        sink, _, _ = _sink(tmp_path / decision_type)
        result = sink.on_exit_order_decision(
            candidate=_candidate(),
            virtual_position=_position(quantity=12),
            exit_decision=_decision(decision_type, exit_percent=100),
            runtime_cycle_at="2026-05-30T09:16:00",
        )
        assert result["accepted"] is True
        assert result["request"]["quantity"] == 12
        assert result["request"]["exit_decision_type"] == decision_type


def test_context_risk_exit_sell_intent_is_saved_without_gateway_command(tmp_path):
    sink, gateway_state, settings = _sink(tmp_path)

    result = sink.on_exit_order_decision(
        candidate=_candidate(),
        virtual_position=_position(quantity=5, closed_at="2026-05-30T09:15:00", close_reason=THEME_WEAK_EXIT),
        exit_decision=_decision(
            THEME_WEAK_EXIT,
            exit_percent=100,
            theme_status_before="LEADING_THEME",
            theme_status_after="WEAK_THEME",
            index_market="KOSPI",
            risk_reason_codes=["THEME_WEAK"],
        ),
        runtime_cycle_at="2026-05-30T09:16:00",
    )

    assert result["accepted"] is True
    assert result["command"] is None
    assert result["request"]["side"] == "sell"
    assert result["request"]["quantity"] == 5
    assert result["request"]["exit_decision_type"] == THEME_WEAK_EXIT
    assert gateway_state.command_snapshot()["queued_count"] == 0
    db = TradingDatabase(str(settings.db_path))
    try:
        row = db.get_runtime_order_intent(result["intent_id"])
    finally:
        db.close()
    assert row["exit_decision_type"] == THEME_WEAK_EXIT


def test_exit_sell_intent_invalid_price_and_quantity_are_recorded(tmp_path):
    sink, _, settings = _sink(tmp_path)

    bad_price = sink.on_exit_order_decision(
        candidate=_candidate(),
        virtual_position=_position(),
        exit_decision=_decision(virtual_exit_price=0),
        runtime_cycle_at="2026-05-30T09:16:00",
    )
    zero_qty = sink.on_exit_order_decision(
        candidate=_candidate(),
        virtual_position=_position(id=42, quantity=0),
        exit_decision=_decision(decision_id=52),
        runtime_cycle_at="2026-05-30T09:17:00",
    )

    assert bad_price["accepted"] is False
    assert bad_price["reason"] == "PRICE_INVALID"
    assert zero_qty["accepted"] is False
    assert zero_qty["reason"] == "QUANTITY_ZERO"
    db = TradingDatabase(str(settings.db_path))
    try:
        rows = db.list_runtime_order_intents(side="sell", order_phase="exit", limit=10)
    finally:
        db.close()
    assert {row["status"] for row in rows} == {"DRY_RUN_REJECTED"}


def test_same_exit_decision_is_deduped_after_restart(tmp_path):
    sink, _, settings = _sink(tmp_path)
    kwargs = {
        "candidate": _candidate(),
        "virtual_position": _position(),
        "exit_decision": _decision(),
        "runtime_cycle_at": "2026-05-30T09:16:00",
    }

    first = sink.on_exit_order_decision(**kwargs)

    gateway_state = GatewayStateStore()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)
    second_sink = DryRunRuntimeOrderSink(settings=settings, service=service)
    second = second_sink.on_exit_order_decision(**kwargs)

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["status"] == "DUPLICATE"
    assert second["duplicate_of"] == first["intent_id"]


def test_exit_intent_detail_links_virtual_position_and_exit_decision(tmp_path):
    sink, _, settings = _sink(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        saved_position = db.save_virtual_position(_position(id=None))
        saved_decision = db.save_exit_decision(_decision(decision_id=None))
    finally:
        db.close()

    result = sink.on_exit_order_decision(
        candidate=_candidate(),
        virtual_position=saved_position,
        exit_decision=saved_decision,
        runtime_cycle_at="2026-05-30T09:16:00",
    )

    detail = sink.service.get_dry_run_order(result["intent_id"])
    assert detail["linked"]["virtual_position"]["id"] == saved_position.id
    assert detail["linked"]["exit_decision"]["id"] == saved_decision.id
