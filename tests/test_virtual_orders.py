from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import TickSizeProvider
from trading.strategy.models import EntryPlan, FillPolicy, VirtualOrderStatus
from trading.strategy.virtual_orders import VirtualOrderService
from trading.strategy.market_data import StrategyTick


class FixedTickProvider(TickSizeProvider):
    def tick_size(self, price: int) -> int:
        return 5


def plan(
    id=10,
    candidate_id=1,
    theme_id="robot",
    entry_type="pullback_limit",
    limit_price=10_000,
    fill_policy=FillPolicy.NORMAL,
    submittable=True,
    timeout=120,
):
    return EntryPlan(
        id=id,
        candidate_id=candidate_id,
        entry_type=entry_type,
        base_price_source="vwap",
        limit_price=limit_price,
        tick_offset=1,
        max_chase_pct=0.7,
        split_plan=[{"leg": 1, "weight_pct": 100, "limit_price": limit_price}],
        order_timeout_sec=timeout,
        cancel_condition={
            "submittable": submittable,
            "theme_id": theme_id,
            "code": "111111",
            "order_kind": "virtual",
        },
        fill_policy=fill_policy,
        created_at="2026-05-29T09:00:00",
    )


def builder_with_completed_candle(code="111111", start=None, low=9_990, close=None, final_volume=1_100):
    start = start or datetime(2026, 5, 29, 9, 1)
    close = low if close is None else close
    builder = CandleBuilder()
    builder.update(StrategyTick.from_realtime(code, 10_100, cum_volume=1_000, timestamp=start + timedelta(seconds=1)))
    builder.update(StrategyTick.from_realtime(code, low, cum_volume=min(1_100, final_volume), timestamp=start + timedelta(seconds=20)))
    builder.update(StrategyTick.from_realtime(code, close, cum_volume=final_volume, timestamp=start + timedelta(seconds=45)))
    builder.flush(code, start + timedelta(minutes=1))
    return builder


def test_not_submittable_plan_is_rejected_without_virtual_order():
    result = VirtualOrderService().submit_virtual_order(plan(submittable=False))

    assert result.order is None
    assert result.submitted is False
    assert result.rejected_reason == "not_submittable"
    assert result.details["primary_reason_code"] == "not_submittable"
    assert result.details["comparison_mode"] == "legacy_only"


def test_duplicate_submitted_virtual_order_is_not_created():
    service = VirtualOrderService()
    entry_plan = plan()

    first = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0))
    second = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 1))

    assert first.submitted is True
    assert second.duplicate is True
    assert second.order is first.order
    assert second.rejected_reason == "duplicate_submitted"


def test_multi_leg_plan_submits_next_leg_only_after_previous_fill():
    service = VirtualOrderService()
    entry_plan = plan()
    entry_plan.split_plan = [
        {"leg": 1, "weight_pct": 40, "limit_price": 10_000, "submittable": True},
        {"leg": 2, "weight_pct": 30, "limit_price": 9_800, "submittable": True, "requires_previous_leg": True},
        {"leg": 3, "weight_pct": 30, "limit_price": 9_600, "submittable": True, "requires_previous_leg": True},
    ]

    first = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0))
    duplicate = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 1))
    first.order.status = VirtualOrderStatus.FILLED
    second = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 2))

    assert first.order.leg_index == 1
    assert first.order.weight_pct == 40
    assert duplicate.duplicate is True
    assert second.submitted is True
    assert second.order.leg_index == 2
    assert second.order.limit_price == 9_800


def test_fill_policies_use_tick_provider_thresholds():
    builder = builder_with_completed_candle(low=9_995)
    service = VirtualOrderService(tick_provider=FixedTickProvider())

    optimistic = service.submit_virtual_order(plan(fill_policy=FillPolicy.OPTIMISTIC), datetime(2026, 5, 29, 9, 0)).order
    normal = service.submit_virtual_order(plan(candidate_id=2, fill_policy=FillPolicy.NORMAL), datetime(2026, 5, 29, 9, 0)).order
    conservative = service.submit_virtual_order(plan(candidate_id=3, fill_policy=FillPolicy.CONSERVATIVE), datetime(2026, 5, 29, 9, 0)).order

    assert service.evaluate_fill(optimistic, plan(fill_policy=FillPolicy.OPTIMISTIC), builder, datetime(2026, 5, 29, 9, 1)).filled
    assert service.evaluate_fill(normal, plan(candidate_id=2, fill_policy=FillPolicy.NORMAL), builder, datetime(2026, 5, 29, 9, 1)).filled
    conservative_result = service.evaluate_fill(
        conservative,
        plan(candidate_id=3, fill_policy=FillPolicy.CONSERVATIVE),
        builder,
        datetime(2026, 5, 29, 9, 1),
    )
    assert conservative_result.filled is False
    assert conservative.status == VirtualOrderStatus.SUBMITTED
    assert conservative_result.details["fill_threshold"] == 9_990


def test_submitted_at_previous_candle_low_does_not_fill():
    builder = builder_with_completed_candle(start=datetime(2026, 5, 29, 9, 0), low=9_000)
    service = VirtualOrderService()
    entry_plan = plan(timeout=300)
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 1)).order

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2))

    assert result.filled is False
    assert order.status == VirtualOrderStatus.SUBMITTED


def test_active_candle_low_is_not_used_for_fill():
    builder = CandleBuilder()
    builder.update(StrategyTick.from_realtime("111111", 10_100, cum_volume=1_000, timestamp=datetime(2026, 5, 29, 9, 1, 1)))
    builder.update(StrategyTick.from_realtime("111111", 9_000, cum_volume=1_100, timestamp=datetime(2026, 5, 29, 9, 1, 20)))
    service = VirtualOrderService()
    entry_plan = plan(timeout=300)
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 1, 30))

    assert result.filled is False
    assert result.details["include_active_candle"] is False
    assert result.details["legacy_result"] is False
    assert result.details["new_result"] is False
    assert order.status == VirtualOrderStatus.SUBMITTED


def test_fill_diagnostics_high_confidence_keeps_legacy_fill_result():
    builder = builder_with_completed_candle(low=9_990, close=10_080, final_volume=2_000)
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order
    latest_tick = StrategyTick.from_realtime(
        "111111",
        10_080,
        cum_volume=2_000,
        spread_ticks=1,
        trade_value=5_000_000,
        execution_strength=130,
        timestamp=datetime(2026, 5, 29, 9, 1, 50),
    )

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2), latest_tick=latest_tick)
    diagnostics = result.details["fill_diagnostics_v2"]

    assert result.filled is True
    assert order.status == VirtualOrderStatus.FILLED
    assert diagnostics["legacy_fill_result"] is True
    assert diagnostics["v2_would_fill"] is True
    assert diagnostics["fill_confidence_level"] == "high"
    assert diagnostics["spread_risk"] is False
    assert result.details["legacy_result"] is True
    assert result.details["new_result"] is True


def test_fill_diagnostics_low_liquidity_records_v2_non_fill_without_changing_legacy_fill():
    builder = builder_with_completed_candle(low=9_990, close=10_080, final_volume=1_020)
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order
    latest_tick = StrategyTick.from_realtime(
        "111111",
        10_080,
        spread_ticks=1,
        execution_strength=130,
        timestamp=datetime(2026, 5, 29, 9, 1, 50),
    )

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2), latest_tick=latest_tick)
    diagnostics = result.details["fill_diagnostics_v2"]

    assert result.filled is True
    assert order.status == VirtualOrderStatus.FILLED
    assert diagnostics["fill_confidence_level"] == "low"
    assert diagnostics["v2_would_fill"] is False
    assert diagnostics["liquidity_risk"] is True
    assert "FILL_LIQUIDITY_WEAK" in diagnostics["v2_non_fill_reason_codes"]
    assert "FILL_LIQUIDITY_WEAK" in result.details["comparison_reason_codes"]
    assert result.details["legacy_result"] is True
    assert result.details["new_result"] is False


def test_fill_diagnostics_records_spread_risk():
    builder = builder_with_completed_candle(low=9_990, close=10_080, final_volume=2_000)
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order
    latest_tick = StrategyTick.from_realtime(
        "111111",
        10_080,
        spread_ticks=5,
        trade_value=5_000_000,
        execution_strength=130,
        timestamp=datetime(2026, 5, 29, 9, 1, 50),
    )

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2), latest_tick=latest_tick)
    diagnostics = result.details["fill_diagnostics_v2"]

    assert diagnostics["spread_ticks"] == 5
    assert diagnostics["spread_risk"] is True
    assert "SPREAD_TOO_WIDE" in diagnostics["v2_non_fill_reason_codes"]
    assert "SPREAD_TOO_WIDE" in result.details["comparison_reason_codes"]


def test_fill_diagnostics_missing_execution_strength_records_input_insufficient():
    builder = builder_with_completed_candle(low=9_990, close=10_080, final_volume=2_000)
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order
    latest_tick = StrategyTick.from_realtime(
        "111111",
        10_080,
        spread_ticks=1,
        trade_value=5_000_000,
        timestamp=datetime(2026, 5, 29, 9, 1, 50),
    )

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2), latest_tick=latest_tick)
    diagnostics = result.details["fill_diagnostics_v2"]

    assert "execution_strength_missing" in diagnostics["input_missing_fields"]
    assert "FILL_INPUT_INSUFFICIENT" in diagnostics["v2_non_fill_reason_codes"]
    assert "INPUT_MISSING" in diagnostics["v2_non_fill_reason_codes"]


def test_v2_apply_default_disabled_so_low_confidence_does_not_change_virtual_fill():
    builder = builder_with_completed_candle(low=9_990, close=10_080, final_volume=1_020)
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order

    result = service.evaluate_fill(order, entry_plan, builder, datetime(2026, 5, 29, 9, 2))

    assert service.v2_apply is False
    assert result.filled is True
    assert order.status == VirtualOrderStatus.FILLED
    assert result.details["fill_diagnostics_v2"]["v2_would_fill"] is False


def test_timeout_unfilled_order_does_not_fill_later():
    service = VirtualOrderService()
    entry_plan = plan(timeout=60)
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order
    empty_builder = CandleBuilder()

    timeout_result = service.evaluate_fill(order, entry_plan, empty_builder, datetime(2026, 5, 29, 9, 2))

    assert timeout_result.timed_out is True
    assert order.status == VirtualOrderStatus.UNFILLED
    assert order.unfilled_reason == "TIMEOUT"

    fill_builder = builder_with_completed_candle(start=datetime(2026, 5, 29, 9, 3), low=9_000)
    later = service.evaluate_fill(order, entry_plan, fill_builder, datetime(2026, 5, 29, 9, 4))

    assert later.filled is False
    assert order.status == VirtualOrderStatus.UNFILLED


def test_cancelled_order_does_not_fill():
    service = VirtualOrderService()
    entry_plan = plan()
    order = service.submit_virtual_order(entry_plan, datetime(2026, 5, 29, 9, 0)).order

    cancel_result = service.cancel_virtual_order(order, "MANUAL_CANCEL", datetime(2026, 5, 29, 9, 1))
    fill_result = service.evaluate_fill(
        order,
        entry_plan,
        builder_with_completed_candle(low=9_000),
        datetime(2026, 5, 29, 9, 2),
    )

    assert cancel_result.cancelled is True
    assert order.status == VirtualOrderStatus.CANCELLED
    assert fill_result.filled is False
    assert order.unfilled_reason == "MANUAL_CANCEL"


def test_entry_plan_and_virtual_order_db_round_trip_and_duplicate_lookup(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    saved_plan = db.save_entry_plan(plan(id=None, candidate_id=7, theme_id="robot"))
    service = VirtualOrderService(db=db)
    submitted = service.submit_virtual_order(saved_plan, datetime(2026, 5, 29, 9, 0))
    saved_order = db.save_virtual_order(submitted.order)

    loaded_plans = db.list_entry_plans(7)
    loaded_orders = db.list_virtual_orders(7)
    duplicate = VirtualOrderService(db=db).submit_virtual_order(saved_plan, datetime(2026, 5, 29, 9, 1))

    assert loaded_plans[0].cancel_condition["theme_id"] == "robot"
    assert loaded_orders[0].status == VirtualOrderStatus.SUBMITTED
    assert duplicate.duplicate is True
    assert duplicate.order.id == saved_order.id
    assert db.conn.execute("SELECT COUNT(*) AS count FROM virtual_positions").fetchone()["count"] == 0
    db.close()
