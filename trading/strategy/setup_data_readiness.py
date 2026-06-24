from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code


class RealtimeCoverageType(str, Enum):
    NONE = "NONE"
    CANDIDATE = "CANDIDATE"
    THEME_BOARD = "THEME_BOARD"
    OPENING_SEED = "OPENING_SEED"
    THEME_EXPANSION = "THEME_EXPANSION"
    POSITION_PROTECTED = "POSITION_PROTECTED"
    MULTI_SOURCE = "MULTI_SOURCE"


class LeaseRequirement(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED_EXACT_THEME = "REQUIRED_EXACT_THEME"
    UNKNOWN = "UNKNOWN"


class SetupDataReadinessStatus(str, Enum):
    READY = "READY"
    WAIT_SUBSCRIPTION_NOT_ACTIVE = "WAIT_SUBSCRIPTION_NOT_ACTIVE"
    WAIT_SUBSCRIPTION_BUDGET = "WAIT_SUBSCRIPTION_BUDGET"
    WAIT_POST_SUBSCRIPTION_TICK = "WAIT_POST_SUBSCRIPTION_TICK"
    WAIT_SELECTED_THEME_LEASE = "WAIT_SELECTED_THEME_LEASE"
    WAIT_REALTIME_TICK_STALE = "WAIT_REALTIME_TICK_STALE"
    WAIT_THEME_SIGNAL_STALE = "WAIT_THEME_SIGNAL_STALE"
    WAIT_MARKET_CONTEXT = "WAIT_MARKET_CONTEXT"
    WAIT_STRATEGY_CONTEXT = "WAIT_STRATEGY_CONTEXT"
    WAIT_CANDLE_WARMUP = "WAIT_CANDLE_WARMUP"
    ERROR = "ERROR"


GENERAL_SUBSCRIPTION_READY = "GENERAL_REALTIME_SUBSCRIPTION_READY"
THEME_EXPANSION_READY = "THEME_EXPANSION_REALTIME_READY"
SELECTED_THEME_LEASE_NOT_REQUIRED = "SELECTED_THEME_LEASE_NOT_REQUIRED"
OTHER_THEME_LEASE_IGNORED = "OTHER_THEME_LEASE_IGNORED"
SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING = "SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING"
SUBSCRIPTION_BUDGET_DEFERRED = "SUBSCRIPTION_BUDGET_DEFERRED"


@dataclass(frozen=True)
class SetupDataReadinessSnapshot:
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    candidate_instance_id: str
    code: str
    selected_theme_id: str = ""
    evaluation_eligible: bool = True
    readiness_status: str = SetupDataReadinessStatus.ERROR.value
    readiness_ready: bool = False
    coverage_type: str = RealtimeCoverageType.NONE.value
    subscription_active: bool = False
    subscription_selected: bool = False
    subscription_sources: tuple[str, ...] = ()
    subscription_primary_source: str = ""
    subscription_screen_no: str = ""
    subscription_generation: int = 0
    subscription_active_since: str = ""
    relevant_source_added_at: str = ""
    subscription_budget_deferred: bool = False
    expansion_lease_required: bool = False
    expansion_lease_requirement: str = LeaseRequirement.NOT_REQUIRED.value
    expansion_lease_requirement_reason: str = ""
    exact_theme_lease_present: bool = False
    exact_theme_lease_active: bool = False
    exact_theme_lease_status: str = ""
    exact_theme_lease_selected_at: str = ""
    exact_theme_lease_first_active_at: str = ""
    latest_tick_at: str = ""
    latest_tick_age_sec: float = 999999.0
    latest_tick_source: str = ""
    post_subscription_tick_verified: bool = False
    gateway_tick_at: str = ""
    core_tick_at: str = ""
    strategy_context_tick_at: str = ""
    setup_feature_tick_at: str = ""
    market_context_fresh: bool = True
    theme_context_fresh: bool = True
    candle_ready: bool = True
    baseline_at: str = ""
    reason_codes: tuple[str, ...] = ()
    informational_reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def build_setup_data_readiness(
    *,
    trade_date: str,
    calculated_at: datetime,
    candidate: Any,
    candidate_instance_id: str,
    selected_theme_id: str,
    context: Mapping[str, Any],
    subscription: Mapping[str, Any],
    exact_lease: Mapping[str, Any] | None,
    other_theme_lease_count: int = 0,
    evaluation_eligible: bool = True,
    max_tick_age_sec: int = 10,
    min_completed_1m_candles: int = 0,
    completed_1m_count: int = 0,
) -> SetupDataReadinessSnapshot:
    current = calculated_at.replace(microsecond=0)
    code = normalize_code(str(getattr(candidate, "code", "") or subscription.get("code") or ""))
    data = dict(context.get("data") or {})
    lease = dict(exact_lease or {})
    requirement, requirement_reason = _lease_requirement(candidate, context, selected_theme_id)
    lease_required = requirement == LeaseRequirement.REQUIRED_EXACT_THEME
    active_statuses = {"ACTIVE", "HOLDING", "PROTECTED"}
    exact_lease_status = str(lease.get("status") or "")
    exact_lease_active = bool(lease) and exact_lease_status.upper() in active_statuses
    subscription_sources = tuple(sorted(str(source) for source in list(subscription.get("subscription_sources") or subscription.get("sources") or []) if str(source)))
    subscription_active = bool(subscription.get("subscription_active") or subscription.get("active"))
    subscription_selected = bool(subscription.get("subscription_selected") or subscription_sources)
    budget_deferred = bool(subscription.get("subscription_budget_deferred") or subscription.get("budget_deferred"))
    latest_tick_at = str(subscription.get("latest_tick_at") or subscription.get("core_tick_at") or "")
    tick_age_sec = _float(subscription.get("latest_tick_age_sec"), 999999.0)
    latest_tick_source = str(subscription.get("latest_tick_source") or "")
    price_source = latest_tick_source.upper()
    baseline_at = _max_time_text(
        subscription.get("subscription_active_since"),
        subscription.get("relevant_source_added_at"),
        _candidate_eligible_at(candidate),
    )
    if lease_required:
        baseline_at = _max_time_text(
            baseline_at,
            lease.get("selected_at") or lease.get("selected_tick_baseline_at"),
            lease.get("first_active_at"),
        )
    tick_dt = _parse_time(latest_tick_at)
    baseline_dt = _parse_time(baseline_at)
    tick_after_baseline = bool(tick_dt is not None and (baseline_dt is None or tick_dt >= baseline_dt))
    realtime_tick_fresh = bool(latest_tick_at and price_source == "REALTIME" and tick_age_sec <= max(1, int(max_tick_age_sec)) and tick_after_baseline)
    market_fresh = bool(data.get("market_context_fresh", True))
    theme_fresh = bool(data.get("theme_context_fresh", True))
    context_fresh = bool(context.get("context_fresh")) if context else False
    context_reasons = [str(reason) for reason in list(context.get("reason_codes") or []) + list(data.get("blocking_reason_codes") or [])]
    signal_stale = any("SIGNAL_STALE" in reason.upper() for reason in context_reasons)
    candle_ready = completed_1m_count >= max(0, int(min_completed_1m_candles or 0))

    status = SetupDataReadinessStatus.READY
    reasons: list[str] = []
    informational: list[str] = []
    if not evaluation_eligible:
        status = SetupDataReadinessStatus.ERROR
        reasons.append("CANDIDATE_NOT_EVALUATION_ELIGIBLE")
    elif budget_deferred:
        status = SetupDataReadinessStatus.WAIT_SUBSCRIPTION_BUDGET
        reasons.append(SUBSCRIPTION_BUDGET_DEFERRED)
    elif not subscription_selected or not subscription_active:
        status = SetupDataReadinessStatus.WAIT_SUBSCRIPTION_NOT_ACTIVE
        reasons.append("SUBSCRIPTION_NOT_ACTIVE")
    elif lease_required and not exact_lease_active:
        status = SetupDataReadinessStatus.WAIT_SELECTED_THEME_LEASE
        reasons.append(SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING)
    elif not latest_tick_at:
        status = SetupDataReadinessStatus.WAIT_POST_SUBSCRIPTION_TICK
        reasons.append("ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK")
    elif price_source != "REALTIME" or tick_age_sec > max(1, int(max_tick_age_sec)):
        status = SetupDataReadinessStatus.WAIT_REALTIME_TICK_STALE
        reasons.append("LATEST_TICK_STALE")
    elif not tick_after_baseline:
        status = SetupDataReadinessStatus.WAIT_POST_SUBSCRIPTION_TICK
        reasons.append("ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK")
    elif not context_fresh:
        status = SetupDataReadinessStatus.WAIT_STRATEGY_CONTEXT
        reasons.append("STRATEGY_CONTEXT_REFRESH_LAG")
    elif signal_stale or not theme_fresh:
        status = SetupDataReadinessStatus.WAIT_THEME_SIGNAL_STALE
        reasons.append("SIGNAL_STALE")
    elif not market_fresh:
        status = SetupDataReadinessStatus.WAIT_MARKET_CONTEXT
        reasons.append("MARKET_CONTEXT_NOT_FRESH")
    elif not candle_ready:
        status = SetupDataReadinessStatus.WAIT_CANDLE_WARMUP
        reasons.append("COMPLETED_1M_CANDLES_INSUFFICIENT")

    if not lease_required:
        informational.append(SELECTED_THEME_LEASE_NOT_REQUIRED)
    if other_theme_lease_count > 0:
        informational.append(OTHER_THEME_LEASE_IGNORED)
    if status == SetupDataReadinessStatus.READY:
        if lease_required:
            informational.append(THEME_EXPANSION_READY)
        else:
            informational.append(GENERAL_SUBSCRIPTION_READY)

    return SetupDataReadinessSnapshot(
        trade_date=trade_date,
        calculated_at=current.isoformat(),
        candidate_id=getattr(candidate, "id", None),
        candidate_instance_id=candidate_instance_id,
        code=code,
        selected_theme_id=str(selected_theme_id or ""),
        evaluation_eligible=bool(evaluation_eligible),
        readiness_status=status.value,
        readiness_ready=status == SetupDataReadinessStatus.READY,
        coverage_type=str(subscription.get("coverage_type") or RealtimeCoverageType.NONE.value),
        subscription_active=subscription_active,
        subscription_selected=subscription_selected,
        subscription_sources=subscription_sources,
        subscription_primary_source=str(subscription.get("subscription_primary_source") or ""),
        subscription_screen_no=str(subscription.get("subscription_screen_no") or ""),
        subscription_generation=_int(subscription.get("subscription_generation")),
        subscription_active_since=str(subscription.get("subscription_active_since") or ""),
        relevant_source_added_at=str(subscription.get("relevant_source_added_at") or ""),
        subscription_budget_deferred=budget_deferred,
        expansion_lease_required=lease_required,
        expansion_lease_requirement=requirement.value,
        expansion_lease_requirement_reason=requirement_reason,
        exact_theme_lease_present=bool(lease),
        exact_theme_lease_active=exact_lease_active,
        exact_theme_lease_status=exact_lease_status,
        exact_theme_lease_selected_at=str(lease.get("selected_at") or ""),
        exact_theme_lease_first_active_at=str(lease.get("first_active_at") or ""),
        latest_tick_at=latest_tick_at,
        latest_tick_age_sec=round(tick_age_sec, 3),
        latest_tick_source=latest_tick_source,
        post_subscription_tick_verified=bool(realtime_tick_fresh and (not lease_required or exact_lease_active)),
        gateway_tick_at=str(subscription.get("gateway_tick_at") or ""),
        core_tick_at=str(subscription.get("core_tick_at") or latest_tick_at),
        strategy_context_tick_at=str(data.get("tick_at") or data.get("realtime_tick_at") or ""),
        setup_feature_tick_at=latest_tick_at,
        market_context_fresh=market_fresh,
        theme_context_fresh=theme_fresh,
        candle_ready=candle_ready,
        baseline_at=baseline_at,
        reason_codes=tuple(_dedupe(reasons)),
        informational_reason_codes=tuple(_dedupe(informational)),
    )


def _lease_requirement(candidate: Any, context: Mapping[str, Any], selected_theme_id: str) -> tuple[LeaseRequirement, str]:
    data = dict(context.get("data") or {})
    if bool(context.get("selected_theme_lease_required") or context.get("theme_expansion_lease_required") or data.get("selected_theme_lease_required") or data.get("theme_expansion_lease_required")):
        return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPLICIT_CONTEXT_FLAG"
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    if bool(metadata.get("expansion_only") or metadata.get("theme_expansion_only")):
        return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPANSION_ONLY_PROVENANCE"
    selected = str(selected_theme_id or "")
    sources = list(metadata.get("source_events") or []) + list(metadata.get("sources_detail") or [])
    source_type = str(metadata.get("source_type") or metadata.get("source") or "")
    if source_type:
        sources.append({"source_type": source_type, "theme_id": metadata.get("theme_id") or metadata.get("selected_theme_id")})
    for raw in sources:
        item = dict(raw or {}) if isinstance(raw, Mapping) else {"source_type": str(raw or "")}
        raw_type = str(item.get("source_type") or item.get("source") or "").lower()
        source_theme = str(item.get("theme_id") or item.get("selected_theme_id") or "")
        if raw_type in {"reboot_v2_theme_expansion", "theme_expansion"} and (not selected or not source_theme or source_theme == selected):
            return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPANSION_SOURCE_PROVENANCE"
    return LeaseRequirement.NOT_REQUIRED, "GENERAL_SUBSCRIPTION_ALLOWED"


def _candidate_eligible_at(candidate: Any) -> str:
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    for key in ("evaluation_eligible_at", "first_eligible_at"):
        if metadata.get(key):
            return str(metadata.get(key))
    return str(getattr(candidate, "detected_at", "") or getattr(candidate, "last_seen_at", "") or "")


def _max_time_text(*values: Any) -> str:
    parsed = [item for item in (_parse_time(value) for value in values) if item is not None]
    return max(parsed).replace(microsecond=0).isoformat() if parsed else ""


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
