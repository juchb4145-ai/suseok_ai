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


class PositionRiskStatus(str, Enum):
    OPEN = "OPEN"
    SCALE_OUT_PENDING = "SCALE_OUT_PENDING"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    DATA_WAIT = "DATA_WAIT"
    STALE_DATA_RISK = "STALE_DATA_RISK"


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
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

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
                    details=dict(item.get("details") or {}),
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
            details={
                **details,
                "tick_age_sec": None if tick_age is None else round(max(0.0, tick_age), 3),
                "vwap": _vwap_from_tick_or_candles(code, tick, self.candle_builder),
                "recent_support": _recent_support(code, tick, self.candle_builder),
                "recent_high": _recent_high(code, tick, self.candle_builder),
                "momentum_1m": _candle_momentum(code, self.candle_builder, 1),
                "momentum_3m": _candle_momentum(code, self.candle_builder, 3),
                "spread_ticks": int(getattr(tick, "spread_ticks", 0) or 0) if tick is not None else 0,
            },
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
        market_status = str(details.get("market_status") or details.get("market_regime_status") or "").upper()
        theme_status = str(details.get("theme_status") or details.get("theme_board_theme_status") or "").upper()
        if position.risk_status == PositionRiskStatus.DATA_WAIT:
            data_level = "DATA_WAIT"
            reasons.append("POSITION_DATA_WAIT")
        if position.risk_status == PositionRiskStatus.STALE_DATA_RISK:
            data_level = "STALE"
            reasons.append("POSITION_STALE_DATA_RISK")
        if theme_status in {"WEAK_THEME", "WATCH_THEME"}:
            theme_level = "CAUTION" if theme_status == "WATCH_THEME" else "REDUCE"
            reasons.append("THEME_RISK_ELEVATED")
        if market_status == "RISK_OFF":
            market_level = "REDUCE"
            reasons.append("MARKET_RISK_OFF")
        elif market_status == "WEAK":
            market_level = "CAUTION"
            reasons.append("MARKET_WEAK")
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
            reason_codes=tuple(_dedupe(reasons)),
            details={"theme_status": theme_status, "market_status": market_status},
        )

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
        total_exposure = 0
        weighted_return = 0.0
        realized_values: list[float] = []
        for position in positions:
            exposure = max(0, int(position.remaining_quantity or 0)) * max(0, int(position.current_price or 0))
            total_exposure += exposure
            exposure_by_theme[position.theme_name or position.theme_id or "UNKNOWN"] += exposure
            side = str((position.details or {}).get("market_side") or "UNKNOWN")
            exposure_by_side[side] += exposure
            weighted_return += exposure * float(position.unrealized_return_pct or 0.0)
            realized_values.append(float(position.realized_return_pct or 0.0))
        unrealized = weighted_return / total_exposure if total_exposure > 0 else 0.0
        max_drawdown = min([float(position.max_drawdown_pct or 0.0) for position in positions] or [0.0])
        daily_realized = sum(realized_values) / len(realized_values) if realized_values else 0.0
        reasons: list[str] = []
        risk_level = "NORMAL"
        if any(item.data_risk_level in {"DATA_WAIT", "STALE"} for item in position_risks):
            risk_level = _max_risk_level([risk_level, "CAUTION"])
            reasons.append("PORTFOLIO_DATA_RISK")
        if any(item.market_risk_level == "REDUCE" for item in position_risks):
            risk_level = _max_risk_level([risk_level, "REDUCE"])
            reasons.append("PORTFOLIO_MARKET_RISK")
        if total_exposure >= self.config.stop_new_entry_exposure_krw > 0:
            risk_level = _max_risk_level([risk_level, "STOP_NEW_ENTRY"])
            reasons.append("PORTFOLIO_EXPOSURE_LIMIT")
        if max_drawdown <= self.config.reduce_drawdown_pct:
            risk_level = _max_risk_level([risk_level, "REDUCE"])
            reasons.append("PORTFOLIO_DRAWDOWN_REDUCE")
        if max_drawdown <= self.config.kill_switch_drawdown_pct:
            risk_level = _max_risk_level([risk_level, "KILL_SWITCH_RECOMMENDED"])
            reasons.append("PORTFOLIO_DRAWDOWN_KILL_SWITCH")
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
            reason_codes=tuple(_dedupe(reasons)),
            details={"position_risk_counts": dict(Counter(item.risk_level for item in position_risks))},
        )

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
    reason_counter = Counter()
    for item in risks:
        for reason in list(item.get("reason_codes") or []):
            reason_counter[str(reason)] += 1
    return {
        "calculated_at": data.get("calculated_at") or portfolio.get("calculated_at", ""),
        "trade_date": data.get("trade_date") or portfolio.get("trade_date", ""),
        "portfolio_risk_level": portfolio.get("risk_level", "NORMAL"),
        "open_position_count": int(portfolio.get("open_position_count") or len(position_items)),
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
    return max(0, int((now - opened).total_seconds() // 60))


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


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
