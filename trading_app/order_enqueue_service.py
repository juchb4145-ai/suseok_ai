from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from storage.db import TradingDatabase
from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerOrderRequest, GatewayCommand, new_message_id, utc_timestamp
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
    ) -> OrderEnqueueResult:
        execution = dict(execution_config or {})
        exit_guard = dict(exit_guard_config or {})
        now = str(self.clock())
        trade_date = self._trade_date(request, now)
        gateway_status = self.gateway_state.snapshot().to_dict()
        broker_request = self._broker_request_from_runtime_live_sim(request, gateway_status=gateway_status)
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
                "request": request.to_dict(),
            },
        )

        block_reason, block_codes, block_details = self._live_sim_pre_submit_block_reason(
            request,
            broker_request,
            gateway_status=gateway_status,
            execution=execution,
            exit_guard=exit_guard,
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
            return (
                f"runtime:livesim:exit:{trade_date}:{account_id_masked}:{broker_request.code}:"
                f"{request.virtual_position_id or ''}:{request.exit_decision_id or ''}:{request.exit_decision_type}:"
                f"{broker_request.price}:{broker_request.quantity}"
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
            return reason, _unique_reason_codes(["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD", reason]), {"account_guard": account_guard}
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
        if broker_request.side == "buy" and min_amount > 0 and amount < min_amount:
            return "ORDER_AMOUNT_BELOW_MIN", ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"], {"amount": amount, "min_order_amount_krw": min_amount}
        if amount > max_amount:
            return "ORDER_AMOUNT_LIMIT", ["LIVE_SIM_ORDER_BLOCKED_ACCOUNT_GUARD"], {"amount": amount, "max_order_amount_krw": max_amount}
        gate_reason = _runtime_gate_block_reason(request)
        if gate_reason:
            return gate_reason, [gate_reason], {"metadata": dict(request.metadata or {}), "gate_status": request.gate_status}
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
        }
    if not raw_modes:
        return {
            "ok": False,
            "reason": "ACCOUNT_GUARD_FAILED_SERVER_MODE_UNKNOWN",
            "account_id_masked": _mask_account(account),
            "raw_modes": [],
        }
    if bool(execution.get("fail_closed_on_account_unknown", True)):
        return {
            "ok": False,
            "reason": "ACCOUNT_GUARD_FAILED_UNKNOWN_ACCOUNT_MODE",
            "account_id_masked": _mask_account(account),
            "raw_modes": raw_modes,
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
        if str(item)
    }
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
    return ""


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


def _mask_account(account: str) -> str:
    text = str(account or "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


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
