from tests.theme_naver_helpers import naver_source
from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_base import BaseThemeSource
from trading.theme_engine.source_sync import ThemeSourceSyncService


class FailingSource(BaseThemeSource):
    source_name = "failing_fixture"

    def fetch_themes(self):
        raise RuntimeError("boom")

    def fetch_members(self, source_theme):
        return []


def test_source_sync_service_records_success_and_failure(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    service = ThemeSourceSyncService(repo, [naver_source(), FailingSource()])

    results = service.sync_all_sources()

    assert [result.source for result in results] == ["naver_theme_universe", "failing_fixture"]
    assert results[0].status == "success"
    assert results[1].status == "failed"
    assert repo.latest_source_sync_run("naver_theme_universe").member_count > 0
    assert repo.latest_source_sync_run("failing_fixture").error_count == 1
    db.close()
