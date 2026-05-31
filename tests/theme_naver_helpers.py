from __future__ import annotations

from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import NAVER_THEME_SOURCE_NAME, NaverThemeUniverseSource


ROOT = Path(__file__).resolve().parents[1]
NAVER_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "naver_theme"
NAVER_LIST_HTML = NAVER_FIXTURE_DIR / "list.html"
NAVER_DETAIL_DIR = NAVER_FIXTURE_DIR


class LocalNaverResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None


class LocalNaverSession:
    def __init__(self, list_html: Path = NAVER_LIST_HTML, detail_dir: Path = NAVER_DETAIL_DIR) -> None:
        self.list_html = list_html
        self.detail_dir = detail_dir
        self.urls: list[str] = []

    def get(self, url: str, **_) -> LocalNaverResponse:
        self.urls.append(str(url))
        if "sise_group_detail.naver" in str(url):
            no = str(url).split("no=", 1)[1].split("&", 1)[0]
            path = self.detail_dir / f"detail_{no}.html"
        else:
            path = self.list_html
        return LocalNaverResponse(path.read_text(encoding="utf-8"))


def naver_source(max_pages: int = 1) -> NaverThemeUniverseSource:
    return NaverThemeUniverseSource(
        session=LocalNaverSession(),
        max_pages=max_pages,
        request_delay_sec=0,
    )


def repo_with_naver_fixture(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    source = naver_source()
    ThemeSourceSyncService(repo, [source]).sync_source(
        NAVER_THEME_SOURCE_NAME,
        replace=True,
        purge_sources=RETIRED_THEME_SOURCE_NAMES,
    )
    return db, repo
