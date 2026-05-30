from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import build_validation_summary, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_watch_policy_shadow_simulates_a_b_c():
    watch_leader = event(
        status="OBSERVE",
        theme_status="WATCH",
        leader_type="leader",
        entry_score=85,
        rank_delta_5m=2,
        breadth=0.7,
    )
    watch_laggard = event(status="OBSERVE", theme_status="WATCH", leader_type="late_laggard", entry_score=40)
    events = [
        label_event_outcome(watch_leader, [_candle(1, 104, 99)]),
        label_event_outcome(watch_laggard, [_candle(1, 100.5, 97)]),
    ]

    summary = build_validation_summary(events)
    rows = {row.policy: row for row in summary.watch_policy_performance}

    assert rows["Policy A"].candidate_count == 2
    assert rows["Policy B"].candidate_count == 1
    assert rows["Policy C"].candidate_count == 1
    assert rows["Policy B"].win_rate_25m == 1.0


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
