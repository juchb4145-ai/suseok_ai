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
from trading.strategy.champion_outcome_validator import ChampionOutcomeValidatorService


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Champion Outcome Validator v1 reports.")
    parser.add_argument("--db", default="data/trader.sqlite3", help="SQLite DB path.")
    parser.add_argument("--trade-date", default="", help="Single trade date alias for --trade-date-from.")
    parser.add_argument("--trade-date-from", default="", help="Start trade date in YYYY-MM-DD.")
    parser.add_argument("--trade-date-to", default="", help="End trade date in YYYY-MM-DD. Defaults to start date.")
    parser.add_argument("--strict-only", action="store_true", help="Restrict detailed samples to strict linked signals.")
    parser.add_argument("--finalize", action="store_true", help="Persist as FINAL append-only revision.")
    parser.add_argument("--export", action="store_true", help="Export JSON/Markdown/CSV report files.")
    parser.add_argument("--source-cutoff-at", default="", help="Optional ISO timestamp cutoff.")
    parser.add_argument("--rebuild-reason", default="operator audit", help="Reason stored in the report metadata.")
    args = parser.parse_args()

    start = args.trade_date_from or args.trade_date
    if not start:
        parser.error("--trade-date-from or --trade-date is required")
    end = args.trade_date_to or start
    cutoff = datetime.fromisoformat(args.source_cutoff_at) if args.source_cutoff_at else datetime.now().replace(microsecond=0)

    db = TradingDatabase(args.db)
    try:
        report = ChampionOutcomeValidatorService(db).build_report(
            trade_date_from=start,
            trade_date_to=end,
            report_state="FINAL" if args.finalize else "LIVE_PREVIEW",
            persist=True,
            export=bool(args.export),
            strict_only=bool(args.strict_only),
            source_cutoff_at=cutoff,
            rebuild_reason=args.rebuild_reason,
        )
        print(json.dumps(_summary(report), ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        db.close()


def _summary(report: dict) -> dict:
    valid = dict(report.get("valid_observe_metrics") or {})
    matched = dict(report.get("matched_signal_metrics") or {})
    discovery = dict(report.get("discovery_metrics") or {})
    context = dict(report.get("context_gate_metrics") or {})
    recommendation = dict(report.get("recommendation") or {})
    data = {
        "schema_version": report.get("schema_version"),
        "report_id": report.get("report_id"),
        "report_state": report.get("report_state"),
        "trade_date_from": report.get("trade_date_from"),
        "trade_date_to": report.get("trade_date_to"),
        "evidence_tier": report.get("evidence_tier"),
        "strict_labeled_signal_count": valid.get("strict_labeled_count"),
        "matched_labeled_signal_count": matched.get("strict_labeled_count"),
        "valid_trade_days": valid.get("valid_trade_days"),
        "controlled_opportunity_count": discovery.get("controlled_opportunity_count"),
        "controlled_opportunity_recall_5m": discovery.get("controlled_opportunity_recall_5m"),
        "avg_cost_adjusted_return": valid.get("avg_cost_adjusted_return"),
        "median_cost_adjusted_return": valid.get("median_cost_adjusted_return"),
        "target_first_rate": valid.get("target_first_rate"),
        "stop_first_rate": valid.get("stop_first_rate"),
        "context_false_block_candidate_rate": context.get("context_false_block_candidate_rate"),
        "primary_recommendation": recommendation.get("primary_recommendation"),
        "analysis_only": report.get("analysis_only", True),
        "auto_apply_allowed": report.get("auto_apply_allowed", False),
        "dry_run_auto_enable_allowed": report.get("dry_run_auto_enable_allowed", False),
        "order_safety": report.get("order_safety"),
        "warning_codes": report.get("warning_codes", []),
        "build_ms": report.get("build_ms"),
    }
    if report.get("exported"):
        data["exported"] = report.get("exported")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
