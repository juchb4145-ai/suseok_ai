from storage.db import TradingDatabase
from trading.theme_engine.models import CanonicalTheme, SourceTheme, ThemeMemberEvidence, ThemeMembership
from trading.theme_engine.normalizer import normalize_theme_name
from trading.theme_engine.repository import ThemeEngineRepository


def test_repository_upserts_core_theme_tables(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)

    saved = repo.upsert_canonical_theme(CanonicalTheme("furiosa_ai", "퓨리오사AI", "퓨리오사AI"))
    repo.upsert_alias(saved.theme_id, "퓨리오사 AI")
    source = repo.upsert_source_theme(
        SourceTheme(
            source="naver_theme_universe",
            source_theme_id="576",
            source_theme_name="퓨리오사 AI",
            normalized_name=normalize_theme_name("퓨리오사 AI"),
            matched_theme_id=saved.theme_id,
            match_confidence=1.0,
        )
    )
    evidence = repo.add_member_evidence(
        ThemeMemberEvidence(
            saved.theme_id,
            "A000001",
            "MOCK-TSINV",
            "naver_theme_universe",
            relation_type="investor",
            confidence=0.9,
        )
    )
    membership = repo.upsert_current_membership(
        ThemeMembership(saved.theme_id, "000001", "MOCK-TSINV", 0.9, "investor", 1, True, True)
    )

    assert repo.get_canonical_theme(saved.theme_id).theme_id == "furiosa_ai"
    assert repo.find_alias("퓨리오사AI") == "furiosa_ai"
    assert source.matched_theme_id == "furiosa_ai"
    assert evidence.stock_code == "000001"
    assert membership.trade_eligible is True
    assert repo.get_themes_by_stock("000001")[0].theme_id == "furiosa_ai"
    assert repo.get_members_by_theme("furiosa_ai")[0].stock_code == "000001"
    db.close()


def test_new_db_does_not_create_legacy_theme_mappings_table(tmp_path):
    db = TradingDatabase(str(tmp_path / "fresh.sqlite3"))
    rows = db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {row["name"] for row in rows}
    assert "theme_mappings" not in names
    assert "canonical_themes" in names
    db.close()


def test_existing_theme_mappings_is_archived_and_dropped(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = TradingDatabase(str(path))
    db.conn.execute("CREATE TABLE theme_mappings(id INTEGER PRIMARY KEY, code TEXT)")
    db.conn.commit()
    db.close()

    migrated = TradingDatabase(str(path))
    names = {
        row["name"]
        for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    assert "theme_mappings" not in names
    assert "legacy_theme_mappings_archive" in names
    migrated.close()
