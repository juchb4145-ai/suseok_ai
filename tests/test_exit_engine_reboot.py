from datetime import datetime, timedelta

from storage.db import TradingDatabase
from trading.strategy.candles import CandleBuilder
from trading.strategy.exit_engine_reboot import (
    ExitDecisionStatus,
    ExitEngine,
    ExitEngineConfig,
    ExitEngineRuntimePipeline,
    ExitReason,
    exit_engine_dashboard_section,
)
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateSourceType, CandidateState, VirtualPosition
from trading.strategy.position_risk import PositionMarketAction, PositionRiskStatus, PositionRuntimeService, PositionRuntimeSnapshot


TRADE_DATE = "2026-06-18"
NOW = datetime(2026, 6, 18, 9, 10, 0)


def test_virtual_filled_buy_position_becomes_runtime_open_position(tmp_path):
    db, market_data, candles = _context(tmp_path)
    candidate = _candidate(db, "100001")
    db.save_virtual_position(VirtualPosition(candidate_id=candidate.id, virtual_order_id=1, entry_price=1000, quantity=10, opened_at=(NOW - timedelta(minutes=5)).isoformat()))
    _tick(market_data, candles, "100001", 1010, NOW)

    result = PositionRuntimeService(db, market_data=market_data, candle_builder=candles).build(trade_date=TRADE_DATE, now=NOW)

    assert len(result.positions) == 1
    assert result.positions[0].code == "100001"
    assert result.positions[0].risk_status == PositionRiskStatus.OPEN


def test_same_candidate_code_does_not_create_duplicate_runtime_position(tmp_path):
    db, market_data, candles = _context(tmp_path)
    candidate = _candidate(db, "100002")
    db.save_virtual_position(VirtualPosition(candidate_id=candidate.id, virtual_order_id=1, entry_price=1000, quantity=10, opened_at=(NOW - timedelta(minutes=5)).isoformat()))
    db.save_virtual_position(VirtualPosition(candidate_id=candidate.id, virtual_order_id=2, entry_price=1010, quantity=5, opened_at=(NOW - timedelta(minutes=4)).isoformat()))
    _tick(market_data, candles, "100002", 1015, NOW)

    result = PositionRuntimeService(db, market_data=market_data, candle_builder=candles).build(trade_date=TRADE_DATE, now=NOW)

    assert len(result.positions) == 1


def test_stop_loss_breach_is_exit_now(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=970, holding_minutes=10, current_return_pct=-3.0)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.STOP_LOSS


def test_fast_stop_loss_has_priority_in_early_minutes(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=985, holding_minutes=2, current_return_pct=-1.5)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.STOP_LOSS_FAST


def test_take_profit_creates_scale_out_decision(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1060, current_return_pct=6.0)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.SCALE_OUT
    assert decision.exit_reason == ExitReason.TAKE_PROFIT
    assert decision.quantity == 5


def test_trailing_stop_exits_after_profit_protection(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1025, current_return_pct=2.5, max_return_pct=5.0, highest_price_since_entry=1050, trailing_stop_price=1037, trailing_active=True)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.TRAILING_STOP


def test_max_hold_without_required_return_is_time_exit(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1000, current_return_pct=0.0, holding_minutes=31)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.TIME_EXIT


def test_theme_weak_exit_requires_confirmation(tmp_path):
    db, _, candles = _context(tmp_path)
    waiting = _position(details={"theme_status": "WEAK_THEME", "theme_weak_confirmation_count": 1})
    confirmed = _position(position_id="virtual:2", details={"theme_status": "WEAK_THEME", "theme_weak_confirmation_count": 2})

    decisions = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[waiting, confirmed]).decisions

    assert decisions[0].exit_status == ExitDecisionStatus.WAIT_CONFIRMATION
    assert decisions[1].exit_status == ExitDecisionStatus.EXIT_NOW
    assert decisions[1].exit_reason == ExitReason.THEME_WEAK_EXIT


def test_leader_collapse_exit_fires_on_confirmed_leader_break(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(details={"leader_vwap_broken": True, "leader_collapse_confirmation_count": 1})

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.LEADER_COLLAPSE_EXIT


def test_market_risk_off_exit_now(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(details={"market_status": "RISK_OFF"})
    _position_risk(db, position, PositionMarketAction.EXIT_NOW.value, ["SIDE_MARKET_RISK_OFF_POSITION_RISK", "POSITION_MARKET_EXIT_NOW"])

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.EXIT_NOW
    assert decision.exit_reason == ExitReason.MARKET_RISK_OFF_EXIT


def test_weak_market_waits_when_position_is_profitable(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1010, current_return_pct=1.0, details={"market_status": "WEAK"})
    _position_risk(db, position, PositionMarketAction.TIGHTEN_STOP.value, ["SIDE_MARKET_WEAK_POSITION_RISK", "POSITION_MARKET_TIGHTEN_STOP"])

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.WAIT_CONFIRMATION
    assert decision.exit_reason == ExitReason.MARKET_WEAK_EXIT


def test_block_new_entry_is_not_position_sell_signal(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(details={"market_action": "BLOCK_NEW_ENTRY", "market_status": "RISK_OFF"})

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.HOLD
    assert decision.exit_reason == ExitReason.HOLD


def test_stale_tick_becomes_data_wait_and_no_sell_intent(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(risk_status=PositionRiskStatus.STALE_DATA_RISK, data_quality_flags=("LATEST_TICK_STALE",))

    result = _engine(db, candles, allow_dry_run=True).build(trade_date=TRADE_DATE, now=NOW, positions=[position])

    assert result.decisions[0].exit_status == ExitDecisionStatus.DATA_WAIT
    assert result.dry_run_sell_intent_count == 0
    assert db.latest_dry_run_sell_intents(trade_date=TRADE_DATE) == []


def test_ambiguous_candle_prefers_stop_loss_and_marks_details(tmp_path):
    db, market_data, candles = _context(tmp_path)
    _ambiguous_candle(market_data, candles, "100003")
    position = _position(code="100003", current_price=1000, current_return_pct=0.0, stop_loss_price=980, take_profit_price=1050)

    decision = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_reason == ExitReason.STOP_LOSS
    assert decision.details["ambiguous_bar"] is True


def test_dry_run_sell_intent_disabled_by_default(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1060, current_return_pct=6.0)

    result = _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position])

    assert result.decisions[0].exit_status == ExitDecisionStatus.SCALE_OUT
    assert result.dry_run_sell_intent_count == 0
    assert db.latest_dry_run_sell_intents(trade_date=TRADE_DATE) == []


def test_dry_run_sell_intent_is_idempotent_when_enabled(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1060, current_return_pct=6.0)
    engine = _engine(db, candles, allow_dry_run=True)

    first = engine.build(trade_date=TRADE_DATE, now=NOW, positions=[position])
    second = engine.build(trade_date=TRADE_DATE, now=NOW, positions=[position])

    assert first.dry_run_sell_intent_count == 1
    assert second.dry_run_sell_intent_count == 0
    intents = db.latest_dry_run_sell_intents(trade_date=TRADE_DATE)
    assert len(intents) == 1
    assert intents[0]["idempotency_key"].startswith("reboot_exit_dry_run:")
    assert intents[0]["gateway_command_created"] is False


def test_remaining_quantity_zero_is_already_closed(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(remaining_quantity=0)

    decision = _engine(db, candles, allow_dry_run=True).build(trade_date=TRADE_DATE, now=NOW, positions=[position]).decisions[0]

    assert decision.exit_status == ExitDecisionStatus.ALREADY_CLOSED
    assert decision.dry_run_sell_intent_allowed is False


def test_exit_engine_dashboard_and_runtime_never_create_live_order(tmp_path):
    db, market_data, candles = _context(tmp_path)
    candidate = _candidate(db, "100004")
    db.save_virtual_position(VirtualPosition(candidate_id=candidate.id, virtual_order_id=1, entry_price=1000, quantity=10, opened_at=(NOW - timedelta(minutes=5)).isoformat()))
    _tick(market_data, candles, "100004", 1060, NOW)
    pipeline = ExitEngineRuntimePipeline(
        db=db,
        market_data=market_data,
        candle_builder=candles,
        config=ExitEngineConfig(enabled=True, allow_dry_run_sell_intents=True),
    )

    summary = pipeline.run_if_due(NOW)
    section = exit_engine_dashboard_section(db, trade_date=TRADE_DATE)

    assert summary["scale_out_count"] == 1
    assert section["status"] == "OK"
    assert section["dry_run_sell_intent_count"] == 1
    assert db.list_runtime_order_intents(limit=10) == []
    assert db.conn.execute("SELECT COUNT(*) AS count FROM order_results").fetchone()["count"] == 0


def test_exit_engine_dashboard_ignores_stale_decisions_when_latest_portfolio_is_flat(tmp_path):
    db, _, candles = _context(tmp_path)
    position = _position(current_price=1060, current_return_pct=6.0)
    _engine(db, candles).build(trade_date=TRADE_DATE, now=NOW, positions=[position])
    db.save_portfolio_risk_snapshot(
        {
            "trade_date": TRADE_DATE,
            "calculated_at": (NOW + timedelta(seconds=30)).isoformat(),
            "open_position_count": 0,
            "total_exposure": 0,
        }
    )

    section = exit_engine_dashboard_section(db, trade_date=TRADE_DATE)

    assert section["status"] == "EMPTY"


def _context(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    market_data = MarketDataStore()
    candles = CandleBuilder()
    return db, market_data, candles


def _candidate(db: TradingDatabase, code: str) -> Candidate:
    return db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code=code,
            name=f"Stock {code}",
            sources=[CandidateSourceType.CONDITION_SEARCH],
            state=CandidateState.WATCHING,
            detected_at=NOW.isoformat(),
            last_seen_at=NOW.isoformat(),
            metadata={
                "theme_board_theme_id": "theme-a",
                "theme_board_theme_name": "Theme A",
                "theme_board_theme_status": "LEADING_THEME",
                "theme_board_stock_role": "LEADER",
                "market_side": "KOSDAQ",
                "market_regime_status": "EXPANSION",
                "market_action": "ALLOW_NORMAL",
            },
        )
    )


def _engine(db, candles, *, allow_dry_run: bool = False) -> ExitEngine:
    return ExitEngine(db, candle_builder=candles, config=ExitEngineConfig(enabled=True, allow_dry_run_sell_intents=allow_dry_run))


def _position(**overrides) -> PositionRuntimeSnapshot:
    payload = {
        "trade_date": TRADE_DATE,
        "calculated_at": NOW.isoformat(),
        "position_id": "virtual:1",
        "candidate_id": 1,
        "code": "100000",
        "name": "Stock 100000",
        "theme_id": "theme-a",
        "theme_name": "Theme A",
        "source_type": "VIRTUAL",
        "entry_price": 1000,
        "quantity": 10,
        "remaining_quantity": 10,
        "avg_entry_price": 1000.0,
        "opened_at": (NOW - timedelta(minutes=5)).isoformat(),
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
        "details": {},
    }
    payload.update(overrides)
    return PositionRuntimeSnapshot(**payload)


def _position_risk(db: TradingDatabase, position: PositionRuntimeSnapshot, action: str, reasons: list[str]) -> None:
    db.save_position_risk_snapshots(
        [
            {
                "trade_date": TRADE_DATE,
                "calculated_at": NOW.isoformat(),
                "position_id": position.position_id,
                "candidate_id": position.candidate_id,
                "code": position.code,
                "risk_status": position.risk_status.value,
                "risk_level": "REDUCE" if action == PositionMarketAction.EXIT_NOW.value else "CAUTION",
                "position_market_action": action,
                "recommended_exit_ratio": 1.0 if action == PositionMarketAction.EXIT_NOW.value else 0.0,
                "position_action_reason_codes": reasons,
                "reason_codes": reasons,
                "details": {
                    "position_market_action": action,
                    "recommended_exit_ratio": 1.0 if action == PositionMarketAction.EXIT_NOW.value else 0.0,
                    "position_action_reason_codes": reasons,
                },
            }
        ]
    )


def _tick(market_data: MarketDataStore, candles: CandleBuilder, code: str, price: int, timestamp: datetime) -> None:
    tick = StrategyTick.from_realtime(code, price=price, cum_volume=100, trade_value=100000, timestamp=timestamp)
    market_data.update_tick(tick)
    candles.update(tick)


def _ambiguous_candle(market_data: MarketDataStore, candles: CandleBuilder, code: str) -> None:
    _tick(market_data, candles, code, 1000, NOW.replace(minute=8, second=0))
    _tick(market_data, candles, code, 1060, NOW.replace(minute=8, second=10))
    _tick(market_data, candles, code, 970, NOW.replace(minute=8, second=20))
    _tick(market_data, candles, code, 1000, NOW.replace(minute=9, second=0))
