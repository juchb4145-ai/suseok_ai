from __future__ import annotations

from storage.db import TradingDatabase
from storage.event_log import EventLogRepository
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.order_models import ManagedOrderStatus
from trading_app.gateway_event_consumer import (
    GatewayEventConsumerConfig,
    GatewayEventDispatcher,
    OrderLifecycleEventConsumer,
)


def _dispatcher(db_path):
    event_log = EventLogRepository(db_path)
    gateway_state = GatewayStateStore(event_log_store=event_log)
    consumer = OrderLifecycleEventConsumer(
        db_path=db_path,
        gateway_state=gateway_state,
        config=GatewayEventConsumerConfig(),
    )
    dispatcher = GatewayEventDispatcher(event_log=event_log, order_consumer=consumer, config=GatewayEventConsumerConfig())
    dispatcher.start()
    return dispatcher, gateway_state, event_log


def _managed_order(db: TradingDatabase) -> dict:
    intent = db.save_managed_order_intent(
        {
            "trade_date": "2026-06-18",
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "005930",
            "account": "ACC-1",
            "quantity": 3,
            "price": 70000,
            "idempotency_key": "idem-order-1",
            "status": "COMMAND_QUEUED",
        }
    )
    return db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": "2026-06-18",
            "source": "TEST_ONLY",
            "side": "BUY",
            "code": "005930",
            "account": "ACC-1",
            "quantity": 3,
            "price": 70000,
            "status": ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
            "command_id": "cmd-buy-1",
            "remaining_quantity": 3,
            "idempotency_key": "idem-order-1",
            "sent_at": "2026-06-18T00:00:00+00:00",
        }
    )


def test_live_order_ack_is_processed_after_event_log_append(tmp_path):
    db_path = tmp_path / "orders.db"
    db = TradingDatabase(str(db_path))
    try:
        order = _managed_order(db)
        dispatcher, gateway_state, event_log = _dispatcher(db_path)

        event = GatewayEvent(
            type="command_ack",
            event_id="evt-order-ack",
            command_id="cmd-buy-1",
            payload={
                "command_id": "cmd-buy-1",
                "command_type": "send_order",
                "status": "ACKED",
                "result_code": 0,
                "order_no": "OID-1",
                "order_result": {"order_no": "OID-1", "code": 0, "message": "accepted"},
            },
        )
        assert gateway_state.record_event(event) is True
        assert event_log.get_by_event_id("evt-order-ack").processing_status == "PENDING"

        result = dispatcher.consume_live_event(event)
        assert result.status == "APPLIED"
        assert event_log.get_by_event_id("evt-order-ack").processing_status == "PROCESSED"

        updated = db.get_managed_order(order["id"])
        assert updated["status"] == ManagedOrderStatus.ACKED_BY_GATEWAY.value
        assert updated["order_no"] == "OID-1"
    finally:
        db.close()


def test_duplicate_execution_id_does_not_increase_filled_quantity(tmp_path):
    db_path = tmp_path / "orders.db"
    db = TradingDatabase(str(db_path))
    try:
        _managed_order(db)
        dispatcher, gateway_state, event_log = _dispatcher(db_path)
        ack = GatewayEvent(
            type="command_ack",
            event_id="evt-ack-before-fill",
            command_id="cmd-buy-1",
            payload={
                "command_id": "cmd-buy-1",
                "command_type": "send_order",
                "status": "ACKED",
                "result_code": 0,
                "order_no": "OID-1",
                "order_result": {"order_no": "OID-1", "code": 0},
            },
        )
        gateway_state.record_event(ack)
        dispatcher.consume_live_event(ack)

        fill_payload = {
            "account": "ACC-1",
            "code": "005930",
            "order_no": "OID-1",
            "side": "BUY",
            "quantity": 3,
            "price": 70100,
            "filled_quantity": 1,
            "remaining_quantity": 2,
            "execution_id": "EXEC-1",
            "command_id": "cmd-buy-1",
            "idempotency_key": "idem-order-1",
        }
        first = GatewayEvent(type="execution_event", event_id="evt-fill-1", payload=fill_payload)
        duplicate_transport = GatewayEvent(type="execution_event", event_id="evt-fill-dup", payload=fill_payload)
        gateway_state.record_event(first)
        assert dispatcher.consume_live_event(first).status == "APPLIED"
        gateway_state.record_event(duplicate_transport)
        assert dispatcher.consume_live_event(duplicate_transport).status == "DUPLICATE_ALREADY_APPLIED"

        updated = db.find_managed_order_by_order_no("OID-1")
        assert updated["filled_quantity"] == 1
        assert updated["remaining_quantity"] == 2
        assert event_log.get_by_event_id("evt-fill-dup").processing_status == "PROCESSED"
    finally:
        db.close()


def test_unmatched_fill_sets_stop_new_buy(tmp_path):
    db_path = tmp_path / "orders.db"
    db = TradingDatabase(str(db_path))
    try:
        dispatcher, gateway_state, _ = _dispatcher(db_path)
        event = GatewayEvent(
            type="execution_event",
            event_id="evt-unmatched-fill",
            payload={
                "account": "ACC-1",
                "code": "005930",
                "order_no": "UNKNOWN",
                "side": "BUY",
                "quantity": 1,
                "price": 70000,
                "filled_quantity": 1,
                "remaining_quantity": 0,
                "execution_id": "EXEC-404",
            },
        )
        gateway_state.record_event(event)
        result = dispatcher.consume_live_event(event)
        assert result.reconcile_required is True
        kill = db.latest_order_kill_switch_state()
        assert kill.get("state") == "STOP_NEW_BUY"
    finally:
        db.close()
