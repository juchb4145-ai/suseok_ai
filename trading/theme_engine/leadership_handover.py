from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot
from trading.theme_engine.turnover_flow import ThemeTurnoverFlow


class ThemeLeadershipStatus(str, Enum):
    NEUTRAL = "NEUTRAL"
    INCUMBENT = "INCUMBENT"
    CHALLENGER = "CHALLENGER"
    TAKEOVER_PENDING = "TAKEOVER_PENDING"
    TAKEOVER_CONFIRMED = "TAKEOVER_CONFIRMED"
    LOSING_LEADERSHIP = "LOSING_LEADERSHIP"
    ROTATED_OUT = "ROTATED_OUT"


@dataclass(frozen=True)
class ThemeLeadershipSnapshot:
    theme_id: str
    theme_name: str = ""
    current_rank: int = 0
    previous_rank: int = 0
    rank_delta: int = 0
    status: str = ThemeLeadershipStatus.NEUTRAL.value
    base_strength_score: float = 0.0
    recent_flow_score: float = 0.0
    leadership_score: float = 0.0
    flow_share: float = 0.0
    flow_share_delta: float = 0.0
    persistence_sec: int = 0
    incumbent_since: str = ""
    challenger_since: str = ""
    takeover_pending_since: str = ""
    takeover_confirmed_at: str = ""
    previous_incumbent_theme_id: str = ""
    handover_reason_codes: tuple[str, ...] = ()
    theme_state: ThemeStateSnapshot | None = None
    flow: ThemeTurnoverFlow | None = None


@dataclass(frozen=True)
class ThemeLeadershipTransition:
    theme_id: str
    previous_status: str
    current_status: str
    detected_at: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeadershipHandoverConfig:
    min_confirm_sec: int = 30
    min_confirm_cycles: int = 3
    min_score_advantage: float = 5.0
    min_flow_share_advantage: float = 0.03
    incumbent_grace_sec: int = 60
    rotated_out_cooldown_sec: int = 60


class ThemeLeadershipRanker:
    def rank(
        self,
        theme_states: Iterable[ThemeStateSnapshot],
        *,
        flows: dict[str, ThemeTurnoverFlow] | None = None,
        previous: dict[str, ThemeLeadershipSnapshot] | None = None,
    ) -> list[ThemeLeadershipSnapshot]:
        flows = flows or {}
        previous = previous or {}
        snapshots: list[ThemeLeadershipSnapshot] = []
        for state in theme_states:
            flow = flows.get(state.theme_id)
            base = _base_strength_score(state)
            recent = _recent_flow_score(flow)
            score = _leadership_score(state, base, recent, flow)
            prev = previous.get(state.theme_id)
            snapshots.append(
                ThemeLeadershipSnapshot(
                    theme_id=state.theme_id,
                    theme_name=state.theme_name,
                    previous_rank=int(prev.current_rank or 0) if prev else 0,
                    base_strength_score=round(base, 4),
                    recent_flow_score=round(recent, 4),
                    leadership_score=round(score, 4),
                    flow_share=round(float(getattr(flow, "theme_flow_share", 0.0) or 0.0), 6),
                    flow_share_delta=round(float(getattr(flow, "theme_flow_share_delta", 0.0) or 0.0), 6),
                    handover_reason_codes=tuple(state.reason_codes),
                    theme_state=state,
                    flow=flow,
                )
            )
        ranked = sorted(snapshots, key=lambda item: (item.leadership_score, item.flow_share, item.base_strength_score), reverse=True)
        return [
            replace(item, current_rank=index, rank_delta=(item.previous_rank - index if item.previous_rank else 0))
            for index, item in enumerate(ranked, start=1)
        ]


class LeadershipHandoverEngine:
    def __init__(self, config: LeadershipHandoverConfig | None = None) -> None:
        self.config = config or LeadershipHandoverConfig()
        self._previous: dict[str, ThemeLeadershipSnapshot] = {}
        self._incumbent_theme_id = ""

    def restore(self, snapshots: Iterable[ThemeLeadershipSnapshot]) -> None:
        self._previous = {item.theme_id: item for item in snapshots if item.theme_id}
        incumbent = next((item for item in snapshots if item.status in {ThemeLeadershipStatus.INCUMBENT.value, ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value}), None)
        self._incumbent_theme_id = incumbent.theme_id if incumbent else ""

    def apply(self, ranked: Iterable[ThemeLeadershipSnapshot], *, now: datetime) -> tuple[list[ThemeLeadershipSnapshot], list[ThemeLeadershipTransition]]:
        current_time = now.replace(microsecond=0)
        items = list(ranked)
        previous_incumbent = self._incumbent_theme_id
        incumbent = self._previous.get(previous_incumbent) if previous_incumbent else None
        top = items[0] if items else None
        updated: list[ThemeLeadershipSnapshot] = []
        transitions: list[ThemeLeadershipTransition] = []
        takeover_theme = ""
        if top is not None and _can_takeover(top):
            if not previous_incumbent:
                takeover_theme = top.theme_id
            elif top.theme_id == previous_incumbent:
                takeover_theme = top.theme_id
            elif _challenger_advantage(top, incumbent, self.config):
                takeover_theme = top.theme_id
        for item in items:
            previous = self._previous.get(item.theme_id)
            status = self._status(item, previous, takeover_theme, previous_incumbent, current_time)
            reasons = _handover_reasons(item, previous, status)
            entered_at = _entered_at(status, item, previous, current_time)
            updated_item = replace(
                item,
                status=status,
                persistence_sec=_persistence_sec(previous, status, current_time),
                incumbent_since=entered_at if status in {ThemeLeadershipStatus.INCUMBENT.value, ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value} else item.incumbent_since,
                challenger_since=entered_at if status == ThemeLeadershipStatus.CHALLENGER.value else item.challenger_since,
                takeover_pending_since=entered_at if status == ThemeLeadershipStatus.TAKEOVER_PENDING.value else item.takeover_pending_since,
                takeover_confirmed_at=current_time.isoformat() if status == ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value and getattr(previous, "status", "") != status else item.takeover_confirmed_at,
                previous_incumbent_theme_id=previous_incumbent,
                handover_reason_codes=tuple(_dedupe([*item.handover_reason_codes, *reasons])),
            )
            if previous is not None and previous.status != updated_item.status:
                transitions.append(
                    ThemeLeadershipTransition(
                        theme_id=item.theme_id,
                        previous_status=previous.status,
                        current_status=updated_item.status,
                        detected_at=current_time.isoformat(),
                        reason_codes=updated_item.handover_reason_codes,
                    )
                )
            updated.append(updated_item)
        confirmed = next((item for item in updated if item.status == ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value), None)
        incumbent_item = next((item for item in updated if item.status == ThemeLeadershipStatus.INCUMBENT.value), None)
        if confirmed is not None:
            self._incumbent_theme_id = confirmed.theme_id
        elif incumbent_item is not None:
            self._incumbent_theme_id = incumbent_item.theme_id
        self._previous = {item.theme_id: item for item in updated if item.theme_id}
        return updated, transitions

    def _status(
        self,
        item: ThemeLeadershipSnapshot,
        previous: ThemeLeadershipSnapshot | None,
        takeover_theme: str,
        previous_incumbent: str,
        now: datetime,
    ) -> str:
        state = str(getattr(item.theme_state, "theme_state", "") or "")
        if state in {ThemeCoreState.DATA_WAIT.value, ThemeCoreState.SEED_WAIT.value, ThemeCoreState.WEAK_THEME.value}:
            return ThemeLeadershipStatus.ROTATED_OUT.value if previous and previous.status == ThemeLeadershipStatus.LOSING_LEADERSHIP.value else ThemeLeadershipStatus.NEUTRAL.value
        if item.theme_id == takeover_theme and (not previous_incumbent or item.theme_id == previous_incumbent):
            return ThemeLeadershipStatus.INCUMBENT.value
        if item.theme_id == takeover_theme:
            if previous is not None and previous.status == ThemeLeadershipStatus.TAKEOVER_PENDING.value:
                if previous.persistence_sec >= self.config.min_confirm_sec or _cycle_count(previous) >= self.config.min_confirm_cycles:
                    return ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value
            return ThemeLeadershipStatus.TAKEOVER_PENDING.value
        if item.theme_id == previous_incumbent:
            if _flow_collapse(item):
                return ThemeLeadershipStatus.LOSING_LEADERSHIP.value
            return ThemeLeadershipStatus.INCUMBENT.value
        if item.current_rank <= 3 and _can_takeover(item):
            return ThemeLeadershipStatus.CHALLENGER.value
        return ThemeLeadershipStatus.NEUTRAL.value


def _leadership_score(state: ThemeStateSnapshot, base: float, recent: float, flow: ThemeTurnoverFlow | None) -> float:
    concentration_penalty = max(0.0, (float(getattr(state.cohort, "leader_concentration", 0.0) or 0.0) - 0.75) * 20.0)
    data_penalty = 15.0 if state.theme_state == ThemeCoreState.DATA_WAIT.value else 0.0
    return max(0.0, min(100.0, 0.55 * base + 0.35 * recent + 10.0 * max(0.0, float(getattr(flow, "theme_flow_share_delta", 0.0) or 0.0)) - concentration_penalty - data_penalty))


def _base_strength_score(state: ThemeStateSnapshot) -> float:
    cohort = state.cohort
    if cohort is None:
        return float(state.theme_score or 0.0)
    return max(0.0, min(100.0, float(state.theme_score or 0.0) + min(10.0, float(cohort.cumulative_strength_score or 0.0) / 10.0)))


def _recent_flow_score(flow: ThemeTurnoverFlow | None) -> float:
    if flow is None:
        return 0.0
    return max(0.0, min(100.0, float(flow.theme_flow_percentile or 0.0) + max(0.0, float(flow.theme_flow_share_delta or 0.0) * 100.0)))


def _can_takeover(item: ThemeLeadershipSnapshot) -> bool:
    state = str(getattr(item.theme_state, "theme_state", "") or "")
    return state not in {ThemeCoreState.DATA_WAIT.value, ThemeCoreState.SEED_WAIT.value, ThemeCoreState.WEAK_THEME.value} and item.leadership_score > 0


def _challenger_advantage(top: ThemeLeadershipSnapshot, incumbent: ThemeLeadershipSnapshot | None, config: LeadershipHandoverConfig) -> bool:
    if incumbent is None:
        return True
    return (
        top.leadership_score - incumbent.leadership_score >= config.min_score_advantage
        and top.flow_share - incumbent.flow_share >= config.min_flow_share_advantage
    )


def _flow_collapse(item: ThemeLeadershipSnapshot) -> bool:
    return item.flow_share_delta <= -0.05 or item.recent_flow_score <= 5.0


def _persistence_sec(previous: ThemeLeadershipSnapshot | None, status: str, now: datetime) -> int:
    if previous is None or previous.status != status:
        return 0
    return int(previous.persistence_sec or 0) + max(1, _seconds_since(previous, status, now))


def _cycle_count(previous: ThemeLeadershipSnapshot) -> int:
    return max(1, int(previous.persistence_sec or 0))


def _seconds_since(previous: ThemeLeadershipSnapshot, status: str, now: datetime) -> int:
    start = ""
    if status == ThemeLeadershipStatus.TAKEOVER_PENDING.value:
        start = previous.takeover_pending_since
    elif status == ThemeLeadershipStatus.CHALLENGER.value:
        start = previous.challenger_since
    elif status in {ThemeLeadershipStatus.INCUMBENT.value, ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value}:
        start = previous.incumbent_since
    parsed = _parse_time(start)
    if parsed is None:
        return 1
    return max(1, int((now - parsed).total_seconds()))


def _entered_at(status: str, item: ThemeLeadershipSnapshot, previous: ThemeLeadershipSnapshot | None, now: datetime) -> str:
    if previous is not None and previous.status == status:
        if status == ThemeLeadershipStatus.TAKEOVER_PENDING.value:
            return previous.takeover_pending_since
        if status == ThemeLeadershipStatus.CHALLENGER.value:
            return previous.challenger_since
        if status in {ThemeLeadershipStatus.INCUMBENT.value, ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value}:
            return previous.incumbent_since
    return now.isoformat()


def _handover_reasons(item: ThemeLeadershipSnapshot, previous: ThemeLeadershipSnapshot | None, status: str) -> list[str]:
    reasons = []
    if previous is not None and item.current_rank != previous.current_rank:
        reasons.append("THEME_RANK_CHANGED")
    if status == ThemeLeadershipStatus.TAKEOVER_PENDING.value:
        reasons.append("TAKEOVER_REQUIRES_CONFIRMATION")
    if status == ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value:
        reasons.append("TAKEOVER_CONFIRMED")
    if status == ThemeLeadershipStatus.LOSING_LEADERSHIP.value:
        reasons.append("INCUMBENT_FLOW_COLLAPSE")
    return reasons


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "LeadershipHandoverConfig",
    "LeadershipHandoverEngine",
    "ThemeLeadershipRanker",
    "ThemeLeadershipSnapshot",
    "ThemeLeadershipStatus",
    "ThemeLeadershipTransition",
]
