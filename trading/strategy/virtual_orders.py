from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candles import Candle, CandleBuilder
from trading.strategy.entry import TickSizeProvider
from trading.strategy.models import EntryPlan, VirtualOrder, VirtualOrderStatus
from trading.strategy.market_data import StrategyTick
from trading.strategy.reason_codes import ReasonCode, normalize_reason_codes, standardize_details
from trading.strategy.runtime_settings import (
    StrategyRuntimeSettings,
    attach_settings_details,
    legacy_strategy_runtime_settings,
)


FINAL_STATUSES = {
    VirtualOrderStatus.FILLED,
    VirtualOrderStatus.UNFILLED,
    VirtualOrderStatus.CANCELLED,
}
FILL_MODEL_MODE_V2_OBSERVE = "v2_observe"
FILL_MODEL_MODE_LEGACY = "legacy"
FILL_MODEL_VERSION_LEGACY = "legacy"
FILL_CONFIDENCE_HIGH = 75.0
FILL_CONFIDENCE_MEDIUM = 50.0
FILL_MIN_CANDLE_VOLUME = 100
FILL_STRONG_CANDLE_VOLUME = 300
FILL_MIN_TRADE_VALUE = 1_000_000.0
FILL_STRONG_TRADE_VALUE = 3_000_000.0
SPREAD_RISK_TICKS = 3
EXECUTION_STRENGTH_WEAK = 80.0
EXECUTION_STRENGTH_HEALTHY = 90.0
EXECUTION_STRENGTH_STRONG = 120.0
CLOSE_POSITION_RISK = 0.35
CLOSE_POSITION_HEALTHY = 0.55


@dataclass
class VirtualOrderSubmissionResult:
    order: Optional[VirtualOrder]
    submitted: bool = False
    duplicate: bool = False
    rejected_reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class VirtualOrderEvaluationResult:
    order: VirtualOrder
    changed: bool = False
    filled: bool = False
    timed_out: bool = False
    cancelled: bool = False
    details: dict = field(default_factory=dict)


class VirtualOrderService:
    def __init__(
        self,
        db=None,
        tick_provider: Optional[TickSizeProvider] = None,
        fill_model_mode: str = FILL_MODEL_MODE_V2_OBSERVE,
        v2_apply: bool = False,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.db = db
        self.tick_provider = tick_provider or TickSizeProvider()
        self.fill_model_mode = fill_model_mode if fill_model_mode in {FILL_MODEL_MODE_LEGACY, FILL_MODEL_MODE_V2_OBSERVE} else FILL_MODEL_MODE_V2_OBSERVE
        self.v2_apply = bool(v2_apply)
        self.settings = settings or legacy_strategy_runtime_settings()
        self._submitted_orders: list[VirtualOrder] = []
        self._submitted_keys: dict[tuple, VirtualOrder] = {}

    def submit_virtual_order(self, plan: EntryPlan, now: Optional[datetime] = None) -> VirtualOrderSubmissionResult:
        details = attach_settings_details({
            "order_kind": "virtual",
            "virtual_status": VirtualOrderStatus.SUBMITTED.value,
            "theme_id": plan.cancel_condition.get("theme_id", ""),
            "entry_type": plan.entry_type,
        }, self.settings)
        if not plan.cancel_condition.get("submittable", True):
            reason = str(plan.cancel_condition.get("reason") or "not_submittable")
            details["rejected_reason"] = reason
            return VirtualOrderSubmissionResult(
                None,
                submitted=False,
                rejected_reason=reason,
                details=_standard_details(details, [reason], now=now, result=False),
            )

        existing = self._find_submitted_order(plan)
        if existing is not None:
            details["duplicate_rejected"] = True
            details["rejected_reason"] = "duplicate_submitted"
            return VirtualOrderSubmissionResult(
                existing,
                submitted=False,
                duplicate=True,
                rejected_reason="duplicate_submitted",
                details=_standard_details(details, ["duplicate_submitted"], now=now, result=False),
            )

        leg = self._next_submittable_leg(plan)
        if leg is None:
            details["rejected_reason"] = "no_submittable_leg"
            return VirtualOrderSubmissionResult(
                None,
                submitted=False,
                rejected_reason="no_submittable_leg",
                details=_standard_details(details, ["no_submittable_leg"], now=now, result=False),
            )

        submitted_at = (now or datetime.now()).replace(microsecond=0).isoformat()
        leg_index = int(leg.get("leg") or 1)
        limit_price = int(leg.get("limit_price") or plan.limit_price)
        weight_pct = float(leg.get("weight_pct") or 0.0)
        details["leg_index"] = leg_index
        details["weight_pct"] = weight_pct
        details["limit_price"] = limit_price
        order = VirtualOrder(
            candidate_id=plan.candidate_id,
            entry_plan_id=plan.id,
            leg_index=leg_index,
            weight_pct=weight_pct,
            status=VirtualOrderStatus.SUBMITTED,
            limit_price=limit_price,
            fill_policy=plan.fill_policy,
            submitted_at=submitted_at,
            unfilled_reason="",
        )
        self._submitted_orders.append(order)
        self._submitted_keys[self._key(plan, leg_index)] = order
        return VirtualOrderSubmissionResult(order, submitted=True, details=_standard_details(details, now=submitted_at, result=True))

    def evaluate_fill(
        self,
        order: VirtualOrder,
        plan: EntryPlan,
        candle_builder: CandleBuilder,
        now: Optional[datetime] = None,
        latest_tick: Optional[StrategyTick] = None,
    ) -> VirtualOrderEvaluationResult:
        details = attach_settings_details({
            "order_kind": "virtual",
            "virtual_status": order.status.value,
            "include_active_candle": False,
            "fill_policy": plan.fill_policy.value,
            "fill_model_mode": self.fill_model_mode,
            "v2_apply": self.v2_apply,
        }, self.settings)
        if order.status in FINAL_STATUSES:
            details["no_op_reason"] = "final_status"
            return VirtualOrderEvaluationResult(order, changed=False, details=_standard_details(details, ["final_status"], now=now, result=False))
        if order.status != VirtualOrderStatus.SUBMITTED:
            details["no_op_reason"] = "not_submitted"
            return VirtualOrderEvaluationResult(order, changed=False, details=_standard_details(details, ["not_submitted"], now=now, result=False))

        submitted_at = _parse_time(order.submitted_at)
        current_time = (now or datetime.now()).replace(microsecond=0)
        timeout_at = submitted_at + timedelta(seconds=max(0, int(plan.order_timeout_sec)))
        threshold = self._fill_threshold(order, plan)
        details["fill_threshold"] = threshold
        details["submitted_at"] = submitted_at.isoformat()
        details["timeout_at"] = timeout_at.isoformat()
        details["leg_index"] = order.leg_index
        details["weight_pct"] = order.weight_pct
        last_diagnostics: Optional[dict] = None

        for candle in candle_builder.completed_candles(plan.cancel_condition.get("code", ""), 1):
            if candle.start_at < submitted_at:
                continue
            if candle.start_at > timeout_at:
                continue
            touched = candle.low <= threshold
            diagnostics = self._fill_diagnostics(
                order,
                plan,
                candle,
                threshold,
                latest_tick,
                legacy_fill_result=touched,
                legacy_fill_reason="LOW_TOUCHED_THRESHOLD" if touched else "LOW_ABOVE_THRESHOLD",
            )
            last_diagnostics = diagnostics
            if touched and (not self.v2_apply or diagnostics["v2_would_fill"]):
                order.status = VirtualOrderStatus.FILLED
                order.virtual_fill_price = order.limit_price
                order.filled_at = candle.start_at.isoformat()
                details["virtual_status"] = order.status.value
                details["filled_candle_start_at"] = candle.start_at.isoformat()
                _attach_fill_diagnostics(details, diagnostics)
                order.details = _standard_details(details, now=candle.start_at, result=True, new_result=diagnostics["v2_would_fill"])
                return VirtualOrderEvaluationResult(order, changed=True, filled=True, details=order.details)

        if current_time >= timeout_at:
            order.status = VirtualOrderStatus.UNFILLED
            order.unfilled_reason = "TIMEOUT"
            details["virtual_status"] = order.status.value
            details["unfilled_reason"] = "TIMEOUT"
            diagnostics = last_diagnostics or self._fill_diagnostics(
                order,
                plan,
                None,
                threshold,
                latest_tick,
                legacy_fill_result=False,
                legacy_fill_reason="TIMEOUT",
            )
            _attach_fill_diagnostics(details, diagnostics)
            order.details = _standard_details(
                details,
                ["TIMEOUT"],
                now=current_time,
                result=False,
                new_result=diagnostics["v2_would_fill"],
            )
            return VirtualOrderEvaluationResult(order, changed=True, timed_out=True, details=order.details)

        diagnostics = last_diagnostics or self._fill_diagnostics(
            order,
            plan,
            None,
            threshold,
            latest_tick,
            legacy_fill_result=False,
            legacy_fill_reason="PENDING",
        )
        _attach_fill_diagnostics(details, diagnostics)
        return VirtualOrderEvaluationResult(
            order,
            changed=False,
            details=_standard_details(details, now=current_time, result=False, new_result=diagnostics["v2_would_fill"]),
        )

    def cancel_virtual_order(
        self,
        order: VirtualOrder,
        reason: str,
        now: Optional[datetime] = None,
    ) -> VirtualOrderEvaluationResult:
        details = attach_settings_details({
            "order_kind": "virtual",
            "virtual_status": order.status.value,
            "cancel_reason": reason,
        }, self.settings)
        if order.status in FINAL_STATUSES:
            details["no_op_reason"] = "final_status"
            return VirtualOrderEvaluationResult(order, changed=False, details=_standard_details(details, ["final_status"], now=now, result=False))
        if order.status != VirtualOrderStatus.SUBMITTED:
            details["no_op_reason"] = "not_submitted"
            return VirtualOrderEvaluationResult(order, changed=False, details=_standard_details(details, ["not_submitted"], now=now, result=False))
        order.status = VirtualOrderStatus.CANCELLED
        order.cancelled_at = (now or datetime.now()).replace(microsecond=0).isoformat()
        order.unfilled_reason = reason
        details["virtual_status"] = order.status.value
        return VirtualOrderEvaluationResult(order, changed=True, cancelled=True, details=_standard_details(details, [reason], now=order.cancelled_at, result=False))

    def _find_submitted_order(self, plan: EntryPlan, leg_index: Optional[int] = None) -> Optional[VirtualOrder]:
        theme_id = str(plan.cancel_condition.get("theme_id") or "")
        for order in self._plan_orders(plan):
            if order.status != VirtualOrderStatus.SUBMITTED:
                continue
            if leg_index is None or int(order.leg_index or 1) == int(leg_index):
                return order
        if self.db is not None and plan.candidate_id is not None:
            return self.db.find_active_virtual_order(plan.candidate_id, theme_id, plan.entry_type)
        return None

    def _next_submittable_leg(self, plan: EntryPlan) -> Optional[dict]:
        orders = self._plan_orders(plan)
        if any(order.status == VirtualOrderStatus.SUBMITTED for order in orders):
            return None
        ordered_legs = {int(order.leg_index or 1) for order in orders}
        filled_legs = {
            int(order.leg_index or 1)
            for order in orders
            if order.status == VirtualOrderStatus.FILLED
        }
        for leg in sorted(plan.split_plan or [], key=lambda item: int(item.get("leg") or 0)):
            leg_index = int(leg.get("leg") or 0)
            if leg_index <= 0 or leg_index in ordered_legs:
                continue
            if not bool(leg.get("submittable", True)):
                continue
            if int(leg.get("limit_price") or 0) <= 0:
                continue
            if bool(leg.get("requires_previous_leg")) and (leg_index - 1) not in filled_legs:
                continue
            return dict(leg)
        return None

    def _plan_orders(self, plan: EntryPlan) -> list[VirtualOrder]:
        orders = []
        if self.db is not None and plan.candidate_id is not None:
            orders.extend(
                order
                for order in self.db.list_virtual_orders(plan.candidate_id)
                if plan.id is None or order.entry_plan_id == plan.id
            )
        for order in self._submitted_orders:
            if order.candidate_id != plan.candidate_id:
                continue
            if plan.id is not None and order.entry_plan_id != plan.id:
                continue
            if order not in orders:
                orders.append(order)
        return orders

    @staticmethod
    def _key(plan: EntryPlan, leg_index: int) -> tuple:
        return (plan.candidate_id, str(plan.cancel_condition.get("theme_id") or ""), plan.entry_type, int(leg_index))

    def _fill_threshold(self, order: VirtualOrder, plan: EntryPlan) -> int:
        if plan.fill_policy.value == "optimistic":
            return order.limit_price
        if plan.fill_policy.value == "conservative":
            return self.tick_provider.subtract_ticks(order.limit_price, 2)
        return self.tick_provider.subtract_ticks(order.limit_price, 1)

    def _fill_diagnostics(
        self,
        order: VirtualOrder,
        plan: EntryPlan,
        candle: Optional[Candle],
        threshold: int,
        latest_tick: Optional[StrategyTick],
        *,
        legacy_fill_result: bool,
        legacy_fill_reason: str,
    ) -> dict:
        touched = bool(legacy_fill_result)
        spread_ticks, spread_missing = self._spread_ticks(latest_tick, order.limit_price or plan.limit_price)
        trade_value, trade_value_missing = _trade_value(latest_tick, candle)
        execution_strength = _execution_strength(latest_tick)
        candle_volume = candle.volume if candle is not None else None
        close_position = _candle_close_position(candle)
        spread_risk = spread_ticks is not None and spread_ticks > self.settings.integer("fill_model_thresholds.spread_risk_ticks", SPREAD_RISK_TICKS)
        liquidity_risk = _liquidity_risk(candle_volume, trade_value, self.settings)
        execution_strength_risk = execution_strength is not None and execution_strength < self.settings.number("fill_model_thresholds.execution_strength_weak", EXECUTION_STRENGTH_WEAK)
        close_position_risk = close_position is not None and close_position < self.settings.number("fill_model_thresholds.close_position_risk", CLOSE_POSITION_RISK)
        input_missing: list[str] = []
        if candle is None:
            input_missing.append("fill_candle_missing")
        if spread_missing:
            input_missing.append("spread_ticks_missing")
        if trade_value_missing:
            input_missing.append("trade_value_missing")
        if execution_strength is None:
            input_missing.append("execution_strength_missing")
        if close_position is None:
            input_missing.append("candle_close_position_missing")

        confidence = _fill_confidence(
            touched=touched,
            candle_volume=candle_volume,
            trade_value=trade_value,
            spread_ticks=spread_ticks,
            execution_strength=execution_strength,
            candle_close_position=close_position,
            spread_risk=spread_risk,
            liquidity_risk=liquidity_risk,
            execution_strength_risk=execution_strength_risk,
            close_position_risk=close_position_risk,
            input_missing=input_missing,
            settings=self.settings,
        )
        confidence_level = _confidence_level(confidence, self.settings)
        v2_would_fill = bool(touched and confidence >= self.settings.number("fill_model_thresholds.confidence_medium", FILL_CONFIDENCE_MEDIUM))
        reason_codes = []
        if spread_risk:
            reason_codes.append(ReasonCode.SPREAD_TOO_WIDE.value)
        if liquidity_risk or execution_strength_risk:
            reason_codes.append(ReasonCode.FILL_LIQUIDITY_WEAK.value)
        if input_missing:
            reason_codes.extend([ReasonCode.FILL_INPUT_INSUFFICIENT.value, ReasonCode.INPUT_MISSING.value])
        return {
            "fill_model_version": self.fill_model_mode if self.fill_model_mode == FILL_MODEL_MODE_V2_OBSERVE else FILL_MODEL_VERSION_LEGACY,
            "legacy_fill_result": touched,
            "legacy_fill_reason": legacy_fill_reason,
            "touched_limit_price": touched,
            "fill_confidence": round(confidence, 4),
            "fill_confidence_level": confidence_level,
            "spread_ticks": spread_ticks,
            "spread_risk": bool(spread_risk),
            "trade_value": _round_optional(trade_value),
            "liquidity_risk": bool(liquidity_risk),
            "execution_strength": _round_optional(execution_strength),
            "execution_strength_risk": bool(execution_strength_risk),
            "candle_volume": candle_volume,
            "candle_close_position": _round_optional(close_position),
            "close_position_risk": bool(close_position_risk),
            "v2_would_fill": v2_would_fill,
            "v2_non_fill_reason_codes": normalize_reason_codes(reason_codes),
            "input_missing_fields": normalize_reason_codes(input_missing),
            **self.settings.settings_details(),
        }

    def _spread_ticks(self, latest_tick: Optional[StrategyTick], reference_price: int) -> tuple[Optional[int], bool]:
        if latest_tick is None:
            return None, True
        if latest_tick.spread_ticks > 0:
            return int(latest_tick.spread_ticks), False
        if latest_tick.best_ask > 0 and latest_tick.best_bid > 0 and latest_tick.best_ask >= latest_tick.best_bid:
            tick = max(1, self.tick_provider.tick_size(reference_price or latest_tick.price))
            return int(round((latest_tick.best_ask - latest_tick.best_bid) / tick)), False
        return None, True


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)


def _attach_fill_diagnostics(details: dict, diagnostics: dict) -> None:
    details["fill_diagnostics_v2"] = diagnostics
    details["comparison_reason_codes"] = normalize_reason_codes(
        list(details.get("comparison_reason_codes") or []) + list(diagnostics.get("v2_non_fill_reason_codes") or [])
    )


def _trade_value(latest_tick: Optional[StrategyTick], candle: Optional[Candle]) -> tuple[Optional[float], bool]:
    if latest_tick is not None and latest_tick.trade_value > 0:
        return float(latest_tick.trade_value), False
    if candle is not None and candle.volume > 0 and candle.close > 0:
        return float(candle.close * candle.volume), False
    return None, True


def _execution_strength(latest_tick: Optional[StrategyTick]) -> Optional[float]:
    if latest_tick is not None and latest_tick.execution_strength > 0:
        return float(latest_tick.execution_strength)
    return None


def _candle_close_position(candle: Optional[Candle]) -> Optional[float]:
    if candle is None:
        return None
    if candle.high <= candle.low:
        return 1.0 if candle.close >= candle.high else 0.0
    return max(0.0, min(1.0, (candle.close - candle.low) / (candle.high - candle.low)))


def _liquidity_risk(
    candle_volume: Optional[int],
    trade_value: Optional[float],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> bool:
    active_settings = settings or legacy_strategy_runtime_settings()
    if candle_volume is None or trade_value is None:
        return False
    return (
        candle_volume < active_settings.integer("fill_model_thresholds.min_candle_volume", FILL_MIN_CANDLE_VOLUME)
        or trade_value < active_settings.number("fill_model_thresholds.min_trade_value", FILL_MIN_TRADE_VALUE)
    )


def _fill_confidence(
    *,
    touched: bool,
    candle_volume: Optional[int],
    trade_value: Optional[float],
    spread_ticks: Optional[int],
    execution_strength: Optional[float],
    candle_close_position: Optional[float],
    spread_risk: bool,
    liquidity_risk: bool,
    execution_strength_risk: bool,
    close_position_risk: bool,
    input_missing: list[str],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> float:
    active_settings = settings or legacy_strategy_runtime_settings()
    score = (
        active_settings.number("fill_model_thresholds.score_touched", 55.0)
        if touched
        else active_settings.number("fill_model_thresholds.score_not_touched", 10.0)
    )
    missing_penalty = active_settings.number("fill_model_thresholds.score_missing_penalty", -5.0)
    if candle_volume is None:
        score += missing_penalty
    elif candle_volume >= active_settings.integer("fill_model_thresholds.strong_candle_volume", FILL_STRONG_CANDLE_VOLUME):
        score += active_settings.number("fill_model_thresholds.score_strong_volume_bonus", 15.0)
    elif candle_volume >= active_settings.integer("fill_model_thresholds.min_candle_volume", FILL_MIN_CANDLE_VOLUME):
        score += active_settings.number("fill_model_thresholds.score_min_volume_bonus", 8.0)
    else:
        score += active_settings.number("fill_model_thresholds.score_weak_volume_penalty", -20.0)

    if trade_value is None:
        score += missing_penalty
    elif trade_value >= active_settings.number("fill_model_thresholds.strong_trade_value", FILL_STRONG_TRADE_VALUE):
        score += active_settings.number("fill_model_thresholds.score_strong_trade_value_bonus", 10.0)
    elif trade_value >= active_settings.number("fill_model_thresholds.min_trade_value", FILL_MIN_TRADE_VALUE):
        score += active_settings.number("fill_model_thresholds.score_min_trade_value_bonus", 5.0)
    else:
        score += active_settings.number("fill_model_thresholds.score_weak_trade_value_penalty", -10.0)

    if spread_ticks is None:
        score += missing_penalty
    elif spread_risk:
        score += active_settings.number("fill_model_thresholds.score_wide_spread_penalty", -20.0)
    elif spread_ticks <= 1:
        score += active_settings.number("fill_model_thresholds.score_tight_spread_bonus", 10.0)
    else:
        score += active_settings.number("fill_model_thresholds.score_normal_spread_penalty", -8.0)

    if execution_strength is None:
        score += missing_penalty
    elif execution_strength >= active_settings.number("fill_model_thresholds.execution_strength_strong", EXECUTION_STRENGTH_STRONG):
        score += active_settings.number("fill_model_thresholds.score_strong_execution_bonus", 10.0)
    elif execution_strength >= active_settings.number("fill_model_thresholds.execution_strength_healthy", EXECUTION_STRENGTH_HEALTHY):
        score += active_settings.number("fill_model_thresholds.score_healthy_execution_bonus", 5.0)
    elif execution_strength_risk:
        score += active_settings.number("fill_model_thresholds.score_weak_execution_penalty", -15.0)
    else:
        score += active_settings.number("fill_model_thresholds.score_soft_execution_penalty", -5.0)

    if candle_close_position is None:
        score += missing_penalty
    elif candle_close_position >= active_settings.number("fill_model_thresholds.close_position_healthy", CLOSE_POSITION_HEALTHY):
        score += active_settings.number("fill_model_thresholds.score_healthy_close_bonus", 10.0)
    elif close_position_risk:
        score += active_settings.number("fill_model_thresholds.score_weak_close_penalty", -10.0)
    else:
        score += active_settings.number("fill_model_thresholds.score_neutral_close_bonus", 3.0)

    if input_missing and not touched:
        score += active_settings.number("fill_model_thresholds.score_pending_missing_penalty", -5.0)
    if touched and liquidity_risk:
        score = min(score, active_settings.number("fill_model_thresholds.liquidity_risk_confidence_cap", 45.0))
    return max(0.0, min(100.0, score))


def _confidence_level(confidence: float, settings: Optional[StrategyRuntimeSettings] = None) -> str:
    active_settings = settings or legacy_strategy_runtime_settings()
    if confidence >= active_settings.number("fill_model_thresholds.confidence_high", FILL_CONFIDENCE_HIGH):
        return "high"
    if confidence >= active_settings.number("fill_model_thresholds.confidence_medium", FILL_CONFIDENCE_MEDIUM):
        return "medium"
    return "low"


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 6)


def _standard_details(details: dict, reason_codes=None, *, now=None, result=None, new_result=None) -> dict:
    return standardize_details(
        details,
        reason_codes,
        passed=result,
        created_at=now,
        legacy_result=result,
        new_result=result if new_result is None else new_result,
    )
