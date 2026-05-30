from pathlib import Path

from storage.db import TradingDatabase
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver
from trading.theme_engine.sources.fixture import FixtureThemeSource


FIXTURE = Path("tests/fixtures/theme_engine/furiosa_ai.json")


def test_fixture_evidence_builds_multi_source_membership(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    service = ThemeEvidenceService(repo, ThemeCanonicalResolver(repo))

    service.sync_source(FixtureThemeSource(FIXTURE))
    memberships = ThemeMembershipBuilder(repo).build_all_current_memberships()

    leader = next(item for item in memberships if item.stock_code == "000001")
    rumor = next(item for item in memberships if item.stock_code == "000005")
    assert leader.source_count == 3
    assert leader.membership_score > rumor.membership_score
    assert leader.trade_eligible is True
    assert rumor.trade_eligible is False
    db.close()


def test_duplicate_evidence_updates_in_place(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    service = ThemeEvidenceService(repo, ThemeCanonicalResolver(repo))
    source = FixtureThemeSource(FIXTURE)

    service.sync_source(source)
    before = repo.list_member_evidence("furiosa_ai", "000001")
    service.sync_source(source)
    after = repo.list_member_evidence("furiosa_ai", "000001")

    assert len(after) == len(before)
    assert max(item.confidence for item in after) >= max(item.confidence for item in before)
    db.close()
