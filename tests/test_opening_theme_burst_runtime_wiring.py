from __future__ import annotations

from datetime import datetime

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.theme_engine.leadership import StockLeadershipRole, ThemeLeadershipStatus
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.opening_burst import OpeningThemeBurstEngine
from trading.theme_engine.opening_runtime import (
    OPENING_RQ_NAME,
    OPENING_TR_CODE,
    OPENING_TURNOVER_SEED_PURPOSE,
    OpeningBurstRuntimeConfig,
    OpeningBurstScheduler,
    OpeningThemeBurstRuntimePipeline,
    opening_theme_burst_dashboard_section,
    parse_opt10032_seed_rows,
)
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot


def test_scheduler_enqueues_opt10032_only_at_seed_times():
    gateway_state = GatewayStateStore()
    scheduler = OpeningBurstScheduler(gateway_state, config=_config())

    off_time = scheduler.enqueue_if_due(_dt("2026-06-15T09:02:00"))
    first = scheduler.enqueue_if_due(_dt("2026-06-15T09:03:00"))
    second = scheduler.enqueue_if_due(_dt("2026-06-15T09:06:00"))

    commands = gateway_state.list_commands(include_finished=True, limit=10)
    assert off_time["enqueued"] is False
    assert off_time["paused_reason"] == "NOT_SEED_TIME"
    assert first["enqueued"] is True
    assert second["enqueued"] is True
    assert [item["command_type"] for item in commands] == ["tr_request", "tr_request"]
    payload = commands[0]["command"]["payload"]
    assert payload["purpose"] == OPENING_TURNOVER_SEED_PURPOSE
    assert payload["response_mode"] == "capture"
    assert payload["tr_code"] == OPENING_TR_CODE
    assert payload["rq_name"] == OPENING_RQ_NAME
    assert payload["screen_no"] == "8720"


def test_scheduler_idempotency_blocks_duplicate_seed_time():
    gateway_state = GatewayStateStore()
    scheduler = OpeningBurstScheduler(gateway_state, config=_config())

    first = scheduler.enqueue_if_due(_dt("2026-06-15T09:03:00"))
    duplicate = scheduler.enqueue_if_due(_dt("2026-06-15T09:03:30"))

    commands = gateway_state.list_commands(include_finished=True, limit=10)
    assert first["enqueued"] is True
    assert duplicate["duplicate"] is True
    assert duplicate["idempotency_key"] == first["idempotency_key"]
    assert len(commands) == 1


def test_scheduler_hard_pauses_outside_observe_or_regular_session(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        gateway_state = GatewayStateStore()
        pipeline = OpeningThemeBurstRuntimePipeline(
            db=db,
            gateway_state=gateway_state,
            market_data=MarketDataStore(),
            repository=ThemeEngineRepository(db),
            config=_config(trading_mode="LIVE"),
        )

        summary = pipeline.run(_dt("2026-06-15T09:03:00"))

        assert summary["status"] == "SKIPPED"
        assert summary["scheduler"]["paused_reason"] == "NOT_OBSERVE_MODE"
        assert gateway_state.list_commands(include_finished=True) == []
    finally:
        db.close()


def test_opt10032_parser_uses_field_name_fallbacks():
    parsed = parse_opt10032_seed_rows(
        [
            {
                "\uc885\ubaa9\ucf54\ub4dc": "A000001",
                "\uc885\ubaa9\uba85": "stock-1",
                "\ud604\uc7ac\uac00": "+10100",
                "\ub4f1\ub77d\ub960": "+5.2%",
                "\uac70\ub798\ub300\uae08": "9,000,000,000",
                "\uac70\ub798\ub7c9": "120,000",
                "\uc21c\uc704": "1",
            }
        ],
        batch_time="09:03",
    )

    assert parsed.parser_status == "OK"
    assert parsed.parsed_count == 1
    row = parsed.rows[0]
    assert row.seed.stock_code == "000001"
    assert row.seed.stock_name == "stock-1"
    assert row.seed.turnover_krw == 9_000_000_000
    assert row.seed.change_rate_pct == 5.2
    assert row.current_price == 10100
    assert row.volume == 120000
    assert row.seed.raw["\uc885\ubaa9\ucf54\ub4dc"] == "A000001"


def test_parser_missing_fields_preserves_raw_row_and_does_not_crash(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        pipeline = OpeningThemeBurstRuntimePipeline(
            db=db,
            gateway_state=GatewayStateStore(),
            market_data=MarketDataStore(),
            repository=ThemeEngineRepository(db),
            config=_config(),
        )

        handled = pipeline.handle_event(
            GatewayEvent(
                type="command_ack",
                command_id="cmd-missing",
                payload={
                    "purpose": OPENING_TURNOVER_SEED_PURPOSE,
                    "command_id": "cmd-missing",
                    "trade_date": "2026-06-15",
                    "seed_time": "09:03",
                    "raw": {"tr_rows": [{"\uc885\ubaa9\uba85": "nameless"}]},
                },
            )
        )

        batches = db.list_opening_turnover_seed_batches(trade_date="2026-06-15")
        rows = db.list_opening_turnover_seed_rows(batch_id=batches[0]["id"])
        assert handled is True
        assert batches[0]["parser_status"] == "MISSING_REQUIRED_FIELDS"
        assert rows[0]["stock_name"] == "nameless"
        assert rows[0]["raw"]["\uc885\ubaa9\uba85"] == "nameless"
        assert "stock_code" in rows[0]["parser_missing_fields"]
    finally:
        db.close()


def test_runtime_computes_observe_result_without_condition_profiles(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        gateway_state = GatewayStateStore()
        market_data = MarketDataStore()
        repo = ThemeEngineRepository(db)
        _seed_theme(repo)
        _seed_batch(db, "2026-06-15", [("000001", 1, 9_000_000_000), ("000002", 2, 7_000_000_000), ("000003", 3, 4_000_000_000)])
        _tick(market_data, "000001", 6.0, 9_000_000_000, speed=1_500_000_000, execution=155)
        _tick(market_data, "000002", 5.0, 7_000_000_000, speed=1_100_000_000, execution=145)
        _tick(market_data, "000003", 3.4, 4_000_000_000, speed=800_000_000, execution=130)

        pipeline = OpeningThemeBurstRuntimePipeline(
            db=db,
            gateway_state=gateway_state,
            market_data=market_data,
            repository=repo,
            config=_config(),
        )
        summary = pipeline.run(_dt("2026-06-15T09:04:00"))

        assert summary["status"] == "OK"
        assert summary["ready_allowed"] is False
        assert summary["order_intent_allowed"] is False
        assert summary["output_mode"] == "OBSERVE"
        assert summary["selected_symbols"]
        assert db.latest_opening_theme_burst_result(trade_date="2026-06-15")["ready_allowed"] is False
        assert all(item["command_type"] != "send_order" for item in gateway_state.list_commands(include_finished=True))
    finally:
        db.close()


def test_register_realtime_targets_respect_max_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        gateway_state = GatewayStateStore()
        repo = ThemeEngineRepository(db)
        _seed_batch(
            db,
            "2026-06-15",
            [(f"{index:06d}", index, 10_000_000_000 - index) for index in range(1, 21)],
        )
        pipeline = OpeningThemeBurstRuntimePipeline(
            db=db,
            gateway_state=gateway_state,
            market_data=MarketDataStore(),
            repository=repo,
            config=_config(max_realtime_register=5),
        )

        summary = pipeline.run(_dt("2026-06-15T09:04:00"))
        register_commands = [
            item for item in gateway_state.list_commands(include_finished=True, limit=10) if item["command_type"] == "register_realtime"
        ]

        assert summary["realtime_registration"]["target_count"] == 5
        assert summary["realtime_registered_count"] == 5
        assert len(register_commands) == 1
        assert len(register_commands[0]["command"]["payload"]["codes"]) == 5
    finally:
        db.close()


def test_leader_only_theme_runtime_excludes_late_laggard_and_overheated():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[
            (
                "two_top",
                "TwoTop",
                [_member("two_top", code) for code in ("000001", "000002", "000003", "000004", "000005")],
            )
        ],
        seed_batches=[
            [
                {"stock_code": "000001", "stock_name": "stock-000001", "rank": 1, "turnover_krw": 12_000_000_000},
                {"stock_code": "000002", "stock_name": "stock-000002", "rank": 2, "turnover_krw": 9_000_000_000},
                {"stock_code": "000003", "stock_name": "stock-000003", "rank": 5, "turnover_krw": 3_000_000_000},
                {"stock_code": "000004", "stock_name": "stock-000004", "rank": 8, "turnover_krw": 1_000_000_000},
                {"stock_code": "000005", "stock_name": "stock-000005", "rank": 9, "turnover_krw": 800_000_000},
            ]
        ],
        snapshots=[
            _snapshot("000001", 6.0, 12_000_000_000, speed=1_400_000_000, execution=160),
            _snapshot("000002", 5.5, 9_000_000_000, speed=1_100_000_000, execution=150),
            _snapshot("000003", 2.0, 3_000_000_000, speed=500_000_000, execution=120),
            _snapshot("000004", 0.5, 1_000_000_000, speed=150_000_000, execution=90),
            _snapshot("000005", 8.0, 800_000_000, speed=100_000_000, execution=100, pullback=0.0),
        ],
    )
    theme = result.ranked_themes[0].snapshot

    assert theme is not None
    assert theme.status == ThemeLeadershipStatus.LEADER_ONLY_THEME
    assert any(stock.role == StockLeadershipRole.LATE_LAGGARD for stock in theme.stocks)
    assert any(stock.role == StockLeadershipRole.OVERHEATED for stock in theme.stocks)
    assert all(stock.role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER} for stock in result.selected)


def test_dashboard_snapshot_includes_opening_theme_burst_section(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        today = datetime.now().date().isoformat()
        db.save_opening_turnover_seed_batch(
            {
                "trade_date": today,
                "batch_time": "09:03",
                "command_id": "cmd-dashboard",
                "row_count": 1,
                "parsed_count": 1,
                "parser_status": "OK",
                "rows": [{"stock_code": "000001", "stock_name": "stock-000001", "rank": 1, "turnover_krw": 1_000_000_000}],
            }
        )
        db.save_opening_theme_burst_result(
            {
                "trade_date": today,
                "calculated_at": f"{today}T09:05:00",
                "output_mode": "OBSERVE",
                "ready_allowed": False,
                "order_intent_allowed": False,
                "seed_batch_count": 1,
                "seed_symbol_count": 1,
                "realtime_registered_count": 1,
                "selected_symbols": ["000001"],
                "top_themes": [{"theme_id": "ai", "theme_name": "AI", "rank": 1}],
                "payload": {"warnings": [], "parser_status": "OK", "selected_symbols": ["000001"], "top_themes": []},
            }
        )

        section = opening_theme_burst_dashboard_section(db, trade_date=today)
        snapshot = build_theme_lab_dashboard_snapshot(db, include_extended=False)

        assert section["selected_symbols"] == ["000001"]
        assert snapshot["opening_theme_burst"]["seed_batch_count"] == 1
        assert snapshot["opening_theme_burst"]["ready_allowed"] is False
    finally:
        db.close()


def _config(*, trading_mode: str = "OBSERVE", max_realtime_register: int = 100) -> OpeningBurstRuntimeConfig:
    return OpeningBurstRuntimeConfig(
        enabled=True,
        observe_only=True,
        trading_mode=trading_mode,
        seed_times=("09:03", "09:06", "09:09", "09:12", "09:15"),
        top_n_per_call=100,
        max_union_size=300,
        max_realtime_register=max_realtime_register,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _seed_theme(repo: ThemeEngineRepository) -> None:
    repo.upsert_canonical_theme(
        CanonicalTheme(
            theme_id="ai",
            canonical_name="AI",
            display_name="AI",
            status=ThemeStatus.ACTIVE,
            confidence=1.0,
            trade_eligible=True,
        )
    )
    for code in ("000001", "000002", "000003", "000004"):
        repo.upsert_current_membership(_member("ai", code))


def _member(theme_id: str, code: str) -> ThemeMembership:
    return ThemeMembership(
        theme_id=theme_id,
        stock_code=code,
        stock_name=f"stock-{code}",
        membership_score=0.9,
        active=True,
        trade_eligible=True,
    )


def _seed_batch(db: TradingDatabase, trade_date: str, rows: list[tuple[str, int, float]]) -> None:
    db.save_opening_turnover_seed_batch(
        {
            "trade_date": trade_date,
            "batch_time": "09:03",
            "command_id": f"cmd-seed-{trade_date}-{len(rows)}",
            "row_count": len(rows),
            "parsed_count": len(rows),
            "parser_status": "OK",
            "rows": [
                {
                    "stock_code": code,
                    "stock_name": f"stock-{code}",
                    "rank": rank,
                    "turnover_krw": turnover,
                    "change_rate_pct": 0.0,
                    "raw": {},
                }
                for code, rank, turnover in rows
            ],
        }
    )


def _tick(
    market_data: MarketDataStore,
    code: str,
    change_rate: float,
    turnover: float,
    *,
    speed: float,
    execution: float,
    pullback: float = 2.0,
) -> None:
    market_data.update_tick(
        StrategyTick.from_realtime(
            code,
            price=10000,
            change_rate=change_rate,
            cum_volume=100_000,
            best_bid=9990,
            best_ask=10010,
            trade_value=turnover,
            execution_strength=execution,
            timestamp=_dt("2026-06-15T09:04:00"),
            metadata=_snapshot_metadata(speed=speed, pullback=pullback),
        )
    )


def _snapshot(
    code: str,
    change_rate: float,
    turnover: float,
    *,
    speed: float,
    execution: float,
    pullback: float = 2.0,
):
    from trading.theme_engine.models import StockSnapshot

    current_price = 10000.0
    high = current_price / (1.0 - pullback / 100.0) if pullback < 100 else current_price
    return StockSnapshot(
        stock_code=code,
        stock_name=f"stock-{code}",
        current_price=current_price,
        change_rate=change_rate,
        volume=100_000,
        turnover=turnover,
        execution_strength=execution,
        best_bid=9990,
        best_ask=10010,
        session_high=high,
        momentum_1m=1.0,
        momentum_3m=1.0,
        momentum_5m=1.0,
        metadata=_snapshot_metadata(speed=speed, pullback=pullback),
    )


def _snapshot_metadata(*, speed: float, pullback: float) -> dict:
    return {
        "opening_turnover_speed_krw_per_min": speed,
        "avg_turnover_20d_krw": 20_000_000_000,
        "minutes_since_open": 5,
        "pullback_from_high_pct": pullback,
        "upper_limit_gap_pct": 10.0,
        "vi_active": False,
        "vwap": 9800.0,
        "momentum_1m": 1.0,
        "momentum_3m": 1.0,
        "momentum_5m": 1.0,
    }
