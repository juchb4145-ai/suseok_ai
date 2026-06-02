from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from trading.strategy.candles import Candle, CandleBuilder, minute_start
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import (
    EntryPlan,
    ExitDecision,
    FillPolicy,
    IndicatorSnapshot,
    StrategyProfile,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.reason_codes import standardize_details
from trading.strategy.runtime_settings import (
    StrategyRuntimeSettings,
    attach_settings_details,
    legacy_strategy_runtime_settings,
)


TAKE_PROFIT = "TAKE_PROFIT"
SUPPORT_LOSS = "SUPPORT_LOSS"
TIME_EXIT = "TIME_EXIT"
TRAILING_STOP = "TRAILING_STOP"
THEME_WEAK_EXIT = "THEME_WEAK_EXIT"
LEADER_COLLAPSE_EXIT = "LEADER_COLLAPSE_EXIT"
INDEX_WEAK_EXIT = "INDEX_WEAK_EXIT"
MARKET_RISK_OFF_EXIT = "MARKET_RISK_OFF_EXIT"
BREADTH_COLLAPSE_EXIT = "BREADTH_COLLAPSE_EXIT"
DATA_INSUFFICIENT_EXIT_BASIS = "DATA_INSUFFICIENT_EXIT_BASIS"
CONTEXT_RISK_EXIT_TYPES = {
    THEME_WEAK_EXIT,
    LEADER_COLLAPSE_EXIT,
    INDEX_WEAK_EXIT,
    MARKET_RISK_OFF_EXIT,
    BREADTH_COLLAPSE_EXIT,
}
DRY_RUN_EXIT_INTENT_TYPES = {TAKE_PROFIT, SUPPORT_LOSS, TIME_EXIT, TRAILING_STOP, *CONTEXT_RISK_EXIT_TYPES}
FINAL_EXIT_TYPES = {SUPPORT_LOSS, TIME_EXIT, TRAILING_STOP, *CONTEXT_RISK_EXIT_TYPES}
CONFIRMATION_CYCLE_MIN = 1
CONFIRMATION_CYCLE_MAX = 5


@dataclass(frozen=True)
class ExitPolicy:
    strategy_profile: StrategyProfile
    take_profit_pct: float
    take_profit_exit_percent: int
    max_hold_minutes: int
    min_expected_return_pct: float
    support_loss_consecutive_closes_below: int = 2
    trailing_recent_low_window: int = 3
    support_dedupe_pct: float = 0.25
    recent_high_failure_window: int = 3


@dataclass
class PositionOpenResult:
    position: Optional[VirtualPosition]
    opened: bool = False
    aggregated: bool = False
    duplicate: bool = False
    rejected_reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class PerformanceUpdateResult:
    position: VirtualPosition
    changed: bool = False
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExitContextRiskSnapshot:
    enabled: bool = False
    theme_id: str = ""
    theme_name: str = ""
    theme_status_before: str = ""
    theme_status_after: str = ""
    theme_score: Optional[float] = None
    previous_theme_score: Optional[float] = None
    leader_symbol: str = ""
    leader_return_pct: Optional[float] = None
    leader_support_broken: bool = False
    leader_vwap_broken: bool = False
    leader_count: Optional[int] = None
    previous_leader_count: Optional[int] = None
    strong_count: Optional[int] = None
    previous_strong_count: Optional[int] = None
    index_market: str = ""
    index_status: str = ""
    index_return_pct: Optional[float] = None
    market_status: str = ""
    breadth_status: str = ""
    stock_role: str = ""
    current_return_pct: Optional[float] = None
    risk_reason_codes: tuple[str, ...] = ()
    calculated_at: str = ""
    context_history_available: bool = False
    context_history_count: int = 0
    theme_score_delta: Optional[float] = None
    theme_status_transition: str = ""
    leader_count_delta: Optional[int] = None
    strong_count_delta: Optional[int] = None
    leader_vwap_break_transition: str = ""
    breadth_before: str = ""
    breadth_deterioration: bool = False
    index_status_before: str = ""
    index_status_deterioration: bool = False
    market_risk_off_transition: str = ""
    theme_weak_consecutive_count: int = 0
    exit_confidence: str = ""
    context_limited_reason: str = ""


@dataclass(frozen=True)
class ContextRiskExitConfirmationConfig:
    theme_weak_confirmation_cycles: int = 2
    leader_collapse_confirmation_cycles: int = 1
    index_weak_confirmation_cycles: int = 1
    breadth_collapse_confirmation_cycles: int = 1
    config_source: str = "default"
    fallback_reasons: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ContextRiskExitConfirmationConfig":
        defaults = cls()
        values: dict[str, int] = {}
        fallback_reasons: list[str] = []
        env_map = {
            "theme_weak_confirmation_cycles": ("TRADING_THEME_WEAK_CONFIRMATION_CYCLES", defaults.theme_weak_confirmation_cycles),
            "leader_collapse_confirmation_cycles": ("TRADING_LEADER_COLLAPSE_CONFIRMATION_CYCLES", defaults.leader_collapse_confirmation_cycles),
            "index_weak_confirmation_cycles": ("TRADING_INDEX_WEAK_CONFIRMATION_CYCLES", defaults.index_weak_confirmation_cycles),
            "breadth_collapse_confirmation_cycles": ("TRADING_BREADTH_COLLAPSE_CONFIRMATION_CYCLES", defaults.breadth_collapse_confirmation_cycles),
        }
        saw_env = False
        for field_name, (env_name, default) in env_map.items():
            raw = os.environ.get(env_name)
            if raw is not None:
                saw_env = True
            value, fallback_reason = _confirmation_cycle_env(env_name, default)
            values[field_name] = value
            if fallback_reason:
                fallback_reasons.append(fallback_reason)
        if fallback_reasons:
            source = "env_invalid_fallback"
        elif saw_env:
            source = "env"
        else:
            source = "default"
        return cls(**values, config_source=source, fallback_reasons=tuple(fallback_reasons))

    def for_decision(self, decision_type: str) -> int:
        if decision_type == THEME_WEAK_EXIT:
            return self.theme_weak_confirmation_cycles
        if decision_type == LEADER_COLLAPSE_EXIT:
            return self.leader_collapse_confirmation_cycles
        if decision_type == INDEX_WEAK_EXIT:
            return self.index_weak_confirmation_cycles
        if decision_type == BREADTH_COLLAPSE_EXIT:
            return self.breadth_collapse_confirmation_cycles
        return 1

    def to_details(self) -> dict[str, Any]:
        return {
            "theme_weak_confirmation_cycles": self.theme_weak_confirmation_cycles,
            "leader_collapse_confirmation_cycles": self.leader_collapse_confirmation_cycles,
            "index_weak_confirmation_cycles": self.index_weak_confirmation_cycles,
            "breadth_collapse_confirmation_cycles": self.breadth_collapse_confirmation_cycles,
            "config_source": self.config_source,
            "fallback_reasons": list(self.fallback_reasons),
        }


class VirtualPositionService:
    def __init__(self, db=None) -> None:
        self.db = db
        self._positions_by_order: dict[int, VirtualPosition] = {}

    def open_from_filled_order(
        self,
        order: VirtualOrder,
        plan: EntryPlan,
        now: Optional[datetime] = None,
    ) -> PositionOpenResult:
        details = {
            "order_kind": "virtual",
            "virtual_order_status": order.status.value,
            "candidate_id": order.candidate_id,
            "entry_plan_id": plan.id,
            **_identity_from_order_plan(order, plan),
        }
        if order.status != VirtualOrderStatus.FILLED:
            details["rejected_reason"] = "virtual_order_not_filled"
            return PositionOpenResult(
                None,
                rejected_reason="virtual_order_not_filled",
                details=_standard_details(details, ["virtual_order_not_filled"], now=now, result=False),
            )

        existing = self._find_position_by_order(order)
        if existing is not None:
            details["duplicate_rejected"] = True
            details["rejected_reason"] = "duplicate_virtual_position"
            return PositionOpenResult(
                existing,
                duplicate=True,
                rejected_reason="duplicate_virtual_position",
                details=_standard_details(details, ["duplicate_virtual_position"], now=now, result=False),
            )

        opened_at = order.filled_at or (now or datetime.now()).replace(microsecond=0).isoformat()
        entry_price = order.virtual_fill_price or order.limit_price or plan.limit_price
        open_position = self._find_open_position(order)
        if open_position is not None:
            position = _aggregate_position_fill(open_position, order, plan, int(entry_price))
            if self.db is not None:
                position = self.db.save_virtual_position(position)
            if order.id is not None:
                self._positions_by_order[order.id] = position
            details["aggregated"] = True
            details["leg_index"] = order.leg_index
            details["weight_pct"] = order.weight_pct
            return PositionOpenResult(position, aggregated=True, details=_standard_details(details, now=opened_at, result=True))

        position = VirtualPosition(
            candidate_id=order.candidate_id,
            virtual_order_id=order.id,
            entry_price=int(entry_price),
            quantity=_plan_quantity(plan),
            opened_at=opened_at,
            max_return_pct=0.0,
            max_drawdown_pct=0.0,
            realized_return_pct=0.0,
            details=_initial_position_details(order, plan),
        )
        if self.db is not None:
            position = self.db.save_virtual_position(position)
        if position.virtual_order_id is not None:
            self._positions_by_order[position.virtual_order_id] = position
        return PositionOpenResult(position, opened=True, details=_standard_details(details, now=opened_at, result=True))

    def update_performance(
        self,
        position: VirtualPosition,
        candle_builder: CandleBuilder,
        code: str = "",
        latest_tick: Optional[StrategyTick] = None,
    ) -> PerformanceUpdateResult:
        clean_code = code or (latest_tick.code if latest_tick is not None else "")
        details = {
            "same_candle_excluded": False,
            "used_completed_candles": 0,
            "insufficient_reason": [],
        }
        if not clean_code:
            details["insufficient_reason"].append("code_missing")
            return PerformanceUpdateResult(position, changed=False, details=_standard_details(details, ["code_missing"], result=False))
        if position.entry_price <= 0 or not position.opened_at:
            details["insufficient_reason"].append("position_entry_missing")
            return PerformanceUpdateResult(position, changed=False, details=_standard_details(details, ["position_entry_missing"], result=False))

        opened_at = _parse_time(position.opened_at)
        candles, same_candle_excluded = _post_open_completed_candles(candle_builder, clean_code, opened_at)
        details["same_candle_excluded"] = same_candle_excluded
        details["used_completed_candles"] = len(candles)
        if not candles:
            details["insufficient_reason"].append("post_open_candles_missing")
            return PerformanceUpdateResult(position, changed=False, details=_standard_details(details, ["post_open_candles_missing"], result=False))

        max_high = max(candle.high for candle in candles)
        min_low = min(candle.low for candle in candles)
        max_return_pct = _return_pct(max_high, position.entry_price)
        max_drawdown_pct = _return_pct(min_low, position.entry_price)
        changed = False
        if max_return_pct > position.max_return_pct:
            position.max_return_pct = max_return_pct
            changed = True
        if max_drawdown_pct < position.max_drawdown_pct:
            position.max_drawdown_pct = max_drawdown_pct
            changed = True
        details["max_high"] = max_high
        details["min_low"] = min_low
        details["max_return_pct"] = position.max_return_pct
        details["max_drawdown_pct"] = position.max_drawdown_pct
        if changed and self.db is not None:
            position = self.db.save_virtual_position(position)
        return PerformanceUpdateResult(position, changed=changed, details=_standard_details(details, result=changed))

    def _find_position_by_order(self, order: VirtualOrder) -> Optional[VirtualPosition]:
        if order.id is None:
            return None
        existing = self._positions_by_order.get(order.id)
        if existing is not None:
            return existing
        if self.db is not None:
            position = self.db.load_virtual_position_by_order(order.id)
            if position is not None:
                return position
            if order.candidate_id is not None:
                for candidate_position in self.db.list_virtual_positions(order.candidate_id):
                    filled_order_ids = candidate_position.details.get("filled_order_ids") or []
                    if order.id in filled_order_ids:
                        return candidate_position
        return None

    def _find_open_position(self, order: VirtualOrder) -> Optional[VirtualPosition]:
        if order.candidate_id is None or self.db is None:
            return None
        return self.db.load_open_virtual_position(order.candidate_id)


class ExitContextRiskEngine:
    def __init__(
        self,
        settings: Optional[StrategyRuntimeSettings] = None,
        confirmation_config: Optional[ContextRiskExitConfirmationConfig] = None,
    ) -> None:
        self.settings = settings or legacy_strategy_runtime_settings()
        self.confirmation_config = confirmation_config or ContextRiskExitConfirmationConfig.from_env()
        self.last_details: dict = {}

    def evaluate(
        self,
        position: VirtualPosition,
        snapshot: IndicatorSnapshot,
        candles: list[Candle],
        existing_decisions: list[ExitDecision],
        created_at: datetime,
        context: Optional[ExitContextRiskSnapshot],
    ) -> Optional[ExitDecision]:
        if context is None or not context.enabled:
            return None
        if _has_final_exit(existing_decisions):
            return None
        self.last_details = {}
        latest = candles[-1] if candles else None
        decision_type, reasons = self._decision_type(context)
        if not decision_type:
            return None
        confirmation = self._confirmation(decision_type, context)
        if not confirmation["allowed"]:
            self.last_details = {
                "context_risk_candidate": decision_type,
                "context_history_available": context.context_history_available,
                "context_history_count": context.context_history_count,
                "exit_confidence": confirmation["exit_confidence"],
                "context_limited_reason": confirmation["reason"],
                "reason_codes": list(reasons) + [confirmation["reason"]],
                "required_confirmation_cycles": confirmation["required_confirmation_cycles"],
                "observed_confirmation_cycles": confirmation["observed_confirmation_cycles"],
                "confirmation_passed": False,
                "config_source": confirmation["config_source"],
                "confirmation_config": self.confirmation_config.to_details(),
                "config_fallback_reasons": list(self.confirmation_config.fallback_reasons),
            }
            return None
        if _has_context_risk_decision(existing_decisions, decision_type):
            return None
        current_return = context.current_return_pct
        if current_return is None:
            current_return = _return_pct(snapshot.price, position.entry_price)
        laggard = str(context.stock_role or "").upper() == "LATE_LAGGARD"
        full_exit = bool(laggard or current_return <= 0)
        exit_percent = 100 if full_exit else 50
        exit_price = int((latest.close if latest is not None else snapshot.price) or snapshot.price or position.entry_price)
        details = {
            "virtual_only": True,
            "strategy_profile": _snapshot_profile(snapshot).value,
            "code": snapshot.code,
            "theme_id": context.theme_id,
            "theme_name": context.theme_name,
            "theme_status_before": context.theme_status_before,
            "theme_status_after": context.theme_status_after,
            "theme_status_current": context.theme_status_after,
            "theme_score": context.theme_score,
            "previous_theme_score": context.previous_theme_score,
            "theme_score_before": context.previous_theme_score,
            "theme_score_current": context.theme_score,
            "theme_score_delta": context.theme_score_delta,
            "theme_status_transition": context.theme_status_transition,
            "leader_symbol": context.leader_symbol,
            "leader_return_pct": context.leader_return_pct,
            "leader_support_broken": bool(context.leader_support_broken),
            "leader_vwap_broken": bool(context.leader_vwap_broken),
            "leader_count": context.leader_count,
            "previous_leader_count": context.previous_leader_count,
            "leader_count_before": context.previous_leader_count,
            "leader_count_current": context.leader_count,
            "leader_count_delta": context.leader_count_delta,
            "strong_count": context.strong_count,
            "previous_strong_count": context.previous_strong_count,
            "strong_count_before": context.previous_strong_count,
            "strong_count_current": context.strong_count,
            "strong_count_delta": context.strong_count_delta,
            "index_market": context.index_market,
            "index_status": context.index_status,
            "index_status_before": context.index_status_before,
            "index_status_current": context.index_status,
            "index_status_deterioration": context.index_status_deterioration,
            "index_return_pct": context.index_return_pct,
            "market_status": context.market_status,
            "market_risk_status": context.market_status,
            "breadth_status": context.breadth_status,
            "breadth_before": context.breadth_before,
            "breadth_current": context.breadth_status,
            "breadth_deterioration": context.breadth_deterioration,
            "stock_role": context.stock_role,
            "current_return_pct": round(float(current_return), 6),
            "risk_reason_codes": list(context.risk_reason_codes or ()),
            "context_history_available": context.context_history_available,
            "context_history_count": context.context_history_count,
            "exit_confidence": confirmation["exit_confidence"],
            "context_limited_reason": context.context_limited_reason or confirmation["reason"],
            "required_confirmation_cycles": confirmation["required_confirmation_cycles"],
            "observed_confirmation_cycles": confirmation["observed_confirmation_cycles"],
            "confirmation_passed": confirmation["allowed"],
            "config_source": confirmation["config_source"],
            "confirmation_config": self.confirmation_config.to_details(),
            "config_fallback_reasons": list(self.confirmation_config.fallback_reasons),
            "primary_exit_reason": decision_type,
            "secondary_exit_reasons": [reason for reason in reasons if reason != _primary_reason_for_decision(decision_type)],
            "exit_reason_priority": _exit_reason_priority(decision_type),
            "exit_reason_confidence": confirmation["exit_confidence"],
            "partial_exit": not full_exit,
            "full_exit": full_exit,
            "exit_percent": exit_percent,
            "position_closed": full_exit,
            "trailing_strengthened": not full_exit,
            "virtual_exit_price": exit_price,
            "trigger_candle_start_at": latest.start_at.isoformat() if latest is not None else "",
            "sequence_ambiguous": False,
            "same_candle_multiple_triggers": False,
            "calculated_at": context.calculated_at,
            **_position_identity(position),
            **self.settings.settings_details(),
        }
        return ExitDecision(
            virtual_position_id=position.id,
            decision_type=decision_type,
            trigger_price=exit_price,
            filled=True,
            fill_policy=FillPolicy.NORMAL,
            reason_codes=reasons,
            details=details,
            created_at=created_at.isoformat(),
        )

    def _confirmation(self, decision_type: str, context: ExitContextRiskSnapshot) -> dict[str, Any]:
        required = self.confirmation_config.for_decision(decision_type)
        observed = _observed_confirmation_cycles(decision_type, context)
        base = {
            "required_confirmation_cycles": required,
            "observed_confirmation_cycles": observed,
            "config_source": self.confirmation_config.config_source,
        }
        if decision_type == MARKET_RISK_OFF_EXIT:
            return {**base, "required_confirmation_cycles": 0, "observed_confirmation_cycles": 0, "allowed": True, "exit_confidence": "HIGH", "reason": ""}
        if decision_type == THEME_WEAK_EXIT:
            if context.context_history_count <= 0:
                return {**base, "allowed": False, "exit_confidence": "LOW", "reason": "DATA_LIMITED_CONTEXT"}
            if observed >= required:
                return {**base, "allowed": True, "exit_confidence": "HIGH" if required >= 2 else "MEDIUM", "reason": ""}
            return {**base, "allowed": False, "exit_confidence": "LOW", "reason": "LOW_CONFIDENCE_EXIT"}
        if decision_type == LEADER_COLLAPSE_EXIT:
            hard_break = bool(context.leader_vwap_broken or context.leader_support_broken)
            if observed >= required:
                return {**base, "allowed": True, "exit_confidence": "MEDIUM" if hard_break else "LOW_MEDIUM", "reason": ""}
            return {**base, "allowed": False, "exit_confidence": "LOW", "reason": "DATA_LIMITED_CONTEXT"}
        if decision_type in {INDEX_WEAK_EXIT, BREADTH_COLLAPSE_EXIT}:
            if observed >= required:
                return {**base, "allowed": True, "exit_confidence": "MEDIUM", "reason": ""}
            return {**base, "allowed": False, "exit_confidence": "LOW", "reason": "DATA_LIMITED_CONTEXT"}
        return {**base, "allowed": True, "exit_confidence": context.exit_confidence or "MEDIUM", "reason": ""}

    def _decision_type(self, context: ExitContextRiskSnapshot) -> tuple[str, list[str]]:
        reasons = [str(code) for code in (context.risk_reason_codes or ()) if str(code)]
        theme_status = str(context.theme_status_after or "").upper()
        index_status = str(context.index_status or "").upper()
        market_status = str(context.market_status or "").upper()
        breadth_status = str(context.breadth_status or "").upper()
        if market_status == "RISK_OFF" or "MARKET_RISK_OFF" in reasons:
            return MARKET_RISK_OFF_EXIT, _append_reason(reasons, "MARKET_RISK_OFF")
        if index_status in {"INDEX_WEAK", "RISK_OFF", "WEAK"} or "INDEX_WEAK" in reasons:
            return INDEX_WEAK_EXIT, _append_reason(reasons, "INDEX_WEAK")
        if (
            theme_status in {"WEAK_THEME", "THEME_WEAK"}
            or "THEME_WEAK" in reasons
            or "WEAK_THEME" in reasons
            or "THEME_SCORE_DROP" in reasons
        ):
            return THEME_WEAK_EXIT, _append_reason(reasons, "THEME_WEAK")
        if (
            context.leader_support_broken
            or context.leader_vwap_broken
            or _optional_float(context.leader_return_pct) is not None
            and float(context.leader_return_pct or 0.0) <= -5.0
            or "LEADER_COLLAPSE" in reasons
        ):
            return LEADER_COLLAPSE_EXIT, _append_reason(reasons, "LEADER_COLLAPSE")
        if (
            breadth_status in {"BREADTH_COLLAPSE", "COLLAPSE", "LOW_BREADTH"}
            or "BREADTH_COLLAPSE" in reasons
            or "LEADER_COUNT_DROP" in reasons
            or "STRONG_COUNT_DROP" in reasons
        ):
            return BREADTH_COLLAPSE_EXIT, _append_reason(reasons, "BREADTH_COLLAPSE")
        return "", reasons


class ExitDecisionEngine:
    def __init__(
        self,
        settings: Optional[StrategyRuntimeSettings] = None,
        context_risk_confirmation_config: Optional[ContextRiskExitConfirmationConfig] = None,
    ) -> None:
        self.settings = settings or legacy_strategy_runtime_settings()
        self.last_details: dict = {}
        self.context_risk_engine = ExitContextRiskEngine(
            settings=self.settings,
            confirmation_config=context_risk_confirmation_config,
        )

    def evaluate(
        self,
        position: Optional[VirtualPosition],
        snapshot: Optional[IndicatorSnapshot],
        candle_builder: CandleBuilder,
        existing_decisions: list[ExitDecision],
        now: Optional[datetime] = None,
        context_risk: Optional[ExitContextRiskSnapshot] = None,
    ) -> list[ExitDecision]:
        evaluated_at = (now or datetime.now()).replace(microsecond=0)
        self.last_details = attach_settings_details({
            "evaluated_at": evaluated_at.isoformat(),
            "reason_codes": [],
            "virtual_only": True,
        }, self.settings)
        if position is None:
            self.last_details["reason_codes"].append("position_missing")
            return []
        if snapshot is None:
            self.last_details["reason_codes"].append("snapshot_missing")
            return []
        if position.closed_at:
            self.last_details["reason_codes"].append("position_already_closed")
            return []
        if position.entry_price <= 0 or not position.opened_at:
            self.last_details["reason_codes"].append("position_entry_missing")
            return []

        profile = _snapshot_profile(snapshot)
        policy = _policy_for_profile(profile, self.settings)
        opened_at = _parse_time(position.opened_at)
        candles, same_candle_excluded = _post_open_completed_candles(candle_builder, snapshot.code, opened_at)
        self.last_details.update(
            {
                "strategy_profile": profile.value,
                "same_candle_excluded": same_candle_excluded,
                "used_completed_candles": len(candles),
            }
        )
        if not candles:
            self.last_details["reason_codes"].append("post_open_candles_missing")
            return []

        decisions: list[ExitDecision] = []
        take_profit = self._take_profit_decision(position, snapshot, policy, candles, existing_decisions, evaluated_at)
        context_risk_decision = self.context_risk_engine.evaluate(
            position,
            snapshot,
            candles,
            existing_decisions,
            evaluated_at,
            context_risk,
        )
        if self.context_risk_engine.last_details:
            self.last_details["context_risk"] = dict(self.context_risk_engine.last_details)
        if _has_partial_take_profit(existing_decisions):
            trailing_stop = self._trailing_stop_decision(position, snapshot, policy, candles, existing_decisions, evaluated_at)
            if take_profit is not None:
                decisions.append(take_profit)
            if trailing_stop is not None:
                _apply_full_close(position, trailing_stop)
                decisions.append(trailing_stop)
            elif context_risk_decision is not None:
                if context_risk_decision.details.get("position_closed") is True:
                    _apply_full_close(position, context_risk_decision)
                decisions.append(context_risk_decision)
            return decisions

        support_loss = self._support_loss_decision(position, snapshot, policy, candles, existing_decisions, evaluated_at)

        _mark_same_candle_ambiguity(take_profit, support_loss)
        _mark_same_candle_ambiguity(take_profit, context_risk_decision)

        if take_profit is not None:
            decisions.append(take_profit)
        if support_loss is not None:
            _apply_full_close(position, support_loss)
            decisions.append(support_loss)
            return decisions
        if context_risk_decision is not None:
            if context_risk_decision.details.get("position_closed") is True:
                _apply_full_close(position, context_risk_decision)
            decisions.append(context_risk_decision)
            return decisions

        time_exit = self._time_exit_decision(position, snapshot, policy, candles, existing_decisions, evaluated_at)
        if time_exit is not None:
            _apply_full_close(position, time_exit)
            decisions.append(time_exit)
        return decisions

    def _take_profit_decision(
        self,
        position: VirtualPosition,
        snapshot: IndicatorSnapshot,
        policy: ExitPolicy,
        candles: list[Candle],
        existing_decisions: list[ExitDecision],
        created_at: datetime,
    ) -> Optional[ExitDecision]:
        target_price = int(round(position.entry_price * (1 + (policy.take_profit_pct / 100.0))))
        if _has_existing_decision(existing_decisions, TAKE_PROFIT, target_price):
            self.last_details["reason_codes"].append("duplicate_take_profit")
            return None
        trigger = next((candle for candle in candles if candle.high >= target_price), None)
        if trigger is None:
            return None
        return ExitDecision(
            virtual_position_id=position.id,
            decision_type=TAKE_PROFIT,
            trigger_price=target_price,
            filled=True,
            fill_policy=FillPolicy.NORMAL,
            reason_codes=["TAKE_PROFIT_TARGET_REACHED"],
            details={
                "virtual_only": True,
                "strategy_profile": policy.strategy_profile.value,
                "code": snapshot.code,
                **_position_identity(position),
                "target_return_pct": policy.take_profit_pct,
                "exit_percent": policy.take_profit_exit_percent,
                "partial_exit": policy.take_profit_exit_percent < 100,
                "position_closed": False,
                "target_price": target_price,
                "virtual_exit_price": target_price,
                "trigger_candle_start_at": trigger.start_at.isoformat(),
                "sequence_ambiguous": False,
                "same_candle_multiple_triggers": False,
                **self.settings.settings_details(),
            },
            created_at=created_at.isoformat(),
        )

    def _support_loss_decision(
        self,
        position: VirtualPosition,
        snapshot: IndicatorSnapshot,
        policy: ExitPolicy,
        candles: list[Candle],
        existing_decisions: list[ExitDecision],
        created_at: datetime,
    ) -> Optional[ExitDecision]:
        if _has_final_exit(existing_decisions):
            self.last_details["reason_codes"].append("duplicate_final_exit")
            return None

        basis = _support_basis(snapshot, policy)
        self.last_details["support_basis"] = basis
        if not basis:
            self.last_details["reason_codes"].append(DATA_INSUFFICIENT_EXIT_BASIS)
            return None
        if len(candles) < 2:
            self.last_details["reason_codes"].append("support_loss_candles_insufficient")
            return None

        required_closes = max(1, int(policy.support_loss_consecutive_closes_below))
        for basis_name, basis_price in basis:
            for window in _rolling_windows(candles, required_closes):
                if all(candle.close < basis_price for candle in window):
                    current = window[-1]
                    return ExitDecision(
                        virtual_position_id=position.id,
                        decision_type=SUPPORT_LOSS,
                        trigger_price=current.close,
                        filled=True,
                        fill_policy=FillPolicy.NORMAL,
                        reason_codes=["SUPPORT_LOSS_CONFIRMED"],
                        details={
                            "virtual_only": True,
                            "strategy_profile": policy.strategy_profile.value,
                            "code": snapshot.code,
                            **_position_identity(position),
                            "support_basis": basis_name,
                            "support_basis_price": basis_price,
                            "consecutive_closes_below": required_closes,
                            "full_exit": True,
                            "position_closed": True,
                            "virtual_exit_price": current.close,
                            "trigger_candle_start_at": current.start_at.isoformat(),
                            "sequence_ambiguous": False,
                            "same_candle_multiple_triggers": False,
                            **self.settings.settings_details(),
                        },
                        created_at=created_at.isoformat(),
                    )
        return None

    def _trailing_stop_decision(
        self,
        position: VirtualPosition,
        snapshot: IndicatorSnapshot,
        policy: ExitPolicy,
        candles: list[Candle],
        existing_decisions: list[ExitDecision],
        created_at: datetime,
    ) -> Optional[ExitDecision]:
        if _has_final_exit(existing_decisions):
            self.last_details["reason_codes"].append("duplicate_final_exit")
            return None
        floor_basis = _trailing_floor_basis(snapshot, candles, policy)
        self.last_details["trailing_floor_basis"] = floor_basis
        if not floor_basis:
            self.last_details["reason_codes"].append(DATA_INSUFFICIENT_EXIT_BASIS)
            return None
        basis_name, calculated_floor = max(floor_basis, key=lambda item: item[1])
        details = dict(position.details or {})
        previous_floor = _float(details.get("trailing_floor"), default=0.0)
        trailing_floor = max(previous_floor, calculated_floor)
        if trailing_floor != previous_floor or details.get("trailing_floor_basis") != basis_name:
            details["trailing_floor"] = trailing_floor
            details["trailing_floor_basis"] = basis_name
            details["trailing_floor_updated_at"] = created_at.isoformat()
            position.details = details
            self.last_details["position_details_changed"] = True
        self.last_details["trailing_floor"] = trailing_floor
        self.last_details["trailing_floor_source"] = basis_name
        if len(candles) < 2:
            self.last_details["reason_codes"].append("trailing_stop_candles_insufficient")
            return None
        previous, current = candles[-2], candles[-1]
        if previous.close >= trailing_floor or current.close >= trailing_floor:
            return None
        return ExitDecision(
            virtual_position_id=position.id,
            decision_type=TRAILING_STOP,
            trigger_price=current.close,
            filled=True,
            fill_policy=FillPolicy.NORMAL,
            reason_codes=["TRAILING_STOP_CONFIRMED"],
            details={
                "virtual_only": True,
                "strategy_profile": policy.strategy_profile.value,
                "code": snapshot.code,
                **_position_identity(position),
                "trailing_floor": trailing_floor,
                "trailing_floor_basis": basis_name,
                "consecutive_closes_below": 2,
                "full_exit": True,
                "position_closed": True,
                "virtual_exit_price": current.close,
                "trigger_candle_start_at": current.start_at.isoformat(),
                **self.settings.settings_details(),
            },
            created_at=created_at.isoformat(),
        )

    def _time_exit_decision(
        self,
        position: VirtualPosition,
        snapshot: IndicatorSnapshot,
        policy: ExitPolicy,
        candles: list[Candle],
        existing_decisions: list[ExitDecision],
        created_at: datetime,
    ) -> Optional[ExitDecision]:
        if _has_final_exit(existing_decisions):
            self.last_details["reason_codes"].append("duplicate_final_exit")
            return None
        opened_at = _parse_time(position.opened_at)
        max_hold_until = opened_at + timedelta(minutes=policy.max_hold_minutes)
        if created_at < max_hold_until:
            return None

        basis = _support_basis(snapshot, policy)
        latest_close = candles[-1].close if candles else snapshot.price
        close_below_basis = any(latest_close < basis_price for _, basis_price in basis)
        return_below_minimum = position.max_return_pct < policy.min_expected_return_pct
        recent_high_failed = _recent_high_update_failed(candles, policy.recent_high_failure_window)
        momentum_failed = close_below_basis or return_below_minimum or recent_high_failed
        details = {
            "virtual_only": True,
            "strategy_profile": policy.strategy_profile.value,
            "code": snapshot.code,
            **_position_identity(position),
            "max_hold_minutes": policy.max_hold_minutes,
            "min_expected_return_pct": policy.min_expected_return_pct,
            "max_hold_until": max_hold_until.replace(microsecond=0).isoformat(),
            "latest_close": latest_close,
            "close_below_basis": close_below_basis,
            "return_below_minimum": return_below_minimum,
            "recent_high_failed": recent_high_failed,
            "full_exit": True,
            "position_closed": True,
            "virtual_exit_price": latest_close,
            "trigger_candle_start_at": candles[-1].start_at.isoformat() if candles else "",
        }
        attach_settings_details(details, self.settings)
        if not momentum_failed:
            self.last_details["time_exit_momentum"] = details
            return None

        return ExitDecision(
            virtual_position_id=position.id,
            decision_type=TIME_EXIT,
            trigger_price=latest_close,
            filled=True,
            fill_policy=FillPolicy.NORMAL,
            reason_codes=["TIME_EXIT_MOMENTUM_FAILED"],
            details=details,
            created_at=created_at.isoformat(),
        )


def _policy_for_profile(
    profile: StrategyProfile,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> ExitPolicy:
    active_settings = settings or legacy_strategy_runtime_settings()
    profile_key = _exit_profile_key(profile)
    defaults = "exit_policy_thresholds.kosdaq"
    if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        defaults = f"exit_policy_thresholds.{profile_key}"
    return ExitPolicy(
        strategy_profile=profile if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE} else StrategyProfile.KOSDAQ_THEME_PROFILE,
        take_profit_pct=active_settings.number(f"{defaults}.take_profit_pct", 3.0 if defaults.endswith(("kospi", "semiconductor_signal")) else 5.0),
        take_profit_exit_percent=active_settings.integer(f"{defaults}.take_profit_exit_percent", 70),
        max_hold_minutes=active_settings.integer(f"{defaults}.max_hold_minutes", 60 if defaults.endswith(("kospi", "semiconductor_signal")) else 40),
        min_expected_return_pct=active_settings.number(f"{defaults}.min_expected_return_pct", 0.6 if defaults.endswith(("kospi", "semiconductor_signal")) else 1.0),
        support_loss_consecutive_closes_below=active_settings.integer("exit_policy_thresholds.support_loss_consecutive_closes_below", 2),
        trailing_recent_low_window=active_settings.integer("exit_policy_thresholds.trailing_recent_low_window", 3),
        support_dedupe_pct=active_settings.number("exit_policy_thresholds.support_dedupe_pct", 0.25),
        recent_high_failure_window=active_settings.integer("exit_policy_thresholds.recent_high_failure_window", 3),
    )


def _exit_profile_key(profile: StrategyProfile) -> str:
    if profile == StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE:
        return "semiconductor_signal"
    if profile == StrategyProfile.KOSPI_LEADER_PROFILE:
        return "kospi"
    return "kosdaq"


def _snapshot_profile(snapshot: IndicatorSnapshot) -> StrategyProfile:
    raw = snapshot.metadata.get("strategy_profile") or snapshot.metadata.get("profile")
    if raw:
        try:
            return StrategyProfile(str(raw))
        except ValueError:
            pass
    return StrategyProfile.KOSDAQ_THEME_PROFILE


def _support_basis(snapshot: IndicatorSnapshot, policy: Optional[ExitPolicy] = None) -> list[tuple[str, float]]:
    basis: list[tuple[str, float]] = []
    if snapshot.vwap is not None:
        basis.append(("vwap", float(snapshot.vwap)))
    if snapshot.base_line_120 is not None:
        basis.append(("base_line_120", float(snapshot.base_line_120)))
    if snapshot.envelope_mid is not None:
        basis.append(("envelope_mid", float(snapshot.envelope_mid)))
    if snapshot.day_mid is not None:
        basis.append(("day_mid", float(snapshot.day_mid)))
    if snapshot.ema20_5m is not None and snapshot.metadata.get("ema20_5m_ready") is True:
        basis.append(("ema20_5m", float(snapshot.ema20_5m)))
    return _dedupe_basis(basis, policy.support_dedupe_pct if policy is not None else 0.25)


def _trailing_floor_basis(
    snapshot: IndicatorSnapshot,
    candles: list[Candle],
    policy: Optional[ExitPolicy] = None,
) -> list[tuple[str, float]]:
    basis = [
        (name, price)
        for name, price in _support_basis(snapshot, policy)
        if price <= snapshot.price
    ]
    window = policy.trailing_recent_low_window if policy is not None else 3
    recent = candles[-max(1, int(window)):]
    if recent:
        recent_low = min(candle.low for candle in recent)
        if recent_low > 0 and recent_low <= snapshot.price:
            basis.append(("recent_3m_low", float(recent_low)))
    return _dedupe_basis(basis, policy.support_dedupe_pct if policy is not None else 0.25)


def _dedupe_basis(values: list[tuple[str, float]], threshold_pct: float = 0.25) -> list[tuple[str, float]]:
    result: list[tuple[str, float]] = []
    for name, price in values:
        if price <= 0:
            continue
        duplicate = False
        for _, kept_price in result:
            if abs((price - kept_price) / kept_price) * 100.0 <= threshold_pct:
                duplicate = True
                break
        if not duplicate:
            result.append((name, price))
    return result


def _post_open_completed_candles(
    candle_builder: CandleBuilder,
    code: str,
    opened_at: datetime,
) -> tuple[list[Candle], bool]:
    open_minute = minute_start(opened_at)
    candles = candle_builder.completed_candles(code, 1)
    same_candle_excluded = any(candle.start_at == open_minute for candle in candles)
    return [candle for candle in candles if candle.start_at > open_minute], same_candle_excluded


def _has_existing_decision(existing_decisions: list[ExitDecision], decision_type: str, trigger_price: int) -> bool:
    return any(
        decision.decision_type == decision_type and int(decision.trigger_price) == int(trigger_price)
        for decision in existing_decisions
    )


def _has_context_risk_decision(existing_decisions: list[ExitDecision], decision_type: str) -> bool:
    return any(
        decision.decision_type == decision_type
        and decision.filled
        for decision in existing_decisions
    )


def _confirmation_cycle_env(name: str, default: int) -> tuple[int, str]:
    raw = os.environ.get(name)
    if raw is None:
        return default, ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, f"{name}=invalid:{raw}"
    if value < CONFIRMATION_CYCLE_MIN or value > CONFIRMATION_CYCLE_MAX:
        return default, f"{name}=out_of_range:{value}"
    return value, ""


def _observed_confirmation_cycles(decision_type: str, context: ExitContextRiskSnapshot) -> int:
    if decision_type == THEME_WEAK_EXIT:
        return max(0, int(context.theme_weak_consecutive_count or 0))
    if decision_type == LEADER_COLLAPSE_EXIT:
        hard_break = bool(context.leader_vwap_broken or context.leader_support_broken)
        return max(int(context.context_history_count or 0), 1 if hard_break else 0)
    if decision_type in {INDEX_WEAK_EXIT, BREADTH_COLLAPSE_EXIT}:
        return max(int(context.context_history_count or 0), 1 if context.current_return_pct is not None else 0)
    return int(context.context_history_count or 0)


def _has_final_exit(existing_decisions: list[ExitDecision]) -> bool:
    return any(
        decision.decision_type in FINAL_EXIT_TYPES
        and decision.filled
        and decision.details.get("position_closed", True)
        for decision in existing_decisions
    )


def _has_partial_take_profit(existing_decisions: list[ExitDecision]) -> bool:
    return any(
        decision.decision_type == TAKE_PROFIT
        and decision.filled
        and bool(decision.details.get("partial_exit"))
        for decision in existing_decisions
    )


def _apply_full_close(position: VirtualPosition, decision: ExitDecision) -> None:
    close_price = int(decision.details.get("virtual_exit_price") or decision.trigger_price)
    position.closed_at = decision.created_at
    position.close_price = close_price
    position.close_reason = decision.decision_type
    position.realized_return_pct = _return_pct(close_price, position.entry_price)
    details = dict(position.details or {})
    if details:
        details["remaining_weight_pct"] = 0.0
        position.details = details


def _mark_same_candle_ambiguity(first: Optional[ExitDecision], second: Optional[ExitDecision]) -> None:
    if first is None or second is None:
        return
    first_candle = first.details.get("trigger_candle_start_at")
    second_candle = second.details.get("trigger_candle_start_at")
    if first_candle and first_candle == second_candle:
        for decision in (first, second):
            decision.details["sequence_ambiguous"] = True
            decision.details["same_candle_multiple_triggers"] = True


def _append_reason(reasons: list[str], reason: str) -> list[str]:
    result = list(reasons)
    if reason not in result:
        result.append(reason)
    return result


def _primary_reason_for_decision(decision_type: str) -> str:
    return {
        MARKET_RISK_OFF_EXIT: "MARKET_RISK_OFF",
        INDEX_WEAK_EXIT: "INDEX_WEAK",
        THEME_WEAK_EXIT: "THEME_WEAK",
        LEADER_COLLAPSE_EXIT: "LEADER_COLLAPSE",
        BREADTH_COLLAPSE_EXIT: "BREADTH_COLLAPSE",
    }.get(decision_type, decision_type)


def _exit_reason_priority(decision_type: str) -> int:
    priority = {
        MARKET_RISK_OFF_EXIT: 100,
        SUPPORT_LOSS: 90,
        TRAILING_STOP: 80,
        LEADER_COLLAPSE_EXIT: 75,
        THEME_WEAK_EXIT: 70,
        INDEX_WEAK_EXIT: 60,
        BREADTH_COLLAPSE_EXIT: 55,
        TAKE_PROFIT: 50,
        TIME_EXIT: 40,
    }
    return priority.get(decision_type, 0)


def _optional_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recent_high_update_failed(candles: list[Candle], window: int = 3) -> bool:
    window = max(2, int(window))
    if len(candles) < window:
        return False
    prior_high = max(candle.high for candle in candles[-window:-1])
    return candles[-1].high <= prior_high


def _rolling_windows(candles: list[Candle], size: int) -> list[list[Candle]]:
    size = max(1, int(size))
    if len(candles) < size:
        return []
    return [candles[index : index + size] for index in range(0, len(candles) - size + 1)]


def _plan_quantity(plan: EntryPlan) -> int:
    value = plan.cancel_condition.get("virtual_quantity")
    if value is None and plan.split_plan:
        value = plan.split_plan[0].get("quantity")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _initial_position_details(order: VirtualOrder, plan: EntryPlan) -> dict:
    weight = _order_weight(order)
    fill_diagnostics = _order_fill_diagnostics(order)
    identity = _identity_from_order_plan(order, plan)
    details = {
        "entry_plan_id": plan.id,
        **identity,
        "candidate_instance_ids": [identity["candidate_instance_id"]] if identity.get("candidate_instance_id") else [],
        "filled_legs": [int(order.leg_index or 1)],
        "filled_order_ids": [order.id] if order.id is not None else [],
        "filled_weight_pct": weight,
        "remaining_weight_pct": max(0.0, round(100.0 - weight, 6)),
        "trailing_floor": None,
        "trailing_floor_basis": "",
    }
    if fill_diagnostics:
        details["fill_diagnostics_v2"] = fill_diagnostics
        details["fill_diagnostics_by_leg"] = [
            {
                "leg_index": int(order.leg_index or 1),
                "order_id": order.id,
                "fill_diagnostics_v2": fill_diagnostics,
            }
        ]
    return _standard_details(details, now=order.filled_at, result=True)


def _aggregate_position_fill(
    position: VirtualPosition,
    order: VirtualOrder,
    plan: EntryPlan,
    entry_price: int,
) -> VirtualPosition:
    details = dict(position.details or {})
    identity = _identity_from_order_plan(order, plan)
    instance_ids = [str(value) for value in details.get("candidate_instance_ids") or [] if str(value)]
    if identity.get("candidate_instance_id") and identity["candidate_instance_id"] not in instance_ids:
        instance_ids.append(identity["candidate_instance_id"])
    filled_legs = [int(value) for value in details.get("filled_legs") or []]
    filled_order_ids = [int(value) for value in details.get("filled_order_ids") or []]
    leg_index = int(order.leg_index or 1)
    if leg_index not in filled_legs:
        filled_legs.append(leg_index)
    if order.id is not None and order.id not in filled_order_ids:
        filled_order_ids.append(order.id)
    fill_diagnostics = _order_fill_diagnostics(order)
    fill_diagnostics_by_leg = list(details.get("fill_diagnostics_by_leg") or [])
    if fill_diagnostics:
        fill_diagnostics_by_leg.append(
            {
                "leg_index": leg_index,
                "order_id": order.id,
                "fill_diagnostics_v2": fill_diagnostics,
            }
        )
    old_weight = _float(details.get("filled_weight_pct"), default=100.0 if not details else 0.0)
    new_weight = _order_weight(order)
    total_weight = old_weight + new_weight
    if total_weight > 0 and position.entry_price > 0 and entry_price > 0:
        position.entry_price = int(round(((position.entry_price * old_weight) + (entry_price * new_weight)) / total_weight))
    position.quantity = max(1, int(position.quantity or 0) + _plan_quantity(plan))
    details.update(
        {
            "entry_plan_id": details.get("entry_plan_id") or plan.id,
            "candidate_instance_id": details.get("candidate_instance_id") or identity.get("candidate_instance_id", ""),
            "candidate_instance_ids": instance_ids,
            "candidate_generation_seq": details.get("candidate_generation_seq") or identity.get("candidate_generation_seq", 0),
            "decision_cycle_id": details.get("decision_cycle_id") or identity.get("decision_cycle_id", ""),
            "filled_legs": sorted(filled_legs),
            "filled_order_ids": filled_order_ids,
            "filled_weight_pct": round(min(100.0, total_weight), 6),
            "remaining_weight_pct": max(0.0, round(100.0 - total_weight, 6)),
        }
    )
    if fill_diagnostics:
        details["fill_diagnostics_v2"] = fill_diagnostics
        details["fill_diagnostics_by_leg"] = fill_diagnostics_by_leg
    details.setdefault("trailing_floor", None)
    details.setdefault("trailing_floor_basis", "")
    position.details = _standard_details(details, now=order.filled_at, result=True)
    return position


def _order_weight(order: VirtualOrder) -> float:
    return max(0.0, _float(order.weight_pct, default=100.0))


def _order_fill_diagnostics(order: VirtualOrder) -> dict:
    details = dict(order.details or {})
    diagnostics = details.get("fill_diagnostics_v2")
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def _identity_from_order_plan(order: VirtualOrder, plan: EntryPlan) -> dict:
    order_details = dict(order.details or {})
    cancel = dict(plan.cancel_condition or {})
    return {
        "candidate_instance_id": str(_first_value(order_details.get("candidate_instance_id"), cancel.get("candidate_instance_id")) or ""),
        "candidate_generation_seq": _first_value(order_details.get("candidate_generation_seq"), cancel.get("candidate_generation_seq"), 0),
        "decision_cycle_id": str(_first_value(order_details.get("decision_cycle_id"), cancel.get("decision_cycle_id")) or ""),
    }


def _position_identity(position: VirtualPosition) -> dict:
    details = dict(position.details or {})
    return {
        "candidate_instance_id": str(details.get("candidate_instance_id") or ""),
        "candidate_instance_ids": list(details.get("candidate_instance_ids") or []),
        "candidate_generation_seq": details.get("candidate_generation_seq", 0),
        "decision_cycle_id": str(details.get("decision_cycle_id") or ""),
    }


def _first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _return_pct(price: int, entry_price: int) -> float:
    if entry_price <= 0:
        return 0.0
    return round(((price - entry_price) / entry_price) * 100.0, 6)


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)


def _standard_details(details: dict, reason_codes=None, *, now=None, result=None) -> dict:
    return standardize_details(
        details,
        reason_codes,
        passed=result,
        created_at=now,
        legacy_result=result,
        new_result=result,
    )
