from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from trading.theme_engine.roles import RawStockRole, StockRoleDecision, TradeStockRole
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


@dataclass(frozen=True)
class FocusedExpansionConfig:
    max_per_theme: int = 6
    max_total: int = 30
    source: str = "reboot_v2_theme_expansion"
    subscription_ttl_sec: int = 90
    minimum_hold_sec: int = 30


@dataclass(frozen=True)
class FocusedExpansionTarget:
    code: str
    name: str = ""
    theme_id: str = ""
    theme_name: str = ""
    priority: int = 0
    source: str = "reboot_v2_theme_expansion"
    trade_role: str = ""
    raw_role: str = ""
    protected: bool = False
    subscription_ttl_sec: int = 90
    minimum_hold_sec: int = 30
    selected_at: str = ""
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FocusedExpansionPlan:
    targets: tuple[FocusedExpansionTarget, ...] = ()
    excluded: tuple[FocusedExpansionTarget, ...] = ()
    focused_expansion_count: int = 0
    source: str = "reboot_v2_theme_expansion"
    reason_codes: tuple[str, ...] = ()


class FocusedExpansionPlanner:
    def __init__(self, config: FocusedExpansionConfig | None = None) -> None:
        self.config = config or FocusedExpansionConfig()

    def plan(
        self,
        theme_states: Iterable[ThemeStateSnapshot],
        role_decisions: Iterable[StockRoleDecision],
        *,
        market_phase: str = "SELECTIVE",
        kosdaq_risk_state: str = "",
    ) -> FocusedExpansionPlan:
        state_by_theme = {state.theme_id: state for state in theme_states}
        eligible_states = {
            ThemeCoreState.EMERGING_THEME.value,
            ThemeCoreState.SPREADING_THEME.value,
            ThemeCoreState.LEADING_THEME.value,
            ThemeCoreState.LEADER_ONLY_THEME.value,
        }
        selected: list[FocusedExpansionTarget] = []
        excluded: list[FocusedExpansionTarget] = []
        counts_by_theme: dict[str, int] = {}
        reasons: list[str] = []
        for decision in sorted(role_decisions, key=_priority):
            state = state_by_theme.get(decision.theme_id)
            target = _target(decision, self.config)
            if state is None or state.theme_state not in eligible_states:
                excluded.append(_with_reason(target, "THEME_NOT_EXPANSION_ELIGIBLE"))
                continue
            if _market_is_kosdaq_risk(decision, kosdaq_risk_state):
                excluded.append(_with_reason(target, "KOSDAQ_RISK_EXPANSION_REDUCED"))
                reasons.append("KOSDAQ_RISK_EXPANSION_REDUCED")
                continue
            if decision.trade_role in {
                TradeStockRole.OVERHEATED_BLOCKED.value,
                TradeStockRole.LATE_LAGGARD_BLOCKED.value,
                TradeStockRole.WEAK_MEMBER_BLOCKED.value,
                TradeStockRole.FOLLOWER_BLOCKED_LEADER_ONLY.value,
            }:
                excluded.append(_with_reason(target, "TRADE_ROLE_BLOCKED"))
                continue
            if counts_by_theme.get(decision.theme_id, 0) >= self.config.max_per_theme:
                excluded.append(_with_reason(target, "THEME_EXPANSION_LIMIT"))
                continue
            if len(selected) >= self.config.max_total:
                excluded.append(_with_reason(target, "TOTAL_EXPANSION_LIMIT"))
                continue
            selected.append(target)
            counts_by_theme[decision.theme_id] = counts_by_theme.get(decision.theme_id, 0) + 1
        return FocusedExpansionPlan(
            targets=tuple(selected),
            excluded=tuple(excluded),
            focused_expansion_count=len(selected),
            source=self.config.source,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


def _priority(decision: StockRoleDecision) -> tuple[int, float]:
    role_priority = {
        TradeStockRole.LEADER_CONFIRMED.value: 1,
        TradeStockRole.CO_LEADER_CONFIRMED.value: 2,
        TradeStockRole.FOLLOWER_ALLOWED.value: 3,
    }.get(decision.trade_role, 9)
    raw_bonus = 0 if decision.raw_role in {RawStockRole.LEADER.value, RawStockRole.CO_LEADER.value} else 1
    return (role_priority + raw_bonus, -decision.role_score)


def _target(decision: StockRoleDecision, config: FocusedExpansionConfig) -> FocusedExpansionTarget:
    return FocusedExpansionTarget(
        code=decision.code,
        name=decision.name,
        theme_id=decision.theme_id,
        theme_name=decision.theme_name,
        priority=_priority(decision)[0],
        source=config.source,
        trade_role=decision.trade_role,
        raw_role=decision.raw_role,
        protected=False,
        subscription_ttl_sec=max(1, int(config.subscription_ttl_sec)),
        minimum_hold_sec=max(0, int(config.minimum_hold_sec)),
        reason_codes=decision.reason_codes,
    )


def _with_reason(target: FocusedExpansionTarget, reason: str) -> FocusedExpansionTarget:
    return FocusedExpansionTarget(
        **{
            **target.__dict__,
            "reason_codes": tuple(dict.fromkeys([*target.reason_codes, reason])),
        }
    )


def _market_is_kosdaq_risk(decision: StockRoleDecision, kosdaq_risk_state: str) -> bool:
    state = str(kosdaq_risk_state or "").upper()
    if state not in {"WEAK", "RISK_OFF"}:
        return False
    market = str(getattr(decision.signal, "market", "") or "").upper()
    return market == "KOSDAQ"


__all__ = [
    "FocusedExpansionConfig",
    "FocusedExpansionPlan",
    "FocusedExpansionPlanner",
    "FocusedExpansionTarget",
]
