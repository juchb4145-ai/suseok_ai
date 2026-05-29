from datetime import datetime

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import StrategyTick


def tick(price, cum_volume, at):
    return StrategyTick.from_realtime(
        code="005930",
        price=price,
        cum_volume=cum_volume,
        timestamp=at,
    )


def test_first_tick_volume_is_zero_and_cum_volume_delta_is_used():
    builder = CandleBuilder()

    builder.update(tick(80_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    builder.update(tick(80_500, 1_120, datetime(2026, 5, 29, 9, 0, 20)))

    active = builder.active_candle("005930", 1)
    assert active.open == 80_000
    assert active.high == 80_500
    assert active.low == 80_000
    assert active.close == 80_500
    assert active.volume == 120


def test_cum_volume_reset_counts_as_zero_delta():
    builder = CandleBuilder()

    builder.update(tick(80_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    builder.update(tick(80_100, 900, datetime(2026, 5, 29, 9, 0, 5)))

    assert builder.active_candle("005930", 1).volume == 0


def test_minute_boundary_completes_one_minute_candle():
    builder = CandleBuilder()

    builder.update(tick(80_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    builder.update(tick(80_500, 1_100, datetime(2026, 5, 29, 9, 0, 59)))
    builder.update(tick(81_000, 1_150, datetime(2026, 5, 29, 9, 1, 0)))

    completed = builder.completed_candles("005930", 1)
    assert len(completed) == 1
    assert completed[0].start_at == datetime(2026, 5, 29, 9, 0)
    assert completed[0].open == 80_000
    assert completed[0].close == 80_500
    assert completed[0].volume == 100
    assert builder.active_candle("005930", 1).start_at == datetime(2026, 5, 29, 9, 1)


def test_three_and_five_minute_candles_are_built_from_completed_one_minute_candles():
    builder = CandleBuilder()

    for minute in range(6):
        builder.update(tick(80_000 + minute * 100, 1_000 + minute * 100, datetime(2026, 5, 29, 9, minute, 1)))
        builder.flush("005930", datetime(2026, 5, 29, 9, minute + 1, 0))

    three_minute = builder.completed_candles("005930", 3)
    five_minute = builder.completed_candles("005930", 5)

    assert [candle.start_at for candle in three_minute] == [
        datetime(2026, 5, 29, 9, 0),
        datetime(2026, 5, 29, 9, 3),
    ]
    assert three_minute[0].open == 80_000
    assert three_minute[0].close == 80_200
    assert [candle.start_at for candle in five_minute] == [datetime(2026, 5, 29, 9, 0)]
    assert five_minute[0].open == 80_000
    assert five_minute[0].close == 80_400


def test_flush_completes_active_candle_without_next_tick():
    builder = CandleBuilder()
    builder.update(tick(80_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))

    flushed = builder.flush("005930", datetime(2026, 5, 29, 9, 1, 0))

    assert flushed is not None
    assert builder.active_candle("005930", 1) is None
    assert builder.completed_candles("005930", 1)[0].start_at == datetime(2026, 5, 29, 9, 0)


def test_out_of_order_tick_does_not_mutate_candle():
    builder = CandleBuilder()
    builder.update(tick(80_000, 1_000, datetime(2026, 5, 29, 9, 0, 10)))
    active_before = builder.active_candle("005930", 1)

    assert not builder.update(tick(79_000, 1_100, datetime(2026, 5, 29, 9, 0, 9)))

    assert builder.active_candle("005930", 1) == active_before
