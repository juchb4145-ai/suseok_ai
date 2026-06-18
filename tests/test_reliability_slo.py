from trading.reliability.models import QualificationStatus, SLOThresholds
from trading.reliability.slo import RuntimeSLOEvaluator


def test_hard_gate_violation_is_fail():
    evaluator = RuntimeSLOEvaluator(SLOThresholds(min_soak_duration_sec=1))
    result = evaluator.evaluate(
        {"counters": {"order_command_count": 1}, "gauges": {"order_lifecycle_ready": 1}, "series": {}},
        profile="quick-ci",
        duration_sec=1,
        executed_long_soak=False,
    )
    assert result.status == QualificationStatus.FAIL
    assert any(item["metric"] == "order_command_count" for item in result.hard_gate_failures)


def test_quick_ci_without_long_soak_is_hold_not_pass():
    evaluator = RuntimeSLOEvaluator(SLOThresholds(min_soak_duration_sec=3600))
    result = evaluator.evaluate(
        {
            "counters": {"order_command_count": 0},
            "gauges": {"order_lifecycle_ready": 1, "new_buy_allowed": 0},
            "series": {},
        },
        profile="quick-ci",
        duration_sec=1,
        executed_long_soak=False,
    )
    assert result.status == QualificationStatus.HOLD
    assert result.sample_status == "SAMPLE_INSUFFICIENT_FOR_LONG_SOAK"


def test_long_soak_clean_metrics_can_pass():
    evaluator = RuntimeSLOEvaluator(SLOThresholds(min_soak_duration_sec=1))
    result = evaluator.evaluate(
        {
            "counters": {"order_command_count": 0},
            "gauges": {"order_lifecycle_ready": 1, "new_buy_allowed": 0},
            "series": {"runtime_cycle_duration_ms": {"p95": 10, "count": 10}},
        },
        profile="observe-soak",
        duration_sec=2,
        executed_long_soak=True,
    )
    assert result.status == QualificationStatus.PASS
