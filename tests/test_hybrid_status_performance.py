from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import build_validation_summary, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_status_performance_groups_status_and_excludes_missing_from_average():
    events = [
        label_event_outcome(event(status="READY"), [_candle(1, 104, 99)]),
        label_event_outcome(event(status="WAIT"), [_candle(1, 104, 100)]),
        label_event_outcome(event(status="BLOCKED"), [_candle(1, 100, 97)]),
        label_event_outcome(event(status="OBSERVE", theme_status="WATCH"), []),
    ]

    summary = build_validation_summary(events)
    rows = {row["status"]: row for row in summary.status_performance}

    assert rows["READY"]["count"] == 1
    assert rows["READY"]["good_ready_count"] == 1
    assert rows["WAIT"]["missed_wait_count"] == 1
    assert rows["BLOCKED"]["good_block_count"] == 1
    assert rows["OBSERVE"]["insufficient_data_count"] == 1
    assert rows["OBSERVE"]["avg_max_return_25m"] is None


def _candle(offset_min: int, high: float, low: float):
    return Candle(
        code="000001",
        interval_min=1,
        start_at=START + timedelta(minutes=offset_min),
        open=100,
        high=int(high),
        low=int(low),
        close=int(high),
    )
