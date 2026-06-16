from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import subprocess
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, PlainTextResponse
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
    new_message_id,
    utc_timestamp,
)
from trading.broker.transport_metrics import (
    TRANSPORT_MODE_WEBSOCKET_MOCK,
    TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
    TransportLatencySample,
    ensure_transport_trace,
    monotonic_delta_ms,
    monotonic_ms,
    payload_size_bytes,
    should_sample_transport_message,
    trace_from_payload,
    utc_now_ms,
    wall_ms,
)
from trading.broker.ws_messages import GatewayWsMessage
from trading.strategy.candidates import CandidateCollector
from trading.strategy.hybrid_validation import HybridValidationRepository
from trading.strategy.models import BlockType, CandidateState
from trading.strategy.reason_taxonomy import normalize_reason_status, reason_status_family, reason_summary
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading.theme_engine.backfill import ThemeBackfillConfig, apply_dispatch_guard
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.source_sync import RETIRED_THEME_SOURCE_NAMES, ThemeSourceSyncService
from trading.theme_engine.sources.naver import NAVER_THEME_SOURCE_NAME, NaverThemeUniverseSource
from trading_app.dependencies import close_database, get_settings, open_database, verify_gateway_token
from trading_app.buy_zero_rca import BuyZeroRCAAnalyzer
from trading_app.conservative_reason_outcomes import (
    ConservativeReasonOutcomeAnalyzer,
    empty_payload as conservative_reason_empty_payload,
    snapshot_payload as conservative_reason_snapshot_payload,
)
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer, config_from_settings
from trading_app.dry_run_threshold_ab import DryRunThresholdABAnalyzer, config_from_settings as threshold_ab_config_from_settings
from trading_app.exit_policy_validation import (
    ExitPolicyValidationAnalyzer,
    ExitPolicyValidationConfig,
    config_from_dry_run_config as exit_policy_config_from_dry_run_config,
)
from trading_app.intraday_outcomes import (
    IntradayOutcomeLabeler,
    ThemeLabFlowPricePathProvider,
    config_from_settings as outcome_config_from_settings,
)
from trading_app.market_gate_review import MarketGateReviewAnalyzer
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.live_sim_canary import canary_config_from_settings, evaluate_live_sim_canary
from trading_app.live_sim_canary_performance import (
    LiveSimCanaryPerformanceAnalyzer,
    LiveSimCanaryPerformanceConfig,
)
from trading_app.live_sim_preflight import LiveSimPreflightService, compact_preflight_snapshot
from trading_app.ops_alerts import build_ops_alerts
from trading_app.order_enqueue_service import OrderEnqueueService
from trading_app.promotion_evidence import DEFAULT_PROMOTION_POLICY_ID, PromotionEvidenceAdapter
from trading_app.replay_tick_buffer import ReplayGradeTickBuffer, replay_tick_writer_config_from_settings
from trading_app.runtime_supervisor import RuntimeSupervisor
from trading_app.schemas import GatewayCommandBatch, GatewayCommandIn, GatewayEventIn, HealthResponse, OrderEnqueueRequest
from trading_app.shadow_small_entry_promotion import (
    ShadowSmallEntryPromotionAnalyzer,
    empty_payload as shadow_small_entry_empty_payload,
    snapshot_payload as shadow_small_entry_snapshot_payload,
)
from trading_app.shadow_small_entry_ops import ShadowSmallEntryOpsService, snapshot_payload as shadow_small_entry_ops_snapshot_payload
from trading_app.shadow_small_entry_pilot import (
    ShadowSmallEntryPilotService,
    empty_payload as shadow_small_entry_pilot_empty_payload,
    snapshot_payload as shadow_small_entry_pilot_snapshot_payload,
)
from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer
from trading_app.shadow_strategy import ShadowStrategyEvaluator, config_from_settings as shadow_config_from_settings
from trading_app.strategy_change_proposals import (
    StrategyChangeProposalGenerator,
    build_config_diff,
    config_from_settings as change_proposal_config_from_settings,
)
from trading_app.strategy_replay import (
    DEFAULT_BUNDLE_ROOT,
    DEFAULT_REPLAY_DB_ROOT,
    StrategyReplayBundleExporter,
    StrategyRuntimeReplayRunner,
    get_replay_report_detail,
    get_replay_run_detail,
    list_replay_bundles,
    scan_replay_reports,
    scan_replay_runs,
)
from trading_app.themelab_dashboard import build_theme_lab_dashboard_snapshot
from trading_app.transport_latency import TransportLatencyAnalyzer, TransportLatencyConfig
from trading_app.websocket import DashboardConnectionManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    replay_tick_buffer.start()
    await _start_gateway_condition_event_worker()
    await _start_core_ws_event_worker()
    await runtime_supervisor.startup()
    try:
        yield
    finally:
        _shutdown_dashboard_snapshot_refresh_executor()
        _shutdown_theme_lab_dashboard_snapshot_refresh_executor()
        await _stop_core_ws_event_worker()
        await _stop_gateway_condition_event_worker()
        replay_tick_buffer.stop()
        await runtime_supervisor.shutdown()


app = FastAPI(title="Trading Core API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
KST = timezone(timedelta(hours=9), "KST")
TRANSPORT_LIVE_WINDOW_SEC = 15 * 60
LOG_LIVE_WINDOW_SEC = 5 * 60
DASHBOARD_EVENT_PUSH_MIN_INTERVAL_SEC = 1.0
DASHBOARD_SNAPSHOT_CACHE_TTL_SEC = 5.0
DASHBOARD_HEAVY_SECTION_CACHE_TTL_SEC = 30.0
DASHBOARD_WS_PUSH_INTERVAL_SEC = 5.0
DASHBOARD_SNAPSHOT_DETAIL_SLIM = "slim"
DASHBOARD_SNAPSHOT_DETAIL_FULL = "full"
THEMELAB_OPERATOR_EVENT_TYPES = {
    "BUY_READY_NEW",
    "BUY_READY_SMALL_NEW",
    "READY_TO_WAIT",
    "READY_BUT_LIVE_BLOCKED",
    "ORDER_INTENT_CREATED",
    "VIRTUAL_ORDER_CREATED",
    "MARKET_WAIT_STARTED",
    "MARKET_RECOVERED",
    "DATA_QUALITY_DEGRADED",
    "DATA_QUALITY_RECOVERED",
    "CHASE_RISK_BLOCKED",
    "LATE_CHASE_TEMP_WAIT",
    "GATEWAY_DISCONNECTED",
    "GATEWAY_RECOVERED",
    "SNAPSHOT_STALE",
    "SNAPSHOT_RECOVERED",
    "TOP_THEME_CHANGED",
    "TOP_LEADER_CHANGED",
    "ACTION_EXECUTED",
    "ACTION_FAILED",
    "ACTION_BLOCKED",
}
THEMELAB_OPERATOR_EVENT_SEVERITIES = {"CRITICAL", "WARNING", "OPPORTUNITY", "INFO"}
THEMELAB_OPERATOR_EVENT_CATEGORIES = {
    "opportunity",
    "warning",
    "critical",
    "order",
    "data",
    "market",
    "gateway",
    "snapshot",
    "theme",
    "risk",
    "action",
    "info",
}
THEMELAB_OPERATOR_ACTION_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "BLOCKED", "SKIPPED"}
FORBIDDEN_OPERATOR_ACTIONS = {
    "LIVE_BUY": {
        "label_ko": "LIVE 매수",
        "reason_ko": "이번 PR 범위 제외: LIVE 주문 금지",
    },
    "LIVE_SELL": {
        "label_ko": "LIVE 매도",
        "reason_ko": "이번 PR 범위 제외: LIVE 주문 금지",
    },
    "CANCEL_LIVE_ORDER": {
        "label_ko": "LIVE 주문 취소",
        "reason_ko": "이번 PR 범위 제외: LIVE 주문 취소 금지",
    },
    "OVERRIDE_LIVE_GUARD": {
        "label_ko": "LIVE Guard 우회",
        "reason_ko": "LIVE Guard 우회는 지원하지 않습니다.",
    },
    "FORCE_READY": {
        "label_ko": "강제 READY",
        "reason_ko": "운영자가 상태를 강제로 바꾸는 액션은 금지됩니다.",
    },
    "CHANGE_RISK_THRESHOLD": {
        "label_ko": "리스크 임계값 변경",
        "reason_ko": "전략/리스크 파라미터 변경은 이번 PR 범위가 아닙니다.",
    },
    "CHANGE_STRATEGY_PARAMETER": {
        "label_ko": "전략 파라미터 변경",
        "reason_ko": "전략 파라미터 변경은 이번 PR 범위가 아닙니다.",
    },
    "DISABLE_RISK_GATE": {
        "label_ko": "Risk Gate 비활성화",
        "reason_ko": "리스크 게이트 비활성화는 지원하지 않습니다.",
    },
}
OPERATOR_ACTION_CATALOG = {
    "REFRESH_SNAPSHOT": {
        "label_ko": "스냅샷 새로고침",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/themelab/snapshot",
    },
    "RUNTIME_CYCLE_ONCE": {
        "label_ko": "Runtime 1회 평가",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/runtime/cycle",
    },
    "RUNTIME_START": {
        "label_ko": "Runtime 시작",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/runtime/start",
    },
    "RUNTIME_STOP": {
        "label_ko": "Runtime 중지",
        "risk_level": "HIGH",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/runtime/stop",
    },
    "RUNTIME_RESTART": {
        "label_ko": "Runtime 재시작",
        "risk_level": "HIGH",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/runtime/restart",
    },
    "CHECK_RUNTIME_READINESS": {
        "label_ko": "Runtime 준비 상태 확인",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/runtime/readiness",
    },
    "CHECK_GATEWAY_STATUS": {
        "label_ko": "Gateway 상태 확인",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/gateway/status",
    },
    "START_KIWOOM_GATEWAY": {
        "label_ko": "32bit Gateway 실행",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/gateway/kiwoom/start",
    },
    "OPEN_DRY_RUN_ORDER_DETAIL": {
        "label_ko": "DRY_RUN 주문 상세",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/runtime/orders/dry-run/{intent_id}",
    },
    "REBUILD_DRY_RUN_PERFORMANCE": {
        "label_ko": "DRY_RUN 성과 재계산",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/runtime/performance/dry-run/rebuild",
    },
    "REBUILD_POSTMARKET_REVIEW": {
        "label_ko": "Post-market 리뷰 재생성",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/themelab/postmarket-review/rebuild",
    },
    "REBUILD_TRANSPORT_LATENCY_REPORT": {
        "label_ko": "전송 지연 리포트 재계산",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/gateway/transport/latency/rebuild",
    },
    "EXPORT_TRANSPORT_LATENCY_REPORT": {
        "label_ko": "전송 지연 리포트 내보내기",
        "risk_level": "MEDIUM",
        "requires_token": True,
        "confirmation_required": True,
        "endpoint": "/api/gateway/transport/latency/export",
    },
    "ACK_EVENT": {
        "label_ko": "이벤트 확인",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/themelab/operator-events/ack",
    },
    "HIDE_EVENT": {
        "label_ko": "이벤트 숨김",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": True,
        "endpoint": "/api/themelab/operator-events/hide",
    },
    "SNOOZE_EVENT": {
        "label_ko": "이벤트 잠시 보류",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": True,
        "endpoint": "/api/themelab/operator-events/snooze",
    },
    "ADD_OPERATOR_NOTE": {
        "label_ko": "운영 메모 추가",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "/api/themelab/operator-actions/execute",
    },
    "OPEN_RUNBOOK": {
        "label_ko": "Runbook 열기",
        "risk_level": "LOW",
        "requires_token": False,
        "confirmation_required": False,
        "endpoint": "client:runbook",
    },
}


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
    "last_send_ms": 0.0,
    "last_receive_ms": 0.0,
    "gateway_ws_send_completed_count": 0,
    "gateway_ws_send_completed_update_count": 0,
    "gateway_ws_send_completed_miss_count": 0,
    "gateway_ws_send_completed_skipped_count": 0,
    "gateway_ws_last_send_completed_duration_ms": 0.0,
    "gateway_ws_last_send_completed_to_core_receive_ms": 0.0,
    "gateway_ws_last_send_completed_message_type": "",
    "gateway_ws_last_send_completed_at": "",
    "outbound_queue_size": 0,
    "control_outbound_queue_size": 0,
    "data_outbound_queue_size": 0,
    "core_ws_outbound_writer_active": False,
    "core_ws_outbound_queue_size": 0,
    "core_ws_outbound_queue_max_size": 0,
    "core_ws_outbound_queued_count": 0,
    "core_ws_outbound_sent_count": 0,
    "core_ws_outbound_dropped_count": 0,
    "core_ws_last_send_json_ms": 0.0,
    "core_ws_last_send_queue_wait_ms": 0.0,
    "core_ws_last_send_json_type": "",
    "core_ws_last_send_json_at": "",
    "core_ws_slow_send_count": 0,
    "core_ws_last_slow_send_json_ms": 0.0,
    "core_ws_last_slow_send_at": "",
    "core_ws_last_receive_text_ms": 0.0,
    "core_ws_receive_loop_gap_ms": 0.0,
    "condition_event_queue_size": 0,
    "command_queue_size": 0,
    "last_ws_event_at": "",
    "last_ws_ack_at": "",
}
gateway_condition_event_worker_state: dict[str, Any] = {
    "enabled": False,
    "queue_size": 0,
    "queue_sizes_by_worker": [],
    "queue_batch_count": 0,
    "queue_batch_counts_by_worker": [],
    "queue_max_size": 0,
    "worker_count": 1,
    "active_worker_count": 0,
    "active_count": 0,
    "received_count": 0,
    "queued_count": 0,
    "coalesced_count": 0,
    "stale_skipped_count": 0,
    "stale_queue_wait_skipped_count": 0,
    "processed_count": 0,
    "failed_count": 0,
    "dropped_count": 0,
    "last_batch_size": 0,
    "last_drained_batch_count": 0,
    "last_received_count": 0,
    "last_queued_count": 0,
    "last_queued_batch_count": 0,
    "last_coalesced_count": 0,
    "last_stale_skipped_count": 0,
    "last_stale_queue_wait_skipped_count": 0,
    "last_stale_queue_wait_ms": 0.0,
    "last_queue_wait_ms": 0.0,
    "stale_include_skip_ms": 15000.0,
    "batch_chunk_size": 64,
    "last_batch_duration_ms": 0.0,
    "last_worker_index": 0,
    "last_shard_key": "",
    "last_queued_at": "",
    "last_processed_at": "",
    "last_error": "",
}
gateway_core_ws_event_worker_state: dict[str, Any] = {
    "enabled": False,
    "queue_size": 0,
    "queue_max_size": 0,
    "active_count": 0,
    "queued_count": 0,
    "processed_count": 0,
    "failed_count": 0,
    "dropped_count": 0,
    "priority_enabled": True,
    "split_enabled": True,
    "control_worker_count": 1,
    "control_queue_size": 0,
    "control_queue_sizes": [],
    "data_queue_size": 0,
    "control_active_count": 0,
    "data_active_count": 0,
    "control_queued_count": 0,
    "data_queued_count": 0,
    "last_priority": 0,
    "last_worker_kind": "",
    "last_control_worker_index": 0,
    "price_tick_coalesce_enabled": True,
    "price_tick_pending_key_count": 0,
    "price_tick_received_count": 0,
    "price_tick_queued_count": 0,
    "price_tick_coalesced_count": 0,
    "price_tick_processed_count": 0,
    "price_tick_dropped_count": 0,
    "price_tick_last_key": "",
    "heartbeat_fast_skipped_count": 0,
    "last_heartbeat_queue_key": "",
    "last_heartbeat_queue_key_at": "",
    "last_message_type": "",
    "last_event_id": "",
    "last_queue_wait_ms": 0.0,
    "last_duration_ms": 0.0,
    "last_queued_at": "",
    "last_processed_at": "",
    "last_error": "",
}


@dataclass
class _ConditionEventBatchWorkItem:
    events: list[GatewayEvent]
    queued_at: str
    queued_monotonic_ms: float


@dataclass
class _CoreWsEventWorkItem:
    kind: str
    metadata: dict[str, Any]
    queued_at: str = field(default_factory=utc_now_ms)
    queued_monotonic_ms: float = field(default_factory=monotonic_ms)
    event: GatewayEvent | None = None
    message: GatewayWsMessage | None = None
    coalesce_key: str = ""


@dataclass(order=True)
class _CoreWsEventQueuedItem:
    priority: int
    sequence: int
    work_item: _CoreWsEventWorkItem = field(compare=False)


@dataclass
class _CoreWsOutboundMessage:
    payload: dict[str, Any]
    message_type: str
    queued_at: str
    queued_monotonic_ms: float
    connection_id: str


_gateway_condition_event_queue: asyncio.Queue[_ConditionEventBatchWorkItem] | None = None
_gateway_condition_event_worker_task: asyncio.Task | None = None
_gateway_condition_event_queues: list[asyncio.Queue[_ConditionEventBatchWorkItem]] = []
_gateway_condition_event_worker_tasks: list[asyncio.Task] = []
_gateway_condition_event_worker_loop: asyncio.AbstractEventLoop | None = None
_gateway_condition_event_executor: ThreadPoolExecutor | None = None
_gateway_condition_event_executor_worker_count = 0
_gateway_condition_event_coalesce_lock = RLock()
_gateway_condition_event_generation = 0
_gateway_condition_event_latest_generation: dict[str, int] = {}
_core_ws_event_queue: asyncio.PriorityQueue[_CoreWsEventQueuedItem] | None = None
_core_ws_event_control_queue: asyncio.PriorityQueue[_CoreWsEventQueuedItem] | None = None
_core_ws_event_data_queue: asyncio.PriorityQueue[_CoreWsEventQueuedItem] | None = None
_core_ws_event_control_queues: list[asyncio.PriorityQueue[_CoreWsEventQueuedItem]] = []
_core_ws_event_worker_task: asyncio.Task | None = None
_core_ws_event_control_worker_task: asyncio.Task | None = None
_core_ws_event_data_worker_task: asyncio.Task | None = None
_core_ws_event_control_worker_tasks: list[asyncio.Task] = []
_core_ws_event_worker_loop: asyncio.AbstractEventLoop | None = None
_core_ws_event_queue_sequence = count()
_core_ws_price_tick_coalesce_lock = RLock()
_core_ws_price_tick_latest_by_key: dict[str, _CoreWsEventWorkItem] = {}
_dashboard_snapshot_task: asyncio.Task | None = None
_dashboard_snapshot_last_sent_monotonic = 0.0
_dashboard_snapshot_cache_lock = RLock()
_dashboard_snapshot_cache_payload: dict[str, Any] | None = None
_dashboard_snapshot_cache_db_path = ""
_dashboard_snapshot_cache_monotonic = 0.0
_dashboard_snapshot_cache_build_ms = 0.0
_dashboard_snapshot_cache_hit_count = 0
_dashboard_snapshot_cache_miss_count = 0
_dashboard_snapshot_cache_refreshing: set[str] = set()
_dashboard_snapshot_refresh_executor: ThreadPoolExecutor | None = None
_dashboard_fragment_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_theme_lab_dashboard_snapshot_cache_lock = RLock()
_theme_lab_dashboard_snapshot_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_theme_lab_dashboard_snapshot_refreshing: set[tuple[str, str]] = set()
_theme_lab_dashboard_snapshot_refresh_executor: ThreadPoolExecutor | None = None


def _build_runtime_supervisor() -> RuntimeSupervisor:
    return RuntimeSupervisor(settings=get_settings(), gateway_state=gateway_state)


def _build_replay_tick_buffer() -> ReplayGradeTickBuffer:
    settings = get_settings()
    return ReplayGradeTickBuffer(
        settings.db_path,
        config=replay_tick_writer_config_from_settings(settings),
    )


runtime_supervisor = _build_runtime_supervisor()
replay_tick_buffer = _build_replay_tick_buffer()


def _order_service() -> OrderEnqueueService:
    settings = get_settings()
    return OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path)


def _performance_analyzer(db: TradingDatabase) -> DryRunPerformanceAnalyzer:
    return DryRunPerformanceAnalyzer(db, config=config_from_settings(get_settings()))


def _intraday_outcome_labeler(db: TradingDatabase) -> IntradayOutcomeLabeler:
    return IntradayOutcomeLabeler(
        db,
        config=outcome_config_from_settings(get_settings()),
        price_provider=ThemeLabFlowPricePathProvider(db),
    )


def _shadow_strategy_evaluator(db: TradingDatabase) -> ShadowStrategyEvaluator:
    return ShadowStrategyEvaluator(db, config=shadow_config_from_settings(get_settings()))


def _promotion_evidence_adapter(db: TradingDatabase) -> PromotionEvidenceAdapter:
    return PromotionEvidenceAdapter(db)


def _change_proposal_generator(db: TradingDatabase) -> StrategyChangeProposalGenerator:
    return StrategyChangeProposalGenerator(
        db,
        config=change_proposal_config_from_settings(get_settings()),
        replay_db_root=DEFAULT_REPLAY_DB_ROOT,
    )


def _market_gate_review_analyzer(db: TradingDatabase) -> MarketGateReviewAnalyzer:
    return MarketGateReviewAnalyzer(db)


def _theme_lab_gate_reason_outcome_analyzer(db: TradingDatabase) -> ThemeLabGateReasonOutcomeAnalyzer:
    return ThemeLabGateReasonOutcomeAnalyzer(db)


def _conservative_reason_outcome_analyzer(db: TradingDatabase) -> ConservativeReasonOutcomeAnalyzer:
    return ConservativeReasonOutcomeAnalyzer(db)


def _shadow_small_entry_promotion_analyzer(db: TradingDatabase) -> ShadowSmallEntryPromotionAnalyzer:
    return ShadowSmallEntryPromotionAnalyzer(db)


def _shadow_small_entry_ops_service(db: TradingDatabase) -> ShadowSmallEntryOpsService:
    return ShadowSmallEntryOpsService(db, gateway_state=gateway_state, core_settings=get_settings())


def _shadow_small_entry_pilot_service(db: TradingDatabase) -> ShadowSmallEntryPilotService:
    return ShadowSmallEntryPilotService(db, gateway_state=gateway_state)


def _buy_zero_rca_analyzer(db: TradingDatabase) -> BuyZeroRCAAnalyzer:
    return BuyZeroRCAAnalyzer(db)


def _live_sim_auditor(db: TradingDatabase) -> LiveSimLifecycleAuditor:
    return LiveSimLifecycleAuditor(db, gateway_state=gateway_state)


def _live_sim_canary_performance_analyzer(db: TradingDatabase) -> LiveSimCanaryPerformanceAnalyzer:
    settings = get_settings()
    config = LiveSimCanaryPerformanceConfig(
        good_entry_slippage_bp=float(getattr(settings, "live_sim_canary_good_entry_slippage_bp", 10.0)),
        acceptable_entry_slippage_bp=float(getattr(settings, "live_sim_canary_acceptable_entry_slippage_bp", 30.0)),
        bad_entry_slippage_bp=float(getattr(settings, "live_sim_canary_bad_entry_slippage_bp", 50.0)),
        good_exit_slippage_bp=float(getattr(settings, "live_sim_canary_good_exit_slippage_bp", -10.0)),
        acceptable_exit_slippage_bp=float(getattr(settings, "live_sim_canary_acceptable_exit_slippage_bp", -30.0)),
        high_latency_ms=float(getattr(settings, "live_sim_canary_high_latency_ms", 1000.0)),
        stale_tick_age_sec=float(getattr(settings, "live_sim_canary_stale_tick_age_sec", 3.0)),
        match_tolerance_pct=float(getattr(settings, "live_sim_canary_match_tolerance_pct", 0.05)),
        commission_bp_per_side=float(getattr(settings, "dry_run_commission_bp_per_side", 1.5)),
        sell_tax_bp=float(getattr(settings, "dry_run_sell_tax_bp", 15.0)),
    )
    return LiveSimCanaryPerformanceAnalyzer(
        db,
        config=config,
        gateway_state=gateway_state,
        dry_run_analyzer=_performance_analyzer(db),
    )


def _exit_policy_validation_analyzer(db: TradingDatabase) -> ExitPolicyValidationAnalyzer:
    dry_config = config_from_settings(get_settings())
    config = exit_policy_config_from_dry_run_config(dry_config)
    try:
        runtime_settings = StrategyRuntimeSettingsRepository(db).load()
        exit_guard = dict(runtime_settings.value("live_sim_exit_guard", {}) or {})
    except Exception:
        exit_guard = {}
    config = ExitPolicyValidationConfig(
        baseline_stop_loss_pct=float(exit_guard.get("stop_loss_pct", config.baseline_stop_loss_pct)),
        baseline_take_profit_pct=float(exit_guard.get("take_profit_pct", config.baseline_take_profit_pct)),
        baseline_max_hold_minutes=int(exit_guard.get("max_hold_minutes", config.baseline_max_hold_minutes)),
        commission_bp_per_side=config.commission_bp_per_side,
        sell_tax_bp=config.sell_tax_bp,
        primary_slippage_bp=config.primary_slippage_bp,
        comparison_tolerance_pct=config.comparison_tolerance_pct,
        min_candidate_samples=config.min_candidate_samples,
        large_giveback_pct=config.large_giveback_pct,
        stale_tick_gap_sec=config.stale_tick_gap_sec,
        market_close_hhmm=config.market_close_hhmm,
    )
    return ExitPolicyValidationAnalyzer(
        db,
        config=config,
        canary_analyzer=_live_sim_canary_performance_analyzer(db),
    )


def _live_sim_preflight_service(db: TradingDatabase) -> LiveSimPreflightService:
    return LiveSimPreflightService(db, gateway_state, settings=get_settings())


def _live_sim_preflight_theme_lab_snapshot(db: TradingDatabase) -> dict[str, Any]:
    try:
        return build_theme_lab_dashboard_snapshot(
            db,
            runtime_status=runtime_supervisor.status(),
            gateway_state=gateway_state,
            include_extended=False,
        )
    except Exception:
        try:
            return db.latest_theme_lab_flow_result()
        except Exception:
            return {}


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
        "replay_tick_history": replay_tick_buffer.snapshot(),
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


@app.post("/api/gateway/kiwoom/start")
def start_kiwoom_gateway(_: None = Depends(verify_gateway_token)) -> dict[str, Any]:
    return _start_kiwoom_gateway_response()


def _start_kiwoom_gateway_response() -> dict[str, Any]:
    snapshot = gateway_state.snapshot().to_dict()
    gateway_status = _gateway_start_status(snapshot)
    if snapshot.get("connected") and snapshot.get("heartbeat_ok"):
        return {
            "started": False,
            "reason": "ALREADY_CONNECTED",
            "gateway": gateway_status,
            "processes": [],
        }
    processes = _find_kiwoom_gateway_processes()
    stale_for_start = bool(gateway_status.get("stale_for_start"))
    orphan_for_start = bool(processes and _gateway_snapshot_orphan_process_for_start(snapshot))
    stopped_processes: list[dict[str, Any]] = []
    if (stale_for_start or orphan_for_start) and processes:
        stopped_processes = _stop_kiwoom_gateway_processes(processes)
        time.sleep(0.5)
        processes = _find_kiwoom_gateway_processes()
        if processes:
            return {
                "started": False,
                "reason": "ORPHAN_RESTART_BLOCKED" if orphan_for_start else "STALE_RESTART_BLOCKED",
                "gateway": gateway_status,
                "processes": processes,
                "stale_recovery": {
                    "stale": True,
                    "orphan": orphan_for_start,
                    "stopped_processes": stopped_processes,
                    "remaining_processes": processes,
                },
            }
    if processes:
        return {
            "started": False,
            "reason": "ALREADY_RUNNING",
            "gateway": gateway_status,
            "processes": processes,
        }
    started = _start_kiwoom_gateway_process()
    reason = (
        "RESTARTED_ORPHAN"
        if orphan_for_start and stopped_processes
        else "RESTARTED_STALE"
        if stopped_processes
        else "STARTED_STALE_STATE"
        if stale_for_start
        else "STARTED"
    )
    return {
        "started": True,
        "reason": reason,
        "gateway": gateway_status,
        "processes": [started],
        "stale_recovery": {
            "stale": stale_for_start,
            "orphan": orphan_for_start,
            "stopped_processes": stopped_processes,
            "remaining_processes": [],
        },
        "logs": {
            "stdout": str(PROJECT_ROOT / "logs" / "kiwoom_gateway_dashboard.out.log"),
            "stderr": str(PROJECT_ROOT / "logs" / "kiwoom_gateway_dashboard.err.log"),
        },
    }


def _gateway_start_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    stale_for_start = _gateway_snapshot_stale_for_start(snapshot)
    return {
        "connected": bool(snapshot.get("connected")),
        "heartbeat_ok": bool(snapshot.get("heartbeat_ok")),
        "heartbeat_age_sec": snapshot.get("heartbeat_age_sec"),
        "heartbeat_timeout_sec": snapshot.get("heartbeat_timeout_sec"),
        "kiwoom_logged_in": bool(snapshot.get("kiwoom_logged_in")),
        "orderable": bool(snapshot.get("orderable")),
        "connection_state": str(snapshot.get("connection_state") or "UNKNOWN"),
        "stale_for_start": stale_for_start,
    }


def _gateway_snapshot_stale_for_start(snapshot: dict[str, Any]) -> bool:
    if bool(snapshot.get("heartbeat_ok")):
        return False
    connection_state = str(snapshot.get("connection_state") or "").upper()
    if connection_state == "STALE":
        return True
    if not bool(snapshot.get("connected")):
        return False
    heartbeat_age = snapshot.get("heartbeat_age_sec")
    timeout = snapshot.get("heartbeat_timeout_sec")
    if heartbeat_age is None:
        return True
    try:
        return float(heartbeat_age) > float(timeout or 0)
    except (TypeError, ValueError):
        return True


def _gateway_snapshot_orphan_process_for_start(snapshot: dict[str, Any]) -> bool:
    if bool(snapshot.get("connected")) or bool(snapshot.get("heartbeat_ok")):
        return False
    last_heartbeat_at = str(snapshot.get("last_heartbeat_at") or "").strip()
    last_event_at = str(snapshot.get("last_event_at") or "").strip()
    try:
        received_event_count = int(snapshot.get("received_event_count") or 0)
    except (TypeError, ValueError):
        received_event_count = 0
    return not last_heartbeat_at and not last_event_at and received_event_count <= 0


def _find_kiwoom_gateway_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -match 'python|pythonw') -and ($_.CommandLine -match 'apps[\\\\/]kiwoom_gateway.py|kiwoom_gateway.py') } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 4"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    text = str(result.stdout or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    return [
        {
            "pid": int(row.get("ProcessId") or 0),
            "name": str(row.get("Name") or ""),
            "command_line": str(row.get("CommandLine") or ""),
        }
        for row in rows
        if isinstance(row, dict) and int(row.get("ProcessId") or 0) > 0
    ]


def _stop_kiwoom_gateway_processes(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    pids = sorted({int(process.get("pid") or 0) for process in processes if int(process.get("pid") or 0) > 0})
    if not pids:
        return []
    script = "$ErrorActionPreference = 'SilentlyContinue'; " + "; ".join(
        f"Stop-Process -Id {pid} -Force" for pid in pids
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    return [process for process in processes if int(process.get("pid") or 0) in set(pids)]


def _start_kiwoom_gateway_process() -> dict[str, Any]:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "kiwoom_gateway_dashboard.out.log"
    err_path = logs_dir / "kiwoom_gateway_dashboard.err.log"
    env = os.environ.copy()
    _apply_kiwoom_gateway_runtime_env(env)
    settings = get_settings()
    python_exe = _kiwoom_gateway_python_exe()
    gateway_script = PROJECT_ROOT / "apps" / "kiwoom_gateway.py"
    if python_exe.exists():
        command = [str(python_exe), str(gateway_script)]
    else:
        command = ["py", "-3.9-32", str(gateway_script)]
    command.extend(
        [
            "--core-url",
            str(os.environ.get("TRADING_KIWOOM_GATEWAY_CORE_URL") or "http://127.0.0.1:8000"),
            "--token",
            settings.local_token,
            "--transport",
            str(os.environ.get("TRADING_GATEWAY_TRANSPORT") or "rest"),
            "--poll-wait-sec",
            str(os.environ.get("TRADING_GATEWAY_POLL_WAIT_SEC") or "1.0"),
            "--network-interval-sec",
            str(os.environ.get("TRADING_GATEWAY_NETWORK_INTERVAL_SEC") or "0.5"),
        ]
    )
    out_handle = out_path.open("ab")
    err_handle = err_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=out_handle,
            stderr=err_handle,
            close_fds=True,
        )
    finally:
        out_handle.close()
        err_handle.close()
    return {"pid": process.pid, "name": Path(command[0]).name, "command_line": " ".join(command)}


def _kiwoom_gateway_python_exe() -> Path:
    python_env = str(os.environ.get("TRADING_KIWOOM_GATEWAY_PYTHON") or "").strip()
    if python_env:
        return Path(python_env).expanduser()
    base_32 = Path("C:/Python39-32/python.exe")
    if base_32.exists():
        return base_32
    return PROJECT_ROOT / "venv_32" / "Scripts" / "python.exe"


def _apply_kiwoom_gateway_runtime_env(env: dict[str, str]) -> None:
    site_packages = PROJECT_ROOT / "venv_32" / "Lib" / "site-packages"
    if site_packages.exists():
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(site_packages)
            if not existing_pythonpath
            else f"{site_packages}{os.pathsep}{existing_pythonpath}"
        )
    qt_root = site_packages / "PyQt5" / "Qt5"
    platforms_dir = qt_root / "plugins" / "platforms"
    qt_bin = qt_root / "bin"
    if platforms_dir.exists():
        env.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms_dir))
    if qt_bin.exists():
        env["PATH"] = f"{qt_bin}{os.pathsep}{env.get('PATH', '')}"


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
    payload = runtime_supervisor.status()
    payload["replay_tick_history"] = replay_tick_buffer.snapshot()
    return payload


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


@app.get("/api/runtime/decisions/intraday")
def runtime_intraday_decisions(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    theme_name: Optional[str] = None,
    gate_status: Optional[str] = None,
    action_type: Optional[str] = None,
    action_result: Optional[str] = None,
    reason_status: Optional[str] = None,
    reason_family: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        filters = {
            "trade_date": trade_date or "",
            "code": code or "",
            "theme_name": theme_name or "",
            "gate_status": gate_status or "",
            "action_type": action_type or "",
            "action_result": action_result or "",
            "reason_status": reason_status or "",
            "reason_family": reason_family or "",
            "limit": limit,
            "offset": offset,
        }
        items = db.list_strategy_decision_events(
            trade_date=trade_date,
            code=code,
            theme_name=theme_name,
            gate_status=gate_status,
            action_type=action_type,
            action_result=action_result,
            reason_status=reason_status,
            reason_family=reason_family,
            limit=limit,
            offset=offset,
        )
        total = db.strategy_decision_event_count(
            trade_date=trade_date,
            code=code,
            theme_name=theme_name,
            gate_status=gate_status,
            action_type=action_type,
            action_result=action_result,
            reason_status=reason_status,
            reason_family=reason_family,
        )
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items), total=total),
            "filters": filters,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/decisions/summary")
def runtime_intraday_decision_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
) -> dict[str, Any]:
    db = open_database()
    try:
        summary = db.strategy_decision_summary(trade_date=trade_date, window_sec=window_sec)
        return {"summary": summary, "filters": {"trade_date": trade_date or "", "window_sec": window_sec}}
    finally:
        close_database(db)


@app.get("/api/runtime/buy-zero/summary")
def runtime_buy_zero_rca_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: int = Query(50000, ge=1, le=100000),
) -> dict[str, Any]:
    db = open_database()
    try:
        summary = _buy_zero_rca_analyzer(db).build_summary(
            trade_date=trade_date,
            window_sec=window_sec,
            limit=limit,
        )
        return {
            "summary": summary,
            "operator": {
                "today_buy_zero_top3_causes": summary.get("operator_top_3_causes", []),
                "ready_not_ordered_candidates": summary.get("top_ready_not_ordered_candidates", []),
                "observe_blocked_then_rally_candidates": summary.get("top_observe_then_rally_candidates", []),
                "live_sim_block_reasons": summary.get("live_sim_block_reasons", []),
                "data_quality_reasons": summary.get("data_quality_reasons", []),
            },
            "filters": {"trade_date": trade_date or "", "window_sec": window_sec, "limit": limit},
        }
    finally:
        close_database(db)


@app.get("/api/runtime/buy-zero/traces")
def runtime_buy_zero_trace_events(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    candidate_instance_id: Optional[str] = None,
    stage: Optional[str] = None,
    stage_status: Optional[str] = None,
    pass_fail: Optional[str] = Query(None, pattern="^(PASS|FAIL|pass|fail)$"),
    primary_block_reason: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_buy_zero_trace_events(
            trade_date=trade_date,
            code=code,
            candidate_instance_id=candidate_instance_id,
            stage=stage,
            stage_status=stage_status,
            pass_fail=pass_fail.upper() if pass_fail else None,
            primary_block_reason=primary_block_reason,
            window_sec=window_sec,
            limit=limit,
            offset=offset,
        )
        total = db.buy_zero_trace_count(
            trade_date=trade_date,
            code=code,
            candidate_instance_id=candidate_instance_id,
            stage=stage,
            stage_status=stage_status,
            pass_fail=pass_fail.upper() if pass_fail else None,
            primary_block_reason=primary_block_reason,
            window_sec=window_sec,
        )
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items), total=total),
            "filters": {
                "trade_date": trade_date or "",
                "code": code or "",
                "candidate_instance_id": candidate_instance_id or "",
                "stage": stage or "",
                "stage_status": stage_status or "",
                "pass_fail": pass_fail or "",
                "primary_block_reason": primary_block_reason or "",
                "window_sec": window_sec,
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/runtime/buy-zero/ready-not-ordered")
def runtime_buy_zero_ready_not_ordered(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _buy_zero_rca_analyzer(db).ready_not_ordered_report(
            trade_date=trade_date,
            window_sec=window_sec,
            limit=limit,
        )
        report["filters"] = {"trade_date": trade_date or "", "window_sec": window_sec, "limit": limit}
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/buy-zero/missed-opportunities")
def runtime_buy_zero_missed_opportunities(
    trade_date: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _buy_zero_rca_analyzer(db).missed_opportunity_report(trade_date=trade_date, limit=limit)
        report["filters"] = {"trade_date": trade_date or "", "limit": limit}
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/audit")
def runtime_live_sim_lifecycle_audit(
    trade_date: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=5000),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _live_sim_auditor(db).build_report(
            trade_date=trade_date or datetime.now().date().isoformat(),
            limit=limit,
        )
        report["filters"] = {"trade_date": trade_date or "", "limit": limit}
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/preflight")
def runtime_live_sim_preflight(include_details: bool = True) -> dict[str, Any]:
    db = open_database()
    try:
        transport_status = _transport_status_payload(db)
        snapshot = _live_sim_preflight_service(db).build_snapshot(
            runtime_status=runtime_supervisor.status(),
            transport_status=transport_status,
            theme_lab_snapshot=_live_sim_preflight_theme_lab_snapshot(db),
            include_details=include_details,
        )
        return snapshot
    finally:
        close_database(db)


@app.post("/api/runtime/live-sim/preflight/rebuild")
def rebuild_runtime_live_sim_preflight(
    include_details: bool = True,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        transport_status = _transport_status_payload(db)
        snapshot = _live_sim_preflight_service(db).build_snapshot(
            runtime_status=runtime_supervisor.status(),
            transport_status=transport_status,
            theme_lab_snapshot=_live_sim_preflight_theme_lab_snapshot(db),
            persist=True,
            include_details=True,
        )
        return snapshot if include_details else compact_preflight_snapshot(snapshot)
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/preflight/history")
def runtime_live_sim_preflight_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_live_sim_preflight_snapshots(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/summary")
def runtime_live_sim_canary_summary(trade_date: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        date = trade_date or datetime.now().date().isoformat()
        summary = db.live_sim_canary_summary(trade_date=date)
        runtime_settings = StrategyRuntimeSettingsRepository(db).load()
        config = canary_config_from_settings(runtime_settings)
        latest_preflight = db.latest_live_sim_preflight_snapshot() or {}
        performance = dict(latest_preflight.get("performance_summary") or {})
        go_no_go = dict(performance.get("go_no_go") or {})
        config_status = "disabled"
        if bool(config.get("enabled")) and bool(config.get("order_enabled")):
            config_status = "order-enabled"
        elif bool(config.get("enabled")):
            config_status = "observe-only"
        summary.update(
            {
                "config_status": config_status,
                "canary_config": {
                    "enabled": bool(config.get("enabled")),
                    "order_enabled": bool(config.get("order_enabled")),
                    "max_orders_per_day": config.get("max_orders_per_day"),
                    "max_position_amount_krw": config.get("max_position_amount_krw"),
                    "position_size_multiplier": config.get("position_size_multiplier"),
                },
                "preflight_status": summary.get("preflight_status") or latest_preflight.get("status", ""),
                "load_guard_status": summary.get("load_guard_status") or dict(latest_preflight.get("backfill_summary") or {}).get("load_guard", {}).get("load_guard_status", ""),
                "dry_run_go_no_go_status": summary.get("dry_run_go_no_go_status")
                or str(go_no_go.get("decision") or go_no_go.get("readiness") or ""),
                "net_expectancy_pass": _live_sim_canary_net_expectancy_pass(performance, config),
                "watch_provisional_notice_ko": "WATCH/PROVISIONAL은 아직 LIVE_SIM Canary 주문 대상이 아닙니다.",
            }
        )
        return summary
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/decisions")
def runtime_live_sim_canary_decisions(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    status: Optional[str] = None,
    eligible: Optional[bool] = None,
    reason_code: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_live_sim_canary_decisions(
            trade_date=trade_date,
            code=code,
            status=status,
            eligible=eligible,
            reason_code=reason_code,
            limit=limit + 1,
            offset=offset,
        )
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {
            "items": items,
            "pagination": pagination,
            "filters": {
                "trade_date": trade_date or "",
                "code": code or "",
                "status": status or "",
                "eligible": eligible,
                "reason_code": reason_code or "",
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/decisions/{decision_id}")
def runtime_live_sim_canary_decision_detail(decision_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        item = db.get_live_sim_canary_decision(decision_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Canary decision not found")
        return item
    finally:
        close_database(db)


@app.post("/api/runtime/live-sim/canary/rebuild")
def rebuild_runtime_live_sim_canary(
    body: dict[str, Any] = Body(default_factory=dict),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        trade_date = str(payload.get("trade_date") or datetime.now().date().isoformat())
        limit = max(1, min(5000, int(payload.get("limit") or 500)))
        runtime_settings = StrategyRuntimeSettingsRepository(db).load()
        config = canary_config_from_settings(runtime_settings)
        analysis_config = {**config, "order_enabled": False}
        latest_preflight = db.latest_live_sim_preflight_snapshot() or {}
        events = HybridValidationRepository(db).list_events(trade_date=trade_date)
        saved: list[dict[str, Any]] = []
        for event in events[:limit]:
            metadata = _live_sim_canary_metadata_from_hybrid_event(event)
            decision = evaluate_live_sim_canary(
                runtime_settings=runtime_settings,
                canary_config=analysis_config,
                preflight_snapshot=latest_preflight,
                metadata={
                    **metadata,
                    "rebuild_analysis_only": True,
                    "reason_codes": list(metadata.get("reason_codes") or []) + ["CANARY_REBUILD_ANALYSIS_ONLY"],
                },
                counters={
                    "orders_per_day": 0,
                    "orders_per_cycle": 0,
                    "new_positions_per_day": 0,
                    "has_open_order_for_code": False,
                    "has_position_for_code": False,
                },
            )
            saved.append(db.save_live_sim_canary_decision(decision.to_dict()))
        return {
            "status": "REBUILT_ANALYSIS_ONLY",
            "trade_date": trade_date,
            "source_event_count": len(events[:limit]),
            "saved_count": len(saved),
            "order_created": False,
            "gateway_command_created": False,
            "items": saved[:20],
        }
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/performance")
def runtime_live_sim_canary_performance(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    final_status: Optional[str] = None,
    fill_quality_grade: Optional[str] = None,
    exit_quality_grade: Optional[str] = None,
    outcome_match: Optional[str] = None,
    issue_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _live_sim_canary_performance_analyzer(db).build_report(
            trade_date=trade_date,
            code=code,
            final_status=final_status,
            fill_quality_grade=fill_quality_grade,
            exit_quality_grade=exit_quality_grade,
            outcome_match=outcome_match,
            issue_type=issue_type,
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


@app.post("/api/runtime/live-sim/canary/performance/rebuild")
def rebuild_runtime_live_sim_canary_performance(
    body: Optional[dict[str, Any]] = Body(default=None),
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        resolved_trade_date = str(payload.get("trade_date") or trade_date or datetime.now().date().isoformat())
        resolved_persist = bool(payload.get("persist", persist))
        resolved_export = str(payload.get("export") or export or "json")
        analyzer = _live_sim_canary_performance_analyzer(db)
        report = analyzer.build_report(trade_date=resolved_trade_date, limit=10000)
        persisted = analyzer.persist_report(report) if resolved_persist else None
        exports = analyzer.export_report(report, fmt=resolved_export) if resolved_export else {}
        return {
            "report_id": report["report_id"],
            "persisted": persisted is not None,
            "exported": exports,
            "safety_scope": report.get("safety_scope", {}),
            "analysis_only": True,
            "report": report,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/performance/reports")
def runtime_live_sim_canary_performance_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_live_sim_canary_performance_reports(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/performance/reports/{report_id}")
def runtime_live_sim_canary_performance_report_detail(report_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        report = db.get_live_sim_canary_performance_report(report_id)
        if report is None:
            return {"report_id": report_id, "found": False}
        report["found"] = True
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/performance/cases")
def runtime_live_sim_canary_performance_cases(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    final_status: Optional[str] = None,
    fill_quality_grade: Optional[str] = None,
    exit_quality_grade: Optional[str] = None,
    outcome_match: Optional[str] = None,
    issue_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_live_sim_canary_performance_cases(
            trade_date=trade_date,
            code=code,
            final_status=final_status,
            fill_quality_grade=fill_quality_grade,
            exit_quality_grade=exit_quality_grade,
            outcome_match=outcome_match,
            issue_type=issue_type,
            limit=limit + 1,
            offset=offset,
        )
        if not items and offset == 0:
            report = _live_sim_canary_performance_analyzer(db).build_report(
                trade_date=trade_date,
                code=code,
                final_status=final_status,
                fill_quality_grade=fill_quality_grade,
                exit_quality_grade=exit_quality_grade,
                outcome_match=outcome_match,
                issue_type=issue_type,
                limit=limit + 1,
                offset=0,
            )
            items = list(report.get("items") or [])
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {
            "items": items,
            "pagination": pagination,
            "filters": {
                "trade_date": trade_date or "",
                "code": code or "",
                "final_status": final_status or "",
                "fill_quality_grade": fill_quality_grade or "",
                "exit_quality_grade": exit_quality_grade or "",
                "outcome_match": outcome_match or "",
                "issue_type": issue_type or "",
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/runtime/live-sim/canary/performance/cases/{case_id}")
def runtime_live_sim_canary_performance_case_detail(case_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        item = db.get_live_sim_canary_performance_case(case_id)
        if item is None:
            raise HTTPException(status_code=404, detail="LIVE_SIM Canary performance case not found")
        return item
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation")
def runtime_exit_policy_validation(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    scenario_id: Optional[str] = None,
    comparison_label: Optional[str] = None,
    exit_trigger_type: Optional[str] = None,
    segment: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    issue_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _exit_policy_validation_analyzer(db).build_report(
            trade_date=trade_date,
            code=code,
            scenario_id=scenario_id,
            comparison_label=comparison_label,
            exit_trigger_type=exit_trigger_type,
            segment=segment,
            recommendation_grade=recommendation_grade,
            issue_type=issue_type,
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


@app.post("/api/runtime/exit-policy/validation/rebuild")
def rebuild_runtime_exit_policy_validation(
    body: Optional[dict[str, Any]] = Body(default=None),
    trade_date: Optional[str] = None,
    persist: bool = True,
    export: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        resolved_trade_date = str(payload.get("trade_date") or trade_date or "")
        resolved_persist = bool(payload.get("persist", persist))
        resolved_export = str(payload.get("export") or export or "json")
        analyzer = _exit_policy_validation_analyzer(db)
        report = analyzer.build_report(trade_date=resolved_trade_date or None, limit=10000)
        persisted = analyzer.persist_report(report) if resolved_persist else None
        exports = analyzer.export_report(report, fmt=resolved_export) if resolved_export else {}
        return {
            "report_id": report["report_id"],
            "persisted": persisted is not None,
            "persisted_report": persisted or {},
            "exported": exports,
            "safety_scope": report.get("safety_scope", {}),
            "analysis_only": True,
            "gateway_command_created": False,
            "settings_changed": False,
            "report": report,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation/reports")
def runtime_exit_policy_validation_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = _exit_policy_validation_analyzer(db).list_reports(limit=limit + 1, offset=offset)
        items, pagination = _trim_page(items, limit=limit, offset=offset)
        return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation/reports/{report_id}")
def runtime_exit_policy_validation_report_detail(report_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        report = _exit_policy_validation_analyzer(db).get_report(report_id)
        if report is None:
            return {"report_id": report_id, "found": False}
        report["found"] = True
        return report
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation/scenarios")
def runtime_exit_policy_validation_scenarios(
    trade_date: Optional[str] = None,
    scenario_id: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _exit_policy_validation_analyzer(db).build_report(
            trade_date=trade_date,
            scenario_id=scenario_id,
            recommendation_grade=recommendation_grade,
            limit=10000,
        )
        items = list(report.get("scenario_summary") or [])
        if scenario_id:
            items = [item for item in items if item.get("scenario_id") == scenario_id]
        if recommendation_grade:
            items = [item for item in items if item.get("recommendation_grade") == recommendation_grade]
        page, pagination = _trim_page(items[offset : offset + limit + 1], limit=limit, offset=offset)
        return {
            "items": page,
            "pagination": pagination,
            "filters": {
                "trade_date": trade_date or "",
                "scenario_id": scenario_id or "",
                "recommendation_grade": recommendation_grade or "",
                "limit": limit,
                "offset": offset,
            },
            "analysis_only": True,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation/cases")
def runtime_exit_policy_validation_cases(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    scenario_id: Optional[str] = None,
    comparison_label: Optional[str] = None,
    exit_trigger_type: Optional[str] = None,
    segment: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    issue_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        report = _exit_policy_validation_analyzer(db).build_report(
            trade_date=trade_date,
            code=code,
            scenario_id=scenario_id,
            comparison_label=comparison_label,
            exit_trigger_type=exit_trigger_type,
            segment=segment,
            recommendation_grade=recommendation_grade,
            issue_type=issue_type,
            limit=limit,
            offset=offset,
        )
        return {
            "items": report.get("items", []),
            "pagination": _pagination_payload(
                limit=limit,
                offset=offset,
                count=len(report.get("items") or []),
                total=int(report.get("total_items") or 0),
            ),
            "filters": report.get("filters", {}),
            "analysis_only": True,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/exit-policy/validation/cases/{case_id}")
def runtime_exit_policy_validation_case_detail(case_id: str, trade_date: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _exit_policy_validation_analyzer(db)
        for meta in analyzer.list_reports(limit=500, offset=0):
            report = analyzer.get_report(str(meta.get("report_id") or ""))
            if not report:
                continue
            for item in report.get("items") or []:
                if item.get("case_id") == case_id:
                    item["found"] = True
                    return item
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        for item in report.get("items") or []:
            if item.get("case_id") == case_id:
                item["found"] = True
                return item
        raise HTTPException(status_code=404, detail="Exit policy validation case not found")
    finally:
        close_database(db)


def _live_sim_canary_net_expectancy_pass(performance: dict[str, Any], config: dict[str, Any]) -> bool:
    if not performance:
        return False
    value = performance.get("net_expectancy_pct", performance.get("net_expectancy"))
    try:
        expectancy = float(value)
    except (TypeError, ValueError):
        return False
    try:
        minimum = float(config.get("min_net_expectancy_pct") or 0.0)
    except (TypeError, ValueError):
        minimum = 0.0
    return expectancy >= minimum


def _live_sim_canary_metadata_from_hybrid_event(event: Any) -> dict[str, Any]:
    details = dict(getattr(event, "details_json", {}) or {})
    pipeline = dict(details.get("pipeline_details") or details.get("pipeline_summary") or {})
    return {
        "trade_date": str(getattr(event, "trade_date", "") or ""),
        "code": str(getattr(event, "stock_code", "") or ""),
        "hybrid_status": str(getattr(event, "hybrid_status", "") or ""),
        "hybrid_score": getattr(event, "hybrid_score", None),
        "hybrid_position_tier": str(getattr(event, "hybrid_position_tier", "") or ""),
        "dynamic_theme_status": str(getattr(event, "theme_status", "") or ""),
        "theme_name": str(getattr(event, "theme_name", "") or ""),
        "theme_score": getattr(event, "theme_score", None),
        "stock_role": str(getattr(event, "leader_type", "") or ""),
        "price_location_status": str(details.get("price_location_status") or pipeline.get("price_location_status") or ""),
        "price_location_readiness": str(details.get("price_location_readiness") or pipeline.get("price_location_readiness") or ""),
        "risk_level": str(details.get("risk_level") or pipeline.get("risk_level") or ""),
        "latest_tick_ready": bool(details.get("latest_tick_ready", False)),
        "support_ready": bool(details.get("support_ready", False)),
        "vwap_or_recent_support_ready": bool(details.get("vwap_or_recent_support_ready", False)),
        "gate_usable": bool(details.get("gate_usable", False)),
        "limit_price": int(details.get("limit_price") or details.get("base_price") or 0),
        "current_price": int(details.get("current_price") or details.get("base_price") or 0),
        "reason_codes": list(getattr(event, "hybrid_reason_codes", []) or []),
        "hybrid_validation_event_id": getattr(event, "id", None),
        "hybrid_validation_details": details,
    }


def _live_sim_canary_snapshot_payload(db: TradingDatabase, *, trade_date: str) -> dict[str, Any]:
    runtime_settings = StrategyRuntimeSettingsRepository(db).load()
    config = canary_config_from_settings(runtime_settings)
    summary = db.live_sim_canary_summary(trade_date=trade_date)
    recent = db.list_live_sim_canary_decisions(trade_date=trade_date, limit=8)
    config_status = "disabled"
    if bool(config.get("enabled")) and bool(config.get("order_enabled")):
        config_status = "order-enabled"
    elif bool(config.get("enabled")):
        config_status = "observe-only"
    return {
        "available": bool(summary.get("total_count") or config.get("enabled")),
        "status": config_status.upper().replace("-", "_"),
        "config_status": config_status,
        "enabled": bool(config.get("enabled")),
        "order_enabled": bool(config.get("order_enabled")),
        "trade_date": trade_date,
        "summary": summary,
        "recent_decisions": recent,
        "max_orders_per_day": config.get("max_orders_per_day"),
        "max_position_amount_krw": config.get("max_position_amount_krw"),
        "position_size_multiplier": config.get("position_size_multiplier"),
        "watch_provisional_notice_ko": "WATCH/PROVISIONAL은 아직 LIVE_SIM Canary 주문 대상이 아닙니다.",
        "last_updated_at": recent[0].get("created_at") if recent else "",
    }


@app.get("/api/runtime/outcomes/intraday")
def runtime_intraday_outcomes(
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    outcome_label: Optional[str] = None,
    action_type: Optional[str] = None,
    gate_status: Optional[str] = None,
    reason_family: Optional[str] = None,
    reason_code: Optional[str] = None,
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    min_max_return_pct: Optional[float] = None,
    max_drawdown_pct: Optional[float] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_strategy_decision_outcomes(
            trade_date=trade_date,
            code=code,
            outcome_label=outcome_label,
            action_type=action_type,
            gate_status=gate_status,
            reason_family=reason_family,
            reason_code=reason_code,
            horizon_sec=horizon_sec,
            min_max_return_pct=min_max_return_pct,
            max_drawdown_pct=max_drawdown_pct,
            limit=limit,
            offset=offset,
        )
        total = db.strategy_decision_outcome_count(
            trade_date=trade_date,
            code=code,
            outcome_label=outcome_label,
            action_type=action_type,
            gate_status=gate_status,
            reason_family=reason_family,
            reason_code=reason_code,
            horizon_sec=horizon_sec,
            min_max_return_pct=min_max_return_pct,
            max_drawdown_pct=max_drawdown_pct,
        )
        filters = {
            "trade_date": trade_date or "",
            "code": code or "",
            "outcome_label": outcome_label or "",
            "action_type": action_type or "",
            "gate_status": gate_status or "",
            "reason_family": reason_family or "",
            "reason_code": reason_code or "",
            "horizon_sec": horizon_sec,
            "min_max_return_pct": min_max_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "limit": limit,
            "offset": offset,
        }
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items), total=total),
            "filters": filters,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/outcomes/intraday/summary")
def runtime_intraday_outcome_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
) -> dict[str, Any]:
    db = open_database()
    try:
        summary = db.strategy_decision_outcome_summary(
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
        )
        return {
            "summary": summary,
            "filters": {"trade_date": trade_date or "", "window_sec": window_sec, "horizon_sec": horizon_sec},
        }
    finally:
        close_database(db)


@app.post("/api/runtime/outcomes/intraday/rebuild")
def rebuild_runtime_intraday_outcomes(
    trade_date: Optional[str] = None,
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    force: bool = False,
    limit: int = Query(10000, ge=1, le=100000),
    persist: bool = True,
) -> dict[str, Any]:
    db = open_database()
    try:
        result = _intraday_outcome_labeler(db).rebuild(
            trade_date=trade_date,
            horizon_sec=horizon_sec,
            force=force,
            limit=limit,
            persist=persist,
        )
        result["summary"] = db.strategy_decision_outcome_summary(trade_date=trade_date, horizon_sec=horizon_sec) if persist else {}
        return result
    finally:
        close_database(db)


@app.get("/api/runtime/promotion/evidence")
def runtime_promotion_evidence(
    trade_date: Optional[str] = None,
    policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
    current_stage: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: Optional[int] = Query(None, ge=1, le=10000),
) -> dict[str, Any]:
    db = open_database()
    try:
        adapter = _promotion_evidence_adapter(db)
        evidence = adapter.build_evidence(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
        return {
            "policy_id": evidence.policy_id,
            "evidence": evidence.to_dict(),
            "config": adapter.config.to_dict(),
            "filters": adapter.filters(
                policy_id=policy_id,
                current_stage=current_stage,
                trade_date=trade_date,
                window_sec=window_sec,
                horizon_sec=horizon_sec,
                limit=limit,
            ),
        }
    finally:
        close_database(db)


@app.get("/api/runtime/promotion/decision")
def runtime_promotion_decision(
    trade_date: Optional[str] = None,
    policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
    current_stage: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: Optional[int] = Query(None, ge=1, le=10000),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _promotion_evidence_adapter(db).evaluate(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
    finally:
        close_database(db)


@app.get("/api/runtime/promotion/matrix")
def runtime_promotion_matrix(
    trade_date: Optional[str] = None,
    policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
    current_stage: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: Optional[int] = Query(None, ge=1, le=10000),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _promotion_evidence_adapter(db).matrix(
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
        )
    finally:
        close_database(db)


@app.get("/api/runtime/promotion/drilldown")
def runtime_promotion_drilldown(
    trade_date: Optional[str] = None,
    blocker: Optional[str] = None,
    policy_id: str = DEFAULT_PROMOTION_POLICY_ID,
    current_stage: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: Optional[int] = Query(None, ge=1, le=10000),
    detail_limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _promotion_evidence_adapter(db).drilldown(
            blocker=blocker,
            policy_id=policy_id,
            current_stage=current_stage,
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            limit=limit,
            detail_limit=detail_limit,
        )
    finally:
        close_database(db)


@app.get("/api/runtime/shadow-strategies/policies")
def runtime_shadow_strategy_policies() -> dict[str, Any]:
    db = open_database()
    try:
        evaluator = _shadow_strategy_evaluator(db)
        policies = [policy.to_dict() for policy in evaluator.load_policies(include_baseline=True)]
        return {
            "policies": policies,
            "items": policies,
            "enabled": bool(evaluator.config.enabled),
            "observe_only": bool(evaluator.config.observe_only),
            "allow_apply": False,
            "disclaimer_ko": "Shadow 결과는 장중 진단용이며 실제 전략 설정에 자동 적용되지 않습니다.",
        }
    finally:
        close_database(db)


@app.get("/api/runtime/shadow-strategies/evaluations")
def runtime_shadow_strategy_evaluations(
    trade_date: Optional[str] = None,
    policy_id: Optional[str] = None,
    code: Optional[str] = None,
    theme_name: Optional[str] = None,
    baseline_gate_status: Optional[str] = None,
    shadow_gate_status: Optional[str] = None,
    change_type: Optional[str] = None,
    changed_decision: Optional[bool] = None,
    outcome_label: Optional[str] = None,
    expected_risk: Optional[str] = None,
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_shadow_strategy_evaluations(
            trade_date=trade_date,
            policy_id=policy_id,
            code=code,
            theme_name=theme_name,
            baseline_gate_status=baseline_gate_status,
            shadow_gate_status=shadow_gate_status,
            change_type=change_type,
            changed_decision=changed_decision,
            outcome_label=outcome_label,
            expected_risk=expected_risk,
            horizon_sec=horizon_sec,
            limit=limit,
            offset=offset,
        )
        total = db.shadow_strategy_evaluation_count(
            trade_date=trade_date,
            policy_id=policy_id,
            code=code,
            theme_name=theme_name,
            baseline_gate_status=baseline_gate_status,
            shadow_gate_status=shadow_gate_status,
            change_type=change_type,
            changed_decision=changed_decision,
            outcome_label=outcome_label,
            expected_risk=expected_risk,
            horizon_sec=horizon_sec,
        )
        filters = {
            "trade_date": trade_date or "",
            "policy_id": policy_id or "",
            "code": code or "",
            "theme_name": theme_name or "",
            "baseline_gate_status": baseline_gate_status or "",
            "shadow_gate_status": shadow_gate_status or "",
            "change_type": change_type or "",
            "changed_decision": changed_decision,
            "outcome_label": outcome_label or "",
            "expected_risk": expected_risk or "",
            "horizon_sec": horizon_sec,
            "limit": limit,
            "offset": offset,
        }
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items), total=total),
            "filters": filters,
        }
    finally:
        close_database(db)


@app.get("/api/runtime/shadow-strategies/summary")
def runtime_shadow_strategy_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
    horizon_sec: Optional[int] = Query(None, ge=1, le=86400),
    policy_id: Optional[str] = None,
) -> dict[str, Any]:
    db = open_database()
    try:
        summary = db.shadow_strategy_summary(
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            policy_id=policy_id,
        )
        return {
            "summary": summary,
            "filters": {
                "trade_date": trade_date or "",
                "window_sec": window_sec,
                "horizon_sec": horizon_sec,
                "policy_id": policy_id or "",
            },
        }
    finally:
        close_database(db)


@app.post("/api/runtime/shadow-strategies/rebuild")
def rebuild_runtime_shadow_strategies(
    trade_date: Optional[str] = None,
    policy_id: Optional[str] = None,
    force: bool = False,
    limit: int = Query(10000, ge=1, le=100000),
    persist: bool = True,
) -> dict[str, Any]:
    db = open_database()
    try:
        result = _shadow_strategy_evaluator(db).rebuild(
            trade_date=trade_date,
            policy_id=policy_id,
            force=force,
            limit=limit,
            persist=persist,
        )
        result["summary"] = db.shadow_strategy_summary(trade_date=trade_date, policy_id=policy_id) if persist else {}
        return result
    finally:
        close_database(db)


@app.get("/api/runtime/replay/bundles")
def runtime_replay_bundles(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items = list_replay_bundles(DEFAULT_BUNDLE_ROOT, limit=limit + 1, offset=offset)
    items, pagination = _trim_page(items, limit=limit, offset=offset)
    return {"items": items, "pagination": pagination, "filters": {"limit": limit, "offset": offset}}


@app.post("/api/runtime/replay/bundles/export")
def export_runtime_replay_bundle(
    trade_date: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    codes: Optional[str] = None,
    theme_names: Optional[str] = None,
    force: bool = False,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    settings = get_settings()
    exporter = StrategyReplayBundleExporter(settings.db_path, output_root=DEFAULT_BUNDLE_ROOT)
    bundle = exporter.export_bundle(
        trade_date,
        start_time=start_time,
        end_time=end_time,
        codes=_csv_values(codes),
        theme_names=_csv_values(theme_names),
        force=force,
    )
    return {
        "replay_id": bundle.manifest.replay_id,
        "bundle_path": str(bundle.path),
        "manifest": bundle.manifest.to_dict(),
        "summary": bundle.manifest.data_quality,
        "warnings": bundle.manifest.warnings,
    }


@app.get("/api/runtime/replay/runs")
def runtime_replay_runs(
    trade_date: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items = scan_replay_runs(DEFAULT_REPLAY_DB_ROOT, trade_date=trade_date, mode=mode, limit=limit + 1, offset=offset)
    items, pagination = _trim_page(items, limit=limit, offset=offset)
    return {
        "items": items,
        "pagination": pagination,
        "filters": {"trade_date": trade_date or "", "mode": mode or "", "limit": limit, "offset": offset},
    }


@app.post("/api/runtime/replay/run")
def run_runtime_replay(
    bundle_path: Optional[str] = None,
    trade_date: Optional[str] = None,
    mode: str = Query("decision_led", pattern="^(data_only|decision_led|full_runtime)$"),
    cycle_interval_sec: Optional[float] = Query(None, ge=0.1, le=3600),
    speed: float = Query(1.0, ge=0.0, le=1000),
    replay_db: Optional[str] = None,
    force: bool = False,
    limit: Optional[int] = Query(None, ge=1, le=100000),
    export_report: bool = True,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    settings = get_settings()
    runner = StrategyRuntimeReplayRunner(
        source_db_path=settings.db_path,
        replay_db_root=DEFAULT_REPLAY_DB_ROOT,
        bundle_root=DEFAULT_BUNDLE_ROOT,
    )
    result = runner.run(
        bundle_path=bundle_path,
        trade_date=trade_date,
        mode=mode,
        cycle_interval_sec=cycle_interval_sec,
        speed=speed,
        replay_db=replay_db,
        force=force,
        limit=limit,
        export_report=export_report,
    )
    return {
        "replay_id": result.replay_id,
        "status": result.status,
        "replay_db_path": result.replay_db_path,
        "source_bundle_path": result.source_bundle_path,
        "report_id": (result.report or {}).get("report_id", ""),
        "summary": result.summary,
        "warnings": result.warnings,
        "error": result.error,
    }


@app.get("/api/runtime/replay/runs/{replay_id}")
def runtime_replay_run_detail(replay_id: str) -> dict[str, Any]:
    return get_replay_run_detail(replay_id, DEFAULT_REPLAY_DB_ROOT)


@app.get("/api/runtime/replay/reports/{report_id}")
def runtime_replay_report_detail(report_id: str) -> dict[str, Any]:
    return get_replay_report_detail(report_id, DEFAULT_REPLAY_DB_ROOT)


@app.get("/api/runtime/replay/summary")
def runtime_replay_summary(
    trade_date: Optional[str] = None,
    replay_id: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    reports = scan_replay_reports(
        DEFAULT_REPLAY_DB_ROOT,
        trade_date=trade_date,
        replay_id=replay_id,
        mode=mode,
        limit=limit + 1,
        offset=offset,
    )
    items, pagination = _trim_page(reports, limit=limit, offset=offset)
    latest = items[0] if items else None
    return {
        "latest": latest,
        "items": items,
        "pagination": pagination,
        "filters": {"trade_date": trade_date or "", "replay_id": replay_id or "", "mode": mode or "", "limit": limit, "offset": offset},
    }


@app.get("/api/runtime/change-proposals")
def runtime_change_proposals(
    trade_date: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    recommendation_grade: Optional[str] = None,
    source_type: Optional[str] = None,
    target_component: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_strategy_change_proposals(
            trade_date=trade_date,
            status=status,
            category=category,
            recommendation_grade=recommendation_grade,
            source_type=source_type,
            target_component=target_component,
            limit=limit,
            offset=offset,
        )
        total = db.strategy_change_proposal_count(
            trade_date=trade_date,
            status=status,
            category=category,
            recommendation_grade=recommendation_grade,
            source_type=source_type,
            target_component=target_component,
        )
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items), total=total),
            "filters": {
                "trade_date": trade_date or "",
                "status": status or "",
                "category": category or "",
                "recommendation_grade": recommendation_grade or "",
                "source_type": source_type or "",
                "target_component": target_component or "",
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/runtime/change-proposals/evidence")
def runtime_change_proposal_evidence(
    proposal_id: Optional[str] = None,
    trade_date: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = db.list_strategy_change_evidence(
            proposal_id=proposal_id,
            trade_date=trade_date,
            source_type=source_type,
            limit=limit,
            offset=offset,
        )
        return {
            "items": items,
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(items)),
            "filters": {"proposal_id": proposal_id or "", "trade_date": trade_date or "", "source_type": source_type or "", "limit": limit, "offset": offset},
        }
    finally:
        close_database(db)


@app.get("/api/runtime/change-proposals/summary")
def runtime_change_proposal_summary(
    trade_date: Optional[str] = None,
    window_sec: Optional[int] = Query(None, ge=1, le=86400),
) -> dict[str, Any]:
    db = open_database()
    try:
        summary = db.strategy_change_proposal_summary(trade_date=trade_date, window_sec=window_sec)
        return {"summary": summary, "filters": {"trade_date": trade_date or "", "window_sec": window_sec}}
    finally:
        close_database(db)


@app.get("/api/runtime/change-proposals/{proposal_id}")
def runtime_change_proposal_detail(proposal_id: str) -> dict[str, Any]:
    db = open_database()
    try:
        proposal = db.get_strategy_change_proposal(proposal_id)
        if proposal is None:
            return {"found": False, "proposal_id": proposal_id}
        evidence = db.list_strategy_change_evidence(proposal_id, limit=1000)
        approvals = db.list_strategy_change_approvals(proposal_id, limit=200)
        config_diff = build_config_diff(proposal)
        return {
            "found": True,
            "proposal": proposal,
            "evidence": evidence,
            "approvals": approvals,
            "config_diff": config_diff,
            "rollout_plan": proposal.get("rollout_plan") or {},
            "rollback_plan": proposal.get("rollback_plan") or {},
            "related_reports": _proposal_related_reports(proposal),
            "disclaimer_ko": "승인해도 실제 runtime config에 자동 반영하지 않습니다. 후속 PR에서 observe-only rollout에 연결할 수 있습니다.",
        }
    finally:
        close_database(db)


@app.post("/api/runtime/change-proposals/generate")
def generate_runtime_change_proposals(
    trade_date: str,
    source_type: Optional[str] = Query("combined", pattern="^(intraday_outcome|shadow_strategy|replay|threshold_ab|conservative_reason_outcome|shadow_small_entry_promotion|shadow_small_entry_pilot|combined)$"),
    replay_id: Optional[str] = None,
    force: bool = False,
    persist: bool = True,
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        # force is accepted for API symmetry. Stable proposal IDs make generation idempotent without deleting approvals.
        result = _change_proposal_generator(db).generate(
            trade_date=trade_date,
            source_type=source_type or "combined",
            replay_id=replay_id,
            persist=persist,
        )
        result["force"] = bool(force)
        return result
    finally:
        close_database(db)


@app.post("/api/runtime/change-proposals/{proposal_id}/approve-observe")
def approve_observe_change_proposal(
    proposal_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    return _record_change_proposal_action(proposal_id, "approve_observe", "APPROVED_FOR_OBSERVE", body or {})


@app.post("/api/runtime/change-proposals/{proposal_id}/approve-dry-run")
def approve_dry_run_change_proposal(
    proposal_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    return _record_change_proposal_action(proposal_id, "approve_dry_run", "APPROVED_FOR_DRY_RUN", body or {})


@app.post("/api/runtime/change-proposals/{proposal_id}/reject")
def reject_change_proposal(
    proposal_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    if not str((body or {}).get("note") or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="note is required")
    return _record_change_proposal_action(proposal_id, "reject", "REJECTED", body or {})


@app.post("/api/runtime/change-proposals/{proposal_id}/expire")
def expire_change_proposal(
    proposal_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    return _record_change_proposal_action(proposal_id, "expire", "EXPIRED", body or {})


@app.post("/api/runtime/change-proposals/{proposal_id}/note")
def note_change_proposal(
    proposal_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    if not str((body or {}).get("note") or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="note is required")
    return _record_change_proposal_action(proposal_id, "note", "", body or {})


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


@app.get("/api/runtime/market-gate/review")
def runtime_market_gate_review(
    trade_date: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _market_gate_review_analyzer(db).build_report(trade_date=trade_date, limit=limit)
    finally:
        close_database(db)


@app.get("/api/runtime/market-gate/review/export")
def export_runtime_market_gate_review(
    trade_date: Optional[str] = None,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _market_gate_review_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=10000)
        return {
            "report_id": report["report_id"],
            "exports": analyzer.export_report(report, fmt=format),
            "notes": report.get("notes", []),
        }
    finally:
        close_database(db)


@app.get("/api/runtime/performance/theme-lab-gate-reasons")
def runtime_theme_lab_gate_reason_outcomes(
    trade_date: Optional[str] = None,
    limit: int = Query(10000, ge=1, le=50000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _theme_lab_gate_reason_outcome_analyzer(db).build_report(trade_date=trade_date, limit=limit, offset=offset)
    finally:
        close_database(db)


@app.get("/api/runtime/performance/theme-lab-gate-reasons/export")
def export_runtime_theme_lab_gate_reason_outcomes(
    trade_date: Optional[str] = None,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        analyzer = _theme_lab_gate_reason_outcome_analyzer(db)
        report = analyzer.build_report(trade_date=trade_date, limit=50000)
        return {
            "report_id": report["report_id"],
            "exports": analyzer.export_report(report, fmt=format),
            "notes": report.get("notes", []),
        }
    finally:
        close_database(db)


@app.get("/api/conservative-reason-outcomes")
def conservative_reason_outcomes(
    trade_date: Optional[str] = None,
    limit: int = Query(10000, ge=1, le=50000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        return _conservative_reason_outcome_analyzer(db).build_report(trade_date=resolved_trade_date, limit=limit, offset=offset)
    finally:
        close_database(db)


@app.get("/api/conservative-reason-outcomes/summary")
def conservative_reason_outcomes_summary(
    trade_date: Optional[str] = None,
    limit: int = Query(10000, ge=1, le=50000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        report = _conservative_reason_outcome_analyzer(db).build_report(trade_date=resolved_trade_date, limit=limit)
        return conservative_reason_snapshot_payload(report)
    finally:
        close_database(db)


@app.get("/api/conservative-reason-outcomes/items")
def conservative_reason_outcomes_items(
    trade_date: Optional[str] = None,
    reason_group: str = "",
    reason_code: str = "",
    recommendation: str = "",
    code: str = "",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        analyzer = _conservative_reason_outcome_analyzer(db)
        report = analyzer.build_report(trade_date=resolved_trade_date, limit=50000)
        rows = analyzer.filter_items(
            report,
            reason_group=reason_group,
            reason_code=reason_code,
            recommendation=recommendation,
            code=code,
        )
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "trade_date": report.get("trade_date") or resolved_trade_date,
            "generated_at": report.get("generated_at") or "",
            "total": len(rows),
            "items": rows[start:end],
            "pagination": _pagination_payload(limit=limit, offset=offset, count=len(rows[start:end]), total=len(rows)),
            "filters": {
                "reason_group": reason_group,
                "reason_code": reason_code,
                "recommendation": recommendation,
                "code": code,
            },
        }
    finally:
        close_database(db)


@app.get("/api/conservative-reason-outcomes/generate")
def generate_conservative_reason_outcomes(
    trade_date: Optional[str] = None,
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    limit: int = Query(50000, ge=1, le=100000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        analyzer = _conservative_reason_outcome_analyzer(db)
        report = analyzer.build_report(trade_date=resolved_trade_date, limit=limit)
        return {
            "report_id": report.get("report_id") or "",
            "trade_date": report.get("trade_date") or resolved_trade_date,
            "summary": report.get("summary") or {},
            "exports": analyzer.export_report(report, fmt=format) if export else {},
            "disclaimer_ko": report.get("disclaimer_ko") or "",
        }
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-promotion/summary")
def shadow_small_entry_promotion_summary(
    trade_date: Optional[str] = None,
    limit: int = Query(50000, ge=1, le=100000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        report = _shadow_small_entry_promotion_analyzer(db).build_report(
            trade_date=resolved_trade_date,
            limit=limit,
            include_traces=True,
        )
        return shadow_small_entry_snapshot_payload(report)
    except Exception as exc:
        return shadow_small_entry_empty_payload(str(exc))
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-promotion/candidates")
def shadow_small_entry_promotion_candidates(
    trade_date: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        items = _shadow_small_entry_promotion_analyzer(db).candidates(trade_date=resolved_trade_date, limit=limit)
        return {
            "trade_date": resolved_trade_date,
            "total": len(items),
            "items": items,
            "filters": {"trade_date": resolved_trade_date, "limit": limit},
        }
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-promotion/traces")
def shadow_small_entry_promotion_traces(
    trade_date: Optional[str] = None,
    code: str = "",
    candidate_instance_id: str = "",
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        items = _shadow_small_entry_promotion_analyzer(db).traces(
            trade_date=resolved_trade_date,
            code=code,
            candidate_instance_id=candidate_instance_id,
            limit=limit,
        )
        return {
            "trade_date": resolved_trade_date,
            "total": len(items),
            "items": items,
            "filters": {
                "trade_date": resolved_trade_date,
                "code": code,
                "candidate_instance_id": candidate_instance_id,
                "limit": limit,
            },
        }
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-promotion/generate")
def generate_shadow_small_entry_promotion(
    trade_date: Optional[str] = None,
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
    limit: int = Query(50000, ge=1, le=100000),
) -> dict[str, Any]:
    resolved_trade_date = trade_date or datetime.now().date().isoformat()
    db = open_database()
    try:
        analyzer = _shadow_small_entry_promotion_analyzer(db)
        report = analyzer.build_report(trade_date=resolved_trade_date, limit=limit)
        normalized = "md" if format == "markdown" else format
        exports = {}
        if export:
            if normalized == "all":
                exports = analyzer.export_all(report)
            elif normalized == "csv":
                exports = {"csv": str(analyzer.export_csv(report, analyzer.report_root / resolved_trade_date / f"shadow_small_entry_promotion_{resolved_trade_date}.csv"))}
            elif normalized == "md":
                exports = {"md": str(analyzer.export_markdown(report, analyzer.report_root / resolved_trade_date / f"shadow_small_entry_promotion_{resolved_trade_date}.md"))}
            else:
                exports = {"json": str(analyzer.export_json(report, analyzer.report_root / resolved_trade_date / f"shadow_small_entry_promotion_{resolved_trade_date}.json"))}
        return {
            "trade_date": resolved_trade_date,
            "summary": report.get("summary") or {},
            "exports": exports,
            "disclaimer_ko": report.get("disclaimer_ko") or "",
        }
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-ops/status")
def shadow_small_entry_ops_status(trade_date: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        return shadow_small_entry_ops_snapshot_payload(
            _shadow_small_entry_ops_service(db).status(trade_date=trade_date)
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/preflight")
def shadow_small_entry_ops_preflight(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_ops_service(db).preflight(trade_date=payload.get("trade_date"), persist=True)
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/arm")
def shadow_small_entry_ops_arm(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_ops_service(db).arm(
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            note=str(payload.get("note") or payload.get("operator_note") or ""),
            trade_date=payload.get("trade_date"),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/confirm")
def shadow_small_entry_ops_confirm(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_ops_service(db).confirm(
            activation_token=str(payload.get("activation_token") or payload.get("token") or ""),
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            note=str(payload.get("note") or payload.get("operator_note") or ""),
            trade_date=payload.get("trade_date"),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/pause")
def shadow_small_entry_ops_pause(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        emergency = bool(payload.get("emergency"))
        return _shadow_small_entry_ops_service(db).pause(
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            note=str(payload.get("note") or payload.get("operator_note") or ("emergency pause" if emergency else "")),
            reason=str(payload.get("reason") or ("SHADOW_SMALL_ENTRY_EMERGENCY_PAUSE" if emergency else "SHADOW_SMALL_ENTRY_OPERATOR_PAUSE")),
            status=str(payload.get("status") or "PAUSED_BY_OPERATOR"),
            trade_date=payload.get("trade_date"),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/rollback")
def shadow_small_entry_ops_rollback(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_ops_service(db).rollback(
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            note=str(payload.get("note") or payload.get("operator_note") or ""),
            trade_date=payload.get("trade_date"),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-ops/risk-check")
def shadow_small_entry_ops_risk_check(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_ops_service(db).risk_check(
            trade_date=payload.get("trade_date"),
            auto_pause=bool(payload.get("auto_pause", True)),
        )
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-ops/audit-log")
def shadow_small_entry_ops_audit_log(
    trade_date: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    db = open_database()
    try:
        items = _shadow_small_entry_ops_service(db).audit_log(trade_date=trade_date, limit=limit)
        return {"trade_date": trade_date or datetime.now().date().isoformat(), "total": len(items), "items": items}
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-ops/report")
def shadow_small_entry_ops_report(
    trade_date: Optional[str] = None,
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
) -> dict[str, Any]:
    db = open_database()
    try:
        service = _shadow_small_entry_ops_service(db)
        report = service.build_report(trade_date=trade_date, limit=500)
        normalized = "md" if format == "markdown" else format
        return {
            **report,
            "exports": service.export_report(report, fmt=normalized) if export else {},
        }
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-pilot/status")
def shadow_small_entry_pilot_status(trade_date: Optional[str] = None) -> dict[str, Any]:
    db = open_database()
    try:
        return shadow_small_entry_pilot_snapshot_payload(
            _shadow_small_entry_pilot_service(db).status(trade_date=trade_date)
        )
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-pilot/report")
def shadow_small_entry_pilot_report(
    trade_date: Optional[str] = None,
    pilot_id: str = "",
    export: bool = False,
    format: str = Query("json", pattern="^(json|csv|md|markdown|all)$"),
) -> dict[str, Any]:
    db = open_database()
    try:
        service = _shadow_small_entry_pilot_service(db)
        report = service.build_report(trade_date=trade_date, pilot_id=pilot_id, persist=False)
        return {**report, "exports": service.export_report(report, fmt=format) if export else {}}
    finally:
        close_database(db)


@app.get("/api/shadow-small-entry-pilot/items")
def shadow_small_entry_pilot_items(
    trade_date: Optional[str] = None,
    pilot_id: str = "",
    status: str = "",
    recommendation: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = open_database()
    try:
        return _shadow_small_entry_pilot_service(db).items(
            trade_date=trade_date,
            pilot_id=pilot_id,
            status=status,
            recommendation=recommendation,
            limit=limit,
            offset=offset,
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-pilot/start")
def shadow_small_entry_pilot_start(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_pilot_service(db).start(
            trade_date=payload.get("trade_date"),
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            operator_note=str(payload.get("note") or payload.get("operator_note") or ""),
            source_report_trade_date=str(payload.get("source_report_trade_date") or ""),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-pilot/complete")
def shadow_small_entry_pilot_complete(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        return _shadow_small_entry_pilot_service(db).complete(
            trade_date=payload.get("trade_date"),
            operator=str(payload.get("operator") or payload.get("changed_by") or "operator"),
            operator_note=str(payload.get("note") or payload.get("operator_note") or ""),
            export=bool(payload.get("export", False)),
            fmt=str(payload.get("format") or "all"),
        )
    finally:
        close_database(db)


@app.post("/api/shadow-small-entry-pilot/generate-report")
def shadow_small_entry_pilot_generate_report(
    body: Optional[dict[str, Any]] = Body(default=None),
    _: None = Depends(verify_gateway_token),
) -> dict[str, Any]:
    db = open_database()
    try:
        payload = dict(body or {})
        service = _shadow_small_entry_pilot_service(db)
        report = service.build_report(trade_date=payload.get("trade_date"), pilot_id=str(payload.get("pilot_id") or ""), persist=True)
        exports = service.export_report(report, fmt=str(payload.get("format") or "all")) if bool(payload.get("export", True)) else {}
        return {"ok": True, "report": report, "exports": exports}
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
            items = [item for item in items if item.get("dry_run_false_positive_type") or item.get("net_bad_ready_type")]
        elif type == "false_negative":
            items = [item for item in items if item.get("dry_run_false_negative_type") or item.get("net_opportunity_type")]
        elif type == "opportunity_loss":
            items = [item for item in items if item.get("opportunity_loss_type") or item.get("net_opportunity_type")]
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
def snapshot(refresh: bool = Query(False), detail: str = Query(DASHBOARD_SNAPSHOT_DETAIL_SLIM)) -> dict[str, Any]:
    return _build_dashboard_snapshot_payload(force=refresh, detail=detail)


@app.get("/api/themelab/snapshot")
def theme_lab_snapshot(refresh: bool = Query(False)) -> dict[str, Any]:
    db = open_database()
    try:
        builder = lambda: build_theme_lab_dashboard_snapshot(
            db,
            runtime_status=runtime_supervisor.status(),
            gateway_state=gateway_state,
        )
        return _cached_theme_lab_dashboard_snapshot(
            db,
            "theme_lab:v2:shared",
            builder,
            force=refresh,
            refresh_builder=_build_theme_lab_dashboard_snapshot_with_fresh_db,
        )
    finally:
        close_database(db)


@app.post("/api/themelab/operator-events")
def ingest_theme_lab_operator_events(body: dict[str, Any]) -> dict[str, Any]:
    events = body.get("events") if isinstance(body, dict) else []
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be a list")
    normalized: list[dict[str, Any]] = []
    rejected_count = 0
    for event in events:
        try:
            normalized.append(_validate_theme_lab_operator_event(event))
        except ValueError:
            rejected_count += 1
    db = open_database()
    try:
        result = db.save_operator_events(normalized)
    finally:
        close_database(db)
    return {
        "inserted_count": int(result.get("inserted_count") or 0),
        "duplicate_count": int(result.get("duplicate_count") or 0),
        "rejected_count": int(result.get("rejected_count") or 0) + rejected_count,
    }


@app.get("/api/themelab/operator-events")
def list_theme_lab_operator_events(
    trade_date: str | None = Query(None),
    severity: str | None = Query(None),
    category: str | None = Query(None),
    symbol: str | None = Query(None),
    include_acknowledged: bool = Query(True),
    include_hidden: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    normalized_severity = str(severity or "").upper() or None
    normalized_category = str(category or "").lower() or None
    if normalized_severity and normalized_severity not in THEMELAB_OPERATOR_EVENT_SEVERITIES:
        raise HTTPException(status_code=400, detail="invalid severity")
    if normalized_category and normalized_category not in THEMELAB_OPERATOR_EVENT_CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    db = open_database()
    try:
        events = db.list_operator_events(
            resolved_trade_date,
            severity=normalized_severity,
            category=normalized_category,
            symbol=symbol,
            include_acknowledged=include_acknowledged,
            include_hidden=include_hidden,
            limit=limit,
        )
    finally:
        close_database(db)
    return {"trade_date": resolved_trade_date, "events": events}


@app.post("/api/themelab/operator-events/ack")
def acknowledge_theme_lab_operator_events(body: dict[str, Any]) -> dict[str, Any]:
    event_ids = body.get("event_ids") if isinstance(body, dict) else []
    if not isinstance(event_ids, list):
        raise HTTPException(status_code=400, detail="event_ids must be a list")
    db = open_database()
    try:
        updated_count = db.acknowledge_operator_events(
            [str(event_id) for event_id in event_ids],
            acknowledged_by=str(body.get("acknowledged_by") or "") if isinstance(body, dict) else "",
        )
    finally:
        close_database(db)
    return {"updated_count": updated_count}


@app.post("/api/themelab/operator-events/hide")
def hide_theme_lab_operator_events(body: dict[str, Any]) -> dict[str, Any]:
    event_ids = body.get("event_ids") if isinstance(body, dict) else []
    if not isinstance(event_ids, list):
        raise HTTPException(status_code=400, detail="event_ids must be a list")
    db = open_database()
    try:
        updated_count = db.hide_operator_events([str(event_id) for event_id in event_ids])
    finally:
        close_database(db)
    return {"updated_count": updated_count}


@app.get("/api/themelab/operator-events/summary")
def theme_lab_operator_event_summary(trade_date: str | None = Query(None)) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    db = open_database()
    try:
        return db.summarize_operator_events(resolved_trade_date)
    finally:
        close_database(db)


@app.get("/api/themelab/operator-actions/catalog")
def theme_lab_operator_action_catalog() -> dict[str, Any]:
    return {
        "actions": [_operator_action_catalog_item(action_type, meta) for action_type, meta in OPERATOR_ACTION_CATALOG.items()],
        "disabled_actions": [_disabled_operator_action_item(action_type, meta) for action_type, meta in FORBIDDEN_OPERATOR_ACTIONS.items()],
    }


@app.get("/api/themelab/operator-actions/recommendations")
def theme_lab_operator_action_recommendations(
    event_id: str | None = Query(None),
    symbol: str | None = Query(None),
    candidate_instance_id: str | None = Query(None),
    trade_date: str | None = Query(None),
) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    db = open_database()
    try:
        return _build_operator_action_recommendations(
            db,
            trade_date=resolved_trade_date,
            event_id=str(event_id or ""),
            symbol=str(symbol or ""),
            candidate_instance_id=str(candidate_instance_id or ""),
        )
    finally:
        close_database(db)


@app.post("/api/themelab/operator-actions/execute")
async def execute_theme_lab_operator_action(body: dict[str, Any], request: Request) -> dict[str, Any]:
    payload = body if isinstance(body, dict) else {}
    action_type = str(payload.get("action_type") or "").strip().upper()
    if not action_type:
        raise HTTPException(status_code=400, detail="action_type is required")
    if action_type not in OPERATOR_ACTION_CATALOG and action_type not in FORBIDDEN_OPERATOR_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown operator action")

    action_id = str(payload.get("action_id") or new_message_id("act"))
    requested_at = datetime.now(KST).isoformat(timespec="seconds")
    db = open_database()
    try:
        existing = db.get_operator_action(action_id)
        if existing and existing.get("status") in {"SUCCESS", "FAILED", "BLOCKED", "SKIPPED"}:
            return {"status": existing.get("status"), "duplicate": True, "action": existing}

        event = db.get_operator_event(str(payload.get("event_id") or "")) if payload.get("event_id") else None
        if action_type in FORBIDDEN_OPERATOR_ACTIONS:
            action = db.save_operator_action(
                _operator_action_record(
                    action_id=action_id,
                    action_type=action_type,
                    status="BLOCKED",
                    requested_at=requested_at,
                    payload=payload,
                    event=event,
                    meta=_disabled_operator_action_item(action_type, FORBIDDEN_OPERATOR_ACTIONS[action_type]),
                    error_message=FORBIDDEN_OPERATOR_ACTIONS[action_type]["reason_ko"],
                )
            )
            _save_operator_action_result_event(db, action, "BLOCKED", {"blocked_reason": action.get("error_message")})
            return {
                "status": "BLOCKED",
                "blocked": True,
                "reason_ko": FORBIDDEN_OPERATOR_ACTIONS[action_type]["reason_ko"],
                "action": action,
            }

        meta = _operator_action_catalog_item(action_type, OPERATOR_ACTION_CATALOG[action_type])
        if meta["confirmation_required"] and not bool(payload.get("confirm")):
            action = db.save_operator_action(
                _operator_action_record(
                    action_id=action_id,
                    action_type=action_type,
                    status="PENDING",
                    requested_at=requested_at,
                    payload=payload,
                    event=event,
                    meta=meta,
                )
            )
            return {"status": "PENDING", "confirmation_required": True, "action": action, "catalog_item": meta}

        action = db.save_operator_action(
            _operator_action_record(
                action_id=action_id,
                action_type=action_type,
                status="RUNNING",
                requested_at=requested_at,
                payload=payload,
                event=event,
                meta=meta,
            )
        )
        if meta["requires_token"]:
            try:
                _verify_operator_action_token(request)
            except HTTPException as exc:
                failed = db.update_operator_action_status(action_id, "FAILED", error_message=str(exc.detail)) or action
                _save_operator_action_result_event(db, failed, "FAILED", {"error": str(exc.detail)})
                raise

        try:
            response_payload = await _execute_operator_action(action_type, payload, db=db, event=event)
        except Exception as exc:
            failed = db.update_operator_action_status(action_id, "FAILED", error_message=str(exc)) or action
            _save_operator_action_result_event(db, failed, "FAILED", {"error": str(exc)})
            return {"status": "FAILED", "action": failed, "error": str(exc)}

        saved = db.update_operator_action_status(action_id, "SUCCESS", response=response_payload) or action
        _save_operator_action_result_event(db, saved, "SUCCESS", response_payload)
        return {"status": "SUCCESS", "action": saved, "result": response_payload}
    finally:
        close_database(db)


@app.get("/api/themelab/operator-actions")
def list_theme_lab_operator_actions(
    trade_date: str | None = Query(None),
    action_type: str | None = Query(None),
    status: str | None = Query(None),
    symbol: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    normalized_status = str(status or "").upper() or None
    if normalized_status and normalized_status not in THEMELAB_OPERATOR_ACTION_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    db = open_database()
    try:
        items = db.list_operator_actions(
            resolved_trade_date,
            action_type=action_type,
            status=normalized_status,
            symbol=symbol,
            limit=limit + 1,
            offset=offset,
        )
        page, pagination = _trim_page(items, limit=limit, offset=offset)
        return {
            "trade_date": resolved_trade_date,
            "actions": page,
            "items": page,
            "pagination": pagination,
            "filters": {
                "trade_date": resolved_trade_date,
                "action_type": action_type or "",
                "status": normalized_status or "",
                "symbol": symbol or "",
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/themelab/operator-actions/summary")
def theme_lab_operator_action_summary(trade_date: str | None = Query(None)) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    db = open_database()
    try:
        return db.summarize_operator_actions(resolved_trade_date)
    finally:
        close_database(db)


@app.post("/api/themelab/postmarket-review/rebuild")
def rebuild_theme_lab_postmarket_review(body: dict[str, Any], request: Request) -> dict[str, Any]:
    _verify_operator_action_token(request)
    payload = body if isinstance(body, dict) else {}
    action_id = str(payload.get("action_id") or new_message_id("act"))
    requested_at = datetime.now(KST).isoformat(timespec="seconds")
    db = open_database()
    try:
        meta = _operator_action_catalog_item("REBUILD_POSTMARKET_REVIEW", OPERATOR_ACTION_CATALOG["REBUILD_POSTMARKET_REVIEW"])
        action = db.save_operator_action(
            _operator_action_record(
                action_id=action_id,
                action_type="REBUILD_POSTMARKET_REVIEW",
                status="RUNNING",
                requested_at=requested_at,
                payload={**payload, "confirm": True},
                event=None,
                meta=meta,
            )
        )
        try:
            response_payload = _rebuild_postmarket_review_payload(db, payload)
        except Exception as exc:
            failed = db.update_operator_action_status(action_id, "FAILED", error_message=str(exc)) or action
            _save_operator_action_result_event(db, failed, "FAILED", {"error": str(exc)})
            raise
        saved = db.update_operator_action_status(action_id, "SUCCESS", response=response_payload) or action
        _save_operator_action_result_event(db, saved, "SUCCESS", response_payload)
        return response_payload
    finally:
        close_database(db)


@app.get("/api/themelab/postmarket-review")
def list_theme_lab_postmarket_review(
    trade_date: str | None = Query(None),
    review_scope: str | None = Query(None),
    outcome_label: str | None = Query(None),
    event_type: str | None = Query(None),
    symbol: str | None = Query(None),
    primary_theme: str | None = Query(None),
    min_return_5m_pct: float | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    db = open_database()
    try:
        items = db.list_postmarket_review_items(
            resolved_trade_date,
            review_scope=review_scope,
            outcome_label=outcome_label,
            event_type=event_type,
            symbol=symbol,
            primary_theme=primary_theme,
            min_return_5m_pct=min_return_5m_pct,
            limit=limit + 1,
            offset=offset,
        )
        page, pagination = _trim_page(items, limit=limit, offset=offset)
        return {
            "trade_date": resolved_trade_date,
            "items": page,
            "pagination": pagination,
            "filters": {
                "trade_date": resolved_trade_date,
                "review_scope": review_scope or "",
                "outcome_label": str(outcome_label or "").upper(),
                "event_type": str(event_type or "").upper(),
                "symbol": symbol or "",
                "primary_theme": primary_theme or "",
                "min_return_5m_pct": min_return_5m_pct,
                "limit": limit,
                "offset": offset,
            },
        }
    finally:
        close_database(db)


@app.get("/api/themelab/postmarket-review/summary")
def theme_lab_postmarket_review_summary(trade_date: str | None = Query(None)) -> dict[str, Any]:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    db = open_database()
    try:
        return db.summarize_postmarket_reviews(resolved_trade_date)
    finally:
        close_database(db)


@app.get("/api/themelab/postmarket-review/export", response_model=None)
def export_theme_lab_postmarket_review(
    trade_date: str | None = Query(None),
    format: str = Query("csv"),
) -> dict[str, Any] | PlainTextResponse:
    resolved_trade_date = _theme_lab_trade_date(trade_date)
    export_format = str(format or "csv").lower()
    db = open_database()
    try:
        items = db.list_postmarket_review_items(resolved_trade_date, limit=1000)
        summary = db.summarize_postmarket_reviews(resolved_trade_date)
    finally:
        close_database(db)
    if export_format == "json":
        return {"trade_date": resolved_trade_date, "summary": summary, "items": items}
    if export_format != "csv":
        raise HTTPException(status_code=400, detail="format must be csv or json")
    csv_body = _postmarket_review_csv(items)
    return PlainTextResponse(
        csv_body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="postmarket-review-{resolved_trade_date}.csv"'},
    )


def _rebuild_postmarket_review_payload(db: TradingDatabase, payload: dict[str, Any]) -> dict[str, Any]:
    trade_date = _theme_lab_trade_date(str(payload.get("trade_date") or "") or None)
    review_scope = str(payload.get("review_scope") or "postmarket").lower()
    if review_scope not in {"postmarket", "intraday"}:
        raise HTTPException(status_code=400, detail="review_scope must be postmarket or intraday")
    deleted_count = 0
    if bool(payload.get("force")):
        deleted_count = db.delete_postmarket_reviews_for_date(trade_date, review_scope=review_scope)
    report = db.rebuild_postmarket_reviews(trade_date, review_scope=review_scope)
    persisted_summary = db.summarize_postmarket_reviews(trade_date)
    analysis_summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    summary = {**analysis_summary, **persisted_summary}
    return {
        "trade_date": trade_date,
        "review_scope": review_scope,
        "generated_at": report.get("generated_at") or "",
        "generated_count": int(report.get("generated_count") or len(report.get("items") or [])),
        "inserted_count": int(report.get("inserted_count") or 0),
        "duplicate_count": int(report.get("duplicate_count") or 0),
        "rejected_count": int(report.get("rejected_count") or 0),
        "deleted_count": deleted_count,
        "data_insufficient_count": int(summary.get("data_insufficient_count") or 0),
        "summary": summary,
    }


def _postmarket_review_csv(items: list[dict[str, Any]]) -> str:
    fields = [
        "review_id",
        "trade_date",
        "generated_at",
        "review_scope",
        "symbol",
        "stock_name",
        "primary_theme",
        "stock_role",
        "candidate_instance_id",
        "event_id",
        "event_type",
        "source_status",
        "block_reason",
        "base_time",
        "base_price",
        "price_1m",
        "price_3m",
        "price_5m",
        "price_10m",
        "price_close_or_last",
        "return_1m_pct",
        "return_3m_pct",
        "return_5m_pct",
        "return_10m_pct",
        "return_close_or_last_pct",
        "outcome_label",
        "confidence",
        "confidence_reason",
        "recommendation_ko",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow({field: item.get(field) if item.get(field) is not None else "" for field in fields})
    return output.getvalue()


def _operator_action_catalog_item(action_type: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "label_ko": str(meta.get("label_ko") or action_type),
        "risk_level": str(meta.get("risk_level") or "LOW"),
        "requires_token": bool(meta.get("requires_token")),
        "confirmation_required": bool(meta.get("confirmation_required")),
        "enabled": True,
        "endpoint": str(meta.get("endpoint") or ""),
    }


def _disabled_operator_action_item(action_type: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "label_ko": str(meta.get("label_ko") or action_type),
        "risk_level": "BLOCKED",
        "requires_token": False,
        "confirmation_required": False,
        "enabled": False,
        "reason_ko": str(meta.get("reason_ko") or "이번 PR 범위에서 금지된 액션입니다."),
    }


def _build_operator_action_recommendations(
    db: TradingDatabase,
    *,
    trade_date: str,
    event_id: str = "",
    symbol: str = "",
    candidate_instance_id: str = "",
) -> dict[str, Any]:
    event = db.get_operator_event(event_id) if event_id else None
    context = _operator_action_context(event=event, symbol=symbol, candidate_instance_id=candidate_instance_id)
    event_type = str((event or {}).get("event_type") or "")
    recommendations: list[dict[str, Any]] = []
    disabled: list[dict[str, Any]] = []

    def add(action_type: str, reason_ko: str) -> None:
        recommendations.append(_operator_action_recommendation(action_type, reason_ko))

    def block(action_type: str) -> None:
        if action_type in FORBIDDEN_OPERATOR_ACTIONS:
            disabled.append(_disabled_operator_action_item(action_type, FORBIDDEN_OPERATOR_ACTIONS[action_type]))

    if event_type == "GATEWAY_DISCONNECTED":
        add("CHECK_GATEWAY_STATUS", "Gateway 연결 끊김 이벤트가 발생했습니다.")
        add("START_KIWOOM_GATEWAY", "Gateway 프로세스 또는 heartbeat 회복이 필요할 수 있습니다.")
        add("OPEN_RUNBOOK", "Gateway 복구 순서를 확인합니다.")
    elif event_type == "SNAPSHOT_STALE":
        add("REFRESH_SNAPSHOT", "스냅샷 지연 상태를 다시 확인합니다.")
        add("RUNTIME_CYCLE_ONCE", "Runtime 평가를 1회 트리거해 최신 결과 생성을 시도합니다.")
        add("CHECK_RUNTIME_READINESS", "Runtime 준비 상태와 차단 요인을 확인합니다.")
    elif event_type == "DATA_QUALITY_DEGRADED":
        add("REFRESH_SNAPSHOT", "데이터 품질 상태를 다시 조회합니다.")
        add("RUNTIME_CYCLE_ONCE", "Runtime 재평가로 보조 데이터 회복 여부를 확인합니다.")
        add("OPEN_RUNBOOK", "데이터 품질 저하 대응 절차를 확인합니다.")
    elif event_type == "READY_BUT_LIVE_BLOCKED":
        add("OPEN_RUNBOOK", "LIVE Guard 차단 사유 확인 절차를 봅니다.")
        add("ADD_OPERATOR_NOTE", "차단 사유와 수동 판단을 감사 로그에 남깁니다.")
        add("ACK_EVENT", "확인한 이벤트를 ACK 처리합니다.")
        block("OVERRIDE_LIVE_GUARD")
        block("LIVE_BUY")
    elif event_type == "ORDER_INTENT_CREATED":
        add("OPEN_DRY_RUN_ORDER_DETAIL", "생성된 DRY_RUN 주문 의도 상세를 확인합니다.")
        add("ADD_OPERATOR_NOTE", "주문 의도 검토 내용을 남깁니다.")
    elif event_type == "MARKET_WAIT_STARTED":
        add("OPEN_RUNBOOK", "시장 대기 상태 대응 절차를 확인합니다.")
        add("SNOOZE_EVENT", "재확인 전까지 이벤트를 잠시 보류합니다.")
        add("ADD_OPERATOR_NOTE", "시장 대기 판단 근거를 남깁니다.")
    elif event_type in {"CHASE_RISK_BLOCKED", "LATE_CHASE_TEMP_WAIT"}:
        add("SNOOZE_EVENT", "late chase 재확인 시점까지 알림을 보류합니다.")
        add("ADD_OPERATOR_NOTE", "추격 차단 판단 근거를 남깁니다.")
        add("OPEN_RUNBOOK", "추격 리스크 대응 절차를 확인합니다.")

    candidate = _find_operator_action_candidate(db, context)
    if candidate:
        context.update(
            {
                "symbol": candidate.get("symbol") or context.get("symbol") or "",
                "candidate_instance_id": candidate.get("candidate_instance_id") or context.get("candidate_instance_id") or "",
                "gate_status": candidate.get("gate_status") or "",
                "display_status": candidate.get("display_status") or "",
                "stock_name": candidate.get("stock_name") or candidate.get("name") or context.get("stock_name") or "",
            }
        )
        if str(candidate.get("gate_status") or "") in {"READY", "READY_SMALL"}:
            add("RUNTIME_CYCLE_ONCE", "선택 후보가 READY 계열이므로 최신 평가를 1회 확인합니다.")
            add("ADD_OPERATOR_NOTE", "READY 판단을 운영 메모로 남깁니다.")
            add("OPEN_RUNBOOK", "READY 후보 점검 절차를 확인합니다.")
            block("LIVE_BUY")

    if not recommendations:
        add("REFRESH_SNAPSHOT", "현재 컨텍스트의 최신 상태를 확인합니다.")
        add("OPEN_RUNBOOK", "일반 운영 점검 절차를 확인합니다.")

    deduped = list({item["action_type"]: item for item in recommendations}.values())
    disabled_deduped = list({item["action_type"]: item for item in disabled}.values())
    return {
        "trade_date": trade_date,
        "context": context,
        "recommendations": deduped,
        "disabled_actions": disabled_deduped,
        "runbook": _runbook_payload(event_type or str(context.get("display_status") or context.get("gate_status") or "")),
    }


def _operator_action_recommendation(action_type: str, reason_ko: str) -> dict[str, Any]:
    meta = _operator_action_catalog_item(action_type, OPERATOR_ACTION_CATALOG[action_type])
    return {**meta, "reason_ko": reason_ko}


def _operator_action_context(*, event: Optional[dict], symbol: str, candidate_instance_id: str) -> dict[str, Any]:
    event = event or {}
    payload = dict(event.get("payload") or {})
    return {
        "event_id": event.get("event_id") or "",
        "event_type": event.get("event_type") or "",
        "symbol": symbol or event.get("symbol") or payload.get("symbol") or "",
        "stock_name": event.get("stock_name") or payload.get("stock_name") or "",
        "candidate_instance_id": candidate_instance_id or event.get("candidate_instance_id") or payload.get("candidate_instance_id") or "",
    }


def _find_operator_action_candidate(db: TradingDatabase, context: dict[str, Any]) -> Optional[dict[str, Any]]:
    symbol = str(context.get("symbol") or "")
    candidate_instance_id = str(context.get("candidate_instance_id") or "")
    if not symbol and not candidate_instance_id:
        return None
    snapshot = build_theme_lab_dashboard_snapshot(db, runtime_status=runtime_supervisor.status(), gateway_state=gateway_state)
    for item in snapshot.get("watchset") or []:
        if symbol and str(item.get("symbol") or "") == symbol:
            return item
        if candidate_instance_id and str(item.get("candidate_instance_id") or "") == candidate_instance_id:
            return item
    return None


def _operator_action_record(
    *,
    action_id: str,
    action_type: str,
    status: str,
    requested_at: str,
    payload: dict[str, Any],
    event: Optional[dict],
    meta: dict[str, Any],
    error_message: str = "",
) -> dict[str, Any]:
    event = event or {}
    request_payload = dict(payload or {})
    return {
        "action_id": action_id,
        "trade_date": str(
            payload.get("trade_date")
            or event.get("trade_date")
            or _theme_lab_trade_date(None, occurred_at=requested_at)
        ),
        "requested_at": requested_at,
        "action_type": action_type,
        "status": status,
        "requested_by": str(payload.get("requested_by") or "operator"),
        "event_id": str(payload.get("event_id") or event.get("event_id") or ""),
        "symbol": str(payload.get("symbol") or event.get("symbol") or ""),
        "stock_name": str(payload.get("stock_name") or event.get("stock_name") or ""),
        "candidate_instance_id": str(payload.get("candidate_instance_id") or event.get("candidate_instance_id") or ""),
        "requires_token": bool(meta.get("requires_token")),
        "confirmation_required": bool(meta.get("confirmation_required")),
        "endpoint": str(meta.get("endpoint") or ""),
        "request_payload": request_payload,
        "response_payload": {},
        "error_message": error_message,
    }


def _verify_operator_action_token(request: Request) -> None:
    verify_gateway_token(
        request,
        authorization=request.headers.get("authorization"),
        x_local_token=request.headers.get("x-local-token"),
    )


async def _execute_operator_action(
    action_type: str,
    payload: dict[str, Any],
    *,
    db: TradingDatabase,
    event: Optional[dict],
) -> dict[str, Any]:
    if action_type == "REFRESH_SNAPSHOT":
        snapshot = build_theme_lab_dashboard_snapshot(db, runtime_status=runtime_supervisor.status(), gateway_state=gateway_state)
        return {"refreshed": True, "summary": snapshot.get("summary", {})}
    if action_type == "CHECK_GATEWAY_STATUS":
        return gateway_status()
    if action_type == "START_KIWOOM_GATEWAY":
        return _start_kiwoom_gateway_response()
    if action_type == "CHECK_RUNTIME_READINESS":
        return await runtime_supervisor.readiness()
    if action_type == "RUNTIME_CYCLE_ONCE":
        return await runtime_supervisor.run_once(reason="operator_action_center")
    if action_type == "RUNTIME_START":
        return await runtime_supervisor.start()
    if action_type == "RUNTIME_STOP":
        return await runtime_supervisor.stop()
    if action_type == "RUNTIME_RESTART":
        return await runtime_supervisor.restart()
    if action_type == "OPEN_DRY_RUN_ORDER_DETAIL":
        intent_id = _operator_action_intent_id(payload, event)
        return _order_service().get_dry_run_order(intent_id) if intent_id else {"found": False, "reason": "INTENT_ID_MISSING"}
    if action_type == "REBUILD_DRY_RUN_PERFORMANCE":
        trade_date = str(payload.get("trade_date") or "") or None
        report = _performance_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        persisted = _performance_analyzer(db).persist_report(report)
        return {"report_id": report.get("report_id"), "persisted": persisted is not None, "summary": report.get("summary", {})}
    if action_type == "REBUILD_POSTMARKET_REVIEW":
        return _rebuild_postmarket_review_payload(db, payload)
    if action_type == "REBUILD_TRANSPORT_LATENCY_REPORT":
        trade_date = str(payload.get("trade_date") or "") or None
        report = _transport_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        saved = db.save_gateway_transport_latency_report(report)
        return {"report_id": report.get("report_id"), "saved": saved, "summary": report.get("summary", {})}
    if action_type == "EXPORT_TRANSPORT_LATENCY_REPORT":
        trade_date = str(payload.get("trade_date") or "") or None
        report = _transport_analyzer(db).build_report(trade_date=trade_date, limit=10000)
        return {"report_id": report.get("report_id"), "export_paths": _transport_analyzer(db).export_report(report)}
    if action_type == "ACK_EVENT":
        event_id = str(payload.get("event_id") or (event or {}).get("event_id") or "")
        return {"updated_count": db.acknowledge_operator_event(event_id, acknowledged_by=str(payload.get("requested_by") or "operator"))}
    if action_type == "HIDE_EVENT":
        event_id = str(payload.get("event_id") or (event or {}).get("event_id") or "")
        return {"updated_count": db.hide_operator_event(event_id)}
    if action_type == "SNOOZE_EVENT":
        event_id = str(payload.get("event_id") or (event or {}).get("event_id") or "")
        minutes = max(1, min(240, int(payload.get("snooze_minutes") or 15)))
        snoozed_until = (datetime.now(KST) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return {"updated_count": db.snooze_operator_event(event_id, snoozed_until), "snoozed_until": snoozed_until}
    if action_type == "ADD_OPERATOR_NOTE":
        return {"noted": True, "note": str(payload.get("note") or ""), "event_id": str(payload.get("event_id") or "")}
    if action_type == "OPEN_RUNBOOK":
        key = str((event or {}).get("event_type") or payload.get("status") or payload.get("display_status") or "")
        return {"opened": True, "runbook": _runbook_payload(key)}
    raise ValueError(f"unsupported action: {action_type}")


def _operator_action_intent_id(payload: dict[str, Any], event: Optional[dict]) -> str:
    event = event or {}
    event_payload = dict(event.get("payload") or {})
    for source in (payload, event_payload, event):
        for key in ("intent_id", "order_intent_id", "runtime_order_intent_id", "candidate_instance_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _save_operator_action_result_event(db: TradingDatabase, action: dict[str, Any], status: str, response_payload: Optional[dict] = None) -> None:
    action_id = str(action.get("action_id") or "")
    if not action_id:
        return
    event_type = "ACTION_EXECUTED" if status == "SUCCESS" else "ACTION_BLOCKED" if status == "BLOCKED" else "ACTION_FAILED"
    severity = "INFO" if status == "SUCCESS" else "WARNING"
    message = {
        "SUCCESS": "운영 액션 실행 완료",
        "BLOCKED": "운영 액션 차단",
        "FAILED": "운영 액션 실패",
    }.get(status, "운영 액션 상태 변경")
    try:
        db.save_operator_event(
            {
                "event_id": f"operator-action:{action_id}:{status}",
                "trade_date": action.get("trade_date") or _theme_lab_trade_date(None),
                "occurred_at": datetime.now(KST).isoformat(timespec="seconds"),
                "source": "themelab_dashboard",
                "event_type": event_type,
                "severity": severity,
                "category": "action",
                "symbol": action.get("symbol") or "",
                "stock_name": action.get("stock_name") or "",
                "candidate_instance_id": action.get("candidate_instance_id") or "",
                "message_ko": f"{message}: {action.get('action_type')}",
                "payload": {"action": action, "response": response_payload or {}},
            }
        )
    except Exception:
        return


def _runbook_payload(key: str) -> dict[str, Any]:
    normalized = str(key or "").upper()
    runbooks = {
        "GATEWAY_DISCONNECTED": (
            "Gateway 연결 복구",
            [
                "Gateway 상태에서 connected, heartbeat_ok, kiwoom_logged_in, orderable을 확인합니다.",
                "이미 실행 중인 32bit Gateway 프로세스가 있는지 확인합니다.",
                "START_KIWOOM_GATEWAY 실행 후 30초 내 heartbeat 회복 여부를 봅니다.",
                "회복되지 않으면 HTS 로그인 상태와 Gateway 로그를 수동 확인합니다.",
            ],
        ),
        "SNAPSHOT_STALE": (
            "스냅샷 지연 복구",
            [
                "Runtime 준비 상태를 확인합니다.",
                "Runtime 1회 평가를 실행합니다.",
                "snapshot_age가 줄어드는지 확인합니다.",
                "반복 stale이면 Runtime restart 필요 여부를 판단합니다.",
            ],
        ),
        "READY_BUT_LIVE_BLOCKED": (
            "LIVE Guard 차단 점검",
            [
                "LIVE Guard 차단 사유와 candidate_instance_id를 확인합니다.",
                "주문 실행이나 Guard 우회는 하지 않습니다.",
                "차단 사유를 운영 메모로 남깁니다.",
                "장 마감 후 guard 정책 개선 대상으로 검토합니다.",
            ],
        ),
        "DATA_QUALITY_DEGRADED": (
            "데이터 품질 저하 점검",
            [
                "latest tick, VWAP, support 데이터 준비 상태를 확인합니다.",
                "Runtime 1회 평가 또는 스냅샷 새로고침으로 회복 여부를 확인합니다.",
                "회복 후 READY 전환이 정상인지 확인합니다.",
            ],
        ),
        "CHASE_RISK_BLOCKED": (
            "추격 리스크 차단 점검",
            [
                "late_chase_level과 점수, 재확인 시간을 확인합니다.",
                "강제 매수하지 않습니다.",
                "재확인 시점까지 알림을 보류하고, 장 마감 후 late chase 차단 효율을 리뷰합니다.",
            ],
        ),
        "LATE_CHASE_TEMP_WAIT": (
            "추격 대기 점검",
            [
                "재확인까지 남은 시간을 확인합니다.",
                "즉시 주문하지 않고 다음 Runtime 평가를 기다립니다.",
                "필요하면 운영 메모를 남깁니다.",
            ],
        ),
        "MARKET_WAIT_STARTED": (
            "시장 대기 점검",
            [
                "후보의 candidate_market과 시장 breadth를 확인합니다.",
                "시장 확인 또는 회복 조건을 기다립니다.",
                "재확인 시점까지 알림을 보류할 수 있습니다.",
            ],
        ),
    }
    title, steps = runbooks.get(
        normalized,
        (
            "일반 운영 점검",
            [
                "스냅샷과 Runtime 준비 상태를 확인합니다.",
                "필요하면 이벤트를 ACK 처리하고 운영 메모를 남깁니다.",
                "LIVE 주문, 취소, Guard 우회는 이번 Action Center에서 수행하지 않습니다.",
            ],
        ),
    )
    return {"key": normalized or "GENERAL", "title_ko": title, "steps_ko": steps}


def _validate_theme_lab_operator_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    event_type = str(event.get("event_type") or event.get("type") or "").strip()
    severity = str(event.get("severity") or "").strip().upper()
    category = str(event.get("category") or "").strip().lower()
    event_id = str(event.get("event_id") or event.get("id") or "").strip()
    occurred_at = str(event.get("occurred_at") or event.get("created_at") or datetime.now(KST).isoformat(timespec="seconds")).strip()
    message = str(event.get("message_ko") or event.get("message") or "").strip()
    if event_type not in THEMELAB_OPERATOR_EVENT_TYPES:
        raise ValueError("invalid event_type")
    if severity not in THEMELAB_OPERATOR_EVENT_SEVERITIES:
        raise ValueError("invalid severity")
    if category not in THEMELAB_OPERATOR_EVENT_CATEGORIES:
        raise ValueError("invalid category")
    if not event_id or not message:
        raise ValueError("missing required fields")
    payload = dict(event.get("payload") or event)
    return {
        "event_id": event_id,
        "trade_date": str(event.get("trade_date") or _theme_lab_trade_date(None, occurred_at=occurred_at)),
        "occurred_at": occurred_at,
        "received_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": str(event.get("source") or "themelab_dashboard"),
        "event_type": event_type,
        "severity": severity,
        "category": category,
        "symbol": str(event.get("symbol") or ""),
        "stock_name": str(event.get("stock_name") or ""),
        "primary_theme": str(event.get("primary_theme") or ""),
        "stock_role": str(event.get("stock_role") or ""),
        "candidate_instance_id": str(event.get("candidate_instance_id") or ""),
        "from_status": str(event.get("from_status") or ""),
        "to_status": str(event.get("to_status") or ""),
        "gate_status": str(event.get("gate_status") or ""),
        "display_status": str(event.get("display_status") or ""),
        "message_ko": message,
        "payload": payload,
        "acknowledged_at": str(event.get("acknowledged_at") or ""),
        "acknowledged_by": str(event.get("acknowledged_by") or ""),
        "hidden": bool(event.get("hidden")),
        "snoozed_until": str(event.get("snoozed_until") or ""),
    }


def _theme_lab_trade_date(trade_date: str | None, *, occurred_at: str | None = None) -> str:
    explicit = str(trade_date or "").strip()
    if explicit:
        return explicit[:10]
    timestamp = str(occurred_at or "").strip()
    if len(timestamp) >= 10 and timestamp[4] == "-" and timestamp[7] == "-":
        return timestamp[:10]
    return datetime.now(KST).date().isoformat()


def _csv_values(value: Optional[str]) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _record_change_proposal_action(
    proposal_id: str,
    action: str,
    next_status: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    db = open_database()
    try:
        proposal = db.get_strategy_change_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="proposal not found")
        previous_status = str(proposal.get("status") or "")
        approval = db.save_strategy_change_approval(
            {
                "proposal_id": proposal_id,
                "action": action,
                "previous_status": previous_status,
                "next_status": next_status,
                "operator": str((body or {}).get("operator") or ""),
                "note": str((body or {}).get("note") or ""),
                "details": {
                    "auto_apply": False,
                    "config_changed": False,
                    "message_ko": "승인 상태만 저장했습니다. 실제 runtime config는 변경하지 않았습니다.",
                },
            }
        )
        updated = db.get_strategy_change_proposal(proposal_id)
        return {
            "proposal_id": proposal_id,
            "action": action,
            "approval": approval,
            "proposal": updated,
            "config_changed": False,
            "auto_apply": False,
            "disclaimer_ko": "이번 PR은 자동 적용을 하지 않습니다. 승인 상태만 저장합니다.",
        }
    finally:
        close_database(db)


def _proposal_related_reports(proposal: dict[str, Any]) -> dict[str, Any]:
    source_ids = [str(item) for item in proposal.get("source_ids") or [] if str(item)]
    return {
        "source_type": proposal.get("source_type") or "",
        "source_ids": source_ids,
        "replay_report_ids": [item for item in source_ids if item.startswith("replay_report")],
        "threshold_ab_report_ids": [item for item in source_ids if item.startswith("threshold_ab")],
        "shadow_policy_ids": [item.rsplit(":", 1)[-1] for item in source_ids if item.startswith("shadow_strategy:")],
    }


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
    processed = await asyncio.to_thread(_process_gateway_event_persist, event)
    event = GatewayEvent.from_dict(processed["event"])
    result = dict(processed["result"])
    if bool(processed.get("accepted")):
        runtime_started = time.perf_counter()
        await runtime_supervisor.handle_gateway_event(event)
        runtime_forward_ms = (time.perf_counter() - runtime_started) * 1000.0
        result.setdefault("transport", {})["runtime_forward_ms"] = runtime_forward_ms
    await _schedule_dashboard_snapshot_broadcast()
    return result


def _process_gateway_event_persist(event: GatewayEvent) -> dict[str, Any]:
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
            _queue_replay_tick_history(event)
            _save_gateway_event_transport_sample(
                db,
                event,
                accepted=True,
                core_receive_ms=wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
                core_persist_ms=persist_ms,
            )
        finally:
            close_database(db)
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
    return {
        "event": event.to_dict(),
        "accepted": accepted,
        "result": {
            "accepted": accepted,
            "event_id": event.event_id,
            "type": event.type,
            "transport": {
                "core_receive_ms": wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
                "core_persist_ms": persist_ms,
                "runtime_forward_ms": 0.0,
            },
        },
    }


async def _start_gateway_condition_event_worker() -> None:
    if not _gateway_condition_event_async_enabled():
        _update_gateway_condition_event_worker_state({"enabled": False})
        return
    _ensure_gateway_condition_event_worker()


async def _stop_gateway_condition_event_worker() -> None:
    global _gateway_condition_event_queue, _gateway_condition_event_queues
    global _gateway_condition_event_worker_task, _gateway_condition_event_worker_tasks, _gateway_condition_event_executor
    global _gateway_condition_event_executor_worker_count
    tasks = [task for task in _gateway_condition_event_worker_tasks if task is not None and not task.done()]
    if tasks:
        try:
            queues = list(_gateway_condition_event_queues)
            if queues:
                await asyncio.wait_for(asyncio.gather(*(queue.join() for queue in queues)), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    _gateway_condition_event_worker_task = None
    _gateway_condition_event_worker_tasks = []
    _gateway_condition_event_queue = None
    _gateway_condition_event_queues = []
    if _gateway_condition_event_executor is not None:
        _gateway_condition_event_executor.shutdown(wait=False, cancel_futures=True)
        _gateway_condition_event_executor = None
        _gateway_condition_event_executor_worker_count = 0
    _update_gateway_condition_event_worker_state(
        {
            "enabled": False,
            "queue_size": 0,
            "queue_batch_count": 0,
            "worker_count": 0,
            "active_worker_count": 0,
            "active_count": 0,
        }
    )


async def _start_core_ws_event_worker() -> None:
    if not _core_ws_event_async_enabled():
        _update_gateway_core_ws_event_worker_state({"enabled": False})
        return
    _ensure_core_ws_event_worker()


async def _stop_core_ws_event_worker() -> None:
    global _core_ws_event_worker_task
    global _core_ws_event_control_worker_task
    global _core_ws_event_data_worker_task
    global _core_ws_event_control_worker_tasks
    global _core_ws_event_queue
    global _core_ws_event_control_queue
    global _core_ws_event_data_queue
    global _core_ws_event_control_queues
    task_candidates = [
        _core_ws_event_worker_task,
        _core_ws_event_control_worker_task,
        _core_ws_event_data_worker_task,
        *_core_ws_event_control_worker_tasks,
    ]
    tasks = []
    seen_task_ids: set[int] = set()
    for task in task_candidates:
        if task is not None and not task.done() and id(task) not in seen_task_ids:
            tasks.append(task)
            seen_task_ids.add(id(task))
    queue_candidates = [
        _core_ws_event_queue,
        _core_ws_event_control_queue,
        _core_ws_event_data_queue,
        *_core_ws_event_control_queues,
    ]
    queues = []
    seen_queue_ids: set[int] = set()
    for queue in queue_candidates:
        if queue is not None and id(queue) not in seen_queue_ids:
            queues.append(queue)
            seen_queue_ids.add(id(queue))
    if tasks:
        try:
            if queues:
                await asyncio.wait_for(asyncio.gather(*(queue.join() for queue in queues)), timeout=_core_ws_event_drain_timeout_sec())
        except asyncio.TimeoutError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    _core_ws_event_worker_task = None
    _core_ws_event_control_worker_task = None
    _core_ws_event_data_worker_task = None
    _core_ws_event_control_worker_tasks = []
    _core_ws_event_queue = None
    _core_ws_event_control_queue = None
    _core_ws_event_data_queue = None
    _core_ws_event_control_queues = []
    with _core_ws_price_tick_coalesce_lock:
        _core_ws_price_tick_latest_by_key.clear()
    _update_gateway_core_ws_event_worker_state(
        {
            "enabled": False,
            "queue_size": 0,
            "control_worker_count": 0,
            "control_queue_size": 0,
            "control_queue_sizes": [],
            "data_queue_size": 0,
            "active_count": 0,
            "control_active_count": 0,
            "data_active_count": 0,
            "price_tick_pending_key_count": 0,
        }
    )


def _gateway_condition_event_async_enabled() -> bool:
    return os.environ.get("TRADING_CORE_WS_CONDITION_EVENT_ASYNC_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _gateway_condition_event_worker_count() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_CONDITION_EVENT_WORKERS", "4"))
    except ValueError:
        value = 4
    return max(1, min(8, value))


def _gateway_condition_event_stale_include_skip_ms() -> float:
    try:
        value = float(os.environ.get("TRADING_CORE_WS_CONDITION_EVENT_STALE_INCLUDE_SKIP_MS", "15000"))
    except ValueError:
        value = 15000.0
    return max(0.0, value)


def _gateway_condition_event_batch_chunk_size() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_CONDITION_EVENT_BATCH_CHUNK_SIZE", "64"))
    except ValueError:
        value = 64
    return max(1, min(500, value))


def _core_ws_event_async_enabled() -> bool:
    return os.environ.get("TRADING_CORE_WS_EVENT_ASYNC_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _core_ws_event_priority_enabled() -> bool:
    return os.environ.get("TRADING_CORE_WS_EVENT_PRIORITY_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _core_ws_event_worker_split_enabled() -> bool:
    return os.environ.get("TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _core_ws_event_control_worker_count() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_EVENT_CONTROL_WORKERS", "2"))
    except ValueError:
        value = 2
    return max(1, min(8, value))


def _core_ws_price_tick_coalesce_enabled() -> bool:
    return os.environ.get("TRADING_CORE_WS_PRICE_TICK_COALESCE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _gateway_condition_event_queue_max_size() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_CONDITION_EVENT_QUEUE_SIZE", "5000"))
    except ValueError:
        value = 5000
    return max(1, value)


def _core_ws_event_queue_max_size() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_EVENT_QUEUE_SIZE", "10000"))
    except ValueError:
        value = 10000
    return max(1, value)


def _core_ws_event_drain_timeout_sec() -> float:
    try:
        value = float(os.environ.get("TRADING_CORE_WS_EVENT_DRAIN_TIMEOUT_SEC", "2.0"))
    except ValueError:
        value = 2.0
    return max(0.1, value)


def _ensure_gateway_condition_event_worker() -> None:
    global _gateway_condition_event_queue
    global _gateway_condition_event_worker_task
    global _gateway_condition_event_queues
    global _gateway_condition_event_worker_tasks
    global _gateway_condition_event_worker_loop
    global _gateway_condition_event_executor
    global _gateway_condition_event_executor_worker_count
    if not _gateway_condition_event_async_enabled():
        _update_gateway_condition_event_worker_state({"enabled": False})
        return
    loop = asyncio.get_running_loop()
    worker_count = _gateway_condition_event_worker_count()
    queue_max_size = _gateway_condition_event_queue_max_size()
    if _gateway_condition_event_worker_loop is not loop or len(_gateway_condition_event_queues) != worker_count:
        _gateway_condition_event_queues = [asyncio.Queue(maxsize=queue_max_size) for _ in range(worker_count)]
        _gateway_condition_event_queue = _gateway_condition_event_queues[0] if _gateway_condition_event_queues else None
        _gateway_condition_event_worker_task = None
        _gateway_condition_event_worker_tasks = []
        _gateway_condition_event_worker_loop = loop
        _update_gateway_condition_event_worker_state(
            {
                "queue_size": 0,
                "queue_sizes_by_worker": [0 for _ in range(worker_count)],
                "queue_batch_count": 0,
                "queue_batch_counts_by_worker": [0 for _ in range(worker_count)],
                "worker_count": worker_count,
                "active_worker_count": 0,
                "active_count": 0,
            }
        )
    if _gateway_condition_event_executor is None or _gateway_condition_event_executor_worker_count != worker_count:
        if _gateway_condition_event_executor is not None:
            _gateway_condition_event_executor.shutdown(wait=False, cancel_futures=True)
        _gateway_condition_event_executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="core-condition-events")
        _gateway_condition_event_executor_worker_count = worker_count
    active_tasks = [task for task in _gateway_condition_event_worker_tasks if task is not None and not task.done()]
    if len(active_tasks) != worker_count:
        for task in active_tasks:
            task.cancel()
        _gateway_condition_event_worker_tasks = [
            loop.create_task(_gateway_condition_event_worker_loop_main(worker_index)) for worker_index in range(worker_count)
        ]
        _gateway_condition_event_worker_task = _gateway_condition_event_worker_tasks[0] if _gateway_condition_event_worker_tasks else None
    _update_gateway_condition_event_worker_state(
        {
            "enabled": True,
            "queue_size": int(gateway_condition_event_worker_state.get("queue_size") or 0),
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
            "queue_max_size": queue_max_size,
            "worker_count": worker_count,
            "stale_include_skip_ms": _gateway_condition_event_stale_include_skip_ms(),
            "batch_chunk_size": _gateway_condition_event_batch_chunk_size(),
        }
    )


def _ensure_core_ws_event_worker() -> None:
    global _core_ws_event_queue
    global _core_ws_event_worker_task
    global _core_ws_event_control_queue
    global _core_ws_event_data_queue
    global _core_ws_event_control_queues
    global _core_ws_event_control_worker_task
    global _core_ws_event_data_worker_task
    global _core_ws_event_control_worker_tasks
    global _core_ws_event_worker_loop
    if not _core_ws_event_async_enabled():
        _update_gateway_core_ws_event_worker_state({"enabled": False})
        return
    loop = asyncio.get_running_loop()
    split_enabled = _core_ws_event_worker_split_enabled()
    control_worker_count = _core_ws_event_control_worker_count() if split_enabled else 0
    queue_max_size = _core_ws_event_queue_max_size()
    if (
        _core_ws_event_worker_loop is not loop
        or (split_enabled and len(_core_ws_event_control_queues) != control_worker_count)
    ):
        if split_enabled:
            _core_ws_event_queue = None
            _core_ws_event_control_queues = [
                asyncio.PriorityQueue(maxsize=queue_max_size) for _ in range(control_worker_count)
            ]
            _core_ws_event_control_queue = _core_ws_event_control_queues[0] if _core_ws_event_control_queues else None
            _core_ws_event_data_queue = asyncio.PriorityQueue(maxsize=queue_max_size)
        else:
            _core_ws_event_queue = asyncio.PriorityQueue(maxsize=queue_max_size)
            _core_ws_event_control_queue = None
            _core_ws_event_control_queues = []
            _core_ws_event_data_queue = None
        _core_ws_event_worker_task = None
        _core_ws_event_control_worker_task = None
        _core_ws_event_data_worker_task = None
        _core_ws_event_control_worker_tasks = []
        _core_ws_event_worker_loop = loop
        with _core_ws_price_tick_coalesce_lock:
            _core_ws_price_tick_latest_by_key.clear()
        _update_gateway_core_ws_event_worker_state(
            {
                "queue_size": 0,
                "control_worker_count": control_worker_count,
                "control_queue_size": 0,
                "control_queue_sizes": [0 for _ in range(control_worker_count)],
                "data_queue_size": 0,
                "active_count": 0,
                "control_active_count": 0,
                "data_active_count": 0,
                "split_enabled": split_enabled,
                "priority_enabled": _core_ws_event_priority_enabled(),
                "price_tick_pending_key_count": 0,
            }
        )
    if split_enabled:
        if len(_core_ws_event_control_queues) != control_worker_count:
            _core_ws_event_control_queues = [
                asyncio.PriorityQueue(maxsize=queue_max_size) for _ in range(control_worker_count)
            ]
            _core_ws_event_control_worker_tasks = []
        if _core_ws_event_control_queue is None:
            _core_ws_event_control_queue = _core_ws_event_control_queues[0] if _core_ws_event_control_queues else None
        if _core_ws_event_data_queue is None:
            _core_ws_event_data_queue = asyncio.PriorityQueue(maxsize=queue_max_size)
        active_control_tasks = [
            task for task in _core_ws_event_control_worker_tasks if task is not None and not task.done()
        ]
        if len(active_control_tasks) != control_worker_count:
            for task in active_control_tasks:
                task.cancel()
            _core_ws_event_control_worker_tasks = [
                loop.create_task(_core_ws_event_worker_loop_main("control", worker_index))
                for worker_index in range(control_worker_count)
            ]
            _core_ws_event_control_worker_task = (
                _core_ws_event_control_worker_tasks[0] if _core_ws_event_control_worker_tasks else None
            )
        if _core_ws_event_data_worker_task is None or _core_ws_event_data_worker_task.done():
            _core_ws_event_data_worker_task = loop.create_task(_core_ws_event_worker_loop_main("data"))
        _core_ws_event_worker_task = _core_ws_event_control_worker_task
    else:
        if _core_ws_event_queue is None:
            _core_ws_event_queue = asyncio.PriorityQueue(maxsize=queue_max_size)
        if _core_ws_event_worker_task is None or _core_ws_event_worker_task.done():
            _core_ws_event_worker_task = loop.create_task(_core_ws_event_worker_loop_main("single"))
    _update_gateway_core_ws_event_worker_state(
        {
            "enabled": True,
            "queue_size": _core_ws_event_total_queue_size(),
            "queue_max_size": queue_max_size,
            "control_worker_count": control_worker_count,
            "control_queue_size": _core_ws_event_queue_size("control"),
            "control_queue_sizes": _core_ws_event_control_queue_sizes(),
            "data_queue_size": _core_ws_event_queue_size("data"),
            "split_enabled": split_enabled,
            "priority_enabled": _core_ws_event_priority_enabled(),
            "price_tick_coalesce_enabled": _core_ws_price_tick_coalesce_enabled(),
            "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
        }
    )


def _core_ws_price_tick_coalesce_key(event: GatewayEvent | None) -> str:
    if event is None or event.type != "price_tick":
        return ""
    payload = dict(event.payload or {})
    code = str(payload.get("code") or payload.get("stock_code") or "").strip()
    if not code:
        return ""
    instrument_type = str(payload.get("instrument_type") or "stock").strip() or "stock"
    return f"{instrument_type}:{code}"


def _gateway_condition_event_queue_batch_count() -> int:
    return sum(queue.qsize() for queue in _gateway_condition_event_queues)


def _gateway_condition_event_queue_batch_counts() -> list[int]:
    return [queue.qsize() for queue in _gateway_condition_event_queues]


def _gateway_condition_event_queue_sizes_by_worker() -> list[int]:
    worker_count = len(_gateway_condition_event_queues) or _gateway_condition_event_worker_count()
    sizes = list(gateway_condition_event_worker_state.get("queue_sizes_by_worker") or [])
    if len(sizes) < worker_count:
        sizes.extend([0 for _ in range(worker_count - len(sizes))])
    return [max(0, int(size or 0)) for size in sizes[:worker_count]]


def _condition_event_queue_sizes_with_delta(worker_index: int, delta: int) -> list[int]:
    sizes = _gateway_condition_event_queue_sizes_by_worker()
    if 0 <= worker_index < len(sizes):
        sizes[worker_index] = max(0, int(sizes[worker_index]) + int(delta))
    return sizes


def _chunk_condition_event_worker_batches(
    batches_by_worker: dict[int, list[GatewayEvent]],
    chunk_size: int | None = None,
) -> dict[int, list[list[GatewayEvent]]]:
    size = max(1, int(chunk_size or _gateway_condition_event_batch_chunk_size()))
    chunked: dict[int, list[list[GatewayEvent]]] = {}
    for worker_index, worker_events in batches_by_worker.items():
        chunks = [worker_events[index : index + size] for index in range(0, len(worker_events), size)]
        chunked[worker_index] = [chunk for chunk in chunks if chunk]
    return chunked


def _condition_event_shard_key(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    code = str(payload.get("code") or payload.get("stock_code") or "").strip()
    if code:
        return code
    return _condition_event_coalesce_key(event) or str(event.event_id or "")


def _gateway_condition_event_worker_index(event: GatewayEvent, worker_count: int | None = None) -> int:
    count = max(1, int(worker_count or len(_gateway_condition_event_queues) or _gateway_condition_event_worker_count()))
    key = _condition_event_shard_key(event)
    if not key:
        key = str(event.event_id or "")
    return zlib.crc32(key.encode("utf-8", errors="ignore")) % count


def _core_ws_price_tick_pending_key_count() -> int:
    with _core_ws_price_tick_coalesce_lock:
        return len(_core_ws_price_tick_latest_by_key)


_CORE_WS_CONTROL_MESSAGE_TYPES = {
    "command_ack",
    "command_started",
    "command_failed",
    "rate_limited",
    "heartbeat",
    "transport_heartbeat",
    "login_status",
    "order_result",
    "execution_event",
    "execution",
}


def _core_ws_event_priority(kind: str, event: GatewayEvent | None = None, message: GatewayWsMessage | None = None) -> int:
    if not _core_ws_event_priority_enabled():
        return 10
    message_type = _core_ws_event_work_message_type(kind, event=event, message=message)
    if message_type in _CORE_WS_CONTROL_MESSAGE_TYPES:
        return 0
    if message_type == "price_tick":
        return 50
    return 10


def _core_ws_event_queue_item(item: _CoreWsEventWorkItem) -> _CoreWsEventQueuedItem:
    return _CoreWsEventQueuedItem(
        priority=_core_ws_event_priority(item.kind, event=item.event, message=item.message),
        sequence=next(_core_ws_event_queue_sequence),
        work_item=item,
    )


def _core_ws_event_queue_name_for_priority(priority: int) -> str:
    if not _core_ws_event_worker_split_enabled():
        return "single"
    return "control" if int(priority) <= 0 else "data"


def _core_ws_event_queue_for_name(queue_name: str) -> asyncio.PriorityQueue[_CoreWsEventQueuedItem] | None:
    if queue_name.startswith("control:"):
        try:
            index = int(queue_name.split(":", 1)[1])
        except (IndexError, ValueError):
            index = 0
        if 0 <= index < len(_core_ws_event_control_queues):
            return _core_ws_event_control_queues[index]
        return _core_ws_event_control_queue
    if queue_name == "control":
        if _core_ws_event_control_queues:
            return _core_ws_event_control_queues[0]
        return _core_ws_event_control_queue
    if queue_name == "data":
        return _core_ws_event_data_queue
    return _core_ws_event_queue


def _core_ws_event_queue_size(queue_name: str) -> int:
    if queue_name == "control" and _core_ws_event_control_queues:
        return sum(queue.qsize() for queue in _core_ws_event_control_queues)
    queue = _core_ws_event_queue_for_name(queue_name)
    return queue.qsize() if queue is not None else 0


def _core_ws_event_control_queue_sizes() -> list[int]:
    if _core_ws_event_control_queues:
        return [queue.qsize() for queue in _core_ws_event_control_queues]
    return [_core_ws_event_control_queue.qsize()] if _core_ws_event_control_queue is not None else []


def _core_ws_event_total_queue_size() -> int:
    if _core_ws_event_worker_split_enabled():
        return _core_ws_event_queue_size("control") + _core_ws_event_queue_size("data")
    return _core_ws_event_queue_size("single")


def _core_ws_event_worker_state_patch(queue_name: str = "") -> dict[str, Any]:
    return {
        "queue_size": _core_ws_event_total_queue_size(),
        "control_queue_size": _core_ws_event_queue_size("control"),
        "control_queue_sizes": _core_ws_event_control_queue_sizes(),
        "data_queue_size": _core_ws_event_queue_size("data"),
        "queue_max_size": _core_ws_event_queue_max_size(),
        "split_enabled": _core_ws_event_worker_split_enabled(),
        "control_worker_count": len(_core_ws_event_control_queues)
        or (1 if _core_ws_event_control_queue is not None and _core_ws_event_worker_split_enabled() else 0),
        "priority_enabled": _core_ws_event_priority_enabled(),
        "last_worker_kind": queue_name or gateway_core_ws_event_worker_state.get("last_worker_kind") or "",
    }


def _core_ws_event_active_state_patch(worker_kind: str, *, active: bool, worker_index: int = 0) -> dict[str, int]:
    if worker_kind == "single":
        return {"active_count": 1 if active else 0, "control_active_count": 0, "data_active_count": 0}
    control_active = int(gateway_core_ws_event_worker_state.get("control_active_count") or 0)
    data_active = int(gateway_core_ws_event_worker_state.get("data_active_count") or 0)
    delta = 1 if active else -1
    if worker_kind == "control":
        control_active = max(0, control_active + delta)
    elif worker_kind == "data":
        data_active = max(0, data_active + delta)
    return {
        "active_count": control_active + data_active,
        "control_active_count": control_active,
        "data_active_count": data_active,
        "last_control_worker_index": worker_index if worker_kind == "control" else int(
            gateway_core_ws_event_worker_state.get("last_control_worker_index") or 0
        ),
    }


def _core_ws_event_control_shard_key(
    kind: str,
    *,
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
) -> str:
    payload = dict(event.payload if event is not None else (message.payload if message is not None else {}) or {})
    command_id = str(payload.get("command_id") or (event.command_id if event is not None else "") or (message.command_id if message is not None else "") or "").strip()
    if command_id:
        return f"command:{command_id}"
    order_id = str(
        payload.get("order_id")
        or payload.get("order_no")
        or payload.get("original_order_no")
        or payload.get("order_number")
        or ""
    ).strip()
    if order_id:
        return f"order:{order_id}"
    code = str(payload.get("code") or payload.get("stock_code") or payload.get("symbol") or "").strip()
    message_type = _core_ws_event_work_message_type(kind, event=event, message=message)
    if message_type in {"execution_event", "execution", "order_result"} and code:
        return f"{message_type}:{code}"
    event_id = str((event.event_id if event is not None else "") or (message.event_id if message is not None else "") or "").strip()
    if event_id:
        return f"event:{event_id}"
    return f"type:{message_type}"


def _core_ws_event_control_worker_index(
    kind: str,
    *,
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
    worker_count: int | None = None,
) -> int:
    count = max(1, int(worker_count or len(_core_ws_event_control_queues) or _core_ws_event_control_worker_count()))
    key = _core_ws_event_control_shard_key(kind, event=event, message=message)
    return zlib.crc32(key.encode("utf-8", errors="ignore")) % count


def _core_ws_event_target_queue_name(
    kind: str,
    *,
    priority: int,
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
) -> str:
    queue_name = _core_ws_event_queue_name_for_priority(priority)
    if queue_name == "control" and _core_ws_event_worker_split_enabled():
        return f"control:{_core_ws_event_control_worker_index(kind, event=event, message=message)}"
    return queue_name


def _build_core_ws_event_work_item(
    *,
    kind: str,
    metadata: dict[str, Any],
    queue_size: int,
    queued_at: str,
    queued_monotonic: float,
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
    coalesce_key: str = "",
) -> _CoreWsEventWorkItem:
    queued_event = event
    if event is not None:
        trace_updates = {
            "core_ws_event_queued_at_utc": queued_at,
            "core_ws_event_queued_monotonic_ms": queued_monotonic,
            "core_ws_event_queue_size": queue_size,
        }
        if coalesce_key:
            trace_updates["core_ws_price_tick_coalesce_key"] = coalesce_key
        queued_event = _event_with_trace(event, trace_updates)
    return _CoreWsEventWorkItem(
        kind=kind,
        metadata=dict(metadata or {}),
        queued_at=queued_at,
        queued_monotonic_ms=queued_monotonic,
        event=queued_event,
        message=message,
        coalesce_key=coalesce_key,
    )


def _enqueue_core_ws_event_work(
    *,
    kind: str,
    metadata: dict[str, Any],
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
) -> dict[str, Any]:
    if not _core_ws_event_async_enabled():
        return {
            "accepted": False,
            "queued": False,
            "reason": "WORKER_DISABLED",
            "queue_size": 0,
            "queue_max_size": _core_ws_event_queue_max_size(),
        }
    _ensure_core_ws_event_worker()
    priority = _core_ws_event_priority(kind, event=event, message=message)
    queue_name = _core_ws_event_queue_name_for_priority(priority)
    target_queue_name = _core_ws_event_target_queue_name(kind, priority=priority, event=event, message=message)
    queue = _core_ws_event_queue_for_name(target_queue_name)
    if queue is None:
        return {
            "accepted": False,
            "queued": False,
            "reason": "WORKER_UNAVAILABLE",
            "queue_size": 0,
            "queue_max_size": _core_ws_event_queue_max_size(),
        }
    queue_size = queue.qsize()
    if _core_ws_price_tick_coalesce_enabled() and kind == "gateway_event":
        coalesce_key = _core_ws_price_tick_coalesce_key(event)
        if coalesce_key:
            return _enqueue_core_ws_price_tick_work(
                queue,
                queue_size=queue_size,
                queue_name=target_queue_name,
                kind=kind,
                metadata=metadata,
                event=event,
                coalesce_key=coalesce_key,
            )
    queued_at = utc_now_ms()
    queued_monotonic = monotonic_ms()
    item = _build_core_ws_event_work_item(
        kind=kind,
        metadata=dict(metadata or {}),
        queue_size=queue_size + 1,
        queued_at=queued_at,
        queued_monotonic=queued_monotonic,
        event=event,
        message=message,
    )
    queued_item = _core_ws_event_queue_item(item)
    message_type = _core_ws_event_work_message_type(kind, event=event, message=message)
    try:
        queue.put_nowait(queued_item)
    except asyncio.QueueFull:
        _update_gateway_core_ws_event_worker_state(
            {
                "enabled": True,
                **_core_ws_event_worker_state_patch(target_queue_name),
                "last_priority": queued_item.priority,
                "dropped_count": int(gateway_core_ws_event_worker_state.get("dropped_count") or 0) + 1,
                "last_message_type": message_type,
                "last_event_id": event.event_id if event is not None else (message.event_id if message is not None else ""),
                "last_queued_at": queued_at,
                "last_error": "CORE_WS_EVENT_QUEUE_FULL",
            }
        )
        return {
            "accepted": False,
            "queued": False,
            "reason": "QUEUE_FULL",
            "queue_size": _core_ws_event_total_queue_size(),
            "queue_max_size": queue.maxsize,
        }
    _update_gateway_core_ws_event_worker_state(
        {
            "enabled": True,
            **_core_ws_event_worker_state_patch(target_queue_name),
            "control_queued_count": int(gateway_core_ws_event_worker_state.get("control_queued_count") or 0)
            + (1 if queue_name == "control" else 0),
            "data_queued_count": int(gateway_core_ws_event_worker_state.get("data_queued_count") or 0)
            + (1 if queue_name == "data" else 0),
            "last_priority": queued_item.priority,
            "queued_count": int(gateway_core_ws_event_worker_state.get("queued_count") or 0) + 1,
            "last_message_type": message_type,
            "last_event_id": event.event_id if event is not None else (message.event_id if message is not None else ""),
            "last_queued_at": queued_at,
            "last_error": "",
        }
    )
    return {
        "accepted": True,
        "queued": True,
        "reason": "",
        "queue_size": _core_ws_event_total_queue_size(),
        "queue_max_size": queue.maxsize,
    }


def _enqueue_core_ws_price_tick_work(
    queue: asyncio.PriorityQueue[_CoreWsEventQueuedItem],
    *,
    queue_size: int,
    queue_name: str,
    kind: str,
    metadata: dict[str, Any],
    event: GatewayEvent | None,
    coalesce_key: str,
) -> dict[str, Any]:
    queued_at = utc_now_ms()
    queued_monotonic = monotonic_ms()
    with _core_ws_price_tick_coalesce_lock:
        already_pending = coalesce_key in _core_ws_price_tick_latest_by_key
    item = _build_core_ws_event_work_item(
        kind=kind,
        metadata=metadata,
        queue_size=queue_size if already_pending else queue_size + 1,
        queued_at=queued_at,
        queued_monotonic=queued_monotonic,
        event=event,
        coalesce_key=coalesce_key,
    )
    if already_pending:
        with _core_ws_price_tick_coalesce_lock:
            _core_ws_price_tick_latest_by_key[coalesce_key] = item
            pending_key_count = len(_core_ws_price_tick_latest_by_key)
        priority = _core_ws_event_priority(kind, event=event)
        _update_gateway_core_ws_event_worker_state(
            {
                "enabled": True,
                **_core_ws_event_worker_state_patch(queue_name),
                "last_priority": priority,
                "price_tick_coalesce_enabled": True,
                "price_tick_pending_key_count": pending_key_count,
                "price_tick_received_count": int(gateway_core_ws_event_worker_state.get("price_tick_received_count") or 0) + 1,
                "price_tick_coalesced_count": int(gateway_core_ws_event_worker_state.get("price_tick_coalesced_count") or 0) + 1,
                "price_tick_last_key": coalesce_key,
                "last_message_type": event.type if event is not None else "price_tick",
                "last_event_id": event.event_id if event is not None else "",
                "last_queued_at": queued_at,
                "last_error": "",
            }
        )
        return {
            "accepted": True,
            "queued": True,
            "coalesced": True,
            "reason": "",
            "queue_size": _core_ws_event_total_queue_size(),
            "queue_max_size": queue.maxsize,
        }
    queued_item = _core_ws_event_queue_item(item)
    try:
        queue.put_nowait(queued_item)
    except asyncio.QueueFull:
        _update_gateway_core_ws_event_worker_state(
            {
                "enabled": True,
                **_core_ws_event_worker_state_patch(queue_name),
                "last_priority": queued_item.priority,
                "dropped_count": int(gateway_core_ws_event_worker_state.get("dropped_count") or 0) + 1,
                "price_tick_coalesce_enabled": True,
                "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
                "price_tick_received_count": int(gateway_core_ws_event_worker_state.get("price_tick_received_count") or 0) + 1,
                "price_tick_dropped_count": int(gateway_core_ws_event_worker_state.get("price_tick_dropped_count") or 0) + 1,
                "price_tick_last_key": coalesce_key,
                "last_message_type": event.type if event is not None else "price_tick",
                "last_event_id": event.event_id if event is not None else "",
                "last_queued_at": queued_at,
                "last_error": "CORE_WS_EVENT_QUEUE_FULL",
            }
        )
        return {
            "accepted": False,
            "queued": False,
            "coalesced": False,
            "reason": "QUEUE_FULL",
            "queue_size": _core_ws_event_total_queue_size(),
            "queue_max_size": queue.maxsize,
        }
    with _core_ws_price_tick_coalesce_lock:
        _core_ws_price_tick_latest_by_key[coalesce_key] = item
        pending_key_count = len(_core_ws_price_tick_latest_by_key)
    _update_gateway_core_ws_event_worker_state(
        {
            "enabled": True,
            **_core_ws_event_worker_state_patch(queue_name),
            "data_queued_count": int(gateway_core_ws_event_worker_state.get("data_queued_count") or 0) + 1,
            "last_priority": queued_item.priority,
            "queued_count": int(gateway_core_ws_event_worker_state.get("queued_count") or 0) + 1,
            "price_tick_coalesce_enabled": True,
            "price_tick_pending_key_count": pending_key_count,
            "price_tick_received_count": int(gateway_core_ws_event_worker_state.get("price_tick_received_count") or 0) + 1,
            "price_tick_queued_count": int(gateway_core_ws_event_worker_state.get("price_tick_queued_count") or 0) + 1,
            "price_tick_last_key": coalesce_key,
            "last_message_type": event.type if event is not None else "price_tick",
            "last_event_id": event.event_id if event is not None else "",
            "last_queued_at": queued_at,
            "last_error": "",
        }
    )
    return {
        "accepted": True,
        "queued": True,
        "coalesced": False,
        "reason": "",
        "queue_size": _core_ws_event_total_queue_size(),
        "queue_max_size": queue.maxsize,
    }


def _prepare_condition_events_for_queue(
    events: list[GatewayEvent],
    *,
    metadata: dict[str, Any],
    queue_size: int,
) -> dict[str, Any]:
    latest_by_key: dict[str, tuple[int, GatewayEvent]] = {}
    passthrough: list[tuple[int, GatewayEvent]] = []
    coalesced_count = 0
    for index, event in enumerate(events):
        _record_ws_message_side_effects(event, metadata)
        key = _condition_event_coalesce_key(event)
        if key:
            if key in latest_by_key:
                coalesced_count += 1
            latest_by_key[key] = (index, event)
        else:
            passthrough.append((index, event))
    merged = passthrough + list(latest_by_key.values())
    merged.sort(key=lambda item: item[0])
    return {
        "events": [event for _, event in merged],
        "received_count": len(events),
        "coalesced_count": coalesced_count,
        "queue_size": queue_size,
    }


def _condition_event_coalesce_key(event: GatewayEvent) -> str:
    if event.type != "condition_event":
        return ""
    payload = dict(event.payload or {})
    code = str(payload.get("code") or payload.get("stock_code") or "").strip()
    condition_index = str(payload.get("condition_index") or payload.get("index") or "").strip()
    condition_name = str(payload.get("condition_name") or payload.get("name") or "").strip()
    if not code or not (condition_index or condition_name):
        return ""
    return f"condition:{condition_index}|name:{condition_name}|code:{code}"


def _assign_condition_event_generation(event: GatewayEvent) -> GatewayEvent:
    key = _condition_event_coalesce_key(event)
    if not key:
        return event
    global _gateway_condition_event_generation
    with _gateway_condition_event_coalesce_lock:
        _gateway_condition_event_generation += 1
        generation = _gateway_condition_event_generation
        _gateway_condition_event_latest_generation[key] = generation
    return _event_with_trace(
        event,
        {
            "core_condition_event_coalesce_key": key,
            "core_condition_event_generation": generation,
        },
    )


def _condition_event_is_stale(event: GatewayEvent) -> bool:
    trace = trace_from_payload(event.payload)
    key = str(trace.get("core_condition_event_coalesce_key") or _condition_event_coalesce_key(event) or "")
    generation = _optional_int_value(trace.get("core_condition_event_generation"))
    if not key or generation is None:
        return False
    with _gateway_condition_event_coalesce_lock:
        latest_generation = _gateway_condition_event_latest_generation.get(key)
    return latest_generation is not None and latest_generation != generation


def _condition_event_action(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    return str(payload.get("event_type") or "include").strip().lower()


def _condition_event_queue_wait_ms(event: GatewayEvent, worker_started_monotonic: float) -> float | None:
    trace = trace_from_payload(event.payload)
    return monotonic_delta_ms(trace.get("core_condition_event_queued_monotonic_ms"), worker_started_monotonic)


def _condition_event_should_skip_stale_include(event: GatewayEvent, worker_started_monotonic: float) -> tuple[bool, float | None, float]:
    threshold_ms = _gateway_condition_event_stale_include_skip_ms()
    if threshold_ms <= 0.0 or event.type != "condition_event" or _condition_event_action(event) != "include":
        return False, None, threshold_ms
    queue_wait_ms = _condition_event_queue_wait_ms(event, worker_started_monotonic)
    if queue_wait_ms is None:
        return False, None, threshold_ms
    return queue_wait_ms > threshold_ms, queue_wait_ms, threshold_ms


def _clear_condition_event_generation_if_current(event: GatewayEvent) -> None:
    trace = trace_from_payload(event.payload)
    key = str(trace.get("core_condition_event_coalesce_key") or _condition_event_coalesce_key(event) or "")
    generation = _optional_int_value(trace.get("core_condition_event_generation"))
    if not key or generation is None:
        return
    with _gateway_condition_event_coalesce_lock:
        if _gateway_condition_event_latest_generation.get(key) == generation:
            _gateway_condition_event_latest_generation.pop(key, None)


def _enqueue_gateway_condition_events(events: list[GatewayEvent], metadata: dict[str, Any]) -> dict[str, Any]:
    if not events:
        return {"accepted": True, "queued": True, "count": 0, "queued_count": 0, "dropped_count": 0, "queue_size": 0}
    _ensure_gateway_condition_event_worker()
    queues = list(_gateway_condition_event_queues)
    if not queues:
        return {"accepted": False, "queued": False, "count": len(events), "queued_count": 0, "dropped_count": len(events), "queue_size": 0, "reason": "WORKER_DISABLED"}
    queue_size = int(gateway_condition_event_worker_state.get("queue_size") or 0)
    queue_max_size = _gateway_condition_event_queue_max_size()
    worker_count = len(queues)
    prepared = _prepare_condition_events_for_queue(events, metadata=metadata, queue_size=queue_size)
    queued_events = prepared["events"]
    received_count = int(prepared["received_count"])
    coalesced_count = int(prepared["coalesced_count"])
    if not queued_events:
        now = utc_now_ms()
        _update_gateway_condition_event_worker_state(
            {
                "enabled": True,
                "queue_size": queue_size,
                "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                "queue_max_size": queue_max_size,
                "worker_count": worker_count,
                "received_count": int(gateway_condition_event_worker_state.get("received_count") or 0) + received_count,
                "coalesced_count": int(gateway_condition_event_worker_state.get("coalesced_count") or 0) + coalesced_count,
                "last_received_count": received_count,
                "last_queued_count": 0,
                "last_coalesced_count": coalesced_count,
                "last_queued_at": now,
                "last_error": "",
            }
        )
        return {
            "accepted": True,
            "queued": True,
            "count": received_count,
            "queued_count": 0,
            "coalesced_count": coalesced_count,
            "dropped_count": 0,
            "queue_size": queue_size,
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_max_size": queue_max_size,
        }
    if queue_size + len(queued_events) > queue_max_size:
        now = utc_now_ms()
        _update_gateway_condition_event_worker_state(
            {
                "enabled": True,
                "queue_size": queue_size,
                "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                "queue_max_size": queue_max_size,
                "worker_count": worker_count,
                "received_count": int(gateway_condition_event_worker_state.get("received_count") or 0) + received_count,
                "coalesced_count": int(gateway_condition_event_worker_state.get("coalesced_count") or 0) + coalesced_count,
                "dropped_count": int(gateway_condition_event_worker_state.get("dropped_count") or 0) + len(queued_events),
                "last_received_count": received_count,
                "last_queued_count": 0,
                "last_coalesced_count": coalesced_count,
                "last_error": "CONDITION_EVENT_QUEUE_FULL",
                "last_queued_at": now,
            }
        )
        return {
            "accepted": False,
            "queued": False,
            "count": received_count,
            "queued_count": 0,
            "coalesced_count": coalesced_count,
            "dropped_count": len(queued_events),
            "queue_size": queue_size,
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_max_size": queue_max_size,
            "reason": "QUEUE_FULL",
        }
    queued_at = utc_now_ms()
    queued_monotonic = monotonic_ms()
    traced_events: list[GatewayEvent] = []
    for index, event in enumerate(queued_events):
        event = _assign_condition_event_generation(event)
        traced_events.append(
            _event_with_trace(
                event,
                {
                    "core_condition_event_queued_at_utc": queued_at,
                    "core_condition_event_queued_monotonic_ms": queued_monotonic,
                    "core_condition_event_queue_size": queue_size + index + 1,
                    "core_condition_event_queue_batch_size": len(queued_events),
                    "core_condition_event_queue_batch_index": index,
                },
            )
        )
    batches_by_worker: dict[int, list[GatewayEvent]] = {}
    for event in traced_events:
        worker_index = _gateway_condition_event_worker_index(event, worker_count)
        batches_by_worker.setdefault(worker_index, []).append(event)
    chunk_size = _gateway_condition_event_batch_chunk_size()
    chunked_batches_by_worker = _chunk_condition_event_worker_batches(batches_by_worker, chunk_size)
    queued_worker_indexes: list[int] = []
    queue_full = False
    for worker_index, worker_batches in chunked_batches_by_worker.items():
        queue = queues[worker_index]
        if queue.maxsize > 0 and queue.qsize() + len(worker_batches) > queue.maxsize:
            queue_full = True
            break
    if queue_full:
        for event in traced_events:
            _clear_condition_event_generation_if_current(event)
        _update_gateway_condition_event_worker_state(
            {
                "enabled": True,
                "queue_size": queue_size,
                "queue_sizes_by_worker": _gateway_condition_event_queue_sizes_by_worker(),
                "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                "queue_max_size": queue_max_size,
                "worker_count": worker_count,
                "batch_chunk_size": chunk_size,
                "received_count": int(gateway_condition_event_worker_state.get("received_count") or 0) + received_count,
                "coalesced_count": int(gateway_condition_event_worker_state.get("coalesced_count") or 0) + coalesced_count,
                "dropped_count": int(gateway_condition_event_worker_state.get("dropped_count") or 0) + len(traced_events),
                "last_received_count": received_count,
                "last_queued_count": 0,
                "last_coalesced_count": coalesced_count,
                "last_error": "CONDITION_EVENT_SHARD_QUEUE_FULL",
                "last_queued_at": queued_at,
            }
        )
        return {
            "accepted": False,
            "queued": False,
            "count": received_count,
            "queued_count": 0,
            "coalesced_count": coalesced_count,
            "dropped_count": len(traced_events),
            "queue_size": queue_size,
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_max_size": queue_max_size,
            "reason": "QUEUE_FULL",
        }
    try:
        for worker_index, worker_batches in chunked_batches_by_worker.items():
            for worker_events in worker_batches:
                queues[worker_index].put_nowait(
                    _ConditionEventBatchWorkItem(
                        events=worker_events,
                        queued_at=queued_at,
                        queued_monotonic_ms=queued_monotonic,
                    )
                )
                queued_worker_indexes.append(worker_index)
    except asyncio.QueueFull:
        for event in traced_events:
            _clear_condition_event_generation_if_current(event)
        _update_gateway_condition_event_worker_state(
            {
                "enabled": True,
                "queue_size": queue_size,
                "queue_sizes_by_worker": _gateway_condition_event_queue_sizes_by_worker(),
                "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                "queue_max_size": queue_max_size,
                "worker_count": worker_count,
                "batch_chunk_size": chunk_size,
                "received_count": int(gateway_condition_event_worker_state.get("received_count") or 0) + received_count,
                "coalesced_count": int(gateway_condition_event_worker_state.get("coalesced_count") or 0) + coalesced_count,
                "dropped_count": int(gateway_condition_event_worker_state.get("dropped_count") or 0) + len(traced_events),
                "last_received_count": received_count,
                "last_queued_count": 0,
                "last_coalesced_count": coalesced_count,
                "last_error": "CONDITION_EVENT_BATCH_QUEUE_FULL",
                "last_queued_at": queued_at,
            }
        )
        return {
            "accepted": False,
            "queued": False,
            "count": received_count,
            "queued_count": 0,
            "coalesced_count": coalesced_count,
            "dropped_count": len(traced_events),
            "queue_size": queue_size,
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_max_size": queue_max_size,
            "reason": "QUEUE_FULL",
        }
    next_queue_size = queue_size + len(traced_events)
    queue_sizes_by_worker = _gateway_condition_event_queue_sizes_by_worker()
    for worker_index, worker_events in batches_by_worker.items():
        if 0 <= worker_index < len(queue_sizes_by_worker):
            queue_sizes_by_worker[worker_index] += len(worker_events)
    _update_gateway_condition_event_worker_state(
        {
            "enabled": True,
            "queue_size": next_queue_size,
            "queue_sizes_by_worker": queue_sizes_by_worker,
            "queue_batch_count": _gateway_condition_event_queue_batch_count(),
            "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
            "queue_max_size": queue_max_size,
            "worker_count": worker_count,
            "batch_chunk_size": chunk_size,
            "received_count": int(gateway_condition_event_worker_state.get("received_count") or 0) + received_count,
            "queued_count": int(gateway_condition_event_worker_state.get("queued_count") or 0) + len(traced_events),
            "coalesced_count": int(gateway_condition_event_worker_state.get("coalesced_count") or 0) + coalesced_count,
            "last_received_count": received_count,
            "last_queued_count": len(traced_events),
            "last_queued_batch_count": len(queued_worker_indexes),
            "last_coalesced_count": coalesced_count,
            "last_worker_index": queued_worker_indexes[-1] if queued_worker_indexes else 0,
            "last_shard_key": _condition_event_shard_key(traced_events[-1]) if traced_events else "",
            "last_queued_at": queued_at,
            "last_error": "",
        }
    )
    return {
        "accepted": True,
        "queued": True,
        "count": received_count,
        "queued_count": len(traced_events),
        "coalesced_count": coalesced_count,
        "dropped_count": 0,
        "queue_size": next_queue_size,
        "queue_batch_count": _gateway_condition_event_queue_batch_count(),
        "queue_max_size": queue_max_size,
    }


async def _gateway_condition_event_worker_loop_main(worker_index: int = 0) -> None:
    while True:
        if worker_index >= len(_gateway_condition_event_queues):
            await asyncio.sleep(0.1)
            continue
        queue = _gateway_condition_event_queues[worker_index]
        item = await queue.get()
        drained_items = [item]
        events = list(item.events)
        chunk_size = _gateway_condition_event_batch_chunk_size()
        while queue.qsize() > 0 and len(events) < chunk_size:
            try:
                drained_item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            drained_items.append(drained_item)
            events.extend(drained_item.events)
        batch_size = len(events)
        batch_queue_wait_ms = monotonic_delta_ms(item.queued_monotonic_ms, monotonic_ms()) or 0.0
        queue_size = max(0, int(gateway_condition_event_worker_state.get("queue_size") or 0) - batch_size)
        _update_gateway_condition_event_worker_state(
            {
                "queue_size": queue_size,
                "queue_sizes_by_worker": _condition_event_queue_sizes_with_delta(worker_index, -batch_size),
                "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                "active_worker_count": int(gateway_condition_event_worker_state.get("active_worker_count") or 0) + 1,
                "active_count": int(gateway_condition_event_worker_state.get("active_count") or 0) + batch_size,
                "last_batch_size": batch_size,
                "last_drained_batch_count": len(drained_items),
                "last_queue_wait_ms": round(batch_queue_wait_ms, 3),
                "last_worker_index": worker_index,
                "last_shard_key": _condition_event_shard_key(events[-1]) if events else "",
            }
        )
        try:
            loop = asyncio.get_running_loop()
            if _gateway_condition_event_executor is None:
                raise RuntimeError("condition event worker executor is not initialized")
            batch_started = time.perf_counter()
            result = await loop.run_in_executor(
                _gateway_condition_event_executor,
                _process_condition_event_batch_in_worker,
                events,
            )
            batch_duration_ms = (time.perf_counter() - batch_started) * 1000.0
            processed_count = int(result.get("processed_count") or 0)
            failed_count = int(result.get("failed_count") or 0)
            stale_skipped_count = int(result.get("stale_skipped_count") or 0)
            stale_queue_wait_skipped_count = int(result.get("stale_queue_wait_skipped_count") or 0)
            _update_gateway_condition_event_worker_state(
                {
                    "queue_size": int(gateway_condition_event_worker_state.get("queue_size") or 0),
                    "queue_sizes_by_worker": _gateway_condition_event_queue_sizes_by_worker(),
                    "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                    "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                    "active_worker_count": max(0, int(gateway_condition_event_worker_state.get("active_worker_count") or 0) - 1),
                    "active_count": max(0, int(gateway_condition_event_worker_state.get("active_count") or 0) - batch_size),
                    "processed_count": int(gateway_condition_event_worker_state.get("processed_count") or 0) + processed_count,
                    "failed_count": int(gateway_condition_event_worker_state.get("failed_count") or 0) + failed_count,
                    "stale_skipped_count": int(gateway_condition_event_worker_state.get("stale_skipped_count") or 0) + stale_skipped_count,
                    "stale_queue_wait_skipped_count": int(gateway_condition_event_worker_state.get("stale_queue_wait_skipped_count") or 0)
                    + stale_queue_wait_skipped_count,
                    "last_stale_skipped_count": stale_skipped_count,
                    "last_stale_queue_wait_skipped_count": stale_queue_wait_skipped_count,
                    "last_stale_queue_wait_ms": round(float(result.get("last_stale_queue_wait_ms") or 0.0), 3),
                    "last_queue_wait_ms": round(float(result.get("last_queue_wait_ms") or batch_queue_wait_ms), 3),
                    "last_batch_duration_ms": round(batch_duration_ms, 3),
                    "last_worker_index": worker_index,
                    "last_processed_at": utc_now_ms(),
                    "last_error": "" if failed_count == 0 else str(result.get("last_error") or ""),
                }
            )
            if int(result.get("accepted_count") or 0) > 0:
                await _schedule_dashboard_snapshot_broadcast()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_gateway_condition_event_worker_state(
                {
                    "queue_size": int(gateway_condition_event_worker_state.get("queue_size") or 0),
                    "queue_sizes_by_worker": _gateway_condition_event_queue_sizes_by_worker(),
                    "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                    "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                    "active_worker_count": max(0, int(gateway_condition_event_worker_state.get("active_worker_count") or 0) - 1),
                    "active_count": max(0, int(gateway_condition_event_worker_state.get("active_count") or 0) - batch_size),
                    "failed_count": int(gateway_condition_event_worker_state.get("failed_count") or 0) + batch_size,
                    "last_worker_index": worker_index,
                    "last_error": str(exc) or repr(exc),
                }
            )
        finally:
            for _drained_item in drained_items:
                queue.task_done()
            _update_gateway_condition_event_worker_state(
                {
                    "queue_batch_count": _gateway_condition_event_queue_batch_count(),
                    "queue_batch_counts_by_worker": _gateway_condition_event_queue_batch_counts(),
                }
            )


async def _core_ws_event_worker_loop_main(worker_kind: str = "single", worker_index: int = 0) -> None:
    while True:
        worker_queue_name = f"control:{worker_index}" if worker_kind == "control" else worker_kind
        queue = _core_ws_event_queue_for_name(worker_queue_name)
        if queue is None:
            await asyncio.sleep(0.1)
            continue
        queued_item = await queue.get()
        item = _resolve_core_ws_event_work_item(queued_item.work_item)
        item = replace(
            item,
            metadata={
                **dict(item.metadata or {}),
                "core_ws_event_worker_kind": worker_kind,
                "core_ws_event_worker_index": worker_index,
                "core_ws_event_worker_queue_name": worker_queue_name,
            },
        )
        message_type = _core_ws_event_work_message_type(item.kind, event=item.event, message=item.message)
        event_id = item.event.event_id if item.event is not None else (item.message.event_id if item.message is not None else "")
        queue_wait_ms = monotonic_delta_ms(item.queued_monotonic_ms, monotonic_ms()) or 0.0
        _update_gateway_core_ws_event_worker_state(
            {
                **_core_ws_event_worker_state_patch(worker_queue_name),
                **_core_ws_event_active_state_patch(worker_kind, active=True, worker_index=worker_index),
                "last_priority": queued_item.priority,
                "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
                "last_message_type": message_type,
                "last_event_id": event_id,
                "last_queue_wait_ms": round(queue_wait_ms, 3),
            }
        )
        try:
            started = time.perf_counter()
            await _process_core_ws_event_work_item(item, queue_wait_ms=queue_wait_ms)
            duration_ms = (time.perf_counter() - started) * 1000.0
            _update_gateway_core_ws_event_worker_state(
                {
                    **_core_ws_event_worker_state_patch(worker_queue_name),
                    **_core_ws_event_active_state_patch(worker_kind, active=False, worker_index=worker_index),
                    "processed_count": int(gateway_core_ws_event_worker_state.get("processed_count") or 0) + 1,
                    "price_tick_processed_count": int(gateway_core_ws_event_worker_state.get("price_tick_processed_count") or 0)
                    + (1 if message_type == "price_tick" else 0),
                    "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
                    "last_duration_ms": round(duration_ms, 3),
                    "last_processed_at": utc_now_ms(),
                    "last_error": "",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _update_gateway_core_ws_event_worker_state(
                {
                    **_core_ws_event_worker_state_patch(worker_queue_name),
                    **_core_ws_event_active_state_patch(worker_kind, active=False, worker_index=worker_index),
                    "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
                    "failed_count": int(gateway_core_ws_event_worker_state.get("failed_count") or 0) + 1,
                    "last_error": _truncate_log_detail(str(exc) or repr(exc)),
                }
            )
        finally:
            queue.task_done()
            _update_gateway_core_ws_event_worker_state(
                {
                    **_core_ws_event_worker_state_patch(worker_queue_name),
                    "price_tick_pending_key_count": _core_ws_price_tick_pending_key_count(),
                }
            )


def _resolve_core_ws_event_work_item(item: _CoreWsEventWorkItem) -> _CoreWsEventWorkItem:
    if not item.coalesce_key:
        return item
    with _core_ws_price_tick_coalesce_lock:
        latest = _core_ws_price_tick_latest_by_key.pop(item.coalesce_key, None)
    return latest or item


async def _process_core_ws_event_work_item(item: _CoreWsEventWorkItem, *, queue_wait_ms: float) -> dict[str, Any]:
    if item.kind == "send_completed":
        if item.message is None:
            raise ValueError("send_completed work item missing message")
        return await asyncio.to_thread(_record_gateway_ws_send_completed, item.message, item.metadata)
    if item.event is None:
        raise ValueError(f"{item.kind} work item missing event")
    started_at = utc_now_ms()
    started_monotonic = monotonic_ms()
    event = _event_with_trace(
        item.event,
        {
            "core_ws_event_worker_started_at_utc": started_at,
            "core_ws_event_worker_started_monotonic_ms": started_monotonic,
            "core_ws_event_queue_wait_ms": queue_wait_ms,
            "core_ws_event_worker_kind": item.metadata.get("core_ws_event_worker_kind"),
            "core_ws_event_worker_index": item.metadata.get("core_ws_event_worker_index"),
            "core_ws_event_worker_queue_name": item.metadata.get("core_ws_event_worker_queue_name"),
        },
    )
    if item.kind == "transport_heartbeat":
        _record_ws_message_side_effects(event, item.metadata)
        await asyncio.to_thread(_maybe_record_ws_pilot_diagnostic_log, dict(event.payload or {}))
        return {"accepted": True, "event_id": event.event_id, "type": event.type, "transport_only": True}
    if item.kind == "gateway_event":
        _record_ws_message_side_effects(event, item.metadata)
        return await _process_gateway_event(event)
    raise ValueError(f"unsupported core ws event work kind: {item.kind}")


def _core_ws_event_work_message_type(
    kind: str,
    *,
    event: GatewayEvent | None = None,
    message: GatewayWsMessage | None = None,
) -> str:
    if event is not None:
        return event.type
    if message is not None:
        return message.type
    return kind


def _update_gateway_core_ws_event_worker_state(patch: dict[str, Any]) -> None:
    gateway_core_ws_event_worker_state.update({key: value for key, value in patch.items() if value is not None})


def _record_core_ws_fast_status_hint(event: GatewayEvent, metadata: dict[str, Any]) -> None:
    if event.type not in {"heartbeat", "transport_heartbeat"}:
        return
    _record_ws_message_side_effects(event, metadata)
    if event.type == "heartbeat":
        gateway_state.record_heartbeat_hint(event)
    _update_gateway_core_ws_event_worker_state(
        {
            "last_fast_status_hint_type": event.type,
            "last_fast_status_hint_event_id": event.event_id,
            "last_fast_status_hint_at": utc_now_ms(),
        }
    )


def _core_ws_heartbeat_queue_required(event: GatewayEvent) -> bool:
    if event.type != "heartbeat":
        return True
    payload = dict(event.payload or {})
    if _ws_pilot_diagnostic_signature(payload):
        return True
    settings = get_settings()
    sample_key = event.event_id or event.command_id or event.request_id
    if settings.transport_metrics_enabled and should_sample_transport_message(
        message_type=event.type,
        sample_key=sample_key,
        price_tick_rate=settings.transport_metrics_sample_price_tick_rate,
        heartbeat_rate=settings.transport_metrics_sample_heartbeat_rate,
    ):
        return True
    state_key_payload = {
        "kiwoom_logged_in": bool(payload.get("kiwoom_logged_in", False)),
        "orderable": bool(payload.get("orderable", False)),
        "account": str(payload.get("account") or ""),
        "mode": str(payload.get("mode") or ""),
        "reconnect_count": str(payload.get("reconnect_count") if payload.get("reconnect_count") is not None else ""),
        "broker_env": str(payload.get("broker_env") or ""),
        "server_mode": str(payload.get("server_mode") or ""),
        "account_mode": str(payload.get("account_mode") or ""),
        "last_error": str(payload.get("last_error") or ""),
    }
    state_key = json.dumps(state_key_payload, ensure_ascii=False, sort_keys=True, default=str)
    if state_key != str(gateway_core_ws_event_worker_state.get("last_heartbeat_queue_key") or ""):
        _update_gateway_core_ws_event_worker_state(
            {
                "last_heartbeat_queue_key": state_key,
                "last_heartbeat_queue_key_at": utc_now_ms(),
            }
        )
        return True
    _update_gateway_core_ws_event_worker_state(
        {
            "heartbeat_fast_skipped_count": int(gateway_core_ws_event_worker_state.get("heartbeat_fast_skipped_count") or 0) + 1,
            "last_fast_status_hint_type": event.type,
            "last_fast_status_hint_event_id": event.event_id,
            "last_fast_status_hint_at": utc_now_ms(),
        }
    )
    return False


def _process_condition_event_in_worker(event: GatewayEvent) -> dict[str, Any]:
    result = _process_condition_event_batch_in_worker([event])
    results = list(result.get("results") or [])
    if not results:
        return {"accepted": False, "event_id": event.event_id, "type": event.type, "failed": True, "error": "EMPTY_BATCH_RESULT"}
    return results[0]


def _process_condition_event_batch_in_worker(events: list[GatewayEvent]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    accepted_count = 0
    processed_count = 0
    failed_count = 0
    stale_skipped_count = 0
    stale_queue_wait_skipped_count = 0
    last_queue_wait_ms = 0.0
    last_stale_queue_wait_ms = 0.0
    last_error = ""
    if not events:
        return {
            "processed_count": 0,
            "accepted_count": 0,
            "failed_count": 0,
            "stale_skipped_count": 0,
            "stale_queue_wait_skipped_count": 0,
            "results": [],
        }
    db: TradingDatabase | None = None
    try:
        collector: CandidateCollector | None = None
        for event in events:
            if _condition_event_is_stale(event):
                stale_skipped_count += 1
                results.append(_condition_event_stale_skip_result(event))
                continue
            if db is None:
                db = open_database()
                collector = CandidateCollector(db)
            worker_started_monotonic = monotonic_ms()
            skip_stale_include, queue_wait_ms, _threshold_ms = _condition_event_should_skip_stale_include(event, worker_started_monotonic)
            if queue_wait_ms is not None:
                last_queue_wait_ms = queue_wait_ms
            if skip_stale_include:
                stale_skipped_count += 1
                stale_queue_wait_skipped_count += 1
                last_stale_queue_wait_ms = float(queue_wait_ms or 0.0)
                try:
                    result = _record_condition_event_stale_include_skip(
                        db,
                        collector,
                        event,
                        worker_started_monotonic=worker_started_monotonic,
                        queue_wait_ms=last_stale_queue_wait_ms,
                    )
                except Exception as exc:
                    result = _record_condition_event_worker_failure(db, event, exc)
                    failed_count += 1
                    last_error = str(result.get("error") or last_error)
                finally:
                    _clear_condition_event_generation_if_current(event)
                results.append(result)
                continue
            try:
                result = _process_condition_event_with_db(db, collector, event)
            except Exception as exc:
                result = _record_condition_event_worker_failure(db, event, exc)
            finally:
                _clear_condition_event_generation_if_current(event)
            results.append(result)
            processed_count += 1
            if result.get("accepted"):
                accepted_count += 1
            if result.get("failed"):
                failed_count += 1
                last_error = str(result.get("error") or last_error)
    finally:
        if db is not None:
            close_database(db)
    return {
        "processed_count": processed_count,
        "accepted_count": accepted_count,
        "failed_count": failed_count,
        "stale_skipped_count": stale_skipped_count,
        "stale_queue_wait_skipped_count": stale_queue_wait_skipped_count,
        "last_queue_wait_ms": last_queue_wait_ms,
        "last_stale_queue_wait_ms": last_stale_queue_wait_ms,
        "last_error": last_error,
        "results": results,
    }


def _condition_event_stale_skip_result(event: GatewayEvent) -> dict[str, Any]:
    trace = trace_from_payload(event.payload)
    return {
        "accepted": False,
        "event_id": event.event_id,
        "type": event.type,
        "skipped": True,
        "skip_reason": "STALE_CONDITION_EVENT_COALESCED",
        "coalesce_key": str(trace.get("core_condition_event_coalesce_key") or _condition_event_coalesce_key(event) or ""),
    }


def _record_condition_event_stale_include_skip(
    db: TradingDatabase,
    collector: CandidateCollector,
    event: GatewayEvent,
    *,
    worker_started_monotonic: float,
    queue_wait_ms: float,
) -> dict[str, Any]:
    core_received_at = utc_now_ms()
    trace = trace_from_payload(event.payload)
    threshold_ms = _gateway_condition_event_stale_include_skip_ms()
    event = _event_with_trace(
        event,
        {
            "core_condition_event_worker_started_at_utc": core_received_at,
            "core_condition_event_worker_started_monotonic_ms": worker_started_monotonic,
            "core_condition_event_queue_wait_ms": queue_wait_ms,
            "core_condition_event_stale_include_skipped": True,
            "core_condition_event_stale_include_skip_ms": threshold_ms,
            "core_condition_event_stale_reason": "STALE_CONDITION_INCLUDE_QUEUE_WAIT",
            "core_event_received_at_utc": core_received_at,
            "core_event_received_monotonic_ms": worker_started_monotonic,
            "core_event_persisted_at_utc": utc_now_ms(),
            "core_event_persisted_monotonic_ms": monotonic_ms(),
        },
    )
    condition_event = BrokerConditionEvent.from_dict(event.payload)
    collector.reject_condition_event(
        condition_event,
        "include",
        warning=f"STALE_CONDITION_INCLUDE_QUEUE_WAIT:{condition_event.condition_name}:{condition_event.code}",
        reason="stale condition include queue wait",
    )
    _save_gateway_event_transport_sample(
        db,
        event,
        accepted=False,
        core_receive_ms=wall_ms(trace.get("gateway_event_post_end_at_utc"), core_received_at),
        core_persist_ms=0.0,
        error="STALE_CONDITION_INCLUDE_QUEUE_WAIT",
    )
    return {
        "accepted": False,
        "event_id": event.event_id,
        "type": event.type,
        "skipped": True,
        "skip_reason": "STALE_CONDITION_INCLUDE_QUEUE_WAIT",
        "queue_wait_ms": queue_wait_ms,
        "threshold_ms": threshold_ms,
    }


def _process_condition_event_with_db(db: TradingDatabase, collector: CandidateCollector, event: GatewayEvent) -> dict[str, Any]:
    if event.type != "condition_event":
        raise ValueError(f"unsupported async condition event type: {event.type}")
    core_received_at = utc_now_ms()
    core_received_monotonic = monotonic_ms()
    trace = trace_from_payload(event.payload)
    condition_queue_wait_ms = monotonic_delta_ms(
        trace.get("core_condition_event_queued_monotonic_ms"),
        core_received_monotonic,
    )
    event = _event_with_trace(
        event,
        {
            "core_condition_event_worker_started_at_utc": core_received_at,
            "core_condition_event_worker_started_monotonic_ms": core_received_monotonic,
            "core_condition_event_queue_wait_ms": condition_queue_wait_ms,
            "core_event_received_at_utc": core_received_at,
            "core_event_received_monotonic_ms": core_received_monotonic,
        },
    )
    process_started = time.perf_counter()
    accepted = gateway_state.record_event(event)
    persist_ms = 0.0
    if accepted:
        persist_started = time.perf_counter()
        _persist_condition_event_with_collector(db, collector, event)
        persist_ms = (time.perf_counter() - persist_started) * 1000.0
        event = _event_with_trace(
            event,
            {
                "core_condition_event_process_ms": (time.perf_counter() - process_started) * 1000.0,
                "core_event_persisted_at_utc": utc_now_ms(),
                "core_event_persisted_monotonic_ms": monotonic_ms(),
            },
        )
        _queue_replay_tick_history(event)
        _save_gateway_event_transport_sample(
            db,
            event,
            accepted=True,
            core_receive_ms=wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
            core_persist_ms=persist_ms,
        )
    else:
        event = _event_with_trace(
            event,
            {
                "core_condition_event_process_ms": (time.perf_counter() - process_started) * 1000.0,
                "core_event_persisted_at_utc": utc_now_ms(),
                "core_event_persisted_monotonic_ms": monotonic_ms(),
            },
        )
        _save_gateway_event_transport_sample(
            db,
            event,
            accepted=False,
            core_receive_ms=wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
            core_persist_ms=persist_ms,
            error="DUPLICATE_OR_REJECTED_EVENT",
        )
    return {
        "accepted": accepted,
        "event_id": event.event_id,
        "type": event.type,
        "transport": {
            "core_receive_ms": wall_ms(trace_from_payload(event.payload).get("gateway_event_post_end_at_utc"), core_received_at),
            "core_persist_ms": persist_ms,
        },
    }


def _record_condition_event_worker_failure(db: TradingDatabase, event: GatewayEvent, exc: Exception) -> dict[str, Any]:
    core_received_at = utc_now_ms()
    core_received_monotonic = monotonic_ms()
    trace = trace_from_payload(event.payload)
    condition_queue_wait_ms = monotonic_delta_ms(
        trace.get("core_condition_event_queued_monotonic_ms"),
        core_received_monotonic,
    )
    event = _event_with_trace(
        event,
        {
            "core_condition_event_worker_started_at_utc": core_received_at,
            "core_condition_event_worker_started_monotonic_ms": core_received_monotonic,
            "core_condition_event_queue_wait_ms": condition_queue_wait_ms,
            "core_condition_event_process_ms": 0.0,
            "core_event_received_at_utc": core_received_at,
            "core_event_received_monotonic_ms": core_received_monotonic,
            "core_event_persisted_at_utc": utc_now_ms(),
            "core_event_persisted_monotonic_ms": monotonic_ms(),
        },
    )
    error = str(exc) or repr(exc)
    try:
        _save_gateway_event_transport_sample(
            db,
            event,
            accepted=False,
            core_receive_ms=wall_ms(trace.get("gateway_event_post_end_at_utc"), core_received_at),
            core_persist_ms=0.0,
            error=error,
        )
    except Exception:
        pass
    return {
        "accepted": False,
        "event_id": event.event_id,
        "type": event.type,
        "failed": True,
        "error": error,
        "transport": {
            "core_receive_ms": wall_ms(trace.get("gateway_event_post_end_at_utc"), core_received_at),
            "core_persist_ms": 0.0,
        },
    }


def _persist_condition_event_with_collector(db: TradingDatabase, collector: CandidateCollector, event: GatewayEvent) -> None:
    if event.type != "condition_event":
        _persist_gateway_event(db, event)
        return
    condition_event = BrokerConditionEvent.from_dict(event.payload)
    if condition_event.event_type == "remove":
        collector.handle_condition_remove(condition_event)
    else:
        collector.handle_condition_include(condition_event)


def _update_gateway_condition_event_worker_state(patch: dict[str, Any]) -> None:
    gateway_condition_event_worker_state.update({key: value for key, value in patch.items() if value is not None})


def _dashboard_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def _dashboard_snapshot_cache_ttl_sec() -> float:
    return _dashboard_float_env("TRADING_DASHBOARD_SNAPSHOT_CACHE_TTL_SEC", DASHBOARD_SNAPSHOT_CACHE_TTL_SEC)


def _dashboard_heavy_section_cache_ttl_sec() -> float:
    return _dashboard_float_env("TRADING_DASHBOARD_HEAVY_SECTION_CACHE_TTL_SEC", DASHBOARD_HEAVY_SECTION_CACHE_TTL_SEC)


def _dashboard_ws_push_interval_sec() -> float:
    return _dashboard_float_env("TRADING_DASHBOARD_WS_PUSH_INTERVAL_SEC", DASHBOARD_WS_PUSH_INTERVAL_SEC, minimum=1.0)


def _dashboard_snapshot_detail(detail: str | None = None) -> str:
    value = str(detail or DASHBOARD_SNAPSHOT_DETAIL_SLIM).strip().lower()
    if value in {DASHBOARD_SNAPSHOT_DETAIL_FULL, "debug", "verbose"}:
        return DASHBOARD_SNAPSHOT_DETAIL_FULL
    return DASHBOARD_SNAPSHOT_DETAIL_SLIM


def _dashboard_database_cache_key(db: TradingDatabase | None = None) -> str:
    if db is not None and getattr(db, "path", None) is not None:
        return str(Path(db.path).resolve())
    try:
        return str(Path(get_settings().db_path).resolve())
    except Exception:
        return ""


def _cached_dashboard_fragment(
    db: TradingDatabase,
    key: str,
    builder,
    *,
    ttl_sec: float | None = None,
):
    ttl = _dashboard_heavy_section_cache_ttl_sec() if ttl_sec is None else max(0.0, float(ttl_sec))
    if ttl <= 0.0:
        return builder()
    cache_key = (_dashboard_database_cache_key(db), key)
    now = time.monotonic()
    with _dashboard_snapshot_cache_lock:
        cached = _dashboard_fragment_cache.get(cache_key)
        if cached is not None and now - cached[0] <= ttl:
            return cached[1]
    value = builder()
    with _dashboard_snapshot_cache_lock:
        _dashboard_fragment_cache[cache_key] = (time.monotonic(), value)
    return value


def _cached_theme_lab_dashboard_snapshot(
    db: TradingDatabase,
    key: str,
    builder,
    *,
    force: bool = False,
    refresh_builder=None,
    ttl_sec: float | None = None,
):
    ttl = _dashboard_snapshot_cache_ttl_sec() if ttl_sec is None else max(0.0, float(ttl_sec))
    if ttl <= 0.0:
        return builder()
    cache_key = (_dashboard_database_cache_key(db), key)
    now = time.monotonic()
    if not force:
        with _theme_lab_dashboard_snapshot_cache_lock:
            cached = _theme_lab_dashboard_snapshot_cache.get(cache_key)
            if cached is not None and now - cached[0] <= ttl:
                return cached[1]
            if cached is not None and refresh_builder is not None:
                _schedule_theme_lab_dashboard_snapshot_refresh_locked(cache_key, refresh_builder)
                return cached[1]
    value = builder()
    with _theme_lab_dashboard_snapshot_cache_lock:
        _theme_lab_dashboard_snapshot_cache[cache_key] = (time.monotonic(), value)
    return value


def _theme_lab_dashboard_snapshot_refresh_executor_instance() -> ThreadPoolExecutor:
    global _theme_lab_dashboard_snapshot_refresh_executor
    if _theme_lab_dashboard_snapshot_refresh_executor is None:
        _theme_lab_dashboard_snapshot_refresh_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="theme-lab-snapshot-refresh",
        )
    return _theme_lab_dashboard_snapshot_refresh_executor


def _schedule_theme_lab_dashboard_snapshot_refresh_locked(cache_key: tuple[str, str], refresh_builder) -> None:
    if cache_key in _theme_lab_dashboard_snapshot_refreshing:
        return
    _theme_lab_dashboard_snapshot_refreshing.add(cache_key)
    _defer_theme_lab_dashboard_snapshot_refresh(cache_key, refresh_builder)


def _defer_theme_lab_dashboard_snapshot_refresh(cache_key: tuple[str, str], refresh_builder) -> None:
    timer = Timer(0.25, _submit_theme_lab_dashboard_snapshot_refresh, args=(cache_key, refresh_builder))
    timer.daemon = True
    timer.start()


def _submit_theme_lab_dashboard_snapshot_refresh(cache_key: tuple[str, str], refresh_builder) -> None:
    try:
        executor = _theme_lab_dashboard_snapshot_refresh_executor_instance()
        executor.submit(_refresh_theme_lab_dashboard_snapshot_cache, cache_key, refresh_builder)
    except Exception:
        with _theme_lab_dashboard_snapshot_cache_lock:
            _theme_lab_dashboard_snapshot_refreshing.discard(cache_key)


def _refresh_theme_lab_dashboard_snapshot_cache(cache_key: tuple[str, str], refresh_builder) -> None:
    try:
        payload = refresh_builder()
        with _theme_lab_dashboard_snapshot_cache_lock:
            _theme_lab_dashboard_snapshot_cache[cache_key] = (time.monotonic(), payload)
    finally:
        with _theme_lab_dashboard_snapshot_cache_lock:
            _theme_lab_dashboard_snapshot_refreshing.discard(cache_key)


def _shutdown_theme_lab_dashboard_snapshot_refresh_executor() -> None:
    global _theme_lab_dashboard_snapshot_refresh_executor
    executor = _theme_lab_dashboard_snapshot_refresh_executor
    _theme_lab_dashboard_snapshot_refresh_executor = None
    with _theme_lab_dashboard_snapshot_cache_lock:
        _theme_lab_dashboard_snapshot_refreshing.clear()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _build_theme_lab_dashboard_snapshot_with_fresh_db(*, include_extended: bool = True) -> dict[str, Any]:
    db = open_database()
    try:
        return build_theme_lab_dashboard_snapshot(
            db,
            runtime_status=runtime_supervisor.status(),
            gateway_state=gateway_state,
            include_extended=include_extended,
        )
    finally:
        close_database(db)


def _build_dashboard_snapshot_payload_uncached(*, detail: str = DASHBOARD_SNAPSHOT_DETAIL_SLIM) -> dict[str, Any]:
    db = open_database()
    try:
        return build_dashboard_snapshot(db, detail=detail)
    finally:
        close_database(db)


def _build_dashboard_snapshot_payload(*, force: bool = False, detail: str = DASHBOARD_SNAPSHOT_DETAIL_SLIM) -> dict[str, Any]:
    global _dashboard_snapshot_cache_payload
    global _dashboard_snapshot_cache_db_path
    global _dashboard_snapshot_cache_monotonic
    global _dashboard_snapshot_cache_build_ms
    global _dashboard_snapshot_cache_hit_count
    global _dashboard_snapshot_cache_miss_count
    resolved_detail = _dashboard_snapshot_detail(detail)
    ttl = _dashboard_snapshot_cache_ttl_sec()
    db_path = f"{_dashboard_database_cache_key()}:{resolved_detail}"
    if ttl <= 0.0:
        return _build_dashboard_snapshot_payload_uncached(detail=resolved_detail)
    now = time.monotonic()
    should_refresh = False
    with _dashboard_snapshot_cache_lock:
        if force and _dashboard_snapshot_cache_payload is not None and _dashboard_snapshot_cache_db_path == db_path:
            _dashboard_snapshot_cache_hit_count += 1
            should_refresh = db_path not in _dashboard_snapshot_cache_refreshing
            if should_refresh:
                _dashboard_snapshot_cache_refreshing.add(db_path)
            stale_payload = _dashboard_snapshot_cache_payload
        elif (
            not force
            and _dashboard_snapshot_cache_payload is not None
            and _dashboard_snapshot_cache_db_path == db_path
            and now - _dashboard_snapshot_cache_monotonic <= ttl
        ):
            _dashboard_snapshot_cache_hit_count += 1
            return _dashboard_snapshot_cache_payload
        elif (
            not force
            and _dashboard_snapshot_cache_payload is not None
            and _dashboard_snapshot_cache_db_path == db_path
        ):
            _dashboard_snapshot_cache_hit_count += 1
            should_refresh = db_path not in _dashboard_snapshot_cache_refreshing
            if should_refresh:
                _dashboard_snapshot_cache_refreshing.add(db_path)
            stale_payload = _dashboard_snapshot_cache_payload
        else:
            stale_payload = None
        _dashboard_snapshot_cache_miss_count += 1
    if stale_payload is not None:
        if should_refresh:
            _submit_dashboard_snapshot_refresh(db_path, resolved_detail)
        return stale_payload
    started = time.perf_counter()
    payload = _build_dashboard_snapshot_payload_uncached(detail=resolved_detail)
    build_ms = (time.perf_counter() - started) * 1000.0
    with _dashboard_snapshot_cache_lock:
        _dashboard_snapshot_cache_build_ms = build_ms
        _dashboard_snapshot_cache_payload = payload
        _dashboard_snapshot_cache_db_path = db_path
        _dashboard_snapshot_cache_monotonic = time.monotonic()
    return payload


def _dashboard_snapshot_refresh_executor_instance() -> ThreadPoolExecutor:
    global _dashboard_snapshot_refresh_executor
    if _dashboard_snapshot_refresh_executor is None:
        _dashboard_snapshot_refresh_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="dashboard-snapshot-refresh",
        )
    return _dashboard_snapshot_refresh_executor


def _submit_dashboard_snapshot_refresh(db_path: str, detail: str) -> None:
    try:
        _dashboard_snapshot_refresh_executor_instance().submit(_refresh_dashboard_snapshot_cache, db_path, detail)
    except Exception:
        with _dashboard_snapshot_cache_lock:
            _dashboard_snapshot_cache_refreshing.discard(db_path)


def _refresh_dashboard_snapshot_cache(db_path: str, detail: str) -> None:
    global _dashboard_snapshot_cache_payload
    global _dashboard_snapshot_cache_db_path
    global _dashboard_snapshot_cache_monotonic
    global _dashboard_snapshot_cache_build_ms
    try:
        started = time.perf_counter()
        payload = _build_dashboard_snapshot_payload_uncached(detail=detail)
        build_ms = (time.perf_counter() - started) * 1000.0
        with _dashboard_snapshot_cache_lock:
            _dashboard_snapshot_cache_payload = payload
            _dashboard_snapshot_cache_db_path = db_path
            _dashboard_snapshot_cache_monotonic = time.monotonic()
            _dashboard_snapshot_cache_build_ms = build_ms
    finally:
        with _dashboard_snapshot_cache_lock:
            _dashboard_snapshot_cache_refreshing.discard(db_path)


def _shutdown_dashboard_snapshot_refresh_executor() -> None:
    global _dashboard_snapshot_refresh_executor
    executor = _dashboard_snapshot_refresh_executor
    _dashboard_snapshot_refresh_executor = None
    with _dashboard_snapshot_cache_lock:
        _dashboard_snapshot_cache_refreshing.clear()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _dashboard_snapshot_for_client_count(payload: dict[str, Any], client_count: int) -> dict[str, Any]:
    snapshot = dict(payload)
    gateway = dict(snapshot.get("gateway") or {})
    gateway["dashboard_snapshot_detail"] = _dashboard_snapshot_detail(snapshot.get("snapshot_detail"))
    gateway["dashboard_ws_client_count"] = int(client_count)
    with _dashboard_snapshot_cache_lock:
        cache_age_sec = max(0.0, time.monotonic() - _dashboard_snapshot_cache_monotonic) if _dashboard_snapshot_cache_payload is not None else 0.0
        gateway["dashboard_snapshot_cache_age_sec"] = round(cache_age_sec, 3)
        gateway["dashboard_snapshot_cache_build_ms"] = round(float(_dashboard_snapshot_cache_build_ms or 0.0), 3)
        gateway["dashboard_snapshot_cache_hit_count"] = int(_dashboard_snapshot_cache_hit_count)
        gateway["dashboard_snapshot_cache_miss_count"] = int(_dashboard_snapshot_cache_miss_count)
    snapshot["gateway"] = gateway
    return snapshot


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
    snapshot = _dashboard_snapshot_for_client_count(payload, dashboard_connections.client_count)
    await dashboard_connections.broadcast_json({"type": "snapshot", "snapshot": snapshot})
    _dashboard_snapshot_last_sent_monotonic = time.monotonic()


def _consume_dashboard_snapshot_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        pass


@app.get("/api/gateway/commands", response_model=GatewayCommandBatch)
async def gateway_commands(
    limit: int = Query(20, ge=1, le=100),
    wait_sec: float = Query(0.0, ge=0.0, le=15.0),
    _: None = Depends(verify_gateway_token),
) -> GatewayCommandBatch:
    poll_received_at = utc_now_ms()
    poll_started = time.perf_counter()
    deadline = asyncio.get_event_loop().time() + wait_sec
    _apply_theme_backfill_dispatch_guard()
    commands = gateway_state.dispatch_commands(limit)
    while not commands and wait_sec > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.25)
        _apply_theme_backfill_dispatch_guard()
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


def _apply_theme_backfill_dispatch_guard() -> None:
    db = open_database()
    try:
        apply_dispatch_guard(
            gateway_state,
            db.latest_theme_lab_flow_result(),
            config=ThemeBackfillConfig.from_env(trading_mode=get_settings().mode),
        )
    finally:
        close_database(db)


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
            snapshot_payload = _dashboard_snapshot_for_client_count(payload, dashboard_connections.client_count)
            await dashboard_connections.send_json(websocket, {"type": "snapshot", "snapshot": snapshot_payload})
            await asyncio.sleep(_dashboard_ws_push_interval_sec())
    except WebSocketDisconnect:
        dashboard_connections.disconnect(websocket)
    except Exception:
        dashboard_connections.disconnect(websocket)


def _core_ws_outbound_queue_max_size() -> int:
    try:
        value = int(os.environ.get("TRADING_CORE_WS_OUTBOUND_QUEUE_SIZE", "1000"))
    except ValueError:
        value = 1000
    return max(1, value)


def _core_ws_send_slow_ms() -> float:
    try:
        value = float(os.environ.get("TRADING_CORE_WS_SEND_SLOW_MS", "1000"))
    except ValueError:
        value = 1000.0
    return max(1.0, value)


def _core_ws_outbound_metadata(queue: asyncio.Queue[_CoreWsOutboundMessage]) -> dict[str, Any]:
    return {
        "core_ws_outbound_queue_size": queue.qsize(),
        "core_ws_outbound_queue_max_size": queue.maxsize,
        "core_ws_last_send_json_ms": float(gateway_ws_transport_state.get("core_ws_last_send_json_ms") or 0.0),
        "core_ws_last_send_queue_wait_ms": float(
            gateway_ws_transport_state.get("core_ws_last_send_queue_wait_ms") or 0.0
        ),
    }


def _queue_core_ws_outbound(
    queue: asyncio.Queue[_CoreWsOutboundMessage],
    payload: dict[str, Any],
    *,
    connection_id: str,
) -> None:
    queued_at = utc_now_ms()
    item = _CoreWsOutboundMessage(
        payload=payload,
        message_type=str(payload.get("type") or ""),
        queued_at=queued_at,
        queued_monotonic_ms=monotonic_ms(),
        connection_id=connection_id,
    )
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull as exc:
        dropped_count = int(gateway_ws_transport_state.get("core_ws_outbound_dropped_count") or 0) + 1
        _update_gateway_ws_transport_state(
            {
                "core_ws_outbound_writer_active": True,
                "core_ws_outbound_queue_size": queue.qsize(),
                "core_ws_outbound_queue_max_size": queue.maxsize,
                "core_ws_outbound_dropped_count": dropped_count,
                "last_error": "CORE_WS_OUTBOUND_QUEUE_FULL",
                "last_error_type": type(exc).__name__,
                "last_error_stage": "core_ws_outbound_enqueue",
                "last_error_at": queued_at,
            }
        )
        raise RuntimeError("CORE_WS_OUTBOUND_QUEUE_FULL") from exc
    _update_gateway_ws_transport_state(
        {
            "core_ws_outbound_writer_active": True,
            "core_ws_outbound_queue_size": queue.qsize(),
            "core_ws_outbound_queue_max_size": queue.maxsize,
            "core_ws_outbound_queued_count": int(gateway_ws_transport_state.get("core_ws_outbound_queued_count") or 0) + 1,
        }
    )


async def _core_ws_outbound_writer_loop(
    websocket: WebSocket,
    queue: asyncio.Queue[_CoreWsOutboundMessage],
) -> None:
    while True:
        item = await queue.get()
        try:
            queue_wait_ms = monotonic_delta_ms(item.queued_monotonic_ms, monotonic_ms()) or 0.0
            send_started = time.perf_counter()
            await websocket.send_json(item.payload)
            send_ms = (time.perf_counter() - send_started) * 1000.0
            sent_at = utc_now_ms()
            patch: dict[str, Any] = {
                "core_ws_outbound_writer_active": True,
                "core_ws_outbound_queue_size": queue.qsize(),
                "core_ws_outbound_queue_max_size": queue.maxsize,
                "core_ws_outbound_sent_count": int(gateway_ws_transport_state.get("core_ws_outbound_sent_count") or 0) + 1,
                "core_ws_last_send_json_ms": send_ms,
                "core_ws_last_send_queue_wait_ms": queue_wait_ms,
                "core_ws_last_send_json_type": item.message_type,
                "core_ws_last_send_json_at": sent_at,
            }
            if send_ms >= _core_ws_send_slow_ms():
                patch.update(
                    {
                        "core_ws_slow_send_count": int(gateway_ws_transport_state.get("core_ws_slow_send_count") or 0) + 1,
                        "core_ws_last_slow_send_json_ms": send_ms,
                        "core_ws_last_slow_send_at": sent_at,
                    }
                )
            _update_gateway_ws_transport_state(patch)
        except Exception as exc:
            _update_gateway_ws_transport_state(
                {
                    "core_ws_outbound_writer_active": False,
                    "core_ws_outbound_queue_size": queue.qsize(),
                    "last_error": _truncate_log_detail(str(exc) or repr(exc)),
                    "last_error_type": type(exc).__name__,
                    "last_error_stage": "core_ws_send_json",
                    "last_error_at": utc_now_ms(),
                }
            )
            raise
        finally:
            queue.task_done()


async def _stop_core_ws_outbound_writer(
    task: asyncio.Task,
    queue: asyncio.Queue[_CoreWsOutboundMessage],
) -> None:
    if not task.done():
        try:
            await asyncio.wait_for(queue.join(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    _update_gateway_ws_transport_state(
        {
            "core_ws_outbound_writer_active": False,
            "core_ws_outbound_queue_size": 0,
            "core_ws_outbound_queue_max_size": queue.maxsize,
        }
    )


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
    outbound_queue: asyncio.Queue[_CoreWsOutboundMessage] = asyncio.Queue(maxsize=_core_ws_outbound_queue_max_size())
    outbound_writer_task = asyncio.create_task(_core_ws_outbound_writer_loop(websocket, outbound_queue))
    _update_gateway_ws_transport_state(
        {
            "connected": True,
            "state": "CONNECTED",
            "transport_mode": connection_transport_mode,
            "ws_session_id": session_id,
            "ws_connection_id": connection_id,
            "core_ws_outbound_writer_active": True,
            "core_ws_outbound_queue_size": 0,
            "core_ws_outbound_queue_max_size": outbound_queue.maxsize,
        }
    )
    _queue_core_ws_outbound(
        outbound_queue,
        GatewayWsMessage(
            type="hello_ack",
            source="core",
            payload={
                "transport_mode": TRANSPORT_MODE_WEBSOCKET_MOCK,
                "websocket_session_id": session_id,
                "real_gateway_switch_ready": False,
            },
            metadata={"connection_id": connection_id, "websocket_session_id": session_id},
        ).to_dict(),
        connection_id=connection_id,
    )
    last_receive_done_monotonic = monotonic_ms()
    try:
        while True:
            if outbound_writer_task.done():
                await outbound_writer_task
            receive_ready_monotonic = monotonic_ms()
            receive_loop_gap_ms = monotonic_delta_ms(last_receive_done_monotonic, receive_ready_monotonic) or 0.0
            receive_started = time.perf_counter()
            raw_text = await websocket.receive_text()
            receive_ms = (time.perf_counter() - receive_started) * 1000.0
            last_receive_done_monotonic = monotonic_ms()
            _update_gateway_ws_transport_state(
                {
                    "core_ws_last_receive_text_ms": receive_ms,
                    "core_ws_receive_loop_gap_ms": receive_loop_gap_ms,
                    "core_ws_outbound_queue_size": outbound_queue.qsize(),
                    "core_ws_outbound_queue_max_size": outbound_queue.maxsize,
                }
            )
            try:
                raw = json.loads(raw_text)
                if not isinstance(raw, dict):
                    raise ValueError("websocket message must be a JSON object")
                message = GatewayWsMessage.from_dict(raw)
            except Exception as exc:
                metadata = {
                    "connection_id": connection_id,
                    "websocket_session_id": session_id,
                    "ws_connection_id": connection_id,
                    "ws_session_id": session_id,
                    "transport_mode": connection_transport_mode,
                    "ws_receive_ms": receive_ms,
                    "ws_message_sequence": sequence,
                    "core_ws_receive_loop_gap_ms": receive_loop_gap_ms,
                    **_core_ws_outbound_metadata(outbound_queue),
                }
                _record_gateway_ws_protocol_error(
                    exc,
                    stage="receive_decode",
                    connection_transport_mode=connection_transport_mode,
                    session_id=session_id,
                    connection_id=connection_id,
                )
                await _send_gateway_ws_error(
                    websocket,
                    code="BAD_MESSAGE",
                    message="invalid websocket message",
                    metadata=metadata,
                    sequence=sequence,
                    outbound_queue=outbound_queue,
                    connection_id=connection_id,
                )
                continue
            if gateway_ws_transport_state.get("state") == "PROTOCOL_ERROR":
                _update_gateway_ws_transport_state(
                    {
                        "connected": True,
                        "state": "CONNECTED",
                        "transport_mode": connection_transport_mode,
                        "ws_session_id": session_id,
                        "ws_connection_id": connection_id,
                    }
                )
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
                "core_ws_receive_loop_gap_ms": receive_loop_gap_ms,
                **_core_ws_outbound_metadata(outbound_queue),
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
                _queue_core_ws_outbound(
                    outbound_queue,
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
                    ).to_dict(),
                    connection_id=connection_id,
                )
            elif message.type == "ping":
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="pong",
                        trace_id=message.trace_id,
                        source="core",
                        payload={"received_at": utc_now_ms()},
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
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
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="core_command_batch",
                        trace_id=message.trace_id,
                        source="core",
                        payload={"commands": payloads, "count": len(payloads), "timestamp": sent_at},
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
                )
            elif message.type == "transport_heartbeat":
                event = _gateway_event_from_ws_message(message, metadata=metadata)
                _record_core_ws_fast_status_hint(event, metadata)
                if _ws_pilot_diagnostic_signature(dict(event.payload or {})):
                    queue_result = _enqueue_core_ws_event_work(
                        kind="transport_heartbeat",
                        event=event,
                        metadata=metadata,
                    )
                else:
                    queue_result = {
                        "accepted": True,
                        "queued": False,
                        "reason": "FAST_STATUS_ONLY",
                        "queue_size": _core_ws_event_total_queue_size(),
                        "queue_max_size": _core_ws_event_queue_max_size(),
                    }
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=event.event_id,
                        command_id=event.command_id,
                        payload={
                            "accepted": bool(queue_result.get("accepted")),
                            "event_id": event.event_id,
                            "type": event.type,
                            "transport_only": True,
                            "queued": bool(queue_result.get("queued")),
                            "queue_size": int(queue_result.get("queue_size") or 0),
                            "queue_max_size": int(queue_result.get("queue_max_size") or 0),
                            "reason": str(queue_result.get("reason") or ""),
                        },
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
                )
            elif message.type == "transport_send_completed":
                if _gateway_ws_send_completed_sample_expected(message, metadata):
                    queue_result = _enqueue_core_ws_event_work(
                        kind="send_completed",
                        message=message,
                        metadata=metadata,
                    )
                else:
                    record_result = _record_gateway_ws_send_completed(message, metadata)
                    queue_result = {
                        "accepted": True,
                        "queued": False,
                        "reason": str(record_result.get("reason") or "UNSAMPLED_SEND_COMPLETED"),
                        "queue_size": _core_ws_event_total_queue_size(),
                        "queue_max_size": _core_ws_event_queue_max_size(),
                        **record_result,
                    }
                result = {
                    "accepted": bool(queue_result.get("accepted")),
                    "type": "transport_send_completed",
                    "queued": bool(queue_result.get("queued")),
                    "updated": False,
                    "skipped": bool(queue_result.get("skipped")),
                    "queue_size": int(queue_result.get("queue_size") or 0),
                    "queue_max_size": int(queue_result.get("queue_max_size") or 0),
                    "reason": str(queue_result.get("reason") or ""),
                }
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=message.event_id,
                        command_id=message.command_id,
                        payload=result,
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
                )
            elif message.type == "condition_event_batch":
                events = _gateway_events_from_ws_batch_message(message, metadata=metadata)
                if _gateway_condition_event_async_enabled():
                    queue_result = _enqueue_gateway_condition_events(events, metadata)
                    accepted_count = int(queue_result.get("queued_count") or 0)
                    ack_payload = {
                        "accepted": bool(queue_result.get("accepted")),
                        "queued": bool(queue_result.get("queued")),
                        "type": "condition_event_batch",
                        "batch_id": message.payload.get("batch_id") or message.event_id,
                        "count": len(events),
                        "accepted_count": accepted_count,
                        "queued_count": accepted_count,
                        "coalesced_count": int(queue_result.get("coalesced_count") or 0),
                        "dropped_count": int(queue_result.get("dropped_count") or 0),
                        "queue_size": int(queue_result.get("queue_size") or 0),
                        "queue_batch_count": int(queue_result.get("queue_batch_count") or 0),
                        "queue_max_size": int(queue_result.get("queue_max_size") or 0),
                        "reason": str(queue_result.get("reason") or ""),
                        "event_ids": [event.event_id for event in events],
                    }
                else:
                    results = []
                    for event in events:
                        _record_ws_message_side_effects(event, metadata)
                        results.append(await _process_gateway_event(event))
                    accepted_count = sum(1 for result in results if result.get("accepted"))
                    ack_payload = {
                        "accepted": accepted_count == len(events),
                        "queued": False,
                        "type": "condition_event_batch",
                        "batch_id": message.payload.get("batch_id") or message.event_id,
                        "count": len(events),
                        "accepted_count": accepted_count,
                        "queued_count": 0,
                        "dropped_count": 0,
                        "event_ids": [str(result.get("event_id") or "") for result in results],
                    }
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=message.event_id,
                        command_id=message.command_id,
                        payload=ack_payload,
                        metadata={**metadata, "condition_event_batch_count": len(events)},
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
                )
            elif message.type in {"gateway_event", "heartbeat", "command_started", "command_ack", "command_failed", "rate_limited"}:
                event = _gateway_event_from_ws_message(message, metadata=metadata)
                _record_core_ws_fast_status_hint(event, metadata)
                if event.type == "heartbeat" and not _core_ws_heartbeat_queue_required(event):
                    result = {
                        "accepted": True,
                        "event_id": event.event_id,
                        "type": event.type,
                        "queued": False,
                        "coalesced": False,
                        "queue_size": _core_ws_event_total_queue_size(),
                        "queue_max_size": _core_ws_event_queue_max_size(),
                        "reason": "FAST_STATUS_ONLY",
                    }
                elif event.type == "condition_event" and _gateway_condition_event_async_enabled():
                    queue_result = _enqueue_gateway_condition_events([event], metadata)
                    result = {
                        "accepted": bool(queue_result.get("accepted")),
                        "event_id": event.event_id,
                        "type": event.type,
                        "queued": bool(queue_result.get("queued")),
                        "queued_count": int(queue_result.get("queued_count") or 0),
                        "coalesced_count": int(queue_result.get("coalesced_count") or 0),
                        "dropped_count": int(queue_result.get("dropped_count") or 0),
                        "queue_size": int(queue_result.get("queue_size") or 0),
                        "queue_max_size": int(queue_result.get("queue_max_size") or 0),
                        "reason": str(queue_result.get("reason") or ""),
                    }
                else:
                    queue_result = _enqueue_core_ws_event_work(
                        kind="gateway_event",
                        event=event,
                        metadata=metadata,
                    )
                    result = {
                        "accepted": bool(queue_result.get("accepted")),
                        "event_id": event.event_id,
                        "type": event.type,
                        "queued": bool(queue_result.get("queued")),
                        "coalesced": bool(queue_result.get("coalesced")),
                        "queue_size": int(queue_result.get("queue_size") or 0),
                        "queue_max_size": int(queue_result.get("queue_max_size") or 0),
                        "reason": str(queue_result.get("reason") or ""),
                    }
                _queue_core_ws_outbound(
                    outbound_queue,
                    GatewayWsMessage(
                        type="event_ack",
                        trace_id=message.trace_id,
                        source="core",
                        event_id=result.get("event_id", ""),
                        command_id=event.command_id,
                        payload=result,
                        metadata=metadata,
                        sequence=sequence,
                    ).to_dict(),
                    connection_id=connection_id,
                )
            else:
                event = GatewayEvent(
                    type="transport_error",
                    source=message.source,
                    payload={"message": f"unsupported websocket message type: {message.type}", "metadata": metadata},
                )
                await _process_gateway_event(event)
                await _send_gateway_ws_error(
                    websocket,
                    code="UNSUPPORTED_MESSAGE_TYPE",
                    message=f"unsupported websocket message type: {message.type}",
                    metadata=metadata,
                    trace_id=message.trace_id,
                    sequence=sequence,
                    outbound_queue=outbound_queue,
                    connection_id=connection_id,
                )
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
    except Exception as exc:
        _record_gateway_ws_protocol_error(
            exc,
            stage="connection_loop",
            connection_transport_mode=connection_transport_mode,
            session_id=session_id,
            connection_id=connection_id,
        )
        _update_gateway_ws_transport_state(
            {
                "connected": False,
                "state": "ERROR",
                "transport_mode": connection_transport_mode,
                "ws_session_id": session_id,
                "ws_connection_id": connection_id,
            }
        )
        return
    finally:
        await _stop_core_ws_outbound_writer(outbound_writer_task, outbound_queue)


def _dashboard_field_subset(payload: dict[str, Any], fields: tuple[str, ...] | list[str]) -> dict[str, Any]:
    return {field: payload.get(field) for field in fields if field in payload}


def _dashboard_slim_gateway_payload(payload: dict[str, Any]) -> dict[str, Any]:
    gateway = _dashboard_field_subset(
        dict(payload or {}),
        (
            "connection_state",
            "connected",
            "kiwoom_logged_in",
            "orderable",
            "mode",
            "account",
            "last_heartbeat_at",
            "last_event_at",
            "last_error",
            "heartbeat_timeout_sec",
            "heartbeat_age_sec",
            "heartbeat_ok",
            "pending_command_count",
            "received_event_count",
            "deduped_event_count",
            "reconnect_count",
            "gateway_client_id",
        ),
    )
    heartbeat = dict((payload or {}).get("last_heartbeat_payload") or {})
    if heartbeat:
        gateway["last_heartbeat_summary"] = _dashboard_field_subset(
            heartbeat,
            (
                "transport_mode",
                "ws_connection_state",
                "ws_session_id",
                "ws_reconnect_count",
                "ws_fallback_reason",
                "gateway_event_queue_size",
                "gateway_command_queue_size",
            ),
        )
    return gateway


def _dashboard_slim_command_record(record: dict[str, Any]) -> dict[str, Any]:
    command = dict(record.get("command") or {})
    return {
        "command_id": record.get("command_id") or command.get("command_id") or "",
        "command_type": record.get("command_type") or command.get("type") or "",
        "status": record.get("status") or "",
        "priority": record.get("priority") or "",
        "created_at": record.get("created_at") or command.get("timestamp") or "",
        "dispatched_at": record.get("dispatched_at") or "",
        "acked_at": record.get("acked_at") or "",
        "finished_at": record.get("finished_at") or "",
        "expires_at": record.get("expires_at") or "",
        "attempts": record.get("attempts") or 0,
        "max_attempts": record.get("max_attempts") or 0,
        "last_error": record.get("last_error") or "",
        "source": record.get("source") or command.get("source") or "",
    }


def _dashboard_slim_commands_payload(payload: dict[str, Any], *, recent_limit: int = 5) -> dict[str, Any]:
    commands = dict(payload or {})
    commands["recent"] = [
        _dashboard_slim_command_record(dict(item or {}))
        for item in list(commands.get("recent") or [])[:recent_limit]
    ]
    return commands


def _dashboard_slim_themes_payload(payload: dict[str, Any], *, item_limit: int = 20) -> dict[str, Any]:
    return {
        "summary": dict((payload or {}).get("summary") or {}),
        "items": [
            _dashboard_field_subset(
                dict(item or {}),
                ("rank", "theme_id", "theme_name", "theme_score", "breadth", "leader_gap", "top3_concentration", "status"),
            )
            for item in list((payload or {}).get("items") or [])[:item_limit]
        ],
    }


def _dashboard_slim_orders_payload(payload: dict[str, Any], *, item_limit: int = 10) -> dict[str, Any]:
    order_results = []
    for item in list((payload or {}).get("order_results") or [])[:item_limit]:
        row = dict(item or {})
        request = dict(row.get("request") or {})
        order_results.append(
            {
                "id": row.get("id"),
                "created_at": row.get("created_at") or "",
                "ok": bool(row.get("ok")),
                "result_code": row.get("result_code") or "",
                "message": row.get("message") or "",
                "request": _dashboard_field_subset(request, ("code", "side", "quantity", "price", "order_type", "tag")),
            }
        )
    return {
        "summary": dict((payload or {}).get("summary") or {}),
        "order_results": order_results,
        "executions": [
            _dashboard_field_subset(
                dict(item or {}),
                (
                    "id",
                    "created_at",
                    "code",
                    "order_no",
                    "side",
                    "quantity",
                    "price",
                    "filled_quantity",
                    "remaining_quantity",
                    "tag",
                ),
            )
            for item in list((payload or {}).get("executions") or [])[:item_limit]
        ],
        "positions": list((payload or {}).get("positions") or [])[:item_limit],
        "virtual_orders": [
            _dashboard_field_subset(
                dict(item or {}),
                ("id", "candidate_id", "entry_plan_id", "leg_index", "weight_pct", "status", "limit_price", "submitted_at"),
            )
            for item in list((payload or {}).get("virtual_orders") or [])[:item_limit]
        ],
    }


def _dashboard_slim_reviews_payload(payload: dict[str, Any], *, item_limit: int = 10) -> dict[str, Any]:
    review_fields = (
        "id",
        "candidate_id",
        "code",
        "trade_date",
        "final_status",
        "max_return_5m",
        "max_return_10m",
        "max_return_20m",
        "false_positive_flag",
        "false_negative_flag",
        "blocked_but_later_rallied",
        "expired_but_later_rallied",
    )
    return {
        "summary": dict((payload or {}).get("summary") or {}),
        "items": [
            _dashboard_field_subset(dict(item or {}), review_fields)
            for item in list((payload or {}).get("items") or [])[:item_limit]
        ],
    }


def _dashboard_slim_theme_lab_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data_quality = dict((payload or {}).get("data_quality") or {})
    backfill_runtime = dict((payload or {}).get("theme_backfill_runtime") or {})
    return {
        "available": bool((payload or {}).get("available")),
        "source": (payload or {}).get("source") or "",
        "created_at": (payload or {}).get("created_at") or "",
        "calculated_at": (payload or {}).get("calculated_at") or "",
        "last_updated_at": (payload or {}).get("last_updated_at") or "",
        "summary": dict((payload or {}).get("summary") or {}),
        "operator_view": dict((payload or {}).get("operator_view") or {}),
        "data_quality": _dashboard_field_subset(
            data_quality,
            (
                "status",
                "raw_status",
                "display_status",
                "message",
                "raw_message",
                "display_message",
                "watchset_size",
                "stale",
                "age_sec",
                "snapshot_age_sec",
                "calculated_age_sec",
                "vi_status_supported",
                "condition_coverage_pct",
                "candle_missing_count",
            ),
        ),
        "runtime": _dashboard_field_subset(dict((payload or {}).get("runtime") or {}), ("enabled", "running", "mode")),
        "theme_backfill_runtime": _dashboard_field_subset(
            backfill_runtime,
            (
                "enabled",
                "observe_only",
                "paused_reason",
                "queued_count",
                "dispatched_count",
                "parser_miss_ratio",
                "tr_backfill_caused_ready_count",
                "gateway_command_queue_depth",
                "load_guard",
                "load_guard_status",
                "paused_backfill",
                "pause_reason_codes",
            ),
        ),
        "gateway": _dashboard_field_subset(
            dict((payload or {}).get("gateway") or {}),
            ("connected", "heartbeat_ok", "kiwoom_logged_in", "orderable", "connection_state"),
        ),
    }


def _dashboard_slim_logs_payload(payload: dict[str, Any], *, item_limit: int = 40) -> dict[str, Any]:
    gateway_limit = max(10, item_limit // 4)
    return {
        "core": list((payload or {}).get("core") or [])[:item_limit],
        "gateway": [
            _dashboard_slim_gateway_log_event(dict(item or {}))
            for item in list((payload or {}).get("gateway") or [])[:gateway_limit]
        ],
        "items": [
            _dashboard_slim_log_item(dict(item or {}))
            for item in list((payload or {}).get("items") or [])[:item_limit]
        ],
        "warnings": list((payload or {}).get("warnings") or [])[:10],
        "timezone": (payload or {}).get("timezone") or "Asia/Seoul",
        "live_window_sec": (payload or {}).get("live_window_sec") or LOG_LIVE_WINDOW_SEC,
        "stale_core_log_count": (payload or {}).get("stale_core_log_count") or 0,
        "hidden_gateway_event_counts": dict((payload or {}).get("hidden_gateway_event_counts") or {}),
    }


def _dashboard_slim_log_item(item: dict[str, Any]) -> dict[str, Any]:
    slim = _dashboard_field_subset(
        item,
        ("source", "timestamp", "timestamp_utc", "type", "line"),
    )
    event = dict(item.get("event") or {})
    if event:
        slim["event"] = _dashboard_slim_gateway_log_event(event)
    return slim


def _dashboard_slim_gateway_log_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload") or {})
    slim = _dashboard_field_subset(
        event,
        ("type", "event_id", "request_id", "timestamp", "source", "command_id", "idempotency_key"),
    )
    payload_summary = _dashboard_field_subset(
        payload,
        (
            "command_type",
            "status",
            "result_code",
            "error",
            "message",
            "code",
            "screen_no",
            "condition_name",
            "order_no",
            "broker_order_id",
            "transport_mode",
        ),
    )
    if payload_summary:
        slim["payload"] = payload_summary
    return slim


def _dashboard_slim_intraday_decisions_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    result = _dashboard_field_subset(
        data,
        ("trade_date", "window_sec", "event_count", "funnel", "major_reason_distribution", "readiness"),
    )
    for key in ("top_block_reasons", "top_wait_reasons", "top_data_quality_issues"):
        result[key] = list(data.get(key) or [])[:10]
    return result


def _dashboard_slim_shadow_strategies_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    result = _dashboard_field_subset(
        data,
        (
            "trade_date",
            "window_sec",
            "horizon_sec",
            "policy_id",
            "total_evaluations",
            "changed_decision_count",
            "by_policy",
            "by_change_type",
            "by_shadow_gate_status",
            "by_outcome_label",
            "baseline_ready_count",
            "shadow_ready_count",
            "ready_delta",
            "estimated_opportunity_loss_reduced_count",
            "estimated_false_positive_increase_count",
            "estimated_risk_block_effective_count",
            "estimated_exit_too_late_reduced_count",
        ),
    )
    result["policy_ranking"] = [
        _dashboard_field_subset(
            dict(item or {}),
            (
                "policy_id",
                "policy_name",
                "label_ko",
                "total_count",
                "changed_decision_count",
                "ready_delta",
                "estimated_net_benefit_score",
                "confidence",
                "recommendation_grade",
            ),
        )
        for item in list(data.get("policy_ranking") or [])[:5]
    ]
    result["data_quality_issues"] = list(data.get("data_quality_issues") or [])[:10]
    return result


def _dashboard_slim_threshold_ab_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    return {
        "generated_at": data.get("generated_at", ""),
        "trade_date": data.get("trade_date", ""),
        "report_id": data.get("report_id", ""),
        "summary": dict(data.get("summary") or {}),
        "recommendations": [
            _dashboard_field_subset(
                dict(item or {}),
                ("metric", "category", "recommendation", "label_ko", "message_ko", "candidate_count"),
            )
            for item in list(data.get("recommendations") or [])[:3]
        ],
        "candidates": [
            _dashboard_field_subset(
                dict(item or {}),
                ("candidate_id", "code", "name", "category", "metric", "recommendation", "score"),
            )
            for item in list(data.get("candidates") or [])[:3]
        ],
        "disclaimer_ko": data.get("disclaimer_ko", ""),
    }


def _dashboard_slim_report_list_payload(
    payload: dict[str, Any],
    *,
    item_limit: int = 10,
) -> dict[str, Any]:
    data = dict(payload or {})
    result = dict(data)
    for key in (
        "items",
        "candidates",
        "traces",
        "recent",
        "ready_not_ordered_items",
        "observe_blocked_after_rally_items",
        "missed_opportunity_items",
        "early_small_candidates",
    ):
        if key in result and isinstance(result[key], list):
            result[key] = list(result[key])[:item_limit]
    for key in ("ready_not_ordered_report", "observe_blocked_after_rally", "live_sim_blocked"):
        section = result.get(key)
        if isinstance(section, dict):
            section_copy = dict(section)
            if isinstance(section_copy.get("items"), list):
                section_copy["items"] = list(section_copy["items"])[:item_limit]
            result[key] = section_copy
    return result


def _dashboard_slim_buy_zero_rca_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _dashboard_slim_report_list_payload(payload, item_limit=10)


def _dashboard_slim_conservative_reason_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _dashboard_slim_report_list_payload(payload, item_limit=10)


def _dashboard_slim_shadow_small_entry_promotion_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _dashboard_slim_report_list_payload(payload, item_limit=10)


def _dashboard_slim_strategy_replay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    latest = dict(data.get("latest") or {})
    latest_summary = dict(latest.get("summary") or {})
    return {
        "latest": _dashboard_field_subset(
            latest,
            (
                "report_id",
                "trade_date",
                "generated_at",
                "status",
                "mode",
                "source_db_path",
                "replay_db_path",
            ),
        ),
        "recent_runs": [
            _dashboard_field_subset(
                dict(item or {}),
                ("replay_id", "trade_date", "status", "created_at", "started_at", "finished_at", "replay_db_path"),
            )
            for item in list(data.get("recent_runs") or [])[:5]
        ],
        "summary": _dashboard_field_subset(
            dict(data.get("summary") or {}),
            (
                "total_count",
                "candidate_count",
                "baseline_ready_count",
                "shadow_ready_count",
                "changed_decision_count",
                "opportunity_loss_count",
                "false_positive_count",
                "warning_count",
            ),
        ),
        "funnel": dict(data.get("funnel") or {}),
        "shadow_ranking": [
            _dashboard_field_subset(
                dict(item or {}),
                ("policy_id", "policy_name", "recommendation_grade", "score", "confidence", "message_ko"),
            )
            for item in list(data.get("shadow_ranking") or [])[:5]
        ],
        "data_quality": list(data.get("data_quality") or latest_summary.get("warnings") or [])[:10],
        "diff_summary": dict(data.get("diff_summary") or {}),
    }


def _dashboard_slim_runtime_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(payload or {})
    if "dry_run_orders" in runtime:
        runtime["dry_run_orders"] = {"summary": dict((runtime.get("dry_run_orders") or {}).get("summary") or {})}
    if "intraday_decisions" in runtime:
        runtime["intraday_decisions"] = _dashboard_field_subset(
            dict(runtime.get("intraday_decisions") or {}),
            ("trade_date", "window_sec", "event_count", "funnel", "readiness"),
        )
    if "intraday_outcomes" in runtime:
        runtime["intraday_outcomes"] = _dashboard_field_subset(
            dict(runtime.get("intraday_outcomes") or {}),
            (
                "trade_date",
                "window_sec",
                "horizon_sec",
                "total_decisions",
                "outcome_count",
                "labeled_count",
                "insufficient_count",
                "by_label",
                "ready_count",
            ),
        )
    if "buy_zero_rca" in runtime:
        data = dict(runtime.get("buy_zero_rca") or {})
        runtime["buy_zero_rca"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "trade_date",
                "generated_at",
                "summary",
            ),
        )
    if "live_sim_audit" in runtime:
        data = dict(runtime.get("live_sim_audit") or {})
        runtime["live_sim_audit"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "trade_date",
                "generated_at",
                "summary",
                "exit_guard_ready",
            ),
        )
    if "live_sim_canary_performance" in runtime:
        data = dict(runtime.get("live_sim_canary_performance") or {})
        runtime["live_sim_canary_performance"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "generated_at",
                "trade_date",
                "summary",
                "recommendations",
                "cases",
                "disclaimer_ko",
            ),
        )
    if "exit_policy_validation" in runtime:
        data = dict(runtime.get("exit_policy_validation") or {})
        runtime["exit_policy_validation"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "generated_at",
                "trade_date",
                "summary",
                "actual_exit_quality",
                "scenario_summary",
                "cases",
                "recommendations",
                "disclaimer_ko",
                "analysis_only",
            ),
        )
    if "live_sim_preflight" in runtime:
        data = dict(runtime.get("live_sim_preflight") or {})
        runtime["live_sim_preflight"] = _dashboard_field_subset(
            data,
            (
                "snapshot_id",
                "status",
                "blocking_reasons",
                "warning_reasons",
                "operator_message_ko",
                "recommended_action_ko",
                "checked_at",
                "account_mode_summary",
                "performance_summary",
                "gateway_load_summary",
                "backfill_summary",
                "safety_summary",
            ),
        )
    if "conservative_reason_outcomes" in runtime:
        data = dict(runtime.get("conservative_reason_outcomes") or {})
        runtime["conservative_reason_outcomes"] = _dashboard_field_subset(
            data,
            ("available", "status", "trade_date", "generated_at", "summary", "warnings"),
        )
    if "shadow_small_entry_promotion" in runtime:
        data = dict(runtime.get("shadow_small_entry_promotion") or {})
        runtime["shadow_small_entry_promotion"] = _dashboard_field_subset(
            data,
            (
                "available",
                "enabled",
                "mode",
                "order_enabled",
                "status",
                "source_report_trade_date",
                "candidate_count",
                "observe_only_count",
                "promoted_count",
                "blocked_count",
                "submitted_count",
                "summary",
                "warnings",
                "last_updated_at",
            ),
        )
    if "shadow_small_entry_ops" in runtime:
        data = dict(runtime.get("shadow_small_entry_ops") or {})
        runtime["shadow_small_entry_ops"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "mode",
                "order_enabled",
                "preflight_status",
                "preflight_blocking_reasons",
                "activation_armed",
                "activation_expires_at",
                "last_status_change_at",
                "today",
                "limits",
                "audit",
                "warnings",
                "operator_message_ko",
                "last_updated_at",
            ),
        )
    if "shadow_small_entry_pilot" in runtime:
        data = dict(runtime.get("shadow_small_entry_pilot") or {})
        runtime["shadow_small_entry_pilot"] = _dashboard_field_subset(
            data,
            (
                "available",
                "status",
                "mode",
                "order_enabled",
                "preflight_status",
                "preflight_blocking_reasons",
                "today",
                "audit",
                "warnings",
                "operator_message_ko",
                "last_updated_at",
            ),
        )
    if "shadow_strategies" in runtime:
        runtime["shadow_strategies"] = _dashboard_field_subset(
            dict(runtime.get("shadow_strategies") or {}),
            (
                "trade_date",
                "window_sec",
                "horizon_sec",
                "total_evaluations",
                "changed_decision_count",
                "baseline_ready_count",
                "shadow_ready_count",
                "ready_delta",
                "by_policy",
                "by_change_type",
            ),
        )
    if "strategy_replay" in runtime:
        runtime["strategy_replay"] = _dashboard_field_subset(
            dict(runtime.get("strategy_replay") or {}),
            ("latest", "summary", "funnel", "data_quality", "diff_summary"),
        )
    if "change_proposals" in runtime:
        data = dict(runtime.get("change_proposals") or {})
        runtime["change_proposals"] = {
            "summary": dict(data.get("summary") or {}),
            "top_recommendations": list(data.get("top_recommendations") or [])[:3],
        }
    if "dry_run_performance" in runtime:
        data = dict(runtime.get("dry_run_performance") or {})
        runtime["dry_run_performance"] = _dashboard_field_subset(
            data,
            (
                "generated_at",
                "trade_date",
                "total_lifecycle_count",
                "completed_lifecycle_count",
                "win_rate",
                "avg_realized_return_pct",
                "net_expectancy",
                "net_win_rate",
                "false_positive_count",
                "false_negative_count",
                "opportunity_loss_count",
                "cost_adjusted_bad_ready_count",
                "cost_adjusted_bad_ready_rate",
                "cost_adjusted_opportunity_loss_count",
                "cost_adjusted_opportunity_loss_rate",
                "live_would_pass_win_rate",
                "live_would_reject_but_rallied_count",
                "primary_cost_assumption",
                "cost_scenario_expectancy",
                "execution_realism",
                "go_no_go",
            ),
        )
    if "threshold_ab" in runtime:
        data = dict(runtime.get("threshold_ab") or {})
        runtime["threshold_ab"] = {
            "generated_at": data.get("generated_at", ""),
            "trade_date": data.get("trade_date", ""),
            "report_id": data.get("report_id", ""),
            "summary": dict(data.get("summary") or {}),
            "recommendations": list(data.get("recommendations") or [])[:1],
        }
    return runtime


def _dashboard_dry_run_performance_trade_date(db: TradingDatabase) -> str:
    try:
        row = db.conn.execute(
            """
            SELECT MAX(trade_date) AS trade_date
            FROM (
                SELECT trade_date FROM runtime_order_intents WHERE COALESCE(trade_date, '') != ''
                UNION ALL
                SELECT trade_date FROM trade_reviews WHERE COALESCE(trade_date, '') != ''
            )
            """
        ).fetchone()
        return str(row["trade_date"] or "") if row else ""
    except Exception:
        return ""


def build_dashboard_snapshot(db: TradingDatabase, *, detail: str = DASHBOARD_SNAPSHOT_DETAIL_SLIM) -> dict[str, Any]:
    resolved_detail = _dashboard_snapshot_detail(detail)
    full_detail = resolved_detail == DASHBOARD_SNAPSHOT_DETAIL_FULL
    buy_zero_limit = 50000 if full_detail else 250
    live_sim_audit_limit = 2000 if full_detail else 250
    heavy_report_limit = 10000 if full_detail else 100
    performance_limit = 500 if full_detail else 120
    status_payload = api_status()
    commands_payload = dict(status_payload["commands"])
    commands_payload["recent"] = gateway_state.list_commands(limit=12 if full_detail else 5, include_finished=True)
    if not full_detail:
        commands_payload = _dashboard_slim_commands_payload(commands_payload, recent_limit=5)
    candidates_payload = build_candidates_snapshot(db, limit=200 if full_detail else 40)
    themes_payload = _cached_dashboard_fragment(
        db,
        "themes:v2:50" if full_detail else "themes:v2:20",
        lambda: build_themes_snapshot(db, limit=50 if full_detail else 20),
    )
    if not full_detail:
        themes_payload = _dashboard_slim_themes_payload(themes_payload, item_limit=20)
    orders_payload = build_orders_snapshot(db, limit=100 if full_detail else 10)
    if not full_detail:
        orders_payload = _dashboard_slim_orders_payload(orders_payload, item_limit=10)
    reviews_payload = _cached_dashboard_fragment(
        db,
        "reviews:v2:100" if full_detail else "reviews:v2:10",
        lambda: build_reviews_snapshot(db, limit=100 if full_detail else 10),
    )
    if not full_detail:
        reviews_payload = _dashboard_slim_reviews_payload(reviews_payload, item_limit=10)
    logs_payload = build_logs_snapshot(db, limit=100 if full_detail else 40)
    if not full_detail:
        logs_payload = _dashboard_slim_logs_payload(logs_payload, item_limit=40)
    transport_payload = dict(status_payload.get("transport") or _transport_dashboard_payload(_transport_status_payload(db)))
    transport_experiment_payload = _transport_experiment_dashboard_payload(db)
    runtime_status = runtime_supervisor.status()
    runtime_payload = _runtime_dashboard_payload(runtime_status)
    dry_run_orders_payload = {
        "summary": db.runtime_order_intent_summary(),
        "items": db.list_runtime_order_intents(limit=12) if full_detail else [],
        "recent_sell": db.list_runtime_order_intents(side="sell", order_phase="exit", limit=12) if full_detail else [],
    }
    today = datetime.now().date().isoformat()
    decision_summary_payload = _cached_dashboard_fragment(
        db,
        f"intraday_decisions:v2:{today}",
        lambda: db.strategy_decision_summary(trade_date=today),
    )
    outcome_summary_payload = _cached_dashboard_fragment(
        db,
        f"intraday_outcomes:v2:{today}",
        lambda: db.strategy_decision_outcome_summary(trade_date=today),
    )
    buy_zero_rca_payload = _cached_dashboard_fragment(
        db,
        f"buy_zero_rca:v2:{today}:{buy_zero_limit}",
        lambda: _buy_zero_rca_analyzer(db).build_summary(
            trade_date=today,
            limit=buy_zero_limit,
            include_missed_opportunities=False,
        ),
    )
    live_sim_audit_payload = _cached_dashboard_fragment(
        db,
        f"live_sim_audit:v2:{today}:{live_sim_audit_limit}",
        lambda: _live_sim_auditor(db).build_report(trade_date=today, limit=live_sim_audit_limit),
    )
    live_sim_canary_performance_report = _cached_dashboard_fragment(
        db,
        f"live_sim_canary_performance:v1:{today}:{performance_limit}",
        lambda: _live_sim_canary_performance_analyzer(db).build_report(trade_date=today, limit=performance_limit),
    )
    live_sim_canary_performance_payload = {
        "available": bool(live_sim_canary_performance_report.get("summary", {}).get("total_lifecycle_count")),
        "status": live_sim_canary_performance_report.get("status", "READY"),
        "generated_at": live_sim_canary_performance_report.get("generated_at", ""),
        "trade_date": live_sim_canary_performance_report.get("trade_date", today),
        "summary": live_sim_canary_performance_report.get("summary", {}),
        "recommendations": list(live_sim_canary_performance_report.get("recommendations") or [])[:5],
        "cases": list(live_sim_canary_performance_report.get("items") or [])[: 10 if full_detail else 5],
        "disclaimer_ko": live_sim_canary_performance_report.get("disclaimer_ko", ""),
    }
    exit_policy_validation_report = _cached_dashboard_fragment(
        db,
        f"exit_policy_validation:v1:{today}:{performance_limit}",
        lambda: _exit_policy_validation_analyzer(db).build_report(trade_date=today, limit=performance_limit),
    )
    exit_policy_validation_payload = {
        "available": bool(exit_policy_validation_report.get("summary", {}).get("shadow_case_count")),
        "status": exit_policy_validation_report.get("status", "READY"),
        "generated_at": exit_policy_validation_report.get("generated_at", ""),
        "trade_date": exit_policy_validation_report.get("trade_date", today),
        "summary": exit_policy_validation_report.get("summary", {}),
        "actual_exit_quality": exit_policy_validation_report.get("actual_exit_quality", {}),
        "scenario_summary": list(exit_policy_validation_report.get("scenario_summary") or [])[: 12 if full_detail else 6],
        "cases": list(exit_policy_validation_report.get("items") or [])[: 10 if full_detail else 5],
        "recommendations": list(exit_policy_validation_report.get("recommendations") or [])[:5],
        "disclaimer_ko": exit_policy_validation_report.get("disclaimer_ko", ""),
        "analysis_only": True,
    }
    conservative_reason_report: dict[str, Any] | None = None
    try:
        conservative_reason_report = _cached_dashboard_fragment(
            db,
            f"conservative_reason:v2:{today}:{heavy_report_limit}",
            lambda: _conservative_reason_outcome_analyzer(db).build_report(trade_date=today, limit=heavy_report_limit),
        )
        conservative_reason_payload = conservative_reason_snapshot_payload(conservative_reason_report)
    except Exception as exc:
        conservative_reason_payload = conservative_reason_empty_payload(str(exc))
    shadow_small_entry_report: dict[str, Any] | None = None
    try:
        if not bool(conservative_reason_payload.get("available")):
            shadow_small_entry_payload = shadow_small_entry_empty_payload()
        else:
            shadow_small_entry_report = _cached_dashboard_fragment(
                db,
                f"shadow_small_entry_promotion:v2:{today}:{heavy_report_limit}",
                lambda: _shadow_small_entry_promotion_analyzer(db).build_report(
                    trade_date=today,
                    limit=heavy_report_limit,
                    include_traces=False,
                    conservative_report=conservative_reason_report,
                ),
            )
            shadow_small_entry_payload = shadow_small_entry_snapshot_payload(shadow_small_entry_report)
    except Exception as exc:
        shadow_small_entry_payload = shadow_small_entry_empty_payload(str(exc))
    try:
        shadow_small_entry_ops_payload = shadow_small_entry_ops_snapshot_payload(
            ShadowSmallEntryOpsService(
                db,
                gateway_state=gateway_state,
                core_settings=get_settings(),
                promotion_evidence=(
                    dict(shadow_small_entry_report.get("evidence") or {}) if shadow_small_entry_report is not None else None
                ),
                live_audit_report=live_sim_audit_payload,
            ).status(trade_date=today)
        )
    except Exception as exc:
        shadow_small_entry_ops_payload = {
            "available": False,
            "status": "ERROR",
            "mode": "observe_only",
            "order_enabled": False,
            "preflight_status": "ERROR",
            "preflight_blocking_reasons": [str(exc)],
            "operator_message_ko": "Shadow Small Entry 운영 상태를 불러오지 못했습니다.",
            "last_updated_at": "",
        }
    try:
        shadow_small_entry_pilot_payload = shadow_small_entry_pilot_snapshot_payload(
            _shadow_small_entry_pilot_service(db).status(trade_date=today)
        )
    except Exception as exc:
        shadow_small_entry_pilot_payload = shadow_small_entry_pilot_empty_payload(today, str(exc))
    shadow_summary_payload = _cached_dashboard_fragment(
        db,
        f"shadow_strategies:v2:{today}",
        lambda: db.shadow_strategy_summary(trade_date=today),
    )
    if not full_detail:
        decision_summary_payload = _dashboard_slim_intraday_decisions_payload(decision_summary_payload)
        shadow_summary_payload = _dashboard_slim_shadow_strategies_payload(shadow_summary_payload)
    replay_reports = scan_replay_reports(DEFAULT_REPLAY_DB_ROOT, limit=1)
    replay_runs = scan_replay_runs(DEFAULT_REPLAY_DB_ROOT, limit=5)
    replay_payload = {
        "latest": replay_reports[0] if replay_reports else {},
        "recent_runs": replay_runs,
        "summary": (replay_reports[0].get("summary") if replay_reports else {}) or {},
        "funnel": (replay_reports[0].get("funnel") if replay_reports else {}) or {},
        "shadow_ranking": (replay_reports[0].get("recommendations") if replay_reports else []) or [],
        "data_quality": ((replay_reports[0].get("summary") or {}).get("warnings") if replay_reports else []) or [],
        "diff_summary": (replay_reports[0].get("diff_summary") if replay_reports else {}) or {},
    }
    change_proposal_summary_payload = db.strategy_change_proposal_summary(trade_date=today)
    change_proposal_payload = {
        "summary": change_proposal_summary_payload,
        "top_recommendations": change_proposal_summary_payload.get("top_recommendations", []),
        "disclaimer_ko": "자동 적용 아님: 승인 상태만 저장하며 runtime config는 변경하지 않습니다.",
    }
    dry_run_performance_trade_date = _dashboard_dry_run_performance_trade_date(db)
    dry_run_performance_report = _cached_dashboard_fragment(
        db,
        f"dry_run_performance:v4:{dry_run_performance_trade_date or 'all'}:{performance_limit}",
        lambda: _performance_analyzer(db).build_report(
            trade_date=dry_run_performance_trade_date or None,
            limit=performance_limit,
        ),
    )
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
                "net_expectancy",
                "net_win_rate",
                "false_positive_count",
                "false_negative_count",
                "opportunity_loss_count",
                "cost_adjusted_bad_ready_count",
                "cost_adjusted_bad_ready_rate",
                "cost_adjusted_opportunity_loss_count",
                "cost_adjusted_opportunity_loss_rate",
                "live_would_pass_win_rate",
                "live_would_reject_but_rallied_count",
            ]
        },
        "primary_cost_assumption": dry_run_performance_report.get("summary", {}).get("primary_cost_assumption", {}),
        "cost_scenario_expectancy": list(dry_run_performance_report.get("summary", {}).get("cost_scenario_expectancy", []) or [])[:16],
        "execution_realism": dry_run_performance_report.get("summary", {}).get("execution_realism", {}),
        "go_no_go": dry_run_performance_report.get("summary", {}).get("go_no_go", {}),
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
        ][: 10 if full_detail else 3],
        "intraday_outcomes": outcome_summary_payload,
        "shadow_strategies": shadow_summary_payload,
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
    theme_lab_preflight_payload = _cached_theme_lab_dashboard_snapshot(
        db,
        "theme_lab:v3:full" if full_detail else "theme_lab:v3:slim",
        lambda: build_theme_lab_dashboard_snapshot(
            db,
            runtime_status=runtime_status,
            gateway_state=gateway_state,
            include_extended=full_detail,
        ),
        refresh_builder=lambda: _build_theme_lab_dashboard_snapshot_with_fresh_db(include_extended=full_detail),
    )
    theme_lab_payload = theme_lab_preflight_payload
    try:
        live_sim_preflight_payload = _live_sim_preflight_service(db).build_snapshot(
            runtime_status=runtime_status,
            performance_report=dry_run_performance_report,
            transport_status=transport_payload,
            theme_lab_snapshot=theme_lab_preflight_payload,
            include_details=full_detail,
        )
    except Exception as exc:
        live_sim_preflight_payload = {
            "status": "FAIL_CLOSED",
            "blocking_reasons": ["PREFLIGHT_EXCEPTION"],
            "warning_reasons": [],
            "operator_message_ko": f"LIVE_SIM 사전 점검 중 오류가 발생해 fail-closed 처리합니다: {exc}",
            "recommended_action_ko": "자동주문을 시작하지 말고 로그와 설정을 점검한 뒤 preflight rebuild를 다시 실행하세요.",
            "checked_at": utc_timestamp(),
            "account_mode_summary": {},
            "performance_summary": {},
            "gateway_load_summary": {},
            "backfill_summary": {},
            "safety_summary": {},
        }
    live_sim_canary_payload = _live_sim_canary_snapshot_payload(db, trade_date=today)
    if not full_detail:
        buy_zero_rca_payload = _dashboard_slim_buy_zero_rca_payload(buy_zero_rca_payload)
        conservative_reason_payload = _dashboard_slim_conservative_reason_payload(conservative_reason_payload)
        shadow_small_entry_payload = _dashboard_slim_shadow_small_entry_promotion_payload(shadow_small_entry_payload)
        replay_payload = _dashboard_slim_strategy_replay_payload(replay_payload)
        threshold_ab_payload = _dashboard_slim_threshold_ab_payload(threshold_ab_payload)
        theme_lab_payload = _dashboard_slim_theme_lab_payload(theme_lab_payload)
    runtime_payload["dry_run_orders"] = dry_run_orders_payload
    runtime_payload["intraday_decisions"] = decision_summary_payload
    runtime_payload["intraday_outcomes"] = outcome_summary_payload
    runtime_payload["buy_zero_rca"] = buy_zero_rca_payload
    runtime_payload["live_sim_audit"] = live_sim_audit_payload
    runtime_payload["live_sim_preflight"] = live_sim_preflight_payload
    runtime_payload["live_sim_canary"] = live_sim_canary_payload
    runtime_payload["live_sim_canary_performance"] = live_sim_canary_performance_payload
    runtime_payload["exit_policy_validation"] = exit_policy_validation_payload
    runtime_payload["conservative_reason_outcomes"] = conservative_reason_payload
    runtime_payload["shadow_small_entry_promotion"] = shadow_small_entry_payload
    runtime_payload["shadow_small_entry_ops"] = shadow_small_entry_ops_payload
    runtime_payload["shadow_small_entry_pilot"] = shadow_small_entry_pilot_payload
    runtime_payload["shadow_strategies"] = shadow_summary_payload
    runtime_payload["strategy_replay"] = replay_payload
    runtime_payload["change_proposals"] = change_proposal_payload
    runtime_payload["dry_run_performance"] = dry_run_performance_payload
    runtime_payload["threshold_ab"] = threshold_ab_payload
    gateway_payload = dict(status_payload["gateway"]) if full_detail else _dashboard_slim_gateway_payload(status_payload["gateway"])
    ops_alerts_payload = build_ops_alerts(
        core=status_payload["core"],
        gateway=gateway_payload,
        commands=status_payload["commands"],
        transport=transport_payload,
        runtime=runtime_payload,
        dry_run_performance=dry_run_performance_payload,
        logs=logs_payload,
    )
    runtime_snapshot_payload = runtime_payload if full_detail else _dashboard_slim_runtime_payload(runtime_payload)
    return {
        "snapshot_detail": resolved_detail,
        "timestamp": utc_timestamp(),
        "core": status_payload["core"],
        "gateway": gateway_payload,
        "commands": commands_payload,
        "transport": transport_payload,
        "transport_experiment": transport_experiment_payload,
        "runtime": runtime_snapshot_payload,
        "dry_run_orders": dry_run_orders_payload,
        "intraday_decisions": decision_summary_payload,
        "intraday_outcomes": outcome_summary_payload,
        "buy_zero_rca": buy_zero_rca_payload,
        "live_sim_audit": live_sim_audit_payload,
        "live_sim_preflight": live_sim_preflight_payload,
        "live_sim_canary": live_sim_canary_payload,
        "live_sim_canary_performance": live_sim_canary_performance_payload,
        "exit_policy_validation": exit_policy_validation_payload,
        "conservative_reason_outcomes": conservative_reason_payload,
        "shadow_small_entry_promotion": shadow_small_entry_payload,
        "shadow_small_entry_ops": shadow_small_entry_ops_payload,
        "shadow_small_entry_pilot": shadow_small_entry_pilot_payload,
        "shadow_strategies": shadow_summary_payload,
        "strategy_replay": replay_payload,
        "change_proposals": change_proposal_payload,
        "dry_run_performance": dry_run_performance_payload,
        "threshold_ab": threshold_ab_payload,
        "ops_alerts": ops_alerts_payload,
        "safety": status_payload["safety"],
        "candidates": candidates_payload,
        "themes": themes_payload,
        "orders": orders_payload,
        "reviews": reviews_payload,
        "logs": logs_payload,
        "theme_lab": theme_lab_payload,
        "market_data": {
            "latest_ticks": gateway_state.latest_ticks(limit=30 if full_detail else 10),
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
    trace = trace_from_payload(event.payload)
    transport_mode = str(event.payload.get("transport_mode") or trace.get("transport_mode") or "rest_long_poll")
    if (
        str(event.type or "") == "price_tick"
        and transport_mode == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
        and not settings.transport_metrics_persist_ws_price_ticks
    ):
        return
    sample_key = event.event_id or event.command_id or event.request_id
    if not should_sample_transport_message(
        message_type=event.type,
        sample_key=sample_key,
        price_tick_rate=settings.transport_metrics_sample_price_tick_rate,
        heartbeat_rate=settings.transport_metrics_sample_heartbeat_rate,
    ):
        return
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
            "transport_mode": transport_mode,
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
            _append_live_sim_command_audit_event(
                db,
                command_id=command_id,
                event_type="command_started",
                status=str(event.payload.get("status") or CommandStatus.DISPATCHED.value),
                payload=dict(event.payload or {}),
            )
            gateway_state.ack_command(
                command_id,
                status=CommandStatus.DISPATCHED.value,
                result_payload=dict(event.payload or {}),
            )
            db.save_log(f"[gateway][command_started] {event.payload.get('command_type', '')} {command_id}")
    elif event.type == "command_ack":
        _handle_command_ack(db, event)
    elif event.type == "market_symbols":
        saved = _handle_market_symbols_event(db, dict(event.payload or {}))
        if saved:
            db.save_log(f"[gateway][market_symbols] saved={saved}")
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


def _queue_replay_tick_history(event: GatewayEvent) -> None:
    if event.type != "price_tick":
        return
    try:
        replay_tick_buffer.enqueue_event(event)
    except Exception:
        pass


def _handle_market_symbols_event(db: TradingDatabase, payload: dict[str, Any]) -> int:
    rows: list[dict[str, Any]] = []
    market_payloads = list(payload.get("markets") or [])
    if not market_payloads and payload.get("symbols"):
        market_payloads = [payload]
    for market_payload in market_payloads:
        market = str(market_payload.get("market") or "").strip().upper()
        market_code = str(market_payload.get("market_code") or "").strip()
        symbols = list(market_payload.get("symbols") or [])
        for symbol in symbols:
            if isinstance(symbol, dict):
                code = symbol.get("code") or symbol.get("symbol")
                name = symbol.get("name") or ""
            else:
                code = symbol
                name = ""
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "market": market,
                    "market_code": market_code,
                    "source": "kiwoom_code_list",
                    "raw": {"gateway_event": "market_symbols"},
                }
            )
    return db.upsert_kiwoom_symbol_master(rows)


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
        _append_live_sim_command_audit_event(
            db,
            command_id=command_id,
            event_type="command_rejected" if status in {CommandStatus.REJECTED.value, CommandStatus.FAILED.value} else "command_acked",
            status=status,
            payload=payload,
            message=error or str(payload.get("message") or ""),
        )
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


def _append_live_sim_command_audit_event(
    db: TradingDatabase,
    *,
    command_id: str,
    event_type: str,
    status: str,
    payload: dict[str, Any],
    message: str = "",
) -> None:
    record = gateway_state.get_command(command_id)
    record_payload: dict[str, Any] = {}
    record_command_type = ""
    if record is not None:
        record_data = record.to_dict()
        record_command = dict(record_data.get("command") or {})
        record_payload = dict(record_command.get("payload") or {})
        record_command_type = str(record_data.get("command_type") or record_command.get("type") or "")
    command_type = str(payload.get("command_type") or record_command_type or "")
    if command_type not in {"send_order", "cancel_order"}:
        return
    merged_payload = {**record_payload, **dict(payload or {})}
    if str(merged_payload.get("order_mode") or "") != "LIVE_SIM" and str((record or {}).metadata.get("runtime") if record else "") != "LIVE_SIM":
        return
    order = None
    if command_type == "send_order":
        order = db.find_live_sim_order_by_command_id(command_id)
    elif command_type == "cancel_order":
        original_order_id = str(merged_payload.get("original_order_id") or "")
        order = db.get_live_sim_order(original_order_id) if original_order_id else None
    if order is None:
        return
    now = str(payload.get("timestamp") or payload.get("received_at") or "")
    status_from = str(order.get("order_status") or "")
    status_to = status_from
    reason = message or str(payload.get("message") or status or event_type)
    if command_type == "send_order" and status in {CommandStatus.REJECTED.value, CommandStatus.FAILED.value}:
        status_to = "REJECTED" if status == CommandStatus.REJECTED.value else "FAILED"
        codes = _append_reason_codes(order.get("reason_codes") or [], ["LIVE_SIM_COMMAND_REJECTED", status_to])
        order = db.update_live_sim_order(
            str(order.get("order_intent_id") or ""),
            {
                "order_status": status_to,
                "rejected_at": now,
                "updated_at": now,
                "reason_codes": codes,
                "details": {
                    **dict(order.get("details") or {}),
                    "command_audit": {
                        "command_id": command_id,
                        "command_type": command_type,
                        "command_status": status,
                        "message": reason,
                        "payload": merged_payload,
                    },
                },
            },
        ) or order
    db.append_live_sim_order_event(
        str(order.get("order_intent_id") or ""),
        event_type,
        status_from=status_from,
        status_to=status_to,
        message=reason,
        payload={
            "command_id": command_id,
            "command_type": command_type,
            "command_status": status,
            "command_payload": merged_payload,
        },
        created_at=now,
    )


def _append_reason_codes(existing: list[Any], additions: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*list(existing or []), *additions]:
        text = str(item or "")
        if text and text not in merged:
            merged.append(text)
    return merged


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
        "realtime_data_quality": dict(status.get("realtime_data_quality") or {}),
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
        "recent_errors": [
            _transport_dashboard_error_payload(dict(item or {}))
            for item in list(status.get("recent_errors") or [])[:5]
        ],
        "real_gateway_websocket_pilot": _transport_dashboard_real_pilot_payload(real_pilot),
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
        "real_pilot_price_tick_sample_rate": real_pilot.get("price_tick_sample_rate", 0),
        "real_pilot_price_tick_sampled_count": real_pilot.get("price_tick_sampled_count", 0),
        "real_pilot_price_tick_fallback_count": real_pilot.get("price_tick_fallback_count", 0),
        "real_pilot_event_fallback_count": real_pilot.get("event_fallback_count", 0),
        "real_pilot_last_ws_event_at": real_pilot.get("last_ws_event_at", ""),
        "real_pilot_last_ws_ack_at": real_pilot.get("last_ws_ack_at", ""),
    }


def _transport_dashboard_error_payload(error: dict[str, Any]) -> dict[str, Any]:
    return _dashboard_field_subset(
        error,
        (
            "id",
            "sample_id",
            "trace_id",
            "trade_date",
            "created_at",
            "direction",
            "message_type",
            "event_id",
            "command_id",
            "request_id",
            "source",
            "success",
            "error",
            "transport_mode",
            "latency_ms",
            "total_latency_ms",
            "event_latency_ms",
            "command_latency_ms",
            "ack_latency_ms",
            "long_poll_wait_ms",
            "gateway_execute_ms",
            "rate_limit_wait_ms",
            "ws_receive_ms",
            "ws_send_ms",
            "ws_session_id",
            "ws_connection_id",
            "connection_id",
        ),
    )


def _transport_dashboard_real_pilot_payload(real_pilot: dict[str, Any]) -> dict[str, Any]:
    return _dashboard_field_subset(
        real_pilot,
        (
            "enabled",
            "connected",
            "state",
            "ws_session_id",
            "ws_connection_id",
            "reconnect_count",
            "fallback_state",
            "fallback_reason",
            "fallback_detail",
            "fallback_at",
            "last_error",
            "last_error_type",
            "last_error_stage",
            "last_error_at",
            "last_close_code",
            "last_close_reason",
            "blocked_order_command_count",
            "session_loss_count",
            "duplicate_ack_count",
            "unknown_ack_count",
            "price_tick_sample_rate",
            "price_tick_sampled_count",
            "price_tick_fallback_count",
            "event_fallback_count",
            "last_ws_event_at",
            "last_ws_ack_at",
        ),
    )


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


def _gateway_ws_send_completed_sample_expected(message: GatewayWsMessage, metadata: dict[str, Any]) -> bool:
    settings = get_settings()
    if not settings.transport_metrics_enabled:
        return False
    payload = dict(message.payload or {})
    sample_message_type = str(payload.get("sample_message_type") or metadata.get("sample_message_type") or payload.get("original_type") or "")
    if not sample_message_type:
        return False
    if (
        sample_message_type == "price_tick"
        and str(payload.get("transport_mode") or metadata.get("transport_mode") or "") == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
        and not settings.transport_metrics_persist_ws_price_ticks
    ):
        return False
    sample_key = str(
        payload.get("original_event_id")
        or message.event_id
        or payload.get("original_command_id")
        or message.command_id
        or payload.get("original_trace_id")
        or payload.get("original_message_id")
        or ""
    )
    return should_sample_transport_message(
        message_type=sample_message_type,
        sample_key=sample_key,
        price_tick_rate=settings.transport_metrics_sample_price_tick_rate,
        heartbeat_rate=settings.transport_metrics_sample_heartbeat_rate,
    )


def _record_gateway_ws_send_completed(message: GatewayWsMessage, metadata: dict[str, Any]) -> dict[str, Any]:
    payload = dict(message.payload or {})
    original_sequence = int(payload.get("original_sequence") or metadata.get("original_sequence") or 0)
    sample_message_type = str(payload.get("sample_message_type") or metadata.get("sample_message_type") or payload.get("original_type") or "")
    ws_session_id = str(payload.get("ws_session_id") or metadata.get("ws_session_id") or metadata.get("websocket_session_id") or "")
    send_started_at = str(payload.get("gateway_ws_send_started_at_utc") or "")
    send_completed_at = str(payload.get("gateway_ws_send_completed_at_utc") or "")
    send_duration_ms = _optional_float_value(payload.get("gateway_ws_send_duration_ms"))
    if send_duration_ms is None:
        send_duration_ms = wall_ms(send_started_at, send_completed_at)
    stage_updates: dict[str, Any] = {
        "gateway_ws_send_start_to_send_complete_ms": send_duration_ms,
    }
    metadata_updates: dict[str, Any] = {
        "gateway_ws_send_completed_at_utc": send_completed_at,
        "gateway_ws_send_completed_monotonic_ms": payload.get("gateway_ws_send_completed_monotonic_ms"),
        "gateway_ws_send_duration_ms": send_duration_ms,
        "gateway_ws_send_completed_diagnostic_received_at_utc": utc_now_ms(),
        "gateway_ws_payload_size_bytes": payload.get("gateway_ws_payload_size_bytes"),
        "gateway_ws_original_message_id": payload.get("original_message_id"),
        "gateway_ws_original_type": payload.get("original_type"),
        "gateway_ws_original_sequence": original_sequence,
    }
    updated = False
    sample_id = ""
    complete_to_core_receive_ms: float | None = None
    skipped = not _gateway_ws_send_completed_sample_expected(message, metadata)
    if not skipped:
        db = open_database()
        try:
            sample = db.find_gateway_transport_latency_sample_by_ws_message(
                ws_session_id=ws_session_id,
                ws_message_sequence=original_sequence,
                message_type=sample_message_type,
                event_id=str(payload.get("original_event_id") or message.event_id or ""),
                command_id=str(payload.get("original_command_id") or message.command_id or ""),
            )
            if sample is None:
                sample = db.find_gateway_transport_latency_sample_by_ws_message(
                    ws_session_id=ws_session_id,
                    ws_message_sequence=original_sequence,
                    message_type=sample_message_type,
                )
            if sample is not None:
                sample_id = str(sample.get("sample_id") or "")
                core_received_at = str((sample.get("metadata") or {}).get("core_ws_received_at_utc") or "")
                complete_to_core_receive_ms = wall_ms(send_completed_at, core_received_at)
                stage_updates["gateway_ws_send_complete_to_core_receive_ms"] = complete_to_core_receive_ms
                db.update_gateway_transport_latency_sample_stage(
                    sample_id,
                    stage_updates=stage_updates,
                    metadata_updates=metadata_updates,
                )
                updated = True
        finally:
            close_database(db)
    _update_gateway_ws_transport_state(
        {
            "gateway_ws_send_completed_count": int(gateway_ws_transport_state.get("gateway_ws_send_completed_count") or 0) + 1,
            "gateway_ws_send_completed_update_count": int(gateway_ws_transport_state.get("gateway_ws_send_completed_update_count") or 0) + (1 if updated else 0),
            "gateway_ws_send_completed_miss_count": int(gateway_ws_transport_state.get("gateway_ws_send_completed_miss_count") or 0)
            + (0 if skipped or updated else 1),
            "gateway_ws_send_completed_skipped_count": int(gateway_ws_transport_state.get("gateway_ws_send_completed_skipped_count") or 0)
            + (1 if skipped else 0),
            "gateway_ws_last_send_completed_duration_ms": send_duration_ms,
            "gateway_ws_last_send_completed_to_core_receive_ms": complete_to_core_receive_ms,
            "gateway_ws_last_send_completed_message_type": sample_message_type,
            "gateway_ws_last_send_completed_at": send_completed_at,
        }
    )
    return {
        "accepted": True,
        "type": "transport_send_completed",
        "updated": updated,
        "skipped": skipped,
        "reason": "UNSAMPLED_SEND_COMPLETED" if skipped else "",
        "sample_id": sample_id,
        "message_type": sample_message_type,
        "original_sequence": original_sequence,
        "gateway_ws_send_duration_ms": send_duration_ms,
        "gateway_ws_send_complete_to_core_receive_ms": complete_to_core_receive_ms,
    }


def _optional_float_value(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int_value(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_gateway_ws_token(websocket: WebSocket) -> bool:
    expected = get_settings().local_token
    provided = str(websocket.query_params.get("token") or websocket.headers.get("x-local-token") or "")
    authorization = str(websocket.headers.get("authorization") or "")
    if authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
    return bool(expected and provided == expected)


async def _send_gateway_ws_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    metadata: dict[str, Any],
    trace_id: str = "",
    sequence: int = 0,
    outbound_queue: asyncio.Queue[_CoreWsOutboundMessage] | None = None,
    connection_id: str = "",
) -> None:
    payload = GatewayWsMessage(
        type="error",
        trace_id=trace_id or f"trace_ws_error_{int(time.time() * 1000)}",
        source="core",
        payload={
            "accepted": False,
            "code": code,
            "message": message,
        },
        metadata=metadata,
        sequence=sequence,
    ).to_dict()
    if outbound_queue is not None:
        _queue_core_ws_outbound(outbound_queue, payload, connection_id=connection_id)
        return
    await websocket.send_json(payload)


def _record_gateway_ws_protocol_error(
    exc: Exception,
    *,
    stage: str,
    connection_transport_mode: str,
    session_id: str,
    connection_id: str,
) -> None:
    _update_gateway_ws_transport_state(
        {
            "connected": True,
            "state": "PROTOCOL_ERROR",
            "transport_mode": connection_transport_mode,
            "ws_session_id": session_id,
            "ws_connection_id": connection_id,
            "last_error": _truncate_log_detail(str(exc) or repr(exc)),
            "last_error_type": type(exc).__name__,
            "last_error_stage": stage,
            "last_error_at": utc_now_ms(),
        }
    )


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
        ("ws_priority_price_tick_code_count", "priority_price_tick_code_count"),
        ("ws_priority_price_tick_sampled_count", "priority_price_tick_sampled_count"),
        ("ws_condition_event_batch_queued_count", "condition_event_batch_queued_count"),
        ("ws_condition_event_batch_sent_count", "condition_event_batch_sent_count"),
        ("ws_condition_event_batched_count", "condition_event_batched_count"),
        ("ws_condition_event_batch_coalesced_count", "condition_event_batch_coalesced_count"),
        ("ws_last_send_ms", "last_send_ms"),
        ("ws_last_send_completed_at", "gateway_ws_last_send_completed_at"),
        ("ws_last_send_completed_message_type", "gateway_ws_last_send_completed_message_type"),
        ("ws_last_send_completed_duration_ms", "gateway_ws_last_send_completed_duration_ms"),
        ("ws_last_receive_ms", "last_receive_ms"),
        ("ws_outbound_queue_size", "outbound_queue_size"),
        ("ws_control_outbound_queue_size", "control_outbound_queue_size"),
        ("ws_data_outbound_queue_size", "data_outbound_queue_size"),
        ("ws_condition_event_queue_size", "condition_event_queue_size"),
        ("ws_command_queue_size", "command_queue_size"),
    ):
        if source_key in payload:
            patch[target_key] = payload.get(source_key)
    if "ws_priority_price_tick_sources" in payload:
        patch["priority_price_tick_sources"] = list(payload.get("ws_priority_price_tick_sources") or [])
    for source_key, target_key in (
        ("ws_condition_event_batch_enabled", "condition_event_batch_enabled"),
        ("ws_condition_event_batch_max_size", "condition_event_batch_max_size"),
        ("ws_condition_event_batch_max_wait_ms", "condition_event_batch_max_wait_ms"),
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
    condition_worker = dict(gateway_condition_event_worker_state)
    core_event_worker = dict(gateway_core_ws_event_worker_state)
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
        "last_send_ms": float(heartbeat.get("ws_last_send_ms") or state.get("last_send_ms") or 0.0),
        "gateway_ws_send_completed_count": int(state.get("gateway_ws_send_completed_count") or 0),
        "gateway_ws_send_completed_update_count": int(state.get("gateway_ws_send_completed_update_count") or 0),
        "gateway_ws_send_completed_miss_count": int(state.get("gateway_ws_send_completed_miss_count") or 0),
        "gateway_ws_send_completed_skipped_count": int(state.get("gateway_ws_send_completed_skipped_count") or 0),
        "gateway_ws_last_send_completed_duration_ms": float(
            heartbeat.get("ws_last_send_completed_duration_ms")
            or state.get("gateway_ws_last_send_completed_duration_ms")
            or 0.0
        ),
        "gateway_ws_last_send_completed_to_core_receive_ms": float(
            state.get("gateway_ws_last_send_completed_to_core_receive_ms") or 0.0
        ),
        "gateway_ws_last_send_completed_message_type": (
            heartbeat.get("ws_last_send_completed_message_type")
            or state.get("gateway_ws_last_send_completed_message_type")
            or ""
        ),
        "gateway_ws_last_send_completed_at": (
            heartbeat.get("ws_last_send_completed_at")
            or state.get("gateway_ws_last_send_completed_at")
            or ""
        ),
        "last_receive_ms": float(heartbeat.get("ws_last_receive_ms") or state.get("last_receive_ms") or 0.0),
        "outbound_queue_size": int(heartbeat.get("ws_outbound_queue_size") or state.get("outbound_queue_size") or 0),
        "control_outbound_queue_size": int(
            heartbeat.get("ws_control_outbound_queue_size") or state.get("control_outbound_queue_size") or 0
        ),
        "data_outbound_queue_size": int(
            heartbeat.get("ws_data_outbound_queue_size") or state.get("data_outbound_queue_size") or 0
        ),
        "condition_event_queue_size": int(
            heartbeat.get("ws_condition_event_queue_size") or state.get("condition_event_queue_size") or 0
        ),
        "command_queue_size": int(heartbeat.get("ws_command_queue_size") or state.get("command_queue_size") or 0),
        "core_ws_outbound_writer_active": bool(state.get("core_ws_outbound_writer_active")),
        "core_ws_outbound_queue_size": int(state.get("core_ws_outbound_queue_size") or 0),
        "core_ws_outbound_queue_max_size": int(state.get("core_ws_outbound_queue_max_size") or 0),
        "core_ws_outbound_queued_count": int(state.get("core_ws_outbound_queued_count") or 0),
        "core_ws_outbound_sent_count": int(state.get("core_ws_outbound_sent_count") or 0),
        "core_ws_outbound_dropped_count": int(state.get("core_ws_outbound_dropped_count") or 0),
        "core_ws_last_send_json_ms": float(state.get("core_ws_last_send_json_ms") or 0.0),
        "core_ws_last_send_queue_wait_ms": float(state.get("core_ws_last_send_queue_wait_ms") or 0.0),
        "core_ws_last_send_json_type": state.get("core_ws_last_send_json_type") or "",
        "core_ws_last_send_json_at": state.get("core_ws_last_send_json_at") or "",
        "core_ws_slow_send_count": int(state.get("core_ws_slow_send_count") or 0),
        "core_ws_last_slow_send_json_ms": float(state.get("core_ws_last_slow_send_json_ms") or 0.0),
        "core_ws_last_slow_send_at": state.get("core_ws_last_slow_send_at") or "",
        "core_ws_last_receive_text_ms": float(state.get("core_ws_last_receive_text_ms") or 0.0),
        "core_ws_receive_loop_gap_ms": float(state.get("core_ws_receive_loop_gap_ms") or 0.0),
        "core_ws_event_async_enabled": bool(core_event_worker.get("enabled")),
        "core_ws_event_queue_size": int(core_event_worker.get("queue_size") or 0),
        "core_ws_event_queue_max_size": int(core_event_worker.get("queue_max_size") or 0),
        "core_ws_event_active_count": int(core_event_worker.get("active_count") or 0),
        "core_ws_event_split_enabled": bool(core_event_worker.get("split_enabled")),
        "core_ws_event_control_worker_count": int(core_event_worker.get("control_worker_count") or 0),
        "core_ws_event_control_queue_size": int(core_event_worker.get("control_queue_size") or 0),
        "core_ws_event_control_queue_sizes": list(core_event_worker.get("control_queue_sizes") or []),
        "core_ws_event_data_queue_size": int(core_event_worker.get("data_queue_size") or 0),
        "core_ws_event_control_active_count": int(core_event_worker.get("control_active_count") or 0),
        "core_ws_event_data_active_count": int(core_event_worker.get("data_active_count") or 0),
        "core_ws_event_queued_count": int(core_event_worker.get("queued_count") or 0),
        "core_ws_event_processed_count": int(core_event_worker.get("processed_count") or 0),
        "core_ws_event_failed_count": int(core_event_worker.get("failed_count") or 0),
        "core_ws_event_dropped_count": int(core_event_worker.get("dropped_count") or 0),
        "core_ws_event_priority_enabled": bool(core_event_worker.get("priority_enabled")),
        "core_ws_event_control_queued_count": int(core_event_worker.get("control_queued_count") or 0),
        "core_ws_event_data_queued_count": int(core_event_worker.get("data_queued_count") or 0),
        "core_ws_event_last_priority": int(core_event_worker.get("last_priority") or 0),
        "core_ws_event_last_worker_kind": core_event_worker.get("last_worker_kind") or "",
        "core_ws_event_last_control_worker_index": int(core_event_worker.get("last_control_worker_index") or 0),
        "core_ws_price_tick_coalesce_enabled": bool(core_event_worker.get("price_tick_coalesce_enabled")),
        "core_ws_price_tick_pending_key_count": int(core_event_worker.get("price_tick_pending_key_count") or 0),
        "core_ws_price_tick_received_count": int(core_event_worker.get("price_tick_received_count") or 0),
        "core_ws_price_tick_queued_count": int(core_event_worker.get("price_tick_queued_count") or 0),
        "core_ws_price_tick_coalesced_count": int(core_event_worker.get("price_tick_coalesced_count") or 0),
        "core_ws_price_tick_processed_count": int(core_event_worker.get("price_tick_processed_count") or 0),
        "core_ws_price_tick_dropped_count": int(core_event_worker.get("price_tick_dropped_count") or 0),
        "core_ws_price_tick_last_key": core_event_worker.get("price_tick_last_key") or "",
        "core_ws_heartbeat_fast_skipped_count": int(core_event_worker.get("heartbeat_fast_skipped_count") or 0),
        "core_ws_event_last_message_type": core_event_worker.get("last_message_type") or "",
        "core_ws_event_last_event_id": core_event_worker.get("last_event_id") or "",
        "core_ws_event_last_fast_status_hint_type": core_event_worker.get("last_fast_status_hint_type") or "",
        "core_ws_event_last_fast_status_hint_event_id": core_event_worker.get("last_fast_status_hint_event_id") or "",
        "core_ws_event_last_fast_status_hint_at": core_event_worker.get("last_fast_status_hint_at") or "",
        "core_ws_event_last_queue_wait_ms": float(core_event_worker.get("last_queue_wait_ms") or 0.0),
        "core_ws_event_last_duration_ms": float(core_event_worker.get("last_duration_ms") or 0.0),
        "core_ws_event_last_queued_at": core_event_worker.get("last_queued_at") or "",
        "core_ws_event_last_processed_at": core_event_worker.get("last_processed_at") or "",
        "core_ws_event_last_error": core_event_worker.get("last_error") or "",
        "price_tick_sample_rate": float(heartbeat.get("ws_price_tick_sample_rate") or state.get("price_tick_sample_rate") or 0),
        "price_tick_sampled_count": int(
            heartbeat.get("ws_price_tick_sampled_count") or state.get("price_tick_sampled_count") or 0
        ),
        "price_tick_fallback_count": int(
            heartbeat.get("ws_price_tick_fallback_count") or state.get("price_tick_fallback_count") or 0
        ),
        "priority_price_tick_code_count": int(
            heartbeat.get("ws_priority_price_tick_code_count") or state.get("priority_price_tick_code_count") or 0
        ),
        "priority_price_tick_sampled_count": int(
            heartbeat.get("ws_priority_price_tick_sampled_count") or state.get("priority_price_tick_sampled_count") or 0
        ),
        "priority_price_tick_sources": list(
            heartbeat.get("ws_priority_price_tick_sources") or state.get("priority_price_tick_sources") or []
        ),
        "event_fallback_count": int(heartbeat.get("ws_event_fallback_count") or state.get("event_fallback_count") or 0),
        "condition_event_batch_enabled": bool(
            heartbeat.get("ws_condition_event_batch_enabled") or state.get("condition_event_batch_enabled") or False
        ),
        "condition_event_batch_max_size": int(
            heartbeat.get("ws_condition_event_batch_max_size") or state.get("condition_event_batch_max_size") or 0
        ),
        "condition_event_batch_max_wait_ms": float(
            heartbeat.get("ws_condition_event_batch_max_wait_ms") or state.get("condition_event_batch_max_wait_ms") or 0
        ),
        "condition_event_batch_queued_count": int(
            heartbeat.get("ws_condition_event_batch_queued_count") or state.get("condition_event_batch_queued_count") or 0
        ),
        "condition_event_batch_sent_count": int(
            heartbeat.get("ws_condition_event_batch_sent_count") or state.get("condition_event_batch_sent_count") or 0
        ),
        "condition_event_batched_count": int(
            heartbeat.get("ws_condition_event_batched_count") or state.get("condition_event_batched_count") or 0
        ),
        "condition_event_batch_coalesced_count": int(
            heartbeat.get("ws_condition_event_batch_coalesced_count") or state.get("condition_event_batch_coalesced_count") or 0
        ),
        "core_condition_event_async_enabled": bool(condition_worker.get("enabled")),
        "core_condition_event_queue_size": int(condition_worker.get("queue_size") or 0),
        "core_condition_event_queue_sizes_by_worker": list(condition_worker.get("queue_sizes_by_worker") or []),
        "core_condition_event_queue_batch_count": int(condition_worker.get("queue_batch_count") or 0),
        "core_condition_event_queue_batch_counts_by_worker": list(condition_worker.get("queue_batch_counts_by_worker") or []),
        "core_condition_event_queue_max_size": int(condition_worker.get("queue_max_size") or 0),
        "core_condition_event_worker_count": int(condition_worker.get("worker_count") or 0),
        "core_condition_event_active_worker_count": int(condition_worker.get("active_worker_count") or 0),
        "core_condition_event_active_count": int(condition_worker.get("active_count") or 0),
        "core_condition_event_received_count": int(condition_worker.get("received_count") or 0),
        "core_condition_event_queued_count": int(condition_worker.get("queued_count") or 0),
        "core_condition_event_coalesced_count": int(condition_worker.get("coalesced_count") or 0),
        "core_condition_event_stale_skipped_count": int(condition_worker.get("stale_skipped_count") or 0),
        "core_condition_event_stale_queue_wait_skipped_count": int(condition_worker.get("stale_queue_wait_skipped_count") or 0),
        "core_condition_event_processed_count": int(condition_worker.get("processed_count") or 0),
        "core_condition_event_failed_count": int(condition_worker.get("failed_count") or 0),
        "core_condition_event_dropped_count": int(condition_worker.get("dropped_count") or 0),
        "core_condition_event_last_batch_size": int(condition_worker.get("last_batch_size") or 0),
        "core_condition_event_last_drained_batch_count": int(condition_worker.get("last_drained_batch_count") or 0),
        "core_condition_event_last_received_count": int(condition_worker.get("last_received_count") or 0),
        "core_condition_event_last_queued_count": int(condition_worker.get("last_queued_count") or 0),
        "core_condition_event_last_queued_batch_count": int(condition_worker.get("last_queued_batch_count") or 0),
        "core_condition_event_last_coalesced_count": int(condition_worker.get("last_coalesced_count") or 0),
        "core_condition_event_last_stale_skipped_count": int(condition_worker.get("last_stale_skipped_count") or 0),
        "core_condition_event_last_stale_queue_wait_skipped_count": int(
            condition_worker.get("last_stale_queue_wait_skipped_count") or 0
        ),
        "core_condition_event_last_stale_queue_wait_ms": float(condition_worker.get("last_stale_queue_wait_ms") or 0.0),
        "core_condition_event_last_queue_wait_ms": float(condition_worker.get("last_queue_wait_ms") or 0.0),
        "core_condition_event_stale_include_skip_ms": float(
            condition_worker.get("stale_include_skip_ms") or _gateway_condition_event_stale_include_skip_ms()
        ),
        "core_condition_event_batch_chunk_size": int(
            condition_worker.get("batch_chunk_size") or _gateway_condition_event_batch_chunk_size()
        ),
        "core_condition_event_last_batch_duration_ms": float(condition_worker.get("last_batch_duration_ms") or 0.0),
        "core_condition_event_last_worker_index": int(condition_worker.get("last_worker_index") or 0),
        "core_condition_event_last_shard_key": condition_worker.get("last_shard_key") or "",
        "core_condition_event_last_queued_at": condition_worker.get("last_queued_at") or "",
        "core_condition_event_last_processed_at": condition_worker.get("last_processed_at") or "",
        "core_condition_event_last_error": condition_worker.get("last_error") or "",
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


def _gateway_events_from_ws_batch_message(message: GatewayWsMessage, *, metadata: dict[str, Any]) -> list[GatewayEvent]:
    payload = dict(message.payload or {})
    raw_events = list(payload.get("events") or [])
    batch_id = str(payload.get("batch_id") or message.event_id or message.message_id)
    batch_size = len(raw_events)
    received_at = utc_now_ms()
    events: list[GatewayEvent] = []
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            continue
        event = GatewayEvent.from_dict(raw_event)
        if event.type != "condition_event":
            continue
        trace = trace_from_payload(event.payload)
        trace_payload = ensure_transport_trace(
            event.payload,
            trace_id=trace.get("trace_id") or f"trace:{event.event_id or batch_id}:{index}",
            process="gateway",
            extra={
                **metadata,
                "transport_mode": metadata.get("transport_mode") or TRANSPORT_MODE_WEBSOCKET_MOCK,
                "gateway_ws_message_id": message.message_id,
                "gateway_ws_message_type": message.type,
                "gateway_ws_message_timestamp": message.timestamp,
                "gateway_ws_message_sequence": message.sequence,
                "gateway_ws_message_trace_id": message.trace_id,
                "gateway_ws_condition_batch_id": batch_id,
                "gateway_ws_condition_batch_size": batch_size,
                "gateway_ws_condition_batch_index": index,
                "core_ws_received_at_utc": received_at,
            },
        )
        data = event.to_dict()
        data["payload"] = trace_payload
        data["event_id"] = event.event_id or f"evt_ws_{batch_id}_{index}"
        data["source"] = event.source or message.source
        events.append(GatewayEvent.from_dict(data))
    return events


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
