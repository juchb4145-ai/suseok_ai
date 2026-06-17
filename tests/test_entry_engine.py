from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry_engine import (
    EntryDecisionStatus,
    EntryEngine,
    EntryEngineConfig,
    EntryEngineRuntimePipeline,
    PriceLocationStatus,
    entry_engine_dashboard_section,
)
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import CandidateState
from trading_app.api import build_candidates_snapshot, build_dashboard_snapshot


TRADE_DATE = "2026-06-18"
OPEN_AT = datetime(2026, 6, 18, 9, 5, 0)


def test_data_ready_failure_becomes_data_wait(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000001")

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.DATA_WAIT
    assert "LATEST_TICK_MISSING" in decision.reason_codes
    assert db.list_runtime_order_intents(limit=10) == []


def test_tr_price_only_without_realtime_tick_is_not_observe_ready(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000002")
    _ready_ticks(market_data, candles, "000002", [1000, 990, 985, 980], metadata={"price_source": "TR_BACKFILL"})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.DATA_WAIT
    assert "TR_PRICE_ONLY_NOT_READY" in decision.reason_codes


def test_leading_leader_allow_normal_vwap_reclaim_observe_ready(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000003", role="LEADER", theme_status="LEADING_THEME", market_action="ALLOW_NORMAL", market_status="EXPANSION")
    _ready_ticks(market_data, candles, "000003", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.OBSERVE_READY
    assert decision.price_location == PriceLocationStatus.VWAP_RECLAIM.value
    assert decision.ready_allowed is True
    assert decision.live_order_allowed is False


def test_leader_only_theme_follower_is_blocked(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000004", role="FOLLOWER", theme_status="LEADER_ONLY_THEME")
    _ready_ticks(market_data, candles, "000004", [1000, 990, 985, 980], metadata={"recent_support": 978})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "THEME_LEADER_ONLY_FOLLOWER_BLOCK" in decision.reason_codes


def test_risk_off_candidate_is_hard_block_without_deleting_candidate(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000005", market_action="BLOCK_NEW_ENTRY", market_status="RISK_OFF")
    _ready_ticks(market_data, candles, "000005", [1000, 990, 985, 980], metadata={"recent_support": 978})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]
    reloaded = db.load_candidate(TRADE_DATE, "000005")

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "MARKET_RISK_OFF_BLOCK" in decision.reason_codes
    assert reloaded.state == CandidateState.WATCHING


def test_selective_market_follower_does_not_pass(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000006", role="FOLLOWER", market_action="ALLOW_REDUCED", market_status="SELECTIVE")
    _ready_ticks(market_data, candles, "000006", [1000, 990, 985, 980], metadata={"recent_support": 978})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "ROLE_FOLLOWER_EXPANSION_ONLY" in decision.reason_codes


def test_expansion_market_follower_can_observe_ready(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000007", role="FOLLOWER", theme_status="SPREADING_THEME", market_action="ALLOW_NORMAL", market_status="EXPANSION")
    _ready_ticks(market_data, candles, "000007", [1000, 990, 985, 980], metadata={"recent_support": 978})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.OBSERVE_READY
    assert decision.price_location == PriceLocationStatus.GOOD_PULLBACK.value
    assert decision.dry_run_intent_allowed is False


def test_chase_high_is_price_wait(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000008")
    _ready_ticks(market_data, candles, "000008", [980, 990, 995, 999], metadata={"vwap": 980, "momentum_1m": 1.0})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.PRICE_WAIT
    assert decision.price_location == PriceLocationStatus.CHASE_HIGH.value


def test_vwap_overextended_is_blocked(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000009")
    _ready_ticks(market_data, candles, "000009", [1150, 1120, 1110, 1100], metadata={"vwap": 1000, "momentum_1m": 1.0})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert decision.price_location == PriceLocationStatus.VWAP_OVEREXTENDED.value


def test_vi_active_is_hard_block(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000010")
    _ready_ticks(market_data, candles, "000010", [1000, 990, 985, 980], metadata={"recent_support": 978, "vi_active": True})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "VI_ACTIVE_BLOCK" in decision.reason_codes


def test_overheated_stock_is_not_observe_ready(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000011", role="OVERHEATED")
    _ready_ticks(market_data, candles, "000011", [1000, 990, 985, 980], metadata={"recent_support": 978})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "ROLE_OVERHEATED_BLOCK" in decision.reason_codes


def test_good_pullback_pullback_reclaim_and_vwap_reclaim_are_ready_locations(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000012", role="LEADER")
    _candidate(db, "000013", role="LEADER")
    _candidate(db, "000014", role="LEADER")
    _ready_ticks(market_data, candles, "000012", [1000, 990, 985, 980], metadata={"recent_support": 978})
    _ready_ticks(market_data, candles, "000013", [1000, 990, 970, 982], metadata={"recent_support": 980, "support_reclaimed": True})
    _ready_ticks(market_data, candles, "000014", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})

    decisions = {item.code: item for item in _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions}

    assert decisions["000012"].price_location == PriceLocationStatus.GOOD_PULLBACK.value
    assert decisions["000013"].price_location == PriceLocationStatus.PULLBACK_RECLAIM.value
    assert decisions["000014"].price_location == PriceLocationStatus.VWAP_RECLAIM.value
    assert all(item.entry_status == EntryDecisionStatus.OBSERVE_READY for item in decisions.values())


def test_candidate_metadata_entry_fields_are_merged(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000015")
    _ready_ticks(market_data, candles, "000015", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})

    _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT)
    reloaded = db.load_candidate(TRADE_DATE, "000015")

    assert reloaded.metadata["entry_status"] == EntryDecisionStatus.OBSERVE_READY.value
    assert reloaded.metadata["entry_price_location"] == PriceLocationStatus.VWAP_RECLAIM.value
    assert reloaded.metadata["entry_live_order_allowed"] is False


def test_dashboard_snapshot_includes_entry_engine_section(tmp_path):
    db, market_data, candles = _context(tmp_path)
    today = datetime.now().date().isoformat()
    _candidate(db, "000016", trade_date=today)
    _ready_ticks(market_data, candles, "000016", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})
    _engine(db, market_data, candles).build(trade_date=today, now=OPEN_AT)

    section = entry_engine_dashboard_section(db, trade_date=today)
    dashboard = build_dashboard_snapshot(db)
    candidates = build_candidates_snapshot(db, trade_date=today)

    assert section["status"] == "OK"
    assert "entry_engine" in dashboard
    assert dashboard["entry_engine"]["observe_ready_count"] == 1
    assert candidates["items"][0]["entry_status"] == EntryDecisionStatus.OBSERVE_READY.value


def test_dry_run_intent_disabled_by_default(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000017")
    _ready_ticks(market_data, candles, "000017", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.entry_status == EntryDecisionStatus.OBSERVE_READY
    assert decision.dry_run_intent_allowed is False
    assert db.list_runtime_order_intents(limit=10) == []


def test_live_order_command_is_never_created(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _candidate(db, "000018")
    _ready_ticks(market_data, candles, "000018", [1030, 1000, 995, 1005], metadata={"vwap": 1000, "momentum_1m": 1.0})

    decision = _engine(db, market_data, candles).build(trade_date=TRADE_DATE, now=OPEN_AT).decisions[0]

    assert decision.live_order_allowed is False
    assert db.conn.execute("SELECT COUNT(*) AS count FROM order_results").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_runtime_pipeline_disabled_by_default(tmp_path):
    db, market_data, candles = _context(tmp_path)
    pipeline = EntryEngineRuntimePipeline(db=db, market_data=market_data, candle_builder=candles)

    summary = pipeline.run_if_due(OPEN_AT)

    assert summary["status"] == "DISABLED"
    assert summary["output_mode"] == "OBSERVE"


def _context(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    return db, market_data, candles


def _config(*, enabled: bool = True, allow_dry_run: bool = False) -> EntryEngineConfig:
    return EntryEngineConfig(enabled=enabled, allow_dry_run_intents=allow_dry_run)


def _engine(db, market_data, candles, *, allow_dry_run: bool = False) -> EntryEngine:
    return EntryEngine(db, market_data=market_data, candle_builder=candles, config=_config(allow_dry_run=allow_dry_run))


def _candidate(
    db: TradingDatabase,
    code: str,
    *,
    trade_date: str = TRADE_DATE,
    theme_status: str = "LEADING_THEME",
    role: str = "LEADER",
    market_action: str = "ALLOW_NORMAL",
    market_status: str = "EXPANSION",
):
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date=trade_date,
            code=code,
            name=f"Stock {code}",
            source_type="condition_search",
            source_id=f"condition:{code}",
            source_score=50.0,
            theme_id="theme-a",
            theme_name="Theme A",
            detected_at=f"{trade_date}T09:01:00",
        )
    ).candidate
    candidate.state = CandidateState.WATCHING
    candidate.metadata.update(
        {
            "theme_board_theme_id": "theme-a",
            "theme_board_theme_name": "Theme A",
            "theme_board_theme_status": theme_status,
            "theme_board_theme_score": 80.0,
            "theme_board_stock_role": role,
            "theme_board_stock_score": 80.0,
            "market_side": "KOSPI",
            "market_regime_status": market_status,
            "global_market_regime_status": market_status,
            "market_action": market_action,
            "market_position_size_multiplier_hint": 1.0 if market_action == "ALLOW_NORMAL" else 0.6,
            "market_block_new_entry": market_action == "BLOCK_NEW_ENTRY",
        }
    )
    return db.save_candidate(candidate)


def _ready_ticks(
    market_data: MarketDataStore,
    candles: CandleBuilder,
    code: str,
    prices: list[int],
    *,
    metadata: dict | None = None,
) -> None:
    start = OPEN_AT - timedelta(minutes=len(prices) - 1)
    for index, price in enumerate(prices):
        timestamp = start + timedelta(minutes=index)
        tick_metadata = dict(metadata or {}) if index == len(prices) - 1 else {}
        tick = StrategyTick.from_realtime(
            code,
            price=price,
            change_rate=2.0,
            cum_volume=(index + 1) * 1000,
            trade_value=1_000_000_000,
            spread_ticks=1,
            timestamp=timestamp,
            metadata=tick_metadata,
        )
        market_data.update_tick(tick)
        candles.update(tick)
