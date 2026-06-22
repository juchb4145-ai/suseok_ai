from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder
from trading.strategy.candidate_fsm import build_candidate_fsm_summary
from trading.strategy.candidate_state_contract import CandidateStateContractReconciler
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import Candidate, CandidateState, VirtualOrderStatus
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile
from trading.strategy.runtime import StrategyRuntimeConfig
from trading.strategy.context_dirty_publisher import MarketRegimeDirtyPublisher, StrategyContextDirtyPublisher, ThemeStateDirtyPublisher
from trading.strategy.market_data_service import DirtyReason
from trading.theme_engine.expansion_lease import ExpansionLeaseManager


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
    intraday_discovery_pipeline: Any = None
    theme_board_pipeline: Any = None
    market_regime_pipeline: Any = None
    strategy_context_pipeline: Any = None
    entry_engine_pipeline: Any = None
    dirty_strategy_evaluator: Any = None
    setup_router_v3_pipeline: Any = None
    candidate_state_contract_reconciler: Any = None
    market_dirty_publisher: Any = None
    theme_dirty_publisher: Any = None
    strategy_context_dirty_publisher: Any = None
    expansion_lease_manager: Any = None
    market_relative_strength_shadow_pipeline: Any = None
    market_relative_strength_outcome_pipeline: Any = None
    exit_engine_reboot_pipeline: Any = None
    position_risk_pipeline: Any = None
    order_manager_pipeline: Any = None
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
        self.market_dirty_publisher = self.market_dirty_publisher or MarketRegimeDirtyPublisher()
        self.theme_dirty_publisher = self.theme_dirty_publisher or ThemeStateDirtyPublisher()
        self.strategy_context_dirty_publisher = self.strategy_context_dirty_publisher or StrategyContextDirtyPublisher()
        self.expansion_lease_manager = self.expansion_lease_manager or ExpansionLeaseManager()
        self.candidate_state_contract_reconciler = self.candidate_state_contract_reconciler or CandidateStateContractReconciler(self.db, clock=self.clock)
        self._expansion_lease_restored_trade_date = ""

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
        self._recover_intraday_discovery(snapshot)
        self._reconcile_base_subscriptions(snapshot)
        self._run_pipeline(snapshot, "market_regime", self.market_regime_pipeline, current)
        downstream_market_context = self._downstream_market_context(snapshot, current)
        self._publish_market_dirty(snapshot, current)
        self._run_pipeline(snapshot, "opening_burst", self.opening_burst_pipeline, current)
        self._run_pipeline(snapshot, "intraday_discovery", self.intraday_discovery_pipeline, current)
        self._run_pipeline(snapshot, "theme_board", self.theme_board_pipeline, current, market_context=downstream_market_context)
        self._publish_theme_dirty(snapshot, current)
        self._reconcile_theme_expansion_subscriptions(snapshot, current)
        self._enqueue_hydration(snapshot, current)
        self._reconcile_candidate_state_contract(snapshot, current)
        self._reconcile_candidate_subscriptions(snapshot, current)
        self._run_pipeline(
            snapshot,
            "strategy_context",
            self.strategy_context_pipeline,
            current,
            market_context=downstream_market_context,
            theme_board=getattr(self.theme_board_pipeline, "last_result", None) or self._latest_theme_board(current.date().isoformat()),
        )
        self._publish_strategy_context_dirty(snapshot, current)
        self._run_pipeline(snapshot, "dirty_evaluator", self.dirty_strategy_evaluator, current)
        if _component_enabled(self.dirty_strategy_evaluator):
            dashboard_section = getattr(self.dirty_strategy_evaluator, "dashboard_section", None)
            snapshot["entry_engine"] = dict(dashboard_section() if callable(dashboard_section) else {})
            snapshot["entry_engine"].setdefault("enabled", True)
            snapshot["entry_engine"].setdefault("status", "DIRTY_EVALUATOR_ACTIVE")
            snapshot["entry_engine"]["fallback_full_scan_available"] = _component_enabled(self.entry_engine_pipeline)
            snapshot["entry_engine"]["live_order_allowed"] = False
            snapshot["entry_engine"]["dry_run_order_allowed"] = False
        else:
            self._run_pipeline(snapshot, "entry_engine", self.entry_engine_pipeline, current)
        self._run_pipeline(snapshot, "setup_router_v3", self.setup_router_v3_pipeline, current)
        self._run_pipeline(snapshot, "market_relative_strength_shadow", self.market_relative_strength_shadow_pipeline, current)
        self._run_pipeline(snapshot, "market_relative_strength_outcomes", self.market_relative_strength_outcome_pipeline, current)
        if self._has_open_positions(current.date().isoformat()):
            self._run_pipeline(snapshot, "position_risk", self.position_risk_pipeline, current)
            self._run_pipeline(snapshot, "exit_engine_reboot", self.exit_engine_reboot_pipeline, current)
        else:
            snapshot["exit_engine_reboot"] = _disabled_section("NO_OPEN_POSITIONS")
            snapshot["position_risk"] = _disabled_section("NO_OPEN_POSITIONS")
        self._run_pipeline(snapshot, "order_manager_v2", self.order_manager_pipeline, current)

        self._attach_counts(snapshot, current)
        snapshot["order_manager"] = snapshot.get("order_manager_v2") or _disabled_section("ORDER_MANAGER_DISABLED_IN_V2_OBSERVE")
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
                "intraday_discovery": _component_enabled(self.intraday_discovery_pipeline),
                "theme_board": _component_enabled(self.theme_board_pipeline),
                "market_regime": _component_enabled(self.market_regime_pipeline),
                "strategy_context": _component_enabled(self.strategy_context_pipeline),
                "entry_engine": _component_enabled(self.entry_engine_pipeline),
                "dirty_strategy_evaluator": _component_enabled(self.dirty_strategy_evaluator),
                "setup_router_v3": _component_enabled(self.setup_router_v3_pipeline),
                "market_relative_strength_shadow": _component_enabled(self.market_relative_strength_shadow_pipeline),
                "market_relative_strength_outcomes": _component_enabled(self.market_relative_strength_outcome_pipeline),
                "exit_engine": _component_enabled(self.exit_engine_reboot_pipeline),
                "position_risk": _component_enabled(self.position_risk_pipeline),
                "order_manager": _component_enabled(self.order_manager_pipeline),
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

    def _recover_intraday_discovery(self, snapshot: dict[str, Any]) -> None:
        pipeline = self.intraday_discovery_pipeline
        recover = getattr(pipeline, "recover_from_command_history", None)
        if not callable(recover):
            snapshot["intraday_discovery_recovery"] = {"status": "DISABLED"}
            return
        try:
            snapshot["intraday_discovery_recovery"] = dict(recover() or {})
        except Exception as exc:
            snapshot["intraday_discovery_recovery"] = {"status": "ERROR", "error": str(exc)}
            snapshot.setdefault("warnings", []).append(f"INTRADAY_DISCOVERY_RECOVERY_FAILED:{exc}")

    def _reconcile_candidate_state_contract(self, snapshot: dict[str, Any], now: datetime) -> None:
        reconciler = self.candidate_state_contract_reconciler
        if reconciler is None:
            snapshot["candidate_state_contract"] = {"status": "DISABLED"}
            return
        try:
            snapshot["candidate_state_contract"] = dict(reconciler.reconcile_trade_date(now.date().isoformat()) or {})
        except Exception as exc:
            snapshot["candidate_state_contract"] = {"status": "ERROR", "error": str(exc)}
            snapshot.setdefault("warnings", []).append(f"CANDIDATE_STATE_CONTRACT_RECONCILE_FAILED:{exc}")

    def _publish_market_dirty(self, snapshot: dict[str, Any], now: datetime) -> None:
        publisher = self.market_dirty_publisher
        queue = self._dirty_queue()
        if publisher is None or queue is None:
            snapshot["market_dirty_publish"] = _disabled_section("DIRTY_QUEUE_UNAVAILABLE")
            return
        result = publisher.publish(
            dict(snapshot.get("market_regime") or {}),
            dirty_queue=queue,
            codes_by_side=self._candidate_codes_by_side(now),
            now=now,
        )
        snapshot["market_dirty_publish"] = _dirty_publish_payload(result)

    def _publish_theme_dirty(self, snapshot: dict[str, Any], now: datetime) -> None:
        publisher = self.theme_dirty_publisher
        queue = self._dirty_queue()
        if publisher is None or queue is None:
            snapshot["theme_dirty_publish"] = _disabled_section("DIRTY_QUEUE_UNAVAILABLE")
            return
        board = getattr(self.theme_board_pipeline, "last_result", None) or self._latest_theme_board(now.date().isoformat())
        theme_states = list(getattr(self.theme_board_pipeline, "last_theme_states", []) or [])
        stock_roles = list(getattr(self.theme_board_pipeline, "last_role_decisions", []) or [])
        result = publisher.publish(
            theme_states or list(dict(board or {}).get("top_themes") or []),
            dirty_queue=queue,
            code_by_theme=self._theme_codes_by_theme_from_roles(stock_roles) if stock_roles else self._theme_codes_by_theme(board),
            stock_roles=stock_roles or list(dict(board or {}).get("stocks") or []),
            now=now,
        )
        snapshot["theme_dirty_publish"] = _dirty_publish_payload(result)

    def _publish_strategy_context_dirty(self, snapshot: dict[str, Any], now: datetime) -> None:
        publisher = self.strategy_context_dirty_publisher
        queue = self._dirty_queue()
        if publisher is None or queue is None:
            snapshot["strategy_context_dirty_publish"] = _disabled_section("DIRTY_QUEUE_UNAVAILABLE")
            return
        contexts = list(getattr(self.strategy_context_pipeline, "last_result", []) or [])
        result = publisher.publish(contexts, dirty_queue=queue, now=now)
        snapshot["strategy_context_dirty_publish"] = _dirty_publish_payload(result)

    def _dirty_queue(self) -> Any | None:
        evaluator = getattr(self.dirty_strategy_evaluator, "evaluator", None)
        service = getattr(evaluator, "market_data_service", None)
        return getattr(service, "dirty_queue", None)

    def _candidate_codes_by_side(self, now: datetime) -> dict[str, list[str]]:
        codes = [normalize_code(candidate.code) for candidate in self.db.list_candidates(trade_date=now.date().isoformat()) if normalize_code(candidate.code)]
        by_side = {"KOSPI": [], "KOSDAQ": [], "UNKNOWN": []}
        for code in codes:
            tick = self.market_data.latest_tick(code)
            side = str(dict(getattr(tick, "metadata", {}) or {}).get("market") or "UNKNOWN").upper() if tick is not None else "UNKNOWN"
            if side not in by_side:
                side = "UNKNOWN"
            by_side[side].append(code)
            by_side["UNKNOWN"].append(code)
        return by_side

    def _theme_codes_by_theme(self, board: Mapping[str, Any] | None) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for stock in list(dict(board or {}).get("stocks") or []):
            if not isinstance(stock, Mapping):
                continue
            theme_id = str(stock.get("theme_id") or "")
            code = normalize_code(str(stock.get("code") or ""))
            if theme_id and code:
                result.setdefault(theme_id, []).append(code)
        return result

    def _theme_codes_by_theme_from_roles(self, roles: Iterable[Any]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for role in list(roles or []):
            theme_id = str(getattr(role, "theme_id", "") or "")
            code = normalize_code(str(getattr(role, "code", "") or ""))
            if theme_id and code:
                result.setdefault(theme_id, []).append(code)
        return result

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
        snapshot["base_realtime_subscription"] = {
            "status": "OK",
            "source": "reboot_v2_index/reboot_v2_position",
            "active_count": len(
                [
                    code
                    for code, record in manager.records.items()
                    if code in manager.code_to_screen
                    and ("reboot_v2_index" in record.sources or "reboot_v2_position" in record.sources)
                ]
            ),
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
        base_section = dict(snapshot.get("base_realtime_subscription") or {})
        if base_section:
            base_section["active_count"] = len(
                [
                    code
                    for code, record in manager.records.items()
                    if code in active
                    and ("reboot_v2_index" in record.sources or "reboot_v2_position" in record.sources)
                ]
            )
            base_section["warnings"] = list(manager.warnings)
            snapshot["base_realtime_subscription"] = base_section
        snapshot["candidate_realtime_subscription"] = {
            "status": "OK",
            "sources": {source: sum(1 for value in selected.values() if value == source) for source in sorted(set(selected.values()))},
            "selected_count": len(selected),
            "active_count": len([code for code in selected if code in active]),
            "warnings": list(manager.warnings),
        }

    def _reconcile_theme_expansion_subscriptions(self, snapshot: dict[str, Any], now: datetime) -> None:
        plan = getattr(self.theme_board_pipeline, "last_expansion_plan", None)
        config = getattr(getattr(self.theme_board_pipeline, "config", None), "theme_expansion_subscriptions_enabled", False)
        source = str(getattr(plan, "source", "reboot_v2_theme_expansion") or "reboot_v2_theme_expansion")
        if not plan or not bool(config):
            snapshot["theme_expansion_subscription"] = _disabled_section("THEME_EXPANSION_SUBSCRIPTIONS_DISABLED")
            return
        targets = list(getattr(plan, "targets", ()) or ())
        manager = self.subscription_manager
        self._restore_expansion_leases(now)
        protected_codes = self._protected_codes()
        fresh_tick_events = self._fresh_tick_events(now)
        lease_snapshot = self.expansion_lease_manager.reconcile(
            targets,
            now=now,
            active_codes=list(manager.code_to_screen.keys()),
            fresh_tick_events=fresh_tick_events,
            protected_codes=protected_codes,
        )
        self._persist_expansion_leases(lease_snapshot, trade_date=now.date().isoformat())
        self._publish_expansion_tick_ready(lease_snapshot, now)
        retained_leases = list(getattr(self.expansion_lease_manager, "retained_leases", lambda: ())() or ())
        target_codes = {normalize_code(getattr(lease, "code", "")): lease for lease in retained_leases if normalize_code(getattr(lease, "code", ""))}
        for code, record in list(manager.records.items()):
            if source in record.sources and code not in target_codes:
                manager.remove_subscription(code, source)
                self._save_expansion_subscription_decision(now, code=code, target=None, action="REMOVE", status="STALE")
        for code, target in target_codes.items():
            manager.ensure_subscription(code, source, protected=code in protected_codes or str(getattr(target, "status", "")) == "PROTECTED")
            self._save_expansion_subscription_decision(now, code=code, target=target, action="ENSURE", status="SELECTED")
        active = manager.sync()
        by_theme: dict[str, int] = {}
        waiting = 0
        for code, target in target_codes.items():
            theme_id = str(getattr(target, "theme_id", "") or "")
            by_theme[theme_id] = by_theme.get(theme_id, 0) + 1
            if code not in active:
                waiting += 1
        rejected = list(getattr(plan, "excluded", ()) or [])
        reason_counts: dict[str, int] = {}
        for target in rejected:
            for reason in tuple(getattr(target, "reason_codes", ()) or ()):
                reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
        snapshot["theme_expansion_subscription"] = {
            "enabled": True,
            "status": "OK",
            "source": source,
            "selected_count": len(target_codes),
            "active_count": len([code for code in target_codes if code in active]),
            "rejected_count": len(rejected),
            "by_theme": by_theme,
            "budget_used": len(target_codes),
            "waiting_for_tick_count": waiting,
            "top_reject_reasons": [{"reason": key, "count": count} for key, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:10]],
            "lease_snapshot": _expansion_lease_payload(lease_snapshot),
            "warnings": list(manager.warnings),
        }

    def _save_expansion_subscription_decision(self, now: datetime, *, code: str, target: Any | None, action: str, status: str) -> None:
        saver = getattr(self.db, "save_theme_expansion_subscription_decision", None)
        if not callable(saver):
            return
        saver(
            {
                "trade_date": now.date().isoformat(),
                "calculated_at": now.isoformat(),
                "code": code,
                "theme_id": str(getattr(target, "theme_id", "") or "") if target is not None else "",
                "source": str(getattr(target, "source", "reboot_v2_theme_expansion") or "reboot_v2_theme_expansion") if target is not None else "reboot_v2_theme_expansion",
                "action": action,
                "status": status,
                "reason_codes": list(getattr(target, "reason_codes", ()) or ()) if target is not None else ["STALE_EXPANSION_TARGET"],
                "target": getattr(target, "__dict__", {}) if target is not None else {},
            }
        )

    def _restore_expansion_leases(self, now: datetime) -> None:
        trade_date = now.date().isoformat()
        if self._expansion_lease_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_theme_expansion_leases", None)
        restore = getattr(self.expansion_lease_manager, "restore", None)
        if callable(loader) and callable(restore):
            restore(list(loader(trade_date=trade_date, active_only=True) or []))
        self._expansion_lease_restored_trade_date = trade_date

    def _persist_expansion_leases(self, lease_snapshot: Any, *, trade_date: str) -> int:
        saver = getattr(self.db, "save_theme_expansion_leases", None)
        if not callable(saver):
            return 0
        rows = []
        for lease in list(getattr(lease_snapshot, "leases", ()) or ()):
            payload = dict(getattr(lease, "__dict__", {}) or {})
            target = payload.get("target")
            payload["target"] = dict(getattr(target, "__dict__", {}) or {}) if target is not None else {}
            payload["reason_codes"] = list(getattr(lease, "reason_codes", ()) or ())
            rows.append(payload)
        return int(saver(rows, trade_date=trade_date) or 0)

    def _publish_expansion_tick_ready(self, lease_snapshot: Any, now: datetime) -> None:
        queue = self._dirty_queue()
        mark = getattr(queue, "mark_dirty", None)
        if not callable(mark):
            return
        for lease in list(getattr(lease_snapshot, "leases", ()) or ()):
            if "THEME_EXPANSION_TICK_READY" in set(getattr(lease, "reason_codes", ()) or ()) and str(getattr(lease, "first_fresh_tick_at", "") or "") == now.isoformat():
                mark(
                    getattr(lease, "code", ""),
                    DirtyReason.THEME_EXPANSION_TICK_READY.value,
                    source_event_id=str(getattr(lease, "first_tick_source_event_id", "") or ""),
                    marked_at=now,
                )

    def _protected_codes(self) -> set[str]:
        codes: set[str] = set()
        for position in self._open_positions():
            code = normalize_code(str(getattr(position, "code", "") or getattr(position, "stock_code", "") or ""))
            if code:
                codes.add(code)
        for order in self._pending_orders():
            code = normalize_code(str(getattr(order, "code", "") or getattr(order, "stock_code", "") or ""))
            if code:
                codes.add(code)
        return codes

    def _fresh_tick_events(self, now: datetime) -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        max_tick_age_sec = _market_data_max_tick_age_sec()
        for code in list(getattr(self.subscription_manager, "code_to_screen", {}) or {}):
            tick = self.market_data.latest_tick(code)
            if tick is None or getattr(tick, "price", 0) <= 0:
                continue
            tick_at = getattr(tick, "timestamp", None)
            if tick_at is None:
                continue
            age_sec = max(0.0, (now - tick_at.replace(tzinfo=None)).total_seconds())
            if age_sec > max_tick_age_sec:
                continue
            metadata = dict(getattr(tick, "metadata", {}) or {})
            if str(metadata.get("price_source") or "REALTIME").upper() == "TR_BACKFILL":
                continue
            clean_code = normalize_code(code)
            if clean_code:
                events[clean_code] = {
                    "code": clean_code,
                    "tick_at": tick_at.replace(tzinfo=None).isoformat(),
                    "age_sec": round(age_sec, 3),
                    "price_source": str(metadata.get("price_source") or "REALTIME"),
                    "source_event_id": str(metadata.get("source_event_id") or metadata.get("event_id") or ""),
                }
        return events

    def _run_pipeline(self, snapshot: dict[str, Any], name: str, pipeline: Any, now: datetime, **kwargs: Any) -> None:
        if pipeline is None:
            snapshot[name] = _disabled_section("CONFIG_DISABLED")
            return
        runner = getattr(pipeline, "run_if_due", None) or getattr(pipeline, "run", None)
        if not callable(runner):
            snapshot[name] = _disabled_section("NO_RUNNER")
            return
        try:
            snapshot[name] = dict(runner(now, **kwargs) or {})
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
        snapshot["open_position_count"] = len(self._open_positions(now.date().isoformat()))
        snapshot["entry_plan_count"] = 0
        snapshot["virtual_order_count"] = 0
        snapshot["filled_order_count"] = 0
        snapshot["exit_decision_count"] = 0
        snapshot["review_count"] = 0
        snapshot["db_write_count_per_cycle"] = 0
        snapshot["candidate_fsm"] = build_candidate_fsm_summary(self.db, trade_date=now.date().isoformat())

    def _overall_status(self, snapshot: Mapping[str, Any]) -> str:
        if not self.started:
            return "NOT_STARTED"
        statuses = [
            str(dict(snapshot.get(name) or {}).get("status") or "").upper()
            for name in ("opening_burst", "theme_board", "market_regime", "strategy_context", "dirty_evaluator", "entry_engine", "setup_router_v3")
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
        snapshot = self._latest_theme_board(trade_date)
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

    def _latest_theme_board(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_theme_board_snapshot", None)
        if not callable(loader):
            return {}
        return dict(loader(trade_date=trade_date) or {})

    def _latest_market_regime(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_market_regime_snapshot", None)
        if not callable(loader):
            return {}
        return dict(loader(trade_date=trade_date) or {})

    def _downstream_market_context(self, snapshot: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        for payload in (
            _pipeline_market_snapshot(self.market_regime_pipeline),
            self._latest_market_regime(now.date().isoformat()),
            dict(snapshot.get("market_regime") or {}),
        ):
            if _market_context_payload_usable(payload):
                return dict(payload)
        return {}

    def _open_positions(self, trade_date: str | None = None) -> list[Any]:
        loader = getattr(self.db, "list_open_virtual_positions", None)
        if not callable(loader):
            return []
        positions = list(loader() or [])
        if not trade_date:
            return positions
        return [position for position in positions if self._position_matches_trade_date(position, trade_date)]

    def _pending_orders(self) -> list[Any]:
        loader = getattr(self.db, "list_virtual_orders_by_status", None)
        if not callable(loader):
            return []
        return list(loader(VirtualOrderStatus.SUBMITTED) or [])

    def _has_open_positions(self, trade_date: str | None = None) -> bool:
        return bool(self._open_positions(trade_date))

    def _position_matches_trade_date(self, position: Any, trade_date: str) -> bool:
        candidate_id = getattr(position, "candidate_id", None)
        if candidate_id in (None, ""):
            return True
        loader = getattr(self.db, "load_candidate_by_id", None)
        if not callable(loader):
            return True
        try:
            candidate = loader(int(candidate_id))
        except Exception:
            return True
        if candidate is None:
            return True
        candidate_trade_date = str(getattr(candidate, "trade_date", "") or "")
        return candidate_trade_date in {"", str(trade_date or "")}


def _component_enabled(component: Any) -> bool:
    if component is None:
        return False
    config = getattr(component, "config", None)
    if config is not None and hasattr(config, "enabled"):
        return bool(getattr(config, "enabled"))
    return True


def _pipeline_market_snapshot(pipeline: Any) -> dict[str, Any]:
    result = getattr(pipeline, "last_result", None)
    snapshot = getattr(result, "snapshot", None)
    if snapshot is None and isinstance(result, Mapping):
        snapshot = result.get("snapshot")
    if hasattr(snapshot, "to_dict"):
        return dict(snapshot.to_dict() or {})
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    return {}


def _market_context_payload_usable(payload: Mapping[str, Any] | None) -> bool:
    if not payload:
        return False
    if payload.get("candidate_policy_by_code") or payload.get("kospi_snapshot") or payload.get("kosdaq_snapshot"):
        return True
    return bool(payload.get("calculated_at") and (payload.get("kospi_status") or payload.get("kosdaq_status") or payload.get("global_status")))


def _market_data_max_tick_age_sec() -> int:
    try:
        return max(1, int(str(os.getenv("TRADING_MARKET_DATA_MAX_TICK_AGE_SEC", "10")).strip()))
    except (TypeError, ValueError):
        return 10


def _disabled_section(reason: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "DISABLED",
        "blocking_reason": reason,
        "next_required_action": "ENABLE_CONFIG" if reason == "CONFIG_DISABLED" else "NONE",
        "live_order_allowed": False,
        "dry_run_order_allowed": False,
    }


def _dirty_publish_payload(result: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": "OK",
        "published_count": int(getattr(result, "published_count", 0) or 0),
        "skipped_count": int(getattr(result, "skipped_count", 0) or 0),
        "reason": str(getattr(result, "reason", "") or ""),
        "codes": list(getattr(result, "codes", ()) or ()),
        "live_order_allowed": False,
        "dry_run_order_allowed": False,
    }


def _expansion_lease_payload(snapshot: Any) -> dict[str, Any]:
    leases = []
    for lease in list(getattr(snapshot, "leases", ()) or ()):
        leases.append(
            {
                "code": getattr(lease, "code", ""),
                "theme_id": getattr(lease, "theme_id", ""),
                "source": getattr(lease, "source", ""),
                "subscription_generation": int(getattr(lease, "subscription_generation", 1) or 1),
                "status": getattr(lease, "status", ""),
                "selected_at": getattr(lease, "selected_at", ""),
                "selected_tick_baseline_at": getattr(lease, "selected_tick_baseline_at", ""),
                "first_active_at": getattr(lease, "first_active_at", ""),
                "first_fresh_tick_at": getattr(lease, "first_fresh_tick_at", ""),
                "first_post_subscription_tick_at": getattr(lease, "first_post_subscription_tick_at", ""),
                "first_tick_source_event_id": getattr(lease, "first_tick_source_event_id", ""),
                "minimum_hold_until": getattr(lease, "minimum_hold_until", ""),
                "expires_at": getattr(lease, "expires_at", ""),
                "cooldown_until": getattr(lease, "cooldown_until", ""),
                "reason_codes": list(getattr(lease, "reason_codes", ()) or ()),
            }
        )
    return {
        "calculated_at": str(getattr(snapshot, "calculated_at", "") or ""),
        "active_lease_count": int(getattr(snapshot, "active_lease_count", 0) or 0),
        "holding_count": int(getattr(snapshot, "holding_count", 0) or 0),
        "protected_count": int(getattr(snapshot, "protected_count", 0) or 0),
        "pending_removal_count": int(getattr(snapshot, "pending_removal_count", 0) or 0),
        "expired_count": int(getattr(snapshot, "expired_count", 0) or 0),
        "churn_count": int(getattr(snapshot, "churn_count", 0) or 0),
        "first_tick_wait_count": int(getattr(snapshot, "first_tick_wait_count", 0) or 0),
        "leases_by_theme": dict(getattr(snapshot, "lease_by_theme", {}) or {}),
        "top_removal_reasons": list(getattr(snapshot, "top_removal_reasons", ()) or ()),
        "leases": leases,
    }


__all__ = ["RebootV2Runtime", "V2_RUNTIME_MODE", "V2_ENTRY_ORDER_PATH_BLOCKED"]
