from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from trading.strategy.candidates import (
    QUALITY_ACTIONABLE,
    QUALITY_DATA_WAIT,
    QUALITY_DISCOVERY_ONLY,
    QUALITY_INVALID_CODE,
    QUALITY_UNMAPPED,
    candidate_is_discovery_only,
    candidate_quality_status,
)
from trading.strategy.models import BlockType, Candidate, CandidateState, StrategyProfile

if TYPE_CHECKING:
    from storage.db import TradingDatabase


ACTIVE_READINESS_STATES = {
    CandidateState.DETECTED,
    CandidateState.WATCHING,
    CandidateState.READY,
}
LOW_THEME_MAPPING_COVERAGE_PCT = 50.0


@dataclass
class ReadinessReport:
    condition_profiles_count: int = 0
    unresolved_condition_profiles_count: int = 0
    theme_mappings_count: int = 0
    enabled_theme_mappings_count: int = 0
    active_candidates_count: int = 0
    active_candidates_with_theme_mapping: int = 0
    active_candidates_without_theme_mapping: int = 0
    theme_mapping_coverage_pct: float = 0.0
    quality_actionable_count: int = 0
    quality_discovery_only_count: int = 0
    quality_unmapped_count: int = 0
    quality_invalid_code_count: int = 0
    quality_data_wait_count: int = 0
    market_session_status: str = "open"
    data_warmup_status: str = "ready"
    gate_skip_reason: str = ""
    candidate_subscription_selected_count: int = 0
    candidate_subscription_skipped_discovery_count: int = 0
    candidate_subscription_skipped_unmapped_count: int = 0
    protected_subscription_usage: str = ""
    warnings: list[str] = field(default_factory=list)


def build_readiness_report(
    db: "TradingDatabase",
    *,
    trade_date: Optional[str] = None,
    subscription_manager=None,
    extra_warnings: Optional[list[str]] = None,
    market_session_status: str = "open",
    data_warmup_status: str = "ready",
    gate_skip_reason: str = "",
    candidate_subscription_selected_count: int = 0,
    candidate_subscription_skipped_discovery_count: int = 0,
    candidate_subscription_skipped_unmapped_count: int = 0,
) -> ReadinessReport:
    profiles = db.list_condition_profiles(enabled=None)
    enabled_profiles = [profile for profile in profiles if profile.enabled]
    unresolved = [profile for profile in enabled_profiles if profile.last_resolved_index is None]
    theme_mappings = db.list_theme_mappings(enabled=None)
    enabled_theme_mappings = [mapping for mapping in theme_mappings if mapping.enabled]
    candidates = _active_candidates(db, trade_date)
    mapping_by_code = {
        candidate.code: bool(db.theme_mappings_for_code(candidate.code, enabled=True))
        for candidate in candidates
    }
    quality_counts = {
        QUALITY_ACTIONABLE: 0,
        QUALITY_DISCOVERY_ONLY: 0,
        QUALITY_UNMAPPED: 0,
        QUALITY_INVALID_CODE: 0,
        QUALITY_DATA_WAIT: 0,
    }
    for candidate in candidates:
        quality_counts[candidate_quality_status(candidate, mapping_by_code.get(candidate.code, False))] += 1
    mapped_count = sum(1 for candidate in candidates if mapping_by_code.get(candidate.code, False))
    active_count = len(candidates)
    unmapped_count = active_count - mapped_count
    coverage_pct = round((mapped_count / active_count) * 100.0, 2) if active_count else 0.0

    warnings = list(extra_warnings or [])
    if not theme_mappings:
        warnings.append("THEME_MAPPING_EMPTY")
    if active_count and coverage_pct < LOW_THEME_MAPPING_COVERAGE_PCT:
        warnings.append("NO_THEME_MAPPING_FOR_ACTIVE_CANDIDATES")
    if quality_counts[QUALITY_INVALID_CODE]:
        warnings.append("INVALID_CODE_ACTIVE_CANDIDATES")
    for profile in unresolved:
        warnings.append(f"CONDITION_PROFILE_UNRESOLVED:{profile.condition_name}")
    if _broad_candidates_only(candidates):
        warnings.append("BROAD_CANDIDATES_ONLY")
    if gate_skip_reason:
        warnings.append(gate_skip_reason)

    return ReadinessReport(
        condition_profiles_count=len(profiles),
        unresolved_condition_profiles_count=len(unresolved),
        theme_mappings_count=len(theme_mappings),
        enabled_theme_mappings_count=len(enabled_theme_mappings),
        active_candidates_count=active_count,
        active_candidates_with_theme_mapping=mapped_count,
        active_candidates_without_theme_mapping=unmapped_count,
        theme_mapping_coverage_pct=coverage_pct,
        quality_actionable_count=quality_counts[QUALITY_ACTIONABLE],
        quality_discovery_only_count=quality_counts[QUALITY_DISCOVERY_ONLY],
        quality_unmapped_count=quality_counts[QUALITY_UNMAPPED],
        quality_invalid_code_count=quality_counts[QUALITY_INVALID_CODE],
        quality_data_wait_count=quality_counts[QUALITY_DATA_WAIT],
        market_session_status=market_session_status,
        data_warmup_status=data_warmup_status,
        gate_skip_reason=gate_skip_reason,
        candidate_subscription_selected_count=int(candidate_subscription_selected_count),
        candidate_subscription_skipped_discovery_count=int(candidate_subscription_skipped_discovery_count),
        candidate_subscription_skipped_unmapped_count=int(candidate_subscription_skipped_unmapped_count),
        protected_subscription_usage=_protected_subscription_usage(subscription_manager),
        warnings=dedupe_warnings(warnings),
    )


def dedupe_warnings(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result


def _active_candidates(db: "TradingDatabase", trade_date: Optional[str]) -> list[Candidate]:
    candidates = db.list_candidates(trade_date=trade_date) if trade_date else db.list_candidates()
    result: list[Candidate] = []
    for candidate in candidates:
        if candidate.state in ACTIVE_READINESS_STATES:
            result.append(candidate)
        elif (
            candidate.state == CandidateState.BLOCKED
            and candidate.block_type == BlockType.TEMPORARY
            and candidate.can_recover
        ):
            result.append(candidate)
        elif _has_open_virtual_activity(db, candidate):
            result.append(candidate)
    return result


def _has_open_virtual_activity(db: "TradingDatabase", candidate: Candidate) -> bool:
    if candidate.id is None:
        return False
    if db.load_open_virtual_position(candidate.id) is not None:
        return True
    return any(order.status.value == "submitted" for order in db.list_virtual_orders(candidate.id))


def _broad_candidates_only(candidates: list[Candidate]) -> bool:
    if not candidates:
        return False
    broad_count = sum(1 for candidate in candidates if _is_broad_candidate(candidate))
    return broad_count == len(candidates)


def _is_broad_candidate(candidate: Candidate) -> bool:
    return candidate_is_discovery_only(candidate)


def _protected_subscription_usage(subscription_manager) -> str:
    if subscription_manager is None:
        return ""
    records = getattr(subscription_manager, "records", {})
    protected_count = sum(1 for record in records.values() if getattr(record, "protected", False))
    max_codes = int(getattr(subscription_manager, "max_codes", 0) or 0)
    return f"{protected_count}/{max_codes}" if max_codes else str(protected_count)
