from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.setup_features import SetupFeatureBuilder

from tests.test_setup_router_v3 import _candidate, _context, _entry_decision, _seed_candles


def test_setup_feature_builder_marks_stale_realtime_tick_as_data_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-features.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)
    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3, max_tick_age_sec=1).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 7, 0),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
    )

    assert "REALTIME_TICK_STALE" in feature.data_wait_reasons
    assert feature.schema_version == "setup_router_v3.features.v3"


def test_setup_feature_builder_uses_market_data_tick_age_contract(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-features-tick-age.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)

    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3, max_tick_age_sec=10).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 16),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
    )

    assert "REALTIME_TICK_STALE" in feature.data_wait_reasons


def test_setup_feature_builder_blocks_missing_required_selected_theme_lease(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-features-lease.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)

    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3, max_tick_age_sec=10).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 6),
        strategy_context={**_context(), "selected_theme_lease_required": True},
        entry_decision=_entry_decision(),
        selected_theme_lease_required=True,
        other_theme_lease_count=1,
    )

    assert feature.post_subscription_tick_verified is False
    assert feature.post_subscription_tick_reason == "SETUP_SELECTED_THEME_LEASE_MISSING"
    assert "SETUP_SELECTED_THEME_LEASE_MISSING" in feature.data_wait_reasons
    assert feature.other_theme_lease_count == 1
