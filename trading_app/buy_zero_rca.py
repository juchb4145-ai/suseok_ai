from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Optional

from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


READY_NOT_ORDERED_CLASSES = (
    "READY_BUT_HYBRID_OBSERVE_ONLY",
    "READY_BUT_LEGACY_NOT_ELIGIBLE",
    "READY_BUT_CANDIDATE_STATE_NOT_READY",
    "READY_BUT_GATE_RESULT_KEY_MISMATCH",
    "READY_BUT_ENTRY_EXCLUDED",
    "READY_BUT_ENTRY_PLAN_DIAGNOSTIC_ONLY",
    "READY_BUT_DRY_RUN_REJECTED",
    "READY_BUT_LIVE_SIM_BLOCKED",
    "READY_BUT_DUPLICATE_ORDER",
    "READY_BUT_ORDER_SINK_NOOP",
)

DATA_REASON_MARKERS = ("DATA", "TICK", "SUPPORT", "VWAP", "BASELINE", "RELIABILITY", "WARMUP")
LATE_CHASE_MARKERS = ("LATE_CHASE", "CHASE_HIGH", "CHASE_RISK", "VWAP_OVEREXTENDED")
TRACE_STAGE_FUNNEL = (
    "CANDIDATE_GENERATED",
    "THEME_ENGINE_EVALUATED",
    "THEMELAB_GATE_EVALUATED",
    "HYBRID_GATE_EVALUATED",
    "RISK_GATE_EVALUATED",
    "SHADOW_SMALL_ENTRY_EVIDENCE_LOADED",
    "SHADOW_SMALL_ENTRY_CANDIDATE_EVALUATED",
    "SHADOW_SMALL_ENTRY_OBSERVE_ONLY",
    "SHADOW_SMALL_ENTRY_PROMOTED",
    "SHADOW_SMALL_ENTRY_BLOCKED",
    "LIFECYCLE_UPDATED",
    "ENTRY_PLAN_CREATED",
    "VIRTUAL_ORDER_SUBMITTED",
    "DRY_RUN_INTENT_CREATED",
    "LIVE_SIM_COMMAND_QUEUED",
    "SHADOW_SMALL_ENTRY_ORDER_SUBMITTED",
    "SHADOW_SMALL_ENTRY_ORDER_BLOCKED",
    "BROKER_ORDER_ACCEPTED",
    "PARTIAL_FILLED",
    "FILLED",
)
ORDER_SUBMITTED_STAGES = {"LIVE_SIM_COMMAND_QUEUED", "BROKER_ORDER_ACCEPTED", "PARTIAL_FILLED", "FILLED"}


class BuyZeroRCAAnalyzer:
    def __init__(self, db) -> None:
        self.db = db

    def build_summary(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        limit: int = 50000,
        include_missed_opportunities: bool = True,
    ) -> dict[str, Any]:
        traces = self._load_traces(trade_date=trade_date, window_sec=window_sec, limit=limit)
        by_candidate = _group_by_candidate(traces)
        ready_not_ordered = self.ready_not_ordered_report(
            trade_date=trade_date,
            window_sec=window_sec,
            traces=traces,
            limit=20,
        )
        missed = (
            self.missed_opportunity_report(trade_date=trade_date, limit=20)
            if include_missed_opportunities
            else {"summary": {}, "top_observe_then_rally_candidates": []}
        )

        block_stages: Counter[str] = Counter()
        block_reasons: Counter[str] = Counter()
        data_reasons: Counter[str] = Counter()
        late_chase: list[dict[str, Any]] = []
        live_sim_reasons: Counter[str] = Counter()
        live_sim_block_rows: list[dict[str, Any]] = []
        data_quality_reasons: Counter[str] = Counter()
        data_quality_actions: Counter[str] = Counter()
        early_small_rows: list[dict[str, Any]] = []
        status_counts: Counter[str] = Counter()
        last_updated_at = ""

        for trace in traces:
            created_at = str(trace.get("created_at") or "")
            if created_at > last_updated_at:
                last_updated_at = created_at
            reasons = _trace_reasons(trace)
            if trace.get("pass_fail") == "FAIL":
                block_stages[str(trace.get("stage") or "UNKNOWN")] += 1
                block_reasons.update(reasons or [str(trace.get("primary_block_reason") or "UNKNOWN")])
            for reason in reasons:
                if any(marker in reason.upper() for marker in DATA_REASON_MARKERS):
                    data_reasons[reason] += 1
                if any(marker in reason.upper() for marker in LATE_CHASE_MARKERS):
                    late_chase.append(_candidate_row(trace, reason=reason))
            bucket = str(trace.get("data_quality_bucket") or "")
            if bucket and bucket != "OK":
                data_quality_reasons[bucket] += 1
            action = str(trace.get("data_quality_action") or "")
            if action and action != "PASS":
                data_quality_actions[action] += 1
            if trace.get("early_small_candidate") is True:
                early_small_rows.append(_candidate_row(trace, reason=trace.get("early_small_rejected_reason") or bucket or action))
            if trace.get("stage") == "LIVE_SIM_BLOCKED":
                live_sim_reasons.update(reasons or [str(trace.get("live_sim_reason") or "UNKNOWN")])
                live_sim_block_rows.append(_candidate_row(trace, reason=(reasons[0] if reasons else "")))

        total_candidates = len(by_candidate)
        mapped_candidates = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("theme_id") or row.get("theme_name") for row in rows)
        }
        ready_candidates = {key for key, rows in by_candidate.items() if any(_is_ready(row) for row in rows)}
        entry_plan_candidates = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("stage") == "ENTRY_PLAN_CREATED" or row.get("entry_plan_id") for row in rows)
        }
        entry_plan_submittable = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("entry_plan_submittable") is True for row in rows)
        }
        dry_run_candidates = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("stage") == "DRY_RUN_INTENT_CREATED" and row.get("dry_run_intent_id") for row in rows)
        }
        live_sim_submitted = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("stage") in ORDER_SUBMITTED_STAGES for row in rows)
        }
        live_sim_blocked = {
            key
            for key, rows in by_candidate.items()
            if any(row.get("stage") == "LIVE_SIM_BLOCKED" for row in rows)
        }
        for rows in by_candidate.values():
            status = _candidate_gate_status(rows)
            if status:
                status_counts[status] += 1

        top_causes = _operator_top_causes(block_reasons, block_stages, live_sim_reasons, data_reasons)
        summary_fields = {
            "available": bool(traces),
            "total_candidates": total_candidates,
            "gate_evaluated_count": len(
                {
                    key
                    for key, rows in by_candidate.items()
                    if any(str(row.get("stage") or "").endswith("GATE_EVALUATED") for row in rows)
                }
            ),
            "ready_count": len(ready_candidates),
            "ready_exact_count": int(status_counts.get("READY", 0)),
            "ready_small_count": int(status_counts.get("READY_SMALL", 0)),
            "wait_count": int(status_counts.get("WAIT", 0)),
            "observe_count": int(status_counts.get("OBSERVE", 0)),
            "blocked_count": int(status_counts.get("BLOCKED", 0)),
            "entry_plan_created_count": len(entry_plan_candidates),
            "entry_plan_submittable_count": len(entry_plan_submittable),
            "dry_run_intent_count": len(dry_run_candidates),
            "live_sim_submitted_count": len(live_sim_submitted),
            "live_sim_blocked_count": len(live_sim_blocked),
            "last_updated_at": last_updated_at,
            "data_quality_taxonomy": {
                "data_insufficient_total_count": sum(data_quality_reasons.values()),
                "core_blocking_count": int(data_quality_reasons.get("CORE_BLOCKING", 0)),
                "entry_blocking_count": int(data_quality_reasons.get("ENTRY_BLOCKING", 0)),
                "warmup_optional_count": int(data_quality_reasons.get("WARMUP_OPTIONAL", 0)),
                "backfill_only_observe_count": int(data_quality_reasons.get("BACKFILL_ONLY_OBSERVE", 0)),
                "early_small_candidate_count": len(_dedupe_candidate_rows(early_small_rows)),
                "early_small_order_enabled_count": len(
                    _dedupe_candidate_rows([row for row in early_small_rows if row.get("early_small_order_enabled") is True])
                ),
                "early_small_observe_only_count": len(
                    _dedupe_candidate_rows([row for row in early_small_rows if row.get("early_small_order_enabled") is not True])
                ),
                "actions": _counter_rows(data_quality_actions, key_name="action", limit=10),
                "buckets": _counter_rows(data_quality_reasons, key_name="bucket", limit=10),
            },
        }
        return {
            "trade_date": trade_date or "",
            "window_sec": window_sec,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "available": bool(traces),
            "today_buy_zero": len(live_sim_submitted) == 0,
            "last_updated_at": last_updated_at,
            "total_trace_events": len(traces),
            "total_candidates": total_candidates,
            "theme_mapped_count": len(mapped_candidates),
            "unmapped_count": max(0, total_candidates - len(mapped_candidates)),
            "gate_evaluated_count": summary_fields["gate_evaluated_count"],
            "ready_count": len(ready_candidates),
            "ready_exact_count": int(status_counts.get("READY", 0)),
            "ready_small_count": int(status_counts.get("READY_SMALL", 0)),
            "wait_count": int(status_counts.get("WAIT", 0)),
            "observe_count": int(status_counts.get("OBSERVE", 0)),
            "blocked_count": int(status_counts.get("BLOCKED", 0)),
            "status_counts": _counter_rows(status_counts, key_name="status", limit=10),
            "ready_but_no_entry_plan_count": len(ready_candidates - entry_plan_candidates),
            "entry_plan_created_count": len(entry_plan_candidates),
            "entry_plan_submittable_count": len(entry_plan_submittable),
            "dry_run_intent_count": len(dry_run_candidates),
            "live_sim_submitted_count": len(live_sim_submitted),
            "live_sim_blocked_count": len(live_sim_blocked),
            "summary": summary_fields,
            "stage_funnel": _build_stage_funnel(traces),
            "top_block_stage": _counter_rows(block_stages, key_name="stage", limit=5),
            "top_block_reasons": _counter_rows(block_reasons, key_name="reason", limit=10),
            "top_data_insufficient_reasons": _counter_rows(data_reasons, key_name="reason", limit=10),
            "top_late_chase_candidates": _dedupe_candidate_rows(late_chase)[:10],
            "top_observe_then_rally_candidates": missed.get("top_observe_then_rally_candidates", [])[:10],
            "top_ready_not_ordered_candidates": ready_not_ordered.get("items", [])[:10],
            "operator_top_3_causes": top_causes[:3],
            "live_sim_block_reasons": _counter_rows(live_sim_reasons, key_name="reason", limit=10),
            "data_quality_reasons": _counter_rows(data_quality_reasons, key_name="bucket", limit=10),
            "data_quality_actions": _counter_rows(data_quality_actions, key_name="action", limit=10),
            "data_quality_taxonomy": summary_fields["data_quality_taxonomy"],
            "early_small_candidates": _dedupe_candidate_rows(early_small_rows)[:20],
            "ready_not_ordered": ready_not_ordered.get("summary", {}),
            "ready_not_ordered_report": ready_not_ordered,
            "ready_not_ordered_items": ready_not_ordered.get("items", [])[:20],
            "missed_opportunity": missed.get("summary", {}),
            "observe_blocked_after_rally": {
                "summary": missed.get("summary", {}),
                "items": missed.get("top_observe_then_rally_candidates", [])[:20],
                "reason_code_missed_opportunity_ranking": missed.get("reason_code_missed_opportunity_ranking", []),
            },
            "live_sim_blocked": {
                "count": len(live_sim_blocked),
                "reasons": _counter_rows(live_sim_reasons, key_name="reason", limit=10),
                "items": _dedupe_candidate_rows(live_sim_block_rows)[:20],
            },
            "data_quality_blocks": {
                "reasons": _counter_rows(data_reasons, key_name="reason", limit=10),
                "buckets": _counter_rows(data_quality_reasons, key_name="bucket", limit=10),
                "actions": _counter_rows(data_quality_actions, key_name="action", limit=10),
                "early_small_candidate_count": summary_fields["data_quality_taxonomy"]["early_small_candidate_count"],
                "early_small_order_enabled_count": summary_fields["data_quality_taxonomy"]["early_small_order_enabled_count"],
                "early_small_observe_only_count": summary_fields["data_quality_taxonomy"]["early_small_observe_only_count"],
            },
        }

    def ready_not_ordered_report(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        traces: Optional[list[dict[str, Any]]] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        traces = traces if traces is not None else self._load_traces(trade_date=trade_date, window_sec=window_sec)
        by_candidate = _group_by_candidate(traces)
        rows: list[dict[str, Any]] = []
        classification_counts: Counter[str] = Counter()
        for key, candidate_traces in by_candidate.items():
            if not any(_is_ready(row) for row in candidate_traces):
                continue
            if any(row.get("stage") in ORDER_SUBMITTED_STAGES for row in candidate_traces):
                continue
            classification = _classify_ready_not_ordered(candidate_traces)
            classification_counts[classification] += 1
            latest = _latest_trace(candidate_traces)
            rows.append(
                {
                    **_candidate_row(latest),
                    "classification": classification,
                    "stage_path": _stage_path(candidate_traces),
                    "primary_block_reason": _primary_candidate_reason(candidate_traces),
                    "reason_codes": sorted({reason for row in candidate_traces for reason in _trace_reasons(row)}),
                    "gate_score": _latest_value(candidate_traces, "gate_score"),
                    "theme_score": _latest_value(candidate_traces, "theme_score"),
                    "entry_plan_submittable": _latest_value(candidate_traces, "entry_plan_submittable"),
                    "entry_plan_diagnostic_only": _latest_value(candidate_traces, "entry_plan_diagnostic_only"),
                    "dry_run_status": _latest_non_empty(candidate_traces, "dry_run_status"),
                    "dry_run_reason": _latest_non_empty(candidate_traces, "dry_run_reason"),
                    "live_sim_status": _latest_non_empty(candidate_traces, "live_sim_status"),
                    "live_sim_reason": _latest_non_empty(candidate_traces, "live_sim_reason"),
                    "latest_tick_age_sec": _latest_value(candidate_traces, "latest_tick_age_sec"),
                    "support_ready": _latest_value(candidate_traces, "support_ready"),
                    "selected_support_source": _latest_non_empty(candidate_traces, "selected_support_source"),
                    "selected_support_price": _latest_value(candidate_traces, "selected_support_price"),
                    "trace_updated_at": latest.get("created_at") or "",
                }
            )
        rows.sort(key=lambda item: (item.get("classification") or "", item.get("created_at") or ""), reverse=True)
        return {
            "trade_date": trade_date or "",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "ready_not_ordered_count": len(rows),
                "by_classification": _counter_rows(classification_counts, key_name="classification", limit=len(READY_NOT_ORDERED_CLASSES)),
            },
            "items": rows[: max(1, int(limit or 100))],
            "classifications": list(READY_NOT_ORDERED_CLASSES),
        }

    def missed_opportunity_report(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            report = ThemeLabGateReasonOutcomeAnalyzer(self.db).build_report(trade_date=trade_date, limit=50000)
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
                "summary": {},
                "top_observe_then_rally_candidates": [],
                "missed_opportunity_ranking": [],
            }
        traces_by_code = defaultdict(list)
        for trace in self._load_traces(trade_date=trade_date, limit=50000):
            if trace.get("code"):
                traces_by_code[str(trace.get("code"))].append(trace)
        missed_items = []
        for item in report.get("items") or []:
            if not bool(item.get("missed_opportunity")):
                continue
            status = str(item.get("status") or "")
            if status not in {"WAIT", "OBSERVE", "BLOCKED"}:
                continue
            code = str(item.get("code") or "")
            linked = _latest_trace(traces_by_code.get(code, []))
            missed_items.append(
                {
                    "code": code,
                    "name": item.get("name") or linked.get("name") or "",
                    "candidate_instance_id": linked.get("candidate_instance_id") or "",
                    "status": status,
                    "primary_reason": item.get("primary_reason") or "",
                    "reason_codes": list(item.get("reason_codes") or []),
                    "base_price": item.get("base_price"),
                    "return_5m_pct": item.get("return_5m_pct"),
                    "return_15m_pct": item.get("return_15m_pct"),
                    "return_30m_pct": item.get("return_30m_pct"),
                    "mfe_15m_pct": item.get("mfe_15m_pct"),
                    "mae_15m_pct": item.get("mae_15m_pct"),
                    "minutes_to_ready": item.get("minutes_to_ready"),
                    "missed_opportunity": True,
                    "good_block": item.get("good_block"),
                    "theme_name": item.get("theme_name") or linked.get("theme_name") or "",
                    "stock_role": item.get("stock_role") or linked.get("stock_role") or "",
                    "price_location_status": item.get("price_location_status") or linked.get("price_location_status") or "",
                    "trace_id": linked.get("trace_id") or "",
                    "stage": linked.get("stage") or "",
                }
            )
        missed_items.sort(key=lambda row: (float(row.get("mfe_15m_pct") or 0.0), float(row.get("return_15m_pct") or 0.0)), reverse=True)
        ranking = list(report.get("top_missed_opportunity_reasons") or [])
        return {
            "status": report.get("status") or "READY",
            "trade_date": report.get("trade_date") or trade_date or "",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                **dict(report.get("summary") or {}),
                "linked_trace_count": sum(1 for item in missed_items if item.get("trace_id")),
            },
            "top_observe_then_rally_candidates": missed_items[: max(1, int(limit or 100))],
            "missed_opportunity_ranking": ranking,
            "reason_code_missed_opportunity_ranking": ranking,
        }

    def _load_traces(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        return self.db.list_buy_zero_trace_events(
            trade_date=trade_date,
            window_sec=window_sec,
            limit=max(1, int(limit or 50000)),
        )


def _group_by_candidate(traces: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        key = _candidate_key(trace)
        grouped[key].append(trace)
    for rows in grouped.values():
        rows.sort(key=lambda row: (row.get("created_at") or "", int(row.get("id") or 0)))
    return grouped


def _candidate_key(trace: dict[str, Any]) -> str:
    return str(
        trace.get("candidate_instance_id")
        or trace.get("candidate_id")
        or f"{trace.get('trade_date') or ''}:{trace.get('code') or ''}"
        or trace.get("trace_id")
    )


def _build_stage_funnel(traces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_stage: dict[str, dict[str, Any]] = {
        stage: {"stage": stage, "total": 0, "passed": 0, "failed": 0, "reasons": Counter()}
        for stage in TRACE_STAGE_FUNNEL
    }
    for trace in traces:
        stage = str(trace.get("stage") or "UNKNOWN")
        if stage not in by_stage:
            continue
        bucket = by_stage[stage]
        bucket["total"] += 1
        pass_fail = str(trace.get("pass_fail") or "").upper()
        passed = trace.get("passed")
        if pass_fail == "PASS" or passed is True:
            bucket["passed"] += 1
        elif pass_fail == "FAIL" or passed is False:
            bucket["failed"] += 1
            bucket["reasons"].update(_trace_reasons(trace) or [str(trace.get("primary_block_reason") or "UNKNOWN")])
    rows: list[dict[str, Any]] = []
    for stage in TRACE_STAGE_FUNNEL:
        item = by_stage[stage]
        top_reason, top_count = ("", 0)
        if item["reasons"]:
            top_reason, top_count = item["reasons"].most_common(1)[0]
        rows.append(
            {
                "stage": stage,
                "total": int(item["total"]),
                "passed": int(item["passed"]),
                "failed": int(item["failed"]),
                "top_reason": top_reason,
                "top_reason_count": int(top_count),
            }
        )
    return rows


def _candidate_gate_status(traces: list[dict[str, Any]]) -> str:
    for row in reversed(traces):
        status = str(row.get("gate_status") or "").upper()
        if status in {"READY", "READY_SMALL", "WAIT", "OBSERVE", "BLOCKED"}:
            return status
    for row in reversed(traces):
        status = str(row.get("stage_status") or "").upper()
        if status in {"READY", "READY_SMALL", "WAIT", "OBSERVE", "BLOCKED"}:
            return status
    return ""


def _is_ready(trace: dict[str, Any]) -> bool:
    return str(trace.get("gate_status") or "").upper().startswith("READY") or str(trace.get("stage_status") or "").upper().startswith("READY")


def _classify_ready_not_ordered(traces: list[dict[str, Any]]) -> str:
    text = " ".join(
        part
        for row in traces
        for part in [
            str(row.get("primary_block_reason") or ""),
            str(row.get("dry_run_reason") or ""),
            str(row.get("live_sim_reason") or ""),
            " ".join(_trace_reasons(row)),
            str(row.get("details") or ""),
        ]
    ).upper()
    stages = {str(row.get("stage") or "") for row in traces}
    if "HYBRID_OBSERVE_ONLY" in text or "HYBRID_GATE_OBSERVE_ONLY" in text:
        return "READY_BUT_HYBRID_OBSERVE_ONLY"
    if "LEGACY_NOT_ELIGIBLE" in text or "STRATEGY_NOT_ELIGIBLE" in text:
        return "READY_BUT_LEGACY_NOT_ELIGIBLE"
    if "GATE_RESULT_KEY_MISMATCH" in text or "KEY_MISMATCH" in text:
        return "READY_BUT_GATE_RESULT_KEY_MISMATCH"
    if "ENTRY_NOT_ALLOWED_FOR_CANDIDATE" in text or "ENTRY_EXCLUDED" in text:
        return "READY_BUT_ENTRY_EXCLUDED"
    if any(row.get("entry_plan_diagnostic_only") is True for row in traces) or "DIAGNOSTIC_ONLY" in text or "MAX_CHASE_EXCEEDED" in text:
        return "READY_BUT_ENTRY_PLAN_DIAGNOSTIC_ONLY"
    if "ORDER_SINK_MISSING" in text or "OBSERVE_VIRTUAL_ONLY" in text or "DRY_RUN_ORDER_ENQUEUE_DISABLED" in text:
        return "READY_BUT_ORDER_SINK_NOOP"
    if "DUPLICATE" in text or any(str(row.get("dry_run_status") or row.get("live_sim_status") or "").upper() == "DUPLICATE" for row in traces):
        return "READY_BUT_DUPLICATE_ORDER"
    if any(row.get("stage") == "LIVE_SIM_BLOCKED" for row in traces) or any(
        str(row.get("live_sim_status") or "").upper() in {"BLOCKED", "SKIPPED", "REJECTED", "ERROR", "DUPLICATE"}
        for row in traces
    ):
        return "READY_BUT_LIVE_SIM_BLOCKED"
    if "DRY_RUN" in text and ("REJECT" in text or "SKIPPED" in text or "ERROR" in text):
        return "READY_BUT_DRY_RUN_REJECTED"
    if "LIFECYCLE_UPDATED" not in stages or not any(str(row.get("stage_status") or "").upper().startswith("READY") for row in traces if row.get("stage") == "LIFECYCLE_UPDATED"):
        return "READY_BUT_CANDIDATE_STATE_NOT_READY"
    return "READY_BUT_DRY_RUN_REJECTED"


def _trace_reasons(trace: dict[str, Any]) -> list[str]:
    values = [str(reason) for reason in trace.get("reason_codes") or [] if str(reason)]
    primary = str(trace.get("primary_block_reason") or "")
    if primary:
        values.append(primary)
    return _dedupe(values)


def _candidate_row(trace: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    return {
        "trace_id": trace.get("trace_id") or "",
        "created_at": trace.get("created_at") or "",
        "candidate_instance_id": trace.get("candidate_instance_id") or "",
        "candidate_id": trace.get("candidate_id"),
        "code": trace.get("code") or "",
        "name": trace.get("name") or "",
        "theme_id": trace.get("theme_id") or "",
        "theme_name": trace.get("theme_name") or "",
        "stage": trace.get("stage") or "",
        "stage_status": trace.get("stage_status") or "",
        "gate_status": trace.get("gate_status") or "",
        "primary_block_reason": reason or trace.get("primary_block_reason") or "",
        "price_location_status": trace.get("price_location_status") or "",
        "price_location_readiness": trace.get("price_location_readiness") or "",
        "stock_role": trace.get("stock_role") or "",
        "data_quality_bucket": trace.get("data_quality_bucket") or "",
        "data_quality_action": trace.get("data_quality_action") or "",
        "missing_core_fields": list(trace.get("missing_core_fields") or []),
        "missing_entry_fields": list(trace.get("missing_entry_fields") or []),
        "missing_optional_fields": list(trace.get("missing_optional_fields") or []),
        "early_small_candidate": trace.get("early_small_candidate"),
        "early_small_order_enabled": trace.get("early_small_order_enabled"),
        "early_small_position_size_multiplier": trace.get("early_small_position_size_multiplier"),
        "early_small_rejected_reason": trace.get("early_small_rejected_reason") or "",
        "operator_message_ko": trace.get("operator_message_ko") or "",
    }


def _latest_trace(traces: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(traces or [])
    if not rows:
        return {}
    return sorted(rows, key=lambda row: (row.get("created_at") or "", int(row.get("id") or 0)))[-1]


def _latest_non_empty(traces: list[dict[str, Any]], key: str) -> str:
    for row in reversed(traces):
        value = str(row.get(key) or "")
        if value:
            return value
    return ""


def _latest_value(traces: list[dict[str, Any]], key: str) -> Any:
    for row in reversed(traces):
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _stage_path(traces: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "stage": str(row.get("stage") or ""),
            "stage_status": str(row.get("stage_status") or ""),
            "pass_fail": str(row.get("pass_fail") or ""),
            "reason": str(row.get("primary_block_reason") or ""),
        }
        for row in traces
    ]


def _primary_candidate_reason(traces: list[dict[str, Any]]) -> str:
    for row in reversed(traces):
        if str(row.get("primary_block_reason") or ""):
            return str(row.get("primary_block_reason") or "")
    for row in reversed(traces):
        reasons = _trace_reasons(row)
        if reasons:
            return reasons[0]
    return ""


def _operator_top_causes(
    block_reasons: Counter[str],
    block_stages: Counter[str],
    live_sim_reasons: Counter[str],
    data_reasons: Counter[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reason, count in block_reasons.most_common(5):
        rows.append({"category": "block_reason", "reason": reason, "count": count})
    for stage, count in block_stages.most_common(3):
        rows.append({"category": "block_stage", "reason": stage, "count": count})
    for reason, count in live_sim_reasons.most_common(3):
        rows.append({"category": "live_sim_block", "reason": reason, "count": count})
    for reason, count in data_reasons.most_common(3):
        rows.append({"category": "data_quality", "reason": reason, "count": count})
    rows.sort(key=lambda row: int(row.get("count") or 0), reverse=True)
    return rows


def _counter_rows(counter: Counter[str], *, key_name: str, limit: int = 10) -> list[dict[str, Any]]:
    return [{key_name: key, "count": count} for key, count in counter.most_common(max(1, int(limit or 10)))]


def _dedupe_candidate_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("candidate_instance_id") or row.get("code") or row.get("trace_id") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
