from datetime import datetime
from types import SimpleNamespace

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.setup_features import SetupFeatureBuilder
from trading.strategy.setup_router_v3 import SetupRouterConfig, SetupRouterV3, _confirmed_local_peak

from tests.test_setup_router_v3 import TRADE_DATE, _candidate, _context, _entry_decision, _seed_candles


def test_lfp_matched_terminal_does_not_reactivate_same_generation(tmp_path):
    db = TradingDatabase(str(tmp_path / "lfp-terminal.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[100, 110, 108, 107, 107], vwap=100)
    previous = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-000001",
        "theme_id": "ai",
        "setup_type": "LEADER_FIRST_PULLBACK",
        "shape_status": "MATCHED",
        "lifecycle_state": "MATCHED",
        "setup_generation": 1,
        "setup_instance_id": "lfp-1",
        "terminal_at": "2026-06-22T09:05:00",
        "last_material_change_at": "2026-06-22T09:05:00",
        "state_payload": {
            "phase": "MATCHED",
            "local_peak_price": 110,
            "local_peak_at": "2026-06-22T09:01:00",
            "first_pullback_consumed": True,
            "terminal_at": "2026-06-22T09:05:00",
        },
    }
    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 6, 5),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
        setup_states={"LEADER_FIRST_PULLBACK": previous},
    )

    observations = SetupRouterV3(SetupRouterConfig(enabled=True)).classify(feature)
    lfp = next(item for item in observations if item.setup_type == "LEADER_FIRST_PULLBACK")

    assert lfp.shape_status == "MATCHED"
    assert lfp.lifecycle_state == "MATCHED"
    assert lfp.setup_generation == 1
    assert lfp.setup_instance_id == "lfp-1"
    assert "TERMINAL_LIFECYCLE_LOCKED" in lfp.reason_codes


def test_vwap_anchor_is_fixed_for_active_generation(tmp_path):
    db = TradingDatabase(str(tmp_path / "vwap-anchor.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 950, 1008], vwap=1000)
    previous = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-000001",
        "theme_id": "ai",
        "setup_type": "VWAP_RECLAIM",
        "shape_status": "FORMING",
        "lifecycle_state": "FORMING",
        "setup_generation": 1,
        "setup_instance_id": "vwap-1",
        "state_payload": {
            "phase": "BELOW_CONFIRMED",
            "below_candle_at": "2026-06-22T09:01:00",
            "below_price": 970,
            "below_vwap_at_close": 1000,
            "below_close_vs_vwap_pct": -3.0,
            "anchor_fixed_at": "2026-06-22T09:01:00",
        },
    }
    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 5),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
        setup_states={"VWAP_RECLAIM": previous},
    )

    observations = SetupRouterV3(SetupRouterConfig(enabled=True)).classify(feature)
    vwap = next(item for item in observations if item.setup_type == "VWAP_RECLAIM")

    assert vwap.price_structure["below_candle_at"] == "2026-06-22T09:01:00"
    assert vwap.price_structure["below_price"] == 970
    assert vwap.setup_generation == 1


def test_local_peak_requires_lower_high_and_lower_close():
    loose_feature = SimpleNamespace(
        calculated_at="2026-06-22T09:05:00",
        completed_1m_candles=[
            {"candle_at": "2026-06-22T09:00:00", "high": 110, "close": 109, "completed": True},
            {"candle_at": "2026-06-22T09:01:00", "high": 111, "close": 108, "completed": True},
        ],
    )
    strict_feature = SimpleNamespace(
        calculated_at="2026-06-22T09:05:00",
        completed_1m_candles=[
            {"candle_at": "2026-06-22T09:00:00", "high": 110, "close": 109, "completed": True},
            {"candle_at": "2026-06-22T09:01:00", "high": 109, "close": 108, "completed": True},
            {"candle_at": "2026-06-22T09:02:00", "high": 108, "close": 107, "completed": True},
        ],
    )
    config = SetupRouterConfig(enabled=True, leader_local_peak_min_age_sec=0)

    assert _confirmed_local_peak(loose_feature, config).found is False
    peak = _confirmed_local_peak(strict_feature, config)
    assert peak.found is True
    assert peak.price == 109
    assert peak.confirmation_candle_index == 2
