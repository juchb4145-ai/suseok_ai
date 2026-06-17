from datetime import datetime, timedelta, timezone

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent, utc_timestamp
from trading.strategy.order_models import ManagedOrderStatus
from trading.strategy.order_manager import OrderManagerConfig
from trading.strategy.unfilled_cancel import UnfilledCancelScheduler


NOW = datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc)


def test_unfilled_ack_order_after_timeout_queues_cancel_command(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    order = _managed_order(db, acked_at=(NOW - timedelta(seconds=60)).isoformat())

    summary = UnfilledCancelScheduler(db, gateway, _enabled_config()).run_if_due(NOW)

    assert summary["queued_cancel_count"] == 1
    updated = db.get_managed_order(order["id"])
    assert updated["status"] == ManagedOrderStatus.CANCEL_PENDING.value
    command = gateway.list_commands(limit=1)[0]["command"]
    assert command["type"] == "cancel_order"
    assert command["payload"]["original_order_no"] == "A0001"
    assert command["payload"]["quantity"] == 1


def test_unfilled_cancel_waits_until_timeout(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _managed_order(db, acked_at=(NOW - timedelta(seconds=10)).isoformat())

    summary = UnfilledCancelScheduler(db, gateway, _enabled_config()).run_if_due(NOW)

    assert summary["queued_cancel_count"] == 0
    assert gateway.command_snapshot()["queued_count"] == 0


def test_unfilled_cancel_requires_original_order_no(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    gateway = _sim_gateway()
    _managed_order(db, order_no="", acked_at=(NOW - timedelta(seconds=60)).isoformat())

    summary = UnfilledCancelScheduler(db, gateway, _enabled_config()).run_if_due(NOW)

    assert summary["queued_cancel_count"] == 0
    assert gateway.command_snapshot()["queued_count"] == 0


def _enabled_config() -> OrderManagerConfig:
    return OrderManagerConfig(
        enabled=True,
        mode="LIVE_SIM",
        allow_live_sim_orders=True,
        observe_only=False,
        cancel_unfilled_after_sec=45,
    )


def _managed_order(db: TradingDatabase, *, order_no: str = "A0001", acked_at: str) -> dict:
    intent = db.save_managed_order_intent(
        {
            "trade_date": "2026-06-18",
            "created_at": NOW.isoformat(),
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "200001",
            "account": "12345678",
            "quantity": 1,
            "price": 1000,
            "idempotency_key": "intent-cancel-1",
            "status": "COMMAND_ACKED",
        }
    )
    return db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": "2026-06-18",
            "created_at": (NOW - timedelta(seconds=70)).isoformat(),
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "200001",
            "account": "12345678",
            "quantity": 1,
            "price": 1000,
            "status": ManagedOrderStatus.ACKED_BY_GATEWAY.value,
            "command_id": "cmd-buy",
            "order_no": order_no,
            "remaining_quantity": 1,
            "acked_at": acked_at,
            "cancel_after_sec": 45,
            "idempotency_key": "order-cancel-1",
        }
    )


def _sim_gateway() -> GatewayStateStore:
    gateway = GatewayStateStore()
    gateway.record_event(
        GatewayEvent(
            type="heartbeat",
            timestamp=utc_timestamp(),
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": "12345678",
                "mode": "LIVE_SIM",
                "broker_env": "SIMULATION",
                "server_gubun": "1",
            },
        )
    )
    return gateway
