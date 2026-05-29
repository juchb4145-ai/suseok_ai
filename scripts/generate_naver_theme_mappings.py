#!/usr/bin/env python3
"""Generate a review-only theme_mappings_auto.csv from Naver Finance themes."""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - exercised only in under-provisioned envs.
    requests = None
    BeautifulSoup = None
    OPTIONAL_IMPORT_ERROR = exc
else:
    OPTIONAL_IMPORT_ERROR = None


BASE_URL = "https://finance.naver.com"
THEME_LIST_URLS = {
    "change_rate": f"{BASE_URL}/sise/theme.naver?field=change_rate&ordering=desc",
    "recent_3days": f"{BASE_URL}/sise/theme.naver?field=recent_3days_change_rate&ordering=desc",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
NEXT_STEPS = (
    "Next steps: review and edit data/theme_mappings_auto.csv, save the approved rows as "
    "data/theme_mappings.csv, then run the existing CSV import procedure."
)

COLUMNS = [
    "code",
    "name",
    "market",
    "theme_id",
    "theme_name",
    "strategy_profile",
    "enabled",
    "sub_theme",
    "is_large_cap",
    "is_leader_candidate",
    "base_priority",
    "is_signal_stock",
    "memo",
]

SIGNAL_STOCKS = {"005930", "000660"}
RANKING_SOURCES = ("combined", "change_rate", "recent_3days")


@dataclass
class ThemeGroup:
    no: str
    name: str
    url: str
    page: int
    order: int
    raw_href: str = ""
    leading_text: str = ""
    change_rate: str = ""
    recent_3days_change_rate: str = ""
    rank_change_rate: int = 0
    rank_recent3d: int = 0


@dataclass
class ThemeMember:
    code: str
    name: str
    rank: int


@dataclass
class MarketCodeResult:
    codes: set[str] = field(default_factory=set)
    request_count: int = 0
    pages_seen: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class CrawlReport:
    theme_pages_seen: int = 0
    themes_found: int = 0
    themes_selected: int = 0
    themes_fetched: int = 0
    rows_written: int = 0
    duplicate_rows_skipped: int = 0
    kospi_codes: int = 0
    kosdaq_codes: int = 0
    market_pages_seen: int = 0
    request_count: int = 0
    estimated_min_duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_code(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"[^0-9]", "", value)
    if len(value) > 6:
        value = value[-6:]
    return value.zfill(6) if value else ""


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def with_query(url: str, **updates: object) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in updates.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = [str(value)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def fetch_html(session, url: str, timeout_sec: int) -> str:
    _require_optional_dependencies()
    response = session.get(url, timeout=timeout_sec)
    response.raise_for_status()
    content = response.content
    if isinstance(content, str):
        return content
    encodings = [
        getattr(response, "apparent_encoding", None),
        getattr(response, "encoding", None),
        "euc-kr",
        "cp949",
        "utf-8",
    ]
    for encoding in [encoding for encoding in encodings if encoding]:
        try:
            return content.decode(encoding, errors="replace")
        except LookupError:
            continue
    return content.decode("euc-kr", errors="replace")


def parse_theme_list(html: str, page: int, base_url: str = BASE_URL) -> list[ThemeGroup]:
    _require_optional_dependencies()
    soup = BeautifulSoup(html, "html.parser")
    groups: list[ThemeGroup] = []
    seen: set[str] = set()

    for row in soup.find_all("tr"):
        link = row.find("a", href=lambda href: _is_theme_detail_href(str(href or "")))
        if link is None:
            continue
        href = str(link.get("href") or "")
        parsed = urlparse(urljoin(base_url, href))
        no_values = parse_qs(parsed.query).get("no")
        if not no_values:
            continue
        no = re.sub(r"[^0-9]", "", no_values[0])
        name = clean_text(link.get_text(" "))
        if not no or not name or no in seen:
            continue

        cells = [clean_text(cell.get_text(" ")) for cell in row.find_all("td")]
        order = len(groups) + 1
        seen.add(no)
        groups.append(
            ThemeGroup(
                no=no,
                name=name,
                url=urljoin(base_url, href),
                page=page,
                order=order,
                raw_href=href,
                change_rate=_rate_cell(cells, 1),
                recent_3days_change_rate=_rate_cell(cells, 2),
                leading_text=" ".join(text for text in cells[6:] if text),
            )
        )
    return groups


def parse_theme_members(html: str) -> list[ThemeMember]:
    _require_optional_dependencies()
    soup = BeautifulSoup(html, "html.parser")
    members: list[ThemeMember] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "/item/" not in href or "code=" not in href:
            continue
        code = normalize_code(parse_qs(urlparse(urljoin(BASE_URL, href)).query).get("code", [""])[0])
        name = clean_text(link.get_text(" "))
        if not re.fullmatch(r"\d{6}", code or "") or not name or code in seen:
            continue
        seen.add(code)
        members.append(ThemeMember(code=code, name=name, rank=len(members) + 1))
    return members


def crawl_market_codes(
    session,
    sosok: int,
    delay_sec: float,
    timeout_sec: int,
    *,
    needed_codes: Optional[set[str]] = None,
    max_pages: int = 80,
    sleeper=time.sleep,
) -> MarketCodeResult:
    result = MarketCodeResult()
    needed = set(needed_codes or set())
    empty_pages = 0

    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
        result.request_count += 1
        result.pages_seen += 1
        try:
            html = fetch_html(session, url, timeout_sec)
        except Exception as exc:
            result.warnings.append(f"MARKET_CODE_FETCH_FAILED:sosok={sosok}:page={page}:{exc}")
            break

        page_codes = _parse_market_codes(html)
        if not page_codes:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
            result.codes.update(page_codes)
            if needed and needed.issubset(result.codes):
                break
        sleeper(delay_sec)
    return result


def infer_market(code: str, kospi: set[str], kosdaq: set[str]) -> str:
    if code in kospi:
        return "KOSPI"
    if code in kosdaq:
        return "KOSDAQ"
    return "UNKNOWN"


def infer_row(
    theme: ThemeGroup,
    member: ThemeMember,
    market: str,
    generated_at: str,
    default_enabled: int,
) -> dict[str, str]:
    code = member.code
    market = market if market in {"KOSPI", "KOSDAQ"} else "UNKNOWN"
    profile, is_large, base_priority, is_signal = _base_profile(code, market)
    is_leader = 1 if is_signal else 0

    if member.rank <= 2 or _is_leading_member(theme, member):
        is_leader = 1
        base_priority = max(base_priority, 80)
    if is_signal:
        base_priority = 100

    enabled = 1 if int(default_enabled) else 0
    if market == "UNKNOWN":
        enabled = 0

    memo = {
        "auto_source": "naver",
        "naver_theme_no": theme.no,
        "generated_at": generated_at,
        "member_rank": str(member.rank),
    }
    if theme.change_rate:
        memo["theme_change_rate"] = theme.change_rate
    if theme.recent_3days_change_rate:
        memo["theme_recent_3days_change_rate"] = theme.recent_3days_change_rate
    if theme.rank_change_rate:
        memo["rank_change_rate"] = str(theme.rank_change_rate)
    if theme.rank_recent3d:
        memo["rank_recent3d"] = str(theme.rank_recent3d)
    if market == "UNKNOWN":
        memo["market_unresolved"] = "1"

    return {
        "code": code,
        "name": member.name,
        "market": market,
        "theme_id": f"naver_{theme.no}",
        "theme_name": theme.name,
        "strategy_profile": profile,
        "enabled": str(enabled),
        "sub_theme": _sub_theme(theme.name),
        "is_large_cap": str(int(is_large)),
        "is_leader_candidate": str(int(is_leader)),
        "base_priority": str(int(base_priority)),
        "is_signal_stock": str(int(is_signal)),
        "memo": _memo_text(memo),
    }


def crawl_naver_themes(
    start_url: Optional[str],
    output: Path,
    overwrite: bool,
    max_pages: int,
    max_themes: Optional[int],
    delay_ms: int,
    timeout_sec: int,
    default_enabled: int,
    include_keywords: Optional[list[str]],
    ranking_source: str = "combined",
    max_market_pages: int = 80,
    now: Optional[datetime] = None,
    session=None,
    sleeper=time.sleep,
    progress=None,
) -> CrawlReport:
    _require_optional_dependencies()
    report = CrawlReport()
    output = Path(output)
    delay_sec = max(delay_ms, 0) / 1000.0

    if output.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {output}. Use --overwrite to replace it.")

    if session is None:
        session = requests.Session()
    if hasattr(session, "headers"):
        session.headers.update({"User-Agent": USER_AGENT, "Referer": BASE_URL})

    groups_by_no: dict[str, ThemeGroup] = {}
    for source_key, source_url in _source_urls(start_url, ranking_source):
        source_groups = _crawl_theme_source(
            session,
            source_url,
            source_key,
            max_pages=max_pages,
            delay_sec=delay_sec,
            timeout_sec=timeout_sec,
            report=report,
            sleeper=sleeper,
        )
        for group in source_groups:
            _merge_theme_group(groups_by_no, group)

    report.themes_found = len(groups_by_no)
    groups = _filter_theme_groups(list(groups_by_no.values()), include_keywords)
    groups = _sort_theme_groups(groups, ranking_source if not start_url else _source_key_from_url(start_url))
    if max_themes is not None:
        groups = groups[: max(0, int(max_themes))]
    report.themes_selected = len(groups)
    report.estimated_min_duration_sec = round(
        max(0, report.themes_selected + 2 - 1) * delay_sec,
        2,
    )
    if progress is not None:
        progress(
            f"themes_to_fetch={report.themes_selected}, "
            f"estimated_min_duration={report.estimated_min_duration_sec:.2f}s"
        )

    generated_at = (now or datetime.now()).replace(microsecond=0).isoformat()
    members_by_theme: dict[str, list[ThemeMember]] = {}
    needed_codes: set[str] = set()
    seen_row_keys: set[tuple[str, str]] = set()

    for theme in groups:
        report.request_count += 1
        try:
            html = fetch_html(session, theme.url, timeout_sec)
            members = parse_theme_members(html)
        except Exception as exc:
            report.errors.append(f"THEME_DETAIL_FETCH_FAILED:theme_no={theme.no}:name={theme.name}:{exc}")
            sleeper(delay_sec)
            continue
        report.themes_fetched += 1
        if not members:
            report.warnings.append(f"THEME_NO_MEMBERS:theme_no={theme.no}:name={theme.name}")
        for member in members:
            key = (member.code, f"naver_{theme.no}")
            if key in seen_row_keys:
                report.duplicate_rows_skipped += 1
                continue
            seen_row_keys.add(key)
            needed_codes.add(member.code)
            members_by_theme.setdefault(theme.no, []).append(member)
        sleeper(delay_sec)

    kospi_codes: set[str] = set()
    kosdaq_codes: set[str] = set()
    if needed_codes:
        kospi_result = crawl_market_codes(
            session,
            sosok=0,
            delay_sec=delay_sec,
            timeout_sec=timeout_sec,
            needed_codes=needed_codes,
            max_pages=max_market_pages,
            sleeper=sleeper,
        )
        report.request_count += kospi_result.request_count
        report.market_pages_seen += kospi_result.pages_seen
        report.warnings.extend(kospi_result.warnings)
        kospi_codes = kospi_result.codes

        remaining_codes = needed_codes - kospi_codes
        kosdaq_result = crawl_market_codes(
            session,
            sosok=1,
            delay_sec=delay_sec,
            timeout_sec=timeout_sec,
            needed_codes=remaining_codes,
            max_pages=max_market_pages,
            sleeper=sleeper,
        )
        report.request_count += kosdaq_result.request_count
        report.market_pages_seen += kosdaq_result.pages_seen
        report.warnings.extend(kosdaq_result.warnings)
        kosdaq_codes = kosdaq_result.codes
    report.kospi_codes = len(kospi_codes)
    report.kosdaq_codes = len(kosdaq_codes)

    rows: list[dict[str, str]] = []
    for theme in groups:
        for member in members_by_theme.get(theme.no, []):
            market = infer_market(member.code, kospi_codes, kosdaq_codes)
            if market == "UNKNOWN":
                report.warnings.append(f"MARKET_UNRESOLVED:code={member.code}:name={member.name}:theme_no={theme.no}")
            rows.append(infer_row(theme, member, market, generated_at, default_enabled=default_enabled))

    rows.sort(key=lambda row: (row["theme_id"], row["code"]))
    _write_csv(output, rows)
    report.rows_written = len(rows)
    report.warnings = _dedupe(report.warnings)
    report.errors = _dedupe(report.errors)
    return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a review-only theme_mappings_auto.csv from Naver Finance.")
    parser.add_argument("--url", default="", help="Optional single Naver Finance theme list URL.")
    parser.add_argument("--output", default="data/theme_mappings_auto.csv", help="Output CSV path.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output file.")
    parser.add_argument("--max-pages", type=int, default=20, help="Max listing pages per ranking source.")
    parser.add_argument("--max-market-pages", type=int, default=80, help="Max market-sum pages per market.")
    parser.add_argument("--max-themes", type=int, default=None, help="Max themes to fetch details for.")
    parser.add_argument("--request-delay-ms", type=int, default=1200, help="Delay between HTTP requests.")
    parser.add_argument("--timeout-sec", type=int, default=20, help="HTTP timeout seconds.")
    parser.add_argument(
        "--default-enabled",
        type=int,
        choices=[0, 1],
        default=0,
        help="Default enabled value for generated rows. Default 0 for review-first workflow.",
    )
    parser.add_argument(
        "--include-keywords",
        default="",
        help="Comma-separated theme name or leading-stock keywords, for example: 반도체,로봇,전력.",
    )
    parser.add_argument(
        "--ranking-source",
        choices=RANKING_SOURCES,
        default="combined",
        help="Theme ranking source to crawl when --url is not supplied.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    keywords = [keyword.strip() for keyword in str(args.include_keywords or "").split(",") if keyword.strip()] or None
    output = Path(args.output)

    print("Naver Finance theme mapping auto generator")
    print(f"Output: {output}")
    if args.url:
        print(f"URL: {args.url}")
    else:
        print(f"ranking_source={args.ranking_source}")
    print(
        f"default_enabled={args.default_enabled}; request_delay_ms={args.request_delay_ms}; "
        f"max_themes={args.max_themes}"
    )
    print("WARNING: generated rows are a manual-review draft. Review before DB import.")
    if args.default_enabled == 0:
        print("NOTE: generated rows are enabled=0 by default. Review and set enabled=1 before DB import.")

    try:
        report = crawl_naver_themes(
            start_url=args.url or None,
            output=output,
            overwrite=args.overwrite,
            max_pages=args.max_pages,
            max_themes=args.max_themes,
            delay_ms=args.request_delay_ms,
            timeout_sec=args.timeout_sec,
            default_enabled=args.default_enabled,
            include_keywords=keywords,
            ranking_source=args.ranking_source,
            max_market_pages=args.max_market_pages,
            progress=print,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_report(report, output)
    return 1 if report.errors else 0


def _source_urls(start_url: Optional[str], ranking_source: str) -> list[tuple[str, str]]:
    if start_url:
        return [(_source_key_from_url(start_url), start_url)]
    if ranking_source == "combined":
        return [("change_rate", THEME_LIST_URLS["change_rate"]), ("recent_3days", THEME_LIST_URLS["recent_3days"])]
    return [(ranking_source, THEME_LIST_URLS[ranking_source])]


def _source_key_from_url(url: str) -> str:
    field = parse_qs(urlparse(url).query).get("field", [""])[0]
    if field == "change_rate":
        return "change_rate"
    if field == "recent_3days_change_rate":
        return "recent_3days"
    return "custom"


def _crawl_theme_source(
    session,
    source_url: str,
    source_key: str,
    *,
    max_pages: int,
    delay_sec: float,
    timeout_sec: int,
    report: CrawlReport,
    sleeper=time.sleep,
) -> list[ThemeGroup]:
    groups: list[ThemeGroup] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        page_url = with_query(source_url, page=page)
        report.theme_pages_seen += 1
        report.request_count += 1
        try:
            html = fetch_html(session, page_url, timeout_sec)
        except Exception as exc:
            report.warnings.append(f"THEME_LIST_FETCH_FAILED:source={source_key}:page={page}:{exc}")
            break
        page_groups = parse_theme_list(html, page=page)
        if not page_groups:
            if page == 1:
                report.warnings.append(f"NO_THEMES_FOUND_ON_FIRST_PAGE:source={source_key}")
            break
        for group in page_groups:
            if group.no in seen:
                continue
            seen.add(group.no)
            _assign_source_rank(group, source_key, len(groups) + 1)
            groups.append(group)
        sleeper(delay_sec)
    return groups


def _merge_theme_group(groups_by_no: dict[str, ThemeGroup], incoming: ThemeGroup) -> None:
    existing = groups_by_no.get(incoming.no)
    if existing is None:
        groups_by_no[incoming.no] = incoming
        return
    if not existing.name and incoming.name:
        existing.name = incoming.name
    if not existing.url and incoming.url:
        existing.url = incoming.url
    if not existing.leading_text and incoming.leading_text:
        existing.leading_text = incoming.leading_text
    if not existing.change_rate and incoming.change_rate:
        existing.change_rate = incoming.change_rate
    if not existing.recent_3days_change_rate and incoming.recent_3days_change_rate:
        existing.recent_3days_change_rate = incoming.recent_3days_change_rate
    if incoming.rank_change_rate:
        existing.rank_change_rate = incoming.rank_change_rate
    if incoming.rank_recent3d:
        existing.rank_recent3d = incoming.rank_recent3d


def _filter_theme_groups(groups: list[ThemeGroup], include_keywords: Optional[list[str]]) -> list[ThemeGroup]:
    keywords = [keyword.strip().lower() for keyword in include_keywords or [] if keyword and keyword.strip()]
    if not keywords:
        return groups
    filtered: list[ThemeGroup] = []
    for group in groups:
        haystack = f"{group.name} {group.leading_text}".lower()
        if any(keyword in haystack for keyword in keywords):
            filtered.append(group)
    return filtered


def _sort_theme_groups(groups: list[ThemeGroup], ranking_source: str) -> list[ThemeGroup]:
    return sorted(groups, key=lambda group: _theme_sort_key(group, ranking_source))


def _theme_sort_key(group: ThemeGroup, ranking_source: str) -> tuple[int, int, int, str]:
    large = 999_999
    if ranking_source == "change_rate":
        primary = group.rank_change_rate or large
        secondary = group.rank_recent3d or large
    elif ranking_source == "recent_3days":
        primary = group.rank_recent3d or large
        secondary = group.rank_change_rate or large
    else:
        ranks = [rank for rank in [group.rank_change_rate, group.rank_recent3d] if rank]
        primary = min(ranks) if ranks else large
        secondary = group.rank_change_rate or large
    return primary, secondary, group.page, group.no


def _parse_market_codes(html: str) -> set[str]:
    _require_optional_dependencies()
    soup = BeautifulSoup(html, "html.parser")
    codes: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if "/item/main.naver" not in href or "code=" not in href:
            continue
        code = normalize_code(parse_qs(urlparse(urljoin(BASE_URL, href)).query).get("code", [""])[0])
        if re.fullmatch(r"\d{6}", code or ""):
            codes.add(code)
    return codes


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in COLUMNS} for row in rows)
    tmp.replace(path)


def _assign_source_rank(group: ThemeGroup, source_key: str, rank: int) -> None:
    if source_key == "change_rate":
        group.rank_change_rate = rank
    elif source_key == "recent_3days":
        group.rank_recent3d = rank


def _is_theme_detail_href(href: str) -> bool:
    return "sise_group_detail" in href and "type=theme" in href and "no=" in href


def _rate_cell(cells: list[str], index: int) -> str:
    if len(cells) <= index:
        return ""
    return clean_text(cells[index]).replace(" ", "")


def _base_profile(code: str, market: str) -> tuple[str, int, int, int]:
    if code in SIGNAL_STOCKS:
        return "SEMICONDUCTOR_SIGNAL_PROFILE", 1, 100, 1
    if market == "KOSPI":
        return "KOSPI_LEADER_PROFILE", 1, 70, 0
    if market == "KOSDAQ":
        return "KOSDAQ_THEME_PROFILE", 0, 60, 0
    return "KOSDAQ_THEME_PROFILE", 0, 50, 0


def _is_leading_member(theme: ThemeGroup, member: ThemeMember) -> bool:
    return bool(member.name and member.name in theme.leading_text)


def _sub_theme(theme_name: str) -> str:
    if "_" not in theme_name:
        return ""
    _, suffix = theme_name.split("_", 1)
    return suffix.strip()


def _memo_text(values: dict[str, str]) -> str:
    parts = []
    for key, value in values.items():
        safe_value = str(value).replace(";", " ").replace("\n", " ").strip()
        parts.append(f"{key}={safe_value}")
    return ";".join(parts)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _print_report(report: CrawlReport, output: Path) -> None:
    print("\nDone.")
    print(f"output={output}")
    print(f"theme_pages_seen={report.theme_pages_seen}")
    print(f"themes_found={report.themes_found}")
    print(f"themes_selected={report.themes_selected}")
    print(f"themes_fetched={report.themes_fetched}")
    print(f"rows_written={report.rows_written}")
    print(f"duplicate_rows_skipped={report.duplicate_rows_skipped}")
    print(f"kospi_codes={report.kospi_codes}; kosdaq_codes={report.kosdaq_codes}")
    print(f"market_pages_seen={report.market_pages_seen}; requests={report.request_count}")
    print(f"warnings={len(report.warnings)}; errors={len(report.errors)}")
    for warning in report.warnings[:20]:
        print(f"WARNING: {warning}")
    for error in report.errors[:20]:
        print(f"ERROR: {error}")
    print("WARNING: generated CSV is a manual-review draft. Default enabled is 0 unless explicitly changed.")
    print(NEXT_STEPS)


def _require_optional_dependencies() -> None:
    if OPTIONAL_IMPORT_ERROR is None:
        return
    raise RuntimeError(
        "requests and beautifulsoup4 are required. Install project requirements with "
        ".\\venv_32\\Scripts\\python.exe -m pip install -r requirements.txt"
    ) from OPTIONAL_IMPORT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
