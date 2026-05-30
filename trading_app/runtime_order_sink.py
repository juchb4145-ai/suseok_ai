from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from trading.strategy.exit import SUPPORT_LOSS, TAKE_PROFIT, TIME_EXIT, TRAILING_STOP
from trading.strategy.models import Candidate, EntryPlan, ExitDecision, VirtualOrder, VirtualPosition
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

    def on_exit_order_decision(
        self,
        *,
        candidate: Candidate,
        virtual_position: VirtualPosition,
        exit_decision: ExitDecision,
        runtime_cycle_at: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class NoopRuntimeOrderSink(RuntimeOrderSink):
    reason: str = "OBSERVE_VIRTUAL_ONLY"
    skipped_count: int = 0
    entry_skipped_count: int = 0
    exit_skipped_count: int = 0

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
        self.entry_skipped_count += 1
        return {"accepted": False, "status": "SKIPPED", "reason": self.reason}

    def on_exit_order_decision(
        self,
        *,
        candidate: Candidate,
        virtual_position: VirtualPosition,
        exit_decision: ExitDecision,
        runtime_cycle_at: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.skipped_count += 1
        self.exit_skipped_count += 1
        return {"accepted": False, "status": "SKIPPED", "reason": self.reason}

    def snapshot(self) -> dict[str, Any]:
        return {
            "dry_run_order_sink_enabled": False,
            "dry_run_order_policy": self.reason,
            "dry_run_order_skipped_count": self.skipped_count,
            "dry_run_entry_order_skipped_count": self.entry_skipped_count,
            "dry_run_exit_order_skipped_count": self.exit_skipped_count,
            "dry_run_order_intent_count": 0,
            "dry_run_entry_order_intent_count": 0,
            "dry_run_exit_order_intent_count": 0,
            "dry_run_sell_order_intent_count": 0,
            "dry_run_order_accepted_count": 0,
            "dry_run_order_rejected_count": 0,
            "dry_run_order_duplicate_count": 0,
            "dry_run_exit_accepted_count": 0,
            "dry_run_exit_rejected_count": 0,
            "dry_run_exit_duplicate_count": 0,
            "dry_run_exit_live_would_pass_count": 0,
            "dry_run_exit_live_would_reject_count": 0,
            "dry_run_order_live_would_pass_count": 0,
            "dry_run_order_live_would_reject_count": 0,
            "last_dry_run_order_intent_at": "",
            "last_dry_run_order_reject_reason": "",
            "last_dry_run_exit_order_intent_at": "",
            "last_dry_run_exit_order_reject_reason": "",
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
    exit_total_count: int = 0
    exit_accepted_count: int = 0
    exit_rejected_count: int = 0
    exit_duplicate_count: int = 0
    exit_live_would_pass_count: int = 0
    exit_live_would_reject_count: int = 0
    last_intent_at: str = ""
    last_reject_reason: str = ""
    last_exit_intent_at: str = ""
    last_exit_reject_reason: str = ""
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
        self._record_result(result, runtime_cycle_at, phase="entry")
        return payload

    def on_exit_order_decision(
        self,
        *,
        candidate: Candidate,
        virtual_position: VirtualPosition,
        exit_decision: ExitDecision,
        runtime_cycle_at: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.total_count += 1
        self.exit_total_count += 1
        quantity_payload = self._exit_quantity_payload(virtual_position, exit_decision)
        price = int(quantity_payload["price"] or 0)
        details = dict(exit_decision.details or {})
        request = RuntimeOrderIntentRequest(
            source="strategy_runtime",
            dry_run=True,
            account=self.settings.runtime_dry_run_account,
            code=candidate.code,
            side="sell",
            quantity=int(quantity_payload["quantity"] or 0),
            price=price,
            order_type=int(self.settings.runtime_dry_run_order_type_sell),
            hoga=self.settings.runtime_dry_run_hoga,
            tag=f"runtime:exit:{exit_decision.decision_type}",
            strategy_name=candidate.strategy_profile.value if candidate.strategy_profile else "",
            candidate_id=candidate.id,
            virtual_order_id=virtual_position.virtual_order_id,
            virtual_position_id=virtual_position.id,
            order_phase="exit",
            reason=quantity_payload["reason"] if quantity_payload["reason"] != "OK" else exit_decision.decision_type,
            exit_decision_id=exit_decision.id,
            exit_decision_type=exit_decision.decision_type,
            exit_reason=",".join(exit_decision.reason_codes or []) or exit_decision.decision_type,
            exit_percent=quantity_payload["exit_percent"],
            exit_quantity=quantity_payload["quantity"],
            remaining_quantity=quantity_payload["remaining_quantity"],
            position_entry_price=virtual_position.entry_price,
            position_quantity=virtual_position.quantity,
            position_opened_at=virtual_position.opened_at,
            position_closed_at=virtual_position.closed_at,
            position_max_return_pct=virtual_position.max_return_pct,
            position_max_drawdown_pct=virtual_position.max_drawdown_pct,
            realized_return_pct=virtual_position.realized_return_pct,
            virtual_exit_price=price,
            runtime_cycle_at=runtime_cycle_at,
            metadata={
                **dict(context or {}),
                "decision_type": exit_decision.decision_type,
                "reason_codes": list(exit_decision.reason_codes or []),
                "partial_exit": bool(details.get("partial_exit")),
                "full_exit": bool(details.get("full_exit") or details.get("position_closed")),
                "position_closed": bool(details.get("position_closed")),
                "remaining_weight_pct": dict(virtual_position.details or {}).get("remaining_weight_pct"),
                "filled_weight_pct": dict(virtual_position.details or {}).get("filled_weight_pct"),
                "exit_quantity_calculation_reason": quantity_payload["reason"],
                "quantity_calculation_reason": quantity_payload["reason"],
                "exit_percent_source": quantity_payload["exit_percent_source"],
                "price_source": quantity_payload["price_source"],
            },
        )
        try:
            result = self.service.enqueue_dry_run_order(request)
        except Exception as exc:
            message = f"RUNTIME_DRY_RUN_EXIT_ORDER_SINK_FAILED:{candidate.code}:{exc}"
            self.errors.append(message)
            if self.warning_sink is not None:
                self.warning_sink(message)
            return {"accepted": False, "status": "ERROR", "reason": str(exc)}

        self._record_result(result, runtime_cycle_at, phase="exit")
        return result.to_dict()

    def snapshot(self) -> dict[str, Any]:
        persisted = self.service.dry_run_summary()
        return {
            "dry_run_order_sink_enabled": True,
            "dry_run_order_policy": "DRY_RUN_INTENT_ONLY",
            "dry_run_order_intent_count": persisted.get("total", self.total_count),
            "dry_run_entry_order_intent_count": persisted.get("entry_total", 0),
            "dry_run_exit_order_intent_count": persisted.get("exit_total", self.exit_total_count),
            "dry_run_sell_order_intent_count": persisted.get("sell_total", self.exit_total_count),
            "dry_run_order_accepted_count": persisted.get("accepted", self.accepted_count),
            "dry_run_order_rejected_count": persisted.get("rejected", self.rejected_count),
            "dry_run_order_duplicate_count": persisted.get("duplicate", self.duplicate_count),
            "dry_run_exit_accepted_count": self.exit_accepted_count,
            "dry_run_exit_rejected_count": self.exit_rejected_count,
            "dry_run_exit_duplicate_count": self.exit_duplicate_count,
            "dry_run_exit_live_would_pass_count": persisted.get("exit_live_would_pass", self.exit_live_would_pass_count),
            "dry_run_exit_live_would_reject_count": persisted.get("exit_live_would_reject", self.exit_live_would_reject_count),
            "dry_run_order_live_would_pass_count": persisted.get("live_would_pass", self.live_would_pass_count),
            "dry_run_order_live_would_reject_count": persisted.get("live_would_reject", self.live_would_reject_count),
            "last_dry_run_order_intent_at": self.last_intent_at,
            "last_dry_run_order_reject_reason": self.last_reject_reason,
            "last_dry_run_exit_order_intent_at": self.last_exit_intent_at,
            "last_dry_run_exit_order_reject_reason": self.last_exit_reject_reason,
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

    def _exit_quantity_payload(self, position: VirtualPosition, decision: ExitDecision) -> dict[str, Any]:
        details = dict(decision.details or {})
        position_quantity = max(0, int(position.quantity or 0))
        partial_exit = bool(details.get("partial_exit"))
        full_exit = bool(details.get("full_exit") or details.get("position_closed")) or decision.decision_type in {
            SUPPORT_LOSS,
            TIME_EXIT,
            TRAILING_STOP,
        }
        exit_percent_source = ""
        if partial_exit:
            exit_percent = _optional_float(details.get("exit_percent"))
            exit_percent_source = "exit_percent"
            if exit_percent is None:
                exit_percent = _optional_float(details.get("take_profit_exit_percent"))
                exit_percent_source = "take_profit_exit_percent"
            if exit_percent is None:
                exit_percent = 70.0
                exit_percent_source = "default_partial_exit"
        elif full_exit:
            exit_percent = 100.0
            exit_percent_source = "full_exit"
        else:
            exit_percent = 100.0
            exit_percent_source = "default_full_exit"
        if full_exit:
            quantity = position_quantity
        else:
            quantity = int(math.floor(position_quantity * float(exit_percent or 0.0) / 100.0))
        min_quantity = max(1, int(self.settings.runtime_dry_run_min_quantity or 1))
        reason = "OK"
        if position_quantity <= 0 or quantity <= 0 or quantity < min_quantity:
            quantity = 0
            reason = "QUANTITY_ZERO"
        price_source = "virtual_exit_price"
        price = int(details.get("virtual_exit_price") or 0)
        if price <= 0:
            price_source = "trigger_price"
            price = int(decision.trigger_price or 0)
        if price <= 0:
            price_source = "position_close_price"
            price = int(position.close_price or 0)
        if price <= 0:
            reason = "PRICE_INVALID"
        return {
            "quantity": quantity,
            "remaining_quantity": max(0, position_quantity - quantity),
            "exit_percent": float(exit_percent or 0.0),
            "exit_percent_source": exit_percent_source,
            "price": price,
            "price_source": price_source,
            "reason": reason,
        }

    def _record_result(self, result, runtime_cycle_at: str, *, phase: str) -> None:
        payload = result.to_dict()
        if result.status == "DUPLICATE":
            self.duplicate_count += 1
            if phase == "exit":
                self.exit_duplicate_count += 1
        elif result.accepted:
            self.accepted_count += 1
            if phase == "exit":
                self.exit_accepted_count += 1
        else:
            self.rejected_count += 1
            self.last_reject_reason = result.reason
            if phase == "exit":
                self.exit_rejected_count += 1
                self.last_exit_reject_reason = result.reason
        if result.live_would_pass:
            self.live_would_pass_count += 1
            if phase == "exit":
                self.exit_live_would_pass_count += 1
        else:
            self.live_would_reject_count += 1
            if phase == "exit":
                self.exit_live_would_reject_count += 1
        timestamp = str(payload.get("response", {}).get("created_at") or runtime_cycle_at)
        self.last_intent_at = timestamp
        if phase == "exit":
            self.last_exit_intent_at = timestamp


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
