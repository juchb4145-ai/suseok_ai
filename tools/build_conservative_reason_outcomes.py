from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading_app.conservative_reason_outcomes import ConservativeReasonOutcomeAnalyzer


def main() -> int:
    parser = argparse.ArgumentParser(description="Build conservative reason outcome report from ThemeLab outcome observations.")
    parser.add_argument("--db", default="data/trading.sqlite3", help="SQLite DB path")
    parser.add_argument("--trade-date", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--export", action="store_true", help="Export JSON/CSV/Markdown under reports/conservative_reason_outcomes/{trade_date}")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args()

    trade_date = datetime.now().date().isoformat() if str(args.trade_date).lower() == "today" else str(args.trade_date)
    db = TradingDatabase(str(args.db))
    try:
        analyzer = ConservativeReasonOutcomeAnalyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=max(1, int(args.limit or 50000)))
        exports = analyzer.export_report(report, fmt="all") if args.export else {}
    finally:
        db.close()

    payload = {
        "status": report.get("status"),
        "trade_date": report.get("trade_date"),
        "generated_at": report.get("generated_at"),
        "summary": report.get("summary") or {},
        "top_missed_opportunity_reasons": list(report.get("top_missed_opportunity_reasons") or [])[:5],
        "top_good_block_reasons": list(report.get("top_good_block_reasons") or [])[:5],
        "review_for_small_entry": (report.get("review_for_small_entry") or {}).get("summary") or {},
        "exports": exports,
        "disclaimer_ko": report.get("disclaimer_ko") or "",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = payload["summary"]
        print(f"Conservative Reason Outcomes {payload['trade_date']} [{payload['status']}]")
        print(f"- events: {summary.get('event_count', 0)} / labeled: {summary.get('labeled_event_count', 0)}")
        print(f"- missed: {summary.get('missed_opportunity_count', 0)} ({summary.get('missed_opportunity_rate', 0)})")
        print(f"- good_block: {summary.get('good_block_count', 0)} ({summary.get('good_block_rate', 0)})")
        print(f"- risk_avoided: {summary.get('risk_avoided_count', 0)} ({summary.get('risk_avoided_rate', 0)})")
        print(f"- review_small_entry_candidates: {payload['review_for_small_entry'].get('candidate_count', 0)}")
        if exports:
            print("- exports:")
            for key, path in exports.items():
                print(f"  {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
