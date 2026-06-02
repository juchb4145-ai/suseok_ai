from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
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
    TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
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
from trading.strategy.models import BlockType, CandidateState
from trading.strategy.reason_taxonomy import normalize_reason_status, reason_status_family, reason_summary
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import NAVER_THEME_SOURCE_NAME, NaverThemeUniverseSource
from trading_app.dependencies import close_database, get_settings, open_database, verify_gateway_token
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer, config_from_settings
from trading_app.dry_run_threshold_ab import DryRunThresholdABAnalyzer, config_from_settings as threshold_ab_config_from_settings
from trading_app.ops_alerts import build_ops_alerts
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.runtime_supervisor import RuntimeSupervisor
from trading_app.schemas import GatewayCommandBatch, GatewayCommandIn, GatewayEventIn, HealthResponse, OrderEnqueueRequest
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot
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
KST = timezone(timedelta(hours=9), "KST")
TRANSPORT_LIVE_WINDOW_SEC = 15 * 60
LOG_LIVE_WINDOW_SEC = 5 * 60
DASHBOARD_EVENT_PUSH_MIN_INTERVAL_SEC = 1.0


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
gateway_ws_transport_state: dict[str, Any] = {
    "enabled": False,
    "connected": False,
    "state": "DISCONNECTED",
    "transport_mode": "",
    "ws_session_id": "",
    "ws_connection_id": "",
    "reconnect_count": 0,
    "fallback_state": "",
    "fallback_reason": "",
    "fallback_detail": "",
    "fallback_at": "",
    "last_error": "",
    "last_error_type": "",
    "last_error_stage": "",
    "last_error_at": "",
    "last_error_reconnect_count": 0,
    "last_close_code": "",
    "last_close_reason": "",
    "last_diagnostic_log_signature": "",
    "blocked_order_command_count": 0,
    "session_loss_count": 0,
    "duplicate_ack_count": 0,
    "unknown_ack_count": 0,
    "last_ws_event_at": "",
    "last_ws_ack_at": "",
}
_dashboard_snapshot_task: asyncio.Task | None = None
_dashboard_snapshot_last_sent_monotonic = 0.0


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


def _threshold_ab_analyzer(*, min_sample_count: Optional[int] = None) -> DryRunThresholdABAnalyzer:
    config = threshold_ab_config_from_settings(get_settings())
    if min_sample_count is not None:
        config = replace(config, min_sample_count=int(min_sample_count))
    return DryRunThresholdABAnalyzer(config=config)


def _filter_threshold_ab_report(
    report: dict[str, Any],
    *,
    category: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    parameter_name: Optional[str] = None,
    include_risky: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    candidates = list(report.get("candidates") or [])
    results = dict(report.get("results") or {})
    normalized_category = (category or "").strip()
    normalized_grade = (recommendation_grade or "").strip()
    normalized_parameter = (parameter_name or "").strip()

    def keep(candidate: dict[str, Any]) -> bool:
        result = dict(results.get(str(candidate.get("candidate_id") or "")) or {})
        grade = str((result.get("recommendation") or {}).get("grade") or candidate.get("recommendation_grade") or "")
        if normalized_category and str(candidate.get("category") or "") != normalized_category:
            return False
        if normalized_grade and grade != normalized_grade:
            return False
        if not include_risky and grade in {"RISKY_CANDIDATE", "DO_NOT_APPLY"}:
            return False
        if normalized_parameter and normalized_parameter not in str(candidate.get("parameter_name") or ""):
            return False
        return True

    filtered = [candidate for candidate in candidates if keep(candidate)]
    start = max(0, int(offset or 0))
    page_limit = max(1, int(limit or 100))
    report = dict(report)
    report["all_candidate_count"] = int(report.get("total_candidates") or len(candidates))
    report["total_candidates"] = len(filtered)
    report["candidates"] = filtered[start : start + page_limit]
    report["pagination"] = _pagination_payload(
        limit=page_limit,
        offset=start,
        count=len(report["candidates"]),
        total=len(filtered),
    )
    report["filters"] = {
        **dict(report.get("filters") or {}),
        "category": normalized_category,
        "recommendation_grade": normalized_grade,
        "parameter_name": normalized_parameter,
        "include_risky": include_risky,
        "limit": page_limit,
        "offset": start,
    }
    return report


def _pagination_payload(
    *,
    limit: int,
    offset: int,
    count: int,
    total: Optional[int] = None,
    has_next: Optional[bool] = None,
) -> dict[str, Any]:
    normalized_limit = max(1, int(limit or 1))
    normalized_offset = max(0, int(offset or 0))
    normalized_count = max(0, int(count or 0))
    if has_next is None:
        has_next = (normalized_offset + normalized_count) < int(total) if total is not None else normalized_count >= normalized_limit
    prev_offset = max(0, normalized_offset - normalized_limit)
    payload = {
        "limit": normalized_limit,
        "offset": normalized_offset,
        "count": normalized_count,
        "has_next": bool(has_next),
        "has_prev": normalized_offset > 0,
        "next_offset": normalized_offset + normalized_limit if has_next else normalized_offset,
        "prev_offset": prev_offset,
    }
    if total is not None:
        payload["total"] = int(total)
    return payload


def _trim_page(rows: list[Any], *, limit: int, offset: int, total: Optional[int] = None) -> tuple[list[Any], dict[str, Any]]:
    normalized_limit = max(1, int(limit or 1))
    trimmed = rows[:normalized_limit]
    pagination = _pagination_payload(
        limit=normalized_limit,
        offset=offset,
        count=len(trimmed),
        total=total,
        has_next=len(rows) > normalized_limit if total is None else None,
    )
    return trimmed, pagination


def _transport_status_payload(db: TradingDatabase) -> dict[str, Any]:
    settings = get_settings()
    analyzer = _transport_analyzer(db)
    historical_report = analyzer.build_report(limit=1000)
    historical_summary = dict(historical_report.get("summary") or {})
    recent_samples = _recent_transport_samples(db.list_gateway_transport_latency_samples(limit=1000), max_age_sec=TRANSPORT_LIVE_WINDOW_SEC)
    live_summary = analyzer.aggregate_summary(recent_samples)
    summary = dict(live_summary if recent_samples else historical_summary)
    summary["summary_window"] = "live" if recent_samples else "historical_fallback"
    summary["live_window_sec"] = TRANSPORT_LIVE_WINDOW_SEC
    summary["live_sample_count"] = live_summary.get("count", 0)
    summary["historical_sample_count"] = historical_summary.get("count", 0)
    summary["historical_sample_window_sec"] = historical_summary.get("sample_window_sec", 0)
    recommendation = analyzer.advisor.evaluate(summary)
    latest_reports = db.list_gateway_transport_latency_reports(limit=1)
    recent_errors = db.latest_gateway_transport_errors(limit=10)
    gateway_snapshot = gateway_state.snapshot().to_dict()
    heartbeat_payload = gateway_snapshot.get("last_heartbeat_payload") or {}
    real_pilot = _real_gateway_websocket_pilot_status(heartbeat_payload)
    return {
        "transport_mode": heartbeat_payload.get("transport_mode") or "rest_long_poll",
        "metrics_enabled": settings.transport_metrics_enabled,
        "latest_summary": summary,
        "historical_summary": historical_summary,
        "warning_flags": summary.get("warning_flags", []),
        "websocket_recommendation": recommendation,
        "recent_errors": recent_errors,
        "latest_report_id": latest_reports[0].get("report_id") if latest_reports else "",
        "real_gateway_websocket_pilot": real_pilot,
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


@app.get("/themelab", response_class=HTMLResponse)
def theme_lab_dashboard(request: Request):
    return templates.TemplateResponse(request, "themelab.html", {})


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


@app.get("/api/ops/alerts")
def ops_alerts() -> dict[str, Any]:
    db = open_database()
    try:
        status_payload = api_status()
        runtime_payload = _runtime_dashboard_payload(runtime_supervisor.status())
        performance_report = _performance_analyzer(db).build_report(limit=20)
        dry_run_performance_payload = {
            **dict(performance_report.get("summary") or {}),
            "top_false_positive_types": performance_report.get("false_signal_summary", {}).get("top_false_positive_types", []),
            "top_false_negative_types": performance_report.get("false_signal_summary", {}).get("top_false_negative_types", []),
            "top_reject_reasons_with_rally": performance_report.get("false_signal_summary", {}).get(
                "top_live_reject_reasons_with_rally",
                [],
            ),
        }
        return build_ops_alerts(
            core=status_payload["core"],
            gateway=status_payload["gateway"],
            commands=status_payload["commands"],
            transport=status_payload["transport"],
            runtime=runtime_payload,
            dry_run_performance=dry_run_performance_payload,
            logs=build_logs_snapshot(db),
        )
    finally:
        close_database(db)


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
            limit=limit + 1,
            offset=offset,
        )
        samples, pagination = _trim_page(samples, limit=limit, offset=offset)
        report = _transport_analyzer(db).build_report(
            trade_date=trade_date,
            direction=direction,
            message_type=message_type,
            transport_mode=transport_mode,
            experiment_id=experiment_id,
            scenario=scenario,
            limit=10000,
        )
        filters = {
            **dict(report.get("filters") or {}),
            "command_id": command_id or "",
            "event_id": event_id or "",
            "limit": limit,
            "offset": offset,
        }
        return {
            "summary": report.get("summary", {}),
            "samples": samples,
            "items": samples,
            "pagination": pagination,
            "filters": filters,
        }
    finally:
        close_database(db)

@app.get("/api/gateway/transport/latency/summary")
def gateway_transport_latency_summary(
    trade_date: Optional[str] = None,
    transport_mode: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=604800),
    group_by: Optional[str] = None,
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _transport_analyzer(db).build_report(trade_date=trade_date, transport_mode=transport_mode, limit=10000)
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
        items = db.list_gateway_transport_latency_reports(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
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


@app.get("/api/gateway/transport/latency/{sample_id}")
def gateway_transport_latency_sample_detail(sample_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        sample = db.get_gateway_transport_latency_sample(sample_id)
        return {"found": sample is not None, "sample_id": sample_id, "record": sample}
    finally:
        close_database(db)


@app.get("/api/gateway/transport/experiments")
def gateway_transport_experiments(
    experiment_id: Optional[str] = None,
    scenario: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100000),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _transport_analyzer(db)
        items = []
        rows = db.list_gateway_transport_experiments(
            experiment_id=experiment_id,
            scenario=scenario,
            limit=limit + 1,
            offset=offset,
        )
        rows, pagination = _trim_page(rows, limit=limit, offset=offset)
        for item in rows:
            comparison = analyzer.build_transport_comparison_report(
                experiment_id=item.get("experiment_id"),
                scenario=item.get("scenario"),
            )
            items.append(
                {
                    **item,
                    "latest_recommendation": comparison.get("websocket_recommendation", {}).get("recommendation", ""),
                    "sample_counts": comparison.get("sample_counts", {}),
                    "rest_summary": comparison.get("rest_summary", {}),
                    "websocket_summary": comparison.get("websocket_summary", {}),
                    "delta": comparison.get("delta", {}),
                    "real_gateway_switch_ready": comparison.get("websocket_recommendation", {}).get("real_gateway_switch_ready", False),
                }
            )
        return {
            "items": items,
            "pagination": pagination,
            "filters": {
                "experiment_id": experiment_id or "",
                "scenario": scenario or "",
                "limit": limit,
                "offset": offset,
            },
        }
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
        real_pilot_report = analyzer.build_transport_comparison_report(
            trade_date=trade_date,
            baseline_transport="rest_long_poll",
            candidate_transport=TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
        )
        real_status = _real_gateway_websocket_pilot_status(gateway_state.snapshot().last_heartbeat_payload)
        payload["real_pilot_summary"] = {
            "status": real_status,
            "sample_counts": real_pilot_report.get("sample_counts", {}),
            "rest_summary": real_pilot_report.get("rest_summary", {}),
            "websocket_real_pilot_summary": real_pilot_report.get("websocket_summary", {}),
            "delta": real_pilot_report.get("delta", {}),
            "recommendation": real_pilot_report.get("websocket_recommendation", {}),
        }
        payload["real_pilot_ready"] = bool(real_status.get("enabled") and real_status.get("connected"))
        payload["switch_to_websocket_ready"] = False
        payload["real_gateway_switch_ready"] = False
        payload["next_required_soak_test"] = {
            "duration_sec": 3600,
            "max_reconnect_count": 3,
            "fail_on_duplicate_ack": True,
            "fail_on_session_loss": True,
        }
        payload.setdefault("blockers", [])
        payload["blockers"] = list(payload["blockers"]) + ["REAL_GATEWAY_WEBSOCKET_REQUIRES_LIMITED_SOAK_TEST"]
        return payload
    finally:
        close_database(db)


@app.get("/api/gateway/transport/websocket-pilot/status")
def gateway_transport_websocket_pilot_status() -> dict[str, Any]:
    return _real_gateway_websocket_pilot_status(gateway_state.snapshot().last_heartbeat_payload)


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
    payload = _order_service().list_dry_run_orders(
        trade_date=trade_date,
        status=status,
        code=code,
        candidate_id=candidate_id,
        side=side,
        order_phase=order_phase,
        virtual_position_id=virtual_position_id,
        exit_decision_id=exit_decision_id,
        limit=limit + 1,
        offset=offset,
    )
    items, pagination = _trim_page(list(payload.get("items") or []), limit=limit, offset=offset)
    return {
        **payload,
        "items": items,
        "pagination": pagination,
        "filters": {
            "trade_date": trade_date or "",
            "status": status or "",
            "code": code or "",
            "candidate_id": candidate_id,
            "side": side or "",
            "order_phase": order_phase or "",
            "virtual_position_id": virtual_position_id,
            "exit_decision_id": exit_decision_id,
            "limit": limit,
            "offset": offset,
        },
    }


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
        report = _performance_analyzer(db).build_report(
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
        total = int(report.get("total_items") or len(report.get("items") or []))
        report["pagination"] = _pagination_payload(
            limit=limit,
            offset=offset,
            count=len(report.get("items") or []),
            total=total,
        )
        return report
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
        items = db.list_dry_run_performance_reports(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
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
        page_items = items[start:end]
        return {
            "summary": report.get("false_signal_summary", {}),
            "type": type,
            "total": len(items),
            "items": page_items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(page_items), total=len(items)),
            "filters": {"trade_date": trade_date or "", "type": type, "limit": limit, "offset": offset},
        }
    finally:
        close_database(db)


@app.get("/api/runtime/threshold-ab/dry-run")
def runtime_threshold_ab_dry_run(
    trade_date: Optional[str] = None,
    strategy_name: Optional[str] = None,
    code: Optional[str] = None,
    theme_name: Optional[str] = None,
    session_bucket: Optional[str] = None,
    category: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    parameter_name: Optional[str] = None,
    min_sample_count: Optional[int] = None,
    include_risky: bool = True,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        performance_report = _performance_analyzer(db).build_report(
            trade_date=trade_date,
            strategy_name=strategy_name,
            code=code,
            theme_name=theme_name,
            session_bucket=session_bucket,
            limit=10000,
            offset=0,
        )
        analyzer = _threshold_ab_analyzer(min_sample_count=min_sample_count)
        filters = {
            "trade_date": trade_date or "",
            "strategy_name": strategy_name or "",
            "code": code or "",
            "theme_name": theme_name or "",
            "session_bucket": session_bucket or "",
            "category": category or "",
            "recommendation_grade": recommendation_grade or "",
            "parameter_name": parameter_name or "",
            "min_sample_count": min_sample_count,
            "include_risky": include_risky,
        }
        report = analyzer.build_report(
            performance_report,
            trade_date=trade_date,
            filters=filters,
            limit=10000,
            offset=0,
            include_risky=include_risky,
        )
        return _filter_threshold_ab_report(
            report,
            category=category,
            recommendation_grade=recommendation_grade,
            parameter_name=parameter_name,
            include_risky=include_risky,
            limit=limit,
            offset=offset,
        )
    finally:
        close_database(db)


@app.post("/api/runtime/threshold-ab/dry-run/rebuild")
def rebuild_runtime_threshold_ab_dry_run(
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    min_sample_count: Optional[int] = None,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        performance_report = _performance_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        analyzer = _threshold_ab_analyzer(min_sample_count=min_sample_count)
        report = analyzer.build_report(
            performance_report,
            trade_date=trade_date,
            filters={"trade_date": trade_date or "", "min_sample_count": min_sample_count},
            limit=10000,
            offset=0,
        )
        persisted = db.save_dry_run_threshold_ab_report(report) if persist else None
        exports = analyzer.export_report(report, fmt=format) if export else {}
        return {
            "report_id": report["report_id"],
            "persisted": persisted is not None,
            "exported": exports,
            "report": report,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/threshold-ab/dry-run/reports")
def runtime_threshold_ab_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_dry_run_threshold_ab_reports(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
    finally:
        close_database(db)


@app.get("/api/runtime/threshold-ab/dry-run/reports/{report_id}")
def runtime_threshold_ab_report_detail(report_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        report = db.get_dry_run_threshold_ab_report(report_id)
        if report is None:
            return {"report_id": report_id, "found": False}
        report["found"] = True
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/threshold-ab/dry-run/candidates/{candidate_id}")
def runtime_threshold_ab_candidate_detail(
    candidate_id: str,
    trade_date: Optional[str] = None,
    report_id: Optional[str] = None,
) -> dict[str, Any]:
    db = open_database()
    try:
        if report_id:
            report = db.get_dry_run_threshold_ab_report(report_id) or {}
        else:
            performance_report = _performance_analyzer(db).build_report(trade_date=trade_date, limit=10000)
            report = _threshold_ab_analyzer().build_report(
                performance_report,
                trade_date=trade_date,
                filters={"trade_date": trade_date or ""},
                limit=10000,
            )
        candidates = list(report.get("candidates") or [])
        candidate = next((item for item in candidates if item.get("candidate_id") == candidate_id), None)
        result = dict((report.get("results") or {}).get(candidate_id) or {})
        return {
            "found": candidate is not None,
            "candidate_id": candidate_id,
            "candidate": candidate,
            "result": result,
            "affected_lifecycles": result.get("affected_lifecycles", []),
            "report_id": report.get("report_id", report_id or ""),
            "disclaimer_ko": "실제 적용이 아니라 DRY_RUN 사후 분석 후보입니다.",
        }
    finally:
        close_database(db)


@app.get("/api/runtime/performance/dry-run/lifecycles/{lifecycle_id}")
def runtime_dry_run_performance_lifecycle_detail(
    lifecycle_id: str,
    trade_date: Optional[str] = None,
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _performance_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        for item in report.get("items") or []:
            if str(item.get("lifecycle_id") or "") == lifecycle_id:
                return {"found": True, "lifecycle_id": lifecycle_id, "item": item, "report_id": report.get("report_id")}
        return {"found": False, "lifecycle_id": lifecycle_id, "item": None, "report_id": report.get("report_id")}
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


@app.post("/api/themes/sync/naver")
def sync_naver_themes(
    replace: bool = True,
    max_pages: int = Query(20, ge=1, le=100),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        repository = ThemeEngineRepository(db)
        source = NaverThemeUniverseSource(max_pages=max_pages)
        result = ThemeSourceSyncService(repository, [source]).sync_source(
            NAVER_THEME_SOURCE_NAME,
            replace=replace,
            purge_sources=RETIRED_THEME_SOURCE_NAMES,
        )
        return _dataclass_dict(result)
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


@app.get("/api/themelab/snapshot")
def theme_lab_snapshot() -> dict[str, Any]:
    db = open_database()
    try:
        return build_theme_lab_dashboard_snapshot(db)
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
    await _schedule_dashboard_snapshot_broadcast()
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


async def _schedule_dashboard_snapshot_broadcast() -> None:
    global _dashboard_snapshot_task
    if dashboard_connections.client_count <= 0:
        return
    task = _dashboard_snapshot_task
    if task is not None and not task.done():
        return
    elapsed = time.monotonic() - _dashboard_snapshot_last_sent_monotonic
    delay = max(0.0, DASHBOARD_EVENT_PUSH_MIN_INTERVAL_SEC - elapsed)
    _dashboard_snapshot_task = asyncio.create_task(_broadcast_dashboard_snapshot_after(delay))
    _dashboard_snapshot_task.add_done_callback(_consume_dashboard_snapshot_task)


async def _broadcast_dashboard_snapshot_after(delay_sec: float) -> None:
    global _dashboard_snapshot_last_sent_monotonic
    if delay_sec > 0:
        await asyncio.sleep(delay_sec)
    if dashboard_connections.client_count <= 0:
        return
    payload = await asyncio.to_thread(_build_dashboard_snapshot_payload)
    payload["gateway"]["dashboard_ws_client_count"] = dashboard_connections.client_count
    await dashboard_connections.broadcast_json({"type": "snapshot", "snapshot": payload})
    _dashboard_snapshot_last_sent_monotonic = time.monotonic()


def _consume_dashboard_snapshot_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        pass


def _build_dashboard_snapshot_payload() -> dict[str, Any]:
    db = open_database()
    try:
        return build_dashboard_snapshot(db)
    finally:
        close_database(db)


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
    request: Request,
    status: Optional[str] = None,
    command_type: Optional[str] = None,
    trade_date: Optional[str] = None,
    command_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=100000),
    include_finished: bool = True,
    include_payload: bool = False,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_if_payload_requested(
        include_payload,
        request,
        authorization=authorization,
        x_local_token=x_local_token,
    )
    if command_id:
        record = gateway_state.get_command(command_id)
        items = [record.to_dict()] if record is not None else []
        pagination = _pagination_payload(limit=limit, offset=offset, count=len(items), total=len(items))
    else:
        rows = gateway_state.list_commands(
            status=status,
            command_type=command_type,
            trade_date=trade_date,
            limit=limit + 1,
            offset=offset,
            include_finished=include_finished,
        )
        items, pagination = _trim_page(rows, limit=limit, offset=offset)
    return {
        "summary": gateway_state.command_snapshot(),
        "items": [_command_history_item(item, include_payload=include_payload) for item in items],
        "pagination": pagination,
        "filters": {
            "status": status or "",
            "command_type": command_type or "",
            "trade_date": trade_date or "",
            "command_id": command_id or "",
            "include_finished": include_finished,
            "include_payload": include_payload,
            "limit": limit,
            "offset": offset,
        },
    }


@app.get("/api/gateway/commands/{command_id}/events")
def gateway_command_events(
    command_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    include_payload: bool = False,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_if_payload_requested(
        include_payload,
        request,
        authorization=authorization,
        x_local_token=x_local_token,
    )
    events = gateway_state.command_events(command_id, limit=limit)
    return {"command_id": command_id, "events": [_command_event_item(event, include_payload=include_payload) for event in events]}


@app.get("/api/gateway/commands/{command_id}")
def gateway_command_detail(
    command_id: str,
    request: Request,
    include_payload: bool = False,
    authorization: Optional[str] = Header(default=None),
    x_local_token: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _verify_if_payload_requested(
        include_payload,
        request,
        authorization=authorization,
        x_local_token=x_local_token,
    )
    record = gateway_state.get_command(command_id)
    record_payload = _command_history_item(record.to_dict(), include_payload=include_payload) if record else None
    events = gateway_state.command_events(command_id, limit=200)
    return {
        "found": record is not None,
        "record": record_payload,
        "events": [_command_event_item(event, include_payload=include_payload) for event in events],
    }


@app.post("/api/gateway/commands/prune")
def gateway_commands_prune(
    older_than_sec: int = Query(3600, ge=0, le=86400),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    removed = gateway_state.prune_commands(older_than_sec=older_than_sec)
    return {"removed": removed, "summary": gateway_state.command_snapshot()}


@app.post("/api/gateway/commands/{command_id}/cancel")
def gateway_command_cancel(
    command_id: str,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
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
    if command.type in ORDER_COMMAND_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ORDER_COMMAND_REQUIRES_ORDER_ENQUEUE",
        )
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
def enqueue_order(
    order_in: OrderEnqueueRequest,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    return _order_service().enqueue_order(order_in).to_dict()


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await dashboard_connections.connect(websocket)
    try:
        while True:
            payload = await asyncio.to_thread(_build_dashboard_snapshot_payload)
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
    connection_transport_mode = TRANSPORT_MODE_WEBSOCKET_MOCK
    _update_gateway_ws_transport_state(
        {
            "connected": True,
            "state": "CONNECTED",
            "transport_mode": connection_transport_mode,
            "ws_session_id": session_id,
            "ws_connection_id": connection_id,
        }
    )
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
            if message.type == "hello":
                connection_transport_mode = _ws_message_transport_mode(message, default=connection_transport_mode)
            message_transport_mode = _ws_message_transport_mode(message, default=connection_transport_mode)
            metadata = {
                **dict(message.metadata or {}),
                "connection_id": connection_id,
                "websocket_session_id": session_id,
                "ws_connection_id": connection_id,
                "ws_session_id": session_id,
                "transport_mode": message_transport_mode,
                "ws_receive_ms": receive_ms,
                "ws_message_sequence": sequence,
            }
            if message.type == "hello":
                _update_gateway_ws_transport_state(
                    {
                        "enabled": message_transport_mode == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
                        "connected": True,
                        "state": "AUTHENTICATED",
                        "transport_mode": message_transport_mode,
                        "ws_session_id": session_id,
                        "ws_connection_id": connection_id,
                        "reconnect_count": int(message.payload.get("reconnect_count") or message.metadata.get("ws_reconnect_count") or 0),
                    }
                )
                await websocket.send_json(
                    GatewayWsMessage(
                        type="hello_ack",
                        trace_id=message.trace_id,
                        source="core",
                        payload={
                            "transport_mode": message_transport_mode,
                            "websocket_session_id": session_id,
                            "real_gateway_switch_ready": False,
                        },
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
                        transport_mode=message_transport_mode,
                    )
                    for command in commands
                ]
                if payloads and get_settings().transport_metrics_enabled:
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
            elif message.type == "transport_heartbeat":
                event = _gateway_event_from_ws_message(message, metadata=metadata)
                _record_ws_message_side_effects(event, metadata)
                _maybe_record_ws_pilot_diagnostic_log(dict(event.payload or {}))
                await websocket.send_json(
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=event.event_id,
                        command_id=event.command_id,
                        payload={
                            "accepted": True,
                            "event_id": event.event_id,
                            "type": event.type,
                            "transport_only": True,
                        },
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict()
                )
            elif message.type in {"gateway_event", "heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
                event = _gateway_event_from_ws_message(message, metadata=metadata)
                _record_ws_message_side_effects(event, metadata)
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
        _update_gateway_ws_transport_state(
            {
                "connected": False,
                "state": "DISCONNECTED",
                "transport_mode": connection_transport_mode,
                "ws_session_id": session_id,
                "ws_connection_id": connection_id,
            }
        )
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
    dry_run_performance_report = _performance_analyzer(db).build_report(limit=10000)
    threshold_ab_report = _threshold_ab_analyzer().build_report(dry_run_performance_report, limit=10, offset=0)
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
        "support_vwap_coverage": dry_run_performance_report.get("summary", {}).get("data_quality", {}).get("support_vwap_coverage", {}),
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
    threshold_ab_payload = {
        "generated_at": threshold_ab_report.get("generated_at", ""),
        "trade_date": threshold_ab_report.get("trade_date", ""),
        "report_id": threshold_ab_report.get("report_id", ""),
        "summary": threshold_ab_report.get("summary", {}),
        "recommendations": list(threshold_ab_report.get("recommendations") or [])[:5],
        "candidates": list(threshold_ab_report.get("candidates") or [])[:5],
        "disclaimer_ko": threshold_ab_report.get("disclaimer_ko", ""),
    }
    runtime_payload["dry_run_orders"] = dry_run_orders_payload
    runtime_payload["dry_run_performance"] = dry_run_performance_payload
    runtime_payload["threshold_ab"] = threshold_ab_payload
    ops_alerts_payload = build_ops_alerts(
        core=status_payload["core"],
        gateway=status_payload["gateway"],
        commands=status_payload["commands"],
        transport=transport_payload,
        runtime=runtime_payload,
        dry_run_performance=dry_run_performance_payload,
        logs=logs_payload,
    )
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
        "threshold_ab": threshold_ab_payload,
        "ops_alerts": ops_alerts_payload,
        "safety": status_payload["safety"],
        "candidates": candidates_payload,
        "themes": themes_payload,
        "orders": orders_payload,
        "reviews": reviews_payload,
        "logs": logs_payload,
        "theme_lab": build_theme_lab_dashboard_snapshot(db),
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
    display_state_counts = Counter(_candidate_display_state(candidate) for candidate in candidates)
    block_reasons = Counter()
    items = []
    for candidate in candidates[:limit]:
        metadata = dict(candidate.metadata or {})
        gate_record = _best_gate_record(metadata)
        reason_codes = _reason_codes(metadata, gate_record)
        display_state = _candidate_display_state(candidate)
        reason_status = normalize_reason_status(
            reason_codes=reason_codes,
            display_state=display_state,
            existing_status=metadata.get("sub_status") or gate_record.get("sub_status") or "",
            block_type=candidate.block_type.value,
            can_recover=candidate.can_recover,
        )
        theme_score = _number(
            _first_present(
                metadata.get("theme_score"),
                metadata.get("dynamic_theme_score"),
                gate_record.get("theme_score"),
                gate_record.get("dynamic_theme_score"),
            )
        )
        membership_score = _number(
            _first_present(
                metadata.get("membership_score"),
                gate_record.get("membership_score"),
            )
        )
        hybrid_score = _number(
            _first_present(
                metadata.get("hybrid_score"),
                gate_record.get("hybrid_score"),
                gate_record.get("score"),
            )
        )
        block_reasons.update(reason_codes)
        items.append(
            {
                "id": candidate.id,
                "trade_date": candidate.trade_date,
                "code": candidate.code,
                "name": candidate.name,
                "state": candidate.state.value,
                "display_state": display_state,
                "reason_status": reason_status,
                "reason_family": reason_status_family(reason_status),
                "sub_status": reason_status,
                "block_type": candidate.block_type.value,
                "can_recover": candidate.can_recover,
                "theme_id": metadata.get("best_theme_id") or gate_record.get("theme_id", ""),
                "theme_score": theme_score,
                "membership_score": membership_score,
                "hybrid_score": hybrid_score,
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
            "ready": display_state_counts.get(CandidateState.READY.value, 0),
            "blocked": display_state_counts.get(CandidateState.BLOCKED.value, 0),
            "wait": display_state_counts.get("WAIT", 0),
            "expired": display_state_counts.get(CandidateState.EXPIRED.value, 0),
            "removed": display_state_counts.get(CandidateState.REMOVED.value, 0),
            "top_block_reasons": [
                {"reason": reason, "count": count}
                for reason, count in block_reasons.most_common(10)
            ],
            "reason_summary": reason_summary(items),
            "support_coverage_summary": _candidate_support_coverage_summary(candidates),
        },
        "items": items,
    }


def _candidate_support_coverage_summary(candidates) -> dict[str, Any]:
    rows = []
    reasons = Counter()
    minute_status = Counter()
    source_counts = Counter()
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        gate_record = _best_gate_record(metadata)
        coverage = dict(
            _first_present(
                metadata.get("support_coverage"),
                gate_record.get("support_coverage"),
                (gate_record.get("theme_lab_bridge") or {}).get("support_coverage") if isinstance(gate_record.get("theme_lab_bridge"), dict) else None,
                {},
            )
            or {}
        )
        if coverage:
            rows.append(coverage)
        reason = str(
            _first_present(
                metadata.get("support_missing_reason"),
                metadata.get("support_taxonomy"),
                gate_record.get("support_missing_reason"),
                gate_record.get("support_taxonomy"),
            )
            or ""
        )
        if reason:
            reasons[reason] += 1
        status = str(coverage.get("minute_bar_quality_status") or "")
        if status:
            minute_status[status] += 1
        presence = coverage.get("support_source_presence")
        if isinstance(presence, dict):
            for source, present in presence.items():
                if present:
                    source_counts[str(source)] += 1
    total = len(rows)
    return {
        "sample_count": total,
        "support_metadata_coverage_pct": _ratio(
            sum(1 for row in rows if row.get("support_source_present_count", 0) or row.get("support_candidate_count", 0)),
            total,
        ),
        "vwap_metadata_coverage_pct": _ratio(sum(1 for row in rows if row.get("vwap_present")), total),
        "minute_bar_coverage_pct": _ratio(sum(1 for row in rows if row.get("minute_bar_present")), total),
        "stale_vwap_count": sum(1 for row in rows if row.get("vwap_stale")),
        "support_missing_count_by_reason": [{"reason": key, "count": value} for key, value in reasons.most_common(10)],
        "support_source_distribution": [{"source": key, "count": value} for key, value in source_counts.most_common(10)],
        "minute_bar_quality_status_counts": [{"status": key, "count": value} for key, value in minute_status.most_common(10)],
    }


def _candidate_display_state(candidate) -> str:
    if candidate.state in {CandidateState.DETECTED, CandidateState.WATCHING}:
        return "WAIT"
    if (
        candidate.state == CandidateState.BLOCKED
        and (candidate.block_type == BlockType.TEMPORARY or candidate.can_recover)
    ):
        return "WAIT"
    return candidate.state.value


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
    raw_logs = db.recent_logs(limit=limit)
    recent_logs = _recent_log_lines(raw_logs, max_age_sec=LOG_LIVE_WINDOW_SEC)
    core_items = [_core_log_item(line) for line in recent_logs]
    recent_gateway_events = [
        event
        for event in reversed(gateway_state.recent_events(limit=50))
        if _is_recent_timestamp(event.timestamp, max_age_sec=LOG_LIVE_WINDOW_SEC)
    ]
    hidden_gateway_event_counts = _hidden_gateway_event_counts(recent_gateway_events)
    gateway_items = [
        _gateway_event_log_item(event)
        for event in recent_gateway_events
        if not _is_noisy_gateway_log(event)
    ]
    items = sorted(
        [item for item in [*core_items, *gateway_items] if item.get("timestamp_utc")],
        key=lambda item: str(item.get("timestamp_utc") or ""),
        reverse=True,
    )
    return {
        "core": [str(item.get("line") or "") for item in sorted(core_items, key=lambda item: str(item.get("timestamp_utc") or ""), reverse=True)],
        "gateway": [dict(item.get("event") or {}) for item in sorted(gateway_items, key=lambda item: str(item.get("timestamp_utc") or ""), reverse=True)],
        "items": items,
        "warnings": [str(item.get("line") or "") for item in core_items if "WARN" in str(item.get("line") or "").upper() or "ERROR" in str(item.get("line") or "").upper()],
        "timezone": "Asia/Seoul",
        "live_window_sec": LOG_LIVE_WINDOW_SEC,
        "stale_core_log_count": max(0, len(raw_logs) - len(recent_logs)),
        "hidden_gateway_event_counts": hidden_gateway_event_counts,
    }


def _core_log_item(line: str) -> dict[str, Any]:
    parsed = _parse_timestamp_utc(str(line or "")[:19])
    display = _log_line_to_kst(line)
    return {
        "source": "core",
        "timestamp": _timestamp_to_kst_display(parsed) if parsed is not None else "",
        "timestamp_utc": parsed.isoformat() if parsed is not None else "",
        "type": "log",
        "line": display,
    }


def _gateway_event_log_item(event: GatewayEvent) -> dict[str, Any]:
    parsed = _parse_timestamp_utc(event.timestamp)
    item = event.to_dict()
    item["timestamp"] = _timestamp_to_kst_display(parsed) if parsed is not None else _timestamp_to_kst_display(item.get("timestamp"))
    line = f"{item.get('timestamp')} [gateway_event] {item.get('type') or ''}".strip()
    return {
        "source": "gateway",
        "timestamp": item["timestamp"],
        "timestamp_utc": parsed.isoformat() if parsed is not None else "",
        "type": item.get("type") or "",
        "line": line,
        "event": item,
    }


def _is_noisy_gateway_log(event: GatewayEvent) -> bool:
    return str(event.type or "") in {"heartbeat", "transport_heartbeat", "price_tick"}


def _hidden_gateway_event_counts(events: list[GatewayEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if not _is_noisy_gateway_log(event):
            continue
        event_type = str(event.type or "")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _log_line_to_kst(line: str) -> str:
    text = str(line or "")
    if len(text) < 19:
        return text
    converted = _timestamp_to_kst_display(text[:19])
    if not converted:
        return text
    return f"{converted}{text[19:]}"


def _recent_log_lines(lines: list[str], *, max_age_sec: int) -> list[str]:
    return [line for line in lines if _is_recent_timestamp(str(line or "")[:19], max_age_sec=max_age_sec)]


def _is_recent_timestamp(value: Any, *, max_age_sec: int) -> bool:
    parsed = _parse_timestamp_utc(value)
    if parsed is None:
        return False
    age_sec = (datetime.now(timezone.utc) - parsed).total_seconds()
    return age_sec <= max(1, int(max_age_sec))


def _timestamp_to_kst_display(value: Any) -> str:
    parsed = _parse_timestamp_utc(value)
    if parsed is None:
        return str(value or "").strip()
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _parse_timestamp_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    if event.type in {"heartbeat", "transport_heartbeat"}:
        _record_ws_pilot_diagnostic_log(db, dict(event.payload or {}))
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
        trace = trace_from_payload(event.payload)
        wait_time = event.payload.get("wait_time_sec", trace.get("wait_time_sec", ""))
        db.save_log(
            f"[gateway][rate_limited] {event.payload.get('command_type', '')} "
            f"{event.payload.get('command_id', '')} wait={wait_time}"
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
        existing_record = gateway_state.get_command(command_id)
        if status == CommandStatus.FAILED.value:
            handled = gateway_state.fail_command(command_id, error, retryable=False)
        else:
            handled = gateway_state.ack_command(command_id, status=status, result_payload=payload, error=error)
        trace = trace_from_payload(payload)
        if trace.get("transport_mode") == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT:
            if existing_record is None and not handled:
                gateway_ws_transport_state["unknown_ack_count"] = int(gateway_ws_transport_state.get("unknown_ack_count") or 0) + 1
                db.save_log(f"[gateway][ws_real_pilot][WARN] unknown command ack {command_id}")
            elif existing_record is not None and (
                getattr(getattr(existing_record, "status", ""), "value", str(getattr(existing_record, "status", "")))
            ) in {
                CommandStatus.ACKED.value,
                CommandStatus.FAILED.value,
                CommandStatus.REJECTED.value,
                CommandStatus.EXPIRED.value,
                CommandStatus.CANCELLED.value,
            }:
                gateway_ws_transport_state["duplicate_ack_count"] = int(gateway_ws_transport_state.get("duplicate_ack_count") or 0) + 1
                gateway_state.append_command_event(
                    command_id,
                    "duplicate_ack",
                    message="duplicate websocket real pilot ack",
                    payload=payload,
                )
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
        "reason_summary": snapshot_payload.get("reason_summary", {}),
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
    real_pilot = dict(status.get("real_gateway_websocket_pilot") or {})
    reason = ""
    reasons = recommendation.get("reasons") or []
    if reasons:
        reason = str(reasons[0])
    return {
        "mode": status.get("transport_mode", "rest_long_poll"),
        "metrics_enabled": status.get("metrics_enabled", True),
        "live_window_sec": summary.get("live_window_sec", 0),
        "sample_count": summary.get("count", 0),
        "historical_sample_count": summary.get("historical_sample_count", 0),
        "historical_sample_window_sec": summary.get("historical_sample_window_sec", 0),
        "active_command_count": summary.get("active_command_count", _active_command_count(summary)),
        "non_heartbeat_event_count": summary.get("non_heartbeat_event_count", _non_heartbeat_event_count(summary)),
        "event_latency_p95_ms": summary.get("event_latency_p95_ms", 0),
        "command_latency_p95_ms": summary.get("command_latency_p95_ms", 0),
        "ack_latency_p95_ms": summary.get("ack_latency_p95_ms", 0),
        "long_poll_wait_p95_ms": summary.get("long_poll_wait_p95_ms", 0),
        "gateway_execute_p95_ms": summary.get("gateway_execute_p95_ms", 0),
        "rate_limit_wait_p95_ms": summary.get("rate_limit_wait_p95_ms", 0),
        "empty_poll_rate": summary.get("empty_poll_rate", 0),
        "reconnect_count": gateway.get("reconnect_count", 0),
        "transport_error_count": summary.get("transport_error_count", 0),
        "rate_limited_count": summary.get("rate_limited_count", 0),
        "websocket_recommended": bool(recommendation.get("should_switch")),
        "websocket_recommendation": recommendation.get("recommendation", "KEEP_REST_LONG_POLL"),
        "websocket_recommendation_reason": reason,
        "latest_report_id": status.get("latest_report_id", ""),
        "warning_flags": summary.get("warning_flags", []),
        "recent_errors": status.get("recent_errors", [])[:10],
        "real_gateway_websocket_pilot": real_pilot,
        "real_pilot_enabled": real_pilot.get("enabled", False),
        "real_pilot_connected": real_pilot.get("connected", False),
        "real_pilot_state": real_pilot.get("state", "DISCONNECTED"),
        "real_pilot_ws_session_id": real_pilot.get("ws_session_id", ""),
        "real_pilot_reconnect_count": real_pilot.get("reconnect_count", 0),
        "real_pilot_fallback_reason": real_pilot.get("fallback_reason", ""),
        "real_pilot_blocked_order_command_count": real_pilot.get("blocked_order_command_count", 0),
        "real_pilot_session_loss_count": real_pilot.get("session_loss_count", 0),
        "real_pilot_duplicate_ack_count": real_pilot.get("duplicate_ack_count", 0),
        "real_pilot_unknown_ack_count": real_pilot.get("unknown_ack_count", 0),
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


def _recent_transport_samples(samples: list[dict[str, Any]], *, max_age_sec: int) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    recent: list[dict[str, Any]] = []
    for sample in samples:
        created_at = _parse_timestamp_utc(sample.get("created_at"))
        if created_at is None:
            continue
        if (now - created_at).total_seconds() <= max(1, int(max_age_sec)):
            recent.append(sample)
    return recent


def _non_heartbeat_event_count(summary: dict[str, Any]) -> int:
    by_message_type = dict(summary.get("by_event_type") or {})
    noisy = {"heartbeat", "transport_heartbeat", "login_status"}
    count = 0
    for message_type, stats in by_message_type.items():
        if str(message_type or "") in noisy:
            continue
        if isinstance(stats, dict):
            count += int(stats.get("count") or 0)
    return count


def _active_command_count(summary: dict[str, Any]) -> int:
    by_command_type = dict(summary.get("by_command_type") or {})
    count = 0
    for stats in by_command_type.values():
        if isinstance(stats, dict):
            count += int(stats.get("count") or 0)
    return count


def _valid_gateway_ws_token(websocket: WebSocket) -> bool:
    expected = get_settings().local_token
    provided = str(websocket.query_params.get("token") or websocket.headers.get("x-local-token") or "")
    authorization = str(websocket.headers.get("authorization") or "")
    if authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    return bool(expected and provided == expected)


def _ws_message_transport_mode(message: GatewayWsMessage, *, default: str = TRANSPORT_MODE_WEBSOCKET_MOCK) -> str:
    raw = (
        message.metadata.get("transport_mode")
        or message.payload.get("transport_mode")
        or trace_from_payload(message.payload).get("transport_mode")
        or default
    )
    normalized = str(raw or default)
    if normalized in {TRANSPORT_MODE_WEBSOCKET_MOCK, TRANSPORT_MODE_WEBSOCKET_REAL_PILOT}:
        return normalized
    return default


def _update_gateway_ws_transport_state(patch: dict[str, Any]) -> None:
    gateway_ws_transport_state.update({key: value for key, value in patch.items() if value is not None})


def _record_ws_message_side_effects(event: GatewayEvent, metadata: dict[str, Any], db: TradingDatabase | None = None) -> None:
    if metadata.get("transport_mode") != TRANSPORT_MODE_WEBSOCKET_REAL_PILOT:
        return
    patch: dict[str, Any] = {
        "enabled": True,
        "connected": True,
        "state": "AUTHENTICATED",
        "transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
        "ws_session_id": metadata.get("ws_session_id") or metadata.get("websocket_session_id") or "",
        "ws_connection_id": metadata.get("ws_connection_id") or metadata.get("connection_id") or "",
    }
    if event.type in {"command_ack", "command_failed"}:
        patch["last_ws_ack_at"] = utc_now_ms()
    else:
        patch["last_ws_event_at"] = utc_now_ms()
    payload = dict(event.payload or {})
    for source_key, target_key in (
        ("ws_reconnect_count", "reconnect_count"),
        ("ws_fallback_state", "fallback_state"),
        ("ws_fallback_reason", "fallback_reason"),
        ("ws_fallback_detail", "fallback_detail"),
        ("ws_fallback_at", "fallback_at"),
        ("ws_last_error", "last_error"),
        ("ws_last_error_type", "last_error_type"),
        ("ws_last_error_stage", "last_error_stage"),
        ("ws_last_error_at", "last_error_at"),
        ("ws_last_error_reconnect_count", "last_error_reconnect_count"),
        ("ws_last_close_code", "last_close_code"),
        ("ws_last_close_reason", "last_close_reason"),
        ("pilot_blocked_order_command_count", "blocked_order_command_count"),
        ("ws_session_loss_count", "session_loss_count"),
        ("ws_duplicate_ack_count", "duplicate_ack_count"),
        ("ws_unknown_ack_count", "unknown_ack_count"),
    ):
        if source_key in payload:
            patch[target_key] = payload.get(source_key)
    _update_gateway_ws_transport_state(patch)
    if db is not None:
        _record_ws_pilot_diagnostic_log(db, payload)


def _maybe_record_ws_pilot_diagnostic_log(payload: dict[str, Any]) -> None:
    signature = _ws_pilot_diagnostic_signature(payload)
    if not signature:
        return
    if signature == str(gateway_ws_transport_state.get("last_diagnostic_log_signature") or ""):
        return
    db = open_database()
    try:
        _record_ws_pilot_diagnostic_log(db, payload)
    finally:
        close_database(db)


def _record_ws_pilot_diagnostic_log(db: TradingDatabase, payload: dict[str, Any]) -> None:
    signature = _ws_pilot_diagnostic_signature(payload)
    if not signature:
        return
    if signature == str(gateway_ws_transport_state.get("last_diagnostic_log_signature") or ""):
        return
    gateway_ws_transport_state["last_diagnostic_log_signature"] = signature
    diagnostic = _ws_pilot_diagnostic_fields(payload)
    parts = [
        f"state={diagnostic['state'] or '-'}",
        f"reconnect={diagnostic['reconnect_count'] or '0'}",
        f"fallback={diagnostic['fallback_reason'] or '-'}",
        f"stage={diagnostic['last_error_stage'] or '-'}",
        f"error_type={diagnostic['last_error_type'] or '-'}",
    ]
    if diagnostic["last_close_code"]:
        parts.append(f"close={diagnostic['last_close_code']}")
    detail = diagnostic["fallback_detail"] or diagnostic["last_error"] or diagnostic["last_close_reason"]
    if detail:
        parts.append(f"detail={_truncate_log_detail(detail)}")
    db.save_log(f"[gateway][ws_real_pilot][WARN] {' '.join(parts)}")


def _truncate_log_detail(value: str, *, limit: int = 500) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _ws_pilot_diagnostic_signature(payload: dict[str, Any]) -> str:
    if not payload.get("ws_pilot_enabled") and payload.get("transport_mode") != TRANSPORT_MODE_WEBSOCKET_REAL_PILOT:
        return ""
    diagnostic = _ws_pilot_diagnostic_fields(payload)
    if not any(diagnostic[key] for key in ("fallback_reason", "fallback_detail", "last_error", "last_error_type", "last_close_code")):
        return ""
    return "|".join(diagnostic.values())


def _ws_pilot_diagnostic_fields(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "state": str(payload.get("ws_connection_state") or ""),
        "reconnect_count": str(payload.get("ws_reconnect_count") or ""),
        "fallback_reason": str(payload.get("ws_fallback_reason") or ""),
        "fallback_detail": str(payload.get("ws_fallback_detail") or ""),
        "last_error": str(payload.get("ws_last_error") or ""),
        "last_error_type": str(payload.get("ws_last_error_type") or ""),
        "last_error_stage": str(payload.get("ws_last_error_stage") or ""),
        "last_error_at": str(payload.get("ws_last_error_at") or ""),
        "last_close_code": str(payload.get("ws_last_close_code") or ""),
        "last_close_reason": str(payload.get("ws_last_close_reason") or ""),
    }


def _real_gateway_websocket_pilot_status(heartbeat_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    heartbeat = dict(heartbeat_payload or {})
    state = dict(gateway_ws_transport_state)
    enabled = bool(
        state.get("enabled")
        or heartbeat.get("ws_pilot_enabled")
        or heartbeat.get("transport_mode") == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
        or heartbeat.get("original_transport") == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
    )
    connected = bool(
        state.get("connected")
        or str(heartbeat.get("ws_connection_state") or "").upper() in {"CONNECTED", "AUTHENTICATED"}
    )
    return {
        "enabled": enabled,
        "connected": connected,
        "state": heartbeat.get("ws_connection_state") or state.get("state") or "DISCONNECTED",
        "ws_session_id": heartbeat.get("ws_session_id") or state.get("ws_session_id") or "",
        "ws_connection_id": heartbeat.get("ws_connection_id") or state.get("ws_connection_id") or "",
        "reconnect_count": int(heartbeat.get("ws_reconnect_count") or state.get("reconnect_count") or 0),
        "fallback_state": heartbeat.get("ws_fallback_state") or state.get("fallback_state") or "",
        "fallback_reason": heartbeat.get("ws_fallback_reason") or state.get("fallback_reason") or "",
        "fallback_detail": heartbeat.get("ws_fallback_detail") or state.get("fallback_detail") or "",
        "fallback_at": heartbeat.get("ws_fallback_at") or state.get("fallback_at") or "",
        "last_error": heartbeat.get("ws_last_error") or state.get("last_error") or "",
        "last_error_type": heartbeat.get("ws_last_error_type") or state.get("last_error_type") or "",
        "last_error_stage": heartbeat.get("ws_last_error_stage") or state.get("last_error_stage") or "",
        "last_error_at": heartbeat.get("ws_last_error_at") or state.get("last_error_at") or "",
        "last_error_reconnect_count": int(
            heartbeat.get("ws_last_error_reconnect_count") or state.get("last_error_reconnect_count") or 0
        ),
        "last_close_code": heartbeat.get("ws_last_close_code") or state.get("last_close_code") or "",
        "last_close_reason": heartbeat.get("ws_last_close_reason") or state.get("last_close_reason") or "",
        "blocked_order_command_count": int(
            heartbeat.get("pilot_blocked_order_command_count") or state.get("blocked_order_command_count") or 0
        ),
        "session_loss_count": int(heartbeat.get("ws_session_loss_count") or state.get("session_loss_count") or 0),
        "duplicate_ack_count": int(heartbeat.get("ws_duplicate_ack_count") or state.get("duplicate_ack_count") or 0),
        "unknown_ack_count": int(heartbeat.get("ws_unknown_ack_count") or state.get("unknown_ack_count") or 0),
        "last_ws_event_at": heartbeat.get("last_ws_event_at") or state.get("last_ws_event_at") or "",
        "last_ws_ack_at": heartbeat.get("last_ws_ack_at") or state.get("last_ws_ack_at") or "",
    }


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
            "transport_mode": metadata.get("transport_mode") or TRANSPORT_MODE_WEBSOCKET_MOCK,
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


def _ws_command_dict_with_trace(command: GatewayCommand, trace_updates: dict[str, Any], *, transport_mode: str = TRANSPORT_MODE_WEBSOCKET_MOCK) -> dict[str, Any]:
    data = command.to_dict()
    trace = trace_from_payload(data.get("payload") or {})
    payload = ensure_transport_trace(
        data.get("payload") or {},
        trace_id=trace.get("trace_id") or f"trace:{command.command_id}",
        process="core",
        extra={
            "core_command_created_at_utc": command.timestamp,
            "core_command_dispatched_at_utc": trace_updates.get("core_command_ws_send_at_utc"),
            "transport_mode": transport_mode,
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
        transport_mode=str(metadata.get("transport_mode") or TRANSPORT_MODE_WEBSOCKET_MOCK),
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
    result_payload = dict(compact.get("result_payload") or {})
    if result_payload:
        compact["result_payload_summary"] = {
            key: result_payload.get(key)
            for key in ("ok", "code", "message", "command_id", "result_code", "reason", "error")
            if key in result_payload
        }
        compact.pop("result_payload", None)
    return compact


def _command_event_item(item: dict[str, Any], *, include_payload: bool) -> dict[str, Any]:
    if include_payload:
        return item
    compact = dict(item)
    payload = dict(compact.get("payload") or {})
    compact["payload_summary"] = {
        key: payload.get(key)
        for key in ("command_id", "command_type", "status", "message", "reason", "error", "result_code")
        if key in payload
    }
    compact.pop("payload", None)
    return compact


def _verify_if_payload_requested(
    include_payload: bool,
    request: Request,
    *,
    authorization: Optional[str] = None,
    x_local_token: Optional[str] = None,
) -> None:
    if include_payload:
        verify_gateway_token(request, authorization=authorization, x_local_token=x_local_token)


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
    values = gate_record.get("reason_codes") or metadata.get("reason_codes") or []
    reasons.extend(str(value) for value in list(values))
    primary = gate_record.get("primary_reason_code") or metadata.get("primary_reason_code")
    if primary:
        reasons.append(str(primary))
    if metadata.get("quality_reason"):
        reasons.append(_display_quality_reason(metadata["quality_reason"]))
    return _dedupe(reasons)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _display_quality_reason(value: Any) -> str:
    text = str(value or "")
    if text == "no_active_dynamic_theme":
        return "NO_ACTIVE_THEME"
    return text


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return 0


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
