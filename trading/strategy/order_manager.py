from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from trading.broker.command_queue import CommandPriority
from trading.broker.models import GatewayCommand, new_message_id
from trading.strategy.candidate_fsm import CandidateBlockingStage, CandidateFsmService, CandidateReasonCode
from trading.strategy.kill_switch import OrderKillSwitchManager
from trading.strategy.order_models import (
    ManagedOrder,
    ManagedOrderIntent,
    ManagedOrderStatus,
    OrderExecutionReconcileResult,
    OrderIntentSource,
    OrderIntentStatus,
    OrderKillSwitchState,
    OrderManagerConfig,
    OrderManagerSnapshot,
    OrderRiskDecision,
    OrderRiskResult,
    OrderSide,
    UnfilledCancelDecision,
)
from trading.strategy.order_reconcile import ManagedOrderReconciler
from trading.strategy.order_risk import OrderRiskManager, broker_environment_state
from trading.strategy.unfilled_cancel import UnfilledCancelScheduler


ENTRY_ALLOWED_ROLES = {"LEADER", "CO_LEADER"}
ENTRY_ALLOWED_MARKET_ACTIONS = {"ALLOW_NORMAL", "ALLOW_REDUCED"}
ENTRY_ALLOWED_PRICE_LOCATIONS = {"GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM"}
ENTRY_ALLOWED_CANDIDATE_STATES = {"WATCHING", "READY", "SETUP_READY", "TIMING_READY"}
EXIT_ALLOWED_STATUSES = {"SCALE_OUT", "EXIT_NOW"}
EXIT_ALLOWED_POSITION_SOURCES = {"", "LIVE_SIM", "LIVE_SIM_OBSERVED", "DRY_RUN_TO_LIVE_SIM", "VIRTUAL"}


class OrderManagerRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        gateway_state: Any,
        market_data: Any = None,
        config: OrderManagerConfig | None = None,
        clock: Any = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.market_data = market_data
        self.config = config or OrderManagerConfig.from_env()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.risk_manager = OrderRiskManager(db, gateway_state, self.config)
        self.kill_switch = OrderKillSwitchManager(db, self.config)
        self.cancel_scheduler = UnfilledCancelScheduler(db, gateway_state, self.config)
        self.reconciler = ManagedOrderReconciler(db)
        self.fsm = CandidateFsmService(db, clock=lambda: self.clock())
        self.last_summary: dict[str, Any] = {}
        self._last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = _clean_time(now or self.clock())
        if self._last_run_at is not None and self.config.run_interval_sec > 0:
            if (current - self._last_run_at).total_seconds() < self.config.run_interval_sec:
                return dict(self.last_summary or self._snapshot(current).to_dict())
        self._last_run_at = current
        summary = self.run(now=current)
        self.last_summary = summary
        return summary

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        current = _clean_time(now or self.clock())
        trade_date = current.date().isoformat()
        warnings: list[str] = []
        created_intents = 0
        queued_commands = 0
        rejected_intents = 0

        if not self.config.enabled:
            snapshot = self._snapshot(current, warnings=("ORDER_MANAGER_DISABLED",))
            self.last_summary = snapshot.to_dict()
            return dict(self.last_summary)

        kill_state = self.kill_switch.evaluate(trade_date=trade_date, now=current)
        reconcile_summary = self.reconcile_open_orders(now=current)
        if not self.config.intent_enabled:
            snapshot = self._snapshot(
                current,
                reconcile_required_count=int(reconcile_summary.get("reconcile_required_count") or 0),
                warnings=tuple(_dedupe(["ORDER_INTENT_DISABLED", *list(reconcile_summary.get("warnings") or [])])),
            )
            self.last_summary = snapshot.to_dict()
            return dict(self.last_summary)
        for intent in self._entry_intents(trade_date=trade_date, now=current):
            result = self._process_intent(intent, now=current, kill_switch_state=str(kill_state.get("state") or ""))
            created_intents += int(result.get("created_intent", 0))
            queued_commands += int(result.get("queued_command", 0))
            rejected_intents += int(result.get("rejected_intent", 0))
            warnings.extend(result.get("warnings") or [])

        for intent in self._exit_intents(trade_date=trade_date, now=current):
            result = self._process_intent(intent, now=current, kill_switch_state=str(kill_state.get("state") or ""))
            created_intents += int(result.get("created_intent", 0))
            queued_commands += int(result.get("queued_command", 0))
            rejected_intents += int(result.get("rejected_intent", 0))
            warnings.extend(result.get("warnings") or [])

        cancel_summary = self.cancel_scheduler.run_if_due(current)
        queued_commands += int(cancel_summary.get("queued_cancel_count") or 0)
        snapshot = self._snapshot(
            current,
            created_intent_count=created_intents,
            queued_command_count=queued_commands,
            rejected_intent_count=rejected_intents,
            risk_approved_count=sum(1 for item in self.db.list_managed_order_intents(trade_date=trade_date, limit=500) if str(item.get("status") or "") in {OrderIntentStatus.RISK_APPROVED.value, OrderIntentStatus.LOCAL_ORDER_CREATED.value, OrderIntentStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value, OrderIntentStatus.COMMAND_QUEUED.value}),
            risk_rejected_count=rejected_intents,
            local_order_created_count=sum(1 for item in self.db.list_managed_orders(trade_date=trade_date, limit=500) if str(item.get("status") or "") in {ManagedOrderStatus.LOCAL_ORDER_CREATED.value, ManagedOrderStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value, ManagedOrderStatus.QUEUED_TO_GATEWAY.value}),
            command_blocked_observe_only_count=sum(1 for item in self.db.list_managed_orders(trade_date=trade_date, limit=500) if str(item.get("status") or "") == ManagedOrderStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value),
            reconcile_required_count=int(reconcile_summary.get("reconcile_required_count") or 0),
            warnings=tuple(_dedupe([*warnings, *list(reconcile_summary.get("warnings") or [])])),
        )
        self.last_summary = snapshot.to_dict()
        return dict(self.last_summary)

    def _process_intent(self, intent: ManagedOrderIntent, *, now: datetime, kill_switch_state: str) -> dict[str, Any]:
        if self._intent_exists(intent.idempotency_key):
            return {"created_intent": 0, "queued_command": 0, "rejected_intent": 0, "warnings": ["DUPLICATE_INTENT_SKIPPED"]}

        saved_intent = self.db.save_managed_order_intent(intent.to_dict())
        risk = self.risk_manager.evaluate(
            {**intent.to_dict(), "intent_id": saved_intent.get("id")},
            now=now,
            kill_switch_state=kill_switch_state,
        )
        self._save_risk_decision(saved_intent, risk, now)
        if not risk.approved:
            self.db.save_managed_order_intent(
                {
                    **saved_intent,
                    "status": OrderIntentStatus.RISK_REJECTED.value,
                    "details": {**dict(saved_intent.get("details") or {}), "risk": risk.to_dict()},
                }
            )
            self._append_event(
                None,
                saved_intent.get("id"),
                "risk_rejected",
                status_from=OrderIntentStatus.CREATED.value,
                status_to=OrderIntentStatus.RISK_REJECTED.value,
                payload=risk.to_dict(),
                message=",".join(risk.reason_codes),
            )
            self._apply_candidate_fsm_risk(saved_intent, risk, now=now)
            return {"created_intent": 1, "queued_command": 0, "rejected_intent": 1, "warnings": list(risk.reason_codes)}

        saved_intent = self.db.save_managed_order_intent(
            {
                **saved_intent,
                "status": OrderIntentStatus.RISK_APPROVED.value,
                "details": {**dict(saved_intent.get("details") or {}), "risk": risk.to_dict()},
            }
        )
        if not self.config.create_local_order:
            self._append_event(
                None,
                saved_intent.get("id"),
                "local_order_blocked_config",
                status_from=OrderIntentStatus.RISK_APPROVED.value,
                status_to=OrderIntentStatus.RISK_APPROVED.value,
                payload={"reason": "LOCAL_ORDER_CREATION_DISABLED"},
            )
            return {"created_intent": 1, "queued_command": 0, "rejected_intent": 0, "warnings": ["LOCAL_ORDER_CREATION_DISABLED"]}
        order = self._persist_local_order(saved_intent, now=now)
        self.db.save_managed_order_intent({**saved_intent, "status": OrderIntentStatus.LOCAL_ORDER_CREATED.value})
        self._append_event(
            order.get("id"),
            saved_intent.get("id"),
            "local_order_created",
            status_from=OrderIntentStatus.RISK_APPROVED.value,
            status_to=ManagedOrderStatus.LOCAL_ORDER_CREATED.value,
            payload={"order": order},
        )
        guard_reasons = self._gateway_command_guard(order, risk)
        if guard_reasons:
            blocked_order = self.db.save_managed_order(
                {
                    **order,
                    "status": ManagedOrderStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value,
                    "updated_at": now.isoformat(),
                    "details": {
                        **dict(order.get("details") or {}),
                        "risk": risk.to_dict(),
                        "gateway_command_guard_reasons": guard_reasons,
                    },
                }
            )
            self.db.save_managed_order_intent({**saved_intent, "status": OrderIntentStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value})
            self._append_event(
                blocked_order.get("id"),
                saved_intent.get("id"),
                "command_blocked_observe_only",
                status_from=ManagedOrderStatus.LOCAL_ORDER_CREATED.value,
                status_to=ManagedOrderStatus.COMMAND_BLOCKED_OBSERVE_ONLY.value,
                payload={"risk": risk.to_dict(), "guard_reasons": guard_reasons},
                message=",".join(guard_reasons),
            )
            self._apply_candidate_fsm_order_block(saved_intent, guard_reasons, now=now)
            return {"created_intent": 1, "queued_command": 0, "rejected_intent": 0, "warnings": guard_reasons}
        command = self._command_for_order(order)
        enqueue = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.HIGH,
            ttl_sec=self.config.command_ttl_sec,
            max_attempts=self.config.command_max_attempts,
            metadata={
                "runtime": "REBOOT_LIVE_SIM",
                "managed_order_id": order.get("id"),
                "managed_intent_id": saved_intent.get("id"),
                "dedupe_key": intent.idempotency_key,
            },
            now=now,
        )
        if enqueue.accepted:
            self.db.save_managed_order(
                {
                    **order,
                    "status": ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
                    "command_id": command.command_id,
                    "sent_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "details": {
                        **dict(order.get("details") or {}),
                        "risk": risk.to_dict(),
                        "command": command.to_dict(),
                        "enqueue_result": enqueue.to_dict(),
                    },
                }
            )
            self.db.save_managed_order_intent({**saved_intent, "status": OrderIntentStatus.COMMAND_QUEUED.value})
            self._append_event(
                order.get("id"),
                saved_intent.get("id"),
                "command_queued",
                status_from=ManagedOrderStatus.PENDING_LOCAL.value,
                status_to=ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
                payload={"command": command.to_dict(), "enqueue_result": enqueue.to_dict()},
            )
            return {"created_intent": 1, "queued_command": 1, "rejected_intent": 0, "warnings": []}

        self.db.save_managed_order(
            {
                **order,
                "status": ManagedOrderStatus.REJECTED_BY_GATEWAY.value,
                "command_id": command.command_id,
                "updated_at": now.isoformat(),
                "details": {
                    **dict(order.get("details") or {}),
                    "command": command.to_dict(),
                    "enqueue_result": enqueue.to_dict(),
                },
            }
        )
        self.db.save_managed_order_intent({**saved_intent, "status": OrderIntentStatus.COMMAND_REJECTED.value})
        self._append_event(
            order.get("id"),
            saved_intent.get("id"),
            "command_enqueue_rejected",
            status_from=ManagedOrderStatus.PENDING_LOCAL.value,
            status_to=ManagedOrderStatus.REJECTED_BY_GATEWAY.value,
            payload={"command": command.to_dict(), "enqueue_result": enqueue.to_dict()},
            message=str(enqueue.reason or ""),
        )
        return {"created_intent": 1, "queued_command": 0, "rejected_intent": 1, "warnings": [str(enqueue.reason or "COMMAND_QUEUE_REJECTED")]}

    def _entry_intents(self, *, trade_date: str, now: datetime) -> list[ManagedOrderIntent]:
        if not self.config.intent_enabled:
            return []
        loader = getattr(self.db, "latest_entry_decisions", None)
        if not callable(loader):
            return []
        intents: list[ManagedOrderIntent] = []
        for decision in loader(trade_date=trade_date):
            if not self._entry_decision_allowed(decision, trade_date=trade_date):
                continue
            code = str(decision.get("code") or "")
            quantity = min(max(1, int(decision.get("quantity") or 1)), self.config.max_order_quantity)
            price, details = self._price_for_entry(decision, code)
            decision_id = decision.get("id")
            bucket = decision_id or int(now.timestamp() // max(1, self.config.cycle_bucket_sec))
            price_bucket = int((price or 0) // 10) * 10
            idempotency_key = f"buy:{trade_date}:{decision.get('candidate_id') or ''}:{code}:{bucket}:{price_bucket}"
            intents.append(
                ManagedOrderIntent(
                    trade_date=trade_date,
                    source=OrderIntentSource.REBOOT_ENTRY_ENGINE.value,
                    side=OrderSide.BUY.value,
                    code=code,
                    name=str(decision.get("name") or ""),
                    account=self._account(),
                    quantity=quantity,
                    price=price,
                    hoga=self.config.order_hoga,
                    idempotency_key=idempotency_key,
                    created_at=now.isoformat(),
                    candidate_id=decision.get("candidate_id"),
                    decision_id=decision_id,
                    theme_id=str(decision.get("theme_id") or ""),
                    theme_name=str(decision.get("theme_name") or ""),
                    reason="ENTRY_OBSERVE_READY",
                    details={
                        **details,
                        **dict(decision.get("details") or {}),
                        "decision": decision,
                        "calculated_at": decision.get("calculated_at") or "",
                        "market_status": decision.get("market_status") or "",
                        "market_action": decision.get("market_action") or "",
                        "stock_role": decision.get("stock_role") or "",
                        "price_location": decision.get("price_location") or "",
                        "theme_id": decision.get("theme_id") or "",
                        "theme_name": decision.get("theme_name") or "",
                    },
                )
            )
        return intents

    def _exit_intents(self, *, trade_date: str, now: datetime) -> list[ManagedOrderIntent]:
        if not self.config.intent_enabled:
            return []
        loader = getattr(self.db, "latest_exit_decisions_reboot", None)
        if not callable(loader):
            return []
        positions = self._latest_positions(trade_date)
        intents: list[ManagedOrderIntent] = []
        for decision in loader(trade_date=trade_date):
            if not self._exit_decision_allowed(decision, positions):
                continue
            code = str(decision.get("code") or "")
            position_id = str(decision.get("position_id") or "")
            position = positions.get(position_id, {})
            quantity = self._exit_quantity(decision, position)
            if quantity <= 0:
                continue
            price, details = self._price_for_exit(decision, code)
            exit_reason = str(decision.get("exit_reason") or "")
            bucket = int(now.timestamp() // max(1, self.config.cycle_bucket_sec))
            idempotency_key = f"reboot_live_sim_sell:{trade_date}:{position_id}:{exit_reason}:{bucket}"
            intents.append(
                ManagedOrderIntent(
                    trade_date=trade_date,
                    source=OrderIntentSource.REBOOT_EXIT_ENGINE.value,
                    side=OrderSide.SELL.value,
                    code=code,
                    name=str(decision.get("name") or ""),
                    account=self._account(),
                    quantity=quantity,
                    price=price,
                    hoga=str(decision.get("hoga_hint") or self.config.order_hoga),
                    idempotency_key=idempotency_key,
                    created_at=now.isoformat(),
                    candidate_id=decision.get("candidate_id"),
                    position_id=position_id,
                    decision_id=decision.get("id"),
                    theme_id=str(position.get("theme_id") or dict(position.get("details") or {}).get("theme_id") or ""),
                    theme_name=str(position.get("theme_name") or dict(position.get("details") or {}).get("theme_name") or ""),
                    reason=exit_reason,
                    details={
                        **details,
                        **dict(decision.get("details") or {}),
                        "decision": decision,
                        "position": position,
                        "calculated_at": decision.get("calculated_at") or "",
                        "exit_status": decision.get("exit_status") or "",
                        "exit_reason": exit_reason,
                    },
                )
            )
        return intents

    def _entry_decision_allowed(self, decision: dict[str, Any], *, trade_date: str) -> bool:
        if str(decision.get("entry_status") or "") != "OBSERVE_READY":
            return False
        if str(decision.get("market_action") or "") not in ENTRY_ALLOWED_MARKET_ACTIONS:
            return False
        if str(decision.get("stock_role") or "") not in ENTRY_ALLOWED_ROLES:
            return False
        if str(decision.get("price_location") or "") not in ENTRY_ALLOWED_PRICE_LOCATIONS:
            return False
        if "OVERHEATED" in {str(item) for item in list(decision.get("reason_codes") or [])}:
            return False
        candidate_id = decision.get("candidate_id")
        if candidate_id:
            candidate = self._candidate_by_code(trade_date, str(decision.get("code") or ""))
            if candidate is not None:
                state = getattr(getattr(candidate, "state", ""), "value", str(getattr(candidate, "state", "")))
                if state and state not in ENTRY_ALLOWED_CANDIDATE_STATES:
                    return False
                fsm = dict(dict(candidate.metadata or {}).get("candidate_fsm") or {})
                if fsm:
                    if str(fsm.get("v2_state") or "") != "TIMING_READY":
                        return False
                    if str(fsm.get("blocking_stage") or "") not in {"", "NONE"}:
                        return False
                    if str(fsm.get("price_source") or "REALTIME").upper() == "TR_BACKFILL":
                        return False
                    if str(fsm.get("latest_tick_fresh") or "").lower() == "false":
                        return False
        return True

    def _exit_decision_allowed(self, decision: dict[str, Any], positions: dict[str, dict[str, Any]]) -> bool:
        if str(decision.get("exit_status") or "") not in EXIT_ALLOWED_STATUSES:
            return False
        if not bool(decision.get("dry_run_sell_intent_allowed") or decision.get("live_sim_intent_allowed") or decision.get("live_order_allowed")):
            return False
        position_id = str(decision.get("position_id") or "")
        position = positions.get(position_id, {})
        source = str(position.get("source_type") or dict(position.get("details") or {}).get("source") or "").upper()
        if source not in EXIT_ALLOWED_POSITION_SOURCES:
            return False
        remaining = int(position.get("remaining_quantity") or position.get("current_qty") or decision.get("quantity") or 0)
        return remaining > 0 or int(decision.get("quantity") or 0) > 0

    def _price_for_entry(self, decision: dict[str, Any], code: str) -> tuple[int, dict[str, Any]]:
        tick = self._latest_tick(code)
        price = int(decision.get("limit_price_hint") or 0)
        if price <= 0 and tick:
            price = int(getattr(tick, "best_ask", 0) or getattr(tick, "price", 0) or 0)
        if price <= 0:
            price = int(decision.get("current_price") or 0)
        return price, self._tick_details(tick)

    def _price_for_exit(self, decision: dict[str, Any], code: str) -> tuple[int, dict[str, Any]]:
        tick = self._latest_tick(code)
        price = int(decision.get("price_hint") or 0)
        if price <= 0 and tick:
            price = int(getattr(tick, "best_bid", 0) or getattr(tick, "price", 0) or 0)
        if price <= 0:
            price = int(decision.get("current_price") or 0)
        return price, self._tick_details(tick)

    def _tick_details(self, tick: Any) -> dict[str, Any]:
        if tick is None:
            return {}
        return {
            "current_price": int(getattr(tick, "price", 0) or 0),
            "best_ask": int(getattr(tick, "best_ask", 0) or 0),
            "best_bid": int(getattr(tick, "best_bid", 0) or 0),
            "spread_ticks": int(getattr(tick, "spread_ticks", 0) or 0),
            "execution_strength": float(getattr(tick, "execution_strength", 0.0) or 0.0),
            "trade_value": float(getattr(tick, "trade_value", 0.0) or 0.0),
            "last_tick_at": getattr(tick, "timestamp", None).isoformat() if getattr(tick, "timestamp", None) else "",
            **dict(getattr(tick, "metadata", {}) or {}),
        }

    def _latest_tick(self, code: str) -> Any:
        if self.market_data is None:
            return None
        latest = getattr(self.market_data, "latest_tick", None)
        return latest(code) if callable(latest) else None

    def _latest_positions(self, trade_date: str) -> dict[str, dict[str, Any]]:
        positions: dict[str, dict[str, Any]] = {}
        loader = getattr(self.db, "latest_position_runtime_snapshots", None)
        if callable(loader):
            for row in loader(trade_date=trade_date):
                positions[str(row.get("position_id") or "")] = row
        lister = getattr(self.db, "list_live_sim_positions", None)
        if callable(lister):
            for row in lister(status="OPEN", limit=1000):
                position_id = str(row.get("position_id") or "")
                positions.setdefault(
                    position_id,
                    {
                        "position_id": position_id,
                        "code": row.get("code") or "",
                        "remaining_quantity": int(row.get("current_qty") or 0),
                        "current_qty": int(row.get("current_qty") or 0),
                        "source_type": "LIVE_SIM",
                        "details": row.get("details") or {},
                    },
                )
        return positions

    def _exit_quantity(self, decision: dict[str, Any], position: dict[str, Any]) -> int:
        remaining = int(position.get("remaining_quantity") or position.get("current_qty") or 0)
        decision_qty = int(decision.get("quantity") or 0)
        if str(decision.get("exit_status") or "") == "EXIT_NOW" and remaining > 0:
            return remaining
        if remaining > 0:
            return min(decision_qty or remaining, remaining)
        return decision_qty

    def _persist_local_order(self, intent: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        order = ManagedOrder(
            intent_id=int(intent.get("id") or intent.get("intent_id") or 0),
            trade_date=str(intent.get("trade_date") or now.date().isoformat()),
            side=str(intent.get("side") or ""),
            code=str(intent.get("code") or ""),
            account=str(intent.get("account") or ""),
            quantity=int(intent.get("quantity") or 0),
            price=int(intent.get("price") or 0),
            hoga=str(intent.get("hoga") or self.config.order_hoga),
            idempotency_key=str(intent.get("idempotency_key") or ""),
            created_at=now.isoformat(),
            source=str(intent.get("source") or ""),
            candidate_id=int(intent.get("candidate_id") or 0) or None,
            position_id=str(intent.get("position_id") or ""),
            remaining_quantity=int(intent.get("quantity") or 0),
            cancel_after_sec=self.config.cancel_unfilled_after_sec,
            details=dict(intent.get("details") or {}),
        )
        return self.db.save_managed_order(order.to_dict())

    def _gateway_command_guard(self, order: dict[str, Any], risk: OrderRiskDecision) -> list[str]:
        reasons: list[str] = []
        if not self.config.enqueue_gateway_command:
            reasons.append("GATEWAY_COMMAND_ENQUEUE_DISABLED")
        if self.config.observe_only:
            reasons.append("ORDER_MANAGER_OBSERVE_ONLY")
        if not self.config.send_order_allowed:
            reasons.append("SEND_ORDER_NOT_ALLOWED")
        if self.config.mode != "LIVE_SIM":
            reasons.append("ORDER_MANAGER_NOT_LIVE_SIM_MODE")
        if not self.config.allow_live_sim_orders:
            reasons.append("LIVE_SIM_FLAG_DISABLED")
        broker = self.gateway_state.snapshot().to_dict()
        if broker_environment_state(broker) != "SIMULATION":
            reasons.append("SIMULATION_BROKER_REQUIRED")
        if self.config.block_real_broker and broker_environment_state(broker) == "REAL":
            reasons.append("REAL_BROKER_BLOCKED")
        if not risk.approved:
            reasons.append("RISK_NOT_APPROVED")
        if self.kill_switch.latest_state(trade_date=str(order.get("trade_date") or "")).get("state") == OrderKillSwitchState.KILL_SWITCH_ACTIVE.value:
            reasons.append("KILL_SWITCH_ACTIVE")
        return _dedupe(reasons)

    def _command_for_order(self, order: dict[str, Any]) -> GatewayCommand:
        side = str(order.get("side") or "").upper()
        code = str(order.get("code") or "")
        order_type = 1 if side == OrderSide.BUY.value else 2
        strategy = "reboot_v2_entry" if side == OrderSide.BUY.value else "reboot_v2_exit"
        tag = f"REBOOT_LIVE_SIM_BUY_{code}" if side == OrderSide.BUY.value else f"REBOOT_LIVE_SIM_SELL_{code}"
        command_id = new_message_id("cmd_order")
        return GatewayCommand(
            type="send_order",
            command_id=command_id,
            idempotency_key=str(order.get("idempotency_key") or ""),
            payload={
                "account": order.get("account") or "",
                "code": code,
                "quantity": int(order.get("quantity") or 0),
                "price": int(order.get("price") or 0),
                "side": side,
                "order_type": order_type,
                "hoga": order.get("hoga") or self.config.order_hoga,
                "tag": tag,
                "strategy": strategy,
                "order_mode": "LIVE_SIM",
                "broker_env": "SIMULATION",
                "managed_order_id": order.get("id"),
                "managed_intent_id": order.get("intent_id"),
                "candidate_id": order.get("candidate_id"),
                "position_id": order.get("position_id"),
                "idempotency_key": order.get("idempotency_key") or "",
                "metadata": {
                    "managed_order_id": order.get("id"),
                    "managed_intent_id": order.get("intent_id"),
                    "source": order.get("source") or "",
                },
            },
        )

    def _save_risk_decision(self, intent: dict[str, Any], risk: OrderRiskDecision, now: datetime) -> None:
        saver = getattr(self.db, "save_order_risk_decision", None)
        if callable(saver):
            saver(
                {
                    "intent_id": intent.get("id"),
                    "trade_date": intent.get("trade_date") or now.date().isoformat(),
                    "created_at": now.isoformat(),
                    "side": intent.get("side") or "",
                    "code": intent.get("code") or "",
                    "decision": risk.decision,
                    "reason_codes": list(risk.reason_codes),
                    "operator_message_ko": risk.operator_message_ko,
                    "idempotency_key": intent.get("idempotency_key") or "",
                    "details": risk.details,
                }
            )

    def _snapshot(
        self,
        now: datetime,
        *,
        created_intent_count: int = 0,
        risk_approved_count: int = 0,
        risk_rejected_count: int = 0,
        local_order_created_count: int = 0,
        command_blocked_observe_only_count: int = 0,
        queued_command_count: int = 0,
        reconcile_required_count: int = 0,
        rejected_intent_count: int = 0,
        warnings: tuple[str, ...] = (),
    ) -> OrderManagerSnapshot:
        broker = self.gateway_state.snapshot().to_dict()
        summary = self.db.managed_order_summary(trade_date=now.date().isoformat()) if hasattr(self.db, "managed_order_summary") else {}
        kill_state = self.kill_switch.latest_state(trade_date=now.date().isoformat())
        recent = self.db.list_managed_orders(trade_date=now.date().isoformat(), limit=10) if hasattr(self.db, "list_managed_orders") else []
        env = broker_environment_state(broker)
        account = str(broker.get("account") or "")
        whitelist = tuple(self.config.live_sim_account_whitelist or ())
        status = "DISABLED" if not self.config.enabled else "READY"
        if self.config.enabled and warnings:
            status = "WARN"
        return OrderManagerSnapshot(
            status=status,
            mode=self.config.mode,
            enabled=self.config.enabled,
            observe_only=self.config.observe_only,
            intent_enabled=self.config.intent_enabled,
            local_order_enabled=self.config.create_local_order,
            gateway_command_enqueue_enabled=self.config.enqueue_gateway_command,
            send_order_allowed=self.config.send_order_allowed,
            live_sim_orders_allowed=bool(
                self.config.enabled
                and self.config.mode == "LIVE_SIM"
                and self.config.allow_live_sim_orders
                and not self.config.observe_only
                and self.config.enqueue_gateway_command
                and self.config.send_order_allowed
                and env == "SIMULATION"
            ),
            broker_env=env,
            account=account,
            account_whitelisted=bool(account and (not whitelist or account in whitelist)),
            risk_state=str(summary.get("risk_state") or "READY"),
            kill_switch_state=str(kill_state.get("state") or OrderKillSwitchState.NORMAL.value),
            today_buy_order_count=int(summary.get("today_buy_order_count") or 0),
            today_sell_order_count=int(summary.get("today_sell_order_count") or 0),
            open_order_count=int(summary.get("open_order_count") or 0),
            pending_cancel_count=int(summary.get("pending_cancel_count") or 0),
            rejected_order_count=int(summary.get("rejected_order_count") or 0),
            created_intent_count=created_intent_count,
            risk_approved_count=risk_approved_count,
            risk_rejected_count=risk_rejected_count,
            local_order_created_count=local_order_created_count,
            command_blocked_observe_only_count=command_blocked_observe_only_count,
            queued_command_count=queued_command_count,
            reconcile_required_count=reconcile_required_count,
            stop_new_buy=str(kill_state.get("state") or "") in {OrderKillSwitchState.STOP_NEW_BUY.value, OrderKillSwitchState.KILL_SWITCH_ACTIVE.value},
            reduce_only=str(kill_state.get("state") or "") == OrderKillSwitchState.REDUCE_ONLY.value,
            rejected_intent_count=rejected_intent_count,
            last_order_at=str(summary.get("last_order_at") or ""),
            last_reject_reason=str(summary.get("last_reject_reason") or ""),
            warnings=tuple(warnings),
            recent_orders=tuple(recent),
        )

    def apply_order_ack(self, event: Any) -> dict[str, Any]:
        payload = dict(getattr(event, "payload", event) or {})
        result = self.reconciler.handle_command_ack(payload)
        if not result.get("matched"):
            self._record_reconcile_required("ORDER_ACK_UNMATCHED", payload)
        return result

    def apply_order_reject(self, event: Any) -> dict[str, Any]:
        payload = dict(getattr(event, "payload", event) or {})
        payload["status"] = "REJECTED"
        return self.apply_order_ack(payload)

    def apply_order_fill(self, event: Any) -> dict[str, Any]:
        payload = dict(getattr(event, "payload", event) or {})
        result = self.reconciler.handle_execution(payload)
        if not result.matched:
            self._record_reconcile_required("FILL_WITHOUT_LOCAL_ORDER", payload)
        return result.to_dict()

    def apply_balance_snapshot(self, event: Any) -> dict[str, Any]:
        payload = dict(getattr(event, "payload", event) or {})
        if bool(payload.get("mismatch") or payload.get("local_position_mismatch")):
            self._record_reconcile_required("BALANCE_MISMATCH", payload)
            return {"matched": False, "status": "RECONCILE_REQUIRED", "reason": "BALANCE_MISMATCH"}
        return {"matched": True, "status": "OK"}

    def apply_order_status_snapshot(self, event: Any) -> dict[str, Any]:
        payload = dict(getattr(event, "payload", event) or {})
        return {"matched": True, "status": "OBSERVED", "payload": payload}

    def reconcile_open_orders(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _clean_time(now or self.clock())
        rows = self.db.list_managed_orders(
            status=[ManagedOrderStatus.QUEUED_TO_GATEWAY.value],
            limit=500,
        ) if hasattr(self.db, "list_managed_orders") else []
        count = 0
        for order in rows:
            sent_at = str(order.get("sent_at") or order.get("created_at") or "")
            if _age_sec(sent_at, current) <= self.config.ack_timeout_sec:
                continue
            self.db.save_managed_order({**order, "status": ManagedOrderStatus.RECONCILE_REQUIRED.value, "updated_at": current.isoformat()})
            self._append_event(
                order.get("id"),
                order.get("intent_id"),
                "ack_timeout_reconcile_required",
                status_from=str(order.get("status") or ""),
                status_to=ManagedOrderStatus.RECONCILE_REQUIRED.value,
                payload={"ack_timeout_sec": self.config.ack_timeout_sec},
            )
            self._record_reconcile_required("ORDER_ACK_TIMEOUT", order)
            count += 1
        return {"status": "OK", "reconcile_required_count": count, "warnings": ["RECONCILE_REQUIRED"] if count else []}

    def reconcile_positions(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {"status": "SKELETON", "reconcile_required_count": 0}

    def _intent_exists(self, idempotency_key: str) -> bool:
        finder = getattr(self.db, "find_managed_order_intent_by_idempotency", None)
        return bool(finder(idempotency_key)) if callable(finder) else False

    def _candidate_by_code(self, trade_date: str, code: str) -> Any:
        loader = getattr(self.db, "load_candidate", None)
        return loader(trade_date, code) if callable(loader) else None

    def _account(self) -> str:
        try:
            return str(self.gateway_state.snapshot().to_dict().get("account") or "")
        except Exception:
            return ""

    def _append_event(
        self,
        order_id: Any,
        intent_id: Any,
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
                    "order_id": order_id,
                    "intent_id": intent_id,
                    "event_type": event_type,
                    "status_from": status_from,
                    "status_to": status_to,
                    "message": message,
                    "payload": payload,
                    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                }
            )

    def _record_reconcile_required(self, reason: str, payload: dict[str, Any]) -> None:
        trade_date = str(payload.get("trade_date") or datetime.now(timezone.utc).date().isoformat())
        saver = getattr(self.db, "save_order_kill_switch_state", None)
        if callable(saver):
            saver(
                {
                    "trade_date": trade_date,
                    "state": OrderKillSwitchState.STOP_NEW_BUY.value,
                    "reason_codes": [reason],
                    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "details": {"payload": payload},
                }
            )

    def _apply_candidate_fsm_risk(self, intent: dict[str, Any], risk: OrderRiskDecision, *, now: datetime) -> None:
        candidate = self._candidate_by_code(str(intent.get("trade_date") or now.date().isoformat()), str(intent.get("code") or ""))
        if candidate is None:
            return
        reason = CandidateReasonCode.ORDER_RISK_REJECTED.value
        if risk.reason_codes:
            reason = str(risk.reason_codes[0])
        self.fsm.apply_blocking_reason(
            candidate,
            CandidateBlockingStage.RISK,
            reason,
            details={"intent": intent, "risk": risk.to_dict()},
            source_event_type="order_risk",
            source_component="OrderManager",
        )
        self.db.save_candidate(candidate)

    def _apply_candidate_fsm_order_block(self, intent: dict[str, Any], reasons: list[str], *, now: datetime) -> None:
        candidate = self._candidate_by_code(str(intent.get("trade_date") or now.date().isoformat()), str(intent.get("code") or ""))
        if candidate is None:
            return
        self.fsm.apply_blocking_reason(
            candidate,
            CandidateBlockingStage.ORDER,
            CandidateReasonCode.ORDER_MANAGER_OBSERVE_ONLY.value,
            details={"intent": intent, "guard_reasons": reasons},
            source_event_type="order_command_guard",
            source_component="OrderManager",
        )
        self.db.save_candidate(candidate)


def order_manager_dashboard_section(db: Any, *, gateway_state: Any = None, trade_date: str | None = None) -> dict[str, Any]:
    trade_date = trade_date or datetime.now().date().isoformat()
    config = OrderManagerConfig.from_env()
    summary = db.managed_order_summary(trade_date=trade_date) if hasattr(db, "managed_order_summary") else {}
    rows = db.list_managed_orders(trade_date=trade_date, limit=12) if hasattr(db, "list_managed_orders") else []
    kill = db.latest_order_kill_switch_state(trade_date=trade_date) if hasattr(db, "latest_order_kill_switch_state") else {}
    broker_payload: dict[str, Any] = {}
    if gateway_state is not None:
        try:
            broker_payload = gateway_state.snapshot().to_dict()
        except Exception:
            broker_payload = {}
    env = broker_environment_state(broker_payload)
    account = str(broker_payload.get("account") or "")
    whitelist = tuple(config.live_sim_account_whitelist or ())
    reasons = Counter()
    for row in rows:
        for reason in list(dict(row.get("details") or {}).get("risk", {}).get("reason_codes") or []):
            reasons[str(reason)] += 1
    return {
        "status": "READY" if config.enabled else "DISABLED",
        "mode": config.mode,
        "enabled": config.enabled,
        "observe_only": config.observe_only,
        "intent_enabled": config.intent_enabled,
        "local_order_enabled": config.create_local_order,
        "gateway_command_enqueue_enabled": config.enqueue_gateway_command,
        "send_order_allowed": config.send_order_allowed,
        "live_sim_orders_allowed": bool(config.enabled and config.mode == "LIVE_SIM" and config.allow_live_sim_orders and not config.observe_only and config.enqueue_gateway_command and config.send_order_allowed and env == "SIMULATION"),
        "broker_env": env,
        "account": account,
        "account_whitelisted": bool(account and (not whitelist or account in whitelist)),
        "risk_state": summary.get("risk_state", "READY"),
        "kill_switch_state": kill.get("state", OrderKillSwitchState.NORMAL.value),
        "today_buy_order_count": int(summary.get("today_buy_order_count") or 0),
        "today_sell_order_count": int(summary.get("today_sell_order_count") or 0),
        "open_order_count": int(summary.get("open_order_count") or 0),
        "pending_cancel_count": int(summary.get("pending_cancel_count") or 0),
        "rejected_order_count": int(summary.get("rejected_order_count") or 0),
        "last_order_at": summary.get("last_order_at", ""),
        "last_reject_reason": summary.get("last_reject_reason", ""),
        "top_wait_or_block_reasons": [{"reason": key, "count": count} for key, count in reasons.most_common(10)],
        "managed_orders": rows,
        "warnings": _dashboard_warnings(config, env, broker_payload),
    }


def _dashboard_warnings(config: OrderManagerConfig, env: str, broker: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if config.observe_only:
        warnings.append("ORDER_MANAGER_OBSERVE_ONLY")
    if config.mode == "LIVE_SIM" and env != "SIMULATION":
        warnings.append("LIVE_SIM_BROKER_GUARD_NOT_PASSED")
    if config.mode == "LIVE_SIM" and not bool(broker.get("heartbeat_ok")):
        warnings.append("GATEWAY_HEARTBEAT_STALE")
    return warnings


def _clean_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc, microsecond=0)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _age_sec(value: str, now: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo or timezone.utc)
    return max(0.0, (_clean_time(now) - _clean_time(parsed)).total_seconds())


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


__all__ = [
    "ManagedOrder",
    "ManagedOrderIntent",
    "ManagedOrderStatus",
    "OrderExecutionReconcileResult",
    "OrderIntentSource",
    "OrderIntentStatus",
    "OrderManagerConfig",
    "OrderManagerRuntimePipeline",
    "OrderManagerSnapshot",
    "OrderRiskDecision",
    "OrderSide",
    "UnfilledCancelDecision",
    "order_manager_dashboard_section",
]
