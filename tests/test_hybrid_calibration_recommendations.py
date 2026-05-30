from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import (
    HybridValidationConfig,
    build_validation_summary,
    generate_calibration_recommendations,
    label_event_outcome,
)

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_calibration_recommendations_are_shadow_only_and_low_confidence_when_small_sample():
    config = HybridValidationConfig(calibration_min_sample_size=20, calibration_auto_apply=False)
    events = [
        label_event_outcome(event(status="WAIT", theme_score=70, membership_score=0.6), [_candle(1, 104, 99)]),
        label_event_outcome(
            event(status="OBSERVE", theme_status="WATCH", leader_type="leader", entry_score=80, rank_delta_5m=2, breadth=0.7),
            [_candle(1, 104, 99)],
        ),
    ]

    summary = build_validation_summary(events, config, trade_date="2026-05-30")
    recommendations = generate_calibration_recommendations(summary, config)

    assert recommendations["auto_apply"] is False
    assert recommendations["recommendations"]["hybrid_min_ready_score"]["recommended"] == 72
    assert recommendations["recommendations"]["hybrid_min_ready_score"]["low_sample_size"] is True
    assert recommendations["recommendations"]["watch_theme_allows_small_entry"]["recommended"] is True


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
