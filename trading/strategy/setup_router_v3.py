from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.setup_features import SetupFeatureSnapshot


SETUP_ROUTER_SCHEMA_VERSION = "setup_router_v3.observe.v1"
SETUP_ROUTER_OUTPUT_MODE = "OBSERVE"


class SetupType(str, Enum):
    LEADER_FIRST_PULLBACK = "LEADER_FIRST_PULLBACK"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    AVOID = "AVOID"
    UNKNOWN = "UNKNOWN"


class SetupShapeStatus(str, Enum):
    NOT_SEEN = "NOT_SEEN"
    FORMING = "FORMING"
    MATCHED = "MATCHED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    DATA_WAIT = "DATA_WAIT"


class SetupContextStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    WAIT = "WAIT"
    BLOCKED = "BLOCKED"
    DATA_WAIT = "DATA_WAIT"


class SetupRouterStatus(str, Enum):
    VALID_OBSERVE = "VALID_OBSERVE"
    PENDING = "PENDING"
    DATA_WAIT = "DATA_WAIT"
    CONTEXT_BLOCKED = "CONTEXT_BLOCKED"
    AVOID = "AVOID"
    UNKNOWN = "UNKNOWN"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class EntryAlignmentStatus(str, Enum):
    ENTRY_OBSERVE_READY = "ENTRY_OBSERVE_READY"
    ENTRY_PRICE_WAIT = "ENTRY_PRICE_WAIT"
    ENTRY_THEME_WAIT = "ENTRY_THEME_WAIT"
    ENTRY_MARKET_WAIT = "ENTRY_MARKET_WAIT"
    ENTRY_DATA_WAIT = "ENTRY_DATA_WAIT"
    ENTRY_HARD_BLOCK = "ENTRY_HARD_BLOCK"
    ENTRY_DECISION_MISSING = "ENTRY_DECISION_MISSING"


@dataclass(frozen=True)
class SetupRouterConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: float = 1.0
    max_candidates_per_cycle: int = 100
    periodic_reconcile_sec: int = 30
    min_completed_1m_candles: int = 3
    save_history: bool = True
    max_tick_age_sec: int = 30
    leader_pullback_min_pct: float = 0.7
    leader_pullback_max_pct: float = 3.5
    leader_deep_invalidate_pct: float = 5.5
    leader_max_below_vwap_pct: float = 0.7
    leader_ttl_sec: int = 900
    vwap_prior_below_min_pct: float = 0.15
    vwap_reclaim_above_min_pct: float = 0.05
    vwap_max_extension_pct: float = 1.5
    vwap_invalidate_below_pct: float = 0.5
    vwap_lookback: int = 5
    vwap_ttl_sec: int = 600
    breakout_buffer_pct: float = 0.25
    retest_lower_tol_pct: float = 0.30
    retest_upper_tol_pct: float = 0.80
    retest_hold_pct: float = 0.20
    retest_invalidate_below_pct: float = 0.70
    breakout_lookback: int = 5
    breakout_min_bars_between: int = 1
    breakout_ttl_sec: int = 900

    @classmethod
    def from_env(cls) -> "SetupRouterConfig":
        return cls(
            enabled=_env_bool("TRADING_SETUP_ROUTER_V3_ENABLED", False),
            observe_only=_env_bool("TRADING_SETUP_ROUTER_V3_OBSERVE_ONLY", True),
            interval_sec=max(0.1, _env_float("TRADING_SETUP_ROUTER_V3_INTERVAL_SEC", 1.0)),
            max_candidates_per_cycle=max(1, _env_int("TRADING_SETUP_ROUTER_V3_MAX_CANDIDATES_PER_CYCLE", 100)),
            periodic_reconcile_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_PERIODIC_RECONCILE_SEC", 30)),
            min_completed_1m_candles=max(0, _env_int("TRADING_SETUP_ROUTER_V3_MIN_COMPLETED_1M_CANDLES", 3)),
            save_history=_env_bool("TRADING_SETUP_ROUTER_V3_SAVE_HISTORY", True),
            max_tick_age_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_MAX_TICK_AGE_SEC", 30)),
        )


@dataclass(frozen=True)
class SetupHypothesis:
    setup_type: SetupType
    shape_status: SetupShapeStatus
    reason_codes: tuple[str, ...] = ()
    price_structure: dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0


@dataclass(frozen=True)
class SetupObservation:
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    candidate_instance_id: str
    code: str
    name: str
    setup_type: str
    shape_status: str
    context_status: str
    router_status: str
    entry_alignment_status: str
    primary_setup: bool
    setup_quality_score: float
    context_id: str
    theme_id: str
    theme_name: str
    theme_state: str
    leadership_status: str
    stock_role: str
    market_side: str
    market_action: str
    session_phase: str
    current_price: float
    fingerprint: str
    reason_codes: tuple[str, ...] = ()
    price_structure: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SETUP_ROUTER_SCHEMA_VERSION
    output_mode: str = SETUP_ROUTER_OUTPUT_MODE
    ready_allowed: bool = False
    candidate_promotion_allowed: bool = False
    opportunity_rank_allowed: bool = False
    order_intent_allowed: bool = False
    live_order_allowed: bool = False
    recommended_position_size_multiplier: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class SetupRouterV3:
    def __init__(self, config: SetupRouterConfig | None = None) -> None:
        self.config = config or SetupRouterConfig()

    def classify(self, feature: SetupFeatureSnapshot) -> list[SetupObservation]:
        context_status, context_reasons = self._context_status(feature)
        entry_alignment = _entry_alignment_status(feature)
        hypotheses = [
            self._classify_leader_first_pullback(feature),
            self._classify_vwap_reclaim(feature),
            self._classify_breakout_retest(feature),
        ]
        filtered = [item for item in hypotheses if item.setup_type not in {SetupType.AVOID, SetupType.UNKNOWN}]
        primary_type = self._primary_setup_type(filtered, context_status, feature)
        observations = []
        for hypothesis in filtered:
            setup_context = self._context_for_setup(hypothesis.setup_type, context_status, context_reasons, feature)
            router_status = self._router_status(hypothesis.shape_status, setup_context)
            reasons = _dedupe(
                [
                    *feature.data_wait_reasons,
                    *context_reasons,
                    *feature.entry_reason_codes,
                    *hypothesis.reason_codes,
                    "SETUP_ROUTER_V3_OBSERVE_ONLY",
                ]
            )
            evidence = {
                "context": {
                    "context_id": feature.context_id,
                    "context_fresh": feature.context_fresh,
                    "theme_state": feature.theme_state,
                    "leadership_status": feature.leadership_status,
                    "stock_role": feature.stock_role,
                    "market_action": feature.market_action,
                    "side_market_regime": feature.side_market_regime,
                },
                "entry_decision": {
                    "entry_status": feature.entry_status,
                    "price_location": feature.entry_price_location,
                    "alignment_status": entry_alignment.value,
                },
                "data": {
                    "realtime_tick_fresh": feature.realtime_tick_fresh,
                    "completed_1m_count": feature.completed_1m_count,
                    "latest_completed_candle_at": feature.latest_completed_candle_at,
                    "tick_at": feature.tick_at,
                    "price_source": feature.price_source,
                },
            }
            observations.append(
                SetupObservation(
                    trade_date=feature.trade_date,
                    calculated_at=feature.calculated_at,
                    candidate_id=feature.candidate_id,
                    candidate_instance_id=feature.candidate_instance_id,
                    code=feature.code,
                    name=feature.name,
                    setup_type=hypothesis.setup_type.value,
                    shape_status=hypothesis.shape_status.value,
                    context_status=setup_context.value,
                    router_status=router_status.value,
                    entry_alignment_status=entry_alignment.value,
                    primary_setup=hypothesis.setup_type == primary_type,
                    setup_quality_score=round(hypothesis.quality_score + _context_score(setup_context), 3),
                    context_id=feature.context_id,
                    theme_id=feature.theme_id,
                    theme_name=feature.theme_name,
                    theme_state=feature.theme_state,
                    leadership_status=feature.leadership_status,
                    stock_role=feature.stock_role,
                    market_side=feature.market_side,
                    market_action=feature.market_action,
                    session_phase=feature.session_phase,
                    current_price=feature.current_price,
                    fingerprint=_fingerprint(feature, hypothesis, setup_context, router_status),
                    reason_codes=tuple(reasons),
                    price_structure=dict(hypothesis.price_structure or {}),
                    evidence=evidence,
                    safety=_safety_flags(),
                )
            )
        return observations

    def _context_status(self, feature: SetupFeatureSnapshot) -> tuple[SetupContextStatus, tuple[str, ...]]:
        reasons: list[str] = []
        if feature.data_wait_reasons:
            return SetupContextStatus.DATA_WAIT, tuple(feature.data_wait_reasons)
        market_session = feature.market_session_status.upper()
        theme_state = feature.theme_state.upper()
        role = feature.stock_role.upper()
        leadership = feature.leadership_status.upper()
        market_action = feature.market_action.upper()
        side = feature.side_market_regime.upper()
        if (
            feature.systemic_risk_off
            or feature.market_block_new_entry
            or feature.block_new_entry
            or feature.vi_active
            or feature.upper_limit_near
            or feature.overheated
            or feature.chase_risk
            or feature.stale_data_block
            or side in {"RISK_OFF", "WEAK"}
            or theme_state in {"FADING_THEME", "WEAK_THEME"}
            or leadership in {"LOSING_LEADERSHIP", "ROTATED_OUT"}
            or market_session in {"CLOSING", "MARKET_CLOSED", "CLOSED"}
        ):
            reasons.extend(
                _flag_reasons(
                    [
                        (feature.systemic_risk_off, "SYSTEMIC_RISK_OFF"),
                        (feature.market_block_new_entry or feature.block_new_entry, "MARKET_BLOCK_NEW_ENTRY"),
                        (feature.vi_active, "VI_BLOCK"),
                        (feature.upper_limit_near, "UPPER_LIMIT_NEAR_BLOCK"),
                        (feature.overheated, "OVERHEATED_BLOCK"),
                        (feature.chase_risk, "CHASE_RISK_BLOCK"),
                        (feature.stale_data_block, "STALE_DATA_BLOCK"),
                        (side in {"RISK_OFF", "WEAK"}, f"SIDE_MARKET_{side}_BLOCK" if side else "SIDE_MARKET_BLOCK"),
                        (theme_state in {"FADING_THEME", "WEAK_THEME"}, f"THEME_{theme_state}_BLOCK" if theme_state else "THEME_BLOCK"),
                        (leadership in {"LOSING_LEADERSHIP", "ROTATED_OUT"}, f"LEADERSHIP_{leadership}_BLOCK" if leadership else "LEADERSHIP_BLOCK"),
                        (market_session in {"CLOSING", "MARKET_CLOSED", "CLOSED"}, "SESSION_CLOSED_BLOCK"),
                    ]
                )
            )
            return SetupContextStatus.BLOCKED, tuple(_dedupe(reasons))
        if (
            theme_state in {"EMERGING_THEME", "WATCH_THEME", "DATA_WAIT"}
            or leadership in {"CHALLENGER", "TAKEOVER_PENDING"}
            or feature.leadership_wait_new_entry
            or market_action in {"DATA_WAIT", "WAIT", "WAIT_MARKET", "MIDDAY_CHOP", "CHOPPY"}
            or side in {"CHOPPY", "DATA_WAIT"}
        ):
            reasons.extend(
                _flag_reasons(
                    [
                        (theme_state in {"EMERGING_THEME", "WATCH_THEME", "DATA_WAIT"}, f"THEME_{theme_state}_WAIT" if theme_state else "THEME_WAIT"),
                        (leadership in {"CHALLENGER", "TAKEOVER_PENDING"}, f"LEADERSHIP_{leadership}_WAIT" if leadership else "LEADERSHIP_WAIT"),
                        (feature.leadership_wait_new_entry, "LEADERSHIP_WAIT_NEW_ENTRY"),
                        (market_action in {"DATA_WAIT", "WAIT", "WAIT_MARKET", "MIDDAY_CHOP", "CHOPPY"}, f"MARKET_ACTION_{market_action}_WAIT" if market_action else "MARKET_WAIT"),
                        (side in {"CHOPPY", "DATA_WAIT"}, f"SIDE_MARKET_{side}_WAIT" if side else "SIDE_MARKET_WAIT"),
                    ]
                )
            )
            return SetupContextStatus.WAIT, tuple(_dedupe(reasons))
        allowed_theme = theme_state in {"LEADING_THEME", "SPREADING_THEME"} or (
            theme_state == "LEADER_ONLY_THEME" and role in {"LEADER_CONFIRMED", "CO_LEADER_CONFIRMED", "LEADER", "CO_LEADER"}
        )
        allowed_role = role in {"LEADER_CONFIRMED", "CO_LEADER_CONFIRMED", "LEADER", "CO_LEADER"}
        allowed_market = market_action in {"ALLOW_NORMAL", "ALLOW_REDUCED", ""}
        allowed_leadership = leadership in {"INCUMBENT", "TAKEOVER_CONFIRMED", ""}
        if allowed_theme and allowed_role and allowed_market and allowed_leadership:
            if not leadership:
                reasons.append("LEADERSHIP_STATUS_BLANK_DIAGNOSTIC")
            return SetupContextStatus.ELIGIBLE, tuple(_dedupe(reasons))
        reasons.extend(
            _flag_reasons(
                [
                    (not allowed_theme, "THEME_NOT_SETUP_ELIGIBLE"),
                    (not allowed_role, "ROLE_NOT_SETUP_ELIGIBLE"),
                    (not allowed_market, "MARKET_ACTION_NOT_SETUP_ELIGIBLE"),
                    (not allowed_leadership, "LEADERSHIP_NOT_SETUP_ELIGIBLE"),
                ]
            )
        )
        return SetupContextStatus.WAIT, tuple(_dedupe(reasons))

    def _context_for_setup(
        self,
        setup_type: SetupType,
        context_status: SetupContextStatus,
        context_reasons: Iterable[str],
        feature: SetupFeatureSnapshot,
    ) -> SetupContextStatus:
        if setup_type == SetupType.BREAKOUT_RETEST and context_status == SetupContextStatus.ELIGIBLE:
            if feature.market_action.upper() == "ALLOW_REDUCED":
                return SetupContextStatus.WAIT
        return context_status

    def _classify_leader_first_pullback(self, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return SetupHypothesis(SetupType.LEADER_FIRST_PULLBACK, SetupShapeStatus.DATA_WAIT, ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        peak = _local_peak(feature)
        pullback = feature.pullback_from_high_pct or _pullback_pct(feature.current_price, peak)
        below_vwap_pct = _below_vwap_pct(feature.current_price, feature.vwap)
        structure = {
            "local_peak": peak,
            "pullback_from_high_pct": round(pullback, 4),
            "vwap": feature.vwap,
            "below_vwap_pct": round(below_vwap_pct, 4),
            "momentum_1m_pct": feature.momentum_1m_pct,
        }
        if pullback > self.config.leader_deep_invalidate_pct:
            return SetupHypothesis(SetupType.LEADER_FIRST_PULLBACK, SetupShapeStatus.INVALIDATED, ("PULLBACK_TOO_DEEP",), structure, 0.0)
        if peak <= 0:
            return SetupHypothesis(SetupType.LEADER_FIRST_PULLBACK, SetupShapeStatus.NOT_SEEN, ("LOCAL_PEAK_NOT_FOUND",), structure, 0.0)
        matched = (
            self.config.leader_pullback_min_pct <= pullback <= self.config.leader_pullback_max_pct
            and below_vwap_pct <= self.config.leader_max_below_vwap_pct
        )
        if matched:
            return SetupHypothesis(SetupType.LEADER_FIRST_PULLBACK, SetupShapeStatus.MATCHED, ("LEADER_FIRST_PULLBACK_MATCHED",), structure, 78.0)
        return SetupHypothesis(SetupType.LEADER_FIRST_PULLBACK, SetupShapeStatus.FORMING, ("LEADER_FIRST_PULLBACK_FORMING",), structure, 45.0)

    def _classify_vwap_reclaim(self, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.DATA_WAIT, ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        if feature.vwap <= 0 or feature.current_price <= 0:
            return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.DATA_WAIT, ("VWAP_OR_PRICE_MISSING",), {}, 0.0)
        window = feature.completed_1m_candles[-self.config.vwap_lookback :]
        prior_below = any(_float(candle.get("close")) <= feature.vwap * (1 - self.config.vwap_prior_below_min_pct / 100.0) for candle in window)
        above_pct = (feature.current_price - feature.vwap) / feature.vwap * 100.0
        structure = {
            "vwap": feature.vwap,
            "prior_below_vwap": prior_below,
            "current_above_vwap_pct": round(above_pct, 4),
            "lookback": len(window),
        }
        if above_pct < -self.config.vwap_invalidate_below_pct:
            return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.INVALIDATED, ("VWAP_RECLAIM_LOST",), structure, 0.0)
        if prior_below and self.config.vwap_reclaim_above_min_pct <= above_pct <= self.config.vwap_max_extension_pct:
            return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.MATCHED, ("VWAP_RECLAIM_MATCHED",), structure, 82.0)
        if prior_below:
            return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.FORMING, ("VWAP_RECLAIM_FORMING",), structure, 48.0)
        return SetupHypothesis(SetupType.VWAP_RECLAIM, SetupShapeStatus.NOT_SEEN, ("VWAP_PRIOR_BELOW_NOT_SEEN",), structure, 0.0)

    def _classify_breakout_retest(self, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.DATA_WAIT, ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        reference = _breakout_reference(feature)
        if reference <= 0:
            return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.NOT_SEEN, ("BREAKOUT_LEVEL_MISSING",), {}, 0.0)
        window = feature.completed_1m_candles[-self.config.breakout_lookback :]
        breakout_index = -1
        for index, candle in enumerate(window):
            if _float(candle.get("high")) >= reference * (1 + self.config.breakout_buffer_pct / 100.0):
                breakout_index = index
                break
        bars_after = len(window) - breakout_index - 1 if breakout_index >= 0 else 0
        low_bound = reference * (1 - self.config.retest_lower_tol_pct / 100.0)
        high_bound = reference * (1 + self.config.retest_upper_tol_pct / 100.0)
        hold_bound = reference * (1 - self.config.retest_hold_pct / 100.0)
        invalidate_bound = reference * (1 - self.config.retest_invalidate_below_pct / 100.0)
        price = feature.current_price
        structure = {
            "breakout_level": reference,
            "breakout_seen": breakout_index >= 0,
            "bars_after_breakout": bars_after,
            "retest_band_low": round(low_bound, 4),
            "retest_band_high": round(high_bound, 4),
            "current_price": price,
        }
        if price < invalidate_bound:
            return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.INVALIDATED, ("BREAKOUT_RETEST_INVALIDATED",), structure, 0.0)
        if breakout_index >= 0 and bars_after >= self.config.breakout_min_bars_between and low_bound <= price <= high_bound and price >= hold_bound:
            return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.MATCHED, ("BREAKOUT_RETEST_MATCHED",), structure, 80.0)
        if breakout_index >= 0:
            return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.FORMING, ("BREAKOUT_RETEST_FORMING",), structure, 46.0)
        return SetupHypothesis(SetupType.BREAKOUT_RETEST, SetupShapeStatus.NOT_SEEN, ("BREAKOUT_NOT_SEEN",), structure, 0.0)

    def _router_status(self, shape_status: SetupShapeStatus, context_status: SetupContextStatus) -> SetupRouterStatus:
        if shape_status == SetupShapeStatus.DATA_WAIT or context_status == SetupContextStatus.DATA_WAIT:
            return SetupRouterStatus.DATA_WAIT
        if shape_status == SetupShapeStatus.INVALIDATED:
            return SetupRouterStatus.INVALIDATED
        if shape_status == SetupShapeStatus.EXPIRED:
            return SetupRouterStatus.EXPIRED
        if shape_status == SetupShapeStatus.MATCHED and context_status == SetupContextStatus.ELIGIBLE:
            return SetupRouterStatus.VALID_OBSERVE
        if shape_status == SetupShapeStatus.MATCHED and context_status == SetupContextStatus.WAIT:
            return SetupRouterStatus.PENDING
        if shape_status == SetupShapeStatus.MATCHED and context_status == SetupContextStatus.BLOCKED:
            return SetupRouterStatus.CONTEXT_BLOCKED
        if shape_status == SetupShapeStatus.FORMING and context_status in {SetupContextStatus.ELIGIBLE, SetupContextStatus.WAIT}:
            return SetupRouterStatus.PENDING
        if context_status == SetupContextStatus.BLOCKED:
            return SetupRouterStatus.AVOID
        return SetupRouterStatus.UNKNOWN

    def _primary_setup_type(
        self,
        hypotheses: list[SetupHypothesis],
        context_status: SetupContextStatus,
        feature: SetupFeatureSnapshot,
    ) -> SetupType:
        priority = [SetupType.VWAP_RECLAIM, SetupType.BREAKOUT_RETEST, SetupType.LEADER_FIRST_PULLBACK]
        previous = str(dict(feature.previous_observation or {}).get("setup_type") or "")
        for setup_type in priority:
            if previous == setup_type.value and any(item.setup_type == setup_type and item.shape_status == SetupShapeStatus.MATCHED for item in hypotheses):
                return setup_type
        for setup_type in priority:
            if any(item.setup_type == setup_type and item.shape_status == SetupShapeStatus.MATCHED for item in hypotheses):
                return setup_type
        for setup_type in priority:
            if any(item.setup_type == setup_type and item.shape_status == SetupShapeStatus.FORMING for item in hypotheses):
                return setup_type
        return priority[0]


def _entry_alignment_status(feature: SetupFeatureSnapshot) -> EntryAlignmentStatus:
    if not feature.entry_decision:
        return EntryAlignmentStatus.ENTRY_DECISION_MISSING
    status = feature.entry_status.upper()
    if status in {"OBSERVE_READY", "READY", "TIMING_READY"}:
        return EntryAlignmentStatus.ENTRY_OBSERVE_READY
    if status in {"PRICE_WAIT"}:
        return EntryAlignmentStatus.ENTRY_PRICE_WAIT
    if status in {"THEME_WAIT"}:
        return EntryAlignmentStatus.ENTRY_THEME_WAIT
    if status in {"MARKET_WAIT"}:
        return EntryAlignmentStatus.ENTRY_MARKET_WAIT
    if status in {"DATA_WAIT"}:
        return EntryAlignmentStatus.ENTRY_DATA_WAIT
    if status in {"HARD_BLOCK", "BLOCKED"}:
        return EntryAlignmentStatus.ENTRY_HARD_BLOCK
    return EntryAlignmentStatus.ENTRY_PRICE_WAIT


def _local_peak(feature: SetupFeatureSnapshot) -> float:
    highs = [_float(candle.get("high")) for candle in feature.completed_1m_candles if _float(candle.get("high")) > 0]
    if feature.day_high > 0:
        highs.append(feature.day_high)
    return max(highs) if highs else 0.0


def _breakout_reference(feature: SetupFeatureSnapshot) -> float:
    details = dict(feature.entry_decision.get("details") or {})
    for value in (
        details.get("breakout_level"),
        details.get("reference_high"),
        feature.strategy_context.get("breakout_level"),
        dict(feature.strategy_context.get("stock") or {}).get("breakout_level"),
    ):
        number = _float(value)
        if number > 0:
            return number
    if len(feature.completed_1m_candles) < 2:
        return 0.0
    previous = feature.completed_1m_candles[:-1]
    highs = [_float(candle.get("high")) for candle in previous if _float(candle.get("high")) > 0]
    return max(highs) if highs else 0.0


def _below_vwap_pct(price: float, vwap: float) -> float:
    if price <= 0 or vwap <= 0 or price >= vwap:
        return 0.0
    return (vwap - price) / vwap * 100.0


def _pullback_pct(price: float, high: float) -> float:
    if price <= 0 or high <= 0:
        return 0.0
    return max(0.0, (high - price) / high * 100.0)


def _context_score(status: SetupContextStatus) -> float:
    if status == SetupContextStatus.ELIGIBLE:
        return 15.0
    if status == SetupContextStatus.WAIT:
        return 5.0
    return 0.0


def _fingerprint(
    feature: SetupFeatureSnapshot,
    hypothesis: SetupHypothesis,
    context_status: SetupContextStatus,
    router_status: SetupRouterStatus,
) -> str:
    material = "|".join(
        [
            SETUP_ROUTER_SCHEMA_VERSION,
            feature.trade_date,
            feature.candidate_instance_id,
            hypothesis.setup_type.value,
            hypothesis.shape_status.value,
            context_status.value,
            router_status.value,
            feature.context_id,
            feature.latest_completed_candle_at,
            str(round(feature.current_price or 0.0, 2)),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _safety_flags() -> dict[str, Any]:
    return {
        "observe_only": True,
        "ready_allowed": False,
        "candidate_promotion_allowed": False,
        "opportunity_rank_allowed": False,
        "order_intent_allowed": False,
        "live_order_allowed": False,
        "recommended_position_size_multiplier": 0,
    }


def _flag_reasons(items: Iterable[tuple[bool, str]]) -> list[str]:
    return [reason for flag, reason in items if flag and reason]


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default
