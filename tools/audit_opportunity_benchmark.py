from __future__ import annotations

import argparse
import json
from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.opportunity_benchmark import OpportunityBenchmarkService


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/export Opportunity Benchmark Collector v1 reports.")
    parser.add_argument("--db", default="data/trader.sqlite3")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--strict-only", action="store_true")
    parser.add_argument("--source-cutoff-at", default="")
    parser.add_argument("--rebuild-reason", default="operator audit")
    args = parser.parse_args()

    db = TradingDatabase(args.db)
    try:
        report = OpportunityBenchmarkService(db).build_report(
            trade_date=args.trade_date,
            report_state="FINAL" if args.final else "LIVE_PREVIEW",
            persist=bool(args.rebuild),
            export=bool(args.export),
            strict_only=bool(args.strict_only),
            source_cutoff_at=args.source_cutoff_at or datetime.now().replace(microsecond=0),
            rebuild_reason=args.rebuild_reason if args.rebuild else "",
        )
        print(json.dumps(_summary(report), ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        db.close()


def _summary(report: dict) -> dict:
    keys = [
        "schema_version",
        "report_id",
        "trade_date",
        "report_state",
        "qualification_status",
        "strict_sample_eligible",
        "source_batch_count",
        "complete_batch_count",
        "partial_batch_count",
        "source_error_batch_count",
        "observation_count",
        "unique_code_count",
        "episode_count",
        "candidate_captured_episode_count",
        "candidate_not_captured_episode_count",
        "candidate_capture_rate",
        "exact_label_count",
        "sampled_label_count",
        "insufficient_label_count",
        "invariant_violation_count",
        "build_ms",
    ]
    data = {key: report.get(key) for key in keys}
    if report.get("exported"):
        data["exported"] = report.get("exported")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
