from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import TickSizeProvider
from trading.strategy.models import EntryPlan, VirtualOrder, VirtualOrderStatus


FINAL_STATUSES = {
    VirtualOrderStatus.FILLED,
    VirtualOrderStatus.UNFILLED,
    VirtualOrderStatus.CANCELLED,
}


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
    def __init__(self, db=None, tick_provider: Optional[TickSizeProvider] = None) -> None:
        self.db = db
        self.tick_provider = tick_provider or TickSizeProvider()
        self._submitted_orders: list[VirtualOrder] = []
        self._submitted_keys: dict[tuple, VirtualOrder] = {}

    def submit_virtual_order(self, plan: EntryPlan, now: Optional[datetime] = None) -> VirtualOrderSubmissionResult:
        details = {
            "order_kind": "virtual",
            "virtual_status": VirtualOrderStatus.SUBMITTED.value,
            "theme_id": plan.cancel_condition.get("theme_id", ""),
            "entry_type": plan.entry_type,
        }
        if not plan.cancel_condition.get("submittable", True):
            reason = str(plan.cancel_condition.get("reason") or "not_submittable")
            details["rejected_reason"] = reason
            return VirtualOrderSubmissionResult(None, submitted=False, rejected_reason=reason, details=details)

        existing = self._find_submitted_order(plan)
        if existing is not None:
            details["duplicate_rejected"] = True
            details["rejected_reason"] = "duplicate_submitted"
            return VirtualOrderSubmissionResult(
                existing,
                submitted=False,
                duplicate=True,
                rejected_reason="duplicate_submitted",
                details=details,
            )

        leg = self._next_submittable_leg(plan)
        if leg is None:
            details["rejected_reason"] = "no_submittable_leg"
            return VirtualOrderSubmissionResult(
                None,
                submitted=False,
                rejected_reason="no_submittable_leg",
                details=details,
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
        return VirtualOrderSubmissionResult(order, submitted=True, details=details)

    def evaluate_fill(
        self,
        order: VirtualOrder,
        plan: EntryPlan,
        candle_builder: CandleBuilder,
        now: Optional[datetime] = None,
    ) -> VirtualOrderEvaluationResult:
        details = {
            "order_kind": "virtual",
            "virtual_status": order.status.value,
            "include_active_candle": False,
            "fill_policy": plan.fill_policy.value,
        }
        if order.status in FINAL_STATUSES:
            details["no_op_reason"] = "final_status"
            return VirtualOrderEvaluationResult(order, changed=False, details=details)
        if order.status != VirtualOrderStatus.SUBMITTED:
            details["no_op_reason"] = "not_submitted"
            return VirtualOrderEvaluationResult(order, changed=False, details=details)

        submitted_at = _parse_time(order.submitted_at)
        current_time = (now or datetime.now()).replace(microsecond=0)
        timeout_at = submitted_at + timedelta(seconds=max(0, int(plan.order_timeout_sec)))
        threshold = self._fill_threshold(order, plan)
        details["fill_threshold"] = threshold
        details["submitted_at"] = submitted_at.isoformat()
        details["timeout_at"] = timeout_at.isoformat()
        details["leg_index"] = order.leg_index
        details["weight_pct"] = order.weight_pct

        for candle in candle_builder.completed_candles(plan.cancel_condition.get("code", ""), 1):
            if candle.start_at < submitted_at:
                continue
            if candle.start_at > timeout_at:
                continue
            if candle.low <= threshold:
                order.status = VirtualOrderStatus.FILLED
                order.virtual_fill_price = order.limit_price
                order.filled_at = candle.start_at.isoformat()
                details["virtual_status"] = order.status.value
                details["filled_candle_start_at"] = candle.start_at.isoformat()
                return VirtualOrderEvaluationResult(order, changed=True, filled=True, details=details)

        if current_time >= timeout_at:
            order.status = VirtualOrderStatus.UNFILLED
            order.unfilled_reason = "TIMEOUT"
            details["virtual_status"] = order.status.value
            details["unfilled_reason"] = "TIMEOUT"
            return VirtualOrderEvaluationResult(order, changed=True, timed_out=True, details=details)

        return VirtualOrderEvaluationResult(order, changed=False, details=details)

    def cancel_virtual_order(
        self,
        order: VirtualOrder,
        reason: str,
        now: Optional[datetime] = None,
    ) -> VirtualOrderEvaluationResult:
        details = {
            "order_kind": "virtual",
            "virtual_status": order.status.value,
            "cancel_reason": reason,
        }
        if order.status in FINAL_STATUSES:
            details["no_op_reason"] = "final_status"
            return VirtualOrderEvaluationResult(order, changed=False, details=details)
        if order.status != VirtualOrderStatus.SUBMITTED:
            details["no_op_reason"] = "not_submitted"
            return VirtualOrderEvaluationResult(order, changed=False, details=details)
        order.status = VirtualOrderStatus.CANCELLED
        order.cancelled_at = (now or datetime.now()).replace(microsecond=0).isoformat()
        order.unfilled_reason = reason
        details["virtual_status"] = order.status.value
        return VirtualOrderEvaluationResult(order, changed=True, cancelled=True, details=details)

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


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)
