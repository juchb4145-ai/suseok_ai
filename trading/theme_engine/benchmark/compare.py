from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from storage.db import TradingDatabase
from trading.theme_engine.benchmark.comparator import compare_theme_benchmarks
from trading.theme_engine.benchmark.internal_export import export_internal_theme_benchmark
from trading.theme_engine.benchmark.loader import load_external_theme_benchmark
from trading.theme_engine.benchmark.report import write_benchmark_reports
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import DynamicThemeEngineRuntime
from trading.theme_engine.sources.fixture import FixtureThemeSource


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    internal_snapshot = _load_or_replay_internal(args, out_dir)
    trade_date = str(args.trade_date or internal_snapshot.get("trade_date") or "")
    if not trade_date:
        raise ValueError("trade_date is required")

    external_path = Path(args.external) if args.external else None
    if external_path is None or not external_path.exists():
        result = _skipped_result(internal_snapshot, external_path)
    else:
        external_snapshot = load_external_theme_benchmark(external_path)
        result = compare_theme_benchmarks(external_snapshot, internal_snapshot)
        result["source"] = external_snapshot.get("source", "")
        result["trade_date"] = trade_date
        result["summary"]["status"] = "COMPARED"
        result["external_top5_by_theme"] = _external_top5_by_theme(external_snapshot)
        result["internal_top5_by_theme"] = _internal_top5_by_theme(internal_snapshot)
    write_benchmark_reports(result, out_dir, trade_date)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare internal replay and external theme benchmark snapshots.")
    parser.add_argument("--internal", help="Existing internal benchmark snapshot JSON.")
    parser.add_argument("--fixture", help="Fixture JSON to replay into an internal benchmark snapshot.")
    parser.add_argument("--db", help="Fresh SQLite database path for fixture replay.")
    parser.add_argument("--trade-date", help="Trade date for replay/report, YYYY-MM-DD.")
    parser.add_argument("--external", help="External benchmark JSON or CSV snapshot. Missing/omitted file skips comparison.")
    parser.add_argument("--out", required=True, help="Output report directory.")
    return parser.parse_args(argv)


def _load_or_replay_internal(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    if args.fixture:
        if not args.db:
            raise ValueError("--db is required with --fixture")
        if not args.trade_date:
            raise ValueError("--trade-date is required with --fixture")
        snapshot = _replay_fixture(Path(args.fixture), Path(args.db), args.trade_date)
        internal_path = out_dir / f"internal_{args.trade_date}.json"
        internal_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return snapshot
    if args.internal:
        return dict(json.loads(Path(args.internal).read_text(encoding="utf-8")))
    raise ValueError("--internal or --fixture is required")


def _replay_fixture(fixture_path: Path, db_path: Path, trade_date: str) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_existing_db(db_path)
    source = FixtureThemeSource(fixture_path)
    db = TradingDatabase(str(db_path))
    try:
        repository = ThemeEngineRepository(db)
        runtime = DynamicThemeEngineRuntime(repository)
        runtime.sync_source(source)
        ranked = runtime.score_fixture_ticks(source.mock_snapshots())
        return dict(export_internal_theme_benchmark(ranked, trade_date=trade_date))
    finally:
        db.close()


def _skipped_result(internal_snapshot: dict[str, Any], external_path: Path | None) -> dict[str, Any]:
    internal_themes = list(internal_snapshot.get("themes") or [])
    return {
        "source": "",
        "trade_date": internal_snapshot.get("trade_date", ""),
        "summary": {
            "status": "SKIPPED",
            "reason": "EXTERNAL_BENCHMARK_NOT_PROVIDED",
            "external_theme_count": 0,
            "internal_theme_count": len(internal_themes),
            "matched_theme_count": 0,
            "avg_member_jaccard": 0.0,
            "avg_top5_overlap": 0.0,
            "leader_match_rate": 0.0,
            "top10_theme_rank_overlap": 0.0,
            "top20_theme_rank_overlap": 0.0,
            "missing_external_theme_count": 0,
            "internal_only_theme_count": len(internal_themes),
            "external_path": str(external_path or ""),
        },
        "themes": [],
        "missing_external_themes": [],
        "internal_only_themes": [
            {
                "internal_theme_id": str(theme.get("theme_id") or ""),
                "internal_theme_name": str(theme.get("theme_name") or ""),
                "internal_rank": int(theme.get("rank") or 0),
                "mismatch_reasons": ["EXTERNAL_THEME_MISSING"],
            }
            for theme in internal_themes
        ],
        "alias_candidates": [],
        "internal_top5_by_theme": _internal_top5_by_theme(internal_snapshot),
    }


def _external_top5_by_theme(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(theme.get("external_theme_name") or ""): _top5_codes(theme)
        for theme in list(snapshot.get("themes") or [])
    }


def _internal_top5_by_theme(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(theme.get("theme_id") or ""): _top5_codes(theme)
        for theme in list(snapshot.get("themes") or [])
    }


def _top5_codes(theme: dict[str, Any]) -> list[str]:
    stocks = sorted(
        [dict(item) for item in list(theme.get("top_stocks") or []) if isinstance(item, dict)],
        key=lambda item: int(item.get("rank") or 0),
    )
    return [str(stock.get("stock_code") or "") for stock in stocks[:5] if str(stock.get("stock_code") or "")]


def _remove_existing_db(db_path: Path) -> None:
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
