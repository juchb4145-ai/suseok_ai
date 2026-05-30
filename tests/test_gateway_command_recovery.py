from datetime import datetime, timedelta, timezone

from trading.broker.command_persistence import SQLiteCommandStore
from trading.broker.command_queue import CommandStatus
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand


def _store(db_path):
    return SQLiteCommandStore(db_path, dedupe_retention_sec=86400, history_retention_sec=86400)


def test_history_survives_gateway_state_recreation(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    first = GatewayStateStore(command_store=_store(db_path))
    first.enqueue_command(GatewayCommand(type="login", command_id="cmd-history"))
    first.dispatch_commands(limit=1)
    first.ack_command("cmd-history", status="ACKED", result_payload={"message": "ok"})

    second = GatewayStateStore(command_store=_store(db_path))
    history = second.list_commands(status="ACKED", include_finished=True)

    assert history[0]["command_id"] == "cmd-history"
    assert second.command_events("cmd-history")[-1]["event_type"] == "command_ack"


def test_queued_command_is_recovered_and_dispatchable_after_restart(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    first = GatewayStateStore(command_store=_store(db_path))
    first.enqueue_command(GatewayCommand(type="tr_request", command_id="cmd-recover"))

    second = GatewayStateStore(command_store=_store(db_path))
    dispatched = second.dispatch_commands(limit=1)

    assert second.command_snapshot()["recovered_queued_count"] == 1
    assert [command.command_id for command in dispatched] == ["cmd-recover"]


def test_dispatched_order_is_not_requeued_after_restart(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    first = GatewayStateStore(command_store=_store(db_path))
    command = GatewayCommand(
        type="send_order",
        command_id="cmd-order-dispatched",
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
    first.enqueue_command(command)
    first.dispatch_commands(limit=1)

    second = GatewayStateStore(command_store=_store(db_path))

    assert second.dispatch_commands(limit=1) == []
    assert second.get_command("cmd-order-dispatched").status == CommandStatus.DISPATCHED
    assert second.command_snapshot()["stale_dispatched_count"] == 1


def test_expired_queued_command_is_not_recovered(tmp_path):
    db_path = tmp_path / "trader.sqlite3"
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    first = GatewayStateStore(command_store=_store(db_path))
    first.enqueue_command(GatewayCommand(type="login", command_id="cmd-old"), ttl_sec=1, now=now)

    second = GatewayStateStore(command_store=_store(db_path))
    second.expire_old_commands(now + timedelta(seconds=2))

    third = GatewayStateStore(command_store=_store(db_path))
    assert third.dispatch_commands(limit=1, now=now + timedelta(seconds=3)) == []
    assert third.get_command("cmd-old").status == CommandStatus.EXPIRED
