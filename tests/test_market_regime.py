from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import IndexTick, MarketIndexStore
from trading.strategy.market_regime import (
    CandidateMarketAction,
    CompositeMarketMode,
    MarketRegimeConfig,
    MarketRegimeEngine,
    MarketRegimeRuntimePipeline,
    MarketRegimeStatus,
    MarketSide,
    market_regime_dashboard_section,
)
from trading.strategy.models import CandidateState, StrategyProfile
from trading_app.api import build_candidates_snapshot, build_dashboard_snapshot


TRADE_DATE = "2026-06-18"
OPEN_AT = datetime(2026, 6, 18, 9, 5, 0)


def test_index_tick_missing_returns_data_wait_not_exception(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000001", market="KOSPI")
    _tick(market_data, "000001", change=1.0)

    result = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)

    assert result.snapshot.kospi_status == MarketRegimeStatus.DATA_WAIT
    assert "INDEX_TICK_MISSING" in result.snapshot.kospi_snapshot.data_quality_flags
    assert db.list_runtime_order_intents(limit=10) == []


def test_zero_padded_index_storage_alias_is_accepted(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000017", market="KOSPI")
    _tick(market_data, "000017", change=1.0)
    index_store.market_data.update_tick(
        StrategyTick.from_realtime(
            "000001",
            price=3007,
            change_rate=0.7,
            timestamp=OPEN_AT,
        )
    )
    _index(index_store, "KOSDAQ", 0.1)

    result = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)

    assert result.snapshot.kospi_snapshot.index_price == 3007
    assert result.snapshot.kospi_status != MarketRegimeStatus.DATA_WAIT
    assert "INDEX_TICK_MISSING" not in result.snapshot.kospi_snapshot.data_quality_flags


def test_risk_off_blocks_new_entry_without_deleting_candidate(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    candidate = _candidate(db, "000002", market="KOSDAQ")
    _index(index_store, "KOSDAQ", -3.0)
    _tick(market_data, "000002", change=-2.5)

    result = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)

    policy = result.snapshot.candidate_policy_by_code["000002"]
    reloaded = db.load_candidate(TRADE_DATE, "000002")
    assert policy.market_action == CandidateMarketAction.BLOCK_NEW_ENTRY
    assert policy.block_new_entry is True
    assert reloaded.state == candidate.state == CandidateState.WATCHING
    assert reloaded.metadata["market_action"] == CandidateMarketAction.BLOCK_NEW_ENTRY.value
    assert db.list_runtime_order_intents(limit=10) == []


def test_weak_market_waits_and_blocks_entry(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000003", market="KOSPI")
    _index(index_store, "KOSPI", -1.1)
    _tick(market_data, "000003", change=-0.5)

    policy = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code["000003"]

    assert policy.market_action == CandidateMarketAction.WAIT_MARKET
    assert policy.block_new_entry is True
    assert policy.wait_reason == "WEAK_MARKET"


def test_expansion_allows_normal_size(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000004", market="KOSPI")
    _index(index_store, "KOSPI", 1.0)
    _index(index_store, "KOSDAQ", 0.7)
    _tick(market_data, "000004", change=2.0)

    policy = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code["000004"]

    assert policy.market_action == CandidateMarketAction.ALLOW_NORMAL
    assert policy.position_size_multiplier_hint == 1.0
    assert policy.block_new_entry is False


def test_selective_allows_reduced_size(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000005", market="KOSDAQ")
    _index(index_store, "KOSDAQ", 0.2)
    _tick(market_data, "000005", change=3.5)

    policy = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code["000005"]

    assert policy.market_action == CandidateMarketAction.ALLOW_REDUCED
    assert 0.5 <= policy.position_size_multiplier_hint <= 0.7


def test_choppy_waits_without_hard_block(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000006", market="KOSPI")
    _index(index_store, "KOSPI", -0.1)
    _tick(market_data, "000006", change=-0.2)

    policy = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code["000006"]
    reloaded = db.load_candidate(TRADE_DATE, "000006")

    assert policy.market_action == CandidateMarketAction.WAIT_MARKET
    assert policy.position_size_multiplier_hint == 0.35
    assert policy.block_new_entry is False
    assert reloaded.state == CandidateState.WATCHING


def test_market_closed_policy_is_market_closed(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000007", market="KOSPI")
    _index(index_store, "KOSPI", 0.8, timestamp=datetime(2026, 6, 18, 16, 0, 0))
    _tick(market_data, "000007", change=1.0, timestamp=datetime(2026, 6, 18, 16, 0, 0))

    snapshot = _engine(db, market_data, index_store).build(
        trade_date=TRADE_DATE,
        now=datetime(2026, 6, 18, 16, 0, 0),
    ).snapshot

    assert snapshot.global_status == MarketRegimeStatus.MARKET_CLOSED
    assert snapshot.candidate_policy_by_code["000007"].market_action == CandidateMarketAction.MARKET_CLOSED


def test_removed_and_expired_candidates_are_excluded(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000008", market="KOSPI", state=CandidateState.REMOVED)
    _candidate(db, "000009", market="KOSPI", state=CandidateState.EXPIRED)
    _candidate(db, "000010", market="KOSPI", state=CandidateState.WATCHING)
    _index(index_store, "KOSPI", 0.8)
    _tick(market_data, "000010", change=1.0)

    policies = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code

    assert set(policies) == {"000010"}
    assert "market_action" not in db.load_candidate(TRADE_DATE, "000008").metadata
    assert "market_action" not in db.load_candidate(TRADE_DATE, "000009").metadata


def test_wait_data_candidate_keeps_wait_data_state(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000011", market="KOSPI", state=CandidateState.WAIT_DATA)
    _tick(market_data, "000011", change=0.5)

    policy = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot.candidate_policy_by_code["000011"]
    reloaded = db.load_candidate(TRADE_DATE, "000011")

    assert policy.market_action == CandidateMarketAction.DATA_WAIT
    assert reloaded.state == CandidateState.WAIT_DATA
    assert reloaded.metadata["market_action"] == CandidateMarketAction.DATA_WAIT.value


def test_snapshot_is_persisted_with_side_and_candidate_policy_rows(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000012", market="KOSPI")
    _index(index_store, "KOSPI", 0.7)
    _tick(market_data, "000012", change=1.3)

    result = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)

    latest = db.latest_market_regime_snapshot(trade_date=TRADE_DATE)
    side_count = db.conn.execute("SELECT COUNT(*) AS count FROM market_side_snapshots").fetchone()["count"]
    policy_rows = db.list_candidate_market_policies(trade_date=TRADE_DATE)
    assert result.saved is True
    assert latest["global_status"] in {MarketRegimeStatus.EXPANSION.value, MarketRegimeStatus.SELECTIVE.value}
    assert side_count == 2
    assert policy_rows[0]["code"] == "000012"


def test_candidate_policy_upsert_is_idempotent_by_trade_date_calculated_at_code(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000013", market="KOSPI")
    _index(index_store, "KOSPI", 0.7)
    _tick(market_data, "000013", change=1.3)
    engine = _engine(db, market_data, index_store)

    engine.build(trade_date=TRADE_DATE, now=OPEN_AT)
    engine.build(trade_date=TRADE_DATE, now=OPEN_AT)

    count = db.conn.execute("SELECT COUNT(*) AS count FROM candidate_market_policies WHERE code = '000013'").fetchone()["count"]
    assert count == 1


def test_theme_board_overlay_adds_market_context_without_forcing_theme_status(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000014", market="KOSDAQ")
    _tick(market_data, "000014", change=-2.0)
    _index(index_store, "KOSDAQ", -2.8)
    db.save_theme_board_snapshot(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": OPEN_AT.isoformat(),
            "board_status": "OBSERVE",
            "theme_count": 1,
            "top_themes": [{"theme_id": "risk-theme", "theme_name": "Risk", "theme_rank": 1, "theme_status": "LEADING_THEME"}],
            "stocks": [{"code": "000014", "name": "Stock 000014", "theme_id": "risk-theme", "stock_role": "LEADER"}],
            "output_mode": "OBSERVE",
            "ready_allowed": False,
            "order_intent_allowed": False,
        }
    )

    result = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)
    board = db.latest_theme_board_snapshot(trade_date=TRADE_DATE)

    assert result.theme_overlay_applied is True
    assert board["top_themes"][0]["theme_status"] == "LEADING_THEME"
    assert board["top_themes"][0]["market_side_distribution"]["KOSDAQ"] == 1
    assert board["top_themes"][0]["market_risk_flag"] is True


def test_dashboard_and_candidate_snapshot_include_market_regime(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000015", market="KOSPI")
    _index(index_store, "KOSPI", 0.7)
    _tick(market_data, "000015", change=1.3)
    _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT)

    section = market_regime_dashboard_section(db, trade_date=TRADE_DATE)
    dashboard = build_dashboard_snapshot(db)
    candidates = build_candidates_snapshot(db, trade_date=TRADE_DATE)

    assert section["status"] == "OK"
    assert "market_regime" in dashboard
    assert dashboard["market_regime"]["ready_allowed"] is False
    assert candidates["items"][0]["market_action"]


def test_split_kospi_on_reduces_healthy_side_without_global_block(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000201", market="KOSPI")
    _candidate(db, "000202", market="KOSDAQ")
    _index(index_store, "KOSPI", 1.1)
    _index(index_store, "KOSDAQ", -3.0)
    _tick(market_data, "000201", change=2.4)
    _tick(market_data, "000202", change=-2.8)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot

    kospi_policy = snapshot.candidate_policy_by_code["000201"]
    kosdaq_policy = snapshot.candidate_policy_by_code["000202"]
    assert snapshot.global_status == MarketRegimeStatus.RISK_OFF
    assert snapshot.composite_market_mode == CompositeMarketMode.SPLIT_KOSPI_ON
    assert snapshot.systemic_risk_off is False
    assert kospi_policy.market_action == CandidateMarketAction.ALLOW_REDUCED
    assert kospi_policy.position_size_multiplier_hint == 0.6
    assert "SPLIT_MARKET_HEALTHY_SIDE_REDUCED" in kospi_policy.reason_codes
    assert kosdaq_policy.market_action == CandidateMarketAction.BLOCK_NEW_ENTRY
    assert "SIDE_MARKET_RISK_OFF_BLOCK" in kosdaq_policy.reason_codes


def test_split_kosdaq_on_reduces_healthy_side_without_global_block(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000203", market="KOSPI")
    _candidate(db, "000204", market="KOSDAQ")
    _index(index_store, "KOSPI", -2.3)
    _index(index_store, "KOSDAQ", 1.0)
    _tick(market_data, "000203", change=-2.5)
    _tick(market_data, "000204", change=2.8)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot

    kospi_policy = snapshot.candidate_policy_by_code["000203"]
    kosdaq_policy = snapshot.candidate_policy_by_code["000204"]
    assert snapshot.global_status == MarketRegimeStatus.RISK_OFF
    assert snapshot.composite_market_mode == CompositeMarketMode.SPLIT_KOSDAQ_ON
    assert snapshot.systemic_risk_off is False
    assert kospi_policy.market_action == CandidateMarketAction.BLOCK_NEW_ENTRY
    assert "SIDE_MARKET_RISK_OFF_BLOCK" in kospi_policy.reason_codes
    assert kosdaq_policy.market_action == CandidateMarketAction.ALLOW_REDUCED
    assert "SPLIT_MARKET_HEALTHY_SIDE_REDUCED" in kosdaq_policy.reason_codes


def test_systemic_risk_off_blocks_all_sides(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000205", market="KOSPI")
    _candidate(db, "000206", market="KOSDAQ")
    _index(index_store, "KOSPI", -2.5)
    _index(index_store, "KOSDAQ", -3.0)
    _tick(market_data, "000205", change=-2.2)
    _tick(market_data, "000206", change=-3.2)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot

    assert snapshot.composite_market_mode == CompositeMarketMode.SYSTEMIC_RISK_OFF
    assert snapshot.systemic_risk_off is True
    assert "SYSTEMIC_RISK_OFF_BLOCK" in snapshot.systemic_reason_codes
    assert {
        policy.market_action for policy in snapshot.candidate_policy_by_code.values()
    } == {CandidateMarketAction.BLOCK_NEW_ENTRY}
    assert all("SYSTEMIC_RISK_OFF_BLOCK" in policy.reason_codes for policy in snapshot.candidate_policy_by_code.values())


def test_one_risk_off_one_weak_is_systemic_and_blocks_all_sides(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000207", market="KOSPI")
    _candidate(db, "000208", market="KOSDAQ")
    _index(index_store, "KOSPI", -1.2)
    _index(index_store, "KOSDAQ", -3.0)
    _tick(market_data, "000207", change=-0.7)
    _tick(market_data, "000208", change=-2.7)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot

    assert snapshot.kospi_status == MarketRegimeStatus.WEAK
    assert snapshot.kosdaq_status == MarketRegimeStatus.RISK_OFF
    assert snapshot.systemic_risk_off is True
    assert snapshot.composite_market_mode == CompositeMarketMode.SYSTEMIC_RISK_OFF
    assert all("SYSTEMIC_RISK_OFF_BLOCK" in policy.reason_codes for policy in snapshot.candidate_policy_by_code.values())


def test_counterpart_data_wait_reduces_healthy_expansion_side(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000209", market="KOSPI")
    _candidate(db, "000210", market="KOSDAQ")
    _index(index_store, "KOSPI", 1.0)
    _tick(market_data, "000209", change=2.2)
    _tick(market_data, "000210", change=1.0)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot

    kospi_policy = snapshot.candidate_policy_by_code["000209"]
    kosdaq_policy = snapshot.candidate_policy_by_code["000210"]
    assert snapshot.composite_market_mode == CompositeMarketMode.DATA_DEGRADED
    assert kospi_policy.market_action == CandidateMarketAction.ALLOW_REDUCED
    assert "COUNTERPART_MARKET_DATA_WAIT_REDUCED" in kospi_policy.reason_codes
    assert kosdaq_policy.market_action == CandidateMarketAction.DATA_WAIT


def test_unknown_market_side_waits_without_inheriting_global_status(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000211", market="")
    _index(index_store, "KOSPI", 1.0)
    _index(index_store, "KOSDAQ", 0.8)
    _tick(market_data, "000211", change=2.0)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot
    policy = snapshot.candidate_policy_by_code["000211"]
    reloaded = db.load_candidate(TRADE_DATE, "000211")

    assert snapshot.composite_market_mode == CompositeMarketMode.BROAD_RISK_ON
    assert policy.market_side == MarketSide.UNKNOWN
    assert policy.market_action == CandidateMarketAction.DATA_WAIT
    assert policy.position_size_multiplier_hint == 0.0
    assert policy.block_new_entry is True
    assert policy.wait_reason == "MARKET_SIDE_UNRESOLVED"
    assert "MARKET_SIDE_UNKNOWN" in policy.reason_codes
    assert "MARKET_SIDE_UNRESOLVED" in policy.reason_codes
    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.market == ""
    assert db.list_runtime_order_intents(limit=10) == []


def test_kiwoom_symbol_master_has_resolution_priority_and_persists_market(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    candidate = _candidate(db, "000212", market="KOSPI")
    candidate.strategy_profile = StrategyProfile.KOSPI_LEADER_PROFILE
    candidate.metadata["source_payload"] = {"market": "KOSPI"}
    db.save_candidate(candidate)
    db.upsert_kiwoom_symbol_master(
        [
            {
                "code": "000212",
                "name": "Master 000212",
                "market": "KOSDAQ",
                "market_code": "10",
                "source": "test_master",
            }
        ]
    )
    _index(index_store, "KOSPI", 1.0)
    _index(index_store, "KOSDAQ", 0.9)
    _tick(market_data, "000212", change=2.5)

    snapshot = _engine(db, market_data, index_store).build(trade_date=TRADE_DATE, now=OPEN_AT).snapshot
    policy = snapshot.candidate_policy_by_code["000212"]
    reloaded = db.load_candidate(TRADE_DATE, "000212")

    assert policy.market_side == MarketSide.KOSDAQ
    assert policy.market_side_source == "kiwoom_symbol_master"
    assert policy.market_side_resolution_status == "RESOLVED"
    assert "MARKET_SIDE_RESOLVED_FROM_KIWOOM_MASTER" in policy.reason_codes
    assert reloaded.market == "KOSDAQ"
    assert reloaded.metadata["market_side_source"] == "kiwoom_symbol_master"


def test_runtime_pipeline_disabled_by_default(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    pipeline = MarketRegimeRuntimePipeline(db=db, market_data=market_data, market_index_store=index_store)

    summary = pipeline.run_if_due(OPEN_AT)

    assert summary["status"] == "DISABLED"
    assert summary["output_mode"] == "OBSERVE"


def test_runtime_pipeline_enabled_runs_on_interval(tmp_path):
    db, market_data, index_store = _context(tmp_path)
    _candidate(db, "000016", market="KOSPI")
    _index(index_store, "KOSPI", 0.7)
    _tick(market_data, "000016", change=1.3)
    pipeline = MarketRegimeRuntimePipeline(
        db=db,
        market_data=market_data,
        market_index_store=index_store,
        config=_config(enabled=True, interval_sec=5),
    )

    first = pipeline.run_if_due(OPEN_AT)
    second = pipeline.run_if_due(OPEN_AT + timedelta(seconds=2))

    assert first["status"] == "OK"
    assert second == first
    assert db.latest_market_regime_snapshot(trade_date=TRADE_DATE)


def _context(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    market_data = MarketDataStore()
    index_store = MarketIndexStore()
    return db, market_data, index_store


def _config(*, enabled: bool = True, interval_sec: int = 5) -> MarketRegimeConfig:
    return MarketRegimeConfig(
        enabled=enabled,
        interval_sec=interval_sec,
        min_breadth_sample_kospi=1,
        min_breadth_sample_kosdaq=1,
        max_quote_age_sec=120,
    )


def _engine(db, market_data, index_store) -> MarketRegimeEngine:
    return MarketRegimeEngine(db, market_data=market_data, market_index_store=index_store, config=_config())


def _candidate(
    db: TradingDatabase,
    code: str,
    *,
    market: str = "",
    state: CandidateState = CandidateState.WATCHING,
):
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date=TRADE_DATE,
            code=code,
            name=f"Stock {code}",
            source_type="condition_search",
            source_id=f"condition:{code}",
            source_score=50.0,
            detected_at=f"{TRADE_DATE}T09:01:00",
        )
    ).candidate
    candidate.state = state
    candidate.market = market
    return db.save_candidate(candidate)


def _tick(
    market_data: MarketDataStore,
    code: str,
    *,
    change: float,
    timestamp: datetime = OPEN_AT,
) -> None:
    market_data.update_tick(
        StrategyTick.from_realtime(
            code,
            price=1000 + int(change * 10),
            change_rate=change,
            trade_value=1_000_000_000,
            timestamp=timestamp,
        )
    )


def _index(
    index_store: MarketIndexStore,
    side: str,
    change: float,
    *,
    timestamp: datetime = OPEN_AT,
) -> None:
    price = 1000 + int(change * 10)
    index_store.update_index_tick(
        IndexTick.from_realtime(
            side,
            side,
            price=price,
            change_rate=change,
            day_high=max(price, 1000),
            day_low=min(price, 990),
            timestamp=timestamp,
        )
    )
