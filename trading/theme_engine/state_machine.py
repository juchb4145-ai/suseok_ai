from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
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
    min_leading_persistence_cycles: int = 3
    min_leading_persistence_sec: int = 30
    min_leader_stability_cycles: int = 2
    min_leader_stability_sec: int = 20
    state_min_dwell_sec: int = 15
    fading_min_hold_sec: int = 30
    recovery_confirm_cycles: int = 3
    recovery_confirm_sec: int = 30
    signal_stale_grace_sec: int = 20
    leading_score_threshold: float = 70.0
    spreading_score_threshold: float = 50.0
    emerging_score_threshold: float = 35.0
    fading_score_delta: float = -15.0

    @classmethod
    def from_env(cls) -> "ThemeStateConfig":
        return cls(
            min_leading_persistence_cycles=max(1, _env_int("TRADING_THEME_MIN_LEADING_PERSISTENCE_CYCLES", cls.min_leading_persistence_cycles)),
            min_leading_persistence_sec=max(0, _env_int("TRADING_THEME_MIN_LEADING_PERSISTENCE_SEC", cls.min_leading_persistence_sec)),
            min_leader_stability_cycles=max(1, _env_int("TRADING_THEME_MIN_LEADER_STABILITY_CYCLES", cls.min_leader_stability_cycles)),
            min_leader_stability_sec=max(0, _env_int("TRADING_THEME_MIN_LEADER_STABILITY_SEC", cls.min_leader_stability_sec)),
            state_min_dwell_sec=max(0, _env_int("TRADING_THEME_STATE_MIN_DWELL_SEC", cls.state_min_dwell_sec)),
            fading_min_hold_sec=max(0, _env_int("TRADING_THEME_FADING_MIN_HOLD_SEC", cls.fading_min_hold_sec)),
            recovery_confirm_cycles=max(1, _env_int("TRADING_THEME_RECOVERY_CONFIRM_CYCLES", cls.recovery_confirm_cycles)),
            recovery_confirm_sec=max(0, _env_int("TRADING_THEME_RECOVERY_CONFIRM_SEC", cls.recovery_confirm_sec)),
            signal_stale_grace_sec=max(0, _env_int("TRADING_THEME_SIGNAL_STALE_GRACE_SEC", cls.signal_stale_grace_sec)),
        )


@dataclass(frozen=True)
class ThemeStateSnapshot:
    theme_id: str
    theme_name: str = ""
    theme_state: str = ThemeCoreState.SEED_WAIT.value
    previous_state: str = ""
    transition: str = ""
    persistence_count: int = 1
    theme_score: float = 0.0
    theme_score_delta: float = 0.0
    leader_symbol: str = ""
    previous_leader_symbol: str = ""
    co_leader_symbols: tuple[str, ...] = ()
    leader_changed: bool = False
    leader_stability_count: int = 1
    data_quality_reason: str = ""
    reason_codes: tuple[str, ...] = ()
    state_entered_at: str = ""
    state_age_sec: int = 0
    state_cycle_count: int = 1
    strong_since: str = ""
    spreading_since: str = ""
    leading_since: str = ""
    fading_since: str = ""
    recovery_pending_since: str = ""
    recovery_cycle_count: int = 0
    temporal_persistence_sec: int = 0
    leader_stability_sec: int = 0
    last_strong_at: str = ""
    last_fresh_signal_at: str = ""
    cohort: ThemeCohortSnapshot | None = None


class ThemeStateMachine:
    def __init__(self, config: ThemeStateConfig | None = None) -> None:
        self.config = config or ThemeStateConfig.from_env()
        self._previous: dict[str, ThemeStateSnapshot] = {}

    def restore(self, snapshots: Iterable[ThemeStateSnapshot]) -> None:
        self._previous = {item.theme_id: item for item in snapshots if item.theme_id}

    def apply(self, cohorts: Iterable[ThemeCohortSnapshot], now: datetime | None = None) -> list[ThemeStateSnapshot]:
        current_time = (now or datetime.now()).replace(microsecond=0)
        current = [self._apply_one(cohort, current_time) for cohort in cohorts]
        self._previous = {item.theme_id: item for item in current if item.theme_id}
        return current

    def _apply_one(self, cohort: ThemeCohortSnapshot, now: datetime) -> ThemeStateSnapshot:
        previous = self._previous.get(cohort.theme_id)
        score = _theme_score(cohort)
        raw_state, reasons = self._raw_state(cohort, score)
        state_score = score
        state_raw = raw_state
        stale_grace_active = _stale_grace_active(previous, cohort, self.config, now)
        if stale_grace_active:
            reasons.append("SIGNAL_STALE_GRACE_ACTIVE")
            if previous is not None and previous.theme_state in {ThemeCoreState.SPREADING_THEME.value, ThemeCoreState.LEADING_THEME.value}:
                state_raw = ThemeCoreState.SPREADING_THEME
                state_score = max(score, previous.theme_score)
        timing = _timing(previous, cohort, state_raw, now)
        candidate_state = self._candidate_state(previous, cohort, state_raw, state_score, timing, reasons, now)
        final_state, timing = self._apply_dwell_and_recovery(previous, candidate_state, state_raw, timing, reasons, now)
        previous_state = previous.theme_state if previous is not None else ""
        state_entered_at = previous.state_entered_at if previous is not None and previous.theme_state == final_state.value and previous.state_entered_at else now.isoformat()
        state_cycle_count = (previous.state_cycle_count + 1) if previous is not None and previous.theme_state == final_state.value else 1
        state_age_sec = _elapsed_sec(state_entered_at, now)
        leader_symbol = cohort.leader_symbol or (previous.leader_symbol if stale_grace_active and previous is not None else "")
        co_leader_symbols = cohort.co_leader_symbols or (previous.co_leader_symbols if stale_grace_active and previous is not None else ())
        leader_changed = bool(previous is not None and previous.leader_symbol and previous.leader_symbol != leader_symbol)
        return ThemeStateSnapshot(
            theme_id=cohort.theme_id,
            theme_name=cohort.theme_name,
            theme_state=final_state.value,
            previous_state=previous_state,
            transition=f"{previous_state or 'NONE'}->{final_state.value}",
            persistence_count=state_cycle_count,
            theme_score=round(state_score, 4),
            theme_score_delta=round(state_score - previous.theme_score, 4) if previous is not None else 0.0,
            leader_symbol=leader_symbol,
            previous_leader_symbol=previous.leader_symbol if previous is not None else "",
            co_leader_symbols=co_leader_symbols,
            leader_changed=leader_changed,
            leader_stability_count=timing["leader_stability_count"],
            data_quality_reason=cohort.data_quality_reason,
            reason_codes=tuple(dict.fromkeys([*cohort.reason_codes, *reasons])),
            state_entered_at=state_entered_at,
            state_age_sec=state_age_sec,
            state_cycle_count=state_cycle_count,
            strong_since=timing["strong_since"],
            spreading_since=timing["spreading_since"],
            leading_since=timing["leading_since"],
            fading_since=timing["fading_since"] if final_state == ThemeCoreState.FADING_THEME else "",
            recovery_pending_since=timing["recovery_pending_since"],
            recovery_cycle_count=timing["recovery_cycle_count"],
            temporal_persistence_sec=timing["temporal_persistence_sec"],
            leader_stability_sec=timing["leader_stability_sec"],
            last_strong_at=timing["last_strong_at"],
            last_fresh_signal_at=timing["last_fresh_signal_at"],
            cohort=cohort,
        )

    def _candidate_state(
        self,
        previous: ThemeStateSnapshot | None,
        cohort: ThemeCohortSnapshot,
        raw_state: ThemeCoreState,
        score: float,
        timing: dict[str, int | str],
        reasons: list[str],
        now: datetime,
    ) -> ThemeCoreState:
        if previous is not None and previous.theme_state in _ACTIVE_STATES:
            if _is_fading(previous, cohort, score, self.config):
                if _stale_grace_active(previous, cohort, self.config, now):
                    reasons.append("FADING_SUPPRESSED_BY_STALE_GRACE")
                else:
                    reasons.append(f"{previous.theme_state}_TO_FADING")
                    return ThemeCoreState.FADING_THEME
        if raw_state == ThemeCoreState.SPREADING_THEME and score >= self.config.leading_score_threshold:
            if _leading_confirmed(timing, self.config) and (not _stale_blocked(cohort) or _stale_grace_active(previous, cohort, self.config, now)):
                reasons.append("LEADING_PERSISTENCE_CONFIRMED")
                return ThemeCoreState.LEADING_THEME
            reasons.append("LEADING_TIME_HYSTERESIS_WAIT")
            return ThemeCoreState.SPREADING_THEME
        return raw_state

    def _apply_dwell_and_recovery(
        self,
        previous: ThemeStateSnapshot | None,
        candidate_state: ThemeCoreState,
        raw_state: ThemeCoreState,
        timing: dict[str, int | str],
        reasons: list[str],
        now: datetime,
    ) -> tuple[ThemeCoreState, dict[str, int | str]]:
        if previous is None:
            if candidate_state == ThemeCoreState.FADING_THEME:
                timing["fading_since"] = now.isoformat()
            return candidate_state, timing
        if previous.theme_state == ThemeCoreState.FADING_THEME and candidate_state in _RECOVERY_STATES:
            recovery_since = previous.recovery_pending_since or now.isoformat()
            recovery_cycles = previous.recovery_cycle_count + 1 if previous.recovery_pending_since else 1
            timing["recovery_pending_since"] = recovery_since
            timing["recovery_cycle_count"] = recovery_cycles
            if _elapsed_sec(recovery_since, now) < self.config.recovery_confirm_sec or recovery_cycles < self.config.recovery_confirm_cycles:
                reasons.append("FADING_RECOVERY_REQUIRES_CONFIRMATION")
                return ThemeCoreState.FADING_THEME, timing
            reasons.append("FADING_RECOVERY_CONFIRMED")
            timing["recovery_pending_since"] = ""
            timing["recovery_cycle_count"] = 0
        if candidate_state == ThemeCoreState.FADING_THEME:
            timing["fading_since"] = previous.fading_since or now.isoformat()
            return candidate_state, timing
        if previous.theme_state == ThemeCoreState.FADING_THEME and _elapsed_sec(previous.fading_since or previous.state_entered_at, now) < self.config.fading_min_hold_sec:
            reasons.append("FADING_MIN_HOLD_ACTIVE")
            timing["fading_since"] = previous.fading_since or previous.state_entered_at or now.isoformat()
            return ThemeCoreState.FADING_THEME, timing
        if previous.theme_state != candidate_state.value:
            previous_age = _elapsed_sec(previous.state_entered_at, now)
            if previous.theme_state in _DWELL_PROTECTED_STATES and previous_age < self.config.state_min_dwell_sec:
                reasons.append("STATE_MIN_DWELL_ACTIVE")
                if previous.theme_state == ThemeCoreState.FADING_THEME.value:
                    timing["fading_since"] = previous.fading_since or previous.state_entered_at
                return ThemeCoreState(previous.theme_state), timing
        if candidate_state == ThemeCoreState.FADING_THEME:
            timing["fading_since"] = now.isoformat()
        return candidate_state, timing

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
            return ThemeCoreState.SPREADING_THEME, ["LEADING_REQUIRES_PERSISTENCE", "LEADING_REQUIRES_TIME_HYSTERESIS"]
        if cohort.cohesion_passed and score >= self.config.spreading_score_threshold:
            return ThemeCoreState.SPREADING_THEME, []
        if cohort.strong_count > 0 and score >= self.config.emerging_score_threshold:
            return ThemeCoreState.EMERGING_THEME, []
        if cohort.seed_member_count > 0:
            return ThemeCoreState.WATCH_THEME, []
        return ThemeCoreState.WEAK_THEME, []


_ACTIVE_STATES = {
    ThemeCoreState.LEADING_THEME.value,
    ThemeCoreState.SPREADING_THEME.value,
    ThemeCoreState.LEADER_ONLY_THEME.value,
}
_RECOVERY_STATES = {
    ThemeCoreState.LEADING_THEME,
    ThemeCoreState.SPREADING_THEME,
    ThemeCoreState.LEADER_ONLY_THEME,
    ThemeCoreState.EMERGING_THEME,
}
_DWELL_PROTECTED_STATES = {
    ThemeCoreState.LEADING_THEME.value,
    ThemeCoreState.SPREADING_THEME.value,
    ThemeCoreState.LEADER_ONLY_THEME.value,
    ThemeCoreState.FADING_THEME.value,
}


def _timing(
    previous: ThemeStateSnapshot | None,
    cohort: ThemeCohortSnapshot,
    raw_state: ThemeCoreState,
    now: datetime,
) -> dict[str, int | str]:
    strong_active = cohort.strong_count > 0 and raw_state not in {ThemeCoreState.DATA_WAIT, ThemeCoreState.SEED_WAIT, ThemeCoreState.WEAK_THEME}
    spreading_active = raw_state == ThemeCoreState.SPREADING_THEME
    leading_candidate = spreading_active
    same_leader = bool(
        previous is not None
        and previous.leader_symbol
        and (
            previous.leader_symbol == cohort.leader_symbol
            or (raw_state == ThemeCoreState.SPREADING_THEME and _stale_blocked(cohort) and not cohort.leader_symbol)
        )
    )
    elapsed_since_last = _elapsed_sec(getattr(previous, "last_fresh_signal_at", "") or getattr(previous, "last_strong_at", ""), now) if previous is not None else 0
    persistence_cycles = previous.persistence_count + 1 if previous is not None and spreading_active and previous.theme_state in {ThemeCoreState.SPREADING_THEME.value, ThemeCoreState.LEADING_THEME.value} else 1
    strong_since = previous.strong_since if previous is not None and strong_active and previous.strong_since else now.isoformat() if strong_active else ""
    spreading_since = previous.spreading_since if previous is not None and spreading_active and previous.spreading_since else now.isoformat() if spreading_active else ""
    leading_since = previous.leading_since if previous is not None and leading_candidate and previous.leading_since else now.isoformat() if leading_candidate else ""
    leader_count = previous.leader_stability_count + 1 if same_leader else 1 if cohort.leader_symbol else 0
    leader_sec = (previous.leader_stability_sec + elapsed_since_last) if same_leader else 0
    last_strong = now.isoformat() if strong_active else getattr(previous, "last_strong_at", "") if previous is not None else ""
    fresh_signal = cohort.realtime_valid_count > 0 and raw_state != ThemeCoreState.DATA_WAIT and not _stale_blocked(cohort)
    last_fresh = now.isoformat() if fresh_signal else getattr(previous, "last_fresh_signal_at", "") if previous is not None else ""
    return {
        "strong_since": strong_since,
        "spreading_since": spreading_since,
        "leading_since": leading_since,
        "fading_since": getattr(previous, "fading_since", "") if previous is not None else "",
        "recovery_pending_since": "",
        "recovery_cycle_count": 0,
        "temporal_persistence_sec": _elapsed_sec(spreading_since or strong_since, now),
        "temporal_persistence_cycles": persistence_cycles,
        "leader_stability_count": leader_count,
        "leader_stability_sec": leader_sec,
        "last_strong_at": last_strong,
        "last_fresh_signal_at": last_fresh,
    }


def _leading_confirmed(timing: dict[str, int | str], config: ThemeStateConfig) -> bool:
    return (
        int(timing.get("temporal_persistence_cycles") or 0) >= config.min_leading_persistence_cycles
        and int(timing.get("temporal_persistence_sec") or 0) >= config.min_leading_persistence_sec
        and int(timing.get("leader_stability_count") or 0) >= config.min_leader_stability_cycles
        and int(timing.get("leader_stability_sec") or 0) >= config.min_leader_stability_sec
    )


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


def _stale_blocked(cohort: ThemeCohortSnapshot) -> bool:
    reasons = set(str(item) for item in tuple(cohort.reason_codes or ()))
    return bool(cohort.data_quality_reason in {ThemeDataWaitReason.TR_BACKFILL_ONLY.value, ThemeDataWaitReason.CANDLE_WARMUP.value} or "SIGNAL_STALE" in reasons)


def _stale_grace_active(
    previous: ThemeStateSnapshot | None,
    cohort: ThemeCohortSnapshot,
    config: ThemeStateConfig,
    now: datetime,
) -> bool:
    if previous is None or previous.theme_state not in _ACTIVE_STATES or not _stale_blocked(cohort):
        return False
    last_fresh = previous.last_fresh_signal_at or previous.last_strong_at
    if not last_fresh:
        return False
    return _elapsed_sec(last_fresh, now) <= config.signal_stale_grace_sec


def _elapsed_sec(timestamp: str, now: datetime) -> int:
    parsed = _parse_time(timestamp)
    if parsed is None:
        return 0
    return max(0, int((now - parsed).total_seconds()))


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "ThemeCoreState",
    "ThemeStateConfig",
    "ThemeStateMachine",
    "ThemeStateSnapshot",
]
