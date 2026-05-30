from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
                metadata={"api": "/api/orders/enqueue"},
            )
            return self.enqueue_dry_run_order(intent)

        gateway_status_payload = self.gateway_state.snapshot().to_dict()
        duplicate = self.gateway_state.has_duplicate(dedupe_key)
        duplicate_of = self.gateway_state.duplicate_of(dedupe_key) if duplicate else ""
        safety = self._live_guard(requested_mode).validate(
            broker_request,
            gateway_status=gateway_status_payload,
            existing_order_command_count=self._order_command_count(broker_request.code, broker_request.side, broker_request.tag),
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
                    request=broker_request.to_dict(),
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
                existing_order_command_count=self._order_command_count(broker_request.code, broker_request.side, broker_request.tag),
                duplicate=self.gateway_state.has_duplicate(dedupe_key),
            )
            status = DRY_RUN_ACCEPTED if decision_safety.ok else DRY_RUN_REJECTED
            reason = "DRY_RUN_ORDER_INTENT_RECORDED" if decision_safety.ok else decision_safety.reason
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
                request=broker_request.to_dict(),
                response=response,
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

    def list_dry_run_orders(
        self,
        *,
        trade_date: str | None = None,
        status: str | None = None,
        code: str | None = None,
        candidate_id: int | None = None,
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
                "leg_index": request.leg_index,
                "reason": request.reason,
                "strategy_order_id": f"{request.virtual_order_id or ''}:{request.leg_index or ''}",
            },
        )

    def _runtime_idempotency_key(self, request: RuntimeOrderIntentRequest, broker_request: BrokerOrderRequest) -> str:
        trade_date = self._trade_date(request, str(self.clock()))
        if request.side == "sell":
            return (
                f"runtime:dryrun:exit:{trade_date}:{request.virtual_position_id or ''}:"
                f"{request.reason}:{broker_request.code}:{broker_request.side}:{broker_request.price}"
            )
        return (
            f"runtime:dryrun:entry:{trade_date}:{request.candidate_id or ''}:"
            f"{request.entry_plan_id or ''}:{request.virtual_order_id or ''}:"
            f"{request.leg_index or ''}:{broker_request.code}:{broker_request.side}:{broker_request.price}"
        )

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
            )
        )

    @staticmethod
    def _synthetic_gateway_status(request: BrokerOrderRequest) -> dict[str, Any]:
        return {
            "connected": True,
            "heartbeat_ok": True,
            "kiwoom_logged_in": True,
            "orderable": True,
            "account": request.account,
        }

    def _order_command_count(self, code: str, side: str, tag: str) -> int:
        count = 0
        for record in self.gateway_state.list_commands(limit=500, include_finished=True):
            command = dict(record.get("command") or {})
            payload = dict(command.get("payload") or {})
            if payload.get("code") != code or payload.get("side") != side:
                continue
            if tag and payload.get("tag") != tag:
                continue
            count += 1
        return count

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
