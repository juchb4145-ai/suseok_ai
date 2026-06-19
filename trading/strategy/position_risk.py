from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick


POSITION_RISK_OUTPUT_MODE = "OBSERVE"
MARKET_SIDES = ("KOSPI", "KOSDAQ")
PENDING_BUY_STATUSES = {
    "RISK_APPROVED",
    "LOCAL_ORDER_CREATED",
    "COMMAND_BLOCKED_OBSERVE_ONLY",
    "COMMAND_QUEUED",
    "QUEUED_TO_GATEWAY",
    "ACKED_BY_GATEWAY",
    "COMMAND_ACKED",
    "PARTIALLY_FILLED",
    "RECONCILE_REQUIRED",
}


class PositionRiskStatus(str, Enum):
    OPEN = "OPEN"
    SCALE_OUT_PENDING = "SCALE_OUT_PENDING"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    DATA_WAIT = "DATA_WAIT"
    STALE_DATA_RISK = "STALE_DATA_RISK"


class MarketSideBudgetAction(str, Enum):
    ALLOW_BUDGET = "ALLOW_BUDGET"
    REDUCED_BUDGET = "REDUCED_BUDGET"
    STOP_NEW_ENTRY = "STOP_NEW_ENTRY"
    DATA_WAIT = "DATA_WAIT"
    MARKET_CLOSED = "MARKET_CLOSED"


class PositionMarketAction(str, Enum):
    HOLD = "HOLD"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    SCALE_OUT = "SCALE_OUT"
    EXIT_IF_LOSER = "EXIT_IF_LOSER"
    EXIT_NOW = "EXIT_NOW"
    DATA_WAIT = "DATA_WAIT"


@dataclass(frozen=True)
class PositionRuntimeSnapshot:
    trade_date: str
    calculated_at: str
    position_id: str
    candidate_id: int | None
    code: str
    name: str = ""
    theme_id: str = ""
    theme_name: str = ""
    source_type: str = "VIRTUAL"
    entry_price: int = 0
    quantity: int = 0
    remaining_quantity: int = 0
    avg_entry_price: float = 0.0
    opened_at: str = ""
    holding_minutes: int = 0
    current_price: int = 0
    current_return_pct: float = 0.0
    max_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    highest_price_since_entry: int = 0
    lowest_price_since_entry: int = 0
    realized_return_pct: float = 0.0
    unrealized_return_pct: float = 0.0
    stop_loss_price: int = 0
    take_profit_price: int = 0
    trailing_stop_price: int = 0
    trailing_active: bool = False
    first_profit_taken: bool = False
    last_tick_at: str = ""
    data_quality_flags: tuple[str, ...] = ()
    risk_status: PositionRiskStatus = PositionRiskStatus.OPEN
    market_side: str = "UNKNOWN"
    market_side_source: str = ""
    market_side_resolution_status: str = "UNRESOLVED"
    side_market_regime: str = "DATA_WAIT"
    counterpart_market_regime: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    market_context_calculated_at: str = ""
    market_context_fresh: bool = False
    candidate_market_action: str = "DATA_WAIT"
    strategy_context_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class PositionRiskConfig:
    enabled: bool = False
    interval_sec: int = 5
    max_tick_age_sec: int = 10
    stop_loss_pct: float = -2.0
    take_profit_1_pct: float = 5.0
    trailing_gap_pct: float = 1.2
    stop_new_entry_exposure_krw: int = 10_000_000
    reduce_drawdown_pct: float = -4.0
    kill_switch_drawdown_pct: float = -8.0
    portfolio_gross_exposure_limit_krw: int = 10_000_000
    kospi_exposure_limit_krw: int = 0
    kosdaq_exposure_limit_krw: int = 0
    kospi_max_open_positions: int = 3
    kosdaq_max_open_positions: int = 3
    market_side_pending_buy_included: bool = True
    market_side_portfolio_enabled: bool = False
    market_side_portfolio_observe_only: bool = True
    market_side_portfolio_enforce_buy_limits: bool = False
    market_side_budget_max_age_sec: int = 60
    position_market_context_max_age_sec: int = 60

    @classmethod
    def from_env(cls) -> "PositionRiskConfig":
        return cls(
            enabled=_env_bool("TRADING_POSITION_RISK_ENABLED", False),
            interval_sec=max(1, _env_int("TRADING_POSITION_RISK_INTERVAL_SEC", 5)),
            max_tick_age_sec=max(1, _env_int("TRADING_POSITION_RISK_MAX_TICK_AGE_SEC", 10)),
            stop_loss_pct=_env_float("TRADING_EXIT_STOP_LOSS_PCT", -2.0),
            take_profit_1_pct=_env_float("TRADING_EXIT_TAKE_PROFIT_1_PCT", 5.0),
            trailing_gap_pct=_env_float("TRADING_EXIT_TRAILING_GAP_PCT", 1.2),
            stop_new_entry_exposure_krw=max(0, _env_int("TRADING_POSITION_RISK_STOP_NEW_ENTRY_EXPOSURE_KRW", 10_000_000)),
            reduce_drawdown_pct=_env_float("TRADING_POSITION_RISK_REDUCE_DRAWDOWN_PCT", -4.0),
            kill_switch_drawdown_pct=_env_float("TRADING_POSITION_RISK_KILL_SWITCH_DRAWDOWN_PCT", -8.0),
            portfolio_gross_exposure_limit_krw=max(0, _env_int("TRADING_PORTFOLIO_GROSS_EXPOSURE_LIMIT_KRW", 10_000_000)),
            kospi_exposure_limit_krw=max(0, _env_int("TRADING_KOSPI_EXPOSURE_LIMIT_KRW", 0)),
            kosdaq_exposure_limit_krw=max(0, _env_int("TRADING_KOSDAQ_EXPOSURE_LIMIT_KRW", 0)),
            kospi_max_open_positions=max(0, _env_int("TRADING_KOSPI_MAX_OPEN_POSITIONS", 3)),
            kosdaq_max_open_positions=max(0, _env_int("TRADING_KOSDAQ_MAX_OPEN_POSITIONS", 3)),
            market_side_pending_buy_included=_env_bool("TRADING_MARKET_SIDE_PENDING_BUY_INCLUDED", True),
            market_side_portfolio_enabled=_env_bool("TRADING_MARKET_SIDE_PORTFOLIO_ENABLED", False),
            market_side_portfolio_observe_only=_env_bool("TRADING_MARKET_SIDE_PORTFOLIO_OBSERVE_ONLY", True),
            market_side_portfolio_enforce_buy_limits=_env_bool("TRADING_MARKET_SIDE_PORTFOLIO_ENFORCE_BUY_LIMITS", False),
            market_side_budget_max_age_sec=max(1, _env_int("TRADING_MARKET_SIDE_BUDGET_MAX_AGE_SEC", 60)),
            position_market_context_max_age_sec=max(1, _env_int("TRADING_POSITION_MARKET_CONTEXT_MAX_AGE_SEC", 60)),
        )


@dataclass(frozen=True)
class PositionRuntimeResult:
    trade_date: str
    calculated_at: str
    positions: tuple[PositionRuntimeSnapshot, ...] = ()
    saved: bool = False
    warnings: tuple[str, ...] = ()
    output_mode: str = POSITION_RISK_OUTPUT_MODE
    live_order_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class PositionRiskSnapshot:
    trade_date: str
    calculated_at: str
    position_id: str
    candidate_id: int | None
    code: str
    risk_status: str = PositionRiskStatus.OPEN.value
    risk_level: str = "NORMAL"
    stop_loss_distance_pct: float = 0.0
    take_profit_distance_pct: float = 0.0
    trailing_distance_pct: float = 0.0
    theme_risk_level: str = "NORMAL"
    market_risk_level: str = "NORMAL"
    data_risk_level: str = "NORMAL"
    market_side: str = "UNKNOWN"
    side_market_regime: str = "DATA_WAIT"
    counterpart_market_regime: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    position_market_action: str = PositionMarketAction.HOLD.value
    recommended_exit_ratio: float = 0.0
    recommended_trailing_gap_pct: float = 0.0
    position_action_reason_codes: tuple[str, ...] = ()
    market_context_fresh: bool = False
    structure_intact: bool = True
    support_broken: bool = False
    vwap_broken: bool = False
    theme_weak: bool = False
    leader_collapsed: bool = False
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class MarketSidePortfolioBudget:
    market_side: str
    side_market_regime: str = "DATA_WAIT"
    counterpart_market_regime: str = "DATA_WAIT"
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    base_exposure_limit_krw: int = 0
    effective_exposure_limit_krw: int = 0
    open_exposure_krw: int = 0
    pending_buy_exposure_krw: int = 0
    reserved_exposure_krw: int = 0
    available_exposure_krw: int = 0
    utilization_pct: float = 0.0
    open_position_count: int = 0
    pending_buy_count: int = 0
    max_open_positions: int = 0
    available_position_slots: int = 0
    budget_action: str = MarketSideBudgetAction.DATA_WAIT.value
    reason_codes: tuple[str, ...] = ()
    data_quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    trade_date: str
    calculated_at: str
    open_position_count: int = 0
    total_exposure: int = 0
    theme_exposure_by_theme: dict[str, int] = field(default_factory=dict)
    market_side_exposure: dict[str, int] = field(default_factory=dict)
    unrealized_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    daily_realized_pnl_pct: float = 0.0
    risk_level: str = "NORMAL"
    stop_new_entry_recommended: bool = False
    kill_switch_recommended: bool = False
    gross_exposure_limit_krw: int = 0
    gross_open_exposure_krw: int = 0
    gross_pending_buy_exposure_krw: int = 0
    gross_reserved_exposure_krw: int = 0
    gross_available_exposure_krw: int = 0
    gross_utilization_pct: float = 0.0
    composite_market_mode: str = "DATA_DEGRADED"
    systemic_risk_off: bool = False
    market_side_budgets: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop_new_entry_by_side: dict[str, bool] = field(default_factory=dict)
    reduce_only_by_side: dict[str, bool] = field(default_factory=dict)
    market_context_calculated_at: str = ""
    market_context_fresh: bool = False
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class PositionRiskResult:
    trade_date: str
    calculated_at: str
    position_risks: tuple[PositionRiskSnapshot, ...] = ()
    portfolio_risk: PortfolioRiskSnapshot | None = None
    saved: bool = False
    output_mode: str = POSITION_RISK_OUTPUT_MODE
    live_order_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class PositionRuntimeService:
    def __init__(
        self,
        db: Any,
        *,
        market_data: MarketDataStore | None = None,
        candle_builder: CandleBuilder | None = None,
        config: PositionRiskConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or PositionRiskConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        save: bool = True,
    ) -> PositionRuntimeResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        snapshots = tuple(self._dedupe_positions(self._virtual_positions(trade_date, current) + self._live_sim_positions(trade_date, current)))
        saved = False
        if save and hasattr(self.db, "save_position_runtime_snapshots"):
            self.db.save_position_runtime_snapshots([snapshot.to_dict() for snapshot in snapshots])
            saved = True
        return PositionRuntimeResult(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            positions=snapshots,
            saved=saved,
            warnings=(),
            live_order_allowed=False,
        )

    def _virtual_positions(self, trade_date: str, now: datetime) -> list[PositionRuntimeSnapshot]:
        loader = getattr(self.db, "list_open_virtual_positions", None)
        if not callable(loader):
            return []
        snapshots: list[PositionRuntimeSnapshot] = []
        for position in list(loader() or []):
            candidate = self._candidate(getattr(position, "candidate_id", None), trade_date)
            details = dict(getattr(position, "details", {}) or {})
            source_type = str(details.get("position_source") or details.get("source_type") or "VIRTUAL").upper()
            code = str((candidate.code if candidate else "") or details.get("code") or "")
            snapshots.append(
                self._snapshot_from_raw(
                    trade_date=trade_date,
                    now=now,
                    position_id=f"virtual:{getattr(position, 'id', '') or getattr(position, 'virtual_order_id', '')}",
                    candidate_id=getattr(position, "candidate_id", None),
                    code=code,
                    name=str((candidate.name if candidate else "") or details.get("name") or ""),
                    theme_id=str(details.get("theme_id") or self._candidate_metadata(candidate).get("theme_board_theme_id") or ""),
                    theme_name=str(details.get("theme_name") or self._candidate_metadata(candidate).get("theme_board_theme_name") or ""),
                    source_type=source_type if source_type in {"DRY_RUN", "VIRTUAL", "LIVE_SIM_OBSERVED", "MANUAL"} else "VIRTUAL",
                    entry_price=int(getattr(position, "entry_price", 0) or 0),
                    quantity=int(getattr(position, "quantity", 0) or 0),
                    remaining_quantity=max(0, int(details.get("remaining_quantity") or getattr(position, "quantity", 0) or 0)),
                    opened_at=str(getattr(position, "opened_at", "") or ""),
                    realized_return_pct=_float(getattr(position, "realized_return_pct", 0.0)),
                    existing_max_return_pct=_float(getattr(position, "max_return_pct", 0.0)),
                    existing_max_drawdown_pct=_float(getattr(position, "max_drawdown_pct", 0.0)),
                    details={**details, "candidate_metadata": self._candidate_metadata(candidate)},
                )
            )
        return snapshots

    def _live_sim_positions(self, trade_date: str, now: datetime) -> list[PositionRuntimeSnapshot]:
        loader = getattr(self.db, "list_live_sim_positions", None)
        if not callable(loader):
            return []
        try:
            positions = list(loader(statuses=["OPEN", "PARTIAL"]) or [])
        except TypeError:
            positions = [row for row in list(loader() or []) if str(row.get("status") or "") in {"OPEN", "PARTIAL"}]
        snapshots: list[PositionRuntimeSnapshot] = []
        for row in positions:
            item = dict(row or {})
            candidate_id = _optional_int(item.get("candidate_id") or item.get("candidate_instance_id"))
            candidate = self._candidate(candidate_id, trade_date)
            code = str(item.get("code") or (candidate.code if candidate else "") or "")
            entry_price = int(_first_number(item.get("avg_price"), item.get("avg_entry_price"), item.get("entry_price")))
            quantity = int(_first_number(item.get("current_qty"), item.get("quantity")))
            snapshots.append(
                self._snapshot_from_raw(
                    trade_date=trade_date,
                    now=now,
                    position_id=str(item.get("position_id") or f"live_sim:{code}"),
                    candidate_id=candidate_id,
                    code=code,
                    name=str(item.get("name") or (candidate.name if candidate else "") or ""),
                    theme_id=str(item.get("theme_id") or self._candidate_metadata(candidate).get("theme_board_theme_id") or ""),
                    theme_name=str(item.get("theme_name") or self._candidate_metadata(candidate).get("theme_board_theme_name") or ""),
                    source_type="LIVE_SIM_OBSERVED",
                    entry_price=entry_price,
                    quantity=quantity,
                    remaining_quantity=quantity,
                    opened_at=str(item.get("opened_at") or item.get("created_at") or ""),
                    realized_return_pct=_float(item.get("realized_pnl_pct")),
                    existing_max_return_pct=_float(item.get("max_return_pct")),
                    existing_max_drawdown_pct=_float(item.get("max_drawdown_pct")),
                    details={**dict(item.get("details") or {}), "candidate_metadata": self._candidate_metadata(candidate)},
                )
            )
        return snapshots

    def _snapshot_from_raw(
        self,
        *,
        trade_date: str,
        now: datetime,
        position_id: str,
        candidate_id: int | None,
        code: str,
        name: str,
        theme_id: str,
        theme_name: str,
        source_type: str,
        entry_price: int,
        quantity: int,
        remaining_quantity: int,
        opened_at: str,
        realized_return_pct: float,
        existing_max_return_pct: float,
        existing_max_drawdown_pct: float,
        details: dict[str, Any],
    ) -> PositionRuntimeSnapshot:
        tick = self.market_data.latest_tick(code) if self.market_data is not None and code else None
        flags: list[str] = []
        current_price = int(getattr(tick, "price", 0) or 0) if tick is not None else 0
        last_tick_at = getattr(tick, "timestamp", None).isoformat() if tick is not None else ""
        tick_age = (now - tick.timestamp).total_seconds() if tick is not None else None
        if tick is None:
            flags.append("LATEST_TICK_MISSING")
        elif tick_age is not None and tick_age > self.config.max_tick_age_sec:
            flags.append("LATEST_TICK_STALE")
        if current_price <= 0:
            flags.append("CURRENT_PRICE_MISSING")
        return_pct = _return_pct(current_price, entry_price)
        highest = max(int(details.get("highest_price_since_entry") or 0), entry_price, current_price)
        lowest_candidates = [value for value in [int(details.get("lowest_price_since_entry") or 0), entry_price, current_price] if value > 0]
        lowest = min(lowest_candidates) if lowest_candidates else 0
        max_return = max(existing_max_return_pct, _return_pct(highest, entry_price), return_pct)
        max_drawdown = min(existing_max_drawdown_pct, _return_pct(lowest, entry_price), return_pct)
        holding_minutes = _holding_minutes(opened_at, now)
        stop_price = int(round(entry_price * (1.0 + self.config.stop_loss_pct / 100.0))) if entry_price > 0 else 0
        take_price = int(round(entry_price * (1.0 + self.config.take_profit_1_pct / 100.0))) if entry_price > 0 else 0
        trailing_active = bool(details.get("trailing_active"))
        trailing_stop = int(details.get("trailing_stop_price") or 0)
        if trailing_active and highest > 0:
            trailing_stop = max(trailing_stop, int(round(highest * (1.0 - self.config.trailing_gap_pct / 100.0))))
        status = PositionRiskStatus.OPEN
        if "LATEST_TICK_STALE" in flags:
            status = PositionRiskStatus.STALE_DATA_RISK
        if "CURRENT_PRICE_MISSING" in flags or "LATEST_TICK_MISSING" in flags:
            status = PositionRiskStatus.DATA_WAIT
        market_context = self._position_market_context(
            code=code,
            trade_date=trade_date,
            now=now,
            details=details,
        )
        merged_details = {
            **details,
            "tick_age_sec": None if tick_age is None else round(max(0.0, tick_age), 3),
            "vwap": _vwap_from_tick_or_candles(code, tick, self.candle_builder),
            "recent_support": _recent_support(code, tick, self.candle_builder),
            "recent_high": _recent_high(code, tick, self.candle_builder),
            "momentum_1m": _candle_momentum(code, self.candle_builder, 1),
            "momentum_3m": _candle_momentum(code, self.candle_builder, 3),
            "spread_ticks": int(getattr(tick, "spread_ticks", 0) or 0) if tick is not None else 0,
            **market_context,
        }
        return PositionRuntimeSnapshot(
            trade_date=trade_date,
            calculated_at=now.isoformat(),
            position_id=str(position_id),
            candidate_id=candidate_id,
            code=str(code),
            name=name,
            theme_id=theme_id,
            theme_name=theme_name,
            source_type=source_type,
            entry_price=entry_price,
            quantity=quantity,
            remaining_quantity=remaining_quantity,
            avg_entry_price=float(entry_price or 0),
            opened_at=opened_at,
            holding_minutes=holding_minutes,
            current_price=current_price,
            current_return_pct=round(return_pct, 6),
            max_return_pct=round(max_return, 6),
            max_drawdown_pct=round(max_drawdown, 6),
            highest_price_since_entry=highest,
            lowest_price_since_entry=lowest,
            realized_return_pct=round(realized_return_pct, 6),
            unrealized_return_pct=round(return_pct, 6),
            stop_loss_price=stop_price,
            take_profit_price=take_price,
            trailing_stop_price=trailing_stop,
            trailing_active=trailing_active,
            first_profit_taken=bool(details.get("first_profit_taken")),
            last_tick_at=last_tick_at,
            data_quality_flags=tuple(_dedupe(flags)),
            risk_status=status,
            market_side=str(market_context.get("market_side") or "UNKNOWN"),
            market_side_source=str(market_context.get("market_side_source") or ""),
            market_side_resolution_status=str(market_context.get("market_side_resolution_status") or "UNRESOLVED"),
            side_market_regime=str(market_context.get("side_market_regime") or "DATA_WAIT"),
            counterpart_market_regime=str(market_context.get("counterpart_market_regime") or "DATA_WAIT"),
            composite_market_mode=str(market_context.get("composite_market_mode") or "DATA_DEGRADED"),
            systemic_risk_off=bool(market_context.get("systemic_risk_off")),
            market_context_calculated_at=str(market_context.get("market_context_calculated_at") or ""),
            market_context_fresh=bool(market_context.get("market_context_fresh")),
            candidate_market_action=str(market_context.get("candidate_market_action") or "DATA_WAIT"),
            strategy_context_id=str(market_context.get("strategy_context_id") or ""),
            details=merged_details,
        )

    def _candidate(self, candidate_id: int | None, trade_date: str):
        if candidate_id is None:
            return None
        loader = getattr(self.db, "load_candidate_by_id", None)
        if callable(loader):
            return loader(int(candidate_id))
        return None

    @staticmethod
    def _candidate_metadata(candidate: Any) -> dict[str, Any]:
        return dict(getattr(candidate, "metadata", {}) or {}) if candidate is not None else {}

    def _position_market_context(
        self,
        *,
        code: str,
        trade_date: str,
        now: datetime,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        details = dict(details or {})
        candidate_metadata = dict(details.get("candidate_metadata") or {})
        strategy_context = dict(candidate_metadata.get("strategy_context_v3") or details.get("strategy_context_v3") or {})
        context_market = dict(strategy_context.get("market") or {})
        latest_regime = self._latest_market_regime(trade_date)
        side, source, status = self._resolve_market_side(code, candidate_metadata, context_market, details)
        side_status, counterpart_status = self._side_regimes(side, latest_regime, context_market, details)
        calculated_at = str(
            latest_regime.get("calculated_at")
            or context_market.get("calculated_at")
            or strategy_context.get("calculated_at")
            or details.get("market_context_calculated_at")
            or ""
        )
        fresh = bool(calculated_at) and _fresh_iso(calculated_at, now, self.config.position_market_context_max_age_sec)
        if side not in MARKET_SIDES:
            status = "UNRESOLVED"
            fresh = False
        if not fresh:
            side_status = "DATA_WAIT"
            counterpart_status = "DATA_WAIT"
        return {
            "market_side": side,
            "market_side_source": source,
            "market_side_resolution_status": status,
            "side_market_regime": side_status,
            "counterpart_market_regime": counterpart_status,
            "composite_market_mode": str(
                latest_regime.get("composite_market_mode")
                or context_market.get("composite_market_mode")
                or details.get("composite_market_mode")
                or "DATA_DEGRADED"
            ).upper(),
            "systemic_risk_off": bool(
                latest_regime.get("systemic_risk_off")
                or latest_regime.get("risk_off_detected")
                or context_market.get("systemic_risk_off")
                or details.get("systemic_risk_off")
            ),
            "market_context_calculated_at": calculated_at,
            "market_context_fresh": fresh,
            "candidate_market_action": str(context_market.get("market_action") or candidate_metadata.get("market_action") or details.get("market_action") or "DATA_WAIT").upper(),
            "candidate_metadata": candidate_metadata,
            "strategy_context_id": str(strategy_context.get("context_id") or candidate_metadata.get("strategy_context_id") or ""),
        }

    def _resolve_market_side(
        self,
        code: str,
        candidate_metadata: Mapping[str, Any],
        context_market: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        side = _normalized_side(context_market.get("market_side"))
        if side in MARKET_SIDES:
            return side, "strategy_context_v3.market.market_side", "RESOLVED"
        side = _normalized_side(candidate_metadata.get("market_side") or candidate_metadata.get("market"))
        if side in MARKET_SIDES:
            return side, "candidate.market", "RESOLVED"
        side = self._symbol_master_side(code)
        if side in MARKET_SIDES:
            return side, "kiwoom_symbol_master", "RESOLVED"
        side = _normalized_side(details.get("market_side"))
        if side in MARKET_SIDES:
            return side, "position.details.market_side", "RESOLVED"
        return "UNKNOWN", "unresolved", "UNRESOLVED"

    def _symbol_master_side(self, code: str) -> str:
        loader = getattr(self.db, "list_kiwoom_symbol_master", None)
        if not callable(loader) or not code:
            return "UNKNOWN"
        try:
            rows = list(loader([code]) or [])
        except Exception:
            return "UNKNOWN"
        for row in rows:
            item = dict(row or {})
            side = _normalized_side(item.get("market_side") or item.get("market") or item.get("market_type"))
            if side in MARKET_SIDES:
                return side
        return "UNKNOWN"

    def _latest_market_regime(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_market_regime_snapshot", None)
        if not callable(loader):
            return {}
        try:
            return dict(loader(trade_date=trade_date) or {})
        except TypeError:
            return dict(loader() or {})

    @staticmethod
    def _side_regimes(
        side: str,
        latest_regime: Mapping[str, Any],
        context_market: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> tuple[str, str]:
        if side == "KOSPI":
            side_status = latest_regime.get("kospi_status")
            counterpart_status = latest_regime.get("kosdaq_status")
        elif side == "KOSDAQ":
            side_status = latest_regime.get("kosdaq_status")
            counterpart_status = latest_regime.get("kospi_status")
        else:
            side_status = ""
            counterpart_status = ""
        return (
            str(side_status or context_market.get("side_market_regime") or details.get("side_market_regime") or details.get("market_status") or "DATA_WAIT").upper(),
            str(counterpart_status or context_market.get("counterpart_market_regime") or details.get("counterpart_market_regime") or "DATA_WAIT").upper(),
        )

    @staticmethod
    def _dedupe_positions(items: Iterable[PositionRuntimeSnapshot]) -> list[PositionRuntimeSnapshot]:
        result: list[PositionRuntimeSnapshot] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            key = (str(item.candidate_id or ""), str(item.code or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result


class PositionRiskManager:
    def __init__(self, db: Any, *, config: PositionRiskConfig | None = None, clock=None) -> None:
        self.db = db
        self.config = config or PositionRiskConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        positions: Iterable[PositionRuntimeSnapshot | Mapping[str, Any]] | None = None,
        save: bool = True,
    ) -> PositionRiskResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        raw_positions = list(positions) if positions is not None else self._latest_positions(trade_date)
        snapshots = [_position_snapshot_from(item) for item in raw_positions]
        position_risks = tuple(self._position_risk(item, current) for item in snapshots)
        portfolio = self._portfolio_risk(trade_date, current, snapshots, position_risks)
        saved = False
        if save:
            if hasattr(self.db, "save_position_risk_snapshots"):
                self.db.save_position_risk_snapshots([item.to_dict() for item in position_risks])
                saved = True
            if hasattr(self.db, "save_portfolio_risk_snapshot"):
                self.db.save_portfolio_risk_snapshot(portfolio.to_dict())
                saved = True
            self._merge_candidate_metadata(snapshots, position_risks, current)
        return PositionRiskResult(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            position_risks=position_risks,
            portfolio_risk=portfolio,
            saved=saved,
            live_order_allowed=False,
        )

    def _position_risk(self, position: PositionRuntimeSnapshot, now: datetime) -> PositionRiskSnapshot:
        reasons: list[str] = []
        data_level = "NORMAL"
        theme_level = "NORMAL"
        market_level = "NORMAL"
        details = dict(position.details or {})
        market_status = str(position.side_market_regime or details.get("side_market_regime") or details.get("market_status") or details.get("market_regime_status") or "DATA_WAIT").upper()
        theme_status = str(details.get("theme_status") or details.get("theme_board_theme_status") or "").upper()
        support_broken, vwap_broken, theme_weak, leader_collapsed = self._structure_flags(position, details, theme_status)
        structure_intact = not (support_broken or vwap_broken or theme_weak or leader_collapsed)
        if position.risk_status == PositionRiskStatus.DATA_WAIT:
            data_level = "DATA_WAIT"
            reasons.append("POSITION_DATA_WAIT")
        if position.risk_status == PositionRiskStatus.STALE_DATA_RISK:
            data_level = "STALE"
            reasons.append("POSITION_STALE_DATA_RISK")
        if theme_status in {"WEAK_THEME", "WATCH_THEME"}:
            theme_level = "CAUTION" if theme_status == "WATCH_THEME" else "REDUCE"
            reasons.append("THEME_RISK_ELEVATED")
        if market_status == "RISK_OFF" or bool(position.systemic_risk_off):
            market_level = "REDUCE"
            reasons.append("SIDE_MARKET_RISK_OFF_POSITION_RISK" if not position.systemic_risk_off else "SYSTEMIC_RISK_OFF_POSITION_RISK")
        elif market_status == "WEAK":
            market_level = "CAUTION"
            reasons.append("SIDE_MARKET_WEAK_POSITION_RISK")
        position_action, exit_ratio, trailing_gap, action_reasons = self._position_market_action(
            position,
            side_market_regime=market_status,
            structure_intact=structure_intact,
            support_broken=support_broken,
            vwap_broken=vwap_broken,
            theme_weak=theme_weak,
            leader_collapsed=leader_collapsed,
        )
        reasons.extend(action_reasons)
        if position_action in {PositionMarketAction.EXIT_NOW.value, PositionMarketAction.SCALE_OUT.value}:
            market_level = _max_risk_level([market_level, "REDUCE"])
        elif position_action in {PositionMarketAction.TIGHTEN_STOP.value, PositionMarketAction.EXIT_IF_LOSER.value, PositionMarketAction.DATA_WAIT.value}:
            market_level = _max_risk_level([market_level, "CAUTION"])
        risk_level = _max_risk_level([data_level, theme_level, market_level])
        if position.current_return_pct <= self.config.stop_loss_pct:
            risk_level = _max_risk_level([risk_level, "REDUCE"])
            reasons.append("STOP_LOSS_DISTANCE_BREACHED")
        return PositionRiskSnapshot(
            trade_date=position.trade_date,
            calculated_at=now.isoformat(),
            position_id=position.position_id,
            candidate_id=position.candidate_id,
            code=position.code,
            risk_status=position.risk_status.value if isinstance(position.risk_status, PositionRiskStatus) else str(position.risk_status),
            risk_level=risk_level,
            stop_loss_distance_pct=round(_distance_pct(position.current_price, position.stop_loss_price), 6),
            take_profit_distance_pct=round(_distance_pct(position.current_price, position.take_profit_price), 6),
            trailing_distance_pct=round(_distance_pct(position.current_price, position.trailing_stop_price), 6),
            theme_risk_level=theme_level,
            market_risk_level=market_level,
            data_risk_level=data_level,
            market_side=position.market_side,
            side_market_regime=market_status,
            counterpart_market_regime=position.counterpart_market_regime,
            composite_market_mode=position.composite_market_mode,
            systemic_risk_off=position.systemic_risk_off,
            position_market_action=position_action,
            recommended_exit_ratio=exit_ratio,
            recommended_trailing_gap_pct=trailing_gap,
            position_action_reason_codes=tuple(_dedupe(action_reasons)),
            market_context_fresh=position.market_context_fresh,
            structure_intact=structure_intact,
            support_broken=support_broken,
            vwap_broken=vwap_broken,
            theme_weak=theme_weak,
            leader_collapsed=leader_collapsed,
            reason_codes=tuple(_dedupe(reasons)),
            details={
                "theme_status": theme_status,
                "market_status": market_status,
                "market_side": position.market_side,
                "market_side_source": position.market_side_source,
                "market_side_resolution_status": position.market_side_resolution_status,
                "side_market_regime": market_status,
                "counterpart_market_regime": position.counterpart_market_regime,
                "composite_market_mode": position.composite_market_mode,
                "systemic_risk_off": position.systemic_risk_off,
                "market_context_calculated_at": position.market_context_calculated_at,
                "market_context_fresh": position.market_context_fresh,
                "candidate_market_action": position.candidate_market_action,
                "strategy_context_id": position.strategy_context_id,
                "position_market_action": position_action,
                "recommended_exit_ratio": exit_ratio,
                "recommended_trailing_gap_pct": trailing_gap,
                "position_action_reason_codes": list(_dedupe(action_reasons)),
                "structure_intact": structure_intact,
                "support_broken": support_broken,
                "vwap_broken": vwap_broken,
                "theme_weak": theme_weak,
                "leader_collapsed": leader_collapsed,
            },
        )

    def _structure_flags(
        self,
        position: PositionRuntimeSnapshot,
        details: Mapping[str, Any],
        theme_status: str,
    ) -> tuple[bool, bool, bool, bool]:
        support = int(_first_number(details.get("recent_support"), details.get("support_price")))
        support_broken = bool(details.get("support_broken")) or (support > 0 and int(position.current_price or 0) > 0 and int(position.current_price or 0) < support)
        vwap = _float(details.get("vwap"))
        momentum = _float(details.get("momentum_1m"))
        vwap_broken = bool(details.get("vwap_broken")) or (vwap > 0 and int(position.current_price or 0) > 0 and int(position.current_price or 0) < vwap and momentum < 0)
        theme_weak = bool(details.get("theme_weak")) or theme_status in {"WEAK_THEME", "WATCH_THEME"}
        leader_collapsed = bool(details.get("leader_collapsed") or details.get("leader_vwap_broken") or details.get("leader_support_broken"))
        return support_broken, vwap_broken, theme_weak, leader_collapsed

    def _position_market_action(
        self,
        position: PositionRuntimeSnapshot,
        *,
        side_market_regime: str,
        structure_intact: bool,
        support_broken: bool,
        vwap_broken: bool,
        theme_weak: bool,
        leader_collapsed: bool,
    ) -> tuple[str, float, float, list[str]]:
        reasons: list[str] = []
        damaged = not structure_intact
        profitable = float(position.current_return_pct or 0.0) >= 0.0
        tight_gap = round(max(0.1, min(float(self.config.trailing_gap_pct or 0.0), 0.6)), 6)
        side = str(position.market_side or "UNKNOWN").upper()
        if position.risk_status in {PositionRiskStatus.DATA_WAIT, PositionRiskStatus.STALE_DATA_RISK}:
            return PositionMarketAction.DATA_WAIT.value, 0.0, 0.0, ["POSITION_MARKET_DATA_WAIT"]
        if not bool(position.market_context_fresh):
            return PositionMarketAction.DATA_WAIT.value, 0.0, 0.0, ["POSITION_MARKET_CONTEXT_STALE"]
        if side not in MARKET_SIDES or str(position.market_side_resolution_status or "").upper() != "RESOLVED":
            return PositionMarketAction.DATA_WAIT.value, 0.0, 0.0, ["POSITION_MARKET_SIDE_UNRESOLVED"]
        if bool(position.systemic_risk_off):
            reasons.append("SYSTEMIC_RISK_OFF_POSITION_RISK")
            if not profitable or damaged:
                return PositionMarketAction.EXIT_NOW.value, 1.0, 0.0, reasons + ["POSITION_MARKET_EXIT_NOW"]
            return PositionMarketAction.SCALE_OUT.value, 0.5, tight_gap, reasons + ["POSITION_MARKET_SCALE_OUT"]
        status = str(side_market_regime or "DATA_WAIT").upper()
        counterpart = str(position.counterpart_market_regime or "").upper()
        if status in {"EXPANSION", "SELECTIVE"}:
            reasons.append("POSITION_MARKET_HOLD")
            if counterpart in {"WEAK", "RISK_OFF"}:
                reasons.append("COUNTERPART_MARKET_RISK_IGNORED")
            return PositionMarketAction.HOLD.value, 0.0, 0.0, reasons
        if status == "CHOPPY":
            if support_broken or vwap_broken:
                return PositionMarketAction.EXIT_NOW.value, 1.0, 0.0, ["POSITION_MARKET_EXIT_NOW"]
            return PositionMarketAction.TIGHTEN_STOP.value, 0.0, tight_gap, ["POSITION_MARKET_TIGHTEN_STOP"]
        if status == "WEAK":
            reasons.append("SIDE_MARKET_WEAK_POSITION_RISK")
            if not profitable and damaged:
                return PositionMarketAction.EXIT_NOW.value, 1.0, 0.0, reasons + ["POSITION_MARKET_EXIT_NOW"]
            if not profitable:
                return PositionMarketAction.EXIT_IF_LOSER.value, 0.0, tight_gap, reasons + ["POSITION_MARKET_EXIT_IF_LOSER"]
            if theme_weak or leader_collapsed or support_broken or vwap_broken:
                return PositionMarketAction.SCALE_OUT.value, 0.5, tight_gap, reasons + ["POSITION_MARKET_SCALE_OUT"]
            return PositionMarketAction.TIGHTEN_STOP.value, 0.0, tight_gap, reasons + ["POSITION_MARKET_TIGHTEN_STOP"]
        if status == "RISK_OFF":
            reasons.append("SIDE_MARKET_RISK_OFF_POSITION_RISK")
            if not profitable or damaged:
                return PositionMarketAction.EXIT_NOW.value, 1.0, 0.0, reasons + ["POSITION_MARKET_EXIT_NOW"]
            return PositionMarketAction.SCALE_OUT.value, 0.5, tight_gap, reasons + ["POSITION_MARKET_SCALE_OUT"]
        if status in {"DATA_WAIT", "MARKET_CLOSED", "UNKNOWN", ""}:
            return PositionMarketAction.DATA_WAIT.value, 0.0, 0.0, ["POSITION_MARKET_DATA_WAIT"]
        return PositionMarketAction.DATA_WAIT.value, 0.0, 0.0, ["POSITION_MARKET_DATA_WAIT"]

    def _merge_candidate_metadata(
        self,
        positions: Iterable[PositionRuntimeSnapshot],
        risks: Iterable[PositionRiskSnapshot],
        now: datetime,
    ) -> int:
        loader = getattr(self.db, "load_candidate_by_id", None)
        saver = getattr(self.db, "save_candidate", None)
        if not callable(loader) or not callable(saver):
            return 0
        risks_by_position = {item.position_id: item for item in risks}
        updated = 0
        for position in positions:
            if position.candidate_id is None:
                continue
            candidate = loader(int(position.candidate_id))
            if candidate is None:
                continue
            risk = risks_by_position.get(position.position_id)
            metadata = dict(candidate.metadata or {})
            if risk is not None:
                metadata["position_risk_level"] = risk.risk_level
                metadata["position_risk_status"] = risk.risk_status
                metadata["position_risk_reason_codes"] = list(risk.reason_codes)
            metadata["current_return_pct"] = position.current_return_pct
            metadata["max_return_pct"] = position.max_return_pct
            metadata["max_drawdown_pct"] = position.max_drawdown_pct
            metadata["stop_loss_price"] = position.stop_loss_price
            metadata["take_profit_price"] = position.take_profit_price
            metadata["trailing_stop_price"] = position.trailing_stop_price
            metadata["updated_by_position_risk_at"] = now.isoformat()
            candidate.metadata = metadata
            saver(candidate)
            updated += 1
        return updated

    def _portfolio_risk(
        self,
        trade_date: str,
        now: datetime,
        positions: list[PositionRuntimeSnapshot],
        position_risks: tuple[PositionRiskSnapshot, ...],
    ) -> PortfolioRiskSnapshot:
        exposure_by_theme: defaultdict[str, int] = defaultdict(int)
        exposure_by_side: defaultdict[str, int] = defaultdict(int)
        open_count_by_side: Counter[str] = Counter()
        quality_flags_by_side: defaultdict[str, list[str]] = defaultdict(list)
        total_exposure = 0
        weighted_return = 0.0
        realized_values: list[float] = []
        for position in positions:
            side = _normalized_side(position.market_side)
            if side not in MARKET_SIDES:
                side = _normalized_side((position.details or {}).get("market_side"))
            price = int(position.current_price or 0)
            if price <= 0:
                fallback_price = int(_first_number(position.avg_entry_price, position.entry_price))
                if fallback_price > 0:
                    price = fallback_price
                    quality_flags_by_side[side].append("OPEN_EXPOSURE_PRICE_FALLBACK")
                else:
                    quality_flags_by_side[side].append("OPEN_EXPOSURE_PRICE_MISSING")
            exposure = max(0, int(position.remaining_quantity or 0)) * max(0, price)
            total_exposure += exposure
            exposure_by_theme[position.theme_name or position.theme_id or "UNKNOWN"] += exposure
            exposure_by_side[side] += exposure
            if side in MARKET_SIDES:
                open_count_by_side[side] += 1
            weighted_return += exposure * float(position.unrealized_return_pct or 0.0)
            realized_values.append(float(position.realized_return_pct or 0.0))
        pending_by_side, pending_count_by_side, pending_quality = self._pending_buy_reservations(trade_date)
        for side, flags in pending_quality.items():
            quality_flags_by_side[side].extend(flags)
        unrealized = weighted_return / total_exposure if total_exposure > 0 else 0.0
        max_drawdown = min([float(position.max_drawdown_pct or 0.0) for position in positions] or [0.0])
        daily_realized = sum(realized_values) / len(realized_values) if realized_values else 0.0
        reasons: list[str] = []
        risk_level = "NORMAL"
        latest_regime = self._latest_market_regime(trade_date)
        market_calculated_at = str(latest_regime.get("calculated_at") or "")
        market_fresh = bool(market_calculated_at) and _fresh_iso(market_calculated_at, now, self.config.market_side_budget_max_age_sec)
        composite = str(latest_regime.get("composite_market_mode") or "DATA_DEGRADED").upper()
        systemic = bool(latest_regime.get("systemic_risk_off") or latest_regime.get("risk_off_detected") or composite == "SYSTEMIC_RISK_OFF")
        side_status = {
            "KOSPI": str(latest_regime.get("kospi_status") or self._latest_position_side_status(positions, "KOSPI") or "DATA_WAIT").upper(),
            "KOSDAQ": str(latest_regime.get("kosdaq_status") or self._latest_position_side_status(positions, "KOSDAQ") or "DATA_WAIT").upper(),
        }
        if not market_fresh:
            composite = "DATA_DEGRADED"
            side_status = {side: "DATA_WAIT" for side in MARKET_SIDES}
            for side in MARKET_SIDES:
                quality_flags_by_side[side].append("MARKET_CONTEXT_STALE")
        budgets: dict[str, dict[str, Any]] = {}
        for side in MARKET_SIDES:
            counterpart = "KOSDAQ" if side == "KOSPI" else "KOSPI"
            budget = self._market_side_budget(
                side=side,
                side_status=side_status[side],
                counterpart_status=side_status.get(counterpart, "DATA_WAIT"),
                composite=composite,
                systemic=systemic,
                open_exposure=int(exposure_by_side.get(side, 0)),
                pending_exposure=int(pending_by_side.get(side, 0)),
                open_count=int(open_count_by_side.get(side, 0)),
                pending_count=int(pending_count_by_side.get(side, 0)),
                data_quality_flags=quality_flags_by_side.get(side, []),
                market_fresh=market_fresh,
            )
            budgets[side] = budget.to_dict()
            reasons.extend(budget.reason_codes)
            if budget.budget_action in {MarketSideBudgetAction.STOP_NEW_ENTRY.value, MarketSideBudgetAction.DATA_WAIT.value, MarketSideBudgetAction.MARKET_CLOSED.value}:
                risk_level = _max_risk_level([risk_level, "STOP_NEW_ENTRY"])
        if any(item.data_risk_level in {"DATA_WAIT", "STALE"} for item in position_risks):
            risk_level = _max_risk_level([risk_level, "CAUTION"])
            reasons.append("PORTFOLIO_DATA_RISK")
        if any(item.market_risk_level == "REDUCE" for item in position_risks):
            risk_level = _max_risk_level([risk_level, "REDUCE"])
            reasons.append("PORTFOLIO_MARKET_RISK")
        if total_exposure >= self.config.stop_new_entry_exposure_krw > 0:
            risk_level = _max_risk_level([risk_level, "STOP_NEW_ENTRY"])
            reasons.append("PORTFOLIO_EXPOSURE_LIMIT")
        gross_pending = sum(int(value or 0) for value in pending_by_side.values())
        gross_reserved = total_exposure + gross_pending
        gross_limit = int(self.config.portfolio_gross_exposure_limit_krw or 0)
        if gross_limit > 0 and gross_reserved >= gross_limit:
            risk_level = _max_risk_level([risk_level, "STOP_NEW_ENTRY"])
            reasons.append("PORTFOLIO_GROSS_EXPOSURE_LIMIT")
        if max_drawdown <= self.config.reduce_drawdown_pct:
            risk_level = _max_risk_level([risk_level, "REDUCE"])
            reasons.append("PORTFOLIO_DRAWDOWN_REDUCE")
        if max_drawdown <= self.config.kill_switch_drawdown_pct:
            risk_level = _max_risk_level([risk_level, "KILL_SWITCH_RECOMMENDED"])
            reasons.append("PORTFOLIO_DRAWDOWN_KILL_SWITCH")
        gross_available = max(0, gross_limit - gross_reserved) if gross_limit > 0 else 0
        gross_utilization = round((gross_reserved / gross_limit) * 100.0, 6) if gross_limit > 0 else 0.0
        return PortfolioRiskSnapshot(
            trade_date=trade_date,
            calculated_at=now.isoformat(),
            open_position_count=len(positions),
            total_exposure=total_exposure,
            theme_exposure_by_theme=dict(exposure_by_theme),
            market_side_exposure=dict(exposure_by_side),
            unrealized_pnl_pct=round(unrealized, 6),
            max_drawdown_pct=round(max_drawdown, 6),
            daily_realized_pnl_pct=round(daily_realized, 6),
            risk_level=risk_level,
            stop_new_entry_recommended=risk_level in {"STOP_NEW_ENTRY", "KILL_SWITCH_RECOMMENDED"},
            kill_switch_recommended=risk_level == "KILL_SWITCH_RECOMMENDED",
            gross_exposure_limit_krw=gross_limit,
            gross_open_exposure_krw=total_exposure,
            gross_pending_buy_exposure_krw=gross_pending,
            gross_reserved_exposure_krw=gross_reserved,
            gross_available_exposure_krw=gross_available,
            gross_utilization_pct=gross_utilization,
            composite_market_mode=composite,
            systemic_risk_off=systemic,
            market_side_budgets=budgets,
            stop_new_entry_by_side={
                side: budgets[side]["budget_action"] in {MarketSideBudgetAction.STOP_NEW_ENTRY.value, MarketSideBudgetAction.DATA_WAIT.value, MarketSideBudgetAction.MARKET_CLOSED.value}
                for side in MARKET_SIDES
            },
            reduce_only_by_side={
                side: budgets[side]["budget_action"] in {MarketSideBudgetAction.STOP_NEW_ENTRY.value, MarketSideBudgetAction.DATA_WAIT.value, MarketSideBudgetAction.MARKET_CLOSED.value}
                for side in MARKET_SIDES
            },
            market_context_calculated_at=market_calculated_at,
            market_context_fresh=market_fresh,
            reason_codes=tuple(_dedupe(reasons)),
            details={
                "position_risk_counts": dict(Counter(item.risk_level for item in position_risks)),
                "position_market_action_counts": dict(Counter(item.position_market_action for item in position_risks)),
                "market_side_budgets": budgets,
                "market_side_portfolio_enabled": self.config.market_side_portfolio_enabled,
                "market_side_portfolio_observe_only": self.config.market_side_portfolio_observe_only,
                "market_side_portfolio_enforce_buy_limits": self.config.market_side_portfolio_enforce_buy_limits,
            },
        )

    def _market_side_budget(
        self,
        *,
        side: str,
        side_status: str,
        counterpart_status: str,
        composite: str,
        systemic: bool,
        open_exposure: int,
        pending_exposure: int,
        open_count: int,
        pending_count: int,
        data_quality_flags: Iterable[str],
        market_fresh: bool,
    ) -> MarketSidePortfolioBudget:
        side_limit = self.config.kospi_exposure_limit_krw if side == "KOSPI" else self.config.kosdaq_exposure_limit_krw
        gross = int(self.config.portfolio_gross_exposure_limit_krw or 0)
        composite_multiplier = _composite_budget_multiplier(composite)
        side_multiplier = _side_budget_multiplier(side_status)
        derived_limit = int(round(gross * composite_multiplier * side_multiplier)) if gross > 0 else 0
        base_limit = int(side_limit or gross or 0)
        effective_limit = min(side_limit, derived_limit) if side_limit > 0 else derived_limit
        reserved = max(0, int(open_exposure or 0)) + max(0, int(pending_exposure or 0))
        max_positions = self.config.kospi_max_open_positions if side == "KOSPI" else self.config.kosdaq_max_open_positions
        available_slots = max(0, int(max_positions or 0) - int(open_count or 0) - int(pending_count or 0)) if max_positions > 0 else 0
        action, reasons = self._budget_action(
            side=side,
            side_status=side_status,
            composite=composite,
            systemic=systemic,
            effective_limit=effective_limit,
            market_fresh=market_fresh,
            data_quality_flags=list(data_quality_flags),
            composite_multiplier=composite_multiplier,
            side_multiplier=side_multiplier,
        )
        if effective_limit > 0 and reserved >= effective_limit:
            reasons.append("MARKET_SIDE_EXPOSURE_LIMIT")
            if action == MarketSideBudgetAction.ALLOW_BUDGET.value:
                action = MarketSideBudgetAction.STOP_NEW_ENTRY.value
        if max_positions > 0 and available_slots <= 0:
            reasons.append("MARKET_SIDE_POSITION_LIMIT")
            if action == MarketSideBudgetAction.ALLOW_BUDGET.value:
                action = MarketSideBudgetAction.STOP_NEW_ENTRY.value
        if pending_exposure > 0:
            reasons.append("PENDING_BUY_EXPOSURE_RESERVED")
        available = max(0, effective_limit - reserved) if effective_limit > 0 else 0
        utilization = round((reserved / effective_limit) * 100.0, 6) if effective_limit > 0 else 0.0
        return MarketSidePortfolioBudget(
            market_side=side,
            side_market_regime=side_status,
            counterpart_market_regime=counterpart_status,
            composite_market_mode=composite,
            systemic_risk_off=systemic,
            base_exposure_limit_krw=base_limit,
            effective_exposure_limit_krw=effective_limit,
            open_exposure_krw=open_exposure,
            pending_buy_exposure_krw=pending_exposure,
            reserved_exposure_krw=reserved,
            available_exposure_krw=available,
            utilization_pct=utilization,
            open_position_count=open_count,
            pending_buy_count=pending_count,
            max_open_positions=max_positions,
            available_position_slots=available_slots,
            budget_action=action,
            reason_codes=tuple(_dedupe(reasons)),
            data_quality_flags=tuple(_dedupe(data_quality_flags)),
        )

    def _budget_action(
        self,
        *,
        side: str,
        side_status: str,
        composite: str,
        systemic: bool,
        effective_limit: int,
        market_fresh: bool,
        data_quality_flags: list[str],
        composite_multiplier: float,
        side_multiplier: float,
    ) -> tuple[str, list[str]]:
        reasons: list[str] = []
        if side not in MARKET_SIDES:
            return MarketSideBudgetAction.DATA_WAIT.value, ["MARKET_SIDE_UNKNOWN_BUY_BLOCK"]
        if not market_fresh or data_quality_flags:
            reasons.append("MARKET_SIDE_BUDGET_DATA_WAIT")
            return MarketSideBudgetAction.DATA_WAIT.value, reasons
        if systemic or composite == "SYSTEMIC_RISK_OFF":
            return MarketSideBudgetAction.STOP_NEW_ENTRY.value, reasons + ["MARKET_SIDE_STOP_NEW_ENTRY"]
        if composite == "MARKET_CLOSED" or side_status == "MARKET_CLOSED":
            return MarketSideBudgetAction.MARKET_CLOSED.value, reasons + ["MARKET_SIDE_STOP_NEW_ENTRY"]
        if side_status in {"DATA_WAIT", "UNKNOWN", ""}:
            return MarketSideBudgetAction.DATA_WAIT.value, reasons + ["MARKET_SIDE_BUDGET_DATA_WAIT"]
        if side_status in {"RISK_OFF", "WEAK", "CHOPPY"} or effective_limit <= 0:
            return MarketSideBudgetAction.STOP_NEW_ENTRY.value, reasons + ["MARKET_SIDE_STOP_NEW_ENTRY"]
        if side_status == "SELECTIVE" or composite_multiplier < 1.0 or side_multiplier < 1.0:
            return MarketSideBudgetAction.REDUCED_BUDGET.value, reasons + ["MARKET_SIDE_BUDGET_REDUCED"]
        return MarketSideBudgetAction.ALLOW_BUDGET.value, reasons + ["MARKET_SIDE_BUDGET_ALLOW"]

    def _pending_buy_reservations(self, trade_date: str) -> tuple[Counter[str], Counter[str], dict[str, list[str]]]:
        exposure: Counter[str] = Counter()
        counts: Counter[str] = Counter()
        quality: defaultdict[str, list[str]] = defaultdict(list)
        if not self.config.market_side_pending_buy_included:
            return exposure, counts, dict(quality)
        seen: set[str] = set()
        orders = _call(self.db, "list_managed_orders", trade_date=trade_date, status=list(PENDING_BUY_STATUSES), side="BUY", limit=1000) or []
        for row in orders:
            item = dict(row or {})
            key = self._pending_key(item)
            if key in seen:
                continue
            seen.add(key)
            side = self._pending_market_side(item)
            qty = _pending_remaining_quantity(item)
            price = int(_first_number(item.get("price"), item.get("limit_price_hint"), dict(item.get("details") or {}).get("current_price")))
            if price <= 0:
                quality[side].append("PENDING_BUY_PRICE_MISSING")
                continue
            amount = max(0, qty) * price
            if amount > 0:
                exposure[side] += amount
                counts[side] += 1
        intents = _call(self.db, "list_managed_order_intents", trade_date=trade_date, limit=1000) or []
        for row in intents:
            item = dict(row or {})
            if str(item.get("side") or "").upper() != "BUY" or str(item.get("status") or "").upper() not in PENDING_BUY_STATUSES:
                continue
            key = self._pending_key(item)
            if key in seen:
                continue
            seen.add(key)
            side = self._pending_market_side(item)
            qty = max(0, int(item.get("quantity") or 0))
            price = int(_first_number(item.get("price"), dict(item.get("details") or {}).get("current_price")))
            if price <= 0:
                quality[side].append("PENDING_BUY_PRICE_MISSING")
                continue
            amount = qty * price
            if amount > 0:
                exposure[side] += amount
                counts[side] += 1
        return exposure, counts, dict(quality)

    def _pending_key(self, item: Mapping[str, Any]) -> str:
        if item.get("intent_id"):
            return f"intent:{item.get('intent_id')}"
        if item.get("idempotency_key"):
            return f"idem:{item.get('idempotency_key')}"
        if item.get("id") or item.get("order_id"):
            return f"order:{item.get('id') or item.get('order_id')}"
        return f"{item.get('code')}:{item.get('side')}:{item.get('status')}"

    def _pending_market_side(self, item: Mapping[str, Any]) -> str:
        details = dict(item.get("details") or {})
        context = dict(details.get("strategy_context_v3") or {})
        market = dict(context.get("market") or {})
        side = _normalized_side(market.get("market_side") or details.get("market_side") or item.get("market_side"))
        if side in MARKET_SIDES:
            return side
        side = self._symbol_master_side(str(item.get("code") or ""))
        return side if side in MARKET_SIDES else "UNKNOWN"

    def _symbol_master_side(self, code: str) -> str:
        loader = getattr(self.db, "list_kiwoom_symbol_master", None)
        if not callable(loader) or not code:
            return "UNKNOWN"
        try:
            rows = list(loader([code]) or [])
        except Exception:
            return "UNKNOWN"
        for row in rows:
            item = dict(row or {})
            side = _normalized_side(item.get("market_side") or item.get("market") or item.get("market_type"))
            if side in MARKET_SIDES:
                return side
        return "UNKNOWN"

    def _latest_market_regime(self, trade_date: str) -> dict[str, Any]:
        loader = getattr(self.db, "latest_market_regime_snapshot", None)
        if not callable(loader):
            return {}
        try:
            return dict(loader(trade_date=trade_date) or {})
        except TypeError:
            return dict(loader() or {})

    @staticmethod
    def _latest_position_side_status(positions: Iterable[PositionRuntimeSnapshot], side: str) -> str:
        for position in positions:
            if _normalized_side(position.market_side) == side and str(position.side_market_regime or ""):
                return str(position.side_market_regime or "")
        return ""

    def _latest_positions(self, trade_date: str) -> list[dict[str, Any]]:
        loader = getattr(self.db, "latest_position_runtime_snapshots", None)
        if not callable(loader):
            return []
        return list(loader(trade_date=trade_date) or [])


class PositionRiskRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        config: PositionRiskConfig | None = None,
        runtime_service: PositionRuntimeService | None = None,
        risk_manager: PositionRiskManager | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or PositionRiskConfig.from_env()
        self.clock = clock or datetime.now
        self.runtime_service = runtime_service or PositionRuntimeService(
            db,
            market_data=market_data,
            candle_builder=candle_builder,
            config=self.config,
            clock=self.clock,
        )
        self.risk_manager = risk_manager or PositionRiskManager(db, config=self.config, clock=self.clock)
        self.last_summary: dict[str, Any] = {"status": "DISABLED", "enabled": False, "output_mode": POSITION_RISK_OUTPUT_MODE}
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"status": "DISABLED", "enabled": False, "output_mode": POSITION_RISK_OUTPUT_MODE}
            return dict(self.last_summary)
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        runtime_result = self.runtime_service.build(trade_date=current.date().isoformat(), now=current, save=True)
        risk_result = self.risk_manager.build(
            trade_date=current.date().isoformat(),
            now=current,
            positions=runtime_result.positions,
            save=True,
        )
        self.last_run_at = current
        self.last_summary = position_risk_dashboard_payload(risk_result, positions=runtime_result.positions)
        self.last_summary["enabled"] = True
        self.last_summary["status"] = "OK"
        return dict(self.last_summary)


def position_risk_dashboard_payload(
    result: PositionRiskResult | Mapping[str, Any],
    *,
    positions: Iterable[PositionRuntimeSnapshot | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
    risks = list(data.get("position_risks") or [])
    portfolio = dict(data.get("portfolio_risk") or {})
    position_items = [_position_dict(item) for item in positions]
    if not position_items:
        position_items = []
    risk_by_position = {str(item.get("position_id") or ""): dict(item or {}) for item in risks}
    for item in position_items:
        risk = risk_by_position.get(str(item.get("position_id") or ""))
        if not risk:
            continue
        details = dict(risk.get("details") or {})
        item.update(
            {
                "risk_level": risk.get("risk_level", "NORMAL"),
                "market_side": risk.get("market_side") or item.get("market_side") or details.get("market_side") or "",
                "side_market_regime": risk.get("side_market_regime") or details.get("side_market_regime") or "",
                "position_market_action": risk.get("position_market_action") or details.get("position_market_action") or "",
                "recommended_exit_ratio": float(risk.get("recommended_exit_ratio") or details.get("recommended_exit_ratio") or 0.0),
                "recommended_trailing_gap_pct": float(risk.get("recommended_trailing_gap_pct") or details.get("recommended_trailing_gap_pct") or 0.0),
                "structure_intact": bool(risk.get("structure_intact", details.get("structure_intact", True))),
                "support_broken": bool(risk.get("support_broken") or details.get("support_broken")),
                "vwap_broken": bool(risk.get("vwap_broken") or details.get("vwap_broken")),
                "theme_weak": bool(risk.get("theme_weak") or details.get("theme_weak")),
                "leader_collapsed": bool(risk.get("leader_collapsed") or details.get("leader_collapsed")),
            }
        )
    reason_counter = Counter()
    action_counter = Counter()
    for item in risks:
        for reason in list(item.get("reason_codes") or []):
            reason_counter[str(reason)] += 1
        action = str(item.get("position_market_action") or dict(item.get("details") or {}).get("position_market_action") or "")
        if action:
            action_counter[action] += 1
    side_budgets = dict(portfolio.get("market_side_budgets") or dict(portfolio.get("details") or {}).get("market_side_budgets") or {})
    return {
        "calculated_at": data.get("calculated_at") or portfolio.get("calculated_at", ""),
        "trade_date": data.get("trade_date") or portfolio.get("trade_date", ""),
        "portfolio_risk_level": portfolio.get("risk_level", "NORMAL"),
        "open_position_count": int(portfolio.get("open_position_count") or len(position_items)),
        "total_exposure": int(portfolio.get("total_exposure") or 0),
        "gross_exposure_limit_krw": int(portfolio.get("gross_exposure_limit_krw") or 0),
        "gross_open_exposure_krw": int(portfolio.get("gross_open_exposure_krw") or portfolio.get("total_exposure") or 0),
        "gross_pending_buy_exposure_krw": int(portfolio.get("gross_pending_buy_exposure_krw") or 0),
        "gross_reserved_exposure_krw": int(portfolio.get("gross_reserved_exposure_krw") or 0),
        "gross_available_exposure_krw": int(portfolio.get("gross_available_exposure_krw") or 0),
        "gross_utilization_pct": float(portfolio.get("gross_utilization_pct") or 0.0),
        "composite_market_mode": portfolio.get("composite_market_mode", "DATA_DEGRADED"),
        "systemic_risk_off": bool(portfolio.get("systemic_risk_off")),
        "market_context_calculated_at": portfolio.get("market_context_calculated_at", ""),
        "market_context_fresh": bool(portfolio.get("market_context_fresh")),
        "market_side_budgets": side_budgets,
        "stop_new_entry_by_side": dict(portfolio.get("stop_new_entry_by_side") or {}),
        "reduce_only_by_side": dict(portfolio.get("reduce_only_by_side") or {}),
        "position_market_action_counts": dict(action_counter),
        "theme_exposure": dict(portfolio.get("theme_exposure_by_theme") or {}),
        "market_side_exposure": dict(portfolio.get("market_side_exposure") or {}),
        "unrealized_pnl_pct": float(portfolio.get("unrealized_pnl_pct") or 0.0),
        "daily_realized_pnl_pct": float(portfolio.get("daily_realized_pnl_pct") or 0.0),
        "stop_new_entry_recommended": bool(portfolio.get("stop_new_entry_recommended")),
        "kill_switch_recommended": bool(portfolio.get("kill_switch_recommended")),
        "top_position_risks": [{"reason": key, "count": count} for key, count in reason_counter.most_common(10)],
        "positions": position_items[:20],
        "warnings": list(portfolio.get("reason_codes") or []),
        "output_mode": POSITION_RISK_OUTPUT_MODE,
        "live_order_allowed": False,
    }


def position_risk_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    portfolio_loader = getattr(db, "latest_portfolio_risk_snapshot", None)
    risk_loader = getattr(db, "latest_position_risk_snapshots", None)
    position_loader = getattr(db, "latest_position_runtime_snapshots", None)
    if not callable(portfolio_loader) or not callable(risk_loader):
        return {"status": "UNAVAILABLE", "output_mode": POSITION_RISK_OUTPUT_MODE, "live_order_allowed": False}
    portfolio = portfolio_loader(trade_date=trade_date)
    if not portfolio:
        return {"status": "EMPTY", "output_mode": POSITION_RISK_OUTPUT_MODE, "live_order_allowed": False}
    payload = position_risk_dashboard_payload(
        {"trade_date": portfolio.get("trade_date", ""), "calculated_at": portfolio.get("calculated_at", ""), "position_risks": risk_loader(trade_date=trade_date), "portfolio_risk": portfolio},
        positions=position_loader(trade_date=trade_date) if callable(position_loader) else [],
    )
    payload["status"] = "OK"
    return payload


def _position_snapshot_from(item: PositionRuntimeSnapshot | Mapping[str, Any]) -> PositionRuntimeSnapshot:
    if isinstance(item, PositionRuntimeSnapshot):
        return item
    data = dict(item or {})
    return PositionRuntimeSnapshot(
        trade_date=str(data.get("trade_date") or ""),
        calculated_at=str(data.get("calculated_at") or ""),
        position_id=str(data.get("position_id") or ""),
        candidate_id=_optional_int(data.get("candidate_id")),
        code=str(data.get("code") or ""),
        name=str(data.get("name") or ""),
        theme_id=str(data.get("theme_id") or ""),
        theme_name=str(data.get("theme_name") or ""),
        source_type=str(data.get("source_type") or "VIRTUAL"),
        entry_price=int(data.get("entry_price") or 0),
        quantity=int(data.get("quantity") or 0),
        remaining_quantity=int(data.get("remaining_quantity") or 0),
        avg_entry_price=_float(data.get("avg_entry_price")),
        opened_at=str(data.get("opened_at") or ""),
        holding_minutes=int(data.get("holding_minutes") or 0),
        current_price=int(data.get("current_price") or 0),
        current_return_pct=_float(data.get("current_return_pct")),
        max_return_pct=_float(data.get("max_return_pct")),
        max_drawdown_pct=_float(data.get("max_drawdown_pct")),
        highest_price_since_entry=int(data.get("highest_price_since_entry") or 0),
        lowest_price_since_entry=int(data.get("lowest_price_since_entry") or 0),
        realized_return_pct=_float(data.get("realized_return_pct")),
        unrealized_return_pct=_float(data.get("unrealized_return_pct")),
        stop_loss_price=int(data.get("stop_loss_price") or 0),
        take_profit_price=int(data.get("take_profit_price") or 0),
        trailing_stop_price=int(data.get("trailing_stop_price") or 0),
        trailing_active=bool(data.get("trailing_active")),
        first_profit_taken=bool(data.get("first_profit_taken")),
        last_tick_at=str(data.get("last_tick_at") or ""),
        data_quality_flags=tuple(str(value) for value in list(data.get("data_quality_flags") or [])),
        risk_status=PositionRiskStatus(str(data.get("risk_status") or PositionRiskStatus.OPEN.value)),
        market_side=str(data.get("market_side") or dict(data.get("details") or {}).get("market_side") or "UNKNOWN"),
        market_side_source=str(data.get("market_side_source") or dict(data.get("details") or {}).get("market_side_source") or ""),
        market_side_resolution_status=str(data.get("market_side_resolution_status") or dict(data.get("details") or {}).get("market_side_resolution_status") or "UNRESOLVED"),
        side_market_regime=str(data.get("side_market_regime") or dict(data.get("details") or {}).get("side_market_regime") or "DATA_WAIT"),
        counterpart_market_regime=str(data.get("counterpart_market_regime") or dict(data.get("details") or {}).get("counterpart_market_regime") or "DATA_WAIT"),
        composite_market_mode=str(data.get("composite_market_mode") or dict(data.get("details") or {}).get("composite_market_mode") or "DATA_DEGRADED"),
        systemic_risk_off=bool(data.get("systemic_risk_off") or dict(data.get("details") or {}).get("systemic_risk_off")),
        market_context_calculated_at=str(data.get("market_context_calculated_at") or dict(data.get("details") or {}).get("market_context_calculated_at") or ""),
        market_context_fresh=bool(data.get("market_context_fresh") or dict(data.get("details") or {}).get("market_context_fresh")),
        candidate_market_action=str(data.get("candidate_market_action") or dict(data.get("details") or {}).get("candidate_market_action") or "DATA_WAIT"),
        strategy_context_id=str(data.get("strategy_context_id") or dict(data.get("details") or {}).get("strategy_context_id") or ""),
        details=dict(data.get("details") or {}),
    )


def _position_dict(item: PositionRuntimeSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    return item.to_dict() if hasattr(item, "to_dict") else dict(item or {})


def _vwap_from_tick_or_candles(code: str, tick: StrategyTick | None, candle_builder: CandleBuilder | None) -> float:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    value = _float(metadata.get("vwap"))
    if value > 0:
        return value
    if candle_builder is None:
        return 0.0
    candles = candle_builder.completed_candles(code, 1)
    total_volume = sum(max(0, candle.volume) for candle in candles)
    if total_volume <= 0:
        return 0.0
    return sum(candle.close * max(0, candle.volume) for candle in candles) / total_volume


def _recent_support(code: str, tick: StrategyTick | None, candle_builder: CandleBuilder | None) -> int:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    explicit = int(_first_number(metadata.get("recent_support"), metadata.get("support_price"), metadata.get("session_low")))
    if explicit > 0:
        return explicit
    if candle_builder is None:
        return 0
    candles = candle_builder.completed_candles(code, 1)[-5:]
    lows = [int(candle.low or 0) for candle in candles if int(candle.low or 0) > 0]
    return min(lows) if lows else 0


def _recent_high(code: str, tick: StrategyTick | None, candle_builder: CandleBuilder | None) -> int:
    metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
    explicit = int(_first_number(metadata.get("recent_high"), metadata.get("session_high")))
    if explicit > 0:
        return explicit
    if candle_builder is None:
        return 0
    candles = candle_builder.completed_candles(code, 1)[-5:]
    highs = [int(candle.high or 0) for candle in candles if int(candle.high or 0) > 0]
    return max(highs) if highs else 0


def _candle_momentum(code: str, candle_builder: CandleBuilder | None, interval_min: int) -> float:
    if candle_builder is None:
        return 0.0
    candles = candle_builder.completed_candles(code, interval_min)
    if not candles and interval_min == 1:
        active = candle_builder.active_candle(code)
        candles = [active] if active is not None else []
    if not candles:
        return 0.0
    candle = candles[-1]
    return _return_pct(int(candle.close or 0), int(candle.open or 0))


def _holding_minutes(opened_at: str, now: datetime) -> int:
    opened = _parse_dt(opened_at)
    if opened is None:
        return 0
    current = now
    if opened.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=opened.tzinfo)
    if opened.tzinfo is None and current.tzinfo is not None:
        opened = opened.replace(tzinfo=current.tzinfo)
    return max(0, int((current - opened).total_seconds() // 60))


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fresh_iso(value: str, now: datetime, max_age_sec: int) -> bool:
    parsed = _parse_dt(value)
    if parsed is None:
        return False
    current = now
    if parsed.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=parsed.tzinfo)
    if parsed.tzinfo is None and current.tzinfo is not None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    return (current - parsed).total_seconds() <= max(0, int(max_age_sec or 0))


def _return_pct(price: int, base: int) -> float:
    if price <= 0 or base <= 0:
        return 0.0
    return ((float(price) - float(base)) / float(base)) * 100.0


def _distance_pct(price: int, target: int) -> float:
    if price <= 0 or target <= 0:
        return 0.0
    return ((float(target) - float(price)) / float(price)) * 100.0


def _max_risk_level(values: Iterable[str]) -> str:
    order = {"NORMAL": 0, "DATA_WAIT": 1, "STALE": 1, "CAUTION": 1, "REDUCE": 2, "STOP_NEW_ENTRY": 3, "KILL_SWITCH_RECOMMENDED": 4}
    return max((str(value or "NORMAL") for value in values), key=lambda item: order.get(item, 0), default="NORMAL")


def _first_number(*values: Any) -> float:
    for value in values:
        number = _float(value)
        if number != 0.0:
            return number
    return 0.0


def _float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None


def _normalized_side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"KOSPI", "STK", "001", "0", "KRX"}:
        return "KOSPI"
    if text in {"KOSDAQ", "KSQ", "101", "10", "KQ"}:
        return "KOSDAQ"
    return "UNKNOWN"


def _composite_budget_multiplier(mode: str) -> float:
    return {
        "BROAD_RISK_ON": 1.0,
        "SPLIT_KOSPI_ON": 0.7,
        "SPLIT_KOSDAQ_ON": 0.7,
        "MIXED_CAUTION": 0.6,
        "DATA_DEGRADED": 0.5,
        "SYSTEMIC_RISK_OFF": 0.0,
        "MARKET_CLOSED": 0.0,
    }.get(str(mode or "").upper(), 0.5)


def _side_budget_multiplier(status: str) -> float:
    return {
        "EXPANSION": 1.0,
        "SELECTIVE": 0.6,
        "CHOPPY": 0.0,
        "WEAK": 0.0,
        "RISK_OFF": 0.0,
        "DATA_WAIT": 0.0,
        "MARKET_CLOSED": 0.0,
    }.get(str(status or "").upper(), 0.0)


def _pending_remaining_quantity(item: Mapping[str, Any]) -> int:
    status = str(item.get("status") or "").upper()
    quantity = max(0, int(item.get("quantity") or 0))
    remaining = int(item.get("remaining_quantity") or 0)
    filled = int(item.get("filled_quantity") or 0)
    if status == "PARTIALLY_FILLED":
        return max(0, remaining if remaining > 0 else quantity - filled)
    return max(0, remaining if remaining > 0 else quantity)


def _call(target: Any, name: str, **kwargs):
    fn = getattr(target, name, None)
    if not callable(fn):
        return None
    return fn(**kwargs)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value
