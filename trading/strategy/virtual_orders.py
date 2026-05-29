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

        submitted_at = (now or datetime.now()).replace(microsecond=0).isoformat()
        order = VirtualOrder(
            candidate_id=plan.candidate_id,
            entry_plan_id=plan.id,
            status=VirtualOrderStatus.SUBMITTED,
            limit_price=plan.limit_price,
            fill_policy=plan.fill_policy,
            submitted_at=submitted_at,
            unfilled_reason="",
        )
        self._submitted_orders.append(order)
        self._submitted_keys[self._key(plan)] = order
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
        threshold = self._fill_threshold(plan)
        details["fill_threshold"] = threshold
        details["submitted_at"] = submitted_at.isoformat()
        details["timeout_at"] = timeout_at.isoformat()

        for candle in candle_builder.completed_candles(plan.cancel_condition.get("code", ""), 1):
            if candle.start_at < submitted_at:
                continue
            if candle.start_at > timeout_at:
                continue
            if candle.low <= threshold:
                order.status = VirtualOrderStatus.FILLED
                order.virtual_fill_price = plan.limit_price
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

    def _find_submitted_order(self, plan: EntryPlan) -> Optional[VirtualOrder]:
        theme_id = str(plan.cancel_condition.get("theme_id") or "")
        order = self._submitted_keys.get(self._key(plan))
        if order is not None and order.status == VirtualOrderStatus.SUBMITTED:
            return order
        if self.db is not None and plan.candidate_id is not None:
            return self.db.find_active_virtual_order(plan.candidate_id, theme_id, plan.entry_type)
        return None

    @staticmethod
    def _key(plan: EntryPlan) -> tuple:
        return (plan.candidate_id, str(plan.cancel_condition.get("theme_id") or ""), plan.entry_type)

    def _fill_threshold(self, plan: EntryPlan) -> int:
        if plan.fill_policy.value == "optimistic":
            return plan.limit_price
        if plan.fill_policy.value == "conservative":
            return self.tick_provider.subtract_ticks(plan.limit_price, 2)
        return self.tick_provider.subtract_ticks(plan.limit_price, 1)


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)
