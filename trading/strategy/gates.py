from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import Candle, CandleBuilder
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_index import MarketIndexState, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import BlockType, Candidate, CandidateState, GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.themes import StockLeadershipResult, ThemeMapping, ThemeRepository, ThemeStrengthResult


ACTIVE_STATES = {
    CandidateState.DETECTED,
    CandidateState.WATCHING,
    CandidateState.READY,
    CandidateState.BLOCKED,
}
A_THRESHOLD = 75.0
B_THRESHOLD = 55.0
B_PLUS_THRESHOLD = 70.0
KOSDAQ_PULLBACK_RANGE = (-5.0, -2.0)
KOSDAQ_SHALLOW_PULLBACK_RANGE = (-2.0, -1.5)
KOSPI_PULLBACK_RANGE = (-3.0, -0.8)
KOSDAQ_SUPPORT_NEAR_PCT = 2.5
KOSPI_SUPPORT_NEAR_PCT = 1.5


@dataclass
class _ThemeEntry:
    candidate: Candidate
    mapping: ThemeMapping
    tick: Optional[StrategyTick]
    turnover: int


class ThemeStrengthGate:
    def __init__(self, repository: ThemeRepository, market_data: MarketDataStore) -> None:
        self.repository = repository
        self.market_data = market_data

    def evaluate(self, candidates: list[Candidate]) -> list[ThemeStrengthResult]:
        groups = self._theme_groups(candidates)
        theme_turnovers = {theme_id: sum(entry.turnover for entry in entries) for theme_id, entries in groups.items()}
        theme_top2_turnovers = {
            theme_id: sum(sorted((entry.turnover for entry in entries), reverse=True)[:2])
            for theme_id, entries in groups.items()
        }
        max_theme_turnover = max(theme_turnovers.values(), default=0)
        max_top2_turnover = max(theme_top2_turnovers.values(), default=0)

        results = []
        for theme_id, entries in sorted(groups.items()):
            results.append(
                self._evaluate_theme(
                    theme_id,
                    entries,
                    max_theme_turnover=max_theme_turnover,
                    max_top2_turnover=max_top2_turnover,
                )
            )
        return results

    def _theme_groups(self, candidates: list[Candidate]) -> dict[str, list[_ThemeEntry]]:
        groups: dict[str, list[_ThemeEntry]] = {}
        for candidate in candidates:
            if candidate.state not in ACTIVE_STATES:
                continue
            clean_code = normalize_code(candidate.code)
            tick = self.market_data.latest_tick(clean_code)
            turnover = _turnover(tick)
            for mapping in self.repository.themes_for_code(clean_code):
                groups.setdefault(mapping.theme_id, []).append(
                    _ThemeEntry(
                        candidate=self.repository.enrich_candidate(candidate),
                        mapping=mapping,
                        tick=tick,
                        turnover=turnover,
                    )
                )
        return groups

    def _evaluate_theme(
        self,
        theme_id: str,
        entries: list[_ThemeEntry],
        max_theme_turnover: int,
        max_top2_turnover: int,
    ) -> ThemeStrengthResult:
        active_count = len(entries)
        valid_ticks = [entry for entry in entries if _valid_tick(entry.tick)]
        valid_turnovers = [entry for entry in entries if entry.turnover > 0]
        missing_turnover_count = active_count - len(valid_turnovers)
        valid_tick_ratio = len(valid_ticks) / active_count if active_count else 0.0
        insufficient_reason = []
        if len(valid_ticks) < active_count:
            insufficient_reason.append("tick_missing")
        if missing_turnover_count:
            insufficient_reason.append("turnover_missing")

        avg_change = sum(entry.tick.change_rate for entry in valid_ticks if entry.tick) / len(valid_ticks) if valid_ticks else 0.0
        rising_ratio = (
            sum(1 for entry in valid_ticks if entry.tick and entry.tick.change_rate > 0) / len(valid_ticks)
            if valid_ticks
            else 0.0
        )
        theme_turnover = sum(entry.turnover for entry in entries)
        top_entries = sorted(entries, key=lambda entry: (entry.turnover, entry.tick.change_rate if entry.tick else 0.0), reverse=True)
        top2_turnover = sum(entry.turnover for entry in top_entries[:2])

        score_components = {
            "candidate_count": min(20.0, (active_count / 3.0) * 20.0),
            "average_change_rate": _clamp((avg_change / 5.0) * 20.0, 0.0, 20.0),
            "rising_ratio": rising_ratio * 20.0,
            "theme_turnover": (theme_turnover / max_theme_turnover) * 20.0 if max_theme_turnover > 0 else 0.0,
            "leader_turnover": (top2_turnover / max_top2_turnover) * 20.0 if max_top2_turnover > 0 else 0.0,
        }
        score = sum(score_components.values())
        signal_pair = sum(1 for entry in valid_turnovers if entry.mapping.is_signal_stock) >= 2
        grade = _theme_grade(
            score=score,
            active_count=active_count,
            valid_tick_ratio=valid_tick_ratio,
            valid_turnover_count=len(valid_turnovers),
            signal_pair=signal_pair,
        )
        leader_codes = [entry.candidate.code for entry in top_entries[:2]]
        theme_name = entries[0].mapping.theme_name if entries else ""
        return ThemeStrengthResult(
            theme_id=theme_id,
            theme_name=theme_name,
            score=round(score, 4),
            grade=grade,
            active_candidate_count=active_count,
            valid_tick_ratio=round(valid_tick_ratio, 4),
            leader_codes=leader_codes,
            details={
                "theme_id": theme_id,
                "theme_name": theme_name,
                "score_components": score_components,
                "active_candidate_count": active_count,
                "valid_tick_count": len(valid_ticks),
                "valid_tick_ratio": valid_tick_ratio,
                "valid_turnover_count": len(valid_turnovers),
                "missing_turnover_count": missing_turnover_count,
                "theme_turnover": theme_turnover,
                "top2_turnover": top2_turnover,
                "leader_codes": leader_codes,
                "signal_pair": signal_pair,
                "insufficient_reason": insufficient_reason,
            },
        )


class StockLeadershipGate:
    def __init__(self, repository: ThemeRepository, market_data: MarketDataStore) -> None:
        self.repository = repository
        self.market_data = market_data

    def evaluate(self, candidate: Candidate, candidates: list[Candidate]) -> list[StockLeadershipResult]:
        clean_code = normalize_code(candidate.code)
        mappings = self.repository.themes_for_code(clean_code)
        return [self._evaluate_mapping(candidate, mapping, candidates) for mapping in mappings]

    def evaluate_all(self, candidates: list[Candidate]) -> list[StockLeadershipResult]:
        results: list[StockLeadershipResult] = []
        for candidate in candidates:
            if candidate.state in ACTIVE_STATES:
                results.extend(self.evaluate(candidate, candidates))
        return results

    def _evaluate_mapping(self, candidate: Candidate, mapping: ThemeMapping, candidates: list[Candidate]) -> StockLeadershipResult:
        scope = _leadership_scope(candidate, mapping)
        scope_entries = self._scope_entries(mapping, candidates, scope)
        ranked = sorted(
            scope_entries,
            key=lambda entry: (
                entry.turnover,
                entry.tick.change_rate if entry.tick else 0.0,
                _clamp(entry.mapping.base_priority, 0, 100),
            ),
            reverse=True,
        )
        code = normalize_code(candidate.code)
        rank = next((index + 1 for index, entry in enumerate(ranked) if entry.candidate.code == code), 0)
        role = _leadership_role(rank, mapping.is_signal_stock)
        tick = self.market_data.latest_tick(code)
        turnover = _turnover(tick)
        base_priority_original = mapping.base_priority
        base_priority_normalized = _clamp(base_priority_original, 0, 100)
        rank_count = max(1, len(ranked))
        turnover_rank_score = _rank_score(35.0, rank, rank_count)
        change_rank = _change_rate_rank(code, ranked)
        change_rate_rank_score = _rank_score(25.0, change_rank, rank_count)
        leader_candidate_score = 15.0 if mapping.is_leader_candidate else 0.0
        base_priority_score = (base_priority_normalized / 100.0) * 15.0
        signal_large_cap_score = 10.0 if mapping.is_signal_stock or mapping.is_large_cap else 0.0
        score_components = {
            "turnover_rank": turnover_rank_score,
            "change_rate_rank": change_rate_rank_score,
            "leader_candidate": leader_candidate_score,
            "base_priority": base_priority_score,
            "signal_or_large_cap": signal_large_cap_score,
        }
        insufficient_reason = []
        if not _valid_tick(tick):
            insufficient_reason.append("tick_missing")
        if turnover <= 0:
            insufficient_reason.append("turnover_missing")

        return StockLeadershipResult(
            candidate_id=candidate.id,
            code=code,
            theme_id=mapping.theme_id,
            theme_name=mapping.theme_name,
            score=round(sum(score_components.values()), 4),
            leadership_rank=rank,
            leadership_role=role,
            leadership_scope=scope,
            details={
                "theme_id": mapping.theme_id,
                "theme_name": mapping.theme_name,
                "leadership_scope": scope,
                "leadership_rank": rank,
                "leadership_role": role,
                "score_components": score_components,
                "scope_candidate_codes": [entry.candidate.code for entry in ranked],
                "turnover": turnover,
                "change_rate": tick.change_rate if tick else 0.0,
                "base_priority_original": base_priority_original,
                "base_priority_normalized": base_priority_normalized,
                "insufficient_reason": insufficient_reason,
            },
        )

    def _scope_entries(self, mapping: ThemeMapping, candidates: list[Candidate], scope: str) -> list[_ThemeEntry]:
        entries: list[_ThemeEntry] = []
        for candidate in candidates:
            if candidate.state not in ACTIVE_STATES:
                continue
            enriched = self.repository.enrich_candidate(candidate)
            for member_mapping in self.repository.themes_for_code(enriched.code):
                if member_mapping.theme_id != mapping.theme_id:
                    continue
                if not _in_leadership_scope(mapping, member_mapping, enriched, scope):
                    continue
                tick = self.market_data.latest_tick(enriched.code)
                entries.append(_ThemeEntry(enriched, member_mapping, tick, _turnover(tick)))
        return entries


class MarketIndexGate:
    def __init__(self, market_index_store: MarketIndexStore) -> None:
        self.market_index_store = market_index_store

    def evaluate(self, candidate: Candidate, theme_mapping: Optional[ThemeMapping] = None) -> GateDecision:
        index_code = _index_code_for(candidate, theme_mapping)
        state = self.market_index_store.state(index_code)
        details = {
            "index_code": index_code,
            "direction_5m": state.direction_5m,
            "low_break_recent": state.low_break_recent,
            "position_vs_mid": state.mid_position,
            "price": state.price,
            "day_mid": state.day_mid,
        }
        if state.price <= 0:
            details["sub_status"] = "DATA_INSUFFICIENT"
            return _decision(
                "MarketIndexGate",
                False,
                0.0,
                BlockType.TEMPORARY,
                ["DATA_INSUFFICIENT"],
                details,
                can_recover=True,
                recheck_after_sec=60,
            )
        if state.low_break_recent or state.direction_5m == "DOWN":
            details["sub_status"] = "MARKET_WAIT"
            return _decision(
                "MarketIndexGate",
                False,
                20.0,
                BlockType.TEMPORARY,
                ["INDEX_WEAK"],
                details,
                can_recover=True,
                recheck_after_sec=60,
            )
        score = 100.0 if state.mid_position in {"ABOVE_MID", "AT_MID"} else 75.0
        return _decision("MarketIndexGate", True, score, BlockType.NONE, [], details)


class ThemePullbackGate:
    def __init__(
        self,
        indicator_calculator: IndicatorCalculator,
        intraday_tracker: IntradayStateTracker,
        candle_builder: CandleBuilder,
        market_data: MarketDataStore,
    ) -> None:
        self.indicator_calculator = indicator_calculator
        self.intraday_tracker = intraday_tracker
        self.candle_builder = candle_builder
        self.market_data = market_data

    def evaluate(self, theme_result: ThemeStrengthResult) -> GateDecision:
        leader_codes = list(theme_result.leader_codes[:2])
        details = {
            "theme_id": theme_result.theme_id,
            "theme_grade": theme_result.grade,
            "leader_codes": leader_codes,
            "leader_pullback_pct": {},
            "leader_chase_risk": {},
            "leader_support_status": {},
            "insufficient_reason": [],
        }
        if theme_result.grade == "C":
            details["sub_status"] = "THEME_WEAK"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["THEME_WEAK"], details)
        if not leader_codes:
            details["sub_status"] = "DATA_INSUFFICIENT"
            details["insufficient_reason"].append("leader_missing")
            return _decision(
                "ThemePullbackGate",
                False,
                0.0,
                BlockType.TEMPORARY,
                ["DATA_INSUFFICIENT"],
                details,
                can_recover=True,
                recheck_after_sec=60,
            )

        healthy_count = 0
        collapse_count = 0
        chase_count = 0
        for code in leader_codes:
            snapshot = _snapshot_for(
                self.indicator_calculator,
                self.intraday_tracker,
                self.candle_builder,
                self.market_data,
                None,
                code,
            )
            if snapshot is None:
                details["insufficient_reason"].append(f"{code}:snapshot_missing")
                continue
            support_status = support_status_for_snapshot(snapshot, self.candle_builder, KOSDAQ_SUPPORT_NEAR_PCT)
            details["leader_pullback_pct"][code] = snapshot.pullback_pct
            details["leader_chase_risk"][code] = snapshot.chase_risk
            details["leader_support_status"][code] = support_status
            collapsed = snapshot.pullback_pct is not None and snapshot.pullback_pct < -7.0
            healthy = (
                snapshot.pullback_pct is not None
                and snapshot.pullback_pct < 0
                and not snapshot.chase_risk
                and not collapsed
                and support_status["support_reclaimed"]
            )
            healthy_count += int(healthy)
            collapse_count += int(collapsed)
            chase_count += int(snapshot.chase_risk)

        if details["insufficient_reason"]:
            details["sub_status"] = "DATA_INSUFFICIENT"
            return _decision(
                "ThemePullbackGate",
                False,
                30.0,
                BlockType.TEMPORARY,
                ["DATA_INSUFFICIENT"],
                details,
                can_recover=True,
                recheck_after_sec=60,
            )
        if collapse_count > 0:
            details["sub_status"] = "THEME_LEADER_COLLAPSE"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["THEME_LEADER_COLLAPSE"], details)
        if chase_count > 0:
            details["sub_status"] = "THEME_LEADER_CHASE_RISK"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["CHASE_RISK"], details)
        if healthy_count >= 1:
            return _decision("ThemePullbackGate", True, 100.0, BlockType.NONE, [], details)
        details["sub_status"] = "WAIT_PULLBACK_CONFIRMATION"
        return _decision(
            "ThemePullbackGate",
            False,
            55.0,
            BlockType.TEMPORARY,
            ["WAIT_PULLBACK_CONFIRMATION"],
            details,
            can_recover=True,
            recheck_after_sec=60,
        )


class StockPullbackEntryGate:
    def __init__(
        self,
        indicator_calculator: IndicatorCalculator,
        intraday_tracker: IntradayStateTracker,
        candle_builder: CandleBuilder,
        market_data: MarketDataStore,
    ) -> None:
        self.indicator_calculator = indicator_calculator
        self.intraday_tracker = intraday_tracker
        self.candle_builder = candle_builder
        self.market_data = market_data

    def evaluate(
        self,
        candidate: Candidate,
        theme_result: ThemeStrengthResult,
        leadership_result: StockLeadershipResult,
    ) -> tuple[GateDecision, Optional[IndicatorSnapshot]]:
        snapshot = _snapshot_for(
            self.indicator_calculator,
            self.intraday_tracker,
            self.candle_builder,
            self.market_data,
            candidate.id,
            candidate.code,
        )
        profile = candidate.strategy_profile
        if profile == StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE:
            profile = StrategyProfile.KOSPI_LEADER_PROFILE
        details = {
            "theme_id": theme_result.theme_id,
            "profile": profile.value if profile else "",
            "pullback_pct": snapshot.pullback_pct if snapshot else None,
            "valid_range": _valid_pullback_range(profile),
            "nearest_support": None,
            "support_distance_pct": None,
            "support_touched": False,
            "support_reclaimed": False,
            "volume_reaccel": snapshot.volume_reaccel if snapshot else False,
            "failed_low_break_rebound": snapshot.failed_low_break_rebound if snapshot else False,
            "chase_risk": snapshot.chase_risk if snapshot else False,
            "insufficient_reason": [],
        }
        if snapshot is None:
            details["sub_status"] = "DATA_INSUFFICIENT"
            details["insufficient_reason"].append("snapshot_missing")
            return (
                _decision(
                    "StockPullbackEntryGate",
                    False,
                    0.0,
                    BlockType.TEMPORARY,
                    ["DATA_INSUFFICIENT"],
                    details,
                    can_recover=True,
                    recheck_after_sec=60,
                ),
                None,
            )
        if snapshot.chase_risk:
            details["sub_status"] = "CHASE_RISK"
            return (
                _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["CHASE_RISK"], details),
                snapshot,
            )

        support_threshold = KOSPI_SUPPORT_NEAR_PCT if profile == StrategyProfile.KOSPI_LEADER_PROFILE else KOSDAQ_SUPPORT_NEAR_PCT
        support_status = support_status_for_snapshot(snapshot, self.candle_builder, support_threshold)
        details.update(support_status)
        if support_status["nearest_support"] is None:
            details["sub_status"] = "DATA_INSUFFICIENT"
            details["insufficient_reason"].append("support_missing")
            return (
                _decision(
                    "StockPullbackEntryGate",
                    False,
                    35.0,
                    BlockType.TEMPORARY,
                    ["DATA_INSUFFICIENT"],
                    details,
                    can_recover=True,
                    recheck_after_sec=60,
                ),
                snapshot,
            )

        if profile == StrategyProfile.KOSPI_LEADER_PROFILE:
            return self._evaluate_kospi(candidate, snapshot, details)
        return self._evaluate_kosdaq(candidate, theme_result, leadership_result, snapshot, details)

    def _evaluate_kosdaq(
        self,
        candidate: Candidate,
        theme_result: ThemeStrengthResult,
        leadership_result: StockLeadershipResult,
        snapshot: IndicatorSnapshot,
        details: dict,
    ) -> tuple[GateDecision, IndicatorSnapshot]:
        pullback = snapshot.pullback_pct
        if pullback is None:
            return self._temporary_stock_wait(details, snapshot, "DATA_INSUFFICIENT")
        support_ok = details["support_touched"] and details["support_reclaimed"]
        confirmation_ok = snapshot.volume_reaccel or snapshot.failed_low_break_rebound
        basic_range = KOSDAQ_PULLBACK_RANGE[0] <= pullback <= KOSDAQ_PULLBACK_RANGE[1]
        shallow_exception = (
            KOSDAQ_SHALLOW_PULLBACK_RANGE[0] < pullback <= KOSDAQ_SHALLOW_PULLBACK_RANGE[1]
            and theme_result.grade == "A"
            and leadership_result.leadership_role in {"leader", "second_leader"}
            and support_ok
            and confirmation_ok
        )
        if pullback < KOSDAQ_PULLBACK_RANGE[0]:
            details["sub_status"] = "PULLBACK_TOO_DEEP"
            return _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["PULLBACK_TOO_DEEP"], details), snapshot
        if (basic_range and support_ok and confirmation_ok) or shallow_exception:
            details["sub_status"] = "PASS"
            details["shallow_exception"] = shallow_exception
            return _decision("StockPullbackEntryGate", True, 100.0, BlockType.NONE, [], details), snapshot
        return self._temporary_stock_wait(details, snapshot, "WAIT_PULLBACK_CONFIRMATION")

    def _evaluate_kospi(
        self,
        candidate: Candidate,
        snapshot: IndicatorSnapshot,
        details: dict,
    ) -> tuple[GateDecision, IndicatorSnapshot]:
        pullback = snapshot.pullback_pct
        if pullback is None:
            return self._temporary_stock_wait(details, snapshot, "DATA_INSUFFICIENT")
        if pullback < KOSPI_PULLBACK_RANGE[0]:
            details["sub_status"] = "PULLBACK_TOO_DEEP"
            return _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["PULLBACK_TOO_DEEP"], details), snapshot
        recovery = self._kospi_recovery(snapshot)
        details["recovery_confirmed"] = recovery
        support_ok = details["support_touched"] and details["support_reclaimed"]
        if KOSPI_PULLBACK_RANGE[0] <= pullback <= KOSPI_PULLBACK_RANGE[1] and support_ok and recovery:
            details["sub_status"] = "PASS"
            return _decision("StockPullbackEntryGate", True, 100.0, BlockType.NONE, [], details), snapshot
        return self._temporary_stock_wait(details, snapshot, "WAIT_PULLBACK_CONFIRMATION")

    def _temporary_stock_wait(
        self,
        details: dict,
        snapshot: IndicatorSnapshot,
        sub_status: str,
    ) -> tuple[GateDecision, IndicatorSnapshot]:
        details["sub_status"] = sub_status
        return (
            _decision(
                "StockPullbackEntryGate",
                False,
                55.0,
                BlockType.TEMPORARY,
                [sub_status],
                details,
                can_recover=True,
                recheck_after_sec=60,
            ),
            snapshot,
        )

    def _kospi_recovery(self, snapshot: IndicatorSnapshot) -> bool:
        if snapshot.vwap is not None and snapshot.price >= snapshot.vwap:
            return True
        if snapshot.day_mid is not None and snapshot.price >= snapshot.day_mid:
            return True
        if snapshot.failed_low_break_rebound:
            return True
        candles = self.candle_builder.completed_candles(snapshot.code, 1)
        if len(candles) >= 2 and candles[-1].close > candles[-2].high:
            return True
        return False


def support_status_for_snapshot(snapshot: IndicatorSnapshot, candle_builder: CandleBuilder, threshold_pct: float) -> dict:
    supports = _support_candidates(snapshot)
    if not supports:
        return {
            "nearest_support": None,
            "support_distance_pct": None,
            "support_touched": False,
            "support_reclaimed": False,
            "support_candidates": {},
        }
    current_price = snapshot.price
    above_supports = {
        name: price
        for name, price in supports.items()
        if price > 0 and current_price >= price
    }
    if not above_supports:
        nearest_name, nearest_price = min(
            supports.items(),
            key=lambda item: abs(_distance_pct(current_price, item[1])),
        )
    else:
        nearest_name, nearest_price = min(
            above_supports.items(),
            key=lambda item: abs(_distance_pct(current_price, item[1])),
        )
    distance = abs(_distance_pct(current_price, nearest_price))
    candles = candle_builder.completed_candles(snapshot.code, 1)[-3:]
    support_touched = any(candle.low <= nearest_price * (1 + threshold_pct / 100.0) for candle in candles)
    support_reclaimed = current_price >= nearest_price and (
        not candles or candles[-1].close >= nearest_price or snapshot.price >= nearest_price
    )
    return {
        "nearest_support": nearest_name,
        "nearest_support_price": nearest_price,
        "support_distance_pct": distance,
        "support_touched": bool(distance <= threshold_pct and support_touched),
        "support_reclaimed": bool(distance <= threshold_pct and support_reclaimed),
        "support_candidates": supports,
    }


def _snapshot_for(
    indicator_calculator: IndicatorCalculator,
    intraday_tracker: IntradayStateTracker,
    candle_builder: CandleBuilder,
    market_data: MarketDataStore,
    candidate_id: Optional[int],
    code: str,
) -> Optional[IndicatorSnapshot]:
    snapshot = indicator_calculator.build_snapshot(candidate_id or 0, code)
    if snapshot is None:
        return None
    tick = market_data.latest_tick(code)
    if tick is None:
        return snapshot
    candles = candle_builder.completed_candles(code, 1)
    return intraday_tracker.apply(snapshot, candles, tick)


def _support_candidates(snapshot: IndicatorSnapshot) -> dict:
    candidates = {
        "vwap": snapshot.vwap,
        "day_mid": snapshot.day_mid,
        "prev_high": float(snapshot.prev_high) if snapshot.prev_high > 0 else None,
        "ema20_5m": snapshot.ema20_5m,
    }
    return {name: float(value) for name, value in candidates.items() if value is not None and value > 0}


def _distance_pct(price: int, support: float) -> float:
    if support <= 0:
        return 999.0
    return ((price - support) / support) * 100.0


def _index_code_for(candidate: Candidate, theme_mapping: Optional[ThemeMapping]) -> str:
    profile = candidate.strategy_profile or (theme_mapping.strategy_profile if theme_mapping else None)
    market = candidate.market or (theme_mapping.market if theme_mapping else "")
    if profile == StrategyProfile.KOSDAQ_THEME_PROFILE or str(market).upper() == "KOSDAQ":
        return "KOSDAQ"
    return "KOSPI"


def _valid_pullback_range(profile: Optional[StrategyProfile]) -> tuple[float, float]:
    if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        return KOSPI_PULLBACK_RANGE
    return KOSDAQ_PULLBACK_RANGE


def _decision(
    gate_name: str,
    passed: bool,
    score: float,
    block_type: BlockType,
    reason_codes: list[str],
    details: dict,
    can_recover: bool = False,
    recheck_after_sec: int = 0,
) -> GateDecision:
    return GateDecision(
        gate_name=gate_name,
        passed=passed,
        score=score,
        block_type=block_type,
        can_recover=can_recover,
        recheck_after_sec=recheck_after_sec,
        reason_codes=reason_codes,
        details=details,
    )


def _theme_grade(
    score: float,
    active_count: int,
    valid_tick_ratio: float,
    valid_turnover_count: int,
    signal_pair: bool,
) -> str:
    if valid_tick_ratio < 0.5:
        return "C"
    if valid_tick_ratio < 0.67:
        return "B" if score >= B_THRESHOLD else "C"
    if signal_pair:
        if score >= A_THRESHOLD:
            return "A_SIGNAL"
        return "B+_SIGNAL" if score >= B_THRESHOLD else "C"
    if active_count >= 3 and valid_turnover_count >= 2 and score >= A_THRESHOLD:
        return "A"
    if score >= B_PLUS_THRESHOLD:
        return "B+"
    if score >= B_THRESHOLD:
        return "B"
    return "C"


def _leadership_scope(candidate: Candidate, mapping: ThemeMapping) -> str:
    if mapping.is_signal_stock:
        return "signal_only"
    profile = mapping.strategy_profile or candidate.strategy_profile
    if profile is not None:
        return "same_strategy_profile"
    if mapping.market or candidate.market:
        return "same_market"
    return "same_theme_all"


def _in_leadership_scope(
    target_mapping: ThemeMapping,
    candidate_mapping: ThemeMapping,
    candidate: Candidate,
    scope: str,
) -> bool:
    if scope == "signal_only":
        return candidate_mapping.is_signal_stock
    if candidate_mapping.is_signal_stock:
        return False
    if scope == "same_strategy_profile":
        target_profile = target_mapping.strategy_profile
        candidate_profile = candidate_mapping.strategy_profile or candidate.strategy_profile
        return target_profile is None or candidate_profile == target_profile
    if scope == "same_market":
        return bool(target_mapping.market) and candidate_mapping.market == target_mapping.market
    return True


def _leadership_role(rank: int, is_signal_stock: bool) -> str:
    if rank == 1:
        return "signal_leader" if is_signal_stock else "leader"
    if rank == 2:
        return "signal_second_leader" if is_signal_stock else "second_leader"
    if rank > 2:
        return "follower"
    return "unranked"


def _change_rate_rank(code: str, entries: list[_ThemeEntry]) -> int:
    ranked = sorted(
        entries,
        key=lambda entry: entry.tick.change_rate if entry.tick else -999.0,
        reverse=True,
    )
    return next((index + 1 for index, entry in enumerate(ranked) if entry.candidate.code == code), 0)


def _rank_score(max_points: float, rank: int, count: int) -> float:
    if rank <= 0:
        return 0.0
    if count <= 1:
        return max_points
    return max_points * ((count - rank) / (count - 1))


def _turnover(tick: Optional[StrategyTick]) -> int:
    if not _valid_tick(tick) or tick is None:
        return 0
    return tick.price * tick.cum_volume


def _valid_tick(tick: Optional[StrategyTick]) -> bool:
    return tick is not None and tick.price > 0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))
