from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.setup_features import SETUP_ROUTER_FEATURE_SCHEMA_VERSION, SetupFeatureSnapshot


SETUP_ROUTER_SCHEMA_VERSION = "setup_router_v3.observe.v2"
SETUP_ROUTER_VERSION = "setup_router_v3.2"
SETUP_ROUTER_STATE_VERSION = "setup_router_v3.state.v1"
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


class SetupLifecycleState(str, Enum):
    SEEKING = "SEEKING"
    FORMING = "FORMING"
    MATCHED = "MATCHED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


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
    ENTRY_DECISION_STALE = "ENTRY_DECISION_STALE"


@dataclass(frozen=True)
class SetupRouterConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: float = 1.0
    max_candidates_per_cycle: int = 100
    periodic_reconcile_sec: int = 30
    run_heartbeat_sec: int = 30
    min_completed_1m_candles: int = 3
    save_history: bool = True
    max_tick_age_sec: int = 10
    entry_decision_max_age_sec: int = 60
    leader_pullback_min_pct: float = 0.7
    leader_pullback_max_pct: float = 3.5
    leader_deep_invalidate_pct: float = 5.5
    leader_max_below_vwap_pct: float = 0.7
    leader_local_peak_min_age_sec: int = 60
    leader_new_peak_generation_min_pct: float = 0.5
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
        market_tick_age = _env_int("TRADING_MARKET_DATA_MAX_TICK_AGE_SEC", 10)
        return cls(
            enabled=_env_bool("TRADING_SETUP_ROUTER_V3_ENABLED", False),
            observe_only=_env_bool("TRADING_SETUP_ROUTER_V3_OBSERVE_ONLY", True),
            interval_sec=max(0.1, _env_float("TRADING_SETUP_ROUTER_V3_INTERVAL_SEC", 1.0)),
            max_candidates_per_cycle=max(1, _env_int("TRADING_SETUP_ROUTER_V3_MAX_CANDIDATES_PER_CYCLE", 100)),
            periodic_reconcile_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_PERIODIC_RECONCILE_SEC", 30)),
            run_heartbeat_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_RUN_HEARTBEAT_SEC", 30)),
            min_completed_1m_candles=max(0, _env_int("TRADING_SETUP_ROUTER_V3_MIN_COMPLETED_1M_CANDLES", 3)),
            save_history=_env_bool("TRADING_SETUP_ROUTER_V3_SAVE_HISTORY", True),
            max_tick_age_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_MAX_TICK_AGE_SEC", market_tick_age)),
            entry_decision_max_age_sec=max(1, _env_int("TRADING_SETUP_ROUTER_V3_ENTRY_DECISION_MAX_AGE_SEC", 60)),
            leader_pullback_min_pct=_env_float("TRADING_SETUP_LFP_PULLBACK_MIN_PCT", 0.7),
            leader_pullback_max_pct=_env_float("TRADING_SETUP_LFP_PULLBACK_MAX_PCT", 3.5),
            leader_deep_invalidate_pct=_env_float("TRADING_SETUP_LFP_PULLBACK_DEEP_PCT", 5.5),
            leader_max_below_vwap_pct=_env_float("TRADING_SETUP_LFP_MAX_BELOW_VWAP_PCT", 0.7),
            leader_local_peak_min_age_sec=max(0, _env_int("TRADING_SETUP_LFP_LOCAL_PEAK_MIN_AGE_SEC", 60)),
            leader_new_peak_generation_min_pct=_env_float("TRADING_SETUP_LFP_NEW_PEAK_GENERATION_MIN_PCT", 0.5),
            leader_ttl_sec=max(1, _env_int("TRADING_SETUP_LFP_TTL_SEC", 900)),
            vwap_prior_below_min_pct=_env_float("TRADING_SETUP_VWAP_PRIOR_BELOW_MIN_PCT", 0.15),
            vwap_reclaim_above_min_pct=_env_float("TRADING_SETUP_VWAP_RECLAIM_ABOVE_MIN_PCT", 0.05),
            vwap_max_extension_pct=_env_float("TRADING_SETUP_VWAP_MAX_EXTENSION_PCT", 1.5),
            vwap_invalidate_below_pct=_env_float("TRADING_SETUP_VWAP_INVALIDATE_BELOW_PCT", 0.5),
            vwap_lookback=max(1, _env_int("TRADING_SETUP_VWAP_LOOKBACK", 5)),
            vwap_ttl_sec=max(1, _env_int("TRADING_SETUP_VWAP_TTL_SEC", 600)),
            breakout_buffer_pct=_env_float("TRADING_SETUP_BREAKOUT_BUFFER_PCT", 0.25),
            retest_lower_tol_pct=_env_float("TRADING_SETUP_BREAKOUT_RETEST_LOWER_TOL_PCT", 0.30),
            retest_upper_tol_pct=_env_float("TRADING_SETUP_BREAKOUT_RETEST_UPPER_TOL_PCT", 0.80),
            retest_hold_pct=_env_float("TRADING_SETUP_BREAKOUT_RETEST_HOLD_PCT", 0.20),
            retest_invalidate_below_pct=_env_float("TRADING_SETUP_BREAKOUT_INVALIDATE_BELOW_PCT", 0.70),
            breakout_lookback=max(2, _env_int("TRADING_SETUP_BREAKOUT_LOOKBACK", 5)),
            breakout_min_bars_between=max(1, _env_int("TRADING_SETUP_BREAKOUT_MIN_BARS_BETWEEN", 1)),
            breakout_ttl_sec=max(1, _env_int("TRADING_SETUP_BREAKOUT_TTL_SEC", 900)),
        )


@dataclass(frozen=True)
class SetupHypothesis:
    setup_type: SetupType
    shape_status: SetupShapeStatus
    reason_codes: tuple[str, ...] = ()
    price_structure: dict[str, Any] = field(default_factory=dict)
    quality_score: float = 0.0
    lifecycle_state: SetupLifecycleState = SetupLifecycleState.SEEKING
    setup_generation: int = 1
    setup_instance_id: str = ""
    state_payload: dict[str, Any] = field(default_factory=dict)
    last_material_change_at: str = ""


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
    lifecycle_state: str
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
    setup_generation: int = 1
    setup_instance_id: str = ""
    state_payload: dict[str, Any] = field(default_factory=dict)
    last_material_change_at: str = ""
    post_subscription_tick_verified: bool = True
    entry_decision_id: int | None = None
    entry_decision_at: str = ""
    entry_decision_age_sec: float = 0.0
    entry_decision_fresh: bool = False
    entry_decision_source: str = ""
    reason_codes: tuple[str, ...] = ()
    price_structure: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SETUP_ROUTER_SCHEMA_VERSION
    feature_schema_version: str = SETUP_ROUTER_FEATURE_SCHEMA_VERSION
    router_version: str = SETUP_ROUTER_VERSION
    state_version: str = SETUP_ROUTER_STATE_VERSION
    output_mode: str = SETUP_ROUTER_OUTPUT_MODE
    ready_allowed: bool = False
    candidate_promotion_allowed: bool = False
    opportunity_rank_allowed: bool = False
    order_intent_allowed: bool = False
    live_order_allowed: bool = False
    recommended_position_size_multiplier: float = 0.0
    quantity: int = 0

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
        filtered = [
            self._apply_session_shape_guard(item, feature)
            for item in hypotheses
            if item.setup_type not in {SetupType.AVOID, SetupType.UNKNOWN}
        ]
        primary_type = self._primary_setup_type(filtered, feature)
        observations: list[SetupObservation] = []
        for hypothesis in filtered:
            setup_context = self._context_for_setup(hypothesis.setup_type, context_status, context_reasons, feature)
            router_status = self._router_status(hypothesis.shape_status, setup_context, entry_alignment)
            reasons = _dedupe(
                [
                    *feature.data_wait_reasons,
                    *context_reasons,
                    *feature.entry_reason_codes,
                    *_entry_reason_codes(entry_alignment),
                    *hypothesis.reason_codes,
                    "SETUP_ROUTER_V3_OBSERVE_ONLY",
                ]
            )
            evidence = {
                "versions": {
                    "schema_version": SETUP_ROUTER_SCHEMA_VERSION,
                    "feature_schema_version": feature.schema_version,
                    "router_version": SETUP_ROUTER_VERSION,
                    "state_version": SETUP_ROUTER_STATE_VERSION,
                },
                "context": {
                    "context_id": feature.context_id,
                    "context_fresh": feature.context_fresh,
                    "theme_state": feature.theme_state,
                    "leadership_status": feature.leadership_status,
                    "stock_role": feature.stock_role,
                    "market_action": feature.market_action,
                    "side_market_regime": feature.side_market_regime,
                    "session_phase": feature.session_phase,
                    "market_session_status": feature.market_session_status,
                },
                "entry_decision": {
                    "entry_decision_id": feature.entry_decision_id,
                    "entry_decision_at": feature.entry_decision_at,
                    "entry_decision_age_sec": feature.entry_decision_age_sec,
                    "entry_decision_fresh": feature.entry_decision_fresh,
                    "entry_status": feature.entry_status,
                    "price_location": feature.entry_price_location,
                    "alignment_status": entry_alignment.value,
                    "source": feature.entry_decision_source,
                },
                "data": {
                    "realtime_tick_fresh": feature.realtime_tick_fresh,
                    "completed_1m_count": feature.completed_1m_count,
                    "latest_completed_candle_at": feature.latest_completed_candle_at,
                    "tick_at": feature.tick_at,
                    "tick_age_sec": feature.tick_age_sec,
                    "price_source": feature.price_source,
                    "max_tick_age_sec": self.config.max_tick_age_sec,
                    "post_subscription_tick_verified": feature.post_subscription_tick_verified,
                    "post_subscription_tick_reason": feature.post_subscription_tick_reason,
                },
                "expansion_lease": {
                    "present": feature.expansion_lease_present,
                    "status": feature.lease_status,
                    "selected_at": feature.lease_selected_at,
                    "first_active_at": feature.lease_first_active_at,
                    "first_fresh_tick_at": feature.lease_first_fresh_tick_at,
                },
            }
            primary = hypothesis.setup_type == primary_type and hypothesis.shape_status in {SetupShapeStatus.MATCHED, SetupShapeStatus.FORMING}
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
                    lifecycle_state=hypothesis.lifecycle_state.value,
                    context_status=setup_context.value,
                    router_status=router_status.value,
                    entry_alignment_status=entry_alignment.value,
                    primary_setup=primary,
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
                    fingerprint=_fingerprint(feature, hypothesis, setup_context, router_status, entry_alignment),
                    setup_generation=max(1, int(hypothesis.setup_generation or 1)),
                    setup_instance_id=hypothesis.setup_instance_id or _setup_instance_id(feature, hypothesis.setup_type, hypothesis.setup_generation),
                    state_payload=dict(hypothesis.state_payload or {}),
                    last_material_change_at=hypothesis.last_material_change_at or feature.calculated_at,
                    post_subscription_tick_verified=bool(feature.post_subscription_tick_verified),
                    entry_decision_id=feature.entry_decision_id,
                    entry_decision_at=feature.entry_decision_at,
                    entry_decision_age_sec=feature.entry_decision_age_sec,
                    entry_decision_fresh=feature.entry_decision_fresh,
                    entry_decision_source=feature.entry_decision_source,
                    reason_codes=tuple(reasons),
                    price_structure=dict(hypothesis.price_structure or {}),
                    evidence=evidence,
                    safety=_safety_flags(),
                )
            )
        return observations

    def _context_status(self, feature: SetupFeatureSnapshot) -> tuple[SetupContextStatus, tuple[str, ...]]:
        reasons: list[str] = []
        session_phase = feature.session_phase.upper()
        if session_phase == "PRE_OPEN":
            return SetupContextStatus.BLOCKED, ("SETUP_PRE_OPEN_BLOCK",)
        if session_phase == "CLOSING_RISK":
            return SetupContextStatus.BLOCKED, ("SETUP_CLOSING_RISK_BLOCK",)
        if session_phase == "MARKET_CLOSED":
            return SetupContextStatus.BLOCKED, ("SETUP_MARKET_CLOSED_EXPIRE",)
        if session_phase == "OPENING_DISCOVERY" and feature.completed_1m_count < self.config.min_completed_1m_candles:
            return SetupContextStatus.DATA_WAIT, tuple(_dedupe([*feature.data_wait_reasons, "OPENING_DISCOVERY_COMPLETED_CANDLE_WAIT"]))
        if feature.data_wait_reasons:
            return SetupContextStatus.DATA_WAIT, tuple(feature.data_wait_reasons)
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
                    ]
                )
            )
            return SetupContextStatus.BLOCKED, tuple(_dedupe(reasons))
        if session_phase == "MIDDAY_CHOP":
            return SetupContextStatus.WAIT, ("SETUP_MIDDAY_CHOP_WAIT",)
        if (
            theme_state in {"EMERGING_THEME", "WATCH_THEME", "DATA_WAIT"}
            or leadership in {"CHALLENGER", "TAKEOVER_PENDING"}
            or not leadership
            or feature.leadership_wait_new_entry
            or market_action in {"DATA_WAIT", "WAIT", "WAIT_MARKET", "MIDDAY_CHOP", "CHOPPY"}
            or side in {"CHOPPY", "DATA_WAIT"}
        ):
            reasons.extend(
                _flag_reasons(
                    [
                        (theme_state in {"EMERGING_THEME", "WATCH_THEME", "DATA_WAIT"}, f"THEME_{theme_state}_WAIT" if theme_state else "THEME_WAIT"),
                        (leadership in {"CHALLENGER", "TAKEOVER_PENDING"}, f"LEADERSHIP_{leadership}_WAIT" if leadership else "LEADERSHIP_WAIT"),
                        (not leadership, "LEADERSHIP_STATUS_MISSING_WAIT"),
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
        allowed_market = market_action in {"ALLOW_NORMAL", "ALLOW_REDUCED"}
        allowed_leadership = leadership in {"INCUMBENT", "TAKEOVER_CONFIRMED"}
        if session_phase == "AFTERNOON_ROTATION":
            if allowed_theme and theme_state in {"LEADING_THEME", "SPREADING_THEME"} and allowed_role and allowed_market and allowed_leadership and feature.post_subscription_tick_verified:
                return SetupContextStatus.ELIGIBLE, ()
            return SetupContextStatus.WAIT, tuple(
                _dedupe(
                    _flag_reasons(
                        [
                            (not (theme_state in {"LEADING_THEME", "SPREADING_THEME"}), "AFTERNOON_ROTATION_THEME_NOT_LEADING_WAIT"),
                            (not allowed_role, "AFTERNOON_ROTATION_LEADER_NOT_CONFIRMED_WAIT"),
                            (not allowed_leadership, "AFTERNOON_ROTATION_LEADERSHIP_NOT_CONFIRMED_WAIT"),
                            (not feature.post_subscription_tick_verified, "SETUP_POST_SUBSCRIPTION_FRESH_TICK_MISSING"),
                        ]
                    )
                )
            )
        if allowed_theme and allowed_role and allowed_market and allowed_leadership:
            return SetupContextStatus.ELIGIBLE, ()
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
        setup_type = SetupType.LEADER_FIRST_PULLBACK
        previous = _previous_state(feature, setup_type)
        peak = _confirmed_local_peak(feature, self.config)
        previous_payload = dict(previous.get("state_payload") or previous.get("price_structure") or {})
        generation = _generation(previous)
        if _terminal(previous) and peak > 0:
            old_peak = _float(previous_payload.get("local_peak"))
            if old_peak <= 0 or peak >= old_peak * (1 + self.config.leader_new_peak_generation_min_pct / 100.0):
                generation += 1
                previous_payload = {}
        setup_instance_id = _setup_instance_id(feature, setup_type, generation)
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return _hypothesis(feature, setup_type, SetupShapeStatus.DATA_WAIT, SetupLifecycleState.SEEKING, generation, setup_instance_id, ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        if peak <= 0:
            structure = {"phase": "SEEK_PEAK", "local_peak": 0, "generation_reason": "LOCAL_PEAK_NOT_CONFIRMED"}
            return _hypothesis(feature, setup_type, SetupShapeStatus.NOT_SEEN, SetupLifecycleState.SEEKING, generation, setup_instance_id, ("LOCAL_PEAK_NOT_CONFIRMED",), structure, 0.0)
        pullback = feature.pullback_from_high_pct or _pullback_pct(feature.current_price, peak)
        below_vwap_pct = _below_vwap_pct(feature.current_price, feature.vwap)
        support = _post_peak_support(feature, peak)
        structural_low_broken = support > 0 and feature.current_price < support * (1 - self.config.retest_hold_pct / 100.0)
        reclaim_ok = feature.vwap <= 0 or feature.current_price >= feature.vwap * (1 - self.config.leader_max_below_vwap_pct / 100.0)
        momentum_ok = feature.momentum_1m_pct > 0 or _last_completed_bullish(feature)
        first_consumed = bool(previous_payload.get("first_pullback_consumed")) and generation == _generation(previous)
        structure = {
            "phase": "PULLBACK_SCAN",
            "local_peak": peak,
            "pullback_from_high_pct": round(pullback, 4),
            "support": support,
            "vwap": feature.vwap,
            "below_vwap_pct": round(below_vwap_pct, 4),
            "momentum_1m_pct": feature.momentum_1m_pct,
            "support_reclaim": bool(reclaim_ok),
            "structural_low_broken": bool(structural_low_broken),
            "first_pullback_consumed": first_consumed,
            "generation_reason": "NEW_LOCAL_PEAK" if generation > _generation(previous) else "ACTIVE_GENERATION",
        }
        if pullback > self.config.leader_deep_invalidate_pct or structural_low_broken:
            return _hypothesis(feature, setup_type, SetupShapeStatus.INVALIDATED, SetupLifecycleState.INVALIDATED, generation, setup_instance_id, ("PULLBACK_TOO_DEEP" if pullback > self.config.leader_deep_invalidate_pct else "STRUCTURAL_LOW_BROKEN",), structure, 0.0)
        matched = (
            not first_consumed
            and feature.realtime_tick_fresh
            and self.config.leader_pullback_min_pct <= pullback <= self.config.leader_pullback_max_pct
            and reclaim_ok
            and momentum_ok
        )
        if matched:
            structure["phase"] = "MATCHED"
            structure["first_pullback_consumed"] = True
            return _hypothesis(feature, setup_type, SetupShapeStatus.MATCHED, SetupLifecycleState.MATCHED, generation, setup_instance_id, ("LEADER_FIRST_PULLBACK_MATCHED",), structure, 78.0)
        reasons = ["LEADER_FIRST_PULLBACK_FORMING"]
        if first_consumed:
            reasons.append("LFP_FIRST_PULLBACK_ALREADY_CONSUMED")
        if not feature.realtime_tick_fresh:
            reasons.append("REALTIME_TICK_NOT_FRESH")
        return _hypothesis(feature, setup_type, SetupShapeStatus.FORMING, SetupLifecycleState.FORMING, generation, setup_instance_id, tuple(reasons), structure, 45.0)

    def _classify_vwap_reclaim(self, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        setup_type = SetupType.VWAP_RECLAIM
        previous = _previous_state(feature, setup_type)
        previous_payload = dict(previous.get("state_payload") or previous.get("price_structure") or {})
        generation = _generation(previous)
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return _hypothesis(feature, setup_type, SetupShapeStatus.DATA_WAIT, SetupLifecycleState.SEEKING, generation, _setup_instance_id(feature, setup_type, generation), ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        if feature.vwap <= 0 or feature.current_price <= 0:
            return _hypothesis(feature, setup_type, SetupShapeStatus.DATA_WAIT, SetupLifecycleState.SEEKING, generation, _setup_instance_id(feature, setup_type, generation), ("VWAP_OR_PRICE_MISSING",), {}, 0.0)
        below = _latest_below_vwap_candle(feature, self.config)
        if _terminal(previous) and below:
            old_below_at = str(previous_payload.get("below_candle_at") or "")
            if not old_below_at or str(below.get("candle_at") or "") > old_below_at:
                generation += 1
                previous_payload = {}
        setup_instance_id = _setup_instance_id(feature, setup_type, generation)
        if not below and previous_payload.get("below_candle_at") and not _terminal(previous):
            below = dict(previous_payload)
        above_pct = _pct(feature.current_price - feature.vwap, feature.vwap)
        structure = {
            "phase": "SEEK_BELOW",
            "vwap": feature.vwap,
            "below_candle_at": str((below or {}).get("candle_at") or ""),
            "below_close_vs_vwap_pct": _float((below or {}).get("close_vs_vwap_pct")),
            "current_above_vwap_pct": round(above_pct, 4),
            "lookback": min(feature.completed_1m_count, self.config.vwap_lookback),
        }
        if above_pct < -self.config.vwap_invalidate_below_pct and below:
            structure["phase"] = "INVALIDATED"
            return _hypothesis(feature, setup_type, SetupShapeStatus.INVALIDATED, SetupLifecycleState.INVALIDATED, generation, setup_instance_id, ("VWAP_RECLAIM_LOST",), structure, 0.0)
        if not below:
            return _hypothesis(feature, setup_type, SetupShapeStatus.NOT_SEEN, SetupLifecycleState.SEEKING, generation, setup_instance_id, ("VWAP_PRIOR_BELOW_NOT_SEEN",), structure, 0.0)
        structure["phase"] = "BELOW_CONFIRMED"
        reclaim_by_completed = _completed_reclaim_after(feature, str(below.get("candle_at") or ""), self.config)
        current_reclaim = _current_reclaim_after(feature, str(below.get("candle_at") or ""), above_pct, self.config)
        if reclaim_by_completed or current_reclaim:
            structure["phase"] = "MATCHED"
            structure["reclaim_source"] = "completed_candle" if reclaim_by_completed else "current_realtime_tick"
            return _hypothesis(feature, setup_type, SetupShapeStatus.MATCHED, SetupLifecycleState.MATCHED, generation, setup_instance_id, ("VWAP_RECLAIM_MATCHED",), structure, 82.0)
        return _hypothesis(feature, setup_type, SetupShapeStatus.FORMING, SetupLifecycleState.FORMING, generation, setup_instance_id, ("VWAP_BELOW_CONFIRMED_RECLAIM_PENDING",), structure, 48.0)

    def _classify_breakout_retest(self, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        setup_type = SetupType.BREAKOUT_RETEST
        previous = _previous_state(feature, setup_type)
        previous_payload = dict(previous.get("state_payload") or previous.get("price_structure") or {})
        generation = _generation(previous)
        if feature.completed_1m_count < self.config.min_completed_1m_candles:
            return _hypothesis(feature, setup_type, SetupShapeStatus.DATA_WAIT, SetupLifecycleState.SEEKING, generation, _setup_instance_id(feature, setup_type, generation), ("COMPLETED_1M_CANDLES_INSUFFICIENT",), {}, 0.0)
        reference, reference_source = _breakout_reference(feature, previous)
        if reference <= 0:
            return _hypothesis(feature, setup_type, SetupShapeStatus.NOT_SEEN, SetupLifecycleState.SEEKING, generation, _setup_instance_id(feature, setup_type, generation), ("BREAKOUT_LEVEL_MISSING",), {}, 0.0)
        breakout = _breakout_close(feature, reference, self.config)
        if _terminal(previous) and breakout:
            old_breakout_at = str(previous_payload.get("breakout_candle_at") or "")
            if not old_breakout_at or str(breakout.get("candle_at") or "") > old_breakout_at:
                generation += 1
                previous_payload = {}
        setup_instance_id = _setup_instance_id(feature, setup_type, generation)
        if not breakout and previous_payload.get("breakout_candle_at") and not _terminal(previous):
            breakout = dict(previous_payload)
            reference = _float(previous_payload.get("breakout_level"), reference)
            reference_source = str(previous_payload.get("reference_source") or "state_fixed")
        low_bound = reference * (1 - self.config.retest_lower_tol_pct / 100.0)
        high_bound = reference * (1 + self.config.retest_upper_tol_pct / 100.0)
        hold_bound = reference * (1 - self.config.retest_hold_pct / 100.0)
        invalidate_bound = reference * (1 - self.config.retest_invalidate_below_pct / 100.0)
        structure = {
            "phase": "SEEK_BREAKOUT",
            "breakout_level": reference,
            "reference_source": reference_source,
            "breakout_candle_at": str((breakout or {}).get("candle_at") or ""),
            "breakout_close": _float((breakout or {}).get("close")),
            "retest_band_low": round(low_bound, 4),
            "retest_band_high": round(high_bound, 4),
            "current_price": feature.current_price,
        }
        if feature.current_price > 0 and feature.current_price < invalidate_bound:
            structure["phase"] = "INVALIDATED"
            return _hypothesis(feature, setup_type, SetupShapeStatus.INVALIDATED, SetupLifecycleState.INVALIDATED, generation, setup_instance_id, ("BREAKOUT_RETEST_INVALIDATED",), structure, 0.0)
        if not breakout:
            return _hypothesis(feature, setup_type, SetupShapeStatus.NOT_SEEN, SetupLifecycleState.SEEKING, generation, setup_instance_id, ("BREAKOUT_NOT_SEEN",), structure, 0.0)
        bars_after = _bars_after(feature, str(breakout.get("candle_at") or ""))
        structure["phase"] = "BREAKOUT_CONFIRMED"
        structure["bars_after_breakout"] = bars_after
        retest_completed = _completed_retest_after(feature, str(breakout.get("candle_at") or ""), low_bound, high_bound, hold_bound)
        retest_current = bars_after >= self.config.breakout_min_bars_between and low_bound <= feature.current_price <= high_bound and feature.current_price >= hold_bound and feature.realtime_tick_fresh
        if retest_completed or retest_current:
            structure["phase"] = "MATCHED"
            structure["retest_source"] = "completed_candle" if retest_completed else "current_realtime_tick"
            return _hypothesis(feature, setup_type, SetupShapeStatus.MATCHED, SetupLifecycleState.MATCHED, generation, setup_instance_id, ("BREAKOUT_RETEST_MATCHED",), structure, 80.0)
        return _hypothesis(feature, setup_type, SetupShapeStatus.FORMING, SetupLifecycleState.FORMING, generation, setup_instance_id, ("BREAKOUT_CONFIRMED_RETEST_PENDING",), structure, 46.0)

    def _apply_session_shape_guard(self, hypothesis: SetupHypothesis, feature: SetupFeatureSnapshot) -> SetupHypothesis:
        session_phase = feature.session_phase.upper()
        if session_phase == "MARKET_CLOSED" and hypothesis.shape_status in {SetupShapeStatus.FORMING, SetupShapeStatus.MATCHED, SetupShapeStatus.DATA_WAIT}:
            return _replace_hypothesis(
                hypothesis,
                shape_status=SetupShapeStatus.EXPIRED,
                lifecycle_state=SetupLifecycleState.EXPIRED,
                reason_codes=tuple(_dedupe([*hypothesis.reason_codes, "SETUP_MARKET_CLOSED_EXPIRE"])),
                quality_score=0.0,
            )
        if session_phase == "OPENING_DISCOVERY" and hypothesis.shape_status == SetupShapeStatus.MATCHED and feature.completed_1m_count < self.config.min_completed_1m_candles:
            return _replace_hypothesis(
                hypothesis,
                shape_status=SetupShapeStatus.DATA_WAIT,
                lifecycle_state=SetupLifecycleState.SEEKING,
                reason_codes=tuple(_dedupe([*hypothesis.reason_codes, "OPENING_DISCOVERY_COMPLETED_CANDLE_WAIT"])),
                quality_score=0.0,
            )
        return hypothesis

    def _router_status(
        self,
        shape_status: SetupShapeStatus,
        context_status: SetupContextStatus,
        entry_alignment: EntryAlignmentStatus,
    ) -> SetupRouterStatus:
        if shape_status == SetupShapeStatus.EXPIRED:
            return SetupRouterStatus.EXPIRED
        if shape_status == SetupShapeStatus.INVALIDATED:
            return SetupRouterStatus.INVALIDATED
        if context_status == SetupContextStatus.BLOCKED:
            return SetupRouterStatus.CONTEXT_BLOCKED
        if shape_status == SetupShapeStatus.DATA_WAIT or context_status == SetupContextStatus.DATA_WAIT:
            return SetupRouterStatus.DATA_WAIT
        if entry_alignment in {EntryAlignmentStatus.ENTRY_DECISION_MISSING, EntryAlignmentStatus.ENTRY_DECISION_STALE, EntryAlignmentStatus.ENTRY_DATA_WAIT}:
            return SetupRouterStatus.DATA_WAIT
        if entry_alignment == EntryAlignmentStatus.ENTRY_HARD_BLOCK:
            return SetupRouterStatus.CONTEXT_BLOCKED
        if shape_status == SetupShapeStatus.MATCHED and context_status == SetupContextStatus.ELIGIBLE:
            return SetupRouterStatus.VALID_OBSERVE
        if shape_status == SetupShapeStatus.MATCHED and context_status == SetupContextStatus.WAIT:
            return SetupRouterStatus.PENDING
        if shape_status == SetupShapeStatus.FORMING and context_status in {SetupContextStatus.ELIGIBLE, SetupContextStatus.WAIT}:
            return SetupRouterStatus.PENDING
        return SetupRouterStatus.UNKNOWN

    def _primary_setup_type(self, hypotheses: list[SetupHypothesis], feature: SetupFeatureSnapshot) -> SetupType:
        active = [item for item in hypotheses if item.shape_status in {SetupShapeStatus.MATCHED, SetupShapeStatus.FORMING}]
        if not active:
            return SetupType.UNKNOWN
        previous = str(dict(feature.previous_observation or {}).get("setup_type") or "")
        previous_generation = _safe_int(dict(feature.previous_observation or {}).get("setup_generation"), 0)
        for item in active:
            if previous == item.setup_type.value and previous_generation in {0, item.setup_generation}:
                return item.setup_type
        priority = [SetupType.VWAP_RECLAIM, SetupType.BREAKOUT_RETEST, SetupType.LEADER_FIRST_PULLBACK]
        for setup_type in priority:
            if any(item.setup_type == setup_type and item.shape_status == SetupShapeStatus.MATCHED for item in active):
                return setup_type
        for setup_type in priority:
            if any(item.setup_type == setup_type and item.shape_status == SetupShapeStatus.FORMING for item in active):
                return setup_type
        return SetupType.UNKNOWN


def _entry_alignment_status(feature: SetupFeatureSnapshot) -> EntryAlignmentStatus:
    if not feature.entry_decision:
        return EntryAlignmentStatus.ENTRY_DECISION_MISSING
    if not feature.entry_decision_fresh:
        return EntryAlignmentStatus.ENTRY_DECISION_STALE
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


def _entry_reason_codes(status: EntryAlignmentStatus) -> tuple[str, ...]:
    if status == EntryAlignmentStatus.ENTRY_DECISION_MISSING:
        return ("ENTRY_DECISION_MISSING",)
    if status == EntryAlignmentStatus.ENTRY_DECISION_STALE:
        return ("ENTRY_DECISION_STALE",)
    return ()


def _previous_state(feature: SetupFeatureSnapshot, setup_type: SetupType) -> dict[str, Any]:
    states = dict(feature.setup_states or {})
    for key in (setup_type.value, setup_type.name):
        item = states.get(key)
        if isinstance(item, Mapping):
            payload = dict(item)
            if payload.get("theme_id") and feature.theme_id and str(payload.get("theme_id")) != feature.theme_id:
                return {}
            return payload
    previous = dict(feature.previous_observation or {})
    if str(previous.get("setup_type") or "") == setup_type.value:
        if previous.get("theme_id") and feature.theme_id and str(previous.get("theme_id")) != feature.theme_id:
            return {}
        return {
            **previous,
            "lifecycle_state": previous.get("lifecycle_state") or _lifecycle_from_shape(str(previous.get("shape_status") or "")),
            "setup_generation": previous.get("setup_generation") or 1,
            "state_payload": previous.get("state_payload") or previous.get("price_structure") or {},
        }
    return {}


def _generation(previous: Mapping[str, Any]) -> int:
    return max(1, _safe_int(previous.get("setup_generation"), 1)) if previous else 1


def _terminal(previous: Mapping[str, Any]) -> bool:
    state = str(previous.get("lifecycle_state") or "").upper()
    shape = str(previous.get("shape_status") or "").upper()
    return state in {"MATCHED", "INVALIDATED", "EXPIRED"} or shape in {"MATCHED", "INVALIDATED", "EXPIRED"}


def _lifecycle_from_shape(shape_status: str) -> str:
    shape = shape_status.upper()
    if shape == "MATCHED":
        return SetupLifecycleState.MATCHED.value
    if shape == "INVALIDATED":
        return SetupLifecycleState.INVALIDATED.value
    if shape == "EXPIRED":
        return SetupLifecycleState.EXPIRED.value
    if shape == "FORMING":
        return SetupLifecycleState.FORMING.value
    return SetupLifecycleState.SEEKING.value


def _confirmed_local_peak(feature: SetupFeatureSnapshot, config: SetupRouterConfig) -> float:
    candles = _completed(feature)
    if len(candles) < 2:
        return 0.0
    now = _parse_time(feature.calculated_at)
    candidates: list[float] = []
    for index, candle in enumerate(candles[:-1]):
        high = _float(candle.get("high"))
        if high <= 0:
            continue
        candle_at = _parse_time(candle.get("candle_at") or candle.get("start_at"))
        if now and candle_at and (now - candle_at).total_seconds() < config.leader_local_peak_min_age_sec:
            continue
        later = candles[index + 1 :]
        if any(_float(item.get("high")) < high and _float(item.get("close")) < high for item in later):
            candidates.append(high)
    return max(candidates) if candidates else 0.0


def _post_peak_support(feature: SetupFeatureSnapshot, peak: float) -> float:
    if peak <= 0:
        return 0.0
    lows = [_float(candle.get("low")) for candle in _completed(feature) if 0 < _float(candle.get("low")) < peak]
    return min(lows[-3:]) if lows else 0.0


def _last_completed_bullish(feature: SetupFeatureSnapshot) -> bool:
    candles = _completed(feature)
    if not candles:
        return False
    last = candles[-1]
    return _float(last.get("close")) > _float(last.get("open"))


def _latest_below_vwap_candle(feature: SetupFeatureSnapshot, config: SetupRouterConfig) -> dict[str, Any]:
    window = _completed(feature)[-config.vwap_lookback :]
    below: dict[str, Any] = {}
    for candle in window:
        vwap = _float(candle.get("derived_vwap_at_close"))
        close = _float(candle.get("close"))
        close_vs = _float(candle.get("close_vs_vwap_pct"), _pct(close - vwap, vwap))
        if vwap > 0 and close > 0 and close_vs <= -abs(config.vwap_prior_below_min_pct):
            below = {**candle, "close_vs_vwap_pct": close_vs}
    return below


def _completed_reclaim_after(feature: SetupFeatureSnapshot, below_at: str, config: SetupRouterConfig) -> bool:
    if not below_at:
        return False
    for candle in _completed(feature):
        candle_at = str(candle.get("candle_at") or "")
        if candle_at <= below_at:
            continue
        vwap = _float(candle.get("derived_vwap_at_close"))
        close = _float(candle.get("close"))
        close_vs = _float(candle.get("close_vs_vwap_pct"), _pct(close - vwap, vwap))
        if vwap > 0 and close > 0 and config.vwap_reclaim_above_min_pct <= close_vs <= config.vwap_max_extension_pct:
            return True
    return False


def _current_reclaim_after(feature: SetupFeatureSnapshot, below_at: str, above_pct: float, config: SetupRouterConfig) -> bool:
    if not below_at or not feature.realtime_tick_fresh:
        return False
    tick_at = str(feature.tick_at or "")
    if tick_at <= below_at:
        return False
    return config.vwap_reclaim_above_min_pct <= above_pct <= config.vwap_max_extension_pct


def _breakout_reference(feature: SetupFeatureSnapshot, previous: Mapping[str, Any]) -> tuple[float, str]:
    previous_payload = dict(previous.get("state_payload") or previous.get("price_structure") or {})
    if previous and not _terminal(previous):
        fixed = _float(previous_payload.get("breakout_level"))
        if fixed > 0:
            return fixed, "state_fixed"
    details = dict(feature.entry_decision.get("details") or {})
    candidates = (
        ("entry_decision.breakout_level", feature.entry_decision.get("breakout_level")),
        ("entry_decision.details.breakout_level", details.get("breakout_level")),
        ("entry_decision.details.reference_high", details.get("reference_high")),
        ("strategy_context.breakout_level", feature.strategy_context.get("breakout_level")),
        ("strategy_context.stock.breakout_level", dict(feature.strategy_context.get("stock") or {}).get("breakout_level")),
    )
    for source, value in candidates:
        number = _float(value)
        if number > 0:
            return number, source
    previous_candles = _completed(feature)[:-1]
    highs = [_float(candle.get("high")) for candle in previous_candles if _float(candle.get("high")) > 0]
    return (max(highs), "fallback_prior_completed_high") if highs else (0.0, "")


def _breakout_close(feature: SetupFeatureSnapshot, reference: float, config: SetupRouterConfig) -> dict[str, Any]:
    window = _completed(feature)[-config.breakout_lookback :]
    for candle in window:
        close = _float(candle.get("close"))
        if close >= reference * (1 + config.breakout_buffer_pct / 100.0):
            return dict(candle)
    return {}


def _bars_after(feature: SetupFeatureSnapshot, candle_at: str) -> int:
    if not candle_at:
        return 0
    return sum(1 for candle in _completed(feature) if str(candle.get("candle_at") or "") > candle_at)


def _completed_retest_after(feature: SetupFeatureSnapshot, breakout_at: str, low_bound: float, high_bound: float, hold_bound: float) -> bool:
    if not breakout_at:
        return False
    for candle in _completed(feature):
        candle_at = str(candle.get("candle_at") or "")
        if candle_at <= breakout_at:
            continue
        low = _float(candle.get("low"))
        close = _float(candle.get("close"))
        if low_bound <= low <= high_bound and close >= hold_bound:
            return True
    return False


def _completed(feature: SetupFeatureSnapshot) -> list[dict[str, Any]]:
    return [dict(item) for item in list(feature.completed_1m_candles or []) if bool(item.get("completed", True))]


def _hypothesis(
    feature: SetupFeatureSnapshot,
    setup_type: SetupType,
    shape_status: SetupShapeStatus,
    lifecycle_state: SetupLifecycleState,
    setup_generation: int,
    setup_instance_id: str,
    reason_codes: tuple[str, ...],
    structure: Mapping[str, Any],
    quality_score: float,
) -> SetupHypothesis:
    payload = dict(structure or {})
    payload.setdefault("setup_type", setup_type.value)
    payload.setdefault("lifecycle_state", lifecycle_state.value)
    payload.setdefault("setup_generation", setup_generation)
    return SetupHypothesis(
        setup_type=setup_type,
        shape_status=shape_status,
        lifecycle_state=lifecycle_state,
        setup_generation=max(1, int(setup_generation or 1)),
        setup_instance_id=setup_instance_id or _setup_instance_id(feature, setup_type, setup_generation),
        reason_codes=tuple(reason_codes or ()),
        price_structure=payload,
        state_payload=payload,
        quality_score=quality_score,
        last_material_change_at=feature.calculated_at,
    )


def _replace_hypothesis(hypothesis: SetupHypothesis, **updates: Any) -> SetupHypothesis:
    values = asdict(hypothesis)
    values.update(updates)
    if isinstance(values.get("setup_type"), str):
        values["setup_type"] = SetupType(values["setup_type"])
    if isinstance(values.get("shape_status"), str):
        values["shape_status"] = SetupShapeStatus(values["shape_status"])
    if isinstance(values.get("lifecycle_state"), str):
        values["lifecycle_state"] = SetupLifecycleState(values["lifecycle_state"])
    return SetupHypothesis(**values)


def _setup_instance_id(feature: SetupFeatureSnapshot, setup_type: SetupType, generation: int) -> str:
    material = "|".join(
        [
            feature.trade_date,
            feature.candidate_instance_id,
            feature.theme_id,
            setup_type.value,
            str(max(1, int(generation or 1))),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]


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
    entry_alignment: EntryAlignmentStatus,
) -> str:
    material = "|".join(
        [
            SETUP_ROUTER_SCHEMA_VERSION,
            SETUP_ROUTER_VERSION,
            feature.trade_date,
            feature.candidate_instance_id,
            feature.theme_id,
            hypothesis.setup_type.value,
            str(hypothesis.setup_generation),
            hypothesis.lifecycle_state.value,
            hypothesis.shape_status.value,
            context_status.value,
            router_status.value,
            entry_alignment.value,
            feature.context_id,
            feature.latest_completed_candle_at,
            feature.entry_decision_at,
            str(feature.entry_decision_id or ""),
            str(feature.post_subscription_tick_verified),
            _stable_json(hypothesis.state_payload),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def _safety_flags() -> dict[str, Any]:
    return {
        "observe_only": True,
        "ready_allowed": False,
        "candidate_promotion_allowed": False,
        "opportunity_rank_allowed": False,
        "order_intent_allowed": False,
        "live_order_allowed": False,
        "recommended_position_size_multiplier": 0,
        "quantity": 0,
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


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator * 100.0


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(str(value).strip().replace(",", "")))
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
