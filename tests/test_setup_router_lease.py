from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState
from trading.strategy.setup_features import SetupFeatureBuilder
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline

from tests.test_setup_router_v3 import TRADE_DATE, _context, _entry_decision, _seed_candles


def test_opening_burst_candidate_does_not_require_expansion_lease_by_source(tmp_path):
    db = TradingDatabase(str(tmp_path / "lease-exact.db"))
    pipeline = SetupRouterV3RuntimePipeline(db=db, config=SetupRouterConfig(enabled=True))
    candidate = Candidate(
        trade_date=TRADE_DATE,
        code="000001",
        state=CandidateState.WATCHING,
        sources=[CandidateSourceType.OPENING_BURST],
    )
    context = {**_context(), "selected_theme_id": "theme-b", "theme": {**_context()["theme"], "theme_id": "theme-b"}}

    selection = pipeline._lease_selection(
        [
            {"code": "000001", "theme_id": "theme-a", "status": "ACTIVE", "selected_at": "2026-06-22T09:00:00"},
            {"code": "000001", "theme_id": "theme-b", "status": "EXPIRED", "selected_at": "2026-06-22T09:01:00"},
        ],
        context,
        candidate=candidate,
    )

    assert selection["required"] is False
    assert selection["lease"] == {}
    assert selection["other_theme_lease_count"] == 1


def test_expansion_only_candidate_requires_exact_selected_theme_lease(tmp_path):
    db = TradingDatabase(str(tmp_path / "lease-expansion-only.db"))
    pipeline = SetupRouterV3RuntimePipeline(db=db, config=SetupRouterConfig(enabled=True))
    candidate = Candidate(
        trade_date=TRADE_DATE,
        code="000001",
        state=CandidateState.WATCHING,
        sources=[CandidateSourceType.OPENING_BURST],
        metadata={"expansion_only": True},
    )
    context = {**_context(), "selected_theme_id": "theme-b", "theme": {**_context()["theme"], "theme_id": "theme-b"}}

    selection = pipeline._lease_selection(
        [
            {"code": "000001", "theme_id": "theme-a", "status": "ACTIVE", "selected_at": "2026-06-22T09:00:00"},
            {"code": "000001", "theme_id": "theme-b", "status": "EXPIRED", "selected_at": "2026-06-22T09:01:00"},
        ],
        context,
        candidate=candidate,
    )

    assert selection["required"] is True
    assert selection["lease"]["theme_id"] == "theme-b"
    assert selection["lease"]["status"] == "EXPIRED"
    assert selection["other_theme_lease_count"] == 1


def test_condition_candidate_is_not_required_by_other_theme_lease(tmp_path):
    db = TradingDatabase(str(tmp_path / "lease-condition.db"))
    pipeline = SetupRouterV3RuntimePipeline(db=db, config=SetupRouterConfig(enabled=True))
    candidate = Candidate(
        trade_date=TRADE_DATE,
        code="000001",
        state=CandidateState.WATCHING,
        sources=[CandidateSourceType.CONDITION_SEARCH],
    )
    selection = pipeline._lease_selection(
        [{"code": "000001", "theme_id": "theme-a", "status": "ACTIVE", "selected_at": "2026-06-22T09:00:00"}],
        {**_context(), "selected_theme_id": "theme-b", "theme": {**_context()["theme"], "theme_id": "theme-b"}},
        candidate=candidate,
    )

    assert selection["required"] is False
    assert selection["lease"] == {}


def test_required_inactive_selected_theme_lease_is_data_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "lease-inactive-feature.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="000001",
            name="테스트",
            state=CandidateState.WATCHING,
            sources=[CandidateSourceType.OPENING_BURST],
            metadata={"candidate_instance_id": "ci-lease"},
        )
    )
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)

    feature = SetupFeatureBuilder(market_data, candles, min_completed_1m_candles=3, max_tick_age_sec=10).build(
        candidate,
        now=datetime(2026, 6, 22, 9, 5, 6),
        strategy_context={**_context(), "selected_theme_id": "ai"},
        entry_decision=_entry_decision(),
        expansion_lease={"code": "000001", "theme_id": "ai", "status": "EXPIRED", "selected_at": "2026-06-22T09:00:00"},
        selected_theme_lease_required=True,
    )

    assert feature.post_subscription_tick_verified is False
    assert feature.post_subscription_tick_reason == "SETUP_SELECTED_THEME_LEASE_INACTIVE"
    assert "SETUP_SELECTED_THEME_LEASE_INACTIVE" in feature.data_wait_reasons
