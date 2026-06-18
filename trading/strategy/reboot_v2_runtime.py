from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import Candidate, CandidateState, VirtualOrderStatus
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile
from trading.strategy.runtime import StrategyRuntimeConfig


V2_RUNTIME_MODE = "strategy_reboot_v2"
V2_ENTRY_ORDER_PATH_BLOCKED = "REBOOT_V2_OBSERVE_ORDER_PATH_DISABLED"


@dataclass
class RebootV2Runtime:
    db: Any
    subscription_manager: RealTimeSubscriptionManager
    candle_builder: CandleBuilder
    market_data: MarketDataStore
    market_index_store: MarketIndexStore
    config: StrategyRuntimeConfig
    profile: RebootV2RuntimeProfile = RebootV2RuntimeProfile.V2_OBSERVE
    candidate_ingestion_service: Any = None
    candidate_hydrator: Any = None
    opening_burst_pipeline: Any = None
    theme_board_pipeline: Any = None
    market_regime_pipeline: Any = None
    entry_engine_pipeline: Any = None
    exit_engine_reboot_pipeline: Any = None
    position_risk_pipeline: Any = None
    clock: Any = datetime.now
    startup_warnings: list[str] = field(default_factory=list)
    readiness_report: Any = None

    def __post_init__(self) -> None:
        self.started = False
        self.started_at = ""
        self.stopped_at = ""
        self.last_snapshot: dict[str, Any] = {}
        self.is_reboot_v2_runtime = True
        self.runtime_profile = self.profile.value

    def start(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        self.started = True
        self.started_at = current.isoformat()
        snapshot = self._base_snapshot(current, status="WARMUP")
        snapshot["blocking_reason"] = "WAITING_FOR_FIRST_RUNTIME_CYCLE"
        snapshot["next_required_action"] = "RUN_RUNTIME_CYCLE"
        self.last_snapshot = snapshot
        return dict(snapshot)

    def stop(self) -> dict[str, Any]:
        self.started = False
        self.stopped_at = self.clock().replace(microsecond=0).isoformat()
        snapshot = dict(self.last_snapshot or {})
        snapshot["started"] = False
        snapshot["status"] = "NOT_STARTED"
        snapshot["stopped_at"] = self.stopped_at
        return snapshot

    def cycle(self, now: datetime | None = None) -> dict[str, Any]:
        started = perf_counter()
        current = (now or self.clock()).replace(microsecond=0)
        if not self.started:
            self.started = True
            self.started_at = current.isoformat()
        snapshot = self._base_snapshot(current, status="WARMUP")
        snapshot["steps"] = []

        self._recover_hydration(snapshot)
        self._reconcile_base_subscriptions(snapshot)
        self._run_pipeline(snapshot, "opening_burst", self.opening_burst_pipeline, current)
        self._enqueue_hydration(snapshot, current)
        self._reconcile_candidate_subscriptions(snapshot, current)
        self._run_pipeline(snapshot, "theme_board", self.theme_board_pipeline, current)
        self._run_pipeline(snapshot, "market_regime", self.market_regime_pipeline, current)
        self._run_pipeline(snapshot, "entry_engine", self.entry_engine_pipeline, current)
        if self._has_open_positions():
            self._run_pipeline(snapshot, "exit_engine_reboot", self.exit_engine_reboot_pipeline, current)
            self._run_pipeline(snapshot, "position_risk", self.position_risk_pipeline, current)
        else:
            snapshot["exit_engine_reboot"] = _disabled_section("NO_OPEN_POSITIONS")
            snapshot["position_risk"] = _disabled_section("NO_OPEN_POSITIONS")

        self._attach_counts(snapshot, current)
        snapshot["order_manager"] = _disabled_section("ORDER_MANAGER_DISABLED_IN_V2_OBSERVE")
        snapshot["legacy_runtime"] = {
            "enabled": False,
            "gate_pipeline_count": 0,
            "entry_plan_count": 0,
            "virtual_buy_order_count": 0,
            "dry_run_intent_count": 0,
        }
        snapshot["status"] = self._overall_status(snapshot)
        snapshot["cycle_duration_ms"] = int(round((perf_counter() - started) * 1000))
        self.last_snapshot = snapshot
        return dict(snapshot)

    def _base_snapshot(self, now: datetime, *, status: str) -> dict[str, Any]:
        return {
            "runtime": V2_RUNTIME_MODE,
            "runtime_profile": self.profile.value,
            "reboot_v2_enabled": True,
            "started": self.started,
            "started_at": self.started_at,
            "cycle_at": now.isoformat(),
            "status": status,
            "order_path_enabled": False,
            "send_order_allowed": False,
            "index_watch_codes_configured": bool(dict(self.config.index_watch_codes or {}).get("KOSPI"))
            and bool(dict(self.config.index_watch_codes or {}).get("KOSDAQ")),
            "data_warmup_status": "warmup" if status in {"NOT_STARTED", "WARMUP"} else "ready",
            "pipeline_status": {
                "candidate_ingestion": _component_enabled(self.candidate_ingestion_service),
                "candidate_hydrator": _component_enabled(self.candidate_hydrator),
                "opening_burst": _component_enabled(self.opening_burst_pipeline),
                "theme_board": _component_enabled(self.theme_board_pipeline),
                "market_regime": _component_enabled(self.market_regime_pipeline),
                "entry_engine": _component_enabled(self.entry_engine_pipeline),
                "exit_engine": _component_enabled(self.exit_engine_reboot_pipeline),
                "position_risk": _component_enabled(self.position_risk_pipeline),
                "order_manager": False,
                "legacy_entry_path": False,
            },
            "warnings": list(self.startup_warnings or []),
        }

    def _recover_hydration(self, snapshot: dict[str, Any]) -> None:
        hydrator = self.candidate_hydrator
        recover = getattr(hydrator, "recover_from_command_history", None)
        if not callable(recover):
            snapshot["candidate_hydration_recovery"] = {"status": "DISABLED"}
            return
        try:
            snapshot["candidate_hydration_recovery"] = dict(recover() or {})
        except Exception as exc:
            snapshot["candidate_hydration_recovery"] = {"status": "ERROR", "error": str(exc)}
            snapshot.setdefault("warnings", []).append(f"CANDIDATE_HYDRATION_RECOVERY_FAILED:{exc}")

    def _reconcile_base_subscriptions(self, snapshot: dict[str, Any]) -> None:
        manager = self.subscription_manager
        for raw_index_code in dict(self.config.index_watch_codes or {}).values():
            if raw_index_code:
                manager.ensure_subscription(raw_index_code, "reboot_v2_index", protected=True)
        for position in self._open_positions():
            code = normalize_code(str(getattr(position, "code", "") or getattr(position, "stock_code", "") or ""))
            if code:
                manager.ensure_subscription(code, "reboot_v2_position", protected=True)
        for order in self._pending_orders():
            code = normalize_code(str(getattr(order, "code", "") or ""))
            if code:
                manager.ensure_subscription(code, "reboot_v2_position", protected=True)
        active = manager.sync()
        snapshot["base_realtime_subscription"] = {
            "status": "OK",
            "source": "reboot_v2_index/reboot_v2_position",
            "active_count": len(active),
            "warnings": list(manager.warnings),
        }

    def _enqueue_hydration(self, snapshot: dict[str, Any], now: datetime) -> None:
        hydrator = self.candidate_hydrator
        enqueue_due = getattr(hydrator, "enqueue_due_candidates", None)
        if not callable(enqueue_due):
            snapshot["candidate_hydration"] = {"status": "DISABLED", "enabled": False}
            return
        results = list(enqueue_due(trade_date=now.date().isoformat()) or [])
        snapshot["candidate_hydration"] = {
            "status": "OK",
            "enabled": True,
            "enqueued_count": sum(1 for item in results if bool(getattr(item, "enqueued", False))),
            "duplicate_count": sum(1 for item in results if bool(getattr(item, "duplicate", False))),
            "result_count": len(results),
        }

    def _reconcile_candidate_subscriptions(self, snapshot: dict[str, Any], now: datetime) -> None:
        manager = self.subscription_manager
        trade_date = now.date().isoformat()
        candidates = list(self.db.list_candidates(trade_date=trade_date) or [])
        selected: dict[str, str] = {}
        for code in self._opening_seed_codes(trade_date)[: max(0, int(self.config.max_candidates_to_watch or 0))]:
            selected[code] = "reboot_v2_opening_seed"
        for candidate in candidates:
            if candidate.state in {CandidateState.WATCHING, CandidateState.WAIT_DATA, CandidateState.HYDRATING}:
                selected.setdefault(normalize_code(candidate.code), "reboot_v2_candidate")
        for code in self._theme_board_codes(trade_date):
            selected.setdefault(code, "reboot_v2_theme_board")

        active_v2_sources = {"reboot_v2_opening_seed", "reboot_v2_candidate", "reboot_v2_theme_board"}
        for code, record in list(manager.records.items()):
            for source in list(record.sources):
                if source in active_v2_sources and selected.get(code) != source:
                    manager.remove_subscription(code, source)
        for code, source in selected.items():
            if code:
                manager.ensure_subscription(code, source)
        active = manager.sync()
        snapshot["candidate_realtime_subscription"] = {
            "status": "OK",
            "sources": {source: sum(1 for value in selected.values() if value == source) for source in sorted(set(selected.values()))},
            "selected_count": len(selected),
            "active_count": len([code for code in selected if code in active]),
            "warnings": list(manager.warnings),
        }

    def _run_pipeline(self, snapshot: dict[str, Any], name: str, pipeline: Any, now: datetime) -> None:
        if pipeline is None:
            snapshot[name] = _disabled_section("CONFIG_DISABLED")
            return
        runner = getattr(pipeline, "run_if_due", None) or getattr(pipeline, "run", None)
        if not callable(runner):
            snapshot[name] = _disabled_section("NO_RUNNER")
            return
        try:
            snapshot[name] = dict(runner(now) or {})
        except Exception as exc:
            snapshot[name] = {"enabled": True, "status": "ERROR", "blocking_reason": str(exc), "next_required_action": "CHECK_RUNTIME_LOG"}
            snapshot.setdefault("warnings", []).append(f"{name.upper()}_FAILED:{exc}")

    def _attach_counts(self, snapshot: dict[str, Any], now: datetime) -> None:
        candidates = list(self.db.list_candidates(trade_date=now.date().isoformat()) or [])
        snapshot["candidate_count"] = len(candidates)
        snapshot["active_candidate_count"] = sum(1 for item in candidates if item.state not in {CandidateState.REMOVED, CandidateState.EXPIRED})
        snapshot["watching_candidate_count"] = sum(1 for item in candidates if item.state == CandidateState.WATCHING)
        snapshot["wait_data_candidate_count"] = sum(1 for item in candidates if item.state == CandidateState.WAIT_DATA)
        snapshot["subscription_active_count"] = len(self.subscription_manager.code_to_screen)
        snapshot["open_position_count"] = len(self._open_positions())
        snapshot["entry_plan_count"] = 0
        snapshot["virtual_order_count"] = 0
        snapshot["filled_order_count"] = 0
        snapshot["exit_decision_count"] = 0
        snapshot["review_count"] = 0
        snapshot["db_write_count_per_cycle"] = 0

    def _overall_status(self, snapshot: Mapping[str, Any]) -> str:
        if not self.started:
            return "NOT_STARTED"
        statuses = [
            str(dict(snapshot.get(name) or {}).get("status") or "").upper()
            for name in ("opening_burst", "theme_board", "market_regime", "entry_engine")
        ]
        if any(status == "ERROR" for status in statuses):
            return "ERROR"
        if int(snapshot.get("watching_candidate_count") or 0) > 0:
            return "READY"
        if int(snapshot.get("candidate_count") or 0) <= 0:
            return "EMPTY"
        if any(status in {"DATA_WAIT", "WAITING_FOR_SEED"} for status in statuses):
            return "DATA_WAIT"
        return "WARMUP"

    def _opening_seed_codes(self, trade_date: str) -> list[str]:
        batch_loader = getattr(self.db, "list_opening_turnover_seed_batches", None)
        row_loader = getattr(self.db, "list_opening_turnover_seed_rows", None)
        if not callable(batch_loader) or not callable(row_loader):
            return []
        codes: list[str] = []
        for batch in list(batch_loader(trade_date=trade_date, limit=5) or []):
            batch_id = int(dict(batch or {}).get("id") or 0)
            for row in list(row_loader(batch_id=batch_id, limit=100) or []):
                code = normalize_code(str(dict(row or {}).get("stock_code") or dict(row or {}).get("code") or ""))
                if code and code not in codes:
                    codes.append(code)
        return codes

    def _theme_board_codes(self, trade_date: str) -> list[str]:
        loader = getattr(self.db, "latest_theme_board_snapshot", None)
        if not callable(loader):
            return []
        snapshot = dict(loader(trade_date=trade_date) or {})
        stocks = list(snapshot.get("stocks") or snapshot.get("stocks_json") or [])
        codes: list[str] = []
        for item in stocks:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("stock_role") or "").upper()
            if role not in {"LEADER", "CO_LEADER"}:
                continue
            code = normalize_code(str(item.get("code") or ""))
            if code and code not in codes:
                codes.append(code)
        return codes

    def _open_positions(self) -> list[Any]:
        loader = getattr(self.db, "list_open_virtual_positions", None)
        if not callable(loader):
            return []
        return list(loader() or [])

    def _pending_orders(self) -> list[Any]:
        loader = getattr(self.db, "list_virtual_orders_by_status", None)
        if not callable(loader):
            return []
        return list(loader(VirtualOrderStatus.SUBMITTED) or [])

    def _has_open_positions(self) -> bool:
        return bool(self._open_positions())


def _component_enabled(component: Any) -> bool:
    if component is None:
        return False
    config = getattr(component, "config", None)
    if config is not None and hasattr(config, "enabled"):
        return bool(getattr(config, "enabled"))
    return True


def _disabled_section(reason: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "blocking_reason": reason,
        "next_required_action": "ENABLE_CONFIG" if reason == "CONFIG_DISABLED" else "NONE",
        "live_order_allowed": False,
        "dry_run_order_allowed": False,
    }


__all__ = ["RebootV2Runtime", "V2_RUNTIME_MODE", "V2_ENTRY_ORDER_PATH_BLOCKED"]
