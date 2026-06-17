from datetime import datetime, timezone

from storage.db import TradingDatabase
from trading.broker.models import BrokerExecutionEvent
from trading.strategy.order_models import ManagedOrderStatus
from trading.strategy.order_reconcile import ManagedOrderReconciler


NOW = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc).isoformat()


def test_buy_execution_fills_managed_order_and_opens_live_sim_position(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    order = _managed_order(db, side="BUY", position_id="pos-buy-1")

    result = ManagedOrderReconciler(db).handle_execution(
        BrokerExecutionEvent(
            code="300001",
            order_no="B0001",
            side="BUY",
            quantity=1,
            price=1010,
            filled_quantity=1,
            remaining_quantity=0,
            account="12345678",
            timestamp=NOW,
        )
    )

    assert result.matched is True
    updated = db.get_managed_order(order["id"])
    assert updated["status"] == ManagedOrderStatus.FILLED.value
    assert updated["filled_quantity"] == 1
    position = db.get_live_sim_position("pos-buy-1")
    assert position["status"] == "OPEN"
    assert position["current_qty"] == 1


def test_partial_execution_keeps_remaining_quantity(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))
    order = _managed_order(db, side="BUY", quantity=3, remaining_quantity=3, position_id="pos-partial-1")

    result = ManagedOrderReconciler(db).handle_execution(
        BrokerExecutionEvent(
            code="300001",
            order_no="B0001",
            side="BUY",
            quantity=3,
            price=1010,
            filled_quantity=1,
            remaining_quantity=2,
            account="12345678",
            timestamp=NOW,
        )
    )

    assert result.status == ManagedOrderStatus.PARTIALLY_FILLED.value
    updated = db.get_managed_order(order["id"])
    assert updated["remaining_quantity"] == 2


def test_unmatched_execution_requires_reconcile(tmp_path):
    db = TradingDatabase(str(tmp_path / "test.db"))

    result = ManagedOrderReconciler(db).handle_execution(
        BrokerExecutionEvent(
            code="399999",
            order_no="UNKNOWN",
            side="BUY",
            quantity=1,
            price=1000,
            filled_quantity=1,
            remaining_quantity=0,
            account="12345678",
            timestamp=NOW,
        )
    )

    assert result.matched is False
    assert "UNMATCHED_EXECUTION" in result.reason_codes
    events = db.list_managed_order_events(limit=10)
    assert events[0]["event_type"] == "execution_unmatched"


def _managed_order(
    db: TradingDatabase,
    *,
    side: str,
    quantity: int = 1,
    remaining_quantity: int = 1,
    position_id: str = "pos-1",
) -> dict:
    intent = db.save_managed_order_intent(
        {
            "trade_date": "2026-06-18",
            "created_at": NOW,
            "source": "TEST_ONLY",
            "side": side,
            "code": "300001",
            "account": "12345678",
            "quantity": quantity,
            "price": 1010,
            "idempotency_key": f"intent-{side}-{position_id}",
            "status": "COMMAND_ACKED",
        }
    )
    return db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": "2026-06-18",
            "created_at": NOW,
            "source": "TEST_ONLY",
            "side": side,
            "code": "300001",
            "account": "12345678",
            "quantity": quantity,
            "price": 1010,
            "status": ManagedOrderStatus.ACKED_BY_GATEWAY.value,
            "command_id": "cmd-fill",
            "order_no": "B0001",
            "position_id": position_id,
            "filled_quantity": 0,
            "remaining_quantity": remaining_quantity,
            "idempotency_key": f"order-{side}-{position_id}",
        }
    )
