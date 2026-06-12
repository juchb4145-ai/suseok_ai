from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable

from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings
from trading.strategy.support_readiness import latest_tick_readiness


BUCKET_OK = "OK"
BUCKET_CORE_BLOCKING = "CORE_BLOCKING"
BUCKET_ENTRY_BLOCKING = "ENTRY_BLOCKING"
BUCKET_WARMUP_OPTIONAL = "WARMUP_OPTIONAL"
BUCKET_BACKFILL_ONLY_OBSERVE = "BACKFILL_ONLY_OBSERVE"

ACTION_PASS = "PASS"
ACTION_BLOCK = "BLOCK"
ACTION_WAIT_DATA = "WAIT_DATA"
ACTION_OBSERVE = "OBSERVE"
ACTION_ALLOW_EARLY_SMALL_CANDIDATE = "ALLOW_EARLY_SMALL_CANDIDATE"

CORE_REASON_CODES = {
    "MISSING_CURRENT_PRICE",
    "LATEST_TICK_MISSING",
    "LATEST_TICK_STALE",
    "STALE_QUOTE",
    "MISSING_CHANGE_RATE",
    "MISSING_TRADE_VALUE",
    "MISSING_EXECUTION_STRENGTH",
    "MARKET_INDEX_MISSING",
}
ENTRY_REASON_CODES = {
    "VWAP_MISSING",
    "VWAP_NOT_READY",
    "SUPPORT_NOT_READY",
    "WAIT_DATA_SUPPORT_NOT_READY",
    "SUPPORT_DATA_MISSING",
    "RECENT_SUPPORT_MISSING",
    "RECENT_SUPPORT_NOT_READY",
    "OPENING_RANGE_MISSING",
    "OPENING_RANGE_NOT_READY",
    "PRICE_LOCATION_UNKNOWN",
    "SUPPORT_STRUCTURALLY_MISSING",
    "SUPPORT_SOURCE_UNAVAILABLE",
    "SUPPORT_STALE_VWAP",
    "SUPPORT_LOW_CONFIDENCE",
}
OPTIONAL_REASON_CODES = {
    "BASE_LINE_120_INSUFFICIENT_CANDLES",
    "BASE_LINE_120_MISSING",
    "BASE_LINE_120_NOT_READY",
    "ENVELOPE_MID_INSUFFICIENT_CANDLES",
    "ENVELOPE_MID_MISSING",
    "ENVELOPE_MID_NOT_READY",
    "EMA20_5M_INSUFFICIENT_CANDLES",
    "VOLATILITY_5M_INSUFFICIENT_CANDLES",
    "RECENT_3M_BAR_INSUFFICIENT",
    "RECENT_5M_BAR_INSUFFICIENT",
    "MISSING_3M_AGGREGATION",
    "MISSING_5M_AGGREGATION",
    "INSUFFICIENT_WARMUP_BARS",
}
BACKFILL_REASON_CODES = {
    "PRICE_SOURCE_TR_BACKFILL",
    "GATE_USABLE_FALSE",
    "REALTIME_TICK_NOT_CONFIRMED",
}
CHASE_BLOCK_CODES = {
    "LATE_CHASE_TEMP_WAIT",
    "LATE_CHASE",
    "CHASE_RISK",
    "CHASE_HIGH",
    "HIGH_CHASE_RISK",
    "VWAP_OVEREXTENDED",
    "BREAKOUT_CONTINUATION",
    "VI_ACTIVE",
    "UPPER_LIMIT_HARD_NEAR",
}
RISK_OFF_EXTREME_CODES = {
    "EXTREME_RISK_OFF",
    "GLOBAL_MARKET_RISK_OFF",
    "WAIT_MARKET_CONFIRMATION_PENDING",
    "MARKET_RISK_OFF_CONFIRMATION_PENDING",
    "CANDIDATE_MARKET_RISK_OFF_UNCONFIRMED",
}


@dataclass(frozen=True)
class DataQualityClassification:
    bucket: str = BUCKET_OK
    action: str = ACTION_PASS
    reason_codes: list[str] = field(default_factory=list)
    missing_core_fields: list[str] = field(default_factory=list)
    missing_entry_fields: list[str] = field(default_factory=list)
    missing_optional_fields: list[str] = field(default_factory=list)
    confidence: str = "HIGH"
    operator_message_ko: str = "핵심 실시간 데이터와 진입 보조 데이터가 준비되었습니다."
    early_small_candidate: bool = False
    early_small_order_enabled: bool = False
    early_small_position_size_multiplier: float = 0.0
    early_small_rejected_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_data_insufficient_reason(
    *,
    reason_codes: Iterable[str] = (),
    tick: Any = None,
    metadata: dict[str, Any] | None = None,
    support: dict[str, Any] | None = None,
    latest_tick_ready: bool | None = None,
    latest_tick_reason: str = "",
    now: datetime | None = None,
    settings: StrategyRuntimeSettings | None = None,
) -> DataQualityClassification:
    active_settings = settings or legacy_strategy_runtime_settings()
    meta = dict(metadata or {})
    codes = _dedupe_upper(reason_codes)
    support_payload = dict(support or {})
    core_missing: list[str] = []
    entry_missing: list[str] = []
    optional_missing: list[str] = []

    if tick is None:
        core_missing.append("LATEST_TICK_MISSING")
    else:
        if _number(getattr(tick, "price", 0)) <= 0:
            core_missing.append("MISSING_CURRENT_PRICE")
        if not _has_numeric_attr_or_meta(tick, "change_rate", meta, ("change_rate", "change_rate_pct", "return_pct")):
            core_missing.append("MISSING_CHANGE_RATE")
        if _number(getattr(tick, "trade_value", 0)) <= 0 and _number(meta.get("trade_value") or meta.get("turnover_krw") or meta.get("turnover")) <= 0:
            core_missing.append("MISSING_TRADE_VALUE")
        if active_settings.value("data_readiness.require_execution_strength", False) and _number(getattr(tick, "execution_strength", 0)) <= 0:
            core_missing.append("MISSING_EXECUTION_STRENGTH")

    if latest_tick_ready is False:
        core_missing.append(str(latest_tick_reason or "LATEST_TICK_STALE").upper())
    elif latest_tick_ready is None and tick is not None:
        latest = latest_tick_readiness(tick, now or datetime.now(), active_settings)
        if not latest.ready:
            core_missing.extend(str(code).upper() for code in latest.reason_codes or (latest.reason,))

    core_missing.extend(code for code in codes if code in CORE_REASON_CODES)

    if _is_backfill_only(meta, codes):
        return DataQualityClassification(
            bucket=BUCKET_BACKFILL_ONLY_OBSERVE,
            action=ACTION_OBSERVE,
            reason_codes=_dedupe(["DATA_INSUFFICIENT", "BACKFILL_ONLY_OBSERVE", *codes, *BACKFILL_REASON_CODES.intersection(codes)]),
            confidence="HIGH",
            operator_message_ko="TR backfill 값만 있고 실시간 확인이 없어 관찰 전용으로 유지합니다.",
        )

    if core_missing:
        return DataQualityClassification(
            bucket=BUCKET_CORE_BLOCKING,
            action=ACTION_BLOCK,
            reason_codes=_dedupe(["DATA_INSUFFICIENT", "CORE_BLOCKING", *core_missing, *codes]),
            missing_core_fields=_dedupe(core_missing),
            confidence="HIGH",
            operator_message_ko="현재가/최신 틱/거래대금 등 핵심 실시간 데이터가 부족해 주문을 금지합니다.",
        )

    support_ready = support_payload.get("ready")
    support_reason = str(support_payload.get("reason") or "").upper()
    support_reason_codes = _dedupe_upper(support_payload.get("reason_codes") or ())
    if support_ready is False:
        entry_missing.extend(support_reason_codes or [support_reason or "SUPPORT_NOT_READY"])
    entry_missing.extend(code for code in codes if code in ENTRY_REASON_CODES)
    if any(code in codes for code in {"PRICE_LOCATION_UNKNOWN", "UNKNOWN"}):
        entry_missing.append("PRICE_LOCATION_UNKNOWN")

    optional_missing.extend(code for code in codes if code in OPTIONAL_REASON_CODES)
    optional_missing.extend(_optional_missing_from_metadata(meta))

    if entry_missing:
        return DataQualityClassification(
            bucket=BUCKET_ENTRY_BLOCKING,
            action=ACTION_WAIT_DATA,
            reason_codes=_dedupe(["DATA_INSUFFICIENT", "ENTRY_BLOCKING", *entry_missing, *codes]),
            missing_entry_fields=_dedupe(entry_missing),
            missing_optional_fields=_dedupe(optional_missing),
            confidence="HIGH",
            operator_message_ko="VWAP/최근 지지선/진입 위치 판단 데이터가 부족해 WAIT_DATA로 유지합니다.",
        )

    if optional_missing:
        return DataQualityClassification(
            bucket=BUCKET_WARMUP_OPTIONAL,
            action=ACTION_ALLOW_EARLY_SMALL_CANDIDATE,
            reason_codes=_dedupe(["DATA_INSUFFICIENT", "WARMUP_OPTIONAL_ONLY", *optional_missing, *codes]),
            missing_optional_fields=_dedupe(optional_missing),
            confidence="MEDIUM",
            operator_message_ko="기준선120/엔벨로프/5분 보조지표만 부족해 소액 후보로 관찰합니다.",
        )

    if "DATA_INSUFFICIENT" in codes or "INDICATOR_DATA_INSUFFICIENT" in codes:
        return DataQualityClassification(
            bucket=BUCKET_ENTRY_BLOCKING,
            action=ACTION_WAIT_DATA,
            reason_codes=_dedupe(["DATA_INSUFFICIENT", "ENTRY_BLOCKING", *codes]),
            missing_entry_fields=["UNCLASSIFIED_DATA_INSUFFICIENT"],
            confidence="LOW",
            operator_message_ko="데이터 부족 사유가 세분화되지 않아 보수적으로 WAIT_DATA로 유지합니다.",
        )

    return DataQualityClassification()


def classify_entry_data_quality(**kwargs: Any) -> DataQualityClassification:
    return classify_data_insufficient_reason(**kwargs)


def data_quality_bucket_for_gate(classification: DataQualityClassification | dict[str, Any] | None) -> str:
    if classification is None:
        return BUCKET_OK
    if isinstance(classification, DataQualityClassification):
        return classification.bucket
    return str(classification.get("bucket") or BUCKET_OK)


def data_quality_action_for_candidate(
    classification: DataQualityClassification,
    *,
    settings: StrategyRuntimeSettings | None = None,
    status: str = "",
    stock_role: str = "",
    theme_status: str = "",
    price_location_status: str = "",
    risk_level: str = "",
    latest_tick_ready: bool = False,
    current_price: int | float = 0,
    trade_value: int | float = 0,
    vwap_ready: bool = False,
    recent_support_ready: bool = False,
    reason_codes: Iterable[str] = (),
    candidate_market_status: str = "",
) -> DataQualityClassification:
    if classification.bucket != BUCKET_WARMUP_OPTIONAL:
        return classification
    policy = data_quality_early_small_settings(settings)
    rejected = _early_small_rejected_reason(
        policy=policy,
        status=status,
        stock_role=stock_role,
        theme_status=theme_status,
        price_location_status=price_location_status,
        risk_level=risk_level,
        latest_tick_ready=latest_tick_ready,
        current_price=current_price,
        trade_value=trade_value,
        vwap_ready=vwap_ready,
        recent_support_ready=recent_support_ready,
        reason_codes=reason_codes,
        candidate_market_status=candidate_market_status,
    )
    if rejected:
        return DataQualityClassification(
            **{
                **classification.to_dict(),
                "action": ACTION_WAIT_DATA,
                "early_small_candidate": False,
                "early_small_order_enabled": False,
                "early_small_position_size_multiplier": 0.0,
                "early_small_rejected_reason": rejected,
                "reason_codes": _dedupe([*classification.reason_codes, "EARLY_SMALL_REJECTED", rejected]),
                "operator_message_ko": "보조지표만 부족하지만 early-small 조건을 만족하지 못해 WAIT_DATA로 유지합니다.",
            }
        )
    order_enabled = bool(policy["order_enabled"])
    return DataQualityClassification(
        **{
            **classification.to_dict(),
            "action": ACTION_ALLOW_EARLY_SMALL_CANDIDATE,
            "early_small_candidate": True,
            "early_small_order_enabled": order_enabled,
            "early_small_position_size_multiplier": float(policy["max_position_size_multiplier"]),
            "early_small_rejected_reason": "" if order_enabled else "EARLY_SMALL_OBSERVE_ONLY",
            "reason_codes": _dedupe(
                [
                    *classification.reason_codes,
                    "WAIT_DATA_EARLY_SMALL_CANDIDATE" if not order_enabled else "READY_EARLY_SMALL",
                    "EARLY_SMALL_OBSERVE_ONLY" if not order_enabled else "DATA_QUALITY_SIZE_REDUCED",
                ]
            ),
            "operator_message_ko": (
                "보조지표만 부족하고 리더/가격 위치 조건이 맞아 READY_EARLY_SMALL 소액 주문 후보로 분류합니다."
                if order_enabled
                else "보조지표만 부족하고 리더/가격 위치 조건이 맞지만 기본 설정상 주문 없이 WAIT_DATA_EARLY_SMALL_CANDIDATE로 관찰합니다."
            ),
        }
    )


def data_quality_early_small_settings(settings: StrategyRuntimeSettings | None = None) -> dict[str, Any]:
    active = settings or legacy_strategy_runtime_settings()
    raw = dict(active.value("data_quality_early_small", {}) or {})
    return {
        "enabled": _bool(raw.get("enabled"), True),
        "order_enabled": _bool(raw.get("order_enabled"), False),
        "max_position_size_multiplier": max(0.0, min(1.0, _number(raw.get("max_position_size_multiplier"), 0.15))),
        "allowed_roles": _upper_tuple(raw.get("allowed_roles") or ("LEADER", "CO_LEADER")),
        "allowed_price_locations": _upper_tuple(raw.get("allowed_price_locations") or ("GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM")),
        "allowed_theme_statuses": _upper_tuple(raw.get("allowed_theme_statuses") or ("LEADING_THEME", "SPREADING_THEME")),
        "allowed_statuses": _upper_tuple(raw.get("allowed_statuses") or ("READY", "READY_SMALL", "WAIT")),
        "allowed_risk_levels": _upper_tuple(raw.get("allowed_risk_levels") or ("PASS", "RISK_ADJUST")),
        "require_vwap_or_recent_support": _bool(raw.get("require_vwap_or_recent_support"), True),
        "max_live_sim_orders_per_cycle": max(0, int(_number(raw.get("max_live_sim_orders_per_cycle"), 1))),
        "block_on_risk_off": _bool(raw.get("block_on_risk_off"), True),
        "block_on_chase": _bool(raw.get("block_on_chase"), True),
    }


def _early_small_rejected_reason(**kwargs: Any) -> str:
    policy = dict(kwargs["policy"])
    if not policy["enabled"]:
        return "EARLY_SMALL_DISABLED"
    if _upper(kwargs["status"]) not in set(policy["allowed_statuses"]):
        return "STATUS_NOT_ALLOWED"
    if _upper(kwargs["stock_role"]) not in set(policy["allowed_roles"]):
        return "ROLE_NOT_ALLOWED"
    if _upper(kwargs["theme_status"]) not in set(policy["allowed_theme_statuses"]):
        return "THEME_STATUS_NOT_ALLOWED"
    if _upper(kwargs["price_location_status"]) not in set(policy["allowed_price_locations"]):
        return "PRICE_LOCATION_NOT_ALLOWED"
    if _upper(kwargs["risk_level"]) not in set(policy["allowed_risk_levels"]):
        return "RISK_LEVEL_NOT_ALLOWED"
    if not bool(kwargs["latest_tick_ready"]):
        return "LATEST_TICK_NOT_READY"
    if _number(kwargs["current_price"]) <= 0:
        return "MISSING_CURRENT_PRICE"
    if _number(kwargs["trade_value"]) <= 0:
        return "MISSING_TRADE_VALUE"
    codes = set(_dedupe_upper(kwargs.get("reason_codes") or ()))
    if policy["block_on_chase"] and (codes & CHASE_BLOCK_CODES or _upper(kwargs["price_location_status"]) in CHASE_BLOCK_CODES):
        return "CHASE_RISK"
    if policy["block_on_risk_off"] and (codes & RISK_OFF_EXTREME_CODES or _upper(kwargs["candidate_market_status"]) == "RISK_OFF"):
        return "RISK_OFF_EXTREME"
    if policy["require_vwap_or_recent_support"] and not (bool(kwargs["vwap_ready"]) or bool(kwargs["recent_support_ready"])):
        return "VWAP_OR_RECENT_SUPPORT_NOT_READY"
    return ""


def _optional_missing_from_metadata(metadata: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if "base_line_120_ready" in metadata and not _bool(metadata.get("base_line_120_ready"), False):
        reasons.append("BASE_LINE_120_MISSING" if _number(metadata.get("base_line_120")) <= 0 else "BASE_LINE_120_NOT_READY")
    base_count = int(_number(metadata.get("base_line_120_candle_count"), 0))
    if 0 < base_count < 120:
        reasons.append("BASE_LINE_120_INSUFFICIENT_CANDLES")
    if "envelope_mid_ready" in metadata and not _bool(metadata.get("envelope_mid_ready"), False):
        reasons.append("ENVELOPE_MID_MISSING" if _number(metadata.get("envelope_mid")) <= 0 else "ENVELOPE_MID_NOT_READY")
    envelope_count = int(_number(metadata.get("envelope_mid_candle_count"), 0))
    if 0 < envelope_count < 20:
        reasons.append("ENVELOPE_MID_INSUFFICIENT_CANDLES")
    ema_count = int(_number(metadata.get("ema20_5m_candle_count"), 0))
    if 0 < ema_count < 20:
        reasons.append("EMA20_5M_INSUFFICIENT_CANDLES")
    count_3m = int(_number(metadata.get("recent_3m_bar_count") or metadata.get("three_minute_bar_count"), 0))
    if 0 < count_3m < 3:
        reasons.append("RECENT_3M_BAR_INSUFFICIENT")
    count_5m = int(_number(metadata.get("recent_5m_bar_count") or metadata.get("five_minute_bar_count"), 0))
    if 0 < count_5m < 3:
        reasons.append("RECENT_5M_BAR_INSUFFICIENT")
    return _dedupe(reasons)


def _is_backfill_only(metadata: dict[str, Any], codes: list[str]) -> bool:
    source = str(metadata.get("price_source") or metadata.get("last_price_source") or "").upper()
    backfill = source == "TR_BACKFILL" or _bool(metadata.get("tr_backfill_applied"), False)
    gate_usable_false = metadata.get("gate_usable") is False or "GATE_USABLE_FALSE" in codes
    realtime_unconfirmed = "REALTIME_TICK_NOT_CONFIRMED" in codes or _bool(metadata.get("realtime_tick_confirmed"), True) is False
    return backfill and (gate_usable_false or realtime_unconfirmed)


def _has_numeric_attr_or_meta(tick: Any, attr: str, metadata: dict[str, Any], keys: Iterable[str]) -> bool:
    if hasattr(tick, attr) and getattr(tick, attr) not in (None, ""):
        return _is_number(getattr(tick, attr))
    for key in keys:
        if key in metadata and metadata.get(key) not in (None, ""):
            return _is_number(metadata.get(key))
    return False


def _upper_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = []
    return tuple(_dedupe(_upper(item) for item in raw if _upper(item)))


def _dedupe_upper(values: Iterable[Any]) -> list[str]:
    return _dedupe(_upper(value) for value in values)


def _upper(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "ready"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True
