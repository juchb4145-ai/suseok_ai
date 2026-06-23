from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline

from tests.test_setup_router_v3 import TRADE_DATE, _context, _entry_decision


def test_deferred_pending_keeps_processed_signature_until_success(tmp_path):
    db = TradingDatabase(str(tmp_path / "pending-queue.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    first = _candidate(db, "000001", "ci-1")
    second = _candidate(db, "000002", "ci-2")
    _seed(market_data, candles, "000001")
    _seed(market_data, candles, "000002")
    db.save_strategy_context_snapshot({**_context(), "code": "000001", "candidate_id": first.id})
    db.save_strategy_context_snapshot({**_context(), "code": "000002", "candidate_id": second.id})
    db.save_entry_decisions(
        [
            {**_entry_decision(), "candidate_id": first.id, "code": "000001"},
            {**_entry_decision(), "candidate_id": second.id, "code": "000002"},
        ]
    )
    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        config=SetupRouterConfig(enabled=True, save_history=True, max_candidates_per_cycle=1, interval_sec=0.1),
    )

    first_summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))
    runtime = {
        row["candidate_instance_id"]: row
        for row in db.list_setup_router_candidate_runtime(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1", "ci-2"])
    }
    pending = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)

    assert first_summary["evaluated_count"] == 1
    assert first_summary["deferred_pending_count"] == 1
    assert runtime["ci-2"]["observed_entry_signature"]
    assert runtime["ci-2"]["processed_entry_signature"] == ""
    assert any(row["candidate_instance_id"] == "ci-2" and row["status"] == "PENDING" for row in pending)

    second_summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 12))
    runtime = {
        row["candidate_instance_id"]: row
        for row in db.list_setup_router_candidate_runtime(trade_date=TRADE_DATE, candidate_instance_ids=["ci-1", "ci-2"])
    }

    assert second_summary["evaluated_count"] == 1
    assert runtime["ci-2"]["processed_entry_signature"] == runtime["ci-2"]["observed_entry_signature"]
    assert runtime["ci-2"]["last_success_at"]


def test_stale_selected_pending_recovers_to_retry_and_evaluates(tmp_path):
    db = TradingDatabase(str(tmp_path / "selected-recovery.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db, "000001", "ci-1")
    _seed(market_data, candles, "000001")
    db.save_strategy_context_snapshot({**_context(), "code": "000001", "candidate_id": candidate.id})
    db.save_entry_decisions([{**_entry_decision(), "candidate_id": candidate.id, "code": "000001"}])
    pending = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.4.1",
        "state_version": "setup_router_v3.state.v3.1",
        "selected_theme_id": "ai",
        "pending_reasons": ["ENTRY_DECISION_CHANGED"],
        "first_pending_at": "2026-06-22T09:04:00",
        "last_pending_at": "2026-06-22T09:04:00",
    }
    db.save_setup_router_pending_evaluations([pending])
    db.update_setup_router_pending_evaluations([{**pending, "status": "SELECTED", "selected_at": "2026-06-22T09:04:00", "last_attempt_at": "2026-06-22T09:04:00"}])
    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        config=SetupRouterConfig(enabled=True, save_history=True, max_candidates_per_cycle=1, selected_lease_sec=30, interval_sec=0.1),
    )

    summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))
    completed = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("COMPLETED",))[0]

    assert summary["selected_lease_expired_count"] == 1
    assert summary["evaluated_count"] == 1
    assert completed["status"] == "COMPLETED"
    assert "SELECTED_LEASE_EXPIRED" in completed["pending_reasons"]


def test_orphan_pending_is_superseded_without_candidate_side_effect(tmp_path):
    db = TradingDatabase(str(tmp_path / "orphan-pending.db"))
    pending = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "missing-ci",
        "code": "000999",
        "router_version": "setup_router_v3.4.1",
        "state_version": "setup_router_v3.state.v3.1",
        "pending_reasons": ["PERIODIC_RECONCILE"],
        "first_pending_at": "2026-06-22T09:04:00",
        "last_pending_at": "2026-06-22T09:04:00",
    }
    db.save_setup_router_pending_evaluations([pending])
    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=MarketDataStore(),
        candle_builder=CandleBuilder(),
        config=SetupRouterConfig(enabled=True, save_history=True, max_candidates_per_cycle=1, interval_sec=0.1),
    )

    summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))
    superseded = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("SUPERSEDED",))[0]

    assert summary["superseded_pending_count"] == 1
    assert summary["evaluated_count"] == 0
    assert superseded["status"] == "SUPERSEDED"
    assert "CANDIDATE_NOT_FOUND" in superseded["pending_reasons"]


def _candidate(db, code, instance_id):
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code=code,
            name=f"테스트{code}",
            state=CandidateState.WATCHING,
            sources=[CandidateSourceType.CONDITION_SEARCH],
            metadata={"candidate_instance_id": instance_id},
        )
    )


def _seed(market_data, candles, code):
    start = datetime(2026, 6, 22, 9, 0, 0)
    closes = [980, 970, 960, 1002, 1008]
    for index, close in enumerate(closes):
        tick = StrategyTick.from_realtime(
            code,
            price=close,
            change_rate=5.0,
            cum_volume=1000 + index * 100,
            trade_value=1_000_000_000 + index * 100_000_000,
            execution_strength=130,
            timestamp=start + timedelta(minutes=index),
            metadata={"vwap": 1000, "price_source": "REALTIME"},
        )
        market_data.update_tick(tick)
        candles.update(tick)
    last = StrategyTick.from_realtime(
        code,
        price=closes[-1],
        change_rate=5.0,
        cum_volume=2000,
        trade_value=1_500_000_000,
        execution_strength=130,
        timestamp=start + timedelta(minutes=len(closes), seconds=5),
        metadata={"vwap": 1000, "price_source": "REALTIME"},
    )
    market_data.update_tick(last)
    candles.update(last)
