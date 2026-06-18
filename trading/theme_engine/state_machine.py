from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from trading.theme_engine.cohort import ThemeCohortSnapshot
from trading.theme_engine.signals import ThemeDataWaitReason


class ThemeCoreState(str, Enum):
    UNIVERSE_EMPTY = "UNIVERSE_EMPTY"
    SEED_WAIT = "SEED_WAIT"
    DATA_WAIT = "DATA_WAIT"
    WATCH_THEME = "WATCH_THEME"
    EMERGING_THEME = "EMERGING_THEME"
    LEADER_ONLY_THEME = "LEADER_ONLY_THEME"
    SPREADING_THEME = "SPREADING_THEME"
    LEADING_THEME = "LEADING_THEME"
    FADING_THEME = "FADING_THEME"
    WEAK_THEME = "WEAK_THEME"


@dataclass(frozen=True)
class ThemeStateConfig:
    min_leading_persistence_cycles: int = 2
    leading_score_threshold: float = 70.0
    spreading_score_threshold: float = 50.0
    emerging_score_threshold: float = 35.0
    fading_score_delta: float = -15.0


@dataclass(frozen=True)
class ThemeStateSnapshot:
    theme_id: str
    theme_name: str = ""
    theme_state: str = ThemeCoreState.SEED_WAIT.value
    previous_state: str = ""
    transition: str = ""
    persistence_count: int = 1
    theme_score: float = 0.0
    leader_symbol: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    leader_changed: bool = False
    data_quality_reason: str = ""
    reason_codes: tuple[str, ...] = ()
    cohort: ThemeCohortSnapshot | None = None


class ThemeStateMachine:
    def __init__(self, config: ThemeStateConfig | None = None) -> None:
        self.config = config or ThemeStateConfig()
        self._previous: dict[str, ThemeStateSnapshot] = {}

    def apply(self, cohorts: Iterable[ThemeCohortSnapshot]) -> list[ThemeStateSnapshot]:
        current = [self._apply_one(cohort) for cohort in cohorts]
        self._previous = {item.theme_id: item for item in current if item.theme_id}
        return current

    def _apply_one(self, cohort: ThemeCohortSnapshot) -> ThemeStateSnapshot:
        previous = self._previous.get(cohort.theme_id)
        score = _theme_score(cohort)
        raw_state, reasons = self._raw_state(cohort, score)
        if previous is not None and previous.theme_state == ThemeCoreState.LEADING_THEME.value:
            if _is_fading(previous, cohort, score, self.config):
                raw_state = ThemeCoreState.FADING_THEME
                reasons.append("LEADING_TO_FADING")
        persistence = 1
        if previous is not None:
            if previous.theme_state == raw_state.value:
                persistence = previous.persistence_count + 1
            elif raw_state in {ThemeCoreState.SPREADING_THEME, ThemeCoreState.EMERGING_THEME} and previous.theme_state in {
                ThemeCoreState.SPREADING_THEME.value,
                ThemeCoreState.EMERGING_THEME.value,
                ThemeCoreState.LEADING_THEME.value,
            }:
                persistence = previous.persistence_count + 1
        final_state = raw_state
        if raw_state == ThemeCoreState.SPREADING_THEME and persistence >= self.config.min_leading_persistence_cycles and score >= self.config.leading_score_threshold:
            final_state = ThemeCoreState.LEADING_THEME
            reasons.append("LEADING_PERSISTENCE_CONFIRMED")
        previous_state = previous.theme_state if previous is not None else ""
        leader_changed = bool(previous is not None and previous.leader_symbol and previous.leader_symbol != cohort.leader_symbol)
        return ThemeStateSnapshot(
            theme_id=cohort.theme_id,
            theme_name=cohort.theme_name,
            theme_state=final_state.value,
            previous_state=previous_state,
            transition=f"{previous_state or 'NONE'}->{final_state.value}",
            persistence_count=persistence,
            theme_score=round(score, 4),
            leader_symbol=cohort.leader_symbol,
            co_leader_symbols=cohort.co_leader_symbols,
            leader_changed=leader_changed,
            data_quality_reason=cohort.data_quality_reason,
            reason_codes=tuple(dict.fromkeys([*cohort.reason_codes, *reasons])),
            cohort=cohort,
        )

    def _raw_state(self, cohort: ThemeCohortSnapshot, score: float) -> tuple[ThemeCoreState, list[str]]:
        reasons: list[str] = []
        if cohort.member_count <= 0 and not cohort.theme_id:
            return ThemeCoreState.UNIVERSE_EMPTY, [ThemeDataWaitReason.UNMAPPED_SEED.value]
        if cohort.member_count <= 0:
            return ThemeCoreState.UNIVERSE_EMPTY, [ThemeDataWaitReason.THEME_MEMBERSHIP_MISSING.value]
        if cohort.seed_member_count <= 0:
            return ThemeCoreState.SEED_WAIT, [ThemeDataWaitReason.SEED_WAIT.value]
        if cohort.data_quality_reason in {
            ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value,
            ThemeDataWaitReason.TR_BACKFILL_ONLY.value,
            ThemeDataWaitReason.CANDLE_WARMUP.value,
        }:
            return ThemeCoreState.DATA_WAIT, [cohort.data_quality_reason]
        if cohort.leader_only_candidate:
            return ThemeCoreState.LEADER_ONLY_THEME, ["LEADER_ONLY_COHORT"]
        if cohort.cohesion_passed and score >= self.config.leading_score_threshold:
            return ThemeCoreState.SPREADING_THEME, ["LEADING_REQUIRES_PERSISTENCE"]
        if cohort.cohesion_passed and score >= self.config.spreading_score_threshold:
            return ThemeCoreState.SPREADING_THEME, []
        if cohort.strong_count > 0 and score >= self.config.emerging_score_threshold:
            return ThemeCoreState.EMERGING_THEME, []
        if cohort.seed_member_count > 0:
            return ThemeCoreState.WATCH_THEME, []
        return ThemeCoreState.WEAK_THEME, []


def _theme_score(cohort: ThemeCohortSnapshot) -> float:
    return max(
        0.0,
        min(
            100.0,
            25.0 * min(1.0, cohort.theme_turnover_krw / 20_000_000_000.0)
            + 25.0 * min(1.0, cohort.strong_ratio * 2.0)
            + 20.0 * min(1.0, cohort.leader_count / 3.0)
            + 15.0 * min(1.0, max(0.0, cohort.weighted_return_pct + 1.0) / 8.0)
            + 15.0 * min(1.0, cohort.coverage_ratio),
        ),
    )


def _is_fading(
    previous: ThemeStateSnapshot,
    cohort: ThemeCohortSnapshot,
    score: float,
    config: ThemeStateConfig,
) -> bool:
    if score - previous.theme_score <= config.fading_score_delta:
        return True
    previous_leaders = 1 if previous.leader_symbol else 0
    if cohort.leader_count < previous_leaders:
        return True
    return cohort.weighted_return_pct <= 0


__all__ = [
    "ThemeCoreState",
    "ThemeStateConfig",
    "ThemeStateMachine",
    "ThemeStateSnapshot",
]
