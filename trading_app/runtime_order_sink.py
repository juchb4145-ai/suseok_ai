from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from trading.strategy.models import Candidate, EntryPlan, VirtualOrder
from trading.strategy.pipeline import GatePipelineResult
from trading_app.dependencies import CoreSettings
from trading_app.order_enqueue_service import OrderEnqueueService, RuntimeOrderIntentRequest


class RuntimeOrderSink:
    def on_entry_order_decision(
        self,
        *,
        candidate: Candidate,
        gate_result: GatePipelineResult,
        entry_plan: EntryPlan,
        virtual_order: VirtualOrder,
        runtime_cycle_at: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class NoopRuntimeOrderSink(RuntimeOrderSink):
    reason: str = "OBSERVE_VIRTUAL_ONLY"
    skipped_count: int = 0

    def on_entry_order_decision(
        self,
        *,
        candidate: Candidate,
        gate_result: GatePipelineResult,
        entry_plan: EntryPlan,
        virtual_order: VirtualOrder,
        runtime_cycle_at: str,
    ) -> dict[str, Any]:
        self.skipped_count += 1
        return {"accepted": False, "status": "SKIPPED", "reason": self.reason}

    def snapshot(self) -> dict[str, Any]:
        return {
            "dry_run_order_sink_enabled": False,
            "dry_run_order_policy": self.reason,
            "dry_run_order_skipped_count": self.skipped_count,
            "dry_run_order_intent_count": 0,
            "dry_run_order_accepted_count": 0,
            "dry_run_order_rejected_count": 0,
            "dry_run_order_duplicate_count": 0,
            "dry_run_order_live_would_pass_count": 0,
            "dry_run_order_live_would_reject_count": 0,
            "last_dry_run_order_intent_at": "",
            "last_dry_run_order_reject_reason": "",
        }


@dataclass
class DryRunRuntimeOrderSink(RuntimeOrderSink):
    settings: CoreSettings
    service: OrderEnqueueService
    warning_sink: Callable[[str], None] | None = None
    total_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    live_would_pass_count: int = 0
    live_would_reject_count: int = 0
    last_intent_at: str = ""
    last_reject_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def on_entry_order_decision(
        self,
        *,
        candidate: Candidate,
        gate_result: GatePipelineResult,
        entry_plan: EntryPlan,
        virtual_order: VirtualOrder,
        runtime_cycle_at: str,
    ) -> dict[str, Any]:
        self.total_count += 1
        quantity_payload = self._quantity_payload(virtual_order)
        request = RuntimeOrderIntentRequest(
            source="strategy_runtime",
            dry_run=True,
            account=self.settings.runtime_dry_run_account,
            code=candidate.code,
            side="buy",
            quantity=quantity_payload["quantity"],
            price=int(virtual_order.limit_price or entry_plan.limit_price or 0),
            order_type=int(self.settings.runtime_dry_run_order_type_buy),
            hoga=self.settings.runtime_dry_run_hoga,
            tag=f"runtime:{entry_plan.entry_type or 'entry'}",
            strategy_name=candidate.strategy_profile.value if candidate.strategy_profile else "",
            candidate_id=candidate.id,
            entry_plan_id=entry_plan.id,
            virtual_order_id=virtual_order.id,
            leg_index=virtual_order.leg_index,
            entry_type=entry_plan.entry_type,
            reason="runtime_entry_virtual_order_submitted",
            gate_reason=str(gate_result.details.get("primary_reason_code") or gate_result.final_grade or ""),
            gate_status="READY" if gate_result.strategy_eligible else "BLOCKED",
            gate_score=gate_result.final_score,
            hybrid_score=_optional_float(gate_result.details.get("hybrid_score") or gate_result.details.get("score")),
            theme_name=str(gate_result.details.get("theme_name") or ""),
            theme_score=_optional_float(gate_result.details.get("theme_score")),
            runtime_cycle_at=runtime_cycle_at,
            metadata={
                "base_amount": self.settings.runtime_dry_run_position_amount,
                "weight_pct": virtual_order.weight_pct,
                "respect_weight_pct": self.settings.runtime_dry_run_respect_weight_pct,
                "calculated_order_amount": quantity_payload["order_amount"],
                "quantity_calculation_reason": quantity_payload["reason"],
                "virtual_order_status": virtual_order.status.value if hasattr(virtual_order.status, "value") else str(virtual_order.status),
                "gate_result_key": gate_result.details.get("gate_result_key", ""),
                "theme_id": gate_result.theme_id,
            },
        )
        try:
            result = self.service.enqueue_dry_run_order(request)
        except Exception as exc:
            message = f"RUNTIME_DRY_RUN_ORDER_SINK_FAILED:{candidate.code}:{exc}"
            self.errors.append(message)
            if self.warning_sink is not None:
                self.warning_sink(message)
            return {"accepted": False, "status": "ERROR", "reason": str(exc)}

        payload = result.to_dict()
        if result.status == "DUPLICATE":
            self.duplicate_count += 1
        elif result.accepted:
            self.accepted_count += 1
        else:
            self.rejected_count += 1
            self.last_reject_reason = result.reason
        if result.live_would_pass:
            self.live_would_pass_count += 1
        else:
            self.live_would_reject_count += 1
        self.last_intent_at = str(payload.get("response", {}).get("created_at") or runtime_cycle_at)
        return payload

    def snapshot(self) -> dict[str, Any]:
        persisted = self.service.dry_run_summary()
        return {
            "dry_run_order_sink_enabled": True,
            "dry_run_order_policy": "DRY_RUN_INTENT_ONLY",
            "dry_run_order_intent_count": persisted.get("total", self.total_count),
            "dry_run_order_accepted_count": persisted.get("accepted", self.accepted_count),
            "dry_run_order_rejected_count": persisted.get("rejected", self.rejected_count),
            "dry_run_order_duplicate_count": persisted.get("duplicate", self.duplicate_count),
            "dry_run_order_live_would_pass_count": persisted.get("live_would_pass", self.live_would_pass_count),
            "dry_run_order_live_would_reject_count": persisted.get("live_would_reject", self.live_would_reject_count),
            "last_dry_run_order_intent_at": self.last_intent_at,
            "last_dry_run_order_reject_reason": self.last_reject_reason,
            "dry_run_order_errors": list(self.errors[-10:]),
        }

    def _quantity_payload(self, virtual_order: VirtualOrder) -> dict[str, Any]:
        price = int(virtual_order.limit_price or 0)
        base_amount = max(0, int(self.settings.runtime_dry_run_position_amount or 0))
        weight_pct = float(virtual_order.weight_pct or 0.0)
        order_amount = base_amount
        if self.settings.runtime_dry_run_respect_weight_pct and weight_pct > 0:
            order_amount = int(math.floor(base_amount * weight_pct / 100.0))
        if price <= 0:
            return {"quantity": 0, "order_amount": order_amount, "reason": "PRICE_INVALID"}
        quantity = int(math.floor(order_amount / price))
        min_quantity = max(1, int(self.settings.runtime_dry_run_min_quantity or 1))
        if quantity < min_quantity:
            return {"quantity": 0, "order_amount": order_amount, "reason": "QUANTITY_BELOW_MIN"}
        return {"quantity": quantity, "order_amount": order_amount, "reason": "OK"}


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
