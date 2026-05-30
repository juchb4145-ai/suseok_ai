import json
from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.scorer import ThemeScoringEngine
from trading.theme_engine.sources.fixture import FixtureThemeSource
from trading.theme_engine.stock_snapshot import snapshot_from_dict


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def _repo_with_fixture(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    ThemeEvidenceService(repo, ThemeCanonicalResolver(repo)).sync_source(FixtureThemeSource(FIXTURE))
    ThemeMembershipBuilder(repo).build_all_current_memberships()
    return db, repo


def test_scorer_calculates_breadth_leader_and_clamps_score(tmp_path):
    db, repo = _repo_with_fixture(tmp_path)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    memberships = repo.get_members_by_theme("furiosa_ai")
    snapshots = [snapshot_from_dict(item) for item in payload["mock_ticks"]]

    ranked = ThemeScoringEngine(repo).score_and_rank([("furiosa_ai", "퓨리오사AI", memberships)], snapshots)

    item = ranked[0]
    assert 0 <= item.theme_score <= 100
    assert item.breadth > 0.5
    assert item.leader_code == "000001"
    assert "LEADER_ONLY_THEME" not in item.details["reason_codes"]
    db.close()


def test_scorer_flags_leader_only_theme(tmp_path):
    db, repo = _repo_with_fixture(tmp_path)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    memberships = repo.get_members_by_theme("furiosa_ai")
    snapshots = [snapshot_from_dict(item) for item in payload["leader_only_ticks"]]

    item = ThemeScoringEngine().score_theme("furiosa_ai", "퓨리오사AI", memberships, snapshots)

    assert "LEADER_ONLY_THEME" in item.details["reason_codes"]
    assert item.leader_gap >= 3.0
    db.close()
