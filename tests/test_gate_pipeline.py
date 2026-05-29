from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.indicators import IndicatorCalculator, PreviousDayLevelProvider
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_index import IndexTick, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateState, StrategyProfile
from trading.strategy.pipeline import GatePipeline
from trading.strategy.themes import ThemeMapping, ThemeRepository


def map_stock(repo, code, theme_id="robot", profile=StrategyProfile.KOSDAQ_THEME_PROFILE, market="KOSDAQ", signal=False, priority=80):
    return repo.upsert_mapping(
        ThemeMapping(
            code=code,
            name=code,
            market=market,
            theme_id=theme_id,
            theme_name=theme_id.title(),
            strategy_profile=profile,
            is_large_cap=market == "KOSPI" or signal,
            is_leader_candidate=True,
            base_priority=priority,
            is_signal_stock=signal,
        )
    )


def candidate(code, profile=StrategyProfile.KOSDAQ_THEME_PROFILE, market="KOSDAQ", state=CandidateState.WATCHING):
    return Candidate(code=code, state=state, strategy_profile=profile, market=market)


def feed_stock(store, builder, code, high=10_000, low=9_500, current=9_750, cum_base=1_000, change_rate=5.0):
    start = datetime(2026, 5, 29, 9, 0)
    points = [
        (start + timedelta(seconds=1), high, cum_base),
        (start + timedelta(seconds=20), high, cum_base + 100),
        (start + timedelta(minutes=1, seconds=1), low, cum_base + 100),
        (start + timedelta(minutes=1, seconds=20), low + 100, cum_base + 200),
        (start + timedelta(minutes=2, seconds=1), current - 50, cum_base + 200),
        (start + timedelta(minutes=2, seconds=20), current, cum_base + 500),
        (start + timedelta(minutes=3, seconds=1), current, cum_base + 500),
    ]
    for at, price, volume in points:
        tick = StrategyTick.from_realtime(code, price=price, change_rate=change_rate, cum_volume=volume, timestamp=at)
        store.update_tick(tick)
        builder.update(tick)


def feed_kospi_wait_stock(store, builder, code):
    start = datetime(2026, 5, 29, 9, 0)
    points = [
        (start + timedelta(seconds=1), 10_000, 1_000),
        (start + timedelta(seconds=20), 10_000, 1_100),
        (start + timedelta(minutes=1, seconds=1), 9_600, 1_100),
        (start + timedelta(minutes=1, seconds=20), 9_900, 1_200),
        (start + timedelta(minutes=2, seconds=1), 9_730, 1_200),
        (start + timedelta(minutes=2, seconds=20), 9_740, 1_500),
        (start + timedelta(minutes=3, seconds=1), 9_750, 1_500),
    ]
    for at, price, volume in points:
        tick = StrategyTick.from_realtime(code, price=price, change_rate=3.0, cum_volume=volume, timestamp=at)
        store.update_tick(tick)
        builder.update(tick)


def feed_index(index_store, code, price=1_000):
    index_store.update_index_tick(IndexTick.from_realtime(code, code, price, timestamp=datetime(2026, 5, 29, 9, 0)))


def build_pipeline(repo, store, builder, index_store, previous_levels=None):
    calculator = IndicatorCalculator(store, builder, PreviousDayLevelProvider(previous_levels or {}))
    return GatePipeline(repo, store, builder, calculator, IntradayStateTracker(), index_store)


def result_for(results, code, theme_id):
    return next(result for result in results if result.code == code and result.theme_id == theme_id)


def test_strategy_eligible_is_not_actual_order_permission_and_pipeline_does_not_persist_or_mutate(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    for index, code in enumerate(["111111", "222222", "333333"]):
        map_stock(repo, code, "robot", priority=90 - index)
        feed_stock(store, builder, code, cum_base=2_000 - index * 100)
    feed_index(index_store, "KOSDAQ")
    candidates = [candidate("111111"), candidate("222222"), candidate("333333")]

    results = build_pipeline(repo, store, builder, index_store).evaluate(candidates)
    target = result_for(results, "111111", "robot")

    assert target.final_grade == "A"
    assert target.strategy_eligible is True
    assert target.details["actual_order_allowed"] is False
    assert target.details["entry_plan_created"] is False
    assert candidates[0].state == CandidateState.WATCHING
    assert db.conn.execute("SELECT COUNT(*) AS count FROM gate_decisions").fetchone()["count"] == 0
    db.close()


def test_active_only_and_multi_theme_results(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    map_stock(repo, "111111", "robot")
    map_stock(repo, "111111", "ai")
    map_stock(repo, "999999", "robot")
    feed_stock(store, builder, "111111")
    feed_stock(store, builder, "999999")
    feed_index(index_store, "KOSDAQ")

    results = build_pipeline(repo, store, builder, index_store).evaluate(
        [candidate("111111"), candidate("999999", state=CandidateState.EXPIRED)]
    )

    assert {(result.code, result.theme_id) for result in results} == {("111111", "robot"), ("111111", "ai")}
    db.close()


def test_a_signal_does_not_promote_kosdaq_candidate_to_plain_a(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    repo.seed_minimal_defaults()
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    map_stock(repo, "111111", "semiconductor", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ", priority=90)
    feed_stock(store, builder, "005930", cum_base=5_000)
    feed_stock(store, builder, "000660", cum_base=4_500)
    feed_stock(store, builder, "111111", cum_base=3_000)
    feed_index(index_store, "KOSDAQ")

    results = build_pipeline(repo, store, builder, index_store).evaluate(
        [
            candidate("005930", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI"),
            candidate("000660", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI"),
            candidate("111111", StrategyProfile.KOSDAQ_THEME_PROFILE, "KOSDAQ"),
        ]
    )
    target = result_for(results, "111111", "semiconductor")

    assert target.final_grade == "B+"
    assert target.strategy_eligible is False
    assert target.details["sub_status"] == "A_SIGNAL_WAIT"
    assert "A_SIGNAL_KOSDAQ_WAIT_CAP" in target.details["cap_rules_applied"]
    db.close()


def test_kospi_signal_direct_candidate_can_be_a(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    repo.seed_minimal_defaults()
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    feed_stock(store, builder, "005930", cum_base=5_000)
    feed_stock(store, builder, "000660", cum_base=4_500)
    feed_index(index_store, "KOSPI")

    results = build_pipeline(repo, store, builder, index_store).evaluate(
        [
            candidate("005930", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI"),
            candidate("000660", StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, "KOSPI"),
        ]
    )
    target = result_for(results, "005930", "semiconductor")

    assert target.final_grade == "A"
    assert target.strategy_eligible is True
    db.close()


def test_leader_collapse_caps_a_even_when_another_leader_is_healthy(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
    feed_stock(store, builder, "111111", cum_base=5_000)
    feed_stock(store, builder, "222222", high=10_000, low=9_000, current=9_000, cum_base=4_500)
    feed_stock(store, builder, "333333", cum_base=1_000)
    feed_index(index_store, "KOSDAQ")

    target = result_for(
        build_pipeline(repo, store, builder, index_store).evaluate(
            [candidate("111111"), candidate("222222"), candidate("333333")]
        ),
        "111111",
        "robot",
    )

    assert target.final_grade == "C"
    assert "THEME_PULLBACK_FINAL_CAP" in target.details["cap_rules_applied"]
    assert target.details["sub_status"] == "THEME_LEADER_COLLAPSE"
    db.close()


def test_kosdaq_shallow_pullback_requires_strong_exception(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    for index, code in enumerate(["111111", "222222", "333333"]):
        map_stock(repo, code, "robot", priority=90 - index)
        feed_stock(store, builder, code, current=9_850, cum_base=3_000 - index * 100)
    feed_index(index_store, "KOSDAQ")

    strong = result_for(
        build_pipeline(repo, store, builder, index_store).evaluate(
            [candidate("111111"), candidate("222222"), candidate("333333")]
        ),
        "111111",
        "robot",
    )

    assert strong.final_grade == "A"
    assert next(dec for dec in strong.decisions if dec.gate_name == "StockPullbackEntryGate").details["shallow_exception"] is True

    weak_results = build_pipeline(repo, store, builder, index_store).evaluate(
        [candidate("111111"), candidate("222222")]
    )
    weak = result_for(weak_results, "111111", "robot")

    assert weak.final_grade in {"B", "B+"}
    assert weak.strategy_eligible is False
    assert weak.details["sub_status"] in {"WAIT_PULLBACK_CONFIRMATION", "A_SIGNAL_WAIT"}
    db.close()


def test_support_details_separate_distance_touch_and_reclaim(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
        feed_stock(store, builder, code)
    feed_index(index_store, "KOSDAQ")

    target = result_for(
        build_pipeline(repo, store, builder, index_store).evaluate(
            [candidate("111111"), candidate("222222"), candidate("333333")]
        ),
        "111111",
        "robot",
    )
    stock_details = next(dec for dec in target.decisions if dec.gate_name == "StockPullbackEntryGate").details

    assert stock_details["nearest_support"] is not None
    assert stock_details["support_distance_pct"] is not None
    assert isinstance(stock_details["support_touched"], bool)
    assert isinstance(stock_details["support_reclaimed"], bool)
    db.close()


def test_kospi_leader_without_recovery_is_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    map_stock(repo, "005380", "auto", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI")
    map_stock(repo, "000270", "auto", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI")
    feed_kospi_wait_stock(store, builder, "005380")
    feed_stock(store, builder, "000270", cum_base=500)
    feed_index(index_store, "KOSPI")

    target = result_for(
        build_pipeline(repo, store, builder, index_store, {"005380": (9_700, 9_200)}).evaluate(
            [
                candidate("005380", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI"),
                candidate("000270", StrategyProfile.KOSPI_LEADER_PROFILE, "KOSPI"),
            ]
        ),
        "005380",
        "auto",
    )

    assert target.final_grade == "B"
    assert target.strategy_eligible is False
    assert target.details["sub_status"] == "WAIT_PULLBACK_CONFIRMATION"
    assert next(dec for dec in target.decisions if dec.gate_name == "StockPullbackEntryGate").details["recovery_confirmed"] is False
    db.close()


def test_data_insufficient_is_temporary_and_distinct_from_final_block(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    map_stock(repo, "111111", "robot")
    feed_index(index_store, "KOSDAQ")

    target = result_for(
        build_pipeline(repo, store, builder, index_store).evaluate([candidate("111111")]),
        "111111",
        "robot",
    )

    assert target.final_grade == "C"
    assert target.block_type.value == "temporary"
    assert target.can_recover is True
    assert target.details["sub_status"] == "DATA_INSUFFICIENT"
    assert "DATA_INSUFFICIENT_CAP" in target.details["cap_rules_applied"]
    db.close()


def test_hard_cap_overrides_high_score_on_chase_risk(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    store = MarketDataStore()
    builder = CandleBuilder()
    index_store = MarketIndexStore()
    for code in ["111111", "222222", "333333"]:
        map_stock(repo, code, "robot")
        feed_stock(store, builder, code, low=9_800, current=9_970, cum_base=3_000)
    feed_index(index_store, "KOSDAQ")

    target = result_for(
        build_pipeline(repo, store, builder, index_store).evaluate(
            [candidate("111111"), candidate("222222"), candidate("333333")]
        ),
        "111111",
        "robot",
    )

    assert target.final_grade == "C"
    assert "CHASE_RISK_CAP" in target.details["cap_rules_applied"]
    assert target.final_score > 55
    db.close()
