from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.naver_local_session import LocalNaverFixtureSession
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import DynamicThemeEngineRuntime
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import NAVER_THEME_SOURCE_NAME, NaverThemeUniverseSource
from trading.theme_engine.ws.schemas import build_theme_rank_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Naver universe theme engine demo.")
    parser.add_argument("--db", required=True, help="SQLite database path.")
    parser.add_argument("--list-html", default="", help="Optional local Naver theme list HTML fixture.")
    parser.add_argument("--detail-dir", default="", help="Directory containing detail_{no}.html fixtures.")
    parser.add_argument("--ticks", default="", help="Optional realtime tick JSON fixture.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = TradingDatabase(args.db)
    runtime_repository = ThemeEngineRepository(db)
    session = LocalNaverFixtureSession(args.list_html, args.detail_dir) if args.list_html and args.detail_dir else None
    source = NaverThemeUniverseSource(session=session, max_pages=1 if session else 20, request_delay_sec=0 if session else 0.1)
    ThemeSourceSyncService(runtime_repository, [source]).sync_source(
        NAVER_THEME_SOURCE_NAME,
        replace=True,
        purge_sources=RETIRED_THEME_SOURCE_NAMES,
    )
    runtime = DynamicThemeEngineRuntime(runtime_repository)
    if args.ticks:
        runtime.score_ticks(json.loads(Path(args.ticks).read_text(encoding="utf-8")))
    payload = build_theme_rank_payload(runtime_repository.get_latest_theme_rank(20), top_n=20)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
