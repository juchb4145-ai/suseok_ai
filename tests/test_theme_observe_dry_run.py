from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import Candidate
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.source_sync import ThemeSourceSyncService
from trading.theme_engine.sources.fixture import FixtureThemeSource
from trading.theme_engine.stock_snapshot import snapshot_from_dict


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def test_observe_dry_run_fields_are_recorded_without_gate_decision_mutation(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    source = FixtureThemeSource(FIXTURE)
    ThemeSourceSyncService(repo, [source]).sync_all_sources()
    runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
    runtime.start()
    for tick in source.mock_snapshots():
        runtime.on_stock_snapshot(snapshot_from_dict(tick))

    provider = DynamicThemeContextProvider(repo)
    enriched = provider.enrich_candidate(Candidate(code="000001"))
    missing = provider.enrich_candidate(Candidate(code="009999"))

    assert enriched.metadata["dynamic_theme_status"] == "ready"
    assert enriched.metadata["active_theme_id"] == "furiosa_ai"
    assert enriched.metadata["theme_gate_dry_run_status"] == "ready"
    assert "strategy_eligible" not in enriched.metadata
    assert missing.metadata["dynamic_theme_status"] == "blocked"
    assert missing.metadata["theme_gate_dry_run_reason"] == "NO_ACTIVE_THEME"
    db.close()
