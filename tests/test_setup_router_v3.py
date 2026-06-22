from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState
from trading.strategy.setup_features import SetupFeatureBuilder
from trading.strategy.setup_router_v3 import SetupRouterConfig, SetupRouterV3


TRADE_DATE = "2026-06-22"


def test_setup_router_vwap_reclaim_requires_temporal_prior_below_vwap(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-router.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[990, 995, 998, 1002, 1008], vwap=1000)
    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 5),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
    )

    observations = SetupRouterV3(SetupRouterConfig(enabled=True)).classify(feature)
    vwap = next(item for item in observations if item.setup_type == "VWAP_RECLAIM")

    assert vwap.shape_status == "MATCHED"
    assert vwap.context_status == "ELIGIBLE"
    assert vwap.router_status == "VALID_OBSERVE"
    assert vwap.ready_allowed is False
    assert vwap.order_intent_allowed is False
    assert vwap.live_order_allowed is False


def test_setup_router_price_above_vwap_without_prior_below_is_not_matched(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-router-no-prior.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[1002, 1004, 1005, 1006, 1008], vwap=1000)
    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 5),
        strategy_context=_context(),
        entry_decision=_entry_decision(),
    )

    observations = SetupRouterV3(SetupRouterConfig(enabled=True)).classify(feature)
    vwap = next(item for item in observations if item.setup_type == "VWAP_RECLAIM")

    assert vwap.shape_status == "NOT_SEEN"
    assert vwap.router_status == "UNKNOWN"


def _candidate(db):
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="000001",
            name="테스트",
            state=CandidateState.WATCHING,
            sources=[CandidateSourceType.CONDITION_SEARCH],
            metadata={"candidate_instance_id": "ci-000001"},
        )
    )


def _seed_candles(market_data, candles, *, closes, vwap):
    start = datetime(2026, 6, 22, 9, 0, 0)
    for index, close in enumerate(closes):
        tick = StrategyTick.from_realtime(
            "000001",
            price=close,
            change_rate=5.0,
            cum_volume=1000 + index * 100,
            trade_value=1_000_000_000 + index * 100_000_000,
            execution_strength=130,
            timestamp=start + timedelta(minutes=index),
            metadata={"vwap": vwap, "price_source": "REALTIME"},
        )
        market_data.update_tick(tick)
        candles.update(tick)
    last = StrategyTick.from_realtime(
        "000001",
        price=closes[-1],
        change_rate=5.0,
        cum_volume=2000,
        trade_value=1_500_000_000,
        execution_strength=130,
        timestamp=start + timedelta(minutes=len(closes), seconds=5),
        metadata={"vwap": vwap, "price_source": "REALTIME"},
    )
    market_data.update_tick(last)
    candles.update(last)


def _context():
    return {
        "schema_version": "strategy_context_v3",
        "context_id": "ctx-1",
        "trade_date": TRADE_DATE,
        "code": "000001",
        "calculated_at": "2026-06-22T09:05:00",
        "context_fresh": True,
        "session_phase": "MORNING_TREND",
        "selected_theme_id": "ai",
        "source_timestamps": {"market_context_at": "2026-06-22T09:05:00", "theme_context_at": "2026-06-22T09:05:00"},
        "market": {
            "market_side": "KOSDAQ",
            "side_market_regime": "EXPANSION",
            "market_action": "ALLOW_NORMAL",
            "market_session_status": "OPENING_DISCOVERY",
        },
        "theme": {
            "theme_id": "ai",
            "theme_name": "AI",
            "theme_state": "LEADING_THEME",
            "leadership_status": "INCUMBENT",
        },
        "stock": {"trade_stock_role": "LEADER_CONFIRMED"},
        "data": {"theme_context_fresh": True, "market_context_fresh": True},
        "risk": {},
        "reason_codes": [],
    }


def _entry_decision():
    return {
        "trade_date": TRADE_DATE,
        "candidate_id": 1,
        "code": "000001",
        "calculated_at": "2026-06-22T09:05:00",
        "entry_status": "OBSERVE_READY",
        "price_location": "VWAP_RECLAIM",
        "reason_codes": [],
    }
