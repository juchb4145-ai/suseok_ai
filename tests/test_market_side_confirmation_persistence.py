from __future__ import annotations

from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import IndexTick, MarketIndexStore
from trading.theme_engine.lab import (
    MarketSide,
    MarketSideBreadthConfig,
    MarketSideGateConfirmationConfig,
    ThemeLabConfig,
    ThemeLabFlowEngine,
    ThemeStatusThresholds,
)
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime_pipeline import MarketSessionConfig, MarketSideConfirmationPersistenceConfig, ThemeLabRuntimePipeline

REGULAR_SESSION_ID = "2026-06-01:regular"


def test_runtime_restart_restores_weak_confirmation_cycles(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    first_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, first_at)
    _set_weak_kosdaq_ticks(market_data, first_at)

    first = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    first_result = first.run(first_at)

    first_state = db.load_market_side_confirmation_states(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        state_version=1,
    )
    kosdaq_first = next(item for item in first_state if item["market_side"] == MarketSide.KOSDAQ.value)
    assert kosdaq_first["weak_consecutive_cycles"] == 1
    assert kosdaq_first["confirmed_status"] == "CHOPPY"
    assert first_result.market.kosdaq_confirmation_pending is True

    second_at = first_at + timedelta(minutes=1)
    _set_index(market_index, second_at)
    _set_weak_kosdaq_ticks(market_data, second_at)
    restarted = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    second_result = restarted.run(second_at)
    decision = next(item for item in second_result.gate_decisions if item.symbol == "000001")
    kosdaq_second = next(
        item
        for item in db.load_market_side_confirmation_states(
            trade_date="2026-06-01",
            session_id=REGULAR_SESSION_ID,
            state_version=1,
        )
        if item["market_side"] == MarketSide.KOSDAQ.value
    )
    transitions = db.list_market_side_confirmation_transitions(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        market_side=MarketSide.KOSDAQ.value,
    )

    assert kosdaq_second["weak_consecutive_cycles"] == 2
    assert kosdaq_second["confirmed_status"] == "WEAK"
    assert second_result.market.kosdaq_confirmed_status.value == "WEAK"
    assert decision.market_confirmation_state_restored is True
    assert decision.market_confirmation_state_persisted is True
    assert decision.market_confirmation_state_source == "restored_db"
    assert "MARKET_CONFIRMATION_STATE_RESTORED" in decision.market_side_reason_codes
    assert "WAIT_CANDIDATE_MARKET_WEAK" in decision.reason_codes
    assert any(item["transition_type"] == "WEAK_CONFIRMED" for item in transitions)


def test_candidate_universe_state_persistence_is_side_isolated(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    now = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, now)
    _set_weak_kosdaq_ticks(market_data, now)

    pipeline = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    pipeline.run(now)
    rows = db.load_market_side_confirmation_states(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        state_version=1,
    )
    by_side = {item["market_side"]: item for item in rows}

    assert by_side[MarketSide.KOSDAQ.value]["weak_consecutive_cycles"] == 1
    assert by_side[MarketSide.KOSPI.value]["confirmed_status"] in {"CHOPPY", "SELECTIVE", "EXPANSION"}


def test_stale_market_confirmation_state_is_rejected_and_reset_logged(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    first_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, first_at)
    _set_weak_kosdaq_ticks(market_data, first_at)

    first = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    first.run(first_at)
    kosdaq_state = next(
        item
        for item in db.load_market_side_confirmation_states(
                trade_date="2026-06-01",
                session_id=REGULAR_SESSION_ID,
                state_version=1,
        )
        if item["market_side"] == MarketSide.KOSDAQ.value
    )
    stale_state = {
        **kosdaq_state,
        "confirmed_status": "WEAK",
        "weak_consecutive_cycles": 2,
        "confirmation_pending": False,
        "updated_at": (first_at - timedelta(minutes=30)).isoformat(),
        "created_at": (first_at - timedelta(minutes=30)).isoformat(),
        "expires_at": (first_at - timedelta(minutes=1)).isoformat(),
    }
    db.upsert_market_side_confirmation_state(stale_state)

    second_at = first_at + timedelta(minutes=1)
    _set_index(market_index, second_at)
    _set_weak_kosdaq_ticks(market_data, second_at)
    restarted = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    result = restarted.run(second_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")
    transitions = db.list_market_side_confirmation_transitions(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        market_side=MarketSide.KOSDAQ.value,
    )

    assert result.market.kosdaq_confirmed_status.value == "CHOPPY"
    assert result.market.kosdaq_confirmation_pending is True
    assert decision.market_confirmation_state_restored is False
    assert decision.market_confirmation_state_reset_reason == "MARKET_CONFIRMATION_STATE_EXPIRED"
    assert "MARKET_CONFIRMATION_STATE_EXPIRED" in decision.market_side_reason_codes
    assert any(item["transition_type"] == "RESET_EXPIRED" for item in transitions)


def test_version_mismatched_market_confirmation_state_is_rejected(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    first_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, first_at)
    _set_weak_kosdaq_ticks(market_data, first_at)

    first = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    first.run(first_at)

    second_at = first_at + timedelta(minutes=1)
    _set_index(market_index, second_at)
    _set_weak_kosdaq_ticks(market_data, second_at)
    restarted = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
        persistence_config=MarketSideConfirmationPersistenceConfig(state_version=2),
    )
    result = restarted.run(second_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert result.market.kosdaq_confirmed_status.value == "CHOPPY"
    assert result.market.kosdaq_confirmation_pending is True
    assert decision.market_confirmation_state_restored is False
    assert decision.market_confirmation_state_reset_reason == "MARKET_CONFIRMATION_STATE_VERSION_MISMATCH"
    assert "MARKET_CONFIRMATION_STATE_VERSION_MISMATCH" in decision.market_side_reason_codes


def test_restore_db_error_uses_conservative_memory_fallback(monkeypatch, tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    now = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, now)
    _set_healthy_ticks(market_data, now)

    def broken_load(*, trade_date: str, session_id: str, state_version: int):
        raise RuntimeError("restore unavailable")

    monkeypatch.setattr(db, "load_market_side_confirmation_states", broken_load)
    pipeline = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    result = pipeline.run(now)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert decision.status == "WAIT"
    assert decision.candidate_market_confirmation_pending is True
    assert decision.market_confirmation_state_source == "db_failed_memory_fallback"
    assert decision.market_confirmation_state_restore_reason == "MARKET_CONFIRMATION_STATE_DB_ERROR"
    assert "MARKET_CONFIRMATION_STATE_DB_ERROR" in decision.market_side_reason_codes
    assert "MARKET_CONFIRMATION_STATE_CONSERVATIVE_FALLBACK" in decision.market_side_reason_codes
    assert "WAIT_MARKET_CONFIRMATION_PENDING" in decision.reason_codes


def test_same_cycle_persistence_is_idempotent(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    now = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, now)
    _set_weak_kosdaq_ticks(market_data, now)

    pipeline = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    )
    pipeline.run(now)
    first_transitions = db.list_market_side_confirmation_transitions(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        market_side=MarketSide.KOSDAQ.value,
    )
    pipeline.run(now)
    second_state = next(
        item
        for item in db.load_market_side_confirmation_states(
            trade_date="2026-06-01",
            session_id=REGULAR_SESSION_ID,
            state_version=1,
        )
        if item["market_side"] == MarketSide.KOSDAQ.value
    )
    second_transitions = db.list_market_side_confirmation_transitions(
        trade_date="2026-06-01",
        session_id=REGULAR_SESSION_ID,
        market_side=MarketSide.KOSDAQ.value,
    )

    assert second_state["weak_consecutive_cycles"] == 1
    assert len(second_transitions) == len(first_transitions)


def test_pre_open_restore_is_skipped_and_blocks_first_cycle(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    regular_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, regular_at)
    _set_weak_kosdaq_ticks(market_data, regular_at)
    ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(regular_at)

    pre_open_at = datetime(2026, 6, 1, 8, 45, 0)
    _set_index(market_index, pre_open_at)
    _set_healthy_ticks(market_data, pre_open_at)
    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(pre_open_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")
    metrics = result.data_quality["market_confirmation_session"]

    assert decision.status == "WAIT"
    assert decision.market_session_id == "2026-06-01:pre_open"
    assert decision.market_session_type == "pre_open"
    assert decision.market_restore_allowed is False
    assert decision.market_confirmation_state_restore_skipped is True
    assert decision.market_confirmation_state_source == "session_boundary_memory_fallback"
    assert "MARKET_SESSION_PRE_OPEN" in decision.market_side_reason_codes
    assert "MARKET_CONFIRMATION_STATE_RESTORE_NOT_ALLOWED" in decision.market_side_reason_codes
    assert "WAIT_MARKET_CONFIRMATION_PENDING" in decision.reason_codes
    assert metrics["market_confirmation_restore_skipped_count"] == 1
    assert metrics["market_confirmation_conservative_fallback_count"] == 1


def test_post_close_restore_is_skipped_and_reset_on_close_logged(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    regular_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, regular_at)
    _set_healthy_ticks(market_data, regular_at)
    ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(regular_at)

    post_close_at = datetime(2026, 6, 1, 15, 45, 0)
    _set_index(market_index, post_close_at)
    _set_healthy_ticks(market_data, post_close_at)
    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(post_close_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")
    transitions = db.list_market_side_confirmation_transitions(
        trade_date="2026-06-01",
        session_id="2026-06-01:post_close",
        market_side=MarketSide.KOSDAQ.value,
    )

    assert decision.status == "WAIT"
    assert decision.market_session_type == "post_close"
    assert decision.market_reset_reason == "MARKET_CONFIRMATION_STATE_RESET_ON_MARKET_CLOSE"
    assert "MARKET_SESSION_POST_CLOSE" in decision.market_side_reason_codes
    assert db.load_market_side_confirmation_states(
        trade_date="2026-06-01",
        session_id="2026-06-01:post_close",
        state_version=1,
    )
    assert any(item["transition_type"] == "SESSION_CLOSE" for item in transitions)


def test_same_trade_date_session_mismatch_is_rejected(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    pre_open_at = datetime(2026, 6, 1, 8, 45, 0)
    _set_index(market_index, pre_open_at)
    _set_weak_kosdaq_ticks(market_data, pre_open_at)
    ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
        session_config=MarketSessionConfig(allow_restore_during_pre_open=True),
        persistence_config=MarketSideConfirmationPersistenceConfig(max_restore_age_sec_pre_open=900),
    ).run(pre_open_at)

    regular_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, regular_at)
    _set_weak_kosdaq_ticks(market_data, regular_at)
    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(regular_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")
    metrics = result.data_quality["market_confirmation_session"]

    assert decision.market_session_id == REGULAR_SESSION_ID
    assert decision.market_confirmation_state_restored is False
    assert decision.market_confirmation_state_reset_reason == "MARKET_CONFIRMATION_STATE_SESSION_MISMATCH"
    assert "MARKET_CONFIRMATION_STATE_SESSION_MISMATCH" in decision.market_side_reason_codes
    assert metrics["market_confirmation_reset_by_reason"]["MARKET_CONFIRMATION_STATE_SESSION_MISMATCH"] == 1


def test_previous_trade_date_state_is_not_restored(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    first_at = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, first_at)
    _set_weak_kosdaq_ticks(market_data, first_at)
    ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(first_at)

    next_trade_at = datetime(2026, 6, 2, 9, 5, 0)
    _set_index(market_index, next_trade_at)
    _set_weak_kosdaq_ticks(market_data, next_trade_at)
    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
    ).run(next_trade_at)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert decision.market_trade_date == "2026-06-02"
    assert decision.market_confirmation_state_restored is False
    assert decision.market_confirmation_state_reset_reason == "MARKET_CONFIRMATION_STATE_DATE_MISMATCH"
    assert "MARKET_CONFIRMATION_STATE_DATE_MISMATCH" in decision.market_side_reason_codes


def test_schedule_unknown_fails_closed_with_conservative_fallback(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    _seed_theme(db)
    market_data = MarketDataStore()
    market_index = MarketIndexStore()
    now = datetime(2026, 6, 1, 9, 5, 0)
    _set_index(market_index, now)
    _set_healthy_ticks(market_data, now)

    result = ThemeLabRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=market_index,
        engine=_engine(),
        session_config=MarketSessionConfig(regular_open="bad"),
    ).run(now)
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")
    metrics = result.data_quality["market_confirmation_session"]

    assert decision.status == "WAIT"
    assert decision.market_schedule_known is False
    assert decision.market_session_type == "closed"
    assert decision.market_confirmation_state_restore_reason == "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN"
    assert "MARKET_CONFIRMATION_STATE_SCHEDULE_UNKNOWN" in decision.market_side_reason_codes
    assert metrics["market_confirmation_schedule_unknown_count"] == 1


def _engine() -> ThemeLabFlowEngine:
    return ThemeLabFlowEngine(
        ThemeLabConfig(
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(
                min_sample_count_kosdaq=4,
                min_sample_count_kospi=2,
                breadth_weak_pct=0.38,
                breadth_risk_off_pct=0.20,
            ),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(weak_confirm_cycles=2, recover_confirm_cycles=2),
        )
    )


def _seed_theme(db: TradingDatabase) -> None:
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(
        CanonicalTheme("ai", "AI", "AI", status=ThemeStatus.ACTIVE, trade_eligible=True)
    )
    for code in ("000001", "000002", "000003", "000004", "100001", "100002"):
        repo.upsert_current_membership(
            ThemeMembership(
                "ai",
                code,
                stock_name=f"stock-{code}",
                membership_score=1.0,
                active=True,
                trade_eligible=True,
            )
        )


def _set_index(store: MarketIndexStore, now: datetime) -> None:
    store.update_index_tick(IndexTick.from_realtime("KOSPI", "KOSPI", 2500, 0.2, timestamp=now))
    store.update_index_tick(IndexTick.from_realtime("KOSDAQ", "KOSDAQ", 850, 0.2, timestamp=now))


def _set_weak_kosdaq_ticks(store: MarketDataStore, now: datetime) -> None:
    ticks = [
        ("000001", 105, 5.0, "KOSDAQ", 108),
        ("000002", 99, -1.0, "KOSDAQ", 101),
        ("000003", 98, -1.2, "KOSDAQ", 101),
        ("000004", 99, -0.7, "KOSDAQ", 101),
        ("100001", 101, 0.5, "KOSPI", 102),
        ("100002", 100, 0.2, "KOSPI", 101),
    ]
    for code, price, change_rate, market, day_high in ticks:
        store.update_tick(
            StrategyTick.from_realtime(
                code,
                price,
                change_rate=change_rate,
                cum_volume=10_000,
                trade_value=10_000_000,
                execution_strength=120.0,
                timestamp=now,
                metadata={
                    "prev_close": 100,
                    "name": f"stock-{code}",
                    "market": market,
                    "day_high": day_high,
                    "session_high": day_high,
                },
            )
        )


def _set_healthy_ticks(store: MarketDataStore, now: datetime) -> None:
    ticks = [
        ("000001", 105, 5.0, "KOSDAQ", 108),
        ("000002", 103, 3.0, "KOSDAQ", 104),
        ("000003", 102, 2.0, "KOSDAQ", 103),
        ("000004", 101, 1.0, "KOSDAQ", 102),
        ("100001", 101, 0.5, "KOSPI", 102),
        ("100002", 100, 0.2, "KOSPI", 101),
    ]
    for code, price, change_rate, market, day_high in ticks:
        store.update_tick(
            StrategyTick.from_realtime(
                code,
                price,
                change_rate=change_rate,
                cum_volume=10_000,
                trade_value=10_000_000,
                execution_strength=120.0,
                timestamp=now,
                metadata={
                    "prev_close": 100,
                    "name": f"stock-{code}",
                    "market": market,
                    "day_high": day_high,
                    "session_high": day_high,
                },
            )
        )
