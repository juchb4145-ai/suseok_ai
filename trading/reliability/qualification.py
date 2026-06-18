from __future__ import annotations

import csv
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading.reliability.faults import FaultInjectionController
from trading.reliability.guards import ReliabilitySafetyGuard
from trading.reliability.metrics import RuntimeMetricsCollector, sqlite_integrity_check
from trading.reliability.models import (
    QualificationRecommendation,
    QualificationScenarioResult,
    QualificationStatus,
    ReliabilityQualificationConfig,
    ReliabilityReport,
    ScenarioId,
    SLOThresholds,
)
from trading.reliability.report import ReliabilityReportWriter
from trading.reliability.replay import DeterministicReplayVerifier
from trading.reliability.slo import RuntimeSLOEvaluator
from trading.reliability.soak import RuntimeSoakRunner
from trading.reliability.workload import SyntheticGatewayEventGenerator, SyntheticWorkloadConfig, event_stream_digest
from trading.broker.models import GatewayEvent


class ReliabilityQualificationRunner:
    def __init__(
        self,
        config: ReliabilityQualificationConfig,
        *,
        thresholds: SLOThresholds | None = None,
    ) -> None:
        self.config = config
        self.thresholds = thresholds or SLOThresholds.from_env()

    def run(self) -> ReliabilityReport:
        started_at = _now()
        started_perf = time.perf_counter()
        run_id = self.config.resolved_run_id()
        run_dir = self.config.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.config.db_path or str(run_dir / "qualification.sqlite3")
        config = replace(self.config, run_id=run_id, db_path=str(db_path))
        guard = ReliabilitySafetyGuard().evaluate(config)
        scenarios: list[QualificationScenarioResult] = []
        failures: list[dict[str, Any]] = []
        warnings: list[str] = list(guard.warnings)
        not_run: list[str] = []
        deterministic: dict[str, Any] = {}
        transport = {
            "status": "NOT_RUN",
            "tool": "tools/websocket_real_pilot_soak.py",
            "note": "transport soak is reported as a subsection when executed separately; transport PASS is not runtime PASS",
        }
        metrics: dict[str, Any] = _base_metrics(guard.evidence)
        if not guard.allowed:
            failures.extend({"category": "SAFETY_GUARD", "reason": reason} for reason in guard.failures)
            report = self._build_report(
                config=config,
                started_at=started_at,
                started_perf=started_perf,
                status=QualificationStatus.ERROR,
                recommendation=QualificationRecommendation.NOT_READY,
                scenarios=scenarios,
                metrics=metrics,
                slo_result={"status": "ERROR", "guard": guard.to_dict()},
                failures=failures,
                warnings=warnings,
                not_run=not_run,
                deterministic=deterministic,
                transport=transport,
                run_dir=run_dir,
            )
            ReliabilityReportWriter(output_dir=config.output_dir).write(report)
            return report

        profile = config.profile.value
        executed_long_soak = False
        if profile in {"quick-ci", "replay", "full"}:
            deterministic = self._run_deterministic_replay(config, run_dir, scenarios, failures)
        else:
            not_run.append("DETERMINISTIC_REPLAY")

        if profile in {"quick-ci", "fault-suite", "full"}:
            scenario_ids = _quick_fault_subset() if profile == "quick-ci" else [item.value for item in ScenarioId]
            fault_results = FaultInjectionController(output_dir=run_dir / "faults", seed=config.seed).run(scenario_ids)
            scenarios.extend(fault_results)
            if profile == "quick-ci":
                not_run.extend(item.value for item in ScenarioId if item.value not in set(scenario_ids))
            failures.extend(_scenario_failures(fault_results))
        else:
            not_run.append("FAULT_SUITE")

        if profile in {"observe-soak", "full"}:
            collector = RuntimeMetricsCollector()
            soak = RuntimeSoakRunner(collector=collector, thresholds=self.thresholds).run(
                duration_sec=config.duration_sec,
                sample_interval_sec=config.sample_interval_sec,
                core_url=config.core_url,
            )
            scenarios.append(soak.to_scenario_result())
            _merge_metrics(metrics, soak.metrics)
            executed_long_soak = float(soak.duration_sec) >= self.thresholds.min_soak_duration_sec
        else:
            not_run.append("OBSERVE_SOAK_1H")

        _merge_fault_evidence(metrics, scenarios)
        integrity = sqlite_integrity_check(db_path) if Path(db_path).exists() else {"ok": True, "status": "NOT_CREATED"}
        if not integrity.get("ok", True):
            metrics["counters"]["runtime_db_corruption_count"] = 1
            failures.append({"category": "HARD_SAFETY_GATE", "reason": "runtime DB integrity check failed", "detail": integrity})
        metrics["gauges"]["qualification_db_integrity_ok"] = 1 if integrity.get("ok", True) else 0

        slo = RuntimeSLOEvaluator(self.thresholds).evaluate(
            metrics,
            profile=profile,
            duration_sec=float(config.duration_sec),
            executed_long_soak=executed_long_soak,
        )
        final_status = _final_status(slo.status, scenarios, failures)
        recommendation = _recommendation(final_status, profile, executed_long_soak)
        report = self._build_report(
            config=config,
            started_at=started_at,
            started_perf=started_perf,
            status=final_status,
            recommendation=recommendation,
            scenarios=scenarios,
            metrics=metrics,
            slo_result=slo.to_dict(),
            failures=failures + list(slo.operational_failures),
            warnings=warnings + list(slo.warnings),
            not_run=not_run,
            deterministic=deterministic,
            transport=transport,
            run_dir=run_dir,
        )
        ReliabilityReportWriter(output_dir=config.output_dir).write(report)
        return report

    def _run_deterministic_replay(
        self,
        config: ReliabilityQualificationConfig,
        run_dir: Path,
        scenarios: list[QualificationScenarioResult],
        failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        events = _load_bundle_events(config.bundle_path) if config.bundle_path else []
        if not events:
            events = SyntheticGatewayEventGenerator(
                SyntheticWorkloadConfig(
                    code_count=config.code_count,
                    ticks_per_sec=config.ticks_per_sec,
                    duration_sec=max(1.0, min(float(config.duration_sec), 10.0)),
                    event_burst_size=config.event_burst_size,
                    duplicate_rate=config.duplicate_rate,
                    out_of_order_rate=config.out_of_order_rate,
                    malformed_event_rate=config.malformed_event_rate,
                    reconnect_rate=config.reconnect_rate,
                    order_event_rate=config.order_event_rate,
                    seed=config.seed,
                    account="QUAL-ACC",
                )
            ).generate()
        started = _now()
        started_perf = time.perf_counter()
        result = DeterministicReplayVerifier(output_dir=run_dir, seed=config.seed).verify_events(events, repeat=max(2, int(config.repeat)))
        status = QualificationStatus.PASS if result.status == "PASS" else QualificationStatus.FAIL
        scenario_failures = []
        if status == QualificationStatus.FAIL:
            scenario_failures.append({"scenario_id": "DETERMINISTIC_REPLAY", "reason": "DIGEST_MISMATCH", "mismatch": result.mismatch})
            failures.extend(scenario_failures)
        scenarios.append(
            QualificationScenarioResult(
                scenario_id="DETERMINISTIC_REPLAY",
                status=status,
                started_at=started,
                finished_at=_now(),
                duration_ms=(time.perf_counter() - started_perf) * 1000.0,
                metrics={"event_count": len(events), "input_digest": event_stream_digest(events), "repeat": result.repeat},
                failures=scenario_failures,
            )
        )
        payload = result.to_dict()
        payload["event_count"] = len(events)
        payload["input_digest"] = event_stream_digest(events)
        return payload

    def _build_report(
        self,
        *,
        config: ReliabilityQualificationConfig,
        started_at: str,
        started_perf: float,
        status: QualificationStatus | str,
        recommendation: QualificationRecommendation | str,
        scenarios: list[QualificationScenarioResult],
        metrics: dict[str, Any],
        slo_result: dict[str, Any],
        failures: list[dict[str, Any]],
        warnings: list[str],
        not_run: list[str],
        deterministic: dict[str, Any],
        transport: dict[str, Any],
        run_dir: Path,
    ) -> ReliabilityReport:
        finished_at = _now()
        config_payload = config.to_dict()
        config_payload.update(
            {
                "test_db_evidence": str(config.db_path),
                "send_order_allowed": False,
                "observe_only": True,
                "enqueue_gateway_command": False,
                "order_intent_enabled": False,
            }
        )
        return ReliabilityReport(
            run_id=config.run_id,
            profile=config.profile,
            status=status,
            recommendation=recommendation,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=time.perf_counter() - started_perf,
            config=config_payload,
            scenarios=scenarios,
            metrics=metrics,
            slo_result=slo_result,
            hard_gate_failures=list(slo_result.get("hard_gate_failures") or []),
            failures=failures,
            warnings=warnings,
            not_run=sorted(set(item for item in not_run if item)),
            report_dir=str(run_dir),
            transport=transport,
            deterministic_replay=deterministic,
        )


def _base_metrics(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "series": {},
        "counters": {
            "order_command_count": int(evidence.get("order_command_count") or 0),
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
            "price_tick_capacity_drop_count": 0,
            "replay_tick_drop_count": 0,
        },
        "gauges": {
            "order_lifecycle_ready": 1,
            "new_buy_allowed": 0,
            "event_log_oldest_pending_age_sec": 0,
            "dashboard_read_model_age_sec": 0,
            "memory_rss_growth_mb": 0,
        },
        "safety_guard": dict(evidence),
    }


def _merge_metrics(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for section in ("series", "counters", "gauges"):
        target_section = target.setdefault(section, {})
        incoming_section = dict((incoming or {}).get(section) or {})
        for key, value in incoming_section.items():
            if section == "counters":
                target_section[key] = int(target_section.get(key, 0)) + int(value or 0)
            else:
                target_section[key] = value


def _merge_fault_evidence(metrics: dict[str, Any], scenarios: list[QualificationScenarioResult]) -> None:
    scenario_metrics = {}
    for result in scenarios:
        scenario_metrics[result.scenario_id] = dict(result.metrics or {})
    metrics["scenario_metrics"] = scenario_metrics


def _scenario_failures(results: list[QualificationScenarioResult]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for result in results:
        if str(result.status) == QualificationStatus.FAIL.value:
            failures.extend(result.failures or [{"scenario_id": result.scenario_id, "reason": "SCENARIO_FAILED"}])
    return failures


def _quick_fault_subset() -> list[str]:
    return [
        ScenarioId.F01_DUPLICATE_PRICE_TICKS.value,
        ScenarioId.F02_DUPLICATE_EXECUTION.value,
        ScenarioId.F03_FILL_BEFORE_ACK.value,
        ScenarioId.F04_OUT_OF_ORDER_PARTIAL_FILLS.value,
        ScenarioId.F05_CRASH_AFTER_RECEIPT.value,
        ScenarioId.F06_STALE_EVENT_CLAIM.value,
        ScenarioId.F08_EVENT_LOG_APPEND_FAILURE.value,
        ScenarioId.F10_MALFORMED_ORDER_EVENT.value,
        ScenarioId.F16_CORE_RESTART_WITH_BACKLOG.value,
        ScenarioId.F18_DEAD_LETTER_PRESENT.value,
    ]


def _load_bundle_events(bundle_path: str) -> list[GatewayEvent]:
    path = Path(bundle_path).expanduser()
    if not path.exists():
        return []
    jsonl_path = path / "gateway_events.jsonl"
    json_path = path / "gateway_events.json"
    if jsonl_path.exists():
        return [GatewayEvent.from_dict(json.loads(line)) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("events", [])
        return [GatewayEvent.from_dict(dict(row)) for row in rows if isinstance(row, dict)]
    ticks_path = path / "ticks.csv"
    if not ticks_path.exists():
        manifest_path = path / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            ticks_path = path / dict(manifest.get("data_files") or {}).get("ticks", "ticks.csv")
    if not ticks_path.exists():
        return []
    events: list[GatewayEvent] = []
    with ticks_path.open("r", encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            code = str(row.get("code") or "").strip()
            timestamp = str(row.get("timestamp") or "")
            if not code:
                continue
            events.append(
                GatewayEvent(
                    type="price_tick",
                    event_id=f"bundle-price-{index}-{code}-{timestamp}",
                    timestamp=timestamp,
                    payload={
                        "code": code,
                        "price": row.get("price", 0),
                        "change_rate": row.get("change_rate", 0),
                        "cum_volume": row.get("cum_volume", row.get("volume", 0)),
                        "trade_value": row.get("trade_value", 0),
                        "execution_strength": row.get("execution_strength", 0),
                        "best_bid": row.get("best_bid", 0),
                        "best_ask": row.get("best_ask", 0),
                        "spread_ticks": row.get("spread_ticks", 0),
                        "source": "strategy_replay_bundle",
                    },
                )
            )
    return events


def _final_status(slo_status: QualificationStatus | str, scenarios: list[QualificationScenarioResult], failures: list[dict[str, Any]]) -> QualificationStatus:
    if failures:
        return QualificationStatus.FAIL
    if any(str(result.status) == QualificationStatus.FAIL.value for result in scenarios):
        return QualificationStatus.FAIL
    if str(slo_status) == QualificationStatus.PASS.value:
        return QualificationStatus.PASS
    if str(slo_status) == QualificationStatus.ERROR.value:
        return QualificationStatus.ERROR
    return QualificationStatus.HOLD


def _recommendation(status: QualificationStatus | str, profile: str, executed_long_soak: bool) -> QualificationRecommendation:
    resolved = str(status.value if isinstance(status, QualificationStatus) else status)
    if resolved in {QualificationStatus.FAIL.value, QualificationStatus.ERROR.value}:
        return QualificationRecommendation.NOT_READY
    if resolved == QualificationStatus.HOLD.value:
        return QualificationRecommendation.OBSERVE_MORE
    if profile == "full" and executed_long_soak:
        return QualificationRecommendation.READY_FOR_LIVE_SIM_CANARY_REVIEW
    return QualificationRecommendation.READY_FOR_KIWOOM_PARSER_VALIDATION


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def qualification_exit_code(status: QualificationStatus | str) -> int:
    resolved = str(status.value if isinstance(status, QualificationStatus) else status)
    if resolved == QualificationStatus.PASS.value:
        return 0
    if resolved == QualificationStatus.HOLD.value:
        return 2
    if resolved == QualificationStatus.FAIL.value:
        return 1
    return 3


__all__ = ["ReliabilityQualificationRunner", "qualification_exit_code"]
