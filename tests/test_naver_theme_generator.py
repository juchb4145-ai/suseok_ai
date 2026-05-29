import csv
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.generate_naver_theme_mappings import (
    COLUMNS,
    CrawlReport,
    _print_report,
    crawl_naver_themes,
    normalize_code,
    parse_args,
    parse_theme_list,
    parse_theme_members,
)


def test_parse_theme_list_extracts_rates_ranks_and_leading_text():
    groups = parse_theme_list(
        """
        <table class="type_1 theme">
          <tr>
            <td><a href="/sise/sise_group_detail.naver?type=theme&no=101">반도체</a></td>
            <td>+10.25%</td>
            <td>+3.50%</td>
            <td>4</td><td>0</td><td>1</td>
            <td>삼성전자</td><td>SK하이닉스</td>
          </tr>
        </table>
        """,
        page=2,
    )

    assert len(groups) == 1
    assert groups[0].no == "101"
    assert groups[0].name == "반도체"
    assert groups[0].change_rate == "+10.25%"
    assert groups[0].recent_3days_change_rate == "+3.50%"
    assert "삼성전자" in groups[0].leading_text
    assert groups[0].page == 2


def test_parse_theme_members_normalizes_codes_and_dedupes():
    members = parse_theme_members(
        """
        <table class="type_5">
          <tr><td><a href="/item/main.naver?code=A005930">삼성전자</a></td></tr>
          <tr><td><a href="/item/main.naver?code=005930">삼성전자</a></td></tr>
          <tr><td><a href="/item/main.naver?code=000660">SK하이닉스</a></td></tr>
        </table>
        """
    )

    assert [(member.code, member.name, member.rank) for member in members] == [
        ("005930", "삼성전자", 1),
        ("000660", "SK하이닉스", 2),
    ]
    assert normalize_code("A005930") == "005930"


def test_crawl_naver_themes_generates_review_csv_from_fake_pages(tmp_path):
    session = FakeNaverSession()
    output = tmp_path / "theme_mappings_auto.csv"
    sleeps = []

    report = crawl_naver_themes(
        start_url=None,
        output=output,
        overwrite=False,
        max_pages=2,
        max_themes=None,
        delay_ms=1200,
        timeout_sec=5,
        default_enabled=1,
        include_keywords=None,
        ranking_source="combined",
        max_market_pages=3,
        now=_fixed_now(),
        session=session,
        sleeper=lambda seconds: sleeps.append(seconds),
    )

    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert list(rows[0]) == COLUMNS
    assert [(row["theme_id"], row["code"]) for row in rows] == [
        ("naver_101", "000660"),
        ("naver_101", "005380"),
        ("naver_101", "005930"),
        ("naver_101", "123456"),
        ("naver_102", "277810"),
        ("naver_102", "999999"),
    ]

    by_key = {(row["theme_id"], row["code"]): row for row in rows}
    samsung = by_key[("naver_101", "005930")]
    assert samsung["strategy_profile"] == "SEMICONDUCTOR_SIGNAL_PROFILE"
    assert samsung["is_signal_stock"] == "1"
    assert samsung["is_large_cap"] == "1"
    assert samsung["is_leader_candidate"] == "1"
    assert samsung["base_priority"] == "100"
    assert "theme_change_rate=+10.00%" in samsung["memo"]
    assert "theme_recent_3days_change_rate=+3.00%" in samsung["memo"]
    assert "rank_change_rate=1" in samsung["memo"]
    assert "rank_recent3d=2" in samsung["memo"]

    hyundai = by_key[("naver_101", "005380")]
    assert hyundai["market"] == "KOSPI"
    assert hyundai["strategy_profile"] == "KOSPI_LEADER_PROFILE"
    assert hyundai["is_large_cap"] == "1"
    assert hyundai["base_priority"] == "70"

    kosdaq = by_key[("naver_101", "123456")]
    assert kosdaq["market"] == "KOSDAQ"
    assert kosdaq["strategy_profile"] == "KOSDAQ_THEME_PROFILE"
    assert kosdaq["base_priority"] == "60"

    robot = by_key[("naver_102", "277810")]
    assert robot["is_leader_candidate"] == "1"
    assert int(robot["base_priority"]) >= 80

    unknown = by_key[("naver_102", "999999")]
    assert unknown["market"] == "UNKNOWN"
    assert unknown["enabled"] == "0"
    assert "market_unresolved=1" in unknown["memo"]

    assert report.themes_found == 2
    assert report.themes_selected == 2
    assert report.rows_written == 6
    assert any(warning.startswith("MARKET_UNRESOLVED") for warning in report.warnings)
    assert any("field=change_rate" in url for url in session.urls)
    assert any("field=recent_3days_change_rate" in url for url in session.urls)
    assert sleeps


def test_crawl_naver_themes_filters_keywords_and_single_url(tmp_path):
    session = FakeNaverSession()

    report = crawl_naver_themes(
        start_url="https://finance.naver.com/sise/theme.naver?field=change_rate&ordering=desc",
        output=tmp_path / "theme_mappings_auto.csv",
        overwrite=False,
        max_pages=2,
        max_themes=1,
        delay_ms=0,
        timeout_sec=5,
        default_enabled=0,
        include_keywords=["로봇"],
        ranking_source="combined",
        max_market_pages=2,
        now=_fixed_now(),
        session=session,
        sleeper=lambda _seconds: None,
    )

    assert report.themes_found == 2
    assert report.themes_selected == 1
    assert report.rows_written == 2
    assert not any("recent_3days_change_rate" in url for url in session.urls if "theme.naver" in url)


def test_crawl_naver_themes_overwrite_guard_does_not_request(tmp_path):
    session = FakeNaverSession()
    output = tmp_path / "theme_mappings_auto.csv"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        crawl_naver_themes(
            start_url=None,
            output=output,
            overwrite=False,
            max_pages=1,
            max_themes=None,
            delay_ms=0,
            timeout_sec=1,
            default_enabled=0,
            include_keywords=None,
            session=session,
        )

    assert output.read_text(encoding="utf-8") == "existing"
    assert session.urls == []


def test_script_defaults_and_report_next_steps(capsys):
    args = parse_args([])

    assert args.output.endswith("theme_mappings_auto.csv")
    assert args.default_enabled == 0
    assert args.ranking_source == "combined"

    _print_report(CrawlReport(rows_written=2), Path("data/theme_mappings_auto.csv"))
    output = capsys.readouterr().out

    assert "manual-review draft" in output
    assert "review and edit data/theme_mappings_auto.csv" in output
    assert "existing CSV import procedure" in output


def test_naver_generator_does_not_import_db_import():
    source = Path("scripts/generate_naver_theme_mappings.py").read_text(encoding="utf-8")

    assert "import_theme_mappings_csv" not in source
    assert "TradingDatabase" not in source


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeNaverSession:
    def __init__(self) -> None:
        self.headers = {}
        self.urls: list[str] = []

    def get(self, url: str, timeout: int) -> FakeResponse:
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path.endswith("/sise/theme.naver"):
            return FakeResponse(self._theme_list(query.get("field", [""])[0], int(query.get("page", ["1"])[0])))
        if path.endswith("/sise/sise_group_detail.naver"):
            return FakeResponse(self._theme_detail(query.get("no", [""])[0]))
        if path.endswith("/sise/sise_market_sum.naver"):
            return FakeResponse(self._market_sum(query.get("sosok", [""])[0], int(query.get("page", ["1"])[0])))
        raise AssertionError(f"unexpected URL: {url}")

    def _theme_list(self, field: str, page: int) -> str:
        if page != 1:
            return "<table></table>"
        if field == "recent_3days_change_rate":
            rows = [
                ("102", "로봇", "+8.00%", "+4.00%", "로보티즈"),
                ("101", "반도체", "+10.00%", "+3.00%", "삼성전자 SK하이닉스"),
            ]
        else:
            rows = [
                ("101", "반도체", "+10.00%", "+3.00%", "삼성전자 SK하이닉스"),
                ("102", "로봇", "+8.00%", "+4.00%", "로보티즈"),
            ]
        return "<table class='type_1 theme'>" + "".join(_theme_row(*row) for row in rows) + "</table>"

    def _theme_detail(self, theme_no: str) -> str:
        if theme_no == "101":
            return _member_table(
                [
                    ("A005930", "삼성전자"),
                    ("000660", "SK하이닉스"),
                    ("005380", "현대차"),
                    ("123456", "코스닥테마"),
                ]
            )
        if theme_no == "102":
            return _member_table(
                [
                    ("277810", "로보티즈"),
                    ("A277810", "로보티즈"),
                    ("999999", "미분류"),
                ]
            )
        return "<table></table>"

    def _market_sum(self, sosok: str, page: int) -> str:
        if sosok == "0" and page == 1:
            return _member_table([("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차")])
        if sosok == "1" and page == 1:
            return _member_table([("123456", "코스닥테마"), ("277810", "로보티즈")])
        return "<table></table>"


def _theme_row(theme_no: str, name: str, change_rate: str, recent_3days: str, leading_text: str) -> str:
    return f"""
    <tr>
      <td><a href="/sise/sise_group_detail.naver?type=theme&no={theme_no}">{name}</a></td>
      <td>{change_rate}</td>
      <td>{recent_3days}</td>
      <td>4</td><td>0</td><td>1</td>
      <td>{leading_text}</td>
    </tr>
    """


def _member_table(rows: list[tuple[str, str]]) -> str:
    return "<table>" + "".join(
        f'<tr><td><a href="/item/main.naver?code={code}">{name}</a></td></tr>' for code, name in rows
    ) + "</table>"


def _fixed_now():
    from datetime import datetime

    return datetime(2026, 5, 29, 8, 30, 0)
