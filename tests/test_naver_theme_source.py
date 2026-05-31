from storage.db import TradingDatabase
from tests.theme_naver_helpers import LocalNaverSession, NAVER_DETAIL_DIR, NAVER_LIST_HTML, naver_source
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import (
    NAVER_THEME_SOURCE_NAME,
    parse_theme_detail,
    parse_theme_list,
)


def test_parse_naver_theme_list_collects_names_and_detail_numbers_only():
    html = NAVER_LIST_HTML.read_text(encoding="utf-8")
    themes = parse_theme_list(html)

    assert themes[0].source_theme_id == "576"
    assert themes[0].source_theme_name == "퓨리오사AI"
    assert "29.96" not in themes[0].__dict__.values()


def test_parse_naver_theme_detail_collects_members_and_reason_without_numeric_market_fields():
    html = (NAVER_DETAIL_DIR / "detail_576.html").read_text(encoding="utf-8")
    members = parse_theme_detail(html)

    assert [member.stock_code for member in members[:3]] == ["000001", "000002", "000003"]
    assert members[0].stock_name == "MOCK-TSINV"
    assert members[0].reason == "퓨리오사AI 투자 이력 부각."
    assert all("29.96" not in member.__dict__.values() for member in members)
    assert all("12,350,164" not in member.__dict__.values() for member in members)


def test_naver_source_sync_persists_universe_and_purges_retired_sources(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.conn.execute(
        """
        INSERT INTO theme_member_evidence(theme_id, stock_code, stock_name, source, evidence_type, relation_type, reason, confidence)
        VALUES ('legacy', '999999', 'LEGACY', 'kiwoom', 'source_member', 'same_industry', 'old', 1.0)
        """
    )
    repo.conn.execute(
        """
        INSERT INTO theme_membership_current(theme_id, stock_code, stock_name, membership_score, relation_type, source_count, active, trade_eligible)
        VALUES ('legacy', '999999', 'LEGACY', 1.0, 'same_industry', 1, 1, 1)
        """
    )
    db.conn.commit()
    source = naver_source()
    service = ThemeSourceSyncService(repo, [source])

    result = service.sync_source(NAVER_THEME_SOURCE_NAME, replace=True, purge_sources=RETIRED_THEME_SOURCE_NAMES)

    assert result.status == "success"
    assert result.theme_count == 1
    assert result.member_count == 5
    assert repo.latest_source_sync_run(NAVER_THEME_SOURCE_NAME).status == "success"
    assert repo.list_member_evidence("legacy") == []
    assert repo.get_members_by_theme("legacy") == []
    assert {member.stock_code for member in repo.get_members_by_theme("furiosa_ai")} >= {"000001", "000005"}
    assert source.fetch_themes()[0].raw_payload == {
        "detail_no": "576",
        "detail_url": "https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no=576",
        "policy": "universe_only",
    }
    db.close()


def test_naver_source_uses_supplied_session():
    session = LocalNaverSession()
    source = naver_source()
    source.session = session

    themes = source.fetch_themes()

    assert themes[0].source_theme_name == "퓨리오사AI"
    assert session.urls
