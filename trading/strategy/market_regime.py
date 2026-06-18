from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import MarketIndexStore, _index_storage_aliases
from trading.strategy.models import Candidate, CandidateState, StrategyProfile


MARKET_REGIME_OUTPUT_MODE = "OBSERVE"
MARKET_REGIME_ALLOWED_STATES = {CandidateState.WATCHING, CandidateState.WAIT_DATA}
MARKET_REGIME_EXCLUDED_STATES = {CandidateState.REMOVED, CandidateState.EXPIRED}


class MarketRegimeStatus(str, Enum):
    EXPANSION = "EXPANSION"
    SELECTIVE = "SELECTIVE"
    CHOPPY = "CHOPPY"
    WEAK = "WEAK"
    RISK_OFF = "RISK_OFF"
    DATA_WAIT = "DATA_WAIT"
    MARKET_CLOSED = "MARKET_CLOSED"


class MarketSide(str, Enum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"
    UNKNOWN = "UNKNOWN"


class CandidateMarketAction(str, Enum):
    ALLOW_NORMAL = "ALLOW_NORMAL"
    ALLOW_REDUCED = "ALLOW_REDUCED"
    WAIT_MARKET = "WAIT_MARKET"
    BLOCK_NEW_ENTRY = "BLOCK_NEW_ENTRY"
    DATA_WAIT = "DATA_WAIT"
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass(frozen=True)
class MarketRegimeConfig:
    enabled: bool = False
    observe_only: bool = True
    interval_sec: int = 5
    kospi_code: str = "001"
    kosdaq_code: str = "101"
    weak_kospi_pct: float = -0.8
    weak_kosdaq_pct: float = -1.0
    risk_off_kospi_pct: float = -2.0
    risk_off_kosdaq_pct: float = -2.5
    breadth_expansion_pct: float = 0.58
    breadth_weak_pct: float = 0.38
    breadth_risk_off_pct: float = 0.28
    min_breadth_sample_kospi: int = 80
    min_breadth_sample_kosdaq: int = 120
    max_quote_age_sec: int = 60

    @classmethod
    def from_env(cls) -> "MarketRegimeConfig":
        return cls(
            enabled=_env_bool("TRADING_MARKET_REGIME_ENABLED", False),
            observe_only=_env_bool("TRADING_MARKET_REGIME_OBSERVE_ONLY", True),
            interval_sec=max(1, _env_int("TRADING_MARKET_REGIME_INTERVAL_SEC", 5)),
            kospi_code=str(os.getenv("TRADING_MARKET_REGIME_KOSPI_CODE", "001") or "001"),
            kosdaq_code=str(os.getenv("TRADING_MARKET_REGIME_KOSDAQ_CODE", "101") or "101"),
            weak_kospi_pct=_env_float("TRADING_MARKET_REGIME_WEAK_KOSPI_PCT", -0.8),
            weak_kosdaq_pct=_env_float("TRADING_MARKET_REGIME_WEAK_KOSDAQ_PCT", -1.0),
            risk_off_kospi_pct=_env_float("TRADING_MARKET_REGIME_RISK_OFF_KOSPI_PCT", -2.0),
            risk_off_kosdaq_pct=_env_float("TRADING_MARKET_REGIME_RISK_OFF_KOSDAQ_PCT", -2.5),
            breadth_expansion_pct=_env_float("TRADING_MARKET_REGIME_BREADTH_EXPANSION_PCT", 0.58),
            breadth_weak_pct=_env_float("TRADING_MARKET_REGIME_BREADTH_WEAK_PCT", 0.38),
            breadth_risk_off_pct=_env_float("TRADING_MARKET_REGIME_BREADTH_RISK_OFF_PCT", 0.28),
            min_breadth_sample_kospi=max(0, _env_int("TRADING_MARKET_REGIME_MIN_BREADTH_SAMPLE_KOSPI", 80)),
            min_breadth_sample_kosdaq=max(0, _env_int("TRADING_MARKET_REGIME_MIN_BREADTH_SAMPLE_KOSDAQ", 120)),
            max_quote_age_sec=max(1, _env_int("TRADING_MARKET_REGIME_MAX_QUOTE_AGE_SEC", 60)),
        )


@dataclass(frozen=True)
class MarketBreadthSnapshot:
    side: MarketSide
    source: str = "candidate_universe"
    sample_count: int = 0
    valid_quote_count: int = 0
    valid_quote_ratio: float = 0.0
    breadth_pct: float = 0.0
    advancing_count: int = 0
    declining_count: int = 0
    flat_count: int = 0
    strong_count: int = 0
    weak_count: int = 0
    turnover_weighted_return_pct: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class MarketSideSnapshot:
    side: MarketSide
    status: MarketRegimeStatus
    index_code: str = ""
    index_name: str = ""
    index_price: int = 0
    index_return_pct: float = 0.0
    index_slope_1m_pct: float | None = None
    index_slope_3m_pct: float | None = None
    index_slope_5m_pct: float | None = None
    index_slope_20m_pct: float | None = None
    position_vs_vwap: str = "UNKNOWN"
    position_vs_day_mid: str = "UNKNOWN"
    low_break_recent: bool = False
    high_break_recent: bool = False
    breadth_pct: float = 0.0
    advancing_count: int = 0
    declining_count: int = 0
    flat_count: int = 0
    strong_count: int = 0
    weak_count: int = 0
    valid_quote_count: int = 0
    valid_quote_ratio: float = 0.0
    turnover_weighted_return_pct: float = 0.0
    risk_score: float = 0.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class CandidateMarketPolicy:
    code: str
    market_side: MarketSide
    market_side_source: str
    market_status: MarketRegimeStatus
    global_market_status: MarketRegimeStatus
    market_action: CandidateMarketAction
    position_size_multiplier_hint: float = 0.0
    block_new_entry: bool = False
    wait_reason: str = ""
    recheck_after_sec: int = 0
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class MarketRegimeSnapshot:
    trade_date: str
    calculated_at: str
    global_status: MarketRegimeStatus
    kospi_status: MarketRegimeStatus
    kosdaq_status: MarketRegimeStatus
    kospi_snapshot: MarketSideSnapshot
    kosdaq_snapshot: MarketSideSnapshot
    candidate_policy_by_code: dict[str, CandidateMarketPolicy] = field(default_factory=dict)
    market_session_status: str = "closed"
    market_open: bool = False
    market_closed: bool = True
    risk_off_detected: bool = False
    weak_market_detected: bool = False
    data_wait_count: int = 0
    policy_summary: dict[str, int] = field(default_factory=dict)
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = MARKET_REGIME_OUTPUT_MODE
    ready_allowed: bool = False
    order_intent_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True)
class MarketRegimeResult:
    snapshot: MarketRegimeSnapshot
    updated_candidate_count: int = 0
    saved: bool = False
    theme_overlay_applied: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class MarketRegimeEngine:
    def __init__(
        self,
        db: Any,
        *,
        market_data: MarketDataStore | None = None,
        market_index_store: MarketIndexStore | None = None,
        candle_builder: CandleBuilder | None = None,
        config: MarketRegimeConfig | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.market_index_store = market_index_store or MarketIndexStore()
        self.candle_builder = candle_builder
        self.config = config or MarketRegimeConfig()
        self.clock = clock or datetime.now

    def build(
        self,
        *,
        trade_date: str | None = None,
        now: datetime | None = None,
        save: bool = True,
    ) -> MarketRegimeResult:
        current = (now or self.clock()).replace(microsecond=0)
        trade_date = trade_date or current.date().isoformat()
        market_session_status = _market_session_status(current)
        market_open = market_session_status == "open"
        candidates = [
            candidate
            for candidate in list(self.db.list_candidates(trade_date=trade_date) or [])
            if candidate.state not in MARKET_REGIME_EXCLUDED_STATES
        ]
        breadth_by_side = self._breadth_by_side(candidates, current)
        kospi_snapshot = self._side_snapshot(
            MarketSide.KOSPI,
            current,
            breadth_by_side.get(MarketSide.KOSPI),
            market_open=market_open,
        )
        kosdaq_snapshot = self._side_snapshot(
            MarketSide.KOSDAQ,
            current,
            breadth_by_side.get(MarketSide.KOSDAQ),
            market_open=market_open,
        )
        global_status = self._global_status(kospi_snapshot.status, kosdaq_snapshot.status, market_open=market_open)
        policies = self._candidate_policies(
            candidates,
            side_status={
                MarketSide.KOSPI: kospi_snapshot.status,
                MarketSide.KOSDAQ: kosdaq_snapshot.status,
            },
            global_status=global_status,
        )
        policy_summary = Counter(policy.market_action.value for policy in policies.values())
        data_quality_flags = _dedupe(
            list(kospi_snapshot.data_quality_flags)
            + list(kosdaq_snapshot.data_quality_flags)
            + (["MARKET_REGIME_OBSERVE_ONLY"] if self.config.observe_only else [])
        )
        reason_codes = _dedupe(list(kospi_snapshot.reason_codes) + list(kosdaq_snapshot.reason_codes))
        snapshot = MarketRegimeSnapshot(
            trade_date=trade_date,
            calculated_at=current.isoformat(),
            global_status=global_status,
            kospi_status=kospi_snapshot.status,
            kosdaq_status=kosdaq_snapshot.status,
            kospi_snapshot=kospi_snapshot,
            kosdaq_snapshot=kosdaq_snapshot,
            candidate_policy_by_code=policies,
            market_session_status=market_session_status,
            market_open=market_open,
            market_closed=not market_open,
            risk_off_detected=global_status == MarketRegimeStatus.RISK_OFF,
            weak_market_detected=global_status in {MarketRegimeStatus.WEAK, MarketRegimeStatus.RISK_OFF},
            data_wait_count=sum(
                1
                for item in [kospi_snapshot.status, kosdaq_snapshot.status]
                if item == MarketRegimeStatus.DATA_WAIT
            )
            + policy_summary.get(CandidateMarketAction.DATA_WAIT.value, 0),
            policy_summary=dict(policy_summary),
            data_quality_flags=tuple(data_quality_flags),
            reason_codes=tuple(reason_codes),
            output_mode=MARKET_REGIME_OUTPUT_MODE,
            ready_allowed=False,
            order_intent_allowed=False,
        )
        updated_count = self._merge_candidate_metadata(trade_date, policies, current)
        theme_overlay_applied = self._apply_theme_board_overlay(snapshot, save=save)
        saved = False
        if save:
            saver = getattr(self.db, "save_market_regime_snapshot", None)
            if callable(saver):
                saver(snapshot.to_dict())
                saved = True
        return MarketRegimeResult(
            snapshot=snapshot,
            updated_candidate_count=updated_count,
            saved=saved,
            theme_overlay_applied=theme_overlay_applied,
            warnings=(),
        )

    def _side_snapshot(
        self,
        side: MarketSide,
        now: datetime,
        breadth: MarketBreadthSnapshot | None,
        *,
        market_open: bool,
    ) -> MarketSideSnapshot:
        config_code = self.config.kospi_code if side == MarketSide.KOSPI else self.config.kosdaq_code
        index_name = side.value
        breadth = breadth or MarketBreadthSnapshot(
            side=side,
            data_quality_flags=("DIAGNOSTIC_ONLY_BREADTH", "LOW_TRUST_BREADTH"),
            reason_codes=("BREADTH_EMPTY",),
        )
        if not market_open:
            return MarketSideSnapshot(
                side=side,
                status=MarketRegimeStatus.MARKET_CLOSED,
                index_code=config_code,
                index_name=index_name,
                breadth_pct=breadth.breadth_pct,
                advancing_count=breadth.advancing_count,
                declining_count=breadth.declining_count,
                flat_count=breadth.flat_count,
                strong_count=breadth.strong_count,
                weak_count=breadth.weak_count,
                valid_quote_count=breadth.valid_quote_count,
                valid_quote_ratio=breadth.valid_quote_ratio,
                turnover_weighted_return_pct=breadth.turnover_weighted_return_pct,
                data_quality_flags=tuple(_dedupe(list(breadth.data_quality_flags))),
                reason_codes=tuple(_dedupe(["MARKET_CLOSED"] + list(breadth.reason_codes))),
            )
        state = self.market_index_store.state(side.value)
        latest_tick = self._latest_index_tick(side)
        if state.price <= 0 or latest_tick is None:
            flags = _dedupe(list(breadth.data_quality_flags) + ["INDEX_TICK_MISSING"])
            reasons = _dedupe(list(breadth.reason_codes) + ["INDEX_DATA_WAIT"])
            return MarketSideSnapshot(
                side=side,
                status=MarketRegimeStatus.DATA_WAIT,
                index_code=config_code,
                index_name=index_name,
                breadth_pct=breadth.breadth_pct,
                advancing_count=breadth.advancing_count,
                declining_count=breadth.declining_count,
                flat_count=breadth.flat_count,
                strong_count=breadth.strong_count,
                weak_count=breadth.weak_count,
                valid_quote_count=breadth.valid_quote_count,
                valid_quote_ratio=breadth.valid_quote_ratio,
                turnover_weighted_return_pct=breadth.turnover_weighted_return_pct,
                data_quality_flags=tuple(flags),
                reason_codes=tuple(reasons),
            )
        flags = list(breadth.data_quality_flags)
        reasons = list(breadth.reason_codes)
        if (now - latest_tick.timestamp).total_seconds() > self.config.max_quote_age_sec:
            flags.append("INDEX_QUOTE_STALE")
            reasons.append("INDEX_DATA_WAIT")
        slopes = {
            1: self.market_index_store.return_pct(side.value, 1),
            3: self.market_index_store.return_pct(side.value, 3),
            5: self.market_index_store.return_pct(side.value, 5),
            20: self.market_index_store.return_pct(side.value, 20),
        }
        if slopes[5] is None:
            slopes[5] = _float(dict(state.metadata or {}).get("index_slope_5m_pct"))
        if slopes[20] is None:
            slopes[20] = _float(dict(state.metadata or {}).get("index_slope_20m_pct"))
        if slopes[1] is None or slopes[3] is None:
            flags.append("INDEX_CANDLE_WARMUP")
        position_vs_vwap = self._position_vs_vwap(side, state.price)
        high_break_recent = bool(state.day_high > 0 and state.price >= state.day_high)
        status, status_reasons, risk_score = self._classify_side(
            side=side,
            index_return_pct=float(state.change_rate or 0.0),
            slope_5m_pct=slopes[5],
            low_break_recent=bool(state.low_break_recent),
            breadth=breadth,
            data_wait=bool("INDEX_QUOTE_STALE" in flags),
        )
        reasons.extend(status_reasons)
        return MarketSideSnapshot(
            side=side,
            status=status,
            index_code=config_code,
            index_name=index_name,
            index_price=int(state.price or 0),
            index_return_pct=round(float(state.change_rate or 0.0), 4),
            index_slope_1m_pct=_round_optional(slopes[1]),
            index_slope_3m_pct=_round_optional(slopes[3]),
            index_slope_5m_pct=_round_optional(slopes[5]),
            index_slope_20m_pct=_round_optional(slopes[20]),
            position_vs_vwap=position_vs_vwap,
            position_vs_day_mid=str(state.mid_position or "UNKNOWN"),
            low_break_recent=bool(state.low_break_recent),
            high_break_recent=high_break_recent,
            breadth_pct=round(float(breadth.breadth_pct or 0.0), 4),
            advancing_count=breadth.advancing_count,
            declining_count=breadth.declining_count,
            flat_count=breadth.flat_count,
            strong_count=breadth.strong_count,
            weak_count=breadth.weak_count,
            valid_quote_count=breadth.valid_quote_count,
            valid_quote_ratio=round(float(breadth.valid_quote_ratio or 0.0), 4),
            turnover_weighted_return_pct=round(float(breadth.turnover_weighted_return_pct or 0.0), 4),
            risk_score=round(risk_score, 4),
            data_quality_flags=tuple(_dedupe(flags)),
            reason_codes=tuple(_dedupe(reasons)),
        )

    def _classify_side(
        self,
        *,
        side: MarketSide,
        index_return_pct: float,
        slope_5m_pct: float | None,
        low_break_recent: bool,
        breadth: MarketBreadthSnapshot,
        data_wait: bool,
    ) -> tuple[MarketRegimeStatus, list[str], float]:
        if data_wait:
            return MarketRegimeStatus.DATA_WAIT, ["INDEX_DATA_WAIT"], 100.0
        weak_threshold = self.config.weak_kospi_pct if side == MarketSide.KOSPI else self.config.weak_kosdaq_pct
        risk_off_threshold = self.config.risk_off_kospi_pct if side == MarketSide.KOSPI else self.config.risk_off_kosdaq_pct
        trusted_breadth = "LOW_TRUST_BREADTH" not in set(breadth.data_quality_flags)
        breadth_pct = float(breadth.breadth_pct or 0.0)
        reasons: list[str] = []
        risk_score = 0.0
        if index_return_pct <= risk_off_threshold:
            reasons.append("INDEX_RISK_OFF")
            risk_score += 75.0
        if (
            trusted_breadth
            and index_return_pct <= weak_threshold
            and breadth.valid_quote_count > 0
            and breadth_pct <= self.config.breadth_risk_off_pct
        ):
            reasons.append("BREADTH_RISK_OFF")
            risk_score += 25.0
        if risk_score >= 55.0:
            return MarketRegimeStatus.RISK_OFF, reasons, min(100.0, risk_score)

        if index_return_pct <= weak_threshold:
            reasons.append("INDEX_WEAK")
            risk_score += 45.0
        if low_break_recent:
            reasons.append("INDEX_LOW_BREAK")
            risk_score += 25.0
        if (
            trusted_breadth
            and index_return_pct < 0
            and breadth.valid_quote_count > 0
            and breadth_pct <= self.config.breadth_weak_pct
        ):
            reasons.append("BREADTH_WEAK")
            risk_score += 20.0
        if risk_score >= 35.0:
            return MarketRegimeStatus.WEAK, reasons, min(100.0, risk_score)

        slope_up = slope_5m_pct is None or slope_5m_pct >= 0
        if index_return_pct >= 0.5 and slope_up and (not trusted_breadth or breadth_pct >= self.config.breadth_expansion_pct):
            return MarketRegimeStatus.EXPANSION, ["INDEX_UP", "BREADTH_EXPANSION"], 0.0
        if index_return_pct >= weak_threshold and breadth.strong_count > 0:
            return MarketRegimeStatus.SELECTIVE, ["SELECTIVE_LEADERSHIP"], 15.0
        if index_return_pct >= weak_threshold and not trusted_breadth:
            return MarketRegimeStatus.SELECTIVE, ["INDEX_OK_BREADTH_DIAGNOSTIC"], 20.0
        return MarketRegimeStatus.CHOPPY, ["MIXED_MARKET"], 30.0

    def _global_status(
        self,
        kospi: MarketRegimeStatus,
        kosdaq: MarketRegimeStatus,
        *,
        market_open: bool,
    ) -> MarketRegimeStatus:
        statuses = {kospi, kosdaq}
        if not market_open:
            return MarketRegimeStatus.MARKET_CLOSED
        if MarketRegimeStatus.RISK_OFF in statuses:
            return MarketRegimeStatus.RISK_OFF
        if MarketRegimeStatus.WEAK in statuses:
            return MarketRegimeStatus.WEAK
        if statuses == {MarketRegimeStatus.DATA_WAIT}:
            return MarketRegimeStatus.DATA_WAIT
        if statuses <= {MarketRegimeStatus.EXPANSION, MarketRegimeStatus.SELECTIVE} and MarketRegimeStatus.EXPANSION in statuses:
            return MarketRegimeStatus.EXPANSION
        if statuses & {MarketRegimeStatus.EXPANSION, MarketRegimeStatus.SELECTIVE}:
            return MarketRegimeStatus.SELECTIVE
        if MarketRegimeStatus.CHOPPY in statuses:
            return MarketRegimeStatus.CHOPPY
        return MarketRegimeStatus.DATA_WAIT

    def _candidate_policies(
        self,
        candidates: Iterable[Candidate],
        *,
        side_status: Mapping[MarketSide, MarketRegimeStatus],
        global_status: MarketRegimeStatus,
    ) -> dict[str, CandidateMarketPolicy]:
        policies: dict[str, CandidateMarketPolicy] = {}
        for candidate in candidates:
            if candidate.state not in MARKET_REGIME_ALLOWED_STATES:
                continue
            side, source = _candidate_market_side(candidate)
            status = side_status.get(side, global_status) if side != MarketSide.UNKNOWN else global_status
            action, multiplier, block, wait_reason, reasons = _policy_for_status(status, global_status)
            side_reasons = [f"MARKET_SIDE_{side.value}"] if side != MarketSide.UNKNOWN else ["MARKET_SIDE_UNKNOWN"]
            policies[candidate.code] = CandidateMarketPolicy(
                code=candidate.code,
                market_side=side,
                market_side_source=source,
                market_status=status,
                global_market_status=global_status,
                market_action=action,
                position_size_multiplier_hint=multiplier,
                block_new_entry=block,
                wait_reason=wait_reason,
                recheck_after_sec=5 if action in {CandidateMarketAction.DATA_WAIT, CandidateMarketAction.WAIT_MARKET} else 0,
                reason_codes=tuple(_dedupe(side_reasons + reasons)),
            )
        return policies

    def _merge_candidate_metadata(
        self,
        trade_date: str,
        policies: Mapping[str, CandidateMarketPolicy],
        now: datetime,
    ) -> int:
        updated = 0
        updated_at = now.isoformat()
        for code, policy in policies.items():
            candidate = self.db.load_candidate(trade_date, code)
            if candidate is None or candidate.state in MARKET_REGIME_EXCLUDED_STATES:
                continue
            metadata = dict(candidate.metadata or {})
            policy_dict = policy.to_dict()
            metadata["market_side"] = policy.market_side.value
            metadata["market_side_source"] = policy.market_side_source
            metadata["market_regime_status"] = policy.market_status.value
            metadata["global_market_regime_status"] = policy.global_market_status.value
            metadata["market_action"] = policy.market_action.value
            metadata["market_position_size_multiplier_hint"] = policy.position_size_multiplier_hint
            metadata["market_block_new_entry"] = policy.block_new_entry
            metadata["market_reason_codes"] = list(policy.reason_codes)
            metadata["updated_by_market_regime_at"] = updated_at
            metadata["candidate_market_policy"] = policy_dict
            candidate.metadata = metadata
            self.db.save_candidate(candidate)
            updated += 1
        return updated

    def _breadth_by_side(self, candidates: Iterable[Candidate], now: datetime) -> dict[MarketSide, MarketBreadthSnapshot]:
        grouped: dict[MarketSide, list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            if candidate.state in MARKET_REGIME_EXCLUDED_STATES:
                continue
            side, _source = _candidate_market_side(candidate)
            if side in {MarketSide.KOSPI, MarketSide.KOSDAQ}:
                grouped[side].append(candidate)
        return {
            side: self._breadth_snapshot(side, items, now)
            for side, items in grouped.items()
        }

    def _breadth_snapshot(
        self,
        side: MarketSide,
        candidates: list[Candidate],
        now: datetime,
    ) -> MarketBreadthSnapshot:
        sample_count = len(candidates)
        flags = ["DIAGNOSTIC_ONLY_BREADTH"]
        reasons = ["BREADTH_SOURCE_CANDIDATE_UNIVERSE"]
        min_sample = self.config.min_breadth_sample_kospi if side == MarketSide.KOSPI else self.config.min_breadth_sample_kosdaq
        valid_ticks: list[StrategyTick] = []
        if self.market_data is not None:
            for candidate in candidates:
                tick = self.market_data.latest_tick(candidate.code)
                if tick is None or tick.price <= 0:
                    continue
                if (now - tick.timestamp).total_seconds() > self.config.max_quote_age_sec:
                    continue
                valid_ticks.append(tick)
        valid_count = len(valid_ticks)
        if valid_count < min_sample:
            flags.append("LOW_TRUST_BREADTH")
            reasons.append("BREADTH_SAMPLE_TOO_SMALL")
        if sample_count and valid_count / sample_count < 0.5:
            flags.append("VALID_QUOTE_RATIO_LOW")
            reasons.append("BREADTH_VALID_QUOTE_RATIO_LOW")
        advancing = sum(1 for tick in valid_ticks if tick.change_rate > 0)
        declining = sum(1 for tick in valid_ticks if tick.change_rate < 0)
        flat = valid_count - advancing - declining
        strong = sum(1 for tick in valid_ticks if tick.change_rate >= 3.0)
        weak = sum(1 for tick in valid_ticks if tick.change_rate <= -2.0)
        turnover_sum = sum(max(0.0, float(tick.trade_value or 0.0)) for tick in valid_ticks)
        if turnover_sum > 0:
            turnover_weighted = sum(max(0.0, float(tick.trade_value or 0.0)) * float(tick.change_rate or 0.0) for tick in valid_ticks) / turnover_sum
        elif valid_ticks:
            turnover_weighted = sum(float(tick.change_rate or 0.0) for tick in valid_ticks) / len(valid_ticks)
        else:
            turnover_weighted = 0.0
        return MarketBreadthSnapshot(
            side=side,
            sample_count=sample_count,
            valid_quote_count=valid_count,
            valid_quote_ratio=round(valid_count / sample_count, 4) if sample_count else 0.0,
            breadth_pct=round(advancing / valid_count, 4) if valid_count else 0.0,
            advancing_count=advancing,
            declining_count=declining,
            flat_count=flat,
            strong_count=strong,
            weak_count=weak,
            turnover_weighted_return_pct=round(turnover_weighted, 4),
            data_quality_flags=tuple(_dedupe(flags)),
            reason_codes=tuple(_dedupe(reasons)),
        )

    def _latest_index_tick(self, side: MarketSide) -> StrategyTick | None:
        if self.market_index_store is None:
            return None
        config_code = self.config.kospi_code if side == MarketSide.KOSPI else self.config.kosdaq_code
        for code in _dedupe([*_index_storage_aliases(side.value), *_index_storage_aliases(config_code)]):
            tick = self.market_index_store.market_data.latest_tick(code)
            if tick is not None:
                return tick
        return None

    def _position_vs_vwap(self, side: MarketSide, price: int) -> str:
        tick = self._latest_index_tick(side)
        metadata = dict(getattr(tick, "metadata", {}) or {}) if tick else {}
        vwap = _float(metadata.get("vwap"))
        if vwap <= 0:
            return "UNKNOWN"
        if price > vwap:
            return "ABOVE_VWAP"
        if price < vwap:
            return "BELOW_VWAP"
        return "AT_VWAP"

    def _apply_theme_board_overlay(self, snapshot: MarketRegimeSnapshot, *, save: bool) -> bool:
        if not save:
            return False
        loader = getattr(self.db, "latest_theme_board_snapshot", None)
        saver = getattr(self.db, "save_theme_board_snapshot", None)
        if not callable(loader) or not callable(saver):
            return False
        theme_board = loader(trade_date=snapshot.trade_date)
        if not theme_board:
            return False
        policies = {code: policy.to_dict() for code, policy in snapshot.candidate_policy_by_code.items()}
        stocks = [dict(stock or {}) for stock in list(theme_board.get("stocks") or [])]
        for stock in stocks:
            policy = policies.get(normalize_code(stock.get("code")))
            if not policy:
                continue
            stock["market_side"] = policy.get("market_side", MarketSide.UNKNOWN.value)
            stock["market_status"] = policy.get("market_status", "")
            stock["market_action"] = policy.get("market_action", "")
            stock["market_reason_codes"] = list(policy.get("reason_codes") or [])
        top_themes = [dict(theme or {}) for theme in list(theme_board.get("top_themes") or [])]
        stocks_by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for stock in stocks:
            stocks_by_theme[str(stock.get("theme_id") or "")].append(stock)
        for theme in top_themes:
            theme_id = str(theme.get("theme_id") or "")
            theme_stocks = stocks_by_theme.get(theme_id, [])
            side_counts = Counter(str(stock.get("market_side") or MarketSide.UNKNOWN.value) for stock in theme_stocks)
            status_counts = Counter(str(stock.get("market_status") or "") for stock in theme_stocks)
            theme["market_side_distribution"] = dict(side_counts)
            theme["dominant_market_side"] = side_counts.most_common(1)[0][0] if side_counts else MarketSide.UNKNOWN.value
            theme["market_status_distribution"] = dict(status_counts)
            theme["market_risk_flag"] = bool(
                status_counts.get(MarketRegimeStatus.RISK_OFF.value, 0)
                or status_counts.get(MarketRegimeStatus.WEAK.value, 0)
            )
        payload = dict(theme_board)
        payload["calculated_at"] = snapshot.calculated_at
        payload["top_themes"] = top_themes
        payload["stocks"] = stocks
        payload["market_regime_overlay"] = {
            "calculated_at": snapshot.calculated_at,
            "global_status": snapshot.global_status.value,
            "kospi_status": snapshot.kospi_status.value,
            "kosdaq_status": snapshot.kosdaq_status.value,
            "risk_off_detected": snapshot.risk_off_detected,
            "weak_market_detected": snapshot.weak_market_detected,
        }
        saver(payload)
        return True


class MarketRegimeRuntimePipeline:
    def __init__(
        self,
        *,
        db: Any,
        market_data: MarketDataStore,
        market_index_store: MarketIndexStore,
        candle_builder: CandleBuilder | None = None,
        config: MarketRegimeConfig | None = None,
        engine: MarketRegimeEngine | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.market_index_store = market_index_store
        self.candle_builder = candle_builder
        self.config = config or MarketRegimeConfig.from_env()
        self.clock = clock or datetime.now
        self.engine = engine or MarketRegimeEngine(
            db,
            market_data=market_data,
            market_index_store=market_index_store,
            candle_builder=candle_builder,
            config=self.config,
            clock=self.clock,
        )
        self.last_result: MarketRegimeResult | None = None
        self.last_summary: dict[str, Any] = {"status": "DISABLED", "enabled": False, "output_mode": MARKET_REGIME_OUTPUT_MODE}
        self.last_run_at: datetime | None = None

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_summary = {"status": "DISABLED", "enabled": False, "output_mode": MARKET_REGIME_OUTPUT_MODE}
            return self.last_summary
        if self.last_run_at is not None and (current - self.last_run_at).total_seconds() < self.config.interval_sec:
            return dict(self.last_summary)
        result = self.engine.build(trade_date=current.date().isoformat(), now=current, save=True)
        self.last_result = result
        self.last_run_at = current
        self.last_summary = market_regime_dashboard_payload(result.snapshot)
        self.last_summary["enabled"] = True
        self.last_summary["status"] = "OK"
        return dict(self.last_summary)


def market_regime_dashboard_payload(snapshot: MarketRegimeSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot or {})
    kospi = dict(data.get("kospi_snapshot") or {})
    kosdaq = dict(data.get("kosdaq_snapshot") or {})
    policy_summary = dict(data.get("policy_summary") or {})
    reason_counter = Counter()
    for reason in list(data.get("reason_codes") or []):
        reason_counter[str(reason)] += 1
    policies = dict(data.get("candidate_policy_by_code") or {})
    for policy in policies.values():
        if isinstance(policy, Mapping):
            for reason in list(policy.get("reason_codes") or []):
                reason_counter[str(reason)] += 1
    return {
        "calculated_at": data.get("calculated_at", ""),
        "trade_date": data.get("trade_date", ""),
        "global_status": data.get("global_status", ""),
        "kospi_status": data.get("kospi_status", ""),
        "kosdaq_status": data.get("kosdaq_status", ""),
        "kospi_return_pct": kospi.get("index_return_pct", 0.0),
        "kosdaq_return_pct": kosdaq.get("index_return_pct", 0.0),
        "kospi_breadth_pct": kospi.get("breadth_pct", 0.0),
        "kosdaq_breadth_pct": kosdaq.get("breadth_pct", 0.0),
        "expansion_reason": _status_reason(data, MarketRegimeStatus.EXPANSION.value),
        "selective_reason": _status_reason(data, MarketRegimeStatus.SELECTIVE.value),
        "choppy_reason": _status_reason(data, MarketRegimeStatus.CHOPPY.value),
        "weak_reason": _status_reason(data, MarketRegimeStatus.WEAK.value),
        "risk_off_reason": _status_reason(data, MarketRegimeStatus.RISK_OFF.value),
        "candidate_policy_summary": policy_summary,
        "block_new_entry_count": int(policy_summary.get(CandidateMarketAction.BLOCK_NEW_ENTRY.value, 0)),
        "wait_market_count": int(policy_summary.get(CandidateMarketAction.WAIT_MARKET.value, 0)),
        "data_wait_count": int(data.get("data_wait_count") or policy_summary.get(CandidateMarketAction.DATA_WAIT.value, 0)),
        "warnings": list(data.get("data_quality_flags") or []),
        "top_reasons": [{"reason": key, "count": count} for key, count in reason_counter.most_common(10)],
        "output_mode": data.get("output_mode", MARKET_REGIME_OUTPUT_MODE),
        "ready_allowed": False,
        "order_intent_allowed": False,
    }


def market_regime_dashboard_section(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    loader = getattr(db, "latest_market_regime_snapshot", None)
    if not callable(loader):
        return {"status": "UNAVAILABLE", "output_mode": MARKET_REGIME_OUTPUT_MODE, "ready_allowed": False, "order_intent_allowed": False}
    snapshot = loader(trade_date=trade_date)
    if not snapshot:
        return {"status": "EMPTY", "output_mode": MARKET_REGIME_OUTPUT_MODE, "ready_allowed": False, "order_intent_allowed": False}
    payload = market_regime_dashboard_payload(snapshot)
    payload["status"] = "OK"
    return payload


def _policy_for_status(
    status: MarketRegimeStatus,
    global_status: MarketRegimeStatus,
) -> tuple[CandidateMarketAction, float, bool, str, list[str]]:
    if global_status == MarketRegimeStatus.MARKET_CLOSED or status == MarketRegimeStatus.MARKET_CLOSED:
        return CandidateMarketAction.MARKET_CLOSED, 0.0, True, "MARKET_CLOSED", ["MARKET_CLOSED"]
    if global_status == MarketRegimeStatus.RISK_OFF or status == MarketRegimeStatus.RISK_OFF:
        return CandidateMarketAction.BLOCK_NEW_ENTRY, 0.0, True, "RISK_OFF", ["MARKET_RISK_OFF_BLOCK"]
    if global_status == MarketRegimeStatus.WEAK or status == MarketRegimeStatus.WEAK:
        return CandidateMarketAction.WAIT_MARKET, 0.0, True, "WEAK_MARKET", ["MARKET_WEAK_WAIT"]
    if status == MarketRegimeStatus.DATA_WAIT or global_status == MarketRegimeStatus.DATA_WAIT:
        return CandidateMarketAction.DATA_WAIT, 0.0, False, "DATA_WAIT", ["MARKET_DATA_WAIT"]
    if status == MarketRegimeStatus.EXPANSION and global_status in {MarketRegimeStatus.EXPANSION, MarketRegimeStatus.SELECTIVE}:
        return CandidateMarketAction.ALLOW_NORMAL, 1.0, False, "", ["MARKET_EXPANSION_ALLOW"]
    if status == MarketRegimeStatus.SELECTIVE or global_status == MarketRegimeStatus.SELECTIVE:
        return CandidateMarketAction.ALLOW_REDUCED, 0.6, False, "SELECTIVE_MARKET", ["MARKET_SELECTIVE_REDUCED"]
    if status == MarketRegimeStatus.CHOPPY or global_status == MarketRegimeStatus.CHOPPY:
        return CandidateMarketAction.WAIT_MARKET, 0.35, False, "CHOPPY_MARKET", ["MARKET_CHOPPY_WAIT"]
    return CandidateMarketAction.DATA_WAIT, 0.0, False, "DATA_WAIT", ["MARKET_DATA_WAIT"]


def _candidate_market_side(candidate: Candidate) -> tuple[MarketSide, str]:
    metadata = dict(candidate.metadata or {})
    raw = str(metadata.get("market_side") or "").strip().upper()
    if raw in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value}:
        return MarketSide(raw), "metadata"
    market = str(candidate.market or "").strip().upper()
    if market in {MarketSide.KOSPI.value, MarketSide.KOSDAQ.value}:
        return MarketSide(market), "candidate.market"
    profile = candidate.strategy_profile
    profile_value = profile.value if isinstance(profile, StrategyProfile) else str(profile or "")
    if profile_value == StrategyProfile.KOSDAQ_THEME_PROFILE.value:
        return MarketSide.KOSDAQ, "strategy_profile"
    if profile_value in {StrategyProfile.KOSPI_LEADER_PROFILE.value, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE.value}:
        return MarketSide.KOSPI, "strategy_profile"
    return MarketSide.UNKNOWN, "unknown"


def _market_session_status(now: datetime) -> str:
    hm = now.strftime("%H:%M")
    return "open" if "09:00" <= hm <= "15:30" else "closed"


def _status_reason(data: Mapping[str, Any], status: str) -> str:
    if str(data.get("global_status") or "") == status:
        return ",".join(str(reason) for reason in list(data.get("reason_codes") or [])[:3])
    return ""


def _round_optional(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


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
