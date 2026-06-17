from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.position_risk import (
    PortfolioRiskSnapshot,
    PositionRiskConfig,
    PositionRiskManager,
    PositionRiskRuntimePipeline,
    PositionRiskStatus,
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
        "details": {"market_side": "KOSDAQ"},
    }
    payload.update(overrides)
    return PositionRuntimeSnapshot(**payload)
