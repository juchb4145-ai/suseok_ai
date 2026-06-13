from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from storage.db import TradingDatabase
from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerExecutionEvent, BrokerOrderRequest, GatewayCommand, new_message_id, utc_timestamp
from trading.risk.safety_guard import OrderCommandSafetyGuard, OrderSafetyConfig, dedupe_key_for_order_request
from trading_app.dependencies import CoreSettings
from trading_app.schemas import OrderEnqueueRequest


DRY_RUN_ACCEPTED = "DRY_RUN_ACCEPTED"
DRY_RUN_REJECTED = "DRY_RUN_REJECTED"
DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class RuntimeOrderIntentRequest:
    source: str = "strategy_runtime"
    dry_run: bool = True
    account: str = ""
    code: str = ""
    side: str = "buy"
    quantity: int = 0
    price: int = 0
    order_type: int = 1
    hoga: str = "00"
    tag: str = ""
    strategy_name: str = ""
    candidate_id: Optional[int] = None
    entry_plan_id: Optional[int] = None
    virtual_order_id: Optional[int] = None
    virtual_position_id: Optional[int] = None
    trade_review_id: Optional[int] = None
    leg_index: Optional[int] = None
    entry_type: str = ""
    reason: str = ""
    gate_reason: str = ""
    gate_status: str = ""
    gate_score: Optional[float] = None
    hybrid_score: Optional[float] = None
    theme_name: str = ""
    theme_score: Optional[float] = None
    runtime_cycle_id: Optional[int] = None
    runtime_cycle_at: str = ""
    idempotency_key: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    order_phase: str = "entry"
    exit_decision_id: Optional[int] = None
    exit_decision_type: str = ""
    exit_reason: str = ""
    exit_percent: Optional[float] = None
    exit_quantity: Optional[int] = None
    remaining_quantity: Optional[int] = None
    position_entry_price: Optional[int] = None
    position_quantity: Optional[int] = None
    position_opened_at: str = ""
    position_closed_at: str = ""
    position_max_return_pct: Optional[float] = None
    position_max_drawdown_pct: Optional[float] = None
    realized_return_pct: Optional[float] = None
    virtual_exit_price: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrderEnqueueResult:
    accepted: bool
    mode: str
    dry_run: bool
    intent_id: str = ""
    command_id: str = ""
    idempotency_key: str = ""
    dedupe_key: str = ""
    duplicate_of: str = ""
    status: str = ""
    reason: str = ""
    safety: dict[str, Any] = field(default_factory=dict)
    live_safety: dict[str, Any] = field(default_factory=dict)
    live_would_pass: Optional[bool] = None
    live_reject_reason: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] | None = None
    command: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "mode": self.mode,
            "dry_run": bool(self.dry_run),
            "intent_id": self.intent_id,
            "command_id": self.command_id,
            "idempotency_key": self.idempotency_key,
            "dedupe_key": self.dedupe_key,
            "duplicate_of": self.duplicate_of,
            "status": self.status,
            "reason": self.reason,
            "safety": self.safety,
            "safety_checks": self.safety,
            "live_safety": self.live_safety,
            "live_would_pass": self.live_would_pass,
            "live_reject_reason": self.live_reject_reason,
            "request": self.request,
            "response": self.response,
            "record": self.record,
            "command": self.command,
        }


class OrderEnqueueService:
    def __init__(
        self,
        *,
        settings: CoreSettings,
        gateway_state: GatewayStateStore,
        db_path: str | Path,
        clock=None,
    ) -> None:
        self.settings = settings
        self.gateway_state = gateway_state
        self.db_path = Path(db_path)
        self.clock = clock or utc_timestamp

    def enqueue_order(self, request: OrderEnqueueRequest | RuntimeOrderIntentRequest) -> OrderEnqueueResult:
        if isinstance(request, RuntimeOrderIntentRequest):
            return self.enqueue_dry_run_order(request)
        requested_mode = "DRY_RUN" if bool(request.dry_run) else self.settings.mode
        broker_request = self._broker_request_from_api(request, requested_mode=requested_mode)
        dedupe_key = dedupe_key_for_order_request(broker_request)
        if requested_mode == "DRY_RUN":
            intent = RuntimeOrderIntentRequest(
                source="api",
                dry_run=True,
                account=broker_request.account,
                code=broker_request.code,
                side=broker_request.side,
                quantity=broker_request.quantity,
                price=broker_request.price,
                order_type=broker_request.order_type,
                hoga=broker_request.hoga,
                tag=broker_request.tag,
                strategy_name=request.strategy_name,
                candidate_id=request.candidate_id,
                reason=request.reason,
                idempotency_key=broker_request.idempotency_key,
                order_phase="exit" if broker_request.side == "sell" else "entry",
                metadata={"api": "/api/orders/enqueue"},
            )
            return self.enqueue_dry_run_order(intent)

        gateway_status_payload = self.gateway_state.snapshot().to_dict()
        duplicate = self.gateway_state.has_duplicate(dedupe_key)
        duplicate_of = self.gateway_state.duplicate_of(dedupe_key) if duplicate else ""
        safety = self._live_guard(requested_mode).validate(
            broker_request,
            gateway_status=gateway_status_payload,
            existing_order_command_count=self._order_command_count(
                broker_request.code,
                broker_request.side,
                broker_request.tag,
                order_type=broker_request.order_type,
            ),
            duplicate=duplicate,
        )
        if requested_mode == "OBSERVE":
            return OrderEnqueueResult(
                accepted=False,
                mode=requested_mode,
                dry_run=False,
                idempotency_key=broker_request.idempotency_key,
                dedupe_key=dedupe_key,
                duplicate_of=duplicate_of,
                status="OBSERVE_ONLY",
                reason="OBSERVE_MODE",
                safety=safety.to_dict(),
                request=broker_request.to_dict(),
            )
        if not safety.ok:
            return OrderEnqueueResult(
                accepted=False,
                mode=requested_mode,
                dry_run=False,
                idempotency_key=broker_request.idempotency_key,
                dedupe_key=dedupe_key,
                duplicate_of=duplicate_of,
                status="REJECTED",
                reason=safety.reason,
                safety=safety.to_dict(),
                request=broker_request.to_dict(),
            )

        command = GatewayCommand(
            type="send_order",
            command_id=new_message_id("cmd_order"),
            idempotency_key=broker_request.idempotency_key,
            payload={**broker_request.to_dict(), **dict(broker_request.metadata or {})},
        )
        enqueue_result = self.gateway_state.enqueue_command(
            command,
            priority=CommandPriority.HIGH,
            ttl_sec=self.settings.command_ttl_sec,
            max_attempts=self.settings.command_max_attempts,
            metadata={"api": "/api/orders/enqueue", "dedupe_key": dedupe_key},
        )
        return OrderEnqueueResult(
            accepted=enqueue_result.accepted,
            mode=requested_mode,
            dry_run=False,
            command_id=command.command_id,
            idempotency_key=broker_request.idempotency_key,
            dedupe_key=dedupe_key,
            duplicate_of=enqueue_result.duplicate_of or duplicate_of,
            status=enqueue_result.record.status.value if enqueue_result.record else "REJECTED",
            reason=enqueue_result.reason or ("QUEUED" if enqueue_result.accepted else "REJECTED"),
            safety=safety.to_dict(),
            request=broker_request.to_dict(),
            command=command.to_dict(),
            record=enqueue_result.record.to_dict() if enqueue_result.record else None,
        )

    def enqueue_dry_run_order(self, request: RuntimeOrderIntentRequest) -> OrderEnqueueResult:
        broker_request = self._broker_request_from_runtime(request)
        idempotency_key = broker_request.idempotency_key or self._runtime_idempotency_key(request, broker_request)
        broker_request = BrokerOrderRequest(
            **{**broker_request.to_dict(), "idempotency_key": idempotency_key}
        )
        dedupe_key = dedupe_key_for_order_request(broker_request)
        now = str(self.clock())
        trade_date = self._trade_date(request, now)

        db = TradingDatabase(str(self.db_path))
        try:
            duplicate = db.find_runtime_order_intent_by_idempotency(idempotency_key) or db.find_runtime_order_intent_by_dedupe(dedupe_key)
            response_request = {**broker_request.to_dict(), **request.to_dict()}
            if duplicate is not None:
                db.append_runtime_order_intent_event(
                    str(duplicate.get("intent_id") or ""),
                    "duplicate_rejected",
                    status_from=str(duplicate.get("status") or ""),
                    status_to=str(duplicate.get("status") or ""),
                    message="DUPLICATE_DRY_RUN_ORDER_INTENT",
                    payload={"dedupe_key": dedupe_key, "idempotency_key": idempotency_key, "request": request.to_dict()},
                    created_at=now,
                )
                return OrderEnqueueResult(
                    accepted=False,
                    mode="DRY_RUN",
                    dry_run=True,
                    intent_id=str(duplicate.get("intent_id") or ""),
                    idempotency_key=idempotency_key,
                    dedupe_key=dedupe_key,
                    duplicate_of=str(duplicate.get("intent_id") or ""),
                    status=DUPLICATE,
                    reason="DUPLICATE_DRY_RUN_ORDER_INTENT",
                    request=response_request,
                    response={"duplicate_of": duplicate.get("intent_id"), "created_at": now},
                )

            decision_safety = self._decision_guard().validate(
                broker_request,
                gateway_status=self._synthetic_gateway_status(broker_request),
                existing_order_command_count=self._dry_run_intent_count(db, broker_request.code, broker_request.side, broker_request.tag),
                duplicate=False,
            )
            live_safety = self._live_guard("LIVE").validate(
                broker_request,
                gateway_status=self.gateway_state.snapshot().to_dict(),
                existing_order_command_count=self._order_command_count(
                    broker_request.code,
                    broker_request.side,
                    broker_request.tag,
                    order_type=broker_request.order_type,
                ),
                duplicate=self.gateway_state.has_duplicate(dedupe_key),
            )
            status = DRY_RUN_ACCEPTED if decision_safety.ok else DRY_RUN_REJECTED
            reason = "DRY_RUN_ORDER_INTENT_RECORDED" if decision_safety.ok else _dry_run_reject_reason(request, decision_safety.reason)
            intent_id = new_message_id("intent")
            metadata = {
                **dict(request.metadata or {}),
                "runtime_cycle_at": request.runtime_cycle_at,
                "gate_score": request.gate_score,
                "hybrid_score": request.hybrid_score,
                "theme_name": request.theme_name,
                "theme_score": request.theme_score,
            }
            record = {
                "intent_id": intent_id,
                "trade_date": trade_date,
                "source": request.source,
                "mode": "DRY_RUN",
                "dry_run": True,
                "status": status,
                "reason": reason,
                "account": broker_request.account,
                "code": broker_request.code,
                "side": broker_request.side,
                "quantity": broker_request.quantity,
                "price": broker_request.price,
                "order_amount": int(broker_request.quantity) * max(0, int(broker_request.price)),
                "order_type": broker_request.order_type,
                "hoga": broker_request.hoga,
                "tag": broker_request.tag,
                "strategy_name": request.strategy_name,
                "candidate_id": request.candidate_id,
                "entry_plan_id": request.entry_plan_id,
                "virtual_order_id": request.virtual_order_id,
                "virtual_position_id": request.virtual_position_id,
                "trade_review_id": request.trade_review_id,
                "leg_index": request.leg_index,
                "entry_type": request.entry_type,
                "order_phase": request.order_phase or ("exit" if request.side == "sell" else "entry"),
                "exit_decision_id": request.exit_decision_id,
                "exit_decision_type": request.exit_decision_type,
                "exit_reason": request.exit_reason,
                "exit_percent": request.exit_percent,
                "exit_quantity": request.exit_quantity,
                "remaining_quantity": request.remaining_quantity,
                "position_entry_price": request.position_entry_price,
                "position_quantity": request.position_quantity,
                "position_opened_at": request.position_opened_at,
                "position_closed_at": request.position_closed_at,
                "position_max_return_pct": request.position_max_return_pct,
                "position_max_drawdown_pct": request.position_max_drawdown_pct,
                "realized_return_pct": request.realized_return_pct,
                "virtual_exit_price": request.virtual_exit_price,
                "gate_reason": request.gate_reason,
                "gate_status": request.gate_status,
                "idempotency_key": idempotency_key,
                "dedupe_key": dedupe_key,
                "duplicate_of": "",
                "safety": decision_safety.to_dict(),
                "live_safety": live_safety.to_dict(),
                "request": request.to_dict(),
                "metadata": metadata,
                "created_at": now,
                "updated_at": now,
            }
            response = {
                "accepted": bool(decision_safety.ok),
                "intent_id": intent_id,
                "status": status,
                "reason": reason,
                "live_would_pass": bool(live_safety.ok),
                "live_reject_reason": "" if live_safety.ok else live_safety.reason,
                "created_at": now,
            }
            record["response"] = response
            saved = db.save_runtime_order_intent(record)
            db.append_runtime_order_intent_event(
                intent_id,
                "created",
                status_to=status,
                message=reason,
                payload=response,
                created_at=now,
            )
            return OrderEnqueueResult(
                accepted=bool(decision_safety.ok),
                mode="DRY_RUN",
                dry_run=True,
                intent_id=intent_id,
                idempotency_key=idempotency_key,
                dedupe_key=dedupe_key,
                status=status,
                reason=reason,
                safety=decision_safety.to_dict(),
                live_safety=live_safety.to_dict(),
                live_would_pass=bool(live_safety.ok),
                live_reject_reason="" if live_safety.ok else live_safety.reason,
                request=response_request,
                response=response,
                record=saved,
            )
        finally:
            db.close()

    def enqueue_live_sim_order(
        self,
        request: RuntimeOrderIntentRequest,
        *,
        execution_config: dict[str, Any] | None = None,
        exit_guard_config: dict[str, Any] | None = None,
        lifecycle_config: dict[str, Any] | None = None,
        reconcile_config: dict[str, Any] | None = None,
    ) -> OrderEnqueueResult:
        execution = dict(execution_config or {})
        exit_guard = dict(exit_guard_config or {})
        lifecycle = dict(lifecycle_config or {})
        reconcile = dict(reconcile_config or {})
        now = str(self.clock())
        trade_date = self._trade_date(request, now)
        gateway_status = self.gateway_state.snapshot().to_dict()
        broker_request = self._broker_request_from_runtime_live_sim(request, gateway_status=gateway_status)
        (
            request,
            broker_request,
            exit_quantity_block_reason,
            exit_quantity_block_codes,
            exit_quantity_details,
        ) = self._apply_live_sim_exit_position_quantity(request, broker_request)
        idempotency_key = broker_request.idempotency_key or self._live_sim_idempotency_key(request, broker_request, trade_date)
        broker_request = BrokerOrderRequest(**{**broker_request.to_dict(), "idempotency_key": idempotency_key})
        dedupe_key = dedupe_key_for_order_request(broker_request)
        order_intent_id = new_message_id("live_sim_intent")
        account_guard = _live_sim_account_guard(
            broker_request,
            gateway_status,
            execution,
        )
        base_record = self._live_sim_order_record(
            request,
            broker_request,
            order_intent_id=order_intent_id,
            trade_date=trade_date,
            now=now,
            status="CREATED",
            dedupe_key=dedupe_key,
            reason_codes=[],
            details={
                "account_guard": account_guard,
                "execution_config": _public_execution_config(execution),
                "exit_guard": exit_guard,
                "lifecycle": lifecycle,
                "reconcile": reconcile,
                "request": request.to_dict(),
                **({"exit_quantity_resolution": exit_quantity_details} if exit_quantity_details else {}),
            },
        )

        if exit_quantity_block_reason:
            block_reason, block_codes, block_details = (
                exit_quantity_block_reason,
                exit_quantity_block_codes,
                {"exit_quantity_resolution": exit_quantity_details},
            )
        else:
            block_reason, block_codes, block_details = self._live_sim_pre_submit_block_reason(
                request,
                broker_request,
                gateway_status=gateway_status,
                execution=execution,
                exit_guard=exit_guard,
                lifecycle=lifecycle,
                reconcile=reconcile,
                account_guard=account_guard,
            )
        db = TradingDatabase(str(self.db_path))
        try:
            duplicate = db.find_live_sim_order_by_idempotency(idempotency_key)
            if duplicate is not None:
                codes = _unique_reason_codes(
                    [
                        "ORDER_DUPLICATE_BLOCKED",
                        "DUPLICATE_FIRST_LEG_ORDER_BLOCKED" if broker_request.side == "buy" else "",
                    ]
                )
                record = {
                    **base_record,
                    "order_status": "DUPLICATE",
                    "reason_codes": codes,
                    "details": {
                        **dict(base_record.get("details") or {}),
                        "duplicate_of": duplicate.get("order_intent_id"),
                        "duplicate_status": duplicate.get("order_status"),
                    },
                }
                saved = db.save_live_sim_order(record)
                db.append_live_sim_order_event(
                    order_intent_id,
                    "duplicate_blocked",
                    status_to="DUPLICATE",
                    message="ORDER_DUPLICATE_BLOCKED",
                    payload={"duplicate_of": duplicate, "idempotency_key": idempotency_key, "dedupe_key": dedupe_key},
                    created_at=now,
                )
                return OrderEnqueueResult(
                    accepted=False,
                    mode="LIVE_SIM",
                    dry_run=False,
                    intent_id=order_intent_id,
                    idempotency_key=idempotency_key,
                    dedupe_key=dedupe_key,
                    duplicate_of=str(duplicate.get("order_intent_id") or ""),
                    status="DUPLICATE",
                    reason="ORDER_DUPLICATE_BLOCKED",
                    safety={"ok": False, "reason": "ORDER_DUPLICATE_BLOCKED", "details": record["details"]},
                    request=broker_request.to_dict(),
                    record=saved,
                )

            if block_reason:
                record = {
                    **base_record,
                    "order_status": "BLOCKED",
                    "reason_codes": block_codes,
                    "details": {**dict(base_record.get("details") or {}), **block_details},
                }
                saved = db.save_live_sim_order(record)
                db.append_live_sim_order_event(
                    order_intent_id,
                    "blocked",
                    status_to="BLOCKED",
                    message=block_reason,
                    payload={"reason_codes": block_codes, "details": record["details"]},
                    created_at=now,
                )
                return OrderEnqueueResult(
                    accepted=False,
                    mode="LIVE_SIM",
                    dry_run=False,
                    intent_id=order_intent_id,
                    idempotency_key=idempotency_key,
                    dedupe_key=dedupe_key,
                    status="BLOCKED",
                    reason=block_reason,
                    safety={"ok": False, "reason": block_reason, "details": record["details"]},
                    request=broker_request.to_dict(),
                    record=saved,
                )

            duplicate_command = self.gateway_state.has_duplicate(dedupe_key)
            live_safety = self._live_sim_guard(execution).validate(
                broker_request,
                gateway_status=gateway_status,
                existing_order_command_count=self._order_command_count(
                    broker_request.code,
                    broker_request.side,
                    broker_request.tag,
                    order_type=broker_request.order_type,
                ),
                duplicate=duplicate_command,
            )
            if not live_safety.ok:
                codes = _unique_reason_codes(["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD", live_safety.reason])
                record = {
                    **base_record,
                    "order_status": "BLOCKED",
                    "reason_codes": codes,
                    "details": {
                        **dict(base_record.get("details") or {}),
                        "live_safety": live_safety.to_dict(),
                    },
                }
                saved = db.save_live_sim_order(record)
                db.append_live_sim_order_event(
                    order_intent_id,
                    "blocked_live_safety",
                    status_to="BLOCKED",
                    message=live_safety.reason,
                    payload=live_safety.to_dict(),
                    created_at=now,
                )
                return OrderEnqueueResult(
                    accepted=False,
                    mode="LIVE_SIM",
                    dry_run=False,
                    intent_id=order_intent_id,
                    idempotency_key=idempotency_key,
                    dedupe_key=dedupe_key,
                    status="BLOCKED",
                    reason=live_safety.reason,
                    safety=live_safety.to_dict(),
                    request=broker_request.to_dict(),
                    record=saved,
                )

            command = GatewayCommand(
                type="send_order",
                command_id=new_message_id("cmd_order"),
                idempotency_key=idempotency_key,
                payload={
                    **broker_request.to_dict(),
                    **dict(broker_request.metadata or {}),
                    "order_mode": "LIVE_SIM",
                    "broker_env": "SIMULATION",
                    "account_id_masked": _mask_account(broker_request.account),
                    "live_sim_order_intent_id": order_intent_id,
                },
            )
            enqueue_result = self.gateway_state.enqueue_command(
                command,
                priority=CommandPriority.HIGH,
                ttl_sec=int(execution.get("command_ttl_sec") or self.settings.command_ttl_sec),
                max_attempts=int(execution.get("command_max_attempts") or self.settings.command_max_attempts),
                metadata={"runtime": "LIVE_SIM", "dedupe_key": dedupe_key, "order_intent_id": order_intent_id},
            )
            if not enqueue_result.accepted:
                status = "UNKNOWN_SUBMIT" if str(enqueue_result.reason or "") in {"TIMEOUT", "UNKNOWN"} else "BLOCKED"
                reason = "ORDER_UNKNOWN_SUBMIT_REQUIRES_RECONCILE" if status == "UNKNOWN_SUBMIT" else (enqueue_result.reason or "ORDER_DUPLICATE_BLOCKED")
                codes = _unique_reason_codes([reason])
                record = {
                    **base_record,
                    "command_id": command.command_id,
                    "order_status": status,
                    "submitted_at": now if status == "UNKNOWN_SUBMIT" else "",
                    "reason_codes": codes,
                    "details": {
                        **dict(base_record.get("details") or {}),
                        "enqueue_result": enqueue_result.to_dict(),
                    },
                }
                saved = db.save_live_sim_order(record)
                db.append_live_sim_order_event(
                    order_intent_id,
                    "enqueue_rejected",
                    status_to=status,
                    message=reason,
                    payload=enqueue_result.to_dict(),
                    created_at=now,
                )
                return OrderEnqueueResult(
                    accepted=False,
                    mode="LIVE_SIM",
                    dry_run=False,
                    intent_id=order_intent_id,
                    command_id=command.command_id,
                    idempotency_key=idempotency_key,
                    dedupe_key=dedupe_key,
                    duplicate_of=enqueue_result.duplicate_of,
                    status=status,
                    reason=reason,
                    safety=live_safety.to_dict(),
                    request=broker_request.to_dict(),
                    command=command.to_dict(),
                    record=saved,
                )

            allowed_codes = _unique_reason_codes(
                [
                    "LIVE_SIM_ORDER_ALLOWED",
                    "ACCOUNT_GUARD_PASSED_SIMULATION",
                    *list(account_guard.get("reason_codes") or []),
                    "LIVE_SIM_FIRST_LEG_ONLY" if broker_request.side == "buy" else "LIVE_SIM_EXIT_ORDER_SUBMITTED",
                    "ORDER_IDEMPOTENCY_KEY_CREATED",
                ]
            )
            record = {
                **base_record,
                "command_id": command.command_id,
                "order_status": "SUBMITTED",
                "submitted_at": now,
                "reason_codes": allowed_codes,
                "details": {
                    **dict(base_record.get("details") or {}),
                    "live_safety": live_safety.to_dict(),
                    "command": command.to_dict(),
                    "enqueue_result": enqueue_result.to_dict(),
                },
            }
            saved = db.save_live_sim_order(record)
            db.append_live_sim_order_event(
                order_intent_id,
                "submitted",
                status_to="SUBMITTED",
                message="LIVE_SIM_ORDER_ALLOWED",
                payload={"command": command.to_dict(), "enqueue_result": enqueue_result.to_dict()},
                created_at=now,
            )
            return OrderEnqueueResult(
                accepted=True,
                mode="LIVE_SIM",
                dry_run=False,
                intent_id=order_intent_id,
                command_id=command.command_id,
                idempotency_key=idempotency_key,
                dedupe_key=dedupe_key,
                status="SUBMITTED",
                reason="LIVE_SIM_ORDER_ALLOWED",
                safety=live_safety.to_dict(),
                live_safety=live_safety.to_dict(),
                live_would_pass=True,
                request=broker_request.to_dict(),
                command=command.to_dict(),
                record=saved,
            )
        finally:
            db.close()

    def run_live_sim_order_lifecycle(
        self,
        *,
        execution_config: dict[str, Any] | None = None,
        lifecycle_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution = dict(execution_config or {})
        lifecycle = dict(lifecycle_config or {})
        now = str(self.clock())
        if not bool(lifecycle.get("enabled", True)):
            return {"status": "SKIPPED", "reason": "LIVE_SIM_ORDER_LIFECYCLE_DISABLED", "cancelled": []}
        db = TradingDatabase(str(self.db_path))
        cancelled: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        try:
            for status in ["SUBMITTED", "ACCEPTED", "PARTIAL_FILLED"]:
                for order in db.list_live_sim_orders(status=status, limit=500):
                    due_reason = _cancel_due_reason(order, lifecycle, now)
                    if not due_reason:
                        continue
                    cancel_qty = _remaining_cancel_qty(order)
                    db.append_live_sim_order_event(
                        str(order.get("order_intent_id") or ""),
                        "cancel_due",
                        status_from=str(order.get("order_status") or ""),
                        status_to=str(order.get("order_status") or ""),
                        message=due_reason,
                        payload={
                            "cancel_due": True,
                            "cancel_due_reason": due_reason,
                            "cancel_qty": cancel_qty,
                            "lifecycle": lifecycle,
                        },
                        created_at=now,
                    )
                    result = self.enqueue_live_sim_cancel_order(
                        order,
                        cancel_qty=cancel_qty,
                        cancel_reason=due_reason,
                        execution_config=execution,
                        lifecycle_config=lifecycle,
                    )
                    payload = result.to_dict()
                    if result.accepted:
                        cancelled.append(payload)
                    else:
                        blocked.append(payload)
            db.save_live_sim_runtime_health(
                "cancel_scheduler",
                status="HEALTHY",
                reason="OK",
                details={"cancelled": len(cancelled), "blocked": len(blocked)},
                updated_at=now,
            )
            return {"status": "HEALTHY", "cancelled": cancelled, "blocked": blocked}
        except Exception as exc:
            db.save_live_sim_runtime_health(
                "cancel_scheduler",
                status="UNHEALTHY",
                reason=str(exc),
                consecutive_failures=1,
                details={"error": str(exc)},
                updated_at=now,
            )
            return {"status": "UNHEALTHY", "reason": str(exc), "cancelled": cancelled, "blocked": blocked}
        finally:
            db.close()

    def enqueue_live_sim_cancel_order(
        self,
        order: dict[str, Any],
        *,
        cancel_qty: int,
        cancel_reason: str,
        execution_config: dict[str, Any] | None = None,
        lifecycle_config: dict[str, Any] | None = None,
    ) -> OrderEnqueueResult:
        execution = dict(execution_config or {})
        lifecycle = dict(lifecycle_config or {})
        now = str(self.clock())
        gateway_status = self.gateway_state.snapshot().to_dict()
        account = str(gateway_status.get("account") or (gateway_status.get("last_heartbeat_payload") or {}).get("account") or "")
        broker_order_id = str(order.get("broker_order_id") or "")
        original_order_id = str(order.get("order_intent_id") or "")
        trade_date = str(order.get("trade_date") or _kst_trade_date(now))
        account_id_masked = _mask_account(account or str(order.get("account_id_masked") or ""))
        cancel_qty = max(0, int(cancel_qty or 0))
        idempotency_key = _live_sim_cancel_idempotency_key(
            trade_date=trade_date,
            broker_order_id=broker_order_id,
            original_order_id=original_order_id,
            code=str(order.get("code") or ""),
            cancel_qty=cancel_qty,
            cancel_reason=cancel_reason,
            account_id_masked=account_id_masked,
        )
        cancel_intent_id = new_message_id("live_sim_cancel")
        base_record = {
            "cancel_intent_id": cancel_intent_id,
            "original_order_id": original_order_id,
            "broker_order_id": broker_order_id,
            "trade_date": trade_date,
            "code": order.get("code"),
            "side": order.get("side"),
            "cancel_qty": cancel_qty,
            "cancel_reason": cancel_reason,
            "order_mode": "LIVE_SIM",
            "account_id_masked": account_id_masked,
            "candidate_instance_id": order.get("candidate_instance_id"),
            "entry_plan_id": order.get("entry_plan_id"),
            "idempotency_key": idempotency_key,
            "status": "CREATED",
            "attempts": int(order.get("details", {}).get("cancel_attempts") or 0) + 1,
            "created_at": now,
            "updated_at": now,
            "reason_codes": _cancel_reason_codes(cancel_reason),
            "details": {"original_order": order},
        }
        db = TradingDatabase(str(self.db_path))
        try:
            if str(order.get("order_status") or "") in {"FILLED", "CANCELLED", "REJECTED", "FAILED", "EXPIRED"}:
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "status": "REJECTED", "reason_codes": ["LIVE_SIM_CANCEL_ORDER_REJECTED"], "details": {**base_record["details"], "reason": "ORDER_TERMINAL"}}
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, idempotency_key=idempotency_key, status="REJECTED", reason="ORDER_TERMINAL", record=saved)
            if not broker_order_id or str(order.get("order_status") or "") == "UNKNOWN_SUBMIT":
                updated = db.update_live_sim_order(
                    original_order_id,
                    {
                        "order_status": "RECONCILE_REQUIRED",
                        "updated_at": now,
                        "reason_codes": _merge_reason_codes(order, ["LIVE_SIM_CANCEL_RECONCILE_REQUIRED"]),
                        "details": {**dict(order.get("details") or {}), "cancel_blocked_reason": "BROKER_ORDER_ID_MISSING"},
                    },
                )
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "status": "RECONCILE_REQUIRED", "reason_codes": ["LIVE_SIM_CANCEL_RECONCILE_REQUIRED"], "details": {**base_record["details"], "order": updated}}
                )
                db.append_live_sim_order_event(
                    original_order_id,
                    "reconcile_required",
                    status_from=str(order.get("order_status") or ""),
                    status_to="RECONCILE_REQUIRED",
                    message="LIVE_SIM_CANCEL_RECONCILE_REQUIRED",
                    payload={"cancel": saved, "issue_type": "BROKER_ORDER_ID_MISSING", "operator_message_ko": "주문번호가 없어 재조회가 필요합니다."},
                    created_at=now,
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, idempotency_key=idempotency_key, status="RECONCILE_REQUIRED", reason="LIVE_SIM_CANCEL_RECONCILE_REQUIRED", record=saved)
            duplicate = db.find_pending_live_sim_cancel(broker_order_id=broker_order_id)
            if duplicate is not None:
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "status": "DUPLICATE", "reason_codes": ["LIVE_SIM_CANCEL_DUPLICATE_BLOCKED"], "details": {**base_record["details"], "duplicate_of": duplicate}}
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, idempotency_key=idempotency_key, duplicate_of=str(duplicate.get("cancel_intent_id") or ""), status="DUPLICATE", reason="LIVE_SIM_CANCEL_DUPLICATE_BLOCKED", record=saved)
            max_attempts = int(lifecycle.get("max_cancel_attempts") or 2)
            attempts = int(base_record["attempts"])
            if attempts > max_attempts:
                updated = db.update_live_sim_order(
                    original_order_id,
                    {
                        "order_status": "RECONCILE_REQUIRED",
                        "updated_at": now,
                        "reason_codes": _merge_reason_codes(order, ["LIVE_SIM_CANCEL_MAX_ATTEMPTS_EXCEEDED", "LIVE_SIM_CANCEL_RECONCILE_REQUIRED"]),
                        "details": {**dict(order.get("details") or {}), "cancel_attempts": attempts},
                    },
                )
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "status": "RECONCILE_REQUIRED", "reason_codes": ["LIVE_SIM_CANCEL_MAX_ATTEMPTS_EXCEEDED", "LIVE_SIM_CANCEL_RECONCILE_REQUIRED"], "details": {**base_record["details"], "order": updated}}
                )
                db.append_live_sim_order_event(
                    original_order_id,
                    "reconcile_required",
                    status_from=str(order.get("order_status") or ""),
                    status_to="RECONCILE_REQUIRED",
                    message="LIVE_SIM_CANCEL_MAX_ATTEMPTS_EXCEEDED",
                    payload={"cancel": saved, "issue_type": "CANCEL_MAX_ATTEMPTS_EXCEEDED", "operator_message_ko": "취소 시도 횟수를 초과해 재조회가 필요합니다."},
                    created_at=now,
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, idempotency_key=idempotency_key, status="RECONCILE_REQUIRED", reason="LIVE_SIM_CANCEL_MAX_ATTEMPTS_EXCEEDED", record=saved)
            guard_request = BrokerOrderRequest(
                account=account,
                code=str(order.get("code") or ""),
                quantity=cancel_qty,
                price=0,
                side=str(order.get("side") or "buy"),
            )
            account_guard = _live_sim_account_guard(guard_request, gateway_status, execution)
            if not account_guard.get("ok") or not bool(gateway_status.get("connected")) or not bool(gateway_status.get("heartbeat_ok")):
                reason = str(account_guard.get("reason") or "LIVE_SIM_BROKER_DISCONNECTED")
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "status": "REJECTED", "reason_codes": ["LIVE_SIM_CANCEL_ORDER_REJECTED", reason], "details": {**base_record["details"], "account_guard": account_guard}}
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, idempotency_key=idempotency_key, status="REJECTED", reason=reason, record=saved)
            command = GatewayCommand(
                type="cancel_order",
                command_id=new_message_id("cmd_cancel"),
                idempotency_key=idempotency_key,
                payload={
                    "account": account,
                    "code": str(order.get("code") or ""),
                    "quantity": cancel_qty,
                    "original_order_no": broker_order_id,
                    "order_mode": "LIVE_SIM",
                    "account_id_masked": account_id_masked,
                    "cancel_intent_id": cancel_intent_id,
                    "original_order_id": original_order_id,
                    "cancel_reason": cancel_reason,
                },
            )
            enqueue_result = self.gateway_state.enqueue_command(
                command,
                priority=CommandPriority.HIGH,
                ttl_sec=int(lifecycle.get("cancel_command_ttl_sec") or self.settings.command_ttl_sec),
                max_attempts=1,
                metadata={"runtime": "LIVE_SIM", "cancel_intent_id": cancel_intent_id, "original_order_id": original_order_id},
            )
            if not enqueue_result.accepted:
                saved = db.save_live_sim_cancel_order(
                    {**base_record, "command_id": command.command_id, "status": "REJECTED", "reason_codes": ["LIVE_SIM_CANCEL_ORDER_REJECTED"], "details": {**base_record["details"], "enqueue_result": enqueue_result.to_dict()}}
                )
                return OrderEnqueueResult(False, "LIVE_SIM", False, intent_id=cancel_intent_id, command_id=command.command_id, idempotency_key=idempotency_key, status="REJECTED", reason=enqueue_result.reason or "LIVE_SIM_CANCEL_ORDER_REJECTED", command=command.to_dict(), record=saved)
            codes = _unique_reason_codes([*base_record["reason_codes"], "LIVE_SIM_CANCEL_ORDER_QUEUED", "LIVE_SIM_CANCEL_ORDER_SUBMITTED"])
            saved = db.save_live_sim_cancel_order(
                {**base_record, "command_id": command.command_id, "status": "SUBMITTED", "submitted_at": now, "reason_codes": codes, "details": {**base_record["details"], "command": command.to_dict(), "enqueue_result": enqueue_result.to_dict()}}
            )
            db.update_live_sim_order(
                original_order_id,
                {
                    "order_status": "CANCEL_REQUESTED",
                    "updated_at": now,
                    "reason_codes": _merge_reason_codes(order, codes),
                    "details": {**dict(order.get("details") or {}), "cancel_attempts": attempts, "cancel_intent": saved},
                },
            )
            db.append_live_sim_order_event(
                original_order_id,
                "cancel_requested",
                status_from=str(order.get("order_status") or ""),
                status_to="CANCEL_REQUESTED",
                message=cancel_reason,
                payload=saved,
                created_at=now,
            )
            return OrderEnqueueResult(True, "LIVE_SIM", False, intent_id=cancel_intent_id, command_id=command.command_id, idempotency_key=idempotency_key, status="SUBMITTED", reason="LIVE_SIM_CANCEL_ORDER_QUEUED", command=command.to_dict(), record=saved)
        finally:
            db.close()

    def run_live_sim_exit_monitor(
        self,
        *,
        execution_config: dict[str, Any] | None = None,
        exit_guard_config: dict[str, Any] | None = None,
        lifecycle_config: dict[str, Any] | None = None,
        reconcile_config: dict[str, Any] | None = None,
        latest_ticks: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        execution = dict(execution_config or {})
        exit_guard = dict(exit_guard_config or {})
        lifecycle = dict(lifecycle_config or {})
        reconcile = dict(reconcile_config or {})
        now = str(now or self.clock())
        if not bool(exit_guard.get("enabled", True)):
            self._save_live_sim_health("exit_monitor", "UNHEALTHY", "LIVE_SIM_EXIT_MONITOR_UNHEALTHY", now=now)
            return {"status": "UNHEALTHY", "reason": "LIVE_SIM_EXIT_MONITOR_UNHEALTHY", "orders": []}
        ticks = dict(latest_ticks or {})
        db = TradingDatabase(str(self.db_path))
        orders: list[dict[str, Any]] = []
        try:
            stale_codes: list[str] = []
            for position in db.list_live_sim_positions(limit=500):
                if str(position.get("status") or "") not in {"OPEN", "PARTIAL"}:
                    continue
                tick = _normalize_tick(ticks.get(str(position.get("code") or "")), now)
                if _tick_is_stale(tick, now, int(exit_guard.get("max_exit_tick_age_sec") or 10)):
                    stale_codes.append(str(position.get("code") or ""))
                    continue
                trigger = _exit_trigger(position, tick, exit_guard, now)
                if not trigger:
                    continue
                if _has_active_exit_order(db, str(position.get("position_id") or ""), str(position.get("code") or "")):
                    orders.append({"accepted": False, "status": "DUPLICATE", "reason": "LIVE_SIM_EXIT_DUPLICATE_BLOCKED", "position_id": position.get("position_id")})
                    continue
                request = RuntimeOrderIntentRequest(
                    source="live_sim_exit_monitor",
                    dry_run=False,
                    account="",
                    code=str(position.get("code") or ""),
                    side="sell",
                    quantity=int(position.get("current_qty") or 0),
                    price=int(tick.get("price") or 0),
                    order_type=int(self.settings.runtime_dry_run_order_type_sell),
                    hoga=self.settings.runtime_dry_run_hoga,
                    tag=f"runtime:exit:{trigger['reason']}",
                    order_phase="exit",
                    reason=str(trigger["reason"]),
                    exit_decision_type=str(trigger["reason"]),
                    runtime_cycle_at=now,
                    metadata={
                        "position_id": position.get("position_id"),
                        "candidate_instance_id": position.get("candidate_instance_id"),
                        "reason_codes": [trigger["reason_code"], "LIVE_SIM_EXIT_ORDER_QUEUED"],
                    },
                )
                result = self.enqueue_live_sim_order(
                    request,
                    execution_config=execution,
                    exit_guard_config=exit_guard,
                    lifecycle_config=lifecycle,
                    reconcile_config=reconcile,
                )
                orders.append(result.to_dict())
                if result.accepted:
                    db.save_live_sim_position(
                        {
                            **position,
                            "status": "EXIT_ORDERED",
                            "updated_at": now,
                            "details": {**dict(position.get("details") or {}), "exit_order": result.to_dict(), "exit_reason": trigger["reason"]},
                        }
                    )
            if stale_codes and bool(exit_guard.get("require_latest_tick_ready_for_exit", True)):
                self._save_live_sim_health(
                    "exit_monitor",
                    "UNHEALTHY",
                    "LIVE_SIM_EXIT_LATEST_TICK_STALE",
                    now=now,
                    details={"stale_codes": stale_codes},
                )
                return {"status": "UNHEALTHY", "reason": "LIVE_SIM_EXIT_LATEST_TICK_STALE", "orders": orders, "stale_codes": stale_codes}
            self._save_live_sim_health("exit_monitor", "HEALTHY", "OK", now=now, details={"orders": len(orders)})
            return {"status": "HEALTHY", "orders": orders}
        except Exception as exc:
            self._save_live_sim_health("exit_monitor", "UNHEALTHY", str(exc), now=now, details={"error": str(exc)})
            return {"status": "UNHEALTHY", "reason": str(exc), "orders": orders}
        finally:
            db.close()

    def run_live_sim_reconcile(
        self,
        *,
        reconcile_config: dict[str, Any] | None = None,
        broker_snapshot: dict[str, Any] | None = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        reconcile = dict(reconcile_config or {})
        now = str(self.clock())
        if not bool(reconcile.get("enabled", True)):
            return {"status": "SKIPPED", "reason": "LIVE_SIM_RECONCILE_DISABLED"}
        event_id = new_message_id("live_sim_reconcile")
        snapshot = dict(broker_snapshot or {})
        reason_codes = _unique_reason_codes(["LIVE_SIM_RECONCILE_STARTED", _reconcile_trigger_code(trigger)])
        db = TradingDatabase(str(self.db_path))
        try:
            orders_reconciled = 0
            positions_reconciled = 0
            external_positions = 0
            position_reconcile_required = False
            for fill in list(snapshot.get("fills") or []):
                broker_order_id = str(fill.get("broker_order_id") or fill.get("order_no") or "")
                order = db.find_live_sim_order_by_broker_order_id(broker_order_id)
                if order is None:
                    continue
                event = BrokerExecutionEvent(
                    code=str(fill.get("code") or order.get("code") or ""),
                    order_no=broker_order_id,
                    side=str(fill.get("side") or order.get("side") or ""),
                    quantity=int(fill.get("quantity") or fill.get("filled_quantity") or order.get("requested_qty") or 0),
                    price=int(fill.get("price") or fill.get("fill_price") or order.get("requested_price") or 0),
                    filled_quantity=int(fill.get("filled_quantity") or fill.get("fill_qty") or fill.get("quantity") or 0),
                    remaining_quantity=int(fill.get("remaining_quantity") or fill.get("remaining_qty") or 0),
                    execution_id=str(fill.get("execution_id") or fill.get("fill_id") or f"reconcile:{broker_order_id}"),
                    command_id=str(order.get("command_id") or ""),
                    idempotency_key=str(order.get("idempotency_key") or ""),
                    timestamp=now,
                    raw={"source": "live_sim_reconcile", **dict(fill or {})},
                )
                db.save_execution(event)
                orders_reconciled += 1
                reason_codes.append("LIVE_SIM_RECONCILE_ORDER_FILLED_FROM_BROKER")
            for broker_order in list(snapshot.get("open_orders") or []):
                broker_order_id = str(broker_order.get("broker_order_id") or broker_order.get("order_no") or "")
                order = db.find_live_sim_order_by_broker_order_id(broker_order_id)
                if order:
                    db.append_live_sim_order_event(
                        str(order.get("order_intent_id") or ""),
                        "reconcile_open_order",
                        status_from=str(order.get("order_status") or ""),
                        status_to=str(order.get("order_status") or ""),
                        message="LIVE_SIM_RECONCILE_ON_STARTUP",
                        payload=broker_order,
                        created_at=now,
                    )
                    orders_reconciled += 1
            if bool(snapshot.get("cancelled_orders")):
                for broker_order in list(snapshot.get("cancelled_orders") or []):
                    broker_order_id = str(broker_order.get("broker_order_id") or broker_order.get("order_no") or "")
                    order = db.find_live_sim_order_by_broker_order_id(broker_order_id)
                    if order:
                        db.update_live_sim_order(
                            str(order.get("order_intent_id") or ""),
                            {
                                "order_status": "CANCELLED",
                                "cancelled_at": now,
                                "updated_at": now,
                                "reason_codes": _merge_reason_codes(order, ["LIVE_SIM_RECONCILE_ORDER_CANCELLED_FROM_BROKER"]),
                            },
                        )
                        db.append_live_sim_order_event(
                            str(order.get("order_intent_id") or ""),
                            "cancelled",
                            status_from=str(order.get("order_status") or ""),
                            status_to="CANCELLED",
                            message="LIVE_SIM_RECONCILE_ORDER_CANCELLED_FROM_BROKER",
                            payload=broker_order,
                            created_at=now,
                        )
                        orders_reconciled += 1
                        reason_codes.append("LIVE_SIM_RECONCILE_ORDER_CANCELLED_FROM_BROKER")
            open_positions = [
                item
                for item in db.list_live_sim_positions(limit=1000)
                if str(item.get("status") or "") in {"OPEN", "PARTIAL", "EXIT_ORDERED", "EXIT_SUBMITTING", "RECONCILE_REQUIRED"}
            ]
            open_by_key: dict[tuple[str, str], dict[str, Any] | None] = {}
            for item in open_positions:
                key = (str(item.get("code") or ""), str(item.get("account_id_masked") or ""))
                open_by_key[key] = item if key not in open_by_key else None
            for broker_position in list(snapshot.get("positions") or []):
                code = str(broker_position.get("code") or "")
                qty = int(broker_position.get("quantity") or broker_position.get("current_qty") or 0)
                account_id_masked = _mask_account(str(broker_position.get("account") or broker_position.get("account_id_masked") or ""))
                if qty <= 0 or not code:
                    continue
                key = (code, account_id_masked)
                if key in open_by_key:
                    position = open_by_key[key]
                    if position is None:
                        position_reconcile_required = True
                        reason_codes.append("LIVE_SIM_RECONCILE_POSITION_AMBIGUOUS")
                        db.save_live_sim_runtime_health(
                            "reconcile",
                            status="RECONCILE_REQUIRED",
                            reason="LIVE_SIM_RECONCILE_POSITION_AMBIGUOUS",
                            details={"code": code, "account_id_masked": account_id_masked},
                            updated_at=now,
                        )
                        continue
                    current_qty = int(position.get("current_qty") or 0)
                    if current_qty != qty or str(position.get("status") or "") == "RECONCILE_REQUIRED":
                        db.save_live_sim_position(
                            {
                                **position,
                                "current_qty": qty,
                                "entry_qty": max(int(position.get("entry_qty") or 0), qty),
                                "status": "OPEN",
                                "details": {
                                    **dict(position.get("details") or {}),
                                    "reconcile_position_sync": {
                                        "previous_current_qty": current_qty,
                                        "broker_current_qty": qty,
                                        "broker_position": broker_position,
                                        "synced_at": now,
                                    },
                                },
                                "updated_at": now,
                            }
                        )
                    positions_reconciled += 1
                    reason_codes.append("LIVE_SIM_RECONCILE_POSITION_SYNCED")
                    continue
                external_positions += 1
                db.save_live_sim_position(
                    {
                        "position_id": f"EXTERNAL:{account_id_masked}:{code}",
                        "candidate_instance_id": "EXTERNAL_POSITION_DETECTED",
                        "code": code,
                        "name": broker_position.get("name", ""),
                        "account_id_masked": account_id_masked,
                        "opened_at": now,
                        "entry_qty": qty,
                        "entry_avg_price": int(broker_position.get("avg_price") or broker_position.get("entry_avg_price") or 0),
                        "current_qty": qty,
                        "status": "RECONCILE_REQUIRED",
                        "details": {"external_position_detected": True, "broker_position": broker_position},
                        "updated_at": now,
                    }
                )
                reason_codes.append("LIVE_SIM_RECONCILE_EXTERNAL_POSITION_DETECTED")
            status = "COMPLETED"
            if external_positions or position_reconcile_required:
                db.save_live_sim_runtime_health(
                    "reconcile",
                    status="RECONCILE_REQUIRED",
                    reason=(
                        "LIVE_SIM_RECONCILE_EXTERNAL_POSITION_DETECTED"
                        if external_positions
                        else "LIVE_SIM_RECONCILE_POSITION_AMBIGUOUS"
                    ),
                    details={
                        "external_positions": external_positions,
                        "position_reconcile_required": position_reconcile_required,
                    },
                    updated_at=now,
                )
            else:
                db.save_live_sim_runtime_health("reconcile", status="HEALTHY", reason="OK", details={}, updated_at=now)
            reason_codes.append("LIVE_SIM_RECONCILE_COMPLETED")
            event = db.save_live_sim_reconcile_event(
                {
                    "event_id": event_id,
                    "trigger": trigger,
                    "status": status,
                    "reason": "OK",
                    "started_at": now,
                    "completed_at": now,
                    "payload": {
                        "orders_reconciled": orders_reconciled,
                        "positions_reconciled": positions_reconciled,
                        "external_positions": external_positions,
                    },
                    "reason_codes": _unique_reason_codes(reason_codes),
                }
            )
            return {"status": status, "event": event}
        except Exception as exc:
            db.save_live_sim_runtime_health("reconcile", status="UNHEALTHY", reason=str(exc), consecutive_failures=1, details={"error": str(exc)}, updated_at=now)
            event = db.save_live_sim_reconcile_event(
                {
                    "event_id": event_id,
                    "trigger": trigger,
                    "status": "FAILED",
                    "reason": str(exc),
                    "started_at": now,
                    "completed_at": now,
                    "payload": {"error": str(exc)},
                    "reason_codes": _unique_reason_codes([*reason_codes, "LIVE_SIM_RECONCILE_FAILED"]),
                }
            )
            return {"status": "FAILED", "reason": str(exc), "event": event}
        finally:
            db.close()

    def dry_run_summary(self, *, trade_date: str | None = None) -> dict:
        db = TradingDatabase(str(self.db_path))
        try:
            return db.runtime_order_intent_summary(trade_date=trade_date)
        finally:
            db.close()

    def live_sim_summary(self, *, trade_date: str | None = None) -> dict:
        db = TradingDatabase(str(self.db_path))
        try:
            return db.live_sim_summary(trade_date=trade_date)
        finally:
            db.close()

    def list_dry_run_orders(
        self,
        *,
        trade_date: str | None = None,
        status: str | None = None,
        code: str | None = None,
        candidate_id: int | None = None,
        side: str | None = None,
        order_phase: str | None = None,
        virtual_position_id: int | None = None,
        exit_decision_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        db = TradingDatabase(str(self.db_path))
        try:
            return {
                "summary": db.runtime_order_intent_summary(trade_date=trade_date),
                "items": db.list_runtime_order_intents(
                    trade_date=trade_date,
                    status=status,
                    code=code,
                    candidate_id=candidate_id,
                    side=side,
                    order_phase=order_phase,
                    virtual_position_id=virtual_position_id,
                    exit_decision_id=exit_decision_id,
                    limit=limit,
                    offset=offset,
                ),
            }
        finally:
            db.close()

    def get_dry_run_order(self, intent_id: str) -> dict:
        db = TradingDatabase(str(self.db_path))
        try:
            record = db.get_runtime_order_intent(intent_id)
            if record is None:
                return {}
            events = db.list_runtime_order_intent_events(intent_id, limit=200)
            linked: dict[str, Any] = {}
            candidate_id = record.get("candidate_id")
            if candidate_id is not None:
                candidate = db.load_candidate_by_id(int(candidate_id))
                linked["candidate"] = candidate.to_dict() if candidate is not None else None
            virtual_order_id = record.get("virtual_order_id")
            if virtual_order_id is not None:
                virtual_order = db.load_virtual_order(int(virtual_order_id))
                linked["virtual_order"] = virtual_order.to_dict() if virtual_order is not None else None
            virtual_position_id = record.get("virtual_position_id")
            if virtual_position_id is not None:
                virtual_position = db.load_virtual_position(int(virtual_position_id))
                linked["virtual_position"] = virtual_position.to_dict() if virtual_position is not None else None
            exit_decision_id = record.get("exit_decision_id")
            if exit_decision_id is not None:
                exit_decision = db.load_exit_decision(int(exit_decision_id))
                linked["exit_decision"] = exit_decision.to_dict() if exit_decision is not None else None
            trade_review_id = record.get("trade_review_id")
            if trade_review_id is not None:
                trade_review = db.load_trade_review(int(trade_review_id))
                linked["trade_review"] = trade_review.to_dict() if trade_review is not None else None
                linked["trade_review_id"] = trade_review_id
            return {"record": record, "events": events, "linked": linked}
        finally:
            db.close()

    def _broker_request_from_api(self, request: OrderEnqueueRequest, *, requested_mode: str) -> BrokerOrderRequest:
        return BrokerOrderRequest(
            account=request.account,
            code=request.code,
            quantity=request.quantity,
            price=request.price,
            side=request.side,
            tag=request.tag,
            order_type=request.order_type,
            hoga=request.hoga,
            idempotency_key=str(request.idempotency_key or ""),
            metadata={
                "strategy_name": request.strategy_name,
                "candidate_id": request.candidate_id,
                "reason": request.reason,
                "mode": requested_mode,
            },
        )

    def _broker_request_from_runtime(self, request: RuntimeOrderIntentRequest) -> BrokerOrderRequest:
        account = request.account or self.settings.runtime_dry_run_account
        metadata = dict(request.metadata or {})
        if not account and not self.settings.runtime_dry_run_require_account:
            account = "dryrun-account"
            metadata["account_placeholder"] = True
            metadata["warning"] = "DRY_RUN_ACCOUNT_PLACEHOLDER_USED"
        return BrokerOrderRequest(
            account=account,
            code=request.code,
            quantity=int(request.quantity or 0),
            price=int(request.price or 0),
            side=request.side,
            tag=request.tag,
            order_type=int(request.order_type or 0),
            hoga=request.hoga,
            idempotency_key=str(request.idempotency_key or ""),
            metadata={
                **metadata,
                "strategy_name": request.strategy_name,
                "candidate_id": request.candidate_id,
                "entry_plan_id": request.entry_plan_id,
                "virtual_order_id": request.virtual_order_id,
                "virtual_position_id": request.virtual_position_id,
                "exit_decision_id": request.exit_decision_id,
                "exit_decision_type": request.exit_decision_type,
                "exit_percent": request.exit_percent,
                "exit_quantity": request.exit_quantity,
                "position_quantity": request.position_quantity,
                "virtual_exit_price": request.virtual_exit_price,
                "order_phase": request.order_phase or ("exit" if request.side == "sell" else "entry"),
                "leg_index": request.leg_index,
                "reason": request.reason,
                "strategy_order_id": self._strategy_order_id(request),
            },
        )

    def _broker_request_from_runtime_live_sim(
        self,
        request: RuntimeOrderIntentRequest,
        *,
        gateway_status: dict[str, Any],
    ) -> BrokerOrderRequest:
        account = request.account or str(gateway_status.get("account") or "") or self.settings.runtime_dry_run_account
        metadata = {
            **dict(request.metadata or {}),
            "strategy_name": request.strategy_name,
            "candidate_id": request.candidate_id,
            "entry_plan_id": request.entry_plan_id,
            "virtual_order_id": request.virtual_order_id,
            "virtual_position_id": request.virtual_position_id,
            "exit_decision_id": request.exit_decision_id,
            "exit_decision_type": request.exit_decision_type,
            "exit_percent": request.exit_percent,
            "exit_quantity": request.exit_quantity,
            "position_quantity": request.position_quantity,
            "virtual_exit_price": request.virtual_exit_price,
            "order_phase": request.order_phase or ("exit" if request.side == "sell" else "entry"),
            "leg_index": request.leg_index,
            "reason": request.reason,
            "strategy_order_id": self._strategy_order_id(request),
            "order_mode": "LIVE_SIM",
        }
        return BrokerOrderRequest(
            account=account,
            code=request.code,
            quantity=int(request.quantity or 0),
            price=int(request.price or 0),
            side=str(request.side or "").lower(),
            tag=request.tag,
            order_type=int(request.order_type or 0),
            hoga=request.hoga,
            idempotency_key=str(request.idempotency_key or ""),
            metadata=metadata,
        )

    def _runtime_idempotency_key(self, request: RuntimeOrderIntentRequest, broker_request: BrokerOrderRequest) -> str:
        trade_date = self._trade_date(request, str(self.clock()))
        if request.side == "sell":
            if request.exit_decision_id is not None:
                return (
                    f"runtime:dryrun:exit:{trade_date}:{request.virtual_position_id or ''}:"
                    f"{request.exit_decision_id}:{request.exit_decision_type}:"
                    f"{broker_request.code}:sell:{broker_request.price}:"
                    f"{_key_number(request.exit_percent)}:{request.exit_quantity or ''}"
                )
            return (
                f"runtime:dryrun:exit:{trade_date}:{request.virtual_position_id or ''}:"
                f"{request.exit_decision_type}:{request.reason}:{broker_request.code}:sell:{broker_request.price}:"
                f"{_key_number(request.exit_percent)}:{request.exit_quantity or ''}"
            )
        return (
            f"runtime:dryrun:entry:{trade_date}:{request.candidate_id or ''}:"
            f"{request.entry_plan_id or ''}:{request.virtual_order_id or ''}:"
            f"{request.leg_index or ''}:{broker_request.code}:{broker_request.side}:{broker_request.price}"
        )

    def _live_sim_idempotency_key(self, request: RuntimeOrderIntentRequest, broker_request: BrokerOrderRequest, trade_date: str) -> str:
        phase = request.order_phase or ("exit" if broker_request.side == "sell" else "entry")
        candidate_instance_id = str((request.metadata or {}).get("candidate_instance_id") or "")
        account_id_masked = _mask_account(broker_request.account)
        if broker_request.side == "sell":
            position_id = str((request.metadata or {}).get("position_id") or request.virtual_position_id or "")
            exit_reason = str(request.exit_decision_type or request.exit_reason or request.reason or "")
            return (
                f"runtime:livesim:exit:{trade_date}:{account_id_masked}:{broker_request.code}:"
                f"{position_id}:{exit_reason}"
            )
        return (
            f"runtime:livesim:{phase}:{trade_date}:{account_id_masked}:{candidate_instance_id}:"
            f"{request.candidate_id or ''}:{request.entry_plan_id or ''}:{request.virtual_order_id or ''}:"
            f"{request.leg_index or ''}:{broker_request.code}:{broker_request.price}:{broker_request.quantity}"
        )

    @staticmethod
    def _strategy_order_id(request: RuntimeOrderIntentRequest) -> str:
        if request.side == "sell":
            return (
                f"exit:{request.virtual_position_id or ''}:"
                f"{request.exit_decision_id or ''}:{request.exit_decision_type}:"
                f"{_key_number(request.exit_percent)}:{request.exit_quantity or ''}"
            )
        return f"entry:{request.virtual_order_id or ''}:{request.leg_index or ''}"

    def _decision_guard(self) -> OrderCommandSafetyGuard:
        return OrderCommandSafetyGuard(
            OrderSafetyConfig(
                mode="LIVE",
                live_order_enabled=True,
                max_order_amount=self.settings.max_order_amount,
                max_daily_orders_per_code=self.settings.max_daily_orders_per_code,
                allow_zero_price=False,
            )
        )

    def _live_guard(self, mode: str) -> OrderCommandSafetyGuard:
        return OrderCommandSafetyGuard(
            OrderSafetyConfig(
                mode=mode,
                live_order_enabled=self.settings.live_order_enabled,
                max_order_amount=self.settings.max_order_amount,
                max_daily_orders_per_code=self.settings.max_daily_orders_per_code,
                allow_zero_price=False,
            )
        )

    def _live_sim_guard(self, execution: dict[str, Any]) -> OrderCommandSafetyGuard:
        return OrderCommandSafetyGuard(
            OrderSafetyConfig(
                mode="LIVE",
                live_order_enabled=True,
                max_order_amount=int(execution.get("max_order_amount_krw") or self.settings.max_order_amount),
                max_daily_orders_per_code=int(execution.get("max_orders_per_day") or self.settings.max_daily_orders_per_code),
                allow_zero_price=False,
                limit_sell_amount=False,
            )
        )

    def _live_sim_pre_submit_block_reason(
        self,
        request: RuntimeOrderIntentRequest,
        broker_request: BrokerOrderRequest,
        *,
        gateway_status: dict[str, Any],
        execution: dict[str, Any],
        exit_guard: dict[str, Any],
        lifecycle: dict[str, Any] | None = None,
        reconcile: dict[str, Any] | None = None,
        account_guard: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any]]:
        codes: list[str] = []
        details: dict[str, Any] = {}
        mode = str(execution.get("mode") or "DRY_RUN").upper()
        if mode == "LIVE_REAL" or bool(execution.get("live_real_enabled")):
            return "LIVE_REAL_ORDER_BLOCKED", ["LIVE_REAL_ORDER_BLOCKED"], {"execution_mode": mode}
        if mode != "LIVE_SIM" or not bool(execution.get("live_sim_enabled")):
            return "LIVE_SIM_DISABLED", ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"], {"execution_mode": mode}
        if bool(execution.get("kill_switch_active")) or (bool(execution.get("kill_switch_enabled", True)) and str(execution.get("kill_switch_state") or "").upper() == "ACTIVE"):
            return "LIVE_SIM_KILL_SWITCH_ACTIVE", ["LIVE_SIM_KILL_SWITCH_ACTIVE"], {"kill_switch_active": True}
        if not account_guard.get("ok"):
            reason = str(account_guard.get("reason") or "LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD")
            return reason, _unique_reason_codes(["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD", reason, *list(account_guard.get("reason_codes") or [])]), {"account_guard": account_guard}
        if broker_request.side == "buy" and not bool(exit_guard.get("enabled")):
            return "EXIT_GUARD_NOT_READY_BUY_BLOCKED", ["EXIT_GUARD_REQUIRED", "EXIT_GUARD_NOT_READY_BUY_BLOCKED"], {"exit_guard": exit_guard}
        if broker_request.side == "buy" and bool(execution.get("submit_first_leg_only", True)):
            try:
                leg_index = int(request.leg_index or broker_request.metadata.get("leg_index") or 1)
            except (TypeError, ValueError):
                leg_index = 1
            if leg_index != 1:
                return (
                    "SECOND_THIRD_LEG_BLOCKED_BEFORE_FIRST_FILL",
                    ["SECOND_THIRD_LEG_BLOCKED_BEFORE_FIRST_FILL"],
                    {"leg_index": leg_index, "submit_first_leg_only": True},
                )
        if not _order_price_type_allowed(broker_request, execution):
            return "PRICE_INVALID_OR_MARKET_ORDER_UNSUPPORTED", ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"], {"hoga": broker_request.hoga, "price": broker_request.price}
        amount = int(broker_request.quantity or 0) * max(0, int(broker_request.price or 0))
        min_amount = int(execution.get("min_order_amount_krw") or 0)
        max_amount = int(execution.get("max_order_amount_krw") or self.settings.max_order_amount)
        early_small_cap_applied = False
        if broker_request.side == "buy" and (_is_ready_early_small(request) or _is_ready_shadow_small_entry(request)):
            multiplier = float(execution.get("early_small_max_order_amount_multiplier") or 0.5)
            if _is_ready_shadow_small_entry(request):
                metadata_multiplier = (request.metadata or {}).get("shadow_small_entry_position_size_multiplier")
                multiplier = float(metadata_multiplier or execution.get("shadow_small_entry_max_order_amount_multiplier") or multiplier)
            multiplier = min(1.0, max(0.0, multiplier))
            max_amount = max(1, int(max_amount * multiplier))
            early_small_cap_applied = True
        if broker_request.side == "buy" and min_amount > 0 and amount < min_amount:
            return "ORDER_AMOUNT_BELOW_MIN", ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"], {"amount": amount, "min_order_amount_krw": min_amount}
        if broker_request.side == "buy" and amount > max_amount:
            return (
                "ORDER_AMOUNT_LIMIT",
                ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"],
                {
                    "amount": amount,
                    "max_order_amount_krw": max_amount,
                    "early_small_cap_applied": early_small_cap_applied,
                },
            )
        gate_reason = _runtime_gate_block_reason(request)
        if gate_reason:
            return gate_reason, [gate_reason], {"metadata": dict(request.metadata or {}), "gate_status": request.gate_status}
        if broker_request.side == "buy":
            shadow_reason, shadow_codes, shadow_details = self._live_sim_shadow_small_entry_block(request, broker_request, trade_date=self._trade_date(request, str(self.clock())))
            if shadow_reason:
                return shadow_reason, shadow_codes, shadow_details
            lifecycle_reason, lifecycle_codes, lifecycle_details = self._live_sim_buy_lifecycle_block(
                request,
                broker_request,
                lifecycle=dict(lifecycle or {}),
                reconcile=dict(reconcile or {}),
                exit_guard=exit_guard,
            )
            if lifecycle_reason:
                return lifecycle_reason, lifecycle_codes, lifecycle_details
        if not bool(gateway_status.get("connected")) or not bool(gateway_status.get("heartbeat_ok")):
            return "LIVE_SIM_BROKER_DISCONNECTED", ["LIVE_SIM_BROKER_DISCONNECTED"], {"gateway_status": _public_gateway_status(gateway_status)}
        summary = self._live_sim_summary_for_guard()
        max_orders = int(execution.get("max_orders_per_day") or 0)
        if max_orders > 0 and int(summary.get("submitted_order_count") or 0) >= max_orders:
            return "LIVE_SIM_MAX_ORDERS_HIT", ["LIVE_SIM_MAX_ORDERS_HIT"], {"summary": summary, "max_orders_per_day": max_orders}
        max_rejects = int(execution.get("max_rejected_orders_per_day") or 0)
        if max_rejects > 0 and int(summary.get("rejected_order_count") or 0) >= max_rejects:
            return "LIVE_SIM_MAX_REJECTS_HIT", ["LIVE_SIM_MAX_REJECTS_HIT"], {"summary": summary, "max_rejected_orders_per_day": max_rejects}
        return "", [], details

    def _live_sim_summary_for_guard(self) -> dict[str, Any]:
        db = TradingDatabase(str(self.db_path))
        try:
            return db.live_sim_summary(trade_date=_kst_trade_date(str(self.clock())))
        finally:
            db.close()

    def _apply_live_sim_exit_position_quantity(
        self,
        request: RuntimeOrderIntentRequest,
        broker_request: BrokerOrderRequest,
    ) -> tuple[RuntimeOrderIntentRequest, BrokerOrderRequest, str, list[str], dict[str, Any]]:
        if str(broker_request.side or "").lower() != "sell":
            return request, broker_request, "", [], {}
        if str(request.order_phase or "").lower() != "exit":
            return request, broker_request, "", [], {}
        account_id_masked = _mask_account(str(broker_request.account or ""))
        candidate_instance_id = str((request.metadata or {}).get("candidate_instance_id") or "")
        db = TradingDatabase(str(self.db_path))
        try:
            positions = [
                position
                for position in db.list_live_sim_positions(
                    code=broker_request.code,
                    account_id_masked=account_id_masked,
                    limit=50,
                )
                if str(position.get("status") or "") in {"OPEN", "PARTIAL"}
                and int(position.get("current_qty") or 0) > 0
            ]
        finally:
            db.close()
        exact = [
            position
            for position in positions
            if candidate_instance_id and str(position.get("candidate_instance_id") or "") == candidate_instance_id
        ]
        if exact:
            position = exact[0]
        elif not candidate_instance_id and len(positions) == 1:
            position = positions[0]
        elif candidate_instance_id and len(positions) == 1:
            position = positions[0]
        else:
            reason = "LIVE_SIM_EXIT_POSITION_NOT_FOUND" if not positions else "LIVE_SIM_EXIT_POSITION_AMBIGUOUS"
            return (
                request,
                broker_request,
                reason,
                [reason],
                {
                    "code": broker_request.code,
                    "account_id_masked": account_id_masked,
                    "candidate_instance_id": candidate_instance_id,
                    "open_position_count": len(positions),
                    "requested_quantity": broker_request.quantity,
                },
            )
        current_qty = max(0, int(position.get("current_qty") or 0))
        requested_qty = max(0, int(broker_request.quantity or 0))
        if current_qty <= 0:
            return (
                request,
                broker_request,
                "LIVE_SIM_EXIT_POSITION_QTY_ZERO",
                ["LIVE_SIM_EXIT_POSITION_QTY_ZERO"],
                {
                    "position_id": position.get("position_id"),
                    "requested_quantity": requested_qty,
                    "current_qty": current_qty,
                },
            )
        metadata = dict(request.metadata or {})
        full_exit = (
            bool(metadata.get("full_exit"))
            or bool(metadata.get("position_closed"))
            or float(request.exit_percent or 0.0) >= 100.0
            or int(request.remaining_quantity or -1) == 0
        )
        if full_exit:
            quantity = current_qty
            reason = "LIVE_SIM_EXIT_FULL_POSITION_QTY"
        elif request.exit_percent is not None:
            quantity = max(1, int(current_qty * max(0.0, float(request.exit_percent or 0.0)) / 100.0))
            quantity = min(current_qty, quantity)
            reason = "LIVE_SIM_EXIT_PERCENT_POSITION_QTY"
        else:
            quantity = min(current_qty, requested_qty)
            reason = "LIVE_SIM_EXIT_CAPPED_TO_POSITION_QTY" if requested_qty > current_qty else "LIVE_SIM_EXIT_REQUEST_QTY"
        if quantity <= 0:
            return (
                request,
                broker_request,
                "LIVE_SIM_EXIT_QUANTITY_ZERO",
                ["LIVE_SIM_EXIT_QUANTITY_ZERO"],
                {
                    "position_id": position.get("position_id"),
                    "requested_quantity": requested_qty,
                    "current_qty": current_qty,
                },
            )
        adjustment = {
            "position_id": position.get("position_id"),
            "candidate_instance_id": position.get("candidate_instance_id"),
            "current_qty": current_qty,
            "requested_quantity": requested_qty,
            "resolved_quantity": quantity,
            "quantity_source": reason,
        }
        metadata.update(
            {
                "live_sim_position_id": position.get("position_id"),
                "live_sim_position_current_qty": current_qty,
                "live_sim_exit_requested_qty": requested_qty,
                "live_sim_exit_resolved_qty": quantity,
                "live_sim_exit_quantity_source": reason,
            }
        )
        request_payload = {
            **request.to_dict(),
            "quantity": quantity,
            "exit_quantity": quantity,
            "position_quantity": current_qty,
            "remaining_quantity": max(0, current_qty - quantity),
            "metadata": metadata,
        }
        request = RuntimeOrderIntentRequest(**request_payload)
        broker_metadata = {
            **dict(broker_request.metadata or {}),
            **metadata,
            "exit_quantity": quantity,
            "position_quantity": current_qty,
        }
        broker_request = BrokerOrderRequest(
            **{
                **broker_request.to_dict(),
                "quantity": quantity,
                "metadata": broker_metadata,
            }
        )
        return request, broker_request, "", [], adjustment

    def _live_sim_order_record(
        self,
        request: RuntimeOrderIntentRequest,
        broker_request: BrokerOrderRequest,
        *,
        order_intent_id: str,
        trade_date: str,
        now: str,
        status: str,
        dedupe_key: str,
        reason_codes: list[str],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "order_intent_id": order_intent_id,
            "command_id": "",
            "entry_plan_id": request.entry_plan_id,
            "candidate_id": request.candidate_id,
            "virtual_order_id": request.virtual_order_id,
            "virtual_position_id": request.virtual_position_id,
            "exit_decision_id": request.exit_decision_id,
            "candidate_instance_id": str((request.metadata or {}).get("candidate_instance_id") or ""),
            "trade_date": trade_date,
            "code": broker_request.code,
            "name": str((request.metadata or {}).get("name") or ""),
            "account_id_masked": _mask_account(broker_request.account),
            "order_mode": "LIVE_SIM",
            "broker": "KIWOOM",
            "broker_env": "SIMULATION",
            "order_leg": int(request.leg_index or 1),
            "side": broker_request.side,
            "order_type": str(broker_request.order_type),
            "requested_qty": broker_request.quantity,
            "requested_price": broker_request.price,
            "submitted_qty": broker_request.quantity,
            "submitted_price": broker_request.price,
            "order_status": status,
            "updated_at": now,
            "idempotency_key": broker_request.idempotency_key,
            "dedupe_key": dedupe_key,
            "reason_codes": _unique_reason_codes(reason_codes),
            "details": details,
        }

    def _live_sim_buy_lifecycle_block(
        self,
        request: RuntimeOrderIntentRequest,
        broker_request: BrokerOrderRequest,
        *,
        lifecycle: dict[str, Any],
        reconcile: dict[str, Any],
        exit_guard: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any]]:
        db = TradingDatabase(str(self.db_path))
        try:
            account_id_masked = _mask_account(broker_request.account)
            code = str(broker_request.code or "")
            candidate_instance_id = str((request.metadata or {}).get("candidate_instance_id") or "")
            if bool(exit_guard.get("block_new_buy_if_exit_loop_unhealthy", True)):
                health = db.get_live_sim_runtime_health("exit_monitor")
                if health and str(health.get("status") or "") == "UNHEALTHY":
                    return (
                        "LIVE_SIM_BUY_BLOCKED_EXIT_MONITOR_UNHEALTHY",
                        ["LIVE_SIM_BUY_BLOCKED_EXIT_MONITOR_UNHEALTHY", "LIVE_SIM_BUY_BLOCKED_LIFECYCLE_GUARD"],
                        {"health": health},
                    )
            if bool(lifecycle.get("block_new_buy_if_cancel_scheduler_unhealthy", True)):
                health = db.get_live_sim_runtime_health("cancel_scheduler")
                if health and str(health.get("status") or "") == "UNHEALTHY":
                    return (
                        "LIVE_SIM_BUY_BLOCKED_LIFECYCLE_GUARD",
                        ["LIVE_SIM_BUY_BLOCKED_LIFECYCLE_GUARD"],
                        {"health": health},
                    )
            if bool(reconcile.get("block_new_buy_on_reconcile_failure", True)):
                health = db.get_live_sim_runtime_health("reconcile")
                if health and str(health.get("status") or "") in {"UNHEALTHY", "RECONCILE_REQUIRED"}:
                    reason = (
                        "LIVE_SIM_BUY_BLOCKED_RECONCILE_FAILURE_LIMIT"
                        if str(health.get("status") or "") == "UNHEALTHY"
                        else "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"
                    )
                    return reason, [reason], {"health": health}
                summary = db.live_sim_summary()
                if int(summary.get("reconcile_required_count") or 0) > 0:
                    return (
                        "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED",
                        ["LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"],
                        {"summary": summary},
                    )
            pending_cancel = db.find_pending_live_sim_cancel(code=code, account_id_masked=account_id_masked)
            if pending_cancel is not None and bool(lifecycle.get("block_new_order_when_cancel_pending", True)):
                return (
                    "LIVE_SIM_BUY_BLOCKED_PENDING_CANCEL",
                    ["LIVE_SIM_BUY_BLOCKED_PENDING_CANCEL", "LIVE_SIM_NEW_BUY_BLOCKED_CANCEL_PENDING"],
                    {"pending_cancel": pending_cancel},
                )
            for status in ["SUBMITTED", "ACCEPTED", "PARTIAL_FILLED"]:
                for order in db.list_live_sim_orders(status=status, code=code, side="buy", limit=50):
                    if str(order.get("account_id_masked") or "") != account_id_masked:
                        continue
                    if candidate_instance_id and str(order.get("candidate_instance_id") or "") not in {"", candidate_instance_id}:
                        continue
                    return "LIVE_SIM_BUY_BLOCKED_PENDING_ORDER", ["LIVE_SIM_BUY_BLOCKED_PENDING_ORDER"], {"pending_order": order}
            for order in db.list_live_sim_orders(status="UNKNOWN_SUBMIT", code=code, limit=50):
                if str(order.get("account_id_masked") or "") == account_id_masked:
                    return "LIVE_SIM_BUY_BLOCKED_UNKNOWN_SUBMIT", ["LIVE_SIM_BUY_BLOCKED_UNKNOWN_SUBMIT"], {"unknown_submit": order}
            for status in ["RECONCILE_REQUIRED", "CANCEL_REJECTED"]:
                for order in db.list_live_sim_orders(status=status, code=code, limit=50):
                    if str(order.get("account_id_masked") or "") == account_id_masked:
                        return "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED", ["LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"], {"order": order}
            for position in db.list_live_sim_positions(code=code, account_id_masked=account_id_masked, limit=50):
                if str(position.get("status") or "") == "RECONCILE_REQUIRED":
                    details = dict(position.get("details") or {})
                    if details.get("external_position_detected"):
                        return "LIVE_SIM_BUY_BLOCKED_EXTERNAL_POSITION", ["LIVE_SIM_BUY_BLOCKED_EXTERNAL_POSITION"], {"position": position}
                    return "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED", ["LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"], {"position": position}
            return "", [], {}
        finally:
            db.close()

    def _live_sim_shadow_small_entry_block(
        self,
        request: RuntimeOrderIntentRequest,
        broker_request: BrokerOrderRequest,
        *,
        trade_date: str,
    ) -> tuple[str, list[str], dict[str, Any]]:
        if not _is_ready_shadow_small_entry(request):
            return "", [], {}
        metadata = dict(request.metadata or {})
        if str(metadata.get("shadow_small_entry_promotion_mode") or "").lower() != "live_sim_guarded" or not bool(metadata.get("shadow_small_entry_promotion_order_enabled")):
            return (
                "SHADOW_SMALL_ENTRY_ORDER_NOT_ENABLED",
                ["SHADOW_SMALL_ENTRY_ORDER_NOT_ENABLED"],
                {"metadata": metadata},
            )
        try:
            leg_index = int(request.leg_index or metadata.get("leg_index") or 1)
        except (TypeError, ValueError):
            leg_index = 1
        if leg_index != 1:
            return (
                "SHADOW_SMALL_ENTRY_FIRST_LEG_ONLY",
                ["SHADOW_SMALL_ENTRY_FIRST_LEG_ONLY"],
                {"leg_index": leg_index},
            )
        amount = int(broker_request.quantity or 0) * max(0, int(broker_request.price or 0))
        max_per_cycle = int(metadata.get("shadow_small_entry_max_promotions_per_cycle") or 1)
        if max_per_cycle > 0 and int(metadata.get("shadow_small_entry_used_this_cycle") or 0) >= max_per_cycle:
            return "SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED", ["SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED"], {"max_per_cycle": max_per_cycle}
        max_day = int(metadata.get("shadow_small_entry_max_promotions_per_day") or 3)
        max_code_day = int(metadata.get("shadow_small_entry_max_promotions_per_code_per_day") or 1)
        max_notional_day = int(metadata.get("shadow_small_entry_max_notional_per_day") or 300000)
        db = TradingDatabase(str(self.db_path))
        try:
            day_orders = [
                order
                for order in db.list_live_sim_orders(trade_date=trade_date, side="buy", limit=1000)
                if _live_sim_order_is_shadow_small(order)
            ]
        finally:
            db.close()
        if max_day > 0 and len(day_orders) >= max_day:
            return "SHADOW_SMALL_ENTRY_MAX_PER_DAY_EXCEEDED", ["SHADOW_SMALL_ENTRY_MAX_PER_DAY_EXCEEDED"], {"used": len(day_orders), "max_day": max_day}
        code_orders = [order for order in day_orders if str(order.get("code") or "") == broker_request.code]
        if max_code_day > 0 and len(code_orders) >= max_code_day:
            return "SHADOW_SMALL_ENTRY_CODE_ALREADY_PROMOTED", ["SHADOW_SMALL_ENTRY_CODE_ALREADY_PROMOTED"], {"used": len(code_orders), "max_code_day": max_code_day}
        notional = sum(int(order.get("submitted_qty") or order.get("requested_qty") or 0) * max(0, int(order.get("submitted_price") or order.get("requested_price") or 0)) for order in day_orders)
        if max_notional_day > 0 and notional + amount > max_notional_day:
            return (
                "SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT_EXCEEDED",
                ["SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT_EXCEEDED"],
                {"used_notional": notional, "order_amount": amount, "max_notional_day": max_notional_day},
            )
        return "", [], {}

    def _save_live_sim_health(
        self,
        component: str,
        status: str,
        reason: str,
        *,
        now: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        db = TradingDatabase(str(self.db_path))
        try:
            db.save_live_sim_runtime_health(
                component,
                status=status,
                reason=reason,
                consecutive_failures=1 if status == "UNHEALTHY" else 0,
                details=details or {},
                updated_at=now,
            )
        finally:
            db.close()

    @staticmethod
    def _synthetic_gateway_status(request: BrokerOrderRequest) -> dict[str, Any]:
        return {
            "connected": True,
            "heartbeat_ok": True,
            "kiwoom_logged_in": True,
            "orderable": True,
            "account": request.account,
        }

    def _order_command_count(self, code: str, side: str, tag: str, *, order_type: int | None = None) -> int:
        trade_date = _kst_trade_date(str(self.clock()))
        counter = getattr(self.gateway_state, "daily_order_command_count", None)
        if callable(counter):
            return int(
                counter(
                    trade_date=trade_date,
                    code=code,
                    side=side,
                    tag=tag,
                    order_type=order_type,
                )
                or 0
            )
        return 0

    @staticmethod
    def _dry_run_intent_count(db: TradingDatabase, code: str, side: str, tag: str) -> int:
        rows = db.list_runtime_order_intents(code=code, limit=500)
        return sum(1 for row in rows if row.get("side") == side and (not tag or row.get("tag") == tag))

    @staticmethod
    def _trade_date(request: RuntimeOrderIntentRequest, now: str) -> str:
        if request.runtime_cycle_at:
            return request.runtime_cycle_at[:10]
        if now:
            return now[:10]
        return datetime.now(timezone.utc).date().isoformat()


def _key_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _dry_run_reject_reason(request: RuntimeOrderIntentRequest, fallback: str) -> str:
    metadata = dict(request.metadata or {})
    quantity_reason = str(metadata.get("quantity_calculation_reason") or "")
    if fallback == "QUANTITY_INVALID" and quantity_reason in {"QUANTITY_ZERO", "QUANTITY_BELOW_MIN"}:
        return quantity_reason
    return fallback


def _live_sim_account_guard(
    request: BrokerOrderRequest,
    gateway_status: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    heartbeat = dict(gateway_status.get("last_heartbeat_payload") or {})
    account = str(request.account or "")
    gateway_account = str(gateway_status.get("account") or heartbeat.get("account") or "")
    if not account:
        return {"ok": False, "reason": "ACCOUNT_GUARD_FAILED_UNKNOWN_ACCOUNT_MODE", "account_id_masked": ""}
    allowed_accounts = {str(item) for item in list(execution.get("allowed_account_numbers") or []) if str(item)}
    if allowed_accounts and account not in allowed_accounts:
        return {
            "ok": False,
            "reason": "ACCOUNT_GUARD_FAILED_ACCOUNT_NOT_ALLOWLISTED",
            "account_id_masked": _mask_account(account),
            "allowlist_size": len(allowed_accounts),
        }
    if gateway_account and gateway_account != account:
        return {
            "ok": False,
            "reason": "ACCOUNT_GUARD_FAILED_ACCOUNT_NOT_ALLOWLISTED",
            "account_id_masked": _mask_account(account),
            "gateway_account_id_masked": _mask_account(gateway_account),
        }
    raw_modes = _gateway_mode_candidates(gateway_status)
    normalized_modes = [_normalize_broker_env(value) for value in raw_modes if str(value or "")]
    normalized_modes = [value for value in normalized_modes if value]
    if any(value == "REAL" for value in normalized_modes):
        return {
            "ok": False,
            "reason": "ACCOUNT_GUARD_FAILED_REAL_ACCOUNT",
            "account_id_masked": _mask_account(account),
            "raw_modes": raw_modes,
        }
    if any(value == "SIMULATION" for value in normalized_modes):
        return {
            "ok": True,
            "reason": "ACCOUNT_GUARD_PASSED_SIMULATION",
            "account_id_masked": _mask_account(account),
            "broker_env": "SIMULATION",
            "raw_modes": raw_modes,
            "reason_codes": ["BROKER_ENV_NORMALIZED", "ACCOUNT_GUARD_PASSED_SIMULATION"],
        }
    if not raw_modes:
        return {
            "ok": False,
            "reason": "BROKER_SERVER_MODE_UNKNOWN",
            "account_id_masked": _mask_account(account),
            "raw_modes": [],
            "reason_codes": ["BROKER_SERVER_MODE_UNKNOWN"],
        }
    if bool(execution.get("fail_closed_on_account_unknown", True)):
        return {
            "ok": False,
            "reason": "BROKER_ENV_UNKNOWN",
            "account_id_masked": _mask_account(account),
            "raw_modes": raw_modes,
            "reason_codes": ["BROKER_ENV_UNKNOWN"],
        }
    return {
        "ok": True,
        "reason": "ACCOUNT_GUARD_UNKNOWN_ALLOWED_BY_CONFIG",
        "account_id_masked": _mask_account(account),
        "raw_modes": raw_modes,
    }


def _gateway_mode_candidates(gateway_status: dict[str, Any]) -> list[str]:
    heartbeat = dict(gateway_status.get("last_heartbeat_payload") or {})
    keys = [
        "account_mode",
        "account_type",
        "broker_env",
        "environment",
        "server_mode",
        "server_type",
        "server",
        "kiwoom_server",
        "mode",
    ]
    values: list[str] = []
    for source in (heartbeat, gateway_status):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)
    return values


def _normalize_broker_env(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text in {"SIM", "SIMULATION", "MOCK", "PAPER", "PAPER_TRADING", "LIVE_SIM", "DEMO", "TEST", "1"}:
        return "SIMULATION"
    if text in {"REAL", "LIVE", "PROD", "PRODUCTION", "LIVE_REAL", "0"}:
        return "REAL"
    if "모의" in str(value):
        return "SIMULATION"
    if "실전" in str(value) or "실계좌" in str(value):
        return "REAL"
    return "UNKNOWN"


def _runtime_gate_block_reason(request: RuntimeOrderIntentRequest) -> str:
    metadata = dict(request.metadata or {})
    reason_codes = {
        str(item)
        for item in list(metadata.get("reason_codes") or [])
        + list(metadata.get("risk_reason_codes") or [])
        + list(metadata.get("market_reason_codes") or [])
        + list(metadata.get("market_side_reason_codes") or [])
        + list(metadata.get("market_side_data_quality_flags") or [])
        + list(metadata.get("data_quality_flags") or [])
        + list(metadata.get("entry_risk_reason_codes") or [])
        if str(item)
    }
    sub_status = str(metadata.get("sub_status") or metadata.get("final_gate_status") or "").upper()
    if sub_status == "ENTRY_RISK_FINAL_BLOCK" or "ENTRY_RISK_FINAL_BLOCK" in reason_codes:
        return "ENTRY_RISK_FINAL_BLOCK"
    if sub_status == "DATA_INSUFFICIENT" and not _is_ready_early_small(request):
        return "DATA_INSUFFICIENT"
    if str(request.gate_status or "").upper() in {"BLOCKED", "WAIT", "TEMP_WAIT"}:
        return str(request.gate_reason or "RUNTIME_GATE_NOT_READY")
    if metadata.get("support_ready") is False or metadata.get("support_missing_reason"):
        return "DATA_INSUFFICIENT"
    if metadata.get("latest_tick_ready") is False or metadata.get("tick_stale") or metadata.get("latest_tick_stale"):
        return "DATA_INSUFFICIENT"
    if metadata.get("chase_risk") or "CHASE_RISK" in reason_codes:
        return "CHASE_RISK"
    if (
        metadata.get("late_chase_temp_wait")
        or str(metadata.get("late_chase_level") or "").lower() == "soft_block"
        or str(metadata.get("sub_status") or "").upper() == "LATE_CHASE_TEMP_WAIT"
        or "LATE_CHASE_TEMP_WAIT" in reason_codes
    ):
        return "LATE_CHASE_TEMP_WAIT"
    market_block_codes = {
        "WAIT_MARKET_CONFIRMATION_PENDING",
        "WAIT_CANDIDATE_MARKET_WEAK",
        "WAIT_CANDIDATE_MARKET_RISK_OFF",
        "WAIT_MARKET_RECOVERY_PENDING",
        "MARKET_WAIT_HYSTERESIS_HOLD",
        "MARKET_WEAK_CONFIRMED",
        "MARKET_RISK_OFF_CONFIRMED",
        "CANDIDATE_MARKET_WEAK",
        "CANDIDATE_MARKET_RISK_OFF",
        "KOSPI_MARKET_WEAK",
        "KOSDAQ_MARKET_WEAK",
        "KOSPI_MARKET_RISK_OFF",
        "KOSDAQ_MARKET_RISK_OFF",
        "GLOBAL_MARKET_RISK_OFF",
    }
    for code in market_block_codes:
        if code in reason_codes:
            return code
    market_status = str(metadata.get("candidate_market_status") or metadata.get("market_status") or "").upper()
    if market_status in {"WEAK", "RISK_OFF", "WAIT", "TEMP_WAIT", "RECOVERY_PENDING", "CONFIRMATION_PENDING"}:
        return "WAIT_CANDIDATE_MARKET_WEAK" if market_status == "WEAK" else "WAIT_MARKET_CONFIRMATION_PENDING"
    if str(metadata.get("order_eligibility") or "").upper() in {"WAIT_DATA", "BLOCKED", "WAIT"}:
        return "DATA_INSUFFICIENT"
    bad_data_codes = {
        "DATA_INSUFFICIENT",
        "INDICATOR_DATA_INSUFFICIENT",
        "INDEX_DATA_INSUFFICIENT",
        "STALE_QUOTE",
        "SIDE_BREADTH_SAMPLE_TOO_SMALL",
        "SIDE_BREADTH_VALID_QUOTE_RATIO_LOW",
    }
    has_bad_data = bool(reason_codes & bad_data_codes)
    breadth_known_bad = "candidate_breadth_ready" in metadata and metadata.get("candidate_breadth_ready") is False
    if (has_bad_data or breadth_known_bad) and not _is_ready_early_small(request):
        return "DATA_INSUFFICIENT"
    return ""


def _is_ready_early_small(request: RuntimeOrderIntentRequest) -> bool:
    metadata = dict(request.metadata or {})
    values = {
        str(request.gate_reason or "").upper(),
        str(metadata.get("ready_type") or "").upper(),
        str(metadata.get("final_status") or "").upper(),
        str(metadata.get("final_gate_status") or "").upper(),
    }
    values.update(str(item or "").upper() for item in list(metadata.get("reason_codes") or []))
    return "READY_EARLY_SMALL" in values


def _is_ready_shadow_small_entry(request: RuntimeOrderIntentRequest) -> bool:
    metadata = dict(request.metadata or {})
    values = {
        str(request.gate_reason or "").upper(),
        str(metadata.get("ready_type") or "").upper(),
        str(metadata.get("final_status") or "").upper(),
        str(metadata.get("final_gate_status") or "").upper(),
        str(metadata.get("order_eligibility") or "").upper(),
        str(metadata.get("shadow_small_entry_promotion_status") or "").upper(),
    }
    values.update(str(item or "").upper() for item in list(metadata.get("reason_codes") or []))
    return "READY_SHADOW_SMALL_ENTRY" in values or "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED" in values or "PROMOTED" in values


def _live_sim_order_is_shadow_small(order: dict[str, Any]) -> bool:
    details = dict(order.get("details") or {})
    request = dict(details.get("request") or {})
    metadata = dict(request.get("metadata") or details.get("metadata") or {})
    values = {
        str(metadata.get("ready_type") or "").upper(),
        str(metadata.get("final_gate_status") or "").upper(),
        str(metadata.get("order_eligibility") or "").upper(),
        str(metadata.get("shadow_small_entry_promotion_status") or "").upper(),
    }
    values.update(str(item or "").upper() for item in list(metadata.get("reason_codes") or []))
    return "READY_SHADOW_SMALL_ENTRY" in values or "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED" in values or "PROMOTED" in values


def _order_price_type_allowed(request: BrokerOrderRequest, execution: dict[str, Any]) -> bool:
    if int(request.quantity or 0) <= 0:
        return False
    if int(request.price or 0) <= 0:
        return False
    if bool(execution.get("allow_market_order")):
        return True
    hoga = str(request.hoga or "")
    return hoga in {"00", "0", ""}


def _public_execution_config(execution: dict[str, Any]) -> dict[str, Any]:
    hidden = {"allowed_account_numbers"}
    return {
        key: (_mask_account(str(value)) if key == "account" else value)
        for key, value in dict(execution or {}).items()
        if key not in hidden
    }


def _public_gateway_status(gateway_status: dict[str, Any]) -> dict[str, Any]:
    payload = dict(gateway_status or {})
    if payload.get("account"):
        payload["account_id_masked"] = _mask_account(str(payload.get("account") or ""))
        payload.pop("account", None)
    heartbeat = dict(payload.get("last_heartbeat_payload") or {})
    if heartbeat.get("account"):
        heartbeat["account_id_masked"] = _mask_account(str(heartbeat.get("account") or ""))
        heartbeat.pop("account", None)
    payload["last_heartbeat_payload"] = heartbeat
    return payload


def _unique_reason_codes(codes: list[str]) -> list[str]:
    result: list[str] = []
    for code in codes:
        text = str(code or "")
        if text and text not in result:
            result.append(text)
    return result


def _merge_reason_codes(order: dict[str, Any], additions: list[str]) -> list[str]:
    return _unique_reason_codes(list(order.get("reason_codes") or []) + list(additions or []))


def _mask_account(account: str) -> str:
    text = str(account or "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


def _cancel_due_reason(order: dict[str, Any], lifecycle: dict[str, Any], now: str) -> str:
    status = str(order.get("order_status") or "")
    side = str(order.get("side") or "").lower()
    started_at = str(order.get("accepted_at") or order.get("submitted_at") or order.get("updated_at") or order.get("created_at") or "")
    if not _age_exceeded(started_at, now, int(lifecycle.get("cancel_unfilled_buy_after_sec") or 60)) and side == "buy" and status in {"SUBMITTED", "ACCEPTED"}:
        return ""
    if side == "buy" and status in {"SUBMITTED", "ACCEPTED"}:
        return "unfilled_buy"
    if side == "sell" and status in {"SUBMITTED", "ACCEPTED"}:
        if _age_exceeded(started_at, now, int(lifecycle.get("cancel_unfilled_sell_after_sec") or 60)):
            return "unfilled_sell"
        return ""
    if status == "PARTIAL_FILLED":
        partial_started_at = str(order.get("last_fill_at") or started_at)
        if _age_exceeded(partial_started_at, now, int(lifecycle.get("cancel_partial_remainder_after_sec") or 90)):
            return "partial_remainder"
    return ""


def _remaining_cancel_qty(order: dict[str, Any]) -> int:
    details = dict(order.get("details") or {})
    last_fill = dict(details.get("last_fill") or {})
    if last_fill.get("remaining_qty") is not None:
        return max(0, int(last_fill.get("remaining_qty") or 0))
    return max(0, int(order.get("submitted_qty") or order.get("requested_qty") or 0))


def _cancel_reason_codes(cancel_reason: str) -> list[str]:
    mapping = {
        "unfilled_buy": "LIVE_SIM_UNFILLED_BUY_CANCEL_DUE",
        "unfilled_sell": "LIVE_SIM_UNFILLED_SELL_CANCEL_DUE",
        "partial_remainder": "LIVE_SIM_PARTIAL_REMAINDER_CANCEL_DUE",
    }
    return _unique_reason_codes([mapping.get(str(cancel_reason or ""), "LIVE_SIM_ORDER_UNFILLED_TIMEOUT")])


def _live_sim_cancel_idempotency_key(
    *,
    trade_date: str,
    broker_order_id: str,
    original_order_id: str,
    code: str,
    cancel_qty: int,
    cancel_reason: str,
    account_id_masked: str,
) -> str:
    return (
        f"runtime:livesim:cancel:{trade_date}:{account_id_masked}:{code}:"
        f"{broker_order_id}:{original_order_id}:{cancel_qty}:{cancel_reason}"
    )


def _normalize_tick(raw: Any, now: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return {
            "price": int(raw.get("price") or raw.get("current_price") or 0),
            "timestamp": str(raw.get("timestamp") or raw.get("trade_time") or raw.get("created_at") or now),
        }
    try:
        price = int(raw or 0)
    except (TypeError, ValueError):
        price = 0
    return {"price": price, "timestamp": now if price > 0 else ""}


def _tick_is_stale(tick: dict[str, Any], now: str, max_age_sec: int) -> bool:
    if int(tick.get("price") or 0) <= 0:
        return True
    timestamp = str(tick.get("timestamp") or "")
    if not timestamp:
        return True
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() > max_age_sec


def _exit_trigger(position: dict[str, Any], tick: dict[str, Any], exit_guard: dict[str, Any], now: str) -> dict[str, str] | None:
    price = int(tick.get("price") or 0)
    entry = int(position.get("entry_avg_price") or 0)
    if price <= 0 or entry <= 0:
        return None
    stop_price = int(position.get("stop_loss_price") or round(entry * (1.0 + float(exit_guard.get("stop_loss_pct") or -2.0) / 100.0)))
    take_price = int(position.get("take_profit_price") or round(entry * (1.0 + float(exit_guard.get("take_profit_pct") or 5.0) / 100.0)))
    if stop_price > 0 and price <= stop_price:
        return {"reason": "stop_loss", "reason_code": "LIVE_SIM_STOP_LOSS_TRIGGERED"}
    if take_price > 0 and price >= take_price:
        return {"reason": "take_profit", "reason_code": "LIVE_SIM_TAKE_PROFIT_TRIGGERED"}
    max_hold_at = str(position.get("max_hold_exit_at") or "")
    if max_hold_at and _time_reached(max_hold_at, now):
        return {"reason": "max_hold", "reason_code": "LIVE_SIM_MAX_HOLD_EXIT_TRIGGERED"}
    if bool(exit_guard.get("market_close_liquidation_enabled", True)) and _market_close_reached(str(exit_guard.get("market_close_liquidation_time") or "15:15"), now):
        return {"reason": "market_close_liquidation", "reason_code": "LIVE_SIM_MARKET_CLOSE_LIQUIDATION_TRIGGERED"}
    return None


def _has_active_exit_order(db: TradingDatabase, position_id: str, code: str) -> bool:
    for status in ["SUBMITTED", "ACCEPTED", "PARTIAL_FILLED", "UNKNOWN_SUBMIT"]:
        for order in db.list_live_sim_orders(status=status, code=code, side="sell", limit=100):
            details = dict(order.get("details") or {})
            request = dict(details.get("request") or {})
            metadata = dict(request.get("metadata") or {})
            if str(metadata.get("position_id") or "") == position_id:
                return True
    return False


def _reconcile_trigger_code(trigger: str) -> str:
    if str(trigger or "") == "startup":
        return "LIVE_SIM_RECONCILE_ON_STARTUP"
    if str(trigger or "") == "reconnect":
        return "LIVE_SIM_RECONCILE_ON_RECONNECT"
    return "LIVE_SIM_RECONCILE_STARTED"


def _age_exceeded(started_at: str, now: str, threshold_sec: int) -> bool:
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds() >= max(0, int(threshold_sec or 0))


def _time_reached(target: str, now: str) -> bool:
    try:
        target_dt = datetime.fromisoformat(str(target).replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return False
    if target_dt.tzinfo is None:
        target_dt = target_dt.replace(tzinfo=timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return now_dt.astimezone(timezone.utc) >= target_dt.astimezone(timezone.utc)


def _market_close_reached(close_time: str, now: str) -> bool:
    text = str(now or "")
    if "T" not in text:
        return False
    current_hhmm = text.split("T", 1)[1][:5]
    return current_hhmm >= str(close_time or "15:15")


def _kst_trade_date(timestamp: str) -> str:
    text = str(timestamp or "")
    if not text:
        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=9))).date().isoformat()
