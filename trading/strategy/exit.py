from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

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


TAKE_PROFIT = "TAKE_PROFIT"
SUPPORT_LOSS = "SUPPORT_LOSS"
TIME_EXIT = "TIME_EXIT"
DATA_INSUFFICIENT_EXIT_BASIS = "DATA_INSUFFICIENT_EXIT_BASIS"
FINAL_EXIT_TYPES = {SUPPORT_LOSS, TIME_EXIT}


@dataclass(frozen=True)
class ExitPolicy:
    strategy_profile: StrategyProfile
    take_profit_pct: float
    take_profit_exit_percent: int
    max_hold_minutes: int
    min_expected_return_pct: float


@dataclass
class PositionOpenResult:
    position: Optional[VirtualPosition]
    opened: bool = False
    duplicate: bool = False
    rejected_reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class PerformanceUpdateResult:
    position: VirtualPosition
    changed: bool = False
    details: dict = field(default_factory=dict)


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
        }
        if order.status != VirtualOrderStatus.FILLED:
            details["rejected_reason"] = "virtual_order_not_filled"
            return PositionOpenResult(None, rejected_reason="virtual_order_not_filled", details=details)

        existing = self._find_position_by_order(order)
        if existing is not None:
            details["duplicate_rejected"] = True
            details["rejected_reason"] = "duplicate_virtual_position"
            return PositionOpenResult(existing, duplicate=True, rejected_reason="duplicate_virtual_position", details=details)

        opened_at = order.filled_at or (now or datetime.now()).replace(microsecond=0).isoformat()
        entry_price = order.virtual_fill_price or order.limit_price or plan.limit_price
        position = VirtualPosition(
            candidate_id=order.candidate_id,
            virtual_order_id=order.id,
            entry_price=int(entry_price),
            quantity=_plan_quantity(plan),
            opened_at=opened_at,
            max_return_pct=0.0,
            max_drawdown_pct=0.0,
            realized_return_pct=0.0,
        )
        if self.db is not None:
            position = self.db.save_virtual_position(position)
        if position.virtual_order_id is not None:
            self._positions_by_order[position.virtual_order_id] = position
        return PositionOpenResult(position, opened=True, details=details)

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
            return PerformanceUpdateResult(position, changed=False, details=details)
        if position.entry_price <= 0 or not position.opened_at:
            details["insufficient_reason"].append("position_entry_missing")
            return PerformanceUpdateResult(position, changed=False, details=details)

        opened_at = _parse_time(position.opened_at)
        candles, same_candle_excluded = _post_open_completed_candles(candle_builder, clean_code, opened_at)
        details["same_candle_excluded"] = same_candle_excluded
        details["used_completed_candles"] = len(candles)
        if not candles:
            details["insufficient_reason"].append("post_open_candles_missing")
            return PerformanceUpdateResult(position, changed=False, details=details)

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
        return PerformanceUpdateResult(position, changed=changed, details=details)

    def _find_position_by_order(self, order: VirtualOrder) -> Optional[VirtualPosition]:
        if order.id is None:
            return None
        existing = self._positions_by_order.get(order.id)
        if existing is not None:
            return existing
        if self.db is not None:
            return self.db.load_virtual_position_by_order(order.id)
        return None


class ExitDecisionEngine:
    def __init__(self) -> None:
        self.last_details: dict = {}

    def evaluate(
        self,
        position: Optional[VirtualPosition],
        snapshot: Optional[IndicatorSnapshot],
        candle_builder: CandleBuilder,
        existing_decisions: list[ExitDecision],
        now: Optional[datetime] = None,
    ) -> list[ExitDecision]:
        evaluated_at = (now or datetime.now()).replace(microsecond=0)
        self.last_details = {
            "evaluated_at": evaluated_at.isoformat(),
            "reason_codes": [],
            "virtual_only": True,
        }
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
        policy = _policy_for_profile(profile)
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
        support_loss = self._support_loss_decision(position, snapshot, policy, candles, existing_decisions, evaluated_at)

        if take_profit is not None and support_loss is not None:
            take_candle = take_profit.details.get("trigger_candle_start_at")
            support_candle = support_loss.details.get("trigger_candle_start_at")
            if take_candle and take_candle == support_candle:
                for decision in (take_profit, support_loss):
                    decision.details["sequence_ambiguous"] = True
                    decision.details["same_candle_multiple_triggers"] = True

        if take_profit is not None:
            decisions.append(take_profit)
        if support_loss is not None:
            _apply_full_close(position, support_loss)
            decisions.append(support_loss)
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
                "target_return_pct": policy.take_profit_pct,
                "exit_percent": policy.take_profit_exit_percent,
                "partial_exit": policy.take_profit_exit_percent < 100,
                "position_closed": False,
                "target_price": target_price,
                "virtual_exit_price": target_price,
                "trigger_candle_start_at": trigger.start_at.isoformat(),
                "sequence_ambiguous": False,
                "same_candle_multiple_triggers": False,
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

        basis = _support_basis(snapshot)
        self.last_details["support_basis"] = basis
        if not basis:
            self.last_details["reason_codes"].append(DATA_INSUFFICIENT_EXIT_BASIS)
            return None
        if len(candles) < 2:
            self.last_details["reason_codes"].append("support_loss_candles_insufficient")
            return None

        for basis_name, basis_price in basis:
            for previous, current in zip(candles, candles[1:]):
                if previous.close < basis_price and current.close < basis_price:
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
                            "support_basis": basis_name,
                            "support_basis_price": basis_price,
                            "consecutive_closes_below": 2,
                            "full_exit": True,
                            "position_closed": True,
                            "virtual_exit_price": current.close,
                            "trigger_candle_start_at": current.start_at.isoformat(),
                            "sequence_ambiguous": False,
                            "same_candle_multiple_triggers": False,
                        },
                        created_at=created_at.isoformat(),
                    )
        return None

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

        basis = _support_basis(snapshot)
        latest_close = candles[-1].close if candles else snapshot.price
        close_below_basis = any(latest_close < basis_price for _, basis_price in basis)
        return_below_minimum = position.max_return_pct < policy.min_expected_return_pct
        recent_high_failed = _recent_high_update_failed(candles)
        momentum_failed = close_below_basis or return_below_minimum or recent_high_failed
        details = {
            "virtual_only": True,
            "strategy_profile": policy.strategy_profile.value,
            "code": snapshot.code,
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


def _policy_for_profile(profile: StrategyProfile) -> ExitPolicy:
    if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        return ExitPolicy(
            strategy_profile=profile,
            take_profit_pct=3.0,
            take_profit_exit_percent=70,
            max_hold_minutes=60,
            min_expected_return_pct=0.6,
        )
    return ExitPolicy(
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
        take_profit_pct=5.0,
        take_profit_exit_percent=70,
        max_hold_minutes=40,
        min_expected_return_pct=1.0,
    )


def _snapshot_profile(snapshot: IndicatorSnapshot) -> StrategyProfile:
    raw = snapshot.metadata.get("strategy_profile") or snapshot.metadata.get("profile")
    if raw:
        try:
            return StrategyProfile(str(raw))
        except ValueError:
            pass
    return StrategyProfile.KOSDAQ_THEME_PROFILE


def _support_basis(snapshot: IndicatorSnapshot) -> list[tuple[str, float]]:
    basis: list[tuple[str, float]] = []
    if snapshot.vwap is not None:
        basis.append(("vwap", float(snapshot.vwap)))
    if snapshot.day_mid is not None:
        basis.append(("day_mid", float(snapshot.day_mid)))
    if snapshot.ema20_5m is not None and snapshot.metadata.get("ema20_5m_ready") is True:
        basis.append(("ema20_5m", float(snapshot.ema20_5m)))
    return basis


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


def _has_final_exit(existing_decisions: list[ExitDecision]) -> bool:
    return any(
        decision.decision_type in FINAL_EXIT_TYPES
        and decision.filled
        and decision.details.get("position_closed", True)
        for decision in existing_decisions
    )


def _apply_full_close(position: VirtualPosition, decision: ExitDecision) -> None:
    close_price = int(decision.details.get("virtual_exit_price") or decision.trigger_price)
    position.closed_at = decision.created_at
    position.close_price = close_price
    position.close_reason = decision.decision_type
    position.realized_return_pct = _return_pct(close_price, position.entry_price)


def _recent_high_update_failed(candles: list[Candle]) -> bool:
    if len(candles) < 3:
        return False
    prior_high = max(candle.high for candle in candles[-3:-1])
    return candles[-1].high <= prior_high


def _plan_quantity(plan: EntryPlan) -> int:
    value = plan.cancel_condition.get("virtual_quantity")
    if value is None and plan.split_plan:
        value = plan.split_plan[0].get("quantity")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _return_pct(price: int, entry_price: int) -> float:
    if entry_price <= 0:
        return 0.0
    return round(((price - entry_price) / entry_price) * 100.0, 6)


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)
