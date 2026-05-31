from storage.db import TradingDatabase
from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.strategy.candles import CandleBuilder
from trading.strategy.gates import StockLeadershipGate, ThemeStrengthGate
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import Candidate, CandidateState
from trading.strategy.pipeline import GatePipeline
from trading.theme_engine.context_provider import DynamicThemeContextProvider


def _provider_and_market(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    provider = DynamicThemeContextProvider(repo)
    market = MarketDataStore()
    for code, rate, value in [("000001", 8.7, 120_000_000_000), ("000002", 5.9, 65_000_000_000), ("000003", 4.2, 28_000_000_000)]:
        market.update_tick(StrategyTick.from_realtime(code, price=1000, change_rate=rate, trade_value=value, execution_strength=150))
    return db, provider, market


def test_dynamic_theme_strength_and_leadership_gates_use_context_provider(tmp_path):
    db, provider, market = _provider_and_market(tmp_path)
    candidates = [
        Candidate(id=1, code="000001", state=CandidateState.DETECTED),
        Candidate(id=2, code="000002", state=CandidateState.DETECTED),
        Candidate(id=3, code="000003", state=CandidateState.DETECTED),
    ]

    theme_result = ThemeStrengthGate(provider, market).evaluate(candidates)[0]
    leadership = StockLeadershipGate(provider, market).evaluate(candidates[0], candidates)[0]

    assert theme_result.theme_id == "furiosa_ai"
    assert theme_result.active_candidate_count == 3
    assert leadership.leadership_role == "leader"
    db.close()


def test_gate_pipeline_is_wired_to_dynamic_context_provider(tmp_path):
    db, provider, market = _provider_and_market(tmp_path)
    candle_builder = CandleBuilder()
    pipeline = GatePipeline(
        provider,
        market,
        candle_builder,
        IndicatorCalculator(market, candle_builder),
        IntradayStateTracker(),
        MarketIndexStore(),
    )

    assert pipeline.theme_context_provider is provider
    assert not hasattr(pipeline, "theme_repository")
    db.close()
