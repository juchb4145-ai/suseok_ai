from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Callable

from trading.strategy.exit import FINAL_EXIT_TYPES
from trading.strategy.models import Candidate, EntryPlan, ExitDecision, VirtualOrder, VirtualPosition
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.themelab_adapter import ORDER_PHASE_ENTRY, SOURCE as THEMELAB_SOURCE, themelab_entry_idempotency_key
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
        bridge_metadata = _theme_lab_bridge_metadata(candidate, gate_result, entry_plan, virtual_order)
        source = str(bridge_metadata.get("source") or "strategy_runtime")
        order_phase = ORDER_PHASE_ENTRY
        idempotency_key = ""
        if source == THEMELAB_SOURCE:
            idempotency_key = themelab_entry_idempotency_key(
                trade_date=str(bridge_metadata.get("trade_date") or candidate.trade_date or ""),
                code=candidate.code,
                candidate_id=candidate.id,
                order_phase=order_phase,
                leg_index=virtual_order.leg_index,
                candidate_instance_id=str(bridge_metadata.get("candidate_instance_id") or ""),
            )
        request = RuntimeOrderIntentRequest(
            source=source,
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
            order_phase=order_phase,
            reason="runtime_entry_virtual_order_submitted",
            gate_reason=str(gate_result.details.get("primary_reason_code") or gate_result.final_grade or ""),
            gate_status="READY" if gate_result.strategy_eligible else "BLOCKED",
            gate_score=gate_result.final_score,
            hybrid_score=_optional_float(gate_result.details.get("hybrid_score") or gate_result.details.get("score")),
            theme_name=str(gate_result.details.get("theme_name") or ""),
            theme_score=_optional_float(gate_result.details.get("theme_score")),
            runtime_cycle_at=runtime_cycle_at,
            idempotency_key=idempotency_key or None,
            metadata={
                **bridge_metadata,
                "base_amount": self.settings.runtime_dry_run_position_amount,
                "weight_pct": virtual_order.weight_pct,
                "respect_weight_pct": self.settings.runtime_dry_run_respect_weight_pct,
                "calculated_order_amount": quantity_payload["order_amount"],
                "quantity_calculation_reason": quantity_payload["reason"],
                "virtual_order_status": virtual_order.status.value if hasattr(virtual_order.status, "value") else str(virtual_order.status),
                "gate_result_key": gate_result.details.get("gate_result_key", ""),
                "theme_id": gate_result.theme_id,
                "split_plan": list(entry_plan.split_plan or []),
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
        position_details = dict(virtual_position.details or {})
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
                "candidate_instance_id": details.get("candidate_instance_id") or position_details.get("candidate_instance_id") or "",
                "candidate_instance_ids": list(position_details.get("candidate_instance_ids") or []),
                "candidate_generation_seq": details.get("candidate_generation_seq") or position_details.get("candidate_generation_seq") or 0,
                "decision_cycle_id": details.get("decision_cycle_id") or position_details.get("decision_cycle_id") or "",
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
                "theme_status_before": details.get("theme_status_before", ""),
                "theme_status_after": details.get("theme_status_after", ""),
                "theme_score": details.get("theme_score"),
                "leader_symbol": details.get("leader_symbol", ""),
                "leader_return_pct": details.get("leader_return_pct"),
                "index_market": details.get("index_market", ""),
                "index_return_pct": details.get("index_return_pct"),
                "breadth_status": details.get("breadth_status", ""),
                "risk_reason_codes": list(details.get("risk_reason_codes") or []),
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
        full_exit = bool(details.get("full_exit") or details.get("position_closed")) or decision.decision_type in FINAL_EXIT_TYPES
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


@dataclass
class LiveSimRuntimeOrderSink(DryRunRuntimeOrderSink):
    runtime_settings: Any = None
    live_sim_total_count: int = 0
    live_sim_accepted_count: int = 0
    live_sim_blocked_count: int = 0
    live_sim_duplicate_count: int = 0
    live_sim_exit_accepted_count: int = 0
    live_sim_exit_blocked_count: int = 0
    last_live_sim_order_intent_at: str = ""
    last_live_sim_reject_reason: str = ""

    def on_entry_order_decision(
        self,
        *,
        candidate: Candidate,
        gate_result: GatePipelineResult,
        entry_plan: EntryPlan,
        virtual_order: VirtualOrder,
        runtime_cycle_at: str,
    ) -> dict[str, Any]:
        dry_payload = super().on_entry_order_decision(
            candidate=candidate,
            gate_result=gate_result,
            entry_plan=entry_plan,
            virtual_order=virtual_order,
            runtime_cycle_at=runtime_cycle_at,
        )
        live_payload = self._submit_live_sim_from_dry_payload(dry_payload, phase="entry")
        return {**dry_payload, "live_sim": live_payload}

    def on_exit_order_decision(
        self,
        *,
        candidate: Candidate,
        virtual_position: VirtualPosition,
        exit_decision: ExitDecision,
        runtime_cycle_at: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dry_payload = super().on_exit_order_decision(
            candidate=candidate,
            virtual_position=virtual_position,
            exit_decision=exit_decision,
            runtime_cycle_at=runtime_cycle_at,
            context=context,
        )
        live_payload = self._submit_live_sim_from_dry_payload(dry_payload, phase="exit")
        return {**dry_payload, "live_sim": live_payload}

    def snapshot(self) -> dict[str, Any]:
        payload = super().snapshot()
        summary = self.service.live_sim_summary()
        payload.update(
            {
                "live_sim_order_sink_enabled": True,
                "live_sim_order_policy": "LIVE_SIM_FIRST_LEG_GUARDED",
                "live_sim_order_intent_count": int(summary.get("submitted_order_count") or self.live_sim_total_count),
                "live_sim_order_accepted_count": int(summary.get("accepted_order_count") or self.live_sim_accepted_count),
                "live_sim_order_blocked_count": self.live_sim_blocked_count,
                "live_sim_order_duplicate_count": int(summary.get("duplicate_order_blocked_count") or self.live_sim_duplicate_count),
                "live_sim_exit_accepted_count": self.live_sim_exit_accepted_count,
                "live_sim_exit_blocked_count": self.live_sim_exit_blocked_count,
                "last_live_sim_order_intent_at": self.last_live_sim_order_intent_at,
                "last_live_sim_reject_reason": self.last_live_sim_reject_reason,
                "live_sim_summary": summary,
            }
        )
        return payload

    def _submit_live_sim_from_dry_payload(self, dry_payload: dict[str, Any], *, phase: str) -> dict[str, Any]:
        if not dry_payload.get("accepted"):
            return {
                "accepted": False,
                "mode": "LIVE_SIM",
                "status": "SKIPPED",
                "reason": "DRY_RUN_INTENT_NOT_ACCEPTED",
                "dry_run_status": dry_payload.get("status"),
                "dry_run_reason": dry_payload.get("reason"),
            }
        try:
            dry_request = _runtime_request_from_dry_payload(dry_payload)
            request = _replace_runtime_request(
                dry_request,
                dry_run=False,
                idempotency_key=None,
                metadata={
                    **dict(dry_request.metadata or {}),
                    "dry_run_intent_id": dry_payload.get("intent_id"),
                    "dry_run_status": dry_payload.get("status"),
                    "dry_run_reason": dry_payload.get("reason"),
                },
            )
            result = self.service.enqueue_live_sim_order(
                request,
                execution_config=self._execution_config(),
                exit_guard_config=self._exit_guard_config(),
            )
        except Exception as exc:
            message = f"RUNTIME_LIVE_SIM_ORDER_SINK_FAILED:{dry_payload.get('request', {}).get('code', '')}:{exc}"
            self.errors.append(message)
            if self.warning_sink is not None:
                self.warning_sink(message)
            return {"accepted": False, "mode": "LIVE_SIM", "status": "ERROR", "reason": str(exc)}
        self._record_live_sim_result(result, phase=phase)
        return result.to_dict()

    def _execution_config(self) -> dict[str, Any]:
        if self.runtime_settings is None:
            return {"mode": "DRY_RUN", "live_sim_enabled": False, "live_real_enabled": False}
        return dict(self.runtime_settings.value("order_execution", {}) or {})

    def _exit_guard_config(self) -> dict[str, Any]:
        if self.runtime_settings is None:
            return {"enabled": False}
        return dict(self.runtime_settings.value("live_sim_exit_guard", {}) or {})

    def _record_live_sim_result(self, result, *, phase: str) -> None:
        self.live_sim_total_count += 1
        if result.status == "DUPLICATE":
            self.live_sim_duplicate_count += 1
        elif result.accepted:
            self.live_sim_accepted_count += 1
            if phase == "exit":
                self.live_sim_exit_accepted_count += 1
        else:
            self.live_sim_blocked_count += 1
            self.last_live_sim_reject_reason = result.reason
            if phase == "exit":
                self.live_sim_exit_blocked_count += 1
        timestamp = str((result.response or {}).get("created_at") or "")
        if not timestamp and result.record:
            timestamp = str(result.record.get("updated_at") or result.record.get("created_at") or "")
        self.last_live_sim_order_intent_at = timestamp


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _theme_lab_bridge_metadata(
    candidate: Candidate,
    gate_result: GatePipelineResult,
    entry_plan: EntryPlan,
    virtual_order: VirtualOrder,
) -> dict[str, Any]:
    details = dict(gate_result.details or {})
    bridge = dict(details.get("theme_lab_bridge") or {})
    if str(bridge.get("source") or "") != THEMELAB_SOURCE:
        return {}
    cancel = dict(entry_plan.cancel_condition or {})
    leg = _split_leg(entry_plan, virtual_order.leg_index)
    candidate_metadata = dict(candidate.metadata or {})
    metadata = {
        "source": THEMELAB_SOURCE,
        "code": candidate.code,
        "trade_date": candidate.trade_date,
        "candidate_id": candidate.id,
        "candidate_instance_id": _first_text(details.get("candidate_instance_id"), bridge.get("candidate_instance_id"), cancel.get("candidate_instance_id"), candidate_metadata.get("candidate_instance_id")),
        "candidate_generation_seq": _first_text(details.get("candidate_generation_seq"), bridge.get("candidate_generation_seq"), cancel.get("candidate_generation_seq"), candidate_metadata.get("candidate_generation_seq")),
        "decision_cycle_id": _first_text(details.get("decision_cycle_id"), bridge.get("decision_cycle_id"), cancel.get("decision_cycle_id"), candidate_metadata.get("decision_cycle_id")),
        "theme_id": gate_result.theme_id,
        "theme_name": details.get("theme_name") or bridge.get("theme_name") or "",
        "lab_gate_status": details.get("lab_gate_status") or bridge.get("lab_gate_status") or "",
        "final_gate_status": details.get("final_gate_status") or bridge.get("final_gate_status") or "",
        "order_eligibility": details.get("order_eligibility") or bridge.get("order_eligibility") or "",
        "price_location_status": details.get("price_location_status") or bridge.get("price_location_status") or "",
        "risk_level": details.get("risk_level") or bridge.get("risk_level") or "",
        "risk_reason_codes": list(details.get("risk_reason_codes") or bridge.get("risk_reason_codes") or []),
        "reason_codes": list(details.get("reason_codes") or bridge.get("reason_codes") or []),
        "support_price": cancel.get("support_price") or bridge.get("support_price") or details.get("support_price") or 0,
        "support_missing_reason": cancel.get("support_missing_reason") or bridge.get("support_missing_reason") or details.get("support_missing_reason") or "",
        "support_taxonomy": cancel.get("support_taxonomy") or bridge.get("support_taxonomy") or details.get("support_taxonomy") or "",
        "support_coverage": dict(cancel.get("support_coverage") or bridge.get("support_coverage") or details.get("support_coverage") or {}),
        "support_ready": bool(
            cancel.get("support_ready")
            or bridge.get("support_ready")
            or details.get("support_ready")
            or cancel.get("support_price")
            or bridge.get("support_price")
            or details.get("support_price")
            or cancel.get("vwap_ready")
            or bridge.get("vwap_ready")
            or details.get("vwap_ready")
        ),
        "support_reclaimed": bool(cancel.get("support_reclaimed") or bridge.get("support_reclaimed") or details.get("support_reclaimed")),
        "recent_support_price_present": bool(cancel.get("recent_support_price_present") or bridge.get("recent_support_price_present") or details.get("recent_support_price_present")),
        "vwap_present": bool(cancel.get("vwap_present") or bridge.get("vwap_present") or details.get("vwap_present")),
        "vwap_ready": bool(cancel.get("vwap_ready") or bridge.get("vwap_ready") or details.get("vwap_ready")),
        "minute_bar_present": bool(cancel.get("minute_bar_present") or bridge.get("minute_bar_present") or details.get("minute_bar_present")),
        "minute_bar_count": _first_text(cancel.get("minute_bar_count"), bridge.get("minute_bar_count"), details.get("minute_bar_count"), 0),
        "limit_price": int(virtual_order.limit_price or entry_plan.limit_price or 0),
        "limit_vs_current_pct": cancel.get("limit_vs_current_pct"),
        "max_chase_pct": cancel.get("max_chase_pct"),
        "split_leg": virtual_order.leg_index,
        "weight_pct": virtual_order.weight_pct,
        "late_chase_level": details.get("late_chase_level") or bridge.get("late_chase_level") or cancel.get("late_chase_level") or "",
        "sub_status": details.get("sub_status") or bridge.get("sub_status") or "",
        "chase_risk": bool(details.get("chase_risk") or bridge.get("chase_risk") or "CHASE_RISK" in list(details.get("risk_reason_codes") or [])),
        "latest_tick_ready": not bool(details.get("latest_tick_stale") or bridge.get("latest_tick_stale")),
        "latest_tick_stale": bool(details.get("latest_tick_stale") or bridge.get("latest_tick_stale")),
        "candidate_market_status": details.get("candidate_market_status") or bridge.get("candidate_market_status") or "",
        "market_status": details.get("market_status") or bridge.get("market_status") or "",
        "market_reason_codes": list(details.get("market_reason_codes") or bridge.get("market_reason_codes") or []),
    }
    if leg:
        metadata["split_leg_plan"] = dict(leg)
    return metadata


def _split_leg(entry_plan: EntryPlan, leg_index: int | None) -> dict[str, Any]:
    if leg_index is None:
        return {}
    for leg in entry_plan.split_plan or []:
        try:
            if int(leg.get("leg") or 0) == int(leg_index):
                return dict(leg)
        except (TypeError, ValueError):
            continue
    return {}


def _first_text(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _runtime_request_from_dry_payload(payload: dict[str, Any]) -> RuntimeOrderIntentRequest:
    raw_request = dict(payload.get("request") or {})
    field_names = {item.name for item in fields(RuntimeOrderIntentRequest)}
    values = {name: raw_request.get(name) for name in field_names if name in raw_request}
    values.setdefault("metadata", dict(raw_request.get("metadata") or {}))
    values.setdefault("source", str(raw_request.get("source") or "strategy_runtime"))
    values.setdefault("dry_run", False)
    return RuntimeOrderIntentRequest(**values)


def _replace_runtime_request(request: RuntimeOrderIntentRequest, **updates: Any) -> RuntimeOrderIntentRequest:
    payload = request.to_dict()
    payload.update(updates)
    return RuntimeOrderIntentRequest(**payload)
