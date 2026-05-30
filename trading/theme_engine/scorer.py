from __future__ import annotations

import math

from trading.theme_engine.models import StockSnapshot, StockThemeState, ThemeActivitySnapshot, ThemeMembership, ThemeRankItem
from trading.theme_engine.ranker import RankHistory, ThemeRanker
from trading.theme_engine.repository import ThemeEngineRepository


class ThemeScoringEngine:
    def __init__(self, repository: ThemeEngineRepository | None = None, rank_history: RankHistory | None = None) -> None:
        self.repository = repository
        self.ranker = ThemeRanker(rank_history)

    def score_theme(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
    ) -> ThemeActivitySnapshot:
        snapshot_by_code = _snapshot_map(snapshots)
        active_members = [member for member in memberships if member.active]
        member_snapshots = [(member, snapshot_by_code.get(member.stock_code)) for member in active_members]
        valid = [(member, snap) for member, snap in member_snapshots if snap is not None]
        total_count = len(active_members)
        rising = [(member, snap) for member, snap in valid if snap.change_rate > 0]
        falling = [(member, snap) for member, snap in valid if snap.change_rate < 0]
        turnover_total = sum(max(0.0, snap.turnover) for _, snap in valid)
        weighted_return = _weighted_return(valid)
        turnover_strength = _average([snap.turnover_strength for _, snap in valid], default=0.0)
        breadth = (len(rising) / total_count) if total_count else 0.0
        leaders = sorted(valid, key=lambda item: _stock_score(item[1], item[0]), reverse=True)
        leader_member, leader = leaders[0] if leaders else (None, None)
        leader_gap = _leader_gap(leaders)
        top3 = sum(max(0.0, snap.turnover) for _, snap in leaders[:3])
        top3_concentration = (top3 / turnover_total) if turnover_total > 0 else 0.0
        momentum = _average([(snap.momentum_1m + snap.momentum_3m + snap.momentum_5m) / 3.0 for _, snap in valid], default=0.0)
        normalized_weighted_return = _clamp((weighted_return + 5.0) / 10.0, 0.0, 1.0) * 100.0
        normalized_turnover_strength = _clamp(turnover_strength / 5.0, 0.0, 1.0) * 100.0
        breadth_score = breadth * 100.0
        leader_score = _clamp(_stock_score(leader, leader_member) / 100.0 if leader else 0.0, 0.0, 1.0) * 100.0
        momentum_score = _clamp((momentum + 3.0) / 6.0, 0.0, 1.0) * 100.0
        theme_score = (
            0.30 * normalized_weighted_return
            + 0.25 * normalized_turnover_strength
            + 0.20 * breadth_score
            + 0.15 * leader_score
            + 0.10 * momentum_score
        )
        reason_codes = []
        if breadth < 0.35 and leader_gap >= 3.0:
            reason_codes.append("LEADER_ONLY_THEME")
        if breadth < 0.5:
            reason_codes.append("WEAK_BREADTH")
        details = {
            "reason_codes": reason_codes,
            "component_scores": {
                "normalized_weighted_return": round(normalized_weighted_return, 4),
                "normalized_turnover_strength": round(normalized_turnover_strength, 4),
                "breadth_score": round(breadth_score, 4),
                "leader_score": round(leader_score, 4),
                "momentum_score": round(momentum_score, 4),
            },
            "leader_only": "LEADER_ONLY_THEME" in reason_codes,
        }
        return ThemeActivitySnapshot(
            theme_id=theme_id,
            theme_name=theme_name,
            theme_score=round(_clamp(theme_score, 0.0, 100.0), 4),
            weighted_return_pct=round(weighted_return, 4),
            turnover=round(turnover_total, 4),
            turnover_strength=round(turnover_strength, 4),
            breadth=round(breadth, 4),
            rising_count=len(rising),
            falling_count=len(falling),
            total_count=total_count,
            leader_code=leader.stock_code if leader else "",
            leader_name=leader.stock_name if leader else "",
            leader_return_pct=round(leader.change_rate, 4) if leader else 0.0,
            leader_turnover=round(leader.turnover, 4) if leader else 0.0,
            leader_gap=round(leader_gap, 4),
            top3_concentration=round(top3_concentration, 4),
            details=details,
        )

    def score_and_rank(
        self,
        theme_inputs: list[tuple[str, str, list[ThemeMembership]]],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
        top_n: int | None = None,
    ) -> list[ThemeActivitySnapshot]:
        scored = [self.score_theme(theme_id, theme_name, memberships, snapshots) for theme_id, theme_name, memberships in theme_inputs]
        ranked = self.ranker.rank(scored, top_n=top_n)
        if self.repository is not None:
            for item in ranked:
                self.repository.save_activity_snapshot(item)
        return ranked

    def stock_theme_state(
        self,
        stock_code: str,
        memberships: list[ThemeMembership],
        rank_items: list[ThemeRankItem],
    ) -> StockThemeState:
        rank_by_theme = {item.theme_id: item for item in rank_items}
        best = None
        for membership in memberships:
            rank = rank_by_theme.get(membership.theme_id)
            if rank is None:
                continue
            value = (rank.theme_score, membership.membership_score)
            if best is None or value > best[0]:
                best = (value, membership, rank)
        if best is None:
            return StockThemeState(stock_code=stock_code, reason_code="NO_ACTIVE_THEME", ready=True)
        _, membership, rank = best
        return StockThemeState(
            stock_code=stock_code,
            stock_name=membership.stock_name,
            primary_theme_id=membership.theme_id,
            primary_theme_name=rank.theme_name,
            primary_rank=rank.rank,
            membership_score=membership.membership_score,
            leadership_role="leader" if rank.leader_code == stock_code else "member",
            ready=True,
        )


def _snapshot_map(snapshots: dict[str, StockSnapshot] | list[StockSnapshot]) -> dict[str, StockSnapshot]:
    if isinstance(snapshots, dict):
        return snapshots
    return {snapshot.stock_code: snapshot for snapshot in snapshots}


def _weighted_return(values: list[tuple[ThemeMembership, StockSnapshot]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for membership, snapshot in values:
        weight = max(0.01, membership.membership_score) * math.sqrt(max(0.0, snapshot.turnover) + 1.0)
        numerator += snapshot.change_rate * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else 0.0


def _stock_score(snapshot: StockSnapshot | None, membership: ThemeMembership | None = None) -> float:
    if snapshot is None:
        return 0.0
    membership_score = membership.membership_score if membership else 1.0
    return (
        snapshot.change_rate * 6.0
        + min(30.0, math.log10(max(1.0, snapshot.turnover)) * 2.0)
        + min(20.0, snapshot.execution_strength / 10.0)
        + (snapshot.momentum_1m + snapshot.momentum_3m + snapshot.momentum_5m) * 2.0
        + membership_score * 10.0
    )


def _leader_gap(leaders: list[tuple[ThemeMembership, StockSnapshot]]) -> float:
    if len(leaders) < 2:
        return 0.0
    leader = leaders[0][1].change_rate
    followers = [snap.change_rate for _, snap in leaders[1:]]
    return max(0.0, leader - (sum(followers) / len(followers)))


def _average(values: list[float], default: float = 0.0) -> float:
    return sum(values) / len(values) if values else default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
