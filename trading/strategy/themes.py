from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from trading.strategy.candidates import add_unique, normalize_code
from trading.strategy.models import Candidate, StrategyProfile

if TYPE_CHECKING:
    from storage.db import TradingDatabase


@dataclass
class ThemeMapping:
    id: Optional[int] = None
    code: str = ""
    name: str = ""
    market: str = ""
    theme_id: str = ""
    theme_name: str = ""
    sub_theme: str = ""
    strategy_profile: Optional[StrategyProfile] = None
    is_large_cap: bool = False
    is_leader_candidate: bool = False
    base_priority: int = 0
    is_signal_stock: bool = False
    enabled: bool = True
    memo: str = ""


@dataclass
class ThemeImportResult:
    total_rows: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    disabled: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ThemeStrengthResult:
    theme_id: str
    theme_name: str = ""
    score: float = 0.0
    grade: str = "C"
    active_candidate_count: int = 0
    valid_tick_ratio: float = 0.0
    leader_codes: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass
class StockLeadershipResult:
    candidate_id: Optional[int]
    code: str
    theme_id: str
    theme_name: str = ""
    score: float = 0.0
    leadership_rank: int = 0
    leadership_role: str = "unranked"
    leadership_scope: str = ""
    details: dict = field(default_factory=dict)


class ThemeRepository:
    def __init__(self, db: "TradingDatabase") -> None:
        self.db = db

    def upsert_mapping(self, mapping: ThemeMapping) -> ThemeMapping:
        mapping.code = normalize_code(mapping.code)
        return self.db.upsert_theme_mapping(mapping)

    def seed_minimal_defaults(self) -> list[ThemeMapping]:
        defaults = [
            ThemeMapping(
                code="005930",
                name="Samsung Electronics",
                market="KOSPI",
                theme_id="semiconductor",
                theme_name="Semiconductor",
                sub_theme="sector_leader",
                strategy_profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE,
                is_large_cap=True,
                is_leader_candidate=True,
                base_priority=100,
                is_signal_stock=True,
            ),
            ThemeMapping(
                code="000660",
                name="SK Hynix",
                market="KOSPI",
                theme_id="semiconductor",
                theme_name="Semiconductor",
                sub_theme="sector_leader",
                strategy_profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE,
                is_large_cap=True,
                is_leader_candidate=True,
                base_priority=95,
                is_signal_stock=True,
            ),
            ThemeMapping(
                code="005380",
                name="Hyundai Motor",
                market="KOSPI",
                theme_id="auto",
                theme_name="Auto",
                strategy_profile=StrategyProfile.KOSPI_LEADER_PROFILE,
                is_large_cap=True,
                is_leader_candidate=True,
                base_priority=90,
            ),
            ThemeMapping(
                code="000270",
                name="Kia",
                market="KOSPI",
                theme_id="auto",
                theme_name="Auto",
                strategy_profile=StrategyProfile.KOSPI_LEADER_PROFILE,
                is_large_cap=True,
                is_leader_candidate=True,
                base_priority=85,
            ),
        ]
        return [self.upsert_mapping(mapping) for mapping in defaults]

    def themes_for_code(self, code: str) -> list[ThemeMapping]:
        return self.db.theme_mappings_for_code(normalize_code(code), enabled=True)

    def members_for_theme(self, theme_id: str) -> list[ThemeMapping]:
        return self.db.theme_members(theme_id, enabled=True)

    def enrich_candidate(self, candidate: Candidate) -> Candidate:
        mappings = self.themes_for_code(candidate.code)
        if not mappings:
            return replace(candidate, code=normalize_code(candidate.code))

        enriched = replace(candidate)
        enriched.code = normalize_code(enriched.code)
        enriched.theme_ids = list(enriched.theme_ids)
        enriched.metadata = dict(enriched.metadata or {})
        mapping_details = []
        for mapping in mappings:
            add_unique(enriched.theme_ids, mapping.theme_id)
            mapping_details.append(
                {
                    "theme_id": mapping.theme_id,
                    "theme_name": mapping.theme_name,
                    "sub_theme": mapping.sub_theme,
                    "strategy_profile": mapping.strategy_profile.value if mapping.strategy_profile else None,
                    "is_signal_stock": mapping.is_signal_stock,
                }
            )

        first = mappings[0]
        if not enriched.name:
            enriched.name = first.name
        if not enriched.market:
            enriched.market = first.market
        if enriched.strategy_profile is None:
            enriched.strategy_profile = first.strategy_profile
        enriched.metadata["theme_mappings"] = mapping_details
        return enriched


REQUIRED_THEME_COLUMNS = {
    "code",
    "name",
    "market",
    "theme_id",
    "theme_name",
    "strategy_profile",
    "enabled",
}
OPTIONAL_THEME_COLUMNS = {
    "sub_theme",
    "is_large_cap",
    "is_leader_candidate",
    "base_priority",
    "is_signal_stock",
    "memo",
}
ALLOWED_THEME_MARKETS = {"KOSPI", "KOSDAQ"}


def import_theme_mappings_csv(db: "TradingDatabase", csv_path) -> ThemeImportResult:
    path = Path(csv_path)
    result = ThemeImportResult()
    repository = ThemeRepository(db)

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or [])
            missing = sorted(REQUIRED_THEME_COLUMNS - fieldnames)
            if missing:
                result.errors.append(f"missing required columns: {', '.join(missing)}")
                return result
            unknown = sorted(fieldnames - REQUIRED_THEME_COLUMNS - OPTIONAL_THEME_COLUMNS)
            if unknown:
                result.warnings.append(f"unknown columns ignored: {', '.join(unknown)}")
            for row_number, row in enumerate(reader, start=2):
                result.total_rows += 1
                mapping = _parse_theme_mapping_row(row, row_number, result)
                if mapping is None:
                    result.skipped += 1
                    continue
                existing = _find_theme_mapping(db, mapping.code, mapping.theme_id)
                saved = repository.upsert_mapping(mapping)
                if existing is None:
                    result.inserted += 1
                else:
                    result.updated += 1
                if not saved.enabled:
                    result.disabled += 1
    except FileNotFoundError:
        result.errors.append(f"file not found: {path}")
    return result


def _parse_theme_mapping_row(row: dict, row_number: int, result: ThemeImportResult) -> Optional[ThemeMapping]:
    code = _parse_import_code(row.get("code"), row_number, result)
    if code is None:
        result.errors.append(f"row {row_number}: invalid code {row.get('code')!r}")
        return None

    market = str(row.get("market") or "").strip().upper()
    if market not in ALLOWED_THEME_MARKETS:
        result.errors.append(f"row {row_number}: invalid market {row.get('market')!r}")
        return None

    try:
        strategy_profile = StrategyProfile(str(row.get("strategy_profile") or "").strip())
    except ValueError:
        result.errors.append(f"row {row_number}: invalid strategy_profile {row.get('strategy_profile')!r}")
        return None

    enabled = _parse_bool(row.get("enabled"), row_number, "enabled", result)
    if enabled is None:
        return None
    is_large_cap = _parse_bool(row.get("is_large_cap", "0"), row_number, "is_large_cap", result)
    is_leader_candidate = _parse_bool(row.get("is_leader_candidate", "0"), row_number, "is_leader_candidate", result)
    is_signal_stock = _parse_bool(row.get("is_signal_stock", "0"), row_number, "is_signal_stock", result)
    if is_large_cap is None or is_leader_candidate is None or is_signal_stock is None:
        return None

    base_priority = _parse_base_priority(row.get("base_priority", "0"), row_number, result)
    if base_priority is None:
        return None

    theme_id = str(row.get("theme_id") or "").strip()
    theme_name = str(row.get("theme_name") or "").strip()
    if not theme_id:
        result.errors.append(f"row {row_number}: theme_id is required")
        return None
    if not theme_name:
        result.errors.append(f"row {row_number}: theme_name is required")
        return None

    return ThemeMapping(
        code=code,
        name=str(row.get("name") or "").strip(),
        market=market,
        theme_id=theme_id,
        theme_name=theme_name,
        sub_theme=str(row.get("sub_theme") or "").strip(),
        strategy_profile=strategy_profile,
        is_large_cap=is_large_cap,
        is_leader_candidate=is_leader_candidate,
        base_priority=base_priority,
        is_signal_stock=is_signal_stock,
        enabled=enabled,
        memo=str(row.get("memo") or "").strip(),
    )


def _parse_import_code(value, row_number: int, result: ThemeImportResult) -> Optional[str]:
    raw = str(value or "").strip().upper()
    code = normalize_code(raw)
    if re.fullmatch(r"\d+\.0+", code):
        code = code.split(".", 1)[0]
    if code.isdigit() and 1 <= len(code) <= 6:
        normalized = code.zfill(6)
        if normalized != code and "short numeric codes were left-padded to 6 digits" not in result.warnings:
            result.warnings.append("short numeric codes were left-padded to 6 digits")
        return normalized
    return None


def _parse_bool(value, row_number: int, field_name: str, result: ThemeImportResult) -> Optional[bool]:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "t", "y", "yes"}:
        return True
    if text in {"0", "false", "f", "n", "no"}:
        return False
    result.errors.append(f"row {row_number}: invalid bool {field_name}={value!r}")
    return None


def _parse_base_priority(value, row_number: int, result: ThemeImportResult) -> Optional[int]:
    text = str(value or "0").strip()
    try:
        priority = int(text)
    except ValueError:
        result.errors.append(f"row {row_number}: invalid base_priority {value!r}")
        return None
    if not 0 <= priority <= 100:
        result.errors.append(f"row {row_number}: base_priority out of range {priority}")
        return None
    return priority


def _find_theme_mapping(db: "TradingDatabase", code: str, theme_id: str) -> Optional[ThemeMapping]:
    for mapping in db.theme_mappings_for_code(code, enabled=None):
        if mapping.theme_id == theme_id:
            return mapping
    return None
