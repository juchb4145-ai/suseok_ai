from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit SetupRouter V3 OBSERVE outputs.")
    parser.add_argument("--db", default="data/trader.sqlite3")
    parser.add_argument("--trade-date", default=datetime.now().date().isoformat())
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--router-version", default="setup_router_v3.5")
    parser.add_argument("--include-legacy-version", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    trade_date = str(args.trade_date)
    output_dir = Path(args.output_dir or Path("reports") / "setup_router_v3" / trade_date)
    output_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        observations = _rows(con, "setup_observations_latest_v2", trade_date, limit=args.limit, router_version=args.router_version, include_legacy_version=args.include_legacy_version)
        transitions = _rows(con, "setup_observation_transitions_v2", trade_date, limit=args.limit, router_version=args.router_version, include_legacy_version=args.include_legacy_version)
        states = _rows(con, "setup_router_state_v3", trade_date, limit=args.limit, router_version=args.router_version, include_legacy_version=args.include_legacy_version)
        state_transitions = _rows(con, "setup_router_state_transitions_v3", trade_date, limit=args.limit, router_version=args.router_version, include_legacy_version=args.include_legacy_version)
        runs = _rows(con, "setup_router_runs", trade_date, limit=1000, router_version=args.router_version)
        runtime_rows = _rows(con, "setup_router_candidate_runtime_v4", trade_date, limit=args.limit, router_version=args.router_version, include_legacy_version=args.include_legacy_version)
        full_counts = _full_counts(con, trade_date, args.router_version)
        integrity = _state_integrity(con, trade_date, args.router_version)
        scheduling = _scheduling_checks(con, trade_date, args.router_version)
        side_effects = _side_effect_counts(con, trade_date)
        span_metrics = _span_metrics(con, trade_date, args.router_version)
    finally:
        con.close()

    type_counts = Counter(row.get("setup_type", "UNKNOWN") for row in observations)
    status_counts = Counter(row.get("router_status", "UNKNOWN") for row in observations)
    shape_counts = Counter(row.get("shape_status", "UNKNOWN") for row in observations)
    context_counts = Counter(row.get("context_status", "UNKNOWN") for row in observations)
    invalid = _invalid_observations(observations)
    flip_analysis = _flip_analysis(transitions)
    context_alignment = _context_alignment(observations)
    run_span_min = float(span_metrics.get("run_span_minutes") or 0.0)
    failures: list[str] = []
    warnings: list[str] = []
    if full_counts.get("observations", 0) == 0 and full_counts.get("runs", 0) == 0:
        warnings.append("NO_SETUP_ROUTER_V3_ROWS")
    if invalid:
        failures.append("INVALID_SETUP_ROUTER_V3_OBSERVATIONS")
    if any(row.get("current_router_status") == "VALID_OBSERVE" and row.get("current_context_status") == "BLOCKED" for row in transitions):
        failures.append("VALID_OBSERVE_WITH_BLOCKED_CONTEXT_TRANSITION")
    if any(value > 0 for value in side_effects.values()):
        failures.append("SETUP_ROUTER_ORDER_SIDE_EFFECTS_PRESENT")
    if full_counts.get("valid_observe", 0) == 0:
        warnings.append("NO_VALID_SETUP_SAMPLE")
    if full_counts.get("valid_observe", 0) == 1:
        warnings.append("SINGLE_VALID_SETUP_SAMPLE")
    if run_span_min < 60:
        warnings.append("RUN_SPAN_LT_60_MIN")
    if full_counts.get("observations", 0) > 0 and full_counts.get("states", 0) == 0:
        failures.append("TEMPORAL_STATE_ROWS_MISSING")
    if full_counts.get("eligible_runtime", 0) == 0 and full_counts.get("observations", 0) > 0:
        warnings.append("CANDIDATE_RUNTIME_SAMPLE_MISSING")
    if full_counts.get("valid_market_closed", 0) > 0:
        failures.append("VALID_IN_MARKET_CLOSED_SQL")
    if full_counts.get("valid_post_subscription_unverified", 0) > 0:
        failures.append("VALID_WITH_POST_SUBSCRIPTION_TICK_MISSING_SQL")
    if full_counts.get("valid_tr_backfill", 0) > 0:
        failures.append("VALID_WITH_TR_BACKFILL_SQL")
    if any(row.get("router_status") == "VALID_OBSERVE" and not bool(row.get("post_subscription_tick_verified", True)) for row in observations):
        failures.append("VALID_WITH_POST_SUBSCRIPTION_TICK_MISSING")
    if any(row.get("router_status") == "VALID_OBSERVE" and row.get("session_phase") in {"CLOSING_RISK", "MARKET_CLOSED"} for row in observations):
        failures.append("VALID_IN_CLOSING_OR_CLOSED_SESSION")
    if any(row.get("router_status") == "VALID_OBSERVE" and row.get("context_status") != "ELIGIBLE" for row in observations):
        failures.append("VALID_CONTEXT_NOT_ELIGIBLE")
    failures.extend(integrity["failures"])
    warnings.extend(integrity["warnings"])
    failures.extend(scheduling["failures"])
    warnings.extend(scheduling["warnings"])
    stable_ready = (
        not failures
        and not warnings
        and full_counts.get("valid_observe", 0) > 0
        and run_span_min >= 60
        and full_counts.get("states", 0) > 0
        and full_counts.get("state_transitions", 0) > 0
    )
    verdict = "STABLE_FOR_OPPORTUNITY_RANKER" if stable_ready else "NOT_STABLE" if failures else "CONDITIONALLY_STABLE"
    summary = {
        "schema_version": "setup_router_v3.audit.v5",
        "trade_date": trade_date,
        "router_version": args.router_version,
        "db_path": str(db_path),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "verdict": verdict,
        "observation_count": len(observations),
        "transition_count": len(transitions),
        "state_count": len(states),
        "state_transition_count": len(state_transitions),
        "run_count": len(runs),
        "candidate_runtime_count": len(runtime_rows),
        "sample_counts": {
            "observations": len(observations),
            "transitions": len(transitions),
            "states": len(states),
            "state_transitions": len(state_transitions),
            "runs": len(runs),
            "candidate_runtime": len(runtime_rows),
        },
        "full_counts": full_counts,
        "run_span_minutes": run_span_min,
        "span_metrics": span_metrics,
        "type_counts": dict(type_counts),
        "status_counts": dict(status_counts),
        "shape_counts": dict(shape_counts),
        "context_counts": dict(context_counts),
        "invalid_count": len(invalid),
        "failures": failures,
        "warnings": warnings,
        "side_effect_counts": side_effects,
        "state_integrity": integrity,
        "scheduling": scheduling,
        "safety": {
            "ready_allowed": False,
            "candidate_promotion_allowed": False,
            "opportunity_rank_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
        },
    }
    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "type_counts.json", {"setup_type_counts": dict(type_counts), "router_status_counts": dict(status_counts)})
    _write_json(output_dir / "transitions.json", transitions[:1000])
    _write_json(output_dir / "states.json", states[:1000])
    _write_json(output_dir / "state_transitions.json", state_transitions[:1000])
    _write_json(output_dir / "candidate_runtime.json", runtime_rows[:1000])
    _write_json(output_dir / "invalid_observations.json", invalid[:1000])
    _write_json(output_dir / "flip_analysis.json", flip_analysis)
    _write_json(output_dir / "context_alignment.json", context_alignment)
    (output_dir / "report.md").write_text(_markdown(summary, flip_analysis, context_alignment), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if failures else 0


def _rows(
    con: sqlite3.Connection,
    table: str,
    trade_date: str,
    *,
    limit: int,
    router_version: str | None = None,
    include_legacy_version: bool = False,
) -> list[dict[str, Any]]:
    if not _has_table(con, table):
        return []
    clauses = ["trade_date = ?"]
    params: list[Any] = [trade_date]
    if router_version and _has_column(con, table, "router_version"):
        clauses.append("(router_version = ? OR router_version = '')" if include_legacy_version else "router_version = ?")
        params.append(router_version)
    order_col = "rowid"
    if table in {"setup_router_state_transitions_v2", "setup_router_state_transitions_v3"}:
        order_col = "occurred_at"
    elif table in {"setup_router_state_v2", "setup_router_state_v3"}:
        order_col = "updated_at"
    rows = con.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE {" AND ".join(clauses)}
        ORDER BY {order_col} DESC
        LIMIT ?
        """,
        (*params, max(1, int(limit))),
    ).fetchall()
    return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    payload = _json(data.pop("payload_json", "{}"), {})
    if isinstance(payload, dict):
        payload.update({key: value for key, value in data.items() if key not in payload})
        data = payload
    for json_key, target in (
        ("reason_codes_json", "reason_codes"),
        ("price_structure_json", "price_structure"),
        ("state_payload_json", "state_payload"),
        ("safety_json", "safety"),
    ):
        if json_key in data:
            data[target] = _json(data.pop(json_key), [] if json_key.endswith("codes_json") else {})
    return data


def _invalid_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invalid = []
    for row in rows:
        safety = dict(row.get("safety") or {})
        flags = {
            "ready_allowed": row.get("ready_allowed") or safety.get("ready_allowed"),
            "candidate_promotion_allowed": row.get("candidate_promotion_allowed") or safety.get("candidate_promotion_allowed"),
            "opportunity_rank_allowed": row.get("opportunity_rank_allowed") or safety.get("opportunity_rank_allowed"),
            "order_intent_allowed": row.get("order_intent_allowed") or safety.get("order_intent_allowed"),
            "live_order_allowed": row.get("live_order_allowed") or safety.get("live_order_allowed"),
        }
        reasons = []
        if any(bool(value) for value in flags.values()):
            reasons.append("SAFETY_FLAG_TRUE")
        if row.get("router_status") == "VALID_OBSERVE" and row.get("context_status") != "ELIGIBLE":
            reasons.append("VALID_OBSERVE_CONTEXT_NOT_ELIGIBLE")
        if row.get("router_status") == "VALID_OBSERVE" and row.get("shape_status") != "MATCHED":
            reasons.append("VALID_OBSERVE_SHAPE_NOT_MATCHED")
        if row.get("router_status") == "VALID_OBSERVE" and row.get("entry_alignment_status") in {"ENTRY_DECISION_MISSING", "ENTRY_DECISION_STALE"}:
            reasons.append("VALID_OBSERVE_ENTRY_DECISION_NOT_FRESH")
        if row.get("router_status") == "VALID_OBSERVE" and row.get("price_source") == "TR_BACKFILL":
            reasons.append("VALID_OBSERVE_TR_BACKFILL")
        if row.get("theme_state") == "LEADER_ONLY_THEME" and row.get("stock_role") not in {"LEADER", "CO_LEADER", "LEADER_CONFIRMED", "CO_LEADER_CONFIRMED"}:
            reasons.append("LEADER_ONLY_NON_LEADER")
        if row.get("primary_setup") and row.get("shape_status") not in {"MATCHED", "FORMING"}:
            reasons.append("PRIMARY_WITHOUT_ACTIVE_SETUP")
        if reasons:
            invalid.append({**row, "audit_reasons": reasons})
    return invalid


def _flip_analysis(transitions: list[dict[str, Any]]) -> dict[str, Any]:
    by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in transitions:
        by_candidate[f"{row.get('candidate_instance_id')}:{row.get('setup_type')}"].append(row)
    flip_counts = {key: len(items) for key, items in by_candidate.items() if len(items) >= 3}
    return {
        "transition_group_count": len(by_candidate),
        "frequent_flip_count": len(flip_counts),
        "frequent_flips": dict(sorted(flip_counts.items(), key=lambda item: item[1], reverse=True)[:50]),
    }


def _context_alignment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    mismatches = [
        row
        for row in rows
        if row.get("router_status") == "VALID_OBSERVE"
        and (row.get("context_status") != "ELIGIBLE" or row.get("shape_status") != "MATCHED")
    ]
    return {
        "valid_observe_count": sum(1 for row in rows if row.get("router_status") == "VALID_OBSERVE"),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
    }


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return row is not None


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(con, table):
        return False
    return column in {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_table_rows(rows: list[dict[str, Any]]) -> bool:
    return bool(rows)


def _side_effect_counts(con: sqlite3.Connection, trade_date: str) -> dict[str, int]:
    tables = {
        "entry_plans": "trade_date",
        "runtime_order_intents": "trade_date",
        "managed_order_intents": "trade_date",
        "virtual_orders": "trade_date",
        "virtual_positions": "trade_date",
    }
    result: dict[str, int] = {}
    for table, date_col in tables.items():
        if not _has_table(con, table) or not _has_column(con, table, date_col):
            result[table] = 0
            continue
        try:
            row = con.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {date_col} = ?", (trade_date,)).fetchone()
            result[table] = int(row["count"] or 0) if row else 0
        except sqlite3.Error:
            result[table] = 0
    result["gateway_order_commands"] = _gateway_order_command_count(con, trade_date)
    result["candidate_events_setup_router_v3"] = _source_count(con, "candidate_events", trade_date)
    result["candidate_state_transitions_setup_router_v3"] = _source_count(con, "candidate_state_transitions", trade_date)
    return result


def _gateway_order_command_count(con: sqlite3.Connection, trade_date: str) -> int:
    if not _has_table(con, "gateway_commands"):
        return 0
    if not _has_column(con, "gateway_commands", "trade_date") or not _has_column(con, "gateway_commands", "command_type"):
        return 0
    row = con.execute(
        """
        SELECT COUNT(*) AS count
        FROM gateway_commands
        WHERE trade_date = ? AND command_type IN ('send_order','cancel_order')
        """,
        (trade_date,),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _source_count(con: sqlite3.Connection, table: str, trade_date: str) -> int:
    if not _has_table(con, table) or not _has_column(con, table, "trade_date"):
        return 0
    source_filters = []
    params: list[Any] = [trade_date]
    if _has_column(con, table, "source"):
        source_filters.append("source = ?")
        params.append("setup_router_v3")
    if _has_column(con, table, "source_type"):
        source_filters.append("source_type = ?")
        params.append("setup_router_v3")
    if _has_column(con, table, "payload_json"):
        source_filters.append("payload_json LIKE ?")
        params.append("%setup_router_v3%")
    if not source_filters:
        return 0
    row = con.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE trade_date = ? AND ({' OR '.join(source_filters)})",
        tuple(params),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _full_counts(con: sqlite3.Connection, trade_date: str, router_version: str) -> dict[str, int]:
    return {
        "observations": _count(con, "setup_observations_latest_v2", trade_date, router_version=router_version),
        "valid_observe": _count(con, "setup_observations_latest_v2", trade_date, router_version=router_version, extra="router_status = 'VALID_OBSERVE'"),
        "valid_market_closed": _count(con, "setup_observations_latest_v2", trade_date, router_version=router_version, extra="router_status = 'VALID_OBSERVE' AND session_phase = 'MARKET_CLOSED'"),
        "valid_post_subscription_unverified": _count(con, "setup_observations_latest_v2", trade_date, router_version=router_version, extra="router_status = 'VALID_OBSERVE' AND post_subscription_tick_verified = 0"),
        "valid_tr_backfill": _count(con, "setup_observations_latest_v2", trade_date, router_version=router_version, extra="router_status = 'VALID_OBSERVE' AND payload_json LIKE '%TR_BACKFILL%'"),
        "commits": _count(con, "setup_router_evaluation_commits_v1", trade_date, router_version=router_version),
        "states": _count(con, "setup_router_state_v3", trade_date, router_version=router_version),
        "state_transitions": _count(con, "setup_router_state_transitions_v3", trade_date, router_version=router_version),
        "runs": _count(con, "setup_router_runs", trade_date, router_version=router_version),
        "eligible_runtime": _count(con, "setup_router_candidate_runtime_v4", trade_date, router_version=router_version),
        "pending_queue": _count(con, "setup_router_pending_evaluations_v5", trade_date, router_version=router_version, extra="status IN ('PENDING','RETRY','SELECTED')"),
    }


def _count(con: sqlite3.Connection, table: str, trade_date: str, *, router_version: str | None = None, extra: str = "") -> int:
    if not _has_table(con, table):
        return 0
    clauses = ["trade_date = ?"]
    params: list[Any] = [trade_date]
    if router_version and _has_column(con, table, "router_version"):
        clauses.append("router_version = ?")
        params.append(router_version)
    if extra:
        clauses.append(extra)
    row = con.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {' AND '.join(clauses)}", tuple(params)).fetchone()
    return int(row["count"] or 0) if row else 0


def _state_integrity(con: sqlite3.Connection, trade_date: str, router_version: str) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    metrics = {
        "price_only_state_transition_count": 0,
        "theme_leak_active_state_count": 0,
        "ttl_violation_count": 0,
        "invalid_generation_count": 0,
        "terminal_revival_same_generation_count": 0,
        "matched_to_forming_same_generation_count": 0,
        "matched_to_seeking_same_generation_count": 0,
        "invalidated_to_active_same_generation_count": 0,
        "expired_to_active_same_generation_count": 0,
        "generation_jump_count": 0,
        "repeated_first_pullback_match_count": 0,
        "same_reference_breakout_new_generation_count": 0,
        "vwap_anchor_changed_same_generation_count": 0,
        "duplicate_material_transition_count": 0,
        "material_hash_state_conflict_count": 0,
        "version_mismatch_row_count": 0,
    }
    for table in (
        "setup_observations_latest_v2",
        "setup_observation_transitions_v2",
        "setup_router_primary_latest_v2",
        "setup_router_evaluation_commits_v1",
        "setup_router_state_v3",
        "setup_router_state_transitions_v3",
        "setup_router_candidate_runtime_v4",
        "setup_router_pending_evaluations_v5",
    ):
        if _has_table(con, table) and _has_column(con, table, "router_version"):
            row = con.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM {table}
                WHERE trade_date = ? AND COALESCE(router_version, '') <> ?
                """,
                (trade_date, router_version),
            ).fetchone()
            metrics["version_mismatch_row_count"] += int(row["count"] or 0) if row else 0
    if _has_table(con, "setup_router_state_transitions_v3") and _has_column(con, "setup_router_state_transitions_v3", "material_change_kind"):
        row = con.execute(
            """
            SELECT COUNT(*) AS count
            FROM setup_router_state_transitions_v3
            WHERE trade_date = ? AND router_version = ? AND COALESCE(material_change_kind, '') IN ('', 'NONE')
            """,
            (trade_date, router_version),
        ).fetchone()
        metrics["price_only_state_transition_count"] = int(row["count"] or 0) if row else 0
        rows = [
            _row(row)
            for row in con.execute(
                """
                SELECT *
                FROM setup_router_state_transitions_v3
                WHERE trade_date = ? AND router_version = ?
                ORDER BY candidate_instance_id, theme_id, setup_type, setup_generation, occurred_at
                """,
                (trade_date, router_version),
            ).fetchall()
        ]
        metrics.update(_terminal_integrity_metrics(rows))
    if _has_table(con, "setup_router_state_v3"):
        if _has_column(con, "setup_router_state_v3", "expires_at"):
            row = con.execute(
                """
                SELECT COUNT(*) AS count
                FROM setup_router_state_v3
                WHERE trade_date = ? AND router_version = ? AND lifecycle_state IN ('FORMING','MATCHED')
                  AND expires_at <> '' AND expires_at <= last_evaluated_at
                """,
                (trade_date, router_version),
            ).fetchone()
            metrics["ttl_violation_count"] = int(row["count"] or 0) if row else 0
        row = con.execute(
            """
            SELECT COUNT(*) AS count
            FROM setup_router_state_v3
            WHERE trade_date = ? AND router_version = ? AND setup_generation < 1
            """,
            (trade_date, router_version),
        ).fetchone()
        metrics["invalid_generation_count"] = int(row["count"] or 0) if row else 0
    if metrics["price_only_state_transition_count"] > 0:
        failures.append("PRICE_ONLY_STATE_TRANSITIONS_PRESENT")
    if metrics["ttl_violation_count"] > 0:
        failures.append("TTL_ACTIVE_STATE_VIOLATION")
    if metrics["invalid_generation_count"] > 0:
        failures.append("INVALID_SETUP_GENERATION")
    if metrics["version_mismatch_row_count"] > 0:
        failures.append("SETUP_ROUTER_VERSION_MISMATCH_ROWS_PRESENT")
    for key, failure in (
        ("terminal_revival_same_generation_count", "TERMINAL_REVIVAL_SAME_GENERATION"),
        ("generation_jump_count", "SETUP_GENERATION_JUMP"),
        ("repeated_first_pullback_match_count", "REPEATED_FIRST_PULLBACK_MATCH"),
        ("same_reference_breakout_new_generation_count", "SAME_REFERENCE_BREAKOUT_GENERATION"),
        ("vwap_anchor_changed_same_generation_count", "VWAP_ANCHOR_CHANGED_SAME_GENERATION"),
        ("duplicate_material_transition_count", "DUPLICATE_MATERIAL_TRANSITION"),
        ("material_hash_state_conflict_count", "MATERIAL_HASH_STATE_CONFLICT"),
    ):
        if metrics.get(key, 0) > 0:
            failures.append(failure)
    if _count(con, "setup_router_state_transitions_v3", trade_date, router_version=router_version) == 0 and _count(con, "setup_router_state_v3", trade_date, router_version=router_version) > 0:
        warnings.append("STATE_TRANSITION_SAMPLE_MISSING")
    return {"metrics": metrics, "failures": failures, "warnings": warnings}


def _scheduling_checks(con: sqlite3.Connection, trade_date: str, router_version: str) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    metrics = {
        "max_actual_starved_candidate_count": 0,
        "max_deferred_incremental_count": 0,
        "max_deferred_ttl_count": 0,
        "run_sample_count": 0,
        "pending_queue_count": 0,
        "retry_queue_count": 0,
        "failed_permanent_count": 0,
        "retry_due_count": 0,
        "stale_selected_count": 0,
        "invalid_pending_epoch_count": 0,
        "oldest_pending_age_sec": 0,
        "processed_signature_without_success_count": 0,
        "never_evaluated_count": 0,
    }
    if not _has_table(con, "setup_router_runs"):
        return {"metrics": metrics, "failures": failures, "warnings": ["SETUP_ROUTER_RUN_TABLE_MISSING"]}
    rows = _rows(con, "setup_router_runs", trade_date, limit=100000, router_version=router_version)
    metrics["run_sample_count"] = len(rows)
    for row in rows:
        metrics["max_actual_starved_candidate_count"] = max(metrics["max_actual_starved_candidate_count"], int(row.get("actual_starved_candidate_count") or 0))
        metrics["max_deferred_incremental_count"] = max(metrics["max_deferred_incremental_count"], int(row.get("deferred_incremental_count") or 0))
        metrics["max_deferred_ttl_count"] = max(metrics["max_deferred_ttl_count"], int(row.get("deferred_ttl_count") or 0))
    if metrics["max_actual_starved_candidate_count"] > 0:
        failures.append("SETUP_ROUTER_STARVATION_PRESENT")
    if _has_table(con, "setup_router_pending_evaluations_v5"):
        pending_rows = [
            _row(row)
            for row in con.execute(
                """
                SELECT *
                FROM setup_router_pending_evaluations_v5
                WHERE trade_date = ? AND router_version = ? AND status IN ('PENDING','RETRY','SELECTED','FAILED_PERMANENT')
                """,
                (trade_date, router_version),
            ).fetchall()
        ]
        metrics["pending_queue_count"] = len(pending_rows)
        metrics["retry_queue_count"] = sum(1 for row in pending_rows if row.get("status") == "RETRY")
        metrics["failed_permanent_count"] = sum(1 for row in pending_rows if row.get("status") == "FAILED_PERMANENT")
        now = max([_parse_time(row.get("calculated_at")) for row in rows if _parse_time(row.get("calculated_at")) is not None], default=datetime.now())
        metrics["retry_due_count"] = sum(
            1
            for row in pending_rows
            if row.get("status") == "RETRY"
            and (not row.get("next_retry_at") or (_parse_time(row.get("next_retry_at")) is not None and _parse_time(row.get("next_retry_at")) <= now))
        )
        metrics["stale_selected_count"] = sum(
            1
            for row in pending_rows
            if row.get("status") == "SELECTED"
            and (now - (_parse_time(row.get("selected_at") or row.get("last_attempt_at")) or now)).total_seconds() > 30
        )
        metrics["invalid_pending_epoch_count"] = sum(1 for row in pending_rows if int(row.get("pending_epoch") or 0) < 1 or not str(row.get("pending_instance_id") or ""))
        pending_ages = [
            max(0.0, (now - parsed).total_seconds())
            for parsed in (_parse_time(row.get("first_pending_at") or row.get("last_pending_at")) for row in pending_rows)
            if parsed is not None
        ]
        metrics["oldest_pending_age_sec"] = int(max(pending_ages, default=0.0))
        if metrics["retry_due_count"] > 0:
            failures.append("SETUP_ROUTER_RETRY_DUE_STALE")
        if metrics["stale_selected_count"] > 0:
            failures.append("SETUP_ROUTER_SELECTED_LEASE_STALE")
        if metrics["invalid_pending_epoch_count"] > 0:
            failures.append("SETUP_ROUTER_PENDING_EPOCH_INVALID")
        if metrics["failed_permanent_count"] > 0:
            failures.append("SETUP_ROUTER_FAILED_PERMANENT_PRESENT")
        if metrics["oldest_pending_age_sec"] > 300:
            failures.append("SETUP_ROUTER_PENDING_BACKLOG_STALE")
    if _has_table(con, "setup_router_candidate_runtime_v4"):
        runtime_rows = [
            _row(row)
            for row in con.execute(
                """
                SELECT *
                FROM setup_router_candidate_runtime_v4
                WHERE trade_date = ? AND router_version = ?
                """,
                (trade_date, router_version),
            ).fetchall()
        ]
        metrics["processed_signature_without_success_count"] = sum(
            1
            for row in runtime_rows
            if not row.get("last_success_at")
            and any(row.get(key) for key in ("processed_entry_signature", "processed_context_id", "processed_candle_at", "processed_theme_id", "processed_lease_signature"))
        )
        metrics["never_evaluated_count"] = sum(1 for row in runtime_rows if not (row.get("last_success_at") or row.get("last_evaluated_at")))
        if metrics["processed_signature_without_success_count"] > 0:
            failures.append("PROCESSED_SIGNATURE_WITHOUT_SUCCESS")
    return {"metrics": metrics, "failures": failures, "warnings": warnings}


def _terminal_integrity_metrics(rows: list[dict[str, Any]]) -> dict[str, int]:
    terminal = {"MATCHED", "INVALIDATED", "EXPIRED"}
    active = {"SEEKING", "FORMING"}
    metrics = {
        "terminal_revival_same_generation_count": 0,
        "matched_to_forming_same_generation_count": 0,
        "matched_to_seeking_same_generation_count": 0,
        "invalidated_to_active_same_generation_count": 0,
        "expired_to_active_same_generation_count": 0,
        "generation_jump_count": 0,
        "repeated_first_pullback_match_count": 0,
        "same_reference_breakout_new_generation_count": 0,
        "vwap_anchor_changed_same_generation_count": 0,
        "duplicate_material_transition_count": 0,
        "material_hash_state_conflict_count": 0,
    }
    previous_generation: dict[tuple[str, str, str], int] = {}
    lfp_matches: Counter[tuple[str, str, int]] = Counter()
    breakout_refs_by_generation: dict[tuple[str, str, float], set[int]] = defaultdict(set)
    vwap_anchor_by_generation: dict[tuple[str, str, int], str] = {}
    material_states: dict[str, set[str]] = defaultdict(set)
    material_counts: Counter[str] = Counter()
    for row in rows:
        previous_state = str(row.get("previous_state") or "").upper()
        current_state = str(row.get("current_state") or "").upper()
        generation = int(row.get("setup_generation") or 0)
        setup_type = str(row.get("setup_type") or "")
        candidate = str(row.get("candidate_instance_id") or "")
        theme = str(row.get("theme_id") or "")
        key = (candidate, theme, setup_type)
        if previous_state in terminal and current_state not in terminal:
            metrics["terminal_revival_same_generation_count"] += 1
        if previous_state == "MATCHED" and current_state == "FORMING":
            metrics["matched_to_forming_same_generation_count"] += 1
        if previous_state == "MATCHED" and current_state == "SEEKING":
            metrics["matched_to_seeking_same_generation_count"] += 1
        if previous_state == "INVALIDATED" and current_state in active | {"MATCHED"}:
            metrics["invalidated_to_active_same_generation_count"] += 1
        if previous_state == "EXPIRED" and current_state in active | {"MATCHED"}:
            metrics["expired_to_active_same_generation_count"] += 1
        old_generation = previous_generation.get(key)
        if old_generation is not None and generation - old_generation > 1:
            metrics["generation_jump_count"] += 1
        previous_generation[key] = max(generation, previous_generation.get(key, 0))
        payload = dict(row.get("state_payload") or {})
        if setup_type == "LEADER_FIRST_PULLBACK" and current_state == "MATCHED":
            lfp_matches[(candidate, theme, generation)] += 1
        if setup_type == "BREAKOUT_RETEST" and generation > 0:
            ref = round(_float(row.get("breakout_reference_price") or payload.get("breakout_reference_price") or payload.get("breakout_level")), 4)
            if ref > 0:
                breakout_refs_by_generation[(candidate, theme, ref)].add(generation)
        if setup_type == "VWAP_RECLAIM" and generation > 0:
            anchor = str(payload.get("below_candle_at") or "")
            anchor_key = (candidate, theme, generation)
            old_anchor = vwap_anchor_by_generation.get(anchor_key)
            if old_anchor and anchor and old_anchor != anchor:
                metrics["vwap_anchor_changed_same_generation_count"] += 1
            if anchor:
                vwap_anchor_by_generation.setdefault(anchor_key, anchor)
        material = str(row.get("material_state_fingerprint_to") or "")
        if material:
            material_counts[material] += 1
            material_states[material].add(current_state)
    metrics["repeated_first_pullback_match_count"] = sum(1 for count in lfp_matches.values() if count > 1)
    metrics["same_reference_breakout_new_generation_count"] = sum(1 for generations in breakout_refs_by_generation.values() if len(generations) > 1)
    metrics["duplicate_material_transition_count"] = sum(1 for count in material_counts.values() if count > 1)
    metrics["material_hash_state_conflict_count"] = sum(1 for states in material_states.values() if len(states) > 1)
    return metrics


def _run_span_minutes(runs: list[dict[str, Any]]) -> float:
    times = []
    for row in runs:
        parsed = _parse_time(row.get("calculated_at"))
        if parsed is not None:
            times.append(parsed)
    if len(times) < 2:
        return 0.0
    return round((max(times) - min(times)).total_seconds() / 60.0, 3)


def _span_metrics(con: sqlite3.Connection, trade_date: str, router_version: str) -> dict[str, Any]:
    run_first = run_last = ""
    obs_first = obs_last = ""
    if _has_table(con, "setup_router_runs"):
        row = con.execute(
            """
            SELECT MIN(calculated_at) AS first_at, MAX(calculated_at) AS last_at
            FROM setup_router_runs
            WHERE trade_date = ? AND router_version = ?
            """,
            (trade_date, router_version),
        ).fetchone()
        if row:
            run_first = str(row["first_at"] or "")
            run_last = str(row["last_at"] or "")
    if _has_table(con, "setup_observations_latest_v2"):
        row = con.execute(
            """
            SELECT MIN(calculated_at) AS first_at, MAX(calculated_at) AS last_at
            FROM setup_observations_latest_v2
            WHERE trade_date = ? AND router_version = ?
            """,
            (trade_date, router_version),
        ).fetchone()
        if row:
            obs_first = str(row["first_at"] or "")
            obs_last = str(row["last_at"] or "")
    return {
        "first_run_at": run_first,
        "last_run_at": run_last,
        "run_span_minutes": _span_between(run_first, run_last),
        "first_observation_at": obs_first,
        "last_observation_at": obs_last,
        "observation_span_minutes": _span_between(obs_first, obs_last),
    }


def _span_between(first: str, last: str) -> float:
    start = _parse_time(first)
    end = _parse_time(last)
    if start is None or end is None:
        return 0.0
    return round(max(0.0, (end - start).total_seconds() / 60.0), 3)


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _markdown(summary: dict[str, Any], flip: dict[str, Any], alignment: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# SetupRouter V3 Audit",
            "",
            f"- verdict: `{summary['verdict']}`",
            f"- observation_count: {summary['observation_count']}",
            f"- transition_count: {summary['transition_count']}",
            f"- invalid_count: {summary['invalid_count']}",
            f"- frequent_flip_count: {flip['frequent_flip_count']}",
            f"- valid_context_mismatch_count: {alignment['mismatch_count']}",
            "",
            "## Safety",
            "",
            "READY, promotion, opportunity rank, order intent, live order flags must remain false.",
            "",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
