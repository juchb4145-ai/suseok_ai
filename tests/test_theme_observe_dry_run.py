import json
from pathlib import Path

from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.strategy.models import Candidate
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading.theme_engine.stock_snapshot import snapshot_from_dict


TICKS = Path("tests/fixtures/theme_engine/furiosa_ticks.json")


def test_observe_dry_run_fields_are_recorded_without_gate_decision_mutation(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
    runtime.start()
    for tick in json.loads(TICKS.read_text(encoding="utf-8")):
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
