from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading.strategy.candidates import normalize_code


DEFAULT_GENERATION_GAP_MINUTES = 90
DEFAULT_GENERATION_MIN_GAP_MINUTES = 20
DEFAULT_MAX_GENERATION_PER_CODE_PER_DAY = 5

GENERATION_REASON_INITIAL = "initial_generation"
GENERATION_REASON_SAME = "same_generation"
GENERATION_REASON_STALE = "stale_re_detected"
GENERATION_REASON_THEME_CHANGED = "theme_changed"
GENERATION_REASON_SOURCE_CHANGED = "source_changed"
GENERATION_REASON_STRATEGY_CHANGED = "strategy_changed"
GENERATION_REASON_PREVIOUS_LIFECYCLE_CLOSED = "previous_lifecycle_closed"
GENERATION_REASON_MANUAL_RESET = "manual_reset"
GENERATION_REASON_SESSION_RESET = "session_reset"
GENERATION_REASON_MIN_GAP_GUARDRAIL = "same_generation_min_gap_guardrail"
GENERATION_REASON_MAX_PER_DAY_GUARDRAIL = "same_generation_max_generation_guardrail"


@dataclass(frozen=True)
class CandidateGenerationConfig:
    stale_redetect_minutes: int = DEFAULT_GENERATION_GAP_MINUTES
    new_generation_on_theme_change: bool = True
    new_generation_on_source_change: bool = True
    new_generation_on_strategy_change: bool = True
    new_generation_after_position_closed: bool = True
    generation_min_gap_minutes: int = DEFAULT_GENERATION_MIN_GAP_MINUTES
    max_generation_per_code_per_day: int = DEFAULT_MAX_GENERATION_PER_CODE_PER_DAY

    @classmethod
    def from_env(cls) -> "CandidateGenerationConfig":
        return cls(
            stale_redetect_minutes=_env_int("TRADING_CANDIDATE_STALE_REDETECT_MINUTES", DEFAULT_GENERATION_GAP_MINUTES),
            new_generation_on_theme_change=_env_bool("TRADING_CANDIDATE_NEW_GENERATION_ON_THEME_CHANGE", True),
            new_generation_on_source_change=_env_bool("TRADING_CANDIDATE_NEW_GENERATION_ON_SOURCE_CHANGE", True),
            new_generation_on_strategy_change=_env_bool("TRADING_CANDIDATE_NEW_GENERATION_ON_STRATEGY_CHANGE", True),
            new_generation_after_position_closed=_env_bool("TRADING_CANDIDATE_NEW_GENERATION_AFTER_POSITION_CLOSED", True),
            generation_min_gap_minutes=_env_int("TRADING_CANDIDATE_GENERATION_MIN_GAP_MINUTES", DEFAULT_GENERATION_MIN_GAP_MINUTES),
            max_generation_per_code_per_day=_env_int("TRADING_CANDIDATE_MAX_GENERATION_PER_CODE_PER_DAY", DEFAULT_MAX_GENERATION_PER_CODE_PER_DAY),
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "stale_redetect_minutes": self.stale_redetect_minutes,
            "new_generation_on_theme_change": self.new_generation_on_theme_change,
            "new_generation_on_source_change": self.new_generation_on_source_change,
            "new_generation_on_strategy_change": self.new_generation_on_strategy_change,
            "new_generation_after_position_closed": self.new_generation_after_position_closed,
            "generation_min_gap_minutes": self.generation_min_gap_minutes,
            "max_generation_per_code_per_day": self.max_generation_per_code_per_day,
        }


@dataclass(frozen=True)
class CandidateInstanceDecision:
    candidate_instance_id: str
    candidate_generation_seq: int
    generation_reason: str
    previous_candidate_instance_id: str = ""
    previous_seen_at: str = ""
    minutes_since_previous_signal: float | None = None
    blocked_generation_reason: str = ""
    excessive_generation_blocked: bool = False


def decide_candidate_instance(
    *,
    trade_date: str,
    code: str,
    source: str,
    strategy_name: str,
    theme_id: str,
    first_seen_at: str,
    theme_name: str = "",
    existing_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
    generation_gap_minutes: int = DEFAULT_GENERATION_GAP_MINUTES,
    config: CandidateGenerationConfig | None = None,
) -> CandidateInstanceDecision:
    active_config = config or CandidateGenerationConfig(stale_redetect_minutes=generation_gap_minutes)
    metadata = dict(existing_metadata or {})
    previous_instance = str(metadata.get("candidate_instance_id") or "")
    previous_theme = str(metadata.get("candidate_instance_theme_id") or metadata.get("theme_lab_primary_theme") or "")
    previous_theme_name = str(metadata.get("candidate_instance_theme_name") or metadata.get("theme_lab_theme_name") or "")
    previous_source = str(metadata.get("candidate_instance_source") or metadata.get("theme_lab_bridge_source") or "")
    previous_strategy = str(metadata.get("candidate_instance_strategy_name") or "")
    previous_seen_at = str(metadata.get("candidate_instance_last_seen_at") or metadata.get("theme_lab_last_seen") or "")
    previous_seq = _int(metadata.get("candidate_generation_seq"), default=0)
    minutes_since_previous = _minutes_since(previous_seen_at, now)
    requested_reason = _requested_generation_reason(metadata, active_config)

    reason = ""
    if not previous_instance:
        seq = max(1, previous_seq or 1)
        reason = GENERATION_REASON_INITIAL
    elif requested_reason:
        seq = previous_seq + 1
        reason = requested_reason
    elif _theme_changed(previous_theme, previous_theme_name, theme_id, theme_name) and active_config.new_generation_on_theme_change:
        seq = previous_seq + 1
        reason = GENERATION_REASON_THEME_CHANGED
    elif previous_source and previous_source != str(source or "") and active_config.new_generation_on_source_change:
        seq = previous_seq + 1
        reason = GENERATION_REASON_SOURCE_CHANGED
    elif previous_strategy and previous_strategy != str(strategy_name or "") and active_config.new_generation_on_strategy_change:
        seq = previous_seq + 1
        reason = GENERATION_REASON_STRATEGY_CHANGED
    elif _gap_exceeded(previous_seen_at, now, active_config.stale_redetect_minutes):
        seq = previous_seq + 1
        reason = GENERATION_REASON_STALE
    else:
        seq = max(1, previous_seq)
        reason = GENERATION_REASON_SAME

    blocked_reason = ""
    excessive_blocked = False
    if previous_instance and reason not in {GENERATION_REASON_SAME}:
        if _generation_gap_blocked(minutes_since_previous, active_config.generation_min_gap_minutes):
            blocked_reason = reason
            excessive_blocked = True
            seq = max(1, previous_seq)
            reason = GENERATION_REASON_MIN_GAP_GUARDRAIL
        elif active_config.max_generation_per_code_per_day > 0 and previous_seq >= active_config.max_generation_per_code_per_day:
            blocked_reason = reason
            excessive_blocked = True
            seq = max(1, previous_seq)
            reason = GENERATION_REASON_MAX_PER_DAY_GUARDRAIL

    instance_id = build_candidate_instance_id(
        trade_date=trade_date,
        code=code,
        source=source,
        strategy_name=strategy_name,
        theme_id=theme_id,
        first_seen_at=first_seen_at,
        candidate_generation_seq=seq,
    )
    return CandidateInstanceDecision(
        candidate_instance_id=instance_id,
        candidate_generation_seq=seq,
        generation_reason=reason,
        previous_candidate_instance_id=previous_instance,
        previous_seen_at=previous_seen_at,
        minutes_since_previous_signal=minutes_since_previous,
        blocked_generation_reason=blocked_reason,
        excessive_generation_blocked=excessive_blocked,
    )


def build_candidate_instance_id(
    *,
    trade_date: str,
    code: str,
    source: str,
    strategy_name: str,
    theme_id: str,
    first_seen_at: str,
    candidate_generation_seq: int,
) -> str:
    normalized = [
        str(trade_date or ""),
        normalize_code(code),
        str(source or ""),
        str(strategy_name or ""),
        str(theme_id or ""),
        str(first_seen_at or ""),
        str(int(candidate_generation_seq or 1)),
    ]
    digest = hashlib.sha1("|".join(normalized).encode("utf-8")).hexdigest()[:12]
    return f"ci:{normalized[0]}:{normalized[1]}:{int(candidate_generation_seq or 1)}:{digest}"


def identity_metadata(
    decision: CandidateInstanceDecision,
    *,
    source: str,
    strategy_name: str,
    theme_id: str,
    first_seen_at: str,
    last_seen_at: str,
    theme_name: str = "",
    config: CandidateGenerationConfig | None = None,
) -> dict[str, Any]:
    return {
        "candidate_instance_id": decision.candidate_instance_id,
        "candidate_generation_seq": decision.candidate_generation_seq,
        "generation_reason": decision.generation_reason,
        "candidate_generation_reason": decision.generation_reason,
        "previous_candidate_instance_id": decision.previous_candidate_instance_id,
        "previous_seen_at": decision.previous_seen_at,
        "minutes_since_previous_signal": decision.minutes_since_previous_signal,
        "blocked_generation_reason": decision.blocked_generation_reason,
        "excessive_generation_blocked": decision.excessive_generation_blocked,
        "candidate_instance_source": source,
        "candidate_instance_strategy_name": strategy_name,
        "candidate_instance_theme_id": theme_id,
        "candidate_instance_theme_name": theme_name,
        "candidate_instance_first_seen_at": first_seen_at,
        "candidate_instance_last_seen_at": last_seen_at,
        "candidate_generation_config": config.to_metadata() if config is not None else {},
    }


def get_candidate_instance_id(*payloads: dict[str, Any] | None) -> str:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        value = payload.get("candidate_instance_id")
        if value:
            return str(value)
        nested = payload.get("theme_lab_bridge")
        if isinstance(nested, dict) and nested.get("candidate_instance_id"):
            return str(nested.get("candidate_instance_id"))
    return ""


def _gap_exceeded(previous_seen_at: str, now: datetime | None, generation_gap_minutes: int) -> bool:
    minutes = _minutes_since(previous_seen_at, now)
    return minutes is not None and minutes >= max(1, int(generation_gap_minutes or DEFAULT_GENERATION_GAP_MINUTES))


def _minutes_since(previous_seen_at: str, now: datetime | None) -> float | None:
    if now is None or not previous_seen_at:
        return None
    try:
        previous = datetime.fromisoformat(str(previous_seen_at))
    except ValueError:
        return None
    return max(0.0, (now.replace(tzinfo=None) - previous.replace(tzinfo=None)).total_seconds() / 60.0)


def _generation_gap_blocked(minutes_since_previous: float | None, min_gap_minutes: int) -> bool:
    if minutes_since_previous is None:
        return False
    return minutes_since_previous < max(0, int(min_gap_minutes or 0))


def _theme_changed(previous_theme: str, previous_theme_name: str, theme_id: str, theme_name: str) -> bool:
    if previous_theme and previous_theme != str(theme_id or ""):
        return True
    if previous_theme_name and previous_theme_name != str(theme_name or ""):
        return True
    return False


def _requested_generation_reason(metadata: dict[str, Any], config: CandidateGenerationConfig) -> str:
    raw = str(metadata.get("candidate_generation_force_new_reason") or metadata.get("candidate_generation_reset_reason") or "")
    normalized = raw.strip().lower()
    if normalized in {"previous_lifecycle_closed", "position_closed", "closed"} and config.new_generation_after_position_closed:
        return GENERATION_REASON_PREVIOUS_LIFECYCLE_CLOSED
    if normalized in {"manual_reset", "manual"}:
        return GENERATION_REASON_MANUAL_RESET
    if normalized in {"session_reset", "session"}:
        return GENERATION_REASON_SESSION_RESET
    return ""


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}
