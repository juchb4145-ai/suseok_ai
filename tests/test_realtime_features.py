from datetime import datetime, timedelta

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import StrategyTick
from trading.strategy.realtime_features import RealtimeFeatureCalculator


def test_realtime_feature_warmup_returns_zero_momentum_and_reason_code():
    calculator = RealtimeFeatureCalculator()

    result = calculator.enrich(
        code="005930",
        price=70000,
        cum_volume=1000,
        trade_value=0,
        timestamp=datetime(2026, 5, 29, 9, 0, 1),
        candle_builder=CandleBuilder(),
        metadata={},
    )

    assert result.trade_value == 70_000_000
    assert result.metadata["momentum_1m"] == 0.0
    assert result.metadata["momentum_3m"] == 0.0
    assert result.metadata["momentum_5m"] == 0.0
    assert result.metadata["turnover_strength"] == 1.0
    assert "MOMENTUM_WARMUP" in result.metadata["reason_codes"]
    assert "TURNOVER_ESTIMATED" in result.metadata["reason_codes"]


def test_realtime_feature_uses_completed_candles_for_momentum():
    builder = CandleBuilder()
    start = datetime(2026, 5, 29, 9, 0, 1)
    builder.update(StrategyTick.from_realtime("005930", 1000, cum_volume=100, timestamp=start))
    builder.update(StrategyTick.from_realtime("005930", 1050, cum_volume=120, timestamp=start + timedelta(seconds=20)))
    builder.update(StrategyTick.from_realtime("005930", 1060, cum_volume=140, timestamp=start + timedelta(minutes=1)))

    result = RealtimeFeatureCalculator().enrich(
        code="005930",
        price=1060,
        cum_volume=140,
        trade_value=148400,
        timestamp=start + timedelta(minutes=1),
        candle_builder=builder,
        metadata={},
    )

    assert result.metadata["momentum_1m"] == 5.0
    assert result.metadata["momentum_3m"] == 0.0
    assert result.metadata["momentum_5m"] == 0.0
    assert "MOMENTUM_WARMUP" in result.metadata["reason_codes"]


def test_realtime_feature_adds_vwap_recent_candles_and_support():
    builder = CandleBuilder()
    start = datetime(2026, 5, 29, 9, 0, 1)
    builder.update(StrategyTick.from_realtime("005930", 1000, cum_volume=100, timestamp=start))
    builder.update(StrategyTick.from_realtime("005930", 1050, cum_volume=120, timestamp=start + timedelta(seconds=20)))
    builder.update(StrategyTick.from_realtime("005930", 1040, cum_volume=140, timestamp=start + timedelta(seconds=40)))
    builder.update(StrategyTick.from_realtime("005930", 1060, cum_volume=160, timestamp=start + timedelta(minutes=1)))

    result = RealtimeFeatureCalculator(support_window=1).enrich(
        code="005930",
        price=1060,
        cum_volume=160,
        trade_value=169600,
        timestamp=start + timedelta(minutes=1, seconds=5),
        candle_builder=builder,
        metadata={},
    )

    assert result.metadata["vwap"] == 1060.0
    assert result.metadata["vwap_ready"] is True
    assert result.metadata["recent_candles_1m"] == [
        {
            "start_at": "2026-05-29T09:00:00",
            "open": 1000,
            "high": 1050,
            "low": 1000,
            "close": 1040,
            "volume": 40,
        }
    ]
    assert result.metadata["recent_support_price"] == 1000.0
    assert result.metadata["recent_support_ready"] is True
    assert result.metadata["completed_minute_bar_count"] == 1


def test_realtime_feature_does_not_mark_vwap_ready_for_estimated_turnover():
    result = RealtimeFeatureCalculator().enrich(
        code="005930",
        price=70000,
        cum_volume=1000,
        trade_value=0,
        timestamp=datetime(2026, 5, 29, 9, 0, 1),
        candle_builder=CandleBuilder(),
        metadata={},
    )

    assert "vwap" not in result.metadata
    assert "vwap_ready" not in result.metadata
    assert "TURNOVER_ESTIMATED" in result.metadata["reason_codes"]
