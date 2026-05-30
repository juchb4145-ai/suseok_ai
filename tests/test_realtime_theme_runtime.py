from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.source_sync import ThemeSourceSyncService
from trading.theme_engine.sources.fixture import FixtureThemeSource
from trading.theme_engine.stock_snapshot import snapshot_from_dict


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def test_realtime_theme_runtime_recalculates_rank_and_health(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    source = FixtureThemeSource(FIXTURE)
    ThemeSourceSyncService(repo, [source]).sync_all_sources()
    runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)

    runtime.start()
    for tick in source.mock_snapshots():
        runtime.on_stock_snapshot(snapshot_from_dict(tick))

    rank = runtime.get_latest_rank()
    health = runtime.health()

    assert runtime.running is True
    assert rank[0].theme_id == "furiosa_ai"
    assert health["data_ready"] is True
    assert repo.latest_activity_snapshots(1)
    assert any(payload["type"] == "theme_rank" for payload in runtime.broadcaster.last_payloads)
    runtime.stop()
    assert runtime.running is False
    db.close()
