from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import Candidate, CandidateState


ENTRY_ENGINE_OUTPUT_MODE = "OBSERVE"
ENTRY_ENGINE_CANDIDATE_STATES = {CandidateState.WATCHING}


class EntryCheckStatus(str, Enum):
    PASS = "PASS"
    WAIT = "WAIT"
    BLOCK = "BLOCK"
    DATA_WAIT = "DATA_WAIT"


class EntryDecisionStatus(str, Enum):
    OBSERVE_READY = "OBSERVE_READY"
    WAIT = "WAIT"
    HARD_BLOCK = "HARD_BLOCK"
    DATA_WAIT = "DATA_WAIT"
    MARKET_WAIT = "MARKET_WAIT"
    THEME_WAIT = "THEME_WAIT"
    PRICE_WAIT = "PRICE_WAIT"


class PriceLocationStatus(str, Enum):
    GOOD_PULLBACK = "GOOD_PULLBACK"
    PULLBACK_RECLAIM = "PULLBACK_RECLAIM"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    BREAKOUT_CONTINUATION = "BREAKOUT_CONTINUATION"
    CHASE_HIGH = "CHASE_HIGH"
    VWAP_OVEREXTENDED = "VWAP_OVEREXTENDED"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    DEEP_PULLBACK = "DEEP_PULLBACK"
    UNKNOWN = "UNKNOWN"
    DATA_WAIT = "DATA_WAIT"


@dataclass(frozen=True)
class EntryEngineConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: int = 5
    allow_dry_run_intents: bool = False
    max_tick_age_sec: int = 10
    min_1m_candles: int = 3
    require_realtime_tick: bool = True
    require_vwap: bool = False
    require_turnover: bool = True
    good_pullback_min_pct: float = 0.7
    good_pullback_max_pct: float = 3.5
    max_vwap_gap_leader_pct: float = 5.0
    max_vwap_gap_co_leader_pct: float = 4.0
    max_vwap_gap_follower_pct: float = 3.0
    chase_high_pullback_min_pct: float = 0.3
    max_spread_ticks: int = 3
    vi_cooldown_sec: int = 180
    upper_limit_min_gap_pct: float = 3.0

    @classmethod
    def from_env(cls) -> "EntryEngineConfig":
        return cls(
            enabled=_env_bool("TRADING_ENTRY_ENGINE_ENABLED", False),
            observe_only=_env_bool("TRADING_ENTRY_ENGINE_OBSERVE_ONLY", True),
            interval_sec=max(1, _env_int("TRADING_ENTRY_ENGINE_INTERVAL_SEC", 5)),
            allow_dry_run_intents=_env_bool("TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS", False),
            max_tick_age_sec=max(1, _env_int("TRADING_ENTRY_MAX_TICK_AGE_SEC", 10)),
            min_1m_candles=max(0, _env_int("TRADING_ENTRY_MIN_1M_CANDLES", 3)),
            require_realtime_tick=_env_bool("TRADING_ENTRY_REQUIRE_REALTIME_TICK", True),
            require_vwap=_env_bool("TRADING_ENTRY_REQUIRE_VWAP", False),
            require_turnover=_env_bool("TRADING_ENTRY_REQUIRE_TURNOVER", True),
            good_pullback_min_pct=_env_float("TRADING_ENTRY_GOOD_PULLBACK_MIN_PCT", 0.7),
            good_pullback_max_pct=_env_float("TRADING_ENTRY_GOOD_PULLBACK_MAX_PCT", 3.5),
            max_vwap_gap_leader_pct=_env_float("TRADING_ENTRY_MAX_VWAP_GAP_LEADER_PCT", 5.0),
            max_vwap_gap_co_leader_pct=_env_float("TRADING_ENTRY_MAX_VWAP_GAP_CO_LEADER_PCT", 4.0),
            max_vwap_gap_follower_pct=_env_float("TRADING_ENTRY_MAX_VWAP_GAP_FOLLOWER_PCT", 3.0),
            chase_high_pullback_min_pct=_env_float("TRADING_ENTRY_CHASE_HIGH_PULLBACK_MIN_PCT", 0.3),
            max_spread_ticks=max(0, _env_int("TRADING_ENTRY_MAX_SPREAD_TICKS", 3)),
            vi_cooldown_sec=max(0, _env_int("TRADING_ENTRY_VI_COOLDOWN_SEC", 180)),
            upper_limit_min_gap_pct=_env_float("TRADING_ENTRY_UPPER_LIMIT_MIN_GAP_PCT", 3.0),
        )


@dataclass(frozen=True)
class _EntryCheck:
    status: EntryCheckStatus
    reason_codes: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class EntryDataReadiness(_EntryCheck):
    data_quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryThemeReadiness(_EntryCheck):
    pass


@dataclass(frozen=True)
class EntryMarketReadiness(_EntryCheck):
    position_size_multiplier_hint: float = 0.0


@dataclass(frozen=True)
class EntryRoleReadiness(_EntryCheck):
    dry_run_role_allowed: bool = False


@dataclass(frozen=True)
class EntryPriceTiming(_EntryCheck):
    price_location: PriceLocationStatus = PriceLocationStatus.UNKNOWN
    limit_price_hint: int = 0
    stop_loss_price_hint: int = 0
    take_profit_price_hint: int = 0
    reference_price: float = 0.0
    vwap: float = 0.0
    support_price: float = 0.0
    breakout_level: float = 0.0


@dataclass(frozen=True)
class EntryDecision:
    trade_date: str
    calculated_at: str
    candidate_id: int | None
    code: str
    name: str = ""
    theme_id: str = ""
    theme_name: str = ""
    theme_status: str = ""
    stock_role: str = ""
    market_side: str = ""
    market_status: str = ""
    market_action: str = ""
    price_location: str = PriceLocationStatus.UNKNOWN.value
    entry_status: EntryDecisionStatus = EntryDecisionStatus.WAIT
    data_ready_status: EntryCheckStatus = EntryCheckStatus.WAIT
    theme_ready_status: EntryCheckStatus = EntryCheckStatus.WAIT
    market_ready_status: EntryCheckStatus = EntryCheckStatus.WAIT
    role_ready_status: EntryCheckStatus = EntryCheckStatus.WAIT
    price_timing_status: EntryCheckStatus = EntryCheckStatus.WAIT
    current_price: int = 0
    reference_price: float = 0.0
    vwap: float = 0.0
    support_price: float = 0.0
    breakout_level: float = 0.0
    limit_price_hint: int = 0
    stop_loss_price_hint: int = 0
    take_profit_price_hint: int = 0
    position_size_multiplier_hint: float = 0.0
    ready_allowed: bool = False
    dry_run_intent_allowed: bool = False
    live_order_allowed: bool = False
    reason_codes: tuple[str, ...] = ()
    operator_message_ko: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class EntryDecisionResult:
    trade_date: str
    calculated_at: str
    decisions: tuple[EntryDecision, ...] = ()
    evaluated_count: int = 0
    updated_candidate_count: int = 0
    saved: bool = False
    dry_run_intent_created_count: int = 0
    warnings: tuple[str, ...] = ()
    output_mode: str = ENTRY_ENGINE_OUTPUT_MODE
    live_order_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class EntryEngine:
    def __init__(
        self,
        db: Any,
        *,
        market_data: MarketDataStore | None = None,
        candle_builder: CandleBuilder | None = None,
        config: EntryEngineConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or EntryEngineConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        save: bool = True,
    ) -> EntryDecisionResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        candidates = [
            candidate
            for candidate in list(self.db.list_candidates(trade_date=trade_date) or [])
            if candidate.state in ENTRY_ENGINE_CANDIDATE_STATES
        ]
        decisions = tuple(self._decision(candidate, trade_date, current) for candidate in candidates)
        updated_count = self._merge_candidate_metadata(decisions, trade_date, current)
        saved = False
        if save:
            saver = getattr(self.db, "save_entry_decisions", None)
            if callable(saver):
                saver([decision.to_dict() for decision in decisions])
                saved = True
        return EntryDecisionResult(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            decisions=decisions,
            evaluated_count=len(decisions),
            updated_candidate_count=updated_count,
            saved=saved,
            dry_run_intent_created_count=0,
            warnings=(),
            output_mode=ENTRY_ENGINE_OUTPUT_MODE,
            live_order_allowed=False,
        )

    def _decision(self, candidate: Candidate, trade_date: str, now: datetime) -> EntryDecision:
        metadata = dict(candidate.metadata or {})
        tick = self.market_data.latest_tick(candidate.code) if self.market_data is not None else None
        data = self._data_ready(candidate, tick, now)
        theme = self._theme_ready(candidate, metadata)
        market = self._market_ready(metadata)
        role = self._role_ready(metadata, market)
        price = self._price_timing(candidate, metadata, tick, data, theme, market, role)
        entry_status = self._entry_status(data, theme, market, role, price)
        reason_codes = _dedupe(
            list(data.reason_codes)
            + list(theme.reason_codes)
            + list(market.reason_codes)
            + list(role.reason_codes)
            + list(price.reason_codes)
        )
        dry_run_allowed = self._dry_run_intent_allowed(entry_status, metadata, market, role, price)
        checks = [
            {"check_name": "data_ready", **data.to_dict()},
            {"check_name": "theme_ready", **theme.to_dict()},
            {"check_name": "market_ready", **market.to_dict()},
            {"check_name": "role_ready", **role.to_dict()},
            {"check_name": "price_timing", **price.to_dict()},
        ]
        details = {
            "checks": checks,
            "data_quality_flags": list(data.data_quality_flags),
            "theme": theme.details,
            "market": market.details,
            "role": role.details,
            "price": price.details,
            "observe_only": self.config.observe_only,
            "dry_run_intent_emitter": "disabled" if not self.config.allow_dry_run_intents else "guarded",
        }
        current_price = int(getattr(tick, "price", 0) or 0) if tick is not None else 0
        return EntryDecision(
            trade_date=trade_date,
            calculated_at=now.isoformat(),
            candidate_id=candidate.id,
            code=candidate.code,
            name=candidate.name,
            theme_id=str(metadata.get("theme_board_theme_id") or ""),
            theme_name=str(metadata.get("theme_board_theme_name") or ""),
            theme_status=str(metadata.get("theme_board_theme_status") or ""),
            stock_role=str(metadata.get("theme_board_stock_role") or ""),
            market_side=str(metadata.get("market_side") or ""),
            market_status=str(metadata.get("market_regime_status") or ""),
            market_action=str(metadata.get("market_action") or ""),
            price_location=price.price_location.value,
            entry_status=entry_status,
            data_ready_status=data.status,
            theme_ready_status=theme.status,
            market_ready_status=market.status,
            role_ready_status=role.status,
            price_timing_status=price.status,
            current_price=current_price,
            reference_price=price.reference_price,
            vwap=price.vwap,
            support_price=price.support_price,
            breakout_level=price.breakout_level,
            limit_price_hint=price.limit_price_hint,
            stop_loss_price_hint=price.stop_loss_price_hint,
            take_profit_price_hint=price.take_profit_price_hint,
            position_size_multiplier_hint=market.position_size_multiplier_hint,
            ready_allowed=entry_status == EntryDecisionStatus.OBSERVE_READY,
            dry_run_intent_allowed=dry_run_allowed,
            live_order_allowed=False,
            reason_codes=tuple(reason_codes),
            operator_message_ko=_operator_message(entry_status, reason_codes),
            details=details,
        )

    def _data_ready(self, candidate: Candidate, tick: StrategyTick | None, now: datetime) -> EntryDataReadiness:
        reasons: list[str] = []
        flags: list[str] = []
        details: dict[str, Any] = {"code": candidate.code}
        if not candidate.code:
            return EntryDataReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("CODE_MISSING",), data_quality_flags=("CODE_MISSING",), details=details)
        if tick is None:
            return EntryDataReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("LATEST_TICK_MISSING",), data_quality_flags=("REALTIME_TICK_MISSING",), details=details)
        metadata = dict(getattr(tick, "metadata", {}) or {})
        if self.config.require_realtime_tick and str(metadata.get("price_source") or "").upper() == "TR_BACKFILL":
            return EntryDataReadiness(
                status=EntryCheckStatus.DATA_WAIT,
                reason_codes=("TR_PRICE_ONLY_NOT_READY",),
                data_quality_flags=("TR_BACKFILL_PRICE_ONLY",),
                details={**details, "price_source": "TR_BACKFILL"},
            )
        age_sec = max(0.0, (now - tick.timestamp).total_seconds())
        details["tick_age_sec"] = round(age_sec, 3)
        if age_sec > self.config.max_tick_age_sec:
            reasons.append("LATEST_TICK_STALE")
            flags.append("REALTIME_TICK_STALE")
        if int(tick.price or 0) <= 0:
            reasons.append("CURRENT_PRICE_MISSING")
            flags.append("CURRENT_PRICE_MISSING")
        turnover = float(tick.trade_value or 0.0)
        volume = int(tick.cum_volume or 0)
        details["turnover_krw"] = turnover
        details["cum_volume"] = volume
        if self.config.require_turnover and turnover <= 0 and volume <= 0:
            reasons.append("TURNOVER_MISSING")
            flags.append("TURNOVER_MISSING")
        candle_count = self._one_minute_candle_count(candidate.code)
        details["one_minute_candle_count"] = candle_count
        if candle_count < self.config.min_1m_candles:
            reasons.append("CANDLE_WARMUP")
            flags.append("CANDLE_WARMUP")
        vwap = self._vwap(candidate.code, tick)
        details["vwap"] = vwap
        if self.config.require_vwap and vwap <= 0:
            reasons.append("VWAP_NOT_READY")
            flags.append("VWAP_NOT_READY")
        day_high, day_low = self._day_high_low(candidate.code, tick)
        details["day_high"] = day_high
        details["day_low"] = day_low
        if day_high <= 0 or day_low <= 0:
            reasons.append("SESSION_HIGH_LOW_MISSING")
            flags.append("SESSION_HIGH_LOW_MISSING")
        if any(reason in reasons for reason in {"LATEST_TICK_STALE", "CURRENT_PRICE_MISSING", "CANDLE_WARMUP", "VWAP_NOT_READY", "SESSION_HIGH_LOW_MISSING"}):
            return EntryDataReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=tuple(_dedupe(reasons)), data_quality_flags=tuple(_dedupe(flags)), details=details)
        if reasons:
            return EntryDataReadiness(status=EntryCheckStatus.WAIT, reason_codes=tuple(_dedupe(reasons)), data_quality_flags=tuple(_dedupe(flags)), details=details)
        return EntryDataReadiness(status=EntryCheckStatus.PASS, reason_codes=("DATA_READY",), data_quality_flags=(), details=details)

    def _theme_ready(self, candidate: Candidate, metadata: Mapping[str, Any]) -> EntryThemeReadiness:
        status = str(metadata.get("theme_board_theme_status") or "").strip().upper()
        role = str(metadata.get("theme_board_stock_role") or "").strip().upper()
        score = _float(metadata.get("theme_board_theme_score"))
        details = {"theme_status": status, "stock_role": role, "theme_score": score}
        if not status:
            return EntryThemeReadiness(status=EntryCheckStatus.WAIT, reason_codes=("THEME_STATUS_MISSING",), details=details)
        if status == "DATA_WAIT":
            return EntryThemeReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("THEME_DATA_WAIT",), details=details)
        if status == "WEAK_THEME":
            return EntryThemeReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("THEME_WEAK",), details=details)
        if status == "LEADER_ONLY_THEME":
            if role in {"LEADER", "CO_LEADER"}:
                return EntryThemeReadiness(status=EntryCheckStatus.PASS, reason_codes=("THEME_LEADER_ONLY",), details=details)
            return EntryThemeReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("THEME_LEADER_ONLY_FOLLOWER_BLOCK",), details=details)
        if status in {"LEADING_THEME", "SPREADING_THEME"}:
            reason = "THEME_LEADING" if status == "LEADING_THEME" else "THEME_SPREADING"
            if score <= 0:
                return EntryThemeReadiness(status=EntryCheckStatus.WAIT, reason_codes=("THEME_SCORE_MISSING",), details=details)
            return EntryThemeReadiness(status=EntryCheckStatus.PASS, reason_codes=(reason,), details=details)
        if status == "WATCH_THEME":
            return EntryThemeReadiness(status=EntryCheckStatus.WAIT, reason_codes=("THEME_WATCH",), details=details)
        return EntryThemeReadiness(status=EntryCheckStatus.WAIT, reason_codes=("THEME_STATUS_UNKNOWN",), details=details)

    def _market_ready(self, metadata: Mapping[str, Any]) -> EntryMarketReadiness:
        action = str(metadata.get("market_action") or "").strip().upper()
        market_status = str(metadata.get("market_regime_status") or "").strip().upper()
        multiplier = _float(metadata.get("market_position_size_multiplier_hint"))
        details = {"market_action": action, "market_status": market_status}
        if not action:
            return EntryMarketReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("MARKET_DATA_WAIT",), details=details)
        if action == "ALLOW_NORMAL":
            return EntryMarketReadiness(status=EntryCheckStatus.PASS, reason_codes=("MARKET_EXPANSION",), position_size_multiplier_hint=1.0, details=details)
        if action == "ALLOW_REDUCED":
            return EntryMarketReadiness(
                status=EntryCheckStatus.PASS,
                reason_codes=("MARKET_SELECTIVE_REDUCED",),
                position_size_multiplier_hint=multiplier if multiplier > 0 else 0.6,
                details=details,
            )
        if action == "WAIT_MARKET":
            reason = "MARKET_CHOPPY_WAIT" if market_status == "CHOPPY" else "MARKET_WEAK_WAIT"
            return EntryMarketReadiness(status=EntryCheckStatus.WAIT, reason_codes=(reason,), position_size_multiplier_hint=0.0, details=details)
        if action == "BLOCK_NEW_ENTRY":
            return EntryMarketReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("MARKET_RISK_OFF_BLOCK",), position_size_multiplier_hint=0.0, details=details)
        if action == "DATA_WAIT":
            return EntryMarketReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("MARKET_DATA_WAIT",), position_size_multiplier_hint=0.0, details=details)
        if action == "MARKET_CLOSED":
            return EntryMarketReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("MARKET_CLOSED",), position_size_multiplier_hint=0.0, details=details)
        return EntryMarketReadiness(status=EntryCheckStatus.DATA_WAIT, reason_codes=("MARKET_ACTION_UNKNOWN",), position_size_multiplier_hint=0.0, details=details)

    def _role_ready(self, metadata: Mapping[str, Any], market: EntryMarketReadiness) -> EntryRoleReadiness:
        role = str(metadata.get("theme_board_stock_role") or "").strip().upper()
        theme_status = str(metadata.get("theme_board_theme_status") or "").strip().upper()
        market_status = str(metadata.get("market_regime_status") or "").strip().upper()
        details = {"stock_role": role, "theme_status": theme_status, "market_status": market_status}
        if role == "LEADER":
            return EntryRoleReadiness(status=EntryCheckStatus.PASS, reason_codes=("ROLE_LEADER",), dry_run_role_allowed=True, details=details)
        if role == "CO_LEADER":
            return EntryRoleReadiness(status=EntryCheckStatus.PASS, reason_codes=("ROLE_CO_LEADER",), dry_run_role_allowed=True, details=details)
        if role == "FOLLOWER":
            if market_status == "EXPANSION" and theme_status in {"LEADING_THEME", "SPREADING_THEME"}:
                return EntryRoleReadiness(status=EntryCheckStatus.PASS, reason_codes=("ROLE_FOLLOWER_EXPANSION_ONLY",), dry_run_role_allowed=False, details=details)
            return EntryRoleReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("ROLE_FOLLOWER_EXPANSION_ONLY",), dry_run_role_allowed=False, details=details)
        if role == "LATE_LAGGARD":
            return EntryRoleReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("ROLE_LATE_LAGGARD_BLOCK",), details=details)
        if role == "WEAK_MEMBER":
            return EntryRoleReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("ROLE_WEAK_MEMBER_BLOCK",), details=details)
        if role == "OVERHEATED":
            return EntryRoleReadiness(status=EntryCheckStatus.BLOCK, reason_codes=("ROLE_OVERHEATED_BLOCK",), details=details)
        return EntryRoleReadiness(status=EntryCheckStatus.WAIT, reason_codes=("ROLE_MISSING",), details=details)

    def _price_timing(
        self,
        candidate: Candidate,
        metadata: Mapping[str, Any],
        tick: StrategyTick | None,
        data: EntryDataReadiness,
        theme: EntryThemeReadiness,
        market: EntryMarketReadiness,
        role: EntryRoleReadiness,
    ) -> EntryPriceTiming:
        if tick is None or int(getattr(tick, "price", 0) or 0) <= 0:
            return EntryPriceTiming(status=EntryCheckStatus.DATA_WAIT, reason_codes=("PRICE_DATA_WAIT",), price_location=PriceLocationStatus.DATA_WAIT)
        tick_metadata = dict(getattr(tick, "metadata", {}) or {})
        price = float(tick.price or 0)
        vwap = self._vwap(candidate.code, tick)
        day_high, day_low = self._day_high_low(candidate.code, tick)
        support = _first_number(
            tick_metadata.get("recent_support"),
            tick_metadata.get("support_price"),
            tick_metadata.get("session_low"),
            day_low,
        )
        breakout = _first_number(tick_metadata.get("breakout_level"), tick_metadata.get("session_high"), day_high)
        pullback = ((day_high - price) / day_high) * 100.0 if day_high > 0 and price > 0 else 100.0
        vwap_gap = ((price - vwap) / vwap) * 100.0 if vwap > 0 and price > 0 else 0.0
        role_name = str(metadata.get("theme_board_stock_role") or "").strip().upper()
        market_status = str(metadata.get("market_regime_status") or "").strip().upper()
        momentum = _first_number(tick_metadata.get("momentum_1m"), tick_metadata.get("momentum_3m"), self._candle_momentum(candidate.code, 1))
        details = {
            "current_price": price,
            "day_high": day_high,
            "day_low": day_low,
            "pullback_from_high_pct": round(pullback, 4),
            "vwap": vwap,
            "vwap_gap_pct": round(vwap_gap, 4),
            "support_price": support,
            "breakout_level": breakout,
            "momentum_1m": momentum,
            "spread_ticks": int(getattr(tick, "spread_ticks", 0) or 0),
            "vi_active": bool(tick_metadata.get("vi_active")),
            "upper_limit_gap_pct": _float(tick_metadata.get("upper_limit_gap_pct"), 100.0),
        }
        hints = self._price_hints(price, support, vwap)
        if bool(tick_metadata.get("vi_active")):
            return self._price_check(EntryCheckStatus.BLOCK, PriceLocationStatus.UNKNOWN, ("VI_ACTIVE_BLOCK",), details, hints, vwap, support, breakout)
        if _float(tick_metadata.get("upper_limit_gap_pct"), 100.0) <= self.config.upper_limit_min_gap_pct:
            return self._price_check(EntryCheckStatus.BLOCK, PriceLocationStatus.UNKNOWN, ("UPPER_LIMIT_NEAR_BLOCK",), details, hints, vwap, support, breakout)
        if bool(tick_metadata.get("failed_breakout")):
            return self._price_check(EntryCheckStatus.BLOCK, PriceLocationStatus.FAILED_BREAKOUT, ("FAILED_BREAKOUT",), details, hints, vwap, support, breakout)
        if vwap > 0 and vwap_gap > self._max_vwap_gap(role_name):
            return self._price_check(EntryCheckStatus.BLOCK, PriceLocationStatus.VWAP_OVEREXTENDED, ("VWAP_OVEREXTENDED",), details, hints, vwap, support, breakout)
        if self.config.max_spread_ticks > 0 and int(getattr(tick, "spread_ticks", 0) or 0) > self.config.max_spread_ticks:
            return self._price_check(EntryCheckStatus.WAIT, PriceLocationStatus.UNKNOWN, ("SPREAD_TOO_WIDE",), details, hints, vwap, support, breakout)
        if pullback > max(7.0, self.config.good_pullback_max_pct * 2.0):
            return self._price_check(EntryCheckStatus.WAIT, PriceLocationStatus.DEEP_PULLBACK, ("DEEP_PULLBACK",), details, hints, vwap, support, breakout)
        if day_high > 0 and pullback < self.config.chase_high_pullback_min_pct:
            return self._price_check(EntryCheckStatus.WAIT, PriceLocationStatus.CHASE_HIGH, ("CHASE_HIGH",), details, hints, vwap, support, breakout)
        if bool(tick_metadata.get("support_reclaimed") or tick_metadata.get("pullback_reclaim")) and support > 0 and price >= support:
            return self._price_check(EntryCheckStatus.PASS, PriceLocationStatus.PULLBACK_RECLAIM, ("PRICE_PULLBACK_RECLAIM",), details, hints, vwap, support, breakout)
        if vwap > 0 and price >= vwap and momentum > 0:
            return self._price_check(EntryCheckStatus.PASS, PriceLocationStatus.VWAP_RECLAIM, ("PRICE_VWAP_RECLAIM",), details, hints, vwap, support, breakout)
        if support > 0 and self.config.good_pullback_min_pct <= pullback <= self.config.good_pullback_max_pct and abs(price - support) / max(price, 1.0) <= 0.015:
            return self._price_check(EntryCheckStatus.PASS, PriceLocationStatus.GOOD_PULLBACK, ("PRICE_GOOD_PULLBACK",), details, hints, vwap, support, breakout)
        if breakout > 0 and price >= breakout and momentum > 0:
            if market_status == "EXPANSION" and role_name == "LEADER":
                return self._price_check(EntryCheckStatus.PASS, PriceLocationStatus.BREAKOUT_CONTINUATION, ("PRICE_BREAKOUT_CONTINUATION",), details, hints, vwap, support, breakout)
            return self._price_check(EntryCheckStatus.WAIT, PriceLocationStatus.BREAKOUT_CONTINUATION, ("BREAKOUT_REQUIRES_EXPANSION_LEADER",), details, hints, vwap, support, breakout)
        return self._price_check(EntryCheckStatus.WAIT, PriceLocationStatus.UNKNOWN, ("PRICE_LOCATION_UNKNOWN",), details, hints, vwap, support, breakout)

    def _price_check(
        self,
        status: EntryCheckStatus,
        location: PriceLocationStatus,
        reasons: tuple[str, ...],
        details: dict[str, Any],
        hints: dict[str, int],
        vwap: float,
        support: float,
        breakout: float,
    ) -> EntryPriceTiming:
        return EntryPriceTiming(
            status=status,
            reason_codes=reasons,
            details=details,
            price_location=location,
            limit_price_hint=hints["limit_price_hint"],
            stop_loss_price_hint=hints["stop_loss_price_hint"],
            take_profit_price_hint=hints["take_profit_price_hint"],
            reference_price=support or vwap or breakout or float(details.get("current_price") or 0.0),
            vwap=round(vwap, 4),
            support_price=round(support, 4),
            breakout_level=round(breakout, 4),
        )

    def _entry_status(
        self,
        data: EntryDataReadiness,
        theme: EntryThemeReadiness,
        market: EntryMarketReadiness,
        role: EntryRoleReadiness,
        price: EntryPriceTiming,
    ) -> EntryDecisionStatus:
        if data.status != EntryCheckStatus.PASS:
            return EntryDecisionStatus.DATA_WAIT if data.status == EntryCheckStatus.DATA_WAIT else EntryDecisionStatus.WAIT
        if theme.status == EntryCheckStatus.DATA_WAIT:
            return EntryDecisionStatus.DATA_WAIT
        if theme.status == EntryCheckStatus.WAIT:
            return EntryDecisionStatus.THEME_WAIT
        if theme.status == EntryCheckStatus.BLOCK:
            return EntryDecisionStatus.HARD_BLOCK
        if market.status == EntryCheckStatus.DATA_WAIT:
            return EntryDecisionStatus.DATA_WAIT
        if market.status == EntryCheckStatus.WAIT:
            return EntryDecisionStatus.MARKET_WAIT
        if market.status == EntryCheckStatus.BLOCK:
            return EntryDecisionStatus.HARD_BLOCK
        if role.status == EntryCheckStatus.WAIT:
            return EntryDecisionStatus.WAIT
        if role.status == EntryCheckStatus.BLOCK:
            return EntryDecisionStatus.HARD_BLOCK
        if price.status == EntryCheckStatus.DATA_WAIT:
            return EntryDecisionStatus.DATA_WAIT
        if price.status == EntryCheckStatus.WAIT:
            return EntryDecisionStatus.PRICE_WAIT
        if price.status == EntryCheckStatus.BLOCK:
            return EntryDecisionStatus.HARD_BLOCK
        return EntryDecisionStatus.OBSERVE_READY

    def _dry_run_intent_allowed(
        self,
        entry_status: EntryDecisionStatus,
        metadata: Mapping[str, Any],
        market: EntryMarketReadiness,
        role: EntryRoleReadiness,
        price: EntryPriceTiming,
    ) -> bool:
        if not self.config.allow_dry_run_intents:
            return False
        if entry_status != EntryDecisionStatus.OBSERVE_READY:
            return False
        if str(metadata.get("market_action") or "") not in {"ALLOW_NORMAL", "ALLOW_REDUCED"}:
            return False
        if not role.dry_run_role_allowed:
            return False
        return price.price_location in {
            PriceLocationStatus.GOOD_PULLBACK,
            PriceLocationStatus.PULLBACK_RECLAIM,
            PriceLocationStatus.VWAP_RECLAIM,
        }

    def _merge_candidate_metadata(self, decisions: Iterable[EntryDecision], trade_date: str, now: datetime) -> int:
        updated = 0
        updated_at = now.isoformat()
        for decision in decisions:
            candidate = self.db.load_candidate(trade_date, decision.code)
            if candidate is None or candidate.state not in ENTRY_ENGINE_CANDIDATE_STATES:
                continue
            metadata = dict(candidate.metadata or {})
            metadata["entry_status"] = decision.entry_status.value
            metadata["entry_price_location"] = decision.price_location
            metadata["entry_reason_codes"] = list(decision.reason_codes)
            metadata["entry_operator_message_ko"] = decision.operator_message_ko
            metadata["entry_ready_allowed"] = decision.ready_allowed
            metadata["entry_dry_run_intent_allowed"] = decision.dry_run_intent_allowed
            metadata["entry_live_order_allowed"] = False
            metadata["entry_limit_price_hint"] = decision.limit_price_hint
            metadata["entry_stop_loss_price_hint"] = decision.stop_loss_price_hint
            metadata["entry_take_profit_price_hint"] = decision.take_profit_price_hint
            metadata["updated_by_entry_engine_at"] = updated_at
            metadata["entry_decision"] = decision.to_dict()
            candidate.metadata = metadata
            self.db.save_candidate(candidate)
            updated += 1
        return updated

    def _one_minute_candle_count(self, code: str) -> int:
        if self.candle_builder is None:
            return 0
        active = 1 if self.candle_builder.active_candle(code) is not None else 0
        return len(self.candle_builder.completed_candles(code, 1)) + active

    def _candle_momentum(self, code: str, interval_min: int) -> float:
        if self.candle_builder is None:
            return 0.0
        candles = self.candle_builder.completed_candles(code, interval_min)
        if not candles and interval_min == 1:
            active = self.candle_builder.active_candle(code)
            candles = [active] if active is not None else []
        if not candles:
            return 0.0
        candle = candles[-1]
        if candle.open <= 0:
            return 0.0
        return ((candle.close - candle.open) / candle.open) * 100.0

    def _vwap(self, code: str, tick: StrategyTick | None) -> float:
        metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
        value = _float(metadata.get("vwap"))
        if value > 0:
            return value
        if self.candle_builder is None:
            return 0.0
        candles = self.candle_builder.completed_candles(code, 1)
        total_volume = sum(max(0, candle.volume) for candle in candles)
        if total_volume <= 0:
            return 0.0
        return sum(candle.close * max(0, candle.volume) for candle in candles) / total_volume

    def _day_high_low(self, code: str, tick: StrategyTick | None) -> tuple[float, float]:
        high = low = 0.0
        if self.market_data is not None:
            day_high, day_low = self.market_data.day_high_low(code)
            high = float(day_high or 0)
            low = float(day_low or 0)
        metadata = dict(getattr(tick, "metadata", {}) or {}) if tick is not None else {}
        high = max(high, _float(metadata.get("session_high")), _float(metadata.get("day_high")))
        low_values = [value for value in (low, _float(metadata.get("session_low")), _float(metadata.get("day_low"))) if value > 0]
        low = min(low_values) if low_values else 0.0
        return high, low

    def _price_hints(self, price: float, support: float, vwap: float) -> dict[str, int]:
        limit = int(round(price))
        stop_base = support if support > 0 else price * 0.97
        take_base = max(price, vwap, support)
        return {
            "limit_price_hint": limit,
            "stop_loss_price_hint": max(0, int(round(stop_base * 0.985))),
            "take_profit_price_hint": max(0, int(round(take_base * 1.035))),
        }

    def _max_vwap_gap(self, role: str) -> float:
        if role == "FOLLOWER":
            return self.config.max_vwap_gap_follower_pct
        if role == "CO_LEADER":
            return self.config.max_vwap_gap_co_leader_pct
        return self.config.max_vwap_gap_leader_pct


class EntryEngineRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        config: EntryEngineConfig | None = None,
        engine: EntryEngine | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.config = config or EntryEngineConfig.from_env()
        self.clock = clock or datetime.now
        self.engine = engine or EntryEngine(
            db,
            market_data=market_data,
            candle_builder=candle_builder,
            config=self.config,
            clock=self.clock,
        )
        self.last_result: EntryDecisionResult | None = None
        self.last_summary: dict[str, Any] = {"status": "DISABLED", "enabled": False, "output_mode": ENTRY_ENGINE_OUTPUT_MODE}
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"status": "DISABLED", "enabled": False, "output_mode": ENTRY_ENGINE_OUTPUT_MODE}
            return self.last_summary
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        result = self.engine.build(trade_date=current.date().isoformat(), now=current, save=True)
        self.last_result = result
        self.last_run_at = current
        self.last_summary = entry_engine_dashboard_payload(result)
        self.last_summary["enabled"] = True
        self.last_summary["status"] = "OK"
        return dict(self.last_summary)


def entry_engine_dashboard_payload(result: EntryDecisionResult | Mapping[str, Any] | list[Mapping[str, Any]]) -> dict[str, Any]:
    if isinstance(result, EntryDecisionResult):
        data = result.to_dict()
        decisions = list(data.get("decisions") or [])
        calculated_at = data.get("calculated_at", "")
        trade_date = data.get("trade_date", "")
    elif isinstance(result, list):
        decisions = [dict(item or {}) for item in result]
        calculated_at = str(decisions[0].get("calculated_at") or "") if decisions else ""
        trade_date = str(decisions[0].get("trade_date") or "") if decisions else ""
    else:
        data = dict(result or {})
        decisions = list(data.get("decisions") or [])
        calculated_at = data.get("calculated_at", "")
        trade_date = data.get("trade_date", "")
    status_counts = Counter(str(item.get("entry_status") or "") for item in decisions)
    reason_counter = Counter()
    for item in decisions:
        for reason in list(item.get("reason_codes") or []):
            reason_counter[str(reason)] += 1
    ready = [item for item in decisions if str(item.get("entry_status") or "") == EntryDecisionStatus.OBSERVE_READY.value]
    return {
        "calculated_at": calculated_at,
        "trade_date": trade_date,
        "evaluated_count": len(decisions),
        "observe_ready_count": status_counts.get(EntryDecisionStatus.OBSERVE_READY.value, 0),
        "wait_count": status_counts.get(EntryDecisionStatus.WAIT.value, 0),
        "hard_block_count": status_counts.get(EntryDecisionStatus.HARD_BLOCK.value, 0),
        "data_wait_count": status_counts.get(EntryDecisionStatus.DATA_WAIT.value, 0),
        "market_wait_count": status_counts.get(EntryDecisionStatus.MARKET_WAIT.value, 0),
        "theme_wait_count": status_counts.get(EntryDecisionStatus.THEME_WAIT.value, 0),
        "price_wait_count": status_counts.get(EntryDecisionStatus.PRICE_WAIT.value, 0),
        "dry_run_intent_allowed_count": sum(1 for item in decisions if bool(item.get("dry_run_intent_allowed"))),
        "top_ready_candidates": sorted(ready, key=lambda item: float(item.get("current_price") or 0.0), reverse=True)[:10],
        "top_wait_reasons": [{"reason": key, "count": count} for key, count in reason_counter.most_common(10) if "WAIT" in key or "MISSING" in key or "UNKNOWN" in key],
        "top_block_reasons": [{"reason": key, "count": count} for key, count in reason_counter.most_common(10) if "BLOCK" in key or "OVEREXTENDED" in key or "CHASE" in key],
        "warnings": [],
        "output_mode": ENTRY_ENGINE_OUTPUT_MODE,
        "live_order_allowed": False,
    }


def entry_engine_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    loader = getattr(db, "latest_entry_decisions", None)
    if not callable(loader):
        return {"status": "UNAVAILABLE", "output_mode": ENTRY_ENGINE_OUTPUT_MODE, "live_order_allowed": False}
    decisions = loader(trade_date=trade_date)
    if not decisions:
        return {"status": "EMPTY", "output_mode": ENTRY_ENGINE_OUTPUT_MODE, "live_order_allowed": False}
    payload = entry_engine_dashboard_payload(decisions)
    payload["status"] = "OK"
    return payload


def _operator_message(status: EntryDecisionStatus, reasons: Iterable[str]) -> str:
    if status == EntryDecisionStatus.OBSERVE_READY:
        return "관찰 기준 매수 준비가 충족되었습니다. LIVE 주문은 비활성화 상태입니다."
    if status == EntryDecisionStatus.DATA_WAIT:
        return "실시간 가격/캔들 데이터가 부족해 대기합니다."
    if status == EntryDecisionStatus.THEME_WAIT:
        return "테마 확산 또는 주도 상태가 충분하지 않아 대기합니다."
    if status == EntryDecisionStatus.MARKET_WAIT:
        return "시장국면이 신규 진입을 허용하지 않아 대기합니다."
    if status == EntryDecisionStatus.PRICE_WAIT:
        return "가격 위치가 아직 매수 타이밍 조건을 충족하지 못했습니다."
    if status == EntryDecisionStatus.HARD_BLOCK:
        return "리스크 조건으로 신규 진입을 차단합니다."
    return "관찰 대기 상태입니다."


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
