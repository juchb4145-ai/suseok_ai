from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading_app.shadow_small_entry_pilot import ShadowSmallEntryPilotService


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow Small Entry 1-day pilot run report tool")
    parser.add_argument("command", choices=["status", "start", "complete", "report"])
    parser.add_argument("--db", default="data/trading.sqlite3")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--pilot-id", default="")
    parser.add_argument("--operator", default="cli")
    parser.add_argument("--note", default="")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--format", default="all", choices=["json", "csv", "md", "markdown", "all"])
    args = parser.parse_args()

    db = TradingDatabase(str(Path(args.db)))
    try:
        service = ShadowSmallEntryPilotService(db)
        if args.command == "status":
            payload = service.status(trade_date=args.trade_date or None)
        elif args.command == "start":
            payload = service.start(
                trade_date=args.trade_date or None,
                operator=args.operator,
                operator_note=args.note,
            )
        elif args.command == "complete":
            payload = service.complete(
                trade_date=args.trade_date or None,
                operator=args.operator,
                operator_note=args.note,
                export=args.export,
                fmt=args.format,
            )
        else:
            report = service.build_report(
                trade_date=args.trade_date or None,
                pilot_id=args.pilot_id,
                persist=True,
            )
            payload = {
                "report": report,
                "exports": service.export_report(report, fmt=args.format) if args.export else {},
            }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        else:
            _print_human(payload)
    finally:
        db.close()
    return 0


def _print_human(payload: dict) -> None:
    report = payload.get("report") or payload.get("run") or payload
    summary = report.get("summary") or {}
    print(f"status: {report.get('status') or payload.get('status')}")
    print(f"pilot_id: {report.get('pilot_id') or (payload.get('run') or {}).get('pilot_id') or ''}")
    print(f"recommendation: {report.get('recommendation') or payload.get('recommendation') or ''}")
    if summary:
        print(f"candidates: {summary.get('candidate_count', 0)}")
        print(f"submitted: {summary.get('submitted_order_count', 0)}")
        print(f"filled: {summary.get('filled_order_count', 0)}")
        print(f"total_pnl_krw: {summary.get('total_pnl_krw', 0)}")
    exports = payload.get("exports") or {}
    if exports:
        print("exports:")
        for key, path in exports.items():
            print(f"  {key}: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
