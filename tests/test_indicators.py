from datetime import datetime

from trading.strategy.candles import CandleBuilder
from trading.strategy.indicators import IndicatorCalculator, PreviousDayLevelProvider
from trading.strategy.market_data import MarketDataStore, StrategyTick


def tick(code, price, cum_volume, at):
    return StrategyTick.from_realtime(code=code, price=price, cum_volume=cum_volume, timestamp=at)


def feed(store, builder, value):
    assert store.update_tick(value)
    assert builder.update(value)


def test_vwap_uses_completed_and_active_without_duplication():
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    feed(store, builder, tick(code, 10_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    feed(store, builder, tick(code, 10_000, 1_100, datetime(2026, 5, 29, 9, 0, 20)))
    feed(store, builder, tick(code, 10_500, 1_100, datetime(2026, 5, 29, 9, 1, 1)))
    feed(store, builder, tick(code, 11_000, 1_150, datetime(2026, 5, 29, 9, 1, 20)))
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, code)

    assert snapshot.vwap == ((10_000 * 100) + (11_000 * 50)) / 150
    assert snapshot.metadata["vwap_ready"] is True
    assert snapshot.metadata["includes_active_candle"] is True


def test_vwap_excludes_zero_volume_candles():
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    feed(store, builder, tick(code, 10_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    feed(store, builder, tick(code, 10_500, 1_100, datetime(2026, 5, 29, 9, 1, 1)))
    feed(store, builder, tick(code, 11_000, 1_200, datetime(2026, 5, 29, 9, 1, 20)))
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, code)

    assert snapshot.vwap == 11_000


def test_no_volume_makes_vwap_not_ready():
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    feed(store, builder, tick(code, 10_000, 1_000, datetime(2026, 5, 29, 9, 0, 1)))
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, code)

    assert snapshot.vwap is None
    assert snapshot.metadata["vwap_ready"] is False
    assert "vwap_volume_missing" in snapshot.metadata["insufficient_reason"]


def test_pullback_pct_is_negative_below_day_high():
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    feed(store, builder, tick(code, 10_000, 1_000, datetime(2026, 5, 29, 9, 0)))
    feed(store, builder, tick(code, 9_500, 1_100, datetime(2026, 5, 29, 9, 1)))
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, code)

    assert snapshot.pullback_pct == -5.0
    assert snapshot.metadata["pullback_pct_basis"] == "negative_below_day_high"


def test_ema20_5m_less_than_twenty_candles_has_value_but_not_ready():
    store, builder = build_5m_history(5)
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, "005930")

    assert snapshot.ema20_5m is not None
    assert snapshot.metadata["ema20_5m_candle_count"] == 5
    assert snapshot.metadata["ema20_5m_ready"] is False


def test_ema20_5m_twenty_candles_is_ready():
    store, builder = build_5m_history(20)
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, "005930")

    assert snapshot.ema20_5m is not None
    assert snapshot.metadata["ema20_5m_candle_count"] == 20
    assert snapshot.metadata["ema20_5m_ready"] is True


def test_base_line_120_and_envelope_mid_use_completed_5m_sma():
    store, builder = build_5m_history(120)
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, "005930")

    completed = builder.completed_candles("005930", 5)
    assert snapshot.base_line_120 == sum(candle.close for candle in completed[-120:]) / 120
    assert snapshot.envelope_mid == sum(candle.close for candle in completed[-20:]) / 20
    assert snapshot.metadata["base_line_120_candle_count"] == 120
    assert snapshot.metadata["base_line_120_ready"] is True
    assert snapshot.metadata["envelope_mid_candle_count"] == 120
    assert snapshot.metadata["envelope_mid_ready"] is True
    assert snapshot.metadata["volatility_5m_ready"] is True


def test_base_line_120_short_history_has_value_but_not_ready_reason():
    store, builder = build_5m_history(80)
    calculator = IndicatorCalculator(store, builder)

    snapshot = calculator.build_snapshot(1, "005930")

    assert snapshot.base_line_120 is not None
    assert snapshot.metadata["base_line_120_candle_count"] == 80
    assert snapshot.metadata["base_line_120_ready"] is False
    assert "base_line_120_insufficient_candles" in snapshot.metadata["insufficient_reason"]


def test_build_snapshot_returns_none_without_latest_tick():
    calculator = IndicatorCalculator(MarketDataStore(), CandleBuilder())

    assert calculator.build_snapshot(1, "005930") is None


def test_partial_insufficient_data_populates_metadata():
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    feed(store, builder, tick(code, 10_000, 1_000, datetime(2026, 5, 29, 9, 0)))
    provider = PreviousDayLevelProvider()
    calculator = IndicatorCalculator(store, builder, provider)

    snapshot = calculator.build_snapshot(1, code)

    assert "prev_high_missing" in snapshot.metadata["insufficient_reason"]
    assert "prev_low_missing" in snapshot.metadata["insufficient_reason"]
    assert "ema20_5m_missing" in snapshot.metadata["insufficient_reason"]


def build_5m_history(count):
    store = MarketDataStore()
    builder = CandleBuilder()
    code = "005930"
    cum_volume = 1_000
    for minute in range(count * 5):
        at = datetime(2026, 5, 29, 9 + (minute // 60), minute % 60, 1)
        price = 10_000 + minute * 10
        feed(store, builder, tick(code, price, cum_volume, at))
        cum_volume += 100
        feed(store, builder, tick(code, price, cum_volume, at.replace(second=20)))
        builder.flush(code, at.replace(minute=(minute % 60) + 1 if minute % 60 < 59 else 0, hour=9 + ((minute + 1) // 60), second=0))
    return store, builder
