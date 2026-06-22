from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry_engine import EntryDecisionStatus, EntryEngine, EntryEngineConfig
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState
from trading.strategy.strategy_context import StrategyContextAssembler, session_phase


TRADE_DATE = "2026-06-19"


def test_strategy_context_assembler_persists_nested_context_without_order_permissions(tmp_path):
    db = TradingDatabase(str(tmp_path / "context.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(_market_snapshot())
    db.save_theme_board_snapshot(_theme_board())

    assembler = StrategyContextAssembler(db, market_data=market_data)
    snapshot = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 20, 0))
    assembler.save_snapshots([candidate], [snapshot], calculated_at=snapshot.calculated_at)

    saved = db.latest_strategy_context(trade_date=TRADE_DATE, code="000001")
    reloaded = db.load_candidate(TRADE_DATE, "000001")

    assert snapshot.schema_version == "strategy_context_v3"
    assert snapshot.session_phase == "MORNING_TREND"
    assert snapshot.market.side_market_regime == "EXPANSION"
    assert snapshot.theme.theme_state == "LEADING_THEME"
    assert snapshot.stock.trade_stock_role == "LEADER_CONFIRMED"
    assert snapshot.ready_allowed is False
    assert snapshot.order_intent_allowed is False
    assert saved["context_id"] == snapshot.context_id
    assert reloaded.metadata["strategy_context_v3"]["context_id"] == snapshot.context_id
    assert reloaded.metadata["strategy_context_v3"]["theme"]["theme_state"] == "LEADING_THEME"


def test_strategy_context_id_changes_when_best_theme_selection_changes(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-best-theme.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(_market_snapshot())
    db.save_theme_board_snapshot(_multi_theme_board(primary="ai", calculated_at="2026-06-19T09:20:00"))
    assembler = StrategyContextAssembler(db, market_data=market_data)

    first = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 20, 0))
    assembler.save_snapshots([candidate], [first], calculated_at=first.calculated_at)
    db.save_theme_board_snapshot(_multi_theme_board(primary="robot", calculated_at="2026-06-19T09:21:00"))
    second = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 21, 0))

    assert first.selected_theme_id == "ai"
    assert second.selected_theme_id == "robot"
    assert second.previous_selected_theme_id == "ai"
    assert second.theme_selection_changed is True
    assert second.context_id != first.context_id


def test_strategy_context_uses_side_status_from_dashboard_market_payload(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-dashboard-market.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    theme_board = _theme_board()
    market_summary = {
        "trade_date": TRADE_DATE,
        "calculated_at": "2026-06-19T09:20:00",
        "global_status": "EXPANSION",
        "kospi_status": "SELECTIVE",
        "kosdaq_status": "EXPANSION",
        "kospi_return_pct": 0.2,
        "kosdaq_return_pct": 0.8,
        "kospi_breadth_pct": 0.52,
        "kosdaq_breadth_pct": 0.62,
        "market_session_status": "open",
        "reason_codes": ["INDEX_UP"],
    }

    snapshot = StrategyContextAssembler(db, market_data=market_data).assemble_candidate(
        candidate,
        now=datetime(2026, 6, 19, 9, 20, 0),
        market_context=market_summary,
        theme_board=theme_board,
    )

    assert snapshot.market.side_market_regime == "EXPANSION"
    assert snapshot.market.market_action == "ALLOW_NORMAL"
    assert snapshot.market.index_return_pct == 0.8
    assert snapshot.data.market_context_fresh is True


def test_entry_engine_uses_strategy_context_v3_without_legacy_theme_board_metadata(tmp_path):
    db = TradingDatabase(str(tmp_path / "entry-context.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(_market_snapshot())
    db.save_theme_board_snapshot(_theme_board())
    assembler = StrategyContextAssembler(db, market_data=market_data)
    snapshot = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 20, 0))
    assembler.save_snapshots([candidate], [snapshot], calculated_at=snapshot.calculated_at)

    engine = EntryEngine(
        db,
        market_data=market_data,
        candle_builder=CandleBuilder(),
        config=EntryEngineConfig(
            enabled=True,
            min_1m_candles=0,
            require_vwap=False,
            use_strategy_context_v3=True,
            allow_legacy_theme_context_fallback=False,
        ),
    )
    result = engine.evaluate_candidates([db.load_candidate(TRADE_DATE, "000001")], now=datetime(2026, 6, 19, 9, 20, 1))

    assert result.evaluated_count == 1
    decision = result.decisions[0]
    assert decision.theme_status == "LEADING_THEME"
    assert decision.stock_role == "LEADER_CONFIRMED"
    assert decision.market_status == "EXPANSION"
    assert decision.entry_status in {EntryDecisionStatus.OBSERVE_READY, EntryDecisionStatus.PRICE_WAIT}
    assert decision.dry_run_intent_allowed is False
    assert decision.live_order_allowed is False


def test_assemble_active_candidates_uses_candidate_state_contract(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-contract.db"))
    market_data = MarketDataStore()
    eligible = db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="001111",
            name="Eligible",
            state=CandidateState.WAIT_DATA,
            sources=[CandidateSourceType.CONDITION_SEARCH],
            metadata={"candidate_hydration": _complete_hydration("001111")},
        )
    )
    retry = db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="002222",
            name="Retry",
            state=CandidateState.WAIT_DATA,
            sources=[CandidateSourceType.CONDITION_SEARCH],
            metadata={"candidate_hydration": {**_complete_hydration("002222"), "status": "RETRY_WAIT"}},
        )
    )

    snapshots = StrategyContextAssembler(db, market_data=market_data).assemble_active_candidates(
        now=datetime(2026, 6, 19, 9, 20, 0),
        save=False,
    )

    assert [snapshot.code for snapshot in snapshots] == ["001111"]
    assert db.load_candidate(TRADE_DATE, eligible.code).state == CandidateState.WATCHING
    assert db.load_candidate(TRADE_DATE, retry.code).state == CandidateState.WAIT_DATA


def test_takeover_pending_leadership_policy_waits_new_entry(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-leadership-wait.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(_market_snapshot())
    db.save_theme_board_snapshot(_theme_board_with_leadership("TAKEOVER_PENDING"))
    assembler = StrategyContextAssembler(db, market_data=market_data)
    snapshot = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 20, 0))
    assembler.save_snapshots([candidate], [snapshot], calculated_at=snapshot.calculated_at)

    decision = _entry_decision(db, market_data)

    assert snapshot.theme.leadership_wait_new_entry is True
    assert snapshot.blocking_stage == "THEME"
    assert snapshot.primary_reason_code == "THEME_TAKEOVER_PENDING_WAIT"
    assert decision.entry_status == EntryDecisionStatus.THEME_WAIT
    assert "THEME_TAKEOVER_PENDING_WAIT" in decision.reason_codes


def test_rotated_out_leadership_policy_hard_blocks_new_entry(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-leadership-block.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(_market_snapshot())
    db.save_theme_board_snapshot(_theme_board_with_leadership("ROTATED_OUT"))
    assembler = StrategyContextAssembler(db, market_data=market_data)
    snapshot = assembler.assemble_candidate(candidate, now=datetime(2026, 6, 19, 9, 20, 0))
    assembler.save_snapshots([candidate], [snapshot], calculated_at=snapshot.calculated_at)

    decision = _entry_decision(db, market_data)

    assert snapshot.theme.leadership_block_new_entry is True
    assert snapshot.blocking_stage == "THEME"
    assert snapshot.primary_reason_code == "THEME_ROTATED_OUT_BLOCK"
    assert decision.entry_status == EntryDecisionStatus.HARD_BLOCK
    assert "THEME_ROTATED_OUT_BLOCK" in decision.reason_codes


def test_entry_engine_blocks_when_strategy_context_v3_missing_and_fallback_disabled(tmp_path):
    db = TradingDatabase(str(tmp_path / "entry-context-missing.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")

    engine = EntryEngine(
        db,
        market_data=market_data,
        candle_builder=CandleBuilder(),
        config=EntryEngineConfig(
            enabled=True,
            min_1m_candles=0,
            require_vwap=False,
            use_strategy_context_v3=True,
            allow_legacy_theme_context_fallback=False,
        ),
    )
    result = engine.evaluate_candidates([candidate], now=datetime(2026, 6, 19, 9, 20, 1))

    assert result.decisions[0].entry_status == EntryDecisionStatus.DATA_WAIT
    assert "STRATEGY_CONTEXT_V3_MISSING" in result.decisions[0].reason_codes


def test_entry_engine_v3_ignores_legacy_metadata_when_context_fields_are_missing(tmp_path):
    db = TradingDatabase(str(tmp_path / "entry-context-no-legacy-fallback.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db)
    _tick(market_data, "000001")
    candidate.metadata.update(
        {
            "theme_board_theme_status": "LEADING_THEME",
            "theme_board_stock_role": "LEADER_CONFIRMED",
            "market_regime_status": "EXPANSION",
            "market_action": "ALLOW_NORMAL",
            "strategy_context_v3": {
                "schema_version": "strategy_context_v3",
                "context_id": "ctx-empty-sections",
                "theme": {},
                "stock": {},
                "market": {},
                "data": {},
                "risk": {},
            },
            "strategy_context_id": "ctx-empty-sections",
        }
    )
    candidate = db.save_candidate(candidate)

    engine = EntryEngine(
        db,
        market_data=market_data,
        candle_builder=CandleBuilder(),
        config=EntryEngineConfig(
            enabled=True,
            min_1m_candles=0,
            require_vwap=False,
            use_strategy_context_v3=True,
            allow_legacy_theme_context_fallback=False,
        ),
    )
    result = engine.evaluate_candidates([candidate], now=datetime(2026, 6, 19, 9, 20, 1))

    decision = result.decisions[0]
    assert decision.entry_status != EntryDecisionStatus.OBSERVE_READY
    assert decision.theme_status == ""
    assert decision.stock_role == ""
    assert decision.market_action == ""
    assert "MARKET_DATA_WAIT" in decision.reason_codes
    assert "ROLE_MISSING" in decision.reason_codes


def test_strategy_context_fallback_does_not_treat_legacy_global_risk_off_as_systemic(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-split-market.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db, market="KOSPI")
    _tick(market_data, "000001")
    db.save_market_regime_snapshot(
        {
            **_market_snapshot(),
            "global_status": "RISK_OFF",
            "kospi_status": "EXPANSION",
            "kosdaq_status": "RISK_OFF",
            "systemic_risk_off": False,
            "candidate_policy_by_code": {},
            "kospi_snapshot": {"side": "KOSPI", "status": "EXPANSION", "index_return_pct": 0.9},
            "kosdaq_snapshot": {"side": "KOSDAQ", "status": "RISK_OFF", "index_return_pct": -3.0},
        }
    )
    db.save_theme_board_snapshot(_theme_board())

    snapshot = StrategyContextAssembler(db, market_data=market_data).assemble_candidate(
        candidate,
        now=datetime(2026, 6, 19, 9, 20, 0),
    )

    assert snapshot.market.global_market_regime == "RISK_OFF"
    assert snapshot.market.side_market_regime == "EXPANSION"
    assert snapshot.market.market_action == "ALLOW_REDUCED"
    assert snapshot.market.block_new_entry is False
    assert "SPLIT_MARKET_HEALTHY_SIDE_REDUCED" in snapshot.market.reason_codes


def test_strategy_context_fallback_infers_systemic_risk_off_from_side_statuses(tmp_path):
    db = TradingDatabase(str(tmp_path / "context-systemic-market.db"))
    market_data = MarketDataStore()
    candidate = _candidate(db, market="KOSPI")
    _tick(market_data, "000001")
    payload = {
        **_market_snapshot(),
        "global_status": "RISK_OFF",
        "kospi_status": "WEAK",
        "kosdaq_status": "RISK_OFF",
        "candidate_policy_by_code": {},
        "kospi_snapshot": {"side": "KOSPI", "status": "WEAK", "index_return_pct": -1.1},
        "kosdaq_snapshot": {"side": "KOSDAQ", "status": "RISK_OFF", "index_return_pct": -3.0},
    }
    payload.pop("systemic_risk_off", None)
    db.save_market_regime_snapshot(payload)
    db.save_theme_board_snapshot(_theme_board())

    snapshot = StrategyContextAssembler(db, market_data=market_data).assemble_candidate(
        candidate,
        now=datetime(2026, 6, 19, 9, 20, 0),
    )

    assert snapshot.market.market_action == "BLOCK_NEW_ENTRY"
    assert snapshot.market.block_new_entry is True
    assert "SYSTEMIC_RISK_OFF_BLOCK" in snapshot.market.reason_codes


def test_session_phase_boundaries():
    assert session_phase(datetime(2026, 6, 19, 8, 59)).value == "PRE_OPEN"
    assert session_phase(datetime(2026, 6, 19, 9, 1)).value == "OPENING_DISCOVERY"
    assert session_phase(datetime(2026, 6, 19, 11, 30)).value == "MIDDAY_CHOP"
    assert session_phase(datetime(2026, 6, 19, 14, 45)).value == "CLOSING_RISK"


def test_theme_state_runtime_latest_persists_same_day_leader_stability(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme-state.db"))
    first = db.save_theme_state_runtime(
        {
            "theme_id": "ai",
            "theme_name": "AI",
            "previous_state": "",
            "current_state": "SPREADING_THEME",
            "theme_state": "SPREADING_THEME",
            "theme_score": 72.0,
            "leader_symbol": "000001",
            "leader_stability_count": 1,
            "persistence_count": 1,
        },
        trade_date=TRADE_DATE,
        calculated_at="2026-06-19T09:05:00",
    )
    second = db.save_theme_state_runtime(
        {
            **first,
            "previous_state": "SPREADING_THEME",
            "current_state": "LEADING_THEME",
            "theme_state": "LEADING_THEME",
            "theme_score": 80.0,
            "theme_score_delta": 8.0,
            "leader_symbol": "000001",
            "leader_stability_count": 2,
            "persistence_count": 2,
        },
        trade_date=TRADE_DATE,
        calculated_at="2026-06-19T09:06:00",
    )

    restored = db.load_theme_state_runtime(trade_date=TRADE_DATE, theme_id="ai")
    previous_day = db.load_theme_state_runtime(trade_date="2026-06-18", theme_id="ai")

    assert second["theme_state"] == "LEADING_THEME"
    assert restored["persistence_count"] == 2
    assert restored["leader_stability_count"] == 2
    assert previous_day == {}


def _candidate(db: TradingDatabase, *, market: str = "KOSDAQ") -> Candidate:
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="000001",
            name="Leader",
            market=market,
            state=CandidateState.WATCHING,
            metadata={},
        )
    )


def _complete_hydration(code: str) -> dict:
    return {
        "status": "ACKED",
        "basic_hydration_complete": True,
        "basic_hydration_completed_at": "2026-06-19T09:20:00",
        "parsed": {
            "code": code,
            "current_price": 1000,
            "change_rate": 1.2,
            "prev_close": 988,
        },
    }


def _entry_decision(db: TradingDatabase, market_data: MarketDataStore):
    engine = EntryEngine(
        db,
        market_data=market_data,
        candle_builder=CandleBuilder(),
        config=EntryEngineConfig(
            enabled=True,
            min_1m_candles=0,
            require_vwap=False,
            use_strategy_context_v3=True,
            allow_legacy_theme_context_fallback=False,
        ),
    )
    return engine.evaluate_candidates([db.load_candidate(TRADE_DATE, "000001")], now=datetime(2026, 6, 19, 9, 20, 1)).decisions[0]


def _tick(market_data: MarketDataStore, code: str) -> None:
    market_data.update_tick(
        StrategyTick.from_realtime(
            code,
            price=10000,
            change_rate=5.4,
            cum_volume=100_000,
            trade_value=8_000_000_000,
            execution_strength=155,
            spread_ticks=1,
            timestamp=datetime(2026, 6, 19, 9, 20, 0),
            metadata={
                "vwap": 9900,
                "day_high": 10300,
                "day_low": 9400,
                "turnover_speed": 800_000_000,
                "momentum_1m": 0.7,
                "momentum_3m": 0.5,
                "momentum_5m": 0.3,
                "upper_limit_gap_pct": 10.0,
            },
        )
    )


def _market_snapshot() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "calculated_at": "2026-06-19T09:20:00",
        "global_status": "EXPANSION",
        "kospi_status": "SELECTIVE",
        "kosdaq_status": "EXPANSION",
        "kosdaq_snapshot": {
            "side": "KOSDAQ",
            "status": "EXPANSION",
            "index_return_pct": 0.8,
            "index_slope_1m_pct": 0.1,
            "index_slope_3m_pct": 0.2,
            "index_slope_5m_pct": 0.3,
            "breadth_pct": 0.62,
            "turnover_weighted_return_pct": 1.1,
            "risk_score": 0.0,
        },
        "kospi_snapshot": {"side": "KOSPI", "status": "SELECTIVE"},
        "candidate_policy_by_code": {
            "000001": {
                "code": "000001",
                "market_side": "KOSDAQ",
                "market_status": "EXPANSION",
                "global_market_status": "EXPANSION",
                "market_action": "ALLOW_NORMAL",
                "position_size_multiplier_hint": 1.0,
                "block_new_entry": False,
                "reason_codes": ["MARKET_EXPANSION_ALLOW"],
            }
        },
        "reason_codes": ["INDEX_UP"],
    }


def _theme_board() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "calculated_at": "2026-06-19T09:20:00",
        "board_status": "OBSERVE",
        "theme_count": 1,
        "active_theme_count": 1,
        "top_themes": [
            {
                "theme_id": "ai",
                "theme_name": "AI",
                "theme_rank": 1,
                "theme_status": "LEADING_THEME",
                "theme_state": "LEADING_THEME",
                "previous_theme_state": "SPREADING_THEME",
                "theme_transition": "SPREADING_THEME->LEADING_THEME",
                "theme_score": 82.0,
                "theme_score_delta": 5.0,
                "persistence_count": 3,
                "leader_symbol": "000001",
                "co_leader_symbols": [],
                "leader_stability_count": 3,
                "strong_count": 3,
                "leader_count": 1,
                "breadth_ratio": 0.75,
                "weighted_return_pct": 4.5,
                "leader_concentration": 0.42,
                "coverage_ratio": 0.9,
                "reason_codes": ["LEADING_PERSISTENCE_CONFIRMED"],
            }
        ],
        "stocks": [
            {
                "code": "000001",
                "name": "Leader",
                "theme_id": "ai",
                "theme_name": "AI",
                "stock_role": "LEADER",
                "raw_role": "LEADER",
                "trade_role": "LEADER_CONFIRMED",
                "stock_score": 88.0,
                "source_rank": 1,
                "reason_codes": ["LEADER_CONFIRMED"],
            }
        ],
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _theme_board_with_leadership(status: str) -> dict:
    board = _theme_board()
    theme = dict(board["top_themes"][0])
    theme["leadership_status"] = status
    theme["leadership_score"] = 80.0
    theme["leadership_rank"] = 1
    board["top_themes"] = [theme]
    board["themes_by_id"] = {"ai": theme}
    board["stock_contexts_by_code"] = {"000001": [dict(board["stocks"][0])]}
    return board


def _multi_theme_board(*, primary: str, calculated_at: str) -> dict:
    ai_score = 91.0 if primary == "ai" else 76.0
    robot_score = 91.0 if primary == "robot" else 76.0
    return {
        "trade_date": TRADE_DATE,
        "calculated_at": calculated_at,
        "board_status": "OBSERVE",
        "top_themes": [],
        "themes_by_id": {
            "ai": {
                "theme_id": "ai",
                "theme_name": "AI",
                "theme_state": "LEADING_THEME" if primary == "ai" else "SPREADING_THEME",
                "theme_status": "LEADING_THEME" if primary == "ai" else "SPREADING_THEME",
                "theme_score": ai_score,
                "leadership_score": ai_score,
                "leadership_status": "INCUMBENT" if primary == "ai" else "CHALLENGER",
                "persistence_count": 4,
            },
            "robot": {
                "theme_id": "robot",
                "theme_name": "Robot",
                "theme_state": "LEADING_THEME" if primary == "robot" else "SPREADING_THEME",
                "theme_status": "LEADING_THEME" if primary == "robot" else "SPREADING_THEME",
                "theme_score": robot_score,
                "leadership_score": robot_score,
                "leadership_status": "INCUMBENT" if primary == "robot" else "CHALLENGER",
                "persistence_count": 4,
            },
        },
        "stock_contexts_by_code": {
            "000001": [
                {"code": "000001", "theme_id": "ai", "theme_name": "AI", "trade_role": "LEADER_CONFIRMED", "stock_role": "LEADER", "stock_score": 88.0},
                {"code": "000001", "theme_id": "robot", "theme_name": "Robot", "trade_role": "LEADER_CONFIRMED", "stock_role": "LEADER", "stock_score": 88.0},
            ]
        },
        "stocks": [],
        "ready_allowed": False,
        "order_intent_allowed": False,
    }
