from datetime import datetime

from trading.broker.gateway_state import GatewayStateStore
from trading.theme_engine.intraday_discovery import (
    INTRADAY_TURNOVER_SEED_PURPOSE,
    IntradayDiscoveryConfig,
    IntradayDiscoveryScheduler,
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
