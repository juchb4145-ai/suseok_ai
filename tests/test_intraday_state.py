from datetime import datetime

from trading.strategy.candles import Candle
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import IndicatorSnapshot


def candle(minute, low=10_000, close=10_000, volume=100):
    return Candle(
        code="005930",
        interval_min=1,
        start_at=datetime(2026, 5, 29, 9, minute),
        open=10_000,
        high=max(10_200, close),
        low=low,
        close=close,
        volume=volume,
    )


def latest(price=10_000):
    return StrategyTick.from_realtime("005930", price, timestamp=datetime(2026, 5, 29, 9, 5))


def snapshot(price=10_000, day_high=10_500, pullback_pct=-4.76):
    return IndicatorSnapshot(
        candidate_id=1,
        code="005930",
        price=price,
        day_high=day_high,
        day_low=9_800,
        pullback_pct=pullback_pct,
        metadata={},
    )


def test_volume_reaccel_uses_ratio_threshold():
    tracker = IntradayStateTracker()
    candles = [candle(0, volume=100), candle(1, volume=100), candle(2, volume=119)]

    result = tracker.apply(snapshot(), candles, latest())

    assert result.volume_reaccel is False

    candles[-1] = candle(2, volume=120)
    result = tracker.apply(snapshot(), candles, latest())

    assert result.volume_reaccel is True
    assert result.metadata["volume_reaccel_ready"] is True


def test_insufficient_volume_history_sets_ready_false():
    tracker = IntradayStateTracker()

    result = tracker.apply(snapshot(), [candle(0), candle(1)], latest())

    assert result.volume_reaccel is False
    assert result.metadata["volume_reaccel_ready"] is False
    assert "volume_reaccel_history_short" in result.metadata["insufficient_reason"]


def test_volume_deceleration_uses_recent_volume_drop():
    tracker = IntradayStateTracker()
    candles = [candle(0, volume=200), candle(1, volume=180), candle(2, volume=120)]

    result = tracker.apply(snapshot(), candles, latest())

    assert result.metadata["volume_deceleration_ready"] is True
    assert result.metadata["volume_deceleration"] is True
    assert result.metadata["volume_deceleration_ratio"] < 0.8


def test_large_candle_state_marks_recent_three_or_five_minute_extension():
    tracker = IntradayStateTracker()
    candles = [
        candle(0, close=10_000, volume=100),
        candle(1, close=10_100, volume=120),
        candle(2, close=10_200, volume=130),
        candle(3, close=10_300, volume=140),
        candle(4, close=10_400, volume=150),
    ]

    result = tracker.apply(snapshot(price=10_400, day_high=10_400), candles, latest(10_400))

    assert result.metadata["after_large_5m_candle"] is True
    assert result.metadata["large_candle_body_pct"] >= 2.0
    assert result.metadata["candle_close_position"] >= 0.75


def test_failed_low_break_rebound_requires_break_and_recovery():
    tracker = IntradayStateTracker()
    candles = [
        candle(0, low=10_000, close=10_100),
        candle(1, low=9_980, close=10_050),
        candle(2, low=10_020, close=10_050),
        candle(3, low=9_970, close=9_990),
    ]

    result = tracker.apply(snapshot(), candles, latest(9_990))

    assert result.failed_low_break_rebound is True
    assert result.metadata["failed_low_break_broke_low"] is True
    assert result.metadata["failed_low_break_recovered"] is True


def test_low_break_without_recovery_is_not_rebound():
    tracker = IntradayStateTracker()
    candles = [
        candle(0, low=10_000, close=10_100),
        candle(1, low=9_980, close=10_050),
        candle(2, low=10_020, close=10_050),
        candle(3, low=9_970, close=9_970),
    ]

    result = tracker.apply(snapshot(), candles, latest(9_970))

    assert result.failed_low_break_rebound is False
    assert result.metadata["failed_low_break_broke_low"] is True
    assert result.metadata["failed_low_break_recovered"] is False


def test_chase_risk_threshold():
    tracker = IntradayStateTracker()

    risky = tracker.apply(snapshot(price=10_470, day_high=10_500, pullback_pct=-0.285), [candle(0), candle(1), candle(2)], latest(10_470))
    pulled_back = tracker.apply(snapshot(price=10_300, day_high=10_500, pullback_pct=-1.90), [candle(0), candle(1), candle(2)], latest(10_300))

    assert risky.chase_risk is True
    assert pulled_back.chase_risk is False


def test_insufficient_history_keeps_pullback_phase_unknown():
    tracker = IntradayStateTracker()

    result = tracker.apply(snapshot(), [candle(0)], latest())

    assert result.metadata["pullback_phase"] == "unknown"
    assert result.metadata["failed_low_break_rebound_ready"] is False
