from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.candles import CandleBuilder
from trading.strategy.dirty_strategy_evaluator import DirtyStrategyEvaluator, DirtyStrategyEvaluatorConfig
from trading.strategy.entry_engine import EntryDecisionStatus, EntryEngine, EntryEngineConfig
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_data_service import DirtyReason, MarketDataService, MarketDataServiceConfig
from trading.strategy.models import CandidateState
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.runtime import StrategyRuntimeConfig


TRADE_DATE = "2026-06-18"
OPEN_AT = datetime(2026, 6, 18, 9, 5, 0)


def test_dirty_code_evaluator_evaluates_only_dirty_candidate(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    _candidate(db, "005901")
    _candidate(db, "005902")
    _ready_ticks(service, "005901", [1030, 1000, 995, 1005])
    _ready_ticks(service, "005902", [1000, 990, 985, 980])
    service.dirty_queue.clear()
    service.dirty_queue.mark_dirty("005901", DirtyReason.PRICE_TICK)

    result = _evaluator(db, market_data, candles, service).evaluate_dirty(now=OPEN_AT)

    assert result.evaluated_code_count == 1
    assert result.evaluated_candidate_count == 1
    assert db.load_candidate(TRADE_DATE, "005901").metadata["entry_status"] == EntryDecisionStatus.OBSERVE_READY.value
    assert "entry_status" not in db.load_candidate(TRADE_DATE, "005902").metadata
    assert db.list_runtime_order_intents(limit=10) == []


def test_dirty_code_absent_cycle_does_not_call_entry_engine(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    evaluator = _evaluator(db, market_data, candles, service)

    def fail(*args, **kwargs):
        raise AssertionError("entry engine should not run without dirty codes")

    evaluator.entry_engine.evaluate_candidates = fail
    evaluator.entry_engine.build = fail

    result = evaluator.evaluate_dirty(now=OPEN_AT)

    assert result.status == "IDLE"
    assert result.evaluated_candidate_count == 0


def test_candidate_debounce_skips_repeated_dirty_evaluation(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    _candidate(db, "005903")
    _ready_ticks(service, "005903", [1030, 1000, 995, 1005])
    service.dirty_queue.clear()
    evaluator = _evaluator(db, market_data, candles, service, debounce_ms=200)

    service.dirty_queue.mark_dirty("005903", DirtyReason.PRICE_TICK)
    first = evaluator.evaluate_dirty(now=OPEN_AT)
    service.dirty_queue.mark_dirty("005903", DirtyReason.DATA_QUALITY_CHANGED, marked_at=OPEN_AT + timedelta(milliseconds=50))
    second = evaluator.evaluate_dirty(now=OPEN_AT + timedelta(milliseconds=50))

    assert first.evaluated_candidate_count == 1
    assert second.evaluated_candidate_count == 0
    assert second.debounced_count == 1


def test_dirty_evaluator_syncs_fsm_timing_ready_without_order_intent(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    _candidate(db, "005904")
    _ready_ticks(service, "005904", [1030, 1000, 995, 1005])
    service.dirty_queue.clear()
    service.dirty_queue.mark_dirty("005904", DirtyReason.PRICE_TICK)

    _evaluator(db, market_data, candles, service).evaluate_dirty(now=OPEN_AT)
    reloaded = db.load_candidate(TRADE_DATE, "005904")
    fsm = reloaded.metadata["candidate_fsm"]

    assert fsm["v2_state"] == "TIMING_READY"
    assert fsm["blocking_stage"] == "NONE"
    assert fsm["primary_reason_code"] == "OBSERVE_READY_ORDER_DISABLED"
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_dirty_evaluator_keeps_tr_backfill_as_data_block(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    _candidate(db, "005905")
    _ready_ticks(service, "005905", [1000, 990, 985, 980], metadata={"price_source": "TR_BACKFILL"})
    service.dirty_queue.clear()
    service.dirty_queue.mark_dirty("005905", DirtyReason.PRICE_TICK)

    _evaluator(db, market_data, candles, service).evaluate_dirty(now=OPEN_AT)
    fsm = db.load_candidate(TRADE_DATE, "005905").metadata["candidate_fsm"]

    assert fsm["v2_state"] == "WATCHING"
    assert fsm["blocking_stage"] == "DATA"
    assert fsm["primary_reason_code"] in {"LATEST_TICK_MISSING", "TR_PRICE_ONLY_NOT_READY", "TR_BACKFILL_PRICE_ONLY"}


def test_market_regime_dirty_reason_evaluates_active_candidates(tmp_path):
    db, market_data, candles, service = _context(tmp_path)
    _candidate(db, "005906")
    _candidate(db, "005907")
    _ready_ticks(service, "005906", [1030, 1000, 995, 1005])
    _ready_ticks(service, "005907", [1000, 990, 985, 980])
    service.dirty_queue.clear()
    service.dirty_queue.mark_dirty("000000", DirtyReason.MARKET_REGIME_CHANGED)

    result = _evaluator(db, market_data, candles, service).evaluate_dirty(now=OPEN_AT)

    assert result.evaluated_candidate_count == 2


def test_reboot_v2_snapshot_includes_dirty_evaluator_and_skips_full_scan_when_enabled(tmp_path):
    db, market_data, candles, service = _context(tmp_path)

    class _Client:
        def register_realtime_records(self, records, screen_no=""):
            return None

        def remove_realtime(self, codes, screen_no=""):
            return None

    class _EntryPipeline:
        config = type("Config", (), {"enabled": True})()

        def run_if_due(self, now=None):
            raise AssertionError("full scan entry pipeline should be shadowed by dirty evaluator")

    dirty = _evaluator(db, market_data, candles, service)
    runtime = RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(_Client()),
        candle_builder=candles,
        market_data=market_data,
        market_index_store=None,
        config=StrategyRuntimeConfig(),
        entry_engine_pipeline=_EntryPipeline(),
        dirty_strategy_evaluator=dirty,
    )

    snapshot = runtime.cycle(OPEN_AT)

    assert snapshot["dirty_evaluator"]["status"] == "IDLE"
    assert snapshot["entry_engine"]["status"] == "SHADOWED_BY_DIRTY_EVALUATOR"


def _context(tmp_path):
    db = TradingDatabase(str(tmp_path / "dirty_eval.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    clock_now = [OPEN_AT]
    service = MarketDataService(
        market_data,
        candles,
        config=MarketDataServiceConfig(dirty_debounce_ms=0),
        clock=lambda: clock_now[0],
    )
    service._test_clock_now = clock_now
    return db, market_data, candles, service


def _evaluator(db, market_data, candles, service, *, debounce_ms: int = 0):
    return DirtyStrategyEvaluator(
        db=db,
        market_data_service=service,
        entry_engine=EntryEngine(
            db,
            market_data=market_data,
            candle_builder=candles,
            config=EntryEngineConfig(enabled=True),
            clock=lambda: OPEN_AT,
        ),
        config=DirtyStrategyEvaluatorConfig(debounce_ms=debounce_ms),
        clock=lambda: OPEN_AT,
    )


def _candidate(db: TradingDatabase, code: str):
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date=TRADE_DATE,
            code=code,
            name=f"Stock {code}",
            source_type="condition_search",
            source_id=f"condition:{code}",
            source_score=50.0,
            theme_id="theme-a",
            theme_name="Theme A",
            detected_at=f"{TRADE_DATE}T09:01:00",
        )
    ).candidate
    candidate.state = CandidateState.WATCHING
    candidate.metadata.update(
        {
            "theme_board_theme_id": "theme-a",
            "theme_board_theme_name": "Theme A",
            "theme_board_theme_status": "LEADING_THEME",
            "theme_board_theme_score": 80.0,
            "theme_board_stock_role": "LEADER",
            "theme_board_stock_score": 80.0,
            "market_side": "KOSPI",
            "market_regime_status": "EXPANSION",
            "global_market_regime_status": "EXPANSION",
            "market_action": "ALLOW_NORMAL",
            "market_position_size_multiplier_hint": 1.0,
            "market_block_new_entry": False,
        }
    )
    return db.save_candidate(candidate)


def _ready_ticks(service: MarketDataService, code: str, prices: list[int], *, metadata: dict | None = None) -> None:
    start = OPEN_AT - timedelta(minutes=len(prices) - 1)
    for index, price in enumerate(prices):
        timestamp = start + timedelta(minutes=index)
        service._test_clock_now[0] = timestamp
        payload = {
            "code": code,
            "price": price,
            "change_rate": 2.0,
            "cum_volume": (index + 1) * 1000,
            "trade_value": 1_000_000_000,
            "spread_ticks": 1,
            "day_high": max(prices),
            "day_low": min(prices),
            "trade_time": timestamp.strftime("%H%M%S"),
            "timestamp": timestamp.isoformat(),
            "metadata": dict(metadata or {}) if index == len(prices) - 1 else {},
        }
        service.handle_price_tick(payload)
