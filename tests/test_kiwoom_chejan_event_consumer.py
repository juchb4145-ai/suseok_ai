import json
from pathlib import Path

from kiwoom.chejan import KiwoomChejanParser
from storage.db import TradingDatabase
from storage.event_log import EventLogRepository
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.order_models import ManagedOrderStatus
from trading_app.gateway_event_consumer import GatewayEventConsumerConfig, GatewayEventDispatcher, OrderLifecycleEventConsumer


FIXTURE_DIR = Path("tests/fixtures/kiwoom_chejan")


def _dispatcher(db_path):
    event_log = EventLogRepository(db_path)
    gateway_state = GatewayStateStore(event_log_store=event_log)
    config = GatewayEventConsumerConfig()
    consumer = OrderLifecycleEventConsumer(db_path=db_path, gateway_state=gateway_state, config=config)
    dispatcher = GatewayEventDispatcher(event_log=event_log, order_consumer=consumer, config=config)
    dispatcher.start()
    return dispatcher, gateway_state, event_log


def _managed_order(db: TradingDatabase):
    intent = db.save_managed_order_intent(
        {
            "trade_date": "2026-06-18",
            "source": "TEST",
            "side": "BUY",
            "code": "005930",
            "account": "ACC_TOKEN_SYNTHETIC",
            "quantity": 3,
            "price": 70000,
            "idempotency_key": "idem-chejan",
            "status": "COMMAND_QUEUED",
        }
    )
    return db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": "2026-06-18",
            "source": "TEST",
            "side": "BUY",
            "code": "005930",
            "account": "ACC_TOKEN_SYNTHETIC",
            "quantity": 3,
            "price": 70000,
            "status": ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
            "command_id": "cmd-chejan",
            "order_no": "OID-1003",
            "remaining_quantity": 3,
            "idempotency_key": "idem-chejan",
        }
    )


def _chejan_event(name: str, event_id: str) -> GatewayEvent:
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    result = KiwoomChejanParser().parse(
        gubun=payload["gubun"],
        item_count=payload["item_count"],
        fid_list=payload.get("fid_list", ""),
        raw_fids=payload["raw_fids"],
    )
    event_payload = result.to_event_payload()
    event_payload["command_id"] = "cmd-chejan"
    event_payload["idempotency_key"] = "idem-chejan"
    return GatewayEvent(type=result.gateway_event_type, payload=event_payload, event_id=event_id)


def test_kiwoom_fill_chejan_is_applied_once_with_receipt(tmp_path):
    db_path = tmp_path / "chejan_orders.db"
    db = TradingDatabase(str(db_path))
    try:
        _managed_order(db)
        dispatcher, gateway_state, event_log = _dispatcher(db_path)
        first = _chejan_event("partial_fill", "evt-chejan-fill-1")
        duplicate = _chejan_event("partial_fill", "evt-chejan-fill-transport-dup")
        gateway_state.record_event(first)
        assert dispatcher.consume_live_event(first).status == "APPLIED"
        gateway_state.record_event(duplicate)
        assert dispatcher.consume_live_event(duplicate).status == "DUPLICATE_ALREADY_APPLIED"
        order = db.find_managed_order_by_order_no("OID-1003")
        assert order["filled_quantity"] == 1
        assert order["remaining_quantity"] == 2
        assert event_log.get_by_event_id("evt-chejan-fill-transport-dup").processing_status == "PROCESSED"
    finally:
        db.close()


def test_invalid_critical_chejan_sets_stop_new_buy(tmp_path):
    db_path = tmp_path / "chejan_invalid.db"
    db = TradingDatabase(str(db_path))
    try:
        dispatcher, gateway_state, _ = _dispatcher(db_path)
        result = KiwoomChejanParser().parse(gubun="0", item_count=1, raw_fids={"9001": "A005930"})
        event = GatewayEvent(type=result.gateway_event_type, payload=result.to_event_payload(), event_id="evt-invalid-chejan")
        gateway_state.record_event(event)
        processed = dispatcher.consume_live_event(event)
        assert processed.reconcile_required is True
        assert db.latest_order_kill_switch_state()["state"] == "STOP_NEW_BUY"
    finally:
        db.close()


def test_balance_chejan_updates_position_projection_without_full_snapshot(tmp_path):
    db_path = tmp_path / "chejan_balance.db"
    db = TradingDatabase(str(db_path))
    try:
        dispatcher, gateway_state, _ = _dispatcher(db_path)
        event = _chejan_event("balance_zero", "evt-balance-zero")
        gateway_state.record_event(event)
        assert dispatcher.consume_live_event(event).status == "APPLIED"
        state = db.get_broker_position_state(account="ACC_TOKEN_SYNTHETIC", code="005930")
        assert state["quantity"] == 0
        assert state["details"]["payload"]["full_account_snapshot"] is False
        assert state["details"]["payload"]["snapshot_scope"] == "SINGLE_CODE_DELTA"
    finally:
        db.close()
