from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, time
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.position_risk import (
    PositionRiskStatus,
    PositionRuntimeService,
    PositionRuntimeSnapshot,
    _position_snapshot_from,
)


EXIT_ENGINE_OUTPUT_MODE = "OBSERVE"
EXIT_ENGINE_SOURCE = "exit_engine_reboot_v2"


class ExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_FAST = "STOP_LOSS_FAST"
    SUPPORT_LOSS = "SUPPORT_LOSS"
    VWAP_LOSS = "VWAP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_EXIT = "TIME_EXIT"
    THEME_WEAK_EXIT = "THEME_WEAK_EXIT"
    LEADER_COLLAPSE_EXIT = "LEADER_COLLAPSE_EXIT"
    MARKET_WEAK_EXIT = "MARKET_WEAK_EXIT"
    MARKET_RISK_OFF_EXIT = "MARKET_RISK_OFF_EXIT"
    BREADTH_COLLAPSE_EXIT = "BREADTH_COLLAPSE_EXIT"
    STALE_DATA_EXIT_GUARD = "STALE_DATA_EXIT_GUARD"
    MANUAL_PROTECT = "MANUAL_PROTECT"
    HOLD = "HOLD"


class ExitDecisionStatus(str, Enum):
    HOLD = "HOLD"
    SCALE_OUT = "SCALE_OUT"
    EXIT_NOW = "EXIT_NOW"
    WAIT_CONFIRMATION = "WAIT_CONFIRMATION"
    DATA_WAIT = "DATA_WAIT"
    ALREADY_CLOSED = "ALREADY_CLOSED"


@dataclass(frozen=True)
class ExitEngineConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: int = 5
    allow_dry_run_sell_intents: bool = False
    max_tick_age_sec: int = 10
    stop_loss_pct: float = -2.0
    fast_stop_loss_pct: float = -1.2
    fast_stop_loss_minutes: int = 5
    support_break_confirm_candles: int = 2
    vwap_break_confirm_candles: int = 2
    take_profit_1_pct: float = 5.0
    take_profit_1_ratio: float = 0.5
    trailing_activate_pct: float = 3.0
    trailing_gap_pct: float = 1.2
    max_hold_minutes: int = 30
    min_return_after_hold_pct: float = 0.0
    force_exit_before_close_min: int = 10
    theme_weak_confirmation_cycles: int = 2
    leader_collapse_confirmation_cycles: int = 1

    @classmethod
    def from_env(cls) -> "ExitEngineConfig":
        return cls(
            enabled=_env_bool("TRADING_EXIT_ENGINE_ENABLED", False),
            observe_only=_env_bool("TRADING_EXIT_ENGINE_OBSERVE_ONLY", True),
            interval_sec=max(1, _env_int("TRADING_EXIT_ENGINE_INTERVAL_SEC", 5)),
            allow_dry_run_sell_intents=_env_bool("TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS", False),
            max_tick_age_sec=max(1, _env_int("TRADING_EXIT_MAX_TICK_AGE_SEC", 10)),
            stop_loss_pct=_env_float("TRADING_EXIT_STOP_LOSS_PCT", -2.0),
            fast_stop_loss_pct=_env_float("TRADING_EXIT_FAST_STOP_LOSS_PCT", -1.2),
            fast_stop_loss_minutes=max(1, _env_int("TRADING_EXIT_FAST_STOP_LOSS_MINUTES", 5)),
            support_break_confirm_candles=max(1, _env_int("TRADING_EXIT_SUPPORT_BREAK_CONFIRM_CANDLES", 2)),
            vwap_break_confirm_candles=max(1, _env_int("TRADING_EXIT_VWAP_BREAK_CONFIRM_CANDLES", 2)),
            take_profit_1_pct=_env_float("TRADING_EXIT_TAKE_PROFIT_1_PCT", 5.0),
            take_profit_1_ratio=max(0.0, min(1.0, _env_float("TRADING_EXIT_TAKE_PROFIT_1_RATIO", 0.5))),
            trailing_activate_pct=_env_float("TRADING_EXIT_TRAILING_ACTIVATE_PCT", 3.0),
            trailing_gap_pct=max(0.0, _env_float("TRADING_EXIT_TRAILING_GAP_PCT", 1.2)),
            max_hold_minutes=max(1, _env_int("TRADING_EXIT_MAX_HOLD_MINUTES", 30)),
            min_return_after_hold_pct=_env_float("TRADING_EXIT_MIN_RETURN_AFTER_HOLD_PCT", 0.0),
            force_exit_before_close_min=max(0, _env_int("TRADING_EXIT_FORCE_EXIT_BEFORE_CLOSE_MIN", 10)),
            theme_weak_confirmation_cycles=max(1, _env_int("TRADING_THEME_WEAK_CONFIRMATION_CYCLES", 2)),
            leader_collapse_confirmation_cycles=max(1, _env_int("TRADING_LEADER_COLLAPSE_CONFIRMATION_CYCLES", 1)),
        )


@dataclass(frozen=True)
class ExitDecision:
    trade_date: str
    calculated_at: str
    position_id: str
    candidate_id: int | None
    code: str
    name: str = ""
    exit_status: ExitDecisionStatus = ExitDecisionStatus.HOLD
    exit_reason: ExitReason = ExitReason.HOLD
    quantity: int = 0
    price_hint: int = 0
    hoga_hint: str = ""
    current_price: int = 0
    current_return_pct: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    operator_message_ko: str = ""
    dry_run_sell_intent_allowed: bool = False
    live_order_allowed: bool = False
    gateway_command_created: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class DryRunSellIntent:
    trade_date: str
    calculated_at: str
    position_id: str
    candidate_id: int | None
    code: str
    side: str = "sell"
    quantity: int = 0
    price_hint: int = 0
    exit_reason: str = ""
    exit_status: str = ""
    hoga_hint: str = ""
    idempotency_key: str = ""
    source: str = EXIT_ENGINE_SOURCE
    live_order_allowed: bool = False
    gateway_command_created: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class ExitDecisionResult:
    trade_date: str
    calculated_at: str
    decisions: tuple[ExitDecision, ...] = ()
    dry_run_sell_intents: tuple[DryRunSellIntent, ...] = ()
    evaluated_count: int = 0
    dry_run_sell_intent_count: int = 0
    saved: bool = False
    warnings: tuple[str, ...] = ()
    output_mode: str = EXIT_ENGINE_OUTPUT_MODE
    live_order_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class ExitEngine:
    def __init__(
        self,
        db: Any,
        *,
        candle_builder: CandleBuilder | None = None,
        config: ExitEngineConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.candle_builder = candle_builder
        self.config = config or ExitEngineConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        positions: Iterable[PositionRuntimeSnapshot | Mapping[str, Any]] | None = None,
        save: bool = True,
    ) -> ExitDecisionResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        position_items = [_position_snapshot_from(item) for item in (positions if positions is not None else self._latest_positions(trade_date))]
        decisions = tuple(self._decision(position, current) for position in position_items)
        self._merge_candidate_metadata(decisions, trade_date, current)
        intents = tuple(self._dry_run_sell_intents(decisions, position_items, current))
        saved = False
        if save:
            if hasattr(self.db, "save_exit_decisions_reboot"):
                self.db.save_exit_decisions_reboot([decision.to_dict() for decision in decisions])
                saved = True
            if intents and hasattr(self.db, "save_dry_run_sell_intents"):
                self.db.save_dry_run_sell_intents([intent.to_dict() for intent in intents])
                saved = True
        return ExitDecisionResult(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            decisions=decisions,
            dry_run_sell_intents=intents,
            evaluated_count=len(decisions),
            dry_run_sell_intent_count=len(intents),
            saved=saved,
            live_order_allowed=False,
        )

    def _decision(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision:
        if int(position.remaining_quantity or 0) <= 0 or position.risk_status == PositionRiskStatus.CLOSED:
            return self._make_decision(position, now, ExitDecisionStatus.ALREADY_CLOSED, ExitReason.HOLD, ("POSITION_ALREADY_CLOSED",), quantity=0)
        guard = self._data_guard(position, now)
        if guard is not None:
            return guard
        ambiguous = self._ambiguous_stop_take_profit(position)
        if ambiguous:
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.EXIT_NOW,
                ExitReason.STOP_LOSS,
                ("AMBIGUOUS_BAR_STOP_PRIORITY", "STOP_LOSS"),
                details={"ambiguous_bar": True},
            )
        if position.holding_minutes <= self.config.fast_stop_loss_minutes and position.current_return_pct <= self.config.fast_stop_loss_pct:
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.STOP_LOSS_FAST, ("FAST_STOP_LOSS",))
        if position.current_return_pct <= self.config.stop_loss_pct:
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.STOP_LOSS, ("STOP_LOSS",))
        market = self._market_decision(position, now)
        if market is not None:
            return market
        support = self._support_loss(position, now)
        if support is not None:
            return support
        vwap = self._vwap_loss(position, now)
        if vwap is not None:
            return vwap
        trailing = self._trailing_stop(position, now)
        if trailing is not None:
            return trailing
        theme = self._theme_decision(position, now)
        if theme is not None:
            return theme
        take_profit = self._take_profit(position, now)
        if take_profit is not None:
            return take_profit
        time_exit = self._time_exit(position, now)
        if time_exit is not None:
            return time_exit
        return self._make_decision(position, now, ExitDecisionStatus.HOLD, ExitReason.HOLD, ("HOLD",))

    def _data_guard(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        flags = set(position.data_quality_flags or ())
        if position.risk_status == PositionRiskStatus.STALE_DATA_RISK or "LATEST_TICK_STALE" in flags:
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.DATA_WAIT,
                ExitReason.STALE_DATA_EXIT_GUARD,
                ("STALE_DATA_RISK", "SELL_INTENT_BLOCKED_BY_DATA_GUARD"),
                quantity=0,
            )
        if position.risk_status == PositionRiskStatus.DATA_WAIT or "CURRENT_PRICE_MISSING" in flags or "LATEST_TICK_MISSING" in flags:
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.DATA_WAIT,
                ExitReason.STALE_DATA_EXIT_GUARD,
                ("POSITION_DATA_WAIT", "SELL_INTENT_BLOCKED_BY_DATA_GUARD"),
                quantity=0,
            )
        if int(position.current_price or 0) <= 0:
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.DATA_WAIT,
                ExitReason.STALE_DATA_EXIT_GUARD,
                ("CURRENT_PRICE_INVALID", "SELL_INTENT_BLOCKED_BY_DATA_GUARD"),
                quantity=0,
            )
        return None

    def _market_decision(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        context = self._context(position)
        status = str(context.get("market_status") or context.get("market_regime_status") or "").upper()
        action = str(context.get("market_action") or "").upper()
        breadth = str(context.get("breadth_status") or "").upper()
        if status == "RISK_OFF":
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.MARKET_RISK_OFF_EXIT, ("MARKET_RISK_OFF_EXIT",))
        if breadth in {"COLLAPSE", "BREADTH_COLLAPSE"} or bool(context.get("breadth_collapse")):
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.BREADTH_COLLAPSE_EXIT, ("BREADTH_COLLAPSE_EXIT",))
        if status == "WEAK" or action == "BLOCK_NEW_ENTRY":
            if position.current_return_pct < 0:
                return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.MARKET_WEAK_EXIT, ("MARKET_WEAK_EXIT",))
            return self._make_decision(position, now, ExitDecisionStatus.WAIT_CONFIRMATION, ExitReason.MARKET_WEAK_EXIT, ("MARKET_WEAK_CONFIRMATION",), quantity=0)
        return None

    def _support_loss(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        support = int(_first_number((position.details or {}).get("recent_support"), position.stop_loss_price))
        if support <= 0:
            return None
        if self._consecutive_closes_below(position.code, support, self.config.support_break_confirm_candles):
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.SUPPORT_LOSS, ("SUPPORT_LOSS",))
        return None

    def _vwap_loss(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        vwap = _float((position.details or {}).get("vwap"))
        if vwap <= 0:
            return None
        momentum = _float((position.details or {}).get("momentum_1m"))
        if momentum < 0 and self._consecutive_closes_below(position.code, vwap, self.config.vwap_break_confirm_candles):
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.VWAP_LOSS, ("VWAP_LOSS",))
        return None

    def _trailing_stop(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        active = bool(position.trailing_active or position.first_profit_taken or position.max_return_pct >= self.config.trailing_activate_pct)
        if not active:
            return None
        trailing_stop = int(position.trailing_stop_price or 0)
        if trailing_stop <= 0 and position.highest_price_since_entry > 0:
            trailing_stop = int(round(position.highest_price_since_entry * (1.0 - self.config.trailing_gap_pct / 100.0)))
        if trailing_stop > 0 and position.current_price <= trailing_stop:
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.EXIT_NOW,
                ExitReason.TRAILING_STOP,
                ("TRAILING_STOP",),
                details={"trailing_stop_price": trailing_stop, "trailing_active": True},
            )
        return None

    def _theme_decision(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        context = self._context(position)
        theme_status = str(context.get("theme_status") or context.get("theme_board_theme_status") or "").upper()
        role = str(context.get("stock_role") or context.get("theme_board_stock_role") or "").upper()
        weak_count = int(_first_number(context.get("theme_weak_confirmation_count"), context.get("theme_weak_confirmed_count")))
        leader_count = int(_first_number(context.get("leader_collapse_confirmation_count"), context.get("leader_collapse_confirmed_count")))
        leader_broken = bool(context.get("leader_vwap_broken") or context.get("leader_support_broken") or context.get("leader_collapsed"))
        role_down = role in {"FOLLOWER", "LATE_LAGGARD", "WEAK_MEMBER"} and bool(context.get("stock_role_downgraded"))
        if leader_broken or role_down:
            if leader_count >= self.config.leader_collapse_confirmation_cycles:
                return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.LEADER_COLLAPSE_EXIT, ("LEADER_COLLAPSE_EXIT",))
            return self._make_decision(position, now, ExitDecisionStatus.WAIT_CONFIRMATION, ExitReason.LEADER_COLLAPSE_EXIT, ("LEADER_COLLAPSE_CONFIRMATION_WAIT",), quantity=0)
        if theme_status in {"WEAK_THEME", "WATCH_THEME"}:
            if weak_count >= self.config.theme_weak_confirmation_cycles:
                return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.THEME_WEAK_EXIT, ("THEME_WEAK_EXIT",))
            return self._make_decision(position, now, ExitDecisionStatus.WAIT_CONFIRMATION, ExitReason.THEME_WEAK_EXIT, ("THEME_WEAK_CONFIRMATION_WAIT",), quantity=0)
        return None

    def _take_profit(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        if position.first_profit_taken:
            return None
        if position.current_return_pct >= self.config.take_profit_1_pct:
            quantity = max(1, int(round(position.remaining_quantity * self.config.take_profit_1_ratio)))
            return self._make_decision(
                position,
                now,
                ExitDecisionStatus.SCALE_OUT,
                ExitReason.TAKE_PROFIT,
                ("TAKE_PROFIT_1",),
                quantity=min(quantity, int(position.remaining_quantity or 0)),
                details={"take_profit_ratio": self.config.take_profit_1_ratio, "first_profit_taken": False},
            )
        return None

    def _time_exit(self, position: PositionRuntimeSnapshot, now: datetime) -> ExitDecision | None:
        if position.holding_minutes >= self.config.max_hold_minutes and position.current_return_pct <= self.config.min_return_after_hold_pct:
            return self._make_decision(position, now, ExitDecisionStatus.EXIT_NOW, ExitReason.TIME_EXIT, ("TIME_EXIT_MAX_HOLD",))
        if self.config.force_exit_before_close_min > 0:
            close_at = datetime.combine(now.date(), time(15, 30))
            minutes_to_close = int((close_at - now).total_seconds() // 60)
            if 0 <= minutes_to_close <= self.config.force_exit_before_close_min:
                return self._make_decision(
                    position,
                    now,
                    ExitDecisionStatus.EXIT_NOW,
                    ExitReason.TIME_EXIT,
                    ("TIME_EXIT_BEFORE_CLOSE",),
                    details={"minutes_to_close": minutes_to_close},
                )
        return None

    def _make_decision(
        self,
        position: PositionRuntimeSnapshot,
        now: datetime,
        status: ExitDecisionStatus,
        reason: ExitReason,
        reason_codes: Iterable[str],
        *,
        quantity: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> ExitDecision:
        qty = int(position.remaining_quantity or 0) if quantity is None else int(quantity or 0)
        dry_run_allowed = self._dry_run_allowed(position, status, qty)
        merged_details = {
            "position": position.to_dict(),
            "observe_only": self.config.observe_only,
            "dry_run_sell_intent_emitter": "disabled" if not self.config.allow_dry_run_sell_intents else "guarded",
            **dict(details or {}),
        }
        return ExitDecision(
            trade_date=position.trade_date,
            calculated_at=now.isoformat(),
            position_id=position.position_id,
            candidate_id=position.candidate_id,
            code=position.code,
            name=position.name,
            exit_status=status,
            exit_reason=reason,
            quantity=max(0, min(qty, int(position.remaining_quantity or 0))),
            price_hint=int(position.current_price or 0),
            hoga_hint="marketable_limit" if status in {ExitDecisionStatus.EXIT_NOW, ExitDecisionStatus.SCALE_OUT} else "",
            current_price=int(position.current_price or 0),
            current_return_pct=float(position.current_return_pct or 0.0),
            data_quality_flags=tuple(position.data_quality_flags or ()),
            reason_codes=tuple(_dedupe(reason_codes)),
            operator_message_ko=_operator_message(status, reason),
            dry_run_sell_intent_allowed=dry_run_allowed,
            live_order_allowed=False,
            gateway_command_created=False,
            details=merged_details,
        )

    def _dry_run_allowed(self, position: PositionRuntimeSnapshot, status: ExitDecisionStatus, quantity: int) -> bool:
        if not self.config.allow_dry_run_sell_intents:
            return False
        if status not in {ExitDecisionStatus.SCALE_OUT, ExitDecisionStatus.EXIT_NOW}:
            return False
        if str(position.source_type or "").upper() not in {"DRY_RUN", "VIRTUAL", "LIVE_SIM_OBSERVED"}:
            return False
        if quantity <= 0 or int(position.remaining_quantity or 0) <= 0:
            return False
        if position.risk_status in {PositionRiskStatus.DATA_WAIT, PositionRiskStatus.STALE_DATA_RISK}:
            return False
        if int(position.current_price or 0) <= 0:
            return False
        return True

    def _dry_run_sell_intents(
        self,
        decisions: Iterable[ExitDecision],
        positions: Iterable[PositionRuntimeSnapshot],
        now: datetime,
    ) -> list[DryRunSellIntent]:
        by_id = {item.position_id: item for item in positions}
        result: list[DryRunSellIntent] = []
        finder = getattr(self.db, "find_dry_run_sell_intent_by_idempotency", None)
        for decision in decisions:
            if not decision.dry_run_sell_intent_allowed:
                continue
            key = _idempotency_key(decision, now)
            if callable(finder) and finder(key):
                continue
            position = by_id.get(decision.position_id)
            result.append(
                DryRunSellIntent(
                    trade_date=decision.trade_date,
                    calculated_at=decision.calculated_at,
                    position_id=decision.position_id,
                    candidate_id=decision.candidate_id,
                    code=decision.code,
                    quantity=decision.quantity,
                    price_hint=decision.price_hint,
                    exit_reason=decision.exit_reason.value if isinstance(decision.exit_reason, ExitReason) else str(decision.exit_reason),
                    exit_status=decision.exit_status.value if isinstance(decision.exit_status, ExitDecisionStatus) else str(decision.exit_status),
                    hoga_hint=decision.hoga_hint,
                    idempotency_key=key,
                    live_order_allowed=False,
                    gateway_command_created=False,
                    details={
                        "decision": decision.to_dict(),
                        "position_source_type": position.source_type if position is not None else "",
                        "exit_bucket": now.strftime("%Y%m%d%H%M"),
                    },
                )
            )
        return result

    def _latest_positions(self, trade_date: str) -> list[dict[str, Any]]:
        loader = getattr(self.db, "latest_position_runtime_snapshots", None)
        if not callable(loader):
            return []
        return list(loader(trade_date=trade_date) or [])

    def _context(self, position: PositionRuntimeSnapshot) -> dict[str, Any]:
        details = dict(position.details or {})
        candidate_metadata = dict(details.get("candidate_metadata") or {})
        return {**candidate_metadata, **details}

    def _consecutive_closes_below(self, code: str, level: float, count: int) -> bool:
        if self.candle_builder is None or level <= 0:
            return False
        candles = self.candle_builder.completed_candles(code, 1)[-count:]
        if len(candles) < count:
            return False
        return all(float(candle.close or 0) < level for candle in candles)

    def _ambiguous_stop_take_profit(self, position: PositionRuntimeSnapshot) -> bool:
        if self.candle_builder is None:
            return False
        candles = self.candle_builder.completed_candles(position.code, 1)
        if not candles:
            return False
        candle = candles[-1]
        stop_price = int(position.stop_loss_price or 0)
        take_price = int(position.take_profit_price or 0)
        return stop_price > 0 and take_price > 0 and int(candle.low or 0) <= stop_price and int(candle.high or 0) >= take_price

    def _merge_candidate_metadata(self, decisions: Iterable[ExitDecision], trade_date: str, now: datetime) -> int:
        updated = 0
        loader = getattr(self.db, "load_candidate_by_id", None)
        saver = getattr(self.db, "save_candidate", None)
        if not callable(loader) or not callable(saver):
            return updated
        for decision in decisions:
            if decision.candidate_id is None:
                continue
            candidate = loader(int(decision.candidate_id))
            if candidate is None:
                continue
            metadata = dict(candidate.metadata or {})
            metadata["exit_status"] = decision.exit_status.value if isinstance(decision.exit_status, ExitDecisionStatus) else str(decision.exit_status)
            metadata["exit_reason"] = decision.exit_reason.value if isinstance(decision.exit_reason, ExitReason) else str(decision.exit_reason)
            metadata["exit_reason_codes"] = list(decision.reason_codes)
            metadata["current_return_pct"] = decision.current_return_pct
            metadata["updated_by_exit_engine_at"] = now.isoformat()
            metadata["exit_live_order_allowed"] = False
            metadata["exit_gateway_command_created"] = False
            candidate.metadata = metadata
            saver(candidate)
            updated += 1
        return updated


class ExitEngineRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        config: ExitEngineConfig | None = None,
        position_runtime_service: PositionRuntimeService | None = None,
        engine: ExitEngine | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or ExitEngineConfig.from_env()
        self.clock = clock or datetime.now
        self.position_runtime_service = position_runtime_service or PositionRuntimeService(
            db,
            market_data=market_data,
            candle_builder=candle_builder,
            clock=self.clock,
        )
        self.engine = engine or ExitEngine(db, candle_builder=candle_builder, config=self.config, clock=self.clock)
        self.last_summary: dict[str, Any] = {"status": "DISABLED", "enabled": False, "output_mode": EXIT_ENGINE_OUTPUT_MODE}
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"status": "DISABLED", "enabled": False, "output_mode": EXIT_ENGINE_OUTPUT_MODE}
            return dict(self.last_summary)
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        runtime_result = self.position_runtime_service.build(trade_date=current.date().isoformat(), now=current, save=True)
        result = self.engine.build(trade_date=current.date().isoformat(), now=current, positions=runtime_result.positions, save=True)
        self.last_run_at = current
        self.last_summary = exit_engine_dashboard_payload(result)
        self.last_summary["enabled"] = True
        self.last_summary["status"] = "OK"
        return dict(self.last_summary)


def exit_engine_dashboard_payload(result: ExitDecisionResult | Mapping[str, Any] | list[Mapping[str, Any]]) -> dict[str, Any]:
    if isinstance(result, ExitDecisionResult):
        data = result.to_dict()
        decisions = list(data.get("decisions") or [])
        intents = list(data.get("dry_run_sell_intents") or [])
        calculated_at = data.get("calculated_at", "")
        trade_date = data.get("trade_date", "")
    elif isinstance(result, list):
        decisions = [dict(item or {}) for item in result]
        intents = []
        calculated_at = str(decisions[0].get("calculated_at") or "") if decisions else ""
        trade_date = str(decisions[0].get("trade_date") or "") if decisions else ""
    else:
        data = dict(result or {})
        decisions = list(data.get("decisions") or [])
        intents = list(data.get("dry_run_sell_intents") or [])
        calculated_at = data.get("calculated_at", "")
        trade_date = data.get("trade_date", "")
    status_counts = Counter(str(item.get("exit_status") or "") for item in decisions)
    reason_counter = Counter(str(item.get("exit_reason") or "") for item in decisions if str(item.get("exit_reason") or ""))
    return {
        "calculated_at": calculated_at,
        "trade_date": trade_date,
        "open_position_count": len(decisions),
        "hold_count": status_counts.get(ExitDecisionStatus.HOLD.value, 0),
        "scale_out_count": status_counts.get(ExitDecisionStatus.SCALE_OUT.value, 0),
        "exit_now_count": status_counts.get(ExitDecisionStatus.EXIT_NOW.value, 0),
        "wait_confirmation_count": status_counts.get(ExitDecisionStatus.WAIT_CONFIRMATION.value, 0),
        "data_wait_count": status_counts.get(ExitDecisionStatus.DATA_WAIT.value, 0),
        "already_closed_count": status_counts.get(ExitDecisionStatus.ALREADY_CLOSED.value, 0),
        "dry_run_sell_intent_count": len(intents),
        "top_exit_reasons": [{"reason": key, "count": count} for key, count in reason_counter.most_common(10)],
        "ready_exit_decisions": [
            item for item in decisions if str(item.get("exit_status") or "") in {ExitDecisionStatus.SCALE_OUT.value, ExitDecisionStatus.EXIT_NOW.value}
        ][:10],
        "warnings": [],
        "output_mode": EXIT_ENGINE_OUTPUT_MODE,
        "live_order_allowed": False,
    }


def exit_engine_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    loader = getattr(db, "latest_exit_decisions_reboot", None)
    if not callable(loader):
        return {"status": "UNAVAILABLE", "output_mode": EXIT_ENGINE_OUTPUT_MODE, "live_order_allowed": False}
    decisions = loader(trade_date=trade_date)
    if not decisions:
        return {"status": "EMPTY", "output_mode": EXIT_ENGINE_OUTPUT_MODE, "live_order_allowed": False}
    payload = exit_engine_dashboard_payload(decisions)
    intent_loader = getattr(db, "latest_dry_run_sell_intents", None)
    if callable(intent_loader):
        payload["dry_run_sell_intent_count"] = len(intent_loader(trade_date=trade_date))
    payload["status"] = "OK"
    return payload


def _idempotency_key(decision: ExitDecision, now: datetime) -> str:
    reason = decision.exit_reason.value if isinstance(decision.exit_reason, ExitReason) else str(decision.exit_reason)
    return f"reboot_exit_dry_run:{decision.trade_date}:{decision.position_id}:{reason}:{now.strftime('%Y%m%d%H%M')}"


def _operator_message(status: ExitDecisionStatus, reason: ExitReason) -> str:
    if status == ExitDecisionStatus.EXIT_NOW:
        return f"Reboot V2 exit now: {reason.value if isinstance(reason, ExitReason) else reason}. LIVE order is disabled."
    if status == ExitDecisionStatus.SCALE_OUT:
        return "Reboot V2 scale-out signal observed. LIVE order is disabled."
    if status == ExitDecisionStatus.DATA_WAIT:
        return "Exit decision is waiting for reliable realtime data. Sell intent is blocked."
    if status == ExitDecisionStatus.WAIT_CONFIRMATION:
        return "Exit risk is elevated but confirmation is still pending."
    if status == ExitDecisionStatus.ALREADY_CLOSED:
        return "Position is already closed."
    return "Hold position under observation."


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
