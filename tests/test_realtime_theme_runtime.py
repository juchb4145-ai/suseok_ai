import json
from pathlib import Path

from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.stock_snapshot import snapshot_from_dict


TICKS = Path("tests/fixtures/theme_engine/furiosa_ticks.json")


def test_realtime_theme_runtime_recalculates_rank_and_health(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)

    runtime.start()
    for tick in json.loads(TICKS.read_text(encoding="utf-8")):
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
