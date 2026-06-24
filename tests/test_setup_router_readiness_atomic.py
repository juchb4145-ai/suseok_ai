from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline

from tests.test_setup_router_runtime import TRADE_DATE, _WaitSubscriptionProvider
from tests.test_setup_router_v3 import _candidate, _context, _entry_decision, _seed_candles


def test_readiness_wait_completion_is_atomic_and_not_shape_success(tmp_path):
    db = TradingDatabase(str(tmp_path / "readiness-atomic.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)
    db.save_strategy_context_snapshot({**_context(), "candidate_id": candidate.id})
    db.save_entry_decisions([{**_entry_decision(), "candidate_id": candidate.id}])

    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        subscription_readiness_provider=_WaitSubscriptionProvider(),
        config=SetupRouterConfig(enabled=True, save_history=True),
    )
    summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))

    readiness = db.list_setup_router_readiness_latest(trade_date=TRADE_DATE)[0]
    runtime = db.list_setup_router_candidate_runtime(trade_date=TRADE_DATE, candidate_instance_ids=["ci-000001"])[0]
    pending = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE, statuses=("COMPLETED",))[0]
    commits = db.conn.execute(
        """
        SELECT *
        FROM setup_router_evaluation_commits_v1
        WHERE trade_date = ? AND router_version = 'setup_router_v3.5.2'
        """,
        (TRADE_DATE,),
    ).fetchall()

    assert summary["readiness_commit_count"] == 1
    assert summary["shape_commit_count"] == 0
    assert readiness["readiness_processed"] is True
    assert readiness["shape_evaluated"] is False
    assert readiness["readiness_commit_id"]
    assert runtime["processed_readiness_fingerprint"] == readiness["readiness_fingerprint"]
    assert runtime["last_shape_success_at"] == ""
    assert pending["status"] == "COMPLETED"
    assert len(commits) == 1
    assert commits[0]["evaluation_kind"] == "READINESS_ONLY"
    assert commits[0]["shape_evaluated"] == 0
    assert commits[0]["shape_result_count"] == 0
    assert db.list_setup_observations_latest(trade_date=TRADE_DATE) == []


def test_readiness_atomic_rejects_stale_pending_epoch(tmp_path):
    db = TradingDatabase(str(tmp_path / "readiness-stale-epoch.db"))
    pending = {
        "trade_date": TRADE_DATE,
        "candidate_instance_id": "ci-1",
        "code": "000001",
        "router_version": "setup_router_v3.5.2",
        "state_version": "setup_router_v3.state.v3.2",
        "pending_reasons": ["READINESS_CHANGED"],
        "first_pending_at": "2026-06-22T09:04:00",
        "last_pending_at": "2026-06-22T09:04:00",
    }
    db.save_setup_router_pending_evaluations([pending])
    selected = db.list_setup_router_pending_evaluations(trade_date=TRADE_DATE)[0]
    db.update_setup_router_pending_evaluations([{**selected, "status": "SELECTED", "selected_at": "2026-06-22T09:04:01"}])

    result = db.complete_setup_router_readiness_evaluation_atomic(
        trade_date=TRADE_DATE,
        router_version="setup_router_v3.5.2",
        candidate_instance_id="ci-1",
        pending_epoch=int(selected["pending_epoch"]) + 1,
        pending_instance_id=selected["pending_instance_id"],
        readiness_snapshot={
            "trade_date": TRADE_DATE,
            "candidate_instance_id": "ci-1",
            "candidate_id": 1,
            "code": "000001",
            "readiness_status": "WAIT_SUBSCRIPTION_NOT_ACTIVE",
            "readiness_ready": False,
            "readiness_fingerprint": "rf-stale",
            "calculated_at": "2026-06-22T09:05:00",
        },
        runtime_update={
            "trade_date": TRADE_DATE,
            "candidate_instance_id": "ci-1",
            "code": "000001",
            "router_version": "setup_router_v3.5.2",
            "last_readiness_status": "WAIT_SUBSCRIPTION_NOT_ACTIVE",
            "processed_readiness_fingerprint": "rf-stale",
        },
        evaluation_commit={"readiness_fingerprint": "rf-stale"},
    )

    assert result["status"] == "STALE_PENDING_EPOCH"
    assert db.list_setup_router_readiness_latest(trade_date=TRADE_DATE) == []
    assert db.conn.execute("SELECT COUNT(*) FROM setup_router_evaluation_commits_v1").fetchone()[0] == 0
