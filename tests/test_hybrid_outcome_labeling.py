from datetime import datetime, timedelta

from trading.strategy.candles import Candle
from trading.strategy.hybrid_validation import HybridOutcomeLabel, label_event_outcome

from tests.test_hybrid_validation_helpers import event


START = datetime(2026, 5, 30, 9, 0)


def test_ready_good_return_is_good_ready():
    labeled = label_event_outcome(event(status="READY"), [_candle(1, 100, 104, 99, 103)])

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.GOOD_READY.value


def test_ready_large_mae_is_bad_ready():
    labeled = label_event_outcome(event(status="READY"), [_candle(1, 100, 100.5, 97, 98)])

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.BAD_READY.value
    assert labeled.details_json["outcome"]["bad_ready"] is True


def test_blocked_low_return_or_drop_is_good_block():
    labeled = label_event_outcome(event(status="BLOCKED"), [_candle(1, 100, 100.5, 97.5, 98)])

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.GOOD_BLOCK.value


def test_blocked_big_rally_is_false_block():
    labeled = label_event_outcome(event(status="BLOCKED"), [_candle(1, 100, 104, 99, 103)])

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.FALSE_BLOCK.value


def test_wait_better_pullback_then_rebound_is_good_wait():
    labeled = label_event_outcome(
        event(status="WAIT"),
        [_candle(1, 100, 100.5, 98, 99), _candle(2, 99, 102, 99, 101)],
    )

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.GOOD_WAIT.value


def test_wait_immediate_breakout_is_missed_wait():
    labeled = label_event_outcome(event(status="WAIT"), [_candle(1, 100, 104, 100, 103)])

    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.MISSED_WAIT.value
    assert labeled.details_json["outcome"]["missed_opportunity"] is True


def test_missing_minute_data_is_insufficient():
    labeled = label_event_outcome(event(status="READY"), [])

    assert labeled.details_json["outcome_data_quality"] == "insufficient"
    assert labeled.details_json["outcome_label"] == HybridOutcomeLabel.INSUFFICIENT.value


def _candle(offset_min: int, open_price: float, high: float, low: float, close: float):
    return Candle(
        code="000001",
        interval_min=1,
        start_at=START + timedelta(minutes=offset_min),
        open=int(open_price),
        high=int(high),
        low=int(low),
        close=int(close),
    )
