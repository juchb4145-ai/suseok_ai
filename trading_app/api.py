from __future__ import annotations

import asyncio
import json
import time
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
from trading.broker.command_queue import ORDER_COMMAND_TYPES, CommandPriority, CommandStatus
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import (
    BrokerConditionEvent,
    BrokerExecutionEvent,
    BrokerOrderResult,
    GatewayCommand,
    GatewayEvent,
    utc_timestamp,
)
from trading.broker.transport_metrics import (
    TRANSPORT_MODE_WEBSOCKET_MOCK,
    TransportLatencySample,
    ensure_transport_trace,
    monotonic_ms,
    payload_size_bytes,
    should_sample_transport_message,
    trace_from_payload,
    utc_now_ms,
    wall_ms,
)
from trading.broker.ws_messages import GatewayWsMessage
from trading.strategy.candidates import CandidateCollector
from trading.strategy.models import CandidateState
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.dependencies import close_database, get_settings, open_database, verify_gateway_token
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer, config_from_settings
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_supervisor import RuntimeSupervisor
from trading_app.schemas import GatewayCommandBatch, GatewayCommandIn, GatewayEventIn, HealthResponse, OrderEnqueueRequest
from trading_app.transport_latency import TransportLatencyAnalyzer, TransportLatencyConfig
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


def _order_service() -> OrderEnqueueService:
    settings = get_settings()
    return OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)


def _performance_analyzer(db: TradingDatabase) -> DryRunPerformanceAnalyzer:
    return DryRunPerformanceAnalyzer(db, config=config_from_settings(get_settings()))


def _transport_config_from_settings() -> TransportLatencyConfig:
    settings = get_settings()
    return TransportLatencyConfig(
        p95_warn_ms=settings.transport_latency_p95_warn_ms,
        p99_warn_ms=settings.transport_latency_p99_warn_ms,
        command_p95_warn_ms=settings.transport_command_p95_warn_ms,
        event_p95_warn_ms=settings.transport_event_p95_warn_ms,
        ack_p95_warn_ms=settings.transport_ack_p95_warn_ms,
        websocket_recommend_p95_ms=settings.transport_websocket_recommend_p95_ms,
        websocket_recommend_empty_poll_rate=settings.transport_websocket_recommend_empty_poll_rate,
    )


def _transport_analyzer(db: TradingDatabase) -> TransportLatencyAnalyzer:
    return TransportLatencyAnalyzer(db, config=_transport_config_from_settings())


def _transport_status_payload(db: TradingDatabase) -> dict[str, Any]:
    settings = get_settings()
    report = _transport_analyzer(db).build_report(limit=1000)
    summary = dict(report.get("summary") or {})
    recommendation = dict(report.get("websocket_recommendation") or {})
    latest_reports = db.list_gateway_transport_latency_reports(limit=1)
    recent_errors = db.latest_gateway_transport_errors(limit=10)
    gateway_snapshot = gateway_state.snapshot().to_dict()
    heartbeat_payload = gateway_snapshot.get("last_heartbeat_payload") or {}
    return {
        "transport_mode": heartbeat_payload.get("transport_mode") or "rest_long_poll",
        "metrics_enabled": settings.transport_metrics_enabled,
        "latest_summary": summary,
        "warning_flags": summary.get("warning_flags", []),
        "websocket_recommendation": recommendation,
        "recent_errors": recent_errors,
        "latest_report_id": latest_reports[0].get("report_id") if latest_reports else "",
        "gateway": {
            "reconnect_count": gateway_snapshot.get("reconnect_count", 0),
            "network_last_error": heartbeat_payload.get("gateway_network_last_error") or heartbeat_payload.get("last_error") or "",
            "last_poll_ms": heartbeat_payload.get("gateway_last_poll_ms"),
            "last_event_post_ms": heartbeat_payload.get("gateway_last_event_post_ms"),
            "poll_interval_sec": heartbeat_payload.get("gateway_poll_interval_sec"),
            "event_queue_size": heartbeat_payload.get("gateway_event_queue_size"),
            "command_queue_size": heartbeat_payload.get("gateway_command_queue_size"),
        },
    }


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
    db = open_database()
    try:
        transport_payload = _transport_status_payload(db)
    finally:
        close_database(db)
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
        "transport": _transport_dashboard_payload(transport_payload),
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


@app.get("/api/gateway/transport/status")
def gateway_transport_status() -> dict[str, Any]:
    db = open_database()
    try:
        return _transport_status_payload(db)
    finally:
        close_database(db)


@app.get("/api/gateway/transport/latency")
def gateway_transport_latency(
    trade_date: Optional[str] = None,
    direction: Optional[str] = None,
    message_type: Optional[str] = None,
    command_id: Optional[str] = None,
    event_id: Optional[str] = None,
    transport_mode: Optional[str] = None,
    experiment_id: Optional[str] = None,
    scenario: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=100000),
) -> dict[str, Any]:
    db = open_database()
    try:
        samples = db.list_gateway_transport_latency_samples(
            trade_date=trade_date,
            direction=direction,
            message_type=message_type,
            command_id=command_id,
            event_id=event_id,
            transport_mode=transport_mode,
            experiment_id=experiment_id,
            scenario=scenario,
            limit=limit,
            offset=offset,
        )
        report = _transport_analyzer(db).build_report(
            trade_date=trade_date,
            direction=direction,
            message_type=message_type,
            transport_mode=transport_mode,
            experiment_id=experiment_id,
            scenario=scenario,
            limit=10000,
        )
        return {"summary": report.get("summary", {}), "samples": samples, "filters": report.get("filters", {})}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/latency/summary")
def gateway_transport_latency_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=604800),
    group_by: Optional[str] = None,
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _transport_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        summary = dict(report.get("summary") or {})
        if group_by:
            key = {
                "direction": "by_direction",
                "message_type": "by_message_type",
                "command_type": "by_command_type",
                "event_type": "by_event_type",
            }.get(group_by, "")
            summary["group_by"] = group_by
            summary["groups"] = summary.get(key, {})
        summary["window_sec"] = window_sec
        return {"summary": summary, "websocket_recommendation": report.get("websocket_recommendation", {})}
    finally:
        close_database(db)


@app.post("/api/gateway/transport/latency/rebuild")
def gateway_transport_latency_rebuild(
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: bool = False,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        saved = db.save_gateway_transport_latency_report(report) if persist else None
        export_paths = analyzer.export_report(report) if export else {}
        return {"report": report, "saved": saved, "export_paths": export_paths}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/latency/reports")
def gateway_transport_latency_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
) -> dict[str, Any]:
    db = open_database()
    try:
        return {"items": db.list_gateway_transport_latency_reports(limit=limit, offset=offset)}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/latency/reports/{report_id}")
def gateway_transport_latency_report_detail(report_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        report = db.get_gateway_transport_latency_report(report_id)
        return {"found": report is not None, "report": report}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/latency/export")
def gateway_transport_latency_export(
    trade_date: Optional[str] = None,
    format: str = "json",
    persist: bool = False,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    if format not in {"json", "csv", "md", "all"}:
        format = "json"
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        if persist:
            db.save_gateway_transport_latency_report(report)
        formats = ["json", "csv", "md"] if format == "all" else [format]
        return {"report_id": report["report_id"], "export_paths": analyzer.export_report(report, formats=formats)}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/experiments")
def gateway_transport_experiments(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        items = []
        for item in db.list_gateway_transport_experiments(limit=limit, offset=offset):
            comparison = analyzer.build_transport_comparison_report(
                experiment_id=item.get("experiment_id"),
                scenario=item.get("scenario"),
            )
            items.append(
                {
                    **item,
                    "latest_recommendation": comparison.get("websocket_recommendation", {}).get("recommendation", ""),
                    "sample_counts": comparison.get("sample_counts", {}),
                }
            )
        return {"items": items}
    finally:
        close_database(db)


@app.post("/api/gateway/transport/experiments/rebuild")
def gateway_transport_experiment_rebuild(
    experiment_id: Optional[str] = None,
    scenario: Optional[str] = None,
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: bool = False,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        report = analyzer.build_transport_comparison_report(
            trade_date=trade_date,
            experiment_id=experiment_id,
            scenario=scenario,
        )
        saved = db.save_gateway_transport_latency_report(report) if persist else None
        export_paths = analyzer.export_report(report, formats=["json", "md"]) if export else {}
        return {"report": report, "saved": saved, "export_paths": export_paths}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/experiments/{experiment_id}")
def gateway_transport_experiment_detail(experiment_id: str, scenario: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        report = _transport_analyzer(db).build_transport_comparison_report(
            experiment_id=experiment_id,
            scenario=scenario,
        )
        return {"found": bool(report.get("sample_counts", {}).get("rest_long_poll") or report.get("sample_counts", {}).get("websocket_mock")), "report": report}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/websocket-decision")
def gateway_transport_websocket_decision(trade_date: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        payload = dict(report.get("websocket_recommendation", {}))
        latest = db.list_gateway_transport_experiments(limit=1)
        latest_comparison = None
        if latest:
            latest_comparison = analyzer.build_transport_comparison_report(
                trade_date=trade_date,
                experiment_id=latest[0].get("experiment_id"),
                scenario=latest[0].get("scenario"),
            )
            payload["latest_comparison_report"] = latest_comparison
            payload["websocket_mock_recommendation"] = latest_comparison.get("websocket_recommendation", {})
        payload["real_gateway_switch_ready"] = False
        payload.setdefault("blockers", [])
        payload["blockers"] = list(payload["blockers"]) + ["REAL_GATEWAY_WEBSOCKET_NOT_ENABLED_IN_PR9"]
        return payload
    finally:
        close_database(db)


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


@app.get("/api/runtime/orders/dry-run/summary")
def runtime_dry_run_order_summary(trade_date: Optional[str] = None) -> dict[str, Any]:
    return _order_service().dry_run_summary(trade_date=trade_date)


@app.get("/api/runtime/orders/dry-run")
def runtime_dry_run_orders(
    trade_date: Optional[str] = None,
    status: Optional[str] = None,
    code: Optional[str] = None,
    candidate_id: Optional[int] = None,
    side: Optional[str] = None,
    order_phase: Optional[str] = None,
    virtual_position_id: Optional[int] = None,
    exit_decision_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return _order_service().list_dry_run_orders(
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
    )


@app.get("/api/runtime/orders/dry-run/{intent_id}")
def runtime_dry_run_order_detail(intent_id: str) -> dict[str, Any]:
    payload = _order_service().get_dry_run_order(intent_id)
    if not payload:
        return {"intent_id": intent_id, "record": None, "events": [], "linked": {}, "found": False}
    payload["found"] = True
    return payload


@app.get("/api/runtime/performance/dry-run")
def runtime_dry_run_performance(
    trade_date: Optional[str] = None,
    strategy_name: Optional[str] = None,
    code: Optional[str] = None,
    theme_name: Optional[str] = None,
    side: Optional[str] = None,
    order_phase: Optional[str] = None,
    include_rejected: bool = True,
    include_duplicates: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _performance_analyzer(db).build_report(
            trade_date=trade_date,
            strategy_name=strategy_name,
            code=code,
            theme_name=theme_name,
            side=side,
            order_phase=order_phase,
            include_rejected=include_rejected,
            include_duplicates=include_duplicates,
            limit=limit,
            offset=offset,
        )
    finally:
        close_database(db)


@app.post("/api/runtime/performance/dry-run/rebuild")
def rebuild_runtime_dry_run_performance(
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _performance_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        persisted = analyzer.persist_report(report) if persist else None
        exports = analyzer.export_report(report, fmt=format) if export else {}
        return {
            "report_id": report["report_id"],
            "persisted": persisted is not None,
            "exported": exports,
            "report": report,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/performance/dry-run/reports")
def runtime_dry_run_performance_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        return {"items": db.list_dry_run_performance_reports(limit=limit, offset=offset)}
    finally:
        close_database(db)


@app.get("/api/runtime/performance/dry-run/reports/{report_id}")
def runtime_dry_run_performance_report_detail(report_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        report = db.get_dry_run_performance_report(report_id)
        if report is None:
            return {"report_id": report_id, "found": False}
        report["found"] = True
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/performance/dry-run/export")
def export_runtime_dry_run_performance(
    trade_date: Optional[str] = None,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    persist: bool = False,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _performance_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        persisted = analyzer.persist_report(report) if persist else None
        return {
            "report_id": report["report_id"],
            "persisted": persisted is not None,
            "exports": analyzer.export_report(report, fmt=format),
        }
    finally:
        close_database(db)


@app.get("/api/runtime/performance/dry-run/false-signals")
def runtime_dry_run_false_signals(
    trade_date: Optional[str] = None,
    type: str = Query("all", pattern="^(false_positive|false_negative|opportunity_loss|all)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _performance_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        items = list(report.get("items") or [])
        if type == "false_positive":
            items = [item for item in items if item.get("dry_run_false_positive_type")]
        elif type == "false_negative":
            items = [item for item in items if item.get("dry_run_false_negative_type")]
        elif type == "opportunity_loss":
            items = [item for item in items if item.get("opportunity_loss_type")]
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "summary": report.get("false_signal_summary", {}),
            "type": type,
            "total": len(items),
            "items": items[start:end],
        }
    finally:
        close_database(db)


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
    event = _event_with_trace(
        event_in.to_gateway_event(),
        {"transport_mode": "rest_long_poll"},
    )
    return await _process_gateway_event(event)


async def _process_gateway_event(event: GatewayEvent) -> dict[str, Any]:
    core_received_at = utc_now_ms()
    core_received_monotonic = monotonic_ms()
    event = _event_with_trace(
        event,
        {
            "core_event_received_at_utc": core_received_at,
            "core_event_received_monotonic_ms": core_received_monotonic,
        },
    )
    accepted = gateway_state.record_event(event)
    persist_ms = 0.0
    runtime_forward_ms = 0.0
    if accepted:
        db = open_database()
        try:
            persist_started = time.perf_counter()
            _persist_gateway_event(db, event)
            persist_ms = (time.perf_counter() - persist_started) * 1000.0
            event = _event_with_trace(
                event,
                {
                    "core_event_persisted_at_utc": utc_now_ms(),
                    "core_event_persisted_monotonic_ms": monotonic_ms(),
                },
            )
            _save_gateway_event_transport_sample(
                db,
                event,
                accepted=True,
                core_receive_ms=wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
                core_persist_ms=persist_ms,
            )
        finally:
            close_database(db)
        runtime_started = time.perf_counter()
        await runtime_supervisor.handle_gateway_event(event)
        runtime_forward_ms = (time.perf_counter() - runtime_started) * 1000.0
    else:
        db = open_database()
        try:
            _save_gateway_event_transport_sample(
                db,
                event,
                accepted=False,
                core_receive_ms=wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
                core_persist_ms=persist_ms,
                error="DUPLICATE_OR_REJECTED_EVENT",
            )
        finally:
            close_database(db)
    await dashboard_connections.broadcast_json({"type": "snapshot", "snapshot": snapshot()})
    return {
        "accepted": accepted,
        "event_id": event.event_id,
        "type": event.type,
        "transport": {
            "core_receive_ms": wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
            "core_persist_ms": persist_ms,
            "runtime_forward_ms": runtime_forward_ms,
        },
    }


@app.get("/api/gateway/commands", response_model=GatewayCommandBatch)
async def gateway_commands(
    limit: int = Query(20, ge=1, le=100),
    wait_sec: float = Query(0.0, ge=0.0, le=15.0),
    _: None = Depends(verify_gateway_token),
) -> GatewayCommandBatch:
    poll_received_at = utc_now_ms()
    poll_started = time.perf_counter()
    deadline = asyncio.get_event_loop().time() + wait_sec
    commands = gateway_state.dispatch_commands(limit)
    while not commands and wait_sec > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        commands = gateway_state.dispatch_commands(limit)
    response_at = utc_now_ms()
    long_poll_wait_ms = (time.perf_counter() - poll_started) * 1000.0
    payloads = [
        _command_dict_with_trace(
            command,
            {
                "core_command_long_poll_request_at_utc": poll_received_at,
                "core_command_long_poll_response_at_utc": response_at,
                "core_command_long_poll_response_monotonic_ms": monotonic_ms(),
                "long_poll_wait_ms": long_poll_wait_ms,
                "long_poll_wait_sec": wait_sec,
            },
        )
        for command in commands
    ]
    db = open_database()
    try:
        if payloads:
            for payload in payloads:
                _save_command_poll_transport_sample(
                    db,
                    payload,
                    long_poll_wait_ms=long_poll_wait_ms,
                    poll_received_at=poll_received_at,
                    response_at=response_at,
                )
        else:
            _save_empty_command_poll_transport_sample(
                db,
                long_poll_wait_ms=long_poll_wait_ms,
                wait_sec=wait_sec,
                poll_received_at=poll_received_at,
                response_at=response_at,
            )
    finally:
        close_database(db)
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
    return _order_service().enqueue_order(order_in).to_dict()


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


@app.websocket("/ws/gateway/transport")
async def gateway_transport_ws(websocket: WebSocket) -> None:
    if not _valid_gateway_ws_token(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_id = f"ws_session_{int(time.time() * 1000)}"
    connection_id = f"ws_conn_{id(websocket)}"
    sequence = 0
    await websocket.send_json(
        GatewayWsMessage(
            type="hello_ack",
            source="core",
            payload={
                "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
                "websocket_session_id": session_id,
                "real_gateway_switch_ready": False,
            },
            metadata={"connection_id": connection_id, "websocket_session_id": session_id},
        ).to_dict()
    )
    try:
        while True:
            receive_started = time.perf_counter()
            raw = await websocket.receive_json()
            receive_ms = (time.perf_counter() - receive_started) * 1000.0
            message = GatewayWsMessage.from_dict(raw)
            sequence = message.sequence or sequence + 1
            metadata = {
                **dict(message.metadata or {}),
                "connection_id": connection_id,
                "websocket_session_id": session_id,
                "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
                "ws_receive_ms": receive_ms,
                "ws_message_sequence": sequence,
            }
            if message.type == "hello":
                await websocket.send_json(
                    GatewayWsMessage(
                        type="hello_ack",
                        trace_id=message.trace_id,
                        source="core",
                        payload={"transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK, "websocket_session_id": session_id},
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict()
                )
            elif message.type == "ping":
                await websocket.send_json(
                    GatewayWsMessage(
                        type="pong",
                        trace_id=message.trace_id,
                        source="core",
                        payload={"received_at": utc_now_ms()},
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict()
                )
            elif message.type == "ready_for_commands":
                limit = int(message.payload.get("limit") or 20)
                commands = gateway_state.dispatch_commands(limit=max(1, min(limit, 100)))
                sent_at = utc_now_ms()
                payloads = [
                    _ws_command_dict_with_trace(
                        command,
                        {
                            **metadata,
                            "core_command_ws_send_at_utc": sent_at,
                            "core_command_ws_send_monotonic_ms": monotonic_ms(),
                            "experiment_id": metadata.get("experiment_id", ""),
                            "scenario": metadata.get("scenario", ""),
                        },
                    )
                    for command in commands
                ]
                db = open_database()
                try:
                    for payload in payloads:
                        _save_ws_command_transport_sample(
                            db,
                            payload,
                            sent_at=sent_at,
                            metadata=metadata,
                        )
                finally:
                    close_database(db)
                await websocket.send_json(
                    GatewayWsMessage(
                        type="core_command_batch",
                        trace_id=message.trace_id,
                        source="core",
                        payload={"commands": payloads, "count": len(payloads), "timestamp": sent_at},
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict()
                )
            elif message.type in {"gateway_event", "heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
                event = _gateway_event_from_ws_message(message, metadata=metadata)
                result = await _process_gateway_event(event)
                await websocket.send_json(
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=result.get("event_id", ""),
                        command_id=event.command_id,
                        payload=result,
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict()
                )
            else:
                event = GatewayEvent(
                    type="transport_error",
                    source=message.source,
                    payload={"message": f"unsupported websocket message type: {message.type}", "metadata": metadata},
                )
                await _process_gateway_event(event)
    except WebSocketDisconnect:
        return



def build_dashboard_snapshot(db: TradingDatabase) -> dict[str, Any]:
    status_payload = api_status()
    commands_payload = dict(status_payload["commands"])
    commands_payload["recent"] = gateway_state.list_commands(limit=12, include_finished=True)
    candidates_payload = build_candidates_snapshot(db)
    themes_payload = build_themes_snapshot(db)
    orders_payload = build_orders_snapshot(db)
    reviews_payload = build_reviews_snapshot(db)
    logs_payload = build_logs_snapshot(db)
    transport_payload = dict(status_payload.get("transport") or _transport_dashboard_payload(_transport_status_payload(db)))
    transport_experiment_payload = _transport_experiment_dashboard_payload(db)
    runtime_payload = _runtime_dashboard_payload(runtime_supervisor.status())
    dry_run_orders_payload = {
        "summary": db.runtime_order_intent_summary(),
        "items": db.list_runtime_order_intents(limit=20),
        "recent_sell": db.list_runtime_order_intents(side="sell", order_phase="exit", limit=20),
    }
    dry_run_performance_report = _performance_analyzer(db).build_report(limit=10)
    dry_run_performance_payload = {
        "generated_at": dry_run_performance_report.get("generated_at", ""),
        "trade_date": dry_run_performance_report.get("trade_date", ""),
        **{
            key: dry_run_performance_report.get("summary", {}).get(key)
            for key in [
                "total_lifecycle_count",
                "completed_lifecycle_count",
                "win_rate",
                "avg_realized_return_pct",
                "false_positive_count",
                "false_negative_count",
                "opportunity_loss_count",
                "live_would_pass_win_rate",
                "live_would_reject_but_rallied_count",
            ]
        },
        "top_false_positive_types": dry_run_performance_report.get("false_signal_summary", {}).get("top_false_positive_types", []),
        "top_false_negative_types": dry_run_performance_report.get("false_signal_summary", {}).get("top_false_negative_types", []),
        "top_reject_reasons_with_rally": dry_run_performance_report.get("false_signal_summary", {}).get(
            "top_live_reject_reasons_with_rally",
            [],
        ),
        "bad_cases": [
            item
            for item in dry_run_performance_report.get("items", [])
            if item.get("dry_run_false_positive_type") or item.get("opportunity_loss_type")
        ][:10],
    }
    runtime_payload["dry_run_orders"] = dry_run_orders_payload
    runtime_payload["dry_run_performance"] = dry_run_performance_payload
    return {
        "timestamp": utc_timestamp(),
        "core": status_payload["core"],
        "gateway": status_payload["gateway"],
        "commands": commands_payload,
        "transport": transport_payload,
        "transport_experiment": transport_experiment_payload,
        "runtime": runtime_payload,
        "dry_run_orders": dry_run_orders_payload,
        "dry_run_performance": dry_run_performance_payload,
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


def _event_with_trace(event: GatewayEvent, trace_updates: dict[str, Any]) -> GatewayEvent:
    payload = ensure_transport_trace(
        event.payload,
        trace_id=trace_from_payload(event.payload).get("trace_id") or f"trace:{event.event_id}",
        process="core",
        extra=trace_updates,
    )
    data = event.to_dict()
    data["payload"] = payload
    return GatewayEvent.from_dict(data)


def _command_dict_with_trace(command: GatewayCommand, trace_updates: dict[str, Any]) -> dict[str, Any]:
    data = command.to_dict()
    trace = trace_from_payload(data.get("payload") or {})
    payload = ensure_transport_trace(
        data.get("payload") or {},
        trace_id=trace.get("trace_id") or f"trace:{command.command_id}",
        process="core",
        extra={
            "core_command_created_at_utc": command.timestamp,
            "core_command_dispatched_at_utc": trace_updates.get("core_command_long_poll_response_at_utc"),
            **trace_updates,
        },
    )
    data["payload"] = payload
    return data


def _save_gateway_event_transport_sample(
    db: TradingDatabase,
    event: GatewayEvent,
    *,
    accepted: bool,
    core_receive_ms: Optional[float],
    core_persist_ms: Optional[float],
    error: str = "",
) -> None:
    settings = get_settings()
    if not settings.transport_metrics_enabled:
        return
    sample_key = event.event_id or event.command_id or event.request_id
    if not should_sample_transport_message(
        message_type=event.type,
        sample_key=sample_key,
        price_tick_rate=settings.transport_metrics_sample_price_tick_rate,
        heartbeat_rate=settings.transport_metrics_sample_heartbeat_rate,
    ):
        return
    trace = trace_from_payload(event.payload)
    sample = TransportLatencySample.from_gateway_event_trace(
        event_type=event.type,
        event_id=event.event_id,
        request_id=event.request_id,
        command_id=event.command_id or str(event.payload.get("command_id") or ""),
        source=event.source,
        trace=trace,
        payload_size=payload_size_bytes(event.to_dict()),
        success=accepted and not error,
        error=error or str(event.payload.get("error") or ""),
        core_receive_ms=core_receive_ms,
        core_persist_ms=core_persist_ms,
        metadata={
            "status": event.payload.get("status"),
            "result_code": event.payload.get("result_code"),
            "transport_mode": event.payload.get("transport_mode") or trace.get("transport_mode") or "rest_long_poll",
        },
    )
    db.save_gateway_transport_latency_sample(sample.to_dict())


def _save_command_poll_transport_sample(
    db: TradingDatabase,
    command_payload: dict[str, Any],
    *,
    long_poll_wait_ms: float,
    poll_received_at: str,
    response_at: str,
) -> None:
    settings = get_settings()
    if not settings.transport_metrics_enabled:
        return
    payload = dict(command_payload.get("payload") or {})
    trace = trace_from_payload(payload)
    total_wall = wall_ms(command_payload.get("timestamp"), response_at) or long_poll_wait_ms
    core_dispatch_wait = max(0.0, total_wall - long_poll_wait_ms) if total_wall is not None else None
    sample = TransportLatencySample(
        sample_id=f"lat_poll_{command_payload.get('command_id')}_{int(time.time() * 1000)}",
        trace_id=str(trace.get("trace_id") or f"trace:{command_payload.get('command_id')}"),
        trade_date=str(response_at)[:10],
        direction="core_to_gateway",
        message_type=str(command_payload.get("type") or ""),
        command_id=str(command_payload.get("command_id") or ""),
        request_id=str(command_payload.get("request_id") or ""),
        source=str(command_payload.get("source") or "core"),
        created_at=str(command_payload.get("timestamp") or poll_received_at),
        completed_at=response_at,
        payload_size_bytes=payload_size_bytes(command_payload),
        stage_ms={
            "long_poll_wait_ms": long_poll_wait_ms,
            "core_dispatch_wait_ms": core_dispatch_wait,
        },
        total_wall_ms=total_wall,
        core_dispatch_wait_ms=core_dispatch_wait,
        long_poll_wait_ms=long_poll_wait_ms,
        metadata={
            **trace,
            "command_count": 1,
            "poll_received_at": poll_received_at,
        },
    )
    db.save_gateway_transport_latency_sample(sample.to_dict())


def _save_empty_command_poll_transport_sample(
    db: TradingDatabase,
    *,
    long_poll_wait_ms: float,
    wait_sec: float,
    poll_received_at: str,
    response_at: str,
) -> None:
    settings = get_settings()
    if not settings.transport_metrics_enabled:
        return
    now_key = int(time.time() * 1000000)
    sample = TransportLatencySample(
        sample_id=f"lat_empty_poll_{now_key}",
        trace_id=f"trace:empty_poll:{now_key}",
        trade_date=str(response_at)[:10],
        direction="core_to_gateway",
        message_type="command_poll_empty",
        source="core",
        created_at=poll_received_at,
        completed_at=response_at,
        stage_ms={"long_poll_wait_ms": long_poll_wait_ms},
        total_wall_ms=long_poll_wait_ms,
        long_poll_wait_ms=long_poll_wait_ms,
        metadata={
            "wait_sec": wait_sec,
            "command_count": 0,
            "transport_mode": "rest_long_poll",
        },
    )
    db.save_gateway_transport_latency_sample(sample.to_dict())


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
        "dry_run_order_sink_enabled": snapshot_payload.get("dry_run_order_sink_enabled", False),
        "dry_run_order_policy": snapshot_payload.get("dry_run_order_policy", status.get("order_policy", "")),
        "dry_run_order_intent_count": snapshot_payload.get("dry_run_order_intent_count", 0),
        "dry_run_entry_order_intent_count": snapshot_payload.get("dry_run_entry_order_intent_count", 0),
        "dry_run_exit_order_intent_count": snapshot_payload.get("dry_run_exit_order_intent_count", 0),
        "dry_run_sell_order_intent_count": snapshot_payload.get("dry_run_sell_order_intent_count", 0),
        "dry_run_order_accepted_count": snapshot_payload.get("dry_run_order_accepted_count", 0),
        "dry_run_order_rejected_count": snapshot_payload.get("dry_run_order_rejected_count", 0),
        "dry_run_order_duplicate_count": snapshot_payload.get("dry_run_order_duplicate_count", 0),
        "dry_run_exit_accepted_count": snapshot_payload.get("dry_run_exit_accepted_count", 0),
        "dry_run_exit_rejected_count": snapshot_payload.get("dry_run_exit_rejected_count", 0),
        "dry_run_exit_duplicate_count": snapshot_payload.get("dry_run_exit_duplicate_count", 0),
        "dry_run_order_live_would_pass_count": snapshot_payload.get("dry_run_order_live_would_pass_count", 0),
        "dry_run_order_live_would_reject_count": snapshot_payload.get("dry_run_order_live_would_reject_count", 0),
        "dry_run_exit_live_would_pass_count": snapshot_payload.get("dry_run_exit_live_would_pass_count", 0),
        "dry_run_exit_live_would_reject_count": snapshot_payload.get("dry_run_exit_live_would_reject_count", 0),
        "last_dry_run_order_intent_at": snapshot_payload.get("last_dry_run_order_intent_at", ""),
        "last_dry_run_order_reject_reason": snapshot_payload.get("last_dry_run_order_reject_reason", ""),
        "last_dry_run_exit_order_intent_at": snapshot_payload.get("last_dry_run_exit_order_intent_at", ""),
        "last_dry_run_exit_order_reject_reason": snapshot_payload.get("last_dry_run_exit_order_reject_reason", ""),
        "market_session_status": readiness.get("market_session_status", ""),
        "data_warmup_status": readiness.get("data_warmup_status", ""),
        "gate_skip_reason": readiness.get("gate_skip_reason", ""),
        "warnings": (status.get("warnings") or [])[-10:],
        "last_error": status.get("last_error", ""),
    }


def _transport_dashboard_payload(status: dict[str, Any]) -> dict[str, Any]:
    summary = dict(status.get("latest_summary") or {})
    recommendation = dict(status.get("websocket_recommendation") or {})
    gateway = dict(status.get("gateway") or {})
    reason = ""
    reasons = recommendation.get("reasons") or []
    if reasons:
        reason = str(reasons[0])
    return {
        "mode": status.get("transport_mode", "rest_long_poll"),
        "metrics_enabled": status.get("metrics_enabled", True),
        "event_latency_p95_ms": summary.get("event_latency_p95_ms", 0),
        "command_latency_p95_ms": summary.get("command_latency_p95_ms", 0),
        "ack_latency_p95_ms": summary.get("ack_latency_p95_ms", 0),
        "long_poll_wait_p95_ms": summary.get("long_poll_wait_p95_ms", 0),
        "gateway_execute_p95_ms": summary.get("gateway_execute_p95_ms", 0),
        "rate_limit_wait_p95_ms": summary.get("rate_limit_wait_p95_ms", 0),
        "empty_poll_rate": summary.get("empty_poll_rate", 0),
        "reconnect_count": gateway.get("reconnect_count", 0),
        "transport_error_count": summary.get("transport_error_count", 0),
        "websocket_recommended": bool(recommendation.get("should_switch")),
        "websocket_recommendation": recommendation.get("recommendation", "KEEP_REST_LONG_POLL"),
        "websocket_recommendation_reason": reason,
        "latest_report_id": status.get("latest_report_id", ""),
        "warning_flags": summary.get("warning_flags", []),
        "recent_errors": status.get("recent_errors", [])[:10],
    }


def _transport_experiment_dashboard_payload(db: TradingDatabase) -> dict[str, Any]:
    experiments = db.list_gateway_transport_experiments(limit=1)
    if not experiments:
        return {
            "latest_experiment_id": "",
            "latest_scenario": "",
            "recommendation": "NO_EXPERIMENT",
            "real_gateway_switch_ready": False,
            "blockers": ["NO_MOCK_EXPERIMENT_DATA"],
            "sample_counts": {},
        }
    latest = experiments[0]
    report = _transport_analyzer(db).build_transport_comparison_report(
        experiment_id=latest.get("experiment_id"),
        scenario=latest.get("scenario"),
    )
    rest = report.get("rest_summary", {})
    ws = report.get("websocket_summary", {})
    delta = report.get("delta", {})
    recommendation = report.get("websocket_recommendation", {})
    return {
        "latest_experiment_id": latest.get("experiment_id", ""),
        "latest_scenario": latest.get("scenario", ""),
        "rest_command_p95_ms": rest.get("command_latency_p95_ms", 0),
        "websocket_command_p95_ms": ws.get("command_latency_p95_ms", 0),
        "command_p95_delta_ms": delta.get("command_p95_delta_ms", 0),
        "rest_ack_p95_ms": rest.get("ack_latency_p95_ms", 0),
        "websocket_ack_p95_ms": ws.get("ack_latency_p95_ms", 0),
        "ack_p95_delta_ms": delta.get("ack_p95_delta_ms", 0),
        "rest_event_p95_ms": rest.get("event_latency_p95_ms", 0),
        "websocket_event_p95_ms": ws.get("event_latency_p95_ms", 0),
        "event_p95_delta_ms": delta.get("event_p95_delta_ms", 0),
        "recommendation": recommendation.get("recommendation", "KEEP_REST_LONG_POLL"),
        "real_gateway_switch_ready": False,
        "blockers": recommendation.get("blockers", []),
        "sample_counts": report.get("sample_counts", {}),
    }


def _valid_gateway_ws_token(websocket: WebSocket) -> bool:
    expected = get_settings().local_token
    provided = str(websocket.query_params.get("token") or websocket.headers.get("x-local-token") or "")
    authorization = str(websocket.headers.get("authorization") or "")
    if authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    return bool(expected and provided == expected)


def _gateway_event_from_ws_message(message: GatewayWsMessage, *, metadata: dict[str, Any]) -> GatewayEvent:
    payload = dict(message.payload or {})
    if message.type == "gateway_event" and isinstance(payload.get("event"), dict):
        event = GatewayEvent.from_dict(payload["event"])
    elif message.type == "gateway_event":
        event_type = str(payload.get("type") or "gateway_event")
        event = GatewayEvent(
            type=event_type,
            payload=dict(payload.get("payload") or payload),
            event_id=str(payload.get("event_id") or message.event_id or ""),
            request_id=str(payload.get("request_id") or ""),
            source=message.source,
            command_id=str(payload.get("command_id") or message.command_id or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
    else:
        event = GatewayEvent(
            type=message.type,
            payload=payload,
            event_id=message.event_id or "",
            request_id=str(payload.get("request_id") or ""),
            source=message.source,
            command_id=message.command_id or str(payload.get("command_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
    trace_payload = ensure_transport_trace(
        event.payload,
        trace_id=message.trace_id or trace_from_payload(event.payload).get("trace_id"),
        process="gateway",
        extra={
            **metadata,
            "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
            "gateway_ws_message_id": message.message_id,
            "gateway_ws_message_type": message.type,
            "gateway_ws_message_timestamp": message.timestamp,
            "gateway_ws_message_sequence": message.sequence,
            "core_ws_received_at_utc": utc_now_ms(),
        },
    )
    data = event.to_dict()
    data["payload"] = trace_payload
    data["event_id"] = event.event_id or message.event_id or f"evt_ws_{message.message_id}"
    data["source"] = event.source or message.source
    data["command_id"] = event.command_id or message.command_id
    return GatewayEvent.from_dict(data)


def _ws_command_dict_with_trace(command: GatewayCommand, trace_updates: dict[str, Any]) -> dict[str, Any]:
    data = command.to_dict()
    trace = trace_from_payload(data.get("payload") or {})
    payload = ensure_transport_trace(
        data.get("payload") or {},
        trace_id=trace.get("trace_id") or f"trace:{command.command_id}",
        process="core",
        extra={
            "core_command_created_at_utc": command.timestamp,
            "core_command_dispatched_at_utc": trace_updates.get("core_command_ws_send_at_utc"),
            "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
            **trace_updates,
        },
    )
    data["payload"] = payload
    return data


def _save_ws_command_transport_sample(
    db: TradingDatabase,
    command_payload: dict[str, Any],
    *,
    sent_at: str,
    metadata: dict[str, Any],
) -> None:
    settings = get_settings()
    if not settings.transport_metrics_enabled:
        return
    payload = dict(command_payload.get("payload") or {})
    trace = trace_from_payload(payload)
    total_wall = wall_ms(command_payload.get("timestamp"), sent_at) or 0.0
    sample = TransportLatencySample(
        sample_id=f"lat_ws_cmd_{command_payload.get('command_id')}_{int(time.time() * 1000)}",
        trace_id=str(trace.get("trace_id") or f"trace:{command_payload.get('command_id')}"),
        trade_date=str(sent_at)[:10],
        direction="core_to_gateway",
        message_type=str(command_payload.get("type") or ""),
        command_id=str(command_payload.get("command_id") or ""),
        request_id=str(command_payload.get("request_id") or ""),
        source="core",
        created_at=str(command_payload.get("timestamp") or sent_at),
        completed_at=sent_at,
        payload_size_bytes=payload_size_bytes(command_payload),
        stage_ms={"ws_send_ms": 0.0, "core_dispatch_wait_ms": total_wall},
        total_wall_ms=total_wall,
        core_dispatch_wait_ms=total_wall,
        ws_send_ms=0.0,
        ws_receive_ms=metadata.get("ws_receive_ms"),
        ws_message_sequence=metadata.get("ws_message_sequence"),
        transport_mode=TRANSPORT_MODE_WEBSOCKET_MOCK,
        metadata={**trace, **metadata},
    )
    db.save_gateway_transport_latency_sample(sample.to_dict())


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
