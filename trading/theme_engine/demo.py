from __future__ import annotations

import argparse
import json

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import DynamicThemeEngineRuntime
from trading.theme_engine.sources.fixture import FixtureThemeSource
from trading.theme_engine.ws.schemas import build_theme_rank_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dynamic theme engine fixture demo.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--fixture", required=True, help="Theme engine JSON fixture path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = TradingDatabase(args.db)
    source = FixtureThemeSource(args.fixture)
    runtime_repository = ThemeEngineRepository(db)
    runtime = DynamicThemeEngineRuntime(runtime_repository)
    runtime.sync_source(source)
    runtime.score_fixture_ticks(source.mock_snapshots())
    payload = build_theme_rank_payload(runtime_repository.get_latest_theme_rank(20), top_n=20)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
