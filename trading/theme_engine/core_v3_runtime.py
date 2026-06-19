from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.theme_engine.board_view import ThemeBoardView
from trading.theme_engine.candidate_bridge import CandidateBridge
from trading.theme_engine.candidate_bridge_reconciler import CandidateBridgeSourceReconciler
from trading.theme_engine.cohort import ThemeCohortEngine
from trading.theme_engine.expansion import FocusedExpansionPlanner
from trading.theme_engine.leadership_handover import LeadershipHandoverEngine, ThemeLeadershipRanker, ThemeLeadershipSnapshot
from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, StockRoleEngine
from trading.theme_engine.signal_registry import ActiveSeedRegistry, ActiveSeedSignal, ActiveSeedSource
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, apply_signal_freshness, merge_seed_signals
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateMachine, ThemeStateSnapshot
from trading.theme_engine.turnover_flow import TurnoverFlowTracker, TurnoverObservation


THEME_CORE_V3_OUTPUT_MODE = "OBSERVE"


@dataclass(frozen=True)
class ThemeCoreV3RuntimeConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: int = 5
    market_phase: str = "SELECTIVE"
    kosdaq_risk_state: str = ""
    top_theme_count: int = 5
    save_candidate_source_events: bool = True
    ingest_candidate_source_events: bool = False
    use_runtime_market_context: bool = False
    theme_expansion_subscriptions_enabled: bool = False
    signal_ttl_sec: int = 600
    max_tick_age_sec: int = 10
    turnover_flow_enabled: bool = True
    leadership_handover_enabled: bool = True
    bridge_reconcile_enabled: bool = True

    @classmethod
    def from_env(cls) -> "ThemeCoreV3RuntimeConfig":
        return cls(
            enabled=_env_bool("TRADING_THEME_CORE_V3_ENABLED", False),
            observe_only=_env_bool("TRADING_THEME_CORE_V3_OBSERVE_ONLY", True),
            interval_sec=max(1, _env_int("TRADING_THEME_CORE_V3_INTERVAL_SEC", 5)),
            market_phase=str(os.getenv("TRADING_THEME_CORE_V3_MARKET_PHASE", "SELECTIVE") or "SELECTIVE").upper(),
            kosdaq_risk_state=str(os.getenv("TRADING_THEME_CORE_V3_KOSDAQ_RISK_STATE", "") or "").upper(),
            top_theme_count=max(1, _env_int("TRADING_THEME_CORE_V3_TOP_THEME_COUNT", 5)),
            save_candidate_source_events=_env_bool("TRADING_THEME_CORE_V3_SAVE_SOURCE_EVENTS", True),
            ingest_candidate_source_events=_env_bool("TRADING_THEME_CORE_V3_INGEST_CANDIDATES", False),
            use_runtime_market_context=_env_bool("TRADING_THEME_CORE_V3_USE_RUNTIME_MARKET_CONTEXT", False),
            theme_expansion_subscriptions_enabled=_env_bool("TRADING_THEME_EXPANSION_SUBSCRIPTIONS_ENABLED", False),
            signal_ttl_sec=max(1, _env_int("TRADING_THEME_SIGNAL_TTL_SEC", 600)),
            max_tick_age_sec=max(1, _env_int("TRADING_MARKET_DATA_MAX_TICK_AGE_SEC", 10)),
            turnover_flow_enabled=_env_bool("TRADING_THEME_TURNOVER_FLOW_ENABLED", True),
            leadership_handover_enabled=_env_bool("TRADING_THEME_LEADERSHIP_HANDOVER_ENABLED", True),
            bridge_reconcile_enabled=_env_bool("TRADING_THEME_BRIDGE_RECONCILE_ENABLED", True),
        )


class ThemeCoreV3RuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        repository: ThemeEngineRepository,
        config: ThemeCoreV3RuntimeConfig | None = None,
        cohort_engine: ThemeCohortEngine | None = None,
        state_machine: ThemeStateMachine | None = None,
        role_engine: StockRoleEngine | None = None,
        expansion_planner: FocusedExpansionPlanner | None = None,
        candidate_bridge: CandidateBridge | None = None,
        board_view: ThemeBoardView | None = None,
        active_seed_registry: ActiveSeedRegistry | None = None,
        turnover_flow_tracker: TurnoverFlowTracker | None = None,
        leadership_ranker: ThemeLeadershipRanker | None = None,
        handover_engine: LeadershipHandoverEngine | None = None,
        bridge_reconciler: CandidateBridgeSourceReconciler | None = None,
        candidate_ingestion_service: Any | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.repository = repository
        self.config = config or ThemeCoreV3RuntimeConfig.from_env()
        self.cohort_engine = cohort_engine or ThemeCohortEngine()
        self.state_machine = state_machine or ThemeStateMachine()
        self.role_engine = role_engine or StockRoleEngine()
        self.expansion_planner = expansion_planner or FocusedExpansionPlanner()
        self.candidate_bridge = candidate_bridge or CandidateBridge()
        self.board_view = board_view or ThemeBoardView()
        self.active_seed_registry = active_seed_registry or ActiveSeedRegistry(ttl_sec=self.config.signal_ttl_sec)
        self.turnover_flow_tracker = turnover_flow_tracker or TurnoverFlowTracker()
        self.leadership_ranker = leadership_ranker or ThemeLeadershipRanker()
        self.handover_engine = handover_engine or LeadershipHandoverEngine()
        self.bridge_reconciler = bridge_reconciler or CandidateBridgeSourceReconciler()
        self.candidate_ingestion_service = candidate_ingestion_service
        self.clock = clock or datetime.now
        self.last_result: dict[str, Any] | None = None
        self.last_summary: dict[str, Any] = _empty_summary(enabled=self.config.enabled, observe_only=self.config.observe_only)
        self.last_expansion_plan = None
        self.last_theme_states: list[ThemeStateSnapshot] = []
        self.last_role_decisions: list[StockRoleDecision] = []
        self.last_leadership_snapshots: list[Any] = []
        self.last_theme_flows: dict[str, Any] = {}
        self.last_run_at: datetime | None = None
        self._active_registry_restored_trade_date = ""
        self._theme_state_restored_trade_date = ""
        self._turnover_flow_restored_trade_date = ""
        self._bridge_reconciler_restored_trade_date = ""
        self._leadership_restored_trade_date = ""

    def run_if_due(self, now: datetime | None = None, *, market_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = _empty_summary(enabled=False, observe_only=self.config.observe_only)
            return dict(self.last_summary)
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        return self.run(current, market_context=market_context)

    def run(self, now: datetime | None = None, *, market_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = current.date().isoformat()
        theme_inputs = _load_theme_inputs(self.repository)
        self._restore_active_seed_registry(trade_date)
        source_delta_signals = self._source_delta_signals(trade_date=trade_date, theme_inputs=theme_inputs)
        active_seed_snapshot = self._update_active_seed_registry(source_delta_signals, current, trade_date=trade_date)
        seed_signals = self._active_seed_signals(active_seed_snapshot, current)
        seed_signals = self._fresh_seed_signals(seed_signals, current)
        market_input = _theme_core_market_input(market_context, config=self.config)
        summary = _empty_summary(enabled=self.config.enabled, observe_only=self.config.observe_only)
        summary.update(
            {
                "status": "OK",
                "trade_date": trade_date,
                "calculated_at": current.isoformat(),
                "theme_input_count": len(theme_inputs),
                "seed_signal_count": len(seed_signals),
                "source_delta_signal_count": len(source_delta_signals),
                "active_seed_count": active_seed_snapshot.active_count,
                "expired_seed_count": active_seed_snapshot.expired_count,
                "market_context_status": market_input["status"],
                "market_phase": market_input["market_phase"],
                "kosdaq_risk_state": market_input["kosdaq_risk_state"],
            }
        )
        if not theme_inputs:
            summary["status"] = "DATA_WAIT"
            summary["reason_codes"] = ["THEME_MEMBERSHIP_EMPTY"]
            self.last_summary = summary
            self.last_run_at = current
            return dict(summary)
        if not seed_signals:
            summary["status"] = "DATA_WAIT"
            summary["reason_codes"] = ["SEED_SIGNAL_EMPTY"]

        self._restore_theme_state(trade_date)
        if self.config.turnover_flow_enabled:
            self._restore_turnover_flow(trade_date)
            stock_flows = self.turnover_flow_tracker.observe_signals(seed_signals, observed_at=current.isoformat())
            self._persist_turnover_observations(stock_flows.values(), trade_date=trade_date)
        cohorts = self.cohort_engine.build(theme_inputs, seed_signals)
        theme_flows = self.turnover_flow_tracker.theme_flows(cohorts, observed_at=current.isoformat()) if self.config.turnover_flow_enabled else {}
        theme_states = self.state_machine.apply(cohorts, now=current)
        self._persist_theme_states(theme_states, trade_date=trade_date, calculated_at=current.isoformat())
        self._restore_leadership_state(trade_date)
        leadership_ranks = self.leadership_ranker.rank(theme_states, flows=theme_flows, previous=self.handover_engine.previous_by_theme()) if self.config.leadership_handover_enabled else []
        leadership_snapshots, leadership_transitions = self.handover_engine.apply(leadership_ranks, now=current) if self.config.leadership_handover_enabled else ([], [])
        self._persist_leadership_state(leadership_snapshots, leadership_transitions, trade_date=trade_date, calculated_at=current.isoformat())
        role_decisions = [
            decision
            for state in theme_states
            for decision in self.role_engine.classify(
                state,
                market_phase=market_input["market_phase"],
                market_phase_by_side=market_input["market_phase_by_side"],
            )
        ]
        expansion_plan = self.expansion_planner.plan(
            theme_states,
            role_decisions,
            market_phase=market_input["market_phase"],
            kosdaq_risk_state=market_input["kosdaq_risk_state"],
        )
        self.last_expansion_plan = expansion_plan
        self._restore_bridge_source_state(trade_date)
        bridge_reconcile = self.bridge_reconciler.reconcile(role_decisions, trade_date=trade_date, detected_at=current.isoformat()) if self.config.bridge_reconcile_enabled else None
        bridge_events = [*list(getattr(bridge_reconcile, "include_events", ()) or ()), *list(getattr(bridge_reconcile, "remove_events", ()) or ())]
        bridge_saved = self._persist_bridge_events(bridge_events)
        bridge_removed = len(list(getattr(bridge_reconcile, "remove_events", ()) or ()))
        bridge_included = len(list(getattr(bridge_reconcile, "include_events", ()) or ()))
        bridge_active_state_saved = self._persist_bridge_source_state(bridge_reconcile, trade_date=trade_date, calculated_at=current.isoformat())
        view = self.board_view.build(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            theme_states=theme_states,
            role_decisions=role_decisions,
            expansion_plan=expansion_plan,
        )
        snapshot = _theme_board_snapshot(
            view.to_dict(),
            theme_states=theme_states,
            role_decisions=role_decisions,
            theme_flows=theme_flows,
            leadership_snapshots=leadership_snapshots,
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            top_theme_count=self.config.top_theme_count,
        )
        saved = self._save_snapshot(snapshot)
        summary.update(_summary_from_snapshot(snapshot))
        summary.update(
            {
                "status": summary.get("status") or "OK",
                "candidate_bridge_event_count": len(bridge_events),
                "candidate_bridge_include_count": bridge_included,
                "candidate_bridge_excluded_count": 0,
                "candidate_source_event_saved_count": bridge_saved,
                "candidate_bridge_removed_count": bridge_removed,
                "candidate_bridge_active_state_saved_count": bridge_active_state_saved,
                "candidate_bridge_reconcile": _bridge_reconcile_summary(bridge_reconcile),
                "candidate_ingestion_enabled": bool(self.config.ingest_candidate_source_events),
                "theme_expansion_subscriptions_enabled": bool(self.config.theme_expansion_subscriptions_enabled),
                "theme_expansion_selected_count": len(expansion_plan.targets),
                "theme_expansion_rejected_count": len(expansion_plan.excluded),
                "turnover_flow": _turnover_flow_summary(theme_flows),
                "leadership_handover": _leadership_summary(leadership_snapshots, leadership_transitions),
                "active_seed_registry": {
                    "active_count": active_seed_snapshot.active_count,
                    "expired_count": active_seed_snapshot.expired_count,
                    "source_counts": dict(active_seed_snapshot.source_counts or {}),
                },
                "ready_allowed": False,
                "order_intent_allowed": False,
                "output_mode": THEME_CORE_V3_OUTPUT_MODE,
                "saved": bool(saved),
            }
        )
        if not seed_signals:
            summary["status"] = "DATA_WAIT"
        if market_input["status"] == "DATA_WAIT":
            summary["status"] = "DATA_WAIT"
            summary["reason_codes"] = list(dict.fromkeys([*list(summary.get("reason_codes") or []), *market_input["reason_codes"]]))
        self.last_result = snapshot
        self.last_theme_states = list(theme_states)
        self.last_role_decisions = list(role_decisions)
        self.last_leadership_snapshots = list(leadership_snapshots)
        self.last_theme_flows = dict(theme_flows or {})
        self.last_summary = summary
        self.last_run_at = current
        return dict(summary)

    def _fresh_seed_signals(self, signals: Iterable[LiveSeedSignal], now: datetime) -> list[LiveSeedSignal]:
        return [
            apply_signal_freshness(signal, now=now, max_tick_age_sec=self.config.max_tick_age_sec)
            for signal in signals
        ]

    def _restore_active_seed_registry(self, trade_date: str) -> None:
        if self._active_registry_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_active_seed_signals", None)
        if not callable(loader):
            self._active_registry_restored_trade_date = trade_date
            return
        for row in list(loader(trade_date=trade_date, active=True, limit=3000) or []):
            self.active_seed_registry.merge(
                ActiveSeedSignal(
                    code=str(row.get("code") or ""),
                    source_type=str(row.get("source_type") or ""),
                    source_id=str(row.get("source_id") or ""),
                    first_seen_at=str(row.get("first_seen_at") or ""),
                    last_seen_at=str(row.get("last_seen_at") or ""),
                    removed_at=str(row.get("removed_at") or ""),
                    active=bool(row.get("active", True)),
                    seed_rank=_int(row.get("seed_rank")),
                    rank_delta=_int(row.get("rank_delta")),
                    seen_count=max(1, _int(row.get("seen_count"))),
                    expires_at=str(row.get("expires_at") or ""),
                    latest_turnover_krw=_float(row.get("latest_turnover_krw")),
                    latest_change_rate_pct=_float(row.get("latest_change_rate_pct")),
                    reason_codes=tuple(str(item) for item in list(row.get("reason_codes") or [])),
                ),
                now=self.clock().replace(microsecond=0),
                ttl_sec=self.config.signal_ttl_sec,
            )
        self._active_registry_restored_trade_date = trade_date

    def _update_active_seed_registry(self, signals: Iterable[LiveSeedSignal], now: datetime, *, trade_date: str):
        for signal in signals:
            self.active_seed_registry.merge(signal, now=now, ttl_sec=self.config.signal_ttl_sec)
        snapshot = self.active_seed_registry.snapshot(now=now)
        self._persist_active_seed_snapshot(snapshot, trade_date=trade_date, calculated_at=now.isoformat())
        return snapshot

    def _source_delta_signals(self, *, trade_date: str, theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]] = ()) -> list[LiveSeedSignal]:
        signals: list[LiveSeedSignal] = []
        for member in _theme_members(theme_inputs).values():
            code = normalize_code(member.stock_code)
            tick = self.market_data.latest_tick(code)
            if tick is not None:
                signals.append(_signal_from_tick(tick, membership=member, ttl_sec=self.config.signal_ttl_sec))
        seed_rows = _opening_seed_rows(self.db, trade_date=trade_date)
        for row in seed_rows:
            code = normalize_code(str(row.get("stock_code") or row.get("code") or ""))
            if not code:
                continue
            row = dict(row)
            row["expiry_at"] = _expiry_at(str(row.get("observed_at") or ""), self.config.signal_ttl_sec)
            signals.append(_signal_from_seed_row(row, tick=None, seed_scope=ActiveSeedSource.OPENING.value))
        intraday_rows = _intraday_seed_rows(self.db, trade_date=trade_date)
        for row in intraday_rows:
            code = normalize_code(str(row.get("stock_code") or row.get("code") or ""))
            if not code:
                continue
            row = dict(row)
            row["expiry_at"] = _expiry_at(str(row.get("observed_at") or ""), self.config.signal_ttl_sec)
            signals.append(_signal_from_seed_row(row, tick=None, seed_scope=ActiveSeedSource.INTRADAY.value))
        for event in _condition_source_events(self.db, trade_date=trade_date):
            code = normalize_code(str(event.get("code") or ""))
            if not code:
                continue
            if _candidate_source_event_type(event) == "remove":
                self.active_seed_registry.remove_source(
                    code,
                    ActiveSeedSource.CONDITION.value,
                    _condition_source_id(event),
                    now=self.clock().replace(microsecond=0),
                    reason="CONDITION_SOURCE_REMOVED",
                )
                continue
            signals.append(_signal_from_condition_event(event, tick=None, ttl_sec=self.config.signal_ttl_sec))
        return signals

    def _active_seed_signals(self, snapshot: Any, now: datetime) -> list[LiveSeedSignal]:
        signals: list[LiveSeedSignal] = []
        for active in tuple(getattr(snapshot, "active_signals", ()) or ()):
            tick = self.market_data.latest_tick(active.code)
            signals.append(_signal_from_active_seed(active, tick=tick, now=now))
        return merge_seed_signals(signals)

    def _persist_active_seed_snapshot(self, snapshot: Any, *, trade_date: str, calculated_at: str) -> int:
        saver = getattr(self.db, "save_active_seed_signals", None)
        if not callable(saver):
            return 0
        rows = [
            _active_seed_payload(item, calculated_at=calculated_at)
            for item in [*list(getattr(snapshot, "active_signals", ()) or ()), *list(getattr(snapshot, "expired_signals", ()) or ())]
        ]
        return int(saver(rows, trade_date=trade_date) or 0)

    def _restore_turnover_flow(self, trade_date: str) -> None:
        if self._turnover_flow_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_turnover_flow_observations", None)
        if callable(loader):
            for row in list(loader(trade_date=trade_date, limit=5000) or []):
                self.turnover_flow_tracker.observe(
                    TurnoverObservation(
                        code=str(row.get("code") or ""),
                        observed_at=str(row.get("observed_at") or ""),
                        cumulative_turnover_krw=_float(row.get("cumulative_turnover_krw")),
                        reason_codes=tuple(str(item) for item in list(row.get("reason_codes") or [])),
                    )
                )
        self._turnover_flow_restored_trade_date = trade_date

    def _persist_turnover_observations(self, flows: Iterable[Any], *, trade_date: str) -> int:
        saver = getattr(self.db, "save_turnover_flow_observations", None)
        if not callable(saver):
            return 0
        rows = [
            {
                "code": flow.code,
                "observed_at": flow.observed_at,
                "cumulative_turnover_krw": flow.cumulative_turnover_krw,
                "reason_codes": list(flow.reason_codes or ()),
            }
            for flow in flows
        ]
        return int(saver(rows, trade_date=trade_date) or 0)

    def _persist_bridge_source_state(self, reconcile: Any, *, trade_date: str, calculated_at: str) -> int:
        saver = getattr(self.db, "save_candidate_bridge_source_state", None)
        if not callable(saver) or reconcile is None:
            return 0
        count = 0
        for state in list(getattr(reconcile, "active_state", ()) or ()):
            if saver({**getattr(state, "__dict__", {}), "active": True, "updated_at": calculated_at}, trade_date=trade_date):
                count += 1
        for event in list(getattr(reconcile, "remove_events", ()) or ()):
            payload = event.to_dict()
            raw = dict(payload.get("raw_payload") or {})
            state = {
                "code": payload.get("code"),
                "theme_id": payload.get("theme_id"),
                "source_id": payload.get("source_id"),
                "trade_role": payload.get("stock_role"),
                "theme_state": raw.get("leadership_status"),
                "active": False,
                "removed_at": calculated_at,
                "updated_at": calculated_at,
                "reason_codes": payload.get("reason_codes") or [],
            }
            if saver(state, trade_date=trade_date):
                count += 1
        return count

    def _restore_bridge_source_state(self, trade_date: str) -> None:
        if self._bridge_reconciler_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_candidate_bridge_source_state", None)
        restore = getattr(self.bridge_reconciler, "restore", None)
        if callable(loader) and callable(restore):
            restore(list(loader(trade_date=trade_date, active=True, limit=3000) or []))
        self._bridge_reconciler_restored_trade_date = trade_date

    def _persist_bridge_events(self, events: Iterable[Any]) -> int:
        event_list = list(events)
        if self.config.ingest_candidate_source_events:
            service = self.candidate_ingestion_service
            ingest = getattr(service, "ingest", None)
            if callable(ingest):
                count = 0
                for event in event_list:
                    ingest(event)
                    count += 1
                return count
        if not self.config.save_candidate_source_events:
            return 0
        saver = getattr(self.db, "save_candidate_source_event", None)
        if not callable(saver):
            return 0
        count = 0
        for event in event_list:
            payload = event.to_dict()
            payload["status"] = "OBSERVED"
            payload["reason"] = "THEME_CORE_V3_OBSERVE_ONLY"
            saver(payload)
            count += 1
        return count

    def _save_snapshot(self, snapshot: dict[str, Any]) -> bool:
        saver = getattr(self.db, "save_theme_board_snapshot", None)
        if not callable(saver):
            return False
        saver(snapshot)
        return True

    def _restore_theme_state(self, trade_date: str) -> None:
        if self._theme_state_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_theme_state_runtime", None)
        if not callable(loader):
            self._theme_state_restored_trade_date = trade_date
            return
        rows = list(loader(trade_date=trade_date) or [])
        snapshots = [_state_snapshot_from_runtime_row(row) for row in rows]
        restore = getattr(self.state_machine, "restore", None)
        if callable(restore):
            restore([snapshot for snapshot in snapshots if snapshot.theme_id])
        self._theme_state_restored_trade_date = trade_date

    def _persist_theme_states(self, states: Iterable[ThemeStateSnapshot], *, trade_date: str, calculated_at: str) -> int:
        saver = getattr(self.db, "save_theme_state_runtime", None)
        if not callable(saver):
            return 0
        count = 0
        for state in states:
            payload = _theme_state_payload(state, trade_date=trade_date, calculated_at=calculated_at)
            saver(payload, trade_date=trade_date, calculated_at=calculated_at)
            count += 1
        return count

    def _restore_leadership_state(self, trade_date: str) -> None:
        if self._leadership_restored_trade_date == trade_date:
            return
        loader = getattr(self.db, "list_theme_leadership_latest", None)
        restore = getattr(self.handover_engine, "restore", None)
        if callable(loader) and callable(restore):
            restore(_leadership_snapshot_from_row(row) for row in list(loader(trade_date=trade_date) or []))
        self._leadership_restored_trade_date = trade_date

    def _persist_leadership_state(self, snapshots: Iterable[Any], transitions: Iterable[Any], *, trade_date: str, calculated_at: str) -> int:
        saver = getattr(self.db, "save_theme_leadership_latest", None)
        transition_saver = getattr(self.db, "save_theme_leadership_transition", None)
        count = 0
        if callable(saver):
            for snapshot in list(snapshots or []):
                if saver(_leadership_state_payload(snapshot, trade_date=trade_date, calculated_at=calculated_at)):
                    count += 1
        if callable(transition_saver):
            for transition in list(transitions or []):
                transition_saver(_leadership_transition_payload(transition, trade_date=trade_date, calculated_at=calculated_at))
        return count


def _load_theme_inputs(repository: ThemeEngineRepository) -> list[tuple[str, str, list[ThemeMembership]]]:
    themes = list(repository.list_canonical_themes() or [])
    if not themes:
        grouped: dict[str, list[ThemeMembership]] = {}
        for membership in repository.list_current_memberships(active=True):
            grouped.setdefault(membership.theme_id, []).append(membership)
        return [(theme_id, theme_id, members) for theme_id, members in grouped.items() if members]
    return [
        (theme.theme_id, theme.display_name or theme.canonical_name or theme.theme_id, members)
        for theme in themes
        for members in [repository.get_members_by_theme(theme.theme_id, active=True)]
        if members
    ]


def _opening_seed_rows(db: Any, *, trade_date: str) -> list[dict[str, Any]]:
    batch_loader = getattr(db, "list_opening_turnover_seed_batches", None)
    row_loader = getattr(db, "list_opening_turnover_seed_rows", None)
    if not callable(batch_loader) or not callable(row_loader):
        return []
    rows: list[dict[str, Any]] = []
    for batch in list(batch_loader(trade_date=trade_date, limit=5) or []):
        batch_id = int(dict(batch or {}).get("id") or 0)
        batch_time = str(dict(batch or {}).get("batch_time") or "")
        for row in list(row_loader(batch_id=batch_id, limit=100) or []):
            item = dict(row or {})
            observed_at = _seed_observed_at(trade_date, str(item.get("batch_time") or batch_time or ""))
            item.setdefault("batch_time", batch_time)
            item.setdefault("observed_at", observed_at)
            item.setdefault("last_seen_at", observed_at)
            item.setdefault("expiry_at", _expiry_at(observed_at, 600))
            item.setdefault("source_id", f"opening:{batch_id}")
            rows.append(item)
    return rows


def _intraday_seed_rows(db: Any, *, trade_date: str) -> list[dict[str, Any]]:
    batch_loader = getattr(db, "list_intraday_theme_discovery_batches", None)
    row_loader = getattr(db, "list_intraday_theme_discovery_rows", None)
    if not callable(batch_loader) or not callable(row_loader):
        return []
    rows: list[dict[str, Any]] = []
    for batch in list(batch_loader(trade_date=trade_date, status="OK", limit=20) or []):
        batch_id = int(dict(batch or {}).get("id") or 0)
        observed_at = str(dict(batch or {}).get("observed_at") or "")
        bucket = str(dict(batch or {}).get("bucket") or "")
        for row in list(row_loader(batch_id=batch_id, limit=100) or []):
            item = dict(row or {})
            item.setdefault("observed_at", observed_at)
            item.setdefault("last_seen_at", observed_at)
            item.setdefault("batch_time", bucket)
            item.setdefault("expiry_at", _expiry_at(observed_at, 600))
            item.setdefault("source_id", f"intraday:{batch_id}")
            rows.append(item)
    return rows


def _condition_source_events(db: Any, *, trade_date: str) -> list[dict[str, Any]]:
    loader = getattr(db, "list_candidate_source_events", None)
    if not callable(loader):
        return []
    return [
        dict(event)
        for event in list(loader(trade_date=trade_date, limit=300) or [])
        if str(dict(event).get("source_type") or "") == "condition_search"
    ]


def _candidate_source_event_type(event: Mapping[str, Any]) -> str:
    raw = dict(event.get("raw_payload") or {})
    return str(raw.get("event_type") or event.get("event_type") or "include").lower()


def _condition_source_id(event: Mapping[str, Any]) -> str:
    return str(event.get("source_id") or event.get("condition_name") or "condition_search")


def _theme_members(theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]]) -> dict[str, ThemeMembership]:
    result: dict[str, ThemeMembership] = {}
    for _theme_id, _theme_name, members in theme_inputs:
        for member in list(members or []):
            code = normalize_code(member.stock_code)
            if code:
                result.setdefault(code, member)
    return result


def _signal_from_seed_row(row: Mapping[str, Any], *, tick: StrategyTick | None, seed_scope: str) -> LiveSeedSignal:
    code = normalize_code(str(row.get("stock_code") or row.get("code") or ""))
    source_types = [SeedSourceType.OPT10032.value]
    if tick is not None:
        source_types.append(SeedSourceType.REALTIME_TICK.value)
    return _signal(
        code=code,
        name=str(row.get("stock_name") or ""),
        source_types=source_types,
        seed_rank=_int(row.get("rank") or row.get("seed_rank")),
        row=row,
        tick=tick,
        seed_scope=seed_scope,
    )


def _signal_from_tick(tick: StrategyTick, *, membership: ThemeMembership | None = None, ttl_sec: int = 600) -> LiveSeedSignal:
    metadata = dict(tick.metadata or {})
    observed_at = tick.timestamp.isoformat() if tick.timestamp else ""
    return _signal(
        code=tick.code,
        name=str(metadata.get("stock_name") or getattr(membership, "stock_name", "") or ""),
        source_types=[SeedSourceType.REALTIME_TICK.value],
        seed_rank=0,
        row={
            "source_id": f"realtime:{normalize_code(tick.code)}",
            "observed_at": observed_at,
            "last_seen_at": observed_at,
            "expiry_at": _expiry_at(observed_at, ttl_sec),
        },
        tick=tick,
        seed_scope=ActiveSeedSource.MANUAL.value,
    )


def _signal_from_condition_event(event: Mapping[str, Any], *, tick: StrategyTick | None, ttl_sec: int = 600) -> LiveSeedSignal:
    source_types = [SeedSourceType.CONDITION_INCLUDE.value]
    if tick is not None:
        source_types.append(SeedSourceType.REALTIME_TICK.value)
    return _signal(
        code=str(event.get("code") or ""),
        name=str(event.get("name") or ""),
        source_types=source_types,
        seed_rank=_int(event.get("source_rank")),
        row={
            "reason_codes": list(event.get("reason_codes") or []),
            "source_id": _condition_source_id(event),
            "observed_at": str(event.get("detected_at") or ""),
            "last_seen_at": str(event.get("detected_at") or ""),
            "expiry_at": _expiry_at(str(event.get("detected_at") or ""), ttl_sec),
        },
        tick=tick,
        condition_score=_float(event.get("source_score")),
        seed_scope=ActiveSeedSource.CONDITION.value,
    )


def _signal_from_active_seed(active: ActiveSeedSignal, *, tick: StrategyTick | None, now: datetime) -> LiveSeedSignal:
    base = active.to_live_signal()
    metadata = dict(base.metadata or {})
    metadata["source_id"] = active.source_id
    metadata["seed_scope"] = active.source_type
    if tick is None:
        return LiveSeedSignal(
            code=base.code,
            name=base.name,
            source_types=base.source_types,
            seed_rank=base.seed_rank,
            change_rate_pct=base.change_rate_pct,
            turnover_krw=base.turnover_krw,
            realtime_valid=False,
            tr_backfill_valid=True,
            observed_at=base.observed_at,
            last_seen_at=base.last_seen_at,
            active=active.active,
            expiry_at=active.expires_at,
            metadata=metadata,
            reason_codes=tuple(base.reason_codes),
        ).normalized()
    tick_metadata = dict(tick.metadata or {})
    metadata.update(tick_metadata)
    upper_gap = _float(tick_metadata.get("upper_limit_gap_pct"), default=100.0)
    source_types = tuple(dict.fromkeys([*base.source_types, SeedSourceType.REALTIME_TICK.value]))
    return LiveSeedSignal(
        code=base.code,
        name=base.name or str(tick_metadata.get("stock_name") or tick_metadata.get("name") or ""),
        source_types=source_types,
        seed_rank=base.seed_rank,
        change_rate_pct=_float(getattr(tick, "change_rate", 0.0)),
        turnover_krw=_float(getattr(tick, "trade_value", 0.0)) or base.turnover_krw,
        turnover_speed=_float(tick_metadata.get("turnover_speed") or tick_metadata.get("turnover_krw_per_min")),
        execution_strength=_float(getattr(tick, "execution_strength", 0.0)),
        realtime_valid=bool(getattr(tick, "price", 0) > 0 and tick_metadata.get("price_source") != "TR_BACKFILL"),
        tr_backfill_valid=bool(tick_metadata.get("price_source") == "TR_BACKFILL"),
        observed_at=base.observed_at,
        last_seen_at=base.last_seen_at,
        tick_at=tick.timestamp.isoformat() if tick.timestamp else now.isoformat(),
        active=active.active,
        expiry_at=active.expires_at,
        market=str(tick_metadata.get("market") or ""),
        momentum_1m=_float(tick_metadata.get("momentum_1m")),
        momentum_3m=_float(tick_metadata.get("momentum_3m")),
        momentum_5m=_float(tick_metadata.get("momentum_5m")),
        vi_active=_bool(tick_metadata.get("vi_active")),
        upper_limit_near=_bool(tick_metadata.get("upper_limit_near")) or upper_gap <= 3.0,
        overheated=_bool(tick_metadata.get("overheated")),
        reason_codes=tuple(base.reason_codes),
        metadata=metadata,
    ).normalized()


def _signal(
    *,
    code: str,
    name: str,
    source_types: list[str],
    seed_rank: int,
    row: Mapping[str, Any],
    tick: StrategyTick | None,
    condition_score: float = 0.0,
    seed_scope: str = "",
) -> LiveSeedSignal:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    upper_gap = _float(metadata.get("upper_limit_gap_pct"), default=100.0)
    reason_codes = list(row.get("reason_codes") or [])
    if condition_score > 0:
        reason_codes.append("CONDITION_INCLUDE_BOOSTER_ONLY")
    observed_at = str(row.get("observed_at") or row.get("batch_time") or row.get("detected_at") or "")
    metadata.update(
        {
            "seed_scope": seed_scope,
            "source_id": str(row.get("source_id") or row.get("batch_id") or seed_scope or ""),
        }
    )
    return LiveSeedSignal(
        code=code,
        name=name or str(metadata.get("stock_name") or metadata.get("name") or ""),
        source_types=tuple(source_types),
        seed_rank=seed_rank,
        change_rate_pct=_float(getattr(tick, "change_rate", 0.0) if tick is not None else row.get("change_rate_pct")),
        turnover_krw=_float(getattr(tick, "trade_value", 0.0) if tick is not None else row.get("turnover_krw")),
        turnover_speed=_float(metadata.get("turnover_speed") or metadata.get("turnover_krw_per_min")),
        execution_strength=_float(getattr(tick, "execution_strength", 0.0) if tick is not None else 0.0),
        realtime_valid=bool(tick is not None and getattr(tick, "price", 0) > 0 and metadata.get("price_source") != "TR_BACKFILL"),
        tr_backfill_valid=bool(tick is None or metadata.get("price_source") == "TR_BACKFILL"),
        reason_codes=tuple(reason_codes),
        observed_at=observed_at,
        last_seen_at=str(row.get("last_seen_at") or observed_at),
        tick_at=tick.timestamp.isoformat() if tick is not None and tick.timestamp else "",
        expiry_at=str(row.get("expiry_at") or _expiry_at(observed_at, 600)),
        market=str(metadata.get("market") or ""),
        momentum_1m=_float(metadata.get("momentum_1m")),
        momentum_3m=_float(metadata.get("momentum_3m")),
        momentum_5m=_float(metadata.get("momentum_5m")),
        vi_active=_bool(metadata.get("vi_active")),
        upper_limit_near=_bool(metadata.get("upper_limit_near")) or upper_gap <= 3.0,
        overheated=_bool(metadata.get("overheated")),
        metadata=metadata,
    )


def _theme_board_snapshot(
    view: dict[str, Any],
    *,
    theme_states: Iterable[ThemeStateSnapshot],
    role_decisions: Iterable[StockRoleDecision],
    theme_flows: Mapping[str, Any] | None = None,
    leadership_snapshots: Iterable[Any] = (),
    trade_date: str,
    calculated_at: str,
    top_theme_count: int,
) -> dict[str, Any]:
    states = sorted(list(theme_states), key=lambda item: item.theme_score, reverse=True)
    decisions = list(role_decisions)
    flow_by_theme = dict(theme_flows or {})
    leadership_by_theme = {item.theme_id: item for item in list(leadership_snapshots or []) if getattr(item, "theme_id", "")}
    all_theme_payloads = [
        _theme_payload(
            state,
            index,
            flow=flow_by_theme.get(state.theme_id),
            leadership=leadership_by_theme.get(state.theme_id),
        )
        for index, state in enumerate(states, start=1)
    ]
    top_themes = all_theme_payloads[:top_theme_count]
    stocks = [_stock_payload(decision) for decision in sorted(decisions, key=lambda item: (item.theme_id, item.source_rank, -item.role_score))]
    stock_contexts_by_code: dict[str, list[dict[str, Any]]] = {}
    for stock in stocks:
        code = normalize_code(str(stock.get("code") or ""))
        if code:
            stock_contexts_by_code.setdefault(code, []).append(stock)
    active_states = {
        ThemeCoreState.LEADING_THEME.value,
        ThemeCoreState.SPREADING_THEME.value,
        ThemeCoreState.LEADER_ONLY_THEME.value,
    }
    return {
        "trade_date": trade_date,
        "calculated_at": calculated_at,
        "board_status": THEME_CORE_V3_OUTPUT_MODE,
        "theme_count": len(states),
        "active_theme_count": sum(1 for state in states if state.theme_state in active_states),
        "watch_theme_count": sum(1 for state in states if state.theme_state == ThemeCoreState.WATCH_THEME.value),
        "data_wait_theme_count": sum(1 for state in states if state.theme_state == ThemeCoreState.DATA_WAIT.value),
        "top_themes": top_themes,
        "themes_by_id": {str(theme.get("theme_id") or ""): theme for theme in all_theme_payloads if str(theme.get("theme_id") or "")},
        "leadership_by_theme": {theme_id: _leadership_payload(leadership) for theme_id, leadership in leadership_by_theme.items()},
        "stock_contexts_by_code": stock_contexts_by_code,
        "stocks": stocks,
        "turnover_flow": _turnover_flow_summary(flow_by_theme),
        "leadership_handover": _leadership_summary(leadership_by_theme.values(), ()),
        "source_counts": dict(view.get("source_counts") or {}),
        "data_quality_flags": [state.data_quality_reason for state in states if state.data_quality_reason],
        "reason_codes": list(view.get("reason_codes") or []),
        "output_mode": THEME_CORE_V3_OUTPUT_MODE,
        "ready_allowed": False,
        "order_intent_allowed": False,
        "theme_core_v3": view,
    }


def _theme_payload(state: ThemeStateSnapshot, rank: int, *, flow: Any = None, leadership: Any = None) -> dict[str, Any]:
    cohort = state.cohort
    return {
        "theme_id": state.theme_id,
        "theme_name": state.theme_name,
        "theme_rank": rank,
        "theme_status": state.theme_state,
        "theme_state": state.theme_state,
        "previous_theme_state": state.previous_state,
        "theme_transition": state.transition,
        "theme_score": state.theme_score,
        "theme_score_delta": getattr(state, "theme_score_delta", 0.0),
        "persistence_count": state.persistence_count,
        "state_entered_at": getattr(state, "state_entered_at", ""),
        "state_age_sec": getattr(state, "state_age_sec", 0),
        "state_cycle_count": getattr(state, "state_cycle_count", 0),
        "temporal_persistence_sec": getattr(state, "temporal_persistence_sec", 0),
        "fading_since": getattr(state, "fading_since", ""),
        "recovery_pending_since": getattr(state, "recovery_pending_since", ""),
        "recovery_cycle_count": getattr(state, "recovery_cycle_count", 0),
        "strong_count": getattr(cohort, "strong_count", 0) if cohort is not None else 0,
        "leader_count": getattr(cohort, "leader_count", 0) if cohort is not None else 0,
        "breadth_ratio": getattr(cohort, "strong_ratio", 0.0) if cohort is not None else 0.0,
        "weighted_return_pct": getattr(cohort, "weighted_return_pct", 0.0) if cohort is not None else 0.0,
        "leader_concentration": getattr(cohort, "leader_concentration", 0.0) if cohort is not None else 0.0,
        "coverage_ratio": getattr(cohort, "coverage_ratio", 0.0) if cohort is not None else 0.0,
        "full_universe_coverage_ratio": getattr(cohort, "full_universe_coverage_ratio", 0.0) if cohort is not None else 0.0,
        "planned_sample_coverage_ratio": getattr(cohort, "planned_sample_coverage_ratio", 0.0) if cohort is not None else 0.0,
        "fresh_sample_count": getattr(cohort, "fresh_sample_count", 0) if cohort is not None else 0,
        "target_sample_count": getattr(cohort, "target_sample_count", 0) if cohort is not None else 0,
        "breadth_trust_level": getattr(cohort, "breadth_trust_level", "") if cohort is not None else "",
        "theme_turnover_krw": getattr(cohort, "theme_turnover_krw", 0.0) if cohort is not None else 0.0,
        "theme_turnover_delta_1m": getattr(flow, "theme_turnover_delta_1m", 0.0),
        "theme_flow_share": getattr(flow, "theme_flow_share", 0.0),
        "theme_flow_share_delta": getattr(flow, "theme_flow_share_delta", 0.0),
        "fresh_flow_coverage_ratio": getattr(flow, "fresh_flow_coverage_ratio", 0.0),
        "leadership_status": getattr(leadership, "status", "NEUTRAL"),
        "leadership_score": getattr(leadership, "leadership_score", 0.0),
        "leadership_rank": getattr(leadership, "current_rank", 0),
        "leadership_rank_delta": getattr(leadership, "rank_delta", 0),
        "takeover_pending_since": getattr(leadership, "takeover_pending_since", ""),
        "leader_symbol": state.leader_symbol,
        "previous_leader_symbol": getattr(state, "previous_leader_symbol", ""),
        "co_leader_symbols": list(state.co_leader_symbols),
        "leader_stability_sec": getattr(state, "leader_stability_sec", 0),
        "last_strong_at": getattr(state, "last_strong_at", ""),
        "last_fresh_signal_at": getattr(state, "last_fresh_signal_at", ""),
        "data_quality_reason": state.data_quality_reason,
        "data_quality_status": state.data_quality_reason or "OK",
        "leader_changed": bool(state.leader_changed),
        "leader_stability_count": getattr(state, "leader_stability_count", 1),
        "reason_codes": list(state.reason_codes),
    }


def _stock_payload(decision: StockRoleDecision) -> dict[str, Any]:
    return {
        "code": decision.code,
        "name": decision.name,
        "theme_id": decision.theme_id,
        "theme_name": decision.theme_name,
        "stock_role": decision.raw_role,
        "raw_role": decision.raw_role,
        "trade_role": decision.trade_role,
        "stock_score": decision.role_score,
        "role_score": decision.role_score,
        "entry_usable": False,
        "source_rank": decision.source_rank,
        "reason_codes": list(decision.reason_codes),
    }


def _leadership_payload(item: Any) -> dict[str, Any]:
    return {
        "theme_id": getattr(item, "theme_id", ""),
        "theme_name": getattr(item, "theme_name", ""),
        "current_rank": getattr(item, "current_rank", 0),
        "previous_rank": getattr(item, "previous_rank", 0),
        "rank_delta": getattr(item, "rank_delta", 0),
        "leadership_status": getattr(item, "status", "NEUTRAL"),
        "leadership_score": getattr(item, "leadership_score", 0.0),
        "recent_flow_score": getattr(item, "recent_flow_score", 0.0),
        "flow_share": getattr(item, "flow_share", 0.0),
        "flow_share_delta": getattr(item, "flow_share_delta", 0.0),
        "status_entered_at": getattr(item, "status_entered_at", ""),
        "status_age_sec": getattr(item, "status_age_sec", 0),
        "status_cycle_count": getattr(item, "status_cycle_count", 0),
        "takeover_pending_since": getattr(item, "takeover_pending_since", ""),
        "takeover_pending_cycle_count": getattr(item, "takeover_pending_cycle_count", 0),
        "takeover_confirmed_at": getattr(item, "takeover_confirmed_at", ""),
        "previous_incumbent_theme_id": getattr(item, "previous_incumbent_theme_id", ""),
        "reason_codes": list(getattr(item, "handover_reason_codes", ()) or ()),
    }


def _theme_core_market_input(market_context: Mapping[str, Any] | None, *, config: ThemeCoreV3RuntimeConfig) -> dict[str, Any]:
    if not config.use_runtime_market_context:
        phase = str(config.market_phase or "SELECTIVE").upper()
        return {
            "status": "OK",
            "market_phase": phase,
            "kosdaq_risk_state": str(config.kosdaq_risk_state or "").upper(),
            "market_phase_by_side": {"GLOBAL": phase, "UNKNOWN": phase},
            "reason_codes": [],
        }
    payload = dict(market_context or {})
    if not payload or not str(payload.get("calculated_at") or ""):
        return {
            "status": "DATA_WAIT",
            "market_phase": "DATA_WAIT",
            "kosdaq_risk_state": "DATA_WAIT",
            "market_phase_by_side": {"GLOBAL": "DATA_WAIT", "UNKNOWN": "DATA_WAIT", "KOSPI": "DATA_WAIT", "KOSDAQ": "DATA_WAIT"},
            "reason_codes": ["MARKET_CONTEXT_NOT_READY"],
        }
    global_status = str(payload.get("global_status") or "DATA_WAIT").upper()
    kospi_status = str(payload.get("kospi_status") or global_status).upper()
    kosdaq_status = str(payload.get("kosdaq_status") or global_status).upper()
    return {
        "status": "DATA_WAIT" if global_status in {"", "DATA_WAIT", "MARKET_CLOSED"} else "OK",
        "market_phase": _market_phase(global_status),
        "kosdaq_risk_state": kosdaq_status if kosdaq_status in {"WEAK", "RISK_OFF"} else "",
        "market_phase_by_side": {
            "GLOBAL": _market_phase(global_status),
            "UNKNOWN": _market_phase(global_status),
            "KOSPI": _market_phase(kospi_status),
            "KOSDAQ": _market_phase(kosdaq_status),
        },
        "reason_codes": list(payload.get("reason_codes") or []),
    }


def _market_phase(status: str) -> str:
    value = str(status or "").upper()
    if value == "EXPANSION":
        return "EXPANSION"
    if value in {"SELECTIVE", "CHOPPY"}:
        return "SELECTIVE"
    if value in {"WEAK", "RISK_OFF"}:
        return "RISK_OFF"
    return "DATA_WAIT"


def _seed_observed_at(trade_date: str, timestamp: str) -> str:
    value = str(timestamp or "")
    if "T" in value:
        return value
    if trade_date and len(value) >= 5:
        return f"{trade_date}T{value[:5]}:00"
    return value


def _expiry_at(observed_at: str, ttl_sec: int) -> str:
    parsed = _parse_time(observed_at)
    if parsed is None:
        return ""
    return (parsed + timedelta(seconds=max(1, int(ttl_sec or 600)))).replace(microsecond=0).isoformat()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _active_seed_payload(item: ActiveSeedSignal, *, calculated_at: str) -> dict[str, Any]:
    return {
        "code": item.code,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "first_seen_at": item.first_seen_at,
        "last_seen_at": item.last_seen_at,
        "active": item.active,
        "removed_at": item.removed_at,
        "expires_at": item.expires_at,
        "seed_rank": item.seed_rank,
        "rank_delta": item.rank_delta,
        "seen_count": item.seen_count,
        "latest_turnover_krw": item.latest_turnover_krw,
        "latest_change_rate_pct": item.latest_change_rate_pct,
        "reason_codes": list(item.reason_codes or ()),
        "updated_at": calculated_at,
    }


def _theme_state_payload(state: ThemeStateSnapshot, *, trade_date: str, calculated_at: str) -> dict[str, Any]:
    return {
        **_theme_payload(state, 0),
        "trade_date": trade_date,
        "calculated_at": calculated_at,
        "previous_state": state.previous_state,
        "current_state": state.theme_state,
        "transition": state.transition,
        "persistence_count": state.persistence_count,
        "last_calculated_at": calculated_at,
    }


def _state_snapshot_from_runtime_row(row: Mapping[str, Any]) -> ThemeStateSnapshot:
    return ThemeStateSnapshot(
        theme_id=str(row.get("theme_id") or ""),
        theme_name=str(row.get("theme_name") or ""),
        theme_state=str(row.get("theme_state") or row.get("current_state") or ThemeCoreState.SEED_WAIT.value),
        previous_state=str(row.get("previous_state") or ""),
        transition=str(row.get("transition") or ""),
        persistence_count=_int(row.get("persistence_count")),
        theme_score=_float(row.get("theme_score")),
        theme_score_delta=_float(row.get("theme_score_delta")),
        leader_symbol=normalize_code(str(row.get("leader_symbol") or "")),
        previous_leader_symbol=normalize_code(str(row.get("previous_leader_symbol") or "")),
        co_leader_symbols=tuple(normalize_code(str(item)) for item in list(row.get("co_leader_symbols") or []) if normalize_code(str(item))),
        leader_changed=_bool(row.get("leader_changed")),
        leader_stability_count=max(1, _int(row.get("leader_stability_count"))),
        data_quality_reason=str(row.get("data_quality_reason") or row.get("data_quality_status") or ""),
        reason_codes=tuple(str(item) for item in list(row.get("reason_codes") or [])),
        state_entered_at=str(row.get("state_entered_at") or ""),
        state_age_sec=_int(row.get("state_age_sec")),
        state_cycle_count=max(1, _int(row.get("state_cycle_count"))),
        strong_since=str(row.get("strong_since") or ""),
        spreading_since=str(row.get("spreading_since") or ""),
        leading_since=str(row.get("leading_since") or ""),
        fading_since=str(row.get("fading_since") or ""),
        recovery_pending_since=str(row.get("recovery_pending_since") or ""),
        recovery_cycle_count=_int(row.get("recovery_cycle_count")),
        temporal_persistence_sec=_int(row.get("temporal_persistence_sec")),
        leader_stability_sec=_int(row.get("leader_stability_sec")),
        last_strong_at=str(row.get("last_strong_at") or ""),
        last_fresh_signal_at=str(row.get("last_fresh_signal_at") or ""),
        cohort=None,
    )


def _leadership_snapshot_from_row(row: Mapping[str, Any]) -> ThemeLeadershipSnapshot:
    return ThemeLeadershipSnapshot(
        theme_id=str(row.get("theme_id") or ""),
        theme_name=str(row.get("theme_name") or ""),
        current_rank=_int(row.get("current_rank")),
        previous_rank=_int(row.get("previous_rank")),
        rank_delta=_int(row.get("rank_delta")),
        status=str(row.get("leadership_status") or row.get("status") or "NEUTRAL"),
        recent_flow_score=_float(row.get("recent_flow_score")),
        leadership_score=_float(row.get("leadership_score")),
        flow_share=_float(row.get("flow_share")),
        flow_share_delta=_float(row.get("flow_share_delta")),
        status_entered_at=str(row.get("status_entered_at") or ""),
        status_age_sec=_int(row.get("status_age_sec")),
        status_cycle_count=_int(row.get("status_cycle_count")),
        challenger_cycle_count=_int(row.get("challenger_cycle_count")),
        takeover_pending_cycle_count=_int(row.get("takeover_pending_cycle_count")),
        incumbent_cycle_count=_int(row.get("incumbent_cycle_count")),
        last_ranked_at=str(row.get("last_ranked_at") or ""),
        incumbent_since=str(row.get("incumbent_since") or ""),
        challenger_since=str(row.get("challenger_since") or ""),
        takeover_pending_since=str(row.get("takeover_pending_since") or ""),
        takeover_confirmed_at=str(row.get("takeover_confirmed_at") or ""),
        previous_incumbent_theme_id=str(row.get("previous_incumbent_theme_id") or ""),
        handover_reason_codes=tuple(str(item) for item in list(row.get("reason_codes") or row.get("handover_reason_codes") or [])),
    )


def _leadership_state_payload(item: Any, *, trade_date: str, calculated_at: str) -> dict[str, Any]:
    return {
        **_leadership_payload(item),
        "trade_date": trade_date,
        "calculated_at": calculated_at,
        "status": getattr(item, "status", ""),
        "base_strength_score": getattr(item, "base_strength_score", 0.0),
        "challenger_cycle_count": getattr(item, "challenger_cycle_count", 0),
        "incumbent_cycle_count": getattr(item, "incumbent_cycle_count", 0),
        "last_ranked_at": getattr(item, "last_ranked_at", ""),
        "incumbent_since": getattr(item, "incumbent_since", ""),
        "challenger_since": getattr(item, "challenger_since", ""),
        "handover_reason_codes": list(getattr(item, "handover_reason_codes", ()) or ()),
    }


def _leadership_transition_payload(item: Any, *, trade_date: str, calculated_at: str) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "calculated_at": calculated_at,
        "theme_id": getattr(item, "theme_id", ""),
        "previous_status": getattr(item, "previous_status", ""),
        "current_status": getattr(item, "current_status", ""),
        "detected_at": getattr(item, "detected_at", calculated_at),
        "previous_incumbent_theme_id": getattr(item, "previous_incumbent_theme_id", ""),
        "current_incumbent_theme_id": getattr(item, "current_incumbent_theme_id", ""),
        "reason_codes": list(getattr(item, "reason_codes", ()) or ()),
    }


def _summary_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    stocks = list(snapshot.get("stocks") or [])
    return {
        "theme_count": int(snapshot.get("theme_count") or 0),
        "active_theme_count": int(snapshot.get("active_theme_count") or 0),
        "data_wait_theme_count": int(snapshot.get("data_wait_theme_count") or 0),
        "top_themes": list(snapshot.get("top_themes") or [])[:5],
        "leader_codes": [
            str(stock.get("code") or "")
            for stock in stocks
            if str(stock.get("stock_role") or "") in {RawStockRole.LEADER.value, RawStockRole.CO_LEADER.value}
        ][:10],
        "excluded_late_laggard_count": sum(1 for stock in stocks if str(stock.get("raw_role") or "") == RawStockRole.LATE_LAGGARD.value),
        "excluded_overheated_count": sum(1 for stock in stocks if str(stock.get("raw_role") or "") == RawStockRole.OVERHEATED.value),
    }


def _turnover_flow_summary(flows: Mapping[str, Any]) -> dict[str, Any]:
    values = list(flows.values())
    return {
        "enabled": True,
        "theme_flow_count": len(values),
        "top_flow_themes": [
            {
                "theme_id": flow.theme_id,
                "flow_share": flow.theme_flow_share,
                "flow_share_delta": flow.theme_flow_share_delta,
                "theme_turnover_delta_1m": flow.theme_turnover_delta_1m,
                "fresh_flow_coverage_ratio": flow.fresh_flow_coverage_ratio,
            }
            for flow in sorted(values, key=lambda item: (item.theme_flow_share, item.theme_turnover_delta_1m), reverse=True)[:5]
        ],
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _leadership_summary(snapshots: Iterable[Any], transitions: Iterable[Any]) -> dict[str, Any]:
    snapshot_list = list(snapshots or [])
    transition_list = list(transitions or [])
    return {
        "enabled": True,
        "ranked_theme_count": len(snapshot_list),
        "transition_count": len(transition_list),
        "top_leadership": [
            {
                "theme_id": item.theme_id,
                "theme_name": item.theme_name,
                "current_rank": item.current_rank,
                "previous_rank": item.previous_rank,
                "rank_delta": item.rank_delta,
                "leadership_status": item.status,
                "leadership_score": item.leadership_score,
                "recent_flow_score": item.recent_flow_score,
                "flow_share": item.flow_share,
                "handover_reason_codes": list(item.handover_reason_codes),
                "status_age_sec": getattr(item, "status_age_sec", 0),
                "status_cycle_count": getattr(item, "status_cycle_count", 0),
                "takeover_pending_since": getattr(item, "takeover_pending_since", ""),
                "takeover_pending_cycle_count": getattr(item, "takeover_pending_cycle_count", 0),
                "takeover_confirmed_at": getattr(item, "takeover_confirmed_at", ""),
            }
            for item in snapshot_list[:5]
        ],
        "transitions": [
            {
                "theme_id": item.theme_id,
                "previous_status": item.previous_status,
                "current_status": item.current_status,
                "previous_incumbent_theme_id": getattr(item, "previous_incumbent_theme_id", ""),
                "current_incumbent_theme_id": getattr(item, "current_incumbent_theme_id", ""),
                "reason_codes": list(item.reason_codes),
            }
            for item in transition_list[:10]
        ],
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _bridge_reconcile_summary(result: Any) -> dict[str, Any]:
    if result is None:
        return {"enabled": False, "status": "DISABLED"}
    return {
        "enabled": True,
        "included_count": int(getattr(result, "included_count", 0) or 0),
        "removed_count": int(getattr(result, "removed_count", 0) or 0),
        "unchanged_count": int(getattr(result, "unchanged_count", 0) or 0),
        "active_source_count": len(tuple(getattr(result, "active_state", ()) or ())),
        "reason_codes": list(getattr(result, "reason_codes", ()) or ()),
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def _empty_summary(*, enabled: bool, observe_only: bool) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "observe_only": bool(observe_only),
        "status": "DISABLED" if not enabled else "DATA_WAIT",
        "output_mode": THEME_CORE_V3_OUTPUT_MODE,
        "ready_allowed": False,
        "order_intent_allowed": False,
        "theme_count": 0,
        "active_theme_count": 0,
        "data_wait_theme_count": 0,
        "top_themes": [],
        "leader_codes": [],
        "reason_codes": [],
    }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


__all__ = [
    "THEME_CORE_V3_OUTPUT_MODE",
    "ThemeCoreV3RuntimeConfig",
    "ThemeCoreV3RuntimePipeline",
]
