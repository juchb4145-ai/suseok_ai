from datetime import datetime, timedelta, timezone

from trading.broker.command_persistence import SQLiteCommandStore
from trading.broker.command_queue import CommandRecord, CommandStatus
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand


def _state(tmp_path, *, dedupe_retention_sec=86400):
    store = SQLiteCommandStore(
        tmp_path / "trader.sqlite3",
        dedupe_retention_sec=dedupe_retention_sec,
        history_retention_sec=86400,
    )
    return GatewayStateStore(command_store=store)


def test_enqueue_creates_gateway_command_row(tmp_path):
    state = _state(tmp_path)
    command = GatewayCommand(type="login", command_id="cmd-db-enqueue")

    result = state.enqueue_command(command)

    assert result.accepted is True
    record = state.get_command("cmd-db-enqueue")
    assert record is not None
    assert record.status == CommandStatus.QUEUED
    assert state.command_snapshot()["queued_count"] == 1


def test_naive_command_record_time_is_interpreted_as_local_time():
    naive_local = datetime(2026, 6, 19, 9, 37, 0)

    record = CommandRecord.create(GatewayCommand(type="register_realtime", command_id="cmd-naive-local"), now=naive_local)

    expected = naive_local.astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")
    assert record.created_at == expected
    assert record.expires_at == (naive_local + timedelta(seconds=60)).astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")


def test_dispatch_ack_fail_cancel_and_expire_are_persisted(tmp_path):
    state = _state(tmp_path)

    state.enqueue_command(GatewayCommand(type="login", command_id="cmd-dispatch"))
    state.dispatch_commands(limit=1)
    assert state.get_command("cmd-dispatch").status == CommandStatus.DISPATCHED

    state.ack_command("cmd-dispatch", status="ACKED", result_payload={"message": "ok"})
    acked = state.get_command("cmd-dispatch")
    assert acked.status == CommandStatus.ACKED
    assert acked.result_payload["message"] == "ok"

    state.enqueue_command(GatewayCommand(type="tr_request", command_id="cmd-failed"))
    state.dispatch_commands(limit=1)
    state.fail_command("cmd-failed", "boom", retryable=False)
    failed = state.get_command("cmd-failed")
    assert failed.status == CommandStatus.FAILED
    assert failed.last_error == "boom"

    state.enqueue_command(GatewayCommand(type="load_conditions", command_id="cmd-cancel"))
    assert state.cancel_command("cmd-cancel") is True
    assert state.get_command("cmd-cancel").status == CommandStatus.CANCELLED

    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    state.enqueue_command(
        GatewayCommand(
            type="tr_request",
            command_id="cmd-expire",
            payload={"rq_name": "expire", "tr_code": "opt", "screen_no": "9001"},
        ),
        ttl_sec=1,
        now=now,
    )
    assert state.expire_old_commands(now + timedelta(seconds=2)) >= 1
    assert state.get_command("cmd-expire").status == CommandStatus.EXPIRED


def test_command_events_are_persisted(tmp_path):
    state = _state(tmp_path)
    state.enqueue_command(GatewayCommand(type="login", command_id="cmd-events"))
    state.dispatch_commands(limit=1)
    state.ack_command("cmd-events", status="ACKED", result_payload={"message": "ok"})

    events = state.command_events("cmd-events")

    assert [event["event_type"] for event in events] == ["enqueue", "dispatch", "command_ack"]


def test_duplicate_rejection_is_persisted(tmp_path):
    state = _state(tmp_path)
    first = GatewayCommand(type="login", command_id="cmd-1", idempotency_key="once")
    second = GatewayCommand(type="login", command_id="cmd-2", idempotency_key="once")

    assert state.enqueue_command(first).accepted is True
    result = state.enqueue_command(second)

    assert result.accepted is False
    assert result.duplicate_of == "cmd-1"
    assert state.command_snapshot()["duplicate_rejected_count"] == 1
    assert any(event["event_type"] == "duplicate_rejected" for event in state.command_events("cmd-2"))


def test_prune_finished_keeps_order_dedupe_until_retention(tmp_path):
    state = _state(tmp_path, dedupe_retention_sec=86400)
    command = GatewayCommand(
        type="send_order",
        command_id="cmd-order-retain",
        payload={
            "account": "1234567890",
            "code": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "tag": "T1",
            "candidate_id": 1,
        },
    )
    state.enqueue_command(command)
    state.dispatch_commands(limit=1)
    state.ack_command("cmd-order-retain", status="ACKED", result_payload={"message": "ok"})

    removed = state.prune_commands(older_than_sec=0)

    assert removed == 1
    duplicate = state.enqueue_command(GatewayCommand(type="send_order", command_id="cmd-order-dup", payload=command.payload))
    assert duplicate.accepted is False
    assert duplicate.duplicate_of == "cmd-order-retain"


def test_dedupe_key_can_be_pruned_after_retention_expires(tmp_path):
    state = _state(tmp_path, dedupe_retention_sec=1)
    command = GatewayCommand(
        type="send_order",
        command_id="cmd-order-short-retain",
        payload={
            "account": "1234567890",
            "code": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "tag": "T1",
            "candidate_id": 1,
        },
    )
    state.enqueue_command(command)
    state.dispatch_commands(limit=1)
    state.ack_command("cmd-order-short-retain", status="ACKED")

    pruned = state.command_store.prune_dedupe_keys(datetime.now(timezone.utc) + timedelta(seconds=2))

    assert pruned == 1
    assert state.has_duplicate(command.idempotency_key or state.get_command("cmd-order-short-retain").dedupe_key) is False
