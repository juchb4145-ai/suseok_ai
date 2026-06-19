from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_CORE_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN = "local-dev-token"
DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_DURATION_SEC = 600.0


@dataclass(frozen=True)
class Issue:
    severity: str
    area: str
    code: str
    status: str
    evidence: dict[str, Any]
    recommendation: str = ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trade_date = args.trade_date or datetime.now().date().isoformat()
    output_dir = Path(args.output_dir or Path("reports") / "strategy_context_v3" / trade_date).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = utc_now()
    samples = collect_samples(
        core_url=args.core_url,
        token=args.token,
        output_dir=output_dir,
        interval_sec=max(1.0, args.interval_sec),
        duration_sec=max(0.0, args.duration_sec),
    )
    db_summary = analyze_db(Path(args.db_path), trade_date=trade_date, sample_started_at=started_at)
    command_history = samples.get("command_history") or fetch_json(
        args.core_url,
        "/api/gateway/commands/history?limit=100",
        args.token,
    )
    write_json(output_dir / "command_history.json", command_history)
    write_json(output_dir / "db_summary.json", db_summary)

    audit = build_audit(
        trade_date=trade_date,
        started_at=started_at,
        finished_at=utc_now(),
        core_url=args.core_url,
        samples=samples,
        db_summary=db_summary,
    )
    write_json(output_dir / "intraday_audit.json", audit)
    write_text(output_dir / "intraday_audit.md", render_markdown(audit))

    print(f"wrote {output_dir}")
    print(f"verdict={audit['verdict']} exit_code={audit['exit_code']}")
    for issue in audit["issues"]:
        print(f"{issue['severity']} {issue['area']} {issue['code']}: {issue['status']}")
    return int(audit["exit_code"])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Strategy Context V3 intraday audit")
    parser.add_argument("--core-url", default=os.getenv("TRADING_CORE_URL", DEFAULT_CORE_URL))
    parser.add_argument("--token", default=os.getenv("TRADING_CORE_TOKEN", DEFAULT_TOKEN))
    parser.add_argument("--db-path", default=os.getenv("TRADING_DB_PATH", "data/trader.sqlite3"))
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--interval-sec", type=float, default=DEFAULT_INTERVAL_SEC)
    parser.add_argument("--duration-sec", type=float, default=DEFAULT_DURATION_SEC)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args(argv)


def collect_samples(
    *,
    core_url: str,
    token: str,
    output_dir: Path,
    interval_sec: float,
    duration_sec: float,
) -> dict[str, Any]:
    started = time.monotonic()
    runtime_samples: list[dict[str, Any]] = []
    gateway_samples: list[dict[str, Any]] = []
    dashboard_samples: list[dict[str, Any]] = []
    status_samples: list[dict[str, Any]] = []
    endpoints_once = {
        "health": "/health",
        "readiness": "/api/runtime/readiness",
        "gateway_commands_status": "/api/gateway/commands/status",
        "snapshot_v2": "/api/snapshot?view=v2",
        "command_history": "/api/gateway/commands/history?limit=100",
    }
    once = {name: fetch_json(core_url, path, token) for name, path in endpoints_once.items()}
    while True:
        runtime = fetch_json(core_url, "/api/runtime/snapshot", token)
        gateway = fetch_json(core_url, "/api/gateway/status", token)
        dashboard = fetch_json(core_url, "/api/dashboard-v2/snapshot", token)
        status = fetch_json(core_url, "/api/runtime/status", token)
        runtime_samples.append(runtime)
        gateway_samples.append(gateway)
        dashboard_samples.append(dashboard)
        status_samples.append(status)
        append_ndjson(output_dir / "runtime_samples.ndjson", runtime)
        append_ndjson(output_dir / "gateway_samples.ndjson", gateway)
        append_ndjson(output_dir / "dashboard_v2_samples.ndjson", dashboard)
        append_ndjson(output_dir / "runtime_status_samples.ndjson", status)
        if time.monotonic() - started >= duration_sec:
            break
        time.sleep(interval_sec)
    return {
        **once,
        "runtime_samples": runtime_samples,
        "gateway_samples": gateway_samples,
        "dashboard_samples": dashboard_samples,
        "status_samples": status_samples,
        "sample_count": len(runtime_samples),
        "duration_sec": duration_sec,
        "interval_sec": interval_sec,
    }


def fetch_json(core_url: str, path: str, token: str) -> dict[str, Any]:
    url = core_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    fetched_at = utc_now()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else None
            return {"ok": True, "status_code": resp.status, "fetched_at": fetched_at, "data": data}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status_code": exc.code, "fetched_at": fetched_at, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "status_code": 0, "fetched_at": fetched_at, "error": str(exc)}


def analyze_db(db_path: Path, *, trade_date: str, sample_started_at: str) -> dict[str, Any]:
    path = db_path.resolve()
    uri = "file:" + urllib.parse.quote(path.as_posix(), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return _analyze_db_connection(con, trade_date=trade_date, sample_started_at=sample_started_at)
    finally:
        con.close()


def _analyze_db_connection(con: sqlite3.Connection, *, trade_date: str, sample_started_at: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "trade_date": trade_date,
        "sample_started_at": sample_started_at,
        "tables": {},
    }
    tables = table_names(con)
    out["tables"] = {name: row_count(con, name) for name in tables}
    out["safety"] = safety_counts(con, trade_date=trade_date, sample_started_at=sample_started_at)
    out["gateway"] = gateway_db_summary(con, trade_date=trade_date, sample_started_at=sample_started_at)
    out["opening_burst"] = opening_burst_summary(con, trade_date=trade_date)
    out["theme_board"] = theme_board_summary(con, trade_date=trade_date)
    out["market_regime"] = market_regime_summary(con, trade_date=trade_date)
    out["strategy_context"] = strategy_context_summary(con, trade_date=trade_date)
    out["candidate_bridge"] = candidate_bridge_summary(con, trade_date=trade_date)
    out["expansion"] = expansion_summary(con, trade_date=trade_date)
    out["dirty_entry"] = dirty_entry_summary(con, trade_date=trade_date)
    return out


def safety_counts(con: sqlite3.Connection, *, trade_date: str, sample_started_at: str) -> dict[str, Any]:
    return {
        "send_order_commands_today": scalar(
            con,
            "select count(*) from gateway_commands where command_type='send_order' and trade_date=?",
            (trade_date,),
        ),
        "cancel_order_commands_today": scalar(
            con,
            "select count(*) from gateway_commands where command_type='cancel_order' and trade_date=?",
            (trade_date,),
        ),
        "send_order_commands_since_sample": scalar(
            con,
            "select count(*) from gateway_commands where command_type='send_order' and created_at>=?",
            (sample_started_at,),
        ),
        "runtime_order_intents_today": count_by_date(con, "runtime_order_intents", trade_date),
        "managed_order_intents_today": count_by_date(con, "managed_order_intents", trade_date),
        "live_sim_orders_today": count_by_date(con, "live_sim_orders", trade_date),
        "entry_plans_today": count_by_date(con, "entry_plans", trade_date),
        "strategy_decisions_with_order_refs_today": scalar(
            con,
            """
            select count(*) from strategy_decision_events
            where trade_date=?
              and (order_intent_id is not null or entry_plan_id is not null or virtual_order_id is not null)
            """,
            (trade_date,),
        )
        if has_table(con, "strategy_decision_events")
        else 0,
        "legacy_virtual_orders_today": virtual_orders_today(con, trade_date),
    }


def gateway_db_summary(con: sqlite3.Connection, *, trade_date: str, sample_started_at: str) -> dict[str, Any]:
    return {
        "price_ticks_since_sample": scalar(
            con,
            "select count(*) from gateway_price_ticks where created_at>=?",
            (sample_started_at,),
        )
        if has_table(con, "gateway_price_ticks")
        else 0,
        "price_tick_distinct_codes_since_sample": scalar(
            con,
            "select count(distinct code) from gateway_price_ticks where created_at>=?",
            (sample_started_at,),
        )
        if has_table(con, "gateway_price_ticks")
        else 0,
        "latest_price_tick": rows(
            con,
            """
            select code, price, change_rate, trade_time, created_at, received_at
            from gateway_price_ticks order by id desc limit 1
            """,
        )[0]
        if has_table(con, "gateway_price_ticks") and row_count(con, "gateway_price_ticks")
        else {},
        "event_counts_since_sample": rows(
            con,
            """
            select event_type, processing_status, count(*) as count
            from gateway_event_log
            where created_at>=?
            group by event_type, processing_status
            order by count desc
            """,
            (sample_started_at,),
        )
        if has_table(con, "gateway_event_log")
        else [],
        "command_counts_today": rows(
            con,
            """
            select command_type, status, count(*) as count
            from gateway_commands
            where trade_date=?
            group by command_type, status
            order by command_type, status
            """,
            (trade_date,),
        )
        if has_table(con, "gateway_commands")
        else [],
    }


def opening_burst_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if has_table(con, "opening_turnover_seed_batches"):
        summary["latest_seed_batches"] = rows(
            con,
            """
            select id, created_at, trade_date, batch_time, command_id, row_count, parsed_count, parser_status,
                   parser_missing_fields_json
            from opening_turnover_seed_batches
            where trade_date=?
            order by id desc limit 5
            """,
            (trade_date,),
        )
    if has_table(con, "opening_turnover_seed_rows"):
        seed_rows = rows(
            con,
            """
            select stock_code, stock_name, rank, change_rate_pct, turnover_krw, current_price, volume, parser_status
            from opening_turnover_seed_rows
            where trade_date=?
            order by rank asc
            """,
            (trade_date,),
        )
        summary["seed_row_count"] = len(seed_rows)
        summary["seed_nonempty_code_count"] = len({r["stock_code"] for r in seed_rows if r.get("stock_code")})
        summary["seed_empty_code_count"] = sum(1 for r in seed_rows if not r.get("stock_code"))
        summary["seed_etf_like_count"] = sum(1 for r in seed_rows if is_etf_like(str(r.get("stock_name") or "")))
        summary["seed_top20"] = seed_rows[:20]
        if has_table(con, "theme_membership_current"):
            cols = table_columns(con, "theme_membership_current")
            code_col = "stock_code" if "stock_code" in cols else "code" if "code" in cols else ""
            if code_col:
                summary["seed_codes_with_membership"] = scalar(
                    con,
                    f"""
                    select count(distinct s.stock_code)
                    from opening_turnover_seed_rows s
                    join theme_membership_current m on m.{code_col}=s.stock_code
                    where s.trade_date=? and s.stock_code<>''
                    """,
                    (trade_date,),
                )
    if has_table(con, "opening_theme_burst_results"):
        latest = rows(
            con,
            """
            select id, created_at, trade_date, calculated_at, output_mode, ready_allowed, order_intent_allowed,
                   seed_batch_count, seed_symbol_count, realtime_registered_count, selected_symbols_json,
                   top_themes_json
            from opening_theme_burst_results
            where trade_date=?
            order by id desc limit 1
            """,
            (trade_date,),
        )
        if latest:
            item = dict(latest[0])
            item["selected_symbols"] = parse_json(item.pop("selected_symbols_json", "[]"), [])
            item["top_themes"] = parse_json(item.pop("top_themes_json", "[]"), [])[:5]
            summary["latest_result"] = item
    return summary


def theme_board_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    latest = latest_theme_board_row(con, trade_date)
    if not latest:
        return {"present": False}
    top_themes = parse_json(latest.get("top_themes_json"), [])
    stocks = parse_json(latest.get("stocks_json"), [])
    source_counts = parse_json(latest.get("source_counts_json"), {})
    reason_codes = parse_json(latest.get("reason_codes_json"), [])
    roles = Counter(str(s.get("stock_role") or "") for s in stocks if isinstance(s, Mapping))
    trade_roles = Counter(str(s.get("trade_role") or "") for s in stocks if isinstance(s, Mapping))
    entry_usable = sum(1 for s in stocks if isinstance(s, Mapping) and bool(s.get("entry_usable")))
    board_id = latest.get("id")
    return {
        "present": True,
        "latest": {
            key: latest.get(key)
            for key in (
                "id",
                "created_at",
                "trade_date",
                "calculated_at",
                "board_status",
                "theme_count",
                "active_theme_count",
                "watch_theme_count",
                "data_wait_theme_count",
            )
        },
        "top_themes": compact_top_themes(top_themes),
        "stock_count": len(stocks),
        "entry_usable_count": entry_usable,
        "role_counts": dict(roles),
        "trade_role_counts": dict(trade_roles),
        "source_counts": source_counts,
        "reason_codes": reason_codes,
        "latest_theme_status_counts": rows(
            con,
            """
            select theme_status, count(*) as count
            from theme_board_theme_snapshots
            where board_snapshot_id=?
            group by theme_status order by count desc
            """,
            (board_id,),
        )
        if board_id and has_table(con, "theme_board_theme_snapshots")
        else [],
        "latest_stock_role_counts": rows(
            con,
            """
            select stock_role, count(*) as count, sum(case when entry_usable then 1 else 0 end) as entry_usable_count
            from theme_board_stock_snapshots
            where board_snapshot_id=?
            group by stock_role order by count desc
            """,
            (board_id,),
        )
        if board_id and has_table(con, "theme_board_stock_snapshots")
        else [],
    }


def market_regime_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    if not has_table(con, "market_regime_snapshots"):
        return {"present": False}
    latest = rows(
        con,
        "select * from market_regime_snapshots where trade_date=? order by id desc limit 1",
        (trade_date,),
    )
    if not latest:
        return {"present": False}
    item = dict(latest[0])
    for key in list(item.keys()):
        if key.endswith("_json"):
            item[key.removesuffix("_json")] = parse_json(item[key], None)
    return {"present": True, "latest": compact_mapping(item, limit=40)}


def strategy_context_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    if not has_table(con, "strategy_context_latest"):
        return {"present": False}
    context_rows = rows(
        con,
        """
        select trade_date, code, candidate_id, context_id, calculated_at, session_phase, context_fresh,
               blocking_stage, primary_reason_code, reason_codes_json, payload_json, updated_at
        from strategy_context_latest
        where trade_date=?
        """,
        (trade_date,),
    )
    blocking = Counter(str(r.get("blocking_stage") or "") for r in context_rows)
    primary = Counter(str(r.get("primary_reason_code") or "") for r in context_rows)
    context_ids = Counter(str(r.get("context_id") or "") for r in context_rows)
    missing_sections = Counter()
    stale_true = 0
    samples = []
    for row in context_rows:
        payload = parse_json(row.get("payload_json"), {})
        for section in ("market", "theme", "stock", "data", "risk"):
            if not isinstance(payload.get(section), Mapping):
                missing_sections[section] += 1
        timestamps = payload.get("source_timestamps") if isinstance(payload, Mapping) else {}
        if row.get("context_fresh") and (not isinstance(timestamps, Mapping) or not timestamps):
            stale_true += 1
        if len(samples) < 20:
            samples.append(
                {
                    "code": row.get("code"),
                    "context_id": row.get("context_id"),
                    "context_fresh": bool(row.get("context_fresh")),
                    "blocking_stage": row.get("blocking_stage"),
                    "primary_reason_code": row.get("primary_reason_code"),
                    "reason_codes": parse_json(row.get("reason_codes_json"), [])[:10],
                    "updated_at": row.get("updated_at"),
                }
            )
    return {
        "present": True,
        "context_count": len(context_rows),
        "fresh_count": sum(1 for r in context_rows if bool(r.get("context_fresh"))),
        "blocking_stage_counts": dict(blocking),
        "primary_reason_counts": dict(primary.most_common(20)),
        "duplicate_context_id_count": sum(count - 1 for key, count in context_ids.items() if key and count > 1),
        "missing_section_counts": dict(missing_sections),
        "context_fresh_without_timestamps_count": stale_true,
        "samples": samples,
    }


def candidate_bridge_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if has_table(con, "candidate_source_events"):
        out["source_type_counts"] = rows(
            con,
            """
            select source_type, count(*) as count
            from candidate_source_events
            where trade_date=?
            group by source_type order by count desc
            """,
            (trade_date,),
        )
        out["condition_like_source_count"] = scalar(
            con,
            """
            select count(*) from candidate_source_events
            where trade_date=? and lower(source_type) like '%condition%'
            """,
            (trade_date,),
        )
    if has_table(con, "candidates"):
        cols = table_columns(con, "candidates")
        state_col = "state" if "state" in cols else ""
        code_col = "code" if "code" in cols else "stock_code" if "stock_code" in cols else ""
        out["candidate_count"] = scalar(con, "select count(*) from candidates where trade_date=?", (trade_date,))
        if state_col:
            out["candidate_state_counts"] = rows(
                con,
                f"select {state_col} as state, count(*) as count from candidates where trade_date=? group by {state_col}",
                (trade_date,),
            )
        if code_col:
            out["duplicate_candidate_code_count"] = scalar(
                con,
                f"""
                select count(*) from (
                    select {code_col}, count(*) as c from candidates
                    where trade_date=? group by {code_col} having c>1
                )
                """,
                (trade_date,),
            )
    return out


def expansion_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    if not has_table(con, "theme_expansion_subscription_decisions"):
        return {"present": False}
    return {
        "present": True,
        "action_status_counts": rows(
            con,
            """
            select action, status, count(*) as count
            from theme_expansion_subscription_decisions
            where trade_date=?
            group by action, status
            order by count desc
            """,
            (trade_date,),
        ),
        "latest": rows(
            con,
            """
            select calculated_at, code, theme_id, source, action, status, reason_codes_json
            from theme_expansion_subscription_decisions
            where trade_date=?
            order by id desc limit 20
            """,
            (trade_date,),
        ),
    }


def dirty_entry_summary(con: sqlite3.Connection, *, trade_date: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if has_table(con, "strategy_decision_events"):
        out["decision_counts"] = rows(
            con,
            """
            select action_type, action_result, gate_status, count(*) as count
            from strategy_decision_events
            where trade_date=?
            group by action_type, action_result, gate_status
            order by count desc limit 30
            """,
            (trade_date,),
        )
        out["decisions_with_order_refs"] = scalar(
            con,
            """
            select count(*) from strategy_decision_events
            where trade_date=?
              and (order_intent_id is not null or entry_plan_id is not null or virtual_order_id is not null)
            """,
            (trade_date,),
        )
    return out


def build_audit(
    *,
    trade_date: str,
    started_at: str,
    finished_at: str,
    core_url: str,
    samples: Mapping[str, Any],
    db_summary: Mapping[str, Any],
) -> dict[str, Any]:
    issues: list[Issue] = []
    runtime = last_data(samples.get("runtime_samples"))
    status = last_data(samples.get("status_samples"))
    gateway = last_data(samples.get("gateway_samples"))
    dashboard = last_data(samples.get("dashboard_samples"))
    health = data_or_empty(samples.get("health"))

    add_contract_issues(issues, runtime=runtime, status=status, dashboard=dashboard, health=health, db_summary=db_summary)
    add_gateway_issues(issues, runtime=runtime, status=status, gateway=gateway, db_summary=db_summary)
    add_market_issues(issues, dashboard=dashboard, db_summary=db_summary)
    add_theme_issues(issues, runtime=runtime, dashboard=dashboard, db_summary=db_summary)
    add_expansion_issues(issues, runtime=runtime, db_summary=db_summary)
    add_candidate_bridge_issues(issues, db_summary=db_summary)
    add_context_issues(issues, runtime=runtime, db_summary=db_summary)
    add_dirty_entry_issues(issues, runtime=runtime, db_summary=db_summary)

    issue_dicts = [asdict(issue) for issue in issues]
    p0 = [i for i in issue_dicts if i["severity"] == "P0"]
    p1 = [i for i in issue_dicts if i["severity"] == "P1"]
    if p0:
        verdict = "NOT_STABLE"
        exit_code = 2
    elif p1:
        verdict = "CONDITIONALLY_STABLE"
        exit_code = 1
    else:
        verdict = "STABLE_FOR_SETUP_ROUTER"
        exit_code = 0
    matrix = build_matrix(issue_dicts)
    return {
        "audit_name": "Strategy Context V3 Intraday Stabilization Audit",
        "trade_date": trade_date,
        "started_at": started_at,
        "finished_at": finished_at,
        "core_url": core_url,
        "verdict": verdict,
        "exit_code": exit_code,
        "summary": {
            "p0_count": len(p0),
            "p1_count": len(p1),
            "p2_count": sum(1 for i in issue_dicts if i["severity"] == "P2"),
            "sample_count": samples.get("sample_count", 0),
            "duration_sec": samples.get("duration_sec", 0),
        },
        "matrix": matrix,
        "issues": issue_dicts,
        "runtime_evidence": compact_runtime(runtime),
        "gateway_evidence": compact_gateway(gateway),
        "dashboard_evidence": compact_dashboard(dashboard),
        "db_summary": db_summary,
        "code_changes": ["tools/audit_strategy_context_v3_intraday.py"],
        "unresolved_risks": unresolved_risks(issue_dicts),
        "setup_router_assessment": verdict,
    }


def add_contract_issues(
    issues: list[Issue],
    *,
    runtime: Mapping[str, Any],
    status: Mapping[str, Any],
    dashboard: Mapping[str, Any],
    health: Mapping[str, Any],
    db_summary: Mapping[str, Any],
) -> None:
    profile = str(runtime.get("runtime_profile") or dashboard_get(dashboard, "v2_status", "runtime_profile") or "")
    mode = str(health.get("mode") or status.get("mode") or dashboard_get(dashboard, "v2_status", "trading_mode") or "")
    if profile != "THEME_CORE_V3":
        add_issue(issues, "P0", "Runtime", "RUNTIME_PROFILE_NOT_V3", f"runtime_profile={profile}", {"profile": profile})
    if mode != "OBSERVE":
        add_issue(issues, "P0", "Runtime", "TRADING_MODE_NOT_OBSERVE", f"mode={mode}", {"mode": mode})
    if bool(runtime.get("send_order_allowed")) or bool(runtime.get("order_path_enabled")):
        add_issue(
            issues,
            "P0",
            "Order Safety",
            "ORDER_PATH_ENABLED",
            "runtime exposes order path or send_order_allowed",
            {"send_order_allowed": runtime.get("send_order_allowed"), "order_path_enabled": runtime.get("order_path_enabled")},
        )
    order_manager = dict(runtime.get("order_manager") or dashboard.get("order_manager") or {})
    if bool(order_manager.get("enabled")) or bool(order_manager.get("send_order_allowed")):
        add_issue(issues, "P0", "Order Safety", "ORDER_MANAGER_ENABLED", "OrderManager is not disabled", order_manager)
    safety = dict(db_summary.get("safety") or {})
    for key in (
        "send_order_commands_today",
        "cancel_order_commands_today",
        "runtime_order_intents_today",
        "managed_order_intents_today",
        "live_sim_orders_today",
        "entry_plans_today",
        "strategy_decisions_with_order_refs_today",
        "legacy_virtual_orders_today",
    ):
        if int(safety.get(key) or 0) > 0:
            add_issue(issues, "P0", "Order Safety", key.upper(), f"{key}={safety.get(key)}", safety)
    for section_name in ("theme_board", "opening_burst", "strategy_context"):
        section = dict(runtime.get(section_name) or {})
        if bool(section.get("ready_allowed")) or bool(section.get("order_intent_allowed")):
            add_issue(
                issues,
                "P0",
                "Order Safety",
                f"{section_name.upper()}_ALLOWS_READY_OR_INTENT",
                f"{section_name} allows ready/order intent",
                section,
            )
    legacy = dict(runtime.get("legacy_runtime") or {})
    if bool(legacy.get("enabled")):
        add_issue(issues, "P0", "Runtime", "LEGACY_RUNTIME_ENABLED", "legacy runtime enabled in V3 path", legacy)


def add_gateway_issues(
    issues: list[Issue],
    *,
    runtime: Mapping[str, Any],
    status: Mapping[str, Any],
    gateway: Mapping[str, Any],
    db_summary: Mapping[str, Any],
) -> None:
    heartbeat_ok = bool(gateway.get("heartbeat_ok"))
    heartbeat_age = float_or_none(gateway.get("heartbeat_age_sec"))
    if not heartbeat_ok or (heartbeat_age is not None and heartbeat_age > 15):
        add_issue(issues, "P0", "Gateway/Data", "GATEWAY_HEARTBEAT_STALE", "gateway heartbeat is stale", compact_gateway(gateway))
    payload = dict(gateway.get("last_heartbeat_payload") or {})
    if bool(payload.get("command_polling_paused")):
        add_issue(issues, "P0", "Gateway/Data", "COMMAND_POLLING_PAUSED", "gateway command polling is paused", compact_gateway(gateway))
    pending = int(gateway.get("pending_command_count") or 0)
    if pending > 0:
        add_issue(issues, "P1", "Gateway/Data", "PENDING_COMMANDS", f"pending_command_count={pending}", compact_gateway(gateway))
    db_gateway = dict(db_summary.get("gateway") or {})
    tick_count = int(db_gateway.get("price_ticks_since_sample") or 0)
    active_subs = int(runtime.get("subscription_active_count") or 0)
    forwarded = int(runtime.get("runtime_forwarded_price_tick_count") or 0)
    if active_subs > 0 and tick_count <= 0:
        add_issue(
            issues,
            "P0",
            "Gateway/Data",
            "ACTIVE_SUBSCRIPTION_WITHOUT_PRICE_TICKS",
            "active realtime subscriptions exist but no price ticks reached DB during sampling",
            {"active_subscription_count": active_subs, "price_ticks_since_sample": tick_count, "latest_tick": db_gateway.get("latest_price_tick")},
        )
    if active_subs > 0 and forwarded <= 0:
        add_issue(
            issues,
            "P1",
            "Gateway/Data",
            "NO_FORWARDED_PRICE_TICKS_IN_RUNTIME",
            "runtime_forwarded_price_tick_count is zero while subscriptions are active",
            {"active_subscription_count": active_subs, "runtime_forwarded_price_tick_count": forwarded},
        )
    dropped = int(status.get("dropped_price_tick_count") or 0)
    if dropped > 0:
        add_issue(issues, "P1", "Gateway/Data", "DROPPED_PRICE_TICKS", f"dropped_price_tick_count={dropped}", {"dropped": dropped})


def add_market_issues(issues: list[Issue], *, dashboard: Mapping[str, Any], db_summary: Mapping[str, Any]) -> None:
    overview = dict(dashboard.get("market_overview") or {})
    global_status = str(overview.get("global_status") or "")
    if global_status == "DATA_WAIT":
        add_issue(
            issues,
            "P1",
            "MarketRegime",
            "MARKET_CONTEXT_DATA_WAIT",
            "market overview is DATA_WAIT",
            compact_mapping(overview),
        )
    market = dict(db_summary.get("market_regime") or {})
    if not market.get("present"):
        add_issue(issues, "P1", "MarketRegime", "MARKET_REGIME_SNAPSHOT_MISSING", "market_regime latest snapshot missing", market)


def add_theme_issues(
    issues: list[Issue],
    *,
    runtime: Mapping[str, Any],
    dashboard: Mapping[str, Any],
    db_summary: Mapping[str, Any],
) -> None:
    theme_board = dict(db_summary.get("theme_board") or {})
    runtime_theme = dict(runtime.get("theme_board") or {})
    if not theme_board.get("present"):
        add_issue(issues, "P1", "Theme Core V3", "THEME_BOARD_MISSING", "theme board snapshot missing", theme_board)
        return
    active = int(theme_board.get("latest", {}).get("active_theme_count") or runtime_theme.get("active_theme_count") or 0)
    top = list(theme_board.get("top_themes") or [])
    if top and active == 0:
        add_issue(
            issues,
            "P1",
            "Theme Core V3",
            "NO_ACTIVE_THEMES",
            "top themes exist but all remain DATA_WAIT/WATCH and active_theme_count is zero",
            {"active_theme_count": active, "top_themes": top[:5]},
        )
    if any("REALTIME_COVERAGE_LOW" in list(t.get("reason_codes") or []) for t in top):
        add_issue(
            issues,
            "P1",
            "Theme Core V3",
            "REALTIME_COVERAGE_LOW",
            "theme core is blocked by realtime coverage",
            {"top_themes": top[:5]},
        )
    opening = dict(db_summary.get("opening_burst") or {})
    latest = dict(opening.get("latest_result") or {})
    runtime_opening = dict(runtime.get("opening_burst") or {})
    if latest and int(latest.get("seed_batch_count") or 0) != int(runtime_opening.get("seed_batch_count") or latest.get("seed_batch_count") or 0):
        add_issue(
            issues,
            "P2",
            "Theme Core V3",
            "OPENING_BURST_RUNTIME_SUMMARY_MISMATCH",
            "runtime opening_burst summary differs from persisted result",
            {"runtime_opening": runtime_opening, "persisted_latest_result": latest},
            "Fix dashboard/runtime projection; persisted OBSERVE result is authoritative.",
        )


def add_expansion_issues(issues: list[Issue], *, runtime: Mapping[str, Any], db_summary: Mapping[str, Any]) -> None:
    section = dict(runtime.get("theme_expansion_subscription") or {})
    if bool(section.get("enabled")):
        selected = int(section.get("selected_count") or 0)
        active = int(section.get("active_count") or 0)
        if selected > active:
            add_issue(
                issues,
                "P1",
                "Expansion",
                "EXPANSION_SELECTED_NOT_ACTIVE",
                "focused expansion targets are not active subscriptions",
                section,
            )


def add_candidate_bridge_issues(issues: list[Issue], *, db_summary: Mapping[str, Any]) -> None:
    bridge = dict(db_summary.get("candidate_bridge") or {})
    duplicate_count = int(bridge.get("duplicate_candidate_code_count") or 0)
    if duplicate_count > 0:
        add_issue(
            issues,
            "P0",
            "CandidateBridge/FSM",
            "DUPLICATE_CANDIDATE_ROWS",
            "same trade_date+code appears as multiple candidates",
            {"duplicate_candidate_code_count": duplicate_count, "candidate_state_counts": bridge.get("candidate_state_counts")},
        )
    condition_count = int(bridge.get("condition_like_source_count") or 0)
    if condition_count > 0:
        add_issue(
            issues,
            "P2",
            "CandidateBridge/FSM",
            "CONDITION_SOURCE_EVENTS_PRESENT",
            "condition source events exist; verify they remain optional boosters only",
            {"condition_like_source_count": condition_count, "source_type_counts": bridge.get("source_type_counts")},
        )


def add_context_issues(issues: list[Issue], *, runtime: Mapping[str, Any], db_summary: Mapping[str, Any]) -> None:
    context = dict(db_summary.get("strategy_context") or {})
    if not context.get("present"):
        add_issue(issues, "P0", "StrategyContext", "CONTEXT_TABLE_MISSING", "strategy_context_latest missing", context)
        return
    active = int(runtime.get("active_candidate_count") or runtime.get("candidate_count") or 0)
    count = int(context.get("context_count") or 0)
    if active > count:
        add_issue(
            issues,
            "P0",
            "StrategyContext",
            "CONTEXT_COVERAGE_BELOW_ACTIVE_CANDIDATES",
            "not every active candidate has a latest StrategyContext",
            {"active_candidate_count": active, "context_count": count},
        )
    missing_sections = {k: v for k, v in dict(context.get("missing_section_counts") or {}).items() if int(v or 0) > 0}
    if missing_sections:
        add_issue(issues, "P0", "StrategyContext", "CONTEXT_SECTION_MISSING", "required context sections are missing", missing_sections)
    if int(context.get("context_fresh_without_timestamps_count") or 0) > 0:
        add_issue(
            issues,
            "P0",
            "StrategyContext",
            "CONTEXT_FRESH_WITHOUT_SOURCE_TIMESTAMPS",
            "context_fresh=true without source timestamps",
            {"count": context.get("context_fresh_without_timestamps_count")},
        )
    if int(context.get("duplicate_context_id_count") or 0) > 0:
        add_issue(
            issues,
            "P1",
            "StrategyContext",
            "DUPLICATE_CONTEXT_ID",
            "duplicate context_id in strategy_context_latest",
            {"duplicate_context_id_count": context.get("duplicate_context_id_count")},
        )
    if count > 0 and int(context.get("fresh_count") or 0) == 0:
        add_issue(
            issues,
            "P1",
            "StrategyContext",
            "NO_FRESH_CONTEXTS",
            "all latest contexts are stale/data-wait",
            {"context_count": count, "blocking_stage_counts": context.get("blocking_stage_counts"), "primary_reason_counts": context.get("primary_reason_counts")},
        )


def add_dirty_entry_issues(issues: list[Issue], *, runtime: Mapping[str, Any], db_summary: Mapping[str, Any]) -> None:
    dirty = dict(runtime.get("dirty_evaluator") or {})
    entry = dict(runtime.get("entry_engine") or {})
    if str(dirty.get("status") or "").upper() == "ERROR":
        add_issue(issues, "P0", "DirtyEvaluator", "DIRTY_EVALUATOR_ERROR", "dirty evaluator reports ERROR", dirty)
    if str(entry.get("status") or "").upper() == "ERROR":
        add_issue(issues, "P0", "EntryEngine", "ENTRY_ENGINE_ERROR", "entry engine reports ERROR", entry)
    if int(runtime.get("entry_plan_count") or 0) > 0 or int(runtime.get("virtual_order_count") or 0) > 0:
        add_issue(
            issues,
            "P0",
            "EntryEngine",
            "LEGACY_ENTRY_OUTPUT_IN_V3",
            "entry_plan_count or virtual_order_count is non-zero in V3 observe path",
            {"entry_plan_count": runtime.get("entry_plan_count"), "virtual_order_count": runtime.get("virtual_order_count")},
        )


def build_matrix(issues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    areas = [
        "Gateway/Data",
        "MarketRegime",
        "Theme Core V3",
        "Expansion",
        "CandidateBridge/FSM",
        "StrategyContext",
        "DirtyEvaluator",
        "EntryEngine",
        "Order Safety",
        "Runtime",
    ]
    issue_list = list(issues)
    matrix = []
    for area in areas:
        area_issues = [issue for issue in issue_list if issue.get("area") == area]
        status = "PASS"
        if any(issue.get("severity") == "P0" for issue in area_issues):
            status = "FAIL"
        elif any(issue.get("severity") == "P1" for issue in area_issues):
            status = "WARN"
        elif any(issue.get("severity") == "P2" for issue in area_issues):
            status = "WARN"
        matrix.append(
            {
                "area": area,
                "status": status,
                "evidence": [issue.get("code") for issue in area_issues],
                "cause": "; ".join(str(issue.get("status")) for issue in area_issues[:3]),
                "patched": area == "Runtime" and False,
                "remaining_risk": "; ".join(str(issue.get("recommendation") or issue.get("status")) for issue in area_issues[:3]),
            }
        )
    return matrix


def render_markdown(audit: Mapping[str, Any]) -> str:
    lines = [
        "# Strategy Context V3 Intraday Audit",
        "",
        f"- Trade date: `{audit.get('trade_date')}`",
        f"- Started: `{audit.get('started_at')}`",
        f"- Finished: `{audit.get('finished_at')}`",
        f"- Verdict: `{audit.get('verdict')}`",
        f"- Exit code: `{audit.get('exit_code')}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in dict(audit.get("summary") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Matrix",
            "",
            "| Area | Status | Evidence | Cause | Patched | Remaining risk |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in audit.get("matrix") or []:
        lines.append(
            "| {area} | {status} | {evidence} | {cause} | {patched} | {remaining_risk} |".format(
                area=row.get("area", ""),
                status=row.get("status", ""),
                evidence=", ".join(row.get("evidence") or []),
                cause=md_escape(str(row.get("cause") or "")),
                patched=row.get("patched", False),
                remaining_risk=md_escape(str(row.get("remaining_risk") or "")),
            )
        )
    lines.extend(["", "## Issues", ""])
    if not audit.get("issues"):
        lines.append("No issues detected.")
    for issue in audit.get("issues") or []:
        lines.append(f"### {issue.get('severity')} {issue.get('area')} {issue.get('code')}")
        lines.append("")
        lines.append(f"- Status: {issue.get('status')}")
        if issue.get("recommendation"):
            lines.append(f"- Recommendation: {issue.get('recommendation')}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(issue.get("evidence") or {}, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    lines.extend(["## SetupRouter Assessment", "", f"`{audit.get('setup_router_assessment')}`", ""])
    return "\n".join(lines)


def unresolved_risks(issues: Iterable[Mapping[str, Any]]) -> list[str]:
    return [f"{issue.get('severity')} {issue.get('area')} {issue.get('code')}" for issue in issues]


def compact_runtime(runtime: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: runtime.get(key)
        for key in (
            "runtime_profile",
            "status",
            "cycle_at",
            "candidate_count",
            "active_candidate_count",
            "subscription_active_count",
            "runtime_forwarded_price_tick_count",
            "entry_plan_count",
            "virtual_order_count",
            "order_path_enabled",
            "send_order_allowed",
            "pipeline_status",
        )
    }


def compact_gateway(gateway: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: gateway.get(key)
        for key in (
            "connection_state",
            "connected",
            "kiwoom_logged_in",
            "orderable",
            "mode",
            "last_heartbeat_at",
            "heartbeat_ok",
            "heartbeat_age_sec",
            "pending_command_count",
            "received_event_count",
            "last_error",
        )
    }


def compact_dashboard(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "v2_status": compact_mapping(dict(dashboard.get("v2_status") or {}), limit=30),
        "market_overview": compact_mapping(dict(dashboard.get("market_overview") or {}), limit=30),
        "leading_themes": compact_mapping(dict(dashboard.get("leading_themes") or {}), limit=20),
        "order_manager": compact_mapping(dict(dashboard.get("order_manager") or {}), limit=30),
    }


def latest_theme_board_row(con: sqlite3.Connection, trade_date: str) -> dict[str, Any] | None:
    if not has_table(con, "theme_board_snapshots"):
        return None
    item = rows(
        con,
        "select * from theme_board_snapshots where trade_date=? order by id desc limit 1",
        (trade_date,),
    )
    return item[0] if item else None


def compact_top_themes(themes: Iterable[Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for theme in themes:
        if not isinstance(theme, Mapping):
            continue
        compact.append(
            {
                "rank": theme.get("theme_rank") or theme.get("rank"),
                "theme_id": theme.get("theme_id"),
                "theme_name": theme.get("theme_name"),
                "theme_status": theme.get("theme_status") or theme.get("theme_state") or theme.get("status"),
                "theme_score": theme.get("theme_score"),
                "strong_count": theme.get("strong_count"),
                "leader_count": theme.get("leader_count"),
                "coverage_ratio": theme.get("coverage_ratio"),
                "breadth_ratio": theme.get("breadth_ratio"),
                "data_quality_status": theme.get("data_quality_status"),
                "reason_codes": theme.get("reason_codes") or [],
                "leader_symbol": theme.get("leader_symbol"),
                "co_leader_symbols": theme.get("co_leader_symbols") or [],
            }
        )
        if len(compact) >= 10:
            break
    return compact


def table_names(con: sqlite3.Connection) -> list[str]:
    return [str(r["name"]) for r in con.execute("select name from sqlite_master where type='table' order by name").fetchall()]


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not has_table(con, table):
        return set()
    return {str(r["name"]) for r in con.execute(f"pragma table_info({table})").fetchall()}


def has_table(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return row is not None


def row_count(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f"select count(*) from {table}").fetchone()[0])
    except Exception:
        return 0


def scalar(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = con.execute(sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)
    except Exception:
        return 0


def rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []


def count_by_date(con: sqlite3.Connection, table: str, trade_date: str) -> int:
    if not has_table(con, table):
        return 0
    cols = table_columns(con, table)
    if "trade_date" in cols:
        return scalar(con, f"select count(*) from {table} where trade_date=?", (trade_date,))
    if "created_at" in cols:
        return scalar(con, f"select count(*) from {table} where substr(created_at,1,10)=?", (trade_date,))
    return 0


def virtual_orders_today(con: sqlite3.Connection, trade_date: str) -> int:
    if not has_table(con, "virtual_orders"):
        return 0
    cols = table_columns(con, "virtual_orders")
    if "submitted_at" in cols:
        return scalar(con, "select count(*) from virtual_orders where substr(submitted_at,1,10)=?", (trade_date,))
    if "created_at" in cols:
        return scalar(con, "select count(*) from virtual_orders where substr(created_at,1,10)=?", (trade_date,))
    return 0


def is_etf_like(name: str) -> bool:
    prefixes = (
        "KODEX",
        "TIGER",
        "RISE",
        "ACE",
        "PLUS",
        "HANARO",
        "TIME",
        "SOL",
        "KBSTAR",
        "ARIRANG",
        "KOSEF",
        "BNK",
        "FOCUS",
        "WOORI",
        "KCGI",
        "UNICORN",
        "TRUE",
        "QV",
    )
    keywords = ("ETF", "ETN", "레버리지", "인버스", "커버드콜", "액티브", "선물", "채권")
    return name.startswith(prefixes) or any(keyword in name for keyword in keywords)


def parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def data_or_empty(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        data = response.get("data")
        return dict(data) if isinstance(data, Mapping) else {}
    return {}


def last_data(samples: Any) -> dict[str, Any]:
    if isinstance(samples, list) and samples:
        return data_or_empty(samples[-1])
    return {}


def dashboard_get(dashboard: Mapping[str, Any], *keys: str) -> Any:
    cur: Any = dashboard
    for key in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def compact_mapping(value: Mapping[str, Any], *, limit: int = 20) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for idx, (key, item) in enumerate(value.items()):
        if idx >= limit:
            break
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = item
        elif isinstance(item, list):
            out[str(key)] = item[:5]
        elif isinstance(item, Mapping):
            out[str(key)] = compact_mapping(item, limit=10)
    return out


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def add_issue(
    issues: list[Issue],
    severity: str,
    area: str,
    code: str,
    status: str,
    evidence: dict[str, Any],
    recommendation: str = "",
) -> None:
    issues.append(Issue(severity=severity, area=area, code=code, status=status, evidence=evidence, recommendation=recommendation))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def append_ndjson(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def md_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
