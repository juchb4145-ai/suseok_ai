from __future__ import annotations

import json
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
from trading.strategy.exit import ExitDecisionEngine, VirtualPositionService
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
    ReviewFinalStatus,
    StrategyProfile,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.pipeline import GatePipeline, GatePipelineResult
from trading.strategy.readiness import ReadinessReport, build_readiness_report, dedupe_warnings
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.review import TradeReviewService
from trading.strategy.virtual_orders import VirtualOrderService


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
        self.started = False
        self._warnings: list[str] = []
        self.startup_warnings: list[str] = []
        self.readiness_report: Optional[ReadinessReport] = None
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

    def cycle(self, now: Optional[datetime] = None) -> StrategyRuntimeSnapshot:
        started = perf_counter()
        current = _clean_time(now or self.clock())
        self._last_runtime_time = current
        snapshot = self._snapshot(current)
        self._drain_candidate_collector_warnings(snapshot)
        try:
            if not self.started:
                snapshot.warnings.append("RUNTIME_NOT_STARTED")
                return snapshot

            trade_date = self.candidate_collector._trade_date()
            self._apply_flow_diagnostics(snapshot, current, trade_date, [])
            if snapshot.gate_skip_reason == GATE_SKIP_MARKET_SESSION_CLOSED:
                snapshot.candidate_count = len(self.db.list_candidates(trade_date))
                snapshot.active_candidate_count = len(self._active_candidates(trade_date, current))
                self._stop_condition_adapter_for_market_closed(snapshot)
                self._reconcile_subscriptions([], snapshot)
                self._refresh_readiness_snapshot(snapshot, current, trade_date)
                snapshot.warnings = dedupe_warnings(snapshot.warnings)
                return snapshot

            self._rollover_previous_trade_date_candidates(trade_date, current, snapshot)
            expired = self.candidate_collector.expire_stale(current, keep_alive=self._candidate_expire_keep_alive)
            snapshot.expired_count += len(expired)
            snapshot.candidate_save_count += len(expired)
            snapshot.db_write_count_per_cycle += len(expired)
            self._apply_quality_controls(trade_date, current, snapshot)
            subscription_candidates = self._subscription_candidates(trade_date, snapshot)
            snapshot.candidate_count = len(self.db.list_candidates(trade_date))
            self._reconcile_subscriptions(subscription_candidates, snapshot)
            self._refresh_readiness_snapshot(snapshot, current, trade_date)
            candidates = self._active_candidates(trade_date, current)
            snapshot.active_candidate_count = len(candidates)
            self._apply_flow_diagnostics(snapshot, current, trade_date, candidates)
            if snapshot.gate_skip_reason == GATE_SKIP_DATA_WARMUP:
                snapshot.evaluated_candidate_count = 0
                self._refresh_readiness_snapshot(snapshot, current, trade_date)
                snapshot.warnings = dedupe_warnings(snapshot.warnings)
                return snapshot
            snapshot.evaluated_candidate_count = len([candidate for candidate in candidates if self._candidate_entry_evaluable(candidate)])

            gate_results = self._evaluate_gates(candidates, snapshot)
            snapshot.gate_result_count = len(gate_results)
            context_by_candidate: dict[int, _ReviewContext] = {}
            lifecycle_changed = self._apply_lifecycle(candidates, gate_results, snapshot, current)
            for candidate_id, result in lifecycle_changed.items():
                context_by_candidate[candidate_id] = _ReviewContext(gate_result=result, review_needed=True)
            for result in self._entry_results(gate_results):
                if result.candidate_id is None:
                    continue
                context = context_by_candidate.setdefault(result.candidate_id, _ReviewContext(gate_result=result))
                context.gate_result = result
                try:
                    self._process_gate_result(result, context, snapshot, current)
                except Exception as exc:
                    snapshot.warnings.append(f"CANDIDATE_PROCESS_FAILED:{result.code}:{exc}")

            self._evaluate_virtual_orders(context_by_candidate, snapshot, current)
            self._open_filled_orders(context_by_candidate, snapshot, current)
            self._evaluate_positions(context_by_candidate, snapshot, current)
            self._save_reviews(context_by_candidate, expired, snapshot, current)
            self._refresh_readiness_snapshot(snapshot, current, trade_date)
            snapshot.warnings = dedupe_warnings(snapshot.warnings)
            return snapshot
        finally:
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
                new_decisions = self.exit_decision_engine.evaluate(position, snapshot_for_exit, self.candle_builder, existing_decisions, now)
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
                if not self._review_changed(review):
                    continue
                self.db.save_trade_review(review)
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
        result: list[Candidate] = []
        for candidate in self.db.list_candidates(trade_date=trade_date):
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
                result.append(candidate)
            elif (
                candidate.state == CandidateState.BLOCKED
                and candidate.block_type == BlockType.TEMPORARY
                and candidate.can_recover
            ):
                result.append(candidate)
            elif has_open_activity:
                result.append(candidate)
        return sorted(result, key=self._candidate_subscription_sort_key)

    def _active_candidates(self, trade_date: str, now: Optional[datetime] = None) -> list[Candidate]:
        result: list[Candidate] = []
        for candidate in self.db.list_candidates(trade_date=trade_date):
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

    def _candidate_subscription_sort_key(self, candidate: Candidate) -> tuple[int, int, str, str]:
        quality = self._candidate_quality_status(candidate)
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

    def _snapshot(self, current: datetime) -> StrategyRuntimeSnapshot:
        return StrategyRuntimeSnapshot(
            started=self.started,
            cycle_at=current.isoformat(),
            warnings=dedupe_warnings(self._warnings),
        )

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
            )
        except Exception as exc:
            snapshot.warnings.append(f"READINESS_REPORT_FAILED:{exc}")
            return
        self.readiness_report = report
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
    review_needed: bool = False


def _snapshot_for_exit(context: _ReviewContext):
    if context.gate_result is not None:
        return context.gate_result.snapshot
    return None


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
