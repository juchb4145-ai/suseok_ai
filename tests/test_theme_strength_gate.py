from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.gates import ThemeStrengthGate
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateState, StrategyProfile
from trading.strategy.themes import ThemeMapping, ThemeRepository


def candidate(code, state=CandidateState.WATCHING, *, strategy_profile=None, metadata=None):
    return Candidate(code=code, state=state, strategy_profile=strategy_profile, metadata=metadata or {})


def tick(store, code, price=10_000, change_rate=5.0, cum_volume=1_000):
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


def map_stock(repo, code, theme_id, profile=StrategyProfile.KOSDAQ_THEME_PROFILE, signal=False, priority=50):
    return repo.upsert_mapping(
        ThemeMapping(
            code=code,
            name=code,
            market="KOSPI" if signal else "KOSDAQ",
            theme_id=theme_id,
            theme_name=theme_id.title(),
            strategy_profile=profile,
            is_signal_stock=signal,
            is_leader_candidate=True,
            base_priority=priority,
        )
    )


def test_general_theme_with_two_strong_candidates_is_capped_at_b_plus(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    for code in ["111111", "222222"]:
        map_stock(repo, code, "robot")
        tick(store, code, change_rate=6.0, cum_volume=10_000)

    result = ThemeStrengthGate(repo, store).evaluate([candidate("111111"), candidate("222222")])[0]

    assert result.grade == "B+"
    assert result.score >= 75
    assert result.active_candidate_count == 2
    db.close()


def test_signal_pair_uses_signal_grade_instead_of_general_a(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    repo.seed_minimal_defaults()
    store = MarketDataStore()
    tick(store, "005930", change_rate=6.0, cum_volume=10_000)
    tick(store, "000660", change_rate=5.5, cum_volume=8_000)

    result = ThemeStrengthGate(repo, store).evaluate([candidate("005930"), candidate("000660")])[0]

    assert result.theme_id == "semiconductor"
    assert result.grade == "A_SIGNAL"
    assert result.details["signal_pair"] is True
    db.close()


def test_tick_shortage_caps_theme_and_records_insufficient_details(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
    tick(store, "111111", change_rate=5.0, cum_volume=10_000)

    result = ThemeStrengthGate(repo, store).evaluate(
        [candidate("111111"), candidate("222222"), candidate("333333")]
    )[0]

    assert result.grade == "C"
    assert result.details["valid_turnover_count"] == 1
    assert result.details["missing_turnover_count"] == 2
    assert "tick_missing" in result.details["insufficient_reason"]
    assert "turnover_missing" in result.details["insufficient_reason"]
    db.close()


def test_discovery_only_without_tick_counts_for_breadth_but_not_missing_tick_penalty(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
    tick(store, "111111", change_rate=5.0, cum_volume=10_000)
    tick(store, "222222", change_rate=4.0, cum_volume=8_000)
    discovery = candidate(
        "333333",
        strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
        metadata={
            "condition_purposes": {"주도테마_넓은후보": "theme_broad_candidate"},
            "entry_condition_names": [],
            "entry_excluded": True,
        },
    )

    result = ThemeStrengthGate(repo, store).evaluate([candidate("111111"), candidate("222222"), discovery])[0]

    assert result.active_candidate_count == 3
    assert result.details["scored_candidate_count"] == 2
    assert result.details["discovery_only_unscored_count"] == 1
    assert result.details["valid_tick_ratio"] == 1.0
    assert result.details["missing_turnover_count"] == 0
    assert "tick_missing" not in result.details["insufficient_reason"]
    db.close()


def test_theme_turnover_score_uses_candidate_pool_relative_strength(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "111111", "robot")
    map_stock(repo, "222222", "robot")
    map_stock(repo, "333333", "auto")
    tick(store, "111111", price=10_000, cum_volume=100)
    tick(store, "222222", price=10_000, cum_volume=100)
    tick(store, "333333", price=10_000, cum_volume=1_000)

    results = {result.theme_id: result for result in ThemeStrengthGate(repo, store).evaluate(
        [candidate("111111"), candidate("222222"), candidate("333333")]
    )}

    assert results["auto"].details["score_components"]["theme_turnover"] == 20.0
    assert results["robot"].details["score_components"]["theme_turnover"] < 20.0
    db.close()


def test_expired_and_removed_candidates_are_not_active(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    map_stock(repo, "111111", "robot")
    tick(store, "111111")

    results = ThemeStrengthGate(repo, store).evaluate([candidate("111111", CandidateState.EXPIRED)])

    assert results == []
    db.close()


def test_theme_diagnostics_v2_caps_one_spike_distortion(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
    tick(store, "111111", change_rate=30.0, cum_volume=10_000)
    tick(store, "222222", change_rate=1.0, cum_volume=9_000)
    tick(store, "333333", change_rate=0.5, cum_volume=8_000)

    result = ThemeStrengthGate(repo, store).evaluate(
        [candidate("111111"), candidate("222222"), candidate("333333")]
    )[0]
    diagnostics = result.details["theme_diagnostics_v2"]

    assert diagnostics["theme_trimmed_avg_change_pct"] == 1.0
    assert diagnostics["theme_capped_avg_change_pct"] < 5.0
    assert diagnostics["theme_capped_avg_change_pct"] < ((30.0 + 1.0 + 0.5) / 3)
    db.close()


def test_theme_sync_weak_is_recorded_without_changing_grade(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    for code in ["111111", "222222", "333333", "444444"]:
        map_stock(repo, code, "robot")
    tick(store, "111111", change_rate=5.0, cum_volume=10_000)
    tick(store, "222222", change_rate=-1.0, cum_volume=9_000)
    tick(store, "333333", change_rate=-2.0, cum_volume=8_000)
    tick(store, "444444", change_rate=-1.5, cum_volume=7_000)

    result = ThemeStrengthGate(repo, store).evaluate(
        [candidate("111111"), candidate("222222"), candidate("333333"), candidate("444444")]
    )[0]

    assert "THEME_SYNC_WEAK" in result.details["comparison_reason_codes"]
    assert "SOFT_BLOCK_ONLY" in result.details["comparison_reason_codes"]
    assert result.details["theme_diagnostics_v2"]["theme_sync_score"] < 50
    assert result.grade == "B"
    db.close()


def test_theme_trade_value_growth_and_input_missing_are_diagnostic_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
        feed_series(
            store,
            builder,
            code,
            [10_000 + index * 10 for index in range(10)],
            volumes=[100] * 5 + [300] * 5,
            change_rate=2.0,
        )

    result = ThemeStrengthGate(repo, store, builder).evaluate(
        [candidate("111111"), candidate("222222"), candidate("333333")]
    )[0]
    diagnostics = result.details["theme_diagnostics_v2"]

    assert diagnostics["theme_trade_value_growth_pct"] > 0
    assert "theme_trade_value_growth_missing" not in diagnostics["input_missing_fields"]
    db.close()
