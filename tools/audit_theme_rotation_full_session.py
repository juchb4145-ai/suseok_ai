from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> int:
    args = _parse_args()
    trade_date = args.trade_date or datetime.now().date().isoformat()
    output_dir = Path(args.output_dir or Path("reports") / "theme_rotation" / trade_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = _open_readonly(args.db_path)
    try:
        data = _collect(conn, trade_date=trade_date, limit=args.limit)
    finally:
        conn.close()
    verdict = _verdict(data)
    _write_outputs(output_dir, trade_date=trade_date, data=data, verdict=verdict)
    print(json.dumps({"trade_date": trade_date, "verdict": verdict["verdict"], "status": verdict["status"], "output_dir": str(output_dir)}, ensure_ascii=False))
    return {"PASS": 0, "WARN": 1, "FAIL": 2}.get(verdict["status"], 1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Theme Rotation P2-Min observe-only qualification from SQLite snapshots.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=500)
    return parser.parse_args()


def _open_readonly(path: str) -> sqlite3.Connection:
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _collect(conn: sqlite3.Connection, *, trade_date: str, limit: int) -> dict[str, Any]:
    theme_states = _rows(conn, "theme_state_transitions", trade_date, "occurred_at", limit)
    leadership = _rows(conn, "theme_leadership_transitions", trade_date, "detected_at", limit)
    leases = _rows(conn, "theme_expansion_leases", trade_date, "updated_at", limit)
    contexts = _rows(conn, "strategy_context_snapshots", trade_date, "calculated_at", limit)
    expansion_decisions = _rows(conn, "theme_expansion_subscription_decisions", trade_date, "calculated_at", limit)
    entry_plans = _count(conn, "entry_plans", "trade_date", trade_date)
    order_intents = _count(conn, "runtime_order_intents", "trade_date", trade_date)
    virtual_buys = _virtual_buy_count(conn, trade_date)
    send_orders = _gateway_command_count(conn, "send_order", trade_date)
    cancel_orders = _gateway_command_count(conn, "cancel_order", trade_date)
    best_theme_changes = _best_theme_changes(contexts)
    return {
        "theme_states": theme_states,
        "leadership_transitions": leadership,
        "expansion_leases": leases,
        "strategy_contexts": contexts,
        "expansion_decisions": expansion_decisions,
        "best_theme_changes": best_theme_changes,
        "metrics": {
            "theme_state_transition_count": len(theme_states),
            "leadership_transition_count": len(leadership),
            "lease_count": len(leases),
            "subscription_churn_count": len(expansion_decisions),
            "context_count": len(contexts),
            "best_theme_change_count": len(best_theme_changes),
            "state_leadership_mismatch_count": _mismatch_count(contexts),
            "entry_plan_count": entry_plans,
            "runtime_order_intent_count": order_intents,
            "virtual_buy_order_count": virtual_buys,
            "send_order_count": send_orders,
            "cancel_order_count": cancel_orders,
        },
    }


def _rows(conn: sqlite3.Connection, table: str, trade_date: str, order_col: str, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    if table == "theme_expansion_leases":
        sql = f"SELECT * FROM {table} WHERE trade_date = ? ORDER BY {order_col} DESC LIMIT ?"
        params: tuple[Any, ...] = (trade_date, max(1, int(limit)))
    else:
        sql = f"SELECT * FROM {table} WHERE trade_date = ? ORDER BY {order_col} DESC, id DESC LIMIT ?"
        params = (trade_date, max(1, int(limit)))
    return [_decode_row(row) for row in conn.execute(sql, params).fetchall()]


def _count(conn: sqlite3.Connection, table: str, date_col: str, trade_date: str, *, where_extra: str = "") -> int:
    if not _table_exists(conn, table):
        return 0
    columns = _columns(conn, table)
    selected_date_col = date_col if date_col in columns else _first_existing(
        columns,
        ("created_at", "updated_at", "submitted_at", "filled_at", "calculated_at"),
    )
    if not selected_date_col:
        return 0
    clauses = [f"{selected_date_col} LIKE ?"]
    params: list[Any] = [f"{trade_date}%"]
    if where_extra:
        clauses.append(where_extra)
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {' AND '.join(clauses)}", tuple(params)).fetchone()
    return int(row["count"] or 0)


def _virtual_buy_count(conn: sqlite3.Connection, trade_date: str) -> int:
    if not _table_exists(conn, "virtual_orders"):
        return 0
    columns = _columns(conn, "virtual_orders")
    date_col = _first_existing(columns, ("created_at", "submitted_at", "filled_at", "cancelled_at"))
    if not date_col:
        return 0
    clauses = [f"{date_col} LIKE ?"]
    params: list[Any] = [f"{trade_date}%"]
    side_col = _first_existing(columns, ("side", "order_side"))
    if side_col:
        clauses.append(f"{side_col} = ?")
        params.append("buy")
    row = conn.execute(f"SELECT COUNT(*) AS count FROM virtual_orders WHERE {' AND '.join(clauses)}", tuple(params)).fetchone()
    return int(row["count"] or 0)


def _gateway_command_count(conn: sqlite3.Connection, command_type: str, trade_date: str) -> int:
    if not _table_exists(conn, "gateway_commands"):
        return 0
    columns = _columns(conn, "gateway_commands")
    date_col = "created_at" if "created_at" in columns else "updated_at" if "updated_at" in columns else ""
    if not date_col or "command_type" not in columns:
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM gateway_commands WHERE command_type = ? AND {date_col} LIKE ?",
        (command_type, f"{trade_date}%"),
    ).fetchone()
    return int(row["count"] or 0)


def _best_theme_changes(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(contexts, key=lambda row: (str(row.get("code") or ""), str(row.get("calculated_at") or ""), int(row.get("id") or 0)))
    previous_by_code: dict[str, str] = {}
    changes: list[dict[str, Any]] = []
    for row in ordered:
        payload = dict(row.get("payload") or row)
        code = str(payload.get("code") or row.get("code") or "")
        selected = str(payload.get("selected_theme_id") or dict(payload.get("theme") or {}).get("theme_id") or "")
        previous = previous_by_code.get(code, "")
        if code and selected and previous and previous != selected:
            changes.append({"code": code, "calculated_at": payload.get("calculated_at", ""), "previous_selected_theme_id": previous, "selected_theme_id": selected, "context_id": payload.get("context_id", "")})
        if code and selected:
            previous_by_code[code] = selected
    return changes


def _mismatch_count(contexts: list[dict[str, Any]]) -> int:
    count = 0
    for row in contexts:
        payload = dict(row.get("payload") or row)
        theme = dict(payload.get("theme") or {})
        if theme.get("state_leadership_consistent") is False:
            count += 1
    return count


def _verdict(data: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(data.get("metrics") or {})
    failures = []
    warnings = []
    for key in ("entry_plan_count", "runtime_order_intent_count", "virtual_buy_order_count", "send_order_count", "cancel_order_count"):
        if int(metrics.get(key) or 0) > 0:
            failures.append(f"{key.upper()}_NONZERO")
    if int(metrics.get("leadership_transition_count") or 0) == 0:
        warnings.append("NO_LEADERSHIP_TRANSITION_OBSERVED")
    if int(metrics.get("context_count") or 0) == 0:
        warnings.append("NO_STRATEGY_CONTEXT_SAMPLES")
    if int(metrics.get("state_leadership_mismatch_count") or 0) > 0:
        warnings.append("STATE_LEADERSHIP_MISMATCH_OBSERVED")
    status = "FAIL" if failures else "WARN" if warnings else "PASS"
    final = "NOT_STABLE" if failures else "CONDITIONALLY_STABLE" if warnings else "STABLE_FOR_SETUP_ROUTER"
    return {"status": status, "verdict": final, "failures": failures, "warnings": warnings, "metrics": metrics}


def _write_outputs(output_dir: Path, *, trade_date: str, data: dict[str, Any], verdict: dict[str, Any]) -> None:
    files = {
        "theme_state_transitions.json": data["theme_states"],
        "leadership_transitions.json": data["leadership_transitions"],
        "expansion_leases.json": data["expansion_leases"],
        "best_theme_changes.json": data["best_theme_changes"],
        "candidate_context_changes.json": data["strategy_contexts"],
        "audit_summary.json": {"trade_date": trade_date, **verdict},
    }
    for name, payload in files.items():
        (output_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "runtime_samples.ndjson").write_text("", encoding="utf-8")
    (output_dir / "theme_samples.ndjson").write_text("", encoding="utf-8")
    report = [
        f"# Theme Rotation Full Session Audit",
        "",
        f"- trade_date: {trade_date}",
        f"- status: {verdict['status']}",
        f"- verdict: {verdict['verdict']}",
        f"- failures: {', '.join(verdict['failures']) or '-'}",
        f"- warnings: {', '.join(verdict['warnings']) or '-'}",
        "",
        "## Metrics",
    ]
    for key, value in sorted(verdict["metrics"].items()):
        report.append(f"- {key}: {value}")
    (output_dir / "audit_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in list(data.keys()):
        if key.endswith("_json"):
            decoded = _json_loads(data.get(key), [] if key.endswith("codes_json") else {})
            data[key[:-5] if key != "payload_json" else "payload"] = decoded
    return data


def _json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
