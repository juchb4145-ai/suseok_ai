from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


REQUIRED_TABLES = {
    "decision_ledger": ["strategy_decision_events"],
    "outcome_labeler": ["strategy_decision_outcomes"],
    "shadow_strategy": ["shadow_strategy_evaluations"],
    "replay": ["strategy_replay_runs", "strategy_replay_reports"],
    "change_proposals": [
        "strategy_change_proposals",
        "strategy_change_evidence",
        "strategy_change_approvals",
    ],
}

SIDE_EFFECT_TABLES = [
    "runtime_order_intents",
    "virtual_orders",
    "virtual_positions",
    "gateway_commands",
]

FORBIDDEN_CONFIG_KEYS = [
    "live_order_enabled",
    "runtime_allow_live_orders",
    "trading_allow_live",
    "trading_allow_live_real",
    "broker_account",
    "account",
    "token",
    "secret",
    "password",
    "gateway_start",
    "order_sink_live",
]

SENSITIVE_KEY_PATTERN = re.compile(r"(token|secret|password|passwd|api[_-]?key|account)", re.IGNORECASE)
SAFE_SENSITIVE_KEY_PATTERN = re.compile(r"(masked|hash|hashed|checksum|fingerprint)", re.IGNORECASE)

GET_ENDPOINTS = [
    "/api/status",
    "/api/runtime/status",
    "/api/runtime/readiness",
]

CONTRACT_ENDPOINTS = [
    "/api/runtime/decisions/summary",
    "/api/runtime/outcomes/intraday/summary",
    "/api/runtime/shadow-strategies/summary",
    "/api/runtime/change-proposals/summary",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_validation(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_date = args.trade_date or datetime.now().date().isoformat()
    json_path = output_dir / f"{trade_date}_validation.json"
    md_path = output_dir / f"{trade_date}_validation.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(json.dumps({"overall_status": report["overall_status"], "json_path": str(json_path), "md_path": str(md_path)}, ensure_ascii=False, indent=2))
    return 2 if report["overall_status"] == "FAIL" and args.fail_on_unsafe else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate suseok_ai strategy feedback loop safety and consistency.")
    parser.add_argument("--db", default="data/trader_validation.sqlite3", help="SQLite DB path to validate.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Core API base URL.")
    parser.add_argument("--trade-date", default="", help="Trade date in YYYY-MM-DD.")
    parser.add_argument("--token", default="", help="Local token. POST endpoints are not called by default.")
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--skip-db", action="store_true")
    parser.add_argument("--skip-replay", action="store_true")
    parser.add_argument("--skip-proposal", action="store_true")
    parser.add_argument("--replay-bundle", default="")
    parser.add_argument("--horizon-sec", type=int, default=300)
    parser.add_argument("--output-dir", default="reports/strategy_validation")
    parser.add_argument("--fail-on-unsafe", dest="fail_on_unsafe", action="store_true", default=True)
    parser.add_argument("--no-fail-on-unsafe", dest="fail_on_unsafe", action="store_false")
    return parser.parse_args(argv)


def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": args.trade_date or "",
        "db_path": str(Path(args.db).resolve()),
        "base_url": args.base_url,
        "overall_status": "PASS",
        "safety_status": "PASS",
        "component_status": {},
        "api_status": {"skipped": bool(args.skip_api)},
        "db_status": {"skipped": bool(args.skip_db)},
        "decision_ledger": {},
        "outcome_labeler": {},
        "shadow_strategy": {},
        "replay": {"skipped": bool(args.skip_replay)},
        "change_proposals": {"skipped": bool(args.skip_proposal)},
        "warnings": [],
        "failures": [],
        "recommended_next_actions": [],
    }

    conn: sqlite3.Connection | None = None
    if not args.skip_db:
        conn = open_readonly_database(Path(args.db), report)
        if conn is not None:
            report["db_status"].update(check_schema(conn))
            report["component_status"].update(report["db_status"].get("component_status", {}))
            if table_exists(conn, "strategy_decision_events"):
                report["decision_ledger"] = check_decision_ledger(conn, trade_date=args.trade_date)
            if table_exists(conn, "strategy_decision_outcomes"):
                report["outcome_labeler"] = check_outcomes(conn, trade_date=args.trade_date, horizon_sec=args.horizon_sec)
            if table_exists(conn, "shadow_strategy_evaluations"):
                report["shadow_strategy"] = check_shadow_strategy(conn, trade_date=args.trade_date)
            if not args.skip_replay:
                report["replay"] = check_replay(conn, operating_db_path=Path(args.db), trade_date=args.trade_date)
            if not args.skip_proposal:
                report["change_proposals"] = check_change_proposals(conn, trade_date=args.trade_date)
            report["safety_status"] = check_static_safety(report)

    if not args.skip_api:
        report["api_status"] = check_api(args.base_url, trade_date=args.trade_date, token=args.token)

    finalize_report(report)
    if conn is not None:
        conn.close()
    return report


def open_readonly_database(path: Path, report: dict[str, Any]) -> sqlite3.Connection | None:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        report["db_status"] = {"status": "MISSING_DB", "path": str(resolved)}
        report["warnings"].append({"code": "MISSING_DB", "message": f"DB path does not exist: {resolved}"})
        return None
    uri = f"file:{resolved}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        report["db_status"] = {"status": "DB_OPEN_FAILED", "path": str(resolved), "error": str(exc)}
        report["failures"].append({"code": "DB_OPEN_FAILED", "message": str(exc)})
        return None


def check_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = set(list_tables(conn))
    component_status: dict[str, Any] = {}
    missing_components: list[dict[str, Any]] = []
    for component, required in REQUIRED_TABLES.items():
        missing = [name for name in required if name not in tables]
        status = "OK" if not missing else "MISSING_COMPONENT"
        component_status[component] = {"status": status, "missing_tables": missing, "required_tables": required}
        if missing:
            missing_components.append({"component": component, "missing_tables": missing})
    return {
        "status": "OK" if not missing_components else "WARN",
        "table_count": len(tables),
        "component_status": component_status,
        "missing_components": missing_components,
    }


def check_decision_ledger(conn: sqlite3.Connection, *, trade_date: str = "") -> dict[str, Any]:
    where, params = trade_date_where(trade_date)
    count = scalar(conn, f"SELECT COUNT(*) FROM strategy_decision_events {where}", params)
    duplicate_ids = rows(conn, "SELECT decision_id, COUNT(*) AS count FROM strategy_decision_events GROUP BY decision_id HAVING COUNT(*) > 1 LIMIT 50")
    gate_status = group_counts(conn, "strategy_decision_events", "gate_status", where, params)
    action_type = group_counts(conn, "strategy_decision_events", "action_type", where, params)
    action_result = group_counts(conn, "strategy_decision_events", "action_result", where, params)
    missing_candidate_instance_filter = where_with_extra(where, "candidate_instance_id = ''")
    candidate_instance_missing = scalar(
        conn,
        f"SELECT COUNT(*) FROM strategy_decision_events {missing_candidate_instance_filter}",
        params,
    )
    empty_reason_codes_filter = where_with_extra(where, "(reason_codes_json = '' OR reason_codes_json = '[]')")
    reason_codes_empty = scalar(
        conn,
        f"SELECT COUNT(*) FROM strategy_decision_events {empty_reason_codes_filter}",
        params,
    )
    ready_without_order = scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM strategy_decision_events
        {where_with_extra(where, "gate_status = 'READY' AND action_type IN ('READY','ENTRY_PLAN','ENTRY_ORDER_INTENT') AND COALESCE(order_intent_id, '') = '' AND virtual_order_id IS NULL")}
        """,
        params,
    )
    sensitive = scan_sensitive_json_columns(
        conn,
        "strategy_decision_events",
        ["reason_codes_json", "data_quality_issues_json", "details_json"],
        where=where,
        params=params,
        limit=50,
    )
    return {
        "status": "FAIL" if duplicate_ids or sensitive else ("EMPTY" if count == 0 else "OK"),
        "event_count": count,
        "gate_status_distribution": gate_status,
        "action_type_distribution": action_type,
        "action_result_distribution": action_result,
        "duplicate_decision_ids": duplicate_ids,
        "candidate_instance_id_missing_count": candidate_instance_missing,
        "candidate_instance_id_coverage_pct": percent(count - candidate_instance_missing, count),
        "reason_codes_empty_count": reason_codes_empty,
        "reason_codes_empty_pct": percent(reason_codes_empty, count),
        "ready_without_order_count_db": ready_without_order,
        "sensitive_json_hits": sensitive,
    }


def check_outcomes(conn: sqlite3.Connection, *, trade_date: str = "", horizon_sec: int = 300) -> dict[str, Any]:
    where, params = trade_date_where(trade_date)
    count = scalar(conn, f"SELECT COUNT(*) FROM strategy_decision_outcomes {where}", params)
    duplicates = rows(
        conn,
        """
        SELECT decision_id, horizon_sec, COUNT(*) AS count
        FROM strategy_decision_outcomes
        GROUP BY decision_id, horizon_sec
        HAVING COUNT(*) > 1
        LIMIT 50
        """,
    )
    horizon_distribution = group_counts(conn, "strategy_decision_outcomes", "horizon_sec", where, params)
    label_distribution = group_counts(conn, "strategy_decision_outcomes", "outcome_label", where, params)
    insufficient_filter = where_with_extra(where, "data_status = 'INSUFFICIENT_OUTCOME_DATA' OR outcome_label = 'INSUFFICIENT_OUTCOME_DATA'")
    insufficient_count = scalar(
        conn,
        f"SELECT COUNT(*) FROM strategy_decision_outcomes {insufficient_filter}",
        params,
    )
    lookahead = detect_outcome_lookahead(conn, trade_date=trade_date, limit=50)
    invalid_prices = scalar(
        conn,
        f"""
        SELECT COUNT(*) FROM strategy_decision_outcomes
        {where_with_extra(where, "price_at_decision IS NULL OR price_at_decision <= 0 OR max_return_pct IS NULL OR max_drawdown_pct IS NULL")}
        """,
        params,
    )
    sensitive = scan_sensitive_json_columns(
        conn,
        "strategy_decision_outcomes",
        ["data_quality_issues_json", "details_json"],
        where=where,
        params=params,
        limit=50,
    )
    return {
        "status": "FAIL" if duplicates or lookahead or sensitive else ("EMPTY" if count == 0 else "OK"),
        "outcome_count": count,
        "horizon_sec_requested": horizon_sec,
        "horizon_distribution": horizon_distribution,
        "outcome_label_distribution": label_distribution,
        "duplicate_decision_horizon": duplicates,
        "lookahead_bias_hits": lookahead,
        "insufficient_outcome_data_count": insufficient_count,
        "insufficient_outcome_data_pct": percent(insufficient_count, count),
        "invalid_price_metric_count": invalid_prices,
        "sensitive_json_hits": sensitive,
    }


def check_shadow_strategy(conn: sqlite3.Connection, *, trade_date: str = "") -> dict[str, Any]:
    where, params = trade_date_where(trade_date)
    count = scalar(conn, f"SELECT COUNT(*) FROM shadow_strategy_evaluations {where}", params)
    duplicates = rows(
        conn,
        """
        SELECT decision_id, policy_id, COUNT(*) AS count
        FROM shadow_strategy_evaluations
        GROUP BY decision_id, policy_id
        HAVING COUNT(*) > 1
        LIMIT 50
        """,
    )
    changed = group_counts(conn, "shadow_strategy_evaluations", "changed_decision", where, params)
    change_type = group_counts(conn, "shadow_strategy_evaluations", "change_type", where, params)
    policy_counts = group_counts(conn, "shadow_strategy_evaluations", "policy_id", where, params)
    side_effect_counts = table_counts(conn, SIDE_EFFECT_TABLES)
    sensitive = scan_sensitive_json_columns(
        conn,
        "shadow_strategy_evaluations",
        ["baseline_reason_codes_json", "shadow_reason_codes_json", "data_quality_issues_json", "details_json"],
        where=where,
        params=params,
        limit=50,
    )
    return {
        "status": "FAIL" if duplicates or sensitive else ("EMPTY" if count == 0 else "OK"),
        "evaluation_count": count,
        "duplicate_decision_policy": duplicates,
        "changed_decision_distribution": changed,
        "change_type_distribution": change_type,
        "policy_distribution": policy_counts,
        "side_effect_table_counts_snapshot": side_effect_counts,
        "side_effect_validation_note": "Counts are a static snapshot. Rebuild delta validation should compare before/after counts.",
        "sensitive_json_hits": sensitive,
    }


def check_replay(conn: sqlite3.Connection, *, operating_db_path: Path, trade_date: str = "") -> dict[str, Any]:
    required = ["strategy_replay_runs", "strategy_replay_reports"]
    missing = [name for name in required if not table_exists(conn, name)]
    if missing:
        return {"status": "MISSING_COMPONENT", "missing_tables": missing}
    where, params = trade_date_where(trade_date)
    run_count = scalar(conn, f"SELECT COUNT(*) FROM strategy_replay_runs {where}", params)
    report_count = scalar(conn, f"SELECT COUNT(*) FROM strategy_replay_reports {where}", params)
    bad_paths: list[dict[str, Any]] = []
    op = normalize_path(operating_db_path)
    for row in rows(conn, f"SELECT replay_id, replay_db_path, status, warnings_json FROM strategy_replay_runs {where} LIMIT 100", params):
        replay_db_path = row.get("replay_db_path") or ""
        if replay_db_path and normalize_path(replay_db_path) == op:
            bad_paths.append(row)
    report_quality = rows(
        conn,
        f"""
        SELECT report_id, replay_id, mode, summary_json, funnel_json, outcome_summary_json, shadow_summary_json, recommendations_json
        FROM strategy_replay_reports
        {where}
        ORDER BY id DESC
        LIMIT 20
        """,
        params,
    )
    missing_report_sections = []
    for item in report_quality:
        for key in ("summary_json", "funnel_json", "outcome_summary_json", "shadow_summary_json"):
            if not json_loads(item.get(key), {}):
                missing_report_sections.append({"report_id": item.get("report_id"), "section": key})
    sensitive = scan_sensitive_json_columns(
        conn,
        "strategy_replay_reports",
        ["summary_json", "funnel_json", "outcome_summary_json", "shadow_summary_json", "diff_summary_json", "recommendations_json"],
        where=where,
        params=params,
        limit=50,
    )
    status = "FAIL" if bad_paths or sensitive else ("EMPTY" if run_count == 0 and report_count == 0 else ("WARN" if missing_report_sections else "OK"))
    return {
        "status": status,
        "run_count": run_count,
        "report_count": report_count,
        "replay_db_same_as_operating_db": bad_paths,
        "missing_report_sections": missing_report_sections[:50],
        "sensitive_json_hits": sensitive,
    }


def check_change_proposals(conn: sqlite3.Connection, *, trade_date: str = "") -> dict[str, Any]:
    required = ["strategy_change_proposals", "strategy_change_evidence", "strategy_change_approvals"]
    missing = [name for name in required if not table_exists(conn, name)]
    if missing:
        return {"status": "MISSING_COMPONENT", "missing_tables": missing}
    where, params = trade_date_where(trade_date)
    count = scalar(conn, f"SELECT COUNT(*) FROM strategy_change_proposals {where}", params)
    grade_distribution = group_counts(conn, "strategy_change_proposals", "recommendation_grade", where, params)
    status_distribution = group_counts(conn, "strategy_change_proposals", "status", where, params)
    evidence_missing = rows(
        conn,
        f"""
        SELECT p.proposal_id, p.recommendation_grade, p.status, p.candidate_config_patch_json
        FROM strategy_change_proposals p
        LEFT JOIN strategy_change_evidence e ON e.proposal_id = p.proposal_id
        {where_alias(where, "p")}
        GROUP BY p.proposal_id
        HAVING COUNT(e.id) = 0
        LIMIT 50
        """,
        params,
    )
    forbidden_patch_hits = detect_forbidden_patch_keys(conn, trade_date=trade_date)
    guardrail_misses = []
    for hit in forbidden_patch_hits:
        grade = str(hit.get("recommendation_grade") or "")
        guardrail_passed = int(hit.get("guardrail_passed") or 0)
        if guardrail_passed or grade not in {"DO_NOT_APPLY", "DATA_INSUFFICIENT", "RISKY_CANDIDATE"}:
            guardrail_misses.append(hit)
    sensitive = scan_sensitive_json_columns(
        conn,
        "strategy_change_proposals",
        [
            "source_ids_json",
            "baseline_config_snapshot_json",
            "candidate_config_patch_json",
            "data_quality_issues_json",
            "rollout_plan_json",
            "rollback_plan_json",
        ],
        where=where,
        params=params,
        limit=50,
    )
    evidence_sensitive = scan_sensitive_json_columns(
        conn,
        "strategy_change_evidence",
        ["evidence_payload_json"],
        where=where,
        params=params,
        limit=50,
    )
    status = "FAIL" if guardrail_misses or sensitive or evidence_sensitive else ("EMPTY" if count == 0 else ("WARN" if evidence_missing or forbidden_patch_hits else "OK"))
    return {
        "status": status,
        "proposal_count": count,
        "status_distribution": status_distribution,
        "recommendation_grade_distribution": grade_distribution,
        "evidence_missing_proposals": evidence_missing,
        "forbidden_patch_hits": forbidden_patch_hits,
        "forbidden_patch_guardrail_misses": guardrail_misses,
        "sensitive_json_hits": sensitive + evidence_sensitive,
        "approve_workflow_note": "Default validation does not call approve endpoints. Use a copied validation DB for protected before/after config-hash checks.",
    }


def check_api(base_url: str, *, trade_date: str = "", token: str = "") -> dict[str, Any]:
    base = base_url.rstrip("/")
    endpoints = list(GET_ENDPOINTS)
    if trade_date:
        endpoints.extend([f"{item}?trade_date={urllib.parse.quote(trade_date)}" for item in CONTRACT_ENDPOINTS])
    else:
        endpoints.extend(CONTRACT_ENDPOINTS)
    results = []
    failures = []
    warnings = []
    for endpoint in endpoints:
        url = f"{base}{endpoint}"
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Local-Token"] = token
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                body = response.read(2_000_000)
                status = int(response.status)
                payload = json.loads(body.decode("utf-8")) if body else {}
            item = {"endpoint": endpoint, "status_code": status, "ok": 200 <= status < 300}
            if endpoint.startswith("/api/runtime/status"):
                item["runtime_mode"] = payload.get("mode")
                item["last_error"] = payload.get("last_error", "")
                latest = payload.get("latest_snapshot") or {}
                item["live_order_enabled"] = bool(latest.get("live_order_enabled"))
                item["runtime_allow_live_orders"] = bool(latest.get("runtime_allow_live_orders", False))
                if item["live_order_enabled"]:
                    failures.append({"code": "UNSAFE_LIVE_ORDER_ENABLED", "endpoint": endpoint})
                if payload.get("last_error"):
                    warnings.append({"code": "RUNTIME_LAST_ERROR", "message": str(payload.get("last_error"))})
            results.append(item)
        except urllib.error.HTTPError as exc:
            item = {"endpoint": endpoint, "status_code": exc.code, "ok": False, "error": str(exc)}
            results.append(item)
            failures.append({"code": "API_CONTRACT_BROKEN", "endpoint": endpoint, "status_code": exc.code})
        except Exception as exc:
            item = {"endpoint": endpoint, "status_code": 0, "ok": False, "error": str(exc)}
            results.append(item)
            warnings.append({"code": "API_UNAVAILABLE", "endpoint": endpoint, "message": str(exc)})
    status = "FAIL" if failures else ("WARN" if warnings else "OK")
    return {"status": status, "checks": results, "warnings": warnings, "failures": failures}


def check_static_safety(report: dict[str, Any]) -> str:
    failures = []
    if report.get("decision_ledger", {}).get("duplicate_decision_ids"):
        failures.append({"code": "DUPLICATE_DECISION_ID", "message": "Duplicate decision_id rows found."})
    if report.get("outcome_labeler", {}).get("duplicate_decision_horizon"):
        failures.append({"code": "DUPLICATE_OUTCOME_HORIZON", "message": "Duplicate decision_id + horizon_sec rows found."})
    if report.get("outcome_labeler", {}).get("lookahead_bias_hits"):
        failures.append({"code": "OUTCOME_LOOKAHEAD_BIAS", "message": "Outcome was evaluated before decision_at + horizon_sec."})
    if report.get("shadow_strategy", {}).get("duplicate_decision_policy"):
        failures.append({"code": "DUPLICATE_SHADOW_POLICY", "message": "Duplicate decision_id + policy_id rows found."})
    if report.get("replay", {}).get("replay_db_same_as_operating_db"):
        failures.append({"code": "REPLAY_DB_CONTAMINATION", "message": "A replay run points to the operating DB path."})
    if report.get("change_proposals", {}).get("forbidden_patch_guardrail_misses"):
        failures.append({"code": "PROPOSAL_AUTO_APPLY_RISK", "message": "Forbidden config patch passed guardrails."})
    for section_name in ("decision_ledger", "outcome_labeler", "shadow_strategy", "replay", "change_proposals"):
        hits = report.get(section_name, {}).get("sensitive_json_hits") or []
        if hits:
            failures.append({"code": "SENSITIVE_DATA_IN_REPORT", "section": section_name, "count": len(hits)})
    report["failures"].extend(failures)
    return "FAIL" if failures else "PASS"


def finalize_report(report: dict[str, Any]) -> None:
    failures = list(report.get("failures") or [])
    warnings = list(report.get("warnings") or [])
    for section_name in ("decision_ledger", "outcome_labeler", "shadow_strategy", "replay", "change_proposals", "api_status", "db_status"):
        section = report.get(section_name) or {}
        if section.get("status") == "FAIL":
            failures.append({"code": f"{section_name.upper()}_FAILED", "section": section_name})
        elif section.get("status") in {"WARN", "MISSING_COMPONENT", "MISSING_DB", "EMPTY"}:
            warnings.append({"code": f"{section_name.upper()}_{section.get('status')}", "section": section_name})
    report["failures"] = dedupe_dicts(failures)
    report["warnings"] = dedupe_dicts(warnings)
    report["overall_status"] = "FAIL" if report["failures"] else ("WARN" if report["warnings"] else "PASS")
    report["recommended_next_actions"] = recommended_actions(report)


def recommended_actions(report: dict[str, Any]) -> list[str]:
    actions = []
    failure_codes = {item.get("code") for item in report.get("failures") or []}
    warning_codes = {item.get("code") for item in report.get("warnings") or []}
    if "MISSING_DB" in warning_codes or "DB_OPEN_FAILED" in failure_codes:
        actions.append("Create a copied validation DB first: Copy-Item data/trader.sqlite3 data/trader_validation.sqlite3")
    if "OUTCOME_LOOKAHEAD_BIAS" in failure_codes:
        actions.append("Stop trusting current outcome labels until evaluated_at >= decision_at + horizon_sec is fixed.")
    if "REPLAY_DB_CONTAMINATION" in failure_codes:
        actions.append("Inspect replay runner options and rerun replay with data/replay/*.sqlite3 only.")
    if "PROPOSAL_AUTO_APPLY_RISK" in failure_codes:
        actions.append("Block proposal approval workflow until forbidden config keys are rejected.")
    if "SENSITIVE_DATA_IN_REPORT" in failure_codes:
        actions.append("Sanitize details/report JSON and rotate any exposed token/account secret if real values were persisted.")
    if "API_CONTRACT_BROKEN" in failure_codes:
        actions.append("Check Core API logs for 500s before using the dashboard panels operationally.")
    if not actions:
        actions.append("Use the runbook dashboard checklist for intraday observe validation and rerun this script after market close.")
    return actions


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Strategy Feedback Loop Validation Report",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- trade_date: `{report.get('trade_date') or ''}`",
        f"- overall_status: `{report.get('overall_status')}`",
        f"- safety_status: `{report.get('safety_status')}`",
        f"- db_path: `{report.get('db_path')}`",
        f"- base_url: `{report.get('base_url')}`",
        "",
        "## Component Status",
    ]
    component_status = report.get("component_status") or {}
    if component_status:
        for name, payload in component_status.items():
            lines.append(f"- {name}: `{payload.get('status')}` missing={payload.get('missing_tables') or []}")
    else:
        lines.append("- No component status was collected.")
    lines.extend(["", "## Key Counts"])
    lines.append(f"- decision_events: `{report.get('decision_ledger', {}).get('event_count', 0)}`")
    lines.append(f"- outcomes: `{report.get('outcome_labeler', {}).get('outcome_count', 0)}`")
    lines.append(f"- shadow_evaluations: `{report.get('shadow_strategy', {}).get('evaluation_count', 0)}`")
    lines.append(f"- replay_runs: `{report.get('replay', {}).get('run_count', 0)}`")
    lines.append(f"- replay_reports: `{report.get('replay', {}).get('report_count', 0)}`")
    lines.append(f"- change_proposals: `{report.get('change_proposals', {}).get('proposal_count', 0)}`")
    lines.extend(["", "## Failures"])
    failures = report.get("failures") or []
    if failures:
        for item in failures:
            lines.append(f"- `{item.get('code')}` {item.get('message', '')}")
    else:
        lines.append("- None")
    lines.extend(["", "## Warnings"])
    warnings = report.get("warnings") or []
    if warnings:
        for item in warnings:
            lines.append(f"- `{item.get('code')}` {item.get('message', '')}")
    else:
        lines.append("- None")
    lines.extend(["", "## Recommended Next Actions"])
    for action in report.get("recommended_next_actions") or []:
        lines.append(f"- {action}")
    lines.extend(["", "## Safety Notes"])
    lines.append("- This validator does not submit orders, start Kiwoom Gateway, run replay by default, or approve proposals.")
    lines.append("- Use a copied validation DB for protected POST workflow tests.")
    return "\n".join(lines) + "\n"


def validate_approval_config_unchanged(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Validate that an approval workflow changed status metadata only, not config payloads."""
    protected_keys = [
        "baseline_config_hash",
        "candidate_config_hash",
        "baseline_config_snapshot",
        "candidate_config_patch",
        "baseline_config_snapshot_json",
        "candidate_config_patch_json",
    ]
    changed = []
    for key in protected_keys:
        if before.get(key) != after.get(key):
            changed.append(key)
    return {
        "status": "FAIL" if changed else "OK",
        "changed_config_keys": changed,
        "previous_status": before.get("status", ""),
        "next_status": after.get("status", ""),
    }


def detect_outcome_lookahead(conn: sqlite3.Connection, *, trade_date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    where, params = trade_date_where(trade_date)
    result = []
    for row in rows(
        conn,
        f"""
        SELECT outcome_id, decision_id, decision_at, evaluated_at, horizon_sec
        FROM strategy_decision_outcomes
        {where}
        LIMIT 5000
        """,
        params,
    ):
        decision_at = parse_dt(row.get("decision_at"))
        evaluated_at = parse_dt(row.get("evaluated_at"))
        horizon = int(row.get("horizon_sec") or 0)
        if decision_at and evaluated_at and horizon > 0 and evaluated_at < decision_at + timedelta(seconds=horizon):
            result.append(row)
            if len(result) >= limit:
                break
    return result


def detect_forbidden_patch_keys(conn: sqlite3.Connection, *, trade_date: str = "") -> list[dict[str, Any]]:
    where, params = trade_date_where(trade_date)
    hits = []
    for row in rows(
        conn,
        f"""
        SELECT proposal_id, recommendation_grade, guardrail_passed, blocked_by_guardrail_reason, candidate_config_patch_json
        FROM strategy_change_proposals
        {where}
        LIMIT 5000
        """,
        params,
    ):
        patch = json_loads(row.get("candidate_config_patch_json"), {})
        found = []
        for path, _value in iter_json_paths(patch):
            lower = path.lower()
            if any(key in lower for key in FORBIDDEN_CONFIG_KEYS):
                found.append(path)
        if found:
            item = dict(row)
            item["forbidden_paths"] = sorted(set(found))
            hits.append(item)
    return hits


def scan_sensitive_json_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    where: str = "",
    params: tuple[Any, ...] = (),
    limit: int = 50,
) -> list[dict[str, Any]]:
    available = set(table_columns(conn, table))
    columns = [col for col in columns if col in available]
    if not columns:
        return []
    id_col = "id" if "id" in available else "rowid"
    select_cols = ", ".join([id_col] + columns)
    hits = []
    for row in rows(conn, f"SELECT {select_cols} FROM {table} {where} LIMIT 5000", params):
        row_id = row.get(id_col)
        for col in columns:
            value = row.get(col)
            payload = json_loads(value, None)
            if payload is None:
                continue
            for path, leaf in iter_json_paths(payload):
                key = path.rsplit(".", 1)[-1]
                if not SENSITIVE_KEY_PATTERN.search(key):
                    continue
                if SAFE_SENSITIVE_KEY_PATTERN.search(key):
                    continue
                if leaf in (None, "", [], {}):
                    continue
                hits.append({"table": table, "row_id": row_id, "column": col, "path": path})
                if len(hits) >= limit:
                    return hits
    return hits


def iter_json_paths(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from iter_json_paths(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from iter_json_paths(item, path)
    else:
        yield prefix, value


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def list_tables(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def table_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for table in tables:
        result[table] = scalar(conn, f"SELECT COUNT(*) FROM {table}") if table_exists(conn, table) else None
    return result


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def group_counts(conn: sqlite3.Connection, table: str, column: str, where: str = "", params: tuple[Any, ...] = ()) -> dict[str, int]:
    if column not in table_columns(conn, table):
        return {}
    data = rows(
        conn,
        f"""
        SELECT COALESCE(CAST({column} AS TEXT), '') AS key, COUNT(*) AS count
        FROM {table}
        {where}
        GROUP BY COALESCE(CAST({column} AS TEXT), '')
        ORDER BY count DESC, key ASC
        LIMIT 50
        """,
        params,
    )
    return {str(item["key"]): int(item["count"] or 0) for item in data}


def trade_date_where(trade_date: str) -> tuple[str, tuple[Any, ...]]:
    return ("WHERE trade_date = ?", (trade_date,)) if trade_date else ("", ())


def where_with_extra(where: str, extra: str) -> str:
    return f"{where} AND {extra}" if where else f"WHERE {extra}"


def where_alias(where: str, alias: str) -> str:
    if not where:
        return ""
    return where.replace("trade_date", f"{alias}.trade_date", 1)


def json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def normalize_path(value: str | os.PathLike[str]) -> str:
    try:
        return str(Path(value).expanduser().resolve()).lower()
    except Exception:
        return str(value).lower()


def percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 4)


def dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
