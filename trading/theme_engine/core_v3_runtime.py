from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.theme_engine.board_view import ThemeBoardView
from trading.theme_engine.candidate_bridge import CandidateBridge
from trading.theme_engine.cohort import ThemeCohortEngine
from trading.theme_engine.expansion import FocusedExpansionPlanner
from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, StockRoleEngine
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, merge_seed_signals
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateMachine, ThemeStateSnapshot


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
        self.candidate_ingestion_service = candidate_ingestion_service
        self.clock = clock or datetime.now
        self.last_result: dict[str, Any] | None = None
        self.last_summary: dict[str, Any] = _empty_summary(enabled=self.config.enabled, observe_only=self.config.observe_only)
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = _empty_summary(enabled=False, observe_only=self.config.observe_only)
            return dict(self.last_summary)
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        return self.run(current)

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = current.date().isoformat()
        theme_inputs = _load_theme_inputs(self.repository)
        seed_signals = self._seed_signals(theme_inputs, trade_date=trade_date)
        summary = _empty_summary(enabled=self.config.enabled, observe_only=self.config.observe_only)
        summary.update(
            {
                "status": "OK",
                "trade_date": trade_date,
                "calculated_at": current.isoformat(),
                "theme_input_count": len(theme_inputs),
                "seed_signal_count": len(seed_signals),
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

        cohorts = self.cohort_engine.build(theme_inputs, seed_signals)
        theme_states = self.state_machine.apply(cohorts)
        role_decisions = [
            decision
            for state in theme_states
            for decision in self.role_engine.classify(state, market_phase=self.config.market_phase)
        ]
        expansion_plan = self.expansion_planner.plan(
            theme_states,
            role_decisions,
            market_phase=self.config.market_phase,
            kosdaq_risk_state=self.config.kosdaq_risk_state,
        )
        bridge_result = self.candidate_bridge.build_events(
            role_decisions,
            trade_date=trade_date,
            detected_at=current.isoformat(),
        )
        bridge_saved = self._persist_bridge_events(bridge_result.events)
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
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            top_theme_count=self.config.top_theme_count,
        )
        saved = self._save_snapshot(snapshot)
        summary.update(_summary_from_snapshot(snapshot))
        summary.update(
            {
                "status": summary.get("status") or "OK",
                "candidate_bridge_event_count": len(bridge_result.events),
                "candidate_bridge_excluded_count": len(bridge_result.excluded),
                "candidate_source_event_saved_count": bridge_saved,
                "candidate_ingestion_enabled": bool(self.config.ingest_candidate_source_events),
                "ready_allowed": False,
                "order_intent_allowed": False,
                "output_mode": THEME_CORE_V3_OUTPUT_MODE,
                "saved": bool(saved),
            }
        )
        if not seed_signals:
            summary["status"] = "DATA_WAIT"
        self.last_result = snapshot
        self.last_summary = summary
        self.last_run_at = current
        return dict(summary)

    def _seed_signals(
        self,
        theme_inputs: list[tuple[str, str, list[ThemeMembership]]],
        *,
        trade_date: str,
    ) -> list[LiveSeedSignal]:
        signals: list[LiveSeedSignal] = []
        seed_rows = _opening_seed_rows(self.db, trade_date=trade_date)
        for row in seed_rows:
            code = normalize_code(str(row.get("stock_code") or row.get("code") or ""))
            if not code:
                continue
            tick = self.market_data.latest_tick(code)
            signals.append(_signal_from_seed_row(row, tick=tick))
        for code, membership in _theme_members(theme_inputs).items():
            tick = self.market_data.latest_tick(code)
            if tick is not None:
                signals.append(_signal_from_tick(tick, membership=membership))
        for event in _condition_source_events(self.db, trade_date=trade_date):
            code = normalize_code(str(event.get("code") or ""))
            if not code:
                continue
            tick = self.market_data.latest_tick(code)
            signals.append(_signal_from_condition_event(event, tick=tick))
        return merge_seed_signals(signals)

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
            item.setdefault("batch_time", batch_time)
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


def _theme_members(theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]]) -> dict[str, ThemeMembership]:
    result: dict[str, ThemeMembership] = {}
    for _theme_id, _theme_name, members in theme_inputs:
        for member in list(members or []):
            code = normalize_code(member.stock_code)
            if code:
                result.setdefault(code, member)
    return result


def _signal_from_seed_row(row: Mapping[str, Any], *, tick: StrategyTick | None) -> LiveSeedSignal:
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
    )


def _signal_from_tick(tick: StrategyTick, *, membership: ThemeMembership | None = None) -> LiveSeedSignal:
    metadata = dict(tick.metadata or {})
    return _signal(
        code=tick.code,
        name=str(metadata.get("stock_name") or getattr(membership, "stock_name", "") or ""),
        source_types=[SeedSourceType.REALTIME_TICK.value],
        seed_rank=0,
        row={},
        tick=tick,
    )


def _signal_from_condition_event(event: Mapping[str, Any], *, tick: StrategyTick | None) -> LiveSeedSignal:
    source_types = [SeedSourceType.CONDITION_INCLUDE.value]
    if tick is not None:
        source_types.append(SeedSourceType.REALTIME_TICK.value)
    return _signal(
        code=str(event.get("code") or ""),
        name=str(event.get("name") or ""),
        source_types=source_types,
        seed_rank=_int(event.get("source_rank")),
        row={"reason_codes": list(event.get("reason_codes") or [])},
        tick=tick,
        condition_score=_float(event.get("source_score")),
    )


def _signal(
    *,
    code: str,
    name: str,
    source_types: list[str],
    seed_rank: int,
    row: Mapping[str, Any],
    tick: StrategyTick | None,
    condition_score: float = 0.0,
) -> LiveSeedSignal:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    upper_gap = _float(metadata.get("upper_limit_gap_pct"), default=100.0)
    reason_codes = list(row.get("reason_codes") or [])
    if condition_score > 0:
        reason_codes.append("CONDITION_INCLUDE_BOOSTER_ONLY")
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
    trade_date: str,
    calculated_at: str,
    top_theme_count: int,
) -> dict[str, Any]:
    states = sorted(list(theme_states), key=lambda item: item.theme_score, reverse=True)
    decisions = list(role_decisions)
    top_themes = [_theme_payload(state, index) for index, state in enumerate(states[:top_theme_count], start=1)]
    stocks = [_stock_payload(decision) for decision in sorted(decisions, key=lambda item: (item.theme_id, item.source_rank, -item.role_score))]
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
        "stocks": stocks,
        "source_counts": dict(view.get("source_counts") or {}),
        "data_quality_flags": [state.data_quality_reason for state in states if state.data_quality_reason],
        "reason_codes": list(view.get("reason_codes") or []),
        "output_mode": THEME_CORE_V3_OUTPUT_MODE,
        "ready_allowed": False,
        "order_intent_allowed": False,
        "theme_core_v3": view,
    }


def _theme_payload(state: ThemeStateSnapshot, rank: int) -> dict[str, Any]:
    cohort = state.cohort
    return {
        "theme_id": state.theme_id,
        "theme_name": state.theme_name,
        "theme_rank": rank,
        "theme_status": state.theme_state,
        "theme_state": state.theme_state,
        "theme_score": state.theme_score,
        "strong_count": getattr(cohort, "strong_count", 0) if cohort is not None else 0,
        "leader_count": getattr(cohort, "leader_count", 0) if cohort is not None else 0,
        "theme_turnover_krw": getattr(cohort, "theme_turnover_krw", 0.0) if cohort is not None else 0.0,
        "leader_symbol": state.leader_symbol,
        "co_leader_symbols": list(state.co_leader_symbols),
        "data_quality_reason": state.data_quality_reason,
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
        "entry_usable": False,
        "source_rank": decision.source_rank,
        "reason_codes": list(decision.reason_codes),
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
