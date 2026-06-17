from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from trading.theme_engine.leadership import (
    ConditionBoost,
    MarketPhase,
    StockLeadershipRole,
    ThemeLeadershipStatus,
)
from trading.theme_engine.models import StockSnapshot, ThemeMembership
from trading.theme_engine.normalizer import normalize_stock_code


class OpeningReturnGrade(str, Enum):
    WEAK = "WEAK"
    ALIVE = "ALIVE"
    STRONG = "STRONG"
    LEADER = "LEADER"
    BURST = "BURST"


@dataclass(frozen=True)
class OpeningBurstConfig:
    seed_call_times: tuple[str, ...] = ("09:03", "09:06", "09:09", "09:12", "09:15")
    top_n_per_call: int = 100
    max_union_size: int = 300
    top_theme_count: int = 5
    max_stocks_per_theme: int = 3
    max_total_watchset: int = 25
    small_theme_member_count: int = 4
    min_strong_for_theme: int = 3
    min_strong_for_small_theme: int = 2
    leading_score_threshold: float = 70.0
    spreading_score_threshold: float = 50.0
    leader_only_penalty: float = 15.0
    data_quality_ratio_min: float = 0.35
    max_condition_boost_score: float = 6.0
    default_condition_boost_score: float = 4.0
    alive_threshold_pct: float = -1.0
    strong_threshold_pct: float = 3.0
    leader_threshold_pct: float = 5.0
    burst_threshold_pct: float = 7.0


@dataclass(frozen=True)
class OpeningTurnoverSeed:
    stock_code: str
    stock_name: str = ""
    turnover_krw: float = 0.0
    change_rate_pct: float = 0.0
    seed_rank: int = 0
    first_seen_at: str = ""
    last_seen_at: str = ""
    seed_times: tuple[str, ...] = ()
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BurstStockSnapshot:
    calculated_at: str
    stock_code: str
    stock_name: str = ""
    seed_rank: int = 0
    seed_times: tuple[str, ...] = ()
    return_grade: OpeningReturnGrade | str = OpeningReturnGrade.WEAK
    stock_burst_score: float = 0.0
    leader_score: float = 0.0
    role: StockLeadershipRole | str = StockLeadershipRole.WEAK_MEMBER
    rank_in_theme: int = 0
    turnover_rank_score: float = 0.0
    turnover_rank_in_theme: float = 0.0
    return_rank_in_theme: float = 0.0
    turnover_speed_score: float = 0.0
    return_grade_score: float = 0.0
    execution_strength_score: float = 0.0
    momentum_score: float = 0.0
    spread_stability_score: float = 0.0
    risk_penalty: float = 0.0
    condition_boost: float = 0.0
    condition_include_count: int = 0
    discovery_sources: tuple[str, ...] = ()
    current_price: float = 0.0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    turnover_speed_krw_per_min: float = 0.0
    turnover_vs_20d_ratio: float = 0.0
    prior_same_time_volume_ratio: float | None = None
    cum_volume: int = 0
    execution_strength: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_ticks: int = 0
    pullback_from_high_pct: float = 100.0
    upper_limit_gap_pct: float = 100.0
    vi_active: bool = False
    timing_status: str = "OBSERVE"
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False


@dataclass(frozen=True)
class ThemeCohesionSnapshot:
    calculated_at: str
    theme_id: str
    theme_name: str = ""
    status: ThemeLeadershipStatus | str = ThemeLeadershipStatus.DATA_WAIT
    theme_score: float = 0.0
    raw_total_members: int = 0
    theme_active_count: int = 0
    alive_count: int = 0
    strong_count: int = 0
    leader_count: int = 0
    alive_ratio: float = 0.0
    strong_ratio: float = 0.0
    leader_ratio: float = 0.0
    theme_turnover_krw: float = 0.0
    theme_turnover_rank_score: float = 0.0
    strong_ratio_score: float = 0.0
    leader_count_score: float = 0.0
    weighted_return_score: float = 0.0
    momentum_score: float = 0.0
    persistence_score: float = 0.0
    leader_only_penalty: float = 0.0
    data_quality_penalty: float = 0.0
    weighted_return_pct: float = 0.0
    leader_concentration: float = 0.0
    data_quality_ratio: float = 0.0
    cohesion_passed: bool = False
    leader_symbol: str = ""
    leader_name: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    stocks: tuple[BurstStockSnapshot, ...] = ()
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = "OBSERVE"


@dataclass(frozen=True)
class OpeningThemeBurstRank:
    rank: int
    theme_id: str
    theme_name: str = ""
    theme_score: float = 0.0
    status: ThemeLeadershipStatus | str = ThemeLeadershipStatus.DATA_WAIT
    leader_symbol: str = ""
    leader_name: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    snapshot: ThemeCohesionSnapshot | None = None
    output_mode: str = "OBSERVE"


@dataclass(frozen=True)
class OpeningThemeBurstResult:
    calculated_at: str
    seed_symbols: tuple[str, ...] = ()
    ranked_themes: tuple[OpeningThemeBurstRank, ...] = ()
    selected: tuple[BurstStockSnapshot, ...] = ()
    selected_symbols: tuple[str, ...] = ()
    excluded: tuple[BurstStockSnapshot, ...] = ()
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False
    reason_codes: tuple[str, ...] = ()


class OpeningTurnoverSeedCollector:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()

    def rolling_schedule(self) -> tuple[str, ...]:
        return self.config.seed_call_times

    def collect(self, seed_batches: Iterable[Iterable[OpeningTurnoverSeed | Mapping[str, Any]]]) -> tuple[OpeningTurnoverSeed, ...]:
        by_symbol: dict[str, OpeningTurnoverSeed] = {}
        for batch_index, batch in enumerate(seed_batches):
            scheduled_at = self.config.seed_call_times[min(batch_index, len(self.config.seed_call_times) - 1)]
            rows = [_seed_from_row(row, scheduled_at) for row in batch]
            rows = sorted(rows, key=lambda item: item.seed_rank or 9999)[: self.config.top_n_per_call]
            for row in rows:
                if not row.stock_code or _is_excluded_seed(row):
                    continue
                previous = by_symbol.get(row.stock_code)
                by_symbol[row.stock_code] = _merge_seed(previous, row) if previous else row
        ordered = sorted(by_symbol.values(), key=lambda item: (item.turnover_krw, -item.seed_rank), reverse=True)
        return tuple(ordered[: self.config.max_union_size])


class BurstStockScorer:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()

    def score(
        self,
        seeds: Iterable[OpeningTurnoverSeed],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        condition_boosts: Mapping[str, Any] | Iterable[ConditionBoost] | None = None,
        calculated_at: str = "",
    ) -> dict[str, BurstStockSnapshot]:
        seed_list = list(seeds)
        snapshot_by_symbol = _snapshot_map(snapshots)
        boost_by_symbol = _condition_boost_map(condition_boosts, self.config)
        turnover_rank_scores = _rank_scores({seed.stock_code: seed.turnover_krw for seed in seed_list}, positive_only=True)
        result: dict[str, BurstStockSnapshot] = {}
        for seed in seed_list:
            snapshot = snapshot_by_symbol.get(seed.stock_code)
            if snapshot is None:
                continue
            result[seed.stock_code] = self._score_seed(
                seed,
                snapshot,
                turnover_rank_scores.get(seed.stock_code, 0.0),
                boost_by_symbol.get(seed.stock_code, (0.0, (), 0)),
                calculated_at,
            )
        return result

    def _score_seed(
        self,
        seed: OpeningTurnoverSeed,
        snapshot: StockSnapshot,
        turnover_rank_score: float,
        boost: tuple[float, tuple[str, ...], int],
        calculated_at: str,
    ) -> BurstStockSnapshot:
        metadata = dict(snapshot.metadata or {})
        change_rate = float(snapshot.change_rate if snapshot.change_rate is not None else seed.change_rate_pct or 0.0)
        grade = _return_grade(change_rate, self.config)
        turnover = max(float(snapshot.turnover or seed.turnover_krw or 0.0), float(seed.turnover_krw or 0.0))
        turnover_speed = _turnover_speed(snapshot, turnover)
        avg20 = _metadata_float(metadata, "avg_turnover_20d_krw", default=0.0)
        turnover_vs_20d = turnover / avg20 if avg20 > 0 else 0.0
        turnover_speed_score = max(_log_score(turnover_speed, 50_000_000.0, 1_500_000_000.0), _scale(turnover_vs_20d, 0.02, 0.20))
        prior_ratio = _optional_float(metadata.get("prior_same_time_volume_ratio"))
        if prior_ratio is not None and prior_ratio >= 2.0:
            turnover_speed_score = max(turnover_speed_score, _scale(prior_ratio, 1.0, 5.0))
        return_grade_score = _return_grade_score(grade)
        execution_score = _scale(float(snapshot.execution_strength or 0.0), 70.0, 180.0)
        momentum_score = _scale((float(snapshot.momentum_1m or 0.0) + float(snapshot.momentum_3m or 0.0)) / 2.0, -1.0, 3.0)
        spread_score = _spread_stability_score(snapshot)
        risk_penalty, risk_reasons = _risk_penalty(snapshot, change_rate)
        condition_boost, discovery_sources, condition_count = boost
        burst_score = (
            0.30 * turnover_rank_score
            + 0.25 * turnover_speed_score
            + 0.20 * return_grade_score
            + 0.10 * execution_score
            + 0.10 * momentum_score
            + 0.05 * spread_score
            + condition_boost
            - risk_penalty
        )
        timing_status = "BLOCKED" if {"VI_ACTIVE", "UPPER_LIMIT_NEAR"} & set(risk_reasons) else ("WAIT" if risk_penalty > 0 else "OBSERVE")
        return BurstStockSnapshot(
            calculated_at=calculated_at,
            stock_code=seed.stock_code,
            stock_name=snapshot.stock_name or seed.stock_name,
            seed_rank=seed.seed_rank,
            seed_times=seed.seed_times,
            return_grade=grade,
            stock_burst_score=round(_clamp(burst_score, 0.0, 100.0), 4),
            turnover_rank_score=round(turnover_rank_score, 4),
            turnover_speed_score=round(turnover_speed_score, 4),
            return_grade_score=round(return_grade_score, 4),
            execution_strength_score=round(execution_score, 4),
            momentum_score=round(momentum_score, 4),
            spread_stability_score=round(spread_score, 4),
            risk_penalty=round(risk_penalty, 4),
            condition_boost=round(condition_boost, 4),
            condition_include_count=condition_count,
            discovery_sources=discovery_sources,
            current_price=float(snapshot.current_price or 0.0),
            change_rate_pct=change_rate,
            turnover_krw=turnover,
            turnover_speed_krw_per_min=round(turnover_speed, 4),
            turnover_vs_20d_ratio=round(turnover_vs_20d, 4),
            prior_same_time_volume_ratio=prior_ratio,
            cum_volume=int(snapshot.volume or 0),
            execution_strength=float(snapshot.execution_strength or 0.0),
            momentum_1m=float(snapshot.momentum_1m or 0.0),
            momentum_3m=float(snapshot.momentum_3m or 0.0),
            momentum_5m=float(snapshot.momentum_5m or 0.0),
            best_bid=float(snapshot.best_bid or 0.0),
            best_ask=float(snapshot.best_ask or 0.0),
            spread_ticks=_spread_ticks(snapshot),
            pullback_from_high_pct=_pullback_from_high_pct(snapshot),
            upper_limit_gap_pct=_metadata_float(metadata, "upper_limit_gap_pct", default=100.0),
            vi_active=_metadata_bool(metadata, "vi_active"),
            timing_status=timing_status,
            reason_codes=tuple(_dedupe([grade.value, *risk_reasons, "CONDITION_INCLUDE_BOOSTER_ONLY" if condition_count else ""])),
        )


class ThemeCohesionScorer:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()

    def score(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        burst_by_symbol: Mapping[str, BurstStockSnapshot],
        *,
        calculated_at: str = "",
    ) -> list[ThemeCohesionSnapshot]:
        result: list[ThemeCohesionSnapshot] = []
        for theme_id, theme_name, memberships in theme_inputs:
            active_members = [member for member in memberships if member.active]
            stocks = [
                burst_by_symbol[code]
                for member in active_members
                for code in [normalize_stock_code(member.stock_code)]
                if code in burst_by_symbol
            ]
            stocks = _classify_roles(stocks)
            result.append(self._score_theme(theme_id, theme_name, active_members, stocks, calculated_at))
        return result

    def _score_theme(
        self,
        theme_id: str,
        theme_name: str,
        active_members: list[ThemeMembership],
        stocks: list[BurstStockSnapshot],
        calculated_at: str,
    ) -> ThemeCohesionSnapshot:
        raw_total = len(active_members)
        active_count = len(stocks)
        cohesion_stocks = [stock for stock in stocks if stock.role != StockLeadershipRole.OVERHEATED]
        alive_count = sum(1 for stock in cohesion_stocks if OpeningReturnGrade(stock.return_grade) != OpeningReturnGrade.WEAK)
        strong_count = sum(1 for stock in cohesion_stocks if OpeningReturnGrade(stock.return_grade) in _strong_grades())
        leader_count = sum(1 for stock in cohesion_stocks if OpeningReturnGrade(stock.return_grade) in {OpeningReturnGrade.LEADER, OpeningReturnGrade.BURST})
        denominator = raw_total or 1
        alive_ratio = alive_count / denominator
        strong_ratio = strong_count / denominator
        leader_ratio = leader_count / denominator
        turnover = sum(max(0.0, stock.turnover_krw) for stock in stocks)
        weighted_return = _weighted_return(stocks)
        leader_concentration = max((stock.turnover_krw for stock in stocks), default=0.0) / turnover if turnover > 0 else 0.0
        data_quality_ratio = active_count / denominator
        cohesion_passed = _cohesion_passed(raw_total, strong_count, self.config)
        leader = next((stock for stock in stocks if stock.role == StockLeadershipRole.LEADER), None)
        co_leaders = tuple(stock.stock_code for stock in stocks if stock.role == StockLeadershipRole.CO_LEADER)
        flags: list[str] = []
        reasons: list[str] = []
        if active_count == 0:
            flags.append("NO_OPENING_SEED_MATCH")
            reasons.append("DATA_WAIT_NO_OPENING_SEED_MATCH")
        if data_quality_ratio < self.config.data_quality_ratio_min:
            flags.append("LOW_THEME_ACTIVE_RATIO")
        if not cohesion_passed and leader_count == 1:
            reasons.append("SINGLE_BURST_NOT_THEME")
        elif not cohesion_passed:
            reasons.append("INSUFFICIENT_THEME_COHESION")
        return ThemeCohesionSnapshot(
            calculated_at=calculated_at,
            theme_id=theme_id,
            theme_name=theme_name,
            raw_total_members=raw_total,
            theme_active_count=active_count,
            alive_count=alive_count,
            strong_count=strong_count,
            leader_count=leader_count,
            alive_ratio=round(alive_ratio, 4),
            strong_ratio=round(strong_ratio, 4),
            leader_ratio=round(leader_ratio, 4),
            theme_turnover_krw=round(turnover, 4),
            weighted_return_pct=round(weighted_return, 4),
            leader_concentration=round(leader_concentration, 4),
            data_quality_ratio=round(data_quality_ratio, 4),
            cohesion_passed=cohesion_passed,
            leader_symbol=leader.stock_code if leader else "",
            leader_name=leader.stock_name if leader else "",
            co_leader_symbols=co_leaders,
            stocks=tuple(stocks),
            data_quality_flags=tuple(flags),
            reason_codes=tuple(_dedupe(reasons)),
        )


class OpeningThemeBurstRanker:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()

    def rank(self, themes: Iterable[ThemeCohesionSnapshot], top_n: int | None = None) -> list[OpeningThemeBurstRank]:
        theme_list = list(themes)
        turnover_rank_scores = _rank_scores({theme.theme_id: theme.theme_turnover_krw for theme in theme_list}, positive_only=True)
        scored = [self._score_theme(theme, turnover_rank_scores.get(theme.theme_id, 0.0)) for theme in theme_list]
        ranked_snapshots = sorted(scored, key=lambda item: (_theme_status_priority(item.status), item.theme_score), reverse=True)
        if top_n is not None:
            ranked_snapshots = ranked_snapshots[:top_n]
        return [
            OpeningThemeBurstRank(
                rank=index,
                theme_id=theme.theme_id,
                theme_name=theme.theme_name,
                theme_score=theme.theme_score,
                status=theme.status,
                leader_symbol=theme.leader_symbol,
                leader_name=theme.leader_name,
                co_leader_symbols=theme.co_leader_symbols,
                snapshot=theme,
            )
            for index, theme in enumerate(ranked_snapshots, start=1)
        ]

    def _score_theme(self, theme: ThemeCohesionSnapshot, turnover_rank_score: float) -> ThemeCohesionSnapshot:
        strong_ratio_score = _clamp(theme.strong_ratio * 100.0, 0.0, 100.0)
        leader_count_score = _leader_count_score(theme.leader_count)
        weighted_return_score = _scale(theme.weighted_return_pct, -2.0, 8.0)
        momentum_score = _average([stock.momentum_score for stock in theme.stocks], default=0.0)
        persistence_score = _persistence_score(theme.stocks, self.config)
        leader_only_penalty = _leader_only_penalty(theme, self.config)
        data_quality_penalty = _data_quality_penalty(theme.data_quality_ratio, theme.theme_active_count)
        base_score = (
            0.25 * turnover_rank_score
            + 0.20 * strong_ratio_score
            + 0.20 * leader_count_score
            + 0.15 * weighted_return_score
            + 0.10 * momentum_score
            + 0.10 * persistence_score
        )
        theme_score = _clamp(base_score - leader_only_penalty - data_quality_penalty, 0.0, 100.0)
        status = _opening_theme_status(theme, theme_score, self.config)
        return replace(
            theme,
            status=status,
            theme_score=round(theme_score, 4),
            theme_turnover_rank_score=round(turnover_rank_score, 4),
            strong_ratio_score=round(strong_ratio_score, 4),
            leader_count_score=round(leader_count_score, 4),
            weighted_return_score=round(weighted_return_score, 4),
            momentum_score=round(momentum_score, 4),
            persistence_score=round(persistence_score, 4),
            leader_only_penalty=round(leader_only_penalty, 4),
            data_quality_penalty=round(data_quality_penalty, 4),
        )


class OpeningBurstWatchsetSelector:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()

    def select(
        self,
        ranks: Iterable[OpeningThemeBurstRank],
        *,
        seed_symbols: Iterable[str] = (),
        market_phase: MarketPhase | str = MarketPhase.SELECTIVE,
        calculated_at: str = "",
    ) -> OpeningThemeBurstResult:
        phase = MarketPhase(market_phase)
        ranked = list(ranks)
        selected_by_symbol: dict[str, BurstStockSnapshot] = {}
        excluded: list[BurstStockSnapshot] = []
        reasons: list[str] = ["OPENING_THEME_BURST_OBSERVE_ONLY"]
        for rank in ranked[: self.config.top_theme_count]:
            theme = rank.snapshot
            if theme is None:
                continue
            if theme.status not in {
                ThemeLeadershipStatus.LEADING_THEME,
                ThemeLeadershipStatus.SPREADING_THEME,
                ThemeLeadershipStatus.LEADER_ONLY_THEME,
            }:
                excluded.extend(theme.stocks)
                continue
            per_theme_count = 0
            for stock in sorted(theme.stocks, key=lambda item: item.leader_score, reverse=True):
                allowed, reason = _watchset_allowed(stock, theme.status, phase)
                if not allowed:
                    excluded.append(stock)
                    reasons.append(reason)
                    continue
                if per_theme_count >= self.config.max_stocks_per_theme:
                    excluded.append(stock)
                    reasons.append("WATCHSET_PER_THEME_LIMIT_REACHED")
                    continue
                if len(selected_by_symbol) >= self.config.max_total_watchset:
                    excluded.append(stock)
                    reasons.append("WATCHSET_TOTAL_LIMIT_REACHED")
                    continue
                previous = selected_by_symbol.get(stock.stock_code)
                if previous is None or stock.leader_score > previous.leader_score:
                    selected_by_symbol[stock.stock_code] = stock
                    per_theme_count += 1
        selected = tuple(selected_by_symbol.values())
        return OpeningThemeBurstResult(
            calculated_at=calculated_at,
            seed_symbols=tuple(_dedupe(seed_symbols)),
            ranked_themes=tuple(ranked),
            selected=selected,
            selected_symbols=tuple(stock.stock_code for stock in selected),
            excluded=tuple(excluded),
            reason_codes=tuple(_dedupe(reasons)),
        )


class OpeningThemeBurstEngine:
    def __init__(self, config: OpeningBurstConfig | None = None) -> None:
        self.config = config or OpeningBurstConfig()
        self.seed_collector = OpeningTurnoverSeedCollector(self.config)
        self.stock_scorer = BurstStockScorer(self.config)
        self.cohesion_scorer = ThemeCohesionScorer(self.config)
        self.ranker = OpeningThemeBurstRanker(self.config)
        self.watchset_selector = OpeningBurstWatchsetSelector(self.config)

    def run(
        self,
        *,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        seed_batches: Iterable[Iterable[OpeningTurnoverSeed | Mapping[str, Any]]],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        condition_boosts: Mapping[str, Any] | Iterable[ConditionBoost] | None = None,
        market_phase: MarketPhase | str = MarketPhase.SELECTIVE,
        calculated_at: str = "",
    ) -> OpeningThemeBurstResult:
        seeds = self.seed_collector.collect(seed_batches)
        burst_by_symbol = self.stock_scorer.score(
            seeds,
            snapshots,
            condition_boosts=condition_boosts,
            calculated_at=calculated_at,
        )
        cohesions = self.cohesion_scorer.score(theme_inputs, burst_by_symbol, calculated_at=calculated_at)
        ranks = self.ranker.rank(cohesions, top_n=self.config.top_theme_count)
        return self.watchset_selector.select(
            ranks,
            seed_symbols=[seed.stock_code for seed in seeds],
            market_phase=market_phase,
            calculated_at=calculated_at,
        )


def _seed_from_row(row: OpeningTurnoverSeed | Mapping[str, Any], scheduled_at: str) -> OpeningTurnoverSeed:
    if isinstance(row, OpeningTurnoverSeed):
        return row if row.seed_times else replace(row, seed_times=(scheduled_at,), first_seen_at=scheduled_at, last_seen_at=scheduled_at)
    raw = dict(row)
    code = normalize_stock_code(str(raw.get("stock_code") or raw.get("code") or raw.get("종목코드") or ""))
    rank = _int(raw.get("rank", raw.get("seed_rank", raw.get("순위", 0))))
    turnover = abs(_float(raw.get("turnover_krw", raw.get("turnover", raw.get("trade_value", raw.get("거래대금", 0.0))))))
    change_rate = _float(raw.get("change_rate_pct", raw.get("change_rate", raw.get("등락률", raw.get("등락율", 0.0)))))
    collected_at = str(raw.get("collected_at") or raw.get("seed_time") or scheduled_at)
    return OpeningTurnoverSeed(
        stock_code=code,
        stock_name=str(raw.get("stock_name") or raw.get("name") or raw.get("종목명") or ""),
        turnover_krw=turnover,
        change_rate_pct=change_rate,
        seed_rank=rank,
        first_seen_at=collected_at,
        last_seen_at=collected_at,
        seed_times=(collected_at,),
        raw=raw,
    )


def _merge_seed(previous: OpeningTurnoverSeed | None, current: OpeningTurnoverSeed) -> OpeningTurnoverSeed:
    if previous is None:
        return current
    best = current if current.turnover_krw >= previous.turnover_krw else previous
    return replace(
        best,
        seed_rank=min(_positive_rank(previous.seed_rank), _positive_rank(current.seed_rank)),
        first_seen_at=previous.first_seen_at or current.first_seen_at,
        last_seen_at=current.last_seen_at or previous.last_seen_at,
        seed_times=tuple(_dedupe([*previous.seed_times, *current.seed_times])),
    )


def _is_excluded_seed(seed: OpeningTurnoverSeed) -> bool:
    raw = dict(seed.raw or {})
    text = " ".join([str(seed.stock_name or ""), str(raw.get("instrument_type") or raw.get("security_type") or "")]).upper()
    if any(_metadata_bool(raw, key) for key in ("is_etf", "is_etn", "is_spac", "is_preferred", "is_suspended", "is_under_administration")):
        return True
    return any(token in text for token in ("ETF", "ETN", "SPAC", "PREFERRED", "SUSPENDED", "ADMINISTRATION"))


def _classify_roles(stocks: list[BurstStockSnapshot]) -> list[BurstStockSnapshot]:
    if not stocks:
        return []
    turnover_scores = _rank_scores({stock.stock_code: stock.turnover_krw for stock in stocks}, positive_only=True)
    return_scores = _rank_scores({stock.stock_code: stock.change_rate_pct for stock in stocks})
    enriched = []
    for stock in stocks:
        leader_score = (
            0.30 * turnover_scores.get(stock.stock_code, 0.0)
            + 0.20 * return_scores.get(stock.stock_code, 0.0)
            + 0.15 * stock.turnover_speed_score
            + 0.15 * stock.execution_strength_score
            + 0.10 * _scale((stock.momentum_1m + stock.momentum_3m + stock.momentum_5m) / 3.0, -1.0, 3.0)
            + 0.10 * stock.spread_stability_score
            + stock.condition_boost
            - stock.risk_penalty
        )
        enriched.append(
            replace(
                stock,
                turnover_rank_in_theme=round(turnover_scores.get(stock.stock_code, 0.0), 4),
                return_rank_in_theme=round(return_scores.get(stock.stock_code, 0.0), 4),
                leader_score=round(_clamp(leader_score, 0.0, 100.0), 4),
            )
        )
    ordered = sorted(enriched, key=lambda item: item.leader_score, reverse=True)
    top_score = ordered[0].leader_score
    top_return = max((stock.change_rate_pct for stock in ordered), default=0.0)
    leader_return = ordered[0].change_rate_pct
    result = []
    for rank, stock in enumerate(ordered, start=1):
        role = StockLeadershipRole.WEAK_MEMBER
        reasons = list(stock.reason_codes)
        if stock.risk_penalty >= 18.0:
            role = StockLeadershipRole.OVERHEATED
            reasons.append("OPENING_RISK_EXCLUDED")
        elif rank == 1 and stock.leader_score >= 50.0:
            role = StockLeadershipRole.LEADER
            reasons.append("OPENING_LEADER_SCORE_TOP")
        elif rank <= 3 and stock.leader_score >= 48.0 and (
            top_score - stock.leader_score <= 14.0 or leader_return - stock.change_rate_pct <= 1.5
        ):
            role = StockLeadershipRole.CO_LEADER
            reasons.append("OPENING_CO_LEADER")
        elif top_return >= 5.0 and stock.change_rate_pct < 3.0 and top_return - stock.change_rate_pct >= 4.0:
            role = StockLeadershipRole.LATE_LAGGARD
            reasons.append("OPENING_LATE_LAGGARD")
        elif stock.leader_score >= 40.0 and OpeningReturnGrade(stock.return_grade) in _strong_grades():
            role = StockLeadershipRole.FOLLOWER
            reasons.append("OPENING_FOLLOWER")
        else:
            reasons.append("OPENING_WEAK_MEMBER")
        result.append(replace(stock, role=role, rank_in_theme=rank, reason_codes=tuple(_dedupe(reasons))))
    return sorted(result, key=lambda item: item.rank_in_theme)


def _opening_theme_status(theme: ThemeCohesionSnapshot, theme_score: float, config: OpeningBurstConfig) -> ThemeLeadershipStatus:
    if theme.theme_active_count == 0:
        return ThemeLeadershipStatus.DATA_WAIT
    if not theme.cohesion_passed:
        if theme.leader_count >= 2:
            return ThemeLeadershipStatus.LEADER_ONLY_THEME
        return ThemeLeadershipStatus.WATCH_THEME
    if theme_score >= config.leading_score_threshold and theme.leader_count >= 1:
        return ThemeLeadershipStatus.LEADING_THEME
    if theme_score >= config.spreading_score_threshold:
        return ThemeLeadershipStatus.SPREADING_THEME
    return ThemeLeadershipStatus.WATCH_THEME


def _watchset_allowed(stock: BurstStockSnapshot, theme_status: ThemeLeadershipStatus | str, market_phase: MarketPhase) -> tuple[bool, str]:
    role = StockLeadershipRole(stock.role)
    if role in {StockLeadershipRole.LATE_LAGGARD, StockLeadershipRole.WEAK_MEMBER, StockLeadershipRole.OVERHEATED}:
        return False, f"{role.value}_EXCLUDED_FROM_OPENING_WATCHSET"
    if theme_status == ThemeLeadershipStatus.LEADER_ONLY_THEME:
        return (role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER}, "LEADER_ONLY_THEME_NON_LEADER_EXCLUDED")
    if role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER}:
        return True, "LEADER_OR_CO_LEADER_ALLOWED"
    if role == StockLeadershipRole.FOLLOWER and market_phase == MarketPhase.EXPANSION:
        return True, "FOLLOWER_ALLOWED_IN_EXPANSION"
    return False, "FOLLOWER_REQUIRES_EXPANSION"


def _cohesion_passed(raw_total: int, strong_count: int, config: OpeningBurstConfig) -> bool:
    if raw_total <= config.small_theme_member_count:
        return strong_count >= config.min_strong_for_small_theme
    return strong_count >= config.min_strong_for_theme


def _leader_only_penalty(theme: ThemeCohesionSnapshot, config: OpeningBurstConfig) -> float:
    if theme.cohesion_passed:
        return 0.0
    if theme.leader_count >= 1:
        return config.leader_only_penalty
    return config.leader_only_penalty * 0.5


def _data_quality_penalty(data_quality_ratio: float, active_count: int) -> float:
    penalty = max(0.0, (0.60 - data_quality_ratio) * 35.0)
    if active_count <= 1:
        penalty += 10.0
    return _clamp(penalty, 0.0, 35.0)


def _risk_penalty(snapshot: StockSnapshot, change_rate: float) -> tuple[float, tuple[str, ...]]:
    metadata = dict(snapshot.metadata or {})
    penalty = 0.0
    reasons: list[str] = []
    if _metadata_bool(metadata, "vi_active"):
        penalty += 35.0
        reasons.append("VI_ACTIVE")
    upper_limit_gap = _metadata_float(metadata, "upper_limit_gap_pct", default=100.0)
    if upper_limit_gap <= 3.0:
        penalty += 30.0
        reasons.append("UPPER_LIMIT_NEAR")
    pullback = _pullback_from_high_pct(snapshot)
    if change_rate >= 7.0 and pullback <= 0.2:
        penalty += 18.0
        reasons.append("CHASE_HIGH_RISK")
    vwap = _metadata_float(metadata, "vwap", default=0.0)
    current = float(snapshot.current_price or 0.0)
    if current > 0 and vwap > 0 and ((current - vwap) / vwap) * 100.0 >= 8.0:
        penalty += 15.0
        reasons.append("VWAP_OVEREXTENDED")
    return _clamp(penalty, 0.0, 60.0), tuple(reasons)


def _return_grade(change_rate: float, config: OpeningBurstConfig) -> OpeningReturnGrade:
    if change_rate >= config.burst_threshold_pct:
        return OpeningReturnGrade.BURST
    if change_rate >= config.leader_threshold_pct:
        return OpeningReturnGrade.LEADER
    if change_rate >= config.strong_threshold_pct:
        return OpeningReturnGrade.STRONG
    if change_rate >= config.alive_threshold_pct:
        return OpeningReturnGrade.ALIVE
    return OpeningReturnGrade.WEAK


def _return_grade_score(grade: OpeningReturnGrade) -> float:
    scores = {
        OpeningReturnGrade.WEAK: 0.0,
        OpeningReturnGrade.ALIVE: 35.0,
        OpeningReturnGrade.STRONG: 65.0,
        OpeningReturnGrade.LEADER: 85.0,
        OpeningReturnGrade.BURST: 100.0,
    }
    return scores[grade]


def _strong_grades() -> set[OpeningReturnGrade]:
    return {OpeningReturnGrade.STRONG, OpeningReturnGrade.LEADER, OpeningReturnGrade.BURST}


def _turnover_speed(snapshot: StockSnapshot, turnover: float) -> float:
    metadata = dict(snapshot.metadata or {})
    explicit = _metadata_float(metadata, "opening_turnover_speed_krw_per_min", "turnover_speed_krw_per_min", default=0.0)
    if explicit > 0:
        return explicit
    minutes = max(1.0, _metadata_float(metadata, "minutes_since_open", default=5.0))
    return turnover / minutes


def _persistence_score(stocks: tuple[BurstStockSnapshot, ...], config: OpeningBurstConfig) -> float:
    max_seen = max(1, len(config.seed_call_times))
    values = [min(100.0, (len(stock.seed_times) / max_seen) * 100.0) for stock in stocks]
    return _average(values, default=0.0)


def _leader_count_score(count: int) -> float:
    if count <= 0:
        return 0.0
    if count == 1:
        return 55.0
    if count == 2:
        return 82.0
    return 100.0


def _weighted_return(stocks: Iterable[BurstStockSnapshot]) -> float:
    numerator = 0.0
    denominator = 0.0
    for stock in stocks:
        weight = math.sqrt(max(0.0, stock.turnover_krw) + 1.0)
        numerator += stock.change_rate_pct * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else 0.0


def _spread_stability_score(snapshot: StockSnapshot) -> float:
    bid = float(snapshot.best_bid or 0.0)
    ask = float(snapshot.best_ask or 0.0)
    price = float(snapshot.current_price or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid or price <= 0:
        return 50.0
    spread_pct = ((ask - bid) / price) * 100.0
    return _clamp(100.0 - spread_pct * 250.0, 0.0, 100.0)


def _spread_ticks(snapshot: StockSnapshot) -> int:
    bid = float(snapshot.best_bid or 0.0)
    ask = float(snapshot.best_ask or 0.0)
    tick = max(1.0, _metadata_float(dict(snapshot.metadata or {}), "tick_size", default=1.0))
    if bid <= 0 or ask <= 0 or ask < bid:
        return 0
    return int(round((ask - bid) / tick))


def _pullback_from_high_pct(snapshot: StockSnapshot) -> float:
    metadata = dict(snapshot.metadata or {})
    explicit = _optional_float(metadata.get("pullback_from_high_pct"))
    if explicit is not None:
        return max(0.0, explicit)
    high = float(snapshot.session_high or metadata.get("day_high") or 0.0)
    price = float(snapshot.current_price or 0.0)
    if high <= 0 or price <= 0:
        return 100.0
    return max(0.0, ((high - price) / high) * 100.0)


def _condition_boost_map(
    values: Mapping[str, Any] | Iterable[ConditionBoost] | None,
    config: OpeningBurstConfig,
) -> dict[str, tuple[float, tuple[str, ...], int]]:
    if values is None:
        return {}
    result: dict[str, tuple[float, tuple[str, ...], int]] = {}
    iterable = [_boost_from_mapping_item(code, raw, config) for code, raw in values.items()] if isinstance(values, Mapping) else list(values)
    for boost in iterable:
        code = normalize_stock_code(boost.stock_code)
        if not code:
            continue
        previous = result.get(code, (0.0, (), 0))
        result[code] = (
            _clamp(previous[0] + float(boost.boost_score or 0.0), 0.0, config.max_condition_boost_score),
            tuple(_dedupe([*previous[1], boost.discovery_source or "condition_search", boost.condition_name])),
            previous[2] + 1,
        )
    return result


def _boost_from_mapping_item(code: str, raw: Any, config: OpeningBurstConfig) -> ConditionBoost:
    if isinstance(raw, ConditionBoost):
        return raw
    if isinstance(raw, (int, float)):
        return ConditionBoost(stock_code=code, boost_score=float(raw))
    if isinstance(raw, str):
        return ConditionBoost(stock_code=code, boost_score=config.default_condition_boost_score, condition_name=raw)
    return ConditionBoost(stock_code=code, boost_score=config.default_condition_boost_score)


def _snapshot_map(snapshots: dict[str, StockSnapshot] | list[StockSnapshot]) -> dict[str, StockSnapshot]:
    result: dict[str, StockSnapshot] = {}
    items = snapshots.items() if isinstance(snapshots, dict) else [(snapshot.stock_code, snapshot) for snapshot in snapshots]
    for key, snapshot in items:
        normalized_key = normalize_stock_code(str(key))
        normalized_snapshot_code = normalize_stock_code(snapshot.stock_code)
        if normalized_key:
            result[normalized_key] = snapshot
        if normalized_snapshot_code:
            result[normalized_snapshot_code] = snapshot
    return result


def _rank_scores(values: Mapping[str, float], *, positive_only: bool = False) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=True)
    if len(ordered) == 1:
        key, value = ordered[0]
        return {key: 100.0 if (value > 0 or not positive_only) else 0.0}
    denominator = len(ordered) - 1
    result: dict[str, float] = {}
    for index, (key, value) in enumerate(ordered):
        if positive_only and value <= 0:
            result[key] = 0.0
            continue
        result[key] = ((denominator - index) / denominator) * 100.0
    return result


def _theme_status_priority(status: ThemeLeadershipStatus | str) -> int:
    value = status.value if hasattr(status, "value") else str(status or "")
    priorities = {
        ThemeLeadershipStatus.LEADING_THEME.value: 5,
        ThemeLeadershipStatus.SPREADING_THEME.value: 4,
        ThemeLeadershipStatus.LEADER_ONLY_THEME.value: 3,
        ThemeLeadershipStatus.WATCH_THEME.value: 2,
        ThemeLeadershipStatus.DATA_WAIT.value: 1,
        ThemeLeadershipStatus.WEAK_THEME.value: 0,
    }
    return priorities.get(value, 0)


def _metadata_float(metadata: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        try:
            value = float(metadata.get(key))
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return float(default or 0.0)


def _metadata_bool(metadata: Mapping[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _optional_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    return _optional_float(value) or 0.0


def _int(value: Any) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _positive_rank(value: int) -> int:
    return int(value) if int(value or 0) > 0 else 9999


def _scale(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp(((value - low) / (high - low)) * 100.0, 0.0, 100.0)


def _log_score(value: float, low: float, high: float) -> float:
    if value <= 0:
        return 0.0
    return _scale(math.log10(max(value, 1.0)), math.log10(max(low, 1.0)), math.log10(max(high, low + 1.0)))


def _average(values: Iterable[float], default: float = 0.0) -> float:
    items = list(values)
    return sum(items) / len(items) if items else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


__all__ = [
    "BurstStockScorer",
    "BurstStockSnapshot",
    "OpeningBurstConfig",
    "OpeningBurstWatchsetSelector",
    "OpeningReturnGrade",
    "OpeningThemeBurstEngine",
    "OpeningThemeBurstRank",
    "OpeningThemeBurstRanker",
    "OpeningThemeBurstResult",
    "OpeningTurnoverSeed",
    "OpeningTurnoverSeedCollector",
    "ThemeCohesionScorer",
    "ThemeCohesionSnapshot",
]
