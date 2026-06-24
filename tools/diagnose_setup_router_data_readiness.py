from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_ROUTER_VERSION = "setup_router_v3.5"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose SetupRouter V3 data-readiness gating.")
    parser.add_argument("--db", default="data/trader.sqlite3")
    parser.add_argument("--trade-date", default=datetime.now().date().isoformat())
    parser.add_argument("--router-version", default=DEFAULT_ROUTER_VERSION)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    trade_date = str(args.trade_date)
    output_dir = Path(args.output_dir or Path("reports") / "setup_router_readiness" / trade_date)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        summary = {
            "schema_version": "setup_router_data_readiness.diagnostic.v1",
            "trade_date": trade_date,
            "router_version": args.router_version,
            "db_path": str(db_path),
            "generated_at": _now(),
            "status": "DB_NOT_FOUND",
        }
        _write_json(output_dir / "readiness_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        readiness = _readiness_rows(con, trade_date, args.router_version, args.limit)
        observations = _observation_rows(con, trade_date, args.router_version, args.limit)
        codes = _codes(readiness, observations)
        leases = _lease_rows(con, trade_date, codes)
        ticks = _latest_ticks(con, trade_date, codes)
        candidates = _candidate_rows(con, trade_date, codes)
    finally:
        con.close()

    reason_counts = _reason_counts(readiness, observations)
    freshness_chain = _freshness_chain(readiness, ticks)
    subscription_mismatch = _subscription_mismatch(readiness, leases, ticks)
    candidate_readiness = [_compact_readiness(row, ticks.get(str(row.get("code") or "")), candidates.get(str(row.get("code") or ""))) for row in readiness]
    summary = _summary(
        trade_date=trade_date,
        router_version=str(args.router_version),
        db_path=db_path,
        readiness=readiness,
        observations=observations,
        leases=leases,
        ticks=ticks,
        reason_counts=reason_counts,
        mismatches=subscription_mismatch,
    )

    _write_json(output_dir / "readiness_summary.json", summary)
    _write_json(output_dir / "candidate_readiness.json", candidate_readiness)
    _write_json(output_dir / "subscription_mismatch.json", subscription_mismatch)
    _write_json(output_dir / "freshness_chain.json", freshness_chain)
    _write_json(output_dir / "reason_counts.json", reason_counts)
    (output_dir / "report.md").write_text(_markdown(summary, subscription_mismatch, candidate_readiness), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["failure_count"] else 0


def _readiness_rows(con: sqlite3.Connection, trade_date: str, router_version: str, limit: int) -> list[dict[str, Any]]:
    if not _has_table(con, "setup_router_readiness_latest"):
        return []
    clauses = ["trade_date = ?"]
    params: list[Any] = [trade_date]
    if _has_column(con, "setup_router_readiness_latest", "router_version"):
        clauses.append("router_version = ?")
        params.append(router_version)
    rows = con.execute(
        f"""
        SELECT *
        FROM setup_router_readiness_latest
        WHERE {" AND ".join(clauses)}
        ORDER BY readiness_ready ASC, calculated_at DESC, code ASC
        LIMIT ?
        """,
        (*params, max(1, int(limit or 1000))),
    ).fetchall()
    return [_row(row) for row in rows]


def _observation_rows(con: sqlite3.Connection, trade_date: str, router_version: str, limit: int) -> list[dict[str, Any]]:
    if not _has_table(con, "setup_observations_latest_v2"):
        return []
    clauses = ["trade_date = ?"]
    params: list[Any] = [trade_date]
    if _has_column(con, "setup_observations_latest_v2", "router_version"):
        clauses.append("router_version = ?")
        params.append(router_version)
    rows = con.execute(
        f"""
        SELECT *
        FROM setup_observations_latest_v2
        WHERE {" AND ".join(clauses)}
        ORDER BY calculated_at DESC, code ASC
        LIMIT ?
        """,
        (*params, max(1, int(limit or 1000))),
    ).fetchall()
    return [_row(row) for row in rows]


def _lease_rows(con: sqlite3.Connection, trade_date: str, codes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    if not _has_table(con, "theme_expansion_leases"):
        return {}
    result: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    order_cols = ["selected_at DESC"]
    if _has_column(con, "theme_expansion_leases", "id"):
        order_cols.append("id DESC")
    elif _has_column(con, "theme_expansion_leases", "created_at"):
        order_cols.append("created_at DESC")
    for code in codes:
        rows = con.execute(
            f"""
            SELECT *
            FROM theme_expansion_leases
            WHERE trade_date = ? AND code = ?
            ORDER BY {", ".join(order_cols)}
            LIMIT 20
            """,
            (trade_date, code),
        ).fetchall()
        result[code] = [_row(row) for row in rows]
    return result


def _latest_ticks(con: sqlite3.Connection, trade_date: str, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    if not _has_table(con, "gateway_price_ticks"):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for code in codes:
        row = con.execute(
            """
            SELECT *
            FROM gateway_price_ticks
            WHERE trade_date = ? AND code = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (trade_date, code),
        ).fetchone()
        if row:
            result[code] = _row(row)
    return result


def _candidate_rows(con: sqlite3.Connection, trade_date: str, codes: Iterable[str]) -> dict[str, dict[str, Any]]:
    if not _has_table(con, "candidates"):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for code in codes:
        row = con.execute(
            """
            SELECT *
            FROM candidates
            WHERE trade_date = ? AND code = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (trade_date, code),
        ).fetchone()
        if row:
            result[code] = _row(row)
    return result


def _codes(*row_sets: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for rows in row_sets:
        for row in rows:
            code = str(row.get("code") or "")
            if code and code not in seen:
                seen.add(code)
                result.append(code)
    return result


def _reason_counts(readiness: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, Any]:
    readiness_reasons: Counter[str] = Counter()
    observation_reasons: Counter[str] = Counter()
    informational: Counter[str] = Counter()
    for row in readiness:
        readiness_reasons.update(str(reason) for reason in row.get("reason_codes", []) if str(reason))
        informational.update(str(reason) for reason in row.get("informational_reason_codes", []) if str(reason))
    for row in observations:
        observation_reasons.update(str(reason) for reason in row.get("reason_codes", []) if str(reason))
    return {
        "readiness_reason_counts": dict(readiness_reasons.most_common()),
        "readiness_informational_reason_counts": dict(informational.most_common()),
        "observation_reason_counts": dict(observation_reasons.most_common()),
    }


def _freshness_chain(readiness: list[dict[str, Any]], ticks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in readiness:
        code = str(row.get("code") or "")
        tick = ticks.get(code) or {}
        rows.append(
            {
                "code": code,
                "name": row.get("name", ""),
                "readiness_status": row.get("readiness_status", ""),
                "readiness_ready": bool(row.get("readiness_ready")),
                "subscription_active_since": row.get("subscription_active_since", ""),
                "relevant_source_added_at": row.get("relevant_source_added_at", ""),
                "baseline_at": row.get("baseline_at", ""),
                "readiness_latest_tick_at": row.get("latest_tick_at", ""),
                "readiness_latest_tick_age_sec": row.get("latest_tick_age_sec", 0),
                "gateway_latest_tick_at": tick.get("timestamp", ""),
                "gateway_latest_received_at": tick.get("received_at", ""),
                "gateway_tick_source": tick.get("source", ""),
                "post_subscription_tick_verified": bool(row.get("post_subscription_tick_verified")),
                "reason_codes": row.get("reason_codes", []),
            }
        )
    return rows


def _subscription_mismatch(readiness: list[dict[str, Any]], leases: dict[str, list[dict[str, Any]]], ticks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for row in readiness:
        code = str(row.get("code") or "")
        reasons = [str(reason) for reason in row.get("reason_codes", [])]
        readiness_status = str(row.get("readiness_status") or "")
        subscription_active = bool(row.get("subscription_active"))
        fresh = bool(row.get("post_subscription_tick_verified"))
        lease_required = bool(row.get("expansion_lease_required"))
        if readiness_status == "WAIT_SELECTED_THEME_LEASE" and not lease_required:
            mismatches.append(_mismatch(row, "LEASE_FALSE_BLOCK", leases.get(code), ticks.get(code)))
        if "SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING" in reasons and not lease_required:
            mismatches.append(_mismatch(row, "LEASE_REASON_WITHOUT_LEASE_REQUIREMENT", leases.get(code), ticks.get(code)))
        if subscription_active and not fresh and ticks.get(code):
            mismatches.append(_mismatch(row, "ACTIVE_SUBSCRIPTION_WITH_GATEWAY_TICK_BUT_NOT_FRESH", leases.get(code), ticks.get(code)))
        if subscription_active and fresh and not bool(row.get("readiness_ready")) and readiness_status not in {
            "WAIT_SELECTED_THEME_LEASE",
            "WAIT_STRATEGY_CONTEXT",
            "WAIT_MARKET_CONTEXT",
            "WAIT_THEME_SIGNAL_STALE",
            "WAIT_CANDLE_WARMUP",
        }:
            mismatches.append(_mismatch(row, "ACTIVE_FRESH_BUT_UNEXPECTED_WAIT", leases.get(code), ticks.get(code)))
    return mismatches


def _mismatch(row: dict[str, Any], kind: str, leases: list[dict[str, Any]] | None, tick: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "code": row.get("code", ""),
        "name": row.get("name", ""),
        "selected_theme_id": row.get("selected_theme_id", ""),
        "readiness_status": row.get("readiness_status", ""),
        "reason_codes": row.get("reason_codes", []),
        "subscription_active": bool(row.get("subscription_active")),
        "post_subscription_tick_verified": bool(row.get("post_subscription_tick_verified")),
        "expansion_lease_required": bool(row.get("expansion_lease_required")),
        "exact_theme_lease_active": bool(row.get("exact_theme_lease_active")),
        "lease_rows": leases or [],
        "latest_gateway_tick": tick or {},
    }


def _compact_readiness(row: dict[str, Any], tick: dict[str, Any] | None, candidate: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "code": row.get("code", ""),
        "name": row.get("name", ""),
        "candidate_instance_id": row.get("candidate_instance_id", ""),
        "candidate_state": (candidate or {}).get("state", ""),
        "selected_theme_id": row.get("selected_theme_id", ""),
        "selected_theme_name": row.get("selected_theme_name", ""),
        "readiness_status": row.get("readiness_status", ""),
        "readiness_ready": bool(row.get("readiness_ready")),
        "coverage_type": row.get("coverage_type", ""),
        "subscription_active": bool(row.get("subscription_active")),
        "subscription_sources": row.get("subscription_sources", []),
        "subscription_budget_deferred": bool(row.get("subscription_budget_deferred")),
        "post_subscription_tick_verified": bool(row.get("post_subscription_tick_verified")),
        "latest_tick_at": row.get("latest_tick_at", ""),
        "gateway_latest_tick_at": (tick or {}).get("timestamp", ""),
        "baseline_at": row.get("baseline_at", ""),
        "reason_codes": row.get("reason_codes", []),
        "informational_reason_codes": row.get("informational_reason_codes", []),
    }


def _summary(
    *,
    trade_date: str,
    router_version: str,
    db_path: Path,
    readiness: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    leases: dict[str, list[dict[str, Any]]],
    ticks: dict[str, dict[str, Any]],
    reason_counts: dict[str, Any],
    mismatches: list[dict[str, Any]],
) -> dict[str, Any]:
    readiness_status_counts = Counter(str(row.get("readiness_status") or "UNKNOWN") for row in readiness)
    observation_status_counts = Counter(str(row.get("router_status") or "UNKNOWN") for row in observations)
    active = [row for row in readiness if bool(row.get("subscription_active"))]
    fresh = [row for row in readiness if bool(row.get("post_subscription_tick_verified"))]
    ready = [row for row in readiness if bool(row.get("readiness_ready"))]
    shape_candidates = {str(row.get("candidate_instance_id") or "") for row in observations if str(row.get("candidate_instance_id") or "")}
    lease_false_blocks = [row for row in mismatches if str(row.get("kind") or "").startswith("LEASE_")]
    return {
        "schema_version": "setup_router_data_readiness.diagnostic.v1",
        "trade_date": trade_date,
        "router_version": router_version,
        "db_path": str(db_path),
        "generated_at": _now(),
        "status": "OK" if readiness else "NO_READINESS_ROWS",
        "failure_count": len(lease_false_blocks),
        "readiness_count": len(readiness),
        "readiness_ready_count": len(ready),
        "readiness_wait_count": len(readiness) - len(ready),
        "observation_count": len(observations),
        "valid_observe_count": observation_status_counts.get("VALID_OBSERVE", 0),
        "shape_evaluated_candidate_count": len(shape_candidates),
        "lease_row_count": sum(len(items) for items in leases.values()),
        "latest_gateway_tick_code_count": len(ticks),
        "funnel": {
            "evaluation_eligible": len(readiness),
            "subscription_active": len(active),
            "fresh_tick": len(fresh),
            "readiness_ready": len(ready),
            "shape_evaluated": len(shape_candidates),
            "valid_observe": observation_status_counts.get("VALID_OBSERVE", 0),
        },
        "readiness_status_counts": dict(readiness_status_counts),
        "observation_status_counts": dict(observation_status_counts),
        "top_readiness_reasons": dict(Counter(reason_counts.get("readiness_reason_counts", {})).most_common(10)),
        "top_observation_reasons": dict(Counter(reason_counts.get("observation_reason_counts", {})).most_common(10)),
        "mismatch_count": len(mismatches),
        "lease_false_block_count": len(lease_false_blocks),
        "active_subscription_with_gateway_tick_but_not_fresh_count": sum(
            1 for row in mismatches if row.get("kind") == "ACTIVE_SUBSCRIPTION_WITH_GATEWAY_TICK_BUT_NOT_FRESH"
        ),
    }


def _markdown(summary: dict[str, Any], mismatches: list[dict[str, Any]], candidate_readiness: list[dict[str, Any]]) -> str:
    lines = [
        "# SetupRouter Data Readiness Diagnostic",
        "",
        f"- status: `{summary['status']}`",
        f"- trade_date: `{summary['trade_date']}`",
        f"- readiness_count: {summary['readiness_count']}",
        f"- readiness_ready_count: {summary['readiness_ready_count']}",
        f"- shape_evaluated_candidate_count: {summary['shape_evaluated_candidate_count']}",
        f"- lease_false_block_count: {summary['lease_false_block_count']}",
        f"- mismatch_count: {summary['mismatch_count']}",
        "",
        "## Funnel",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in dict(summary.get("funnel") or {}).items())
    lines.extend(["", "## Top Wait Rows", ""])
    for row in candidate_readiness[:20]:
        lines.append(
            f"- `{row['code']}` {row['name']} / {row['readiness_status']} / "
            f"sub={row['subscription_active']} fresh={row['post_subscription_tick_verified']} / "
            f"{','.join(row['reason_codes'])}"
        )
    if mismatches:
        lines.extend(["", "## Mismatches", ""])
        for row in mismatches[:20]:
            lines.append(f"- `{row['kind']}` `{row['code']}` {row.get('name', '')}: {','.join(row.get('reason_codes') or [])}")
    return "\n".join(lines) + "\n"


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in list(data):
        if key.endswith("_json"):
            target = key[:-5]
            data[target] = _json(data.get(key), [] if key.endswith("codes_json") or key.endswith("sources_json") else {})
    payload = data.get("payload")
    if isinstance(payload, dict):
        merged = dict(payload)
        merged.update({key: value for key, value in data.items() if key not in merged})
        return merged
    return data


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return row is not None


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(con, table):
        return False
    return column in {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
