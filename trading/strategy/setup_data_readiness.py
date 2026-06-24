from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.market_action import MARKET_ACTION_UNMAPPED, normalize_market_action


READINESS_SCHEMA_VERSION = "setup_router_readiness.v3"


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
    WAIT_REGISTER_COMMAND = "WAIT_REGISTER_COMMAND"
    WAIT_REGISTER_ACK = "WAIT_REGISTER_ACK"
    WAIT_FIRST_TICK = "WAIT_FIRST_TICK"
    WAIT_POST_SUBSCRIPTION_TICK = "WAIT_POST_SUBSCRIPTION_TICK"
    WAIT_SELECTED_THEME_LEASE = "WAIT_SELECTED_THEME_LEASE"
    WAIT_REALTIME_TICK_STALE = "WAIT_REALTIME_TICK_STALE"
    WAIT_THEME_SIGNAL_STALE = "WAIT_THEME_SIGNAL_STALE"
    WAIT_MARKET_CONTEXT = "WAIT_MARKET_CONTEXT"
    WAIT_MARKET_ACTION = "WAIT_MARKET_ACTION"
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
    readiness_schema_version: str = READINESS_SCHEMA_VERSION
    selected_theme_id: str = ""
    evaluation_eligible: bool = True
    readiness_status: str = SetupDataReadinessStatus.ERROR.value
    readiness_ready: bool = False
    readiness_fingerprint: str = ""
    coverage_type: str = RealtimeCoverageType.NONE.value
    canonical_market_action: str = "DATA_WAIT"
    market_action_normalized: bool = False
    market_action_reason_codes: tuple[str, ...] = ()
    subscription_requested: bool = False
    subscription_target_selected: bool = False
    subscription_active: bool = False
    subscription_selected: bool = False
    subscription_sources: tuple[str, ...] = ()
    subscription_primary_source: str = ""
    subscription_screen_no: str = ""
    subscription_generation: int = 0
    subscription_active_since: str = ""
    relevant_source_added_at: str = ""
    subscription_budget_deferred: bool = False
    subscription_lifecycle_schema_version: str = ""
    subscription_lifecycle_state: str = ""
    command_enqueued: bool = False
    command_dispatched: bool = False
    acked: bool = False
    transport_active: bool = False
    first_tick_verified: bool = False
    decision_fresh: bool = False
    stale: bool = False
    released: bool = False
    failed: bool = False
    register_command_id: str = ""
    registration_ack_baseline_at_utc: str = ""
    first_tick_at_utc: str = ""
    last_tick_at_utc: str = ""
    ack_to_first_tick_ms: float | None = None
    readiness_relevant_source: str = ""
    readiness_relevant_source_reason: str = ""
    readiness_relevant_source_generation: int = 0
    baseline_source_type: str = ""
    candidate_active_source_types: tuple[str, ...] = ()
    candidate_primary_source: str = ""
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
    market = dict(context.get("market") or {})
    normalized_action = normalize_market_action(
        market.get("market_action"),
        side_market_regime=market.get("side_market_regime") or market.get("market_status"),
        global_market_regime=market.get("global_market_regime") or market.get("global_market_status"),
        market_session_status=market.get("market_session_status"),
    )
    subscription_sources = tuple(sorted(str(source) for source in list(subscription.get("subscription_sources") or subscription.get("sources") or []) if str(source)))
    subscription_requested = bool(subscription.get("subscription_requested", subscription.get("requested", bool(subscription_sources) or subscription.get("subscription_selected"))))
    subscription_target_selected = bool(subscription.get("subscription_target_selected", subscription.get("target_selected", subscription.get("subscription_selected"))))
    subscription_active = bool(subscription.get("subscription_active") or subscription.get("active"))
    subscription_selected = subscription_target_selected
    budget_deferred = bool(subscription.get("subscription_budget_deferred") or subscription.get("budget_deferred"))
    lifecycle_state = str(subscription.get("subscription_lifecycle_state") or "")
    command_enqueued = bool(subscription.get("command_enqueued"))
    command_dispatched = bool(subscription.get("command_dispatched"))
    acked = bool(subscription.get("acked"))
    first_tick_verified = bool(subscription.get("first_tick_verified"))
    failed = bool(subscription.get("failed"))
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
    context_reasons = [str(reason) for reason in list(context.get("reason_codes") or []) + list(data.get("blocking_reason_codes") or []) + list(normalized_action.reason_codes)]
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
    elif failed or lifecycle_state == "FAILED":
        status = SetupDataReadinessStatus.ERROR
        reasons.append("REALTIME_SUBSCRIPTION_FAILED")
    elif not subscription_requested:
        status = SetupDataReadinessStatus.WAIT_SUBSCRIPTION_NOT_ACTIVE
        reasons.append("SUBSCRIPTION_NOT_REQUESTED")
    elif not subscription_target_selected:
        status = SetupDataReadinessStatus.WAIT_SUBSCRIPTION_BUDGET
        reasons.append(SUBSCRIPTION_BUDGET_DEFERRED)
    elif command_enqueued and not command_dispatched and not acked:
        status = SetupDataReadinessStatus.WAIT_REGISTER_COMMAND
        reasons.append("REGISTER_COMMAND_ENQUEUED")
    elif command_dispatched and not acked:
        status = SetupDataReadinessStatus.WAIT_REGISTER_ACK
        reasons.append("REGISTER_COMMAND_DISPATCHED")
    elif not subscription_active:
        status = SetupDataReadinessStatus.WAIT_SUBSCRIPTION_NOT_ACTIVE
        reasons.append("SUBSCRIPTION_NOT_ACTIVE")
    elif lease_required and not exact_lease_active:
        status = SetupDataReadinessStatus.WAIT_SELECTED_THEME_LEASE
        reasons.append(SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING)
    elif acked and not first_tick_verified:
        status = SetupDataReadinessStatus.WAIT_FIRST_TICK
        reasons.append("ACKED_WAIT_FIRST_TICK")
    elif not latest_tick_at:
        status = SetupDataReadinessStatus.WAIT_POST_SUBSCRIPTION_TICK
        reasons.append("ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK")
    elif price_source != "REALTIME" or tick_age_sec > max(1, int(max_tick_age_sec)):
        status = SetupDataReadinessStatus.WAIT_REALTIME_TICK_STALE
        reasons.append("LATEST_TICK_STALE")
    elif not tick_after_baseline:
        status = SetupDataReadinessStatus.WAIT_POST_SUBSCRIPTION_TICK
        reasons.append("ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK")
    elif normalized_action.action == "DATA_WAIT" and MARKET_ACTION_UNMAPPED in normalized_action.reason_codes:
        status = SetupDataReadinessStatus.WAIT_MARKET_ACTION
        reasons.append(MARKET_ACTION_UNMAPPED)
    elif signal_stale or not theme_fresh:
        status = SetupDataReadinessStatus.WAIT_THEME_SIGNAL_STALE
        reasons.append("SIGNAL_STALE")
    elif not market_fresh:
        status = SetupDataReadinessStatus.WAIT_MARKET_CONTEXT
        reasons.append("MARKET_CONTEXT_NOT_FRESH")
    elif not context_fresh:
        status = SetupDataReadinessStatus.WAIT_STRATEGY_CONTEXT
        reasons.append("STRATEGY_CONTEXT_REFRESH_LAG")
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

    payload = SetupDataReadinessSnapshot(
        trade_date=trade_date,
        calculated_at=current.isoformat(),
        candidate_id=getattr(candidate, "id", None),
        candidate_instance_id=candidate_instance_id,
        code=code,
        selected_theme_id=str(selected_theme_id or ""),
        evaluation_eligible=bool(evaluation_eligible),
        readiness_status=status.value,
        readiness_ready=status == SetupDataReadinessStatus.READY,
        readiness_fingerprint="",
        coverage_type=str(subscription.get("coverage_type") or RealtimeCoverageType.NONE.value),
        canonical_market_action=normalized_action.action,
        market_action_normalized=normalized_action.normalized,
        market_action_reason_codes=tuple(normalized_action.reason_codes),
        subscription_requested=subscription_requested,
        subscription_target_selected=subscription_target_selected,
        subscription_active=subscription_active,
        subscription_selected=subscription_selected,
        subscription_sources=subscription_sources,
        subscription_primary_source=str(subscription.get("subscription_primary_source") or ""),
        subscription_screen_no=str(subscription.get("subscription_screen_no") or ""),
        subscription_generation=_int(subscription.get("subscription_generation")),
        subscription_active_since=str(subscription.get("subscription_active_since") or ""),
        relevant_source_added_at=str(subscription.get("relevant_source_added_at") or ""),
        subscription_budget_deferred=budget_deferred,
        subscription_lifecycle_schema_version=str(subscription.get("subscription_lifecycle_schema_version") or ""),
        subscription_lifecycle_state=lifecycle_state,
        command_enqueued=command_enqueued,
        command_dispatched=command_dispatched,
        acked=acked,
        transport_active=bool(subscription.get("transport_active")),
        first_tick_verified=first_tick_verified,
        decision_fresh=bool(subscription.get("decision_fresh")),
        stale=bool(subscription.get("stale")),
        released=bool(subscription.get("released")),
        failed=failed,
        register_command_id=str(subscription.get("register_command_id") or ""),
        registration_ack_baseline_at_utc=str(subscription.get("registration_ack_baseline_at_utc") or ""),
        first_tick_at_utc=str(subscription.get("first_tick_at_utc") or ""),
        last_tick_at_utc=str(subscription.get("last_tick_at_utc") or ""),
        ack_to_first_tick_ms=subscription.get("ack_to_first_tick_ms"),
        readiness_relevant_source=str(subscription.get("readiness_relevant_source") or ""),
        readiness_relevant_source_reason=str(subscription.get("readiness_relevant_source_reason") or ""),
        readiness_relevant_source_generation=_int(subscription.get("readiness_relevant_source_generation")),
        baseline_source_type=str(subscription.get("baseline_source_type") or ""),
        candidate_active_source_types=tuple(str(item) for item in list(subscription.get("candidate_active_source_types") or []) if str(item)),
        candidate_primary_source=str(subscription.get("candidate_primary_source") or ""),
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
    fingerprint = _fingerprint(payload.to_dict())
    return replace(payload, readiness_fingerprint=fingerprint)


def _lease_requirement(candidate: Any, context: Mapping[str, Any], selected_theme_id: str) -> tuple[LeaseRequirement, str]:
    data = dict(context.get("data") or {})
    if bool(context.get("selected_theme_lease_required") or context.get("theme_expansion_lease_required") or data.get("selected_theme_lease_required") or data.get("theme_expansion_lease_required")):
        return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPLICIT_CONTEXT_FLAG"
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    active_sources = _active_source_map(metadata)
    selected = str(selected_theme_id or "")
    if active_sources:
        for entry in active_sources:
            source_type = str(entry.get("source_type") or entry.get("source") or "").lower()
            source_theme = str(entry.get("theme_id") or entry.get("selected_theme_id") or "")
            if source_type in {"reboot_v2_theme_expansion", "theme_expansion"} and selected and source_theme == selected:
                return LeaseRequirement.REQUIRED_EXACT_THEME, "ACTIVE_EXPANSION_SOURCE_PROVENANCE"
        if bool(metadata.get("expansion_only") or metadata.get("theme_expansion_only")):
            return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPANSION_ONLY_PROVENANCE"
        return LeaseRequirement.NOT_REQUIRED, "ACTIVE_SOURCE_MAP_GENERAL_SUBSCRIPTION_ALLOWED"
    if bool(metadata.get("expansion_only") or metadata.get("theme_expansion_only")):
        return LeaseRequirement.REQUIRED_EXACT_THEME, "EXPANSION_ONLY_PROVENANCE"
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


def _active_source_map(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    ingestion = dict(metadata.get("candidate_ingestion") or {})
    source_map = dict(ingestion.get("source_map") or {})
    result: list[dict[str, Any]] = []
    for value in source_map.values():
        item = dict(value or {}) if isinstance(value, Mapping) else {}
        if item and bool(item.get("active", True)):
            result.append(item)
    return result


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


def _fingerprint(payload: Mapping[str, Any]) -> str:
    keys = (
        "code",
        "selected_theme_id",
        "readiness_status",
        "coverage_type",
        "canonical_market_action",
        "subscription_requested",
        "subscription_target_selected",
        "subscription_active",
        "subscription_budget_deferred",
        "subscription_generation",
        "readiness_relevant_source",
        "readiness_relevant_source_generation",
        "expansion_lease_required",
        "exact_theme_lease_active",
        "post_subscription_tick_verified",
        "market_context_fresh",
        "theme_context_fresh",
        "candle_ready",
        "baseline_at",
        "reason_codes",
    )
    compact = {key: payload.get(key) for key in keys}
    return hashlib.sha1(json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
