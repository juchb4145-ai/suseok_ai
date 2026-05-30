from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from trading.strategy.candidates import candidate_is_discovery_only, normalize_code
from trading.strategy.candles import Candle, CandleBuilder
from trading.strategy.indicators import IndicatorCalculator
from trading.strategy.intraday import IntradayStateTracker
from trading.strategy.market_index import MarketIndexState, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.models import BlockType, Candidate, CandidateState, GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.reason_codes import ReasonCode, normalize_reason_codes
from trading.strategy.runtime_settings import (
    StrategyRuntimeSettings,
    attach_settings_details,
    legacy_strategy_runtime_settings,
)
from trading.theme_engine.context_provider import DynamicThemeContextProvider
from trading.theme_engine.models import StockLeadershipResult, ThemeContext, ThemeStrengthResult


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
SUPPORT_DEDUPE_PCT = 0.25
SIGNIFICANT_RISE_PCT = 1.0
THEME_SYNC_WEAK_THRESHOLD = 50.0
CHANGE_RATE_CAP_PCT = 10.0
LEADER_FOLLOWER_GAP_THRESHOLD = 8.0
LATE_CHASE_NEAR_HIGH_PCT = 0.5
LATE_CHASE_WARNING_SCORE = 25.0
LATE_CHASE_SOFT_BLOCK_SCORE = 80.0


@dataclass
class _ThemeEntry:
    candidate: Candidate
    mapping: ThemeContext
    tick: Optional[StrategyTick]
    turnover: int
    discovery_only: bool = False


class ThemeStrengthGate:
    def __init__(
        self,
        context_provider: DynamicThemeContextProvider,
        market_data: MarketDataStore,
        candle_builder: Optional[CandleBuilder] = None,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.context_provider = context_provider
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.settings = settings or legacy_strategy_runtime_settings()

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
            for mapping in self.context_provider.themes_for_code(clean_code):
                groups.setdefault(mapping.theme_id, []).append(
                    _ThemeEntry(
                        candidate=self.context_provider.enrich_candidate(candidate),
                        mapping=mapping,
                        tick=tick,
                        turnover=turnover,
                        discovery_only=candidate_is_discovery_only(candidate),
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
        scored_entries = [
            entry
            for entry in entries
            if not (entry.discovery_only and not _valid_tick(entry.tick))
        ]
        valid_turnovers = [entry for entry in entries if entry.turnover > 0]
        scored_valid_turnovers = [entry for entry in scored_entries if entry.turnover > 0]
        missing_turnover_count = len(scored_entries) - len(scored_valid_turnovers)
        valid_tick_ratio = len(valid_ticks) / len(scored_entries) if scored_entries else 1.0
        insufficient_reason = []
        if len(valid_ticks) < len(scored_entries):
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
        diagnostics_v2, comparison_reason_codes = _theme_diagnostics_v2(
            entries,
            top_entries,
            candle_builder=self.candle_builder,
            theme_turnover=theme_turnover,
            settings=self.settings,
        )

        candidate_max = self.settings.number("theme_thresholds.candidate_count_score_max", 20.0)
        candidate_full = self.settings.number("theme_thresholds.candidate_count_full_count", 3.0)
        average_max = self.settings.number("theme_thresholds.average_change_rate_score_max", 20.0)
        average_full = self.settings.number("theme_thresholds.average_change_rate_full_pct", 5.0)
        rising_max = self.settings.number("theme_thresholds.rising_ratio_score_max", 20.0)
        turnover_max = self.settings.number("theme_thresholds.theme_turnover_score_max", 20.0)
        leader_turnover_max = self.settings.number("theme_thresholds.leader_turnover_score_max", 20.0)
        score_components = {
            "candidate_count": min(candidate_max, (active_count / candidate_full) * candidate_max),
            "average_change_rate": _clamp((avg_change / average_full) * average_max, 0.0, average_max),
            "rising_ratio": rising_ratio * rising_max,
            "theme_turnover": (theme_turnover / max_theme_turnover) * turnover_max if max_theme_turnover > 0 else 0.0,
            "leader_turnover": (top2_turnover / max_top2_turnover) * leader_turnover_max if max_top2_turnover > 0 else 0.0,
        }
        score = sum(score_components.values())
        signal_pair = False
        grade = _theme_grade(
            score=score,
            active_count=active_count,
            valid_tick_ratio=valid_tick_ratio,
            valid_turnover_count=len(scored_valid_turnovers),
            signal_pair=signal_pair,
            settings=self.settings,
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
            details=attach_settings_details({
                "theme_id": theme_id,
                "theme_name": theme_name,
                "score_components": score_components,
                "active_candidate_count": active_count,
                "scored_candidate_count": len(scored_entries),
                "discovery_only_unscored_count": active_count - len(scored_entries),
                "valid_tick_count": len(valid_ticks),
                "valid_tick_ratio": valid_tick_ratio,
                "valid_turnover_count": len(scored_valid_turnovers),
                "missing_turnover_count": missing_turnover_count,
                "theme_turnover": theme_turnover,
                "top2_turnover": top2_turnover,
                "leader_codes": leader_codes,
                "signal_pair": signal_pair,
                "insufficient_reason": insufficient_reason,
                "theme_diagnostics_v2": diagnostics_v2,
                "comparison_reason_codes": comparison_reason_codes,
                "secondary_reason_codes": comparison_reason_codes,
            }, self.settings),
        )


class StockLeadershipGate:
    def __init__(
        self,
        context_provider: DynamicThemeContextProvider,
        market_data: MarketDataStore,
        candle_builder: Optional[CandleBuilder] = None,
        market_index_store: Optional[MarketIndexStore] = None,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.context_provider = context_provider
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.market_index_store = market_index_store
        self.settings = settings or legacy_strategy_runtime_settings()

    def evaluate(self, candidate: Candidate, candidates: list[Candidate]) -> list[StockLeadershipResult]:
        clean_code = normalize_code(candidate.code)
        mappings = self.context_provider.themes_for_code(clean_code)
        return [self._evaluate_mapping(candidate, mapping, candidates) for mapping in mappings]

    def evaluate_all(self, candidates: list[Candidate]) -> list[StockLeadershipResult]:
        results: list[StockLeadershipResult] = []
        for candidate in candidates:
            if candidate.state in ACTIVE_STATES:
                results.extend(self.evaluate(candidate, candidates))
        return results

    def _evaluate_mapping(self, candidate: Candidate, mapping: ThemeContext, candidates: list[Candidate]) -> StockLeadershipResult:
        scope = "same_dynamic_theme"
        scope_entries = self._scope_entries(mapping, candidates, scope)
        ranked = sorted(
            scope_entries,
            key=lambda entry: (
                entry.turnover,
                entry.tick.change_rate if entry.tick else 0.0,
                entry.mapping.membership_score,
            ),
            reverse=True,
        )
        code = normalize_code(candidate.code)
        rank = next((index + 1 for index, entry in enumerate(ranked) if entry.candidate.code == code), 0)
        role = _dynamic_leadership_role(rank)
        tick = self.market_data.latest_tick(code)
        turnover = _turnover(tick)
        rank_count = max(1, len(ranked))
        turnover_rank_score = _rank_score(
            self.settings.number("leadership_thresholds.turnover_rank_score_max", 35.0),
            rank,
            rank_count,
        )
        change_rank = _change_rate_rank(code, ranked)
        change_rate_rank_score = _rank_score(
            self.settings.number("leadership_thresholds.change_rate_rank_score_max", 25.0),
            change_rank,
            rank_count,
        )
        membership_score = _clamp(mapping.membership_score, 0.0, 1.0) * 25.0
        relation_penalty = 0.0 if str(mapping.relation_type.value if hasattr(mapping.relation_type, "value") else mapping.relation_type) not in {"rumor", "unknown"} else -20.0
        trade_eligible_penalty = 0.0 if mapping.trade_eligible else -15.0
        score_components = {
            "turnover_rank": turnover_rank_score,
            "change_rate_rank": change_rate_rank_score,
            "membership_score": membership_score,
            "relation_penalty": relation_penalty,
            "trade_eligible_penalty": trade_eligible_penalty,
        }
        insufficient_reason = []
        if not _valid_tick(tick):
            insufficient_reason.append("tick_missing")
        if turnover <= 0:
            insufficient_reason.append("turnover_missing")
        diagnostics_v2 = {
            "feature_version": "dynamic_leadership_v1",
            "leader_trade_value_rank": rank,
            "scope_candidate_codes": [entry.candidate.code for entry in ranked],
            "membership_score": mapping.membership_score,
            "relation_type": str(mapping.relation_type.value if hasattr(mapping.relation_type, "value") else mapping.relation_type),
            "trade_eligible": mapping.trade_eligible,
        }
        comparison_reason_codes = []
        if not mapping.trade_eligible:
            comparison_reason_codes.append("THEME_MEMBER_NOT_TRADE_ELIGIBLE")
        if diagnostics_v2["relation_type"] in {"rumor", "unknown"}:
            comparison_reason_codes.append("WEAK_THEME_RELATION")

        return StockLeadershipResult(
            candidate_id=candidate.id,
            code=code,
            theme_id=mapping.theme_id,
            theme_name=mapping.theme_name,
            score=round(_clamp(sum(score_components.values()), 0.0, 100.0), 4),
            leadership_rank=rank,
            leadership_role=role,
            leadership_scope=scope,
            details=attach_settings_details({
                "theme_id": mapping.theme_id,
                "theme_name": mapping.theme_name,
                "leadership_scope": scope,
                "leadership_rank": rank,
                "leadership_role": role,
                "score_components": score_components,
                "scope_candidate_codes": [entry.candidate.code for entry in ranked],
                "turnover": turnover,
                "change_rate": tick.change_rate if tick else 0.0,
                "insufficient_reason": insufficient_reason,
                "leadership_diagnostics_v2": diagnostics_v2,
                "comparison_reason_codes": comparison_reason_codes,
                "secondary_reason_codes": comparison_reason_codes,
            }, self.settings),
        )

    def _scope_entries(self, mapping: ThemeContext, candidates: list[Candidate], scope: str) -> list[_ThemeEntry]:
        entries: list[_ThemeEntry] = []
        for candidate in candidates:
            if candidate.state not in ACTIVE_STATES:
                continue
            enriched = self.context_provider.enrich_candidate(candidate)
            for member_mapping in self.context_provider.themes_for_code(enriched.code):
                if member_mapping.theme_id != mapping.theme_id:
                    continue
                tick = self.market_data.latest_tick(enriched.code)
                entries.append(_ThemeEntry(enriched, member_mapping, tick, _turnover(tick)))
        return entries


class MarketIndexGate:
    def __init__(self, market_index_store: MarketIndexStore, settings: Optional[StrategyRuntimeSettings] = None) -> None:
        self.market_index_store = market_index_store
        self.settings = settings or legacy_strategy_runtime_settings()

    def evaluate(self, candidate: Candidate, theme_context: Optional[ThemeContext] = None) -> GateDecision:
        index_code = _index_code_for(candidate, theme_context)
        state = self.market_index_store.state(index_code)
        details = {
            "index_code": index_code,
            "direction_5m": state.direction_5m,
            "low_break_recent": state.low_break_recent,
            "position_vs_mid": state.mid_position,
            "price": state.price,
            "day_mid": state.day_mid,
            "market_index_metadata": dict(state.metadata or {}),
            "index_slope_5m_pct": state.metadata.get("index_slope_5m_pct"),
            "index_slope_20m_pct": state.metadata.get("index_slope_20m_pct"),
        }
        if state.price <= 0:
            details["sub_status"] = "DATA_INSUFFICIENT"
            return _decision(
                "MarketIndexGate",
                False,
                self.settings.number("market_thresholds.data_insufficient_score", 0.0),
                BlockType.TEMPORARY,
                ["DATA_INSUFFICIENT"],
                details,
                can_recover=True,
                recheck_after_sec=self.settings.integer("market_thresholds.recheck_after_sec", 60),
                settings=self.settings,
            )
        weak_directions = set(self.settings.list_value("market_thresholds.weak_directions", ["DOWN"]))
        if state.low_break_recent or state.direction_5m in weak_directions:
            details["sub_status"] = "MARKET_WAIT"
            return _decision(
                "MarketIndexGate",
                False,
                self.settings.number("market_thresholds.index_weak_score", 20.0),
                BlockType.TEMPORARY,
                ["INDEX_WEAK"],
                details,
                can_recover=True,
                recheck_after_sec=self.settings.integer("market_thresholds.recheck_after_sec", 60),
                settings=self.settings,
            )
        pass_positions = set(self.settings.list_value("market_thresholds.pass_mid_positions", ["ABOVE_MID", "AT_MID"]))
        score = (
            self.settings.number("market_thresholds.pass_score_above_mid", 100.0)
            if state.mid_position in pass_positions
            else self.settings.number("market_thresholds.pass_score_below_mid", 75.0)
        )
        return _decision("MarketIndexGate", True, score, BlockType.NONE, [], details, settings=self.settings)


class ThemePullbackGate:
    def __init__(
        self,
        indicator_calculator: IndicatorCalculator,
        intraday_tracker: IntradayStateTracker,
        candle_builder: CandleBuilder,
        market_data: MarketDataStore,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.indicator_calculator = indicator_calculator
        self.intraday_tracker = intraday_tracker
        self.candle_builder = candle_builder
        self.market_data = market_data
        self.settings = settings or legacy_strategy_runtime_settings()

    def evaluate(self, theme_result: ThemeStrengthResult) -> GateDecision:
        leader_codes = list(theme_result.leader_codes[:2])
        details = attach_settings_details({
            "theme_id": theme_result.theme_id,
            "theme_grade": theme_result.grade,
            "leader_codes": leader_codes,
            "leader_pullback_pct": {},
            "leader_chase_risk": {},
            "leader_support_status": {},
            "insufficient_reason": [],
        }, self.settings)
        if theme_result.grade == "C":
            details["sub_status"] = "THEME_WEAK"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["THEME_WEAK"], details, settings=self.settings)
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
                settings=self.settings,
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
            support_status = support_status_for_snapshot(
                snapshot,
                self.candle_builder,
                self.settings.number("pullback_thresholds.kosdaq_support_near_pct", KOSDAQ_SUPPORT_NEAR_PCT),
                self.settings,
            )
            details["leader_pullback_pct"][code] = snapshot.pullback_pct
            details["leader_chase_risk"][code] = snapshot.chase_risk
            details["leader_support_status"][code] = support_status
            collapsed = snapshot.pullback_pct is not None and snapshot.pullback_pct < self.settings.number(
                "pullback_thresholds.theme_leader_collapse_pct",
                -7.0,
            )
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
                self.settings.number("pullback_thresholds.theme_pullback_data_insufficient_score", 30.0),
                BlockType.TEMPORARY,
                ["DATA_INSUFFICIENT"],
                details,
                can_recover=True,
                recheck_after_sec=60,
                settings=self.settings,
            )
        if collapse_count > 0:
            details["sub_status"] = "THEME_LEADER_COLLAPSE"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["THEME_LEADER_COLLAPSE"], details, settings=self.settings)
        if chase_count > 0:
            details["sub_status"] = "THEME_LEADER_CHASE_RISK"
            return _decision("ThemePullbackGate", False, 0.0, BlockType.FINAL, ["CHASE_RISK"], details, settings=self.settings)
        if healthy_count >= 1:
            return _decision("ThemePullbackGate", True, 100.0, BlockType.NONE, [], details, settings=self.settings)
        details["sub_status"] = "WAIT_PULLBACK_CONFIRMATION"
        return _decision(
            "ThemePullbackGate",
            False,
            self.settings.number("pullback_thresholds.theme_pullback_wait_score", 55.0),
            BlockType.TEMPORARY,
            ["WAIT_PULLBACK_CONFIRMATION"],
            details,
            can_recover=True,
            recheck_after_sec=60,
            settings=self.settings,
        )


class StockPullbackEntryGate:
    def __init__(
        self,
        indicator_calculator: IndicatorCalculator,
        intraday_tracker: IntradayStateTracker,
        candle_builder: CandleBuilder,
        market_data: MarketDataStore,
        settings: Optional[StrategyRuntimeSettings] = None,
    ) -> None:
        self.indicator_calculator = indicator_calculator
        self.intraday_tracker = intraday_tracker
        self.candle_builder = candle_builder
        self.market_data = market_data
        self.settings = settings or legacy_strategy_runtime_settings()

    def evaluate(
        self,
        candidate: Candidate,
        theme_result: ThemeStrengthResult,
        leadership_result: StockLeadershipResult,
        market_decision: Optional[GateDecision] = None,
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
        market_position = ""
        if market_decision is not None:
            market_position = str(market_decision.details.get("position_vs_mid") or "")
        details = attach_settings_details({
            "theme_id": theme_result.theme_id,
            "theme_grade": theme_result.grade,
            "leadership_role": leadership_result.leadership_role,
            "market_position": market_position,
            "profile": profile.value if profile else "",
            "pullback_pct": snapshot.pullback_pct if snapshot else None,
            "valid_range": _valid_pullback_range(profile, self.settings),
            "dynamic_pullback_policy": {},
            "nearest_support": None,
            "support_distance_pct": None,
            "support_touched": False,
            "support_reclaimed": False,
            "volume_reaccel": snapshot.volume_reaccel if snapshot else False,
            "failed_low_break_rebound": snapshot.failed_low_break_rebound if snapshot else False,
            "chase_risk": snapshot.chase_risk if snapshot else False,
            "insufficient_reason": [],
        }, self.settings)
        if snapshot is None:
            details["sub_status"] = "DATA_INSUFFICIENT"
            details["insufficient_reason"].append("snapshot_missing")
            _attach_late_chase_details(
                details,
                None,
                {},
                self.settings.number("pullback_thresholds.kosdaq_support_near_pct", KOSDAQ_SUPPORT_NEAR_PCT),
                self.settings,
            )
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
                    settings=self.settings,
                ),
                None,
            )

        support_threshold = (
            self.settings.number("pullback_thresholds.kospi_support_near_pct", KOSPI_SUPPORT_NEAR_PCT)
            if profile == StrategyProfile.KOSPI_LEADER_PROFILE
            else self.settings.number("pullback_thresholds.kosdaq_support_near_pct", KOSDAQ_SUPPORT_NEAR_PCT)
        )
        support_status = support_status_for_snapshot(snapshot, self.candle_builder, support_threshold, self.settings)
        details.update(support_status)
        _attach_late_chase_details(details, snapshot, support_status, support_threshold, self.settings)
        if snapshot.chase_risk:
            details["sub_status"] = "CHASE_RISK"
            return (
                _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["CHASE_RISK"], details, settings=self.settings),
                snapshot,
            )

        policy = _dynamic_pullback_policy(profile, theme_result, leadership_result, snapshot, market_position, self.settings)
        details["valid_range"] = policy["valid_range"]
        details["dynamic_pullback_policy"] = policy
        if support_status["nearest_support"] is None:
            details["sub_status"] = "DATA_INSUFFICIENT"
            details["insufficient_reason"].append("support_missing")
            return (
                _decision(
                    "StockPullbackEntryGate",
                    False,
                    self.settings.number("pullback_thresholds.stock_support_missing_score", 35.0),
                    BlockType.TEMPORARY,
                    ["DATA_INSUFFICIENT"],
                    details,
                    can_recover=True,
                    recheck_after_sec=60,
                    settings=self.settings,
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
        valid_range = tuple(details.get("valid_range") or _valid_pullback_range(StrategyProfile.KOSDAQ_THEME_PROFILE, self.settings))
        confirmation_mode = str(details.get("dynamic_pullback_policy", {}).get("confirmation_mode") or "standard")
        support_ok = details["support_touched"] and details["support_reclaimed"]
        confirmation_ok = _confirmation_ok(snapshot, details, confirmation_mode)
        basic_range = valid_range[0] <= pullback <= valid_range[1]
        shallow_range = self.settings.range_pair("pullback_thresholds.kosdaq_shallow_range", KOSDAQ_SHALLOW_PULLBACK_RANGE)
        shallow_exception = (
            shallow_range[0] < pullback <= shallow_range[1]
            and theme_result.grade == "A"
            and leadership_result.leadership_role in {"leader", "second_leader"}
            and support_ok
            and confirmation_ok
        )
        if pullback < valid_range[0]:
            details["sub_status"] = "PULLBACK_TOO_DEEP"
            return _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["PULLBACK_TOO_DEEP"], details, settings=self.settings), snapshot
        if (basic_range and support_ok and confirmation_ok) or shallow_exception:
            details["sub_status"] = "PASS"
            details["shallow_exception"] = shallow_exception
            return _decision("StockPullbackEntryGate", True, 100.0, BlockType.NONE, [], details, settings=self.settings), snapshot
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
        valid_range = tuple(details.get("valid_range") or _valid_pullback_range(StrategyProfile.KOSPI_LEADER_PROFILE, self.settings))
        confirmation_mode = str(details.get("dynamic_pullback_policy", {}).get("confirmation_mode") or "standard")
        if pullback < valid_range[0]:
            details["sub_status"] = "PULLBACK_TOO_DEEP"
            return _decision("StockPullbackEntryGate", False, 0.0, BlockType.FINAL, ["PULLBACK_TOO_DEEP"], details, settings=self.settings), snapshot
        recovery = self._kospi_recovery(snapshot)
        details["recovery_confirmed"] = recovery
        support_ok = details["support_touched"] and details["support_reclaimed"]
        confirmation_ok = recovery and _confirmation_ok(snapshot, details, confirmation_mode)
        if valid_range[0] <= pullback <= valid_range[1] and support_ok and confirmation_ok:
            details["sub_status"] = "PASS"
            return _decision("StockPullbackEntryGate", True, 100.0, BlockType.NONE, [], details, settings=self.settings), snapshot
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
                self.settings.number("pullback_thresholds.stock_pullback_wait_score", 55.0),
                BlockType.TEMPORARY,
                [sub_status],
                details,
                can_recover=True,
                recheck_after_sec=60,
                settings=self.settings,
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


def support_status_for_snapshot(
    snapshot: IndicatorSnapshot,
    candle_builder: CandleBuilder,
    threshold_pct: float,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    supports = _support_candidates(snapshot, active_settings)
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


def _attach_late_chase_details(
    details: dict,
    snapshot: Optional[IndicatorSnapshot],
    support_status: dict,
    support_threshold_pct: float,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> None:
    active_settings = settings or legacy_strategy_runtime_settings()
    diagnostics = _late_chase_diagnostics(snapshot, support_status, support_threshold_pct, active_settings)
    details["late_chase_diagnostics"] = diagnostics
    details["late_chase_level"] = diagnostics["late_chase_level"]
    details["late_chase_score"] = diagnostics["late_chase_score"]
    comparison_codes = list(details.get("comparison_reason_codes") or [])
    comparison_codes.extend(diagnostics.get("reason_codes") or [])
    details["comparison_reason_codes"] = normalize_reason_codes(comparison_codes)
    details["secondary_reason_codes"] = normalize_reason_codes(
        list(details.get("secondary_reason_codes") or []) + details["comparison_reason_codes"]
    )


def _late_chase_diagnostics(
    snapshot: Optional[IndicatorSnapshot],
    support_status: dict,
    support_threshold_pct: float,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    input_missing: list[str] = []
    if snapshot is None:
        input_missing.append("snapshot_missing")
        return _late_chase_result(
            near_session_high=False,
            distance_from_session_high_pct=None,
            nearest_support_type=str(support_status.get("nearest_support") or ""),
            nearest_support_price=_float_or_none(support_status.get("nearest_support_price")),
            support_distance_pct=_float_or_none(support_status.get("support_distance_pct")),
            support_distance_excessive=False,
            volume_deceleration=False,
            volume_reacceleration_confirmed=False,
            after_large_3m_candle=False,
            after_large_5m_candle=False,
            large_candle_body_pct=None,
            candle_close_position=None,
            input_missing=input_missing,
            settings=active_settings,
        )
    metadata = dict(snapshot.metadata or {})
    distance_from_high = _float_or_none(metadata.get("chase_risk_current_within_high_pct"))
    if distance_from_high is None and snapshot.day_high > 0 and snapshot.price > 0:
        distance_from_high = ((snapshot.day_high - snapshot.price) / snapshot.day_high) * 100.0
    if distance_from_high is None:
        input_missing.append("session_high_distance_missing")
    near_high_threshold = active_settings.number("late_chase_thresholds.near_session_high_pct", LATE_CHASE_NEAR_HIGH_PCT)
    near_session_high = distance_from_high is not None and 0 <= distance_from_high <= near_high_threshold
    nearest_support_type = str(support_status.get("nearest_support") or "")
    nearest_support_price = _float_or_none(support_status.get("nearest_support_price"))
    support_distance_pct = _float_or_none(support_status.get("support_distance_pct"))
    if not nearest_support_type or nearest_support_price is None or support_distance_pct is None:
        input_missing.append("support_missing")
    support_distance_excessive = support_distance_pct is not None and support_distance_pct > support_threshold_pct
    volume_deceleration_ready = metadata.get("volume_deceleration_ready")
    volume_deceleration = bool(metadata.get("volume_deceleration"))
    if volume_deceleration_ready is False or volume_deceleration_ready is None:
        input_missing.append("volume_deceleration_history_short")
    volume_reacceleration_confirmed = bool(snapshot.volume_reaccel)
    if metadata.get("volume_reaccel_ready") is False:
        input_missing.append("volume_reaccel_history_short")
    after_large_3m = bool(metadata.get("after_large_3m_candle"))
    after_large_5m = bool(metadata.get("after_large_5m_candle"))
    if metadata.get("large_3m_candle_body_pct") is None and metadata.get("large_5m_candle_body_pct") is None:
        input_missing.append("large_candle_history_short")
    return _late_chase_result(
        near_session_high=near_session_high,
        distance_from_session_high_pct=distance_from_high,
        nearest_support_type=nearest_support_type,
        nearest_support_price=nearest_support_price,
        support_distance_pct=support_distance_pct,
        support_distance_excessive=support_distance_excessive,
        volume_deceleration=volume_deceleration,
        volume_reacceleration_confirmed=volume_reacceleration_confirmed,
        after_large_3m_candle=after_large_3m,
        after_large_5m_candle=after_large_5m,
        large_candle_body_pct=_float_or_none(metadata.get("large_candle_body_pct")),
        candle_close_position=_float_or_none(metadata.get("candle_close_position")),
        input_missing=input_missing,
        settings=active_settings,
    )


def _late_chase_result(
    *,
    near_session_high: bool,
    distance_from_session_high_pct: Optional[float],
    nearest_support_type: str,
    nearest_support_price: Optional[float],
    support_distance_pct: Optional[float],
    support_distance_excessive: bool,
    volume_deceleration: bool,
    volume_reacceleration_confirmed: bool,
    after_large_3m_candle: bool,
    after_large_5m_candle: bool,
    large_candle_body_pct: Optional[float],
    candle_close_position: Optional[float],
    input_missing: list[str],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    after_large = after_large_3m_candle or after_large_5m_candle
    score = 0.0
    if near_session_high:
        score += active_settings.number("late_chase_thresholds.score_near_session_high", 20.0)
    if support_distance_excessive:
        score += active_settings.number("late_chase_thresholds.score_support_distance_excessive", 25.0)
    if volume_deceleration:
        score += active_settings.number("late_chase_thresholds.score_volume_deceleration", 20.0)
    if after_large:
        score += active_settings.number("late_chase_thresholds.score_after_large_candle", 20.0)
    if not volume_reacceleration_confirmed:
        score += active_settings.number("late_chase_thresholds.score_no_volume_reacceleration", 15.0)
    if volume_reacceleration_confirmed and not support_distance_excessive:
        score = min(score, active_settings.number("late_chase_thresholds.reaccel_near_high_cap_score", 20.0) if near_session_high else score)
    core_late_chase = (
        near_session_high
        and support_distance_excessive
        and volume_deceleration
        and after_large
        and not volume_reacceleration_confirmed
    )
    if core_late_chase and score >= active_settings.number("late_chase_thresholds.soft_block_score", LATE_CHASE_SOFT_BLOCK_SCORE):
        level = "soft_block"
    elif score >= active_settings.number("late_chase_thresholds.warning_score", LATE_CHASE_WARNING_SCORE):
        level = "warning"
    else:
        level = "none"
    reason_codes = []
    if input_missing:
        reason_codes.append(ReasonCode.INPUT_MISSING.value)
    if level == "soft_block":
        reason_codes.extend([ReasonCode.LATE_CHASE.value, ReasonCode.SOFT_BLOCK_ONLY.value])
    return {
        "feature_version": "late_chase_diagnostics_v1",
        "near_session_high": bool(near_session_high),
        "distance_from_session_high_pct": _round_optional(distance_from_session_high_pct),
        "nearest_support_type": nearest_support_type,
        "nearest_support_price": _round_optional(nearest_support_price),
        "support_distance_pct": _round_optional(support_distance_pct),
        "support_distance_excessive": bool(support_distance_excessive),
        "volume_deceleration": bool(volume_deceleration),
        "volume_reacceleration_confirmed": bool(volume_reacceleration_confirmed),
        "after_large_3m_candle": bool(after_large_3m_candle),
        "after_large_5m_candle": bool(after_large_5m_candle),
        "large_candle_body_pct": _round_optional(large_candle_body_pct),
        "candle_close_position": _round_optional(candle_close_position),
        "late_chase_score": round(score, 4),
        "late_chase_level": level,
        "reason_codes": normalize_reason_codes(reason_codes),
        "input_missing_fields": normalize_reason_codes(input_missing),
    }


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 6)


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


def _support_candidates(snapshot: IndicatorSnapshot, settings: Optional[StrategyRuntimeSettings] = None) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    candidates = {
        "vwap": snapshot.vwap,
        "base_line_120": snapshot.base_line_120,
        "envelope_mid": snapshot.envelope_mid,
        "day_mid": snapshot.day_mid,
        "prev_high": float(snapshot.prev_high) if snapshot.prev_high > 0 else None,
        "ema20_5m": snapshot.ema20_5m,
    }
    supports = {name: float(value) for name, value in candidates.items() if value is not None and value > 0}
    return _dedupe_price_levels(
        supports,
        active_settings.number("pullback_thresholds.support_dedupe_pct", SUPPORT_DEDUPE_PCT),
        snapshot.price,
    )


def _dedupe_price_levels(candidates: dict[str, float], threshold_pct: float, current_price: int) -> dict[str, float]:
    result: dict[str, float] = {}
    for name, price in candidates.items():
        duplicate_name = ""
        for kept_name, kept_price in result.items():
            if kept_price > 0 and abs((price - kept_price) / kept_price) * 100.0 <= threshold_pct:
                duplicate_name = kept_name
                break
        if not duplicate_name:
            result[name] = price
            continue
        kept_price = result[duplicate_name]
        kept_is_support = kept_price <= current_price
        candidate_is_support = price <= current_price
        if candidate_is_support and not kept_is_support:
            result.pop(duplicate_name)
            result[name] = price
    return result


def _theme_diagnostics_v2(
    entries: list[_ThemeEntry],
    top_entries: list[_ThemeEntry],
    *,
    candle_builder: Optional[CandleBuilder],
    theme_turnover: int,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> tuple[dict, list[str]]:
    active_settings = settings or legacy_strategy_runtime_settings()
    valid_entries = [entry for entry in entries if _valid_tick(entry.tick)]
    changes = [float(entry.tick.change_rate) for entry in valid_entries if entry.tick is not None]
    input_missing: list[str] = []
    if len(valid_entries) < len(entries):
        input_missing.append("tick_missing")
    if len(valid_entries) < 3:
        input_missing.append("insufficient_sample")
    advancing_ratio = _ratio(sum(1 for value in changes if value > 0), len(valid_entries))
    significant_rise_pct = active_settings.number("theme_thresholds.significant_rise_pct", SIGNIFICANT_RISE_PCT)
    significant_advancing_ratio = _ratio(sum(1 for value in changes if value >= significant_rise_pct), len(valid_entries))
    growth_values = [
        growth
        for entry in valid_entries
        for growth in [_trade_value_growth_pct(candle_builder, entry.candidate.code, 5)]
        if growth is not None
    ]
    if candle_builder is None or len(growth_values) < len(valid_entries):
        input_missing.append("trade_value_growth_history_short")
    trade_value_growth_ratio = _ratio(sum(1 for value in growth_values if value > 0), len(growth_values))
    sync_score = round(
        (advancing_ratio * 40.0)
        + (significant_advancing_ratio * 30.0)
        + (trade_value_growth_ratio * 30.0),
        4,
    )
    top_leader_share = 0.0
    if theme_turnover > 0 and top_entries:
        top_leader_share = round((top_entries[0].turnover / theme_turnover) * 100.0, 6)
    leader_gap = _leader_follower_gap_pct(top_entries)
    diagnostics = {
        "feature_version": "theme_diagnostics_v2",
        "theme_sync_score": sync_score,
        "theme_advancing_ratio": round(advancing_ratio, 6),
        "theme_significant_advancing_ratio": round(significant_advancing_ratio, 6),
        "theme_trade_value_growth_member_ratio": round(trade_value_growth_ratio, 6),
        "theme_trimmed_avg_change_pct": _trimmed_average(changes, input_missing),
        "theme_capped_avg_change_pct": _average(
            [
                _clamp(
                    value,
                    -active_settings.number("theme_thresholds.change_rate_cap_pct", CHANGE_RATE_CAP_PCT),
                    active_settings.number("theme_thresholds.change_rate_cap_pct", CHANGE_RATE_CAP_PCT),
                )
                for value in changes
            ]
        ),
        "theme_trade_value_growth_pct": _theme_trade_value_growth_pct(candle_builder, valid_entries, input_missing),
        "top_leader_trade_value_share": top_leader_share,
        "leader_persistence_score": _entry_persistence_score(top_entries[0], candle_builder, input_missing) if top_entries else None,
        "leader_follower_gap_pct": leader_gap,
        "theme_member_count": len(entries),
        "valid_member_count": len(valid_entries),
        "input_missing_fields": normalize_reason_codes(input_missing),
    }
    reason_codes = []
    if diagnostics["input_missing_fields"]:
        reason_codes.append(ReasonCode.INPUT_MISSING.value)
    if len(valid_entries) >= 2 and sync_score < active_settings.number("theme_thresholds.theme_sync_weak_score", THEME_SYNC_WEAK_THRESHOLD):
        reason_codes.append(ReasonCode.THEME_SYNC_WEAK.value)
        reason_codes.append(ReasonCode.SOFT_BLOCK_ONLY.value)
    if leader_gap is not None and leader_gap >= active_settings.number("theme_thresholds.leader_follower_gap_threshold_pct", LEADER_FOLLOWER_GAP_THRESHOLD):
        reason_codes.append(ReasonCode.LEADER_FOLLOWER_GAP.value)
        reason_codes.append(ReasonCode.SOFT_BLOCK_ONLY.value)
    return diagnostics, normalize_reason_codes(reason_codes)


def _leadership_diagnostics_v2(
    code: str,
    mapping: ThemeContext,
    ranked: list[_ThemeEntry],
    candle_builder: Optional[CandleBuilder],
    market_index_store: Optional[MarketIndexStore],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> tuple[dict, list[str]]:
    active_settings = settings or legacy_strategy_runtime_settings()
    input_missing: list[str] = []
    target = next((entry for entry in ranked if entry.candidate.code == code), None)
    current_leader = ranked[0] if ranked else None
    expected_leader = _expected_leader(ranked)
    rank_trade_5m = _metric_rank(code, ranked, candle_builder, 5, "trade_value", input_missing)
    rank_trade_20m = _metric_rank(code, ranked, candle_builder, 20, "trade_value", input_missing)
    rank_change_5m = _metric_rank(code, ranked, candle_builder, 5, "change_pct", input_missing)
    rank_change_20m = _metric_rank(code, ranked, candle_builder, 20, "change_pct", input_missing)
    high_times = _high_update_times(candle_builder, code, input_missing)
    first_breakout_at = _first_breakout_at(candle_builder, code, input_missing)
    persistence_score = _persistence_score(
        rank_trade_5m,
        rank_trade_20m,
        rank_change_5m,
        rank_change_20m,
        high_times[1],
        active_settings,
    )
    leader_gap = _leader_follower_gap(target, current_leader, candle_builder, input_missing, active_settings)
    leader_replaced = _leader_replaced(code, expected_leader, current_leader, ranked, candle_builder)
    relative_5m = _relative_strength_vs_index(code, mapping, candle_builder, market_index_store, 5, input_missing)
    relative_20m = _relative_strength_vs_index(code, mapping, candle_builder, market_index_store, 20, input_missing)
    diagnostics = {
        "feature_version": "leadership_diagnostics_v2",
        "leader_first_breakout_at": first_breakout_at,
        "leader_last_high_update_at": high_times[1],
        "leader_trade_value_rank_5m": rank_trade_5m,
        "leader_trade_value_rank_20m": rank_trade_20m,
        "leader_change_rank_5m": rank_change_5m,
        "leader_change_rank_20m": rank_change_20m,
        "leader_persistence_score": persistence_score,
        "leader_follower_gap": leader_gap,
        "leader_follower_gap_pct": leader_gap.get("score_pct"),
        "leader_replaced": leader_replaced,
        "expected_leader_code": expected_leader.candidate.code if expected_leader else "",
        "current_leader_code": current_leader.candidate.code if current_leader else "",
        "relative_strength_vs_index_5m": relative_5m,
        "relative_strength_vs_index_20m": relative_20m,
        "input_missing_fields": normalize_reason_codes(input_missing),
    }
    reason_codes = []
    if diagnostics["input_missing_fields"]:
        reason_codes.append(ReasonCode.INPUT_MISSING.value)
    if leader_gap.get("score_pct") is not None and leader_gap["score_pct"] >= active_settings.number("leadership_thresholds.leader_follower_gap_threshold_pct", LEADER_FOLLOWER_GAP_THRESHOLD):
        reason_codes.append(ReasonCode.LEADER_FOLLOWER_GAP.value)
        reason_codes.append(ReasonCode.SOFT_BLOCK_ONLY.value)
    if leader_replaced:
        reason_codes.append(ReasonCode.LEADER_REPLACED.value)
        reason_codes.append(ReasonCode.SOFT_BLOCK_ONLY.value)
    return diagnostics, normalize_reason_codes(reason_codes)


def _trimmed_average(values: list[float], input_missing: list[str]) -> Optional[float]:
    if not values:
        return None
    if len(values) < 3:
        if "insufficient_sample" not in input_missing:
            input_missing.append("insufficient_sample")
        return _average(values)
    ordered = sorted(values)
    return _average(ordered[1:-1] or ordered)


def _average(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _theme_trade_value_growth_pct(
    candle_builder: Optional[CandleBuilder],
    entries: list[_ThemeEntry],
    input_missing: list[str],
) -> Optional[float]:
    recent_total = 0.0
    previous_total = 0.0
    used = 0
    for entry in entries:
        pair = _trade_value_pair(candle_builder, entry.candidate.code, 5)
        if pair is None:
            continue
        recent, previous = pair
        recent_total += recent
        previous_total += previous
        used += 1
    if used == 0 or previous_total <= 0:
        if "theme_trade_value_growth_missing" not in input_missing:
            input_missing.append("theme_trade_value_growth_missing")
        return None
    return round(((recent_total - previous_total) / previous_total) * 100.0, 6)


def _trade_value_growth_pct(candle_builder: Optional[CandleBuilder], code: str, minutes: int) -> Optional[float]:
    pair = _trade_value_pair(candle_builder, code, minutes)
    if pair is None:
        return None
    recent, previous = pair
    if previous <= 0:
        return None
    return ((recent - previous) / previous) * 100.0


def _trade_value_pair(candle_builder: Optional[CandleBuilder], code: str, minutes: int) -> Optional[tuple[float, float]]:
    if candle_builder is None:
        return None
    candles = candle_builder.completed_candles(code, 1)
    if len(candles) < minutes * 2:
        return None
    previous = candles[-(minutes * 2) : -minutes]
    recent = candles[-minutes:]
    return _candles_trade_value(recent), _candles_trade_value(previous)


def _candles_trade_value(candles: list[Candle]) -> float:
    return sum(float(candle.close) * float(candle.volume) for candle in candles)


def _window_change_pct(candle_builder: Optional[CandleBuilder], code: str, minutes: int) -> Optional[float]:
    if candle_builder is None:
        return None
    candles = candle_builder.completed_candles(code, 1)
    if len(candles) < minutes:
        return None
    window = candles[-minutes:]
    first = window[0].open
    last = window[-1].close
    if first <= 0:
        return None
    return ((last - first) / first) * 100.0


def _metric_rank(
    code: str,
    entries: list[_ThemeEntry],
    candle_builder: Optional[CandleBuilder],
    minutes: int,
    metric: str,
    input_missing: list[str],
) -> Optional[int]:
    values: list[tuple[str, float]] = []
    for entry in entries:
        if metric == "trade_value":
            value = _window_trade_value(candle_builder, entry.candidate.code, minutes)
        else:
            value = _window_change_pct(candle_builder, entry.candidate.code, minutes)
        if value is not None:
            values.append((entry.candidate.code, float(value)))
    if len(values) < len(entries) or not values:
        missing = f"{metric}_rank_{minutes}m_missing"
        if missing not in input_missing:
            input_missing.append(missing)
    ranked = sorted(values, key=lambda item: item[1], reverse=True)
    return next((index + 1 for index, (ranked_code, _) in enumerate(ranked) if ranked_code == code), None)


def _window_trade_value(candle_builder: Optional[CandleBuilder], code: str, minutes: int) -> Optional[float]:
    if candle_builder is None:
        return None
    candles = candle_builder.completed_candles(code, 1)
    if len(candles) < minutes:
        return None
    return _candles_trade_value(candles[-minutes:])


def _high_update_times(candle_builder: Optional[CandleBuilder], code: str, input_missing: list[str]) -> tuple[Optional[str], Optional[str]]:
    if candle_builder is None:
        _append_unique(input_missing, "high_update_history_missing")
        return None, None
    candles = candle_builder.completed_candles(code, 1)
    if not candles:
        _append_unique(input_missing, "high_update_history_missing")
        return None, None
    running_high = 0
    first_update = None
    last_update = None
    for candle in candles:
        if candle.high > running_high:
            running_high = candle.high
            last_update = candle.start_at.isoformat()
            if first_update is None:
                first_update = last_update
    return first_update, last_update


def _first_breakout_at(candle_builder: Optional[CandleBuilder], code: str, input_missing: list[str]) -> Optional[str]:
    if candle_builder is None:
        _append_unique(input_missing, "breakout_history_missing")
        return None
    candles = candle_builder.completed_candles(code, 1)
    if not candles:
        _append_unique(input_missing, "breakout_history_missing")
        return None
    running_high = candles[0].high
    for candle in candles[1:]:
        if candle.high > running_high and candle.close >= candle.open:
            return candle.start_at.isoformat()
        running_high = max(running_high, candle.high)
    rising = next((candle for candle in candles if candle.close > candle.open), None)
    return rising.start_at.isoformat() if rising else None


def _entry_persistence_score(entry: _ThemeEntry, candle_builder: Optional[CandleBuilder], input_missing: list[str]) -> Optional[float]:
    if candle_builder is None:
        _append_unique(input_missing, "leader_persistence_history_missing")
        return None
    code = entry.candidate.code
    change_5m = _window_change_pct(candle_builder, code, 5)
    change_20m = _window_change_pct(candle_builder, code, 20)
    _, last_high_update = _high_update_times(candle_builder, code, input_missing)
    if change_5m is None or change_20m is None:
        _append_unique(input_missing, "leader_persistence_history_short")
        return None
    score = 40.0 if change_5m > 0 else 0.0
    score += 40.0 if change_20m > 0 else 0.0
    score += 20.0 if last_high_update else 0.0
    return round(score, 4)


def _persistence_score(
    trade_rank_5m: Optional[int],
    trade_rank_20m: Optional[int],
    change_rank_5m: Optional[int],
    change_rank_20m: Optional[int],
    last_high_update_at: Optional[str],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> Optional[float]:
    active_settings = settings or legacy_strategy_runtime_settings()
    ranks = [trade_rank_5m, trade_rank_20m, change_rank_5m, change_rank_20m]
    if any(rank is None for rank in ranks):
        return None
    slot_points = active_settings.number("leadership_thresholds.persistence_rank_slot_points", 25.0)
    step_penalty = active_settings.number("leadership_thresholds.persistence_rank_step_penalty", 10.0)
    score = sum(max(0.0, slot_points - ((float(rank) - 1.0) * step_penalty)) for rank in ranks if rank is not None)
    if last_high_update_at:
        score = min(100.0, score + active_settings.number("leadership_thresholds.persistence_high_update_bonus", 10.0))
    return round(score, 4)


def _expected_leader(entries: list[_ThemeEntry]) -> Optional[_ThemeEntry]:
    if not entries:
        return None
    return sorted(
        entries,
        key=lambda entry: (
            entry.mapping.membership_score,
            entry.turnover,
            entry.tick.change_rate if entry.tick else 0.0,
        ),
        reverse=True,
    )[0]


def _leader_replaced(
    code: str,
    expected_leader: Optional[_ThemeEntry],
    current_leader: Optional[_ThemeEntry],
    entries: list[_ThemeEntry],
    candle_builder: Optional[CandleBuilder],
) -> bool:
    if expected_leader is None or current_leader is None:
        return False
    if expected_leader.candidate.code == current_leader.candidate.code:
        return False
    if code not in {expected_leader.candidate.code, current_leader.candidate.code}:
        return False
    current_trade_rank = _metric_rank(current_leader.candidate.code, entries, candle_builder, 5, "trade_value", [])
    current_change_rank = _metric_rank(current_leader.candidate.code, entries, candle_builder, 5, "change_pct", [])
    if current_trade_rank is None or current_change_rank is None:
        return current_leader.turnover > expected_leader.turnover and (
            (current_leader.tick.change_rate if current_leader.tick else 0.0)
            >= (expected_leader.tick.change_rate if expected_leader.tick else 0.0)
        )
    return current_trade_rank <= 2 and current_change_rank <= 2


def _leader_follower_gap_pct(entries: list[_ThemeEntry]) -> Optional[float]:
    if len(entries) < 2:
        return None
    leader = entries[0]
    followers = entries[1:]
    leader_change = leader.tick.change_rate if leader.tick else None
    follower_changes = [entry.tick.change_rate for entry in followers if entry.tick is not None]
    if leader_change is None or not follower_changes:
        return None
    baseline = _average(follower_changes)
    if baseline is None:
        return None
    return round(max(0.0, float(leader_change) - baseline), 6)


def _leader_follower_gap(
    target: Optional[_ThemeEntry],
    leader: Optional[_ThemeEntry],
    candle_builder: Optional[CandleBuilder],
    input_missing: list[str],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    if target is None or leader is None:
        _append_unique(input_missing, "leader_or_candidate_missing")
        return {"score_pct": None}
    target_change = target.tick.change_rate if target.tick else None
    leader_change = leader.tick.change_rate if leader.tick else None
    if target_change is None or leader_change is None:
        _append_unique(input_missing, "leader_gap_tick_missing")
        return {"score_pct": None}
    target_turnover = max(0, target.turnover)
    leader_turnover = max(0, leader.turnover)
    trade_gap_pct = None
    if leader_turnover > 0:
        trade_gap_pct = max(0.0, ((leader_turnover - target_turnover) / leader_turnover) * 100.0)
    else:
        _append_unique(input_missing, "leader_gap_turnover_missing")
    breakout_delay = _time_delay_minutes(
        _first_breakout_at(candle_builder, target.candidate.code, input_missing),
        _first_breakout_at(candle_builder, leader.candidate.code, input_missing),
    )
    high_delay = _time_delay_minutes(
        _high_update_times(candle_builder, target.candidate.code, input_missing)[1],
        _high_update_times(candle_builder, leader.candidate.code, input_missing)[1],
    )
    change_gap = max(0.0, float(leader_change) - float(target_change))
    score = change_gap
    if trade_gap_pct is not None:
        score += min(
            active_settings.number("leadership_thresholds.leader_gap_component_cap", 10.0),
            trade_gap_pct / active_settings.number("leadership_thresholds.leader_gap_trade_value_divisor", 10.0),
        )
    delay_min = active_settings.number("leadership_thresholds.leader_gap_delay_min", 5.0)
    delay_divisor = active_settings.number("leadership_thresholds.leader_gap_delay_divisor", 3.0)
    delay_cap = active_settings.number("leadership_thresholds.leader_gap_component_cap", 10.0)
    if breakout_delay is not None and breakout_delay > delay_min:
        score += min(delay_cap, breakout_delay / delay_divisor)
    if high_delay is not None and high_delay > delay_min:
        score += min(delay_cap, high_delay / delay_divisor)
    return {
        "leader_code": leader.candidate.code,
        "candidate_code": target.candidate.code,
        "change_rate_gap_pct": round(change_gap, 6),
        "trade_value_gap_pct": round(trade_gap_pct, 6) if trade_gap_pct is not None else None,
        "breakout_delay_min": breakout_delay,
        "high_update_delay_min": high_delay,
        "score_pct": round(score, 6),
    }


def _relative_strength_vs_index(
    code: str,
    mapping: ThemeContext,
    candle_builder: Optional[CandleBuilder],
    market_index_store: Optional[MarketIndexStore],
    minutes: int,
    input_missing: list[str],
) -> Optional[float]:
    stock_change = _window_change_pct(candle_builder, code, minutes)
    if stock_change is None:
        _append_unique(input_missing, f"stock_return_{minutes}m_missing")
        return None
    if market_index_store is None:
        _append_unique(input_missing, "market_index_store_missing")
        return None
    index_code = "KOSDAQ" if str(mapping.market).upper() == "KOSDAQ" else "KOSPI"
    index_change = market_index_store.return_pct(index_code, minutes)
    if index_change is None:
        _append_unique(input_missing, f"{index_code.lower()}_return_{minutes}m_missing")
        return None
    return round(stock_change - index_change, 6)


def _time_delay_minutes(value: Optional[str], reference: Optional[str]) -> Optional[float]:
    if not value or not reference:
        return None
    try:
        delta = datetime.fromisoformat(value) - datetime.fromisoformat(reference)
    except ValueError:
        return None
    return round(delta.total_seconds() / 60.0, 4)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _dynamic_pullback_policy(
    profile: Optional[StrategyProfile],
    theme_result: ThemeStrengthResult,
    leadership_result: StockLeadershipResult,
    snapshot: IndicatorSnapshot,
    market_position: str,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> dict:
    active_settings = settings or legacy_strategy_runtime_settings()
    is_kospi = profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}
    base_range = _valid_pullback_range(profile, active_settings)
    role = leadership_result.leadership_role
    leader_role = role in {"leader", "second_leader", "signal_leader", "signal_second_leader"}
    strong_theme = theme_result.grade in {"A", "A_SIGNAL"} and leader_role
    weak_context = theme_result.grade == "B" or not leader_role or market_position == "BELOW_MID"
    mode = "base"
    confirmation_mode = "standard"
    if strong_theme:
        valid_range = (
            active_settings.range_pair("pullback_thresholds.kospi_strong_leader_shallow_range", (-2.5, -0.5))
            if is_kospi
            else active_settings.range_pair("pullback_thresholds.kosdaq_strong_leader_shallow_range", (-4.0, -1.2))
        )
        mode = "strong_leader_shallow"
    elif weak_context:
        valid_range = (
            active_settings.range_pair("pullback_thresholds.kospi_weak_or_late_deep_range", (-4.0, -1.5))
            if is_kospi
            else active_settings.range_pair("pullback_thresholds.kosdaq_weak_or_late_deep_range", (-6.5, -3.0))
        )
        mode = "weak_or_late_deep"
        confirmation_mode = "strong"
    else:
        valid_range = base_range

    volatility = _float_or_none(snapshot.metadata.get("volatility_5m_pct"))
    volatility_adjustment = "none"
    low, high = valid_range
    if volatility is not None and volatility > active_settings.number("pullback_thresholds.high_volatility_pct", 2.5):
        low += active_settings.number("pullback_thresholds.high_volatility_low_adjust", -1.0)
        high += active_settings.number("pullback_thresholds.high_volatility_high_adjust", -0.5)
        volatility_adjustment = "deeper_for_high_volatility"
    elif volatility is not None and volatility < active_settings.number("pullback_thresholds.low_volatility_pct", 1.0):
        low += active_settings.number("pullback_thresholds.low_volatility_low_adjust", 0.5)
        high += active_settings.number("pullback_thresholds.low_volatility_high_adjust", 0.3)
        volatility_adjustment = "shallower_for_low_volatility"

    valid_range = (round(low, 4), round(high, 4))
    return {
        "mode": mode,
        "profile": profile.value if profile else "",
        "theme_grade": theme_result.grade,
        "leadership_role": role,
        "market_position": market_position,
        "base_range": base_range,
        "valid_range": valid_range,
        "confirmation_mode": confirmation_mode,
        "volatility_5m_pct": volatility,
        "volatility_adjustment": volatility_adjustment,
    }


def _confirmation_ok(snapshot: IndicatorSnapshot, details: dict, mode: str) -> bool:
    if mode == "strong":
        return bool(
            snapshot.failed_low_break_rebound
            or (
                snapshot.volume_reaccel
                and details.get("support_reclaimed")
                and details.get("support_touched")
            )
        )
    return bool(snapshot.volume_reaccel or snapshot.failed_low_break_rebound)


def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_pct(price: int, support: float) -> float:
    if support <= 0:
        return 999.0
    return ((price - support) / support) * 100.0


def _index_code_for(candidate: Candidate, theme_context: Optional[ThemeContext]) -> str:
    profile = candidate.strategy_profile or (theme_context.strategy_profile if theme_context else None)
    market = candidate.market or (theme_context.market if theme_context else "")
    if profile == StrategyProfile.KOSDAQ_THEME_PROFILE or str(market).upper() == "KOSDAQ":
        return "KOSDAQ"
    return "KOSPI"


def _valid_pullback_range(
    profile: Optional[StrategyProfile],
    settings: Optional[StrategyRuntimeSettings] = None,
) -> tuple[float, float]:
    active_settings = settings or legacy_strategy_runtime_settings()
    if profile in {StrategyProfile.KOSPI_LEADER_PROFILE, StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE}:
        return active_settings.range_pair("pullback_thresholds.kospi_range", KOSPI_PULLBACK_RANGE)
    return active_settings.range_pair("pullback_thresholds.kosdaq_range", KOSDAQ_PULLBACK_RANGE)


def _decision(
    gate_name: str,
    passed: bool,
    score: float,
    block_type: BlockType,
    reason_codes: list[str],
    details: dict,
    can_recover: bool = False,
    recheck_after_sec: int = 0,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> GateDecision:
    details = attach_settings_details(dict(details), settings)
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
    settings: Optional[StrategyRuntimeSettings] = None,
) -> str:
    active_settings = settings or legacy_strategy_runtime_settings()
    if valid_tick_ratio < active_settings.number("theme_thresholds.valid_tick_ratio_c_min", 0.5):
        return "C"
    if valid_tick_ratio < active_settings.number("theme_thresholds.valid_tick_ratio_full_min", 0.67):
        return "B" if score >= active_settings.number("theme_thresholds.grade_b_score", B_THRESHOLD) else "C"
    if signal_pair:
        if score >= active_settings.number("theme_thresholds.grade_a_score", A_THRESHOLD):
            return "A_SIGNAL"
        return "B+_SIGNAL" if score >= active_settings.number("theme_thresholds.grade_b_score", B_THRESHOLD) else "C"
    if (
        active_count >= active_settings.integer("theme_thresholds.min_active_count_for_a", 3)
        and valid_turnover_count >= active_settings.integer("theme_thresholds.min_valid_turnover_count_for_a", 2)
        and score >= active_settings.number("theme_thresholds.grade_a_score", A_THRESHOLD)
    ):
        return "A"
    if score >= active_settings.number("theme_thresholds.grade_b_plus_score", B_PLUS_THRESHOLD):
        return "B+"
    if score >= active_settings.number("theme_thresholds.grade_b_score", B_THRESHOLD):
        return "B"
    return "C"


def _leadership_role(
    rank: int,
    is_signal_stock: bool,
    settings: Optional[StrategyRuntimeSettings] = None,
) -> str:
    active_settings = settings or legacy_strategy_runtime_settings()
    if rank == active_settings.integer("leadership_thresholds.leader_rank", 1):
        return "signal_leader" if is_signal_stock else "leader"
    if rank == active_settings.integer("leadership_thresholds.second_leader_rank", 2):
        return "signal_second_leader" if is_signal_stock else "second_leader"
    if rank >= active_settings.integer("leadership_thresholds.follower_min_rank", 3):
        return "follower"
    return "unranked"


def _dynamic_leadership_role(rank: int) -> str:
    if rank == 1:
        return "leader"
    if 2 <= rank <= 3:
        return "co_leader"
    if 4 <= rank <= 5:
        return "follower"
    if rank >= 6:
        return "late_laggard"
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
