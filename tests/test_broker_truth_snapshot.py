from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading.strategy.broker_reconcile import BrokerReconcileOrchestrator


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
