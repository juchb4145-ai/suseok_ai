from trading.broker.ws_messages import GatewayWsMessage


def test_gateway_ws_message_round_trip():
    message = GatewayWsMessage(
        type="gateway_event",
        message_id="ws-1",
        trace_id="trace-ws-1",
        source="mock_websocket_gateway",
        payload={"event": {"type": "heartbeat", "payload": {"ok": True}}},
        metadata={"experiment_id": "exp-1"},
        command_id="cmd-1",
        event_id="evt-1",
        sequence=7,
    )

    restored = GatewayWsMessage.from_dict(message.to_dict())

    assert restored == message
