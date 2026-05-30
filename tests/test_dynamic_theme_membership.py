from storage.db import TradingDatabase
from trading.theme_engine.membership import ThemeMembershipBuilder
from trading.theme_engine.models import CanonicalTheme, ThemeMemberEvidence
from trading.theme_engine.repository import ThemeEngineRepository


def test_relation_weights_and_source_count_drive_membership_score(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(CanonicalTheme("furiosa_ai", "퓨리오사AI", "퓨리오사AI"))
    repo.add_member_evidence(ThemeMemberEvidence("furiosa_ai", "000001", "MOCK-TSINV", "src1", relation_type="investor", confidence=0.9))
    repo.add_member_evidence(ThemeMemberEvidence("furiosa_ai", "000001", "MOCK-TSINV", "src2", relation_type="partner", confidence=0.85))
    repo.add_member_evidence(ThemeMemberEvidence("furiosa_ai", "000005", "MOCK-XPERIX", "src1", relation_type="rumor", confidence=0.3))

    memberships = ThemeMembershipBuilder(repo).build_current_membership("furiosa_ai")
    high = next(item for item in memberships if item.stock_code == "000001")
    weak = next(item for item in memberships if item.stock_code == "000005")

    assert high.membership_score >= 0.65
    assert high.trade_eligible is True
    assert weak.membership_score < high.membership_score
    assert weak.trade_eligible is False
    db.close()
