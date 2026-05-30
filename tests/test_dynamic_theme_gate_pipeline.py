from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.gates import StockLeadershipGate, ThemeStrengthGate
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import Candidate, CandidateState
from trading.strategy.pipeline import GatePipeline
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.sources.fixture import FixtureThemeSource


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def _provider_and_market(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    ThemeEvidenceService(repo, ThemeCanonicalResolver(repo)).sync_source(FixtureThemeSource(FIXTURE))
    ThemeMembershipBuilder(repo).build_all_current_memberships()
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
