from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.models import Candidate, CandidateState
from trading.strategy.reason_codes import ReasonCode, normalize_reason_codes


ACTION_TYPE = "MARKET_RELATIVE_STRENGTH_SHADOW"
STRATEGY_NAME = "reboot_v2_market_relative_strength_shadow"
STRATEGY_VERSION = "v1"


class MarketRelativeStrengthShadowScenario(str, Enum):
    HEALTHY_SIDE_REDUCED = "HEALTHY_SIDE_REDUCED"
    COUNTERPART_DATA_DEGRADED_REDUCED = "COUNTERPART_DATA_DEGRADED_REDUCED"
    WEAK_SIDE_STRICT_SHADOW = "WEAK_SIDE_STRICT_SHADOW"
    RISK_OFF_SIDE_DIAGNOSTIC = "RISK_OFF_SIDE_DIAGNOSTIC"
    SYSTEMIC_RISK_EXCLUDED = "SYSTEMIC_RISK_EXCLUDED"
    DATA_WAIT_EXCLUDED = "DATA_WAIT_EXCLUDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MarketRelativeStrengthShadowVariant(str, Enum):
    STRICT = "STRICT"
    BALANCED = "BALANCED"
    NONE = "NONE"


class MarketRelativeStrengthShadowStatus(str, Enum):
    SHADOW_CANDIDATE = "SHADOW_CANDIDATE"
    SHADOW_REJECT = "SHADOW_REJECT"
    DATA_WAIT = "DATA_WAIT"
    SYSTEMIC_EXCLUDED = "SYSTEMIC_EXCLUDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MarketRelativeStrengthCounterfactualAction(str, Enum):
    OBSERVE_SMALL = "OBSERVE_SMALL"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    EXCLUDED = "EXCLUDED"


@dataclass(frozen=True)
class MarketRelativeStrengthShadowConfig:
    enabled: bool = False
    interval_sec: int = 30
    dedupe_sec: int = 60
    max_per_cycle: int = 50
    weak_side_min_relative_strength_pct: float = 4.0
    risk_off_side_min_relative_strength_pct: float = 5.0
    min_theme_persistence_count: int = 2
    balanced_threshold_discount_pct: float = 1.0
    healthy_side_counterfactual_multiplier: float = 0.6
    weak_side_counterfactual_multiplier: float = 0.1
    risk_off_counterfactual_multiplier: float = 0.0

    @classmethod
    def from_env(cls) -> "MarketRelativeStrengthShadowConfig":
        return cls(
            enabled=_env_bool("TRADING_MARKET_RS_SHADOW_ENABLED", False),
            interval_sec=_env_int("TRADING_MARKET_RS_SHADOW_INTERVAL_SEC", 30),
            dedupe_sec=_env_int("TRADING_MARKET_RS_SHADOW_DEDUPE_SEC", 60),
            max_per_cycle=_env_int("TRADING_MARKET_RS_SHADOW_MAX_PER_CYCLE", 50),
            weak_side_min_relative_strength_pct=_env_float("TRADING_MARKET_RS_WEAK_MIN_RELATIVE_STRENGTH_PCT", 4.0),
            risk_off_side_min_relative_strength_pct=_env_float("TRADING_MARKET_RS_RISK_OFF_MIN_RELATIVE_STRENGTH_PCT", 5.0),
            min_theme_persistence_count=_env_int("TRADING_MARKET_RS_MIN_THEME_PERSISTENCE_COUNT", 2),
        )


@dataclass(frozen=True)
class MarketRelativeStrengthShadowDecision:
    shadow_decision_id: str
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    candidate_instance_id: str
    context_id: str
    code: str
    name: str = ""
    market_side: str = "UNKNOWN"
    side_market_regime: str = "DATA_WAIT"
    counterpart_market_regime: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    actual_market_action: str = "DATA_WAIT"
    actual_position_size_multiplier_hint: float = 0.0
    actual_entry_status: str = ""
    actual_ready_allowed: bool = False
    actual_dry_run_intent_allowed: bool = False
    shadow_scenario: str = MarketRelativeStrengthShadowScenario.NOT_APPLICABLE.value
    shadow_variant: str = MarketRelativeStrengthShadowVariant.NONE.value
    shadow_status: str = MarketRelativeStrengthShadowStatus.NOT_APPLICABLE.value
    counterfactual_action: str = MarketRelativeStrengthCounterfactualAction.EXCLUDED.value
    counterfactual_position_size_multiplier_hint: float = 0.0
    shadow_filter_passed: bool = False
    review_candidate: bool = False
    promotion_eligible: bool = False
    trade_stock_role: str = ""
    theme_id: str = ""
    theme_name: str = ""
    theme_state: str = ""
    theme_score: float = 0.0
    persistence_count: int = 0
    relative_strength_vs_index_pct: float = 0.0
    relative_strength_band: str = "LT_2"
    change_rate_pct: float = 0.0
    index_return_pct: float = 0.0
    price_location: str = ""
    current_price: float = 0.0
    vwap: float = 0.0
    price_vs_vwap_pct: float = 0.0
    pullback_from_high_pct: float = 0.0
    turnover_krw: float = 0.0
    turnover_speed: float = 0.0
    execution_strength: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    context_fresh: bool = False
    realtime_tick_age_sec: float = 0.0
    data_quality_status: str = "DATA_WAIT"
    vi_active: bool = False
    upper_limit_near: bool = False
    overheated: bool = False
    chase_risk: bool = False
    reason_codes: tuple[str, ...] = ()
    reject_reason_codes: tuple[str, ...] = ()
    feature_snapshot: dict[str, Any] = field(default_factory=dict)
    material_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def to_event(self) -> dict[str, Any]:
        details = self.to_dict()
        return {
            "decision_id": self.shadow_decision_id,
            "runtime_cycle_id": "",
            "trade_date": self.trade_date,
            "created_at": self.calculated_at,
            "decision_at": self.calculated_at,
            "candidate_id": self.candidate_id,
            "candidate_instance_id": self.candidate_instance_id,
            "code": self.code,
            "name": self.name,
            "theme_name": self.theme_name,
            "strategy_name": STRATEGY_NAME,
            "strategy_version": STRATEGY_VERSION,
            "config_hash": "",
            "gate_status": "OBSERVE",
            "gate_reason": self.shadow_scenario,
            "reason_status": self.shadow_status,
            "reason_family": ACTION_TYPE,
            "reason_codes": list(self.reason_codes),
            "block_type": "OBSERVE_ONLY",
            "action_type": ACTION_TYPE,
            "action_result": self.shadow_status,
            "price": self.current_price or None,
            "change_rate": self.change_rate_pct,
            "trade_value": self.turnover_krw,
            "execution_strength": self.execution_strength,
            "vwap": self.vwap,
            "momentum_1m": self.momentum_1m,
            "momentum_3m": self.momentum_3m,
            "momentum_5m": self.momentum_5m,
            "gate_score": self.relative_strength_vs_index_pct,
            "hybrid_score": self.theme_score,
            "theme_score": self.theme_score,
            "data_status": self.data_quality_status,
            "data_quality_issues": list(self.reject_reason_codes),
            "details": details,
        }


class MarketRelativeStrengthShadowEvaluator:
    def __init__(self, *, config: MarketRelativeStrengthShadowConfig | None = None) -> None:
        self.config = config or MarketRelativeStrengthShadowConfig()

    def evaluate_candidate(self, candidate: Candidate, *, now: datetime | None = None) -> MarketRelativeStrengthShadowDecision:
        current = (now or datetime.now()).replace(microsecond=0)
        metadata = dict(candidate.metadata or {})
        context = dict(metadata.get("strategy_context_v3") or {})
        entry = dict(metadata.get("entry_decision") or {})
        market = dict(context.get("market") or {})
        theme = dict(context.get("theme") or {})
        stock = dict(context.get("stock") or {})
        data = dict(context.get("data") or {})
        risk = dict(context.get("risk") or {})

        scenario = self._scenario(market, data)
        reject_reasons = self._reject_reasons(context, market, theme, stock, data, risk, entry, scenario)
        variant = self._variant(market, theme, stock, reject_reasons, scenario)
        shadow_status = self._shadow_status(scenario, variant, reject_reasons)
        counterfactual_action, counterfactual_multiplier = self._counterfactual(scenario, shadow_status)
        reason_codes = self._reason_codes(scenario, shadow_status, reject_reasons)
        feature_snapshot = self._feature_snapshot(context, entry)
        material_key = _material_key(
            {
                "scenario": scenario.value,
                "variant": variant.value,
                "status": shadow_status.value,
                "side": _enum_text(market.get("market_side")),
                "side_regime": _enum_text(market.get("side_market_regime")),
                "composite": _enum_text(market.get("composite_market_mode")),
                "action": _enum_text(market.get("market_action")),
                "role": _enum_text(stock.get("trade_stock_role")),
                "theme": _enum_text(theme.get("theme_state")),
                "price_location": _enum_text(entry.get("price_location")),
                "rs_band": _enum_text(stock.get("relative_strength_band")),
            }
        )
        code = normalize_code(candidate.code)
        context_id = str(context.get("context_id") or metadata.get("strategy_context_id") or "")
        shadow_decision_id = _decision_id(
            trade_date=candidate.trade_date,
            code=code,
            candidate_instance_id=str(metadata.get("candidate_instance_id") or candidate.id or code),
            context_id=context_id,
            scenario=scenario.value,
            variant=variant.value,
            material_key=material_key,
        )
        shadow_filter_passed = shadow_status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE
        review_candidate = (
            shadow_filter_passed
            and scenario == MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW
            and variant == MarketRelativeStrengthShadowVariant.STRICT
        )
        return MarketRelativeStrengthShadowDecision(
            shadow_decision_id=shadow_decision_id,
            trade_date=candidate.trade_date or current.date().isoformat(),
            calculated_at=current.isoformat(),
            candidate_id=candidate.id,
            candidate_instance_id=str(metadata.get("candidate_instance_id") or candidate.id or code),
            context_id=context_id,
            code=code,
            name=_first_text(
                candidate.name,
                stock.get("name"),
                metadata.get("stock_name"),
                entry.get("stock_name"),
                entry.get("name"),
            ),
            market_side=_enum_text(market.get("market_side"), default="UNKNOWN"),
            side_market_regime=_enum_text(market.get("side_market_regime"), default="DATA_WAIT"),
            counterpart_market_regime=_enum_text(market.get("counterpart_market_regime"), default="DATA_WAIT"),
            composite_market_mode=_enum_text(market.get("composite_market_mode"), default="DATA_DEGRADED"),
            systemic_risk_off=_bool(market.get("systemic_risk_off")),
            actual_market_action=_enum_text(market.get("market_action"), default="DATA_WAIT"),
            actual_position_size_multiplier_hint=_float(market.get("position_size_multiplier_hint")),
            actual_entry_status=_enum_text(entry.get("entry_status")),
            actual_ready_allowed=_bool(entry.get("ready_allowed")),
            actual_dry_run_intent_allowed=_bool(entry.get("dry_run_intent_allowed")),
            shadow_scenario=scenario.value,
            shadow_variant=variant.value,
            shadow_status=shadow_status.value,
            counterfactual_action=counterfactual_action.value,
            counterfactual_position_size_multiplier_hint=counterfactual_multiplier,
            shadow_filter_passed=shadow_filter_passed,
            review_candidate=review_candidate,
            promotion_eligible=False,
            trade_stock_role=_enum_text(stock.get("trade_stock_role")),
            theme_id=str(theme.get("theme_id") or ""),
            theme_name=str(theme.get("theme_name") or ""),
            theme_state=_enum_text(theme.get("theme_state")),
            theme_score=_float(theme.get("theme_score")),
            persistence_count=_int(theme.get("persistence_count")),
            relative_strength_vs_index_pct=_float(stock.get("relative_strength_vs_index_pct")),
            relative_strength_band=_enum_text(stock.get("relative_strength_band"), default=_relative_strength_band(_float(stock.get("relative_strength_vs_index_pct")))),
            change_rate_pct=_float(stock.get("change_rate_pct")),
            index_return_pct=_float(market.get("index_return_pct")),
            price_location=_enum_text(entry.get("price_location")),
            current_price=_float(entry.get("current_price")),
            vwap=_float(stock.get("vwap") or entry.get("vwap")),
            price_vs_vwap_pct=_float(stock.get("price_vs_vwap_pct")),
            pullback_from_high_pct=_float(stock.get("pullback_from_high_pct")),
            turnover_krw=_float(stock.get("turnover_krw")),
            turnover_speed=_float(stock.get("turnover_speed")),
            execution_strength=_float(stock.get("execution_strength")),
            momentum_1m=_float(stock.get("momentum_1m")),
            momentum_3m=_float(stock.get("momentum_3m")),
            momentum_5m=_float(stock.get("momentum_5m")),
            context_fresh=_bool(context.get("context_fresh")),
            realtime_tick_age_sec=_float(data.get("realtime_tick_age_sec")),
            data_quality_status=str(data.get("data_quality_status") or "DATA_WAIT"),
            vi_active=_bool(stock.get("vi_active")) or _bool(risk.get("vi_block")),
            upper_limit_near=_bool(stock.get("upper_limit_near")),
            overheated=_bool(stock.get("overheated")) or _bool(risk.get("overheat_block")),
            chase_risk=_bool(risk.get("chase_risk")),
            reason_codes=tuple(reason_codes),
            reject_reason_codes=tuple(reject_reasons),
            feature_snapshot=feature_snapshot,
            material_key=material_key,
        )

    def _scenario(self, market: Mapping[str, Any], data: Mapping[str, Any]) -> MarketRelativeStrengthShadowScenario:
        if _bool(market.get("systemic_risk_off")):
            return MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED
        side = _enum_text(market.get("market_side"), default="UNKNOWN").upper()
        action = _enum_text(market.get("market_action")).upper()
        side_regime = _enum_text(market.get("side_market_regime")).upper()
        reasons = set(str(item).upper() for item in _list(market.get("reason_codes")))
        if side not in {"KOSPI", "KOSDAQ"} or action == "DATA_WAIT" or not _bool(data.get("market_context_fresh")):
            return MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED
        if ReasonCode.COUNTERPART_MARKET_DATA_WAIT_REDUCED.value in reasons:
            return MarketRelativeStrengthShadowScenario.COUNTERPART_DATA_DEGRADED_REDUCED
        if ReasonCode.SPLIT_MARKET_HEALTHY_SIDE_REDUCED.value in reasons:
            return MarketRelativeStrengthShadowScenario.HEALTHY_SIDE_REDUCED
        if side_regime == "WEAK" and action == "WAIT_MARKET":
            return MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW
        if side_regime == "RISK_OFF" and action == "BLOCK_NEW_ENTRY":
            return MarketRelativeStrengthShadowScenario.RISK_OFF_SIDE_DIAGNOSTIC
        return MarketRelativeStrengthShadowScenario.NOT_APPLICABLE

    def _reject_reasons(
        self,
        context: Mapping[str, Any],
        market: Mapping[str, Any],
        theme: Mapping[str, Any],
        stock: Mapping[str, Any],
        data: Mapping[str, Any],
        risk: Mapping[str, Any],
        entry: Mapping[str, Any],
        scenario: MarketRelativeStrengthShadowScenario,
    ) -> list[str]:
        if scenario == MarketRelativeStrengthShadowScenario.NOT_APPLICABLE:
            return []
        reasons: list[str] = []
        if scenario == MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED:
            return [ReasonCode.SYSTEMIC_RISK_SHADOW_EXCLUDED.value]
        if not _bool(context.get("context_fresh")) or not _bool(data.get("market_context_fresh")) or not _bool(data.get("theme_context_fresh")):
            reasons.append(ReasonCode.MARKET_RS_CONTEXT_NOT_READY.value)
        if not _bool(data.get("realtime_tick_fresh")):
            reasons.append(ReasonCode.MARKET_RS_STALE_DATA.value)
        if scenario == MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED:
            if _enum_text(market.get("market_side")).upper() not in {"KOSPI", "KOSDAQ"}:
                reasons.append(ReasonCode.MARKET_SIDE_UNRESOLVED.value)
            return _dedupe(reasons + [ReasonCode.MARKET_RS_CONTEXT_NOT_READY.value])
        role = _enum_text(stock.get("trade_stock_role")).upper()
        raw_role = _enum_text(stock.get("raw_stock_role")).upper()
        if role not in {"LEADER_CONFIRMED", "CO_LEADER_CONFIRMED", "LEADER", "CO_LEADER"} and raw_role not in {"LEADER", "CO_LEADER"}:
            reasons.append(ReasonCode.MARKET_RS_ROLE_NOT_ALLOWED.value)
        theme_state = str(theme.get("theme_state") or "").upper()
        if theme_state not in {"LEADING_THEME", "SPREADING_THEME", "LEADER_ONLY_THEME"}:
            reasons.append(ReasonCode.MARKET_RS_THEME_NOT_ALLOWED.value)
        if _int(theme.get("persistence_count")) < self.config.min_theme_persistence_count:
            reasons.append(ReasonCode.MARKET_RS_PERSISTENCE_INSUFFICIENT.value)
        if _enum_text(entry.get("price_location")).upper() not in {"GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM"}:
            reasons.append(ReasonCode.MARKET_RS_PRICE_LOCATION_NOT_ALLOWED.value)
        if _bool(stock.get("vi_active")) or _bool(risk.get("vi_block")):
            reasons.append(ReasonCode.MARKET_RS_VI_BLOCK.value)
        if _bool(stock.get("upper_limit_near")) or _bool(stock.get("overheated")) or _bool(risk.get("overheat_block")):
            reasons.append(ReasonCode.MARKET_RS_OVERHEAT_BLOCK.value)
        if _bool(risk.get("chase_risk")) or _enum_text(entry.get("price_location")).upper() in {"CHASE_HIGH", "VWAP_OVEREXTENDED"}:
            reasons.append(ReasonCode.MARKET_RS_CHASE_BLOCK.value)
        if not self._threshold_passes(market, stock, scenario, balanced=False):
            reasons.append(ReasonCode.MARKET_RS_BELOW_THRESHOLD.value)
        return _dedupe(reasons)

    def _variant(
        self,
        market: Mapping[str, Any],
        theme: Mapping[str, Any],
        stock: Mapping[str, Any],
        reject_reasons: Iterable[str],
        scenario: MarketRelativeStrengthShadowScenario,
    ) -> MarketRelativeStrengthShadowVariant:
        if scenario in {
            MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED,
            MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED,
            MarketRelativeStrengthShadowScenario.NOT_APPLICABLE,
        }:
            return MarketRelativeStrengthShadowVariant.NONE
        blocking = set(reject_reasons) - {ReasonCode.MARKET_RS_BELOW_THRESHOLD.value}
        if not blocking and self._threshold_passes(market, stock, scenario, balanced=False):
            return MarketRelativeStrengthShadowVariant.STRICT
        if not blocking and self._threshold_passes(market, stock, scenario, balanced=True):
            return MarketRelativeStrengthShadowVariant.BALANCED
        return MarketRelativeStrengthShadowVariant.NONE

    def _threshold_passes(
        self,
        market: Mapping[str, Any],
        stock: Mapping[str, Any],
        scenario: MarketRelativeStrengthShadowScenario,
        *,
        balanced: bool,
    ) -> bool:
        if scenario in {
            MarketRelativeStrengthShadowScenario.HEALTHY_SIDE_REDUCED,
            MarketRelativeStrengthShadowScenario.COUNTERPART_DATA_DEGRADED_REDUCED,
            MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW,
        }:
            threshold = self.config.weak_side_min_relative_strength_pct
        elif scenario == MarketRelativeStrengthShadowScenario.RISK_OFF_SIDE_DIAGNOSTIC:
            threshold = self.config.risk_off_side_min_relative_strength_pct
        else:
            return False
        if balanced:
            threshold = max(0.0, threshold - self.config.balanced_threshold_discount_pct)
        return _float(stock.get("relative_strength_vs_index_pct")) >= threshold

    def _shadow_status(
        self,
        scenario: MarketRelativeStrengthShadowScenario,
        variant: MarketRelativeStrengthShadowVariant,
        reject_reasons: Iterable[str],
    ) -> MarketRelativeStrengthShadowStatus:
        if scenario == MarketRelativeStrengthShadowScenario.NOT_APPLICABLE:
            return MarketRelativeStrengthShadowStatus.NOT_APPLICABLE
        if scenario == MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED:
            return MarketRelativeStrengthShadowStatus.SYSTEMIC_EXCLUDED
        if scenario == MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED:
            return MarketRelativeStrengthShadowStatus.DATA_WAIT
        if variant in {MarketRelativeStrengthShadowVariant.STRICT, MarketRelativeStrengthShadowVariant.BALANCED} and not list(reject_reasons):
            return MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE
        if variant == MarketRelativeStrengthShadowVariant.BALANCED and set(reject_reasons) == {ReasonCode.MARKET_RS_BELOW_THRESHOLD.value}:
            return MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE
        return MarketRelativeStrengthShadowStatus.SHADOW_REJECT

    def _counterfactual(
        self,
        scenario: MarketRelativeStrengthShadowScenario,
        status: MarketRelativeStrengthShadowStatus,
    ) -> tuple[MarketRelativeStrengthCounterfactualAction, float]:
        if status != MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE:
            return MarketRelativeStrengthCounterfactualAction.EXCLUDED, 0.0
        if scenario in {
            MarketRelativeStrengthShadowScenario.HEALTHY_SIDE_REDUCED,
            MarketRelativeStrengthShadowScenario.COUNTERPART_DATA_DEGRADED_REDUCED,
        }:
            return MarketRelativeStrengthCounterfactualAction.OBSERVE_ONLY, self.config.healthy_side_counterfactual_multiplier
        if scenario == MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW:
            return MarketRelativeStrengthCounterfactualAction.OBSERVE_SMALL, self.config.weak_side_counterfactual_multiplier
        if scenario == MarketRelativeStrengthShadowScenario.RISK_OFF_SIDE_DIAGNOSTIC:
            return MarketRelativeStrengthCounterfactualAction.OBSERVE_ONLY, self.config.risk_off_counterfactual_multiplier
        return MarketRelativeStrengthCounterfactualAction.EXCLUDED, 0.0

    def _reason_codes(
        self,
        scenario: MarketRelativeStrengthShadowScenario,
        status: MarketRelativeStrengthShadowStatus,
        reject_reasons: Iterable[str],
    ) -> list[str]:
        scenario_reason = {
            MarketRelativeStrengthShadowScenario.HEALTHY_SIDE_REDUCED: ReasonCode.HEALTHY_SIDE_REDUCED_OBSERVE.value,
            MarketRelativeStrengthShadowScenario.COUNTERPART_DATA_DEGRADED_REDUCED: ReasonCode.COUNTERPART_DATA_DEGRADED_OBSERVE.value,
            MarketRelativeStrengthShadowScenario.WEAK_SIDE_STRICT_SHADOW: ReasonCode.WEAK_SIDE_RELATIVE_STRENGTH_SHADOW.value,
            MarketRelativeStrengthShadowScenario.RISK_OFF_SIDE_DIAGNOSTIC: ReasonCode.RISK_OFF_SIDE_DIAGNOSTIC_ONLY.value,
            MarketRelativeStrengthShadowScenario.SYSTEMIC_RISK_EXCLUDED: ReasonCode.SYSTEMIC_RISK_SHADOW_EXCLUDED.value,
            MarketRelativeStrengthShadowScenario.DATA_WAIT_EXCLUDED: ReasonCode.MARKET_RS_CONTEXT_NOT_READY.value,
        }.get(scenario)
        base = [scenario_reason] if scenario_reason else []
        if status == MarketRelativeStrengthShadowStatus.SHADOW_CANDIDATE:
            base.append(ReasonCode.MARKET_RS_SHADOW_CANDIDATE.value)
        elif status == MarketRelativeStrengthShadowStatus.SHADOW_REJECT:
            base.append(ReasonCode.MARKET_RS_SHADOW_REJECT.value)
        return normalize_reason_codes(_dedupe(base + list(reject_reasons)))

    def _feature_snapshot(self, context: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "context": {
                "context_id": context.get("context_id"),
                "context_fresh": context.get("context_fresh"),
                "session_phase": context.get("session_phase"),
                "blocking_stage": context.get("blocking_stage"),
                "primary_reason_code": context.get("primary_reason_code"),
            },
            "market": dict(context.get("market") or {}),
            "theme": dict(context.get("theme") or {}),
            "stock": dict(context.get("stock") or {}),
            "data": dict(context.get("data") or {}),
            "risk": dict(context.get("risk") or {}),
            "entry": dict(entry or {}),
        }


@dataclass
class MarketRelativeStrengthShadowRuntimePipeline:
    db: Any
    config: MarketRelativeStrengthShadowConfig = field(default_factory=MarketRelativeStrengthShadowConfig.from_env)
    evaluator: MarketRelativeStrengthShadowEvaluator | None = None
    clock: Any = datetime.now

    def __post_init__(self) -> None:
        self.evaluator = self.evaluator or MarketRelativeStrengthShadowEvaluator(config=self.config)
        self.last_run_at: str = ""
        self.last_summary: dict[str, Any] = {"enabled": self.config.enabled, "status": "IDLE"}

    def run_if_due(self, now: datetime | None = None, **_: Any) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"enabled": False, "status": "DISABLED", "order_intent_allowed": False, "live_order_allowed": False}
            return dict(self.last_summary)
        if self.last_run_at and (current - _parse_dt(self.last_run_at, current)).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        return self.run(current)

    def run(self, now: datetime | None = None, **_: Any) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"enabled": False, "status": "DISABLED", "order_intent_allowed": False, "live_order_allowed": False}
            return dict(self.last_summary)
        trade_date = current.date().isoformat()
        candidates = [
            candidate
            for candidate in list(self.db.list_candidates(trade_date=trade_date) or [])
            if candidate.state not in {CandidateState.REMOVED, CandidateState.EXPIRED}
        ]
        name_fallbacks = _candidate_name_fallbacks(self.db, candidates, trade_date=trade_date)
        decisions: list[MarketRelativeStrengthShadowDecision] = []
        suppressed = 0
        for candidate in candidates:
            if len(decisions) >= max(1, int(self.config.max_per_cycle or 1)):
                break
            code = normalize_code(candidate.code)
            if not str(candidate.name or "").strip() and name_fallbacks.get(code):
                candidate.name = name_fallbacks[code]
            decision = self.evaluator.evaluate_candidate(candidate, now=current)
            if decision.shadow_status == MarketRelativeStrengthShadowStatus.NOT_APPLICABLE.value:
                continue
            if self._duplicate_suppressed(decision, now=current):
                suppressed += 1
                continue
            decisions.append(decision)
        persisted = self.db.save_strategy_decision_events([decision.to_event() for decision in decisions]) if decisions else 0
        counts: dict[str, int] = {}
        scenario_counts: dict[str, int] = {}
        for decision in decisions:
            counts[decision.shadow_status] = counts.get(decision.shadow_status, 0) + 1
            scenario_counts[decision.shadow_scenario] = scenario_counts.get(decision.shadow_scenario, 0) + 1
        self.last_run_at = current.isoformat()
        self.last_summary = {
            "enabled": True,
            "status": "OK",
            "calculated_at": current.isoformat(),
            "evaluated_count": len(candidates),
            "shadow_event_count": len(decisions),
            "persisted_count": persisted,
            "duplicate_suppressed_count": suppressed,
            "shadow_status_counts": counts,
            "scenario_counts": scenario_counts,
            "recent_candidates": [decision.to_dict() for decision in decisions[:10]],
            "order_intent_allowed": False,
            "dry_run_order_allowed": False,
            "live_order_allowed": False,
        }
        return dict(self.last_summary)

    def _duplicate_suppressed(self, decision: MarketRelativeStrengthShadowDecision, *, now: datetime) -> bool:
        loader = getattr(self.db, "list_strategy_decision_events", None)
        if not callable(loader):
            return False
        recent = loader(
            trade_date=decision.trade_date,
            code=decision.code,
            action_type=ACTION_TYPE,
            limit=20,
        )
        for event in recent:
            details = dict(event.get("details") or {})
            if str(details.get("material_key") or "") != decision.material_key:
                continue
            event_at = _parse_dt(event.get("decision_at"), now)
            if (now - event_at).total_seconds() <= max(0, int(self.config.dedupe_sec or 0)):
                return True
        return False


def _decision_id(*, trade_date: str, code: str, candidate_instance_id: str, context_id: str, scenario: str, variant: str, material_key: str) -> str:
    raw = "|".join([trade_date, code, candidate_instance_id, context_id, scenario, variant, material_key])
    return f"market-rs-shadow:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _material_key(values: Mapping[str, Any]) -> str:
    raw = "|".join(f"{key}={values.get(key, '')}" for key in sorted(values))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _candidate_name_fallbacks(db: Any, candidates: Iterable[Candidate], *, trade_date: str) -> dict[str, str]:
    candidate_rows = list(candidates)
    codes = sorted({normalize_code(candidate.code) for candidate in candidate_rows if normalize_code(candidate.code)})
    if not codes:
        return {}
    wanted = set(codes)
    names: dict[str, str] = {}
    for candidate in candidate_rows:
        _remember_name(names, wanted, candidate.code, candidate.name)
    for row in _seed_rows(db, "list_opening_turnover_seed_rows", trade_date=trade_date):
        _remember_name(names, wanted, _row_value(row, "stock_code") or _row_value(row, "code"), _row_value(row, "stock_name") or _row_value(row, "name"))
    for row in _seed_rows(db, "list_intraday_theme_discovery_rows", trade_date=trade_date):
        _remember_name(names, wanted, _row_value(row, "stock_code") or _row_value(row, "code"), _row_value(row, "stock_name") or _row_value(row, "name"))
    for row in _theme_membership_name_rows(db, codes):
        _remember_name(names, wanted, _row_value(row, "stock_code") or _row_value(row, "code"), _row_value(row, "stock_name") or _row_value(row, "name"))
    loader = getattr(db, "list_kiwoom_symbol_master", None)
    if callable(loader):
        for row in list(loader(codes) or []):
            _remember_name(names, wanted, _row_value(row, "code"), _row_value(row, "name"))
    return names


def _seed_rows(db: Any, loader_name: str, *, trade_date: str) -> list[Any]:
    loader = getattr(db, loader_name, None)
    if not callable(loader):
        return []
    try:
        return list(loader(trade_date=trade_date, limit=5000) or [])
    except TypeError:
        return list(loader(trade_date=trade_date) or [])


def _theme_membership_name_rows(db: Any, codes: Iterable[str]) -> list[dict[str, Any]]:
    conn = getattr(db, "conn", None)
    clean_codes = sorted({normalize_code(code) for code in codes if normalize_code(code)})
    if conn is None or not clean_codes:
        return []
    placeholders = ",".join("?" for _ in clean_codes)
    try:
        rows = conn.execute(
            f"""
            SELECT stock_code, stock_name
            FROM theme_membership_current
            WHERE stock_code IN ({placeholders})
              AND COALESCE(stock_name, '') <> ''
            ORDER BY active DESC, trade_eligible DESC, membership_score DESC, updated_at DESC
            """,
            tuple(clean_codes),
        ).fetchall()
    except Exception:
        return []
    return [{"stock_code": _row_value(row, "stock_code"), "stock_name": _row_value(row, "stock_name")} for row in rows]


def _remember_name(result: dict[str, str], wanted: set[str], code: Any, name: Any) -> None:
    clean_code = normalize_code(code)
    text = str(name or "").strip()
    if clean_code and clean_code in wanted and text and clean_code not in result:
        result[clean_code] = text


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, "")


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _relative_strength_band(value: float) -> str:
    if value < 2.0:
        return "LT_2"
    if value < 4.0:
        return "2_TO_4"
    if value < 6.0:
        return "4_TO_6"
    return "GE_6"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or default).replace(",", "")))
    except (TypeError, ValueError):
        return int(default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _enum_text(value: Any, *, default: str = "") -> str:
    raw = getattr(value, "value", value)
    text = str(raw if raw not in (None, "") else default).strip()
    if "." in text:
        head, _, tail = text.rpartition(".")
        if head and tail:
            return tail
    return text


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


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _parse_dt(value: Any, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return default


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "ACTION_TYPE",
    "MarketRelativeStrengthCounterfactualAction",
    "MarketRelativeStrengthShadowConfig",
    "MarketRelativeStrengthShadowDecision",
    "MarketRelativeStrengthShadowEvaluator",
    "MarketRelativeStrengthShadowRuntimePipeline",
    "MarketRelativeStrengthShadowScenario",
    "MarketRelativeStrengthShadowStatus",
    "MarketRelativeStrengthShadowVariant",
]
