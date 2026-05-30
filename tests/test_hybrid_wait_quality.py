from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import build_validation_summary, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_wait_quality_detects_better_pullback_breakout_and_breakdown():
    events = [
        label_event_outcome(event(status="WAIT"), [_candle(1, 100, 98), _candle(2, 102, 99)]),
        label_event_outcome(event(status="WAIT"), [_candle(1, 104, 100)]),
        label_event_outcome(event(status="WAIT"), [_candle(1, 100.5, 97)]),
    ]

    wait_quality = build_validation_summary(events).wait_quality

    assert wait_quality["wait_then_better_pullback_count"] == 2
    assert wait_quality["wait_then_immediate_breakout_count"] == 1
    assert wait_quality["wait_then_breakdown_count"] == 1
    assert wait_quality["missed_wait_count"] == 1
    assert wait_quality["good_wait_count"] == 2


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
