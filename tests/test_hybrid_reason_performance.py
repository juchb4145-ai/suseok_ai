from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import build_validation_summary, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_reason_performance_aggregates_core_reason_codes():
    events = [
        label_event_outcome(event(status="WAIT", reason_codes=["LOW_BREADTH"], breadth=0.2), [_candle(1, 100.5, 97.5)]),
        label_event_outcome(event(status="BLOCKED", reason_codes=["LEADER_ONLY_THEME_LAGGARD_BLOCK"]), [_candle(1, 100.5, 97)]),
        label_event_outcome(event(status="BLOCKED", reason_codes=["LATE_LAGGARD"]), [_candle(1, 104, 99)]),
    ]

    summary = build_validation_summary(events)
    rows = {row.reason_code: row for row in summary.reason_performance}

    assert rows["LOW_BREADTH"].count == 1
    assert "유지" in rows["LOW_BREADTH"].recommendation
    assert rows["LEADER_ONLY_THEME_LAGGARD_BLOCK"].sample_stocks == ["000001"]
    assert rows["LATE_LAGGARD"].false_block_rate == 1.0


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
