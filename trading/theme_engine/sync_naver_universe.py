from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import DEFAULT_NAVER_THEME_URL, NAVER_THEME_SOURCE_NAME, NaverThemeUniverseSource


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Naver Finance theme universe memberships.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--base-url", default=DEFAULT_NAVER_THEME_URL, help="Naver theme list URL.")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum list pages to fetch.")
    parser.add_argument("--timeout-sec", type=float, default=5.0)
    parser.add_argument("--request-delay-sec", type=float, default=0.1)
    parser.add_argument("--no-replace", action="store_true", help="Append/update without purging prior source evidence.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = TradingDatabase(args.db)
    try:
        repo = ThemeEngineRepository(db)
        source = NaverThemeUniverseSource(
            base_url=args.base_url,
            timeout_sec=args.timeout_sec,
            max_pages=args.max_pages,
            request_delay_sec=args.request_delay_sec,
        )
        result = ThemeSourceSyncService(repo, [source]).sync_source(
            NAVER_THEME_SOURCE_NAME,
            replace=not args.no_replace,
            purge_sources=RETIRED_THEME_SOURCE_NAMES,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status == "success" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
