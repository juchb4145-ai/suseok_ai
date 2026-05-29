from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.gates import StockLeadershipGate
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateState, StrategyProfile
from trading.strategy.themes import ThemeMapping, ThemeRepository


def candidate(code, profile=None, market=""):
    return Candidate(code=code, state=CandidateState.WATCHING, strategy_profile=profile, market=market)


def tick(store, code, price=10_000, change_rate=1.0, cum_volume=1_000):
    store.update_tick(
        StrategyTick.from_realtime(
            code,
            price=price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            timestamp=datetime(2026, 5, 29, 9, 0),
        )
    )


def map_stock(
    repo,
    code,
    theme_id,
    profile,
    market="KOSDAQ",
    signal=False,
    priority=50,
    leader=True,
):
    return repo.upsert_mapping(
        ThemeMapping(
            code=code,
            name=code,
            market=market,
            theme_id=theme_id,
            theme_name=theme_id.title(),
            strategy_profile=profile,
            is_signal_stock=signal,
            is_large_cap=signal or market == "KOSPI",
            is_leader_candidate=leader,
            base_priority=priority,
        )
    )


def test_signal_stocks_do_not_distort_kosdaq_theme_leadership_ranking(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "005930", "semiconductor", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI", signal=True, priority=100)
    map_stock(repo, "000660", "semiconductor", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI", signal=True, priority=95)
    map_stock(repo, "111111", "semiconductor", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ", priority=70)
    map_stock(repo, "222222", "semiconductor", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ", priority=60)
    tick(store, "005930", price=80_000, change_rate=3.0, cum_volume=2_000_000)
    tick(store, "000660", price=250_000, change_rate=2.5, cum_volume=500_000)
    tick(store, "111111", price=10_000, change_rate=5.0, cum_volume=1_000)
    tick(store, "222222", price=10_000, change_rate=4.0, cum_volume=2_000)
    candidates = [
        candidate("005930"),
        candidate("000660"),
        candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        candidate("222222", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
    ]

    kosdaq_result = StockLeadershipGate(repo, store).evaluate(candidates[3], candidates)[0]
    signal_result = StockLeadershipGate(repo, store).evaluate(candidates[0], candidates)[0]

    assert kosdaq_result.leadership_scope == "same_strategy_profile"
    assert kosdaq_result.leadership_rank == 1
    assert kosdaq_result.leadership_role == "leader"
    assert kosdaq_result.details["scope_candidate_codes"] == ["222222", "111111"]
    assert signal_result.leadership_scope == "signal_only"
    assert signal_result.details["scope_candidate_codes"] == ["005930", "000660"]
    db.close()


def test_multi_theme_candidate_gets_result_per_theme_id(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "333333", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE)
    map_stock(repo, "333333", "ai", StrategyProfile.KOSDAQ_THEME_PROFILE)
    tick(store, "333333")

    results = StockLeadershipGate(repo, store).evaluate(candidate("333333"), [candidate("333333")])

    assert {result.theme_id for result in results} == {"robot", "ai"}
    assert all(result.leadership_rank == 1 for result in results)
    db.close()


def test_base_priority_is_clamped_and_normalized_in_details(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "444444", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, priority=150)
    tick(store, "444444")

    result = StockLeadershipGate(repo, store).evaluate(candidate("444444"), [candidate("444444")])[0]

    assert result.details["base_priority_original"] == 150
    assert result.details["base_priority_normalized"] == 100.0
    assert result.details["score_components"]["base_priority"] == 15.0
    db.close()


def test_missing_tick_records_insufficient_reason(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "555555", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE)

    result = StockLeadershipGate(repo, store).evaluate(candidate("555555"), [candidate("555555")])[0]

    assert "tick_missing" in result.details["insufficient_reason"]
    assert "turnover_missing" in result.details["insufficient_reason"]
    db.close()
