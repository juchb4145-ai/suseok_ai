from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading.broker.command_persistence import SQLiteCommandStore
from trading.broker.gateway_state import GatewayStateStore
from trading_app.live_sim_audit import LiveSimLifecycleAuditor


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit LIVE_SIM order lifecycle consistency.")
    parser.add_argument("--db", required=True, help="Trading sqlite3 DB path.")
    parser.add_argument("--trade-date", default="today", help="Trade date YYYY-MM-DD or 'today'.")
    parser.add_argument("--json", action="store_true", help="Print JSON report.")
    parser.add_argument("--fail-on-broken", action="store_true", help="Exit non-zero on BROKEN or RECONCILE_REQUIRED.")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    trade_date = date.today().isoformat() if str(args.trade_date).lower() == "today" else str(args.trade_date)
    db_path = Path(args.db)
    db = TradingDatabase(str(db_path))
    command_store = SQLiteCommandStore(db_path)
    gateway_state = GatewayStateStore(command_store=command_store)
    try:
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=trade_date, limit=args.limit)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        else:
            _print_text_report(report)
        if args.fail_on_broken and report.get("status") in {"BROKEN", "RECONCILE_REQUIRED"}:
            return 2
        return 0
    finally:
        db.close()
        command_store.close()


def _print_text_report(report: dict) -> None:
    summary = dict(report.get("summary") or {})
    print(f"LIVE_SIM audit: {report.get('status')} trade_date={report.get('trade_date')}")
    print(f"last_updated_at: {report.get('last_updated_at') or '-'}")
    print("")
    print("Summary")
    for key in [
        "open_live_sim_order_count",
        "unknown_submit_count",
        "reconcile_required_order_count",
        "cancel_requested_stale_count",
        "broker_order_id_missing_count",
        "orphan_execution_count",
        "orphan_position_count",
        "position_qty_mismatch_count",
        "duplicate_open_position_count",
        "reconcile_block_new_buy",
    ]:
        print(f"  {key}: {summary.get(key)}")
    issue_counts = dict(summary.get("issue_counts") or {})
    if issue_counts:
        print("")
        print("Issue counts")
        for issue_type, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {issue_type}: {count}")
    issues = list(report.get("issues") or [])
    if issues:
        print("")
        print("Reconcile required / warnings")
        for issue in issues[:20]:
            print(
                "  "
                f"[{issue.get('severity')}] {issue.get('issue_type')} "
                f"{issue.get('code') or '-'} {issue.get('order_intent_id') or issue.get('position_id') or '-'} "
                f"- {issue.get('operator_message_ko')}"
            )
    actions = list((report.get("operator") or {}).get("top_actions") or [])
    if actions:
        print("")
        print("Suggested operator actions")
        for action in actions:
            print(f"  {action.get('issue_type')}: {action.get('suggested_action')} ({action.get('count')})")


if __name__ == "__main__":
    raise SystemExit(main())
