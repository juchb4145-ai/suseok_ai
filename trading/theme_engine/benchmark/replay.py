from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.benchmark.internal_export import export_internal_theme_benchmark
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import DynamicThemeEngineRuntime
from trading.theme_engine.sources.fixture import FixtureThemeSource


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fixture_path = Path(args.fixture)
    db_path = Path(args.db)
    out_path = Path(args.out)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_existing_db(db_path)
    source = FixtureThemeSource(fixture_path)
    db = TradingDatabase(str(db_path))
    try:
        repository = ThemeEngineRepository(db)
        runtime = DynamicThemeEngineRuntime(repository)
        runtime.sync_source(source)
        ranked = runtime.score_fixture_ticks(source.mock_snapshots())
        snapshot = export_internal_theme_benchmark(ranked, trade_date=args.trade_date)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        db.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a theme fixture into an internal benchmark snapshot.")
    parser.add_argument("--fixture", required=True, help="Path to the theme fixture JSON file.")
    parser.add_argument("--db", required=True, help="SQLite database path to create fresh for replay.")
    parser.add_argument("--trade-date", required=True, help="Trade date for the benchmark snapshot, YYYY-MM-DD.")
    parser.add_argument("--out", required=True, help="Output JSON path.")
    return parser.parse_args(argv)


def _remove_existing_db(db_path: Path) -> None:
    for path in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
