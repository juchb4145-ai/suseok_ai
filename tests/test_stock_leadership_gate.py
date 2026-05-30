from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.gates import StockLeadershipGate
from trading.strategy.market_index import IndexTick, MarketIndexStore
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


def feed_series(store, builder, code, prices, volumes=None, change_rate=1.0, start=None):
    start = start or datetime(2026, 5, 29, 9, 0)
    cum_volume = 1_000
    volumes = volumes or [100] * len(prices)
    for index, price in enumerate(prices):
        cum_volume += volumes[index]
        at = start + timedelta(minutes=index, seconds=1)
        realtime = StrategyTick.from_realtime(
            code,
            price=price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            timestamp=at,
        )
        store.update_tick(realtime)
        builder.update(realtime)
    builder.flush(code, start + timedelta(minutes=len(prices), seconds=1))


def feed_index_series(index_store, index_code, prices, start=None):
    start = start or datetime(2026, 5, 29, 9, 0)
    for index, price in enumerate(prices):
        index_store.update_index_tick(
            IndexTick.from_realtime(
                index_code,
                index_code,
                price,
                timestamp=start + timedelta(minutes=index, seconds=1),
            )
        )
    index_store.candle_builder.flush(f"IDX{index_code}", start + timedelta(minutes=len(prices), seconds=1))


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


def test_leader_follower_gap_is_recorded_as_soft_comparison_reason(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "111111", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, priority=90)
    map_stock(repo, "222222", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, priority=80)
    tick(store, "111111", change_rate=12.0, cum_volume=20_000)
    tick(store, "222222", change_rate=1.0, cum_volume=1_000)

    result = StockLeadershipGate(repo, store).evaluate(
        candidate("222222", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        [
            candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
            candidate("222222", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        ],
    )[0]
    diagnostics = result.details["leadership_diagnostics_v2"]

    assert diagnostics["leader_follower_gap"]["leader_code"] == "111111"
    assert diagnostics["leader_follower_gap"]["candidate_code"] == "222222"
    assert diagnostics["leader_follower_gap_pct"] >= 8.0
    assert "LEADER_FOLLOWER_GAP" in result.details["comparison_reason_codes"]
    assert "SOFT_BLOCK_ONLY" in result.details["comparison_reason_codes"]
    db.close()


def test_leader_replaced_is_detected_without_changing_legacy_role(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "111111", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, priority=100)
    map_stock(repo, "222222", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, priority=60)
    tick(store, "111111", change_rate=1.0, cum_volume=1_000)
    tick(store, "222222", change_rate=8.0, cum_volume=20_000)

    result = StockLeadershipGate(repo, store).evaluate(
        candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        [
            candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
            candidate("222222", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        ],
    )[0]

    assert result.leadership_role == "second_leader"
    assert result.details["leadership_diagnostics_v2"]["leader_replaced"] is True
    assert result.details["leadership_diagnostics_v2"]["expected_leader_code"] == "111111"
    assert result.details["leadership_diagnostics_v2"]["current_leader_code"] == "222222"
    assert "LEADER_REPLACED" in result.details["comparison_reason_codes"]
    db.close()


def test_relative_strength_uses_candidate_market_index(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    map_stock(repo, "111111", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ", priority=90)
    map_stock(repo, "005380", "auto", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI", priority=90)
    feed_series(store, builder, "111111", [10_000 + index * 30 for index in range(20)], change_rate=5.0)
    feed_series(store, builder, "005380", [10_000 + index * 30 for index in range(20)], change_rate=5.0)
    feed_index_series(index_store, "KOSDAQ", [1_000 + index for index in range(20)])
    feed_index_series(index_store, "KOSPI", [1_000 + index * 10 for index in range(20)])

    kosdaq_result = StockLeadershipGate(repo, store, builder, index_store).evaluate(
        candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        [candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ")],
    )[0]
    kospi_result = StockLeadershipGate(repo, store, builder, index_store).evaluate(
        candidate("005380", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI"),
        [candidate("005380", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI")],
    )[0]

    assert kosdaq_result.details["leadership_diagnostics_v2"]["relative_strength_vs_index_20m"] > 0
    assert kospi_result.details["leadership_diagnostics_v2"]["relative_strength_vs_index_20m"] < 0
    db.close()


def test_leadership_diagnostics_records_input_missing_without_blocking(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "555555", "robot", StrategyProfile.KOSDAQ_THEME_PROFILE)

    result = StockLeadershipGate(repo, store).evaluate(candidate("555555"), [candidate("555555")])[0]

    assert result.leadership_rank == 1
    assert "INPUT_MISSING" in result.details["comparison_reason_codes"]
    assert result.details["leadership_diagnostics_v2"]["input_missing_fields"]
    db.close()
