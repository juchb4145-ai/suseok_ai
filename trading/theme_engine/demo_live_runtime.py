from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.source_sync import ThemeSourceSyncService
from trading.theme_engine.sources.fixture import FixtureThemeSource
from trading.theme_engine.stock_snapshot import snapshot_from_dict
from trading.theme_engine.ws.schemas import build_runtime_health_payload, build_theme_rank_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay fixture ticks through the realtime dynamic theme runtime.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--ticks", default="")
    parser.add_argument("--print-rank", action="store_true")
    parser.add_argument("--print-health", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = TradingDatabase(args.db)
    repo = ThemeEngineRepository(db)
    fixture = FixtureThemeSource(args.fixture)
    sync_service = ThemeSourceSyncService(repo, [fixture])
    sync_results = sync_service.sync_all_sources()

    runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
    runtime.start()
    ticks = _load_ticks(args.ticks, fixture)
    for item in ticks:
        runtime.on_stock_snapshot(snapshot_from_dict(item))
    runtime.recalculate_all_themes()

    show_rank = args.print_rank or not args.print_health
    show_health = args.print_health or not args.print_rank
    output = {
        "sync": [result.__dict__ for result in sync_results],
    }
    if show_rank:
        output["theme_rank"] = build_theme_rank_payload(runtime.get_latest_rank(20), top_n=20)
    if show_health:
        output["runtime_health"] = build_runtime_health_payload(runtime.health())
    print(json.dumps(output, ensure_ascii=False, indent=2))
    db.close()
    return 0


def _load_ticks(path: str, fixture: FixtureThemeSource) -> list[dict]:
    if path:
        return list(json.loads(Path(path).read_text(encoding="utf-8")))
    return fixture.mock_snapshots()


if __name__ == "__main__":
    raise SystemExit(main())
