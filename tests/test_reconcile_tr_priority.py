from trading.broker.command_queue import CommandQueue, CommandPriority
from trading.broker.models import GatewayCommand


def test_reconcile_tr_dispatches_after_orders_but_before_backfill():
    queue = CommandQueue()
    order = GatewayCommand(type="send_order", command_id="cmd-order", payload={"account": "A", "code": "005930"})
    reconcile = GatewayCommand(
        type="tr_request",
        command_id="cmd-reconcile",
        payload={"purpose": "broker_reconcile", "tr_code": "opt10075"},
    )
    backfill = GatewayCommand(
        type="tr_request",
        command_id="cmd-backfill",
        payload={"purpose": "theme_backfill", "tr_code": "opt10001"},
    )

    queue.enqueue(backfill, priority=CommandPriority.NORMAL)
    queue.enqueue(reconcile, priority=CommandPriority.NORMAL)
    queue.enqueue(order, priority=CommandPriority.NORMAL)

    dispatched = queue.dispatch(limit=3)

    assert [command.command_id for command in dispatched] == ["cmd-order", "cmd-reconcile", "cmd-backfill"]

