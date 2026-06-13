from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading_app.shadow_small_entry_ops import ShadowSmallEntryOpsService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shadow Small Entry ops control and audit")
    parser.add_argument("command", choices=["status", "preflight", "arm", "confirm", "pause", "rollback", "risk-check", "report"])
    parser.add_argument("--db", default="data/trading.sqlite3")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--operator", default="cli")
    parser.add_argument("--note", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--format", default="all", choices=["json", "csv", "md", "markdown", "all"])
    parser.add_argument("--fail-on-broken", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    trade_date = None if args.trade_date in {"", "today"} else args.trade_date
    db = TradingDatabase(Path(args.db))
    try:
        service = ShadowSmallEntryOpsService(db)
        if args.command == "status":
            return service.status(trade_date=trade_date)
        if args.command == "preflight":
            return service.preflight(trade_date=trade_date, persist=True)
        if args.command == "arm":
            return service.arm(operator=args.operator, note=args.note, trade_date=trade_date)
        if args.command == "confirm":
            return service.confirm(activation_token=args.token, operator=args.operator, note=args.note, trade_date=trade_date)
        if args.command == "pause":
            return service.pause(operator=args.operator, note=args.note, trade_date=trade_date)
        if args.command == "rollback":
            return service.rollback(operator=args.operator, note=args.note, trade_date=trade_date)
        if args.command == "risk-check":
            return service.risk_check(trade_date=trade_date, auto_pause=True)
        report = service.build_report(trade_date=trade_date, limit=500)
        if args.export:
            report["exports"] = service.export_report(report, fmt=args.format)
        return report
    finally:
        db.close()


def _print_human(payload: dict[str, Any]) -> None:
    status = payload.get("status") or payload.get("status", {}).get("status") or "UNKNOWN"
    print(f"status: {status}")
    if payload.get("mode") is not None:
        print(f"mode: {payload.get('mode')} / order_enabled={payload.get('order_enabled')}")
    if payload.get("preflight_status") or payload.get("status") in {"PASS", "WARN", "FAIL"}:
        print(f"preflight: {payload.get('preflight_status') or payload.get('status')}")
    reasons = payload.get("preflight_blocking_reasons") or payload.get("blocking_reasons") or []
    if reasons:
        print("blocking_reasons:")
        for reason in reasons:
            print(f"- {reason}")
    today = payload.get("today") or {}
    if today:
        print("today:")
        for key in ["promotion_count", "submitted_count", "filled_count", "open_position_count", "total_notional_krw", "unknown_submit_count", "reconcile_required_count"]:
            print(f"- {key}: {today.get(key)}")
    if payload.get("operator_message_ko"):
        print(f"operator_message_ko: {payload.get('operator_message_ko')}")
    if payload.get("activation_token"):
        print(f"activation_token: {payload.get('activation_token')}")
        print(f"activation_expires_at: {payload.get('activation_expires_at')}")
    if payload.get("exports"):
        print("exports:")
        for key, value in payload["exports"].items():
            print(f"- {key}: {value}")


def main() -> int:
    args = _parser().parse_args()
    payload = _run(args)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        _print_human(payload)
    if args.fail_on_broken:
        raw_status = payload.get("status")
        status = str(raw_status.get("status") if isinstance(raw_status, dict) else raw_status or "")
        if status in {"BROKEN", "RECONCILE_REQUIRED"}:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
