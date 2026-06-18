from __future__ import annotations

from typing import Any

from trading.reliability.metrics import normalize_metrics
from trading.reliability.models import QualificationStatus, SLOEvaluationResult, SLOThresholds


class RuntimeSLOEvaluator:
    def __init__(self, thresholds: SLOThresholds | None = None) -> None:
        self.thresholds = thresholds or SLOThresholds.from_env()

    def evaluate(
        self,
        metrics: dict[str, Any],
        *,
        profile: str = "quick-ci",
        duration_sec: float = 0.0,
        executed_long_soak: bool = False,
    ) -> SLOEvaluationResult:
        flat = normalize_metrics(metrics)
        hard_failures = self.evaluate_hard_gates(flat)
        operational_failures, warnings = self.evaluate_operational(flat, profile=profile, duration_sec=duration_sec, executed_long_soak=executed_long_soak)
        sample_status = "OK"
        if profile in {"quick-ci", "replay", "fault-suite"} and not executed_long_soak:
            sample_status = "SAMPLE_INSUFFICIENT_FOR_LONG_SOAK"
            warnings.append("LONG_SOAK_NOT_RUN")
        if hard_failures:
            status = QualificationStatus.FAIL
        elif operational_failures:
            status = QualificationStatus.FAIL if executed_long_soak else QualificationStatus.HOLD
        elif sample_status != "OK":
            status = QualificationStatus.HOLD
        else:
            status = QualificationStatus.PASS
        return SLOEvaluationResult(
            status=status,
            hard_gate_failures=hard_failures,
            operational_failures=operational_failures,
            warnings=warnings,
            sample_status=sample_status,
            metrics=flat,
        )

    def evaluate_hard_gates(self, flat: dict[str, Any]) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        checks = {
            "order_command_count": 0,
            "send_order_command_count": 0,
            "cancel_order_command_count": 0,
            "modify_order_command_count": 0,
            "real_broker_access_count": 0,
            "critical_event_lost_count": 0,
            "duplicate_execution_applied_count": 0,
            "order_terminal_state_regression_count": 0,
            "negative_remaining_quantity_count": 0,
            "overfill_count": 0,
            "silent_unmatched_fill_count": 0,
            "unresolved_event_consumer_crash_count": 0,
            "runtime_db_corruption_count": 0,
        }
        for key, expected in checks.items():
            value = _number(flat.get(key), 0)
            if value != expected:
                failures.append(_failure(key, value, expected, "HARD_SAFETY_GATE"))
        if _number(flat.get("event_log_append_failure_count"), 0) > 0 and _number(flat.get("order_lifecycle_ready"), 0) > 0:
            failures.append(_failure("event_log_append_failure_but_lifecycle_ready", 1, 0, "HARD_SAFETY_GATE"))
        if _number(flat.get("event_log_dead_letter_count"), 0) > 0 and _number(flat.get("order_lifecycle_ready"), 0) > 0:
            failures.append(_failure("dead_letter_but_lifecycle_ready", 1, 0, "HARD_SAFETY_GATE"))
        if _number(flat.get("order_reconcile_required_count"), 0) > 0 and _number(flat.get("new_buy_allowed"), 0) > 0:
            failures.append(_failure("reconcile_required_but_new_buy_allowed", 1, 0, "HARD_SAFETY_GATE"))
        return failures

    def evaluate_operational(
        self,
        flat: dict[str, Any],
        *,
        profile: str,
        duration_sec: float,
        executed_long_soak: bool,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        failures: list[dict[str, Any]] = []
        warnings: list[str] = []
        thresholds = self.thresholds
        checks = {
            "runtime_cycle_duration_ms_p95": thresholds.runtime_cycle_p95_ms,
            "dirty_evaluator_duration_ms_p95": thresholds.dirty_evaluator_p95_ms,
            "event_consumer_duration_ms_p95": thresholds.order_event_p95_ms,
            "dashboard_read_duration_ms_p95": thresholds.dashboard_read_p95_ms,
            "dashboard_build_duration_ms_p95": thresholds.dashboard_build_p95_ms,
            "dashboard_read_model_age_sec": thresholds.read_model_max_age_sec,
            "event_log_oldest_pending_age_sec": thresholds.backlog_max_age_sec,
            "event_replay_duration_ms_p95": thresholds.replay_drain_max_sec * 1000.0,
            "price_tick_capacity_drop_count": float(thresholds.max_capacity_drops),
            "replay_tick_drop_count": float(thresholds.max_capacity_drops),
        }
        for key, limit in checks.items():
            value = _number(flat.get(key), 0)
            if value > float(limit):
                failures.append(_failure(key, value, limit, "OPERATIONAL_SLO"))
        rss_growth = _number(flat.get("memory_rss_growth_mb"), 0)
        if rss_growth > thresholds.max_rss_growth_mb:
            failures.append(_failure("memory_rss_growth_mb", rss_growth, thresholds.max_rss_growth_mb, "OPERATIONAL_SLO"))
        if profile in {"observe-soak", "full"} and duration_sec < thresholds.min_soak_duration_sec:
            warnings.append("SOAK_DURATION_BELOW_CONFIGURED_MINIMUM")
        if not executed_long_soak:
            warnings.append("OPERATIONAL_SLO_LONG_SOAK_NOT_EXECUTED")
        return failures, warnings


def _failure(metric: str, actual: Any, expected: Any, category: str) -> dict[str, Any]:
    return {"metric": metric, "actual": actual, "expected": expected, "category": category}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = ["RuntimeSLOEvaluator"]
