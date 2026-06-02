from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings


READY_FULL = "READY_FULL"
READY_EARLY_SMALL = "READY_EARLY_SMALL"
WAIT_DATA = "WAIT_DATA"
OBSERVE = "OBSERVE"

SUPPORT_NOT_READY = "SUPPORT_NOT_READY"
WAIT_DATA_SUPPORT_NOT_READY = "WAIT_DATA_SUPPORT_NOT_READY"
BASE_LINE_120_NOT_READY = "BASE_LINE_120_NOT_READY"
BASE_LINE_120_INSUFFICIENT_CANDLES = "BASE_LINE_120_INSUFFICIENT_CANDLES"
VWAP_NOT_READY = "VWAP_NOT_READY"
RECENT_SUPPORT_NOT_READY = "RECENT_SUPPORT_NOT_READY"
RECENT_SWING_LOW_NOT_READY = "RECENT_SWING_LOW_NOT_READY"
PREV_DAY_LEVEL_NOT_READY = "PREV_DAY_LEVEL_NOT_READY"
OPENING_RANGE_NOT_READY = "OPENING_RANGE_NOT_READY"
LATEST_TICK_MISSING = "LATEST_TICK_MISSING"
LATEST_TICK_STALE = "LATEST_TICK_STALE"
SUPPORT_SOURCE_FALLBACK_USED = "SUPPORT_SOURCE_FALLBACK_USED"

BASE_LINE_120 = "base_line_120"
ENVELOPE_MID = "envelope_mid"
VWAP = "vwap"
RECENT_SUPPORT_PRICE = "recent_support_price"
SUPPORT_PRICE = "support_price"
RECENT_SWING_LOW = "recent_swing_low"
PREV_DAY_LEVEL = "prev_day_level"
OPENING_RANGE = "opening_range"

EARLY_READY_SUPPORT_SOURCES = {
    VWAP,
    RECENT_SUPPORT_PRICE,
    SUPPORT_PRICE,
    RECENT_SWING_LOW,
    PREV_DAY_LEVEL,
    OPENING_RANGE,
}


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str = ""
    reason_codes: tuple[str, ...] = ()
    age_sec: Optional[float] = None


def latest_tick_readiness(
    tick,
    now: datetime,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> ReadinessResult:
    active_settings = settings or legacy_strategy_runtime_settings()
    if tick is None:
        return ReadinessResult(False, LATEST_TICK_MISSING, (LATEST_TICK_MISSING,))
    timestamp = getattr(tick, "timestamp", None)
    if timestamp is None or timestamp == datetime.min:
        return ReadinessResult(True, "", (), None)
    age_sec = max(0.0, (_clean_time(now) - _clean_time(timestamp)).total_seconds())
    max_age_sec = active_settings.number("data_readiness.max_latest_tick_age_sec", 30.0)
    if age_sec > max_age_sec:
        return ReadinessResult(False, LATEST_TICK_STALE, (LATEST_TICK_STALE,), age_sec)
    return ReadinessResult(True, "", (), age_sec)


def support_source_readiness(source: str, metadata: dict[str, Any]) -> ReadinessResult:
    normalized = normalize_support_source(source)
    if not normalized:
        return ReadinessResult(False, SUPPORT_NOT_READY, (SUPPORT_NOT_READY,))
    if normalized == BASE_LINE_120:
        ready = _bool_value(metadata.get("base_line_120_ready"))
        count = _int_value(metadata.get("base_line_120_candle_count"))
        reasons = []
        if not ready:
            reasons.append(BASE_LINE_120_NOT_READY)
        if count < 120:
            reasons.append(BASE_LINE_120_INSUFFICIENT_CANDLES)
        return _result(not reasons, reasons or [""])
    if normalized == ENVELOPE_MID:
        ready = _bool_value(metadata.get("envelope_mid_ready"))
        count = _int_value(metadata.get("envelope_mid_candle_count"))
        reasons = []
        if not ready or count < 20:
            reasons.append("ENVELOPE_MID_NOT_READY")
        return _result(not reasons, reasons or [""])
    if normalized == VWAP:
        if _bool_value(metadata.get("vwap_ready")):
            return ReadinessResult(True)
        return ReadinessResult(False, VWAP_NOT_READY, (VWAP_NOT_READY,))
    if normalized in {RECENT_SUPPORT_PRICE, SUPPORT_PRICE, RECENT_SWING_LOW}:
        ready = (
            _bool_value(metadata.get("recent_support_ready"))
            or _bool_value(metadata.get("recent_swing_low_ready"))
            or _bool_value(metadata.get(f"{normalized}_ready"))
        )
        count = max(
            _int_value(metadata.get("recent_support_candle_count")),
            _int_value(metadata.get("recent_swing_low_candle_count")),
            _int_value(metadata.get(f"{normalized}_candle_count")),
        )
        if ready and (count <= 0 or count >= 3):
            return ReadinessResult(True)
        reason = RECENT_SWING_LOW_NOT_READY if normalized == RECENT_SWING_LOW else RECENT_SUPPORT_NOT_READY
        return ReadinessResult(False, reason, (reason,))
    if normalized == PREV_DAY_LEVEL:
        if _bool_value(metadata.get("prev_day_level_ready")):
            return ReadinessResult(True)
        return ReadinessResult(False, PREV_DAY_LEVEL_NOT_READY, (PREV_DAY_LEVEL_NOT_READY,))
    if normalized == OPENING_RANGE:
        if _bool_value(metadata.get("opening_range_ready")):
            return ReadinessResult(True)
        return ReadinessResult(False, OPENING_RANGE_NOT_READY, (OPENING_RANGE_NOT_READY,))
    ready_key = f"{normalized}_ready"
    if ready_key in metadata and not _bool_value(metadata.get(ready_key)):
        return ReadinessResult(False, SUPPORT_NOT_READY, (SUPPORT_NOT_READY,))
    return ReadinessResult(True)


def normalize_support_source(source: str) -> str:
    value = str(source or "").strip()
    aliases = {
        "recent_support": RECENT_SUPPORT_PRICE,
        "recent_low": RECENT_SWING_LOW,
        "swing_low": RECENT_SWING_LOW,
        "prev_high": PREV_DAY_LEVEL,
        "prev_low": PREV_DAY_LEVEL,
        "prev_close": PREV_DAY_LEVEL,
        "previous_day": PREV_DAY_LEVEL,
        "opening_range_low": OPENING_RANGE,
    }
    return aliases.get(value, value)


def support_metadata(
    *,
    source: str,
    price: int | float,
    metadata: dict[str, Any],
    fallback_used: bool = False,
) -> dict[str, Any]:
    readiness = support_source_readiness(source, metadata)
    reason_codes = list(readiness.reason_codes)
    if fallback_used:
        reason_codes.append(SUPPORT_SOURCE_FALLBACK_USED)
    return {
        "selected_support_source": normalize_support_source(source),
        "selected_support_price": int(float(price or 0)),
        "selected_support_ready": readiness.ready,
        "selected_support_ready_reason": readiness.reason,
        "support_ready": readiness.ready,
        "support_ready_reason": readiness.reason,
        "support_readiness_reason_codes": _dedupe(reason_codes),
        "support_source_fallback_used": bool(fallback_used),
    }


def base_line_120_ready(metadata: dict[str, Any]) -> bool:
    return support_source_readiness(BASE_LINE_120, metadata).ready


def _result(ready: bool, reasons: list[str]) -> ReadinessResult:
    clean = [reason for reason in reasons if reason]
    return ReadinessResult(ready, clean[0] if clean else "", tuple(_dedupe(clean)))


def _bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "y", "yes", "ready"}


def _int_value(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _clean_time(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _dedupe(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
