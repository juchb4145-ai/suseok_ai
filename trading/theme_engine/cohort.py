from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.signals import LiveSeedSignal, ThemeDataWaitReason


@dataclass(frozen=True)
class ThemeCohortConfig:
    alive_threshold_pct: float = -1.0
    strong_threshold_pct: float = 3.0
    leader_threshold_pct: float = 5.0
    min_realtime_valid_ratio: float = 0.35
    small_theme_member_count: int = 4
    min_strong_count: int = 3
    min_strong_count_small_theme: int = 2


@dataclass(frozen=True)
class ThemeCohortSnapshot:
    theme_id: str
    theme_name: str = ""
    member_count: int = 0
    seed_member_count: int = 0
    realtime_valid_count: int = 0
    strong_count: int = 0
    leader_count: int = 0
    alive_ratio: float = 0.0
    strong_ratio: float = 0.0
    leader_ratio: float = 0.0
    theme_turnover_krw: float = 0.0
    weighted_return_pct: float = 0.0
    breadth_ratio: float = 0.0
    leader_concentration: float = 0.0
    leader_symbol: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    coverage_ratio: float = 0.0
    universe_member_count: int = 0
    tradable_member_count: int = 0
    observed_member_count: int = 0
    target_sample_count: int = 0
    fresh_sample_count: int = 0
    full_universe_coverage_ratio: float = 0.0
    planned_sample_coverage_ratio: float = 0.0
    breadth_trust_level: str = "LOW"
    signal_latest_at: str = ""
    signal_age_sec: float = 0.0
    cumulative_strength_score: float = 0.0
    recent_flow_score: float = 0.0
    rotation_score: float = 0.0
    signal_persistence_count: int = 0
    cohesion_passed: bool = False
    leader_only_candidate: bool = False
    signals: tuple[LiveSeedSignal, ...] = ()
    data_quality_reason: str = ""
    reason_codes: tuple[str, ...] = ()


class ThemeCohortEngine:
    def __init__(self, config: ThemeCohortConfig | None = None) -> None:
        self.config = config or ThemeCohortConfig()

    def build(
        self,
        theme_inputs: Iterable[tuple[str, str, list[ThemeMembership]]],
        seed_signals: Iterable[LiveSeedSignal],
    ) -> list[ThemeCohortSnapshot]:
        signal_by_code = {signal.normalized().code: signal.normalized() for signal in seed_signals if signal.normalized().code}
        snapshots = [
            self._build_theme(theme_id, theme_name, memberships, signal_by_code)
            for theme_id, theme_name, memberships in theme_inputs
        ]
        unmapped = [
            signal
            for code, signal in signal_by_code.items()
            if not any(normalize_stock_code(member.stock_code) == code for _, _, memberships in theme_inputs for member in memberships)
        ]
        if unmapped:
            snapshots.append(self._unmapped_snapshot(unmapped))
        return sorted(snapshots, key=lambda item: (item.cohesion_passed, item.theme_turnover_krw, item.strong_count), reverse=True)

    def _build_theme(
        self,
        theme_id: str,
        theme_name: str,
        memberships: list[ThemeMembership],
        signal_by_code: dict[str, LiveSeedSignal],
    ) -> ThemeCohortSnapshot:
        active_members = [member for member in memberships if member.active]
        signals = tuple(
            signal_by_code[code]
            for member in active_members
            for code in [normalize_stock_code(member.stock_code)]
            if code in signal_by_code
        )
        member_count = len(active_members)
        seed_count = len(signals)
        realtime_valid = [signal for signal in signals if signal.realtime_valid]
        tradable_realtime = [signal for signal in signals if signal.tradable_realtime]
        fresh_signals = [
            signal
            for signal in signals
            if signal.tradable_realtime and str(signal.freshness_status or "FRESH") in {"", "FRESH", "DEGRADED"}
        ]
        alive = [signal for signal in tradable_realtime if signal.change_rate_pct >= self.config.alive_threshold_pct]
        strong = [signal for signal in tradable_realtime if signal.change_rate_pct >= self.config.strong_threshold_pct]
        leaders = [signal for signal in tradable_realtime if signal.change_rate_pct >= self.config.leader_threshold_pct]
        denominator = member_count or 1
        target_sample_count = max(seed_count, min(member_count, len(signals))) or member_count
        full_coverage = seed_count / denominator
        planned_coverage = len(fresh_signals) / (target_sample_count or 1)
        trust = _breadth_trust_level(full_coverage, planned_coverage, seed_count)
        turnover = sum(max(0.0, signal.turnover_krw) for signal in signals)
        leader = max(tradable_realtime, key=lambda item: item.turnover_krw + item.change_rate_pct * 100_000_000, default=None)
        co_leaders = tuple(
            signal.code
            for signal in sorted(leaders, key=lambda item: (item.turnover_krw, item.change_rate_pct), reverse=True)
            if leader is None or signal.code != leader.code
        )[:2]
        cohesion = _cohesion_passed(member_count, len(strong), self.config)
        reason = ""
        reasons: list[str] = []
        if member_count <= 0:
            reason = ThemeDataWaitReason.THEME_MEMBERSHIP_MISSING.value
            reasons.append(reason)
        elif seed_count <= 0:
            reason = ThemeDataWaitReason.NO_THEME_SIGNALS.value
            reasons.append(reason)
        elif len(realtime_valid) == 0 and any(signal.tr_backfill_valid for signal in signals):
            reason = ThemeDataWaitReason.TR_BACKFILL_ONLY.value
            reasons.append(reason)
        elif len(realtime_valid) / denominator < self.config.min_realtime_valid_ratio:
            reason = ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value
            reasons.append(reason)
        if any(str(signal.freshness_status) == "STALE" for signal in signals):
            reasons.append(ThemeDataWaitReason.SIGNAL_STALE.value)
        if len(leaders) == 1 and not cohesion:
            reasons.append("SINGLE_LEADER_ONLY_CANDIDATE")
        return ThemeCohortSnapshot(
            theme_id=theme_id,
            theme_name=theme_name or theme_id,
            member_count=member_count,
            seed_member_count=seed_count,
            realtime_valid_count=len(realtime_valid),
            strong_count=len(strong),
            leader_count=len(leaders),
            alive_ratio=round(len(alive) / denominator, 4),
            strong_ratio=round(len(strong) / denominator, 4),
            leader_ratio=round(len(leaders) / denominator, 4),
            theme_turnover_krw=round(turnover, 4),
            weighted_return_pct=round(_weighted_return(signals), 4),
            breadth_ratio=round(len([signal for signal in tradable_realtime if signal.change_rate_pct > 0]) / denominator, 4),
            leader_concentration=round((max((signal.turnover_krw for signal in signals), default=0.0) / turnover) if turnover > 0 else 0.0, 4),
            leader_symbol=leader.code if leader else "",
            co_leader_symbols=co_leaders,
            coverage_ratio=round(seed_count / denominator, 4),
            universe_member_count=member_count,
            tradable_member_count=len(tradable_realtime),
            observed_member_count=seed_count,
            target_sample_count=target_sample_count,
            fresh_sample_count=len(fresh_signals),
            full_universe_coverage_ratio=round(full_coverage, 4),
            planned_sample_coverage_ratio=round(planned_coverage, 4),
            breadth_trust_level=trust,
            signal_latest_at=max((signal.last_seen_at or signal.tick_at or signal.observed_at for signal in signals), default=""),
            signal_age_sec=max((float(signal.tick_age_sec or 0.0) for signal in signals), default=0.0),
            cumulative_strength_score=round(min(100.0, len(strong) * 18.0 + len(leaders) * 12.0 + max(0.0, _weighted_return(signals)) * 4.0), 4),
            signal_persistence_count=max((len(signal.source_types) for signal in signals), default=0),
            cohesion_passed=cohesion,
            leader_only_candidate=bool(len(leaders) >= 1 and not cohesion),
            signals=signals,
            data_quality_reason=reason,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    def _unmapped_snapshot(self, signals: list[LiveSeedSignal]) -> ThemeCohortSnapshot:
        return ThemeCohortSnapshot(
            theme_id="",
            theme_name="UNMAPPED_SEED",
            seed_member_count=len(signals),
            realtime_valid_count=sum(1 for signal in signals if signal.realtime_valid),
            theme_turnover_krw=sum(max(0.0, signal.turnover_krw) for signal in signals),
            observed_member_count=len(signals),
            target_sample_count=len(signals),
            fresh_sample_count=sum(1 for signal in signals if signal.tradable_realtime),
            breadth_trust_level="UNMAPPED",
            signals=tuple(signals),
            data_quality_reason=ThemeDataWaitReason.UNMAPPED_SEED.value,
            reason_codes=(ThemeDataWaitReason.UNMAPPED_SEED.value,),
        )


def _cohesion_passed(member_count: int, strong_count: int, config: ThemeCohortConfig) -> bool:
    if member_count <= config.small_theme_member_count:
        return strong_count >= config.min_strong_count_small_theme
    return strong_count >= config.min_strong_count


def _breadth_trust_level(full_coverage: float, planned_coverage: float, seed_count: int) -> str:
    if seed_count <= 0:
        return "NO_SAMPLE"
    if planned_coverage >= 0.8 and full_coverage >= 0.35:
        return "FULL"
    if planned_coverage >= 0.5:
        return "SAMPLED"
    return "LOW_TRUST"


def _weighted_return(signals: Iterable[LiveSeedSignal]) -> float:
    numerator = 0.0
    denominator = 0.0
    for signal in signals:
        weight = math.sqrt(max(0.0, signal.turnover_krw) + 1.0)
        numerator += signal.change_rate_pct * weight
        denominator += weight
    return numerator / denominator if denominator > 0 else 0.0


__all__ = [
    "ThemeCohortConfig",
    "ThemeCohortEngine",
    "ThemeCohortSnapshot",
]
