from __future__ import annotations

import asyncio

from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading_app.dependencies import CoreSettings
from trading_app.runtime_supervisor import RuntimeSupervisor


class _Consumer:
    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    def consume_live_event(self, event: GatewayEvent):
        self.events.append(event)
        return {"status": "APPLIED"}

    def consumer_health(self):
        return {"status": "READY", "order_lifecycle_ready": True}


def test_runtime_supervisor_routes_order_event_to_consumer_when_runtime_disabled(tmp_path):
    consumer = _Consumer()
    settings = CoreSettings(db_path=str(tmp_path / "runtime.db"), local_token="test-token", runtime_enabled=False)
    supervisor = RuntimeSupervisor(
        settings=settings,
        gateway_state=GatewayStateStore(),
        order_event_consumer=consumer,
    )
    async def scenario():
        event = GatewayEvent(
            type="execution_event",
            event_id="evt-runtime-fill",
            payload={"code": "005930", "order_no": "OID-1", "execution_id": "EXEC-1"},
        )
        await supervisor.handle_gateway_event(event)
        assert [item.event_id for item in consumer.events] == ["evt-runtime-fill"]
        assert supervisor.lightweight_status()["order_event_consumer"]["order_lifecycle_ready"] is True
        await supervisor.shutdown()

    asyncio.run(scenario())
