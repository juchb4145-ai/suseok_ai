from storage.db import TradingDatabase
from tests.theme_naver_helpers import naver_source
from trading.theme_engine.evidence import ThemeEvidenceService
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.resolver import ThemeCanonicalResolver


def test_naver_evidence_builds_universe_membership(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    service = ThemeEvidenceService(repo, ThemeCanonicalResolver(repo))

    service.sync_source(naver_source())
    memberships = ThemeMembershipBuilder(repo).build_all_current_memberships()

    leader = next(item for item in memberships if item.stock_code == "000001")
    member = next(item for item in memberships if item.stock_code == "000005")
    assert leader.source_count == 1
    assert leader.membership_score == member.membership_score
    assert leader.trade_eligible is True
    assert member.trade_eligible is True
    db.close()


def test_duplicate_evidence_updates_in_place(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    service = ThemeEvidenceService(repo, ThemeCanonicalResolver(repo))
    source = naver_source()

    service.sync_source(source)
    before = repo.list_member_evidence("furiosa_ai", "000001")
    service.sync_source(source)
    after = repo.list_member_evidence("furiosa_ai", "000001")

    assert len(after) == len(before)
    assert max(item.confidence for item in after) >= max(item.confidence for item in before)
    db.close()
