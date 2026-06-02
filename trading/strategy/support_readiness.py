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
SUPPORT_STRUCTURALLY_MISSING = "SUPPORT_STRUCTURALLY_MISSING"
SUPPORT_DATA_MISSING = "SUPPORT_DATA_MISSING"
SUPPORT_STALE_VWAP = "SUPPORT_STALE_VWAP"
SUPPORT_LOW_CONFIDENCE = "SUPPORT_LOW_CONFIDENCE"
SUPPORT_SOURCE_UNAVAILABLE = "SUPPORT_SOURCE_UNAVAILABLE"
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
VALID_RECENT_MINUTE_BARS = "VALID_RECENT_MINUTE_BARS"
LOW_RECENT_BAR_COUNT = "LOW_RECENT_BAR_COUNT"
STALE_MINUTE_BARS = "STALE_MINUTE_BARS"
MISSING_1M_BARS = "MISSING_1M_BARS"
MISSING_3M_AGGREGATION = "MISSING_3M_AGGREGATION"
MISSING_5M_AGGREGATION = "MISSING_5M_AGGREGATION"
INSUFFICIENT_WARMUP_BARS = "INSUFFICIENT_WARMUP_BARS"

BASE_LINE_120 = "base_line_120"
ENVELOPE_MID = "envelope_mid"
VWAP = "vwap"
RECENT_SUPPORT_PRICE = "recent_support_price"
SUPPORT_PRICE = "support_price"
RECENT_SWING_LOW = "recent_swing_low"
PREV_DAY_LEVEL = "prev_day_level"
OPENING_RANGE = "opening_range"
DAY_MID = "day_mid"
EMA20_5M = "ema20_5m"
MANUAL_SUPPORT = "manual_support"

SUPPORT_SOURCES = (
    RECENT_SUPPORT_PRICE,
    SUPPORT_PRICE,
    RECENT_SWING_LOW,
    OPENING_RANGE,
    PREV_DAY_LEVEL,
    VWAP,
    BASE_LINE_120,
    ENVELOPE_MID,
    DAY_MID,
    EMA20_5M,
    MANUAL_SUPPORT,
)

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
        if _bool_value(metadata.get("vwap_stale")) or str(metadata.get("vwap_quality") or "").upper() == "STALE":
            return ReadinessResult(False, SUPPORT_STALE_VWAP, (SUPPORT_STALE_VWAP,))
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


def support_coverage(metadata: dict[str, Any], support_candidates: dict[str, float] | None = None) -> dict[str, Any]:
    candidates = dict(support_candidates or {})
    recent_support_price = _positive(metadata.get("recent_support_price")) or _positive(metadata.get("support_price"))
    vwap = _positive(metadata.get("vwap"))
    minute_quality = minute_bar_quality(metadata)
    source_presence = support_source_presence(metadata, candidates)
    present_sources = [source for source, present in source_presence.items() if present]
    return {
        "recent_support_price_present": bool(recent_support_price or candidates.get(RECENT_SUPPORT_PRICE) or candidates.get(SUPPORT_PRICE)),
        "vwap_present": bool(vwap or candidates.get(VWAP)),
        "vwap_ready": _bool_value(metadata.get("vwap_ready")),
        "vwap_stale": _bool_value(metadata.get("vwap_stale")) or str(metadata.get("vwap_quality") or "").upper() == "STALE",
        "minute_bar_present": int(minute_quality["minute_bar_count"]) > 0,
        "minute_bar_count": int(minute_quality["minute_bar_count"]),
        "minute_bar_quality_status": minute_quality["minute_bar_quality_status"],
        "minute_bar_quality_reasons": list(minute_quality["minute_bar_quality_reasons"]),
        "support_source_presence": source_presence,
        "support_source_present_count": len(present_sources),
        "support_source_present": present_sources,
        "support_candidate_count": len([value for value in candidates.values() if _positive(value)]),
    }


def support_missing_taxonomy(metadata: dict[str, Any], support_candidates: dict[str, float] | None = None) -> str:
    coverage = support_coverage(metadata, support_candidates)
    if coverage["vwap_present"] and coverage.get("vwap_stale"):
        return SUPPORT_STALE_VWAP
    if not (
        coverage["recent_support_price_present"]
        or coverage["vwap_present"]
        or coverage["minute_bar_present"]
    ):
        return SUPPORT_DATA_MISSING
    if coverage["support_candidate_count"] <= 0:
        return SUPPORT_STRUCTURALLY_MISSING
    if coverage.get("support_source_present_count", 0) <= 0:
        return SUPPORT_SOURCE_UNAVAILABLE
    if coverage.get("minute_bar_quality_status") in {LOW_RECENT_BAR_COUNT, INSUFFICIENT_WARMUP_BARS, STALE_MINUTE_BARS}:
        return SUPPORT_LOW_CONFIDENCE
    return SUPPORT_NOT_READY


def support_source_presence(metadata: dict[str, Any], support_candidates: dict[str, float] | None = None) -> dict[str, bool]:
    candidates = dict(support_candidates or {})
    return {
        source: bool(_positive(metadata.get(source)) or _positive(candidates.get(source)))
        for source in SUPPORT_SOURCES
    }


def minute_bar_quality(metadata: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    count_1m = max(
        _int_value(metadata.get("minute_bar_count")),
        _int_value(metadata.get("completed_minute_bar_count")),
        _int_value(metadata.get("recent_1m_bar_count")),
        _int_value(metadata.get("recent_support_candle_count")),
    )
    count_3m = _int_value(metadata.get("recent_3m_bar_count") or metadata.get("three_minute_bar_count"))
    count_5m = _int_value(metadata.get("recent_5m_bar_count") or metadata.get("five_minute_bar_count"))
    age_sec = _minute_bar_age_sec(metadata, now)
    session = str(metadata.get("session_bucket") or metadata.get("market_session_bucket") or "").upper()
    reasons: list[str] = []
    if count_1m <= 0:
        reasons.append(MISSING_1M_BARS)
    if count_3m <= 0:
        reasons.append(MISSING_3M_AGGREGATION)
    if count_5m <= 0:
        reasons.append(MISSING_5M_AGGREGATION)
    if age_sec is not None and age_sec > _stale_bar_threshold(session):
        reasons.append(STALE_MINUTE_BARS)
    min_count = _required_minute_bar_count(session)
    if 0 < count_1m < min_count:
        reasons.append(INSUFFICIENT_WARMUP_BARS if session in {"OPEN", "EARLY", "MARKET_OPEN"} else LOW_RECENT_BAR_COUNT)
    if not reasons:
        status = VALID_RECENT_MINUTE_BARS
    elif MISSING_1M_BARS in reasons:
        status = MISSING_1M_BARS
    elif STALE_MINUTE_BARS in reasons:
        status = STALE_MINUTE_BARS
    elif INSUFFICIENT_WARMUP_BARS in reasons:
        status = INSUFFICIENT_WARMUP_BARS
    else:
        status = LOW_RECENT_BAR_COUNT
    return {
        "minute_bar_quality_status": status,
        "minute_bar_quality_reasons": _dedupe(reasons),
        "minute_bar_count": count_1m,
        "minute_bar_1m_count": count_1m,
        "minute_bar_3m_count": count_3m,
        "minute_bar_5m_count": count_5m,
        "minute_bar_age_sec": age_sec,
        "minute_bar_required_count": min_count,
    }


def _required_minute_bar_count(session: str) -> int:
    if session in {"OPEN", "EARLY", "MARKET_OPEN"}:
        return 3
    if session in {"LATE", "CLOSE", "AFTERNOON_LATE"}:
        return 10
    return 10


def _stale_bar_threshold(session: str) -> int:
    if session in {"LATE", "CLOSE", "AFTERNOON_LATE"}:
        return 90
    return 180


def _minute_bar_age_sec(metadata: dict[str, Any], now: datetime | None) -> float | None:
    raw_age = metadata.get("minute_bar_age_sec") or metadata.get("latest_minute_bar_age_sec")
    if raw_age not in (None, ""):
        try:
            return max(0.0, float(raw_age))
        except (TypeError, ValueError):
            return None
    raw_ts = metadata.get("latest_minute_bar_at") or metadata.get("last_minute_bar_at")
    if now is None or not raw_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(raw_ts))
    except ValueError:
        return None
    return max(0.0, (_clean_time(now) - _clean_time(ts)).total_seconds())


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


def _positive(value) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def _clean_time(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _dedupe(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
