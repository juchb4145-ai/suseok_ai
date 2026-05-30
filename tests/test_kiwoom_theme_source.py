from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import ThemeSourceSyncService
from trading.theme_engine.sources.kiwoom import KiwoomThemeSource, parse_theme_group_list, parse_theme_member_codes


class MockKiwoomClient:
    def __init__(self) -> None:
        self.group_calls = 0

    def GetThemeGroupList(self):
        self.group_calls += 1
        return "100^반도체;200^퓨리오사AI"

    def GetThemeGroupCode(self, theme_id):
        return {
            "100": "A005930^삼성전자;000660^SK하이닉스",
            "200": "A000001^MOCK-TSINV;2^MOCK-DSC",
        }[theme_id]

    def GetMasterCodeName(self, code):
        return {"005930": "삼성전자", "000660": "SK하이닉스", "000001": "MOCK-TSINV", "000002": "MOCK-DSC"}[code]


def test_parse_theme_group_list_variants():
    assert parse_theme_group_list("100^반도체;200^AI") == [("100", "반도체"), ("200", "AI")]
    assert parse_theme_group_list([{"theme_id": "300", "theme_name": "로봇"}]) == [("300", "로봇")]


def test_parse_theme_member_codes_normalizes_a_prefix_and_short_codes():
    assert parse_theme_member_codes("A005930^삼성전자;5930^짧은코드") == ["005930"]
    assert parse_theme_member_codes(["A000001", {"stock_code": "2"}]) == ["000001", "000002"]


def test_kiwoom_source_sync_persists_catalog_and_evidence_without_duplicates(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    source = KiwoomThemeSource(MockKiwoomClient())
    service = ThemeSourceSyncService(repo, [source])

    first = service.sync_source("kiwoom")
    second = service.sync_source("kiwoom")

    assert first.theme_count == 2
    assert first.member_count == 4
    assert second.error_count == 0
    assert len(repo.list_member_evidence("furiosa_ai", "000001")) == 1
    assert repo.latest_source_sync_run("kiwoom").status == "success"
    db.close()
