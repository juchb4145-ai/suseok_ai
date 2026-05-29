from trading.rules import (
    calculate_order_quantity,
    calculate_take_profit_quantity,
    is_within_ticks,
    reached_stop_loss,
    reached_take_profit,
    tick_size,
    validate_weights,
)


def test_tick_size_korean_stock_ranges():
    assert tick_size(1_999) == 1
    assert tick_size(2_000) == 5
    assert tick_size(5_000) == 10
    assert tick_size(20_000) == 50
    assert tick_size(50_000) == 100
    assert tick_size(200_000) == 500
    assert tick_size(500_000) == 1_000


def test_is_within_ticks():
    assert is_within_ticks(50_100, 50_000, 1)
    assert not is_within_ticks(50_200, 50_000, 1)


def test_order_quantity_uses_budget_weight_and_target_price():
    assert calculate_order_quantity(1_000_000, 40.0, 50_000) == 8


def test_validate_weights_rejects_over_100_percent():
    assert validate_weights([40.0, 30.0, 30.0])[0]
    ok, message = validate_weights([50.0, 40.0, 20.0])
    assert not ok
    assert "100%" in message


def test_take_profit_quantity_has_minimum_one_share():
    assert calculate_take_profit_quantity(1, 70.0) == 1
    assert calculate_take_profit_quantity(10, 70.0) == 7


def test_profit_and_stop_loss_conditions():
    assert reached_take_profit(105_000, 100_000, 5.0)
    assert not reached_take_profit(104_900, 100_000, 5.0)
    assert reached_stop_loss(90_000, 90_000)
