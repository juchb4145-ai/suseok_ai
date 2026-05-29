from datetime import datetime, timedelta

from trading.strategy.market_data import MarketDataStore, StrategyTick


def test_strategy_tick_normalizes_negative_prices_and_quotes():
    tick = StrategyTick.from_realtime(
        code="A005930",
        price="-80000",
        change_rate="+1.25%",
        cum_volume="-1200",
        best_ask="-80100",
        best_bid="-79900",
        timestamp=datetime(2026, 5, 29, 9, 0),
    )

    assert tick.code == "005930"
    assert tick.price == 80_000
    assert tick.change_rate == 1.25
    assert tick.cum_volume == 1_200
    assert tick.best_ask == 80_100
    assert tick.best_bid == 79_900


def test_market_data_store_handles_bad_volume_and_updates_state():
    store = MarketDataStore()
    first = StrategyTick.from_realtime("005930", 80_000, cum_volume=None, timestamp=datetime(2026, 5, 29, 9, 0))
    second = StrategyTick.from_realtime("005930", 81_000, cum_volume="bad", timestamp=datetime(2026, 5, 29, 9, 1))
    third = StrategyTick.from_realtime("005930", 79_500, cum_volume=-10, timestamp=datetime(2026, 5, 29, 9, 2))

    assert store.update_tick(first)
    assert store.update_tick(second)
    assert store.update_tick(third)

    assert store.latest_tick("005930") == third
    assert store.day_high_low("005930") == (81_000, 79_500)
    assert store.tick_count("005930") == 3
    assert store.has_recent_tick("005930", datetime(2026, 5, 29, 9, 2, 5), 10)
    assert not store.has_recent_tick("005930", datetime(2026, 5, 29, 9, 3), 10)


def test_market_data_store_ignores_out_of_order_tick():
    store = MarketDataStore()
    current = StrategyTick.from_realtime("005930", 80_000, timestamp=datetime(2026, 5, 29, 9, 1))
    old = StrategyTick.from_realtime("005930", 79_000, timestamp=datetime(2026, 5, 29, 9, 0, 59))

    assert store.update_tick(current)
    assert not store.update_tick(old)

    assert store.latest_tick("005930") == current
    assert store.day_high_low("005930") == (80_000, 80_000)
    assert store.tick_count("005930") == 1
