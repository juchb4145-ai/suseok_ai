from trading.broker.models import GatewayEvent
from trading_app.gateway_event_consumer import GatewayEventCodec


def test_order_event_codec_ignores_broker_reconcile_command_ack():
    event = GatewayEvent(
        type="command_ack",
        event_id="evt-reconcile-ack",
        command_id="cmd-reconcile",
        payload={
            "purpose": "broker_reconcile",
            "command_type": "tr_request",
            "reconcile_run_id": "run-1",
            "logical_source": "OPEN_ORDERS",
        },
    )

    canonical = GatewayEventCodec().decode(event)

    assert canonical.ignored is True
    assert canonical.ignore_reason == "BROKER_RECONCILE_EVENT_ROUTED_TO_RECONCILE_CONSUMER"

