from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from inspect import Parameter, signature
from time import perf_counter
from typing import Callable, Optional

from trading.strategy.candidates import (
    QUALITY_ACTIONABLE,
    QUALITY_DATA_WAIT,
    QUALITY_DISCOVERY_ONLY,
    QUALITY_INVALID_CODE,
    QUALITY_UNMAPPED,
    CandidateCollector,
    CandidateLifecycle,
    candidate_quality_status,
    normalize_code,
)
from trading.strategy.candles import CandleBuilder
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.exit import (
    DRY_RUN_EXIT_INTENT_TYPES,
    ExitContextRiskSnapshot,
    ExitDecisionEngine,
    VirtualPositionService,
)
from trading.strategy.holding import HoldingProvider, StaticHoldingProvider
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateState,
    EntryPlan,
    ExitDecision,
    FillPolicy,
    OrderMode,
    PositionContextSnapshot,
    ReviewFinalStatus,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.pipeline import GatePipeline, GatePipelineResult
from trading.strategy.readiness import ReadinessReport, _active_theme_presence_by_code, build_readiness_report, dedupe_warnings
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reason_taxonomy import normalize_reason_status, reason_status_family, reason_summary
from trading.strategy.review import TradeReviewService
from trading.strategy.themelab_adapter import ThemeLabDryRunLifecycleBridge
from trading.strategy.virtual_orders import VirtualOrderService
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.runtime_pipeline import ThemeLabRuntimePipeline
from trading.theme_engine.universe import ThemeUniverseBuilder


ACTIVE_RUNTIME_STATES = {
    CandidateState.DETECTED,
    CandidateState.WATCHING,
    CandidateState.READY,
}
TERMINAL_CANDIDATE_STATES = {CandidateState.EXPIRED, CandidateState.REMOVED}
MARKET_SESSION_OPEN = "open"
MARKET_SESSION_CLOSED = "closed"
DATA_WARMUP_READY = "ready"
DATA_WARMUP_WAITING_INDEX = "waiting_index"
DATA_WARMUP_WAITING_CANDIDATE_TICKS = "waiting_candidate_ticks"
DATA_WARMUP_CLOSED = "closed"
GATE_SKIP_MARKET_SESSION_CLOSED = "MARKET_SESSION_CLOSED"
GATE_SKIP_DATA_WARMUP = "DATA_WARMUP"


@dataclass
class StrategyRuntimeConfig:
    order_mode: OrderMode = OrderMode.OBSERVE
    evaluation_interval_sec: int = 5
    condition_profiles_enabled: bool = True
    index_watch_codes: dict[str, str] = field(default_factory=lambda: {"KOSPI": "001", "KOSDAQ": "101"})
    leader_watch_codes: list[str] = field(default_factory=lambda: ["005930", "000660"])
    semiconductor_signal_codes: list[str] = field(default_factory=lambda: ["005930", "000660"])
    holding_watch_codes: list[str] = field(default_factory=list)
    virtual_fill_policy: FillPolicy = FillPolicy.NORMAL
    review_save_enabled: bool = True
    max_candidates_to_watch: int = 100
    realtime_subscription_limit: int = 80
    theme_engine_mode: str = "themelab_flow"
    theme_lab_dry_run_bridge_enabled: bool = True
    theme_lab_pipeline_interval_sec: int = 3
    exit_context_risk_enabled: bool = False
    theme_lab_condition_names: dict[str, str] = field(
        default_factory=lambda: {"alive": "테마랩_생존_-1", "strong": "테마랩_강세_3", "leader": "테마랩_주도_5"}
    )
    theme_lab_condition_purposes: dict[str, str] = field(
        default_factory=lambda: {"alive": "theme_lab_alive", "strong": "theme_lab_strong", "leader": "theme_lab_leader"}
    )

    def validate(self) -> list[str]:
        warnings: list[str] = []
        if self.order_mode != OrderMode.OBSERVE:
            raise ValueError("StrategyRuntime supports OBSERVE mode only in PR 2-1")
        if type(self.condition_profiles_enabled) is not bool:
            raise ValueError("condition_profiles_enabled must be bool")
        if type(self.review_save_enabled) is not bool:
            raise ValueError("review_save_enabled must be bool")
        if not 1 <= self.evaluation_interval_sec <= 3600:
            raise ValueError("evaluation_interval_sec must be between 1 and 3600")
        if not isinstance(self.index_watch_codes, dict):
            raise ValueError("index_watch_codes must be a mapping")
        if set(self.index_watch_codes) - {"KOSPI", "KOSDAQ"}:
            raise ValueError("index_watch_codes only supports KOSPI/KOSDAQ")
        for logical in ["KOSPI", "KOSDAQ"]:
            if not str(self.index_watch_codes.get(logical, "")).strip():
                raise ValueError(f"index_watch_codes.{logical} must not be empty")
        if not isinstance(self.virtual_fill_policy, FillPolicy):
            self.virtual_fill_policy = FillPolicy(str(self.virtual_fill_policy))
        if self.virtual_fill_policy not in {FillPolicy.OPTIMISTIC, FillPolicy.NORMAL, FillPolicy.CONSERVATIVE}:
            raise ValueError("virtual_fill_policy must be optimistic, normal, or conservative")
        self.leader_watch_codes = _normalize_runtime_stock_codes(self.leader_watch_codes, "leader_watch_codes")
        self.semiconductor_signal_codes = _normalize_runtime_stock_codes(
            self.semiconductor_signal_codes,
            "semiconductor_signal_codes",
        )
        self.holding_watch_codes = _normalize_runtime_stock_codes(self.holding_watch_codes, "holding_watch_codes")
        if self.max_candidates_to_watch < 0:
            raise ValueError("max_candidates_to_watch must be >= 0")
        if self.realtime_subscription_limit < 1:
            raise ValueError("realtime_subscription_limit must be >= 1")
        self.theme_engine_mode = str(self.theme_engine_mode or "").strip().lower()
        if self.theme_engine_mode not in {"legacy", "themelab_flow"}:
            raise ValueError("theme_engine_mode must be legacy or themelab_flow")
        if type(self.theme_lab_dry_run_bridge_enabled) is not bool:
            raise ValueError("theme_lab_dry_run_bridge_enabled must be bool")
        if type(self.exit_context_risk_enabled) is not bool:
            raise ValueError("exit_context_risk_enabled must be bool")
        if not 1 <= int(self.theme_lab_pipeline_interval_sec) <= 3600:
            raise ValueError("theme_lab_pipeline_interval_sec must be between 1 and 3600")
        if self.realtime_subscription_limit < self.max_candidates_to_watch:
            warnings.append("REALTIME_LIMIT_BELOW_MAX_CANDIDATES")
        return warnings


@dataclass
class StrategyRuntimeSnapshot:
    started: bool = False
    cycle_at: str = ""
    candidate_count: int = 0
    active_candidate_count: int = 0
    recovered_candidate_count: int = 0
    gate_result_count: int = 0
    entry_plan_count: int = 0
    virtual_order_count: int = 0
    filled_order_count: int = 0
    open_position_count: int = 0
    exit_decision_count: int = 0
    review_count: int = 0
    expired_count: int = 0
    cycle_duration_ms: int = 0
    evaluated_candidate_count: int = 0
    db_write_count_per_cycle: int = 0
    candidate_save_count: int = 0
    review_upsert_count: int = 0
    ui_refresh_count: int = 0
    ui_refresh_skipped_count: int = 0
    dry_run_order_intent_count: int = 0
    dry_run_order_accepted_count: int = 0
    dry_run_order_rejected_count: int = 0
    dry_run_order_duplicate_count: int = 0
    dry_run_order_live_would_pass_count: int = 0
    dry_run_order_live_would_reject_count: int = 0
    dry_run_entry_order_intent_count: int = 0
    dry_run_exit_order_intent_count: int = 0
    dry_run_sell_order_intent_count: int = 0
    dry_run_exit_accepted_count: int = 0
    dry_run_exit_rejected_count: int = 0
    dry_run_exit_duplicate_count: int = 0
    dry_run_exit_live_would_pass_count: int = 0
    dry_run_exit_live_would_reject_count: int = 0
    last_dry_run_order_intent_at: str = ""
    last_dry_run_order_reject_reason: str = ""
    last_dry_run_exit_order_intent_at: str = ""
    last_dry_run_exit_order_reject_reason: str = ""
    dry_run_order_policy: str = ""
    dry_run_order_sink_enabled: bool = False
    subscription_active_count: int = 0
    virtual_order_status_change_count: int = 0
    condition_profiles_count: int = 0
    unresolved_condition_profiles_count: int = 0
    active_theme_count: int = 0
    watch_theme_count: int = 0
    candidate_theme_count: int = 0
    theme_active_stock_count: int = 0
    theme_last_sync_at: str = ""
    theme_last_tick_at: str = ""
    theme_ws_client_count: int = 0
    top_theme_name: str = ""
    top_theme_score: float = 0.0
    theme_engine_status: str = "stopped"
    theme_data_status: str = "warming"
    active_candidates_with_active_theme: int = 0
    active_candidates_without_active_theme: int = 0
    theme_context_coverage_pct: float = 0.0
    quality_actionable_count: int = 0
    quality_discovery_only_count: int = 0
    quality_unmapped_count: int = 0
    quality_invalid_code_count: int = 0
    quality_data_wait_count: int = 0
    market_session_status: str = MARKET_SESSION_OPEN
    data_warmup_status: str = DATA_WARMUP_READY
    gate_skip_reason: str = ""
    candidate_subscription_selected_count: int = 0
    candidate_subscription_skipped_discovery_count: int = 0
    candidate_subscription_skipped_unmapped_count: int = 0
    protected_subscription_usage: str = ""
    reason_summary: dict = field(default_factory=dict)
    candidate_generation_summary: dict = field(default_factory=dict)
    context_history_prune_summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class StrategyRuntime:
    """Mock/replay OBSERVE orchestration.

    PR 2-2 only mutates candidates for strategy evaluation state
    (WATCHING/READY/BLOCKED/EXPIRED). Virtual order and position progress stays
    in VirtualOrder/VirtualPosition, not in CandidateState.ORDER_*.
    """

    def __init__(
        self,
        *,
        db,
        candidate_collector: CandidateCollector,
        subscription_manager: RealTimeSubscriptionManager,
        candle_builder: CandleBuilder,
        gate_pipeline: GatePipeline,
        entry_plan_builder: EntryPlanBuilder,
        virtual_order_service: VirtualOrderService,
        virtual_position_service: VirtualPositionService,
        exit_decision_engine: ExitDecisionEngine,
        trade_review_service: TradeReviewService,
        config: Optional[StrategyRuntimeConfig] = None,
        clock=None,
        condition_adapter=None,
        holding_provider: Optional[HoldingProvider] = None,
        order_sink=None,
        theme_lab_pipeline: Optional[ThemeLabRuntimePipeline] = None,
    ) -> None:
        self.db = db
        self.candidate_collector = candidate_collector
        self.subscription_manager = subscription_manager
        self.candle_builder = candle_builder
        self.gate_pipeline = gate_pipeline
        self.entry_plan_builder = entry_plan_builder
        self.virtual_order_service = virtual_order_service
        self.virtual_position_service = virtual_position_service
        self.exit_decision_engine = exit_decision_engine
        self.trade_review_service = trade_review_service
        self.config = config or StrategyRuntimeConfig()
        self.clock = clock or datetime.now
        self.condition_adapter = condition_adapter
        self.holding_provider = holding_provider or StaticHoldingProvider()
        self.order_sink = order_sink
        self.theme_lab_pipeline = theme_lab_pipeline
        self._theme_lab_bridge_results: list[GatePipelineResult] = []
        self.started = False
        self._warnings: list[str] = []
        self.startup_warnings: list[str] = []
        self.readiness_report: Optional[ReadinessReport] = None
        self._theme_presence_cache: dict[str, bool] = {}
        self._last_runtime_time = _clean_time(self.clock())
        if hasattr(self.candidate_collector, "set_condition_event_allowed"):
            self.candidate_collector.set_condition_event_allowed(self._condition_events_allowed)

    def start(
        self,
        now: Optional[datetime] = None,
        *,
        timing_callback: Optional[Callable[[str, float], None]] = None,
    ) -> StrategyRuntimeSnapshot:
        total_started = perf_counter()

        def timed(label: str, callback):
            started = perf_counter()
            try:
                return callback()
            finally:
                if timing_callback is not None:
                    timing_callback(label, perf_counter() - started)

        current = timed("prepare", lambda: _clean_time(now or self.clock()))
        self._last_runtime_time = current

        def prepare_snapshot() -> StrategyRuntimeSnapshot:
            self._warnings = dedupe_warnings(list(self.startup_warnings) + self.config.validate())
            self.subscription_manager.max_codes = self.config.realtime_subscription_limit
            self.started = True
            return self._snapshot(current)

        snapshot = timed("validate_config", prepare_snapshot)
        timed("condition_adapter_start", lambda: self._start_condition_adapter(snapshot, current))

        def recover_state():
            trade_date = current.date().isoformat()
            self._run_theme_lab_flow(snapshot, current)
            self._apply_flow_diagnostics(snapshot, current, trade_date, [])
            if snapshot.gate_skip_reason != GATE_SKIP_MARKET_SESSION_CLOSED:
                self._rollover_previous_trade_date_candidates(trade_date, current, snapshot)
            active = self._active_candidates(trade_date)
            subscription_candidates = [] if snapshot.gate_skip_reason == GATE_SKIP_MARKET_SESSION_CLOSED else self._subscription_candidates(trade_date, snapshot)
            submitted_orders = self.db.list_virtual_orders_by_status(VirtualOrderStatus.SUBMITTED)
            filled_orders = [
                order
                for order in self.db.list_virtual_orders_by_status(VirtualOrderStatus.FILLED)
                if order.id is not None and self.db.load_virtual_position_by_order(order.id) is None
            ]
            open_items = self.db.list_open_virtual_positions()
            snapshot.candidate_count = len(self.db.list_candidates(trade_date))
            snapshot.active_candidate_count = len(active)
            snapshot.recovered_candidate_count = len(active)
            snapshot.virtual_order_count = len(submitted_orders)
            snapshot.filled_order_count = len(filled_orders)
            snapshot.open_position_count = len(open_items)
            snapshot.warnings.extend(
                [
                    f"RECOVERED_ACTIVE_CANDIDATES={len(active)}",
                    f"RECOVERED_SUBMITTED_VIRTUAL_ORDERS={len(submitted_orders)}",
                    f"RECOVERED_FILLED_WITHOUT_POSITION={len(filled_orders)}",
                    f"RECOVERED_OPEN_POSITIONS={len(open_items)}",
                ]
            )
            return subscription_candidates

        subscription_candidates = timed("recover_db_state", recover_state)
        timed("reconcile_realtime_subscriptions", lambda: self._reconcile_subscriptions(subscription_candidates, snapshot))
        timed("readiness_snapshot", lambda: self._refresh_readiness_snapshot(snapshot, current))
        snapshot.warnings = dedupe_warnings(snapshot.warnings)
        if timing_callback is not None:
            timing_callback("total", perf_counter() - total_started)
        return snapshot

    def stop(self) -> StrategyRuntimeSnapshot:
        warnings: list[str] = []
        if self.condition_adapter is not None:
            try:
                warnings.extend(self.condition_adapter.stop())
            except Exception as exc:
                warnings.append(f"CONDITION_ADAPTER_STOP_FAILED:{exc}")
        self.started = False
        return StrategyRuntimeSnapshot(started=False, cycle_at=_clean_time(self.clock()).isoformat(), warnings=warnings)

    def cycle(
        self,
        now: Optional[datetime] = None,
        *,
        timing_callback: Optional[Callable[[str, float], None]] = None,
    ) -> StrategyRuntimeSnapshot:
        started = perf_counter()

        def timed(label: str, callback):
            if timing_callback is not None:
                timing_callback(f"{label}:start", 0.0)
            step_started = perf_counter()
            try:
                return callback()
            finally:
                if timing_callback is not None:
                    timing_callback(label, perf_counter() - step_started)

        current = timed("prepare", lambda: _clean_time(now or self.clock()))
        self._last_runtime_time = current
        snapshot = timed("snapshot", lambda: self._snapshot(current))
        timed("drain_candidate_warnings", lambda: self._drain_candidate_collector_warnings(snapshot))
        try:
            if not self.started:
                snapshot.warnings.append("RUNTIME_NOT_STARTED")
                return snapshot

            trade_date = timed("trade_date", self.candidate_collector._trade_date)
            timed("prune_position_context_history", lambda: self._prune_position_context_history(snapshot, current))
            timed("theme_lab_flow", lambda: self._run_theme_lab_flow(snapshot, current))
            timed("flow_diagnostics_empty", lambda: self._apply_flow_diagnostics(snapshot, current, trade_date, []))
            if snapshot.gate_skip_reason == GATE_SKIP_MARKET_SESSION_CLOSED:
                snapshot.candidate_count = timed("candidate_count", lambda: len(self.db.list_candidates(trade_date)))
                snapshot.active_candidate_count = timed("active_candidates_closed", lambda: len(self._active_candidates(trade_date, current)))
                timed("condition_adapter_stop_closed", lambda: self._stop_condition_adapter_for_market_closed(snapshot))
                timed("reconcile_subscriptions_closed", lambda: self._reconcile_subscriptions([], snapshot))
                timed("readiness_snapshot_closed", lambda: self._refresh_readiness_snapshot(snapshot, current, trade_date))
                snapshot.warnings = timed("dedupe_warnings", lambda: dedupe_warnings(snapshot.warnings))
                return snapshot

            timed("condition_adapter_retry", lambda: self._retry_condition_adapter_start(snapshot, current))
            timed("rollover_candidates", lambda: self._rollover_previous_trade_date_candidates(trade_date, current, snapshot))
            expired = timed("expire_stale", lambda: self.candidate_collector.expire_stale(current, keep_alive=self._candidate_expire_keep_alive))
            snapshot.expired_count += len(expired)
            snapshot.candidate_save_count += len(expired)
            snapshot.db_write_count_per_cycle += len(expired)
            timed("quality_controls", lambda: self._apply_quality_controls(trade_date, current, snapshot))
            subscription_candidates = timed("subscription_candidates", lambda: self._subscription_candidates(trade_date, snapshot))
            snapshot.candidate_count = timed("candidate_count", lambda: len(self.db.list_candidates(trade_date)))
            timed("reconcile_subscriptions", lambda: self._reconcile_subscriptions(subscription_candidates, snapshot))
            timed("readiness_snapshot", lambda: self._refresh_readiness_snapshot(snapshot, current, trade_date))
            candidates = timed("active_candidates", lambda: self._active_candidates(trade_date, current))
            snapshot.active_candidate_count = len(candidates)
            timed("flow_diagnostics_candidates", lambda: self._apply_flow_diagnostics(snapshot, current, trade_date, candidates))
            if snapshot.gate_skip_reason == GATE_SKIP_DATA_WARMUP:
                snapshot.evaluated_candidate_count = 0
                timed("readiness_snapshot_warmup", lambda: self._refresh_readiness_snapshot(snapshot, current, trade_date))
                snapshot.warnings = timed("dedupe_warnings", lambda: dedupe_warnings(snapshot.warnings))
                return snapshot
            snapshot.evaluated_candidate_count = timed(
                "evaluated_candidate_count",
                lambda: len([candidate for candidate in candidates if self._candidate_entry_evaluable(candidate)]),
            )

            gate_results = timed("evaluate_gates", lambda: self._evaluate_gates(candidates, snapshot))
            snapshot.gate_result_count = len(gate_results)
            context_by_candidate: dict[int, _ReviewContext] = {}
            lifecycle_changed = timed("apply_lifecycle", lambda: self._apply_lifecycle(candidates, gate_results, snapshot, current))
            for candidate_id, result in lifecycle_changed.items():
                context_by_candidate[candidate_id] = _ReviewContext(gate_result=result, review_needed=True)

            def process_entries() -> None:
                for result in self._entry_results(gate_results):
                    if result.candidate_id is None:
                        continue
                    context = context_by_candidate.setdefault(result.candidate_id, _ReviewContext(gate_result=result))
                    context.gate_result = result
                    try:
                        self._process_gate_result(result, context, snapshot, current)
                    except Exception as exc:
                        snapshot.warnings.append(f"CANDIDATE_PROCESS_FAILED:{result.code}:{exc}")

            timed("process_entries", process_entries)
            timed("evaluate_virtual_orders", lambda: self._evaluate_virtual_orders(context_by_candidate, snapshot, current))
            timed("open_filled_orders", lambda: self._open_filled_orders(context_by_candidate, snapshot, current))
            timed("evaluate_positions", lambda: self._evaluate_positions(context_by_candidate, snapshot, current))
            timed("save_reviews", lambda: self._save_reviews(context_by_candidate, expired, snapshot, current))
            timed("readiness_snapshot_final", lambda: self._refresh_readiness_snapshot(snapshot, current, trade_date))
            snapshot.warnings = timed("dedupe_warnings", lambda: dedupe_warnings(snapshot.warnings))
            return snapshot
        finally:
            self._apply_reason_summary(snapshot)
            self._apply_context_history_prune_snapshot(snapshot)
            snapshot.cycle_duration_ms = int(round((perf_counter() - started) * 1000))

    def _process_gate_result(
        self,
        result: GatePipelineResult,
        context: "_ReviewContext",
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        if not result.strategy_eligible:
            context.review_needed = result.block_type != BlockType.NONE
            return
        if result.candidate_id is None:
            return
        candidate = self.db.load_candidate_by_id(result.candidate_id)
        if candidate is None:
            return
        if not self._entry_allowed_for_candidate(candidate, result):
            return
        plan = self.entry_plan_builder.build(result, now)
        if plan is None:
            context.review_needed = True
            return
        plan.fill_policy = self.config.virtual_fill_policy
        gate_result_key = str(plan.cancel_condition.get("gate_result_key") or "")
        existing_plan = self.db.find_entry_plan(
            result.candidate_id,
            str(plan.cancel_condition.get("theme_id") or ""),
            gate_result_key,
            plan.entry_type,
        )
        if existing_plan is not None:
            plan = existing_plan
        else:
            plan = self.db.save_entry_plan(plan)
            snapshot.entry_plan_count += 1
            snapshot.db_write_count_per_cycle += 1
            context.review_needed = True
        context.entry_plan = plan
        if not plan.cancel_condition.get("submittable", True):
            context.review_needed = True
            return
        existing_order = self.db.find_active_virtual_order(
            result.candidate_id,
            str(plan.cancel_condition.get("theme_id") or ""),
            plan.entry_type,
        )
        if existing_order is not None:
            context.virtual_order = existing_order
            self._emit_entry_order_intent(candidate, result, plan, existing_order, context, snapshot, now)
            return
        submitted = self.virtual_order_service.submit_virtual_order(plan, now)
        if submitted.submitted and submitted.order is not None:
            if submitted.order.id is not None:
                context.virtual_order = submitted.order
            else:
                context.virtual_order = self.db.save_virtual_order(submitted.order)
                snapshot.db_write_count_per_cycle += 1
        if submitted.submitted and context.virtual_order is not None:
            snapshot.virtual_order_count += 1
            context.review_needed = True
            self._emit_entry_order_intent(candidate, result, plan, context.virtual_order, context, snapshot, now)

    def _start_condition_adapter(self, snapshot: StrategyRuntimeSnapshot, now: datetime) -> None:
        if not self.config.condition_profiles_enabled or self.condition_adapter is None:
            return
        if self._market_session_status(now) == MARKET_SESSION_CLOSED:
            snapshot.warnings.append("CONDITION_ADAPTER_SKIPPED_MARKET_CLOSED")
            self._stop_condition_adapter_for_market_closed(snapshot)
            return
        try:
            snapshot.warnings.extend(self.condition_adapter.start(now))
        except Exception as exc:
            snapshot.warnings.append(f"CONDITION_ADAPTER_START_FAILED:{exc}")

    def _retry_condition_adapter_start(self, snapshot: StrategyRuntimeSnapshot, now: datetime) -> None:
        if self.condition_adapter is None:
            return
        registered = getattr(self.condition_adapter, "registered_conditions", None)
        if not isinstance(registered, dict) or registered:
            return
        self._start_condition_adapter(snapshot, now)

    def _drain_candidate_collector_warnings(self, snapshot: StrategyRuntimeSnapshot) -> None:
        warnings = list(getattr(self.candidate_collector, "warnings", []) or [])
        if not warnings:
            return
        snapshot.warnings.extend(warnings)
        try:
            self.candidate_collector.warnings.clear()
        except AttributeError:
            pass

    def _apply_lifecycle(
        self,
        candidates: list[Candidate],
        gate_results: list[GatePipelineResult],
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> dict[int, GatePipelineResult]:
        results_by_candidate: dict[int, list[GatePipelineResult]] = {}
        for result in gate_results:
            if result.candidate_id is not None:
                results_by_candidate.setdefault(result.candidate_id, []).append(result)

        changed: dict[int, GatePipelineResult] = {}
        for candidate in candidates:
            if candidate.id is None:
                continue
            current = self.db.load_candidate_by_id(candidate.id) or candidate
            results = results_by_candidate.get(candidate.id, [])
            try:
                changed_result = self._apply_candidate_lifecycle(current, results, now, snapshot)
                if changed_result is not None:
                    changed[candidate.id] = changed_result
            except Exception as exc:
                snapshot.warnings.append(f"CANDIDATE_LIFECYCLE_FAILED:{candidate.code}:{exc}")
        return changed

    def _apply_candidate_lifecycle(
        self,
        candidate: Candidate,
        results: list[GatePipelineResult],
        now: datetime,
        snapshot: StrategyRuntimeSnapshot,
    ) -> Optional[GatePipelineResult]:
        previous_signature = _lifecycle_signature(candidate)
        previous_persist_signature = _candidate_persist_signature(candidate)
        recheck_due = _blocked_recheck_due(candidate, now, self._has_open_virtual_activity(candidate))
        previous_reasons = _candidate_reason_codes(candidate)
        metadata = dict(candidate.metadata)
        metadata["quality_status"] = self._candidate_quality_status(candidate)

        if not results:
            if _candidate_entry_excluded(candidate):
                metadata["sub_status"] = "DISCOVERY_ONLY"
                metadata["quality_status"] = QUALITY_DISCOVERY_ONLY
                metadata["insufficient_reason"] = ["ENTRY_EXCLUDED_DISCOVERY_ONLY"]
                metadata["subscription_excluded_reason"] = "discovery_only"
                candidate.metadata = metadata
                if candidate.state == CandidateState.DETECTED:
                    CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
                candidate.block_type = BlockType.NONE
                candidate.can_recover = False
                candidate.recheck_after_sec = 0
                self._save_candidate_if_changed(candidate, previous_persist_signature, snapshot)
                return None
            no_gate_reasons = self._no_gate_result_reasons(candidate, snapshot)
            metadata["sub_status"] = "DATA_INSUFFICIENT"
            metadata["quality_status"] = QUALITY_DATA_WAIT
            metadata["insufficient_reason"] = no_gate_reasons
            candidate.metadata = metadata
            snapshot.warnings.extend(reason for reason in no_gate_reasons if reason != "NO_GATE_RESULT")
            if candidate.state in {CandidateState.READY, CandidateState.BLOCKED}:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
                candidate.block_type = BlockType.NONE
                candidate.can_recover = False
                candidate.recheck_after_sec = 0
            self._save_candidate_if_changed(candidate, previous_persist_signature, snapshot)
            return None

        evaluated_at = _clean_time(now).isoformat()
        metadata["gate_results_by_theme"] = {
            result.theme_id: _gate_result_record(result, evaluated_at) for result in results
        }
        metadata["block_reasons_by_theme"] = {
            result.theme_id: _block_record(result) for result in results if result.block_type != BlockType.NONE
        }

        eligible_results = [result for result in results if result.strategy_eligible]
        best_result = _best_result(eligible_results or results)
        metadata["last_gate_evaluated_at"] = evaluated_at

        if eligible_results:
            best_result = _best_result(eligible_results)
            metadata["best_theme_id"] = best_result.theme_id
            metadata["best_gate_result_key"] = _gate_result_key(best_result)
            metadata["sub_status"] = "PASS"
            metadata["quality_status"] = QUALITY_DISCOVERY_ONLY if _candidate_entry_excluded(candidate) else QUALITY_ACTIONABLE
            if candidate.state == CandidateState.DETECTED:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
            elif candidate.state in {CandidateState.WATCHING, CandidateState.BLOCKED, CandidateState.READY}:
                if candidate.state == CandidateState.BLOCKED and not _blocked_recoverable(candidate):
                    return None
                CandidateLifecycle.transition(candidate, CandidateState.READY)
            candidate.block_type = BlockType.NONE
            candidate.can_recover = False
            candidate.recheck_after_sec = 0
            metadata.pop("next_recheck_at", None)
        elif _has_data_insufficient(results):
            metadata.pop("best_theme_id", None)
            metadata.pop("best_gate_result_key", None)
            metadata["sub_status"] = "DATA_INSUFFICIENT"
            metadata["quality_status"] = QUALITY_DISCOVERY_ONLY if _candidate_entry_excluded(candidate) else QUALITY_DATA_WAIT
            insufficient_reasons = _group_reason_codes(results)
            metadata["insufficient_reason"] = insufficient_reasons
            snapshot.warnings.extend(
                reason for reason in insufficient_reasons if reason in {"INDEX_DATA_INSUFFICIENT", "INDICATOR_DATA_INSUFFICIENT"}
            )
            if candidate.state in {CandidateState.READY, CandidateState.BLOCKED, CandidateState.DETECTED}:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
            candidate.block_type = BlockType.NONE
            candidate.can_recover = False
            candidate.recheck_after_sec = 0
        elif _all_final_block(results):
            metadata.pop("best_theme_id", None)
            metadata.pop("best_gate_result_key", None)
            metadata["sub_status"] = best_result.details.get("sub_status", "FINAL_BLOCK")
            metadata["quality_status"] = QUALITY_DISCOVERY_ONLY if _candidate_entry_excluded(candidate) else QUALITY_ACTIONABLE
            if candidate.state == CandidateState.DETECTED:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
                candidate.block_type = BlockType.NONE
                candidate.can_recover = False
                candidate.recheck_after_sec = 0
            else:
                CandidateLifecycle.transition(candidate, CandidateState.BLOCKED)
                candidate.block_type = BlockType.FINAL
                candidate.can_recover = False
                candidate.recheck_after_sec = 0
                _set_block_metadata(metadata, candidate, best_result, now, recheck=False)
        elif _has_temporary_block(results):
            metadata.pop("best_theme_id", None)
            metadata.pop("best_gate_result_key", None)
            metadata["sub_status"] = best_result.details.get("sub_status", "WAIT")
            metadata["quality_status"] = QUALITY_DISCOVERY_ONLY if _candidate_entry_excluded(candidate) else QUALITY_ACTIONABLE
            if candidate.state == CandidateState.DETECTED:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
                candidate.block_type = BlockType.NONE
                candidate.can_recover = False
                candidate.recheck_after_sec = 0
            else:
                CandidateLifecycle.transition(candidate, CandidateState.BLOCKED)
                candidate.block_type = BlockType.TEMPORARY
                candidate.can_recover = True
                candidate.recheck_after_sec = best_result.recheck_after_sec or 60
                _set_block_metadata(metadata, candidate, best_result, now, recheck=True)
        else:
            metadata.pop("best_theme_id", None)
            metadata.pop("best_gate_result_key", None)
            metadata["sub_status"] = best_result.details.get("sub_status", "LOW_SCORE")
            metadata["quality_status"] = QUALITY_DISCOVERY_ONLY if _candidate_entry_excluded(candidate) else QUALITY_ACTIONABLE
            if candidate.state in {CandidateState.READY, CandidateState.BLOCKED, CandidateState.DETECTED}:
                CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
            candidate.block_type = BlockType.NONE
            candidate.can_recover = False
            candidate.recheck_after_sec = 0

        candidate.metadata = metadata
        events = self._lifecycle_events(candidate, previous_signature, previous_reasons, recheck_due, best_result, now)
        if events:
            self.db.save_candidate_with_events(candidate, events)
            snapshot.candidate_save_count += 1
            snapshot.db_write_count_per_cycle += 1
            return best_result
        self._save_candidate_if_changed(candidate, previous_persist_signature, snapshot)
        return None

    def _lifecycle_events(
        self,
        candidate: Candidate,
        previous_signature: tuple,
        previous_reasons: list[str],
        recheck_due: bool,
        result: GatePipelineResult,
        now: datetime,
    ) -> list[CandidateEvent]:
        events: list[CandidateEvent] = []
        current_signature = _lifecycle_signature(candidate)
        payload = _lifecycle_payload(candidate, result, previous_reasons)
        if recheck_due:
            events.append(self._candidate_event("candidate_block_rechecked", candidate, candidate.state, candidate.state, "blocked candidate rechecked", payload, now))
        if current_signature == previous_signature:
            return events
        if candidate.state == CandidateState.READY:
            event_type = "candidate_ready"
            reason = "strategy eligible"
        elif candidate.state == CandidateState.BLOCKED and candidate.block_type == BlockType.FINAL:
            event_type = "candidate_blocked_final"
            reason = "final strategy block"
        elif candidate.state == CandidateState.BLOCKED and candidate.block_type == BlockType.TEMPORARY:
            event_type = "candidate_blocked_temp"
            reason = "temporary strategy block"
        elif candidate.state == CandidateState.WATCHING and previous_signature[0] == CandidateState.BLOCKED.value:
            event_type = "candidate_recovered"
            reason = "candidate returned to watching"
        else:
            event_type = "state_changed"
            reason = "candidate lifecycle updated"
        events.append(
            self._candidate_event(
                event_type,
                candidate,
                CandidateState(previous_signature[0]),
                candidate.state,
                reason,
                payload,
                now,
            )
        )
        return events

    def _candidate_event(
        self,
        event_type: str,
        candidate: Candidate,
        from_state: Optional[CandidateState],
        to_state: Optional[CandidateState],
        reason: str,
        payload: dict,
        now: datetime,
    ) -> CandidateEvent:
        return CandidateEvent(
            candidate_id=candidate.id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            source=None,
            reason=reason,
            created_at=_clean_time(now).isoformat(),
            payload=payload,
        )

    def _save_candidate_if_changed(
        self,
        candidate: Candidate,
        previous_signature: tuple,
        snapshot: StrategyRuntimeSnapshot,
    ) -> bool:
        if _candidate_persist_signature(candidate) == previous_signature:
            return False
        self.db.save_candidate(candidate)
        snapshot.candidate_save_count += 1
        snapshot.db_write_count_per_cycle += 1
        return True

    def _no_gate_result_reasons(self, candidate: Candidate, snapshot: StrategyRuntimeSnapshot) -> list[str]:
        reasons = ["NO_GATE_RESULT"]
        provider = getattr(self.gate_pipeline, "theme_context_provider", None)
        if provider is not None and not provider.is_ready():
            reasons.append("THEME_CONTEXT_NOT_READY")
        elif provider is not None and not provider.themes_for_code(candidate.code):
            reasons.append("NO_ACTIVE_THEME")
        if "NO_ACTIVE_THEME_FOR_ACTIVE_CANDIDATES" in snapshot.warnings:
            reasons.append("NO_ACTIVE_THEME_FOR_ACTIVE_CANDIDATES")
        if any(str(warning).startswith("CONDITION_PROFILE_UNRESOLVED") for warning in snapshot.warnings):
            reasons.append("CONDITION_PROFILE_UNRESOLVED")
        return _dedupe(reasons)

    def _apply_quality_controls(self, trade_date: str, now: datetime, snapshot: StrategyRuntimeSnapshot) -> None:
        for candidate in self._runtime_candidates(trade_date):
            if candidate.id is None or self._has_open_virtual_activity(candidate):
                continue
            quality_status = self._candidate_quality_status(candidate)
            if quality_status == QUALITY_INVALID_CODE:
                self._remove_invalid_candidate(candidate, now, snapshot)
            elif quality_status == QUALITY_UNMAPPED:
                self._block_unmapped_candidate(candidate, now, snapshot)

    def _remove_invalid_candidate(self, candidate: Candidate, now: datetime, snapshot: StrategyRuntimeSnapshot) -> None:
        previous_signature = _candidate_persist_signature(candidate)
        previous_state = candidate.state
        metadata = dict(candidate.metadata or {})
        metadata["quality_status"] = QUALITY_INVALID_CODE
        metadata["quality_reason"] = "invalid_stock_code"
        metadata["sub_status"] = "INVALID_CODE"
        metadata["insufficient_reason"] = ["INVALID_CODE"]
        candidate.metadata = metadata
        if candidate.state != CandidateState.REMOVED:
            CandidateLifecycle.transition(candidate, CandidateState.REMOVED)
        candidate.block_type = BlockType.NONE
        candidate.can_recover = False
        candidate.recheck_after_sec = 0
        if _candidate_persist_signature(candidate) == previous_signature:
            return
        self.db.save_candidate_with_events(
            candidate,
            [
                self._candidate_event(
                    "candidate_quality_removed",
                    candidate,
                    previous_state,
                    CandidateState.REMOVED,
                    "invalid candidate code removed from active quality set",
                    {"code": candidate.code, "quality_status": QUALITY_INVALID_CODE},
                    now,
                )
            ],
        )
        snapshot.candidate_save_count += 1
        snapshot.db_write_count_per_cycle += 1
        snapshot.warnings.append(f"INVALID_CANDIDATE_CODE:{candidate.code}")

    def _block_unmapped_candidate(self, candidate: Candidate, now: datetime, snapshot: StrategyRuntimeSnapshot) -> None:
        previous_signature = _candidate_persist_signature(candidate)
        previous_state = candidate.state
        metadata = dict(candidate.metadata or {})
        metadata["quality_status"] = QUALITY_UNMAPPED
        metadata["quality_reason"] = "no_active_dynamic_theme"
        metadata["sub_status"] = "NO_ACTIVE_THEME"
        metadata["insufficient_reason"] = ["NO_ACTIVE_THEME"]
        metadata["block_reasons_by_theme"] = {
            "__quality__": {
                "theme_id": "",
                "gate_result_key": "",
                "final_grade": "C",
                "block_type": BlockType.TEMPORARY.value,
                "reason_codes": ["NO_ACTIVE_THEME"],
                "sub_status": "NO_ACTIVE_THEME",
            }
        }
        metadata.setdefault("blocked_at", _clean_time(now).isoformat())
        metadata.setdefault("next_recheck_at", (_clean_time(now) + timedelta(seconds=60)).isoformat())
        candidate.metadata = metadata
        if candidate.state != CandidateState.BLOCKED:
            CandidateLifecycle.transition(candidate, CandidateState.BLOCKED)
        candidate.block_type = BlockType.TEMPORARY
        candidate.can_recover = True
        candidate.recheck_after_sec = 60
        if _candidate_persist_signature(candidate) == previous_signature:
            return
        event_type = "candidate_quality_blocked"
        self.db.save_candidate_with_events(
            candidate,
            [
                self._candidate_event(
                    event_type,
                    candidate,
                    previous_state,
                    CandidateState.BLOCKED,
                    "candidate blocked until an active dynamic theme exists",
                    {
                        "code": candidate.code,
                        "quality_status": QUALITY_UNMAPPED,
                        "reason_codes": ["NO_ACTIVE_THEME"],
                    },
                    now,
                )
            ],
        )
        snapshot.candidate_save_count += 1
        snapshot.db_write_count_per_cycle += 1
        snapshot.warnings.append("NO_ACTIVE_THEME")

    def _rollover_previous_trade_date_candidates(
        self,
        current_trade_date: str,
        now: datetime,
        snapshot: StrategyRuntimeSnapshot,
    ) -> None:
        rolled = 0
        for candidate in self.db.list_candidates():
            if not candidate.trade_date or candidate.trade_date >= current_trade_date:
                continue
            if candidate.state not in ACTIVE_RUNTIME_STATES and not (
                candidate.state == CandidateState.BLOCKED
                and candidate.block_type == BlockType.TEMPORARY
                and candidate.can_recover
            ):
                continue
            if self._has_unfinished_virtual_activity(candidate):
                continue
            previous_state = candidate.state
            metadata = dict(candidate.metadata or {})
            metadata["session_rollover_at"] = _clean_time(now).isoformat()
            metadata["session_rollover_trade_date"] = current_trade_date
            metadata["sub_status"] = "SESSION_ROLLOVER_EXPIRED"
            metadata["insufficient_reason"] = ["SESSION_ROLLOVER_EXPIRED"]
            candidate.metadata = metadata
            CandidateLifecycle.transition(candidate, CandidateState.EXPIRED)
            candidate.expires_at = _clean_time(now).isoformat()
            candidate.block_type = BlockType.NONE
            candidate.can_recover = False
            candidate.recheck_after_sec = 0
            self.db.save_candidate_with_events(
                candidate,
                [
                    self._candidate_event(
                        "candidate_session_rolled_over",
                        candidate,
                        previous_state,
                        CandidateState.EXPIRED,
                        "previous trade date candidate expired at session rollover",
                        {
                            "code": candidate.code,
                            "candidate_trade_date": candidate.trade_date,
                            "current_trade_date": current_trade_date,
                            "previous_state": previous_state.value,
                        },
                        now,
                    )
                ],
            )
            rolled += 1
        if rolled:
            snapshot.expired_count += rolled
            snapshot.candidate_save_count += rolled
            snapshot.db_write_count_per_cycle += rolled
            snapshot.warnings.append(f"SESSION_ROLLOVER_EXPIRED_CANDIDATES={rolled}")

    def _evaluate_virtual_orders(
        self,
        contexts: dict[int, "_ReviewContext"],
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        for order in self.db.list_virtual_orders_by_status(VirtualOrderStatus.SUBMITTED):
            try:
                plan = self.db.load_entry_plan(order.entry_plan_id) if order.entry_plan_id is not None else None
                if plan is None:
                    snapshot.warnings.append(f"VIRTUAL_ORDER_PLAN_MISSING:{order.id}")
                    continue
                market_data = getattr(self.gate_pipeline, "market_data", None)
                latest_tick = market_data.latest_tick(str(plan.cancel_condition.get("code") or "")) if market_data is not None else None
                result = self.virtual_order_service.evaluate_fill(order, plan, self.candle_builder, now, latest_tick=latest_tick)
                if result.changed:
                    saved_order = self.db.save_virtual_order(result.order)
                    snapshot.virtual_order_status_change_count += 1
                    snapshot.db_write_count_per_cycle += 1
                else:
                    saved_order = result.order
                if saved_order.candidate_id is not None:
                    context = contexts.setdefault(saved_order.candidate_id, _ReviewContext())
                    context.entry_plan = plan
                    context.virtual_order = saved_order
                    if result.changed:
                        context.review_needed = True
                    if result.filled:
                        snapshot.filled_order_count += 1
            except Exception as exc:
                snapshot.warnings.append(f"VIRTUAL_ORDER_EVALUATION_FAILED:{order.id}:{exc}")

    def _open_filled_orders(
        self,
        contexts: dict[int, "_ReviewContext"],
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        for order in self.db.list_virtual_orders_by_status(VirtualOrderStatus.FILLED):
            try:
                if order.id is None or self.db.load_virtual_position_by_order(order.id) is not None:
                    continue
                plan = self.db.load_entry_plan(order.entry_plan_id) if order.entry_plan_id is not None else None
                if plan is None:
                    snapshot.warnings.append(f"FILLED_ORDER_PLAN_MISSING:{order.id}")
                    continue
                opened = self.virtual_position_service.open_from_filled_order(order, plan, now)
                if opened.position is not None and order.candidate_id is not None:
                    context = contexts.setdefault(order.candidate_id, _ReviewContext())
                    context.entry_plan = plan
                    context.virtual_order = order
                    context.virtual_position = opened.position
                    candidate = self.db.load_candidate_by_id(order.candidate_id)
                    if opened.opened and candidate is not None:
                        entry_context = self._exit_context_risk_snapshot(
                            candidate,
                            context,
                            opened.position,
                            _snapshot_for_exit(context),
                            capture_reason="ENTRY",
                            captured_at=now,
                        )
                        self._save_position_context_snapshot(opened.position, candidate, entry_context, "ENTRY", now)
                    if opened.opened:
                        snapshot.open_position_count += 1
                        snapshot.db_write_count_per_cycle += 1
                        context.review_needed = True
                    elif opened.aggregated:
                        snapshot.db_write_count_per_cycle += 1
                        context.review_needed = True
            except Exception as exc:
                snapshot.warnings.append(f"VIRTUAL_POSITION_OPEN_FAILED:{order.id}:{exc}")

    def _evaluate_positions(
        self,
        contexts: dict[int, "_ReviewContext"],
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        for position in self.db.list_open_virtual_positions():
            try:
                candidate = self.db.load_candidate_by_id(position.candidate_id) if position.candidate_id is not None else None
                code = candidate.code if candidate is not None else ""
                performance = self.virtual_position_service.update_performance(position, self.candle_builder, code=code)
                position = performance.position
                context = contexts.setdefault(position.candidate_id or -1, _ReviewContext())
                context.virtual_position = position
                if performance.changed:
                    snapshot.db_write_count_per_cycle += 1
                    context.review_needed = True
                if context.virtual_order is None and position.virtual_order_id is not None:
                    order = self._load_virtual_order(position.virtual_order_id)
                    context.virtual_order = order
                    if order is not None and order.entry_plan_id is not None:
                        context.entry_plan = self.db.load_entry_plan(order.entry_plan_id)
                snapshot_for_exit = _snapshot_for_exit(context)
                existing_decisions = self.db.list_exit_decisions(position.id) if position.id is not None else []
                context_risk = self._exit_context_risk_snapshot(
                    candidate,
                    context,
                    position,
                    snapshot_for_exit,
                    capture_reason="HOLDING_EVAL",
                    captured_at=now,
                )
                self._save_position_context_snapshot(position, candidate, context_risk, "HOLDING_EVAL", now)
                new_decisions = self.exit_decision_engine.evaluate(
                    position,
                    snapshot_for_exit,
                    self.candle_builder,
                    existing_decisions,
                    now,
                    context_risk=context_risk,
                )
                exit_context_risk = self._exit_context_risk_snapshot(
                    candidate,
                    context,
                    position,
                    snapshot_for_exit,
                    capture_reason="EXIT_EVAL",
                    captured_at=now,
                )
                self._save_position_context_snapshot(position, candidate, exit_context_risk, "EXIT_EVAL", now)
                position_details_changed = bool(self.exit_decision_engine.last_details.get("position_details_changed"))
                saved_decisions: list[ExitDecision] = []
                for decision in new_decisions:
                    if decision.details.get("position_closed") is True:
                        position, saved_decision = self.db.close_virtual_position_with_decision(position, decision)
                        snapshot.db_write_count_per_cycle += 2
                    else:
                        saved_decision = self.db.save_exit_decision(decision)
                        snapshot.db_write_count_per_cycle += 1
                    saved_decisions.append(saved_decision)
                    if candidate is not None:
                        self._emit_exit_order_intent(candidate, position, saved_decision, context, snapshot, now)
                if position_details_changed and not position.closed_at:
                    position = self.db.save_virtual_position(position)
                    snapshot.db_write_count_per_cycle += 1
                    context.review_needed = True
                if saved_decisions:
                    context.exit_decisions.extend(existing_decisions + saved_decisions)
                    context.virtual_position = position
                    context.review_needed = True
                    snapshot.exit_decision_count += len(saved_decisions)
            except Exception as exc:
                snapshot.warnings.append(f"POSITION_EVALUATION_FAILED:{position.id}:{exc}")

    def _save_reviews(
        self,
        contexts: dict[int, "_ReviewContext"],
        expired: list[Candidate],
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        if not self.config.review_save_enabled:
            return
        for candidate in expired:
            contexts.setdefault(candidate.id or -1, _ReviewContext()).review_needed = True
        for candidate_id, context in contexts.items():
            if not context.review_needed:
                continue
            try:
                candidate = self.db.load_candidate_by_id(candidate_id)
                if candidate is None:
                    snapshot.warnings.append(f"REVIEW_CANDIDATE_MISSING:{candidate_id}")
                    continue
                decisions = context.exit_decisions
                if not decisions and context.virtual_position is not None and context.virtual_position.id is not None:
                    decisions = self.db.list_exit_decisions(context.virtual_position.id)
                review = self.trade_review_service.build_review(
                    candidate,
                    context.gate_result,
                    context.entry_plan,
                    context.virtual_order,
                    context.virtual_position,
                    decisions,
                    self.candle_builder,
                    now,
                )
                if context.dry_run_entry_order_result or context.dry_run_exit_order_results or context.dry_run_order_result:
                    _attach_dry_run_order_review_details(review, context)
                if not self._review_changed(review):
                    continue
                saved_review = self.db.save_trade_review(review)
                if getattr(saved_review, "id", None) is not None:
                    for order_result in _context_dry_run_order_results(context):
                        if order_result.get("intent_id"):
                            try:
                                self.db.link_runtime_order_intent_review(
                                    str(order_result.get("intent_id") or ""),
                                    int(saved_review.id),
                                )
                            except Exception:
                                pass
                snapshot.review_upsert_count += 1
                snapshot.db_write_count_per_cycle += 1
                snapshot.review_count += 1
            except Exception as exc:
                snapshot.warnings.append(f"REVIEW_SAVE_FAILED:{candidate_id}:{exc}")

    def _review_changed(self, review: TradeReview) -> bool:
        for existing in self.db.list_trade_reviews(review.candidate_id):
            if (
                existing.trade_date == review.trade_date
                and existing.theme_id == review.theme_id
                and existing.review_key == review.review_key
            ):
                return _trade_review_signature(existing) != _trade_review_signature(review)
        return True

    def _evaluate_gates(self, candidates: list[Candidate], snapshot: StrategyRuntimeSnapshot) -> list[GatePipelineResult]:
        if self._theme_lab_flow_active():
            candidate_ids = {candidate.id for candidate in candidates if candidate.id is not None}
            return [result for result in self._theme_lab_bridge_results if result.candidate_id in candidate_ids]
        entry_candidates = [candidate for candidate in candidates if self._candidate_entry_evaluable(candidate)]
        if not entry_candidates:
            return []
        try:
            return list(self._evaluate_gate_pipeline(candidates, entry_candidates))
        except Exception as exc:
            snapshot.warnings.append(f"GATE_PIPELINE_BATCH_FAILED:{exc}")
        results: list[GatePipelineResult] = []
        for candidate in entry_candidates:
            try:
                results.extend(self._evaluate_gate_pipeline(candidates, [candidate]))
            except Exception as exc:
                snapshot.warnings.append(f"GATE_PIPELINE_CANDIDATE_FAILED:{candidate.code}:{exc}")
        return results

    def _evaluate_gate_pipeline(
        self,
        candidates: list[Candidate],
        entry_candidates: list[Candidate],
    ) -> list[GatePipelineResult]:
        if _accepts_entry_candidates(self.gate_pipeline):
            return list(self.gate_pipeline.evaluate(candidates, entry_candidates=entry_candidates))
        return list(self.gate_pipeline.evaluate(entry_candidates))

    def _entry_results(self, gate_results: list[GatePipelineResult]) -> list[GatePipelineResult]:
        selected: list[GatePipelineResult] = []
        for result in gate_results:
            if result.candidate_id is None or not result.strategy_eligible:
                continue
            candidate = self.db.load_candidate_by_id(result.candidate_id)
            if candidate is None:
                continue
            if self._entry_allowed_for_candidate(candidate, result):
                selected.append(result)
        return selected

    @staticmethod
    def _entry_allowed_for_candidate(candidate: Candidate, result: GatePipelineResult) -> bool:
        if _candidate_entry_excluded(candidate):
            return False
        return (
            candidate.state == CandidateState.READY
            and result.strategy_eligible
            and str(candidate.metadata.get("best_gate_result_key") or "") == _gate_result_key(result)
        )

    def _reconcile_subscriptions(self, candidates: list[Candidate], snapshot: StrategyRuntimeSnapshot) -> None:
        try:
            self._sync_virtual_activity_subscriptions()
            if self._theme_lab_flow_active():
                self._sync_theme_lab_watchset_subscriptions(snapshot)
            else:
                self._sync_theme_universe_subscriptions(snapshot)
            for raw_index_code in self.config.index_watch_codes.values():
                self.subscription_manager.ensure_subscription(raw_index_code, "index", protected=True)
            for code in self.config.leader_watch_codes:
                self.subscription_manager.ensure_subscription(code, "leading_stock", protected=True)
            for code in self.config.semiconductor_signal_codes:
                self.subscription_manager.ensure_subscription(code, "semiconductor_signal", protected=True)
            for code in self.config.holding_watch_codes:
                self.subscription_manager.ensure_subscription(code, "holding", protected=True)
            for code in self._holding_codes(snapshot):
                self.subscription_manager.ensure_subscription(code, "holding", protected=True)
            desired_candidates = candidates[: self.config.max_candidates_to_watch]
            if self._theme_lab_flow_active():
                desired_candidates = []
            snapshot.candidate_subscription_selected_count = len(desired_candidates)
            watched = self.subscription_manager.watch_candidates(desired_candidates)
            for candidate in desired_candidates:
                if candidate.state == CandidateState.DETECTED and normalize_code(candidate.code) in watched:
                    self.candidate_collector.mark_watching(candidate.code, trade_date=candidate.trade_date, reason="realtime subscription registered")
                    snapshot.candidate_save_count += 1
                    snapshot.db_write_count_per_cycle += 1
            self._remove_inactive_candidate_subscriptions(desired_candidates)
            snapshot.subscription_active_count = len(self.subscription_manager.code_to_screen)
            protected_count = sum(1 for record in self.subscription_manager.records.values() if record.protected)
            if protected_count > self.subscription_manager.max_codes:
                snapshot.warnings.append(f"PROTECTED_SUBSCRIPTION_COUNT={protected_count}/{self.subscription_manager.max_codes}")
            elif self.subscription_manager.max_codes - protected_count <= 5:
                snapshot.warnings.append(f"PROTECTED_SUBSCRIPTION_NEAR_LIMIT:{protected_count}/{self.subscription_manager.max_codes}")
            snapshot.warnings.extend(self.subscription_manager.warnings)
            snapshot.warnings.append(f"RECONCILED_SUBSCRIPTIONS={len(watched)}")
        except Exception as exc:
            snapshot.warnings.append(f"SUBSCRIPTION_RECONCILE_FAILED:{exc}")

    def _run_theme_lab_flow(self, snapshot: StrategyRuntimeSnapshot, now: datetime) -> None:
        self._theme_lab_bridge_results = []
        if not self._theme_lab_flow_active():
            return
        if self.theme_lab_pipeline is None:
            snapshot.warnings.append("THEME_LAB_FLOW_NOT_WIRED")
            return
        result = self.theme_lab_pipeline.run_if_due(now)
        snapshot.warnings.extend(self.theme_lab_pipeline.drain_warnings())
        bridge_result = result
        if result is None:
            bridge_result = self._fresh_theme_lab_result(now)
        if result is None and bridge_result is None:
            return
        active_result = bridge_result or result
        if active_result is None:
            return
        snapshot.gate_result_count = len(active_result.gate_decisions)
        if not active_result.watchset:
            snapshot.warnings.append("THEME_LAB_WATCHSET_EMPTY")
        if not self.config.theme_lab_dry_run_bridge_enabled:
            snapshot.warnings.append("THEME_LAB_DRY_RUN_BRIDGE_DISABLED")
            return
        if self._market_session_status(now) == MARKET_SESSION_CLOSED:
            snapshot.warnings.append("THEME_LAB_BRIDGE_SKIPPED_MARKET_CLOSED")
            return
        market_data = getattr(self.gate_pipeline, "market_data", None)
        if market_data is None:
            snapshot.warnings.append("THEME_LAB_BRIDGE_MARKET_DATA_MISSING")
            return
        try:
            bridge = ThemeLabDryRunLifecycleBridge(
                db=self.db,
                market_data=market_data,
                default_ttl_minutes=getattr(self.candidate_collector, "default_ttl_minutes", 30),
                settings=getattr(self.entry_plan_builder, "settings", None),
            )
            built = bridge.build(
                active_result,
                trade_date=self.candidate_collector._trade_date(),
                now=now,
            )
            self._theme_lab_bridge_results = built.gate_results
            snapshot.candidate_save_count += built.candidate_save_count
            snapshot.db_write_count_per_cycle += built.candidate_save_count
            snapshot.warnings.extend(built.warnings or [])
        except Exception as exc:
            snapshot.warnings.append(f"THEME_LAB_BRIDGE_FAILED:{exc}")

    def _fresh_theme_lab_result(self, now: datetime):
        if self.theme_lab_pipeline is None:
            return None
        last_result = getattr(self.theme_lab_pipeline, "last_result", None)
        last_run_at = getattr(self.theme_lab_pipeline, "last_run_at", None)
        if last_result is None or last_run_at is None:
            return None
        interval = max(1, int(getattr(self.theme_lab_pipeline, "interval_sec", self.config.theme_lab_pipeline_interval_sec) or 1))
        try:
            age_sec = (_clean_time(now) - _clean_time(last_run_at)).total_seconds()
        except Exception:
            return None
        if age_sec <= max(interval * 2, int(self.config.evaluation_interval_sec) * 2, 5):
            return last_result
        return None

    def _sync_theme_lab_watchset_subscriptions(self, snapshot: StrategyRuntimeSnapshot) -> None:
        if self.theme_lab_pipeline is None:
            snapshot.warnings.append("THEME_LAB_FLOW_NOT_WIRED")
            return
        target_codes = set(self.theme_lab_pipeline.watchset_codes())
        for code, record in list(self.subscription_manager.records.items()):
            if "theme_lab_watchset" in record.sources and code not in target_codes:
                self.subscription_manager.remove_subscription(code, "theme_lab_watchset")
        for code in sorted(target_codes):
            self.subscription_manager.ensure_subscription(code, "theme_lab_watchset", protected=False)
        if target_codes:
            snapshot.warnings.append(f"THEME_LAB_WATCHSET_SUBSCRIPTIONS={len(target_codes)}")

    def _theme_lab_flow_active(self) -> bool:
        return self.config.theme_engine_mode == "themelab_flow" and self.theme_lab_pipeline is not None

    def _sync_theme_universe_subscriptions(self, snapshot: StrategyRuntimeSnapshot) -> None:
        try:
            target_codes = set(ThemeUniverseBuilder(ThemeEngineRepository(self.db)).build_active_universe())
            for code, record in list(self.subscription_manager.records.items()):
                if "theme_universe" in record.sources and code not in target_codes:
                    self.subscription_manager.remove_subscription(code, "theme_universe")
            for code in sorted(target_codes):
                self.subscription_manager.ensure_subscription(code, "theme_universe", protected=False)
            if target_codes:
                snapshot.warnings.append(f"THEME_UNIVERSE_SUBSCRIPTIONS={len(target_codes)}")
        except Exception as exc:
            snapshot.warnings.append(f"THEME_UNIVERSE_SUBSCRIPTION_FAILED:{exc}")

    def _sync_virtual_activity_subscriptions(self) -> None:
        stale_sources = {"virtual_order", "virtual_position"}
        for code, record in list(self.subscription_manager.records.items()):
            for source in list(record.sources):
                if source in stale_sources:
                    self.subscription_manager.remove_subscription(code, source)
        for order in self.db.list_virtual_orders_by_status(VirtualOrderStatus.SUBMITTED):
            candidate = self.db.load_candidate_by_id(order.candidate_id) if order.candidate_id is not None else None
            if candidate is not None:
                self.subscription_manager.ensure_subscription(candidate.code, "virtual_order", protected=True)
        for position in self.db.list_open_virtual_positions():
            candidate = self.db.load_candidate_by_id(position.candidate_id) if position.candidate_id is not None else None
            if candidate is not None:
                self.subscription_manager.ensure_subscription(candidate.code, "virtual_position", protected=True)

    def _remove_inactive_candidate_subscriptions(self, desired_candidates: list[Candidate]) -> None:
        desired_codes = {normalize_code(candidate.code) for candidate in desired_candidates}
        for code, record in list(self.subscription_manager.records.items()):
            if "candidate_watch" not in record.sources:
                continue
            if code in desired_codes:
                continue
            if self._code_has_protected_subscription(code):
                self.subscription_manager.remove_subscription(code, "candidate_watch")
                continue
            self.subscription_manager.remove_subscription(code, "candidate_watch")
        self.subscription_manager.sync()

    def _code_has_protected_subscription(self, code: str) -> bool:
        clean_code = normalize_code(code)
        record = self.subscription_manager.records.get(clean_code)
        if record is None:
            return False
        return any(source != "candidate_watch" and record.source_protected.get(source, False) for source in record.sources)

    def _holding_codes(self, snapshot: StrategyRuntimeSnapshot) -> set[str]:
        try:
            return self.holding_provider.list_holding_codes()
        except Exception as exc:
            snapshot.warnings.append(f"HOLDING_PROVIDER_FAILED:{exc}")
            return set()

    def _candidate_expire_keep_alive(self, candidate: Candidate, now: datetime) -> bool:
        if self._has_open_virtual_activity(candidate):
            return True
        market_data = getattr(self.gate_pipeline, "market_data", None)
        if market_data is None or not hasattr(market_data, "has_recent_tick"):
            return False
        max_age_sec = max(60, int(self.config.evaluation_interval_sec) * 2)
        try:
            return bool(market_data.has_recent_tick(candidate.code, now, max_age_sec))
        except Exception:
            return False

    def _subscription_candidates(self, trade_date: str, snapshot: Optional[StrategyRuntimeSnapshot] = None) -> list[Candidate]:
        result: list[tuple[Candidate, str]] = []
        candidates = self.db.list_candidates(trade_date=trade_date)
        self._theme_presence_cache = _active_theme_presence_by_code(self.db, candidates, default_when_not_ready=True)
        for candidate in candidates:
            has_open_activity = self._has_open_virtual_activity(candidate)
            quality_status = self._candidate_quality_status(candidate)
            if quality_status == QUALITY_DISCOVERY_ONLY:
                if snapshot is not None:
                    snapshot.candidate_subscription_skipped_discovery_count += 1
                continue
            if quality_status in {QUALITY_INVALID_CODE, QUALITY_UNMAPPED}:
                if snapshot is not None and quality_status == QUALITY_UNMAPPED:
                    snapshot.candidate_subscription_skipped_unmapped_count += 1
                continue
            if not has_open_activity and quality_status not in {QUALITY_ACTIONABLE, QUALITY_DATA_WAIT}:
                continue
            if candidate.state in ACTIVE_RUNTIME_STATES:
                result.append((candidate, quality_status))
            elif (
                candidate.state == CandidateState.BLOCKED
                and candidate.block_type == BlockType.TEMPORARY
                and candidate.can_recover
            ):
                result.append((candidate, quality_status))
            elif has_open_activity:
                result.append((candidate, quality_status))
        return [
            candidate
            for candidate, _quality in sorted(
                result,
                key=lambda item: self._candidate_subscription_sort_key(item[0], item[1]),
            )
        ]

    def _active_candidates(self, trade_date: str, now: Optional[datetime] = None) -> list[Candidate]:
        result: list[Candidate] = []
        candidates = self.db.list_candidates(trade_date=trade_date)
        self._theme_presence_cache = _active_theme_presence_by_code(self.db, candidates, default_when_not_ready=True)
        for candidate in candidates:
            if not self._has_open_virtual_activity(candidate) and self._candidate_quality_status(candidate) in {QUALITY_INVALID_CODE, QUALITY_UNMAPPED}:
                continue
            if candidate.state in ACTIVE_RUNTIME_STATES:
                result.append(candidate)
            elif (
                candidate.state == CandidateState.BLOCKED
                and candidate.block_type == BlockType.TEMPORARY
                and candidate.can_recover
                and (now is None or _blocked_recheck_due(candidate, now, self._has_open_virtual_activity(candidate)))
            ):
                result.append(candidate)
        return result

    def _runtime_candidates(self, trade_date: str) -> list[Candidate]:
        result: list[Candidate] = []
        for candidate in self.db.list_candidates(trade_date=trade_date):
            if candidate.state in ACTIVE_RUNTIME_STATES:
                result.append(candidate)
            elif (
                candidate.state == CandidateState.BLOCKED
                and candidate.block_type == BlockType.TEMPORARY
                and candidate.can_recover
            ):
                result.append(candidate)
            elif self._has_open_virtual_activity(candidate):
                result.append(candidate)
        return result

    def _candidate_quality_status(self, candidate: Candidate, has_active_theme: Optional[bool] = None) -> str:
        if has_active_theme is None:
            code = str(candidate.code or "")
            if code in self._theme_presence_cache:
                has_active_theme = self._theme_presence_cache[code]
            else:
                normalized = normalize_code(code)
                if normalized in self._theme_presence_cache:
                    has_active_theme = self._theme_presence_cache[normalized]
        if has_active_theme is None:
            try:
                provider = getattr(self.gate_pipeline, "theme_context_provider", None)
                if provider is None:
                    has_active_theme = True
                else:
                    has_active_theme = bool(provider.themes_for_code(candidate.code))
            except Exception:
                has_active_theme = False
        return candidate_quality_status(candidate, has_active_theme)

    def _candidate_entry_evaluable(self, candidate: Candidate) -> bool:
        if _candidate_entry_excluded(candidate):
            return False
        return self._candidate_quality_status(candidate) not in {QUALITY_INVALID_CODE, QUALITY_UNMAPPED}

    def _candidate_subscription_sort_key(self, candidate: Candidate, quality: Optional[str] = None) -> tuple[int, int, str, str]:
        quality = quality or self._candidate_quality_status(candidate)
        blocked = candidate.state == CandidateState.BLOCKED
        quality_priority = {
            QUALITY_ACTIONABLE: 0,
            QUALITY_DATA_WAIT: 1,
        }.get(quality, 9)
        if blocked:
            quality_priority = max(quality_priority, 2)
        return (
            quality_priority,
            _candidate_state_priority(candidate.state),
            _last_seen_desc_value(candidate),
            normalize_code(candidate.code),
        )

    def _has_open_virtual_activity(self, candidate: Candidate) -> bool:
        if candidate.id is None:
            return False
        if self.db.load_open_virtual_position(candidate.id) is not None:
            return True
        return any(
            order.status == VirtualOrderStatus.SUBMITTED
            for order in self.db.list_virtual_orders(candidate.id)
        )

    def _has_unfinished_virtual_activity(self, candidate: Candidate) -> bool:
        if candidate.id is None:
            return False
        if self.db.load_open_virtual_position(candidate.id) is not None:
            return True
        for order in self.db.list_virtual_orders(candidate.id):
            if order.status == VirtualOrderStatus.SUBMITTED:
                return True
            if order.status == VirtualOrderStatus.FILLED and not self._virtual_order_has_position(candidate, order):
                return True
        return False

    def _virtual_order_has_position(self, candidate: Candidate, order: VirtualOrder) -> bool:
        if order.id is None:
            return False
        if self.db.load_virtual_position_by_order(order.id) is not None:
            return True
        if candidate.id is None:
            return False
        for position in self.db.list_virtual_positions(candidate.id):
            filled_order_ids = position.details.get("filled_order_ids") or []
            if order.id in filled_order_ids:
                return True
        return False

    def _load_virtual_order(self, order_id: int) -> Optional[VirtualOrder]:
        for status in [VirtualOrderStatus.SUBMITTED, VirtualOrderStatus.FILLED, VirtualOrderStatus.UNFILLED, VirtualOrderStatus.CANCELLED]:
            for order in self.db.list_virtual_orders_by_status(status):
                if order.id == order_id:
                    return order
        return None

    def _latest_virtual_order_for_plan(self, plan: EntryPlan) -> Optional[VirtualOrder]:
        if plan.id is None or plan.candidate_id is None:
            return None
        orders = [
            order
            for order in self.db.list_virtual_orders(plan.candidate_id)
            if order.entry_plan_id == plan.id
        ]
        return sorted(orders, key=lambda order: order.id or 0, reverse=True)[0] if orders else None

    def _apply_flow_diagnostics(
        self,
        snapshot: StrategyRuntimeSnapshot,
        current: datetime,
        trade_date: str,
        candidates: list[Candidate],
    ) -> None:
        snapshot.market_session_status = self._market_session_status(current)
        if snapshot.market_session_status == MARKET_SESSION_CLOSED:
            snapshot.data_warmup_status = DATA_WARMUP_CLOSED
            snapshot.gate_skip_reason = GATE_SKIP_MARKET_SESSION_CLOSED
            snapshot.warnings.append(GATE_SKIP_MARKET_SESSION_CLOSED)
            return
        snapshot.data_warmup_status = self._data_warmup_status(current, trade_date, candidates)
        if snapshot.data_warmup_status != DATA_WARMUP_READY:
            snapshot.gate_skip_reason = GATE_SKIP_DATA_WARMUP
            snapshot.warnings.append(GATE_SKIP_DATA_WARMUP)
            snapshot.warnings.append(f"DATA_WARMUP_STATUS:{snapshot.data_warmup_status}")
            return
        snapshot.gate_skip_reason = ""

    @staticmethod
    def _market_session_status(current: datetime) -> str:
        minutes = current.hour * 60 + current.minute
        open_minutes = 9 * 60
        close_minutes = 15 * 60 + 30
        return MARKET_SESSION_OPEN if open_minutes <= minutes <= close_minutes else MARKET_SESSION_CLOSED

    def _condition_events_allowed(self, current: datetime) -> bool:
        basis = getattr(self, "_last_runtime_time", None) or _clean_time(current)
        return self._market_session_status(basis) == MARKET_SESSION_OPEN

    def _stop_condition_adapter_for_market_closed(self, snapshot: StrategyRuntimeSnapshot) -> None:
        if self.condition_adapter is None or not hasattr(self.condition_adapter, "stop"):
            return
        registered = getattr(self.condition_adapter, "registered_conditions", None)
        if isinstance(registered, dict) and not registered:
            return
        try:
            warnings = self.condition_adapter.stop()
            snapshot.warnings.extend(warnings or [])
            snapshot.warnings.append("CONDITION_ADAPTER_STOPPED_MARKET_CLOSED")
        except Exception as exc:
            snapshot.warnings.append(f"CONDITION_ADAPTER_STOP_MARKET_CLOSED_FAILED:{exc}")

    def _data_warmup_status(self, current: datetime, trade_date: str, candidates: list[Candidate]) -> str:
        market_index_store = getattr(self.gate_pipeline, "market_index_store", None)
        market_data = getattr(self.gate_pipeline, "market_data", None)
        if market_index_store is None or market_data is None:
            return DATA_WARMUP_READY
        for logical_code in self.config.index_watch_codes:
            try:
                if market_index_store.state(logical_code).price <= 0:
                    return DATA_WARMUP_WAITING_INDEX
            except Exception:
                return DATA_WARMUP_WAITING_INDEX
        entry_candidates = [candidate for candidate in (candidates or self._active_candidates(trade_date, current)) if self._candidate_entry_evaluable(candidate)]
        if not entry_candidates:
            return DATA_WARMUP_READY
        max_age_sec = max(60, int(self.config.evaluation_interval_sec) * 4)
        if not any(self._candidate_has_recent_tick(candidate, current, max_age_sec) for candidate in entry_candidates):
            return DATA_WARMUP_WAITING_CANDIDATE_TICKS
        return DATA_WARMUP_READY

    def _candidate_has_recent_tick(self, candidate: Candidate, current: datetime, max_age_sec: int) -> bool:
        market_data = getattr(self.gate_pipeline, "market_data", None)
        if market_data is None or not hasattr(market_data, "has_recent_tick"):
            return True
        try:
            return bool(market_data.has_recent_tick(candidate.code, current, max_age_sec))
        except Exception:
            return False

    def _exit_context_risk_snapshot(
        self,
        candidate: Optional[Candidate],
        context: "_ReviewContext",
        position: VirtualPosition,
        snapshot: Optional["IndicatorSnapshot"],
        *,
        capture_reason: str = "EXIT_EVAL",
        captured_at: Optional[datetime] = None,
    ) -> Optional[ExitContextRiskSnapshot]:
        if not self.config.exit_context_risk_enabled or candidate is None or snapshot is None:
            return None
        captured = (captured_at or datetime.now()).replace(microsecond=0)
        theme_payload = self._latest_theme_lab_payload()
        theme_id = _position_theme_id(candidate, context)
        theme_name = _position_theme_name(candidate, context)
        theme = _find_theme_snapshot(theme_payload, theme_id, theme_name)
        watch = _find_watch_snapshot(theme_payload, candidate.code)
        market = _candidate_index_market(candidate, snapshot)
        index_status, index_return_pct = self._index_risk_status(market)
        theme_status_after = str((theme or {}).get("theme_status") or (watch or {}).get("theme_lab_theme_status") or "")
        theme_score = _float_or_none((theme or {}).get("condition_score") or (theme or {}).get("theme_score"))
        leader_count = _int_or_none((theme or {}).get("leader_count"))
        strong_count = _int_or_none((theme or {}).get("strong_count"))
        previous_details = dict(position.details or {})
        history = self.db.list_position_context_history(position.id, limit=100) if position.id is not None else []
        previous_context = history[-1] if history else None
        current_return_pct = _runtime_return_pct(snapshot.price, position.entry_price)
        previous_theme_score = _float_or_none(previous_context.theme_score if previous_context else previous_details.get("exit_context_theme_score"))
        previous_leader_count = _int_or_none(previous_context.leader_count if previous_context else previous_details.get("exit_context_leader_count"))
        previous_strong_count = _int_or_none(previous_context.strong_count if previous_context else previous_details.get("exit_context_strong_count"))
        breadth_status = _breadth_status(theme, previous_details)
        previous_breadth_status = str(previous_context.breadth_status if previous_context else "")
        previous_index_status = str(previous_context.index_status if previous_context else "")
        previous_market_status = str(previous_context.market_status if previous_context else "")
        previous_theme_status = str(previous_context.theme_status if previous_context else previous_details.get("exit_context_theme_status") or "")
        leader_vwap_broken = bool((watch or {}).get("leader_vwap_broken"))
        previous_leader_vwap_status = str(previous_context.leader_vwap_status if previous_context else "")
        leader_vwap_status = "BROKEN" if leader_vwap_broken else "OK"
        risk_reasons = _context_risk_reasons(
            theme_status_after=theme_status_after,
            theme_score=theme_score,
            previous_theme_score=previous_theme_score,
            leader_count=leader_count,
            previous_leader_count=previous_leader_count,
            strong_count=strong_count,
            previous_strong_count=previous_strong_count,
            leader_return_pct=_float_or_none((theme or {}).get("top_leader_return_pct")),
            index_status=index_status,
            market_status=str((theme_payload.get("market_status") or {}).get("market_status") or ""),
            breadth_status=breadth_status,
        )
        theme_weak_count = _consecutive_theme_weak_count(history, theme_status_after)
        context_count = len(history)
        context_available = context_count > 0
        return ExitContextRiskSnapshot(
            enabled=True,
            theme_id=theme_id,
            theme_name=theme_name,
            theme_status_before=previous_theme_status,
            theme_status_after=theme_status_after,
            theme_score=theme_score,
            previous_theme_score=previous_theme_score,
            leader_symbol=str((theme or {}).get("top_leader_symbol") or ""),
            leader_return_pct=_float_or_none((theme or {}).get("top_leader_return_pct")),
            leader_support_broken=bool((watch or {}).get("leader_support_broken")),
            leader_vwap_broken=leader_vwap_broken,
            leader_count=leader_count,
            previous_leader_count=previous_leader_count,
            strong_count=strong_count,
            previous_strong_count=previous_strong_count,
            index_market=market,
            index_status=index_status,
            index_return_pct=index_return_pct,
            market_status=str((theme_payload.get("market_status") or {}).get("market_status") or ""),
            breadth_status=breadth_status,
            stock_role=str((watch or {}).get("stock_role") or candidate.metadata.get("theme_lab_stock_role") or ""),
            current_return_pct=current_return_pct,
            risk_reason_codes=tuple(risk_reasons),
            calculated_at=str(theme_payload.get("calculated_at") or ""),
            context_history_available=context_available,
            context_history_count=context_count,
            theme_score_delta=_delta(theme_score, previous_theme_score),
            theme_status_transition=_transition(previous_theme_status, theme_status_after),
            leader_count_delta=_int_delta(leader_count, previous_leader_count),
            strong_count_delta=_int_delta(strong_count, previous_strong_count),
            leader_vwap_break_transition=_transition(previous_leader_vwap_status, leader_vwap_status),
            breadth_before=previous_breadth_status,
            breadth_deterioration=_status_deteriorated(previous_breadth_status, breadth_status, weak_values={"LOW_BREADTH", "BREADTH_COLLAPSE", "COLLAPSE"}),
            index_status_before=previous_index_status,
            index_status_deterioration=_status_deteriorated(previous_index_status, index_status, weak_values={"INDEX_WEAK", "RISK_OFF", "WEAK"}),
            market_risk_off_transition=_transition(previous_market_status, str((theme_payload.get("market_status") or {}).get("market_status") or "")),
            theme_weak_consecutive_count=theme_weak_count,
            exit_confidence="HIGH" if context_count >= 2 else ("MEDIUM" if context_count == 1 else "LOW"),
            context_limited_reason="" if context_count > 0 else "DATA_LIMITED_CONTEXT",
        )

    def _save_position_context_snapshot(
        self,
        position: VirtualPosition,
        candidate: Optional[Candidate],
        context: Optional[ExitContextRiskSnapshot],
        capture_reason: str,
        captured_at: datetime,
    ) -> None:
        if context is None or position.id is None or candidate is None:
            return
        details = dict(position.details or {})
        try:
            self.db.save_position_context_snapshot(
                PositionContextSnapshot(
                    position_id=position.id,
                    candidate_id=position.candidate_id,
                    candidate_instance_id=str(details.get("candidate_instance_id") or candidate.metadata.get("candidate_instance_id") or ""),
                    code=candidate.code,
                    trade_date=candidate.trade_date,
                    captured_at=captured_at.replace(microsecond=0).isoformat(),
                    capture_reason=capture_reason,
                    theme_id=context.theme_id,
                    theme_name=context.theme_name,
                    theme_score=context.theme_score,
                    theme_status=context.theme_status_after,
                    leader_count=context.leader_count,
                    strong_count=context.strong_count,
                    breadth_status=context.breadth_status,
                    leader_code=context.leader_symbol,
                    leader_return_pct=context.leader_return_pct,
                    leader_vwap_status="BROKEN" if context.leader_vwap_broken else "OK",
                    leader_support_broken=context.leader_support_broken,
                    index_market=context.index_market,
                    index_status=context.index_status,
                    index_return_pct=context.index_return_pct,
                    market_status=context.market_status,
                    market_risk_status="RISK_OFF" if str(context.market_status or "").upper() == "RISK_OFF" else "",
                    risk_reason_codes=list(context.risk_reason_codes or ()),
                    metadata={
                        "capture_reason": capture_reason,
                        "theme_score_before": context.previous_theme_score,
                        "theme_score_delta": context.theme_score_delta,
                        "theme_status_transition": context.theme_status_transition,
                        "leader_count_delta": context.leader_count_delta,
                        "strong_count_delta": context.strong_count_delta,
                        "context_history_count": context.context_history_count,
                        "exit_confidence": context.exit_confidence,
                    },
                )
            )
        except Exception:
            pass

    def _latest_theme_lab_payload(self) -> dict:
        if self.theme_lab_pipeline is not None:
            last_result = getattr(self.theme_lab_pipeline, "last_result", None)
            if last_result is not None:
                try:
                    from trading.theme_engine.runtime_pipeline import _result_payload

                    return _result_payload(last_result)
                except Exception:
                    pass
        try:
            payload = self.db.latest_theme_lab_flow_result()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _index_risk_status(self, market: str) -> tuple[str, Optional[float]]:
        market_index_store = getattr(self.gate_pipeline, "market_index_store", None)
        if market_index_store is None:
            return "", None
        try:
            state = market_index_store.state(market)
        except Exception:
            return "", None
        change = _float_or_none(getattr(state, "change_rate", None))
        weak_threshold = -1.0 if market == "KOSDAQ" else -0.8
        risk_off_threshold = -2.5 if market == "KOSDAQ" else -2.0
        if change is not None and change <= risk_off_threshold:
            return "RISK_OFF", change
        if change is not None and change <= weak_threshold:
            return "INDEX_WEAK", change
        if getattr(state, "low_break_recent", False) or str(getattr(state, "direction_5m", "")).upper() == "DOWN":
            return "INDEX_WEAK", change
        return "", change

    def _snapshot(self, current: datetime) -> StrategyRuntimeSnapshot:
        snapshot = StrategyRuntimeSnapshot(
            started=self.started,
            cycle_at=current.isoformat(),
            warnings=dedupe_warnings(self._warnings),
        )
        self._apply_order_sink_snapshot(snapshot)
        self._apply_candidate_generation_summary(snapshot)
        self._apply_context_history_prune_snapshot(snapshot)
        return snapshot

    def _prune_position_context_history(self, snapshot: StrategyRuntimeSnapshot, current: datetime) -> None:
        if not _env_bool("TRADING_CONTEXT_HISTORY_PRUNE_ENABLED", True):
            return
        retention_days = _env_int("TRADING_CONTEXT_HISTORY_RETENTION_DAYS", 20)
        batch_size = _env_int("TRADING_CONTEXT_HISTORY_PRUNE_BATCH_SIZE", 1000)
        cutoff = (current - timedelta(days=max(1, retention_days))).replace(microsecond=0).isoformat()
        summary_retention_days = _env_int("TRADING_CONTEXT_HISTORY_SUMMARY_RETENTION_DAYS", 180)
        summary = self.db.prune_position_context_history(
            cutoff_at=cutoff,
            batch_size=batch_size,
            created_at=current.replace(microsecond=0).isoformat(),
            details={
                "retention_days": retention_days,
                "summary_retention_days": summary_retention_days,
                "batch_size": batch_size,
                "source": "strategy_runtime",
            },
        )
        snapshot.context_history_prune_summary = summary

    def _apply_context_history_prune_snapshot(self, snapshot: StrategyRuntimeSnapshot) -> None:
        if snapshot.context_history_prune_summary:
            return
        if not hasattr(self.db, "latest_position_context_prune_summary"):
            return
        try:
            snapshot.context_history_prune_summary = self.db.latest_position_context_prune_summary()
        except Exception as exc:
            snapshot.context_history_prune_summary = {"error": f"CONTEXT_HISTORY_PRUNE_SNAPSHOT_FAILED:{exc}"}

    def _apply_candidate_generation_summary(self, snapshot: StrategyRuntimeSnapshot) -> None:
        try:
            trade_date = self.candidate_collector._trade_date()
            candidates = self.db.list_candidates(trade_date)
            snapshot.candidate_generation_summary = _candidate_generation_summary(candidates)
        except Exception as exc:
            snapshot.candidate_generation_summary = {"error": f"CANDIDATE_GENERATION_SUMMARY_FAILED:{exc}"}

    def _emit_entry_order_intent(
        self,
        candidate: Candidate,
        result: GatePipelineResult,
        plan: EntryPlan,
        virtual_order: VirtualOrder,
        context: "_ReviewContext",
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        if self.order_sink is None:
            return
        try:
            payload = self.order_sink.on_entry_order_decision(
                candidate=candidate,
                gate_result=result,
                entry_plan=plan,
                virtual_order=virtual_order,
                runtime_cycle_at=now.isoformat(),
            )
            context.dry_run_entry_order_result = dict(payload or {})
            context.dry_run_order_result = dict(payload or {})
            if payload and payload.get("status") not in {"SKIPPED", ""}:
                context.review_needed = True
            self._apply_order_sink_snapshot(snapshot)
        except Exception as exc:
            snapshot.warnings.append(f"RUNTIME_DRY_RUN_ORDER_SINK_FAILED:{candidate.code}:{exc}")

    def _emit_exit_order_intent(
        self,
        candidate: Candidate,
        position: VirtualPosition,
        decision: ExitDecision,
        context: "_ReviewContext",
        snapshot: StrategyRuntimeSnapshot,
        now: datetime,
    ) -> None:
        if self.order_sink is None:
            return
        if not bool(decision.filled):
            return
        if decision.decision_type not in DRY_RUN_EXIT_INTENT_TYPES:
            return
        if not decision.details.get("virtual_exit_price") and not decision.trigger_price:
            return
        try:
            payload = self.order_sink.on_exit_order_decision(
                candidate=candidate,
                virtual_position=position,
                exit_decision=decision,
                runtime_cycle_at=now.isoformat(),
                context={"runtime": "strategy_runtime"},
            )
            result = dict(payload or {})
            context.dry_run_exit_order_results.append(result)
            if result and result.get("status") not in {"SKIPPED", ""}:
                context.review_needed = True
            self._apply_order_sink_snapshot(snapshot)
        except Exception as exc:
            snapshot.warnings.append(f"RUNTIME_DRY_RUN_EXIT_ORDER_SINK_FAILED:{candidate.code}:{decision.id}:{exc}")

    def _apply_order_sink_snapshot(self, snapshot: StrategyRuntimeSnapshot) -> None:
        if self.order_sink is None or not hasattr(self.order_sink, "snapshot"):
            return
        try:
            payload = dict(self.order_sink.snapshot() or {})
        except Exception as exc:
            snapshot.warnings.append(f"RUNTIME_ORDER_SINK_SNAPSHOT_FAILED:{exc}")
            return
        snapshot.dry_run_order_intent_count = int(payload.get("dry_run_order_intent_count") or 0)
        snapshot.dry_run_order_accepted_count = int(payload.get("dry_run_order_accepted_count") or 0)
        snapshot.dry_run_order_rejected_count = int(payload.get("dry_run_order_rejected_count") or 0)
        snapshot.dry_run_order_duplicate_count = int(payload.get("dry_run_order_duplicate_count") or 0)
        snapshot.dry_run_order_live_would_pass_count = int(payload.get("dry_run_order_live_would_pass_count") or 0)
        snapshot.dry_run_order_live_would_reject_count = int(payload.get("dry_run_order_live_would_reject_count") or 0)
        snapshot.dry_run_entry_order_intent_count = int(payload.get("dry_run_entry_order_intent_count") or 0)
        snapshot.dry_run_exit_order_intent_count = int(payload.get("dry_run_exit_order_intent_count") or 0)
        snapshot.dry_run_sell_order_intent_count = int(payload.get("dry_run_sell_order_intent_count") or 0)
        snapshot.dry_run_exit_accepted_count = int(payload.get("dry_run_exit_accepted_count") or 0)
        snapshot.dry_run_exit_rejected_count = int(payload.get("dry_run_exit_rejected_count") or 0)
        snapshot.dry_run_exit_duplicate_count = int(payload.get("dry_run_exit_duplicate_count") or 0)
        snapshot.dry_run_exit_live_would_pass_count = int(payload.get("dry_run_exit_live_would_pass_count") or 0)
        snapshot.dry_run_exit_live_would_reject_count = int(payload.get("dry_run_exit_live_would_reject_count") or 0)
        snapshot.last_dry_run_order_intent_at = str(payload.get("last_dry_run_order_intent_at") or "")
        snapshot.last_dry_run_order_reject_reason = str(payload.get("last_dry_run_order_reject_reason") or "")
        snapshot.last_dry_run_exit_order_intent_at = str(payload.get("last_dry_run_exit_order_intent_at") or "")
        snapshot.last_dry_run_exit_order_reject_reason = str(payload.get("last_dry_run_exit_order_reject_reason") or "")
        snapshot.dry_run_order_policy = str(payload.get("dry_run_order_policy") or "")
        snapshot.dry_run_order_sink_enabled = bool(payload.get("dry_run_order_sink_enabled"))

    def _apply_reason_summary(self, snapshot: StrategyRuntimeSnapshot) -> None:
        try:
            trade_date = self.candidate_collector._trade_date()
            rows = []
            for candidate in self.db.list_candidates(trade_date=trade_date):
                metadata = dict(candidate.metadata or {})
                gate_record = _best_gate_record_from_metadata(metadata)
                reason_codes = _runtime_reason_codes(metadata, gate_record)
                display_state = _runtime_candidate_display_state(candidate)
                reason_status = normalize_reason_status(
                    reason_codes=reason_codes,
                    display_state=display_state,
                    existing_status=metadata.get("sub_status") or gate_record.get("sub_status") or "",
                    block_type=candidate.block_type.value,
                    can_recover=candidate.can_recover,
                )
                rows.append(
                    {
                        "state": candidate.state.value,
                        "display_state": display_state,
                        "reason_status": reason_status,
                        "reason_family": reason_status_family(reason_status),
                        "reason_codes": reason_codes,
                        "block_type": candidate.block_type.value,
                        "can_recover": candidate.can_recover,
                    }
                )
            snapshot.reason_summary = reason_summary(rows)
        except Exception as exc:
            snapshot.reason_summary = {"error": f"REASON_SUMMARY_FAILED:{exc}"}

    def _refresh_readiness_snapshot(
        self,
        snapshot: StrategyRuntimeSnapshot,
        current: datetime,
        trade_date: Optional[str] = None,
    ) -> None:
        try:
            report = build_readiness_report(
                self.db,
                trade_date=trade_date or current.date().isoformat(),
                subscription_manager=self.subscription_manager,
                market_session_status=snapshot.market_session_status,
                data_warmup_status=snapshot.data_warmup_status,
                gate_skip_reason=snapshot.gate_skip_reason,
                candidate_subscription_selected_count=snapshot.candidate_subscription_selected_count,
                candidate_subscription_skipped_discovery_count=snapshot.candidate_subscription_skipped_discovery_count,
                candidate_subscription_skipped_unmapped_count=snapshot.candidate_subscription_skipped_unmapped_count,
                theme_engine_mode=self.config.theme_engine_mode,
                theme_lab_flow_wired=self.theme_lab_pipeline is not None,
                condition_adapter=self.condition_adapter,
            )
        except Exception as exc:
            snapshot.warnings.append(f"READINESS_REPORT_FAILED:{exc}")
            return
        self.readiness_report = report
        if report.unresolved_condition_profiles_count <= 0:
            snapshot.warnings = _without_resolved_condition_warnings(snapshot.warnings)
            self._warnings = _without_resolved_condition_warnings(self._warnings)
        snapshot.condition_profiles_count = report.condition_profiles_count
        snapshot.unresolved_condition_profiles_count = report.unresolved_condition_profiles_count
        snapshot.active_theme_count = report.active_theme_count
        snapshot.watch_theme_count = report.watch_theme_count
        snapshot.candidate_theme_count = report.candidate_theme_count
        snapshot.theme_active_stock_count = report.theme_active_stock_count
        snapshot.theme_last_sync_at = report.theme_last_sync_at
        snapshot.theme_last_tick_at = report.theme_last_tick_at
        snapshot.theme_ws_client_count = report.theme_ws_client_count
        snapshot.top_theme_name = report.top_theme_name
        snapshot.top_theme_score = report.top_theme_score
        snapshot.theme_engine_status = report.theme_engine_status
        snapshot.theme_data_status = report.theme_data_status
        snapshot.active_candidates_with_active_theme = report.active_candidates_with_active_theme
        snapshot.active_candidates_without_active_theme = report.active_candidates_without_active_theme
        snapshot.theme_context_coverage_pct = report.theme_context_coverage_pct
        snapshot.quality_actionable_count = report.quality_actionable_count
        snapshot.quality_discovery_only_count = report.quality_discovery_only_count
        snapshot.quality_unmapped_count = report.quality_unmapped_count
        snapshot.quality_invalid_code_count = report.quality_invalid_code_count
        snapshot.quality_data_wait_count = report.quality_data_wait_count
        snapshot.market_session_status = report.market_session_status
        snapshot.data_warmup_status = report.data_warmup_status
        snapshot.gate_skip_reason = report.gate_skip_reason
        snapshot.candidate_subscription_selected_count = report.candidate_subscription_selected_count
        snapshot.candidate_subscription_skipped_discovery_count = report.candidate_subscription_skipped_discovery_count
        snapshot.candidate_subscription_skipped_unmapped_count = report.candidate_subscription_skipped_unmapped_count
        snapshot.protected_subscription_usage = report.protected_subscription_usage
        snapshot.warnings = dedupe_warnings(list(snapshot.warnings) + report.warnings)


@dataclass
class _ReviewContext:
    gate_result: Optional[GatePipelineResult] = None
    entry_plan: Optional[EntryPlan] = None
    virtual_order: Optional[VirtualOrder] = None
    virtual_position: Optional[VirtualPosition] = None
    exit_decisions: list[ExitDecision] = field(default_factory=list)
    dry_run_order_result: dict = field(default_factory=dict)
    dry_run_entry_order_result: dict = field(default_factory=dict)
    dry_run_exit_order_results: list[dict] = field(default_factory=list)
    review_needed: bool = False


def _snapshot_for_exit(context: _ReviewContext):
    if context.gate_result is not None:
        return context.gate_result.snapshot
    return None


def _candidate_generation_summary(candidates: list[Candidate]) -> dict:
    generations_by_code: dict[str, set[int]] = {}
    reason_counts: dict[str, int] = {}
    excessive_count = 0
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        key = f"{candidate.trade_date}:{candidate.code}"
        try:
            seq = int(metadata.get("candidate_generation_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq:
            generations_by_code.setdefault(key, set()).add(seq)
        reason = str(metadata.get("generation_reason") or metadata.get("candidate_generation_reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if metadata.get("excessive_generation_blocked") or reason in {"same_generation_min_gap_guardrail", "same_generation_max_generation_guardrail"}:
            excessive_count += 1
    generation_counts = [len(values) for values in generations_by_code.values()]
    return {
        "multi_generation_code_count": sum(1 for count in generation_counts if count > 1),
        "avg_generation_per_code": round(sum(generation_counts) / len(generation_counts), 4) if generation_counts else None,
        "max_generation_per_code": max(generation_counts) if generation_counts else 0,
        "stale_re_detect_count": reason_counts.get("stale_re_detected", 0),
        "theme_change_generation_count": reason_counts.get("theme_changed", 0),
        "source_change_generation_count": reason_counts.get("source_changed", 0),
        "strategy_change_generation_count": reason_counts.get("strategy_changed", 0),
        "previous_lifecycle_closed_generation_count": reason_counts.get("previous_lifecycle_closed", 0),
        "excessive_generation_count": excessive_count,
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _runtime_candidate_display_state(candidate: Candidate) -> str:
    if candidate.state in {CandidateState.DETECTED, CandidateState.WATCHING}:
        return "WAIT"
    if (
        candidate.state == CandidateState.BLOCKED
        and (candidate.block_type == BlockType.TEMPORARY or candidate.can_recover)
    ):
        return "WAIT"
    return candidate.state.value


def _best_gate_record_from_metadata(metadata: dict) -> dict:
    records = metadata.get("gate_results_by_theme")
    if isinstance(records, dict):
        rows = [dict(item or {}) for item in records.values() if isinstance(item, dict)]
        if rows:
            return sorted(rows, key=lambda item: float(item.get("score") or item.get("final_score") or 0.0), reverse=True)[0]
    record = metadata.get("gate_result")
    return dict(record or {}) if isinstance(record, dict) else {}


def _runtime_reason_codes(metadata: dict, gate_record: dict) -> list[str]:
    values = gate_record.get("reason_codes") or metadata.get("reason_codes") or []
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    sub_status = str(metadata.get("sub_status") or gate_record.get("sub_status") or "").strip()
    if sub_status and sub_status not in result:
        result.insert(0, sub_status)
    return result


def _position_theme_id(candidate: Candidate, context: _ReviewContext) -> str:
    if context.entry_plan is not None:
        value = context.entry_plan.cancel_condition.get("theme_id")
        if value:
            return str(value)
    if candidate.theme_ids:
        return str(candidate.theme_ids[0])
    return str(candidate.metadata.get("theme_lab_primary_theme") or "")


def _position_theme_name(candidate: Candidate, context: _ReviewContext) -> str:
    if context.entry_plan is not None:
        value = context.entry_plan.cancel_condition.get("theme_name")
        if value:
            return str(value)
    return str(candidate.metadata.get("theme_name") or candidate.metadata.get("theme_lab_primary_theme") or "")


def _find_theme_snapshot(payload: dict, theme_id: str, theme_name: str) -> dict:
    target = {str(theme_id or ""), str(theme_name or "")} - {""}
    for key in ("theme_condition_snapshots", "theme_rankings"):
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            names = {
                str(item.get("theme_id") or ""),
                str(item.get("theme_name") or ""),
                str(item.get("name") or ""),
            } - {""}
            if target and target & names:
                return dict(item)
    return {}


def _find_watch_snapshot(payload: dict, code: str) -> dict:
    clean_code = normalize_code(code)
    for item in payload.get("watchset_snapshots") or []:
        if not isinstance(item, dict):
            continue
        if normalize_code(str(item.get("symbol") or item.get("code") or "")) == clean_code:
            return dict(item)
    return {}


def _candidate_index_market(candidate: Candidate, snapshot) -> str:
    raw = str(candidate.market or snapshot.metadata.get("market") or "").upper()
    if "KOSPI" in raw:
        return "KOSPI"
    if "KOSDAQ" in raw:
        return "KOSDAQ"
    if candidate.strategy_profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        return "KOSPI"
    return "KOSDAQ"


def _breadth_status(theme: dict, previous_details: dict) -> str:
    if not theme:
        return ""
    alive_ratio = _float_or_none(theme.get("alive_ratio"))
    leader_count = _int_or_none(theme.get("leader_count"))
    strong_count = _int_or_none(theme.get("strong_count"))
    previous_leader_count = _int_or_none(previous_details.get("exit_context_leader_count"))
    previous_strong_count = _int_or_none(previous_details.get("exit_context_strong_count"))
    if alive_ratio is not None and alive_ratio < 0.35:
        return "BREADTH_COLLAPSE"
    if previous_leader_count is not None and leader_count is not None and leader_count < previous_leader_count:
        return "BREADTH_COLLAPSE"
    if previous_strong_count is not None and strong_count is not None and strong_count < previous_strong_count:
        return "BREADTH_COLLAPSE"
    if alive_ratio is not None and alive_ratio < 0.5:
        return "LOW_BREADTH"
    return ""


def _context_risk_reasons(
    *,
    theme_status_after: str,
    theme_score: Optional[float],
    previous_theme_score: Optional[float],
    leader_count: Optional[int],
    previous_leader_count: Optional[int],
    strong_count: Optional[int],
    previous_strong_count: Optional[int],
    leader_return_pct: Optional[float],
    index_status: str,
    market_status: str,
    breadth_status: str,
) -> list[str]:
    reasons: list[str] = []
    if str(market_status or "").upper() == "RISK_OFF":
        reasons.append("MARKET_RISK_OFF")
    if str(index_status or "").upper() in {"INDEX_WEAK", "RISK_OFF", "WEAK"}:
        reasons.append("INDEX_WEAK")
    if str(theme_status_after or "").upper() in {"WEAK_THEME", "THEME_WEAK"}:
        reasons.append("THEME_WEAK")
    if previous_theme_score is not None and theme_score is not None and previous_theme_score > 0:
        drop_pct = ((previous_theme_score - theme_score) / previous_theme_score) * 100.0
        if drop_pct >= 30.0:
            reasons.append("THEME_SCORE_DROP")
    if leader_return_pct is not None and leader_return_pct <= -5.0:
        reasons.append("LEADER_COLLAPSE")
    if previous_leader_count is not None and leader_count is not None and leader_count < previous_leader_count:
        reasons.append("LEADER_COUNT_DROP")
    if previous_strong_count is not None and strong_count is not None and strong_count < previous_strong_count:
        reasons.append("STRONG_COUNT_DROP")
    if str(breadth_status or "").upper() in {"BREADTH_COLLAPSE", "COLLAPSE", "LOW_BREADTH"}:
        reasons.append("BREADTH_COLLAPSE")
    return list(dict.fromkeys(reasons))


def _consecutive_theme_weak_count(history: list[PositionContextSnapshot], current_status: str) -> int:
    count = 1 if str(current_status or "").upper() in {"WEAK_THEME", "THEME_WEAK"} else 0
    for item in reversed(history):
        if str(item.theme_status or "").upper() in {"WEAK_THEME", "THEME_WEAK"}:
            count += 1
        else:
            break
    return count


def _delta(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    return round(float(current) - float(previous), 6)


def _int_delta(current: Optional[int], previous: Optional[int]) -> Optional[int]:
    if current is None or previous is None:
        return None
    return int(current) - int(previous)


def _transition(previous: str, current: str) -> str:
    previous_text = str(previous or "")
    current_text = str(current or "")
    if not previous_text and not current_text:
        return ""
    return f"{previous_text or 'UNKNOWN'}->{current_text or 'UNKNOWN'}"


def _status_deteriorated(previous: str, current: str, *, weak_values: set[str]) -> bool:
    previous_text = str(previous or "").upper()
    current_text = str(current or "").upper()
    return current_text in weak_values and previous_text not in weak_values


def _runtime_return_pct(price: int, entry_price: int) -> Optional[float]:
    if entry_price <= 0 or price <= 0:
        return None
    return round(((int(price) - int(entry_price)) / int(entry_price)) * 100.0, 6)


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _without_resolved_condition_warnings(warnings: list[str]) -> list[str]:
    stale_prefixes = (
        "CONDITION_PROFILE_UNRESOLVED:",
        "CONDITION_INDEX_NOT_READY:",
        "THEME_LAB_CONDITION_ALIVE_UNRESOLVED",
        "THEME_LAB_CONDITION_STRONG_UNRESOLVED",
        "THEME_LAB_CONDITION_LEADER_UNRESOLVED",
    )
    return [warning for warning in warnings if not str(warning or "").startswith(stale_prefixes)]


def _context_dry_run_order_results(context: _ReviewContext) -> list[dict]:
    results: list[dict] = []
    if context.dry_run_entry_order_result:
        results.append(context.dry_run_entry_order_result)
    elif context.dry_run_order_result:
        results.append(context.dry_run_order_result)
    results.extend([dict(item or {}) for item in context.dry_run_exit_order_results if item])
    return results


def _attach_dry_run_order_review_details(review: TradeReview, context: _ReviewContext) -> None:
    details = dict(review.details or {})
    entry = context.dry_run_entry_order_result or context.dry_run_order_result
    if entry:
        safety = dict(entry.get("safety") or entry.get("safety_checks") or {})
        request = dict(entry.get("request") or {})
        amount = int(request.get("quantity") or 0) * max(0, int(request.get("price") or 0))
        details.update(
            {
                "dry_run_entry_order_intent_id": entry.get("intent_id", ""),
                "dry_run_entry_order_status": entry.get("status", ""),
                "dry_run_entry_order_reason": entry.get("reason", ""),
                "dry_run_entry_dedupe_key": entry.get("dedupe_key", ""),
                "dry_run_entry_live_would_pass": bool(entry.get("live_would_pass")),
                "dry_run_entry_live_reject_reason": entry.get("live_reject_reason", ""),
                "dry_run_entry_quantity": request.get("quantity", 0),
                "dry_run_entry_price": request.get("price", 0),
                "dry_run_entry_order_amount": amount,
                "dry_run_entry_safety_summary": {
                    "ok": safety.get("ok"),
                    "reason": safety.get("reason", ""),
                },
                "dry_run_order_intent_id": entry.get("intent_id", ""),
                "dry_run_order_status": entry.get("status", ""),
                "dry_run_order_reason": entry.get("reason", ""),
                "dry_run_dedupe_key": entry.get("dedupe_key", ""),
                "dry_run_live_would_pass": bool(entry.get("live_would_pass")),
                "dry_run_live_reject_reason": entry.get("live_reject_reason", ""),
                "dry_run_quantity": request.get("quantity", 0),
                "dry_run_price": request.get("price", 0),
                "dry_run_order_amount": amount,
                "dry_run_safety_summary": {
                    "ok": safety.get("ok"),
                    "reason": safety.get("reason", ""),
                },
            }
        )
    exit_results = [dict(item or {}) for item in context.dry_run_exit_order_results if item]
    if exit_results:
        exit_requests = [dict(item.get("request") or {}) for item in exit_results]
        details.update(
            {
                "dry_run_exit_order_intent_ids": [item.get("intent_id", "") for item in exit_results],
                "dry_run_exit_order_statuses": [item.get("status", "") for item in exit_results],
                "dry_run_exit_reasons": [item.get("reason", "") for item in exit_results],
                "dry_run_exit_decision_ids": [request.get("exit_decision_id") for request in exit_requests],
                "dry_run_exit_live_would_pass_count": sum(1 for item in exit_results if item.get("live_would_pass")),
                "dry_run_exit_live_would_reject_count": sum(1 for item in exit_results if not item.get("live_would_pass")),
                "dry_run_exit_sell_quantity_total": sum(int(request.get("quantity") or 0) for request in exit_requests),
                "dry_run_exit_sell_amount_total": sum(
                    int(request.get("quantity") or 0) * max(0, int(request.get("price") or 0))
                    for request in exit_requests
                ),
                "dry_run_exit_summary": [
                    {
                        "intent_id": item.get("intent_id", ""),
                        "status": item.get("status", ""),
                        "reason": item.get("reason", ""),
                        "dedupe_key": item.get("dedupe_key", ""),
                        "live_would_pass": bool(item.get("live_would_pass")),
                        "live_reject_reason": item.get("live_reject_reason", ""),
                        "exit_decision_id": request.get("exit_decision_id"),
                        "exit_decision_type": request.get("exit_decision_type", ""),
                        "quantity": request.get("quantity", 0),
                        "price": request.get("price", 0),
                    }
                    for item, request in zip(exit_results, exit_requests)
                ],
            }
        )
    review.details = details


def _clean_time(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _gate_result_key(result: GatePipelineResult) -> str:
    raw = result.details.get("gate_result_key")
    if raw:
        return str(raw)
    return f"{result.candidate_id}:{result.code}:{result.theme_id}:{result.final_grade}"


def _candidate_entry_excluded(candidate: Candidate) -> bool:
    metadata = dict(candidate.metadata or {})
    if bool(metadata.get("entry_excluded")):
        return True
    profiles = {str(value) for value in dict(metadata.get("condition_profiles", {})).values()}
    purposes = {str(value) for value in dict(metadata.get("condition_purposes", {})).values()}
    entry_conditions = list(metadata.get("entry_condition_names") or [])
    if entry_conditions:
        return False
    if StrategyProfile.THEME_DISCOVERY_PROFILE.value in profiles:
        return True
    if "theme_broad_candidate" in purposes:
        return True
    return candidate.strategy_profile == StrategyProfile.THEME_DISCOVERY_PROFILE


def _candidate_state_priority(state: CandidateState) -> int:
    return {
        CandidateState.READY: 0,
        CandidateState.WATCHING: 1,
        CandidateState.DETECTED: 2,
        CandidateState.BLOCKED: 3,
    }.get(state, 9)


def _last_seen_desc_value(candidate: Candidate) -> float:
    try:
        return -_parse_time(candidate.last_seen_at).timestamp()
    except Exception:
        return 0.0


def _accepts_entry_candidates(pipeline) -> bool:
    callback = getattr(type(pipeline), "evaluate", None) or getattr(pipeline, "evaluate", None)
    try:
        params = signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(param.name == "entry_candidates" or param.kind == Parameter.VAR_KEYWORD for param in params)


def _best_result(results: list[GatePipelineResult]) -> GatePipelineResult:
    return sorted(results, key=lambda result: (result.strategy_eligible, result.final_score, result.final_grade), reverse=True)[0]


def _gate_result_record(result: GatePipelineResult, evaluated_at: str) -> dict:
    return {
        "theme_id": result.theme_id,
        "theme_name": result.details.get("theme_name", ""),
        "gate_result_key": _gate_result_key(result),
        "final_grade": result.final_grade,
        "strategy_eligible": result.strategy_eligible,
        "block_type": result.block_type.value,
        "reason_codes": _result_reason_codes(result),
        "comparison_reason_codes": list(result.details.get("comparison_reason_codes") or []),
        "primary_reason_code": result.details.get("primary_reason_code", ""),
        "secondary_reason_codes": list(result.details.get("secondary_reason_codes") or []),
        "feature_version": result.details.get("feature_version", ""),
        "strategy_feature_version": result.details.get("strategy_feature_version", ""),
        "session_bucket": result.details.get("session_bucket", ""),
        "comparison_mode": result.details.get("comparison_mode", ""),
        "legacy_result": result.details.get("legacy_result"),
        "new_result": result.details.get("new_result"),
        "legacy_score": result.details.get("legacy_score"),
        "new_score": result.details.get("new_score"),
        "sub_status": result.details.get("sub_status", ""),
        "theme_score": result.details.get("theme_score", result.details.get("dynamic_theme_score", 0.0)),
        "dynamic_theme_score": result.details.get("dynamic_theme_score", 0.0),
        "membership_score": result.details.get("membership_score", 0.0),
        "hybrid_score": result.details.get("hybrid_score", result.final_score),
        "score": result.final_score,
        "evaluated_at": evaluated_at,
    }


def _block_record(result: GatePipelineResult) -> dict:
    return {
        "theme_id": result.theme_id,
        "gate_result_key": _gate_result_key(result),
        "final_grade": result.final_grade,
        "block_type": result.block_type.value,
        "reason_codes": _result_reason_codes(result),
        "comparison_reason_codes": list(result.details.get("comparison_reason_codes") or []),
        "primary_reason_code": result.details.get("primary_reason_code", ""),
        "secondary_reason_codes": list(result.details.get("secondary_reason_codes") or []),
        "feature_version": result.details.get("feature_version", ""),
        "strategy_feature_version": result.details.get("strategy_feature_version", ""),
        "session_bucket": result.details.get("session_bucket", ""),
        "comparison_mode": result.details.get("comparison_mode", ""),
        "legacy_result": result.details.get("legacy_result"),
        "new_result": result.details.get("new_result"),
        "legacy_score": result.details.get("legacy_score"),
        "new_score": result.details.get("new_score"),
        "sub_status": result.details.get("sub_status", ""),
        "theme_score": result.details.get("theme_score", result.details.get("dynamic_theme_score", 0.0)),
        "dynamic_theme_score": result.details.get("dynamic_theme_score", 0.0),
        "membership_score": result.details.get("membership_score", 0.0),
        "hybrid_score": result.details.get("hybrid_score", result.final_score),
    }


def _result_reason_codes(result: GatePipelineResult) -> list[str]:
    values: list[str] = []
    for decision in result.decisions:
        decision_codes = [str(code) for code in decision.reason_codes]
        values.extend(decision_codes)
        if "DATA_INSUFFICIENT" in decision_codes or decision.details.get("sub_status") == "DATA_INSUFFICIENT":
            if decision.gate_name == "MarketIndexGate":
                values.append("INDEX_DATA_INSUFFICIENT")
            elif decision.gate_name in {"ThemeStrengthGate", "ThemePullbackGate", "StockPullbackEntryGate"}:
                values.append("INDICATOR_DATA_INSUFFICIENT")
    values.extend(str(code) for code in result.details.get("cap_rules_applied", []))
    sub_status = result.details.get("sub_status")
    if sub_status:
        values.append(str(sub_status))
    return _dedupe(values)


def _group_reason_codes(results: list[GatePipelineResult]) -> list[str]:
    values: list[str] = []
    for result in results:
        values.extend(_result_reason_codes(result))
    return _dedupe(values)


def _candidate_reason_codes(candidate: Candidate) -> list[str]:
    reasons = []
    for record in dict(candidate.metadata.get("block_reasons_by_theme", {})).values():
        reasons.extend(record.get("reason_codes", []))
    return _dedupe(str(reason) for reason in reasons)


def _has_data_insufficient(results: list[GatePipelineResult]) -> bool:
    return any("DATA_INSUFFICIENT" in _result_reason_codes(result) for result in results)


def _all_final_block(results: list[GatePipelineResult]) -> bool:
    return bool(results) and all(result.block_type == BlockType.FINAL for result in results)


def _has_temporary_block(results: list[GatePipelineResult]) -> bool:
    return any(result.block_type == BlockType.TEMPORARY and result.can_recover for result in results)


def _blocked_recoverable(candidate: Candidate) -> bool:
    return (
        candidate.state == CandidateState.BLOCKED
        and candidate.block_type == BlockType.TEMPORARY
        and candidate.can_recover
    )


def _blocked_recheck_due(candidate: Candidate, now: datetime, has_open_virtual_activity: bool = False) -> bool:
    if not _blocked_recoverable(candidate):
        return False
    if candidate.expires_at and _parse_time(candidate.expires_at) <= now:
        return False
    if not candidate.sources and not has_open_virtual_activity and candidate.metadata.get("quality_status") != QUALITY_UNMAPPED:
        return False
    next_recheck_at = candidate.metadata.get("next_recheck_at")
    if next_recheck_at:
        return _parse_time(str(next_recheck_at)) <= now
    blocked_at = candidate.metadata.get("blocked_at")
    if blocked_at:
        return _parse_time(str(blocked_at)) + timedelta(seconds=max(0, candidate.recheck_after_sec)) <= now
    return True


def _set_block_metadata(
    metadata: dict,
    candidate: Candidate,
    result: GatePipelineResult,
    now: datetime,
    *,
    recheck: bool,
) -> None:
    current = _clean_time(now)
    current_text = current.isoformat()
    already_blocked = "blocked_at" in metadata
    if not already_blocked:
        metadata["blocked_at"] = current_text
    if recheck and already_blocked:
        metadata["last_rechecked_at"] = current_text
    metadata["next_recheck_at"] = (current + timedelta(seconds=max(0, result.recheck_after_sec or candidate.recheck_after_sec or 60))).isoformat()
    metadata["block_count"] = int(metadata.get("block_count") or 0) + 1
    metadata["last_block_theme_id"] = result.theme_id
    metadata["last_block_result"] = _block_record(result)


def _lifecycle_signature(candidate: Candidate) -> tuple:
    return (
        candidate.state.value,
        candidate.block_type.value,
        bool(candidate.can_recover),
        int(candidate.recheck_after_sec),
        str(candidate.metadata.get("best_gate_result_key") or ""),
        tuple(_candidate_reason_codes(candidate)),
        str(candidate.metadata.get("sub_status") or ""),
    )


def _candidate_persist_signature(candidate: Candidate) -> tuple:
    metadata = dict(candidate.metadata or {})
    return (
        candidate.state.value,
        candidate.block_type.value,
        bool(candidate.can_recover),
        int(candidate.recheck_after_sec),
        tuple(source.value if hasattr(source, "value") else str(source) for source in candidate.sources),
        tuple(str(name) for name in candidate.condition_names),
        str(candidate.expires_at or ""),
        str(metadata.get("best_theme_id") or ""),
        str(metadata.get("best_gate_result_key") or ""),
        str(metadata.get("sub_status") or ""),
        str(metadata.get("quality_status") or ""),
        str(metadata.get("quality_reason") or ""),
        tuple(str(reason) for reason in (metadata.get("insufficient_reason") or [])),
        _stable_value(_without_evaluated_at(metadata.get("gate_results_by_theme", {}))),
        _stable_value(metadata.get("block_reasons_by_theme", {})),
        str(metadata.get("next_recheck_at") or ""),
        str(metadata.get("last_rechecked_at") or ""),
        str(metadata.get("last_block_theme_id") or ""),
        _stable_value(metadata.get("last_block_result", {})),
        int(metadata.get("block_count") or 0),
    )


def _without_evaluated_at(value) -> dict:
    result = {}
    for key, record in dict(value or {}).items():
        clean_record = dict(record or {})
        clean_record.pop("evaluated_at", None)
        result[str(key)] = clean_record
    return result


def _trade_review_signature(review: TradeReview) -> tuple:
    return (
        review.candidate_id,
        review.trade_date,
        review.code,
        review.name,
        review.market,
        review.theme_id,
        review.theme_name,
        review.strategy_profile,
        review.gate_result_key,
        review.review_key,
        review.entry_plan_id,
        review.virtual_order_id,
        review.virtual_position_id,
        review.final_grade,
        review.final_status.value if hasattr(review.final_status, "value") else str(review.final_status),
        review.virtual_order_status,
        review.exit_reason,
        int(review.entry_price),
        int(review.exit_price),
        review.max_return_5m,
        review.max_return_10m,
        review.max_return_20m,
        review.max_drawdown_20m,
        review.missed_reason,
        bool(review.false_negative_flag),
        bool(review.false_positive_flag),
        bool(review.expired_but_later_rallied),
        bool(review.blocked_but_later_rallied),
        _stable_value(review.details),
    )


def _stable_value(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _lifecycle_payload(
    candidate: Candidate,
    result: GatePipelineResult,
    previous_reasons: list[str],
) -> dict:
    new_reasons = _candidate_reason_codes(candidate)
    return {
        "code": candidate.code,
        "state": candidate.state.value,
        "block_type": candidate.block_type.value,
        "can_recover": candidate.can_recover,
        "recheck_after_sec": candidate.recheck_after_sec,
        "theme_id": result.theme_id,
        "gate_result_key": _gate_result_key(result),
        "final_grade": result.final_grade,
        "block_reason_codes": _result_reason_codes(result),
        "previous_reason_codes": previous_reasons,
        "new_reason_codes": new_reasons,
        "best_theme_id": candidate.metadata.get("best_theme_id", ""),
        "best_gate_result_key": candidate.metadata.get("best_gate_result_key", ""),
        "sub_status": candidate.metadata.get("sub_status", ""),
    }


def _dedupe(values) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _normalize_runtime_stock_codes(values, field_name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [part.strip() for part in values.replace("\n", ",").split(",")]
    elif isinstance(values, list):
        raw_values = values
    else:
        raise ValueError(f"{field_name} must be a list or comma-separated string")
    result: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().upper()
        if not text:
            continue
        if text in {"KOSPI", "KOSDAQ"}:
            raise ValueError(f"{field_name} cannot contain logical index code {text}")
        code = normalize_code(text)
        if not (len(code) == 6 and code.isdigit()):
            raise ValueError(f"{field_name} contains invalid stock code {text}")
        if code not in result:
            result.append(code)
    return result


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)
