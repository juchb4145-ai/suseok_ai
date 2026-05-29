from __future__ import annotations

from dataclasses import dataclass, field, replace
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
