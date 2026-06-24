from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading.strategy.candidate_funnel import TradingDayQualificationService


def main() -> int:
    parser = argparse.ArgumentParser(description="Build trading day qualification report.")
    parser.add_argument("--db", required=True, help="SQLite DB path.")
    parser.add_argument("--trade-date", required=True, help="Trade date in YYYY-MM-DD.")
    parser.add_argument("--as-of", default="", help="Optional ISO timestamp cutoff.")
    parser.add_argument("--finalize", action="store_true", help="Create FINAL report revision.")
    parser.add_argument("--export", action="store_true", help="Export JSON/Markdown/CSV report files.")
    parser.add_argument("--rebuild-reason", default="cli_audit", help="Reason recorded for rebuild/finalize.")
    args = parser.parse_args()

    db = TradingDatabase(args.db)
    try:
        report = TradingDayQualificationService(db).build_report(
            trade_date=args.trade_date,
            as_of=datetime.fromisoformat(args.as_of) if args.as_of else None,
            report_state="FINAL" if args.finalize else "LIVE_PREVIEW",
            finalize=args.finalize,
            persist=True,
            export=args.export,
            rebuild_reason=args.rebuild_reason,
        )
        print(json.dumps(_summary(report), ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


def _summary(report: dict) -> dict:
    return {
        "report_id": report.get("report_id"),
        "trade_date": report.get("trade_date"),
        "report_state": report.get("report_state"),
        "qualification_status": report.get("qualification_status"),
        "qualification_score": report.get("qualification_score"),
        "strict_sample_eligible": report.get("strict_sample_eligible"),
        "no_trade_classification": dict(report.get("no_trade_classification") or {}).get("classification"),
        "revision": report.get("revision"),
        "exported": report.get("exported", {}),
    }


if __name__ == "__main__":
    raise SystemExit(main())
