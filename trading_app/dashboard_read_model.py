from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Sequence

from storage.dashboard_read_model import DashboardReadModelRecord, DashboardReadModelRepository, checksum_snapshot
from trading_app.dashboard_v2 import build_dashboard_v2_snapshot
from trading_app.pre_market_check import pre_market_report_empty


@dataclass(frozen=True)
class DashboardReadModelConfig:
    enabled: bool = True
    api_enabled: bool = True
    persist_enabled: bool = True
    write_interval_sec: float = 1.0
    stale_after_sec: int = 5
    skip_unchanged: bool = True
    fallback_live_build: bool = True
    history_enabled: bool = False
    shadow_compare_enabled: bool = True
    shadow_compare_interval_sec: int = 30
    ws_push_interval_sec: float = 1.0
    schema_version: str = "dashboard_v2.read_model.v1"

    @classmethod
    def from_env(cls) -> "DashboardReadModelConfig":
        return cls(
            enabled=_env_bool("TRADING_DASHBOARD_READ_MODEL_ENABLED", True),
            api_enabled=_env_bool("TRADING_DASHBOARD_READ_MODEL_API_ENABLED", True),
            persist_enabled=_env_bool("TRADING_DASHBOARD_READ_MODEL_PERSIST_ENABLED", True),
            write_interval_sec=max(0.1, _env_float("TRADING_DASHBOARD_READ_MODEL_WRITE_INTERVAL_SEC", 1.0)),
            stale_after_sec=max(1, _env_int("TRADING_DASHBOARD_READ_MODEL_STALE_AFTER_SEC", 5)),
            skip_unchanged=_env_bool("TRADING_DASHBOARD_READ_MODEL_SKIP_UNCHANGED", True),
            fallback_live_build=_env_bool("TRADING_DASHBOARD_READ_MODEL_FALLBACK_LIVE_BUILD", True),
            history_enabled=_env_bool("TRADING_DASHBOARD_READ_MODEL_HISTORY_ENABLED", False),
            shadow_compare_enabled=_env_bool("TRADING_DASHBOARD_READ_MODEL_SHADOW_COMPARE_ENABLED", True),
            shadow_compare_interval_sec=max(1, _env_int("TRADING_DASHBOARD_READ_MODEL_SHADOW_COMPARE_INTERVAL_SEC", 30)),
            ws_push_interval_sec=max(1.0, _env_float("TRADING_DASHBOARD_WS_PUSH_INTERVAL_SEC", 1.0)),
        )


class DashboardReadModelService:
    def __init__(
        self,
        repository: DashboardReadModelRepository | None = None,
        *,
        config: DashboardReadModelConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or DashboardReadModelConfig.from_env()
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc).replace(microsecond=0))
        self._lock = RLock()
        self._latest: DashboardReadModelRecord | None = None
        self._dirty = True
        self._dirty_reasons: list[str] = ["INIT"]
        self.metrics: dict[str, Any] = {
            "build_count": 0,
            "write_count": 0,
            "unchanged_skip_count": 0,
            "coalesced_signal_count": 0,
            "concurrent_write_skip_count": 0,
            "build_duration_ms": 0.0,
            "db_write_duration_ms": 0.0,
            "read_duration_ms": 0.0,
            "api_read_count": 0,
            "fallback_count": 0,
            "stale_read_count": 0,
            "websocket_push_count": 0,
            "websocket_push_skip_unchanged_count": 0,
            "last_build_at": "",
            "last_write_at": "",
            "last_error": "",
            "writer_status": "IDLE",
        }

    def update_snapshot(self, events: Sequence[Any], *, snapshot_at: str) -> None:
        reasons = [str(getattr(event, "type", "") or "CORE_EVENT") for event in events] or ["UPDATE"]
        self.mark_dirty(",".join(reasons))

    def build_from_runtime(
        self,
        runtime_snapshot: dict[str, Any] | None,
        gateway_snapshot: dict[str, Any] | None,
        command_snapshot: dict[str, Any] | None,
        core_status: dict[str, Any] | None,
        *,
        snapshot_at: str | None = None,
        view_name: str = "main",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        now_text = snapshot_at or _format_time(self.clock())
        runtime = dict(runtime_snapshot or {})
        gateway = dict(gateway_snapshot or {})
        commands = dict(command_snapshot or {})
        core = dict(core_status or {})
        source_cycle_at = str(core.get("last_cycle_at") or runtime.get("last_cycle_at") or runtime.get("cycle_at") or "")
        source_cycle_count = _safe_int(core.get("cycle_count") or runtime.get("cycle_count"), 0)
        order_manager = _order_manager_source(runtime)
        runtime_for_builder = dict(runtime)
        if order_manager:
            runtime_for_builder["order_manager"] = order_manager
        if core:
            runtime_for_builder.update(
                {
                    "running": core.get("running"),
                    "last_cycle_at": core.get("last_cycle_at") or runtime_for_builder.get("last_cycle_at"),
                    "last_cycle_duration_ms": core.get("last_cycle_duration_ms")
                    or runtime_for_builder.get("last_cycle_duration_ms"),
                    "last_error": core.get("last_error") or runtime_for_builder.get("last_error", ""),
                    "cycle_count": core.get("cycle_count") or runtime_for_builder.get("cycle_count", 0),
                    "failed_cycle_count": core.get("failed_cycle_count") or runtime_for_builder.get("failed_cycle_count", 0),
                    "skipped_cycle_count": core.get("skipped_cycle_count") or runtime_for_builder.get("skipped_cycle_count", 0),
                    "worker_stage": core.get("worker_stage") or runtime_for_builder.get("worker_stage", ""),
                    "cycle_worker_pending": core.get("cycle_worker_pending")
                    or runtime_for_builder.get("cycle_worker_pending", False),
                }
            )
        base = {
            "snapshot_detail": "slim",
            "timestamp": now_text,
            "core": core,
            "gateway": gateway,
            "commands": commands,
            "transport": dict(runtime.get("transport") or {}),
            "runtime": runtime_for_builder,
            "candidate_ingestion": dict(runtime.get("candidate_ingestion") or {}),
            "theme_board": dict(runtime.get("theme_board") or {}),
            "market_regime": dict(runtime.get("market_regime") or {}),
            "entry_engine": dict(runtime.get("entry_engine") or {}),
            "exit_engine": dict(runtime.get("exit_engine_reboot") or runtime.get("exit_engine") or {}),
            "position_risk": dict(runtime.get("position_risk") or {}),
            "order_manager": order_manager,
            "order_lifecycle": dict(core.get("order_event_consumer") or runtime.get("order_lifecycle") or {}),
            "candidates": dict(runtime.get("candidates") or {}),
            "pre_market_check": dict(runtime.get("pre_market_check") or pre_market_report_empty()),
        }
        payload = build_dashboard_v2_snapshot(base, detail="slim")
        build_duration_ms = (time.perf_counter() - started) * 1000.0
        payload["read_model"] = self._metadata(
            view_name=view_name,
            generation=0,
            status="BUILT",
            snapshot_at=now_text,
            checksum="",
            persisted=False,
            fallback_used=False,
            build_duration_ms=build_duration_ms,
            source_runtime_cycle_at=source_cycle_at,
            source_runtime_cycle_count=source_cycle_count,
        )
        self._augment_system_health(payload, core, gateway, commands, runtime_for_builder)
        payload["safety_banners"] = _dedupe_banners(list(payload.get("safety_banners") or []))
        return payload

    def write_if_due(
        self,
        *,
        runtime_snapshot: dict[str, Any] | None = None,
        gateway_snapshot: dict[str, Any] | None = None,
        command_snapshot: dict[str, Any] | None = None,
        core_status: dict[str, Any] | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "DISABLED", "written": False, "reason": "READ_MODEL_DISABLED"}
        current = now or self.clock()
        now_text = _format_time(current)
        with self._lock:
            last_write_at = str(self.metrics.get("last_write_at") or "")
            if not force and not self._dirty:
                return {"status": "SKIPPED", "written": False, "reason": "NOT_DIRTY"}
            if not force and last_write_at and _age_sec(last_write_at, now_text) < self.config.write_interval_sec:
                self.metrics["coalesced_signal_count"] = int(self.metrics.get("coalesced_signal_count") or 0) + 1
                return {"status": "COALESCED", "written": False, "reason": "WRITE_INTERVAL"}
            if self.metrics.get("writer_status") == "RUNNING":
                self.metrics["concurrent_write_skip_count"] = int(self.metrics.get("concurrent_write_skip_count") or 0) + 1
                return {"status": "SKIPPED", "written": False, "reason": "WRITER_RUNNING"}
            self.metrics["writer_status"] = "RUNNING"
            self._dirty = False
            self._dirty_reasons = []
        try:
            payload = self.build_from_runtime(
                runtime_snapshot,
                gateway_snapshot,
                command_snapshot,
                core_status,
                snapshot_at=now_text,
            )
            record = self.save_snapshot(payload)
            return {
                "status": "OK",
                "written": not record.unchanged,
                "generation": record.generation,
                "checksum": record.checksum,
                "unchanged": record.unchanged,
            }
        except Exception as exc:
            with self._lock:
                self._dirty = True
                self.metrics["last_error"] = str(exc)
                self.metrics["writer_status"] = "FAILED"
            return {"status": "FAILED", "written": False, "error": str(exc)}
        finally:
            with self._lock:
                if self.metrics.get("writer_status") == "RUNNING":
                    self.metrics["writer_status"] = "IDLE"

    def save_snapshot(self, snapshot: dict[str, Any]) -> DashboardReadModelRecord:
        payload = dict(snapshot or {})
        metadata = dict(payload.get("read_model") or {})
        view_name = str(metadata.get("view_name") or "main")
        snapshot_at = str(metadata.get("snapshot_at") or _format_time(self.clock()))
        source_cycle_at = str(metadata.get("source_runtime_cycle_at") or "")
        source_cycle_count = _safe_int(metadata.get("source_runtime_cycle_count"), 0)
        build_duration_ms = float(metadata.get("build_duration_ms") or 0.0)
        checksum = checksum_snapshot(payload)
        generation = _safe_int(metadata.get("generation"), 0)
        payload["read_model"] = {
            **metadata,
            "enabled": self.config.enabled,
            "source": "READ_MODEL",
            "view_name": view_name,
            "schema_version": self.config.schema_version,
            "status": "OK",
            "snapshot_at": snapshot_at,
            "snapshot_age_sec": 0.0,
            "stale": False,
            "stale_after_sec": self.config.stale_after_sec,
            "source_runtime_cycle_at": source_cycle_at,
            "source_runtime_cycle_age_sec": _age_sec(source_cycle_at, snapshot_at) if source_cycle_at else 0.0,
            "source_runtime_cycle_count": source_cycle_count,
            "checksum": checksum,
            "persisted": bool(self.config.persist_enabled and self.repository is not None),
            "fallback_used": False,
            "build_duration_ms": build_duration_ms,
            "last_error": "",
            "warnings": list(metadata.get("warnings") or []),
        }
        started = time.perf_counter()
        if self.config.persist_enabled and self.repository is not None:
            record = self.repository.save_snapshot(
                payload,
                view_name=view_name,
                schema_version=self.config.schema_version,
                generation=generation,
                snapshot_at=snapshot_at,
                source_runtime_cycle_at=source_cycle_at,
                source_runtime_cycle_count=source_cycle_count,
                stale_after_sec=self.config.stale_after_sec,
                build_duration_ms=build_duration_ms,
                checksum=checksum,
                skip_unchanged=self.config.skip_unchanged,
            )
        else:
            record = DashboardReadModelRecord(
                view_name=view_name,
                schema_version=self.config.schema_version,
                generation=generation + 1,
                snapshot=payload,
                checksum=checksum,
                status="OK",
                snapshot_at=snapshot_at,
                source_runtime_cycle_at=source_cycle_at,
                source_runtime_cycle_count=source_cycle_count,
                stale_after_sec=self.config.stale_after_sec,
                build_duration_ms=build_duration_ms,
                persisted=False,
            )
        db_write_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            if record.unchanged:
                self.metrics["unchanged_skip_count"] = int(self.metrics.get("unchanged_skip_count") or 0) + 1
            else:
                self.metrics["write_count"] = int(self.metrics.get("write_count") or 0) + 1
            self.metrics["build_count"] = int(self.metrics.get("build_count") or 0) + 1
            self.metrics["build_duration_ms"] = build_duration_ms
            self.metrics["db_write_duration_ms"] = db_write_ms
            self.metrics["last_build_at"] = snapshot_at
            self.metrics["last_write_at"] = snapshot_at
            self.metrics["last_error"] = ""
            self._latest = record
        return record

    def read_main_snapshot(self) -> dict[str, Any]:
        return self.read_snapshot("main")

    def read_snapshot(self, view_name: str) -> dict[str, Any]:
        started = time.perf_counter()
        record = self._record_for_read(view_name)
        with self._lock:
            self.metrics["api_read_count"] = int(self.metrics.get("api_read_count") or 0) + 1
            self.metrics["read_duration_ms"] = (time.perf_counter() - started) * 1000.0
        if record is None:
            return self._empty_snapshot("MISSING", view_name=view_name)
        if record.status == "CORRUPT":
            return self._empty_snapshot("CORRUPT", view_name=view_name, error=record.last_error)
        payload = dict(record.snapshot or {})
        return self._with_current_metadata(payload, record)

    def mark_dirty(self, reason: str) -> None:
        text = str(reason or "UPDATE").strip()
        with self._lock:
            if self._dirty:
                self.metrics["coalesced_signal_count"] = int(self.metrics.get("coalesced_signal_count") or 0) + 1
            self._dirty = True
            if text and text not in self._dirty_reasons:
                self._dirty_reasons.append(text)
                self._dirty_reasons = self._dirty_reasons[-20:]

    def snapshot_status(self) -> dict[str, Any]:
        latest = self._latest
        return {
            "enabled": self.config.enabled,
            "api_enabled": self.config.api_enabled,
            "persist_enabled": self.config.persist_enabled,
            "dirty": self._dirty,
            "dirty_reasons": list(self._dirty_reasons),
            "latest_generation": latest.generation if latest else 0,
            "latest_snapshot_at": latest.snapshot_at if latest else "",
            "metrics": dict(self.metrics),
            "repository": self.repository.snapshot_status() if self.repository is not None else {},
        }

    def recover_latest_snapshot(self) -> dict[str, Any]:
        if self.repository is None:
            return self._empty_snapshot("MISSING", view_name="main")
        record = self.repository.recover_latest_snapshot("main")
        with self._lock:
            self._latest = record
        if record is None:
            return self._empty_snapshot("MISSING", view_name="main")
        payload = self._with_current_metadata(dict(record.snapshot or {}), record)
        read_model = dict(payload.get("read_model") or {})
        read_model["recovered"] = True
        read_model["stale"] = True
        read_model.setdefault("warnings", [])
        warnings = list(read_model.get("warnings") or [])
        if "READ_MODEL_RECOVERED_FROM_STORAGE" not in warnings:
            warnings.append("READ_MODEL_RECOVERED_FROM_STORAGE")
        read_model["warnings"] = warnings
        payload["read_model"] = read_model
        payload["safety_banners"] = _add_banner(
            payload.get("safety_banners") or [],
            "warning",
            "마지막 저장 Dashboard snapshot을 복구해 표시 중입니다.",
            "READ_MODEL_RECOVERED_FROM_STORAGE",
        )
        return payload

    def _record_for_read(self, view_name: str) -> DashboardReadModelRecord | None:
        view = str(view_name or "main")
        with self._lock:
            if self._latest is not None and self._latest.view_name == view:
                return self._latest
        if self.repository is None:
            return None
        record = self.repository.read_snapshot(view)
        with self._lock:
            if record is not None:
                self._latest = record
        return record

    def _with_current_metadata(self, payload: dict[str, Any], record: DashboardReadModelRecord) -> dict[str, Any]:
        current = _format_time(self.clock())
        age_sec = _age_sec(record.snapshot_at, current)
        source_age_sec = _age_sec(record.source_runtime_cycle_at, current) if record.source_runtime_cycle_at else 0.0
        stale_reasons: list[str] = []
        if age_sec > record.stale_after_sec:
            stale_reasons.append("READ_MODEL_STALE")
        if record.source_runtime_cycle_at and source_age_sec > max(record.stale_after_sec, 10):
            stale_reasons.append("RUNTIME_SNAPSHOT_STALE")
        order = dict(payload.get("order_manager") or {})
        if bool(order.get("reconcile_required_count")) or bool(order.get("stop_new_buy")):
            stale_reasons.append("ORDER_RECONCILE_REQUIRED")
        if stale_reasons:
            with self._lock:
                self.metrics["stale_read_count"] = int(self.metrics.get("stale_read_count") or 0) + 1
        metadata = dict(payload.get("read_model") or {})
        metadata.update(
            {
                "enabled": self.config.enabled,
                "source": metadata.get("source") or "READ_MODEL",
                "view_name": record.view_name,
                "schema_version": record.schema_version or self.config.schema_version,
                "generation": record.generation,
                "status": "STALE" if stale_reasons else record.status,
                "snapshot_at": record.snapshot_at,
                "snapshot_age_sec": round(age_sec, 3),
                "stale": bool(stale_reasons),
                "stale_after_sec": record.stale_after_sec,
                "source_runtime_cycle_at": record.source_runtime_cycle_at,
                "source_runtime_cycle_age_sec": round(source_age_sec, 3),
                "source_runtime_cycle_count": record.source_runtime_cycle_count,
                "checksum": record.checksum,
                "persisted": record.persisted,
                "fallback_used": False,
                "build_duration_ms": record.build_duration_ms,
                "last_error": record.last_error,
                "warnings": _dedupe_texts(list(metadata.get("warnings") or []) + stale_reasons),
            }
        )
        payload["read_model"] = metadata
        if stale_reasons:
            payload["safety_banners"] = _add_banner(
                payload.get("safety_banners") or [],
                "warning",
                "Dashboard snapshot이 오래되었습니다. 마지막 정상 데이터를 표시 중입니다.",
                stale_reasons[0],
            )
        self._augment_system_health(payload, {}, {}, {}, {})
        return payload

    def _empty_snapshot(self, status: str, *, view_name: str, error: str = "") -> dict[str, Any]:
        now_text = _format_time(self.clock())
        payload = build_dashboard_v2_snapshot({}, detail="slim")
        payload["read_model"] = self._metadata(
            view_name=view_name,
            generation=0,
            status=status,
            snapshot_at=now_text,
            checksum="",
            persisted=False,
            fallback_used=False,
            build_duration_ms=0.0,
            source_runtime_cycle_at="",
            source_runtime_cycle_count=0,
            last_error=error,
        )
        payload["safety_banners"] = _add_banner(
            payload.get("safety_banners") or [],
            "warning",
            "Dashboard read model을 아직 사용할 수 없습니다.",
            f"READ_MODEL_{status}",
        )
        return payload

    def _metadata(
        self,
        *,
        view_name: str,
        generation: int,
        status: str,
        snapshot_at: str,
        checksum: str,
        persisted: bool,
        fallback_used: bool,
        build_duration_ms: float,
        source_runtime_cycle_at: str,
        source_runtime_cycle_count: int,
        last_error: str = "",
    ) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "source": "READ_MODEL",
            "view_name": view_name,
            "schema_version": self.config.schema_version,
            "generation": int(generation or 0),
            "status": status,
            "snapshot_at": snapshot_at,
            "snapshot_age_sec": 0.0,
            "stale": False,
            "stale_after_sec": self.config.stale_after_sec,
            "source_runtime_cycle_at": source_runtime_cycle_at,
            "source_runtime_cycle_age_sec": 0.0,
            "source_runtime_cycle_count": int(source_runtime_cycle_count or 0),
            "checksum": checksum,
            "persisted": persisted,
            "fallback_used": fallback_used,
            "build_duration_ms": round(float(build_duration_ms or 0.0), 3),
            "last_error": last_error,
            "warnings": [],
        }

    def _augment_system_health(
        self,
        payload: dict[str, Any],
        core: dict[str, Any],
        gateway: dict[str, Any],
        commands: dict[str, Any],
        runtime: dict[str, Any],
    ) -> None:
        health = dict(payload.get("system_health") or {})
        order = dict(payload.get("order_manager") or {})
        dirty = dict(runtime.get("dirty_evaluator") or runtime.get("dirty_strategy_evaluator") or {})
        fsm = dict(runtime.get("candidate_fsm") or {})
        health.setdefault(
            "core",
            {
                "running": bool(core.get("running")),
                "last_cycle_at": core.get("last_cycle_at") or "",
                "last_cycle_age_sec": _age_sec(str(core.get("last_cycle_at") or ""), _format_time(self.clock()))
                if core.get("last_cycle_at")
                else 0.0,
                "cycle_duration_ms": core.get("last_cycle_duration_ms") or 0,
                "failed_cycle_count": core.get("failed_cycle_count") or 0,
                "skipped_cycle_count": core.get("skipped_cycle_count") or 0,
                "worker_pending": bool(core.get("cycle_worker_pending")),
                "last_error": core.get("last_error") or "",
            },
        )
        health.setdefault(
            "gateway",
            {
                "connected": bool(gateway.get("connected")),
                "heartbeat_ok": bool(gateway.get("heartbeat_ok")),
                "heartbeat_age_sec": gateway.get("heartbeat_age_sec"),
                "kiwoom_logged_in": bool(gateway.get("kiwoom_logged_in")),
                "orderable": bool(gateway.get("orderable")),
                "reconnect_count": int(gateway.get("reconnect_count") or 0),
                "pending_command_count": int(commands.get("queued_count") or gateway.get("pending_command_count") or 0),
            },
        )
        health.setdefault(
            "strategy",
            {
                "dirty_evaluator_status": dirty.get("status", ""),
                "evaluated_count": int(dirty.get("evaluated_count") or 0),
                "debounced_count": int(dirty.get("debounced_count") or 0),
                "skipped_count": int(dirty.get("skipped_count") or 0),
                "candidate_fsm_state_counts": dict(fsm.get("state_counts") or {}),
                "blocking_stage_counts": dict(fsm.get("blocking_stage_counts") or {}),
            },
        )
        event_log = dict(runtime.get("event_log") or runtime.get("gateway_event_log") or {})
        health.setdefault(
            "event_log",
            {
                "pending_count": int(event_log.get("pending_count") or 0),
                "failed_count": int(event_log.get("failed_count") or 0),
                "oldest_pending_age_sec": event_log.get("oldest_pending_age_sec", 0),
                "duplicate_count": int(event_log.get("duplicate_count") or 0),
            },
        )
        market_data = dict(runtime.get("market_data") or runtime.get("market_data_service") or {})
        health.setdefault(
            "market_data",
            {
                "latest_tick_status": market_data.get("latest_tick_status") or market_data.get("status") or "",
                "stale_code_count": int(market_data.get("stale_code_count") or 0),
                "dirty_queue_count": int(market_data.get("dirty_queue_count") or 0),
                "dropped_tick_count": int(market_data.get("dropped_tick_count") or 0),
                "coalesced_tick_count": int(market_data.get("coalesced_tick_count") or 0),
            },
        )
        health.setdefault(
            "order",
            {
                "risk_state": order.get("risk_state", ""),
                "kill_switch_state": order.get("kill_switch_state", "NORMAL"),
                "reconcile_required_count": int(order.get("reconcile_required_count") or 0),
                "stop_new_buy": bool(order.get("stop_new_buy")),
                "reduce_only": bool(order.get("reduce_only")),
                "last_reject_reason": order.get("last_reject_reason", ""),
            },
        )
        order_lifecycle = dict(core.get("order_event_consumer") or payload.get("order_lifecycle") or {})
        if order_lifecycle:
            payload["order_lifecycle"] = order_lifecycle
            health["order_lifecycle"] = {
                "status": order_lifecycle.get("status", ""),
                "consumer_enabled": bool(order_lifecycle.get("consumer_enabled")),
                "consumer_running": bool(order_lifecycle.get("consumer_running")),
                "order_lifecycle_ready": bool(order_lifecycle.get("order_lifecycle_ready")),
                "pending_event_count": int(order_lifecycle.get("pending_event_count") or 0),
                "retry_wait_count": int(order_lifecycle.get("retry_wait_count") or 0),
                "failed_count": int(order_lifecycle.get("failed_count") or 0),
                "dead_letter_count": int(order_lifecycle.get("dead_letter_count") or 0),
                "oldest_pending_age_sec": float(order_lifecycle.get("oldest_pending_age_sec") or 0.0),
                "processed_count": int(order_lifecycle.get("processed_count") or 0),
                "duplicate_applied_count": int(order_lifecycle.get("duplicate_applied_count") or 0),
                "unmatched_event_count": int(order_lifecycle.get("unmatched_event_count") or 0),
                "reconcile_required_count": int(order_lifecycle.get("reconcile_required_count") or 0),
                "last_event_type": order_lifecycle.get("last_event_type", ""),
                "last_event_at": order_lifecycle.get("last_event_at", ""),
                "last_processed_at": order_lifecycle.get("last_processed_at", ""),
                "last_error": order_lifecycle.get("last_error", ""),
                "replay_status": order_lifecycle.get("replay_status", ""),
                "replay_duration_ms": float(order_lifecycle.get("replay_duration_ms") or 0.0),
            }
            if not bool(order_lifecycle.get("order_lifecycle_ready", True)):
                payload["safety_banners"] = _add_banner(
                    payload.get("safety_banners") or [],
                    "critical",
                    "주문 이벤트 처리/복구 상태 확인이 필요합니다.",
                    "ORDER_LIFECYCLE_NOT_READY",
                )
            if int(order_lifecycle.get("dead_letter_count") or 0) > 0:
                payload["safety_banners"] = _add_banner(
                    payload.get("safety_banners") or [],
                    "critical",
                    "주문 이벤트 Dead Letter가 있어 신규 매수를 중지해야 합니다.",
                    "ORDER_EVENT_DEAD_LETTER",
                )
            if int(order_lifecycle.get("unmatched_event_count") or 0) > 0:
                payload["safety_banners"] = _add_banner(
                    payload.get("safety_banners") or [],
                    "critical",
                    "미확인 주문/체결 이벤트가 있어 reconciliation이 필요합니다.",
                    "UNMATCHED_ORDER_EVENT",
                )
        health["read_model"] = {
            "generation": dict(payload.get("read_model") or {}).get("generation", 0),
            "age_sec": dict(payload.get("read_model") or {}).get("snapshot_age_sec", 0),
            "stale": bool(dict(payload.get("read_model") or {}).get("stale")),
            "writer_status": self.metrics.get("writer_status", "IDLE"),
            "last_writer_duration_ms": self.metrics.get("build_duration_ms", 0),
            "last_writer_error": self.metrics.get("last_error", ""),
        }
        payload["system_health"] = health


class DashboardReadModelWriter:
    def __init__(
        self,
        service: DashboardReadModelService,
        *,
        runtime_snapshot: Callable[[], dict[str, Any]],
        gateway_snapshot: Callable[[], dict[str, Any]],
        command_snapshot: Callable[[], dict[str, Any]],
        core_status: Callable[[], dict[str, Any]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self.runtime_snapshot = runtime_snapshot
        self.gateway_snapshot = gateway_snapshot
        self.command_snapshot = command_snapshot
        self.core_status = core_status
        self.clock = clock or (lambda: datetime.now(timezone.utc).replace(microsecond=0))

    def mark_dirty(self, reason: str) -> None:
        self.service.mark_dirty(reason)

    def write_if_due(self, now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
        try:
            runtime_snapshot = self.runtime_snapshot()
            gateway_snapshot = self.gateway_snapshot()
            command_snapshot = self.command_snapshot()
            core_status = self.core_status()
        except Exception as exc:
            self.service.metrics["last_error"] = str(exc)
            self.service.metrics["writer_status"] = "FAILED"
            return {"status": "FAILED", "written": False, "error": str(exc)}
        return self.service.write_if_due(
            runtime_snapshot=runtime_snapshot,
            gateway_snapshot=gateway_snapshot,
            command_snapshot=command_snapshot,
            core_status=core_status,
            now=now or self.clock(),
            force=force,
        )


def compare_dashboard_v2_snapshots(read_model: dict[str, Any], legacy: dict[str, Any], *, compared_at: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    left = dict(read_model or {})
    right = dict(legacy or {})
    mismatches: list[dict[str, Any]] = []
    checks = {
        "v2_status.status_label": (
            _path(left, "v2_status", "status_label"),
            _path(right, "v2_status", "status_label"),
        ),
        "market_overview.global_status": (
            _path(left, "market_overview", "global_status"),
            _path(right, "market_overview", "global_status"),
        ),
        "leading_themes.top5": (
            _theme_keys(_path(left, "leading_themes", "items")),
            _theme_keys(_path(right, "leading_themes", "items")),
        ),
        "entry_candidates.bucket_counts": (
            dict(_path(left, "entry_candidates", "bucket_counts") or {}),
            dict(_path(right, "entry_candidates", "bucket_counts") or {}),
        ),
        "position_risk.counts": (
            (
                _path(left, "position_risk", "open_position_count"),
                _path(left, "position_risk", "exit_now_count"),
                _path(left, "position_risk", "scale_out_count"),
            ),
            (
                _path(right, "position_risk", "open_position_count"),
                _path(right, "position_risk", "exit_now_count"),
                _path(right, "position_risk", "scale_out_count"),
            ),
        ),
        "order_manager.state": (
            (
                _path(left, "order_manager", "risk_state"),
                _path(left, "order_manager", "kill_switch_state"),
                _path(left, "order_manager", "reconcile_required_count"),
            ),
            (
                _path(right, "order_manager", "risk_state"),
                _path(right, "order_manager", "kill_switch_state"),
                _path(right, "order_manager", "reconcile_required_count"),
            ),
        ),
        "wait_block_reasons.top": (
            _reason_keys(_path(left, "wait_block_reasons", "items")),
            _reason_keys(_path(right, "wait_block_reasons", "items")),
        ),
        "system_health.summary_status": (
            _path(left, "system_health", "summary_status"),
            _path(right, "system_health", "summary_status"),
        ),
    }
    for field, (left_value, right_value) in checks.items():
        if left_value != right_value:
            mismatches.append({"field": field, "read_model": left_value, "legacy": right_value})
    sections = sorted({item["field"].split(".", 1)[0] for item in mismatches})
    duration_ms = (time.perf_counter() - started) * 1000.0
    return {
        "compared_at": compared_at or _format_time(datetime.now(timezone.utc)),
        "matched": not mismatches,
        "section_mismatch_count": len(sections),
        "field_mismatch_count": len(mismatches),
        "mismatched_sections": sections,
        "mismatches": mismatches,
        "legacy_build_duration_ms": 0.0,
        "read_model_build_duration_ms": float(dict(left.get("read_model") or {}).get("build_duration_ms") or 0.0),
        "compare_duration_ms": round(duration_ms, 3),
    }


def open_dashboard_read_model_service(
    db_path: str | Path,
    *,
    config: DashboardReadModelConfig | None = None,
) -> DashboardReadModelService:
    resolved_config = config or DashboardReadModelConfig.from_env()
    repository = DashboardReadModelRepository(db_path) if resolved_config.persist_enabled else None
    service = DashboardReadModelService(repository, config=resolved_config)
    if repository is not None:
        service.recover_latest_snapshot()
    return service


def _order_manager_source(runtime: dict[str, Any]) -> dict[str, Any]:
    for key in ("order_manager_v2", "order_manager"):
        value = runtime.get(key)
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def _path(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _theme_keys(rows: Any) -> list[tuple[Any, Any]]:
    result = []
    for item in list(rows or [])[:5]:
        data = dict(item or {}) if isinstance(item, dict) else {}
        result.append((data.get("theme_name") or data.get("name"), data.get("leader_symbol") or data.get("leader_name")))
    return result


def _reason_keys(rows: Any) -> list[Any]:
    result = []
    for item in list(rows or [])[:5]:
        data = dict(item or {}) if isinstance(item, dict) else {}
        result.append(data.get("reason_code") or data.get("reason"))
    return result


def _add_banner(rows: Any, severity: str, message_ko: str, reason_code: str) -> list[dict[str, Any]]:
    values = [dict(item or {}) for item in list(rows or []) if isinstance(item, dict)]
    if not any(str(item.get("reason_code") or "") == reason_code for item in values):
        values.insert(0, {"severity": severity, "message_ko": message_ko, "reason_code": reason_code})
    return values


def _dedupe_banners(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        key = str(item.get("reason_code") or item.get("message_ko") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(item)
    return result


def _dedupe_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _age_sec(start: str, end: str) -> float:
    if not start or not end:
        return 0.0
    try:
        started = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return max(0.0, (ended - started).total_seconds())


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "DashboardReadModelConfig",
    "DashboardReadModelRecord",
    "DashboardReadModelRepository",
    "DashboardReadModelService",
    "DashboardReadModelWriter",
    "compare_dashboard_v2_snapshots",
    "open_dashboard_read_model_service",
]
