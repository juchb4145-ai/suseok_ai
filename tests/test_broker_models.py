from trading.broker.gateway_client import GatewayEventQueue
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import (
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPriceTick,
    GatewayCommand,
    GatewayEvent,
)


def test_broker_order_result_round_trip():
    request = BrokerOrderRequest(
        account="1234567890",
        code="005930",
        quantity=3,
        price=73000,
        side="buy",
        tag="BUY1_005930",
        order_type=1,
        command_id="cmd-1",
        idempotency_key="idem-1",
    )
    result = BrokerOrderResult(ok=True, code=0, message="ok", request=request)

    restored = BrokerOrderResult.from_dict(result.to_dict())

    assert restored.ok is True
    assert restored.request.code == "005930"
    assert restored.request.idempotency_key == "idem-1"


def test_gateway_event_queue_coalesces_price_ticks():
    queue = GatewayEventQueue()
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="005930", price=70000).to_dict()))
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="005930", price=70100).to_dict()))
    queue.put(GatewayEvent(type="condition_event", payload={"code": "005930"}))

    events = queue.drain()

    assert [event.type for event in events] == ["price_tick", "condition_event"]
    assert events[0].payload["price"] == 70100


def test_gateway_state_dedupes_events_and_commands():
    state = GatewayStateStore()
    event = GatewayEvent(type="heartbeat", event_id="evt-1", payload={"kiwoom_logged_in": True})
    command = GatewayCommand(type="login", command_id="cmd-1", idempotency_key="login-once")

    assert state.record_event(event) is True
    assert state.record_event(event) is False
    assert state.enqueue_command(command).accepted is True
    assert state.enqueue_command(command).accepted is False

    snapshot = state.snapshot()
    assert snapshot.kiwoom_logged_in is True
    assert snapshot.deduped_event_count == 1
    assert [item.command_id for item in state.pop_commands()] == ["cmd-1"]
