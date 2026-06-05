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

    assert [event.type for event in events] == ["condition_event", "price_tick"]
    assert events[1].payload["price"] == 70100


def test_gateway_event_queue_coalesces_price_ticks_on_put():
    queue = GatewayEventQueue()
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="005930", price=70000).to_dict()))
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="005930", price=70100).to_dict()))

    assert len(queue) == 1
    assert queue.drain()[0].payload["price"] == 70100


def test_gateway_event_queue_preserves_non_price_events_when_full():
    queue = GatewayEventQueue(max_size=2)
    queue.put(GatewayEvent(type="heartbeat", payload={"kiwoom_logged_in": True}))
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="005930", price=70000).to_dict()))
    queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code="000660", price=120000).to_dict()))

    events = queue.drain(limit=10)

    assert [event.type for event in events] == ["heartbeat", "price_tick"]
    assert events[1].payload["code"] == "000660"


def test_gateway_event_queue_drains_high_priority_events_from_backlog_first():
    queue = GatewayEventQueue()
    for index in range(5):
        queue.put(GatewayEvent(type="price_tick", payload=BrokerPriceTick(code=f"00000{index}", price=1000 + index).to_dict()))
    queue.put(GatewayEvent(type="command_ack", command_id="cmd-1", payload={"status": "ACKED"}))

    events = queue.drain(limit=2)

    assert [event.type for event in events] == ["command_ack", "price_tick"]
    assert events[0].command_id == "cmd-1"


def test_broker_price_tick_from_dict_keeps_backward_compatibility():
    tick = BrokerPriceTick.from_dict({"code": "005930", "price": "-70000", "volume": "1000"})

    assert tick.code == "005930"
    assert tick.price == 70000
    assert tick.volume == 1000
    assert tick.trade_value == 0.0
    assert tick.execution_strength == 0.0
    assert tick.spread_ticks == 0
    assert tick.metadata == {}


def test_broker_price_tick_from_dict_accepts_rich_fields_and_cum_volume_alias():
    tick = BrokerPriceTick.from_dict(
        {
            "code": "005930",
            "price": "70000",
            "cum_volume": "1200",
            "trade_value": "84000000",
            "execution_strength": "123.4",
            "spread_ticks": "1",
            "trade_time": "093015",
            "open_price": "69500",
            "day_high": "71000",
            "day_low": "69000",
            "metadata": {"reason_codes": ["SPREAD_APPROXIMATED"]},
        }
    )

    assert tick.volume == 1200
    assert tick.trade_value == 84_000_000
    assert tick.execution_strength == 123.4
    assert tick.spread_ticks == 1
    assert tick.trade_time == "093015"
    assert tick.open_price == 69500
    assert tick.day_high == 71000
    assert tick.day_low == 69000
    assert tick.metadata["reason_codes"] == ["SPREAD_APPROXIMATED"]


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
