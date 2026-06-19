from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from threading import RLock
from typing import Any, Callable

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerConditionEvent, GatewayEvent
from trading.strategy.readiness import build_readiness_report
from trading.strategy.runtime import StrategyRuntime
from trading_app.dependencies import CoreSettings
from trading_app.intraday_outcomes import (
    IntradayOutcomeLabeler,
    ThemeLabFlowPricePathProvider,
    config_from_settings as outcome_config_from_settings,
)
from trading_app.runtime_factory import CoreRuntimeBundle, build_core_strategy_runtime
from trading_app.shadow_strategy import ShadowStrategyEvaluator, config_from_settings as shadow_config_from_settings


RuntimeBuilder = Callable[..., CoreRuntimeBundle]
MAX_PENDING_PRICE_TICKS = 2000
ORDER_LIFECYCLE_EVENT_TYPES = {
    "command_ack",
    "command_failed",
    "command_timeout",
    "command_expired",
    "order_ack",
    "order_reject",
    "order_fill",
    "execution",
    "execution_event",
    "fill",
    "cancel_ack",
    "order_cancel",
    "order_cancelled",
    "order_status_snapshot",
    "balance_snapshot",
    "position_snapshot",
    "reconcile_snapshot",
    "kiwoom_order_chejan",
    "kiwoom_balance_chejan",
    "kiwoom_special_chejan",
}


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        settings: CoreSettings,
        gateway_state: GatewayStateStore,
        runtime_builder: RuntimeBuilder = build_core_strategy_runtime,
        read_model_writer: Any = None,
        order_event_consumer: Any = None,
        reconcile_orchestrator: Any = None,
    ) -> None:
        self.settings = settings
        self.gateway_state = gateway_state
        self.runtime_builder = runtime_builder
        self.read_model_writer = read_model_writer
        self.order_event_consumer = order_event_consumer
        self.reconcile_orchestrator = reconcile_orchestrator
        self.enabled = bool(settings.runtime_enabled)
        self.auto_start = bool(settings.runtime_auto_start)
        self.running = False
        self.started_at = ""
        self.stopped_at = ""
        self.last_cycle_at = ""
        self.next_cycle_at = ""
        self.last_cycle_duration_ms = 0
        self.last_snapshot: dict[str, Any] = {}
        self.last_error = ""
        self.worker_stage = "idle"
        self.last_cycle_timings: dict[str, float] = {}
        self.warnings: list[str] = []
        self.cycle_count = 0
        self.failed_cycle_count = 0
        self.skipped_cycle_count = 0
        self.manual_cycle_count = 0
        self.mode = settings.runtime_mode
        self.order_policy = _order_policy(settings)
        self.loop_task: asyncio.Task | None = None
        self._cycle_lock = asyncio.Lock()
        self._state_lock = RLock()
        self._event_lock = RLock()
        self._cycle_future: asyncio.Future | None = None
        self._cycle_future_started_at = ""
        self._cycle_future_started_perf = 0.0
        self._cycle_future_reason = ""
        self._pending_price_ticks: dict[str, GatewayEvent] = {}
        self._dropped_price_tick_count = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-runtime")
        self._order_event_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="order-event-consumer")
        self._reconcile_event_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reconcile-event-consumer")
        self._diagnostics_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-diagnostics")
        self._diagnostics_future: asyncio.Future | None = None
        self._diagnostics_started_perf = 0.0
        self.post_cycle_diagnostics_stage = "idle"
        self.post_cycle_diagnostics_running = False
        self.post_cycle_diagnostics_last_started_at = ""
        self.post_cycle_diagnostics_last_finished_at = ""
        self.post_cycle_diagnostics_last_duration_ms = 0
        self.post_cycle_diagnostics_last_error = ""
        self.post_cycle_diagnostics_run_count = 0
        self.post_cycle_diagnostics_failed_count = 0
        self.post_cycle_diagnostics_skipped_count = 0
        self.post_cycle_diagnostics_last_result: dict[str, Any] = {}
        self._bundle: CoreRuntimeBundle | None = None
        self._shutdown = False
        self._last_gateway_logged_in: bool | None = None
        self._last_gateway_reconnect_count: int | None = None
        self._runtime_started_perf = 0.0
        self._last_realtime_no_tick_repair_perf = 0.0
        self._last_realtime_stale_total_ticks = 0
        if self.mode != "OBSERVE":
            self._warn(f"RUNTIME_ORDER_MODE_FORCED_OBSERVE:{self.mode}")
        if self.settings.runtime_allow_live_orders:
            self._warn("RUNTIME_LIVE_ORDERS_DISABLED_IN_PR5")

    def build_runtime(self) -> Any:
        db = TradingDatabase(str(self.settings.db_path))
        self._bundle = self.runtime_builder(
            db,
            self.gateway_state,
            settings=self.settings,
            warning_sink=self._warn,
        )
        return self._bundle.runtime

    async def startup(self) -> dict[str, Any]:
        if not self.enabled:
            self._log_runtime_event("startup", "disabled", "runtime disabled")
            return self.status()
        if self.auto_start:
            return await self.start()
        self._log_runtime_event("startup", "ready", "runtime enabled; auto-start disabled")
        return self.status()

    async def start(self) -> dict[str, Any]:
        if not self.enabled:
            self._log_runtime_event("start_rejected", "disabled", "runtime disabled")
            return self.status()
        if self.running:
            return self.status()
        loop = asyncio.get_running_loop()
        try:
            snapshot = await loop.run_in_executor(self._executor, self._start_in_worker)
        except Exception as exc:
            error = _exception_message(exc)
            with self._state_lock:
                self.last_error = error
                self.failed_cycle_count += 1
            self._log_runtime_event("start_failed", "failed", error)
            return self.status()
        now = _utc_now()
        with self._state_lock:
            self.running = True
            self._runtime_started_perf = perf_counter()
            self.started_at = now
            self.stopped_at = ""
            self.last_error = ""
            self.last_snapshot = snapshot
            self.next_cycle_at = _after_seconds(now, self._interval_sec())
        self._log_runtime_event("started", "running", "runtime started", snapshot)
        self._flush_dashboard_read_model("runtime_start")
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.loop_forever())
        return self.status()

    async def stop(self) -> dict[str, Any]:
        if not self.running and self._bundle is None:
            return self.status()
        with self._state_lock:
            self.running = False
            self.next_cycle_at = ""
        task = self.loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        loop = asyncio.get_running_loop()
        try:
            snapshot = await loop.run_in_executor(self._executor, self._stop_in_worker)
        except Exception as exc:
            error = _exception_message(exc)
            snapshot = {}
            with self._state_lock:
                self.last_error = error
            self._log_runtime_event("stop_failed", "failed", error)
        now = _utc_now()
        with self._state_lock:
            self.running = False
            self.stopped_at = now
            self._runtime_started_perf = 0.0
            self._last_realtime_no_tick_repair_perf = 0.0
            if snapshot:
                self.last_snapshot = snapshot
        self._log_runtime_event("stopped", "stopped", "runtime stopped", snapshot)
        return self.status()

    async def restart(self) -> dict[str, Any]:
        await self.stop()
        return await self.start()

    async def run_once(self, reason: str = "manual") -> dict[str, Any]:
        if not self.enabled:
            return self.status()
        if not self.running:
            self._warn("RUNTIME_CYCLE_REJECTED_NOT_RUNNING")
            return self.status()
        if self._cycle_worker_pending():
            self._skip_cycle(f"cycle worker still running: {reason}")
            return self.status()
        if self._cycle_lock.locked():
            self._skip_cycle(f"cycle already running: {reason}")
            return self.status()
        async with self._cycle_lock:
            if self._cycle_worker_pending():
                self._skip_cycle(f"cycle worker still running: {reason}")
                return self.status()
            if reason == "manual":
                with self._state_lock:
                    self.manual_cycle_count += 1
            started_at = _utc_now()
            started = perf_counter()
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._executor, self._cycle_in_worker)
            with self._state_lock:
                self._cycle_future = future
                self._cycle_future_started_at = started_at
                self._cycle_future_started_perf = started
                self._cycle_future_reason = reason
            try:
                snapshot = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=max(1, int(self.settings.runtime_cycle_timeout_sec)),
                )
            except Exception as exc:
                error = _exception_message(exc)
                duration_ms = int(round((perf_counter() - started) * 1000))
                with self._state_lock:
                    self.failed_cycle_count += 1
                    self.last_error = error
                    self.last_cycle_at = started_at
                    self.last_cycle_duration_ms = duration_ms
                    self.next_cycle_at = _after_seconds(_utc_now(), self._interval_sec()) if self.running else ""
                self._log_runtime_cycle(started_at, _utc_now(), duration_ms, "failed", {}, error)
                self._log_runtime_event("cycle_failed", "failed", error)
                if not future.done():
                    future.add_done_callback(
                        lambda done_future, cycle_started_at=started_at, cycle_started=started: self._consume_late_cycle_future(
                            done_future,
                            cycle_started_at,
                            cycle_started,
                        )
                    )
                else:
                    self._clear_cycle_future(future)
                return self.status()
            duration_ms = int(round((perf_counter() - started) * 1000))
            snapshot["cycle_duration_ms"] = snapshot.get("cycle_duration_ms") or duration_ms
            diagnostics_status = self._schedule_post_cycle_diagnostics(started_at)
            snapshot["post_cycle_diagnostics"] = diagnostics_status
            self._attach_diagnostics_placeholders(snapshot, diagnostics_status)
            with self._state_lock:
                self.cycle_count += 1
                self.last_cycle_at = started_at
                self.last_cycle_duration_ms = duration_ms
                self.last_snapshot = snapshot
                self.last_error = ""
                self.next_cycle_at = _after_seconds(_utc_now(), self._interval_sec()) if self.running else ""
                if self._cycle_future is future:
                    self._cycle_future = None
                    self._cycle_future_started_at = ""
                    self._cycle_future_started_perf = 0.0
                    self._cycle_future_reason = ""
            self._log_runtime_cycle(
                started_at,
                _utc_now(),
                duration_ms,
                "ok",
                snapshot,
                "",
            )
            self._flush_dashboard_read_model("runtime_cycle")
            return self.status()

    async def loop_forever(self) -> None:
        try:
            while self.running and not self._shutdown:
                await asyncio.sleep(max(1, self._interval_sec()))
                if self.running:
                    await self.run_once(reason="loop")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _exception_message(exc)
            with self._state_lock:
                self.last_error = error
            self._log_runtime_event("loop_failed", "failed", error)

    def status(self) -> dict[str, Any]:
        cycle_worker_pending_before_db = self._cycle_worker_pending()
        gateway = self.gateway_state.snapshot().to_dict()
        command_summary = self.gateway_state.command_snapshot()
        dry_run_order_summary = (
            {"status": "SKIPPED", "reason": "CYCLE_WORKER_PENDING"}
            if cycle_worker_pending_before_db
            else _dry_run_order_summary(self.settings.db_path)
        )
        realtime_data_quality = self._realtime_data_quality_snapshot()
        pending_price_tick_count = self._pending_price_tick_count()
        with self._state_lock:
            snapshot_source = dict(self.last_snapshot or {})
            realtime_stale_present = _has_realtime_stale_state(self.warnings, snapshot_source)
            realtime_recovered = realtime_stale_present and _realtime_ticks_recovered(
                realtime_data_quality,
                pending_price_tick_count,
                baseline_total_ticks=self._last_realtime_stale_total_ticks,
            )
            if realtime_recovered:
                self.warnings = _without_realtime_stale_warnings(self.warnings)
            snapshot = _snapshot_with_recovered_realtime_repair(
                snapshot_source,
                realtime_data_quality,
                pending_price_tick_count,
                recovered=realtime_recovered,
            )
            warnings = list(self.warnings[-50:])
            cycle_worker_pending = self._cycle_future is not None and not self._cycle_future.done()
            cycle_worker_elapsed_ms = 0
            if cycle_worker_pending and self._cycle_future_started_perf:
                cycle_worker_elapsed_ms = int(round((perf_counter() - self._cycle_future_started_perf) * 1000))
            return {
                "enabled": self.enabled,
                "auto_start": self.auto_start,
                "running": self.running,
                "mode": self.mode,
                "order_policy": self.order_policy,
                "started_at": self.started_at,
                "stopped_at": self.stopped_at,
                "last_cycle_at": self.last_cycle_at,
                "next_cycle_at": self.next_cycle_at,
                "last_cycle_duration_ms": self.last_cycle_duration_ms,
                "cycle_count": self.cycle_count,
                "failed_cycle_count": self.failed_cycle_count,
                "skipped_cycle_count": self.skipped_cycle_count,
                "manual_cycle_count": self.manual_cycle_count,
                "last_error": self.last_error,
                "worker_stage": self.worker_stage,
                "cycle_worker_pending": cycle_worker_pending,
                "cycle_worker_started_at": self._cycle_future_started_at if cycle_worker_pending else "",
                "cycle_worker_elapsed_ms": cycle_worker_elapsed_ms,
                "cycle_worker_reason": self._cycle_future_reason if cycle_worker_pending else "",
                "post_cycle_diagnostics": self._post_cycle_diagnostics_status_locked(),
                "order_event_consumer": self._order_event_consumer_status_locked(),
                "broker_reconcile": self._broker_reconcile_status_locked(),
                "last_cycle_timings": dict(self.last_cycle_timings),
                "warnings": warnings,
                "latest_snapshot": snapshot,
                "readiness": _readiness_summary(snapshot, warnings),
                "gateway": {
                    "connected": gateway.get("connected"),
                    "heartbeat_ok": gateway.get("heartbeat_ok"),
                    "kiwoom_logged_in": gateway.get("kiwoom_logged_in"),
                    "orderable": gateway.get("orderable"),
                },
                "commands": command_summary,
                "dry_run_orders": dry_run_order_summary,
                "realtime_data_quality": realtime_data_quality,
                "pending_price_tick_count": pending_price_tick_count,
                "dropped_price_tick_count": self._dropped_price_tick_count,
                "db_path": str(self.settings.db_path),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self.last_snapshot or {})

    def lightweight_status(self) -> dict[str, Any]:
        with self._state_lock:
            cycle_worker_pending = self._cycle_future is not None and not self._cycle_future.done()
            cycle_worker_elapsed_ms = 0
            if cycle_worker_pending and self._cycle_future_started_perf:
                cycle_worker_elapsed_ms = int(round((perf_counter() - self._cycle_future_started_perf) * 1000))
            return {
                "running": self.running,
                "started_at": self.started_at,
                "last_cycle_at": self.last_cycle_at,
                "next_cycle_at": self.next_cycle_at,
                "cycle_count": self.cycle_count,
                "failed_cycle_count": self.failed_cycle_count,
                "skipped_cycle_count": self.skipped_cycle_count,
                "last_cycle_duration_ms": self.last_cycle_duration_ms,
                "last_error": self.last_error,
                "worker_stage": self.worker_stage,
                "cycle_worker_pending": cycle_worker_pending,
                "cycle_worker_started_at": self._cycle_future_started_at if cycle_worker_pending else "",
                "cycle_worker_elapsed_ms": cycle_worker_elapsed_ms,
                "cycle_worker_reason": self._cycle_future_reason if cycle_worker_pending else "",
                "order_event_consumer": self._order_event_consumer_status_locked(),
                "broker_reconcile": self._broker_reconcile_status_locked(),
            }

    def set_dashboard_read_model_writer(self, writer: Any) -> None:
        self.read_model_writer = writer

    def set_order_event_consumer(self, consumer: Any) -> None:
        self.order_event_consumer = consumer

    def set_reconcile_orchestrator(self, orchestrator: Any) -> None:
        self.reconcile_orchestrator = orchestrator

    def _order_event_consumer_status_locked(self) -> dict[str, Any]:
        consumer = self.order_event_consumer
        if consumer is None:
            return {"enabled": False, "status": "NOT_CONFIGURED", "order_lifecycle_ready": False}
        health = getattr(consumer, "consumer_health", None)
        if callable(health):
            try:
                return dict(health() or {})
            except Exception as exc:
                return {"enabled": True, "status": "ERROR", "order_lifecycle_ready": False, "last_error": str(exc)}
        return {"enabled": True, "status": "UNKNOWN", "order_lifecycle_ready": False}

    def _broker_reconcile_status_locked(self) -> dict[str, Any]:
        orchestrator = self.reconcile_orchestrator
        if orchestrator is None:
            return {"enabled": False, "status": "NOT_CONFIGURED", "broker_truth_ready": False}
        health = getattr(orchestrator, "health_snapshot", None)
        if callable(health):
            try:
                return dict(health() or {})
            except Exception as exc:
                return {"enabled": True, "status": "ERROR", "broker_truth_ready": False, "last_error": str(exc)}
        return {"enabled": True, "status": "UNKNOWN", "broker_truth_ready": False}

    def _realtime_data_quality_snapshot(self) -> dict[str, Any]:
        bundle = self._bundle
        bridge = getattr(bundle, "market_data_bridge", None) if bundle is not None else None
        snapshot = getattr(bridge, "data_quality_snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            return dict(snapshot() or {})
        except Exception as exc:
            return {"status": "ERROR", "error": str(exc)}

    async def handle_gateway_event(self, event: GatewayEvent) -> None:
        if _is_broker_reconcile_event(event) and self.reconcile_orchestrator is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._reconcile_event_executor, self._consume_reconcile_event_in_worker, event)
            return
        if event.type in ORDER_LIFECYCLE_EVENT_TYPES and self.order_event_consumer is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._order_event_executor, self._consume_order_event_in_worker, event)
            if event.type not in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
                return
        if not self.enabled or self._bundle is None:
            return
        session_reset_reason = self._gateway_session_reset_reason(event)
        if session_reset_reason:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._mark_realtime_subscriptions_stale_in_worker, session_reset_reason)
            if event.type != "price_tick":
                return
        if event.type in {"condition_load_result", "condition_loaded"}:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._handle_gateway_event_in_worker, event)
            return
        if event.type == "condition_event":
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._handle_gateway_event_in_worker, event)
            return
        if event.type in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._handle_gateway_event_in_worker, event)
            return
        if event.type != "price_tick":
            return
        self._queue_price_tick(event)

    def _consume_order_event_in_worker(self, event: GatewayEvent) -> None:
        consumer = self.order_event_consumer
        handler = getattr(consumer, "consume_live_event", None)
        if not callable(handler):
            return
        try:
            handler(event)
        except Exception as exc:
            self._warn(f"ORDER_EVENT_CONSUME_FAILED:{event.type}:{exc}")

    def _consume_reconcile_event_in_worker(self, event: GatewayEvent) -> None:
        orchestrator = self.reconcile_orchestrator
        handler = getattr(orchestrator, "handle_gateway_event", None)
        if not callable(handler):
            return
        try:
            handler(event)
        except Exception as exc:
            self._warn(f"BROKER_RECONCILE_EVENT_CONSUME_FAILED:{event.type}:{exc}")

    async def readiness(self) -> dict[str, Any]:
        if self._bundle is not None:
            return self.status()["readiness"]
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, self._build_readiness_in_worker)
        except Exception as exc:
            return {"ok": False, "reason": f"READINESS_FAILED:{exc}", "warnings": [str(exc)]}

    async def shutdown(self) -> None:
        self._shutdown = True
        await self.stop()
        future = self._diagnostics_future
        if future is not None and not future.done():
            future.cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._order_event_executor.shutdown(wait=True, cancel_futures=True)
        self._reconcile_event_executor.shutdown(wait=True, cancel_futures=True)
        self._diagnostics_executor.shutdown(wait=True, cancel_futures=True)

    def _start_in_worker(self) -> dict[str, Any]:
        self._set_worker_stage("start")
        runtime = self._bundle.runtime if self._bundle is not None else self.build_runtime()
        try:
            snapshot = _call_with_optional_timing(runtime.start, self._record_cycle_timing)
            return _jsonable(snapshot)
        finally:
            self._set_worker_stage("idle")

    def _stop_in_worker(self) -> dict[str, Any]:
        self._set_worker_stage("stop")
        snapshot: dict[str, Any] = {}
        try:
            if self._bundle is not None:
                try:
                    snapshot = _jsonable(self._bundle.runtime.stop())
                finally:
                    self._bundle.db.close()
                    self._bundle = None
                    self._clear_pending_price_ticks()
            return snapshot
        finally:
            self._set_worker_stage("idle")

    def _cycle_in_worker(self) -> dict[str, Any]:
        if self._bundle is None:
            raise RuntimeError("runtime is not built")
        self._reset_cycle_timings()
        try:
            self._set_worker_stage("drain_price_ticks")
            forwarded_count = self._drain_price_ticks_in_worker()
            self._set_worker_stage("runtime_cycle")
            snapshot = _jsonable(_call_with_optional_timing(self._bundle.runtime.cycle, self._record_cycle_timing))
            snapshot["runtime_forwarded_price_tick_count"] = forwarded_count
            self._repair_realtime_subscriptions_if_no_ticks_in_worker(snapshot)
            return snapshot
        finally:
            self._set_worker_stage("idle")

    def _schedule_post_cycle_diagnostics(self, cycle_started_at: str) -> dict[str, Any]:
        if not self._post_cycle_diagnostics_enabled():
            return {"status": "DISABLED", "running": False, "cycle_started_at": cycle_started_at}
        if self._shutdown:
            return {"status": "SKIPPED", "running": False, "skip_reason": "SHUTTING_DOWN", "cycle_started_at": cycle_started_at}
        with self._state_lock:
            if self._diagnostics_future is not None and not self._diagnostics_future.done():
                self.post_cycle_diagnostics_skipped_count += 1
                skipped = self._post_cycle_diagnostics_status_locked()
                skipped.update(
                    {
                        "status": "SKIPPED",
                        "running": True,
                        "skip_reason": "ALREADY_RUNNING",
                        "cycle_started_at": cycle_started_at,
                    }
                )
                return skipped
            queued_at = _utc_now()
            self.post_cycle_diagnostics_running = True
            self.post_cycle_diagnostics_stage = "queued"
            self.post_cycle_diagnostics_last_started_at = queued_at
            self.post_cycle_diagnostics_last_finished_at = ""
            self.post_cycle_diagnostics_last_duration_ms = 0
            self.post_cycle_diagnostics_last_error = ""
            self._diagnostics_started_perf = perf_counter()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._diagnostics_executor,
            self._post_cycle_diagnostics_in_worker,
            cycle_started_at,
            queued_at,
        )
        with self._state_lock:
            self._diagnostics_future = future
            queued = self._post_cycle_diagnostics_status_locked()
            queued.update({"status": "QUEUED", "cycle_started_at": cycle_started_at})
        future.add_done_callback(self._consume_post_cycle_diagnostics)
        return queued

    def _post_cycle_diagnostics_in_worker(self, cycle_started_at: str, queued_at: str) -> dict[str, Any]:
        started = perf_counter()
        db = TradingDatabase(str(self.settings.db_path))
        try:
            self._set_post_cycle_diagnostics_stage("intraday_outcome_labeler")
            outcome = self._label_intraday_outcomes_in_worker(db)
            self._set_post_cycle_diagnostics_stage("shadow_strategy_evaluator")
            shadow = self._evaluate_shadow_strategies_in_worker(db)
            outcome_error = str((outcome or {}).get("error") or "") if isinstance(outcome, dict) else ""
            shadow_error = str((shadow or {}).get("error") or "") if isinstance(shadow, dict) else ""
            failed = (
                isinstance(outcome, dict)
                and str(outcome.get("status") or "").upper() == "FAILED"
                or isinstance(shadow, dict)
                and str(shadow.get("status") or "").upper() == "FAILED"
            )
            status = "FAILED" if failed else "OK"
            error = outcome_error or shadow_error
        except Exception as exc:
            status = "FAILED"
            error = _exception_message(exc)
            outcome = {"status": "FAILED", "error": error, "persisted_count": 0, "outcome_count": 0}
            shadow = {"status": "FAILED", "error": error, "persisted_count": 0, "evaluated_count": 0}
        finally:
            try:
                db.close()
            finally:
                self._set_post_cycle_diagnostics_stage("idle")
        finished_at = _utc_now()
        duration_ms = int(round((perf_counter() - started) * 1000))
        return {
            "intraday_outcome_labeler": _jsonable(outcome),
            "shadow_strategy_evaluator": _jsonable(shadow),
            "post_cycle_diagnostics": {
                "status": status,
                "running": False,
                "cycle_started_at": cycle_started_at,
                "queued_at": queued_at,
                "started_at": queued_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "error": error,
            },
        }

    def _consume_post_cycle_diagnostics(self, future: asyncio.Future) -> None:
        duration_ms = 0
        with self._state_lock:
            if self._diagnostics_started_perf:
                duration_ms = int(round((perf_counter() - self._diagnostics_started_perf) * 1000))
        try:
            result = _jsonable(future.result())
        except asyncio.CancelledError:
            result = {
                "post_cycle_diagnostics": {
                    "status": "CANCELLED",
                    "running": False,
                    "finished_at": _utc_now(),
                    "duration_ms": duration_ms,
                    "error": "CancelledError",
                },
                "intraday_outcome_labeler": {"status": "CANCELLED", "persisted_count": 0, "outcome_count": 0},
                "shadow_strategy_evaluator": {"status": "CANCELLED", "persisted_count": 0, "evaluated_count": 0},
            }
        except Exception as exc:
            error = _exception_message(exc)
            result = {
                "post_cycle_diagnostics": {
                    "status": "FAILED",
                    "running": False,
                    "finished_at": _utc_now(),
                    "duration_ms": duration_ms,
                    "error": error,
                },
                "intraday_outcome_labeler": {"status": "FAILED", "error": error, "persisted_count": 0, "outcome_count": 0},
                "shadow_strategy_evaluator": {"status": "FAILED", "error": error, "persisted_count": 0, "evaluated_count": 0},
            }
        metadata = dict(result.get("post_cycle_diagnostics") or {})
        status = str(metadata.get("status") or "OK")
        error = str(metadata.get("error") or "")
        if not duration_ms:
            duration_ms = int(metadata.get("duration_ms") or 0)
        with self._state_lock:
            self.post_cycle_diagnostics_running = False
            self.post_cycle_diagnostics_stage = "idle"
            self.post_cycle_diagnostics_last_finished_at = str(metadata.get("finished_at") or _utc_now())
            self.post_cycle_diagnostics_last_duration_ms = duration_ms
            self.post_cycle_diagnostics_last_error = error
            if status == "OK":
                self.post_cycle_diagnostics_run_count += 1
            else:
                self.post_cycle_diagnostics_failed_count += 1
            self.post_cycle_diagnostics_last_result = result
            snapshot = dict(self.last_snapshot or {})
            snapshot["intraday_outcome_labeler"] = result.get("intraday_outcome_labeler")
            snapshot["shadow_strategy_evaluator"] = result.get("shadow_strategy_evaluator")
            snapshot["post_cycle_diagnostics"] = metadata
            self.last_snapshot = snapshot
        if error and status not in {"CANCELLED"}:
            self._warn(f"POST_CYCLE_DIAGNOSTICS_FAILED:{error}")

    def _handle_gateway_event_in_worker(self, event: GatewayEvent) -> None:
        if self._bundle is None:
            return
        v2_runtime = self._is_reboot_v2_bundle()
        candidate_hydrator = getattr(self._bundle, "candidate_hydrator", None)
        if v2_runtime and candidate_hydrator is not None:
            handler = getattr(candidate_hydrator, "handle_event", None)
            if callable(handler) and handler(event):
                return
        if event.type == "condition_event":
            candidate_ingestion = getattr(self._bundle, "candidate_ingestion_service", None) if v2_runtime else None
            if candidate_ingestion is not None:
                condition_event = BrokerConditionEvent.from_dict(event.payload)
                candidate_ingestion.handle_condition_event(condition_event)
                return
            if v2_runtime:
                return
        condition_adapter = getattr(self._bundle.runtime, "condition_adapter", None)
        if condition_adapter is not None:
            handler = getattr(condition_adapter, "handle_event", None)
            if callable(handler) and handler(event):
                return
        opening_burst_pipeline = getattr(self._bundle, "opening_burst_pipeline", None) or getattr(self._bundle.runtime, "opening_burst_pipeline", None)
        if v2_runtime and opening_burst_pipeline is not None:
            handler = getattr(opening_burst_pipeline, "handle_event", None)
            if callable(handler) and handler(event):
                return
        self._bundle.market_data_bridge.handle_event(event)
        theme_bridge = getattr(self._bundle, "theme_runtime_bridge", None)
        if theme_bridge is not None:
            theme_bridge.handle_event(event)

    def _queue_price_tick(self, event: GatewayEvent) -> None:
        key = _price_tick_key(event)
        if not key:
            return
        with self._event_lock:
            existing = self._pending_price_ticks.get(key)
            if existing is not None and not _should_replace_pending_price_tick(existing, event):
                return
            if key not in self._pending_price_ticks and len(self._pending_price_ticks) >= MAX_PENDING_PRICE_TICKS:
                oldest_key = next(iter(self._pending_price_ticks), "")
                if oldest_key:
                    self._pending_price_ticks.pop(oldest_key, None)
                    self._dropped_price_tick_count += 1
            self._pending_price_ticks[key] = event

    def _drain_price_ticks_in_worker(self) -> int:
        with self._event_lock:
            events = list(self._pending_price_ticks.values())
            self._pending_price_ticks.clear()
        if self._bundle is None:
            return 0
        forwarded_count = 0
        for event in events:
            try:
                if self._bundle.market_data_bridge.handle_event(event):
                    forwarded_count += 1
            except Exception as exc:
                self._warn(f"RUNTIME_GATEWAY_EVENT_FAILED:{event.type}:{exc}")
        if forwarded_count:
            self._clear_realtime_subscription_stale_warnings()
        theme_bridge = getattr(self._bundle, "theme_runtime_bridge", None)
        if theme_bridge is not None:
            try:
                batch_handler = getattr(theme_bridge, "handle_events", None)
                if callable(batch_handler):
                    batch_handler(events)
                else:
                    for event in events:
                        theme_bridge.handle_event(event)
            except Exception as exc:
                self._warn(f"RUNTIME_GATEWAY_EVENT_FAILED:theme_batch:{exc}")
        return forwarded_count

    def _clear_pending_price_ticks(self) -> None:
        with self._event_lock:
            self._pending_price_ticks.clear()

    def _label_intraday_outcomes_in_worker(self, db: TradingDatabase | None = None) -> dict[str, Any]:
        if not bool(getattr(self.settings, "intraday_outcome_enabled", True)):
            return {"status": "DISABLED", "persisted_count": 0, "outcome_count": 0}
        active_db = db
        if active_db is None:
            if self._bundle is None:
                return {"status": "DISABLED", "persisted_count": 0, "outcome_count": 0}
            active_db = self._bundle.db
        try:
            labeler = IntradayOutcomeLabeler(
                active_db,
                config=outcome_config_from_settings(self.settings),
                price_provider=ThemeLabFlowPricePathProvider(active_db),
            )
            return labeler.rebuild(
                trade_date=datetime.now().date().isoformat(),
                limit=int(getattr(self.settings, "intraday_outcome_max_batch_size", 500)),
                persist=True,
            )
        except Exception as exc:
            self._warn(f"INTRADAY_OUTCOME_LABELER_FAILED:{exc}")
            return {"status": "FAILED", "error": str(exc), "persisted_count": 0, "outcome_count": 0}

    def _evaluate_shadow_strategies_in_worker(self, db: TradingDatabase | None = None) -> dict[str, Any]:
        if (
            not bool(getattr(self.settings, "shadow_strategy_enabled", True))
            or not bool(getattr(self.settings, "shadow_strategy_runtime_hook_enabled", True))
        ):
            return {"status": "DISABLED", "persisted_count": 0, "evaluated_count": 0}
        active_db = db
        if active_db is None:
            if self._bundle is None:
                return {"status": "DISABLED", "persisted_count": 0, "evaluated_count": 0}
            active_db = self._bundle.db
        try:
            evaluator = ShadowStrategyEvaluator(active_db, config=shadow_config_from_settings(self.settings))
            return evaluator.rebuild(
                trade_date=datetime.now().date().isoformat(),
                limit=int(getattr(self.settings, "shadow_strategy_max_batch_size", 500)),
                persist=True,
            )
        except Exception as exc:
            self._warn(f"SHADOW_STRATEGY_EVALUATOR_FAILED:{exc}")
            return {"status": "FAILED", "error": str(exc), "persisted_count": 0, "evaluated_count": 0}

    def _pending_price_tick_count(self) -> int:
        with self._event_lock:
            return len(self._pending_price_ticks)

    def _gateway_session_reset_reason(self, event: GatewayEvent) -> str:
        payload = dict(event.payload or {})
        if event.type == "login_status":
            logged_in = bool(payload.get("logged_in"))
            previous = self._last_gateway_logged_in
            self._last_gateway_logged_in = logged_in
            if logged_in and previous is not True:
                return "LOGIN_STATUS_TRUE"
            return ""
        if event.type != "heartbeat":
            return ""
        logged_in = bool(payload.get("kiwoom_logged_in", False))
        reasons: list[str] = []
        if logged_in and self._last_gateway_logged_in is not True:
            reasons.append("LOGIN_HEARTBEAT_TRUE")
        self._last_gateway_logged_in = logged_in
        reconnect_count = payload.get("reconnect_count")
        if reconnect_count is not None:
            try:
                current_reconnect_count = int(reconnect_count or 0)
            except (TypeError, ValueError):
                current_reconnect_count = None
            if current_reconnect_count is not None:
                previous_reconnect_count = self._last_gateway_reconnect_count
                self._last_gateway_reconnect_count = current_reconnect_count
                if previous_reconnect_count is not None and current_reconnect_count != previous_reconnect_count:
                    reasons.append(f"RECONNECT_COUNT_CHANGED:{previous_reconnect_count}->{current_reconnect_count}")
        if logged_in and reasons:
            return ",".join(reasons)
        return ""

    def _mark_realtime_subscriptions_stale_in_worker(self, reason: str) -> None:
        if self._bundle is None:
            return
        manager = getattr(getattr(self._bundle, "runtime", None), "subscription_manager", None)
        marker = getattr(manager, "mark_all_stale", None)
        if not callable(marker):
            self._warn("REALTIME_SUBSCRIPTION_STALE_MARK_UNSUPPORTED")
            return
        marker(reason)
        with self._state_lock:
            self._last_realtime_stale_total_ticks = _safe_int(
                self._realtime_data_quality_snapshot().get("total_price_ticks"),
                0,
            )
        self._warn(f"REALTIME_SUBSCRIPTIONS_STALE:{reason}")

    def _repair_realtime_subscriptions_if_no_ticks_in_worker(self, snapshot: dict[str, Any]) -> None:
        if not _env_bool("TRADING_REALTIME_NO_TICK_REPAIR_ENABLED", True):
            return
        if str(snapshot.get("market_session_status") or "").strip().lower() == "closed":
            return
        if _safe_int(snapshot.get("subscription_active_count"), 0) <= 0:
            return
        gateway = self.gateway_state.snapshot()
        if not gateway.kiwoom_logged_in or not gateway.heartbeat_ok:
            return
        if self._pending_price_tick_count() > 0:
            return
        quality = self._realtime_data_quality_snapshot()
        total_ticks = _safe_int(quality.get("total_price_ticks"), 0)
        with self._state_lock:
            baseline_ticks = int(self._last_realtime_stale_total_ticks or 0)
            if total_ticks > baseline_ticks:
                self._last_realtime_stale_total_ticks = total_ticks
                return
        started_perf = float(self._runtime_started_perf or 0.0)
        if started_perf <= 0:
            return
        now_perf = perf_counter()
        wait_sec = max(0, _env_int("TRADING_REALTIME_NO_TICK_REPAIR_AFTER_SEC", 45))
        if now_perf - started_perf < wait_sec:
            return
        cooldown_sec = max(1, _env_int("TRADING_REALTIME_NO_TICK_REPAIR_COOLDOWN_SEC", 60))
        if self._last_realtime_no_tick_repair_perf and now_perf < self._last_realtime_no_tick_repair_perf + cooldown_sec:
            return
        if self._bundle is None:
            return
        manager = getattr(getattr(self._bundle, "runtime", None), "subscription_manager", None)
        marker = getattr(manager, "mark_all_stale", None)
        if not callable(marker):
            return
        reason = "NO_PRICE_TICKS_AFTER_REGISTER"
        marker(reason)
        with self._state_lock:
            self._last_realtime_stale_total_ticks = _safe_int(
                self._realtime_data_quality_snapshot().get("total_price_ticks"),
                0,
            )
        self._last_realtime_no_tick_repair_perf = now_perf
        warning = f"REALTIME_SUBSCRIPTIONS_STALE:{reason}"
        self._warn(warning)
        snapshot["warnings"] = _dedupe_texts(list(snapshot.get("warnings") or []) + [warning, "REALTIME_NO_TICK_REPAIR_ENQUEUED"])
        snapshot["realtime_subscription_repair"] = {
            "status": "STALE_MARKED",
            "reason": reason,
            "subscription_active_count": _safe_int(snapshot.get("subscription_active_count"), 0),
            "total_price_ticks": total_ticks,
        }

    def _set_worker_stage(self, stage: str) -> None:
        with self._state_lock:
            self.worker_stage = str(stage or "idle")

    def _cycle_worker_pending(self) -> bool:
        with self._state_lock:
            return self._cycle_future is not None and not self._cycle_future.done()

    def _skip_cycle(self, message: str) -> None:
        with self._state_lock:
            self.skipped_cycle_count += 1
            self.next_cycle_at = _after_seconds(_utc_now(), self._interval_sec()) if self.running else ""
        self._log_runtime_event("cycle_skipped", "skipped", message)

    def _clear_cycle_future(self, future: asyncio.Future) -> None:
        with self._state_lock:
            if self._cycle_future is future:
                self._cycle_future = None
                self._cycle_future_started_at = ""
                self._cycle_future_started_perf = 0.0
                self._cycle_future_reason = ""

    def _consume_late_cycle_future(self, future: asyncio.Future, started_at: str, started: float) -> None:
        duration_ms = int(round((perf_counter() - started) * 1000))
        try:
            snapshot = _jsonable(future.result())
            if isinstance(snapshot, dict):
                snapshot["cycle_duration_ms"] = snapshot.get("cycle_duration_ms") or duration_ms
            self._log_runtime_event(
                "cycle_late_completed",
                "late",
                f"cycle completed after timeout in {duration_ms}ms",
                snapshot if isinstance(snapshot, dict) else {},
            )
        except asyncio.CancelledError:
            self._log_runtime_event("cycle_late_cancelled", "cancelled", "cycle worker future cancelled")
        except Exception as exc:
            self._log_runtime_event("cycle_late_failed", "failed", _exception_message(exc))
        finally:
            self._clear_cycle_future(future)

    def _set_post_cycle_diagnostics_stage(self, stage: str) -> None:
        with self._state_lock:
            self.post_cycle_diagnostics_stage = str(stage or "idle")

    def _post_cycle_diagnostics_enabled(self) -> bool:
        if self._is_reboot_v2_bundle():
            return False
        return bool(getattr(self.settings, "intraday_outcome_enabled", True)) or (
            bool(getattr(self.settings, "shadow_strategy_enabled", True))
            and bool(getattr(self.settings, "shadow_strategy_runtime_hook_enabled", True))
        )

    def _is_reboot_v2_bundle(self) -> bool:
        bundle = self._bundle
        if bundle is None:
            return False
        if str(getattr(bundle, "runtime_profile", "") or "").upper() != "LEGACY":
            return True
        return bool(getattr(getattr(bundle, "runtime", None), "is_reboot_v2_runtime", False))

    def _post_cycle_diagnostics_status_locked(self) -> dict[str, Any]:
        enabled = self._post_cycle_diagnostics_enabled()
        status = "DISABLED"
        if enabled:
            status = "RUNNING" if self.post_cycle_diagnostics_running else "IDLE"
            if self.post_cycle_diagnostics_last_error and not self.post_cycle_diagnostics_running:
                status = "FAILED"
        return {
            "enabled": enabled,
            "status": status,
            "running": self.post_cycle_diagnostics_running,
            "stage": self.post_cycle_diagnostics_stage,
            "last_started_at": self.post_cycle_diagnostics_last_started_at,
            "last_finished_at": self.post_cycle_diagnostics_last_finished_at,
            "last_duration_ms": self.post_cycle_diagnostics_last_duration_ms,
            "last_error": self.post_cycle_diagnostics_last_error,
            "run_count": self.post_cycle_diagnostics_run_count,
            "failed_count": self.post_cycle_diagnostics_failed_count,
            "skipped_count": self.post_cycle_diagnostics_skipped_count,
        }

    def _attach_diagnostics_placeholders(self, snapshot: dict[str, Any], diagnostics_status: dict[str, Any]) -> None:
        with self._state_lock:
            last_outcome = (self.post_cycle_diagnostics_last_result or {}).get("intraday_outcome_labeler")
            last_shadow = (self.post_cycle_diagnostics_last_result or {}).get("shadow_strategy_evaluator")
        placeholder_status = str(diagnostics_status.get("status") or "QUEUED")
        snapshot.setdefault(
            "intraday_outcome_labeler",
            last_outcome or {"status": placeholder_status, "persisted_count": 0, "outcome_count": 0},
        )
        snapshot.setdefault(
            "shadow_strategy_evaluator",
            last_shadow or {"status": placeholder_status, "persisted_count": 0, "evaluated_count": 0},
        )

    def _reset_cycle_timings(self) -> None:
        with self._state_lock:
            self.last_cycle_timings = {}

    def _record_cycle_timing(self, label: str, seconds: float) -> None:
        text = str(label or "")
        if text.endswith(":start"):
            self._set_worker_stage(f"runtime_cycle:{text[:-6]}")
            return
        with self._state_lock:
            self.last_cycle_timings[text] = round(float(seconds or 0.0), 6)

    def _build_readiness_in_worker(self) -> dict[str, Any]:
        db = TradingDatabase(str(self.settings.db_path))
        try:
            report = build_readiness_report(db)
            return _jsonable(report)
        finally:
            db.close()

    def _interval_sec(self) -> int:
        snapshot_interval = int((self.last_snapshot or {}).get("evaluation_interval_sec") or 0)
        return max(1, int(self.settings.runtime_evaluation_interval_sec or snapshot_interval or 5))

    def _warn(self, warning: str) -> None:
        with self._state_lock:
            if warning and warning not in self.warnings:
                self.warnings.append(str(warning))
                self.warnings = self.warnings[-100:]

    def _clear_realtime_subscription_stale_warnings(self) -> None:
        with self._state_lock:
            self.warnings = _without_realtime_stale_warnings(self.warnings)
            self._last_realtime_stale_total_ticks = _safe_int(
                self._realtime_data_quality_snapshot().get("total_price_ticks"),
                0,
            )

    def _log_runtime_event(self, event_type: str, status: str, message: str, payload: dict[str, Any] | None = None) -> None:
        try:
            db = TradingDatabase(str(self.settings.db_path))
            try:
                db.save_runtime_event(event_type, status=status, message=message, payload=payload or {})
                db.save_log(f"[runtime][{status}] {event_type}: {message}")
            finally:
                db.close()
        except Exception:
            pass

    def _log_runtime_cycle(
        self,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        status: str,
        snapshot: dict[str, Any],
        error: str,
    ) -> None:
        try:
            db = TradingDatabase(str(self.settings.db_path))
            try:
                db.save_runtime_cycle(
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    status=status,
                    snapshot=snapshot,
                    warning_count=len(snapshot.get("warnings") or []),
                    error=error,
                )
            finally:
                db.close()
        except Exception:
            pass

    def _flush_dashboard_read_model(self, reason: str) -> None:
        writer = self.read_model_writer
        if writer is None:
            return
        try:
            marker = getattr(writer, "mark_dirty", None)
            if callable(marker):
                marker(reason)
            flusher = getattr(writer, "write_if_due", None)
            if callable(flusher):
                flusher()
        except Exception as exc:
            self._warn(f"DASHBOARD_READ_MODEL_WRITE_FAILED:{_exception_message(exc)}")


def _order_policy(settings: CoreSettings) -> str:
    if settings.runtime_mode == "DRY_RUN":
        return "DRY_RUN_ORDER_ENQUEUE_DISABLED" if not settings.runtime_allow_dry_run_orders else "DRY_RUN_ONLY"
    return "OBSERVE_VIRTUAL_ONLY"


def _readiness_summary(snapshot: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    return {
        "market_session_status": snapshot.get("market_session_status", ""),
        "data_warmup_status": snapshot.get("data_warmup_status", ""),
        "gate_skip_reason": snapshot.get("gate_skip_reason", ""),
        "warning_count": len(warnings) + len(snapshot.get("warnings") or []),
        "warnings": (snapshot.get("warnings") or [])[-20:],
    }


def _has_realtime_stale_state(warnings: list[Any], snapshot: dict[str, Any]) -> bool:
    if any(str(warning or "").startswith("REALTIME_SUBSCRIPTIONS_STALE:") for warning in warnings):
        return True
    repair = dict(snapshot.get("realtime_subscription_repair") or {})
    return str(repair.get("status") or "").upper() == "STALE_MARKED"


def _realtime_ticks_recovered(
    realtime_data_quality: dict[str, Any],
    pending_price_tick_count: int,
    *,
    baseline_total_ticks: int = 0,
) -> bool:
    return pending_price_tick_count > 0 or _safe_int(realtime_data_quality.get("total_price_ticks"), 0) > int(baseline_total_ticks or 0)


def _without_realtime_stale_warnings(warnings: list[Any]) -> list[str]:
    return [
        str(warning)
        for warning in warnings
        if str(warning or "").strip()
        and not str(warning or "").startswith("REALTIME_SUBSCRIPTIONS_STALE:")
        and str(warning or "") != "REALTIME_NO_TICK_REPAIR_ENQUEUED"
    ]


def _snapshot_with_recovered_realtime_repair(
    snapshot: dict[str, Any],
    realtime_data_quality: dict[str, Any],
    pending_price_tick_count: int,
    *,
    recovered: bool,
) -> dict[str, Any]:
    repair = dict(snapshot.get("realtime_subscription_repair") or {})
    if str(repair.get("status") or "").upper() != "STALE_MARKED":
        return snapshot
    if not recovered:
        return snapshot
    repair.update(
        {
            "status": "RECOVERED",
            "reason": "PRICE_TICK_RECEIVED",
            "total_price_ticks": _safe_int(realtime_data_quality.get("total_price_ticks"), 0),
            "pending_price_tick_count": int(pending_price_tick_count),
        }
    )
    snapshot["realtime_subscription_repair"] = repair
    snapshot["warnings"] = _without_realtime_stale_warnings(list(snapshot.get("warnings") or []))
    return snapshot


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _dry_run_order_summary(db_path) -> dict[str, Any]:
    try:
        db = TradingDatabase(str(db_path))
        try:
            return db.runtime_order_intent_summary()
        finally:
            db.close()
    except Exception:
        return {}


def _exception_message(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return type(exc).__name__


def _price_tick_key(event: GatewayEvent) -> str:
    payload = dict(event.payload or {})
    key = str(payload.get("code") or payload.get("stock_code") or "").strip()
    if key:
        return key
    return str(event.event_id or event.command_id or "").strip()


def _is_broker_reconcile_event(event: GatewayEvent) -> bool:
    if event.type not in {"command_ack", "command_failed", "command_timeout", "command_expired"}:
        return False
    return str(dict(event.payload or {}).get("purpose") or "") == "broker_reconcile"


def _should_replace_pending_price_tick(existing: GatewayEvent, incoming: GatewayEvent) -> bool:
    return not (_price_tick_has_current_price(existing) and not _price_tick_has_current_price(incoming))


def _price_tick_has_current_price(event: GatewayEvent) -> bool:
    payload = dict(event.payload or {})
    try:
        return abs(float(str(payload.get("price") or payload.get("current_price") or 0).replace(",", ""))) > 0
    except (TypeError, ValueError):
        return False


def _call_with_optional_timing(callback: Callable[..., Any], timing_callback: Callable[[str, float], None]) -> Any:
    try:
        return callback(timing_callback=timing_callback)
    except TypeError as exc:
        if "timing_callback" not in str(exc):
            raise
        return callback()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _after_seconds(timestamp: str, seconds: int) -> str:
    base = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _dedupe_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
