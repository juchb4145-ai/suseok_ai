from __future__ import annotations

from typing import Any

from trading.theme_engine.benchmark.schemas import ThemeBenchmarkComparison, ThemeBenchmarkComparisonTheme
from trading.theme_engine.normalizer import normalize_stock_code, normalize_theme_name
from trading.theme_engine.repository import ThemeEngineRepository


THEME_ALIAS_MISSING = "THEME_ALIAS_MISSING"
EXTERNAL_THEME_MISSING = "EXTERNAL_THEME_MISSING"
INTERNAL_THEME_MISSING = "INTERNAL_THEME_MISSING"
LOW_MEMBER_OVERLAP = "LOW_MEMBER_OVERLAP"
LOW_TOP5_OVERLAP = "LOW_TOP5_OVERLAP"
LEADER_MISMATCH = "LEADER_MISMATCH"
MULTI_THEME_STOCK_CONFLICT = "MULTI_THEME_STOCK_CONFLICT"
SOURCE_COVERAGE_GAP = "SOURCE_COVERAGE_GAP"
EMPTY_EXTERNAL_TOP_STOCKS = "EMPTY_EXTERNAL_TOP_STOCKS"
EMPTY_INTERNAL_TOP_STOCKS = "EMPTY_INTERNAL_TOP_STOCKS"


def compare_theme_benchmarks(
    external_snapshot: dict,
    internal_snapshot: dict,
    repository: ThemeEngineRepository | None = None,
    alias_map: dict[str, str] | None = None,
) -> ThemeBenchmarkComparison:
    external_themes = list(external_snapshot.get("themes") or [])
    internal_themes = list(internal_snapshot.get("themes") or [])
    internal_index = _InternalThemeIndex(internal_themes)
    external_stock_counts = _stock_theme_counts(external_themes)
    internal_stock_counts = _stock_theme_counts(internal_themes)

    matched_internal_ids: set[str] = set()
    comparisons: list[ThemeBenchmarkComparisonTheme] = []
    missing_external_themes: list[dict[str, Any]] = []
    alias_candidates: list[dict[str, Any]] = []

    for external_theme in external_themes:
        match = _match_theme(external_theme, internal_index, repository, alias_map or {})
        if match is None:
            missing = _missing_external_theme_dict(external_theme)
            missing_external_themes.append(missing)
            alias_candidates.append(_alias_candidate_dict(external_theme))
            continue
        internal_theme, match_method = match
        matched_internal_ids.add(str(internal_theme.get("theme_id") or ""))
        comparisons.append(
            _compare_theme(
                external_theme,
                internal_theme,
                match_method,
                external_stock_counts,
                internal_stock_counts,
            )
        )

    internal_only_themes = [
        _internal_only_theme_dict(theme)
        for theme in internal_themes
        if str(theme.get("theme_id") or "") not in matched_internal_ids
    ]
    summary = _summary(external_themes, internal_themes, comparisons, missing_external_themes, internal_only_themes)
    return {
        "summary": summary,
        "themes": comparisons,
        "missing_external_themes": missing_external_themes,
        "internal_only_themes": internal_only_themes,
        "alias_candidates": alias_candidates,
    }


class _InternalThemeIndex:
    def __init__(self, themes: list[dict]) -> None:
        self.themes = themes
        self.by_id = {str(theme.get("theme_id") or ""): theme for theme in themes if str(theme.get("theme_id") or "")}
        self.by_normalized_name: dict[str, dict] = {}
        for theme in themes:
            normalized = normalize_theme_name(str(theme.get("theme_name") or ""))
            if normalized and normalized not in self.by_normalized_name:
                self.by_normalized_name[normalized] = theme

    def match_target(self, value: str) -> dict | None:
        if not value:
            return None
        if value in self.by_id:
            return self.by_id[value]
        normalized = normalize_theme_name(value)
        if normalized in self.by_normalized_name:
            return self.by_normalized_name[normalized]
        return None


def _match_theme(
    external_theme: dict,
    internal_index: _InternalThemeIndex,
    repository: ThemeEngineRepository | None,
    alias_map: dict[str, str],
) -> tuple[dict, str] | None:
    external_name = str(external_theme.get("external_theme_name") or "")
    hint = str(external_theme.get("canonical_theme_hint") or "")
    if hint:
        matched = internal_index.match_target(hint)
        if matched is not None:
            return matched, "canonical_theme_hint"
    if repository is not None:
        for value in (hint, external_name):
            if not value:
                continue
            theme_id = repository.find_alias(value)
            matched = internal_index.match_target(theme_id or "")
            if matched is not None:
                return matched, "theme_aliases"
    for value in (hint, external_name):
        mapped = _alias_map_value(alias_map, value)
        matched = internal_index.match_target(mapped)
        if matched is not None:
            return matched, "alias_map"
    matched = internal_index.by_normalized_name.get(normalize_theme_name(external_name))
    if matched is not None:
        return matched, "normalized_theme_name"
    return None


def _compare_theme(
    external_theme: dict,
    internal_theme: dict,
    match_method: str,
    external_stock_counts: dict[str, int],
    internal_stock_counts: dict[str, int],
) -> ThemeBenchmarkComparisonTheme:
    external_members = _stock_codes(external_theme.get("members"))
    internal_members = _stock_codes(internal_theme.get("members"))
    external_top5 = _top_stock_codes(external_theme.get("top_stocks"))
    internal_top5 = _top_stock_codes(internal_theme.get("top_stocks"))
    member_overlap = external_members & internal_members
    member_union = external_members | internal_members
    top5_overlap = external_top5 & internal_top5
    top5_denominator = len(external_top5) if external_top5 else 0
    external_leader = _external_leader(external_theme)
    internal_leader = _internal_leader(internal_theme)
    mismatch_reasons: list[str] = []

    member_jaccard = (len(member_overlap) / len(member_union)) if member_union else 0.0
    top5_overlap_ratio = (len(top5_overlap) / top5_denominator) if top5_denominator else 0.0
    leader_match = bool(external_leader and internal_leader and external_leader == internal_leader)
    leader_rank_delta = _leader_rank_delta(external_theme, internal_theme, external_leader)

    if not external_top5:
        mismatch_reasons.append(EMPTY_EXTERNAL_TOP_STOCKS)
    if not internal_top5:
        mismatch_reasons.append(EMPTY_INTERNAL_TOP_STOCKS)
    if member_jaccard < 0.30:
        mismatch_reasons.append(LOW_MEMBER_OVERLAP)
    if top5_overlap_ratio < 0.40:
        mismatch_reasons.append(LOW_TOP5_OVERLAP)
    if not leader_match:
        mismatch_reasons.append(LEADER_MISMATCH)
    if any(external_stock_counts.get(code, 0) > 1 or internal_stock_counts.get(code, 0) > 1 for code in member_overlap | top5_overlap):
        mismatch_reasons.append(MULTI_THEME_STOCK_CONFLICT)
    if _has_source_coverage_gap(internal_theme):
        mismatch_reasons.append(SOURCE_COVERAGE_GAP)

    return {
        "external_theme_name": str(external_theme.get("external_theme_name") or ""),
        "canonical_theme_hint": str(external_theme.get("canonical_theme_hint") or ""),
        "internal_theme_id": str(internal_theme.get("theme_id") or ""),
        "internal_theme_name": str(internal_theme.get("theme_name") or ""),
        "match_method": match_method,
        "external_rank": _int_value(external_theme.get("rank")),
        "internal_rank": _int_value(internal_theme.get("rank")),
        "member_overlap_count": len(member_overlap),
        "member_jaccard_score": round(member_jaccard, 4),
        "top5_overlap_count": len(top5_overlap),
        "top5_overlap_ratio": round(top5_overlap_ratio, 4),
        "leader_match": leader_match,
        "leader_rank_delta": leader_rank_delta,
        "external_only_stocks": _ordered_difference(_ordered_stock_codes(external_theme.get("members")), internal_members),
        "internal_only_stocks": _ordered_difference(_ordered_stock_codes(internal_theme.get("members")), external_members),
        "matched_stocks": _ordered_intersection(_ordered_stock_codes(external_theme.get("members")), internal_members),
        "mismatch_reasons": _dedupe(mismatch_reasons),
    }


def _summary(
    external_themes: list[dict],
    internal_themes: list[dict],
    comparisons: list[ThemeBenchmarkComparisonTheme],
    missing_external_themes: list[dict[str, Any]],
    internal_only_themes: list[dict[str, Any]],
) -> dict[str, Any]:
    matched_count = len(comparisons)
    leader_matches = sum(1 for item in comparisons if item["leader_match"])
    return {
        "external_theme_count": len(external_themes),
        "internal_theme_count": len(internal_themes),
        "matched_theme_count": matched_count,
        "avg_member_jaccard": round(_average([item["member_jaccard_score"] for item in comparisons]), 4),
        "avg_top5_overlap": round(_average([item["top5_overlap_ratio"] for item in comparisons]), 4),
        "leader_match_rate": round((leader_matches / matched_count) if matched_count else 0.0, 4),
        "top10_theme_rank_overlap": round(_rank_overlap(comparisons, 10), 4),
        "top20_theme_rank_overlap": round(_rank_overlap(comparisons, 20), 4),
        "missing_external_theme_count": len(missing_external_themes),
        "internal_only_theme_count": len(internal_only_themes),
    }


def _rank_overlap(comparisons: list[ThemeBenchmarkComparisonTheme], top_n: int) -> float:
    external_top_count = sum(1 for item in comparisons if item["external_rank"] <= top_n)
    if external_top_count == 0:
        return 0.0
    both_top = sum(1 for item in comparisons if item["external_rank"] <= top_n and item["internal_rank"] <= top_n)
    return both_top / external_top_count


def _missing_external_theme_dict(external_theme: dict) -> dict[str, Any]:
    return {
        "external_theme_name": str(external_theme.get("external_theme_name") or ""),
        "canonical_theme_hint": str(external_theme.get("canonical_theme_hint") or ""),
        "external_rank": _int_value(external_theme.get("rank")),
        "mismatch_reasons": [INTERNAL_THEME_MISSING, THEME_ALIAS_MISSING],
    }


def _internal_only_theme_dict(internal_theme: dict) -> dict[str, Any]:
    return {
        "internal_theme_id": str(internal_theme.get("theme_id") or ""),
        "internal_theme_name": str(internal_theme.get("theme_name") or ""),
        "internal_rank": _int_value(internal_theme.get("rank")),
        "mismatch_reasons": [EXTERNAL_THEME_MISSING],
    }


def _alias_candidate_dict(external_theme: dict) -> dict[str, Any]:
    external_name = str(external_theme.get("external_theme_name") or "")
    hint = str(external_theme.get("canonical_theme_hint") or "")
    return {
        "external_theme_name": external_name,
        "canonical_theme_hint": hint,
        "normalized_external_theme_name": normalize_theme_name(external_name),
        "normalized_canonical_theme_hint": normalize_theme_name(hint),
        "mismatch_reasons": [THEME_ALIAS_MISSING, INTERNAL_THEME_MISSING],
    }


def _stock_theme_counts(themes: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for theme in themes:
        codes = _stock_codes(theme.get("members")) | _stock_codes(theme.get("top_stocks"))
        for code in codes:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _stock_codes(values: Any) -> set[str]:
    return set(_ordered_stock_codes(values))


def _ordered_stock_codes(values: Any) -> list[str]:
    result: list[str] = []
    for item in list(values or []):
        if not isinstance(item, dict):
            continue
        code = normalize_stock_code(str(item.get("stock_code") or ""))
        if code and code not in result:
            result.append(code)
    return result


def _top_stock_codes(values: Any) -> set[str]:
    ordered = sorted(
        [dict(item) for item in list(values or []) if isinstance(item, dict)],
        key=lambda item: _int_value(item.get("rank")),
    )
    return set(_ordered_stock_codes(ordered[:5]))


def _external_leader(external_theme: dict) -> str:
    stocks = sorted(
        [dict(item) for item in list(external_theme.get("top_stocks") or []) if isinstance(item, dict)],
        key=lambda item: _int_value(item.get("rank")),
    )
    return normalize_stock_code(str(stocks[0].get("stock_code") or "")) if stocks else ""


def _internal_leader(internal_theme: dict) -> str:
    leader = normalize_stock_code(str(internal_theme.get("leader_code") or ""))
    if leader:
        return leader
    return _external_leader({"top_stocks": internal_theme.get("top_stocks")})


def _leader_rank_delta(external_theme: dict, internal_theme: dict, external_leader: str) -> int | None:
    if not external_leader:
        return None
    external_rank = _stock_rank(external_theme.get("top_stocks"), external_leader)
    internal_rank = _stock_rank(internal_theme.get("top_stocks"), external_leader)
    if external_rank is None or internal_rank is None:
        return None
    return internal_rank - external_rank


def _stock_rank(values: Any, stock_code: str) -> int | None:
    for fallback_rank, item in enumerate(list(values or []), start=1):
        if not isinstance(item, dict):
            continue
        code = normalize_stock_code(str(item.get("stock_code") or ""))
        if code == stock_code:
            return _int_value(item.get("rank"), default=fallback_rank)
    return None


def _ordered_difference(values: list[str], other: set[str]) -> list[str]:
    return [code for code in values if code not in other]


def _ordered_intersection(values: list[str], other: set[str]) -> list[str]:
    return [code for code in values if code in other]


def _has_source_coverage_gap(internal_theme: dict) -> bool:
    quality = dict(internal_theme.get("snapshot_quality") or {})
    coverage = float(quality.get("snapshot_coverage") or 0.0)
    missing = int(quality.get("missing_snapshot_count") or 0)
    return bool(quality) and (coverage < 0.5 or missing > 0)


def _alias_map_value(alias_map: dict[str, str], value: str) -> str:
    if not value:
        return ""
    if value in alias_map:
        return str(alias_map[value])
    normalized = normalize_theme_name(value)
    return str(alias_map.get(normalized) or "")


def _int_value(value: Any, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return int(default)
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return int(default)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
