from types import SimpleNamespace

from trading.strategy.data_quality_taxonomy import (
    ACTION_ALLOW_EARLY_SMALL_CANDIDATE,
    ACTION_BLOCK,
    ACTION_OBSERVE,
    ACTION_WAIT_DATA,
    BUCKET_BACKFILL_ONLY_OBSERVE,
    BUCKET_CORE_BLOCKING,
    BUCKET_ENTRY_BLOCKING,
    BUCKET_WARMUP_OPTIONAL,
    classify_entry_data_quality,
    data_quality_action_for_candidate,
)
from trading.strategy.runtime_settings import legacy_strategy_runtime_settings


def _tick(**overrides):
    payload = {
        "price": 10000,
        "change_rate": 0.0,
        "trade_value": 100_000_000,
        "execution_strength": 120.0,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_core_blocking_requires_current_realtime_fields():
    result = classify_entry_data_quality(
        tick=_tick(price=0),
        latest_tick_ready=True,
        support={"ready": True},
    )

    assert result.bucket == BUCKET_CORE_BLOCKING
    assert result.action == ACTION_BLOCK
    assert "MISSING_CURRENT_PRICE" in result.missing_core_fields


def test_entry_blocking_keeps_support_and_vwap_wait_data():
    result = classify_entry_data_quality(
        tick=_tick(),
        latest_tick_ready=True,
        support={"ready": False, "reason_codes": ["VWAP_NOT_READY"]},
    )

    assert result.bucket == BUCKET_ENTRY_BLOCKING
    assert result.action == ACTION_WAIT_DATA
    assert result.missing_entry_fields == ["VWAP_NOT_READY"]


def test_warmup_optional_is_early_small_candidate_but_order_disabled_by_default():
    settings = legacy_strategy_runtime_settings()
    classified = classify_entry_data_quality(
        reason_codes=["BASE_LINE_120_INSUFFICIENT_CANDLES"],
        tick=_tick(),
        latest_tick_ready=True,
        support={"ready": True, "source": "vwap", "price": 9950},
        metadata={"vwap_ready": True},
    )
    result = data_quality_action_for_candidate(
        classified,
        settings=settings,
        status="READY",
        stock_role="LEADER",
        theme_status="LEADING_THEME",
        price_location_status="GOOD_PULLBACK",
        risk_level="PASS",
        latest_tick_ready=True,
        current_price=10000,
        trade_value=100_000_000,
        vwap_ready=True,
    )

    assert result.bucket == BUCKET_WARMUP_OPTIONAL
    assert result.action == ACTION_ALLOW_EARLY_SMALL_CANDIDATE
    assert result.early_small_candidate is True
    assert result.early_small_order_enabled is False
    assert result.early_small_position_size_multiplier <= 0.15
    assert "WAIT_DATA_EARLY_SMALL_CANDIDATE" in result.reason_codes


def test_warmup_optional_rejects_chase_risk_before_ready():
    settings = legacy_strategy_runtime_settings()
    classified = classify_entry_data_quality(
        reason_codes=["BASE_LINE_120_INSUFFICIENT_CANDLES", "CHASE_RISK"],
        tick=_tick(),
        latest_tick_ready=True,
        support={"ready": True, "source": "vwap", "price": 9950},
        metadata={"vwap_ready": True},
    )
    result = data_quality_action_for_candidate(
        classified,
        settings=settings,
        status="READY",
        stock_role="LEADER",
        theme_status="LEADING_THEME",
        price_location_status="GOOD_PULLBACK",
        risk_level="PASS",
        latest_tick_ready=True,
        current_price=10000,
        trade_value=100_000_000,
        vwap_ready=True,
        reason_codes=["CHASE_RISK"],
    )

    assert result.bucket == BUCKET_WARMUP_OPTIONAL
    assert result.action == ACTION_WAIT_DATA
    assert result.early_small_candidate is False
    assert result.early_small_rejected_reason == "CHASE_RISK"


def test_backfill_only_remains_observe_until_realtime_confirms():
    result = classify_entry_data_quality(
        reason_codes=["PRICE_SOURCE_TR_BACKFILL", "REALTIME_TICK_NOT_CONFIRMED"],
        tick=_tick(),
        latest_tick_ready=True,
        support={"ready": True},
        metadata={"price_source": "TR_BACKFILL", "realtime_tick_confirmed": False},
    )

    assert result.bucket == BUCKET_BACKFILL_ONLY_OBSERVE
    assert result.action == ACTION_OBSERVE
