from datetime import datetime, timedelta, timezone

from trading.broker.command_queue import CommandStatus, dedupe_key_for_command
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand


def _send_order(idempotency_key=""):
    return GatewayCommand(
        type="send_order",
        command_id="cmd-order-1",
        idempotency_key=idempotency_key,
        payload={
            "account": "1234567890",
            "code": "005930",
            "side": "buy",
            "quantity": 10,
            "price": 70000,
            "tag": "T1",
            "candidate_id": 1,
        },
    )


def test_duplicate_idempotency_key_is_rejected():
    state = GatewayStateStore()
    first = GatewayCommand(type="login", command_id="cmd-1", idempotency_key="login-once")
    second = GatewayCommand(type="login", command_id="cmd-2", idempotency_key="login-once")

    assert state.enqueue_command(first).accepted is True
    result = state.enqueue_command(second)

    assert result.accepted is False
    assert result.reason == "DUPLICATE_COMMAND"
    assert result.duplicate_of == "cmd-1"
    assert state.command_snapshot()["duplicate_rejected_count"] == 1


def test_send_order_deterministic_dedupe_key_is_generated():
    command = _send_order()

    assert dedupe_key_for_command(command) == "order:1234567890:005930:buy:10:70000:T1:1"


def test_dispatch_moves_queued_to_dispatched():
    state = GatewayStateStore()
    command = GatewayCommand(type="tr_request", command_id="cmd-tr", payload={"rq_name": "rq", "tr_code": "opt", "screen_no": "9000"})
    state.enqueue_command(command)

    dispatched = state.dispatch_commands(limit=1)

    assert [item.command_id for item in dispatched] == ["cmd-tr"]
    records = state.list_commands(status=CommandStatus.DISPATCHED.value, include_finished=True)
    assert records[0]["status"] == CommandStatus.DISPATCHED.value
    assert records[0]["attempts"] == 1


def test_expired_command_is_not_dispatched():
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    state = GatewayStateStore()
    command = GatewayCommand(type="login", command_id="cmd-expire")
    state.enqueue_command(command, ttl_sec=1, now=now)

    expired = state.expire_old_commands(now + timedelta(seconds=2))

    assert expired == 1
    assert state.dispatch_commands(now=now + timedelta(seconds=3)) == []
    assert state.list_commands(status=CommandStatus.EXPIRED.value, include_finished=True)[0]["command_id"] == "cmd-expire"
