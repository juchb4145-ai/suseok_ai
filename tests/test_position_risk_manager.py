from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState, VirtualPosition
from trading.strategy.position_risk import (
    MarketSideBudgetAction,
    PortfolioRiskSnapshot,
    PositionMarketAction,
    PositionRiskConfig,
    PositionRiskManager,
    PositionRiskRuntimePipeline,
    PositionRiskSnapshot,
    PositionRiskStatus,
    PositionRuntimeService,
    PositionRuntimeSnapshot,
    position_risk_dashboard_section,
)
from trading_app.api import build_dashboard_snapshot


TRADE_DATE = "2026-06-18"
NOW = datetime(2026, 6, 18, 9, 20, 0)


def test_position_risk_manager_builds_position_and_portfolio_risk(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    positions = [
        _position("virtual:1", "200001", current_price=960, current_return_pct=-4.0, max_drawdown_pct=-4.0),
        _position("virtual:2", "200002", current_price=1050, current_return_pct=5.0, max_drawdown_pct=-1.0),
    ]

    result = PositionRiskManager(db).build(trade_date=TRADE_DATE, now=NOW, positions=positions)

    assert len(result.position_risks) == 2
    assert result.portfolio_risk.open_position_count == 2
    assert result.portfolio_risk.total_exposure > 0
    assert db.latest_position_risk_snapshots(trade_date=TRADE_DATE)
    assert db.latest_portfolio_risk_snapshot(trade_date=TRADE_DATE)["open_position_count"] == 2


def test_portfolio_risk_recommends_stop_new_entry_on_large_exposure(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    config = PositionRiskConfig(stop_new_entry_exposure_krw=1)
    positions = [_position("virtual:1", "200003", current_price=1000, remaining_quantity=10)]

    result = PositionRiskManager(db, config=config).build(trade_date=TRADE_DATE, now=NOW, positions=positions)

    assert result.portfolio_risk.risk_level == "STOP_NEW_ENTRY"
    assert result.portfolio_risk.stop_new_entry_recommended is True


def test_portfolio_risk_recommends_kill_switch_on_large_drawdown(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    config = PositionRiskConfig(kill_switch_drawdown_pct=-5.0)
    positions = [_position("virtual:1", "200004", current_price=900, current_return_pct=-10.0, max_drawdown_pct=-10.0)]

    result = PositionRiskManager(db, config=config).build(trade_date=TRADE_DATE, now=NOW, positions=positions)

    assert result.portfolio_risk.risk_level == "KILL_SWITCH_RECOMMENDED"
    assert result.portfolio_risk.kill_switch_recommended is True


def test_data_wait_position_raises_data_risk_not_sell_action(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    positions = [
        _position(
            "virtual:1",
            "200005",
            current_price=0,
            risk_status=PositionRiskStatus.DATA_WAIT,
            data_quality_flags=("CURRENT_PRICE_MISSING",),
        )
    ]

    result = PositionRiskManager(db).build(trade_date=TRADE_DATE, now=NOW, positions=positions)

    assert result.position_risks[0].data_risk_level == "DATA_WAIT"
    assert "POSITION_DATA_WAIT" in result.position_risks[0].reason_codes


def test_position_risk_dashboard_section_and_dashboard_snapshot(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    positions = [_position("virtual:1", "200006", current_price=990, current_return_pct=-1.0)]
    PositionRiskManager(db).build(trade_date=datetime.now().date().isoformat(), now=datetime.now(), positions=positions)

    section = position_risk_dashboard_section(db, trade_date=datetime.now().date().isoformat())
    dashboard = build_dashboard_snapshot(db)

    assert section["status"] == "OK"
    assert "position_risk" in dashboard
    assert dashboard["position_risk"]["open_position_count"] == 1


def test_position_risk_runtime_pipeline_disabled_by_default(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    pipeline = PositionRiskRuntimePipeline(db=db, market_data=MarketDataStore(), candle_builder=CandleBuilder())

    summary = pipeline.run_if_due(NOW)

    assert summary["status"] == "DISABLED"
    assert summary["output_mode"] == "OBSERVE"


def test_position_runtime_skips_previous_trade_date_virtual_positions(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    old_candidate = db.save_candidate(
        Candidate(
            trade_date="2026-06-17",
            code="200099",
            name="Old Position",
            sources=[CandidateSourceType.CONDITION_SEARCH],
            state=CandidateState.WATCHING,
        )
    )
    db.save_virtual_position(
        VirtualPosition(
            candidate_id=old_candidate.id,
            virtual_order_id=1,
            entry_price=1000,
            quantity=10,
            opened_at="2026-06-17T09:05:00",
        )
    )

    result = PositionRuntimeService(db, market_data=MarketDataStore(), candle_builder=CandleBuilder()).build(
        trade_date=TRADE_DATE,
        now=NOW,
        save=True,
    )

    assert result.positions == ()
    assert db.latest_position_runtime_snapshots(trade_date=TRADE_DATE) == []


def test_position_risk_dashboard_uses_portfolio_batch_when_positions_are_zero(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    old_at = (NOW - timedelta(seconds=30)).isoformat()
    old_position = _position("virtual:old", "200098", calculated_at=old_at)
    db.save_position_runtime_snapshots([old_position.to_dict()])
    db.save_position_risk_snapshots(
        [
            PositionRiskSnapshot(
                trade_date=TRADE_DATE,
                calculated_at=old_at,
                position_id=old_position.position_id,
                candidate_id=old_position.candidate_id,
                code=old_position.code,
            ).to_dict()
        ]
    )
    db.save_portfolio_risk_snapshot(
        PortfolioRiskSnapshot(
            trade_date=TRADE_DATE,
            calculated_at=old_at,
            open_position_count=1,
            total_exposure=10000,
        ).to_dict()
    )
    db.save_portfolio_risk_snapshot(
        PortfolioRiskSnapshot(
            trade_date=TRADE_DATE,
            calculated_at=NOW.isoformat(),
            open_position_count=0,
            total_exposure=0,
        ).to_dict()
    )

    section = position_risk_dashboard_section(db, trade_date=TRADE_DATE)

    assert section["open_position_count"] == 0
    assert section["positions"] == []
    assert section["position_market_action_counts"] == {}


def test_portfolio_snapshot_round_trips(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    snapshot = PortfolioRiskSnapshot(
        trade_date=TRADE_DATE,
        calculated_at=NOW.isoformat(),
        open_position_count=1,
        total_exposure=10000,
        theme_exposure_by_theme={"Theme A": 10000},
        market_side_exposure={"KOSDAQ": 10000},
        unrealized_pnl_pct=1.5,
        risk_level="NORMAL",
    )

    saved = db.save_portfolio_risk_snapshot(snapshot.to_dict())

    assert saved["theme_exposure_by_theme"]["Theme A"] == 10000
    assert db.latest_portfolio_risk_snapshot(trade_date=TRADE_DATE)["risk_level"] == "NORMAL"


def test_market_side_portfolio_budget_split_market_and_pending_buy_reservation(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    _market_regime(db, kospi="EXPANSION", kosdaq="RISK_OFF", composite="SPLIT_KOSPI_ON")
    db.save_managed_order_intent(
        {
            "trade_date": TRADE_DATE,
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "300001",
            "quantity": 2,
            "price": 1000,
            "status": "RISK_APPROVED",
            "idempotency_key": "pending:kospi",
            "details": {"market_side": "KOSPI"},
        }
    )
    config = PositionRiskConfig(
        market_side_portfolio_enabled=True,
        portfolio_gross_exposure_limit_krw=100_000,
        kospi_max_open_positions=5,
        kosdaq_max_open_positions=5,
    )
    positions = [
        _position("virtual:1", "300001", market_side="KOSPI", current_price=1000, remaining_quantity=10),
        _position("virtual:2", "300002", market_side="KOSDAQ", current_price=1000, remaining_quantity=10),
    ]

    result = PositionRiskManager(db, config=config).build(trade_date=TRADE_DATE, now=NOW, positions=positions)

    budgets = result.portfolio_risk.market_side_budgets
    assert budgets["KOSPI"]["budget_action"] == MarketSideBudgetAction.REDUCED_BUDGET.value
    assert budgets["KOSPI"]["pending_buy_exposure_krw"] == 2000
    assert "PENDING_BUY_EXPOSURE_RESERVED" in budgets["KOSPI"]["reason_codes"]
    assert budgets["KOSDAQ"]["budget_action"] == MarketSideBudgetAction.STOP_NEW_ENTRY.value
    assert result.portfolio_risk.gross_reserved_exposure_krw == 22_000


def test_position_market_action_ignores_counterpart_risk_for_healthy_side(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    position = _position(
        "virtual:1",
        "300003",
        market_side="KOSPI",
        market_side_resolution_status="RESOLVED",
        side_market_regime="EXPANSION",
        counterpart_market_regime="RISK_OFF",
        composite_market_mode="SPLIT_KOSPI_ON",
        market_context_calculated_at=NOW.isoformat(),
        market_context_fresh=True,
    )

    risk = PositionRiskManager(db).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).position_risks[0]

    assert risk.position_market_action == PositionMarketAction.HOLD.value
    assert "COUNTERPART_MARKET_RISK_IGNORED" in risk.position_action_reason_codes


def test_position_market_action_weak_loser_with_structure_break_exits_now(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    position = _position(
        "virtual:1",
        "300004",
        market_side="KOSDAQ",
        market_side_resolution_status="RESOLVED",
        side_market_regime="WEAK",
        market_context_calculated_at=NOW.isoformat(),
        market_context_fresh=True,
        current_return_pct=-1.0,
        details={"support_broken": True, "market_side": "KOSDAQ"},
    )

    risk = PositionRiskManager(db).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).position_risks[0]

    assert risk.position_market_action == PositionMarketAction.EXIT_NOW.value
    assert "POSITION_MARKET_EXIT_NOW" in risk.position_action_reason_codes


def test_unknown_or_stale_position_market_context_is_data_wait(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    position = _position(
        "virtual:1",
        "300005",
        market_side="UNKNOWN",
        market_side_resolution_status="UNRESOLVED",
        side_market_regime="EXPANSION",
        market_context_fresh=False,
    )

    risk = PositionRiskManager(db).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).position_risks[0]

    assert risk.position_market_action == PositionMarketAction.DATA_WAIT.value
    assert "POSITION_MARKET_CONTEXT_STALE" in risk.position_action_reason_codes


def _position(position_id: str, code: str, **overrides) -> PositionRuntimeSnapshot:
    payload = {
        "trade_date": TRADE_DATE,
        "calculated_at": NOW.isoformat(),
        "position_id": position_id,
        "candidate_id": None,
        "code": code,
        "name": f"Stock {code}",
        "theme_id": "theme-a",
        "theme_name": "Theme A",
        "source_type": "VIRTUAL",
        "entry_price": 1000,
        "quantity": 10,
        "remaining_quantity": 10,
        "avg_entry_price": 1000.0,
        "opened_at": NOW.isoformat(),
        "holding_minutes": 5,
        "current_price": 1000,
        "current_return_pct": 0.0,
        "max_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "highest_price_since_entry": 1000,
        "lowest_price_since_entry": 1000,
        "realized_return_pct": 0.0,
        "unrealized_return_pct": 0.0,
        "stop_loss_price": 980,
        "take_profit_price": 1050,
        "trailing_stop_price": 0,
        "trailing_active": False,
        "first_profit_taken": False,
        "last_tick_at": NOW.isoformat(),
        "data_quality_flags": (),
        "risk_status": PositionRiskStatus.OPEN,
        "market_side": "KOSDAQ",
        "market_side_source": "test",
        "market_side_resolution_status": "RESOLVED",
        "side_market_regime": "EXPANSION",
        "counterpart_market_regime": "EXPANSION",
        "composite_market_mode": "BROAD_RISK_ON",
        "systemic_risk_off": False,
        "market_context_calculated_at": NOW.isoformat(),
        "market_context_fresh": True,
        "candidate_market_action": "ALLOW_NORMAL",
        "strategy_context_id": "ctx-test",
        "details": {"market_side": "KOSDAQ"},
    }
    payload.update(overrides)
    return PositionRuntimeSnapshot(**payload)


def _market_regime(db: TradingDatabase, *, kospi: str, kosdaq: str, composite: str) -> None:
    db.save_market_regime_snapshot(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": NOW.isoformat(),
            "global_status": "SELECTIVE",
            "kospi_status": kospi,
            "kosdaq_status": kosdaq,
            "market_session_status": "REGULAR",
            "risk_off_detected": composite == "SYSTEMIC_RISK_OFF",
            "weak_market_detected": kospi in {"WEAK", "RISK_OFF"} or kosdaq in {"WEAK", "RISK_OFF"},
            "composite_market_mode": composite,
        }
    )
