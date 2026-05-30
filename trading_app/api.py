from __future__ import annotations

import asyncio
import json
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
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import (
    BrokerConditionEvent,
    BrokerExecutionEvent,
    BrokerOrderResult,
    GatewayCommand,
    GatewayEvent,
    utc_timestamp,
)
from trading.strategy.candidates import CandidateCollector
from trading.strategy.models import CandidateState
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.dependencies import close_database, get_settings, open_database, verify_gateway_token
from trading_app.schemas import GatewayCommandBatch, GatewayCommandIn, GatewayEventIn, HealthResponse
from trading_app.websocket import DashboardConnectionManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"

app = FastAPI(title="Trading Core API", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))

gateway_state = GatewayStateStore()
dashboard_connections = DashboardConnectionManager()


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
            "timestamp": utc_timestamp(),
        },
        "gateway": gateway_state.snapshot().to_dict(),
        "safety": {
            "default_mode": "OBSERVE",
            "live_requires_trading_allow_live": True,
            "bind_host": "127.0.0.1",
            "token_required_for_gateway": True,
        },
    }


@app.get("/api/gateway/status")
def gateway_status() -> dict[str, Any]:
    return gateway_state.snapshot().to_dict()


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
    await dashboard_connections.broadcast_json({"type": "snapshot", "snapshot": snapshot()})
    return {"accepted": accepted, "event_id": event.event_id, "type": event.type}


@app.get("/api/gateway/commands", response_model=GatewayCommandBatch)
async def gateway_commands(
    limit: int = Query(20, ge=1, le=100),
    wait_sec: float = Query(0.0, ge=0.0, le=15.0),
    _: None = Depends(verify_gateway_token),
) -> GatewayCommandBatch:
    deadline = asyncio.get_event_loop().time() + wait_sec
    commands = gateway_state.pop_commands(limit)
    while not commands and wait_sec > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        commands = gateway_state.pop_commands(limit)
    payloads = [command.to_dict() for command in commands]
    return GatewayCommandBatch(commands=payloads, count=len(payloads), timestamp=utc_timestamp())


@app.post("/api/gateway/commands")
def enqueue_gateway_command(
    command_in: GatewayCommandIn,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    command = command_in.to_gateway_command()
    accepted = gateway_state.enqueue_command(command)
    return {"accepted": accepted, "command": command.to_dict()}


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
    candidates_payload = build_candidates_snapshot(db)
    themes_payload = build_themes_snapshot(db)
    orders_payload = build_orders_snapshot(db)
    reviews_payload = build_reviews_snapshot(db)
    logs_payload = build_logs_snapshot(db)
    return {
        "timestamp": utc_timestamp(),
        "core": status_payload["core"],
        "gateway": status_payload["gateway"],
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
    elif event.type in {"gateway_log", "log"}:
        db.save_log(f"[gateway] {event.payload.get('message', '')}")
    elif event.type in {"gateway_error", "error"}:
        db.save_log(f"[gateway][WARN] {event.payload.get('message') or event.payload.get('error') or ''}")


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
