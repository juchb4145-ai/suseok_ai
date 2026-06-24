from datetime import datetime
from types import SimpleNamespace

from storage.db import TradingDatabase
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline

from tests.test_setup_router_v3 import _candidate, _context, _entry_decision, _seed_candles
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore


TRADE_DATE = "2026-06-22"


class _ReadySubscriptionProvider:
    def snapshots(self, codes, *, context_by_code=None, candidate_by_code=None, now=None):
        return {
            code: {
                "code": code,
                "calculated_at": "2026-06-22T09:05:10",
                "subscription_selected": True,
                "subscription_active": True,
                "subscription_sources": ["reboot_v2_candidate"],
                "subscription_primary_source": "reboot_v2_candidate",
                "subscription_screen_no": "7000",
                "subscription_generation": 1,
                "subscription_active_since": "2026-06-22T09:04:00",
                "relevant_source_added_at": "2026-06-22T09:04:00",
                "coverage_type": "CANDIDATE",
                "latest_tick_at": "2026-06-22T09:05:05",
                "latest_tick_age_sec": 5.0,
                "latest_tick_source": "REALTIME",
                "post_subscription_tick_verified": True,
                "core_tick_at": "2026-06-22T09:05:05",
            }
            for code in codes
        }


class _WaitSubscriptionProvider:
    def snapshots(self, codes, *, context_by_code=None, candidate_by_code=None, now=None):
        return {
            code: {
                "code": code,
                "calculated_at": "2026-06-22T09:05:10",
                "subscription_selected": False,
                "subscription_active": False,
                "subscription_sources": [],
                "coverage_type": "NONE",
            }
            for code in codes
        }


def test_setup_router_runtime_saves_observe_only_outputs_without_candidate_mutation(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-runtime.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[980, 970, 960, 1002, 1008], vwap=1000)
    db.save_strategy_context_snapshot({**_context(), "candidate_id": candidate.id})
    db.save_entry_decisions([{**_entry_decision(), "candidate_id": candidate.id}])
    before_state = db.load_candidate(TRADE_DATE, "000001").state

    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        subscription_readiness_provider=_ReadySubscriptionProvider(),
        config=SetupRouterConfig(enabled=True, save_history=True),
    )
    summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))

    after = db.load_candidate(TRADE_DATE, "000001")
    latest = db.list_setup_observations_latest(trade_date=TRADE_DATE, router_status="VALID_OBSERVE")
    subscription_readiness = db.list_realtime_subscription_readiness_latest(trade_date=TRADE_DATE)

    assert summary["enabled"] is True
    assert summary["valid_observe_count"] >= 1
    assert summary["readiness_ready_count"] >= 1
    assert summary["shape_evaluated_candidate_count"] >= 1
    assert summary["router_version"] == "setup_router_v3.5"
    assert summary["state_write_count"] >= 1
    assert latest
    assert subscription_readiness
    assert subscription_readiness[0]["subscription_active"] is True
    assert subscription_readiness[0]["post_subscription_tick_verified"] is True
    assert all(item["order_intent_allowed"] is False for item in latest)
    assert after.state == before_state
    assert "setup_router_v3" not in after.metadata


def test_setup_router_runtime_data_wait_saves_candidate_readiness_without_setup_rows(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-runtime-data-wait.db"))
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

    readiness = db.list_setup_router_readiness_latest(trade_date=TRADE_DATE)
    observations = db.list_setup_observations_latest(trade_date=TRADE_DATE)

    assert summary["readiness_evaluated_count"] == 1
    assert summary["readiness_wait_count"] == 1
    assert summary["shape_evaluated_candidate_count"] == 0
    assert summary["data_wait_count"] == 1
    assert len(readiness) == 1
    assert readiness[0]["readiness_status"] == "WAIT_SUBSCRIPTION_NOT_ACTIVE"
    assert observations == []


def test_setup_router_scheduler_keeps_periodic_cursor_when_p0_uses_budget(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-runtime-scheduler.db"))
    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        config=SetupRouterConfig(enabled=True, max_candidates_per_cycle=1),
    )
    candidates = [
        SimpleNamespace(trade_date=TRADE_DATE, code="000001", id=1, metadata={"candidate_instance_id": "ci-1"}),
        SimpleNamespace(trade_date=TRADE_DATE, code="000002", id=2, metadata={"candidate_instance_id": "ci-2"}),
    ]

    periodic = pipeline._periodic_candidates(candidates)
    queue = pipeline._evaluation_queue(
        candidates,
        incremental_codes={"000002"},
        context_codes=set(),
        ttl_codes=set(),
        periodic_candidates=periodic,
    )
    selected, deferred, depth = pipeline._select_evaluation_entries(queue, {}, datetime(2026, 6, 22, 9, 5, 0))

    assert [item["candidate"].code for item in selected] == ["000002"]
    assert any(item["candidate"].code == "000001" and item["priority"] == 3 for item in deferred)
    assert pipeline.reconcile_cursor == 0
    assert depth["0"] == 1
    assert depth["3"] == 1


def test_setup_router_scheduler_prioritizes_oldest_last_evaluated_within_p0(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-runtime-oldest.db"))
    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        config=SetupRouterConfig(enabled=True, max_candidates_per_cycle=1),
    )
    candidates = [
        SimpleNamespace(trade_date=TRADE_DATE, code="000001", id=1, metadata={"candidate_instance_id": "ci-1"}),
        SimpleNamespace(trade_date=TRADE_DATE, code="000002", id=2, metadata={"candidate_instance_id": "ci-2"}),
    ]
    runtime = {
        "ci-1": {"last_evaluated_at": "2026-06-22T09:00:00"},
        "ci-2": {"last_evaluated_at": "2026-06-22T09:04:00"},
    }
    queue = pipeline._evaluation_queue(
        candidates,
        incremental_codes={"000001", "000002"},
        context_codes=set(),
        ttl_codes=set(),
        periodic_candidates=[],
    )

    selected, deferred, _depth = pipeline._select_evaluation_entries(queue, runtime, datetime(2026, 6, 22, 9, 5, 0))

    assert [item["candidate"].code for item in selected] == ["000001"]
    assert [item["candidate"].code for item in deferred] == ["000002"]
