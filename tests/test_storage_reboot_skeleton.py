from storage.outbox import OutboxEvent, OutboxStatus
from storage.postgres_writer import DisabledPostgresWriter
from storage.warehouse_store import DeferredWarehouseStore, WarehouseWritePlan


def test_disabled_postgres_writer_defers_events_without_mutating_status():
    writer = DisabledPostgresWriter()
    event = OutboxEvent(topic="orders_all", key="command-1", payload={"command_id": "command-1"})

    result = writer.publish_batch([event])

    assert writer.enabled is False
    assert result.accepted == 0
    assert result.deferred == 1
    assert result.failed == 0
    assert event.status == OutboxStatus.PENDING


def test_deferred_warehouse_store_converts_write_plan_to_outbox_event():
    store = DeferredWarehouseStore()
    event = store.to_outbox_event(
        WarehouseWritePlan(
            dataset="executions_all",
            idempotency_key="execution-1",
            rows=[{"execution_id": "execution-1"}],
        )
    )

    assert store.write_rows("executions_all", [{"execution_id": "execution-1"}]) == 0
    assert event.topic == "executions_all"
    assert event.key == "execution-1"
    assert event.payload["dataset"] == "executions_all"
