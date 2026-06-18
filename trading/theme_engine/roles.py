from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from trading.theme_engine.signals import LiveSeedSignal
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


class RawStockRole(str, Enum):
    LEADER = "LEADER"
    CO_LEADER = "CO_LEADER"
    FOLLOWER = "FOLLOWER"
    LATE_LAGGARD = "LATE_LAGGARD"
    OVERHEATED = "OVERHEATED"
    WEAK_MEMBER = "WEAK_MEMBER"


class TradeStockRole(str, Enum):
    LEADER_CONFIRMED = "LEADER_CONFIRMED"
    CO_LEADER_CONFIRMED = "CO_LEADER_CONFIRMED"
    LEADER_CANDIDATE_DATA_WAIT = "LEADER_CANDIDATE_DATA_WAIT"
    FOLLOWER_ALLOWED = "FOLLOWER_ALLOWED"
    FOLLOWER_BLOCKED_LEADER_ONLY = "FOLLOWER_BLOCKED_LEADER_ONLY"
    LATE_LAGGARD_BLOCKED = "LATE_LAGGARD_BLOCKED"
    OVERHEATED_BLOCKED = "OVERHEATED_BLOCKED"
    WEAK_MEMBER_BLOCKED = "WEAK_MEMBER_BLOCKED"


@dataclass(frozen=True)
class StockRoleDecision:
    code: str
    name: str = ""
    theme_id: str = ""
    theme_name: str = ""
    raw_role: str = RawStockRole.WEAK_MEMBER.value
    trade_role: str = TradeStockRole.WEAK_MEMBER_BLOCKED.value
    role_score: float = 0.0
    source_rank: int = 0
    reason_codes: tuple[str, ...] = ()
    signal: LiveSeedSignal | None = None
    theme_state: ThemeStateSnapshot | None = None


class StockRoleEngine:
    def classify(
        self,
        theme_state: ThemeStateSnapshot,
        signals: Iterable[LiveSeedSignal] | None = None,
        *,
        market_phase: str = "SELECTIVE",
    ) -> list[StockRoleDecision]:
        cohort_signals = tuple(signals if signals is not None else (theme_state.cohort.signals if theme_state.cohort else ()))
        ranked = sorted(cohort_signals, key=_role_score, reverse=True)
        leader_code = theme_state.leader_symbol or (ranked[0].code if ranked else "")
        co_leaders = set(theme_state.co_leader_symbols or ())
        decisions = []
        top_return = max((signal.change_rate_pct for signal in ranked), default=0.0)
        for index, signal in enumerate(ranked, start=1):
            raw_role = _raw_role(signal, leader_code, co_leaders, top_return, index)
            trade_role, reasons = _trade_role(raw_role, theme_state, signal, market_phase)
            decisions.append(
                StockRoleDecision(
                    code=signal.code,
                    name=signal.name,
                    theme_id=theme_state.theme_id,
                    theme_name=theme_state.theme_name,
                    raw_role=raw_role.value,
                    trade_role=trade_role.value,
                    role_score=round(_role_score(signal), 4),
                    source_rank=index,
                    reason_codes=tuple(dict.fromkeys([*signal.reason_codes, *reasons])),
                    signal=signal,
                    theme_state=theme_state,
                )
            )
        return decisions


def _raw_role(signal: LiveSeedSignal, leader_code: str, co_leaders: set[str], top_return: float, rank: int) -> RawStockRole:
    if signal.vi_active or signal.upper_limit_near or signal.overheated:
        return RawStockRole.OVERHEATED
    if signal.code == leader_code:
        return RawStockRole.LEADER
    if signal.code in co_leaders or (rank <= 3 and signal.change_rate_pct >= 5.0):
        return RawStockRole.CO_LEADER
    if signal.realtime_valid and top_return >= 5.0 and signal.change_rate_pct < 3.0 and top_return - signal.change_rate_pct >= 4.0:
        return RawStockRole.LATE_LAGGARD
    if signal.change_rate_pct >= 3.0 and signal.realtime_valid:
        return RawStockRole.FOLLOWER
    return RawStockRole.WEAK_MEMBER


def _trade_role(
    raw_role: RawStockRole,
    theme_state: ThemeStateSnapshot,
    signal: LiveSeedSignal,
    market_phase: str,
) -> tuple[TradeStockRole, list[str]]:
    state = theme_state.theme_state
    if raw_role == RawStockRole.OVERHEATED:
        return TradeStockRole.OVERHEATED_BLOCKED, ["OVERHEATED_BLOCKED"]
    if raw_role == RawStockRole.LATE_LAGGARD:
        return TradeStockRole.LATE_LAGGARD_BLOCKED, ["LATE_LAGGARD_BLOCKED"]
    if raw_role == RawStockRole.WEAK_MEMBER:
        return TradeStockRole.WEAK_MEMBER_BLOCKED, ["WEAK_MEMBER_BLOCKED"]
    if state == ThemeCoreState.DATA_WAIT.value:
        return TradeStockRole.LEADER_CANDIDATE_DATA_WAIT, ["THEME_DATA_WAIT"]
    if raw_role == RawStockRole.LEADER:
        if state in {ThemeCoreState.SPREADING_THEME.value, ThemeCoreState.LEADING_THEME.value, ThemeCoreState.LEADER_ONLY_THEME.value}:
            return TradeStockRole.LEADER_CONFIRMED, ["LEADER_CONFIRMED"]
        return TradeStockRole.LEADER_CANDIDATE_DATA_WAIT, ["LEADER_WAIT_THEME_CONFIRMATION"]
    if raw_role == RawStockRole.CO_LEADER:
        if state in {ThemeCoreState.SPREADING_THEME.value, ThemeCoreState.LEADING_THEME.value, ThemeCoreState.LEADER_ONLY_THEME.value}:
            return TradeStockRole.CO_LEADER_CONFIRMED, ["CO_LEADER_CONFIRMED"]
        return TradeStockRole.LEADER_CANDIDATE_DATA_WAIT, ["CO_LEADER_WAIT_THEME_CONFIRMATION"]
    if raw_role == RawStockRole.FOLLOWER:
        if state == ThemeCoreState.LEADER_ONLY_THEME.value:
            return TradeStockRole.FOLLOWER_BLOCKED_LEADER_ONLY, ["LEADER_ONLY_THEME_FOLLOWER_BLOCKED"]
        if state in {ThemeCoreState.SPREADING_THEME.value, ThemeCoreState.LEADING_THEME.value} and str(market_phase).upper() == "EXPANSION":
            return TradeStockRole.FOLLOWER_ALLOWED, ["FOLLOWER_ALLOWED_EXPANSION"]
        return TradeStockRole.WEAK_MEMBER_BLOCKED, ["FOLLOWER_NOT_ALLOWED"]
    return TradeStockRole.WEAK_MEMBER_BLOCKED, ["ROLE_NOT_ALLOWED"]


def _role_score(signal: LiveSeedSignal) -> float:
    return (
        min(40.0, signal.turnover_krw / 500_000_000.0)
        + min(20.0, signal.turnover_speed / 100_000_000.0)
        + min(20.0, max(0.0, signal.change_rate_pct) * 2.0)
        + min(20.0, max(0.0, signal.execution_strength - 80.0) / 5.0)
    )


__all__ = [
    "RawStockRole",
    "StockRoleDecision",
    "StockRoleEngine",
    "TradeStockRole",
]
