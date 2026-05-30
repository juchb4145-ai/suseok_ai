from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import build_validation_summary, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_theme_score_and_membership_bands_extract_cases():
    events = [
        label_event_outcome(event(status="READY", theme_score=88, membership_score=0.9), [_candle(1, 100.5, 97)]),
        label_event_outcome(event(status="WAIT", theme_score=70, membership_score=0.6), [_candle(1, 104, 99)]),
        label_event_outcome(event(status="BLOCKED", theme_score=60, membership_score=0.5, source_count=1), [_candle(1, 104, 99)]),
    ]

    summary = build_validation_summary(events)
    theme_rows = {row.band: row for row in summary.theme_score_bands}
    membership_rows = {row.band: row for row in summary.membership_score_bands}

    assert theme_rows["85_100"].count == 1
    assert theme_rows["65_75"].count == 1
    assert membership_rows["0_55_0_65"].count == 1
    assert summary.high_score_failure_cases[0]["stock_code"] == "000001"
    assert summary.threshold_relaxation_candidates[0]["stock_code"] == "000001"
    assert summary.new_theme_membership_relaxation_candidates[0]["stock_code"] == "000001"


def _candle(offset_min: int, high: float, low: float):
    return Candle("000001", 1, START + timedelta(minutes=offset_min), 100, int(high), int(low), int(high))
