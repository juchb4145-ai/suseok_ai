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
    parser.add_argument("--router-version", default="setup_router_v3.2")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    trade_date = str(args.trade_date)
    output_dir = Path(args.output_dir or Path("reports") / "setup_router_v3" / trade_date)
    output_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        observations = _rows(con, "setup_observations_latest", trade_date, limit=args.limit, router_version=args.router_version)
        transitions = _rows(con, "setup_observation_transitions", trade_date, limit=args.limit, router_version=args.router_version)
        states = _rows(con, "setup_router_state_v2", trade_date, limit=args.limit, router_version=args.router_version)
        state_transitions = _rows(con, "setup_router_state_transitions_v2", trade_date, limit=args.limit)
        runs = _rows(con, "setup_router_runs", trade_date, limit=1000, router_version=args.router_version)
        side_effects = _side_effect_counts(con, trade_date)
    finally:
        con.close()

    type_counts = Counter(row.get("setup_type", "UNKNOWN") for row in observations)
    status_counts = Counter(row.get("router_status", "UNKNOWN") for row in observations)
    shape_counts = Counter(row.get("shape_status", "UNKNOWN") for row in observations)
    context_counts = Counter(row.get("context_status", "UNKNOWN") for row in observations)
    invalid = _invalid_observations(observations)
    flip_analysis = _flip_analysis(transitions)
    context_alignment = _context_alignment(observations)
    run_span_min = _run_span_minutes(runs)
    failures: list[str] = []
    warnings: list[str] = []
    if not _has_table_rows(observations) and not runs:
        warnings.append("NO_SETUP_ROUTER_V3_ROWS")
    if invalid:
        failures.append("INVALID_SETUP_ROUTER_V3_OBSERVATIONS")
    if any(row.get("current_router_status") == "VALID_OBSERVE" and row.get("current_context_status") == "BLOCKED" for row in transitions):
        failures.append("VALID_OBSERVE_WITH_BLOCKED_CONTEXT_TRANSITION")
    if any(value > 0 for value in side_effects.values()):
        failures.append("SETUP_ROUTER_ORDER_SIDE_EFFECTS_PRESENT")
    if status_counts.get("VALID_OBSERVE", 0) == 0:
        warnings.append("NO_VALID_SETUP_SAMPLE")
    if status_counts.get("VALID_OBSERVE", 0) == 1:
        warnings.append("SINGLE_VALID_SETUP_SAMPLE")
    if run_span_min < 60:
        warnings.append("RUN_SPAN_LT_60_MIN")
    if observations and not states:
        failures.append("TEMPORAL_STATE_ROWS_MISSING")
    if any(row.get("router_status") == "VALID_OBSERVE" and not bool(row.get("post_subscription_tick_verified", True)) for row in observations):
        failures.append("VALID_WITH_POST_SUBSCRIPTION_TICK_MISSING")
    if any(row.get("router_status") == "VALID_OBSERVE" and row.get("session_phase") in {"CLOSING_RISK", "MARKET_CLOSED"} for row in observations):
        failures.append("VALID_IN_CLOSING_OR_CLOSED_SESSION")
    if any(row.get("router_status") == "VALID_OBSERVE" and row.get("context_status") != "ELIGIBLE" for row in observations):
        failures.append("VALID_CONTEXT_NOT_ELIGIBLE")
    stable_ready = (
        not failures
        and not warnings
        and status_counts.get("VALID_OBSERVE", 0) > 0
        and run_span_min >= 60
        and len(states) > 0
        and len(state_transitions) > 0
    )
    verdict = "STABLE_FOR_OPPORTUNITY_RANKER" if stable_ready else "NOT_STABLE" if failures else "CONDITIONALLY_STABLE"
    summary = {
        "schema_version": "setup_router_v3.audit.v2",
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
        "run_span_minutes": run_span_min,
        "type_counts": dict(type_counts),
        "status_counts": dict(status_counts),
        "shape_counts": dict(shape_counts),
        "context_counts": dict(context_counts),
        "invalid_count": len(invalid),
        "failures": failures,
        "warnings": warnings,
        "side_effect_counts": side_effects,
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
    _write_json(output_dir / "invalid_observations.json", invalid[:1000])
    _write_json(output_dir / "flip_analysis.json", flip_analysis)
    _write_json(output_dir / "context_alignment.json", context_alignment)
    (output_dir / "report.md").write_text(_markdown(summary, flip_analysis, context_alignment), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if failures else 0


def _rows(con: sqlite3.Connection, table: str, trade_date: str, *, limit: int, router_version: str | None = None) -> list[dict[str, Any]]:
    if not _has_table(con, table):
        return []
    clauses = ["trade_date = ?"]
    params: list[Any] = [trade_date]
    if router_version and _has_column(con, table, "router_version"):
        clauses.append("(router_version = ? OR router_version = '')")
        params.append(router_version)
    order_col = "rowid"
    if table == "setup_router_state_transitions_v2":
        order_col = "occurred_at"
    elif table == "setup_router_state_v2":
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
        "runtime_order_intents": "trade_date",
        "managed_order_intents": "trade_date",
        "virtual_orders": "trade_date",
        "virtual_positions": "trade_date",
        "gateway_commands": "trade_date",
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
    return result


def _run_span_minutes(runs: list[dict[str, Any]]) -> float:
    times = []
    for row in runs:
        parsed = _parse_time(row.get("calculated_at"))
        if parsed is not None:
            times.append(parsed)
    if len(times) < 2:
        return 0.0
    return round((max(times) - min(times)).total_seconds() / 60.0, 3)


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
