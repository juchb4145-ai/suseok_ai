from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import TradingDatabase
from trading.strategy.market_relative_strength_shadow import ACTION_TYPE
from trading_app.intraday_outcomes import IntradayOutcomeConfig, IntradayOutcomeLabeler
from trading_app.market_relative_strength_outcomes import (
    MarketRelativeStrengthOutcomeAnalyzer,
    MarketRelativeStrengthOutcomeConfig,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the split-market relative-strength shadow validation report."
    )
    parser.add_argument("--db", default=os.getenv("TRADING_DB_PATH", "data/trader.sqlite3"), help="SQLite DB path")
    parser.add_argument("--trade-date", default="", help="YYYY-MM-DD, today, or empty for all")
    parser.add_argument("--from-date", default="", help="Reserved report window start date")
    parser.add_argument("--to-date", default="", help="Reserved report window end date")
    parser.add_argument("--scenario", default="", help="Filter by shadow scenario")
    parser.add_argument("--market-side", default="", help="Filter by KOSPI/KOSDAQ")
    parser.add_argument("--horizons", default="300,600,1200", help="Comma-separated horizons in seconds")
    parser.add_argument("--rebuild-outcomes", action="store_true", help="Rebuild shadow outcomes before reporting")
    parser.add_argument("--force", action="store_true", help="Overwrite existing outcomes during rebuild")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--export-all", action="store_true", help="Export JSON/CSV/Markdown report artifacts")
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()

    trade_date = _normalize_trade_date(args.trade_date)
    horizons = _parse_horizons(args.horizons)
    db = TradingDatabase(str(args.db))
    rebuild_summary: dict[str, Any] = {"status": "SKIPPED", "persisted_count": 0}
    try:
        if args.rebuild_outcomes:
            rebuild_summary = _rebuild_shadow_outcomes(
                db,
                trade_date=trade_date,
                horizons_sec=horizons,
                force=bool(args.force),
                limit=max(1, int(args.limit or 10000)),
            )
        analyzer = MarketRelativeStrengthOutcomeAnalyzer(
            db,
            config=MarketRelativeStrengthOutcomeConfig(horizons_sec=tuple(horizons)),
        )
        report = analyzer.build_report(
            trade_date=trade_date or None,
            from_date=args.from_date or None,
            to_date=args.to_date or None,
            scenario=args.scenario or None,
            market_side=args.market_side or None,
            limit=max(1, int(args.limit or 10000)),
        )
        exports = analyzer.export_all(report) if args.export_all else {}
    finally:
        db.close()

    payload = {
        "status": report.get("status"),
        "report_name": report.get("report_name"),
        "trade_date": report.get("trade_date"),
        "generated_at": report.get("generated_at"),
        "filters": report.get("filters") or {},
        "summary": report.get("summary") or {},
        "recommendations": report.get("recommendations") or {},
        "rebuild_outcomes": rebuild_summary,
        "exports": exports,
        "safety": {
            "analysis_only": True,
            "action_type": ACTION_TYPE,
            "changes_entry_status": False,
            "creates_orders": False,
        },
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        summary = payload["summary"]
        recommendations = payload["recommendations"]
        print(f"Split Market Relative Strength Outcomes {payload['trade_date'] or 'all'} [{payload['status']}]")
        print(f"- shadow candidates: {summary.get('shadow_candidate_count', 0)} / labeled: {summary.get('labeled_count', 0)}")
        print(f"- 10m avg MFE/MAE: {summary.get('avg_mfe_10m', 0)} / {summary.get('avg_mae_10m', 0)}")
        print(f"- 10m edge/risk: {summary.get('shadow_edge_rate_10m', 0)} / {summary.get('shadow_risk_case_rate_10m', 0)}")
        print(f"- recommendation: {recommendations.get('current_recommendation', 'NO_DATA')}")
        print(f"- rebuild: {rebuild_summary.get('status')} persisted={rebuild_summary.get('persisted_count', 0)}")
        if exports:
            print("- exports:")
            for key, path in exports.items():
                print(f"  {key}: {path}")
    return 0


def _rebuild_shadow_outcomes(
    db: TradingDatabase,
    *,
    trade_date: str,
    horizons_sec: list[int],
    force: bool,
    limit: int,
) -> dict[str, Any]:
    now = datetime.now().replace(microsecond=0)
    labeler = IntradayOutcomeLabeler(
        db,
        config=IntradayOutcomeConfig(enabled=True, horizons_sec=tuple(horizons_sec), max_batch_size=limit),
    )
    events = db.list_strategy_decision_events(
        trade_date=trade_date or None,
        action_type=ACTION_TYPE,
        limit=limit,
    )
    existing = set()
    if not force:
        for outcome in db.list_strategy_decision_outcomes(
            trade_date=trade_date or None,
            action_type=ACTION_TYPE,
            limit=max(limit * len(horizons_sec), 1),
        ):
            existing.add((str(outcome.get("decision_id") or ""), int(outcome.get("horizon_sec") or 0)))
    outcomes: list[dict[str, Any]] = []
    for event in events:
        decision_at = _parse_dt(event.get("decision_at"))
        for horizon_sec in horizons_sec:
            key = (str(event.get("decision_id") or ""), int(horizon_sec))
            if not force and key in existing:
                continue
            if decision_at and decision_at + timedelta(seconds=int(horizon_sec)) > now:
                continue
            decision = dict(event)
            decision["horizon_sec"] = int(horizon_sec)
            outcomes.append(labeler.build_outcome_for_decision(decision, int(horizon_sec), now=now))
    persisted = labeler.persist_outcomes(outcomes, force=force) if outcomes else 0
    return {
        "status": "OK",
        "trade_date": trade_date,
        "horizons_sec": horizons_sec,
        "force": bool(force),
        "event_count": len(events),
        "outcome_count": len(outcomes),
        "persisted_count": persisted,
    }


def _normalize_trade_date(value: str) -> str:
    text = str(value or "").strip()
    if text.lower() == "today":
        return datetime.now().date().isoformat()
    return text


def _parse_horizons(value: str) -> list[int]:
    horizons: list[int] = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        horizons.append(max(1, int(raw)))
    return horizons or [300, 600, 1200]


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
