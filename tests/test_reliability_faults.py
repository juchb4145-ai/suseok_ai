from trading.reliability.faults import FaultInjectionController
from trading.reliability.models import QualificationStatus, ScenarioId


def test_order_fault_subset_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_OBSERVE_ONLY", "true")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND", "false")
    monkeypatch.setenv("TRADING_ORDER_INTENT_ENABLED", "false")
    results = FaultInjectionController(output_dir=tmp_path).run(
        [
            ScenarioId.F02_DUPLICATE_EXECUTION.value,
            ScenarioId.F03_FILL_BEFORE_ACK.value,
            ScenarioId.F04_OUT_OF_ORDER_PARTIAL_FILLS.value,
            ScenarioId.F05_CRASH_AFTER_RECEIPT.value,
            ScenarioId.F06_STALE_EVENT_CLAIM.value,
            ScenarioId.F08_EVENT_LOG_APPEND_FAILURE.value,
            ScenarioId.F10_MALFORMED_ORDER_EVENT.value,
            ScenarioId.F18_DEAD_LETTER_PRESENT.value,
        ]
    )
    assert all(result.status == QualificationStatus.PASS for result in results)
    assert {result.scenario_id for result in results} == {
        ScenarioId.F02_DUPLICATE_EXECUTION.value,
        ScenarioId.F03_FILL_BEFORE_ACK.value,
        ScenarioId.F04_OUT_OF_ORDER_PARTIAL_FILLS.value,
        ScenarioId.F05_CRASH_AFTER_RECEIPT.value,
        ScenarioId.F06_STALE_EVENT_CLAIM.value,
        ScenarioId.F08_EVENT_LOG_APPEND_FAILURE.value,
        ScenarioId.F10_MALFORMED_ORDER_EVENT.value,
        ScenarioId.F18_DEAD_LETTER_PRESENT.value,
    }


def test_fault_suite_has_no_real_order_command_metric(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_SEND_ORDER_ALLOWED", "false")
    results = FaultInjectionController(output_dir=tmp_path).run([ScenarioId.F17_BALANCE_MISMATCH.value])
    assert results[0].status == QualificationStatus.PASS
    assert results[0].metrics["auto_order_command_count"] == 0
