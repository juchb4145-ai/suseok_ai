from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from threading import RLock
from typing import Any, Callable

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.readiness import build_readiness_report
from trading.strategy.runtime import StrategyRuntime
from trading_app.dependencies import CoreSettings
from trading_app.runtime_factory import CoreRuntimeBundle, build_core_strategy_runtime


RuntimeBuilder = Callable[..., CoreRuntimeBundle]
MAX_PENDING_PRICE_TICKS = 2000


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        settings: CoreSettings,
        gateway_state: GatewayStateStore,
        runtime_builder: RuntimeBuilder = build_core_strategy_runtime,
    ) -> None:
        self.settings = settings
        self.gateway_state = gateway_state
        self.runtime_builder = runtime_builder
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
        self._pending_price_ticks: dict[str, GatewayEvent] = {}
        self._dropped_price_tick_count = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-runtime")
        self._bundle: CoreRuntimeBundle | None = None
        self._shutdown = False
        self._last_gateway_logged_in: bool | None = None
        self._last_gateway_reconnect_count: int | None = None
        if self.mode != "OBSERVE":
            self._warn(f"RUNTIME_ORDER_MODE_FORCED_OBSERVE:{self.mode}")
        if self.settings.runtime_allow_live_orders:
            self._warn("RUNTIME_LIVE_ORDERS_DISABLED_IN_PR5")

    def build_runtime(self) -> StrategyRuntime:
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
            self.started_at = now
            self.stopped_at = ""
            self.last_error = ""
            self.last_snapshot = snapshot
            self.next_cycle_at = _after_seconds(now, self._interval_sec())
        self._log_runtime_event("started", "running", "runtime started", snapshot)
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
        if self._cycle_lock.locked():
            with self._state_lock:
                self.skipped_cycle_count += 1
            self._log_runtime_event("cycle_skipped", "skipped", f"cycle already running: {reason}")
            return self.status()
        async with self._cycle_lock:
            if reason == "manual":
                with self._state_lock:
                    self.manual_cycle_count += 1
            started_at = _utc_now()
            started = perf_counter()
            loop = asyncio.get_running_loop()
            try:
                snapshot = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, self._cycle_in_worker),
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
                return self.status()
            duration_ms = int(round((perf_counter() - started) * 1000))
            snapshot["cycle_duration_ms"] = snapshot.get("cycle_duration_ms") or duration_ms
            with self._state_lock:
                self.cycle_count += 1
                self.last_cycle_at = started_at
                self.last_cycle_duration_ms = duration_ms
                self.last_snapshot = snapshot
                self.last_error = ""
                self.next_cycle_at = _after_seconds(_utc_now(), self._interval_sec()) if self.running else ""
            self._log_runtime_cycle(
                started_at,
                _utc_now(),
                duration_ms,
                "ok",
                snapshot,
                "",
            )
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
        gateway = self.gateway_state.snapshot().to_dict()
        command_summary = self.gateway_state.command_snapshot()
        dry_run_order_summary = _dry_run_order_summary(self.settings.db_path)
        with self._state_lock:
            snapshot = dict(self.last_snapshot or {})
            warnings = list(self.warnings[-50:])
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
                "pending_price_tick_count": self._pending_price_tick_count(),
                "dropped_price_tick_count": self._dropped_price_tick_count,
                "db_path": str(self.settings.db_path),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self.last_snapshot or {})

    async def handle_gateway_event(self, event: GatewayEvent) -> None:
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
        if event.type != "price_tick":
            return
        self._queue_price_tick(event)

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
        self._executor.shutdown(wait=True, cancel_futures=True)

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
            return snapshot
        finally:
            self._set_worker_stage("idle")

    def _handle_gateway_event_in_worker(self, event: GatewayEvent) -> None:
        if self._bundle is None:
            return
        condition_adapter = getattr(self._bundle.runtime, "condition_adapter", None)
        if condition_adapter is not None:
            handler = getattr(condition_adapter, "handle_event", None)
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
        self._warn(f"REALTIME_SUBSCRIPTIONS_STALE:{reason}")

    def _set_worker_stage(self, stage: str) -> None:
        with self._state_lock:
            self.worker_stage = str(stage or "idle")

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
