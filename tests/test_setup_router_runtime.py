from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading.strategy.setup_runtime import SetupRouterV3RuntimePipeline

from tests.test_setup_router_v3 import _candidate, _context, _entry_decision, _seed_candles
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore


TRADE_DATE = "2026-06-22"


def test_setup_router_runtime_saves_observe_only_outputs_without_candidate_mutation(tmp_path):
    db = TradingDatabase(str(tmp_path / "setup-runtime.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    candidate = _candidate(db)
    _seed_candles(market_data, candles, closes=[990, 995, 998, 1002, 1008], vwap=1000)
    db.save_strategy_context_snapshot({**_context(), "candidate_id": candidate.id})
    db.save_entry_decisions([{**_entry_decision(), "candidate_id": candidate.id}])
    before_state = db.load_candidate(TRADE_DATE, "000001").state

    pipeline = SetupRouterV3RuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        config=SetupRouterConfig(enabled=True, save_history=True),
    )
    summary = pipeline.run(datetime(2026, 6, 22, 9, 5, 10))

    after = db.load_candidate(TRADE_DATE, "000001")
    latest = db.list_setup_observations_latest(trade_date=TRADE_DATE, router_status="VALID_OBSERVE")

    assert summary["enabled"] is True
    assert summary["valid_observe_count"] >= 1
    assert latest
    assert all(item["order_intent_allowed"] is False for item in latest)
    assert after.state == before_state
    assert "setup_router_v3" not in after.metadata
