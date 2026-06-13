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
from trading_app.shadow_small_entry_promotion import ShadowSmallEntryPromotionAnalyzer


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Shadow Small Entry Promotion report.")
    parser.add_argument("--db", default="data/trader.sqlite3", help="SQLite DB path")
    parser.add_argument("--trade-date", default="today", help="YYYY-MM-DD or today")
    parser.add_argument("--format", default="all", choices=["json", "csv", "md", "markdown", "all"])
    parser.add_argument("--json", action="store_true", help="Print report JSON to stdout")
    parser.add_argument("--export", action="store_true", help="Write report files under reports/shadow_small_entry_promotion/{trade_date}")
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args()

    trade_date = datetime.now().date().isoformat() if args.trade_date == "today" else args.trade_date
    db = TradingDatabase(str(Path(args.db)))
    try:
        analyzer = ShadowSmallEntryPromotionAnalyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=args.limit)
        exports = analyzer.export_all(report) if args.export or args.format == "all" else {}
        if args.export and args.format not in {"all"}:
            target = analyzer.report_root / str(report.get("trade_date") or trade_date)
            stem = f"shadow_small_entry_promotion_{report.get('trade_date') or trade_date}"
            fmt = "md" if args.format == "markdown" else args.format
            if fmt == "csv":
                exports = {"csv": str(analyzer.export_csv(report, target / f"{stem}.csv"))}
            elif fmt == "md":
                exports = {"md": str(analyzer.export_markdown(report, target / f"{stem}.md"))}
            else:
                exports = {"json": str(analyzer.export_json(report, target / f"{stem}.json"))}
        if args.json:
            print(json.dumps({**report, "exports": exports}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        else:
            summary = report.get("summary") or {}
            print(f"status={report.get('status')} trade_date={report.get('trade_date') or trade_date}")
            print(f"candidates={summary.get('candidate_count', 0)} observe_only={summary.get('observe_only_count', 0)} promoted={summary.get('promoted_count', 0)} blocked={summary.get('blocked_count', 0)}")
            if exports:
                print("exports=" + json.dumps(exports, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
