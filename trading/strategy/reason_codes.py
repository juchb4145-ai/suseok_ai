from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from trading.strategy.session import SESSION_BUCKET_UNKNOWN, session_bucket_at


REASON_DETAILS_FEATURE_VERSION = "reason_details_v1"
STRATEGY_FEATURE_VERSION = "observe_p1a_legacy_compare_v1"
COMPARISON_MODE_LEGACY_ONLY = "legacy_only"


class ReasonCode(str, Enum):
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    INDEX_WEAK = "INDEX_WEAK"
    THEME_WEAK = "THEME_WEAK"
    THEME_STRENGTH_C = "THEME_STRENGTH_C"
    THEME_LEADER_COLLAPSE = "THEME_LEADER_COLLAPSE"
    CHASE_RISK = "CHASE_RISK"
    WAIT_PULLBACK_CONFIRMATION = "WAIT_PULLBACK_CONFIRMATION"
    PULLBACK_TOO_DEEP = "PULLBACK_TOO_DEEP"
    LEADERSHIP_WEAK = "LEADERSHIP_WEAK"
    MARKET_INDEX_TEMPORARY_CAP = "MARKET_INDEX_TEMPORARY_CAP"
    DATA_INSUFFICIENT_CAP = "DATA_INSUFFICIENT_CAP"
    THEME_STRENGTH_C_CAP = "THEME_STRENGTH_C_CAP"
    THEME_PULLBACK_FINAL_CAP = "THEME_PULLBACK_FINAL_CAP"
    FINAL_BLOCK_CAP = "FINAL_BLOCK_CAP"
    STOCK_PULLBACK_WAIT_CAP = "STOCK_PULLBACK_WAIT_CAP"
    A_SIGNAL_KOSDAQ_WAIT_CAP = "A_SIGNAL_KOSDAQ_WAIT_CAP"
    LOW_SCORE_CAP = "LOW_SCORE_CAP"
    CHASE_RISK_CAP = "CHASE_RISK_CAP"
    SUPPORT_TOUCHED = "SUPPORT_TOUCHED"
    SUPPORT_RECLAIMED = "SUPPORT_RECLAIMED"
    VOLUME_REACCEL = "VOLUME_REACCEL"
    FAILED_LOW_BREAK_REBOUND = "FAILED_LOW_BREAK_REBOUND"
    TAKE_PROFIT_TARGET_REACHED = "TAKE_PROFIT_TARGET_REACHED"
    SUPPORT_LOSS_CONFIRMED = "SUPPORT_LOSS_CONFIRMED"
    TRAILING_STOP_CONFIRMED = "TRAILING_STOP_CONFIRMED"
    TIME_EXIT_MOMENTUM_FAILED = "TIME_EXIT_MOMENTUM_FAILED"
    DATA_INSUFFICIENT_EXIT_BASIS = "DATA_INSUFFICIENT_EXIT_BASIS"
    MARKET_BREADTH_WEAK = "MARKET_BREADTH_WEAK"
    INDEX_SLOPE_WEAK = "INDEX_SLOPE_WEAK"
    OPEN_GAP_RISK = "OPEN_GAP_RISK"
    THEME_SYNC_WEAK = "THEME_SYNC_WEAK"
    LEADER_FOLLOWER_GAP = "LEADER_FOLLOWER_GAP"
    LEADER_REPLACED = "LEADER_REPLACED"
    LATE_CHASE = "LATE_CHASE"
    LATE_CHASE_TEMP_WAIT = "LATE_CHASE_TEMP_WAIT"
    LATE_CHASE_WARNING = "LATE_CHASE_WARNING"
    LATE_CHASE_RECOVERY_PENDING = "LATE_CHASE_RECOVERY_PENDING"
    RISK_SOFT_BLOCK_TEMP_WAIT = "RISK_SOFT_BLOCK_TEMP_WAIT"
    CANDIDATE_MARKET_WEAK = "CANDIDATE_MARKET_WEAK"
    CANDIDATE_MARKET_RISK_OFF = "CANDIDATE_MARKET_RISK_OFF"
    KOSDAQ_MARKET_WEAK = "KOSDAQ_MARKET_WEAK"
    KOSDAQ_MARKET_RISK_OFF = "KOSDAQ_MARKET_RISK_OFF"
    KOSPI_MARKET_WEAK = "KOSPI_MARKET_WEAK"
    KOSPI_MARKET_RISK_OFF = "KOSPI_MARKET_RISK_OFF"
    GLOBAL_MARKET_RISK_OFF = "GLOBAL_MARKET_RISK_OFF"
    MARKET_CLASSIFICATION_MISSING = "MARKET_CLASSIFICATION_MISSING"
    MARKET_CLASSIFICATION_FALLBACK_STRICT = "MARKET_CLASSIFICATION_FALLBACK_STRICT"
    MARKET_CLASSIFICATION_HEURISTIC_USED = "MARKET_CLASSIFICATION_HEURISTIC_USED"
    MARKET_SIDE_DATA_INSUFFICIENT = "MARKET_SIDE_DATA_INSUFFICIENT"
    SIDE_BREADTH_FALLBACK_GLOBAL = "SIDE_BREADTH_FALLBACK_GLOBAL"
    KOSPI_SIDE_BREADTH_WEAK = "KOSPI_SIDE_BREADTH_WEAK"
    KOSDAQ_SIDE_BREADTH_WEAK = "KOSDAQ_SIDE_BREADTH_WEAK"
    KOSPI_SIDE_BREADTH_RISK_OFF = "KOSPI_SIDE_BREADTH_RISK_OFF"
    KOSDAQ_SIDE_BREADTH_RISK_OFF = "KOSDAQ_SIDE_BREADTH_RISK_OFF"
    KOSPI_SIDE_BREADTH_EXPANSION = "KOSPI_SIDE_BREADTH_EXPANSION"
    KOSDAQ_SIDE_BREADTH_EXPANSION = "KOSDAQ_SIDE_BREADTH_EXPANSION"
    KOSPI_SIDE_BREADTH_SELECTIVE = "KOSPI_SIDE_BREADTH_SELECTIVE"
    KOSDAQ_SIDE_BREADTH_SELECTIVE = "KOSDAQ_SIDE_BREADTH_SELECTIVE"
    KOSPI_SIDE_BREADTH_CHOPPY = "KOSPI_SIDE_BREADTH_CHOPPY"
    KOSDAQ_SIDE_BREADTH_CHOPPY = "KOSDAQ_SIDE_BREADTH_CHOPPY"
    SIDE_BREADTH_NOT_READY = "SIDE_BREADTH_NOT_READY"
    SIDE_BREADTH_SAMPLE_TOO_SMALL = "SIDE_BREADTH_SAMPLE_TOO_SMALL"
    SIDE_BREADTH_VALID_QUOTE_RATIO_LOW = "SIDE_BREADTH_VALID_QUOTE_RATIO_LOW"
    SIDE_BREADTH_FALLBACK_INDEX_RETURN = "SIDE_BREADTH_FALLBACK_INDEX_RETURN"
    SIDE_BREADTH_FALLBACK_WATCH_UNIVERSE = "SIDE_BREADTH_FALLBACK_WATCH_UNIVERSE"
    SIDE_BREADTH_FALLBACK_CANDIDATE_UNIVERSE = "SIDE_BREADTH_FALLBACK_CANDIDATE_UNIVERSE"
    SIDE_BREADTH_WEAK_INDEX_OK = "SIDE_BREADTH_WEAK_INDEX_OK"
    INDEX_WEAK_BREADTH_OK = "INDEX_WEAK_BREADTH_OK"
    SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE = "SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE"
    SIDE_BREADTH_SOURCE_SYMBOL_UNIVERSE = "SIDE_BREADTH_SOURCE_SYMBOL_UNIVERSE"
    SIDE_BREADTH_SOURCE_WATCH_UNIVERSE = "SIDE_BREADTH_SOURCE_WATCH_UNIVERSE"
    SIDE_BREADTH_SOURCE_CANDIDATE_UNIVERSE = "SIDE_BREADTH_SOURCE_CANDIDATE_UNIVERSE"
    WAIT_MARKET_RECOVERY = "WAIT_MARKET_RECOVERY"
    NOT_ELIGIBLE_MARKET = "NOT_ELIGIBLE_MARKET"
    FILL_LIQUIDITY_WEAK = "FILL_LIQUIDITY_WEAK"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SESSION_PROFILE_RESTRICTED = "SESSION_PROFILE_RESTRICTED"
    INPUT_MISSING = "INPUT_MISSING"
    BREADTH_SCOPE_LIMITED = "BREADTH_SCOPE_LIMITED"
    FILL_INPUT_INSUFFICIENT = "FILL_INPUT_INSUFFICIENT"
    SETTINGS_KEY_MISSING = "SETTINGS_KEY_MISSING"
    SOFT_BLOCK_ONLY = "SOFT_BLOCK_ONLY"


P1_REASON_CODES = {
    ReasonCode.MARKET_BREADTH_WEAK.value,
    ReasonCode.INDEX_SLOPE_WEAK.value,
    ReasonCode.OPEN_GAP_RISK.value,
    ReasonCode.THEME_SYNC_WEAK.value,
    ReasonCode.LEADER_FOLLOWER_GAP.value,
    ReasonCode.LEADER_REPLACED.value,
    ReasonCode.LATE_CHASE.value,
    ReasonCode.LATE_CHASE_TEMP_WAIT.value,
    ReasonCode.LATE_CHASE_WARNING.value,
    ReasonCode.LATE_CHASE_RECOVERY_PENDING.value,
    ReasonCode.RISK_SOFT_BLOCK_TEMP_WAIT.value,
    ReasonCode.CANDIDATE_MARKET_WEAK.value,
    ReasonCode.CANDIDATE_MARKET_RISK_OFF.value,
    ReasonCode.KOSDAQ_MARKET_WEAK.value,
    ReasonCode.KOSDAQ_MARKET_RISK_OFF.value,
    ReasonCode.KOSPI_MARKET_WEAK.value,
    ReasonCode.KOSPI_MARKET_RISK_OFF.value,
    ReasonCode.GLOBAL_MARKET_RISK_OFF.value,
    ReasonCode.MARKET_CLASSIFICATION_MISSING.value,
    ReasonCode.MARKET_CLASSIFICATION_FALLBACK_STRICT.value,
    ReasonCode.MARKET_CLASSIFICATION_HEURISTIC_USED.value,
    ReasonCode.MARKET_SIDE_DATA_INSUFFICIENT.value,
    ReasonCode.SIDE_BREADTH_FALLBACK_GLOBAL.value,
    ReasonCode.KOSPI_SIDE_BREADTH_WEAK.value,
    ReasonCode.KOSDAQ_SIDE_BREADTH_WEAK.value,
    ReasonCode.KOSPI_SIDE_BREADTH_RISK_OFF.value,
    ReasonCode.KOSDAQ_SIDE_BREADTH_RISK_OFF.value,
    ReasonCode.KOSPI_SIDE_BREADTH_EXPANSION.value,
    ReasonCode.KOSDAQ_SIDE_BREADTH_EXPANSION.value,
    ReasonCode.KOSPI_SIDE_BREADTH_SELECTIVE.value,
    ReasonCode.KOSDAQ_SIDE_BREADTH_SELECTIVE.value,
    ReasonCode.KOSPI_SIDE_BREADTH_CHOPPY.value,
    ReasonCode.KOSDAQ_SIDE_BREADTH_CHOPPY.value,
    ReasonCode.SIDE_BREADTH_NOT_READY.value,
    ReasonCode.SIDE_BREADTH_SAMPLE_TOO_SMALL.value,
    ReasonCode.SIDE_BREADTH_VALID_QUOTE_RATIO_LOW.value,
    ReasonCode.SIDE_BREADTH_FALLBACK_INDEX_RETURN.value,
    ReasonCode.SIDE_BREADTH_FALLBACK_WATCH_UNIVERSE.value,
    ReasonCode.SIDE_BREADTH_FALLBACK_CANDIDATE_UNIVERSE.value,
    ReasonCode.SIDE_BREADTH_WEAK_INDEX_OK.value,
    ReasonCode.INDEX_WEAK_BREADTH_OK.value,
    ReasonCode.SIDE_BREADTH_SOURCE_REALTIME_UNIVERSE.value,
    ReasonCode.SIDE_BREADTH_SOURCE_SYMBOL_UNIVERSE.value,
    ReasonCode.SIDE_BREADTH_SOURCE_WATCH_UNIVERSE.value,
    ReasonCode.SIDE_BREADTH_SOURCE_CANDIDATE_UNIVERSE.value,
    ReasonCode.WAIT_MARKET_RECOVERY.value,
    ReasonCode.NOT_ELIGIBLE_MARKET.value,
    ReasonCode.FILL_LIQUIDITY_WEAK.value,
    ReasonCode.SPREAD_TOO_WIDE.value,
    ReasonCode.SESSION_PROFILE_RESTRICTED.value,
    ReasonCode.INPUT_MISSING.value,
    ReasonCode.BREADTH_SCOPE_LIMITED.value,
    ReasonCode.FILL_INPUT_INSUFFICIENT.value,
    ReasonCode.SETTINGS_KEY_MISSING.value,
    ReasonCode.SOFT_BLOCK_ONLY.value,
}


def normalize_reason_code(value: Any) -> str:
    if isinstance(value, ReasonCode):
        return value.value
    return str(value or "").strip()


def normalize_reason_codes(values: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = normalize_reason_code(value)
        if text and text not in result:
            result.append(text)
    return result


def standardize_details(
    details: dict | None,
    reason_codes: Iterable[Any] | None = None,
    *,
    passed: Any = None,
    score: Any = None,
    created_at: Any = None,
    session_bucket: str | None = None,
    comparison_mode: str = COMPARISON_MODE_LEGACY_ONLY,
    legacy_result: Any = None,
    new_result: Any = None,
    legacy_score: Any = None,
    new_score: Any = None,
) -> dict:
    normalized = dict(details or {})
    codes = normalize_reason_codes(reason_codes)
    existing_primary = normalize_reason_code(normalized.get("primary_reason_code"))
    primary = existing_primary or (codes[0] if codes else "")
    secondary = normalize_reason_codes(
        list(normalized.get("secondary_reason_codes") or [])
        + [code for code in codes if code != primary]
    )

    normalized.setdefault("feature_version", REASON_DETAILS_FEATURE_VERSION)
    normalized.setdefault("strategy_feature_version", STRATEGY_FEATURE_VERSION)
    normalized.setdefault("session_bucket", session_bucket or session_bucket_at(created_at) or SESSION_BUCKET_UNKNOWN)
    normalized["primary_reason_code"] = primary
    normalized["secondary_reason_codes"] = secondary
    normalized.setdefault("input_missing_fields", _input_missing_fields(normalized))
    normalized.setdefault("comparison_mode", comparison_mode)
    normalized.setdefault("legacy_result", passed if legacy_result is None else legacy_result)
    normalized.setdefault("new_result", normalized.get("legacy_result") if new_result is None else new_result)
    normalized.setdefault("legacy_score", score if legacy_score is None else legacy_score)
    normalized.setdefault("new_score", normalized.get("legacy_score") if new_score is None else new_score)
    return normalized


def reason_code_fields(reason_codes: Iterable[Any] | None) -> dict[str, Any]:
    codes = normalize_reason_codes(reason_codes)
    return {
        "primary_reason_code": codes[0] if codes else "",
        "secondary_reason_codes": codes[1:],
    }


def _input_missing_fields(details: dict) -> list[str]:
    explicit = normalize_reason_codes(details.get("input_missing_fields") or [])
    if explicit:
        return explicit
    missing = []
    for value in details.get("insufficient_reason") or []:
        text = normalize_reason_code(value)
        lowered = text.lower()
        if "missing" in lowered or "insufficient" in lowered:
            missing.append(text)
    return normalize_reason_codes(missing)
