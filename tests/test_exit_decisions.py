from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.exit import (
    DATA_INSUFFICIENT_EXIT_BASIS,
    INDEX_WEAK_EXIT,
    LEADER_COLLAPSE_EXIT,
    MARKET_RISK_OFF_EXIT,
    SUPPORT_LOSS,
    TAKE_PROFIT,
    THEME_WEAK_EXIT,
    TIME_EXIT,
    TRAILING_STOP,
    ExitContextRiskSnapshot,
    ContextRiskExitConfirmationConfig,
    ExitDecisionEngine,
    VirtualPositionService,
)
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import (
    EntryPlan,
    ExitDecision,
    FillPolicy,
    IndicatorSnapshot,
    PositionContextSnapshot,
    StrategyProfile,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)


def entry_plan(candidate_id=1, profile=StrategyProfile.KOSDAQ_THEME_PROFILE):
    return EntryPlan(
        id=10,
        candidate_id=candidate_id,
        entry_type="pullback_limit",
        base_price_source="vwap",
        limit_price=10_000,
        split_plan=[{"leg": 1, "weight_pct": 100, "limit_price": 10_000}],
        order_timeout_sec=120,
        cancel_condition={
            "code": "111111",
            "strategy_profile": profile.value,
            "theme_id": "robot",
            "order_kind": "virtual",
        },
        fill_policy=FillPolicy.NORMAL,
        created_at="2026-05-29T09:00:00",
    )


def virtual_order(status=VirtualOrderStatus.FILLED, candidate_id=1):
    return VirtualOrder(
        id=20,
        candidate_id=candidate_id,
        entry_plan_id=10,
        status=status,
        limit_price=10_000,
        virtual_fill_price=10_000 if status == VirtualOrderStatus.FILLED else 0,
        fill_policy=FillPolicy.NORMAL,
        submitted_at="2026-05-29T09:00:00",
        filled_at="2026-05-29T09:00:30" if status == VirtualOrderStatus.FILLED else "",
    )


def position(opened_at="2026-05-29T09:00:30", profile=StrategyProfile.KOSDAQ_THEME_PROFILE):
    return VirtualPosition(
        id=1,
        candidate_id=1,
        virtual_order_id=20,
        entry_price=10_000,
        quantity=1,
        opened_at=opened_at,
        max_return_pct=0.0,
        max_drawdown_pct=0.0,
    )


def snapshot(profile=StrategyProfile.KOSDAQ_THEME_PROFILE, vwap=10_050, day_mid=10_020, ema20=10_000, ema_ready=True, price=9_900):
    return IndicatorSnapshot(
        candidate_id=1,
        code="111111",
        price=price,
        vwap=vwap,
        day_mid=day_mid,
        ema20_5m=ema20,
        metadata={"strategy_profile": profile.value, "ema20_5m_ready": ema_ready},
    )


def add_completed_candle(builder, start, open_price=10_000, high=10_100, low=9_900, close=10_000, code="111111"):
    builder.update(StrategyTick.from_realtime(code, open_price, cum_volume=1_000, timestamp=start + timedelta(seconds=1)))
    builder.update(StrategyTick.from_realtime(code, high, cum_volume=1_100, timestamp=start + timedelta(seconds=15)))
    builder.update(StrategyTick.from_realtime(code, low, cum_volume=1_200, timestamp=start + timedelta(seconds=30)))
    builder.update(StrategyTick.from_realtime(code, close, cum_volume=1_300, timestamp=start + timedelta(seconds=45)))
    builder.flush(code, start + timedelta(minutes=1))


def candle_builder(*candles):
    builder = CandleBuilder()
    for candle in candles:
        add_completed_candle(builder, *candle)
    return builder


def test_only_filled_virtual_order_opens_position_and_duplicates_are_rejected():
    service = VirtualPositionService()

    rejected = service.open_from_filled_order(virtual_order(VirtualOrderStatus.SUBMITTED), entry_plan())
    opened = service.open_from_filled_order(virtual_order(), entry_plan())
    duplicate = service.open_from_filled_order(virtual_order(), entry_plan())

    assert rejected.position is None
    assert rejected.rejected_reason == "virtual_order_not_filled"
    assert opened.opened is True
    assert opened.position.entry_price == 10_000
    assert duplicate.duplicate is True
    assert duplicate.position is opened.position


def test_filled_legs_aggregate_into_one_virtual_position(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    service = VirtualPositionService(db=db)
    plan_to_save = entry_plan()
    plan_to_save.id = None
    plan = db.save_entry_plan(plan_to_save)
    plan.split_plan = [
        {"leg": 1, "weight_pct": 40, "limit_price": 10_000},
        {"leg": 2, "weight_pct": 30, "limit_price": 9_700},
    ]
    first_order = virtual_order()
    first_order.id = None
    first_order.entry_plan_id = plan.id
    first_order.leg_index = 1
    first_order.weight_pct = 40
    first_order.limit_price = 10_000
    first_order.virtual_fill_price = 10_000
    first_order = db.save_virtual_order(first_order)
    second_order = virtual_order(candidate_id=1)
    second_order.id = None
    second_order.entry_plan_id = plan.id
    second_order.leg_index = 2
    second_order.weight_pct = 30
    second_order.limit_price = 9_700
    second_order.virtual_fill_price = 9_700
    second_order = db.save_virtual_order(second_order)

    opened = service.open_from_filled_order(first_order, plan)
    aggregated = service.open_from_filled_order(second_order, plan)

    assert opened.opened is True
    assert aggregated.aggregated is True
    assert len(db.list_virtual_positions(1)) == 1
    position = db.list_virtual_positions(1)[0]
    assert position.entry_price == 9_871
    assert position.details["filled_legs"] == [1, 2]
    assert position.details["filled_weight_pct"] == 70
    assert position.details["remaining_weight_pct"] == 30
    db.close()


def test_opened_at_previous_and_same_candle_are_excluded_from_mfe_mae():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 0), 10_000, 11_000, 9_500, 10_500),
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_200, 9_900, 10_100),
    )

    result = VirtualPositionService().update_performance(position(), builder, code="111111")

    assert result.changed is True
    assert result.details["same_candle_excluded"] is True
    assert result.position.max_return_pct == 2.0
    assert result.position.max_drawdown_pct == -1.0


def test_take_profit_uses_only_post_open_candles_and_does_not_close_position():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 0), 10_000, 11_000, 9_900, 10_900),
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_510, 10_000, 10_300),
    )
    pos = position()

    decisions = ExitDecisionEngine().evaluate(pos, snapshot(), builder, [], datetime(2026, 5, 29, 9, 2))

    assert [decision.decision_type for decision in decisions] == [TAKE_PROFIT]
    assert decisions[0].filled is True
    assert decisions[0].details["exit_percent"] == 70
    assert decisions[0].details["partial_exit"] is True
    assert decisions[0].details["position_closed"] is False
    assert pos.closed_at == ""


def test_pre_open_high_does_not_create_take_profit():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 0), 10_000, 11_000, 9_900, 10_900),
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_400, 10_000, 10_300),
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(), builder, [], datetime(2026, 5, 29, 9, 2))

    assert decisions == []


def test_context_risk_flag_off_preserves_existing_exit_result():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_900, 10_000))
    context = ExitContextRiskSnapshot(
        enabled=False,
        theme_status_after="WEAK_THEME",
        risk_reason_codes=("THEME_WEAK",),
        current_return_pct=-1.0,
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert decisions == []


def test_theme_weak_context_without_history_is_limited_not_forced_exit():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    pos = position()
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_id="robot",
        theme_name="robotics",
        theme_status_before="LEADING_THEME",
        theme_status_after="WEAK_THEME",
        theme_score=12.0,
        leader_symbol="222222",
        leader_return_pct=-1.0,
        index_market="KOSDAQ",
        index_return_pct=-0.4,
        breadth_status="LOW_BREADTH",
        current_return_pct=-1.0,
        risk_reason_codes=("THEME_WEAK",),
    )

    engine = ExitDecisionEngine()
    decisions = engine.evaluate(pos, snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert decisions == []
    assert engine.last_details["context_risk"]["reason_codes"][-1] == "DATA_LIMITED_CONTEXT"
    assert engine.last_details["context_risk"]["required_confirmation_cycles"] == 2
    assert engine.last_details["context_risk"]["observed_confirmation_cycles"] == 0
    assert engine.last_details["context_risk"]["confirmation_passed"] is False


def test_theme_weak_confirmed_context_risk_creates_full_exit_for_losing_position():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    pos = position()
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_id="robot",
        theme_name="robotics",
        theme_status_before="WEAK_THEME",
        theme_status_after="WEAK_THEME",
        theme_score=12.0,
        previous_theme_score=20.0,
        theme_score_delta=-8.0,
        theme_status_transition="WEAK_THEME->WEAK_THEME",
        leader_symbol="222222",
        leader_return_pct=-1.0,
        index_market="KOSDAQ",
        index_return_pct=-0.4,
        breadth_status="LOW_BREADTH",
        current_return_pct=-1.0,
        risk_reason_codes=("THEME_WEAK",),
        context_history_available=True,
        context_history_count=2,
        theme_weak_consecutive_count=2,
    )

    decisions = ExitDecisionEngine().evaluate(pos, snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [THEME_WEAK_EXIT]
    assert decisions[0].details["theme_status_before"] == "WEAK_THEME"
    assert decisions[0].details["theme_status_after"] == "WEAK_THEME"
    assert decisions[0].details["theme_score_before"] == 20.0
    assert decisions[0].details["theme_score_current"] == 12.0
    assert decisions[0].details["theme_score_delta"] == -8.0
    assert decisions[0].details["context_history_count"] == 2
    assert decisions[0].details["exit_confidence"] == "HIGH"
    assert decisions[0].details["required_confirmation_cycles"] == 2
    assert decisions[0].details["observed_confirmation_cycles"] == 2
    assert decisions[0].details["confirmation_passed"] is True
    assert decisions[0].details["position_closed"] is True
    assert pos.close_reason == THEME_WEAK_EXIT


def test_theme_weak_one_cycle_is_low_confidence_under_default_confirmation():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="WEAK_THEME",
        current_return_pct=-1.0,
        risk_reason_codes=("THEME_WEAK",),
        context_history_available=True,
        context_history_count=1,
        theme_weak_consecutive_count=1,
    )

    engine = ExitDecisionEngine()
    decisions = engine.evaluate(position(), snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert decisions == []
    assert engine.last_details["context_risk"]["context_limited_reason"] == "LOW_CONFIDENCE_EXIT"
    assert engine.last_details["context_risk"]["required_confirmation_cycles"] == 2
    assert engine.last_details["context_risk"]["observed_confirmation_cycles"] == 1


def test_theme_weak_config_one_cycle_creates_exit(monkeypatch):
    monkeypatch.setenv("TRADING_THEME_WEAK_CONFIRMATION_CYCLES", "1")
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="WEAK_THEME",
        current_return_pct=-1.0,
        risk_reason_codes=("THEME_WEAK",),
        context_history_available=True,
        context_history_count=1,
        theme_weak_consecutive_count=1,
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [THEME_WEAK_EXIT]
    assert decisions[0].details["required_confirmation_cycles"] == 1
    assert decisions[0].details["observed_confirmation_cycles"] == 1
    assert decisions[0].details["config_source"] == "env"


def test_invalid_confirmation_config_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRADING_THEME_WEAK_CONFIRMATION_CYCLES", "9")

    config = ContextRiskExitConfirmationConfig.from_env()

    assert config.theme_weak_confirmation_cycles == 2
    assert config.config_source == "env_invalid_fallback"
    assert config.fallback_reasons


def test_leader_collapse_context_risk_creates_exit():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="LEADING_THEME",
        leader_symbol="222222",
        leader_return_pct=-6.0,
        leader_vwap_broken=True,
        current_return_pct=-1.0,
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [LEADER_COLLAPSE_EXIT]
    assert "LEADER_COLLAPSE" in decisions[0].reason_codes


def test_kosdaq_risk_off_context_risk_creates_index_weak_exit():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="LEADING_THEME",
        index_market="KOSDAQ",
        index_status="RISK_OFF",
        index_return_pct=-2.8,
        current_return_pct=-1.0,
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [INDEX_WEAK_EXIT]
    assert decisions[0].details["index_market"] == "KOSDAQ"


def test_market_risk_off_priority_preserves_secondary_reasons():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_850, 9_900))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="WEAK_THEME",
        index_market="KOSDAQ",
        index_status="RISK_OFF",
        market_status="RISK_OFF",
        index_return_pct=-2.8,
        current_return_pct=-1.0,
        risk_reason_codes=("INDEX_WEAK", "THEME_WEAK", "MARKET_RISK_OFF"),
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(price=9_900), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [MARKET_RISK_OFF_EXIT]
    assert decisions[0].details["primary_exit_reason"] == MARKET_RISK_OFF_EXIT
    assert "INDEX_WEAK" in decisions[0].details["secondary_exit_reasons"]
    assert "THEME_WEAK" in decisions[0].details["secondary_exit_reasons"]
    assert decisions[0].details["required_confirmation_cycles"] == 0
    assert decisions[0].details["confirmation_passed"] is True


def test_position_context_history_persists_candidate_instance_id(tmp_path):
    db = TradingDatabase(str(tmp_path / "context.sqlite3"))
    try:
        saved = db.save_position_context_snapshot(
            PositionContextSnapshot(
                position_id=1,
                candidate_id=10,
                candidate_instance_id="ci-123",
                code="111111",
                trade_date="2026-05-29",
                captured_at="2026-05-29T09:00:30",
                capture_reason="ENTRY",
                theme_id="robot",
                theme_name="Robotics",
                theme_score=80.0,
                theme_status="LEADING_THEME",
                leader_count=2,
                strong_count=5,
                breadth_status="OK",
                leader_code="222222",
                leader_return_pct=4.2,
                leader_vwap_status="OK",
                index_market="KOSDAQ",
                index_status="",
                index_return_pct=0.2,
                market_status="EXPANSION",
                risk_reason_codes=["ENTRY"],
            )
        )
        history = db.list_position_context_history(1)
    finally:
        db.close()

    assert saved.id is not None
    assert len(history) == 1
    assert history[0].capture_reason == "ENTRY"
    assert history[0].candidate_instance_id == "ci-123"


def test_take_profit_and_theme_weak_same_candle_are_sequence_ambiguous():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_600, 9_900, 10_550))
    context = ExitContextRiskSnapshot(
        enabled=True,
        theme_status_after="WEAK_THEME",
        current_return_pct=5.5,
        stock_role="LATE_LAGGARD",
        risk_reason_codes=("THEME_WEAK",),
        context_history_available=True,
        context_history_count=2,
        theme_weak_consecutive_count=2,
    )

    decisions = ExitDecisionEngine().evaluate(position(), snapshot(price=10_550), builder, [], datetime(2026, 5, 29, 9, 2), context_risk=context)

    assert [decision.decision_type for decision in decisions] == [TAKE_PROFIT, THEME_WEAK_EXIT]
    assert all(decision.details["sequence_ambiguous"] is True for decision in decisions)


def test_support_loss_full_closes_position():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_800, 9_900),
        (datetime(2026, 5, 29, 9, 2), 9_900, 10_000, 9_700, 9_850),
    )
    pos = position()

    decisions = ExitDecisionEngine().evaluate(pos, snapshot(vwap=10_050), builder, [], datetime(2026, 5, 29, 9, 3))

    assert [decision.decision_type for decision in decisions] == [SUPPORT_LOSS]
    assert decisions[0].details["position_closed"] is True
    assert pos.closed_at == "2026-05-29T09:03:00"
    assert pos.close_price == 9_850
    assert pos.close_reason == SUPPORT_LOSS
    assert pos.realized_return_pct == -1.5


def test_partial_take_profit_switches_remaining_position_to_trailing_stop():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_400, 10_100, 10_200),
        (datetime(2026, 5, 29, 9, 2), 10_200, 10_300, 10_000, 10_000),
        (datetime(2026, 5, 29, 9, 3), 10_000, 10_050, 9_900, 9_950),
    )
    pos = position()
    existing = ExitDecision(
        virtual_position_id=1,
        decision_type=TAKE_PROFIT,
        trigger_price=10_500,
        filled=True,
        details={"partial_exit": True, "exit_percent": 70},
        created_at="2026-05-29T09:02:00",
    )

    decisions = ExitDecisionEngine().evaluate(
        pos,
        snapshot(vwap=10_050, day_mid=None, ema20=None, price=10_100),
        builder,
        [existing],
        datetime(2026, 5, 29, 9, 4),
    )

    assert [decision.decision_type for decision in decisions] == [TRAILING_STOP]
    assert decisions[0].reason_codes == ["TRAILING_STOP_CONFIRMED"]
    assert pos.close_reason == TRAILING_STOP
    assert pos.details["trailing_floor"] == 10_050


def test_time_exit_requires_max_hold_and_momentum_failure_then_full_closes():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_900, 10_000),
        (datetime(2026, 5, 29, 9, 2), 10_000, 10_050, 9_850, 9_900),
    )
    pos = position()

    decisions = ExitDecisionEngine().evaluate(pos, snapshot(vwap=None, day_mid=None, ema20=None), builder, [], datetime(2026, 5, 29, 9, 41))

    assert [decision.decision_type for decision in decisions] == [TIME_EXIT]
    assert decisions[0].details["min_expected_return_pct"] == 1.0
    assert decisions[0].details["position_closed"] is True
    assert pos.closed_at == "2026-05-29T09:41:00"
    assert pos.close_reason == TIME_EXIT


def test_support_loss_not_created_when_basis_missing_or_ema_not_ready():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 1), 10_000, 10_100, 9_800, 9_900),
        (datetime(2026, 5, 29, 9, 2), 9_900, 10_000, 9_700, 9_850),
    )
    engine = ExitDecisionEngine()

    decisions = engine.evaluate(
        position(),
        snapshot(vwap=None, day_mid=None, ema20=10_000, ema_ready=False),
        builder,
        [],
        datetime(2026, 5, 29, 9, 3),
    )

    assert decisions == []
    assert DATA_INSUFFICIENT_EXIT_BASIS in engine.last_details["reason_codes"]
    assert engine.last_details["support_basis"] == []


def test_duplicate_take_profit_and_closed_position_are_no_op():
    builder = candle_builder((datetime(2026, 5, 29, 9, 1), 10_000, 10_600, 10_000, 10_300))
    existing = ExitDecision(
        virtual_position_id=1,
        decision_type=TAKE_PROFIT,
        trigger_price=10_500,
        filled=True,
        created_at="2026-05-29T09:02:00",
    )
    engine = ExitDecisionEngine()

    decisions = engine.evaluate(position(), snapshot(), builder, [existing], datetime(2026, 5, 29, 9, 2))
    closed = position()
    closed.closed_at = "2026-05-29T09:03:00"
    closed_decisions = engine.evaluate(closed, snapshot(), builder, [], datetime(2026, 5, 29, 9, 4))

    assert decisions == []
    assert closed_decisions == []
    assert "position_already_closed" in engine.last_details["reason_codes"]


def test_same_candle_take_profit_and_support_loss_records_ambiguity():
    builder = candle_builder(
        (datetime(2026, 5, 29, 9, 0), 10_000, 10_100, 9_800, 9_900),
        (datetime(2026, 5, 29, 9, 1), 9_900, 10_600, 9_700, 9_850),
    )
    pos = position(opened_at="2026-05-29T08:59:30")

    decisions = ExitDecisionEngine().evaluate(pos, snapshot(vwap=10_050), builder, [], datetime(2026, 5, 29, 9, 2))

    assert [decision.decision_type for decision in decisions] == [TAKE_PROFIT, SUPPORT_LOSS]
    assert all(decision.details["sequence_ambiguous"] for decision in decisions)
    assert all(decision.details["same_candle_multiple_triggers"] for decision in decisions)
    assert pos.close_reason == SUPPORT_LOSS


def test_signal_only_without_position_creates_no_exit_decision():
    decisions = ExitDecisionEngine().evaluate(
        None,
        snapshot(profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE),
        CandleBuilder(),
        [],
        datetime(2026, 5, 29, 9, 2),
    )

    assert decisions == []


def test_virtual_position_and_exit_decision_db_round_trip(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    new_position = position()
    new_position.id = None
    saved_position = db.save_virtual_position(new_position)
    decision = ExitDecision(
        virtual_position_id=saved_position.id,
        decision_type=TAKE_PROFIT,
        trigger_price=10_500,
        filled=True,
        fill_policy=FillPolicy.NORMAL,
        reason_codes=["TAKE_PROFIT_TARGET_REACHED"],
        details={"partial_exit": True, "exit_percent": 70},
        created_at="2026-05-29T09:02:00",
    )
    saved_decision = db.save_exit_decision(decision)
    saved_position.closed_at = "2026-05-29T09:05:00"
    saved_position.close_price = 9_900
    saved_position.close_reason = SUPPORT_LOSS
    saved_position.realized_return_pct = -1.0
    close_decision = ExitDecision(
        virtual_position_id=saved_position.id,
        decision_type=SUPPORT_LOSS,
        trigger_price=9_900,
        filled=True,
        created_at="2026-05-29T09:05:00",
    )
    closed_position, closed_decision = db.close_virtual_position_with_decision(saved_position, close_decision)

    loaded_positions = db.list_virtual_positions(1)
    loaded_decisions = db.list_exit_decisions(saved_position.id)

    assert saved_decision.details["exit_percent"] == 70
    assert closed_position.closed_at == "2026-05-29T09:05:00"
    assert closed_decision.decision_type == SUPPORT_LOSS
    assert loaded_positions[0].close_reason == SUPPORT_LOSS
    assert [decision.decision_type for decision in loaded_decisions] == [TAKE_PROFIT, SUPPORT_LOSS]
    db.close()
