from __future__ import annotations

from typing import Any, Literal, TypedDict


class InternalThemeBenchmarkTheme(TypedDict):
    theme_id: str
    theme_name: str
    rank: int
    theme_score: float
    weighted_return_pct: float
    turnover: float
    breadth: float
    leader_code: str
    top_stocks: list[dict[str, Any]]
    members: list[dict[str, Any]]
    reason_codes: list[str]
    snapshot_quality: dict[str, Any]


class InternalThemeBenchmarkSnapshot(TypedDict):
    source: Literal["internal_dynamic_theme_engine"] | str
    generated_at: str
    trade_date: str
    ranking_basis: Literal["theme_score"]
    themes: list[InternalThemeBenchmarkTheme]


class ExternalThemeBenchmarkStock(TypedDict, total=False):
    stock_code: str
    stock_name: str
    rank: int
    change_rate: float
    turnover: float


class ExternalThemeBenchmarkTheme(TypedDict):
    external_theme_name: str
    canonical_theme_hint: str
    rank: int
    score: float
    top_stocks: list[ExternalThemeBenchmarkStock]
    members: list[ExternalThemeBenchmarkStock]


class ExternalThemeBenchmarkSnapshot(TypedDict):
    source: str
    captured_at: str
    trade_date: str
    ranking_basis: str
    themes: list[ExternalThemeBenchmarkTheme]


class ThemeBenchmarkComparisonTheme(TypedDict):
    external_theme_name: str
    canonical_theme_hint: str
    internal_theme_id: str
    internal_theme_name: str
    match_method: str
    external_rank: int
    internal_rank: int
    member_overlap_count: int
    member_jaccard_score: float
    top5_overlap_count: int
    top5_overlap_ratio: float
    leader_match: bool
    leader_rank_delta: int | None
    external_only_stocks: list[str]
    internal_only_stocks: list[str]
    matched_stocks: list[str]
    mismatch_reasons: list[str]


class ThemeBenchmarkComparison(TypedDict):
    summary: dict[str, Any]
    themes: list[ThemeBenchmarkComparisonTheme]
    missing_external_themes: list[dict[str, Any]]
    internal_only_themes: list[dict[str, Any]]
    alias_candidates: list[dict[str, Any]]
