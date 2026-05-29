from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from kiwoom.tr import KiwoomTrRunner, TrRequestResult
from trading.strategy.candidates import normalize_code
from trading.strategy.models import StrategyProfile


THEME_TEMPLATE_OUTPUT = Path("data") / "theme_mappings_auto.csv"
THEME_MAPPING_CSV_COLUMNS = [
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
OPT90001_FIELDS = ["종목코드", "테마코드", "테마명", "종목수", "주요종목"]
OPT90002_FIELDS = ["종목코드", "종목명"]
SIGNAL_CODES = {"005930", "000660"}
NEXT_STEPS = (
    "Next steps: review and edit data/theme_mappings_auto.csv, save the approved rows as "
    "data/theme_mappings.csv, then run the existing CSV import procedure."
)


@dataclass
class ThemeGroup:
    theme_code: str
    theme_name: str
    stock_count: int = 0
    leading_text: str = ""


@dataclass
class ThemeTemplateResult:
    output_path: str = ""
    themes_total: int = 0
    themes_to_fetch: int = 0
    estimated_min_duration_sec: float = 0.0
    rows_written: int = 0
    request_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def generate_theme_mappings_auto_csv(
    client,
    *,
    output_path=THEME_TEMPLATE_OUTPUT,
    overwrite: bool = False,
    default_enabled: int = 0,
    date_range_days: int = 5,
    request_delay_ms: int = 1200,
    timeout_sec: int = 20,
    max_themes: Optional[int] = None,
    include_keywords: Optional[list[str]] = None,
    runner: Optional[KiwoomTrRunner] = None,
    now: Optional[datetime] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> ThemeTemplateResult:
    output = Path(output_path)
    result = ThemeTemplateResult(output_path=str(output))
    if output.exists() and not overwrite:
        result.errors.append(f"OUTPUT_EXISTS:{output}")
        result.warnings.append("Use --overwrite to replace the existing auto CSV.")
        return result

    generated_at = (now or datetime.now()).replace(microsecond=0).isoformat()
    result.warnings.append("AUTO_CSV_REQUIRES_MANUAL_REVIEW")
    runner = runner or KiwoomTrRunner(client, request_delay_ms=request_delay_ms, timeout_sec=timeout_sec)
    group_result = _fetch_theme_groups(runner, date_range_days=date_range_days)
    result.request_count += group_result.request_count
    result.warnings.extend(group_result.warnings)
    result.errors.extend(group_result.errors)
    groups = [_theme_group_from_row(row, result) for row in group_result.rows]
    groups = [group for group in groups if group is not None]
    result.themes_total = len(groups)
    groups = _filter_theme_groups(groups, include_keywords)
    groups = sorted(groups, key=lambda group: (group.theme_code, group.theme_name))
    if max_themes is not None:
        groups = groups[: max(0, int(max_themes))]
    result.themes_to_fetch = len(groups)
    result.estimated_min_duration_sec = round(
        max(0, result.themes_to_fetch - 1) * max(0, int(request_delay_ms)) / 1000.0,
        2,
    )
    if progress is not None:
        progress(
            f"themes_to_fetch={result.themes_to_fetch}, "
            f"estimated_min_duration={result.estimated_min_duration_sec:.2f}s"
        )

    market_resolver = MarketResolver(client)
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for group in groups:
        member_result = _fetch_theme_members(runner, group, date_range_days=date_range_days)
        result.request_count += member_result.request_count
        result.warnings.extend(member_result.warnings)
        result.errors.extend(member_result.errors)
        if member_result.errors:
            result.warnings.append(f"THEME_FETCH_FAILED:{group.theme_code}:{group.theme_name}")
        if not member_result.rows:
            result.warnings.append(f"THEME_MEMBER_EMPTY:{group.theme_code}:{group.theme_name}")
            continue
        for row in member_result.rows:
            csv_row = _csv_row_for_member(
                row,
                group,
                market_resolver=market_resolver,
                default_enabled=int(default_enabled),
                generated_at=generated_at,
                date_range_days=int(date_range_days),
                warnings=result.warnings,
            )
            if csv_row is None:
                continue
            rows_by_key[(csv_row["code"], csv_row["theme_id"])] = csv_row

    rows = sorted(rows_by_key.values(), key=lambda row: (row["theme_id"], row["code"]))
    _write_csv(output, rows)
    result.rows_written = len(rows)
    result.warnings = _dedupe(result.warnings)
    result.errors = _dedupe(result.errors)
    return result


class MarketResolver:
    def __init__(self, client) -> None:
        self.kospi_codes = _market_code_set(client, "0")
        self.kosdaq_codes = _market_code_set(client, "10")

    def resolve(self, code: str) -> str:
        clean_code = normalize_code(code)
        if clean_code in self.kospi_codes:
            return "KOSPI"
        if clean_code in self.kosdaq_codes:
            return "KOSDAQ"
        return "UNKNOWN"


def _fetch_theme_groups(runner: KiwoomTrRunner, *, date_range_days: int) -> TrRequestResult:
    return runner.request_pages(
        tr_code="OPT90001",
        rq_name="OPT90001",
        inputs={"검색구분": "0", "종목코드": "", "날짜구분": str(date_range_days), "테마명": "", "등락수익구분": "1"},
        fields=OPT90001_FIELDS,
        screen_no="0650",
    )


def _fetch_theme_members(runner: KiwoomTrRunner, group: ThemeGroup, *, date_range_days: int) -> TrRequestResult:
    return runner.request_pages(
        tr_code="OPT90002",
        rq_name="OPT90002",
        inputs={"날짜구분": str(date_range_days), "종목코드": group.theme_code},
        fields=OPT90002_FIELDS,
        screen_no="0651",
    )


def _theme_group_from_row(row: dict[str, str], result: ThemeTemplateResult) -> Optional[ThemeGroup]:
    theme_code = _safe_theme_code(_field(row, ["종목코드", "테마코드"]))
    theme_name = _field(row, ["테마명"])
    if not theme_code:
        result.warnings.append(f"THEME_CODE_MISSING:{theme_name or row}")
        return None
    if not theme_name:
        result.warnings.append(f"THEME_NAME_MISSING:{theme_code}")
        theme_name = theme_code
    return ThemeGroup(
        theme_code=theme_code,
        theme_name=theme_name,
        stock_count=_parse_int(_field(row, ["종목수"])),
        leading_text=_field(row, ["주요종목"]),
    )


def _csv_row_for_member(
    row: dict[str, str],
    group: ThemeGroup,
    *,
    market_resolver: MarketResolver,
    default_enabled: int,
    generated_at: str,
    date_range_days: int,
    warnings: list[str],
) -> Optional[dict[str, str]]:
    code = normalize_code(_field(row, ["종목코드"]).strip().upper())
    name = _field(row, ["종목명"])
    if not (len(code) == 6 and code.isdigit()):
        warnings.append(f"MEMBER_CODE_INVALID:{group.theme_code}:{row}")
        return None
    market = market_resolver.resolve(code)
    profile, large_cap, priority, signal_stock = _base_profile(code, market)
    leader_candidate = 1 if code in SIGNAL_CODES else 0
    if name and name in group.leading_text:
        leader_candidate = 1
        priority = max(priority, 80)
    enabled = 1 if int(default_enabled) else 0
    memo = {
        "auto_source": "kiwoom",
        "theme_code": group.theme_code,
        "generated_at": generated_at,
        "date_range_days": str(date_range_days),
    }
    if market == "UNKNOWN":
        enabled = 0
        memo["market_unresolved"] = "1"
        warnings.append(f"MARKET_UNRESOLVED:{code}:{name}:{group.theme_code}")
    return {
        "code": code,
        "name": name,
        "market": market,
        "theme_id": f"kiwoom_{group.theme_code}",
        "theme_name": group.theme_name,
        "strategy_profile": profile.value,
        "enabled": str(enabled),
        "sub_theme": "",
        "is_large_cap": str(int(large_cap)),
        "is_leader_candidate": str(int(leader_candidate)),
        "base_priority": str(int(priority)),
        "is_signal_stock": str(int(signal_stock)),
        "memo": _memo_text(memo),
    }


def _base_profile(code: str, market: str) -> tuple[StrategyProfile, int, int, int]:
    if code in SIGNAL_CODES:
        return StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE, 1, 100, 1
    if market == "KOSPI":
        return StrategyProfile.KOSPI_LEADER_PROFILE, 1, 70, 0
    if market == "KOSDAQ":
        return StrategyProfile.KOSDAQ_THEME_PROFILE, 0, 60, 0
    return StrategyProfile.KOSDAQ_THEME_PROFILE, 0, 50, 0


def _filter_theme_groups(groups: list[ThemeGroup], include_keywords: Optional[list[str]]) -> list[ThemeGroup]:
    keywords = [keyword.strip().lower() for keyword in include_keywords or [] if keyword and keyword.strip()]
    if not keywords:
        return groups
    result: list[ThemeGroup] = []
    for group in groups:
        haystack = f"{group.theme_name} {group.leading_text}".lower()
        if any(keyword in haystack for keyword in keywords):
            result.append(group)
    return result


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=THEME_MAPPING_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in THEME_MAPPING_CSV_COLUMNS})


def _market_code_set(client, market_code: str) -> set[str]:
    if not hasattr(client, "get_code_list_by_market"):
        return set()
    try:
        return {normalize_code(code) for code in client.get_code_list_by_market(market_code)}
    except Exception:
        return set()


def _field(row: dict[str, str], aliases: list[str]) -> str:
    for alias in aliases:
        value = str(row.get(alias) or "").strip()
        if value:
            return value
    return ""


def _safe_theme_code(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z_가-힣-]+", "_", text)
    return text.strip("_")


def _parse_int(value: str) -> int:
    raw = str(value or "").strip().replace(",", "")
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _memo_text(values: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in values.items())


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
