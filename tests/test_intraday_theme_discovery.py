from datetime import datetime

from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading.broker.gateway_state import GatewayStateStore
from trading.theme_engine.intraday_discovery import (
    INTRADAY_TURNOVER_SEED_PURPOSE,
    IntradayDiscoveryConfig,
    IntradayDiscoveryScheduler,
    IntradayDiscoveryRuntimePipeline,
)


def test_intraday_discovery_enqueues_observe_only_opt10032_seed():
    gateway_state = GatewayStateStore()
    scheduler = IntradayDiscoveryScheduler(
        gateway_state,
        config=IntradayDiscoveryConfig(
            enabled=True,
            trading_mode="OBSERVE",
            max_pending_commands=99,
            queue_depth_limit=99,
            top_n=100,
        ),
    )

    summary = scheduler.enqueue_if_due(datetime(2026, 6, 19, 9, 21, 0))

    assert summary["status"] == "QUEUED"
    assert summary["phase"] == "MORNING"
    assert summary["ready_allowed"] is False
    assert summary["order_intent_allowed"] is False

    commands = gateway_state.list_commands(status="QUEUED", command_type="tr_request")
    assert len(commands) == 1
    command = commands[0]["command"]
    assert command["type"] == "tr_request"
    assert command["payload"]["purpose"] == INTRADAY_TURNOVER_SEED_PURPOSE
    assert command["payload"]["response_mode"] == "capture"
    assert command["payload"]["tr_code"].lower() == "opt10032"
    assert command["payload"]["top_n"] == 100
    assert command["payload"]["ready_allowed"] is False
    assert command["payload"]["order_intent_allowed"] is False


def test_intraday_discovery_dedupes_same_bucket_without_extra_command():
    gateway_state = GatewayStateStore()
    scheduler = IntradayDiscoveryScheduler(
        gateway_state,
        config=IntradayDiscoveryConfig(
            enabled=True,
            trading_mode="OBSERVE",
            max_pending_commands=99,
            queue_depth_limit=99,
        ),
    )

    first = scheduler.enqueue_if_due(datetime(2026, 6, 19, 9, 21, 0))
    second = scheduler.enqueue_if_due(datetime(2026, 6, 19, 9, 24, 59))

    assert first["status"] == "QUEUED"
    assert second["status"] == "SKIPPED"
    assert second["paused_reason"] == "BUCKET_ALREADY_REQUESTED"
    assert len(gateway_state.list_commands(status="QUEUED", command_type="tr_request")) == 1


def test_intraday_discovery_respects_observe_only_mode():
    scheduler = IntradayDiscoveryScheduler(
        GatewayStateStore(),
        config=IntradayDiscoveryConfig(enabled=True, trading_mode="LIVE"),
    )

    summary = scheduler.enqueue_if_due(datetime(2026, 6, 19, 13, 21, 0))

    assert summary["status"] == "SKIPPED"
    assert summary["paused_reason"] == "NOT_OBSERVE_MODE"


def test_intraday_discovery_respects_configured_start_end_window():
    scheduler = IntradayDiscoveryScheduler(
        GatewayStateStore(),
        config=IntradayDiscoveryConfig(
            enabled=True,
            trading_mode="OBSERVE",
            start="10:00",
            end="10:30",
            max_pending_commands=99,
            queue_depth_limit=99,
        ),
    )

    before = scheduler.enqueue_if_due(datetime(2026, 6, 19, 9, 59, 59))
    inside = scheduler.enqueue_if_due(datetime(2026, 6, 19, 10, 0, 0))
    after = scheduler.enqueue_if_due(datetime(2026, 6, 19, 10, 30, 1))

    assert before["status"] == "SKIPPED"
    assert before["paused_reason"] == "OUTSIDE_DISCOVERY_WINDOW"
    assert inside["status"] == "QUEUED"
    assert after["status"] == "SKIPPED"
    assert after["paused_reason"] == "OUTSIDE_DISCOVERY_WINDOW"


def test_intraday_discovery_ack_saves_idempotent_batch_rows(tmp_path):
    db = TradingDatabase(str(tmp_path / "intraday-ack.db"))
    command_id = "cmd-intraday-1"
    pipeline = IntradayDiscoveryRuntimePipeline(
        gateway_state=GatewayStateStore(),
        db=db,
        config=IntradayDiscoveryConfig(enabled=True, trading_mode="OBSERVE"),
    )
    event = GatewayEvent(
        type="command_ack",
        command_id=command_id,
        timestamp="2026-06-19T09:21:05",
        payload={
            "purpose": INTRADAY_TURNOVER_SEED_PURPOSE,
            "command_id": command_id,
            "trade_date": "2026-06-19",
            "session_phase": "MORNING",
            "bucket": "09:20",
            "observed_at": "2026-06-19T09:21:00",
            "raw": {
                "tr_rows": [
                    {"종목코드": "A000001", "종목명": "One", "현재순위": "1", "거래대금": "12000000000", "등락률": "+6.4"},
                    {"종목코드": "000002", "종목명": "Two", "현재순위": "2", "거래대금": "9000000000", "등락률": "+5.5"},
                ]
            },
        },
    )

    assert pipeline.handle_event(event) is True
    assert pipeline.handle_event(event) is True

    batches = db.list_intraday_theme_discovery_batches(trade_date="2026-06-19")
    rows = db.list_intraday_theme_discovery_rows(trade_date="2026-06-19")

    assert len(batches) == 1
    assert batches[0]["command_id"] == command_id
    assert batches[0]["status"] == "OK"
    assert len(rows) == 2
    assert rows[0]["stock_code"] == "000001"
    assert rows[0]["current_turnover_krw"] == 12_000_000_000
    assert pipeline.last_summary["ready_allowed"] is False
    assert pipeline.last_summary["order_intent_allowed"] is False


def test_intraday_discovery_recovery_is_idempotent_across_repeated_runs(tmp_path):
    db = TradingDatabase(str(tmp_path / "intraday-recovery.db"))
    gateway_state = GatewayStateStore()
    pipeline = IntradayDiscoveryRuntimePipeline(
        gateway_state=gateway_state,
        db=db,
        config=IntradayDiscoveryConfig(
            enabled=True,
            trading_mode="OBSERVE",
            max_pending_commands=99,
            queue_depth_limit=99,
        ),
    )
    queued = pipeline.run_if_due(datetime(2026, 6, 19, 9, 21, 0))
    command_id = gateway_state.dispatch_commands(limit=1, now=datetime(2026, 6, 19, 9, 21, 1))[0].command_id
    gateway_state.ack_command(
        command_id,
        "ACKED",
        result_payload={
            "purpose": INTRADAY_TURNOVER_SEED_PURPOSE,
            "observed_at": "2026-06-19T09:21:00",
            "raw": {
                "tr_rows": [
                    {"종목코드": "A000001", "종목명": "One", "현재순위": "1", "거래대금": "12000000000", "등락률": "+6.4"},
                    {"종목코드": "000002", "종목명": "Two", "현재순위": "2", "거래대금": "9000000000", "등락률": "+5.5"},
                ]
            },
        },
    )

    summaries = [pipeline.recover_from_command_history(limit=100) for _ in range(100)]

    batches = db.list_intraday_theme_discovery_batches(trade_date="2026-06-19")
    rows = db.list_intraday_theme_discovery_rows(trade_date="2026-06-19")

    assert queued["status"] == "QUEUED"
    assert sum(summary["recovered_count"] for summary in summaries) == 1
    assert sum(summary["duplicate_skipped_count"] for summary in summaries) == 99
    assert sum(summary["unique_constraint_error_count"] for summary in summaries) == 0
    assert len(batches) == 1
    assert len(rows) == 2


def test_intraday_discovery_failed_ack_saves_batch_without_seed_rows(tmp_path):
    db = TradingDatabase(str(tmp_path / "intraday-failed.db"))
    pipeline = IntradayDiscoveryRuntimePipeline(
        gateway_state=GatewayStateStore(),
        db=db,
        config=IntradayDiscoveryConfig(enabled=True, trading_mode="OBSERVE"),
    )

    handled = pipeline.handle_event(
        GatewayEvent(
            type="command_failed",
            command_id="cmd-failed",
            timestamp="2026-06-19T13:21:05",
            payload={
                "purpose": INTRADAY_TURNOVER_SEED_PURPOSE,
                "command_id": "cmd-failed",
                "trade_date": "2026-06-19",
                "session_phase": "AFTERNOON",
                "bucket": "13:20",
                "error": "timeout",
                "rows": [{"종목코드": "000001"}],
            },
        )
    )

    batches = db.list_intraday_theme_discovery_batches(trade_date="2026-06-19", status="FAILED")
    rows = db.list_intraday_theme_discovery_rows(trade_date="2026-06-19")

    assert handled is True
    assert len(batches) == 1
    assert batches[0]["parsed_count"] == 0
    assert rows == []
