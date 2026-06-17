from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from trading.theme_engine.models import StockSnapshot, ThemeMembership
from trading.theme_engine.normalizer import normalize_stock_code


class ThemeLeadershipStatus(str, Enum):
    DATA_WAIT = "DATA_WAIT"
    LEADING_THEME = "LEADING_THEME"
    SPREADING_THEME = "SPREADING_THEME"
    LEADER_ONLY_THEME = "LEADER_ONLY_THEME"
    WATCH_THEME = "WATCH_THEME"
    WEAK_THEME = "WEAK_THEME"


class StockLeadershipRole(str, Enum):
    LEADER = "LEADER"
    CO_LEADER = "CO_LEADER"
    FOLLOWER = "FOLLOWER"
    LATE_LAGGARD = "LATE_LAGGARD"
    WEAK_MEMBER = "WEAK_MEMBER"
    OVERHEATED = "OVERHEATED"


class MarketPhase(str, Enum):
    EXPANSION = "EXPANSION"
    SELECTIVE = "SELECTIVE"
    CHOPPY = "CHOPPY"
    WEAK = "WEAK"
    RISK_OFF = "RISK_OFF"


@dataclass(frozen=True)
class ThemeLeadershipConfig:
    top_theme_count: int = 5
    max_stocks_per_theme: int = 3
    max_total_watchset: int = 25
    min_valid_members_for_theme: int = 2
    min_data_coverage_for_scoring: float = 0.35
    leading_score_threshold: float = 70.0
    spreading_score_threshold: float = 55.0
    watch_score_threshold: float = 35.0
    leader_min_score: float = 55.0
    co_leader_min_score: float = 52.0
    follower_min_score: float = 45.0
    weak_member_score: float = 30.0
    strong_return_threshold_pct: float = 3.0
    leader_return_threshold_pct: float = 5.0
    late_laggard_return_threshold_pct: float = 3.0
    late_laggard_leader_gap_pct: float = 4.0
    overheat_return_threshold_pct: float = 12.0
    overheat_pullback_from_high_pct: float = 0.2
    overheat_vwap_gap_pct: float = 8.0
    leader_only_breadth_max: float = 0.35
    leader_only_concentration_min: float = 0.65
    max_condition_boost_score: float = 8.0
    default_condition_boost_score: float = 5.0


@dataclass(frozen=True)
class ConditionBoost:
    stock_code: str
    boost_score: float = 5.0
    discovery_source: str = "condition_search"
    condition_name: str = ""


@dataclass(frozen=True)
class StockLeadershipSnapshot:
    calculated_at: str
    theme_id: str
    theme_name: str
    stock_code: str
    stock_name: str = ""
    role: StockLeadershipRole | str = StockLeadershipRole.WEAK_MEMBER
    stock_score: float = 0.0
    rank_in_theme: int = 0
    theme_membership_score: float = 0.0
    turnover_rank_in_theme: float = 0.0
    return_rank_in_theme: float = 0.0
    execution_strength_score: float = 0.0
    momentum_score: float = 0.0
    liquidity_score: float = 0.0
    late_laggard_penalty: float = 0.0
    overheat_penalty: float = 0.0
    condition_boost: float = 0.0
    condition_include_count: int = 0
    discovery_sources: tuple[str, ...] = ()
    current_price: float = 0.0
    change_rate_pct: float = 0.0
    turnover_krw: float = 0.0
    cum_volume: int = 0
    execution_strength: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_ticks: int = 0
    day_high: float = 0.0
    day_low: float = 0.0
    open_price: float = 0.0
    prev_close: float = 0.0
    momentum_1m: float = 0.0
    momentum_3m: float = 0.0
    momentum_5m: float = 0.0
    vwap: float = 0.0
    pullback_from_high_pct: float = 100.0
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False


@dataclass(frozen=True)
class ThemeLeadershipSnapshot:
    calculated_at: str
    theme_id: str
    theme_name: str = ""
    status: ThemeLeadershipStatus | str = ThemeLeadershipStatus.DATA_WAIT
    theme_score: float = 0.0
    raw_total_members: int = 0
    valid_snapshot_count: int = 0
    data_coverage_ratio: float = 0.0
    turnover_krw: float = 0.0
    turnover_rank_score: float = 0.0
    breadth_score: float = 0.0
    weighted_return_score: float = 0.0
    leader_strength_score: float = 0.0
    momentum_score: float = 0.0
    persistence_score: float = 0.0
    concentration_penalty: float = 0.0
    data_quality_penalty: float = 0.0
    breadth_ratio: float = 0.0
    weighted_return_pct: float = 0.0
    rising_count: int = 0
    strong_count: int = 0
    leader_count: int = 0
    leader_symbol: str = ""
    leader_name: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    top3_concentration: float = 0.0
    excluded_late_laggard_count: int = 0
    excluded_overheated_count: int = 0
    condition_boost_count: int = 0
    discovery_sources: tuple[str, ...] = ()
    stocks: tuple[StockLeadershipSnapshot, ...] = ()
    data_quality_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    output_mode: str = "OBSERVE"


@dataclass(frozen=True)
class ThemeLeadershipRank:
    rank: int
    theme_id: str
    theme_name: str = ""
    theme_score: float = 0.0
    status: ThemeLeadershipStatus | str = ThemeLeadershipStatus.DATA_WAIT
    leader_symbol: str = ""
    leader_name: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    excluded_late_laggard_count: int = 0
    excluded_overheated_count: int = 0
    condition_boost_count: int = 0
    discovery_sources: tuple[str, ...] = ()
    snapshot: ThemeLeadershipSnapshot | None = None
    output_mode: str = "OBSERVE"


@dataclass(frozen=True)
class WatchsetSelectionResult:
    calculated_at: str
    selected: tuple[StockLeadershipSnapshot, ...] = ()
    selected_symbols: tuple[str, ...] = ()
    theme_ranks: tuple[ThemeLeadershipRank, ...] = ()
    excluded: tuple[StockLeadershipSnapshot, ...] = ()
    excluded_late_laggard_count: int = 0
    excluded_overheated_count: int = 0
    condition_boost_count: int = 0
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False
    reason_codes: tuple[str, ...] = ()


class ThemeLeadershipRanker:
    def __init__(self, config: ThemeLeadershipConfig | None = None) -> None:
        self.config = config or ThemeLeadershipConfig()

    def rank(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        *,
        condition_boosts: Mapping[str, Any] | Iterable[ConditionBoost] | None = None,
        calculated_at: str = "",
        top_n: int | None = None,
    ) -> list[ThemeLeadershipRank]:
        snapshot_by_symbol = _snapshot_map(snapshots)
        boost_by_symbol = _condition_boost_map(condition_boosts, self.config)
        raw_theme_data = [
            self._collect_theme_data(theme_id, theme_name, memberships, snapshot_by_symbol, boost_by_symbol, calculated_at)
            for theme_id, theme_name, memberships in theme_inputs
        ]
        turnover_rank_scores = _rank_scores(
            {item["theme_id"]: float(item["turnover_krw"]) for item in raw_theme_data},
            positive_only=True,
        )
        theme_snapshots = [
            self._score_theme(item, turnover_rank_scores.get(str(item["theme_id"]), 0.0))
            for item in raw_theme_data
        ]
        ranked_snapshots = sorted(
            theme_snapshots,
            key=lambda item: (
                _theme_status_priority(item.status),
                item.theme_score,
                item.leader_strength_score,
                item.turnover_krw,
            ),
            reverse=True,
        )
        if top_n is not None:
            ranked_snapshots = ranked_snapshots[:top_n]
        return [
            ThemeLeadershipRank(
                rank=index,
                theme_id=item.theme_id,
                theme_name=item.theme_name,
                theme_score=item.theme_score,
                status=item.status,
                leader_symbol=item.leader_symbol,
                leader_name=item.leader_name,
                co_leader_symbols=item.co_leader_symbols,
                excluded_late_laggard_count=item.excluded_late_laggard_count,
                excluded_overheated_count=item.excluded_overheated_count,
                condition_boost_count=item.condition_boost_count,
                discovery_sources=item.discovery_sources,
                snapshot=item,
            )
            for index, item in enumerate(ranked_snapshots, start=1)
        ]

    def _collect_theme_data(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
        snapshot_by_symbol: dict[str, StockSnapshot],
        boost_by_symbol: dict[str, tuple[float, tuple[str, ...], int]],
        calculated_at: str,
    ) -> dict[str, Any]:
        active_members = [member for member in memberships if member.active]
        valid_pairs: list[tuple[ThemeMembership, StockSnapshot]] = []
        missing_count = 0
        for member in active_members:
            code = normalize_stock_code(member.stock_code)
            snapshot = snapshot_by_symbol.get(code) or snapshot_by_symbol.get(member.stock_code)
            if snapshot is None:
                missing_count += 1
                continue
            valid_pairs.append((member, snapshot))

        turnover_rank_scores = _rank_scores(
            {_member_code(member, snapshot): float(snapshot.turnover or 0.0) for member, snapshot in valid_pairs},
            positive_only=True,
        )
        return_rank_scores = _rank_scores(
            {_member_code(member, snapshot): float(snapshot.change_rate or 0.0) for member, snapshot in valid_pairs}
        )
        leader_return = max((float(snapshot.change_rate or 0.0) for _, snapshot in valid_pairs), default=0.0)
        stocks = [
            _score_stock(
                theme_id=theme_id,
                theme_name=theme_name,
                member=member,
                snapshot=snapshot,
                turnover_rank_score=turnover_rank_scores.get(_member_code(member, snapshot), 0.0),
                return_rank_score=return_rank_scores.get(_member_code(member, snapshot), 0.0),
                leader_return_pct=leader_return,
                boost=boost_by_symbol.get(_member_code(member, snapshot), (0.0, (), 0)),
                config=self.config,
                calculated_at=calculated_at,
            )
            for member, snapshot in valid_pairs
        ]
        stocks = _classify_stock_roles(stocks, self.config)
        turnover = sum(max(0.0, stock.turnover_krw) for stock in stocks)
        condition_boost_count = sum(stock.condition_include_count for stock in stocks)
        discovery_sources = _dedupe(
            source
            for stock in stocks
            for source in stock.discovery_sources
        )
        return {
            "theme_id": theme_id,
            "theme_name": theme_name,
            "raw_total_members": len(active_members),
            "valid_snapshot_count": len(valid_pairs),
            "missing_snapshot_count": missing_count,
            "stocks": stocks,
            "turnover_krw": turnover,
            "condition_boost_count": condition_boost_count,
            "discovery_sources": discovery_sources,
            "calculated_at": calculated_at,
        }

    def _score_theme(self, data: dict[str, Any], turnover_rank_score: float) -> ThemeLeadershipSnapshot:
        stocks: list[StockLeadershipSnapshot] = list(data["stocks"])
        raw_total = int(data["raw_total_members"])
        valid_count = int(data["valid_snapshot_count"])
        coverage = valid_count / raw_total if raw_total else 0.0
        rising_count = sum(1 for stock in stocks if stock.change_rate_pct > 0)
        strong_count = sum(1 for stock in stocks if stock.change_rate_pct >= self.config.strong_return_threshold_pct)
        leader_count = sum(1 for stock in stocks if stock.role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER})
        breadth_ratio = rising_count / valid_count if valid_count else 0.0
        strong_ratio = strong_count / valid_count if valid_count else 0.0
        breadth_score = _clamp((breadth_ratio * 60.0) + (strong_ratio * 40.0), 0.0, 100.0)
        weighted_return = _weighted_return(stocks)
        weighted_return_score = _scale(weighted_return, -3.0, 7.0)
        leader_strength_score = max(
            (stock.stock_score for stock in stocks if stock.role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER}),
            default=(max((stock.stock_score for stock in stocks), default=0.0)),
        )
        momentum_score = _average([stock.momentum_score for stock in stocks], default=0.0)
        persistence_score = _persistence_score(stocks)
        turnover = float(data["turnover_krw"])
        top3_turnover = sum(sorted([max(0.0, stock.turnover_krw) for stock in stocks], reverse=True)[:3])
        concentration = top3_turnover / turnover if turnover > 0 else 0.0
        concentration_penalty = _concentration_penalty(concentration, valid_count)
        data_quality_penalty = _data_quality_penalty(coverage, valid_count, raw_total)
        base_score = (
            0.25 * turnover_rank_score
            + 0.20 * breadth_score
            + 0.20 * weighted_return_score
            + 0.15 * leader_strength_score
            + 0.10 * momentum_score
            + 0.10 * persistence_score
        )
        theme_score = _clamp(base_score - concentration_penalty - data_quality_penalty, 0.0, 100.0)
        flags: list[str] = []
        reasons: list[str] = []
        if raw_total == 0:
            flags.append("EMPTY_THEME_MEMBERSHIP")
            reasons.append("DATA_WAIT_EMPTY_THEME")
        if valid_count < raw_total:
            flags.append("MISSING_MEMBER_SNAPSHOT")
        if coverage < self.config.min_data_coverage_for_scoring:
            flags.append("LOW_SNAPSHOT_COVERAGE")
            reasons.append("DATA_WAIT_LOW_SNAPSHOT_COVERAGE")
        if valid_count < self.config.min_valid_members_for_theme:
            flags.append("TOO_FEW_VALID_MEMBERS")
            reasons.append("DATA_WAIT_TOO_FEW_VALID_MEMBERS")
        status = _classify_theme_status(
            theme_score=theme_score,
            coverage=coverage,
            valid_count=valid_count,
            raw_total=raw_total,
            breadth_ratio=breadth_ratio,
            strong_count=strong_count,
            leader_count=leader_count,
            top3_concentration=concentration,
            condition_boost_count=int(data["condition_boost_count"]),
            config=self.config,
        )
        leader = next((stock for stock in stocks if stock.role == StockLeadershipRole.LEADER), None)
        co_leaders = tuple(stock.stock_code for stock in stocks if stock.role == StockLeadershipRole.CO_LEADER)
        excluded_late = sum(1 for stock in stocks if stock.role == StockLeadershipRole.LATE_LAGGARD)
        excluded_overheated = sum(1 for stock in stocks if stock.role == StockLeadershipRole.OVERHEATED)
        if excluded_late:
            reasons.append("LATE_LAGGARD_EXCLUDED_FROM_WATCHSET")
        if excluded_overheated:
            reasons.append("OVERHEATED_EXCLUDED_FROM_WATCHSET")
        return ThemeLeadershipSnapshot(
            calculated_at=str(data["calculated_at"]),
            theme_id=str(data["theme_id"]),
            theme_name=str(data["theme_name"]),
            status=status,
            theme_score=round(theme_score, 4),
            raw_total_members=raw_total,
            valid_snapshot_count=valid_count,
            data_coverage_ratio=round(coverage, 4),
            turnover_krw=round(turnover, 4),
            turnover_rank_score=round(turnover_rank_score, 4),
            breadth_score=round(breadth_score, 4),
            weighted_return_score=round(weighted_return_score, 4),
            leader_strength_score=round(leader_strength_score, 4),
            momentum_score=round(momentum_score, 4),
            persistence_score=round(persistence_score, 4),
            concentration_penalty=round(concentration_penalty, 4),
            data_quality_penalty=round(data_quality_penalty, 4),
            breadth_ratio=round(breadth_ratio, 4),
            weighted_return_pct=round(weighted_return, 4),
            rising_count=rising_count,
            strong_count=strong_count,
            leader_count=leader_count,
            leader_symbol=leader.stock_code if leader else "",
            leader_name=leader.stock_name if leader else "",
            co_leader_symbols=co_leaders,
            top3_concentration=round(concentration, 4),
            excluded_late_laggard_count=excluded_late,
            excluded_overheated_count=excluded_overheated,
            condition_boost_count=int(data["condition_boost_count"]),
            discovery_sources=tuple(data["discovery_sources"]),
            stocks=tuple(stocks),
            data_quality_flags=tuple(flags),
            reason_codes=tuple(_dedupe(reasons)),
        )


class WatchsetSelector:
    def __init__(self, config: ThemeLeadershipConfig | None = None) -> None:
        self.config = config or ThemeLeadershipConfig()

    def select(
        self,
        ranked_themes: Iterable[ThemeLeadershipRank | ThemeLeadershipSnapshot],
        *,
        market_phase: MarketPhase | str = MarketPhase.SELECTIVE,
        calculated_at: str = "",
    ) -> WatchsetSelectionResult:
        phase = MarketPhase(market_phase)
        ranks = _as_ranks(ranked_themes)
        selected_by_symbol: dict[str, StockLeadershipSnapshot] = {}
        excluded: list[StockLeadershipSnapshot] = []
        reason_codes: list[str] = ["RT_TLS_OBSERVE_ONLY"]
        for rank in ranks[: self.config.top_theme_count]:
            snapshot = rank.snapshot
            if snapshot is None:
                continue
            if snapshot.status not in {
                ThemeLeadershipStatus.LEADING_THEME,
                ThemeLeadershipStatus.SPREADING_THEME,
                ThemeLeadershipStatus.LEADER_ONLY_THEME,
            }:
                reason_codes.append(f"THEME_NOT_WATCHSET_ELIGIBLE_{_value(snapshot.status)}")
                excluded.extend(snapshot.stocks)
                continue
            per_theme_count = 0
            for stock in sorted(snapshot.stocks, key=lambda item: (item.stock_score, item.turnover_krw), reverse=True):
                if len(selected_by_symbol) >= self.config.max_total_watchset:
                    reason_codes.append("WATCHSET_TOTAL_LIMIT_REACHED")
                    excluded.append(stock)
                    continue
                allowed, reason = _stock_allowed_for_watchset(stock, snapshot.status, phase)
                if not allowed:
                    reason_codes.append(reason)
                    excluded.append(stock)
                    continue
                if per_theme_count >= self.config.max_stocks_per_theme:
                    reason_codes.append("WATCHSET_PER_THEME_LIMIT_REACHED")
                    excluded.append(stock)
                    continue
                existing = selected_by_symbol.get(stock.stock_code)
                if existing is not None and existing.stock_score >= stock.stock_score:
                    continue
                selected_by_symbol[stock.stock_code] = stock
                per_theme_count += 1
        selected = tuple(selected_by_symbol.values())
        excluded_late = sum(1 for stock in excluded if stock.role == StockLeadershipRole.LATE_LAGGARD)
        excluded_overheated = sum(1 for stock in excluded if stock.role == StockLeadershipRole.OVERHEATED)
        return WatchsetSelectionResult(
            calculated_at=calculated_at,
            selected=selected,
            selected_symbols=tuple(stock.stock_code for stock in selected),
            theme_ranks=tuple(ranks),
            excluded=tuple(excluded),
            excluded_late_laggard_count=excluded_late,
            excluded_overheated_count=excluded_overheated,
            condition_boost_count=sum(rank.condition_boost_count for rank in ranks),
            reason_codes=tuple(_dedupe(reason_codes)),
        )


def _score_stock(
    *,
    theme_id: str,
    theme_name: str,
    member: ThemeMembership,
    snapshot: StockSnapshot,
    turnover_rank_score: float,
    return_rank_score: float,
    leader_return_pct: float,
    boost: tuple[float, tuple[str, ...], int],
    config: ThemeLeadershipConfig,
    calculated_at: str,
) -> StockLeadershipSnapshot:
    code = _member_code(member, snapshot)
    membership_score = _clamp(float(member.membership_score or 0.0) * 100.0, 0.0, 100.0)
    execution_strength_score = _scale(float(snapshot.execution_strength or 0.0), 70.0, 180.0)
    momentum_score = _scale(_stock_momentum(snapshot), -2.0, 3.0)
    liquidity_score = _liquidity_score(float(snapshot.turnover or 0.0))
    late_penalty = _late_laggard_penalty(snapshot, leader_return_pct, config)
    overheat_penalty = _overheat_penalty(snapshot, config)
    condition_boost, discovery_sources, condition_count = boost
    stock_score = (
        0.25 * membership_score
        + 0.25 * turnover_rank_score
        + 0.20 * return_rank_score
        + 0.15 * execution_strength_score
        + 0.10 * momentum_score
        + 0.05 * liquidity_score
        + condition_boost
        - late_penalty
        - overheat_penalty
    )
    spread_ticks = _spread_ticks(snapshot)
    day_high = _metadata_float(snapshot, "day_high", "session_high", default=float(snapshot.session_high or 0.0))
    day_low = _metadata_float(snapshot, "day_low", "session_low", default=float(snapshot.session_low or 0.0))
    return StockLeadershipSnapshot(
        calculated_at=calculated_at,
        theme_id=theme_id,
        theme_name=theme_name,
        stock_code=code,
        stock_name=snapshot.stock_name or member.stock_name,
        stock_score=round(_clamp(stock_score, 0.0, 100.0), 4),
        theme_membership_score=round(membership_score, 4),
        turnover_rank_in_theme=round(turnover_rank_score, 4),
        return_rank_in_theme=round(return_rank_score, 4),
        execution_strength_score=round(execution_strength_score, 4),
        momentum_score=round(momentum_score, 4),
        liquidity_score=round(liquidity_score, 4),
        late_laggard_penalty=round(late_penalty, 4),
        overheat_penalty=round(overheat_penalty, 4),
        condition_boost=round(condition_boost, 4),
        condition_include_count=condition_count,
        discovery_sources=discovery_sources,
        current_price=float(snapshot.current_price or 0.0),
        change_rate_pct=float(snapshot.change_rate or 0.0),
        turnover_krw=float(snapshot.turnover or 0.0),
        cum_volume=int(snapshot.volume or 0),
        execution_strength=float(snapshot.execution_strength or 0.0),
        best_bid=float(snapshot.best_bid or 0.0),
        best_ask=float(snapshot.best_ask or 0.0),
        spread_ticks=spread_ticks,
        day_high=day_high,
        day_low=day_low,
        open_price=_metadata_float(snapshot, "open_price", "open", default=0.0),
        prev_close=_metadata_float(snapshot, "prev_close", "previous_close", "yesterday_close", default=0.0),
        momentum_1m=float(snapshot.momentum_1m or 0.0),
        momentum_3m=float(snapshot.momentum_3m or 0.0),
        momentum_5m=float(snapshot.momentum_5m or 0.0),
        vwap=_metadata_float(snapshot, "vwap", default=0.0),
        pullback_from_high_pct=_pullback_from_high_pct(snapshot),
        reason_codes=tuple(
            _dedupe(
                [
                    "CONDITION_INCLUDE_BOOSTER_ONLY" if condition_count else "",
                    "LATE_LAGGARD_PENALTY" if late_penalty else "",
                    "OVERHEAT_PENALTY" if overheat_penalty else "",
                ]
            )
        ),
    )


def _classify_stock_roles(
    stocks: list[StockLeadershipSnapshot],
    config: ThemeLeadershipConfig,
) -> list[StockLeadershipSnapshot]:
    ordered = sorted(stocks, key=lambda item: (item.stock_score, item.change_rate_pct, item.turnover_krw), reverse=True)
    leader_score = ordered[0].stock_score if ordered else 0.0
    leader_return = ordered[0].change_rate_pct if ordered else 0.0
    classified: list[StockLeadershipSnapshot] = []
    for rank, stock in enumerate(ordered, start=1):
        role = StockLeadershipRole.WEAK_MEMBER
        reasons = list(stock.reason_codes)
        if stock.overheat_penalty > 0:
            role = StockLeadershipRole.OVERHEATED
            reasons.append("OVERHEATED_EXCLUDED")
        elif rank == 1 and stock.stock_score >= config.leader_min_score:
            role = StockLeadershipRole.LEADER
            reasons.append("RT_TLS_LEADER")
        elif (
            rank <= 3
            and stock.stock_score >= config.co_leader_min_score
            and (leader_score - stock.stock_score <= 15.0 or leader_return - stock.change_rate_pct <= 2.0)
        ):
            role = StockLeadershipRole.CO_LEADER
            reasons.append("RT_TLS_CO_LEADER")
        elif stock.late_laggard_penalty > 0:
            role = StockLeadershipRole.LATE_LAGGARD
            reasons.append("LATE_LAGGARD_EXCLUDED")
        elif stock.stock_score >= config.follower_min_score and stock.change_rate_pct > 0:
            role = StockLeadershipRole.FOLLOWER
            reasons.append("RT_TLS_FOLLOWER")
        elif stock.stock_score < config.weak_member_score or stock.change_rate_pct < 0:
            role = StockLeadershipRole.WEAK_MEMBER
            reasons.append("WEAK_MEMBER_EXCLUDED")
        classified.append(replace(stock, role=role, rank_in_theme=rank, reason_codes=tuple(_dedupe(reasons))))
    return sorted(classified, key=lambda item: item.rank_in_theme)


def _classify_theme_status(
    *,
    theme_score: float,
    coverage: float,
    valid_count: int,
    raw_total: int,
    breadth_ratio: float,
    strong_count: int,
    leader_count: int,
    top3_concentration: float,
    condition_boost_count: int,
    config: ThemeLeadershipConfig,
) -> ThemeLeadershipStatus:
    if (
        raw_total == 0
        or valid_count < config.min_valid_members_for_theme
        or coverage < config.min_data_coverage_for_scoring
    ):
        return ThemeLeadershipStatus.DATA_WAIT
    concentrated = valid_count >= 5 and top3_concentration >= config.leader_only_concentration_min
    if (
        leader_count >= 1
        and theme_score >= config.watch_score_threshold
        and (breadth_ratio <= config.leader_only_breadth_max or concentrated)
    ):
        return ThemeLeadershipStatus.LEADER_ONLY_THEME
    if theme_score >= config.leading_score_threshold and breadth_ratio >= 0.5 and leader_count >= 1 and strong_count >= 2:
        return ThemeLeadershipStatus.LEADING_THEME
    if theme_score >= config.spreading_score_threshold and breadth_ratio >= 0.4 and strong_count >= 1:
        return ThemeLeadershipStatus.SPREADING_THEME
    if theme_score >= config.watch_score_threshold or leader_count >= 1 or condition_boost_count > 0:
        return ThemeLeadershipStatus.WATCH_THEME
    return ThemeLeadershipStatus.WEAK_THEME


def _stock_allowed_for_watchset(
    stock: StockLeadershipSnapshot,
    theme_status: ThemeLeadershipStatus | str,
    market_phase: MarketPhase,
) -> tuple[bool, str]:
    role = StockLeadershipRole(stock.role)
    if role in {StockLeadershipRole.LATE_LAGGARD, StockLeadershipRole.WEAK_MEMBER, StockLeadershipRole.OVERHEATED}:
        return False, f"{role.value}_EXCLUDED_FROM_WATCHSET"
    if theme_status == ThemeLeadershipStatus.LEADER_ONLY_THEME:
        if role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER}:
            return True, "LEADER_ONLY_THEME_LEADER_ALLOWED"
        return False, "LEADER_ONLY_THEME_NON_LEADER_EXCLUDED"
    if role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER}:
        return True, "LEADER_OR_CO_LEADER_ALLOWED"
    if role == StockLeadershipRole.FOLLOWER and market_phase == MarketPhase.EXPANSION:
        return True, "FOLLOWER_ALLOWED_IN_EXPANSION"
    return False, "FOLLOWER_REQUIRES_EXPANSION"


def _as_ranks(values: Iterable[ThemeLeadershipRank | ThemeLeadershipSnapshot]) -> list[ThemeLeadershipRank]:
    ranks: list[ThemeLeadershipRank] = []
    for index, value in enumerate(values, start=1):
        if isinstance(value, ThemeLeadershipRank):
            ranks.append(value)
            continue
        ranks.append(
            ThemeLeadershipRank(
                rank=index,
                theme_id=value.theme_id,
                theme_name=value.theme_name,
                theme_score=value.theme_score,
                status=value.status,
                leader_symbol=value.leader_symbol,
                leader_name=value.leader_name,
                co_leader_symbols=value.co_leader_symbols,
                excluded_late_laggard_count=value.excluded_late_laggard_count,
                excluded_overheated_count=value.excluded_overheated_count,
                condition_boost_count=value.condition_boost_count,
                discovery_sources=value.discovery_sources,
                snapshot=value,
            )
        )
    return ranks


def _condition_boost_map(
    values: Mapping[str, Any] | Iterable[ConditionBoost] | None,
    config: ThemeLeadershipConfig,
) -> dict[str, tuple[float, tuple[str, ...], int]]:
    if values is None:
        return {}
    result: dict[str, tuple[float, tuple[str, ...], int]] = {}
    if isinstance(values, Mapping):
        iterable = [_boost_from_mapping_item(code, raw, config) for code, raw in values.items()]
    else:
        iterable = list(values)
    for boost in iterable:
        code = normalize_stock_code(boost.stock_code)
        if not code:
            continue
        score = _clamp(float(boost.boost_score or 0.0), 0.0, config.max_condition_boost_score)
        sources = tuple(
            _dedupe(
                [
                    boost.discovery_source or "condition_search",
                    boost.condition_name,
                ]
            )
        )
        previous = result.get(code, (0.0, (), 0))
        result[code] = (
            _clamp(previous[0] + score, 0.0, config.max_condition_boost_score),
            tuple(_dedupe([*previous[1], *sources])),
            previous[2] + 1,
        )
    return result


def _boost_from_mapping_item(code: str, raw: Any, config: ThemeLeadershipConfig) -> ConditionBoost:
    if isinstance(raw, ConditionBoost):
        return raw
    if isinstance(raw, (int, float)):
        return ConditionBoost(stock_code=code, boost_score=float(raw))
    if isinstance(raw, str):
        return ConditionBoost(stock_code=code, boost_score=config.default_condition_boost_score, condition_name=raw)
    if isinstance(raw, Iterable):
        names = [str(item) for item in raw if str(item or "").strip()]
        return ConditionBoost(
            stock_code=code,
            boost_score=config.default_condition_boost_score * max(1, len(names)),
            condition_name=",".join(names),
        )
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


def _member_code(member: ThemeMembership, snapshot: StockSnapshot | None = None) -> str:
    return normalize_stock_code((snapshot.stock_code if snapshot else "") or member.stock_code)


def _rank_scores(values: Mapping[str, float], *, positive_only: bool = False) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: item[1], reverse=True)
    if len(ordered) == 1:
        key, value = ordered[0]
        return {key: 100.0 if (value > 0 or not positive_only) else 0.0}
    result: dict[str, float] = {}
    denominator = len(ordered) - 1
    for index, (key, value) in enumerate(ordered):
        if positive_only and value <= 0:
            result[key] = 0.0
            continue
        result[key] = ((denominator - index) / denominator) * 100.0
    return result


def _weighted_return(stocks: list[StockLeadershipSnapshot]) -> float:
    numerator = 0.0
    denominator = 0.0
    for stock in stocks:
        weight = max(0.01, stock.theme_membership_score / 100.0) * math.sqrt(max(0.0, stock.turnover_krw) + 1.0)
        numerator += stock.change_rate_pct * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else 0.0


def _persistence_score(stocks: list[StockLeadershipSnapshot]) -> float:
    values = []
    for stock in stocks:
        raw = 0.0
        if stock.momentum_3m > 0:
            raw += 35.0
        if stock.momentum_5m > 0:
            raw += 35.0
        if stock.change_rate_pct > 0:
            raw += 30.0
        values.append(raw)
    return _average(values, default=0.0)


def _late_laggard_penalty(
    snapshot: StockSnapshot,
    leader_return_pct: float,
    config: ThemeLeadershipConfig,
) -> float:
    change_rate = float(snapshot.change_rate or 0.0)
    if leader_return_pct < config.leader_return_threshold_pct:
        return 0.0
    if change_rate >= config.late_laggard_return_threshold_pct:
        return 0.0
    if leader_return_pct - change_rate < config.late_laggard_leader_gap_pct:
        return 0.0
    if _stock_momentum(snapshot) > 0.5 and change_rate > 0:
        return 0.0
    return 18.0


def _overheat_penalty(snapshot: StockSnapshot, config: ThemeLeadershipConfig) -> float:
    change_rate = float(snapshot.change_rate or 0.0)
    pullback = _pullback_from_high_pct(snapshot)
    vwap = _metadata_float(snapshot, "vwap", default=0.0)
    current = float(snapshot.current_price or 0.0)
    vwap_gap = ((current - vwap) / vwap) * 100.0 if current > 0 and vwap > 0 else 0.0
    if change_rate >= config.overheat_return_threshold_pct and pullback <= config.overheat_pullback_from_high_pct:
        return 25.0
    if change_rate >= 10.0 and vwap_gap >= config.overheat_vwap_gap_pct:
        return 20.0
    return 0.0


def _pullback_from_high_pct(snapshot: StockSnapshot) -> float:
    metadata = snapshot.metadata or {}
    raw = metadata.get("pullback_from_high_pct")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    high = float(snapshot.session_high or metadata.get("day_high") or 0.0)
    price = float(snapshot.current_price or 0.0)
    if high <= 0 or price <= 0:
        return 100.0
    return max(0.0, ((high - price) / high) * 100.0)


def _stock_momentum(snapshot: StockSnapshot) -> float:
    return (
        float(snapshot.momentum_1m or 0.0)
        + float(snapshot.momentum_3m or 0.0)
        + float(snapshot.momentum_5m or 0.0)
    ) / 3.0


def _liquidity_score(turnover: float) -> float:
    if turnover <= 0:
        return 0.0
    return _clamp((math.log10(turnover) - 7.0) / 3.0 * 100.0, 0.0, 100.0)


def _spread_ticks(snapshot: StockSnapshot) -> int:
    bid = float(snapshot.best_bid or 0.0)
    ask = float(snapshot.best_ask or 0.0)
    if ask <= 0 or bid <= 0 or ask < bid:
        return 0
    tick = float((snapshot.metadata or {}).get("tick_size") or 1.0)
    return int(round((ask - bid) / max(tick, 1.0)))


def _metadata_float(snapshot: StockSnapshot, *keys: str, default: float = 0.0) -> float:
    metadata = snapshot.metadata or {}
    for key in keys:
        try:
            value = float(metadata.get(key))
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return float(default or 0.0)


def _concentration_penalty(top3_concentration: float, valid_count: int) -> float:
    if valid_count < 5 or top3_concentration <= 0.65:
        return 0.0
    return _clamp((top3_concentration - 0.65) * 50.0, 0.0, 18.0)


def _data_quality_penalty(coverage: float, valid_count: int, raw_total: int) -> float:
    if raw_total <= 0:
        return 40.0
    penalty = 0.0
    if coverage < 0.8:
        penalty += (0.8 - coverage) * 30.0
    if valid_count <= 1:
        penalty += 12.0
    return _clamp(penalty, 0.0, 30.0)


def _theme_status_priority(status: ThemeLeadershipStatus | str) -> int:
    value = _value(status)
    priorities = {
        ThemeLeadershipStatus.LEADING_THEME.value: 5,
        ThemeLeadershipStatus.SPREADING_THEME.value: 4,
        ThemeLeadershipStatus.LEADER_ONLY_THEME.value: 3,
        ThemeLeadershipStatus.WATCH_THEME.value: 2,
        ThemeLeadershipStatus.DATA_WAIT.value: 1,
        ThemeLeadershipStatus.WEAK_THEME.value: 0,
    }
    return priorities.get(value, 0)


def _scale(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp(((value - low) / (high - low)) * 100.0, 0.0, 100.0)


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


def _value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


__all__ = [
    "ConditionBoost",
    "MarketPhase",
    "StockLeadershipRole",
    "StockLeadershipSnapshot",
    "ThemeLeadershipConfig",
    "ThemeLeadershipRank",
    "ThemeLeadershipRanker",
    "ThemeLeadershipSnapshot",
    "ThemeLeadershipStatus",
    "WatchsetSelectionResult",
    "WatchsetSelector",
]
