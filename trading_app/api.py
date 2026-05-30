from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from storage.db import TradingDatabase
from trading.broker.command_persistence import SQLiteCommandStore
from trading.broker.command_queue import ORDER_COMMAND_TYPES, CommandPriority, CommandStatus, dedupe_key_for_command
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import (
    BrokerConditionEvent,
    BrokerExecutionEvent,
    BrokerOrderRequest,
    BrokerOrderResult,
    GatewayCommand,
    GatewayEvent,
    new_message_id,
    utc_timestamp,
)
from trading.risk.safety_guard import OrderCommandSafetyGuard, OrderSafetyConfig, dedupe_key_for_order_request
from trading.strategy.candidates import CandidateCollector
from trading.strategy.models import CandidateState
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.dependencies import close_database, get_settings, open_database, verify_gateway_token
from trading_app.runtime_supervisor import RuntimeSupervisor
from trading_app.schemas import GatewayCommandBatch, GatewayCommandIn, GatewayEventIn, HealthResponse, OrderEnqueueRequest
from trading_app.websocket import DashboardConnectionManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime_supervisor.startup()
    try:
        yield
    finally:
        await runtime_supervisor.shutdown()


app = FastAPI(title="Trading Core API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def _build_gateway_state() -> GatewayStateStore:
    settings = get_settings()
    command_store = SQLiteCommandStore(
        settings.db_path,
        dedupe_retention_sec=settings.command_dedupe_retention_sec,
        history_retention_sec=settings.command_history_retention_sec,
    )
    return GatewayStateStore(
        command_store=command_store,
        expire_stale_dispatched_on_recovery=settings.command_recovery_expire_stale_dispatched,
    )


gateway_state = _build_gateway_state()
dashboard_connections = DashboardConnectionManager()


def _build_runtime_supervisor() -> RuntimeSupervisor:
    return RuntimeSupervisor(settings=get_settings(), gateway_state=gateway_state)


runtime_supervisor = _build_runtime_supervisor()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        ok=True,
        service="trading-core-api",
        mode=settings.mode,
        timestamp=utc_timestamp(),
    )


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "core": {
            "service": "trading-core-api",
            "mode": settings.mode,
            "default_order_mode": "OBSERVE",
            "live_order_enabled": settings.live_order_enabled,
            "order_guard_required": True,
            "db_path": str(settings.db_path),
            "command_dedupe_retention_sec": settings.command_dedupe_retention_sec,
            "command_history_retention_sec": settings.command_history_retention_sec,
            "command_recovery_expire_stale_dispatched": settings.command_recovery_expire_stale_dispatched,
            "runtime_enabled": settings.runtime_enabled,
            "runtime_auto_start": settings.runtime_auto_start,
            "runtime_mode": settings.runtime_mode,
            "runtime_evaluation_interval_sec": settings.runtime_evaluation_interval_sec,
            "timestamp": utc_timestamp(),
        },
        "gateway": gateway_state.snapshot().to_dict(),
        "commands": gateway_state.command_snapshot(),
        "safety": {
            "default_mode": "OBSERVE",
            "live_requires_trading_allow_live": True,
            "bind_host": "127.0.0.1",
            "token_required_for_gateway": True,
        },
    }


@app.get("/api/gateway/status")
def gateway_status() -> dict[str, Any]:
    payload = gateway_state.snapshot().to_dict()
    payload["commands"] = gateway_state.command_snapshot()
    return payload


@app.get("/api/runtime/status")
def runtime_status() -> dict[str, Any]:
    return runtime_supervisor.status()


@app.post("/api/runtime/start")
async def runtime_start(_: None = Depends(verify_gateway_token)) -> dict[str, Any]:
    return await runtime_supervisor.start()


@app.post("/api/runtime/stop")
async def runtime_stop(_: None = Depends(verify_gateway_token)) -> dict[str, Any]:
    return await runtime_supervisor.stop()


@app.post("/api/runtime/restart")
async def runtime_restart(_: None = Depends(verify_gateway_token)) -> dict[str, Any]:
    return await runtime_supervisor.restart()


@app.post("/api/runtime/cycle")
async def runtime_cycle(_: None = Depends(verify_gateway_token)) -> dict[str, Any]:
    return await runtime_supervisor.run_once(reason="manual")


@app.get("/api/runtime/snapshot")
def runtime_snapshot() -> dict[str, Any]:
    return runtime_supervisor.snapshot()


@app.get("/api/runtime/readiness")
async def runtime_readiness() -> dict[str, Any]:
    return await runtime_supervisor.readiness()


@app.get("/api/candidates")
def candidates(
    trade_date: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    db = open_database()
    try:
        return build_candidates_snapshot(db, trade_date=trade_date, limit=limit)
    finally:
        close_database(db)


@app.get("/api/themes")
def themes(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    db = open_database()
    try:
        return build_themes_snapshot(db, limit=limit)
    finally:
        close_database(db)


@app.get("/api/orders")
def orders(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    db = open_database()
    try:
        return build_orders_snapshot(db, limit=limit)
    finally:
        close_database(db)


@app.get("/api/reviews")
def reviews(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    db = open_database()
    try:
        return build_reviews_snapshot(db, limit=limit)
    finally:
        close_database(db)


@app.get("/api/snapshot")
def snapshot() -> dict[str, Any]:
    db = open_database()
    try:
        return build_dashboard_snapshot(db)
    finally:
        close_database(db)


@app.post("/api/gateway/events")
async def gateway_events(
    event_in: GatewayEventIn,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    event = event_in.to_gateway_event()
    accepted = gateway_state.record_event(event)
    if accepted:
        db = open_database()
        try:
            _persist_gateway_event(db, event)
        finally:
            close_database(db)
        await runtime_supervisor.handle_gateway_event(event)
    await dashboard_connections.broadcast_json({"type": "snapshot", "snapshot": snapshot()})
    return {"accepted": accepted, "event_id": event.event_id, "type": event.type}


@app.get("/api/gateway/commands", response_model=GatewayCommandBatch)
async def gateway_commands(
    limit: int = Query(20, ge=1, le=100),
    wait_sec: float = Query(0.0, ge=0.0, le=15.0),
    _: None = Depends(verify_gateway_token),
) -> GatewayCommandBatch:
    deadline = asyncio.get_event_loop().time() + wait_sec
    commands = gateway_state.dispatch_commands(limit)
    while not commands and wait_sec > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        commands = gateway_state.dispatch_commands(limit)
    payloads = [command.to_dict() for command in commands]
    return GatewayCommandBatch(commands=payloads, count=len(payloads), timestamp=utc_timestamp())


@app.get("/api/gateway/commands/status")
def gateway_commands_status() -> dict[str, Any]:
    return gateway_state.command_snapshot()


@app.get("/api/gateway/commands/history")
def gateway_commands_history(
    status: Optional[str] = None,
    command_type: Optional[str] = None,
    trade_date: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100000),
    include_finished: bool = True,
    include_payload: bool = False,
) -> dict[str, Any]:
    items = gateway_state.list_commands(
        status=status,
        command_type=command_type,
        trade_date=trade_date,
        limit=limit,
        offset=offset,
        include_finished=include_finished,
    )
    return {
        "summary": gateway_state.command_snapshot(),
        "items": [_command_history_item(item, include_payload=include_payload) for item in items],
    }


@app.get("/api/gateway/commands/{command_id}/events")
def gateway_command_events(command_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"command_id": command_id, "events": gateway_state.command_events(command_id, limit=limit)}


@app.get("/api/gateway/commands/{command_id}")
def gateway_command_detail(command_id: str) -> dict[str, Any]:
    record = gateway_state.get_command(command_id)
    return {
        "found": record is not None,
        "record": record.to_dict() if record else None,
        "events": gateway_state.command_events(command_id, limit=200),
    }


@app.post("/api/gateway/commands/prune")
def gateway_commands_prune(older_than_sec: int = Query(3600, ge=0, le=86400)) -> dict[str, Any]:
    removed = gateway_state.prune_commands(older_than_sec=older_than_sec)
    return {"removed": removed, "summary": gateway_state.command_snapshot()}


@app.post("/api/gateway/commands/{command_id}/cancel")
def gateway_command_cancel(command_id: str) -> dict[str, Any]:
    record = gateway_state.get_command(command_id)
    if record is None:
        return {
            "cancelled": False,
            "command_id": command_id,
            "reason": "COMMAND_NOT_FOUND",
            "summary": gateway_state.command_snapshot(),
        }
    if record.status != CommandStatus.QUEUED:
        return {
            "cancelled": False,
            "command_id": command_id,
            "reason": f"COMMAND_STATUS_{record.status.value}",
            "summary": gateway_state.command_snapshot(),
        }
    cancelled = gateway_state.cancel_command(command_id)
    return {
        "cancelled": cancelled,
        "command_id": command_id,
        "reason": "CANCELLED" if cancelled else "CANCEL_FAILED",
        "summary": gateway_state.command_snapshot(),
    }


@app.post("/api/gateway/commands")
def enqueue_gateway_command(
    command_in: GatewayCommandIn,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    command = command_in.to_gateway_command()
    result = gateway_state.enqueue_command(
        command,
        priority=command_in.priority,
        ttl_sec=command_in.ttl_sec,
        max_attempts=command_in.max_attempts,
    )
    return {
        "accepted": result.accepted,
        "reason": result.reason,
        "duplicate_of": result.duplicate_of,
        "command": command.to_dict(),
        "record": result.record.to_dict() if result.record else None,
    }


@app.post("/api/orders/enqueue")
def enqueue_order(order_in: OrderEnqueueRequest) -> dict[str, Any]:
    settings = get_settings()
    requested_mode = "DRY_RUN" if order_in.dry_run else settings.mode
    request = BrokerOrderRequest(
        account=order_in.account,
        code=order_in.code,
        quantity=order_in.quantity,
        price=order_in.price,
        side=order_in.side,
        tag=order_in.tag,
        order_type=order_in.order_type,
        hoga=order_in.hoga,
        idempotency_key=str(order_in.idempotency_key or ""),
        metadata={
            "strategy_name": order_in.strategy_name,
            "candidate_id": order_in.candidate_id,
            "reason": order_in.reason,
        },
    )
    dedupe_key = dedupe_key_for_order_request(request)
    gateway_status_payload = gateway_state.snapshot().to_dict()
    duplicate = gateway_state.has_duplicate(dedupe_key)
    duplicate_of = gateway_state.duplicate_of(dedupe_key) if duplicate else ""
    guard = OrderCommandSafetyGuard(
        OrderSafetyConfig(
            mode=requested_mode,
            live_order_enabled=settings.live_order_enabled,
            max_order_amount=settings.max_order_amount,
            max_daily_orders_per_code=settings.max_daily_orders_per_code,
        )
    )
    safety = guard.validate(
        request,
        gateway_status=gateway_status_payload,
        existing_order_command_count=_order_command_count(request.code, request.side, request.tag),
        duplicate=duplicate,
    )

    if requested_mode == "OBSERVE":
        return _order_enqueue_response(
            accepted=False,
            mode=requested_mode,
            request=request,
            dedupe_key=dedupe_key,
            status="OBSERVE_ONLY",
            reason="OBSERVE_MODE",
            safety=safety.to_dict(),
            command=None,
            duplicate_of=duplicate_of,
        )
    if requested_mode == "DRY_RUN":
        return _order_enqueue_response(
            accepted=True,
            mode=requested_mode,
            request=request,
            dedupe_key=dedupe_key,
            status="DRY_RUN_ACCEPTED",
            reason="DRY_RUN_NO_GATEWAY_SEND_ORDER",
            safety=safety.to_dict(),
            command=None,
            duplicate_of=duplicate_of,
        )
    if not safety.ok:
        return _order_enqueue_response(
            accepted=False,
            mode=requested_mode,
            request=request,
            dedupe_key=dedupe_key,
            status="REJECTED",
            reason=safety.reason,
            safety=safety.to_dict(),
            command=None,
            duplicate_of=duplicate_of,
        )

    command = GatewayCommand(
        type="send_order",
        command_id=new_message_id("cmd_order"),
        idempotency_key=str(order_in.idempotency_key or ""),
        payload={
            **request.to_dict(),
            "strategy_name": order_in.strategy_name,
            "candidate_id": order_in.candidate_id,
            "reason": order_in.reason,
        },
    )
    result = gateway_state.enqueue_command(
        command,
        priority=CommandPriority.HIGH,
        ttl_sec=settings.command_ttl_sec,
        max_attempts=settings.command_max_attempts,
        metadata={"api": "/api/orders/enqueue", "dedupe_key": dedupe_key},
    )
    return _order_enqueue_response(
        accepted=result.accepted,
        mode=requested_mode,
        request=request,
        dedupe_key=dedupe_key,
        status=result.record.status.value if result.record else "REJECTED",
        reason=result.reason or ("QUEUED" if result.accepted else "REJECTED"),
        safety=safety.to_dict(),
        command=command.to_dict(),
        record=result.record.to_dict() if result.record else None,
        duplicate_of=result.duplicate_of or duplicate_of,
    )


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await dashboard_connections.connect(websocket)
    try:
        while True:
            db = open_database()
            try:
                payload = build_dashboard_snapshot(db)
            finally:
                close_database(db)
            payload["gateway"]["dashboard_ws_client_count"] = dashboard_connections.client_count
            await dashboard_connections.send_json(websocket, {"type": "snapshot", "snapshot": payload})
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        dashboard_connections.disconnect(websocket)
    except Exception:
        dashboard_connections.disconnect(websocket)


def build_dashboard_snapshot(db: TradingDatabase) -> dict[str, Any]:
    status_payload = api_status()
    commands_payload = dict(status_payload["commands"])
    commands_payload["recent"] = gateway_state.list_commands(limit=12, include_finished=True)
    candidates_payload = build_candidates_snapshot(db)
    themes_payload = build_themes_snapshot(db)
    orders_payload = build_orders_snapshot(db)
    reviews_payload = build_reviews_snapshot(db)
    logs_payload = build_logs_snapshot(db)
    runtime_payload = _runtime_dashboard_payload(runtime_supervisor.status())
    return {
        "timestamp": utc_timestamp(),
        "core": status_payload["core"],
        "gateway": status_payload["gateway"],
        "commands": commands_payload,
        "runtime": runtime_payload,
        "safety": status_payload["safety"],
        "candidates": candidates_payload,
        "themes": themes_payload,
        "orders": orders_payload,
        "reviews": reviews_payload,
        "logs": logs_payload,
        "market_data": {
            "latest_ticks": gateway_state.latest_ticks(limit=30),
            "raw_tick_rendering": "disabled",
        },
    }


def build_candidates_snapshot(
    db: TradingDatabase,
    *,
    trade_date: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    if trade_date is None:
        trade_date = datetime.now().date().isoformat()
    candidates = db.list_candidates(trade_date=trade_date)
    candidates = sorted(candidates, key=lambda item: item.last_seen_at or item.detected_at or "", reverse=True)
    state_counts = Counter(candidate.state.value for candidate in candidates)
    wait_count = state_counts.get(CandidateState.DETECTED.value, 0) + state_counts.get(CandidateState.WATCHING.value, 0)
    block_reasons = Counter()
    items = []
    for candidate in candidates[:limit]:
        metadata = dict(candidate.metadata or {})
        gate_record = _best_gate_record(metadata)
        reason_codes = _reason_codes(metadata, gate_record)
        block_reasons.update(reason_codes)
        items.append(
            {
                "id": candidate.id,
                "trade_date": candidate.trade_date,
                "code": candidate.code,
                "name": candidate.name,
                "state": candidate.state.value,
                "block_type": candidate.block_type.value,
                "can_recover": candidate.can_recover,
                "theme_id": metadata.get("best_theme_id") or gate_record.get("theme_id", ""),
                "theme_score": _number(metadata.get("theme_score", gate_record.get("theme_score", 0))),
                "membership_score": _number(metadata.get("membership_score", gate_record.get("membership_score", 0))),
                "hybrid_score": _number(metadata.get("hybrid_score", gate_record.get("score", 0))),
                "reason_codes": reason_codes,
                "detected_at": candidate.detected_at,
                "last_seen_at": candidate.last_seen_at,
                "expires_at": candidate.expires_at,
            }
        )
    return {
        "trade_date": trade_date,
        "summary": {
            "total": len(candidates),
            "ready": state_counts.get(CandidateState.READY.value, 0),
            "blocked": state_counts.get(CandidateState.BLOCKED.value, 0),
            "wait": wait_count,
            "expired": state_counts.get(CandidateState.EXPIRED.value, 0),
            "removed": state_counts.get(CandidateState.REMOVED.value, 0),
            "top_block_reasons": [
                {"reason": reason, "count": count}
                for reason, count in block_reasons.most_common(10)
            ],
        },
        "items": items,
    }


def build_themes_snapshot(db: TradingDatabase, *, limit: int = 50) -> dict[str, Any]:
    repository = ThemeEngineRepository(db)
    rank_items = repository.get_latest_theme_rank(top_n=limit)
    themes = [_dataclass_dict(item) for item in rank_items]
    status_counts = Counter(str(item.get("status") or "") for item in themes)
    top_theme = themes[0] if themes else {}
    return {
        "summary": {
            "total": len(themes),
            "active": status_counts.get("ACTIVE", 0),
            "watch": status_counts.get("WATCH", 0),
            "top_theme": top_theme.get("theme_name", ""),
            "top_theme_score": _number(top_theme.get("theme_score", 0)),
        },
        "items": themes,
    }


def build_orders_snapshot(db: TradingDatabase, *, limit: int = 100) -> dict[str, Any]:
    order_results = _select_dicts(
        db,
        """
        SELECT id, created_at, ok, result_code, message, request_json
        FROM order_results ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    for row in order_results:
        row["ok"] = bool(row.get("ok"))
        row["request"] = _loads(row.pop("request_json", "{}"))
    executions = _select_dicts(
        db,
        """
        SELECT id, created_at, code, order_no, side, quantity, price,
               filled_quantity, remaining_quantity, tag
        FROM executions ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    positions = [
        {
            "code": item.code,
            "name": item.name,
            "holding_quantity": item.holding_quantity,
            "average_price": item.average_price,
            "current_price": item.current_price,
            "take_profit_done": item.take_profit_done,
        }
        for item in db.load_watch_items()
        if item.holding_quantity > 0 or item.average_price > 0
    ]
    virtual_orders = _select_dicts(
        db,
        """
        SELECT id, candidate_id, entry_plan_id, leg_index, weight_pct, status,
               limit_price, virtual_fill_price, fill_policy, submitted_at,
               filled_at, cancelled_at, unfilled_reason
        FROM virtual_orders ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    return {
        "summary": {
            "order_result_count": len(order_results),
            "execution_count": len(executions),
            "position_count": len(positions),
            "virtual_order_count": len(virtual_orders),
        },
        "order_results": order_results,
        "executions": executions,
        "positions": positions,
        "virtual_orders": virtual_orders,
    }


def build_reviews_snapshot(db: TradingDatabase, *, limit: int = 100) -> dict[str, Any]:
    reviews = [_dataclass_dict(review) for review in db.latest_trade_reviews(limit=limit)]
    return {
        "summary": {
            "total": len(reviews),
            "false_positive": sum(1 for item in reviews if item.get("false_positive_flag")),
            "false_negative": sum(1 for item in reviews if item.get("false_negative_flag")),
            "blocked_but_later_rallied": sum(1 for item in reviews if item.get("blocked_but_later_rallied")),
            "expired_but_later_rallied": sum(1 for item in reviews if item.get("expired_but_later_rallied")),
        },
        "items": reviews,
    }


def build_logs_snapshot(db: TradingDatabase, *, limit: int = 100) -> dict[str, Any]:
    logs = db.recent_logs(limit=limit)
    gateway_events = [event.to_dict() for event in gateway_state.recent_events(limit=50)]
    return {
        "core": logs,
        "gateway": gateway_events,
        "warnings": [line for line in logs if "WARN" in line.upper() or "ERROR" in line.upper()],
    }


def _persist_gateway_event(db: TradingDatabase, event: GatewayEvent) -> None:
    if event.type == "condition_event":
        condition_event = BrokerConditionEvent.from_dict(event.payload)
        collector = CandidateCollector(db)
        if condition_event.event_type == "remove":
            collector.handle_condition_remove(condition_event)
        else:
            collector.handle_condition_include(condition_event)
    elif event.type == "execution_event":
        db.save_execution(BrokerExecutionEvent.from_dict(event.payload))
    elif event.type == "order_result":
        db.save_order_result(BrokerOrderResult.from_dict(event.payload))
    elif event.type == "command_started":
        command_id = str(event.payload.get("command_id") or event.command_id or "")
        if command_id:
            gateway_state.ack_command(
                command_id,
                status=CommandStatus.DISPATCHED.value,
                result_payload=dict(event.payload or {}),
            )
            db.save_log(f"[gateway][command_started] {event.payload.get('command_type', '')} {command_id}")
    elif event.type == "command_ack":
        _handle_command_ack(db, event)
    elif event.type == "command_failed":
        command_id = str(event.payload.get("command_id") or event.command_id or "")
        command_type = str(event.payload.get("command_type") or "")
        retryable = bool(event.payload.get("retryable", False))
        if command_type in ORDER_COMMAND_TYPES:
            retryable = False
        if command_id:
            gateway_state.fail_command(command_id, str(event.payload.get("error") or ""), retryable=retryable)
    elif event.type == "rate_limited":
        db.save_log(
            f"[gateway][rate_limited] {event.payload.get('command_type', '')} "
            f"{event.payload.get('command_id', '')} wait={event.payload.get('wait_time_sec', '')}"
        )
    elif event.type in {"gateway_log", "log"}:
        db.save_log(f"[gateway] {event.payload.get('message', '')}")
    elif event.type in {"gateway_error", "error"}:
        message = str(event.payload.get("message") or event.payload.get("error") or "")
        command_id = str(event.payload.get("command_id") or event.command_id or "")
        if command_id:
            gateway_state.append_command_event(
                command_id,
                event.type,
                message=message,
                payload=dict(event.payload or {}),
            )
        db.save_log(f"[gateway][WARN] {message}")


def _handle_command_ack(db: TradingDatabase, event: GatewayEvent) -> None:
    payload = dict(event.payload or {})
    command_id = str(payload.get("command_id") or event.command_id or "")
    status = str(payload.get("status") or "ACKED")
    command_type = str(payload.get("command_type") or "")
    error = str(payload.get("message") or payload.get("error") or "")
    if command_id:
        if status == CommandStatus.FAILED.value:
            gateway_state.fail_command(command_id, error, retryable=False)
        else:
            gateway_state.ack_command(command_id, status=status, result_payload=payload, error=error)
    order_result = payload.get("order_result")
    if command_type == "send_order" and isinstance(order_result, dict):
        db.save_order_result(BrokerOrderResult.from_dict(order_result))


def _runtime_dashboard_payload(status: dict[str, Any]) -> dict[str, Any]:
    snapshot_payload = dict(status.get("latest_snapshot") or {})
    readiness = dict(status.get("readiness") or {})
    return {
        "enabled": status.get("enabled", False),
        "running": status.get("running", False),
        "mode": status.get("mode", "OBSERVE"),
        "order_policy": status.get("order_policy", "OBSERVE_VIRTUAL_ONLY"),
        "last_cycle_at": status.get("last_cycle_at", ""),
        "next_cycle_at": status.get("next_cycle_at", ""),
        "cycle_count": status.get("cycle_count", 0),
        "failed_cycle_count": status.get("failed_cycle_count", 0),
        "skipped_cycle_count": status.get("skipped_cycle_count", 0),
        "manual_cycle_count": status.get("manual_cycle_count", 0),
        "last_cycle_duration_ms": status.get("last_cycle_duration_ms", 0),
        "active_candidate_count": snapshot_payload.get("active_candidate_count", 0),
        "gate_result_count": snapshot_payload.get("gate_result_count", 0),
        "entry_plan_count": snapshot_payload.get("entry_plan_count", 0),
        "virtual_order_count": snapshot_payload.get("virtual_order_count", 0),
        "review_count": snapshot_payload.get("review_count", 0),
        "market_session_status": readiness.get("market_session_status", ""),
        "data_warmup_status": readiness.get("data_warmup_status", ""),
        "gate_skip_reason": readiness.get("gate_skip_reason", ""),
        "warnings": (status.get("warnings") or [])[-10:],
        "last_error": status.get("last_error", ""),
    }


def _command_history_item(item: dict[str, Any], *, include_payload: bool) -> dict[str, Any]:
    if include_payload:
        return item
    command = dict(item.get("command") or {})
    if "payload" in command:
        payload = dict(command.get("payload") or {})
        command["payload_summary"] = {
            key: payload.get(key)
            for key in ("account", "code", "side", "quantity", "price", "tag", "candidate_id")
            if key in payload
        }
        command.pop("payload", None)
    compact = dict(item)
    compact["command"] = command
    return compact


def _order_command_count(code: str, side: str, tag: str) -> int:
    count = 0
    for record in gateway_state.list_commands(limit=500, include_finished=True):
        command = dict(record.get("command") or {})
        payload = dict(command.get("payload") or {})
        if payload.get("code") != code or payload.get("side") != side:
            continue
        if tag and payload.get("tag") != tag:
            continue
        count += 1
    return count


def _order_enqueue_response(
    *,
    accepted: bool,
    mode: str,
    request: BrokerOrderRequest,
    dedupe_key: str,
    status: str,
    reason: str,
    safety: dict[str, Any],
    command: dict[str, Any] | None,
    record: dict[str, Any] | None = None,
    duplicate_of: str = "",
) -> dict[str, Any]:
    return {
        "accepted": bool(accepted),
        "mode": mode,
        "command_id": command.get("command_id") if command else "",
        "idempotency_key": request.idempotency_key,
        "dedupe_key": dedupe_key,
        "status": status,
        "reason": reason,
        "safety_checks": safety,
        "command": command,
        "record": record,
        "duplicate_of": duplicate_of,
    }


def _select_dicts(db: TradingDatabase, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    rows = db.conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _loads(value: str) -> Any:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def _best_gate_record(metadata: dict[str, Any]) -> dict[str, Any]:
    records = dict(metadata.get("gate_results_by_theme") or {})
    if not records:
        records = dict(metadata.get("block_reasons_by_theme") or {})
    best_theme_id = str(metadata.get("best_theme_id") or "")
    if best_theme_id and isinstance(records.get(best_theme_id), dict):
        return dict(records[best_theme_id])
    for value in records.values():
        if isinstance(value, dict):
            return dict(value)
    return {}


def _reason_codes(metadata: dict[str, Any], gate_record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("reason_codes", "secondary_reason_codes", "comparison_reason_codes"):
        values = gate_record.get(key) or metadata.get(key) or []
        reasons.extend(str(value) for value in list(values))
    primary = gate_record.get("primary_reason_code") or metadata.get("primary_reason_code")
    if primary:
        reasons.append(str(primary))
    if metadata.get("quality_reason"):
        reasons.append(str(metadata["quality_reason"]))
    return _dedupe(reasons)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _dataclass_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return _jsonable(dict(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value
