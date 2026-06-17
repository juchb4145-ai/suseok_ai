from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading.broker.models import BrokerExecutionEvent
from trading.strategy.order_models import (
    ManagedOrderStatus,
    OrderExecutionReconcileResult,
    OrderIntentStatus,
    OrderSide,
)


class ManagedOrderReconciler:
    def __init__(self, db: Any) -> None:
        self.db = db

    def handle_command_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        command_id = str(payload.get("command_id") or "")
        command_type = str(payload.get("command_type") or "")
        status = str(payload.get("status") or "ACKED").upper()
        if not command_id:
            return {"matched": False, "reason": "COMMAND_ID_MISSING"}
        order = self._find_by_command_id(command_id)
        if not order:
            return {"matched": False, "reason": "MANAGED_ORDER_NOT_FOUND", "command_id": command_id}

        now = str(payload.get("timestamp") or _now())
        order_result = payload.get("order_result") if isinstance(payload.get("order_result"), dict) else {}
        result_code = int(order_result.get("code", order_result.get("result_code", 0)) or 0) if order_result else 0
        order_no = str(payload.get("order_no") or order_result.get("order_no") or order.get("order_no") or "")
        error = str(payload.get("message") or payload.get("error") or order_result.get("message") or "")
        if command_type == "cancel_order" or status == "CANCELLED":
            next_status = ManagedOrderStatus.CANCELLED.value if result_code == 0 and status not in {"FAILED", "REJECTED"} else ManagedOrderStatus.RECONCILE_REQUIRED.value
            intent_status = OrderIntentStatus.CANCELLED.value if next_status == ManagedOrderStatus.CANCELLED.value else OrderIntentStatus.FAILED.value
        elif status in {"FAILED", "REJECTED"} or result_code != 0:
            next_status = ManagedOrderStatus.REJECTED_BY_GATEWAY.value
            intent_status = OrderIntentStatus.COMMAND_REJECTED.value
        else:
            next_status = ManagedOrderStatus.ACKED_BY_GATEWAY.value
            intent_status = OrderIntentStatus.COMMAND_ACKED.value

        updated = {
            **order,
            "status": next_status,
            "order_no": order_no,
            "acked_at": now,
            "updated_at": now,
            "details": {
                **dict(order.get("details") or {}),
                "last_command_ack": payload,
                "last_error": error,
            },
        }
        self.db.save_managed_order(updated)
        self._update_intent_status(order.get("intent_id"), intent_status, payload)
        self._append_event(
            order,
            "command_ack",
            status_from=str(order.get("status") or ""),
            status_to=next_status,
            payload=payload,
            message=error,
        )
        return {"matched": True, "order_id": order.get("id") or order.get("order_id"), "status": next_status, "order_no": order_no}

    def handle_execution(self, event: BrokerExecutionEvent | dict[str, Any]) -> OrderExecutionReconcileResult:
        execution = event if isinstance(event, BrokerExecutionEvent) else BrokerExecutionEvent.from_dict(dict(event or {}))
        order = self._find_execution_order(execution)
        if not order:
            payload = execution.to_dict()
            self._append_event(
                {"id": None},
                "execution_unmatched",
                status_from="",
                status_to=ManagedOrderStatus.RECONCILE_REQUIRED.value,
                payload=payload,
                message="execution did not match managed order",
            )
            return OrderExecutionReconcileResult(
                matched=False,
                status=ManagedOrderStatus.RECONCILE_REQUIRED.value,
                reason_codes=("UNMATCHED_EXECUTION",),
                details={"execution": payload},
            )

        previous_filled = int(order.get("filled_quantity") or 0)
        event_filled = int(execution.filled_quantity or execution.quantity or 0)
        filled_quantity = max(previous_filled, event_filled)
        remaining_quantity = int(execution.remaining_quantity)
        if remaining_quantity <= 0:
            remaining_quantity = max(0, int(order.get("quantity") or 0) - filled_quantity)
        next_status = ManagedOrderStatus.FILLED.value if remaining_quantity <= 0 else ManagedOrderStatus.PARTIALLY_FILLED.value
        avg_price = float(execution.price or order.get("avg_fill_price") or order.get("price") or 0)
        now = str(execution.timestamp or _now())
        updated = {
            **order,
            "status": next_status,
            "order_no": execution.order_no or order.get("order_no") or "",
            "filled_quantity": filled_quantity,
            "remaining_quantity": remaining_quantity,
            "avg_fill_price": avg_price,
            "updated_at": now,
            "details": {
                **dict(order.get("details") or {}),
                "last_execution": execution.to_dict(),
            },
        }
        self.db.save_managed_order(updated)
        self._update_intent_status(
            order.get("intent_id"),
            OrderIntentStatus.FILLED.value if next_status == ManagedOrderStatus.FILLED.value else OrderIntentStatus.PARTIALLY_FILLED.value,
            execution.to_dict(),
        )
        self._append_event(
            order,
            "execution_fill",
            status_from=str(order.get("status") or ""),
            status_to=next_status,
            payload=execution.to_dict(),
        )
        self._sync_live_sim_position(updated, execution)
        return OrderExecutionReconcileResult(
            matched=True,
            order_id=order.get("id") or order.get("order_id"),
            status=next_status,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            details={"execution": execution.to_dict()},
        )

    def _find_execution_order(self, execution: BrokerExecutionEvent) -> dict[str, Any] | None:
        if execution.order_no:
            order = self._find_by_order_no(execution.order_no)
            if order:
                return order
        if execution.command_id:
            order = self._find_by_command_id(execution.command_id)
            if order:
                return order
        finder = getattr(self.db, "find_managed_order_for_execution", None)
        if callable(finder):
            return finder(
                code=execution.code,
                side=str(execution.side or "").upper(),
                tag=execution.tag,
                idempotency_key=execution.idempotency_key,
            )
        return None

    def _sync_live_sim_position(self, order: dict[str, Any], execution: BrokerExecutionEvent) -> None:
        saver = getattr(self.db, "save_live_sim_position", None)
        if not callable(saver):
            return
        side = str(order.get("side") or execution.side or "").upper()
        details = dict(order.get("details") or {})
        position_id = str(order.get("position_id") or details.get("position_id") or "")
        if not position_id:
            position_id = f"REBOOT_LIVE_SIM:{execution.account or order.get('account') or ''}:{execution.code}"
        current = None
        getter = getattr(self.db, "get_live_sim_position", None)
        if callable(getter):
            current = getter(position_id)
        current_qty = int((current or {}).get("current_qty") or 0)
        filled_delta = int(execution.filled_quantity or execution.quantity or order.get("filled_quantity") or 0)
        if side == OrderSide.BUY.value:
            next_qty = max(current_qty, filled_delta)
            status = "OPEN" if next_qty > 0 else "CLOSED"
            entry_qty = max(int((current or {}).get("entry_qty") or 0), next_qty)
        else:
            next_qty = max(0, current_qty - filled_delta)
            status = "CLOSED" if next_qty <= 0 else "OPEN"
            entry_qty = int((current or {}).get("entry_qty") or current_qty or filled_delta)
        saver(
            {
                "position_id": position_id,
                "candidate_instance_id": str(order.get("candidate_id") or ""),
                "code": execution.code or order.get("code") or "",
                "name": details.get("name") or "",
                "account": execution.account or order.get("account") or "",
                "order_mode": "LIVE_SIM",
                "opened_at": (current or {}).get("opened_at") or execution.timestamp,
                "closed_at": execution.timestamp if status == "CLOSED" else "",
                "entry_qty": entry_qty,
                "entry_avg_price": int(execution.price or order.get("price") or 0),
                "current_qty": next_qty,
                "realized_qty": 0 if side == OrderSide.BUY.value else filled_delta,
                "status": status,
                "details": {
                    **dict((current or {}).get("details") or {}),
                    "theme_id": details.get("theme_id") or "",
                    "theme_name": details.get("theme_name") or "",
                    "source": "reboot_v2_order_manager",
                    "last_order_id": order.get("id") or order.get("order_id"),
                },
                "updated_at": execution.timestamp,
            }
        )

    def _find_by_command_id(self, command_id: str) -> dict[str, Any] | None:
        finder = getattr(self.db, "find_managed_order_by_command_id", None)
        return finder(command_id) if callable(finder) else None

    def _find_by_order_no(self, order_no: str) -> dict[str, Any] | None:
        finder = getattr(self.db, "find_managed_order_by_order_no", None)
        return finder(order_no) if callable(finder) else None

    def _update_intent_status(self, intent_id: Any, status: str, payload: dict[str, Any]) -> None:
        if not intent_id:
            return
        getter = getattr(self.db, "get_managed_order_intent", None)
        saver = getattr(self.db, "save_managed_order_intent", None)
        if not callable(getter) or not callable(saver):
            return
        intent = getter(int(intent_id))
        if not intent:
            return
        saver(
            {
                **intent,
                "status": status,
                "details": {**dict(intent.get("details") or {}), "last_update": payload},
            }
        )

    def _append_event(
        self,
        order: dict[str, Any],
        event_type: str,
        *,
        status_from: str,
        status_to: str,
        payload: dict[str, Any],
        message: str = "",
    ) -> None:
        appender = getattr(self.db, "append_managed_order_event", None)
        if not callable(appender):
            return
        appender(
            {
                "order_id": order.get("id") or order.get("order_id"),
                "intent_id": order.get("intent_id"),
                "event_type": event_type,
                "status_from": status_from,
                "status_to": status_to,
                "message": message,
                "payload": payload,
                "created_at": _now(),
            }
        )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
