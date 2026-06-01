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
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.models import ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.universe import ThemeUniverseBuilder

if TYPE_CHECKING:
    from storage.db import TradingDatabase


ACTIVE_READINESS_STATES = {
    CandidateState.DETECTED,
    CandidateState.WATCHING,
    CandidateState.READY,
}
LOW_THEME_CONTEXT_COVERAGE_PCT = 50.0


@dataclass
class ReadinessReport:
    condition_profiles_count: int = 0
    unresolved_condition_profiles_count: int = 0
    active_theme_count: int = 0
    watch_theme_count: int = 0
    candidate_theme_count: int = 0
    theme_active_stock_count: int = 0
    theme_last_sync_at: str = ""
    theme_last_tick_at: str = ""
    theme_ws_client_count: int = 0
    top_theme_name: str = ""
    top_theme_score: float = 0.0
    theme_engine_status: str = "stopped"
    theme_data_status: str = "warming"
    active_candidates_count: int = 0
    active_candidates_with_active_theme: int = 0
    active_candidates_without_active_theme: int = 0
    theme_context_coverage_pct: float = 0.0
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
    condition_registration_status: list[dict] = field(default_factory=list)
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
    theme_engine_mode: str = "themelab_flow",
    theme_lab_flow_wired: bool = False,
    condition_adapter=None,
) -> ReadinessReport:
    profiles = db.list_condition_profiles(enabled=None)
    enabled_profiles = [profile for profile in profiles if profile.enabled]
    unresolved = [profile for profile in enabled_profiles if profile.last_resolved_index is None]
    theme_repository = ThemeEngineRepository(db)
    theme_provider = DynamicThemeContextProvider(theme_repository)
    theme_universe = ThemeUniverseBuilder(theme_repository)
    canonical_themes = theme_repository.list_canonical_themes()
    active_themes = [theme for theme in canonical_themes if theme.status == ThemeStatus.ACTIVE]
    watch_themes = [theme for theme in canonical_themes if theme.status == ThemeStatus.WATCH]
    candidate_themes = [theme for theme in canonical_themes if theme.status == ThemeStatus.CANDIDATE]
    theme_ready = theme_provider.is_ready()
    last_sync = theme_repository.latest_source_sync_run()
    latest_rank = theme_repository.get_latest_theme_rank(1)
    top_theme = latest_rank[0] if latest_rank else None
    candidates = _active_candidates(db, trade_date)
    dynamic_theme_by_code = _active_theme_presence_by_code(db, candidates)
    quality_counts = {
        QUALITY_ACTIONABLE: 0,
        QUALITY_DISCOVERY_ONLY: 0,
        QUALITY_UNMAPPED: 0,
        QUALITY_INVALID_CODE: 0,
        QUALITY_DATA_WAIT: 0,
    }
    for candidate in candidates:
        quality_counts[candidate_quality_status(candidate, dynamic_theme_by_code.get(candidate.code, False))] += 1
    mapped_count = sum(1 for candidate in candidates if dynamic_theme_by_code.get(candidate.code, False))
    active_count = len(candidates)
    unmapped_count = active_count - mapped_count
    coverage_pct = round((mapped_count / active_count) * 100.0, 2) if active_count else 0.0

    warnings = list(extra_warnings or [])
    if not theme_ready:
        warnings.append("THEME_CONTEXT_NOT_READY")
    if active_count and coverage_pct < LOW_THEME_CONTEXT_COVERAGE_PCT:
        warnings.append("NO_ACTIVE_THEME_FOR_ACTIVE_CANDIDATES")
    if quality_counts[QUALITY_INVALID_CODE]:
        warnings.append("INVALID_CODE_ACTIVE_CANDIDATES")
    for profile in unresolved:
        warnings.append(f"CONDITION_PROFILE_UNRESOLVED:{profile.condition_name}")
    if theme_engine_mode == "themelab_flow":
        if not theme_lab_flow_wired:
            warnings.append("THEME_LAB_FLOW_NOT_WIRED")
        lab_profiles = {profile.purpose: profile for profile in profiles}
        for key, purpose in {
            "ALIVE": "theme_lab_alive",
            "STRONG": "theme_lab_strong",
            "LEADER": "theme_lab_leader",
        }.items():
            profile = lab_profiles.get(purpose)
            if profile is None or profile.last_resolved_index is None:
                warnings.append(f"THEME_LAB_CONDITION_{key}_UNRESOLVED")
        if theme_repository.count_current_memberships() <= 0:
            warnings.append("THEME_LAB_MAPPING_EMPTY")
    if _broad_candidates_only(candidates):
        warnings.append("BROAD_CANDIDATES_ONLY")
    if gate_skip_reason:
        warnings.append(gate_skip_reason)

    return ReadinessReport(
        condition_profiles_count=len(profiles),
        unresolved_condition_profiles_count=len(unresolved),
        active_theme_count=len(active_themes),
        watch_theme_count=len(watch_themes),
        candidate_theme_count=len(candidate_themes),
        theme_active_stock_count=len(theme_universe.build_active_universe()),
        theme_last_sync_at=last_sync.finished_at if last_sync else "",
        top_theme_name=top_theme.theme_name if top_theme else "",
        top_theme_score=top_theme.theme_score if top_theme else 0.0,
        theme_engine_status="running" if theme_ready else "stopped",
        theme_data_status="ready" if theme_ready else "warming",
        active_candidates_count=active_count,
        active_candidates_with_active_theme=mapped_count,
        active_candidates_without_active_theme=unmapped_count,
        theme_context_coverage_pct=coverage_pct,
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
        condition_registration_status=_condition_registration_status(profiles, condition_adapter),
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


def _active_theme_presence_by_code(
    db: "TradingDatabase",
    candidates: list[Candidate],
    *,
    default_when_not_ready: bool = False,
) -> dict[str, bool]:
    codes = sorted({str(candidate.code or "").strip() for candidate in candidates if str(candidate.code or "").strip()})
    result = {code: False for code in codes}
    if not codes:
        return result
    conn = getattr(db, "conn", None)
    if conn is None:
        return result
    ready_row = conn.execute("SELECT COUNT(*) AS count FROM theme_membership_current").fetchone()
    if not int((ready_row or {})["count"] if ready_row else 0):
        return {code: bool(default_when_not_ready) for code in codes}
    active_statuses = (ThemeStatus.ACTIVE.value, ThemeStatus.WATCH.value)
    for start in range(0, len(codes), 500):
        chunk = codes[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT DISTINCT m.stock_code
            FROM theme_membership_current m
            JOIN canonical_themes c ON c.theme_id = m.theme_id
            WHERE m.active = 1
              AND c.status IN (?, ?)
              AND m.stock_code IN ({placeholders})
            """,
            (*active_statuses, *chunk),
        ).fetchall()
        for row in rows:
            result[str(row["stock_code"])] = True
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


def _condition_registration_status(profiles, condition_adapter) -> list[dict]:
    registered = getattr(condition_adapter, "registered_conditions", {}) if condition_adapter is not None else {}
    event_counts = getattr(condition_adapter, "condition_event_counts", {}) if condition_adapter is not None else {}
    by_name = {condition.condition_name: condition for condition in registered.values()} if isinstance(registered, dict) else {}
    statuses: list[dict] = []
    for profile in profiles:
        condition = by_name.get(profile.condition_name)
        counts = event_counts.get(profile.condition_name, {}) if isinstance(event_counts, dict) else {}
        warning = ""
        if profile.enabled and profile.last_resolved_index is None:
            warning = "CONDITION_PROFILE_UNRESOLVED"
        statuses.append(
            {
                "condition_name": profile.condition_name,
                "purpose": profile.purpose,
                "resolved_index": profile.last_resolved_index,
                "registered": condition is not None,
                "screen_no": condition.screen_no if condition is not None else "",
                "last_event_at": str(counts.get("last_event_at") or ""),
                "include_count": int(counts.get("include_count") or 0),
                "remove_count": int(counts.get("remove_count") or 0),
                "warning": warning,
            }
        )
    return statuses
