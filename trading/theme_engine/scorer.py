from __future__ import annotations

import math
from dataclasses import dataclass

from trading.theme_engine.models import StockSnapshot, StockThemeState, ThemeActivitySnapshot, ThemeMembership, ThemeRankItem
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.ranker import RankHistory, ThemeRanker
from trading.theme_engine.repository import ThemeEngineRepository


@dataclass
class ThemeScoringConfig:
    low_breadth_threshold: float = 0.5
    leader_only_breadth_threshold: float = 0.35
    leader_gap_threshold: float = 3.0
    low_turnover_threshold: float = 1_000_000_000.0
    active_score_threshold: float = 70.0
    active_breadth_threshold: float = 0.5
    min_trade_eligible_for_active: int = 3


class ThemeScoringEngine:
    def __init__(
        self,
        repository: ThemeEngineRepository | None = None,
        rank_history: RankHistory | None = None,
        config: ThemeScoringConfig | None = None,
    ) -> None:
        self.repository = repository
        self.ranker = ThemeRanker(rank_history)
        self.config = config or ThemeScoringConfig()

    def score_theme(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
        snapshots: dict[str, StockSnapshot] | list[StockSnapshot],
    ) -> ThemeActivitySnapshot:
        snapshot_by_code = _snapshot_map(snapshots)
        active_members = [member for member in memberships if member.active]
        member_snapshots = [
            (member, snapshot_by_code.get(member.stock_code) or snapshot_by_code.get(normalize_stock_code(member.stock_code)))
            for member in active_members
        ]
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
        snapshot_quality = _snapshot_quality(total_count, valid)
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
        if total_count < 2:
            reason_codes.append("TOO_FEW_MEMBERS")
        if len(valid) < total_count:
            reason_codes.append("INSUFFICIENT_SNAPSHOT")
        if turnover_total < self.config.low_turnover_threshold:
            reason_codes.append("LOW_TURNOVER")
        if breadth < self.config.leader_only_breadth_threshold and leader_gap >= self.config.leader_gap_threshold:
            reason_codes.append("LEADER_ONLY_THEME")
        if breadth < self.config.low_breadth_threshold:
            reason_codes.append("LOW_BREADTH")
        if snapshot_quality["snapshot_coverage"] < 0.5:
            _append_reason_code(reason_codes, "LOW_SNAPSHOT_COVERAGE")
        if snapshot_quality["estimated_turnover_ratio"] >= 0.5:
            _append_reason_code(reason_codes, "ESTIMATED_TURNOVER_HEAVY")
        trade_eligible_count = sum(1 for member in active_members if member.trade_eligible)
        active_dry_run = (
            trade_eligible_count >= self.config.min_trade_eligible_for_active
            and theme_score >= self.config.active_score_threshold
            and breadth >= self.config.active_breadth_threshold
            and "LEADER_ONLY_THEME" not in reason_codes
            and "TOO_FEW_MEMBERS" not in reason_codes
        )
        details = {
            "reason_codes": reason_codes,
            "active_promotion_dry_run": "ACTIVE" if active_dry_run else "WATCH",
            "trade_eligible_count": trade_eligible_count,
            "component_scores": {
                "normalized_weighted_return": round(normalized_weighted_return, 4),
                "normalized_turnover_strength": round(normalized_turnover_strength, 4),
                "breadth_score": round(breadth_score, 4),
                "leader_score": round(leader_score, 4),
                "momentum_score": round(momentum_score, 4),
            },
            "leader_only": "LEADER_ONLY_THEME" in reason_codes,
            "top_stocks": [
                _top_stock_dict(rank, member, snap)
                for rank, (member, snap) in enumerate(leaders[:5], start=1)
            ],
            "scored_members": [_scored_member_dict(member, snap) for member, snap in member_snapshots],
            "snapshot_quality": _rounded_snapshot_quality(snapshot_quality),
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
            leader_code=_stock_code(leader.stock_code, leader_member.stock_code) if leader and leader_member else "",
            leader_name=(leader.stock_name or leader_member.stock_name) if leader and leader_member else "",
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
    result: dict[str, StockSnapshot] = {}
    if isinstance(snapshots, dict):
        for key, snapshot in snapshots.items():
            result[str(key)] = snapshot
            normalized_key = normalize_stock_code(str(key))
            normalized_snapshot_code = normalize_stock_code(snapshot.stock_code)
            if normalized_key:
                result[normalized_key] = snapshot
            if normalized_snapshot_code:
                result[normalized_snapshot_code] = snapshot
        return result
    for snapshot in snapshots:
        result[snapshot.stock_code] = snapshot
        normalized_code = normalize_stock_code(snapshot.stock_code)
        if normalized_code:
            result[normalized_code] = snapshot
    return result


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


def _top_stock_dict(rank: int, membership: ThemeMembership, snapshot: StockSnapshot) -> dict:
    return {
        "rank": int(rank),
        "stock_code": _stock_code(snapshot.stock_code, membership.stock_code),
        "stock_name": snapshot.stock_name or membership.stock_name,
        "change_rate": round(float(snapshot.change_rate), 4),
        "turnover": round(float(snapshot.turnover), 4),
        "turnover_strength": round(float(snapshot.turnover_strength), 4),
        "execution_strength": round(float(snapshot.execution_strength), 4),
        "momentum_1m": round(float(snapshot.momentum_1m), 4),
        "momentum_3m": round(float(snapshot.momentum_3m), 4),
        "momentum_5m": round(float(snapshot.momentum_5m), 4),
        "membership_score": round(float(membership.membership_score), 4),
        "relation_type": _value(membership.relation_type),
        "source_count": int(membership.source_count),
        "trade_eligible": bool(membership.trade_eligible),
        "stock_score": round(_stock_score(snapshot, membership), 4),
        "metadata_reason_codes": _metadata_reason_codes(snapshot),
    }


def _scored_member_dict(membership: ThemeMembership, snapshot: StockSnapshot | None) -> dict:
    return {
        "stock_code": _stock_code(membership.stock_code, snapshot.stock_code if snapshot else ""),
        "stock_name": (snapshot.stock_name if snapshot else "") or membership.stock_name,
        "has_snapshot": snapshot is not None,
        "active": bool(membership.active),
        "trade_eligible": bool(membership.trade_eligible),
        "membership_score": round(float(membership.membership_score), 4),
        "relation_type": _value(membership.relation_type),
        "source_count": int(membership.source_count),
    }


def _snapshot_quality(total_count: int, valid: list[tuple[ThemeMembership, StockSnapshot]]) -> dict[str, float | int]:
    valid_count = len(valid)
    estimated_count = sum(1 for _, snapshot in valid if "TURNOVER_ESTIMATED" in _metadata_reason_codes(snapshot))
    return {
        "active_member_count": int(total_count),
        "valid_snapshot_count": int(valid_count),
        "snapshot_coverage": (valid_count / total_count) if total_count else 0.0,
        "missing_snapshot_count": max(0, int(total_count) - int(valid_count)),
        "estimated_turnover_count": int(estimated_count),
        "estimated_turnover_ratio": (estimated_count / valid_count) if valid_count else 0.0,
    }


def _rounded_snapshot_quality(value: dict[str, float | int]) -> dict:
    return {
        "active_member_count": int(value["active_member_count"]),
        "valid_snapshot_count": int(value["valid_snapshot_count"]),
        "snapshot_coverage": round(float(value["snapshot_coverage"]), 4),
        "missing_snapshot_count": int(value["missing_snapshot_count"]),
        "estimated_turnover_count": int(value["estimated_turnover_count"]),
        "estimated_turnover_ratio": round(float(value["estimated_turnover_ratio"]), 4),
    }


def _metadata_reason_codes(snapshot: StockSnapshot) -> list[str]:
    raw = snapshot.metadata.get("reason_codes") if snapshot.metadata else []
    if isinstance(raw, str):
        raw = [raw]
    return [str(code) for code in list(raw or []) if str(code or "").strip()]


def _stock_code(*values: str) -> str:
    for value in values:
        normalized = normalize_stock_code(value)
        if normalized:
            return normalized
    return ""


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _append_reason_code(reason_codes: list[str], code: str) -> None:
    if code not in reason_codes:
        reason_codes.append(code)


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
