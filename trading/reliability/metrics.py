from __future__ import annotations

import os
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any


@dataclass
class MetricSeries:
    name: str
    max_samples: int = 2000
    values: deque[float] = field(default_factory=deque)
    error_count: int = 0

    def add(self, value: Any) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            self.error_count += 1
            return
        if len(self.values) >= self.max_samples:
            self.values.popleft()
        self.values.append(numeric)

    def summary(self) -> dict[str, Any]:
        values = sorted(self.values)
        if not values:
            return {"count": 0, "error_count": self.error_count, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
        return {
            "count": len(values),
            "error_count": self.error_count,
            "p50": _percentile(values, 50),
            "p95": _percentile(values, 95),
            "p99": _percentile(values, 99),
            "max": max(values),
            "avg": mean(values),
        }


class RuntimeMetricsCollector:
    def __init__(self, *, max_samples_per_metric: int = 2000) -> None:
        self.max_samples_per_metric = max(10, int(max_samples_per_metric or 2000))
        self.series: dict[str, MetricSeries] = {}
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {}

    def observe(self, name: str, value: Any) -> None:
        series = self.series.setdefault(str(name), MetricSeries(str(name), self.max_samples_per_metric))
        series.add(value)

    def increment(self, name: str, value: int = 1) -> None:
        key = str(name)
        self.counters[key] = int(self.counters.get(key, 0)) + int(value)

    def gauge(self, name: str, value: Any) -> None:
        try:
            self.gauges[str(name)] = float(value)
        except (TypeError, ValueError):
            self.increment(f"{name}_error_count")

    def collect_runtime_snapshot(self, runtime_status: dict[str, Any]) -> None:
        runtime = dict(runtime_status or {})
        self.gauge("runtime_cycle_count", runtime.get("cycle_count", 0))
        self.gauge("runtime_failed_cycle_count", runtime.get("failed_cycle_count", 0))
        self.gauge("runtime_skipped_cycle_count", runtime.get("skipped_cycle_count", 0))
        self.observe("runtime_cycle_duration_ms", runtime.get("last_cycle_duration_ms", 0))
        self.gauge("runtime_cycle_worker_pending", 1 if runtime.get("cycle_worker_pending") else 0)
        order = dict(runtime.get("order_event_consumer") or runtime.get("order_lifecycle") or {})
        self.gauge("event_log_pending_count", order.get("pending_event_count", 0))
        self.gauge("event_log_retry_wait_count", order.get("retry_wait_count", 0))
        self.gauge("event_log_dead_letter_count", order.get("dead_letter_count", 0))
        self.gauge("event_log_oldest_pending_age_sec", order.get("oldest_pending_age_sec", 0))
        self.gauge("order_lifecycle_ready", 1 if order.get("order_lifecycle_ready") else 0)
        self.gauge("order_reconcile_required_count", order.get("reconcile_required_count", 0))
        self.gauge("order_unmatched_event_count", order.get("unmatched_event_count", 0))
        self.observe("event_replay_duration_ms", order.get("replay_duration_ms", 0))

    def collect_dashboard_snapshot(self, snapshot: dict[str, Any]) -> None:
        payload = dict(snapshot or {})
        read_model = dict(payload.get("read_model") or {})
        health = dict(payload.get("system_health") or {})
        self.gauge("dashboard_read_model_generation", read_model.get("generation", 0))
        self.gauge("dashboard_read_model_age_sec", read_model.get("snapshot_age_sec", 0))
        self.observe("dashboard_build_duration_ms", read_model.get("build_duration_ms", 0))
        read_health = dict(health.get("read_model") or {})
        self.gauge("dashboard_stale_read_count", read_health.get("stale_read_count", 0))
        self.gauge("dashboard_fallback_count", read_health.get("fallback_count", 0))

    def collect_process_snapshot(self, *, db_path: str = "") -> None:
        self.gauge("thread_count", threading.active_count())
        rss_mb = _rss_mb()
        if rss_mb:
            self.gauge("memory_rss_mb", rss_mb)
        if db_path:
            path = Path(db_path)
            if path.exists():
                self.gauge("sqlite_db_size_mb", path.stat().st_size / (1024 * 1024))
            wal = Path(str(path) + "-wal")
            if wal.exists():
                self.gauge("sqlite_wal_size_mb", wal.stat().st_size / (1024 * 1024))

    def collect_event_log_snapshot(self, snapshot: dict[str, Any]) -> None:
        payload = dict(snapshot or {})
        self.gauge("event_log_pending_count", payload.get("pending_count", 0))
        self.gauge("event_log_processing_count", payload.get("processing_count", 0))
        self.gauge("event_log_retry_wait_count", payload.get("retry_wait_count", 0))
        self.gauge("event_log_failed_count", payload.get("failed_count", 0))
        self.gauge("event_log_dead_letter_count", payload.get("dead_letter_count", 0))

    def summary(self) -> dict[str, Any]:
        return {
            "series": {name: series.summary() for name, series in sorted(self.series.items())},
            "counters": dict(sorted(self.counters.items())),
            "gauges": dict(sorted(self.gauges.items())),
        }


def normalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    payload = dict(metrics or {})
    series = dict(payload.get("series") or {})
    counters = dict(payload.get("counters") or {})
    gauges = dict(payload.get("gauges") or {})
    flat: dict[str, Any] = {}
    for name, item in series.items():
        summary = dict(item or {})
        for key, value in summary.items():
            flat[f"{name}_{key}"] = value
    flat.update(counters)
    flat.update(gauges)
    return flat


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * (float(percentile) / 100.0)
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    fraction = rank - low
    return values[low] + (values[high] - values[low]) * fraction


def _rss_mb() -> float:
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if os.name == "posix":
            return rss / 1024.0
        return rss / (1024.0 * 1024.0)
    except Exception:
        return 0.0


def sqlite_busy_count_from_error(error: str) -> int:
    text = str(error or "").lower()
    return 1 if "database is locked" in text or "database is busy" in text else 0


def sqlite_integrity_check(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"status": "MISSING", "ok": False}
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        value = str(row[0] if row else "")
        return {"status": "OK" if value.lower() == "ok" else "FAILED", "ok": value.lower() == "ok", "detail": value}
    finally:
        conn.close()


__all__ = [
    "MetricSeries",
    "RuntimeMetricsCollector",
    "normalize_metrics",
    "sqlite_busy_count_from_error",
    "sqlite_integrity_check",
]
