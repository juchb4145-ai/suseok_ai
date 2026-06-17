from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from trading.strategy.reason_codes import normalize_reason_codes


class TradeSetupType(str, Enum):
    CORE_PULLBACK = "CORE_PULLBACK"
    LEADER_PROBE = "LEADER_PROBE"
    RELATIVE_STRENGTH = "RELATIVE_STRENGTH"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    ROTATION_FOLLOWER = "ROTATION_FOLLOWER"
    AVOID = "AVOID"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TradeSetupDecision:
    setup_type: TradeSetupType
    confidence_score: float
    recommended_action: str
    recommended_position_size_multiplier: float
    reason_codes: list[str] = field(default_factory=list)
    operator_message_ko: str = ""
    input_snapshot: dict[str, Any] = field(default_factory=dict)

    def detail_fields(self) -> dict[str, Any]:
        return {
            "trade_setup_type": self.setup_type.value,
            "trade_setup_confidence_score": round(_clamp(self.confidence_score, 0.0, 1.0), 4),
            "trade_setup_action": self.recommended_action,
            "trade_setup_position_size_multiplier": round(max(0.0, float(self.recommended_position_size_multiplier or 0.0)), 4),
            "trade_setup_reason_codes": normalize_reason_codes(self.reason_codes),
            "trade_setup_operator_message_ko": self.operator_message_ko,
        }


class TradeSetupClassifier:
    """Classify candidates for observation/reporting without altering order flow."""

    CORE_LOCATIONS = {"GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM"}
    PROBE_LOCATIONS = {"VWAP_RECLAIM", "PULLBACK_RECLAIM", "GOOD_PULLBACK"}
    MOMENTUM_LOCATIONS = {"BREAKOUT_CONTINUATION", "CHASE_HIGH"}
    LEADER_ROLES = {"LEADER", "CO_LEADER", "LEADER_TYPE_LEADER", "LEADER_TYPE_CO_LEADER", "leader", "co_leader"}
    STRONG_THEME_STATUSES = {"LEADING_THEME", "SPREADING_THEME", "ACTIVE", "WATCH"}
    SPREADING_THEME_STATUSES = {"SPREADING_THEME"}
    WEAK_MARKET_STATUSES = {"WEAK", "RISK_OFF"}
    PASS_RISK_LEVELS = {"", "PASS", "RISK_ADJUST", "LOW", "NONE"}
    READY_STATUSES = {"READY", "READY_SMALL", "READY_RISK_OFF_SMALL", "READY_SHADOW_SMALL_ENTRY", "READY_EARLY_SMALL"}
    AVOID_CODES = {
        "LATE_LAGGARD",
        "LEADER_ONLY_THEME_LAGGARD_BLOCK",
        "VI_ACTIVE",
        "UPPER_LIMIT_HARD_NEAR",
        "STALE_QUOTE",
        "THEME_WEAK",
        "WEAK_THEME",
        "THEME_STALE",
        "HARD_BLOCK",
        "ENTRY_RISK_FINAL_BLOCK",
        "ENTRY_HARD_GUARD",
        "MARKET_HARD_GUARD",
        "CORE_BLOCKING",
        "MISSING_CURRENT_PRICE",
    }
    CORE_DATA_BLOCK_CODES = {
        "DATA_INSUFFICIENT",
        "INDICATOR_DATA_INSUFFICIENT",
        "CORE_BLOCKING",
        "MISSING_CURRENT_PRICE",
        "MISSING_PREV_CLOSE",
    }
    MOMENTUM_CONFIRM_CODES = {
        "MOMENTUM_CONTINUATION",
        "BREAKOUT_CONTINUATION",
        "TURNOVER_MAINTAINED",
        "VOLUME_REACCEL",
        "MOMENTUM_MAINTAINED",
        "STRONG_MOMENTUM",
    }
    RELATIVE_STRENGTH_CODES = {
        "RELATIVE_STRENGTH",
        "RISK_OFF_RELATIVE_STRENGTH",
        "RISK_OFF_SMALL_ENTRY",
        "READY_RISK_OFF_SMALL",
        "OBSERVE_RISK_OFF_SMALL_ENTRY",
    }
    ROTATION_CODES = {
        "ROTATION_FOLLOWER",
        "SPREADING_THEME_ROTATION",
        "FOLLOWER_MOMENTUM",
        "TURNOVER_MAINTAINED",
        "VOLUME_REACCEL",
        "MOMENTUM_MAINTAINED",
    }

    def classify(self, details: Mapping[str, Any] | None) -> TradeSetupDecision:
        snapshot = _snapshot(details or {})
        reasons = set(snapshot["reason_codes"])
        role = snapshot["stock_role"]
        theme_status = snapshot["theme_status"]
        price_location = snapshot["price_location_status"]
        risk_level = snapshot["risk_level"]
        final_status = snapshot["final_gate_status"]
        hybrid_status = snapshot["hybrid_status"]

        avoid_reason = self._avoid_reason(snapshot, reasons)
        if avoid_reason:
            return self._decision(
                TradeSetupType.AVOID,
                0.95,
                "BLOCK",
                0.0,
                [avoid_reason],
                "하드 리스크 또는 데이터 불량 신호가 있어 진입 후보에서 제외하고 원인만 확인합니다.",
                snapshot,
            )

        leader_like = role in {"LEADER", "CO_LEADER", "leader", "co_leader"}
        strong_theme = theme_status in self.STRONG_THEME_STATUSES or float(snapshot["dynamic_theme_score"] or 0.0) >= 75.0
        risk_ok = risk_level in self.PASS_RISK_LEVELS
        ready_like = self._is_ready_like(snapshot)
        normal_ready = self._is_normal_ready(snapshot, reasons)

        if normal_ready and leader_like and strong_theme and price_location in self.CORE_LOCATIONS and risk_ok:
            return self._decision(
                TradeSetupType.CORE_PULLBACK,
                0.9,
                "NORMAL_READY",
                _positive_float(snapshot["position_size_multiplier"], 1.0),
                ["CORE_PULLBACK", "STRONG_THEME_LEADER_PULLBACK"],
                "기존 READY 기준을 통과한 강한 테마 주도주 눌림목 후보입니다.",
                snapshot,
            )

        if self._is_relative_strength(snapshot, reasons, leader_like, strong_theme):
            return self._decision(
                TradeSetupType.RELATIVE_STRENGTH,
                0.82,
                "SMALL_OBSERVE",
                _small_multiplier(snapshot, 0.1),
                ["RELATIVE_STRENGTH", "OBSERVE_ONLY"],
                "시장 약세 구간에서도 테마와 종목 상대강도가 살아 있어 소액 후보로 관찰합니다.",
                snapshot,
            )

        if self._is_momentum_continuation(snapshot, reasons, leader_like):
            return self._decision(
                TradeSetupType.MOMENTUM_CONTINUATION,
                0.78,
                "OBSERVE",
                _small_multiplier(snapshot, 0.1),
                ["MOMENTUM_CONTINUATION", "OBSERVE_ONLY"],
                "돌파 지속 구간이지만 주문에는 반영하지 않고 모멘텀 지속 여부만 관찰합니다.",
                snapshot,
            )

        if self._is_rotation_follower(snapshot, reasons):
            return self._decision(
                TradeSetupType.ROTATION_FOLLOWER,
                0.74,
                "OBSERVE",
                _small_multiplier(snapshot, 0.1),
                ["ROTATION_FOLLOWER", "OBSERVE_ONLY"],
                "확산 테마의 후발주가 거래대금과 모멘텀을 받는지 관찰합니다.",
                snapshot,
            )

        if self._is_leader_probe(snapshot, reasons, leader_like, strong_theme, ready_like):
            return self._decision(
                TradeSetupType.LEADER_PROBE,
                0.8,
                "SMALL_OBSERVE",
                _small_multiplier(snapshot, 0.1),
                ["LEADER_PROBE", "OBSERVE_ONLY"],
                "완전 READY는 아니지만 주도주 재진입 후보로 관찰합니다. 현재 주문에는 반영하지 않습니다.",
                snapshot,
            )

        action = "WAIT" if final_status in {"WAIT", "READY_SMALL"} or hybrid_status == "WAIT" else "OBSERVE"
        return self._decision(
            TradeSetupType.UNKNOWN,
            0.2,
            action,
            0.0,
            ["TRADE_SETUP_UNKNOWN"],
            "전략 유형이 아직 명확하지 않아 기존 상태를 유지하고 관찰합니다.",
            snapshot,
        )

    def _avoid_reason(self, snapshot: dict[str, Any], reasons: set[str]) -> str:
        if snapshot["stock_role"] == "LATE_LAGGARD" or int(snapshot["rank_in_theme"] or 0) >= 6:
            return "LATE_LAGGARD"
        if snapshot["theme_status"] in {"WEAK_THEME", "STALE"}:
            return "THEME_WEAK" if snapshot["theme_status"] == "WEAK_THEME" else "THEME_STALE"
        if snapshot["risk_level"] == "HARD_BLOCK" or snapshot["entry_risk_level"] in {"FINAL", "final"}:
            return "HARD_BLOCK"
        if snapshot["data_quality_bucket"] == "CORE_BLOCKING" or snapshot["missing_core_fields"]:
            return "CORE_BLOCKING"
        if snapshot["latest_tick_ready"] is False:
            return "STALE_QUOTE"
        if self.AVOID_CODES & reasons:
            return sorted(self.AVOID_CODES & reasons)[0]
        if (self.CORE_DATA_BLOCK_CODES & reasons) and (snapshot["data_quality_bucket"] == "CORE_BLOCKING" or snapshot["missing_core_fields"]):
            return "CORE_BLOCKING"
        if snapshot["price_location_status"] in {"VWAP_OVEREXTENDED", "FAILED_BREAKOUT"}:
            return snapshot["price_location_status"]
        if snapshot["price_location_status"] == "CHASE_HIGH" and ({"CHASE_RISK", "HIGH_CHASE_RISK", "LATE_CHASE"} & reasons):
            return "CHASE_RISK"
        return ""

    def _is_relative_strength(
        self,
        snapshot: dict[str, Any],
        reasons: set[str],
        leader_like: bool,
        strong_theme: bool,
    ) -> bool:
        market_statuses = {
            snapshot["candidate_market_status"],
            snapshot["candidate_market_confirmed_status"],
            snapshot["global_market_status"],
        }
        market_weak = bool(market_statuses & self.WEAK_MARKET_STATUSES)
        has_signal = bool(reasons & self.RELATIVE_STRENGTH_CODES) or bool(snapshot["risk_off_entry_allowed"])
        if not has_signal and _number(snapshot["risk_off_relative_strength_pct"]) is not None:
            has_signal = float(snapshot["risk_off_relative_strength_pct"] or 0.0) >= 4.0
        return leader_like and strong_theme and market_weak and has_signal and self._risk_is_observable(snapshot)

    def _is_momentum_continuation(self, snapshot: dict[str, Any], reasons: set[str], leader_like: bool) -> bool:
        if not leader_like or snapshot["price_location_status"] not in self.MOMENTUM_LOCATIONS:
            return False
        if not self._risk_is_observable(snapshot):
            return False
        if snapshot["vi_status"] == "ACTIVE" or "UPPER_LIMIT_HARD_NEAR" in reasons:
            return False
        if snapshot["upper_wick_risk"] is True or bool({"UPPER_WICK_RISK", "FAILED_BREAKOUT"} & reasons):
            return False
        if bool(reasons & self.MOMENTUM_CONFIRM_CODES):
            return True
        if snapshot["volume_reaccel"]:
            return True
        momentum_1m = _number(snapshot["momentum_1m"])
        momentum_3m = _number(snapshot["momentum_3m"])
        turnover = _number(snapshot["turnover_krw"])
        return (momentum_1m is None or momentum_1m >= 0.0) and (momentum_3m is None or momentum_3m >= 0.0) and (
            turnover is None or turnover > 0.0
        )

    def _is_rotation_follower(self, snapshot: dict[str, Any], reasons: set[str]) -> bool:
        if snapshot["stock_role"] != "FOLLOWER" or snapshot["theme_status"] not in self.SPREADING_THEME_STATUSES:
            return False
        if "LEADER_ONLY_THEME" in reasons or snapshot["theme_status"] == "LEADER_ONLY_THEME":
            return False
        if not self._risk_is_observable(snapshot):
            return False
        if bool(reasons & self.ROTATION_CODES) or snapshot["volume_reaccel"]:
            return True
        momentum_1m = _number(snapshot["momentum_1m"])
        momentum_3m = _number(snapshot["momentum_3m"])
        turnover = _number(snapshot["turnover_krw"])
        return (momentum_1m is not None and momentum_1m > 0.0) and (momentum_3m is None or momentum_3m >= 0.0) and (
            turnover is not None and turnover > 0.0
        )

    def _is_leader_probe(
        self,
        snapshot: dict[str, Any],
        reasons: set[str],
        leader_like: bool,
        strong_theme: bool,
        ready_like: bool,
    ) -> bool:
        if ready_like or not leader_like or not strong_theme or not self._risk_is_observable(snapshot):
            return False
        provisional = snapshot["price_location_readiness"] in {"PROVISIONAL", "WARMUP"} or snapshot["price_location_provisional"]
        provisional = provisional or bool({"PRICE_LOCATION_PROVISIONAL", "WAIT_PRICE_LOCATION_PROVISIONAL"} & reasons)
        price_ok = snapshot["price_location_status"] in self.PROBE_LOCATIONS or provisional
        no_core_data_block = snapshot["data_quality_bucket"] != "CORE_BLOCKING" and not snapshot["missing_core_fields"]
        return price_ok and no_core_data_block

    def _risk_is_observable(self, snapshot: dict[str, Any]) -> bool:
        return snapshot["risk_level"] in {"", "PASS", "RISK_ADJUST", "SOFT_BLOCK", "LOW", "NONE"} and snapshot["entry_risk_level"] not in {
            "FINAL",
            "final",
        }

    def _is_ready_like(self, snapshot: dict[str, Any]) -> bool:
        final_status = snapshot["final_gate_status"]
        return final_status in self.READY_STATUSES or final_status.startswith("READY") or snapshot["hybrid_status"] == "READY"

    def _is_normal_ready(self, snapshot: dict[str, Any], reasons: set[str]) -> bool:
        final_status = snapshot["final_gate_status"]
        if bool(reasons & {"RISK_OFF_SMALL_ENTRY", "READY_RISK_OFF_SMALL", "OBSERVE_RISK_OFF_SMALL_ENTRY"}):
            return False
        if final_status in {"READY", "READY_PULLBACK"}:
            return True
        if snapshot["ready_type"] == "READY_FULL":
            return True
        return snapshot["lab_gate_status"] == "READY" and final_status.startswith("READY") and "SMALL" not in final_status

    def _decision(
        self,
        setup_type: TradeSetupType,
        confidence: float,
        action: str,
        multiplier: float,
        reason_codes: list[str],
        message: str,
        snapshot: dict[str, Any],
    ) -> TradeSetupDecision:
        return TradeSetupDecision(
            setup_type=setup_type,
            confidence_score=confidence,
            recommended_action=action,
            recommended_position_size_multiplier=multiplier,
            reason_codes=normalize_reason_codes(reason_codes),
            operator_message_ko=message,
            input_snapshot=dict(snapshot),
        )


def classify_trade_setup(details: Mapping[str, Any] | None) -> TradeSetupDecision:
    return TradeSetupClassifier().classify(details)


def trade_setup_detail_fields(details: Mapping[str, Any] | None) -> dict[str, Any]:
    return classify_trade_setup(details).detail_fields()


def attach_trade_setup_details(details: dict[str, Any]) -> dict[str, Any]:
    details.update(trade_setup_detail_fields(details))
    return details


def _snapshot(details: Mapping[str, Any]) -> dict[str, Any]:
    reasons = _reason_codes(details)
    role = _upper_first(details, "stock_role", "leader_type", "leadership_role")
    if role in {"LEADER_TYPE_LEADER", "leader"}:
        role = "LEADER"
    elif role in {"LEADER_TYPE_CO_LEADER", "co_leader"}:
        role = "CO_LEADER"
    return {
        "final_gate_status": _upper_first(details, "final_gate_status", "gate_status", "sub_status", "display_status"),
        "lab_gate_status": _upper_first(details, "lab_gate_status"),
        "ready_type": _upper_first(details, "ready_type"),
        "hybrid_status": _upper_first(details, "hybrid_status"),
        "hybrid_score": _number(_first(details, "hybrid_score")) or 0.0,
        "theme_status": _upper_first(details, "theme_status", "dynamic_theme_status"),
        "dynamic_theme_score": _number(_first(details, "dynamic_theme_score", "theme_score")) or 0.0,
        "theme_breadth": _number(_first(details, "theme_breadth", "theme_breadth_pct", "strong_ratio")) or 0.0,
        "stock_role": role,
        "leader_type": _upper_first(details, "leader_type", "leadership_role"),
        "rank_in_theme": int(_number(_first(details, "rank_in_theme", "stock_rank_in_theme")) or 0),
        "price_location_status": _upper_first(details, "price_location_status", "price_location"),
        "price_location_readiness": _upper_first(details, "price_location_readiness"),
        "price_location_score": _number(_first(details, "price_location_score")) or 0.0,
        "price_location_provisional": _bool(_first(details, "price_location_provisional")),
        "risk_level": _upper_first(details, "risk_level", "entry_risk_level"),
        "entry_risk_level": _upper_first(details, "entry_risk_level"),
        "late_chase_level": _upper_first(details, "late_chase_level"),
        "support_ready": _optional_bool(_first(details, "support_ready", "selected_support_ready")),
        "selected_support_ready": _optional_bool(_first(details, "selected_support_ready", "support_ready")),
        "latest_tick_ready": _optional_bool(_first(details, "latest_tick_ready")),
        "latest_tick_age_sec": _number(_first(details, "latest_tick_age_sec")),
        "candidate_market_status": _upper_first(details, "candidate_market_status", "market_status"),
        "candidate_market_confirmed_status": _upper_first(details, "candidate_market_confirmed_status", "market_confirmed_status"),
        "global_market_status": _upper_first(details, "global_market_status"),
        "data_quality_bucket": _upper_first(details, "data_quality_bucket"),
        "missing_core_fields": [str(item) for item in _list(_first(details, "missing_core_fields"))],
        "reason_codes": reasons,
        "position_size_multiplier": _number(_first(details, "position_size_multiplier")) or 1.0,
        "risk_off_entry_allowed": _bool(_first(details, "risk_off_entry_allowed")),
        "risk_off_relative_strength_pct": _number(_first(details, "risk_off_relative_strength_pct")),
        "risk_off_max_position_size_multiplier": _number(_first(details, "risk_off_max_position_size_multiplier")),
        "vi_status": _upper_first(details, "vi_status"),
        "upper_limit_gap_pct": _number(_first(details, "upper_limit_gap_pct")),
        "upper_wick_risk": _optional_bool(_first(details, "upper_wick_risk")),
        "volume_reaccel": _bool(_first(details, "volume_reaccel")),
        "momentum_1m": _number(_first(details, "momentum_1m")),
        "momentum_3m": _number(_first(details, "momentum_3m")),
        "turnover_krw": _number(_first(details, "turnover_krw", "today_turnover_krw", "turnover")),
    }


def _reason_codes(details: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in (
        "reason_codes",
        "secondary_reason_codes",
        "risk_reason_codes",
        "entry_risk_reason_codes",
        "price_location_reason_codes",
        "price_location_readiness_reason_codes",
        "market_side_reason_codes",
        "hybrid_reason_codes",
        "hard_guard_codes",
        "data_quality_flags",
        "price_location_data_quality_flags",
    ):
        values.extend(_list(details.get(key)))
    if details.get("primary_reason_code"):
        values.append(details.get("primary_reason_code"))
    if details.get("primary_reason"):
        values.append(details.get("primary_reason"))
    return normalize_reason_codes(str(value).upper() for value in values if str(value or "").strip())


def _first(details: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = details.get(key)
        if value not in (None, ""):
            return value
    return None


def _upper_first(details: Mapping[str, Any], *keys: str) -> str:
    value = _first(details, *keys)
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip().upper()


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return _bool(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any, default: float) -> float:
    number = _number(value)
    if number is None or number <= 0.0:
        return default
    return number


def _small_multiplier(snapshot: dict[str, Any], default: float) -> float:
    risk_off_cap = _number(snapshot.get("risk_off_max_position_size_multiplier"))
    existing = _number(snapshot.get("position_size_multiplier"))
    candidates = [value for value in (risk_off_cap, existing, default) if value is not None and value > 0.0]
    return min(candidates) if candidates else default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(maximum, number))
