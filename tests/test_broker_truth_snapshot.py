from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading.broker.reconcile_tr_models import ReconcileSourceType
from trading.strategy.broker_reconcile import BrokerReconcileConfig, BrokerReconcileOrchestrator


class FakeGatewayState:
    def __init__(self):
        self.enqueued = []

    def enqueue_command(self, command, **kwargs):
        self.enqueued.append((command, kwargs))

        class Result:
            accepted = True
            reason = ""

        return Result()


def test_reconcile_source_result_is_staged_without_publishing_projection(tmp_path):
    db = TradingDatabase(str(tmp_path / "reconcile.db"))
    orchestrator = BrokerReconcileOrchestrator(db=db)
    run = orchestrator.request_manual_reconcile(account="1234567890", broker_env="SIMULATION", sources=["OPEN_ORDERS"])

    result = orchestrator.parser.parse_command_ack(
        {
            "purpose": "broker_reconcile",
            "reconcile_run_id": run["run_id"],
            "logical_source": "OPEN_ORDERS",
            "account": "1234567890",
            "complete": True,
            "captured_rows": [
                {
                    "주문번호": "OID-1",
                    "종목코드": "A005930",
                    "주문구분": "+매수",
                    "주문수량": "3",
                    "미체결수량": "3",
                    "주문상태": "접수",
                }
            ],
        }
    )
    orchestrator.apply_source_result(result, command_id="cmd-reconcile")

    assert db.list_broker_reconcile_open_orders(run["run_id"])[0]["order_no"] == "OID-1"
    assert db.get_broker_order_state(account="ACC_TOKEN_NOT_USED", order_no="OID-1") is None


def test_nested_broker_reconcile_command_ack_is_staged(tmp_path):
    db = TradingDatabase(str(tmp_path / "reconcile-nested.db"))
    orchestrator = BrokerReconcileOrchestrator(db=db)
    run = orchestrator.request_manual_reconcile(account="1234567890", broker_env="SIMULATION", sources=["OPEN_ORDERS"])
    event = GatewayEvent(
        type="command_ack",
        command_id="cmd-reconcile",
        payload={
            "payload": {
                "purpose": "broker_reconcile",
                "reconcile_run_id": run["run_id"],
                "logical_source": "OPEN_ORDERS",
                "account": "1234567890",
                "complete": True,
                "captured_rows": [],
            }
        },
    )

    result = orchestrator.handle_gateway_event(event)

    assert result["status"] == "PROCESSED"
    source_results = db.list_broker_reconcile_source_results(run["run_id"])
    assert source_results[0]["command_id"] == "cmd-reconcile"
    assert source_results[0]["status"] == "VALID_EMPTY"


def test_dispatch_enabled_reconcile_enqueues_read_only_tr_commands(tmp_path):
    db = TradingDatabase(str(tmp_path / "reconcile-dispatch.db"))
    gateway = FakeGatewayState()
    orchestrator = BrokerReconcileOrchestrator(
        db=db,
        gateway_state=gateway,
        config=BrokerReconcileConfig(dispatch_enabled=True, cash_required=True),
    )

    run = orchestrator.request_manual_reconcile(account="1234567890", broker_env="SIMULATION")

    assert run["status"] == "RUNNING"
    assert len(gateway.enqueued) == 3
    command_types = [record[0].type for record in gateway.enqueued]
    assert command_types == ["tr_request", "tr_request", "tr_request"]
    assert {record[0].payload["logical_source"] for record in gateway.enqueued} == {
        ReconcileSourceType.OPEN_ORDERS.value,
        ReconcileSourceType.ACCOUNT_POSITIONS.value,
        ReconcileSourceType.ACCOUNT_CASH.value,
    }
    assert all(record[0].payload["purpose"] == "broker_reconcile" for record in gateway.enqueued)


def test_reconcile_run_finalizes_after_required_sources_complete(tmp_path):
    db = TradingDatabase(str(tmp_path / "reconcile-finalize.db"))
    orchestrator = BrokerReconcileOrchestrator(
        db=db,
        config=BrokerReconcileConfig(dispatch_enabled=False),
    )
    run = orchestrator.request_manual_reconcile(
        account="1234567890",
        broker_env="SIMULATION",
        sources=[ReconcileSourceType.OPEN_ORDERS.value, ReconcileSourceType.ACCOUNT_POSITIONS.value],
    )

    open_orders_result = orchestrator.parser.parse_command_ack(
        {
            "purpose": "broker_reconcile",
            "reconcile_run_id": run["run_id"],
            "logical_source": ReconcileSourceType.OPEN_ORDERS.value,
            "account": "1234567890",
            "complete": True,
            "captured_rows": [],
        }
    )
    orchestrator.apply_source_result(open_orders_result)
    partial = db.get_broker_reconcile_run(run["run_id"])
    assert partial["status"] == "PARTIAL"
    assert partial["snapshot_complete"] is False

    positions_result = orchestrator.parser.parse_command_ack(
        {
            "purpose": "broker_reconcile",
            "reconcile_run_id": run["run_id"],
            "logical_source": ReconcileSourceType.ACCOUNT_POSITIONS.value,
            "account": "1234567890",
            "complete": True,
            "captured_rows": [],
        }
    )
    orchestrator.apply_source_result(positions_result)
    finalized = db.get_broker_reconcile_run(run["run_id"])

    assert finalized["status"] == "CLEAN"
    assert finalized["snapshot_complete"] is True
    assert finalized["broker_truth_ready"] is True


def test_broker_only_open_order_creates_stop_new_buy_discrepancy(tmp_path):
    db = TradingDatabase(str(tmp_path / "reconcile-mismatch.db"))
    orchestrator = BrokerReconcileOrchestrator(db=db)
    run = orchestrator.request_manual_reconcile(account="1234567890", broker_env="SIMULATION", sources=["OPEN_ORDERS"])
    result = orchestrator.parser.parse_command_ack(
        {
            "purpose": "broker_reconcile",
            "reconcile_run_id": run["run_id"],
            "logical_source": "OPEN_ORDERS",
            "account": "1234567890",
            "complete": True,
            "captured_rows": [
                {
                    "주문번호": "OID-BROKER",
                    "종목코드": "A005930",
                    "주문구분": "+매수",
                    "주문수량": "1",
                    "미체결수량": "1",
                    "주문상태": "접수",
                }
            ],
        }
    )
    orchestrator.apply_source_result(result)

    snapshot = orchestrator.finalize_run(run["run_id"])
    discrepancies = snapshot.to_dict()["discrepancies"]

    assert discrepancies[0]["category"] == "BROKER_ONLY_OPEN_ORDER"
    assert discrepancies[0]["severity"] == "STOP_NEW_BUY"
    assert db.get_broker_reconcile_run(run["run_id"])["broker_truth_ready"] is False
