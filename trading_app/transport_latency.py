from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from storage.db import TradingDatabase
from trading.broker.models import new_message_id
from trading.broker.transport_metrics import TransportLatencySummary, percentile, utc_now_ms


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "gateway_transport_latency"


@dataclass(frozen=True)
class TransportLatencyConfig:
    p95_warn_ms: int = 1000
    p99_warn_ms: int = 3000
    command_p95_warn_ms: int = 1000
    event_p95_warn_ms: int = 500
    ack_p95_warn_ms: int = 1000
    websocket_recommend_p95_ms: int = 1000
    websocket_recommend_empty_poll_rate: float = 0.8


class WebSocketDecisionAdvisor:
    def __init__(self, config: Optional[TransportLatencyConfig] = None) -> None:
        self.config = config or TransportLatencyConfig()

    def evaluate(self, summary: dict[str, Any]) -> dict[str, Any]:
        comparison = summary.get("comparison")
        if isinstance(comparison, dict):
            return self.evaluate_comparison(comparison)
        metrics = {
            "event_latency_p95_ms": _float(summary.get("event_latency_p95_ms")),
            "command_latency_p95_ms": _float(summary.get("command_latency_p95_ms")),
            "ack_latency_p95_ms": _float(summary.get("ack_latency_p95_ms")),
            "long_poll_wait_p95_ms": _float(summary.get("long_poll_wait_p95_ms")),
            "gateway_execute_p95_ms": _float(summary.get("gateway_execute_p95_ms")),
            "rate_limit_wait_p95_ms": _float(summary.get("rate_limit_wait_p95_ms")),
            "empty_poll_rate": _float(summary.get("empty_poll_rate")),
            "transport_error_count": int(summary.get("transport_error_count") or 0),
        }
        reasons: list[str] = []
        blockers: list[str] = []
        recommendation = "KEEP_REST_LONG_POLL"
        should_switch = False
        severity = "low"

        if metrics["rate_limit_wait_p95_ms"] >= self.config.websocket_recommend_p95_ms:
            blockers.append("RATE_LIMIT_WAIT_DOMINATES")
            reasons.append("rate_limit_wait_ms p95가 높아 WebSocket보다 command pacing/rate limit 조정이 우선입니다.")
            recommendation = "INVESTIGATE_RATE_LIMIT"
            severity = "warn"
        elif metrics["gateway_execute_p95_ms"] >= self.config.websocket_recommend_p95_ms:
            blockers.append("GATEWAY_EXECUTION_DOMINATES")
            reasons.append("gateway_execute_ms p95가 높아 Kiwoom/COM 호출 병목 가능성이 큽니다.")
            recommendation = "INVESTIGATE_KIWOOM_EXECUTION"
            severity = "warn"
        elif metrics["command_latency_p95_ms"] >= self.config.websocket_recommend_p95_ms and (
            metrics["long_poll_wait_p95_ms"] >= self.config.websocket_recommend_p95_ms * 0.5
        ):
            reasons.append("command dispatch-to-gateway latency와 long_poll_wait_ms가 기준을 넘었습니다.")
            recommendation = "TRY_WEBSOCKET_EXPERIMENT"
            should_switch = False
            severity = "experiment"
        elif metrics["ack_latency_p95_ms"] >= self.config.ack_p95_warn_ms:
            reasons.append("ack round-trip p95가 기준을 넘었습니다. 네트워크/HTTP POST 경로 확인이 필요합니다.")
            recommendation = "TUNE_LONG_POLL_WAIT_SEC"
            severity = "warn"
        elif metrics["empty_poll_rate"] >= self.config.websocket_recommend_empty_poll_rate and metrics["command_latency_p95_ms"] < self.config.command_p95_warn_ms:
            reasons.append("empty poll 비율이 높지만 command latency는 안정적입니다. wait_sec 조정으로 HTTP 요청 낭비를 줄일 수 있습니다.")
            recommendation = "TUNE_LONG_POLL_WAIT_SEC"
            severity = "observe"
        else:
            reasons.append("현재 p95 latency가 기준 이하이거나 병목이 transport 전환으로 해결될 가능성이 낮습니다.")

        if metrics["transport_error_count"]:
            reasons.append("transport error가 있어 WebSocket 전환 전 REST 경로의 timeout/retry 원인을 먼저 확인해야 합니다.")

        return {
            "recommendation": recommendation,
            "should_switch": should_switch,
            "severity": severity,
            "reasons": reasons,
            "blockers": blockers,
            "thresholds": {
                "websocket_recommend_p95_ms": self.config.websocket_recommend_p95_ms,
                "command_p95_warn_ms": self.config.command_p95_warn_ms,
                "event_p95_warn_ms": self.config.event_p95_warn_ms,
                "ack_p95_warn_ms": self.config.ack_p95_warn_ms,
                "empty_poll_rate": self.config.websocket_recommend_empty_poll_rate,
            },
            "current_metrics": metrics,
            "next_experiment_plan": [
                "long-poll wait_sec/network interval을 먼저 조정해 p95 변화를 비교합니다.",
                "그래도 core_dispatch_wait/long_poll_wait 병목이 남으면 mock-only WebSocket 실험을 진행합니다.",
                "rate_limit_wait 또는 gateway_execute 병목이면 WebSocket 전환보다 Kiwoom 호출 pacing을 조정합니다.",
            ],
        }

    def evaluate_comparison(self, comparison: dict[str, Any]) -> dict[str, Any]:
        rest = dict(comparison.get("rest_summary") or {})
        websocket = dict(comparison.get("websocket_summary") or {})
        delta = dict(comparison.get("delta") or {})
        rest_command = _float(rest.get("command_latency_p95_ms"))
        ws_command = _float(websocket.get("command_latency_p95_ms"))
        rest_ack = _float(rest.get("ack_latency_p95_ms"))
        ws_ack = _float(websocket.get("ack_latency_p95_ms"))
        rest_long_poll = _float(rest.get("long_poll_wait_p95_ms"))
        rest_rate_limit = _float(rest.get("rate_limit_wait_p95_ms"))
        rest_execute = _float(rest.get("gateway_execute_p95_ms"))
        ws_errors = _float(websocket.get("transport_error_count"))
        rest_errors = _float(rest.get("transport_error_count"))
        command_improvement = rest_command - ws_command
        significant_command = rest_command > 0 and command_improvement >= max(100.0, rest_command * 0.2)
        recommendation = "KEEP_REST_LONG_POLL"
        reasons: list[str] = []
        blockers: list[str] = []

        if rest_rate_limit >= self.config.websocket_recommend_p95_ms:
            recommendation = "WEBSOCKET_NOT_HELPFUL_RATE_LIMIT_BOUND"
            blockers.append("RATE_LIMIT_WAIT_DOMINATES")
            reasons.append("REST baseline is rate-limit bound; WebSocket is unlikely to help.")
        elif rest_execute >= self.config.websocket_recommend_p95_ms:
            recommendation = "WEBSOCKET_NOT_HELPFUL_KIWOOM_EXECUTION_BOUND"
            blockers.append("GATEWAY_EXECUTION_DOMINATES")
            reasons.append("REST baseline is Gateway/Kiwoom execution bound; WebSocket is unlikely to help.")
        elif significant_command and rest_long_poll >= self.config.websocket_recommend_p95_ms * 0.5 and ws_errors <= rest_errors:
            recommendation = "WEBSOCKET_PROMISING_BUT_NEEDS_REAL_GATEWAY_TEST"
            reasons.append("Mock WebSocket reduced command p95 and REST was long-poll-wait bound.")
        elif significant_command:
            recommendation = "RUN_LONGER_WEBSOCKET_EXPERIMENT"
            reasons.append("Mock WebSocket improved command p95, but more samples are needed before a real pilot.")
        elif rest_long_poll >= self.config.websocket_recommend_p95_ms * 0.5:
            recommendation = "TUNE_REST_LONG_POLL"
            reasons.append("REST long-poll wait is visible, but mock WebSocket did not improve enough.")
        else:
            reasons.append("Mock comparison does not show a strong WebSocket advantage.")

        if ws_errors > rest_errors:
            blockers.append("WEBSOCKET_ERROR_RATE_HIGHER")
            reasons.append("Mock WebSocket has more transport errors than REST.")

        return {
            "recommendation": recommendation,
            "should_switch": False,
            "real_gateway_switch_ready": False,
            "reasons": reasons,
            "blockers": blockers,
            "current_metrics": {
                "rest_command_p95_ms": rest_command,
                "websocket_command_p95_ms": ws_command,
                "command_p95_delta_ms": delta.get("command_p95_delta_ms", command_improvement),
                "rest_ack_p95_ms": rest_ack,
                "websocket_ack_p95_ms": ws_ack,
                "ack_p95_delta_ms": delta.get("ack_p95_delta_ms", rest_ack - ws_ack),
                "rest_long_poll_wait_p95_ms": rest_long_poll,
            },
            "next_steps": [
                "Run a longer mock scenario if sample counts are low.",
                "Keep real Kiwoom Gateway on REST until reconnect/idempotency pilot passes.",
                "If WebSocket remains promising, prepare a limited real-gateway pilot with LIVE orders disabled.",
            ],
        }


class TransportLatencyAnalyzer:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: Optional[TransportLatencyConfig] = None,
        report_root: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.config = config or TransportLatencyConfig()
        self.advisor = WebSocketDecisionAdvisor(self.config)
        self.report_root = report_root or REPORT_ROOT

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        direction: Optional[str] = None,
        message_type: Optional[str] = None,
        transport_mode: Optional[str] = None,
        experiment_id: Optional[str] = None,
        scenario: Optional[str] = None,
        limit: int = 10000,
        offset: int = 0,
    ) -> dict[str, Any]:
        samples = self.db.list_gateway_transport_latency_samples(
            trade_date=trade_date,
            direction=direction,
            message_type=message_type,
            transport_mode=transport_mode,
            experiment_id=experiment_id,
            scenario=scenario,
            limit=limit,
            offset=offset,
        )
        summary = self.aggregate_summary(samples)
        recommendation = self.advisor.evaluate(summary)
        report = {
            "report_id": new_message_id("transport_report"),
            "status": "READY",
            "transport_mode": transport_mode or "rest_long_poll",
            "trade_date": trade_date or "",
            "generated_at": utc_now_ms(),
            "filters": {
                "trade_date": trade_date or "",
                "direction": direction or "",
                "message_type": message_type or "",
                "transport_mode": transport_mode or "",
                "experiment_id": experiment_id or "",
                "scenario": scenario or "",
                "limit": int(limit or 10000),
                "offset": int(offset or 0),
            },
            "summary": summary,
            "websocket_recommendation": recommendation,
            "samples": samples[: min(len(samples), 500)],
        }
        return report

    def build_transport_comparison_report(
        self,
        *,
        trade_date: Optional[str] = None,
        experiment_id: Optional[str] = None,
        scenario: Optional[str] = None,
        baseline_transport: str = "rest_long_poll",
        candidate_transport: str = "websocket_mock",
        limit: int = 10000,
    ) -> dict[str, Any]:
        rest_samples = self.db.list_gateway_transport_latency_samples(
            trade_date=trade_date,
            experiment_id=experiment_id,
            scenario=scenario,
            transport_mode=baseline_transport,
            limit=limit,
        )
        ws_samples = self.db.list_gateway_transport_latency_samples(
            trade_date=trade_date,
            experiment_id=experiment_id,
            scenario=scenario,
            transport_mode=candidate_transport,
            limit=limit,
        )
        rest_summary = self.aggregate_summary(rest_samples)
        ws_summary = self.aggregate_summary(ws_samples)
        delta = {
            "event_p95_delta_ms": _float(rest_summary.get("event_latency_p95_ms")) - _float(ws_summary.get("event_latency_p95_ms")),
            "command_p95_delta_ms": _float(rest_summary.get("command_latency_p95_ms")) - _float(ws_summary.get("command_latency_p95_ms")),
            "ack_p95_delta_ms": _float(rest_summary.get("ack_latency_p95_ms")) - _float(ws_summary.get("ack_latency_p95_ms")),
            "empty_poll_rate_delta": _float(rest_summary.get("empty_poll_rate")) - _float(ws_summary.get("empty_poll_rate")),
            "error_count_delta": _float(rest_summary.get("transport_error_count")) - _float(ws_summary.get("transport_error_count")),
        }
        comparison = {"rest_summary": rest_summary, "websocket_summary": ws_summary, "delta": delta}
        recommendation = self.advisor.evaluate({"comparison": comparison})
        sample_counts = {baseline_transport: len(rest_samples), candidate_transport: len(ws_samples)}
        return {
            "report_id": new_message_id("transport_cmp"),
            "status": "READY",
            "generated_at": utc_now_ms(),
            "trade_date": trade_date or "",
            "transport_mode": f"{baseline_transport}_vs_{candidate_transport}",
            "experiment_id": experiment_id or "",
            "scenario": scenario or "",
            "filters": {
                "trade_date": trade_date or "",
                "experiment_id": experiment_id or "",
                "scenario": scenario or "",
                "baseline_transport": baseline_transport,
                "candidate_transport": candidate_transport,
            },
            "rest_summary": rest_summary,
            "websocket_summary": ws_summary,
            "delta": delta,
            "sample_counts": sample_counts,
            "websocket_recommendation": recommendation,
            "summary": {
                "comparison": comparison,
                "sample_counts": sample_counts,
                "recommendation": recommendation.get("recommendation"),
                **delta,
            },
        }

    def aggregate_summary(self, samples: list[dict[str, Any]]) -> dict[str, Any]:
        base = TransportLatencySummary.from_samples(samples).to_dict()
        by_direction = _group(samples, "direction", "total_wall_ms")
        by_message_type = _group(samples, "message_type", "total_wall_ms")
        command_samples = [sample for sample in samples if sample.get("direction") == "core_to_gateway"]
        event_samples = [sample for sample in samples if sample.get("direction") == "gateway_to_core"]
        ack_samples = [sample for sample in samples if sample.get("direction") == "gateway_ack_to_core"]
        empty_poll_count = sum(1 for sample in samples if sample.get("message_type") == "command_poll_empty")
        poll_count = sum(1 for sample in samples if str(sample.get("message_type") or "").startswith("command_poll") or sample.get("direction") == "core_to_gateway")
        errors = [sample for sample in samples if sample.get("error") or not sample.get("success", True)]
        rate_limited = [
            sample
            for sample in samples
            if sample.get("message_type") == "rate_limited" or (_float(sample.get("rate_limit_wait_ms")) > 0)
        ]
        summary = {
            **base,
            "transport_mode": "rest_long_poll",
            "event_latency_p95_ms": _summary_value(event_samples, "total_wall_ms", 95),
            "command_latency_p95_ms": _summary_value(command_samples, "total_wall_ms", 95),
            "ack_latency_p95_ms": _summary_value(ack_samples, "ack_round_trip_ms", 95)
            or _summary_value(ack_samples, "total_wall_ms", 95),
            "long_poll_wait_p95_ms": _summary_value(samples, "long_poll_wait_ms", 95),
            "gateway_execute_p95_ms": _summary_value(samples, "gateway_execute_ms", 95),
            "rate_limit_wait_p95_ms": _summary_value(samples, "rate_limit_wait_ms", 95),
            "empty_poll_count": empty_poll_count,
            "command_poll_count": poll_count,
            "empty_poll_rate": (empty_poll_count / poll_count) if poll_count else 0.0,
            "event_post_count": len(event_samples),
            "transport_error_count": len(errors),
            "rate_limited_count": len(rate_limited),
            "by_direction": by_direction,
            "by_message_type": by_message_type,
            "by_command_type": {
                key: value
                for key, value in by_message_type.items()
                if key in {"send_order", "cancel_order", "modify_order", "tr_request", "register_realtime", "remove_realtime", "send_condition"}
            },
            "by_event_type": {
                key: value
                for key, value in by_message_type.items()
                if key in {"price_tick", "condition_event", "execution_event", "order_result", "heartbeat", "command_ack", "command_failed", "rate_limited"}
            },
            "warning_flags": self.warning_flags(samples),
        }
        return summary

    def warning_flags(self, samples: list[dict[str, Any]]) -> list[str]:
        flags: list[str] = []
        if _summary_value([s for s in samples if s.get("direction") == "gateway_to_core"], "total_wall_ms", 95) > self.config.event_p95_warn_ms:
            flags.append("EVENT_P95_HIGH")
        if _summary_value([s for s in samples if s.get("direction") == "core_to_gateway"], "total_wall_ms", 95) > self.config.command_p95_warn_ms:
            flags.append("COMMAND_P95_HIGH")
        if _summary_value([s for s in samples if s.get("direction") == "gateway_ack_to_core"], "ack_round_trip_ms", 95) > self.config.ack_p95_warn_ms:
            flags.append("ACK_P95_HIGH")
        if any(sample.get("clock_skew_warning") for sample in samples):
            flags.append("CLOCK_SKEW_WARNING")
        if any(sample.get("error") for sample in samples):
            flags.append("TRANSPORT_ERRORS")
        return flags

    def export_json(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "sample_id",
            "trace_id",
            "trade_date",
            "transport_mode",
            "direction",
            "message_type",
            "event_id",
            "command_id",
            "success",
            "error",
            "total_wall_ms",
            "gateway_queue_wait_ms",
            "gateway_post_ms",
            "core_receive_ms",
            "core_dispatch_wait_ms",
            "long_poll_wait_ms",
            "gateway_local_queue_wait_ms",
            "rate_limit_wait_ms",
            "gateway_execute_ms",
            "ack_round_trip_ms",
            "payload_size_bytes",
            "clock_skew_warning",
            "created_at",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for sample in report.get("samples", []):
                writer.writerow({field: sample.get(field, "") for field in fields})
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = report.get("summary", {})
        recommendation = report.get("websocket_recommendation", {})
        lines = [
            f"# Gateway Transport Latency Report ({report.get('trade_date') or 'all'})",
            "",
            f"- Generated at: {report.get('generated_at')}",
            f"- Transport mode: {report.get('transport_mode')}",
            f"- Samples: {summary.get('count', 0)}",
            "",
            "## Overall Latency",
            f"- p50/p90/p95/p99/max: {summary.get('p50_ms', 0):.1f} / {summary.get('p90_ms', 0):.1f} / {summary.get('p95_ms', 0):.1f} / {summary.get('p99_ms', 0):.1f} / {summary.get('max_ms', 0):.1f} ms",
            f"- Event p95: {summary.get('event_latency_p95_ms', 0):.1f} ms",
            f"- Command p95: {summary.get('command_latency_p95_ms', 0):.1f} ms",
            f"- Ack p95: {summary.get('ack_latency_p95_ms', 0):.1f} ms",
            f"- Long-poll wait p95: {summary.get('long_poll_wait_p95_ms', 0):.1f} ms",
            f"- Gateway execute p95: {summary.get('gateway_execute_p95_ms', 0):.1f} ms",
            f"- Rate limit wait p95: {summary.get('rate_limit_wait_p95_ms', 0):.1f} ms",
            "",
            "## Polling / Errors",
            f"- Empty poll rate: {summary.get('empty_poll_rate', 0) * 100:.1f}%",
            f"- Transport errors: {summary.get('transport_error_count', 0)}",
            f"- Rate limited samples: {summary.get('rate_limited_count', 0)}",
            "",
            "## WebSocket Decision",
            f"- Recommendation: {recommendation.get('recommendation', 'KEEP_REST_LONG_POLL')}",
            f"- Should switch now: {recommendation.get('should_switch', False)}",
            "",
            "### Reasons",
        ]
        lines.extend([f"- {reason}" for reason in recommendation.get("reasons", [])])
        blockers = recommendation.get("blockers") or []
        if blockers:
            lines.extend(["", "### Blockers", *[f"- {blocker}" for blocker in blockers]])
        lines.extend(["", "## Direction Groups"])
        for group, stats in (summary.get("by_direction") or {}).items():
            lines.append(f"- {group}: count={stats.get('count', 0)}, p95={stats.get('p95_ms', 0):.1f} ms")
        lines.extend(["", "## Message Type Groups"])
        for group, stats in (summary.get("by_message_type") or {}).items():
            lines.append(f"- {group}: count={stats.get('count', 0)}, p95={stats.get('p95_ms', 0):.1f} ms")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_report(self, report: dict, *, formats: list[str] | None = None) -> dict[str, str]:
        formats = formats or ["json", "csv", "md"]
        trade_date = report.get("trade_date") or str(report.get("generated_at") or "")[:10] or "all"
        target_dir = self.report_root / trade_date
        stem = f"gateway_transport_latency_{trade_date}"
        paths: dict[str, str] = {}
        if "json" in formats:
            paths["json"] = str(self.export_json(report, target_dir / f"{stem}.json"))
        if "csv" in formats:
            paths["csv"] = str(self.export_csv(report, target_dir / f"{stem}.csv"))
        if "md" in formats:
            paths["md"] = str(self.export_markdown(report, target_dir / f"{stem}.md"))
        return paths


def _group(samples: list[dict[str, Any]], key: str, value_field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        group_key = str(sample.get(key) or "")
        if not group_key:
            group_key = "unknown"
        groups.setdefault(group_key, []).append(sample)
    return {
        group_key: TransportLatencySummary.from_samples(group_samples, value_field=value_field).to_dict()
        for group_key, group_samples in sorted(groups.items())
    }


def _summary_value(samples: list[dict[str, Any]], field: str, p: float) -> float:
    values = [_float(sample.get(field)) for sample in samples]
    values = [value for value in values if value > 0]
    return percentile(values, p) if values else 0.0


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
