from datetime import datetime

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.subscription_lifecycle import RealtimeSubscriptionLifecycleTracker
from trading.strategy.subscription_readiness import RealtimeSubscriptionReadinessProvider
from trading_app.runtime_adapters import GatewayCommandRealtimeClient


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _runtime(tmp_path):
    db = TradingDatabase(str(tmp_path / "subscription-lifecycle.db"))
    clock = _Clock(datetime(2026, 6, 22, 9, 5, 0))
    state = GatewayStateStore()
    client = GatewayCommandRealtimeClient(state)
    tracker = RealtimeSubscriptionLifecycleTracker(db, clock=clock, max_tick_age_sec=10)
    manager = RealTimeSubscriptionManager(client, max_codes=10, clock=clock, lifecycle_tracker=tracker)
    return db, clock, state, tracker, manager


def test_register_enqueue_is_not_active_until_gateway_ack(tmp_path):
    db, clock, state, tracker, manager = _runtime(tmp_path)
    try:
        manager.ensure_subscription("A005930", "reboot_v2_candidate")
        active = manager.sync()

        assert active == set()
        assert manager.code_to_screen == {}
        assert manager.pending_register_by_code == {"005930": "7000"}

        snapshot = tracker.snapshot("005930")
        assert snapshot["lifecycle_state"] == "COMMAND_ENQUEUED"
        assert snapshot["command_enqueued"] is True
        assert snapshot["transport_active"] is False
        assert snapshot["first_tick_verified"] is False

        readiness = RealtimeSubscriptionReadinessProvider(
            manager,
            clock=clock,
            lifecycle_tracker=tracker,
            max_tick_age_sec=10,
        ).snapshot("005930", now=clock.value)
        assert readiness["subscription_active"] is False
        assert readiness["subscription_lifecycle_state"] == "COMMAND_ENQUEUED"
        assert readiness["post_subscription_tick_verified"] is False
    finally:
        db.close()


def test_ack_waits_for_first_realtime_tick_after_gateway_baseline(tmp_path):
    db, clock, state, tracker, manager = _runtime(tmp_path)
    try:
        manager.ensure_subscription("005930", "reboot_v2_candidate")
        manager.sync()
        command = state.dispatch_commands(limit=1)[0]
        base_payload = {
            "command_id": command.command_id,
            "command_type": command.type,
            "codes": list(command.payload.get("codes") or []),
            "screen_no": command.payload.get("screen_no"),
            "subscription_session_id": command.payload.get("subscription_session_id"),
            "subscription_generation": command.payload.get("subscription_generation"),
            "target_digest": command.payload.get("target_digest"),
        }

        clock.value = datetime(2026, 6, 22, 9, 5, 1)
        manager.handle_realtime_command_event(
            GatewayEvent(
                type="command_started",
                payload={
                    **base_payload,
                    "transport_trace": {"gateway_command_started_at_utc": "2026-06-22T09:05:01Z"},
                },
            )
        )

        clock.value = datetime(2026, 6, 22, 9, 5, 2)
        manager.handle_realtime_command_event(
            GatewayEvent(
                type="command_ack",
                payload={
                    **base_payload,
                    "status": "ACKED",
                    "transport_trace": {
                        "gateway_kiwoom_call_started_at_utc": "2026-06-22T09:05:01.100Z",
                        "gateway_kiwoom_call_finished_at_utc": "2026-06-22T09:05:02Z",
                        "gateway_command_ack_created_at_utc": "2026-06-22T09:05:02.050Z",
                    },
                },
            )
        )

        acked = tracker.snapshot("005930")
        assert manager.code_to_screen == {"005930": "7000"}
        assert manager.pending_register_by_code == {}
        assert acked["lifecycle_state"] == "ACKED_WAIT_FIRST_TICK"
        assert acked["acked"] is True
        assert acked["transport_active"] is True
        assert acked["first_tick_verified"] is False
        assert acked["registration_ack_baseline_at_utc"] == "2026-06-22T09:05:02.000Z"

        clock.value = datetime(2026, 6, 22, 9, 5, 4)
        manager.handle_price_tick(
            {
                "code": "005930",
                "price": 70000,
                "timestamp": "2026-06-22T09:05:03Z",
                "transport_trace": {
                    "gateway_received_at_utc": "2026-06-22T09:05:03Z",
                    "core_event_received_at_utc": "2026-06-22T09:05:03.100Z",
                },
            }
        )

        fresh = tracker.snapshot("005930")
        assert fresh["lifecycle_state"] == "ACTIVE_FRESH"
        assert fresh["first_tick_verified"] is True
        assert fresh["decision_fresh"] is True
        assert fresh["last_tick_at_utc"] == "2026-06-22T09:05:03.000Z"
        assert fresh["ack_to_first_tick_ms"] == 1000.0

        rows = db.list_realtime_subscription_lifecycle_latest(trade_date="2026-06-22")
        assert rows[0]["code"] == "005930"
        assert rows[0]["lifecycle_state"] == "ACTIVE_FRESH"

        clock.value = datetime(2026, 6, 22, 9, 5, 20)
        stale_readiness = RealtimeSubscriptionReadinessProvider(
            manager,
            clock=clock,
            lifecycle_tracker=tracker,
            max_tick_age_sec=10,
        ).snapshot("005930", now=clock.value)
        assert stale_readiness["subscription_lifecycle_state"] == "ACTIVE_STALE"
        assert stale_readiness["decision_fresh"] is False
        assert stale_readiness["stale"] is True
        assert stale_readiness["post_subscription_tick_verified"] is False
    finally:
        db.close()


def test_pre_ack_tick_does_not_verify_first_tick(tmp_path):
    db, clock, state, tracker, manager = _runtime(tmp_path)
    try:
        manager.ensure_subscription("005930", "reboot_v2_candidate")
        manager.sync()
        command = state.dispatch_commands(limit=1)[0]
        payload = {
            "command_id": command.command_id,
            "command_type": command.type,
            "codes": list(command.payload.get("codes") or []),
            "screen_no": command.payload.get("screen_no"),
            "status": "ACKED",
            "transport_trace": {
                "gateway_kiwoom_call_finished_at_utc": "2026-06-22T09:05:02Z",
                "gateway_command_ack_created_at_utc": "2026-06-22T09:05:02Z",
            },
        }

        manager.handle_price_tick({"code": "005930", "timestamp": "2026-06-22T09:05:01Z"})
        manager.handle_realtime_command_event(GatewayEvent(type="command_ack", payload=payload))

        snapshot = tracker.snapshot("005930")
        assert snapshot["lifecycle_state"] == "ACKED_WAIT_FIRST_TICK"
        assert snapshot["first_tick_verified"] is False
    finally:
        db.close()
