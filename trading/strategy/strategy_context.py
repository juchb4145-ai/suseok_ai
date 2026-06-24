from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, time
from enum import Enum
from time import perf_counter
from typing import Any, Iterable, Mapping

from trading.strategy.candidate_state_contract import CandidateStateContractService
from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_action import normalize_market_action
from trading.strategy.market_regime import (
    CandidateMarketAction,
    MarketRegimeStatus,
    MarketSide,
    market_policy_for_side,
    systemic_risk_off_state,
)
from trading.strategy.models import Candidate
from trading.theme_engine.context_resolver import BestThemeContextResolver


STRATEGY_CONTEXT_SCHEMA_VERSION = "strategy_context_v3"
STRATEGY_CONTEXT_OUTPUT_MODE = "OBSERVE"


class StrategyContextVersion(str, Enum):
    V3 = STRATEGY_CONTEXT_SCHEMA_VERSION


class SessionPhase(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    OPENING_DISCOVERY = "OPENING_DISCOVERY"
    MORNING_TREND = "MORNING_TREND"
    MIDDAY_CHOP = "MIDDAY_CHOP"
    AFTERNOON_ROTATION = "AFTERNOON_ROTATION"
    CLOSING_RISK = "CLOSING_RISK"
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass(frozen=True)
class StrategyMarketContext:
    market_context_id: str = ""
    market_context_generation: str = ""
    market_context_source: str = ""
    market_context_schema_version: str = ""
    market_side: str = "UNKNOWN"
    market_side_resolution_status: str = "UNRESOLVED"
    side_market_regime: str = "DATA_WAIT"
    counterpart_market_side: str = "UNKNOWN"
    counterpart_market_regime: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    global_market_regime: str = "DATA_WAIT"
    market_action: str = "DATA_WAIT"
    position_size_multiplier_hint: float = 0.0
    block_new_entry: bool = False
    index_return_pct: float = 0.0
    counterpart_index_return_pct: float = 0.0
    index_slope_1m_pct: float | None = None
    index_slope_3m_pct: float | None = None
    index_slope_5m_pct: float | None = None
    breadth_pct: float = 0.0
    breadth_trust_level: str = "LOW"
    turnover_weighted_return_pct: float = 0.0
    risk_score: float = 0.0
    counterpart_risk_score: float = 0.0
    calculated_at: str = ""
    freshness_status: str = "DATA_WAIT"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyThemeContext:
    theme_id: str = ""
    theme_name: str = ""
    theme_state: str = "DATA_WAIT"
    previous_theme_state: str = ""
    theme_transition: str = ""
    theme_score: float = 0.0
    theme_score_delta: float = 0.0
    persistence_count: int = 0
    leader_symbol: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    leader_changed: bool = False
    leader_stability_count: int = 0
    leadership_status: str = ""
    leadership_score: float = 0.0
    leadership_rank: int = 0
    leadership_rank_delta: int = 0
    leadership_entry_policy: str = "USE_THEME_ROLE_POLICY"
    leadership_block_new_entry: bool = False
    leadership_wait_new_entry: bool = False
    leadership_reason_codes: tuple[str, ...] = ()
    strong_count: int = 0
    leader_count: int = 0
    breadth_ratio: float = 0.0
    weighted_return_pct: float = 0.0
    leader_concentration: float = 0.0
    coverage_ratio: float = 0.0
    data_quality_status: str = "DATA_WAIT"
    calculated_at: str = ""
    freshness_status: str = "DATA_WAIT"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyStockContext:
    code: str = ""
    name: str = ""
    raw_stock_role: str = ""
    trade_stock_role: str = ""
    role_score: float = 0.0
    source_rank: int = 0
    relative_strength_vs_index_pct: float = 0.0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    turnover_speed: float = 0.0
    execution_strength: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    vwap: float = 0.0
    price_vs_vwap_pct: float = 0.0
    vwap_position: str = "UNKNOWN"
    momentum_alignment: str = "UNKNOWN"
    relative_strength_band: str = "LT_2"
    pullback_from_high_pct: float = 0.0
    vi_active: bool = False
    upper_limit_near: bool = False
    overheated: bool = False
    stock_data_quality_status: str = "DATA_WAIT"
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyDataContext:
    realtime_tick_available: bool = False
    realtime_tick_age_sec: float = 0.0
    realtime_tick_fresh: bool = False
    price_source: str = ""
    candle_1m_count: int = 0
    candle_3m_count: int = 0
    candle_5m_count: int = 0
    vwap_ready: bool = False
    day_high_low_ready: bool = False
    turnover_ready: bool = False
    theme_context_fresh: bool = False
    market_context_fresh: bool = False
    data_quality_status: str = "DATA_WAIT"
    blocking_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyRiskContext:
    market_block_new_entry: bool = False
    theme_entry_allowed: bool = False
    trade_role_entry_allowed: bool = False
    overheat_block: bool = False
    vi_block: bool = False
    chase_risk: bool = False
    stale_data_block: bool = False
    leadership_block_new_entry: bool = False
    leadership_wait_new_entry: bool = False
    leadership_entry_policy: str = "USE_THEME_ROLE_POLICY"
    position_size_multiplier_hint: float = 0.0
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyContextSnapshot:
    context_id: str
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    code: str
    session_phase: str
    market: StrategyMarketContext
    theme: StrategyThemeContext
    stock: StrategyStockContext
    data: StrategyDataContext
    risk: StrategyRiskContext
    selected_theme_id: str = ""
    previous_selected_theme_id: str = ""
    theme_selection_changed: bool = False
    theme_selection_reason: str = ""
    alternative_theme_ids: tuple[str, ...] = ()
    resolver_version: str = "best_theme_context_v1"
    selected_theme_leadership_status: str = ""
    selected_theme_leadership_score: float = 0.0
    selected_theme_rank: int = 0
    selected_theme_rank_delta: int = 0
    source_versions: dict[str, str] = field(default_factory=dict)
    source_timestamps: dict[str, str] = field(default_factory=dict)
    context_fresh: bool = False
    blocking_stage: str = "DATA"
    primary_reason_code: str = "STRATEGY_CONTEXT_V3_DATA_WAIT"
    reason_codes: tuple[str, ...] = ()
    ready_allowed: bool = False
    order_intent_allowed: bool = False
    live_order_allowed: bool = False
    schema_version: str = STRATEGY_CONTEXT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class StrategyContextAssembler:
    def __init__(
        self,
        db: Any,
        *,
        market_data: MarketDataStore | None = None,
        candle_builder: Any | None = None,
        best_theme_context_resolver: BestThemeContextResolver | None = None,
        state_contract: CandidateStateContractService | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.best_theme_context_resolver = best_theme_context_resolver or BestThemeContextResolver()
        self.state_contract = state_contract or CandidateStateContractService(db, clock=clock or datetime.now)
        self.clock = clock or datetime.now

    def assemble_candidate(
        self,
        candidate: Candidate,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        market_context: Any | None = None,
        theme_board: Mapping[str, Any] | None = None,
    ) -> StrategyContextSnapshot:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or candidate.trade_date or current.date().isoformat()
        code = normalize_code(candidate.code)
        market_payload = market_context if market_context is not None else self._latest_market_context(trade_date)
        theme_payload = dict(theme_board or self._latest_theme_board(trade_date) or {})
        tick = self.market_data.latest_tick(code) if self.market_data is not None else None
        previous_selected_theme_id = self._previous_selected_theme_id(trade_date, code)
        best_theme = self.best_theme_context_resolver.resolve(
            code,
            theme_board=theme_payload,
            previous_selected_theme_id=previous_selected_theme_id,
        )
        stock_payload = dict(best_theme.stock or _stock_payload_for_code(theme_payload, code) or {})
        theme_payload_for_stock = dict(best_theme.theme or _theme_payload_for_stock(theme_payload, stock_payload, candidate) or {})

        market = _market_context(candidate, market_payload, code)
        theme = _theme_context(theme_payload_for_stock, stock_payload, theme_payload)
        stock = _stock_context(candidate, stock_payload, tick, market)
        data = _data_context(code, tick, theme=theme, market=market, candle_builder=self.candle_builder)
        risk = _risk_context(market, theme, stock, data)
        reason_codes = _dedupe(
            [
                *market.reason_codes,
                *theme.reason_codes,
                *stock.reason_codes,
                *data.blocking_reason_codes,
                *risk.reason_codes,
                "STRATEGY_CONTEXT_V3_OBSERVE_ONLY",
            ]
        )
        blocking_stage, primary = _blocking_stage(data, market, theme, stock, risk, reason_codes)
        context_id = _context_id(
            candidate_id=candidate.id,
            code=code,
            market_at=market.calculated_at,
            theme_at=theme.calculated_at,
            role=stock.trade_stock_role,
            theme_state=theme.theme_state,
            selected_theme_id=best_theme.selected_theme_id,
            leadership_status=theme.leadership_status,
            leadership_entry_policy=theme.leadership_entry_policy,
            market_context_id=market.market_context_id,
        )
        return StrategyContextSnapshot(
            context_id=context_id,
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            candidate_id=candidate.id,
            code=code,
            selected_theme_id=best_theme.selected_theme_id,
            previous_selected_theme_id=best_theme.previous_selected_theme_id,
            theme_selection_changed=best_theme.theme_selection_changed,
            theme_selection_reason=best_theme.selected_reason,
            alternative_theme_ids=best_theme.alternative_theme_ids,
            resolver_version=best_theme.resolver_version,
            selected_theme_leadership_status=theme.leadership_status,
            selected_theme_leadership_score=theme.leadership_score,
            selected_theme_rank=_int(theme_payload_for_stock.get("theme_rank") or theme_payload_for_stock.get("leadership_rank")),
            selected_theme_rank_delta=theme.leadership_rank_delta,
            session_phase=session_phase(current).value,
            market=market,
            theme=theme,
            stock=stock,
            data=data,
            risk=risk,
            source_versions={
                "strategy_context": STRATEGY_CONTEXT_SCHEMA_VERSION,
                "theme_core": str(theme_payload.get("output_mode") or ""),
                "market_regime": str(_payload_get(market_payload, "output_mode", "")),
                "market_context_source": market.market_context_source,
                "market_context_schema": market.market_context_schema_version,
            },
            source_timestamps={
                "market_context_at": market.calculated_at,
                "market_context_id": market.market_context_id,
                "market_context_generation": market.market_context_generation,
                "theme_context_at": theme.calculated_at,
            },
            context_fresh=bool(data.theme_context_fresh and data.market_context_fresh),
            blocking_stage=blocking_stage,
            primary_reason_code=primary,
            reason_codes=tuple(reason_codes),
            ready_allowed=False,
            order_intent_allowed=False,
            live_order_allowed=False,
        )

    def assemble_active_candidates(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        market_context: Any | None = None,
        theme_board: Mapping[str, Any] | None = None,
        save: bool = True,
    ) -> list[StrategyContextSnapshot]:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        market_payload = market_context if market_context is not None else self._latest_market_context(trade_date)
        theme_payload = dict(theme_board or self._latest_theme_board(trade_date) or {})
        candidates = [
            candidate
            for candidate in list(self.db.list_candidates(trade_date=trade_date) or [])
            if self._evaluation_eligible(candidate)
        ]
        snapshots = [
            self.assemble_candidate(
                candidate,
                trade_date=trade_date,
                now=current,
                market_context=market_payload,
                theme_board=theme_payload,
            )
            for candidate in candidates
        ]
        if save:
            self.save_snapshots(candidates, snapshots, calculated_at=current.isoformat())
        return snapshots

    def _evaluation_eligible(self, candidate: Candidate) -> bool:
        if getattr(self.state_contract, "enabled", False):
            return bool(self.state_contract.reconcile_candidate(candidate).evaluation_eligible)
        return not self.state_contract.is_terminal(candidate)

    def save_snapshots(
        self,
        candidates: Iterable[Candidate],
        snapshots: Iterable[StrategyContextSnapshot],
        *,
        calculated_at: str,
    ) -> int:
        candidate_by_key = {(candidate.trade_date, normalize_code(candidate.code)): candidate for candidate in candidates}
        snapshot_list = list(snapshots)
        saver = getattr(self.db, "save_strategy_context_snapshot", None)
        saved = 0
        for snapshot in snapshot_list:
            payload = snapshot.to_dict()
            if callable(saver):
                saver(payload)
                saved += 1
            candidate = candidate_by_key.get((snapshot.trade_date, snapshot.code))
            if candidate is None:
                continue
            metadata = dict(candidate.metadata or {})
            metadata["strategy_context_v3"] = payload
            metadata["strategy_context_version"] = STRATEGY_CONTEXT_SCHEMA_VERSION
            metadata["strategy_context_id"] = snapshot.context_id
            metadata["selected_theme_id"] = snapshot.selected_theme_id
            metadata["previous_selected_theme_id"] = snapshot.previous_selected_theme_id
            metadata["theme_selection_changed"] = snapshot.theme_selection_changed
            metadata["session_phase"] = snapshot.session_phase
            metadata["blocking_stage"] = snapshot.blocking_stage
            metadata["primary_reason_code"] = snapshot.primary_reason_code
            metadata["context_fresh"] = snapshot.context_fresh
            metadata["updated_by_strategy_context_at"] = calculated_at
            candidate.metadata = metadata
            self.db.save_candidate(candidate)
        return saved

    def _latest_market_context(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_market_regime_snapshot", None)
        return dict(loader(trade_date=trade_date) or {}) if callable(loader) else {}

    def _latest_theme_board(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_theme_board_snapshot", None)
        return dict(loader(trade_date=trade_date) or {}) if callable(loader) else {}

    def _previous_selected_theme_id(self, trade_date: str, code: str) -> str:
        loader = getattr(self.db, "latest_strategy_context", None)
        if not callable(loader):
            return ""
        previous = dict(loader(trade_date=trade_date, code=code) or {})
        if not previous:
            return ""
        return str(previous.get("selected_theme_id") or dict(previous.get("theme") or {}).get("theme_id") or "")


@dataclass
class StrategyContextRuntimePipeline:
    db: Any
    market_data: MarketDataStore
    candle_builder: Any | None = None
    assembler: StrategyContextAssembler | None = None
    enabled: bool = True
    clock: Any = datetime.now

    def __post_init__(self) -> None:
        self.assembler = self.assembler or StrategyContextAssembler(
            self.db,
            market_data=self.market_data,
            candle_builder=self.candle_builder,
            clock=self.clock,
        )
        self.last_summary: dict[str, Any] = {"enabled": self.enabled, "status": "IDLE", "schema_version": STRATEGY_CONTEXT_SCHEMA_VERSION}
        self.last_result: list[dict[str, Any]] = []

    @property
    def config(self):
        return type("_StrategyContextPipelineConfig", (), {"enabled": self.enabled})()

    def run_if_due(
        self,
        now: datetime | None = None,
        *,
        market_context: Any | None = None,
        theme_board: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.run(now, market_context=market_context, theme_board=theme_board)

    def run(
        self,
        now: datetime | None = None,
        *,
        market_context: Any | None = None,
        theme_board: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.enabled:
            self.last_summary = {"enabled": False, "status": "DISABLED", "schema_version": STRATEGY_CONTEXT_SCHEMA_VERSION}
            return dict(self.last_summary)
        started = perf_counter()
        snapshots = self.assembler.assemble_active_candidates(
            trade_date=current.date().isoformat(),
            now=current,
            market_context=market_context,
            theme_board=theme_board,
            save=True,
        )
        rows = [snapshot.to_dict() for snapshot in snapshots]
        self.last_result = rows
        status_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        for row in rows:
            status_counts[str(row.get("primary_reason_code") or "")] = status_counts.get(str(row.get("primary_reason_code") or ""), 0) + 1
            stage_counts[str(row.get("blocking_stage") or "")] = stage_counts.get(str(row.get("blocking_stage") or ""), 0) + 1
        self.last_summary = {
            "enabled": True,
            "status": "OK",
            "schema_version": STRATEGY_CONTEXT_SCHEMA_VERSION,
            "calculated_at": current.isoformat(),
            "assembled_count": len(rows),
            "assembly_duration_ms": int(round((perf_counter() - started) * 1000)),
            "policy_lookup_count": len(rows),
            "market_context_source": str(_payload_get(market_context, "source", "")),
            "market_context_policy_count": int(getattr(market_context, "policy_count", 0) or 0),
            "context_fresh_count": sum(1 for row in rows if bool(row.get("context_fresh"))),
            "blocking_stage_counts": stage_counts,
            "top_reason_counts": status_counts,
            "ready_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
        }
        return dict(self.last_summary)


def session_phase(now: datetime) -> SessionPhase:
    current = now.time()
    if current < time(9, 0):
        return SessionPhase.PRE_OPEN
    if time(9, 0) <= current < time(9, 15):
        return SessionPhase.OPENING_DISCOVERY
    if time(9, 15) <= current < time(11, 0):
        return SessionPhase.MORNING_TREND
    if time(11, 0) <= current < time(13, 30):
        return SessionPhase.MIDDAY_CHOP
    if time(13, 30) <= current < time(14, 30):
        return SessionPhase.AFTERNOON_ROTATION
    if time(14, 30) <= current <= time(15, 20):
        return SessionPhase.CLOSING_RISK
    return SessionPhase.MARKET_CLOSED


def _market_context(candidate: Candidate, payload: Any, code: str) -> StrategyMarketContext:
    if not payload:
        return StrategyMarketContext(reason_codes=("MARKET_CONTEXT_NOT_READY",), block_new_entry=True)
    policy = _policy_payload(payload, code)
    side = str(_payload_get(policy, "market_side", "") or _candidate_side(candidate) or MarketSide.UNKNOWN.value).upper()
    side_snapshot = _side_snapshot(payload, side)
    status = str(_payload_get(policy, "market_status", "") or side_snapshot.get("status") or _side_status_from_payload(payload, side) or "DATA_WAIT").upper()
    global_status = str(_payload_get(policy, "global_market_status", "") or _payload_get(payload, "global_status", "DATA_WAIT"))
    counterpart_side = _counterpart_side(side)
    counterpart_snapshot = _side_snapshot(payload, counterpart_side)
    counterpart_status = str(counterpart_snapshot.get("status") or _side_status_from_payload(payload, counterpart_side) or "DATA_WAIT").upper()
    systemic = _bool(_payload_get(payload, "systemic_risk_off", False))
    if not _payload_contains(payload, "systemic_risk_off"):
        systemic, _systemic_reasons = systemic_risk_off_state(
            str(_payload_get(payload, "kospi_status", "") or _side_snapshot(payload, "KOSPI").get("status") or ""),
            str(_payload_get(payload, "kosdaq_status", "") or _side_snapshot(payload, "KOSDAQ").get("status") or ""),
            market_open=str(_payload_get(payload, "market_session_status", "")).lower() != "closed",
        )
    if policy:
        raw_action = str(_payload_get(policy, "market_action", "") or "").upper()
        normalized_action = normalize_market_action(
            raw_action,
            side_market_regime=status,
            global_market_regime=global_status,
            market_session_status=_payload_get(payload, "market_session_status", ""),
        )
        action = normalized_action.action
        multiplier = _float(_payload_get(policy, "position_size_multiplier_hint", None), _multiplier_for_action(action))
        block_new_entry = bool(_payload_get(policy, "block_new_entry", False)) or action in {"BLOCK_NEW_ENTRY", "MARKET_CLOSED", "DATA_WAIT"}
        policy_reasons = [*list(_payload_get(policy, "reason_codes", ()) or []), *list(normalized_action.reason_codes)]
    else:
        action, multiplier, block_new_entry, _wait_reason, policy_reasons = _fallback_market_policy(payload, side, status)
        normalized_action = normalize_market_action(
            action,
            side_market_regime=status,
            global_market_regime=global_status,
            market_session_status=_payload_get(payload, "market_session_status", ""),
        )
        action = normalized_action.action
        policy_reasons = [*list(policy_reasons or []), *list(normalized_action.reason_codes)]
    reasons = _dedupe([*list(_payload_get(payload, "reason_codes", ()) or []), *policy_reasons])
    if not reasons:
        reasons = ["MARKET_CONTEXT_READY"]
    return StrategyMarketContext(
        market_context_id=str(_payload_get(payload, "market_context_id", "") or ""),
        market_context_generation=str(_payload_get(payload, "market_context_generation", "") or ""),
        market_context_source=str(_payload_get(payload, "source", "") or ""),
        market_context_schema_version=str(_payload_get(payload, "schema_version", "") or ""),
        market_side=side,
        market_side_resolution_status=str(_payload_get(policy, "market_side_resolution_status", "") or ("RESOLVED" if side in {"KOSPI", "KOSDAQ"} else "UNRESOLVED")),
        side_market_regime=status,
        counterpart_market_side=counterpart_side,
        counterpart_market_regime=counterpart_status,
        composite_market_mode=str(_payload_get(payload, "composite_market_mode", "DATA_DEGRADED") or "DATA_DEGRADED"),
        systemic_risk_off=systemic,
        global_market_regime=global_status,
        market_action=action,
        position_size_multiplier_hint=multiplier,
        block_new_entry=block_new_entry,
        index_return_pct=_float(_mapping_value_or(side_snapshot, "index_return_pct", _side_value_from_payload(payload, side, "return_pct"))),
        counterpart_index_return_pct=_float(
            _mapping_value_or(counterpart_snapshot, "index_return_pct", _side_value_from_payload(payload, counterpart_side, "return_pct"))
        ),
        index_slope_1m_pct=_optional_float(side_snapshot.get("index_slope_1m_pct")),
        index_slope_3m_pct=_optional_float(side_snapshot.get("index_slope_3m_pct")),
        index_slope_5m_pct=_optional_float(side_snapshot.get("index_slope_5m_pct")),
        breadth_pct=_float(_mapping_value_or(side_snapshot, "breadth_pct", _side_value_from_payload(payload, side, "breadth_pct"))),
        breadth_trust_level="LOW" if "LOW_TRUST_BREADTH" in set(side_snapshot.get("data_quality_flags") or []) else "NORMAL",
        turnover_weighted_return_pct=_float(side_snapshot.get("turnover_weighted_return_pct")),
        risk_score=_float(side_snapshot.get("risk_score")),
        counterpart_risk_score=_float(counterpart_snapshot.get("risk_score")),
        calculated_at=str(_payload_get(payload, "calculated_at", "") or ""),
        freshness_status="FRESH" if str(_payload_get(payload, "calculated_at", "") or "") else "DATA_WAIT",
        reason_codes=tuple(reasons),
    )


def _theme_context(theme: Mapping[str, Any], stock: Mapping[str, Any], board: Mapping[str, Any]) -> StrategyThemeContext:
    if not theme:
        return StrategyThemeContext(calculated_at=str(board.get("calculated_at") or ""), reason_codes=("THEME_CONTEXT_NOT_READY",))
    reasons = _dedupe([*list(theme.get("reason_codes") or []), *list(stock.get("reason_codes") or [])])
    policy = _leadership_entry_policy(theme)
    reasons = _dedupe([*reasons, *policy["reason_codes"]])
    return StrategyThemeContext(
        theme_id=str(theme.get("theme_id") or stock.get("theme_id") or ""),
        theme_name=str(theme.get("theme_name") or stock.get("theme_name") or ""),
        theme_state=str(theme.get("theme_state") or theme.get("theme_status") or "DATA_WAIT"),
        previous_theme_state=str(theme.get("previous_theme_state") or theme.get("previous_state") or ""),
        theme_transition=str(theme.get("theme_transition") or theme.get("transition") or ""),
        theme_score=_float(theme.get("theme_score")),
        theme_score_delta=_float(theme.get("theme_score_delta")),
        persistence_count=_int(theme.get("persistence_count")),
        leader_symbol=normalize_code(theme.get("leader_symbol") or ""),
        co_leader_symbols=tuple(normalize_code(item) for item in list(theme.get("co_leader_symbols") or []) if normalize_code(item)),
        leader_changed=bool(theme.get("leader_changed")),
        leader_stability_count=_int(theme.get("leader_stability_count")),
        leadership_status=str(theme.get("leadership_status") or ""),
        leadership_score=_float(theme.get("leadership_score")),
        leadership_rank=_int(theme.get("leadership_rank") or theme.get("theme_rank")),
        leadership_rank_delta=_int(theme.get("leadership_rank_delta") or theme.get("rank_delta")),
        leadership_entry_policy=policy["policy"],
        leadership_block_new_entry=bool(policy["block_new_entry"]),
        leadership_wait_new_entry=bool(policy["wait_new_entry"]),
        leadership_reason_codes=tuple(policy["reason_codes"]),
        strong_count=_int(theme.get("strong_count")),
        leader_count=_int(theme.get("leader_count")),
        breadth_ratio=_float(theme.get("breadth_ratio") or theme.get("strong_ratio")),
        weighted_return_pct=_float(theme.get("weighted_return_pct")),
        leader_concentration=_float(theme.get("leader_concentration")),
        coverage_ratio=_float(theme.get("coverage_ratio")),
        data_quality_status=str(theme.get("data_quality_status") or theme.get("data_quality_reason") or "OK"),
        calculated_at=str(board.get("calculated_at") or theme.get("calculated_at") or ""),
        freshness_status="FRESH" if board.get("calculated_at") else "DATA_WAIT",
        reason_codes=tuple(reasons or ["THEME_CONTEXT_READY"]),
    )


def _stock_context(candidate: Candidate, stock: Mapping[str, Any], tick: StrategyTick | None, market: StrategyMarketContext) -> StrategyStockContext:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    raw_role = str(stock.get("raw_role") or stock.get("stock_role") or "")
    trade_role = str(stock.get("trade_role") or stock.get("stock_role") or "")
    price = float(getattr(tick, "price", 0) or 0)
    day_high = _float(metadata.get("day_high") or metadata.get("session_high"))
    pullback = ((day_high - price) / day_high) * 100.0 if day_high > 0 and price > 0 else _float(metadata.get("pullback_from_high_pct"))
    index_return = market.index_return_pct
    change = _float(getattr(tick, "change_rate", 0.0) if tick is not None else stock.get("change_rate_pct"))
    vwap = _float(metadata.get("vwap"))
    price_vs_vwap = round(((price - vwap) / vwap) * 100.0, 4) if price > 0 and vwap > 0 else 0.0
    momentum_values = [_float(metadata.get(key)) for key in ("momentum_1m", "momentum_3m", "momentum_5m")]
    relative_strength = round(change - index_return, 4)
    reasons = list(stock.get("reason_codes") or [])
    if not stock:
        reasons.append("STOCK_ROLE_CONTEXT_NOT_READY")
    return StrategyStockContext(
        code=normalize_code(candidate.code),
        name=candidate.name or str(stock.get("name") or metadata.get("stock_name") or ""),
        raw_stock_role=raw_role,
        trade_stock_role=trade_role,
        role_score=_float(stock.get("stock_score") or stock.get("role_score")),
        source_rank=_int(stock.get("source_rank")),
        relative_strength_vs_index_pct=relative_strength,
        change_rate_pct=change,
        turnover_krw=_float(getattr(tick, "trade_value", 0.0) if tick is not None else stock.get("turnover_krw")),
        turnover_speed=_float(metadata.get("turnover_speed") or metadata.get("turnover_krw_per_min")),
        execution_strength=_float(getattr(tick, "execution_strength", 0.0) if tick is not None else stock.get("execution_strength")),
        momentum_1m=momentum_values[0],
        momentum_3m=momentum_values[1],
        momentum_5m=momentum_values[2],
        vwap=vwap,
        price_vs_vwap_pct=price_vs_vwap,
        vwap_position="ABOVE" if price > 0 and vwap > 0 and price >= vwap else "BELOW" if price > 0 and vwap > 0 else "UNKNOWN",
        momentum_alignment=_momentum_alignment(momentum_values),
        relative_strength_band=_relative_strength_band(relative_strength),
        pullback_from_high_pct=round(pullback, 4),
        vi_active=bool(metadata.get("vi_active")),
        upper_limit_near=bool(metadata.get("upper_limit_near")) or _float(metadata.get("upper_limit_gap_pct"), 100.0) <= 3.0,
        overheated=bool(metadata.get("overheated")) or raw_role == "OVERHEATED" or trade_role == "OVERHEATED_BLOCKED",
        stock_data_quality_status="OK" if tick is not None and price > 0 else "DATA_WAIT",
        reason_codes=tuple(_dedupe(reasons)),
    )


def _data_context(
    code: str,
    tick: StrategyTick | None,
    *,
    theme: StrategyThemeContext,
    market: StrategyMarketContext,
    candle_builder: Any | None,
) -> StrategyDataContext:
    reasons: list[str] = []
    tick_age = 0.0
    fresh = False
    price_source = ""
    if tick is None:
        reasons.append("LATEST_TICK_MISSING")
    else:
        tick_age = max(0.0, (datetime.now(tick.timestamp.tzinfo) - tick.timestamp).total_seconds()) if tick.timestamp.tzinfo else 0.0
        metadata = dict(tick.metadata or {})
        price_source = str(metadata.get("price_source") or "REALTIME")
        fresh = price_source.upper() != "TR_BACKFILL" and int(tick.price or 0) > 0
        if not fresh:
            reasons.append("LATEST_TICK_NOT_FRESH")
    candle_counts = _candle_counts(candle_builder, code)
    vwap_ready = bool(tick is not None and _float(dict(tick.metadata or {}).get("vwap")) > 0)
    high_low_ready = bool(tick is not None and (_float(dict(tick.metadata or {}).get("day_high")) > 0 or _float(dict(tick.metadata or {}).get("session_high")) > 0))
    turnover_ready = bool(tick is not None and float(tick.trade_value or 0.0) > 0)
    theme_fresh = theme.freshness_status == "FRESH" and theme.theme_state not in {"", "DATA_WAIT"}
    market_fresh = market.freshness_status == "FRESH" and market.market_action != "DATA_WAIT"
    if not theme_fresh:
        reasons.append("THEME_CONTEXT_NOT_READY")
    if not market_fresh:
        reasons.append("MARKET_CONTEXT_NOT_READY")
    return StrategyDataContext(
        realtime_tick_available=tick is not None,
        realtime_tick_age_sec=round(tick_age, 3),
        realtime_tick_fresh=fresh,
        price_source=price_source,
        candle_1m_count=candle_counts.get(1, 0),
        candle_3m_count=candle_counts.get(3, 0),
        candle_5m_count=candle_counts.get(5, 0),
        vwap_ready=vwap_ready,
        day_high_low_ready=high_low_ready,
        turnover_ready=turnover_ready,
        theme_context_fresh=theme_fresh,
        market_context_fresh=market_fresh,
        data_quality_status="OK" if not reasons else "DATA_WAIT",
        blocking_reason_codes=tuple(_dedupe(reasons)),
    )


def _risk_context(
    market: StrategyMarketContext,
    theme: StrategyThemeContext,
    stock: StrategyStockContext,
    data: StrategyDataContext,
) -> StrategyRiskContext:
    theme_allowed = theme.theme_state in {"LEADING_THEME", "SPREADING_THEME", "LEADER_ONLY_THEME"}
    trade_role_allowed = stock.trade_stock_role in {"LEADER_CONFIRMED", "CO_LEADER_CONFIRMED"} or (
        stock.trade_stock_role == "FOLLOWER_ALLOWED" and market.side_market_regime == "EXPANSION"
    )
    reasons: list[str] = []
    if market.block_new_entry:
        reasons.append("MARKET_BLOCK_NEW_ENTRY")
    if not theme_allowed:
        reasons.append("THEME_NOT_ENTRY_ALLOWED")
    if not trade_role_allowed:
        reasons.append("TRADE_ROLE_NOT_ENTRY_ALLOWED")
    if stock.overheated:
        reasons.append("OVERHEATED_BLOCKED")
    if stock.vi_active:
        reasons.append("VI_ACTIVE_BLOCK")
    if not data.realtime_tick_fresh:
        reasons.append("STALE_DATA_BLOCK")
    if theme.leadership_block_new_entry:
        reasons.extend(theme.leadership_reason_codes or ("THEME_LEADERSHIP_BLOCK",))
    if theme.leadership_wait_new_entry:
        reasons.extend(theme.leadership_reason_codes or ("THEME_LEADERSHIP_WAIT",))
    return StrategyRiskContext(
        market_block_new_entry=market.block_new_entry,
        theme_entry_allowed=theme_allowed,
        trade_role_entry_allowed=trade_role_allowed,
        overheat_block=stock.overheated,
        vi_block=stock.vi_active,
        chase_risk=stock.pullback_from_high_pct < 0.3 if stock.pullback_from_high_pct else False,
        stale_data_block=not data.realtime_tick_fresh,
        leadership_block_new_entry=theme.leadership_block_new_entry,
        leadership_wait_new_entry=theme.leadership_wait_new_entry,
        leadership_entry_policy=theme.leadership_entry_policy,
        position_size_multiplier_hint=market.position_size_multiplier_hint,
        reason_codes=tuple(_dedupe(reasons)),
    )


def _blocking_stage(
    data: StrategyDataContext,
    market: StrategyMarketContext,
    theme: StrategyThemeContext,
    stock: StrategyStockContext,
    risk: StrategyRiskContext,
    reasons: list[str],
) -> tuple[str, str]:
    if not data.realtime_tick_fresh or not data.market_context_fresh or not data.theme_context_fresh:
        return "DATA", _primary_reason(reasons, "STRATEGY_CONTEXT_V3_DATA_WAIT")
    if market.block_new_entry or market.market_action in {"BLOCK_NEW_ENTRY", "MARKET_CLOSED"}:
        return "MARKET", _primary_reason(reasons, "MARKET_BLOCK_NEW_ENTRY")
    if risk.leadership_block_new_entry or risk.leadership_wait_new_entry:
        return "THEME", _primary_reason(theme.leadership_reason_codes or risk.reason_codes, "THEME_LEADERSHIP_WAIT")
    if not risk.theme_entry_allowed:
        return "THEME", _primary_reason(reasons, "THEME_NOT_ENTRY_ALLOWED")
    if not risk.trade_role_entry_allowed:
        return "ROLE", _primary_reason(reasons, "TRADE_ROLE_NOT_ENTRY_ALLOWED")
    if risk.overheat_block or risk.vi_block or risk.chase_risk:
        return "RISK", _primary_reason(reasons, "RISK_BLOCK")
    return "PRICE", "WAIT_PRICE_TIMING"


def _stock_payload_for_code(board: Mapping[str, Any], code: str) -> dict[str, Any]:
    grouped = board.get("stock_contexts_by_code")
    if isinstance(grouped, Mapping):
        items = [dict(item or {}) for item in list(grouped.get(code) or [])]
        if items:
            return items[0]
    for item in list(board.get("stocks") or []):
        stock = dict(item or {})
        if normalize_code(stock.get("code") or "") == code:
            return stock
    return {}


def _theme_payload_for_stock(board: Mapping[str, Any], stock: Mapping[str, Any], candidate: Candidate) -> dict[str, Any]:
    theme_id = str(stock.get("theme_id") or "")
    if not theme_id:
        theme_ids = list(candidate.theme_ids or [])
        theme_id = str(theme_ids[0]) if theme_ids else str(dict(candidate.metadata or {}).get("best_theme_id") or "")
    themes_by_id = board.get("themes_by_id")
    if isinstance(themes_by_id, Mapping) and theme_id:
        theme = dict(themes_by_id.get(theme_id) or {})
        if theme:
            return theme
    for item in list(board.get("top_themes") or []):
        theme = dict(item or {})
        if str(theme.get("theme_id") or "") == theme_id:
            return theme
    return {}


def _payload_get(payload: Any, key: str, default: Any = None) -> Any:
    if payload is None:
        return default
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    getter = getattr(payload, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return getter(key)
    return getattr(payload, key, default)


def _payload_contains(payload: Any, key: str) -> bool:
    if payload is None:
        return False
    if isinstance(payload, Mapping):
        return key in payload
    if hasattr(payload, key):
        return True
    keys = getattr(payload, "keys", None)
    return bool(callable(keys) and key in keys())


def _policy_payload(payload: Any, code: str) -> Any:
    policy_for = getattr(payload, "policy_for", None)
    if callable(policy_for):
        return policy_for(code)
    policies = _payload_get(payload, "candidate_policy_by_code", {}) or {}
    if isinstance(policies, Mapping):
        return policies.get(normalize_code(code)) or policies.get(str(code or "")) or {}
    return {}


def _side_snapshot(payload: Any, side: str) -> dict[str, Any]:
    side_context = getattr(payload, "side_context", None)
    if callable(side_context):
        context = side_context(side)
        to_dict = getattr(context, "to_dict", None)
        if callable(to_dict):
            return dict(to_dict() or {})
    key = f"{str(side or '').lower()}_snapshot"
    snapshot = dict(_payload_get(payload, key, {}) or {})
    if snapshot:
        return snapshot
    if side == "KOSPI":
        return dict(_payload_get(payload, "kospi_snapshot", {}) or {})
    if side == "KOSDAQ":
        return dict(_payload_get(payload, "kosdaq_snapshot", {}) or {})
    return {}


def _counterpart_side(side: str) -> str:
    if str(side or "").upper() == MarketSide.KOSPI.value:
        return MarketSide.KOSDAQ.value
    if str(side or "").upper() == MarketSide.KOSDAQ.value:
        return MarketSide.KOSPI.value
    return MarketSide.UNKNOWN.value


def _side_status_from_payload(payload: Any, side: str) -> str:
    side_status = getattr(payload, "side_status", None)
    if callable(side_status):
        return str(side_status(side) or "")
    normalized = str(side or "").upper()
    if normalized == MarketSide.KOSPI.value:
        return str(_payload_get(payload, "kospi_status", "") or "")
    if normalized == MarketSide.KOSDAQ.value:
        return str(_payload_get(payload, "kosdaq_status", "") or "")
    return ""


def _side_value_from_payload(payload: Any, side: str, suffix: str) -> Any:
    side_context = getattr(payload, "side_context", None)
    if callable(side_context):
        context = side_context(side)
        attr_by_suffix = {
            "return_pct": "index_return_pct",
            "breadth_pct": "breadth_pct",
        }
        attr = attr_by_suffix.get(str(suffix or ""), str(suffix or ""))
        return _payload_get(context, attr)
    normalized = str(side or "").upper()
    if normalized == MarketSide.KOSPI.value:
        return _payload_get(payload, f"kospi_{suffix}")
    if normalized == MarketSide.KOSDAQ.value:
        return _payload_get(payload, f"kosdaq_{suffix}")
    return None


def _mapping_value_or(mapping: Mapping[str, Any], key: str, fallback: Any) -> Any:
    value = mapping.get(key)
    return fallback if value in (None, "") else value


def _candidate_side(candidate: Candidate) -> str:
    metadata = dict(candidate.metadata or {})
    return str(candidate.market or metadata.get("market") or metadata.get("market_side") or "UNKNOWN").upper()


def _market_action_from_status(status: str, global_status: str) -> str:
    if status == "MARKET_CLOSED" or global_status == "MARKET_CLOSED":
        return "MARKET_CLOSED"
    if status == "RISK_OFF":
        return "BLOCK_NEW_ENTRY"
    if status == "WEAK":
        return "WAIT_MARKET"
    if status == "CHOPPY":
        return "WAIT_MARKET"
    if status == "EXPANSION":
        return "ALLOW_NORMAL"
    if status == "SELECTIVE":
        return "ALLOW_REDUCED"
    return "DATA_WAIT" if status == "DATA_WAIT" else "WAIT_MARKET"


def _fallback_market_policy(payload: Any, side: str, status: str) -> tuple[str, float, bool, str, list[str]]:
    normalized_side = side if side in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value} else MarketSide.UNKNOWN.value
    if normalized_side == MarketSide.UNKNOWN.value:
        return (
            CandidateMarketAction.DATA_WAIT.value,
            0.0,
            True,
            "MARKET_SIDE_UNRESOLVED",
            ["MARKET_SIDE_UNKNOWN", "MARKET_SIDE_UNRESOLVED"],
        )
    market_open = str(_payload_get(payload, "market_session_status", "") or "").lower() != "closed"
    kospi_status = str(_payload_get(payload, "kospi_status", "") or _side_snapshot(payload, "KOSPI").get("status") or "")
    kosdaq_status = str(_payload_get(payload, "kosdaq_status", "") or _side_snapshot(payload, "KOSDAQ").get("status") or "")
    systemic = bool(_payload_get(payload, "systemic_risk_off", False))
    if not _payload_contains(payload, "systemic_risk_off"):
        systemic, _reasons = systemic_risk_off_state(kospi_status, kosdaq_status, market_open=market_open)
    counterpart = MarketSide.KOSDAQ.value if normalized_side == MarketSide.KOSPI.value else MarketSide.KOSPI.value
    counterpart_snapshot = _side_snapshot(payload, counterpart)
    counterpart_status = str(
        counterpart_snapshot.get("status")
        or (kosdaq_status if counterpart == MarketSide.KOSDAQ.value else kospi_status)
        or MarketRegimeStatus.DATA_WAIT.value
    )
    action, multiplier, block, wait_reason, reasons = market_policy_for_side(
        status or MarketRegimeStatus.DATA_WAIT.value,
        counterpart_status,
        market_open=market_open,
        systemic_risk_off=systemic,
        market_side_known=True,
    )
    return action.value, multiplier, block, wait_reason, reasons


def _multiplier_for_action(action: str) -> float:
    return {"ALLOW_NORMAL": 1.0, "ALLOW_REDUCED": 0.6, "WAIT_MARKET": 0.35}.get(str(action or ""), 0.0)


def _momentum_alignment(values: Iterable[float]) -> str:
    items = [float(value or 0.0) for value in values]
    if not items:
        return "UNKNOWN"
    if all(value > 0 for value in items):
        return "ALL_POSITIVE"
    if any(value > 0 for value in items) and all(value >= 0 for value in items):
        return "NON_NEGATIVE"
    if any(value > 0 for value in items):
        return "MIXED"
    return "NEGATIVE"


def _relative_strength_band(value: float) -> str:
    number = float(value or 0.0)
    if number < 2.0:
        return "LT_2"
    if number < 4.0:
        return "2_TO_4"
    if number < 6.0:
        return "4_TO_6"
    return "GE_6"


def _candle_counts(candle_builder: Any | None, code: str) -> dict[int, int]:
    counts = {1: 0, 3: 0, 5: 0}
    if candle_builder is None:
        return counts
    for interval in counts:
        try:
            active = 1 if candle_builder.active_candle(code) is not None and interval == 1 else 0
            counts[interval] = len(candle_builder.completed_candles(code, interval)) + active
        except Exception:
            counts[interval] = 0
    return counts


def _context_id(
    *,
    candidate_id: int | None,
    code: str,
    market_at: str,
    theme_at: str,
    role: str,
    theme_state: str,
    selected_theme_id: str = "",
    leadership_status: str = "",
    leadership_entry_policy: str = "",
    market_context_id: str = "",
) -> str:
    raw = "|".join(
        [
            STRATEGY_CONTEXT_SCHEMA_VERSION,
            str(candidate_id or ""),
            normalize_code(code),
            str(selected_theme_id or ""),
            str(market_context_id or ""),
            str(market_at or ""),
            str(theme_at or ""),
            str(role or ""),
            str(theme_state or ""),
            str(leadership_status or ""),
            str(leadership_entry_policy or ""),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _leadership_entry_policy(theme: Mapping[str, Any]) -> dict[str, Any]:
    status = str(theme.get("leadership_status") or "").upper()
    theme_state = str(theme.get("theme_state") or theme.get("theme_status") or "").upper()
    if status in {"INCUMBENT", "TAKEOVER_CONFIRMED"}:
        if theme_state == "DATA_WAIT":
            return {"policy": "BLOCK_DATA_WAIT", "block_new_entry": True, "wait_new_entry": False, "reason_codes": ["THEME_LEADERSHIP_DATA_WAIT_BLOCK"]}
        return {"policy": "USE_THEME_ROLE_POLICY", "block_new_entry": False, "wait_new_entry": False, "reason_codes": []}
    if status == "CHALLENGER":
        return {"policy": "WAIT_CHALLENGER", "block_new_entry": False, "wait_new_entry": True, "reason_codes": ["THEME_CHALLENGER_WAIT"]}
    if status == "TAKEOVER_PENDING":
        return {"policy": "WAIT_TAKEOVER_PENDING", "block_new_entry": False, "wait_new_entry": True, "reason_codes": ["THEME_TAKEOVER_PENDING_WAIT"]}
    if status == "LOSING_LEADERSHIP":
        return {"policy": "BLOCK_LOSING_LEADERSHIP", "block_new_entry": True, "wait_new_entry": False, "reason_codes": ["THEME_LOSING_LEADERSHIP_BLOCK"]}
    if status == "ROTATED_OUT":
        return {"policy": "HARD_BLOCK_ROTATED_OUT", "block_new_entry": True, "wait_new_entry": False, "reason_codes": ["THEME_ROTATED_OUT_BLOCK"]}
    if status == "NEUTRAL" and theme_state not in {"LEADING_THEME", "SPREADING_THEME", "LEADER_ONLY_THEME"}:
        return {"policy": "WAIT_NEUTRAL_THEME", "block_new_entry": False, "wait_new_entry": True, "reason_codes": ["THEME_NEUTRAL_WAIT"]}
    return {"policy": "USE_THEME_ROLE_POLICY", "block_new_entry": False, "wait_new_entry": False, "reason_codes": []}


def _primary_reason(reasons: Iterable[str], default: str) -> str:
    for reason in reasons:
        text = str(reason or "")
        if text and not text.endswith("_READY"):
            return text
    return default


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return float(default)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _float(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or default).replace(",", "")))
    except (TypeError, ValueError):
        return int(default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = [
    "STRATEGY_CONTEXT_OUTPUT_MODE",
    "STRATEGY_CONTEXT_SCHEMA_VERSION",
    "SessionPhase",
    "StrategyContextAssembler",
    "StrategyContextRuntimePipeline",
    "StrategyContextSnapshot",
    "StrategyContextVersion",
    "StrategyDataContext",
    "StrategyMarketContext",
    "StrategyRiskContext",
    "StrategyStockContext",
    "StrategyThemeContext",
    "session_phase",
]
