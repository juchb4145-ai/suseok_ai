from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading.broker.command_queue import CommandPriority
from trading.broker.models import GatewayCommand, new_message_id
from trading.strategy.order_models import (
    ManagedOrderStatus,
    OrderIntentStatus,
    OrderManagerConfig,
    OrderSide,
    UnfilledCancelDecision,
)
from trading.strategy.order_risk import broker_environment_state


class UnfilledCancelScheduler:
    def __init__(self, db: Any, gateway_state: Any, config: OrderManagerConfig | None = None) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.config = config or OrderManagerConfig.from_env()
        self.last_summary: dict[str, Any] = {}

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        decisions = self.build_decisions(now=now)
        queued = 0
        skipped = 0
        for decision in decisions:
            if decision.should_cancel:
                if self.queue_cancel(decision, now=now):
                    queued += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        self.last_summary = {
            "status": "READY",
            "mode": self.config.mode,
            "cancel_after_sec": self.config.cancel_unfilled_after_sec,
            "checked_count": len(decisions),
            "queued_cancel_count": queued,
            "skipped_count": skipped,
            "warnings": [],
        }
        return dict(self.last_summary)

    def build_decisions(self, *, now: datetime | None = None) -> list[UnfilledCancelDecision]:
        now = now or datetime.now(timezone.utc)
        rows = self._active_unfilled_orders()
        decisions: list[UnfilledCancelDecision] = []
        for row in rows:
            reasons: list[str] = []
            order_id = row.get("id") or row.get("order_id")
            order_no = str(row.get("order_no") or "")
            remaining = int(row.get("remaining_quantity") or 0)
            if remaining <= 0:
                reasons.append("NO_REMAINING_QUANTITY")
            if not order_no:
                reasons.append("ORIGINAL_ORDER_NO_MISSING")
            age = _age_sec(str(row.get("acked_at") or row.get("sent_at") or row.get("created_at") or ""), now)
            if age is None or age < int(row.get("cancel_after_sec") or self.config.cancel_unfilled_after_sec):
                reasons.append("CANCEL_AGE_NOT_REACHED")
            if self._has_pending_cancel(order_id):
                reasons.append("CANCEL_ALREADY_PENDING")
            if not self._cancel_guard_passes():
                reasons.append("CANCEL_BROKER_GUARD_BLOCK")
            should_cancel = not reasons
            trade_date = str(row.get("trade_date") or now.date().isoformat())
            idempotency = f"reboot_live_sim_cancel:{trade_date}:{order_id}:{order_no}"
            decisions.append(
                UnfilledCancelDecision(
                    should_cancel=should_cancel,
                    order_id=int(order_id or 0) or None,
                    code=str(row.get("code") or ""),
                    order_no=order_no,
                    remaining_quantity=remaining,
                    idempotency_key=idempotency,
                    reason_codes=tuple(reasons),
                    details={"order": row, "age_sec": age},
                )
            )
        return decisions

    def queue_cancel(self, decision: UnfilledCancelDecision, *, now: datetime | None = None) -> bool:
        if not decision.should_cancel or not decision.order_id:
            return False
        order = self.db.get_managed_order(decision.order_id)
        if not order:
            return False
        side = OrderSide.CANCEL_BUY.value if str(order.get("side") or "").upper() == OrderSide.BUY.value else OrderSide.CANCEL_SELL.value
        order_type = 3 if side == OrderSide.CANCEL_BUY.value else 4
        command = GatewayCommand(
            type="cancel_order",
            command_id=new_message_id("cmd_cancel"),
            idempotency_key=decision.idempotency_key,
            payload={
                "account": order.get("account") or "",
                "code": decision.code,
                "quantity": decision.remaining_quantity,
                "price": 0,
                "side": side,
                "order_type": order_type,
                "hoga": self.config.order_hoga,
                "original_order_no": decision.order_no,
                "tag": f"REBOOT_CANCEL_{decision.code}",
                "strategy": "reboot_v2_cancel",
                "order_mode": "LIVE_SIM",
                "broker_env": "SIMULATION",
                "managed_order_id": decision.order_id,
                "idempotency_key": decision.idempotency_key,
            },
        )
        enqueue = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.HIGH,
            ttl_sec=self.config.command_ttl_sec,
            max_attempts=self.config.command_max_attempts,
            metadata={"runtime": "REBOOT_LIVE_SIM", "managed_order_id": decision.order_id, "cancel": True},
            now=now,
        )
        status = ManagedOrderStatus.CANCEL_PENDING.value if enqueue.accepted else ManagedOrderStatus.RECONCILE_REQUIRED.value
        saved = {
            **order,
            "status": status,
            "command_id": command.command_id if enqueue.accepted else order.get("command_id", ""),
            "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "details": {
                **dict(order.get("details") or {}),
                "last_cancel_command": command.to_dict(),
                "cancel_enqueue_result": enqueue.to_dict() if hasattr(enqueue, "to_dict") else {},
            },
        }
        self.db.save_managed_order(saved)
        self._update_intent(order, OrderIntentStatus.CANCEL_REQUESTED.value if enqueue.accepted else OrderIntentStatus.FAILED.value)
        self._append_event(
            order,
            "cancel_queued" if enqueue.accepted else "cancel_rejected",
            status_from=str(order.get("status") or ""),
            status_to=status,
            payload={"decision": decision.to_dict(), "command": command.to_dict()},
            message=enqueue.reason or "",
        )
        return bool(enqueue.accepted)

    def _active_unfilled_orders(self) -> list[dict[str, Any]]:
        lister = getattr(self.db, "list_managed_orders", None)
        if not callable(lister):
            return []
        rows = lister(
            status=[ManagedOrderStatus.ACKED_BY_GATEWAY.value, ManagedOrderStatus.PARTIALLY_FILLED.value],
            limit=1000,
        )
        return [row for row in rows if int(row.get("remaining_quantity") or 0) > 0]

    def _has_pending_cancel(self, order_id: Any) -> bool:
        if not order_id:
            return True
        order = self.db.get_managed_order(int(order_id))
        return str((order or {}).get("status") or "") == ManagedOrderStatus.CANCEL_PENDING.value

    def _cancel_guard_passes(self) -> bool:
        if not self.config.enabled or self.config.mode != "LIVE_SIM" or not self.config.allow_live_sim_orders or self.config.observe_only:
            return False
        snapshot = self.gateway_state.snapshot().to_dict()
        if not bool(snapshot.get("kiwoom_logged_in")) or not bool(snapshot.get("orderable")):
            return False
        if not bool(snapshot.get("heartbeat_ok")):
            return False
        return broker_environment_state(snapshot) == "SIMULATION"

    def _update_intent(self, order: dict[str, Any], status: str) -> None:
        getter = getattr(self.db, "get_managed_order_intent", None)
        saver = getattr(self.db, "save_managed_order_intent", None)
        if not callable(getter) or not callable(saver):
            return
        intent = getter(int(order.get("intent_id") or 0))
        if intent:
            saver({**intent, "status": status})

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
        if callable(appender):
            appender(
                {
                    "order_id": order.get("id") or order.get("order_id"),
                    "intent_id": order.get("intent_id"),
                    "event_type": event_type,
                    "status_from": status_from,
                    "status_to": status_to,
                    "message": message,
                    "payload": payload,
                    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                }
            )


def _age_sec(value: str, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if now.tzinfo is None:
        now = now.replace(tzinfo=parsed.tzinfo)
    return max(0.0, (now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
