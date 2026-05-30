from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ThemeStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    WATCH = "WATCH"
    ACTIVE = "ACTIVE"
    STALE = "STALE"


class RelationType(str, Enum):
    INVESTOR = "investor"
    PARTNER = "partner"
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    SAME_INDUSTRY = "same_industry"
    POLICY_BENEFIT = "policy_benefit"
    NEWS_MENTIONED = "news_mentioned"
    RUMOR = "rumor"
    UNKNOWN = "unknown"


class ThemeEvidenceType(str, Enum):
    SOURCE_MEMBER = "source_member"
    NEWS = "news"
    EVENT = "event"
    CLUSTER = "cluster"
    MANUAL_FIXTURE = "manual_fixture"


@dataclass
class CanonicalTheme:
    theme_id: str
    canonical_name: str
    display_name: str
    theme_group: str = ""
    status: ThemeStatus | str = ThemeStatus.CANDIDATE
    confidence: float = 0.0
    trade_eligible: bool = False
    first_seen_at: str = ""
    last_seen_at: str = ""
    updated_at: str = ""


@dataclass
class ThemeAlias:
    theme_id: str
    alias: str
    normalized_alias: str
    source: str = ""
    id: Optional[int] = None
    created_at: str = ""


@dataclass
class SourceTheme:
    source: str
    source_theme_name: str
    normalized_name: str
    source_theme_id: str = ""
    matched_theme_id: str = ""
    match_confidence: float = 0.0
    raw_payload_hash: str = ""
    id: Optional[int] = None
    first_seen_at: str = ""
    last_seen_at: str = ""
    updated_at: str = ""


@dataclass
class ThemeMemberEvidence:
    theme_id: str
    stock_code: str
    stock_name: str = ""
    source: str = ""
    evidence_type: ThemeEvidenceType | str = ThemeEvidenceType.SOURCE_MEMBER
    relation_type: RelationType | str = RelationType.UNKNOWN
    reason: str = ""
    confidence: float = 0.0
    id: Optional[int] = None
    first_seen_at: str = ""
    last_seen_at: str = ""
    updated_at: str = ""


@dataclass
class ThemeMembership:
    theme_id: str
    stock_code: str
    stock_name: str = ""
    membership_score: float = 0.0
    relation_type: RelationType | str = RelationType.UNKNOWN
    source_count: int = 0
    active: bool = True
    trade_eligible: bool = False
    updated_at: str = ""


@dataclass
class StockSnapshot:
    stock_code: str
    stock_name: str = ""
    current_price: float = 0.0
    change_rate: float = 0.0
    volume: int = 0
    turnover: float = 0.0
    execution_strength: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    session_high: float = 0.0
    session_low: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    turnover_strength: float = 1.0
    ts: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThemeActivitySnapshot:
    theme_id: str
    theme_name: str = ""
    theme_score: float = 0.0
    status: ThemeStatus | str = ThemeStatus.CANDIDATE
    trade_eligible: bool = False
    rank: int = 0
    rank_delta_1m: int = 0
    rank_delta_5m: int = 0
    weighted_return_pct: float = 0.0
    turnover: float = 0.0
    turnover_strength: float = 0.0
    breadth: float = 0.0
    rising_count: int = 0
    falling_count: int = 0
    total_count: int = 0
    leader_code: str = ""
    leader_name: str = ""
    leader_return_pct: float = 0.0
    leader_turnover: float = 0.0
    leader_gap: float = 0.0
    top3_concentration: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    id: Optional[int] = None
    created_at: str = ""


@dataclass
class ThemeRankItem:
    rank: int
    theme_id: str
    theme_name: str = ""
    theme_score: float = 0.0
    status: ThemeStatus | str = ThemeStatus.CANDIDATE
    trade_eligible: bool = False
    rank_delta_1m: int = 0
    rank_delta_5m: int = 0
    weighted_return_pct: float = 0.0
    turnover: float = 0.0
    turnover_strength: float = 0.0
    breadth: float = 0.0
    rising_count: int = 0
    total_count: int = 0
    leader_code: str = ""
    leader_name: str = ""
    leader_return_pct: float = 0.0
    leader_gap: float = 0.0
    top3_concentration: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class StockThemeState:
    stock_code: str
    stock_name: str = ""
    themes: list["ThemeContext"] = field(default_factory=list)
    primary_theme_id: str = ""
    primary_theme_name: str = ""
    primary_rank: int = 0
    membership_score: float = 0.0
    leadership_role: str = ""
    reason_code: str = ""
    ready: bool = False


@dataclass
class ThemeSourcePayload:
    source: str
    source_theme_name: str
    source_theme_id: str = ""
    aliases: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThemeSourceSyncResult:
    source: str
    status: str = "success"
    theme_count: int = 0
    member_count: int = 0
    error_count: int = 0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""


@dataclass
class ThemeSourceSyncRun:
    id: Optional[int] = None
    source: str = ""
    started_at: str = ""
    finished_at: str = ""
    status: str = ""
    theme_count: int = 0
    member_count: int = 0
    error_count: int = 0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DynamicThemeCluster:
    cluster_id: str
    matched_theme_id: str = ""
    status: ThemeStatus | str = ThemeStatus.CANDIDATE
    stock_codes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    updated_at: str = ""


@dataclass
class ThemeContext:
    theme_id: str
    theme_name: str = ""
    status: ThemeStatus | str = ThemeStatus.CANDIDATE
    membership: Optional[ThemeMembership] = None
    activity: Optional[ThemeActivitySnapshot] = None
    membership_score: float = 0.0
    relation_type: RelationType | str = RelationType.UNKNOWN
    active: bool = True
    trade_eligible: bool = False
    source_count: int = 0
    rank: int = 0
    rank_in_theme: int = 0
    leader_code: str = ""
    market: str = ""
    strategy_profile: Optional[Any] = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThemeStrengthResult:
    theme_id: str
    theme_name: str = ""
    score: float = 0.0
    grade: str = "C"
    active_candidate_count: int = 0
    valid_tick_ratio: float = 0.0
    leader_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


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
    details: dict[str, Any] = field(default_factory=dict)
