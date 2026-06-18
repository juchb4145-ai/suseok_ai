from storage.db import TradingDatabase
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import CandidateState
from trading.theme_engine.board_view import ThemeBoardView
from trading.theme_engine.expansion import FocusedExpansionPlan, FocusedExpansionTarget
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, TradeStockRole
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, ThemeDataWaitReason
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot
from trading.theme_engine.theme_board import ThemeBoardConfig, ThemeBoardEngine, theme_board_dashboard_section
from trading_app.api import build_dashboard_snapshot


def test_watching_candidates_realtime_and_membership_build_theme_board_snapshot(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "semis", "Semiconductors", ["000001", "000002"])
    _candidate(db, "000001", theme_id="semis", source_type="opening_burst", source_score=88.0)
    _candidate(db, "000002", theme_id="semis", source_type="condition_search", source_score=55.0)
    _tick(market_data, "000001", change=6.0, turnover=10_000_000_000, execution=160, momentum=1.2)
    _tick(market_data, "000002", change=3.5, turnover=5_000_000_000, execution=140, momentum=0.8)

    result = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17")

    snapshot = result.snapshot
    assert snapshot.theme_count == 1
    assert snapshot.top_themes[0].theme_id == "semis"
    assert snapshot.top_themes[0].realtime_valid_count == 2
    assert {stock.code for stock in snapshot.stocks} == {"000001", "000002"}
    assert snapshot.ready_allowed is False
    assert snapshot.order_intent_allowed is False


def test_condition_source_adds_boost_without_ready_or_order_intent(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "ai", "AI", ["000003"])
    _candidate(db, "000003", source_type="condition_search", source_score=25.0)
    _tick(market_data, "000003", change=2.0, turnover=1_000_000_000, execution=120, momentum=0.2)

    result = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17")

    stock = result.snapshot.stocks[0]
    assert stock.condition_boost > 0
    assert stock.entry_usable is False
    assert db.conn.execute("SELECT COUNT(*) AS count FROM entry_plans").fetchone()["count"] == 0
    assert db.list_runtime_order_intents(limit=10) == []


def test_opening_burst_score_is_reflected_in_stock_snapshot(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "robot", "Robotics", ["000004"])
    _candidate(db, "000004", theme_id="robot", source_type="opening_burst", source_score=92.0)
    _tick(market_data, "000004", change=5.0, turnover=4_000_000_000, execution=150, momentum=1.0)

    snapshot = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot

    assert snapshot.stocks[0].opening_burst_score == 92.0
    assert "opening_burst" in snapshot.stocks[0].source_types


def test_single_spiking_stock_is_not_leading_theme(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "bio", "Bio", ["000005"])
    _candidate(db, "000005", theme_id="bio", source_type="opening_burst", source_score=90.0)
    _tick(market_data, "000005", change=9.0, turnover=12_000_000_000, execution=170, momentum=1.4, day_high=120)

    theme = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot.top_themes[0]

    assert theme.theme_status == "LEADER_ONLY_THEME"
    assert theme.theme_status != "LEADING_THEME"


def test_broad_synchronized_theme_becomes_leading_or_spreading(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "battery", "Battery", ["000006", "000007", "000008"])
    for idx, code in enumerate(["000006", "000007", "000008"], 1):
        _candidate(db, code, theme_id="battery", source_type="opening_burst", source_score=80.0 - idx)
        _tick(market_data, code, change=5.5 - idx * 0.4, turnover=(8 - idx) * 1_000_000_000, execution=155 - idx * 5, momentum=1.1)

    theme = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot.top_themes[0]

    assert theme.theme_status in {"LEADING_THEME", "SPREADING_THEME"}
    assert theme.strong_count >= 2
    assert theme.leader_count >= 1


def test_high_quality_broad_theme_becomes_leading_theme(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "ai_infra", "AI Infra", ["000021", "000022", "000023", "000024"])
    scenarios = [
        ("000021", 7.2, 12_000_000_000, 190, 2.0, 92.0),
        ("000022", 6.4, 10_000_000_000, 180, 1.8, 88.0),
        ("000023", 5.9, 8_000_000_000, 170, 1.5, 84.0),
        ("000024", 4.8, 6_000_000_000, 155, 1.2, 80.0),
    ]
    for code, change, turnover, execution, momentum, source_score in scenarios:
        _candidate(db, code, theme_id="ai_infra", source_type="opening_burst", source_score=source_score)
        _tick(market_data, code, change=change, turnover=turnover, execution=execution, momentum=momentum)

    theme = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot.top_themes[0]

    assert theme.theme_status == "LEADING_THEME"
    assert theme.strong_count >= 2
    assert theme.leader_count >= 1
    assert theme.leader_concentration < 0.75


def test_missing_realtime_data_stays_data_wait_not_weak(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "display", "Display", ["000009", "000010"])
    _candidate(db, "000009", theme_id="display", source_type="condition_search", state=CandidateState.WAIT_DATA)
    _candidate(db, "000010", theme_id="display", source_type="condition_search", state=CandidateState.WAIT_DATA)

    theme = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot.top_themes[0]

    assert theme.theme_status == "DATA_WAIT"
    assert theme.theme_status != "WEAK_THEME"
    assert "REALTIME_COVERAGE_LOW" in theme.data_quality_flags


def test_leader_score_not_highest_return_selects_leader(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "space", "Space", ["000011", "000012"])
    _candidate(db, "000011", theme_id="space", source_type="opening_burst", source_score=80.0)
    _candidate(db, "000012", theme_id="space", source_type="opening_burst", source_score=30.0)
    _tick(market_data, "000011", change=5.0, turnover=15_000_000_000, execution=170, momentum=1.0)
    _tick(market_data, "000012", change=9.0, turnover=300_000_000, execution=95, momentum=-0.2, day_high=130)

    snapshot = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot
    roles = {stock.code: stock.stock_role for stock in snapshot.stocks}

    assert roles["000011"] == "LEADER"
    assert roles["000012"] != "LEADER"


def test_overheated_stock_is_excluded_from_entry_usable(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "quantum", "Quantum", ["000013"])
    _candidate(db, "000013", theme_id="quantum", source_type="opening_burst", source_score=95.0)
    _tick(
        market_data,
        "000013",
        change=14.0,
        turnover=9_000_000_000,
        execution=180,
        momentum=2.0,
        vi_active=True,
        upper_limit_gap_pct=1.0,
    )

    stock = ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17").snapshot.stocks[0]

    assert stock.stock_role == "OVERHEATED"
    assert stock.entry_usable is False
    assert "OVERHEATED" in stock.data_quality_flags


def test_candidate_metadata_is_merged_and_removed_expired_are_excluded(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "auto", "Auto", ["000014", "000015"])
    active = _candidate(db, "000014", theme_id="auto", source_type="opening_burst", source_score=85.0)
    removed = _candidate(db, "000015", theme_id="auto", source_type="opening_burst", source_score=99.0, state=CandidateState.REMOVED)
    _tick(market_data, "000014", change=4.0, turnover=4_000_000_000, execution=135, momentum=0.8)
    _tick(market_data, "000015", change=8.0, turnover=20_000_000_000, execution=180, momentum=1.5)

    ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17")

    reloaded = db.load_candidate_by_id(active.id)
    removed_reloaded = db.load_candidate_by_id(removed.id)
    assert reloaded.metadata["theme_board_theme_id"] == "auto"
    assert reloaded.metadata["entry_usable"] is False
    assert "theme_board_theme_id" not in removed_reloaded.metadata


def test_dashboard_snapshot_includes_theme_board_section(tmp_path):
    db, repo, market_data = _context(tmp_path)
    _theme(repo, "semis", "Semiconductors", ["000016"])
    _candidate(db, "000016", theme_id="semis", source_type="opening_burst", source_score=81.0)
    _tick(market_data, "000016", change=4.5, turnover=3_000_000_000, execution=140, momentum=0.9)
    ThemeBoardEngine(db, market_data=market_data, repository=repo, config=_config()).build(trade_date="2026-06-17")

    section = theme_board_dashboard_section(db, trade_date="2026-06-17")
    dashboard = build_dashboard_snapshot(db)

    assert section["status"] == "OK"
    assert section["ready_allowed"] is False
    assert section["order_intent_allowed"] is False
    assert "theme_board" in dashboard
    assert dashboard["theme_board"]["ready_allowed"] is False


def test_theme_board_view_projects_theme_core_v3_observe_fields():
    leading = ThemeStateSnapshot(
        theme_id="ai",
        theme_name="AI",
        theme_state=ThemeCoreState.LEADING_THEME.value,
        theme_score=82.0,
        leader_symbol="000001",
        co_leader_symbols=("000002",),
    )
    data_wait = ThemeStateSnapshot(
        theme_id="display",
        theme_name="Display",
        theme_state=ThemeCoreState.DATA_WAIT.value,
        theme_score=0.0,
        data_quality_reason=ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value,
    )
    decisions = [
        _role_decision("000001", leading, RawStockRole.LEADER.value, TradeStockRole.LEADER_CONFIRMED.value, 91.0),
        _role_decision("000002", leading, RawStockRole.CO_LEADER.value, TradeStockRole.CO_LEADER_CONFIRMED.value, 85.0),
        _role_decision("000003", leading, RawStockRole.LATE_LAGGARD.value, TradeStockRole.LATE_LAGGARD_BLOCKED.value, 40.0),
        _role_decision("000004", leading, RawStockRole.OVERHEATED.value, TradeStockRole.OVERHEATED_BLOCKED.value, 30.0),
    ]
    plan = FocusedExpansionPlan(
        targets=(
            FocusedExpansionTarget(code="000001", theme_id="ai", trade_role=TradeStockRole.LEADER_CONFIRMED.value),
        ),
        focused_expansion_count=1,
    )

    snapshot = ThemeBoardView().build(
        trade_date="2026-06-17",
        calculated_at="2026-06-17T09:05:00",
        theme_states=[leading, data_wait],
        role_decisions=decisions,
        expansion_plan=plan,
    )

    assert snapshot.output_mode == "OBSERVE"
    assert snapshot.ready_allowed is False
    assert snapshot.order_intent_allowed is False
    assert snapshot.top_themes[0]["theme_id"] == "ai"
    assert snapshot.leaders_by_theme[0]["leaders"][0]["code"] == "000001"
    assert snapshot.excluded_late_laggard_count == 1
    assert snapshot.excluded_overheated_count == 1
    assert snapshot.condition_booster_inflow_count == 1
    assert snapshot.focused_expansion_count == 1
    assert snapshot.data_wait_reasons[ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value] == 1


def _context(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    repo = ThemeEngineRepository(db)
    market_data = MarketDataStore()
    return db, repo, market_data


def _config() -> ThemeBoardConfig:
    return ThemeBoardConfig(enabled=True, min_realtime_valid_ratio=0.35)


def _theme(repo: ThemeEngineRepository, theme_id: str, name: str, codes: list[str]) -> None:
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id=theme_id,
            canonical_name=name,
            display_name=name,
            status=ThemeStatus.ACTIVE,
            confidence=0.9,
            trade_eligible=True,
        )
    )
    for index, code in enumerate(codes, 1):
        repo.upsert_current_membership(
            ThemeMembership(
                theme_id=theme_id,
                stock_code=code,
                stock_name=f"Stock {code}",
                membership_score=max(1.0, 100.0 - index),
                source_count=1,
                active=True,
                trade_eligible=True,
            )
        )


def _candidate(
    db: TradingDatabase,
    code: str,
    *,
    theme_id: str = "",
    source_type: str = "condition_search",
    source_score: float = 50.0,
    state: CandidateState = CandidateState.WATCHING,
):
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date="2026-06-17",
            code=code,
            name=f"Stock {code}",
            source_type=source_type,
            source_id=f"{source_type}:{code}",
            source_score=source_score,
            theme_id=theme_id,
            theme_name=theme_id,
            detected_at="2026-06-17T09:01:00",
        )
    ).candidate
    candidate.state = state
    return db.save_candidate(candidate)


def _tick(
    market_data: MarketDataStore,
    code: str,
    *,
    change: float,
    turnover: float,
    execution: float,
    momentum: float,
    day_high: float = 0.0,
    vi_active: bool = False,
    upper_limit_gap_pct: float = 100.0,
) -> None:
    price = 1000 + int(change * 10)
    market_data.update_tick(
        StrategyTick.from_realtime(
            code,
            price=price,
            change_rate=change,
            cum_volume=100_000,
            trade_value=turnover,
            execution_strength=execution,
            spread_ticks=1,
            metadata={
                "momentum_1m": momentum,
                "momentum_3m": momentum,
                "momentum_5m": momentum,
                "vwap": max(1.0, price * 0.98),
                "session_high": day_high or price * 1.02,
                "vi_active": vi_active,
                "upper_limit_gap_pct": upper_limit_gap_pct,
            },
        )
    )


def _role_decision(
    code: str,
    theme_state: ThemeStateSnapshot,
    raw_role: str,
    trade_role: str,
    role_score: float,
) -> StockRoleDecision:
    return StockRoleDecision(
        code=code,
        name=f"Stock {code}",
        theme_id=theme_state.theme_id,
        theme_name=theme_state.theme_name,
        raw_role=raw_role,
        trade_role=trade_role,
        role_score=role_score,
        signal=LiveSeedSignal(
            code=code,
            source_types=(SeedSourceType.CONDITION_INCLUDE.value,) if code == "000001" else (),
        ),
        theme_state=theme_state,
    )
