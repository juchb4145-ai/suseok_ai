from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from trading.reliability.metrics import RuntimeMetricsCollector
from trading.reliability.models import QualificationScenarioResult, QualificationStatus, SLOThresholds


@dataclass
class RuntimeSoakResult:
    status: QualificationStatus | str
    duration_sec: float
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_scenario_result(self) -> QualificationScenarioResult:
        return QualificationScenarioResult(
            scenario_id="OBSERVE_SOAK",
            status=self.status,
            duration_ms=self.duration_sec * 1000.0,
            metrics=self.metrics,
            warnings=self.warnings,
        )


class RuntimeSoakRunner:
    def __init__(
        self,
        *,
        collector: RuntimeMetricsCollector | None = None,
        thresholds: SLOThresholds | None = None,
    ) -> None:
        self.collector = collector or RuntimeMetricsCollector()
        self.thresholds = thresholds or SLOThresholds.from_env()

    def run(
        self,
        *,
        duration_sec: float,
        sample_interval_sec: float = 1.0,
        core_url: str = "",
    ) -> RuntimeSoakResult:
        started = time.perf_counter()
        samples = 0
        warnings: list[str] = []
        duration = max(0.0, float(duration_sec))
        interval = max(0.05, float(sample_interval_sec or 1.0))
        deadline = started + duration
        while time.perf_counter() < deadline or samples == 0:
            self.collector.collect_process_snapshot()
            if core_url:
                self._collect_core_url(core_url, warnings)
            else:
                self._collect_synthetic_runtime_sample(samples)
            samples += 1
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        actual_duration = time.perf_counter() - started
        metrics = self.collector.summary()
        metrics["soak_duration_sec"] = actual_duration
        metrics["soak_sample_count"] = samples
        if actual_duration < self.thresholds.min_soak_duration_sec:
            status = QualificationStatus.HOLD
            warnings.append("SAMPLE_INSUFFICIENT_FOR_LONG_SOAK")
        else:
            status = QualificationStatus.PASS
        return RuntimeSoakResult(status=status, duration_sec=actual_duration, metrics=metrics, warnings=warnings)

    def _collect_core_url(self, core_url: str, warnings: list[str]) -> None:
        base = core_url.rstrip("/")
        for endpoint in ("/api/runtime/status", "/api/dashboard-v2/snapshot"):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(base + endpoint, timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                duration_ms = (time.perf_counter() - started) * 1000.0
                if endpoint.endswith("/status"):
                    self.collector.observe("runtime_status_read_duration_ms", duration_ms)
                    self.collector.collect_runtime_snapshot(payload if isinstance(payload, dict) else {})
                else:
                    self.collector.observe("dashboard_read_duration_ms", duration_ms)
                    self.collector.collect_dashboard_snapshot(payload if isinstance(payload, dict) else {})
            except Exception as exc:
                warnings.append(f"CORE_POLL_FAILED:{endpoint}:{exc}")
                self.collector.increment("core_poll_error_count")

    def _collect_synthetic_runtime_sample(self, index: int) -> None:
        self.collector.observe("runtime_cycle_duration_ms", 10.0 + (index % 3))
        self.collector.observe("dirty_evaluator_duration_ms", 5.0 + (index % 2))
        self.collector.observe("event_consumer_duration_ms", 3.0)
        self.collector.observe("dashboard_read_duration_ms", 2.0)
        self.collector.observe("dashboard_build_duration_ms", 4.0)
        self.collector.gauge("event_log_pending_count", 0)
        self.collector.gauge("event_log_oldest_pending_age_sec", 0)
        self.collector.gauge("dirty_queue_depth", 0)
        self.collector.gauge("consumer_queue_depth", 0)


__all__ = ["RuntimeSoakResult", "RuntimeSoakRunner"]
