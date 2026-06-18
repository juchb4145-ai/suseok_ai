from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Mapping

from trading.theme_engine.expansion import FocusedExpansionPlan
from trading.theme_engine.roles import RawStockRole, StockRoleDecision
from trading.theme_engine.state_machine import ThemeStateSnapshot


@dataclass(frozen=True)
class ThemeBoardViewSnapshot:
    trade_date: str
    calculated_at: str = ""
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False
    top_themes: tuple[dict[str, Any], ...] = ()
    leaders_by_theme: tuple[dict[str, Any], ...] = ()
    excluded_late_laggard_count: int = 0
    excluded_overheated_count: int = 0
    condition_booster_inflow_count: int = 0
    focused_expansion_count: int = 0
    data_wait_reasons: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


class ThemeBoardView:
    """Dashboard/API projection for Theme Core V3 calculation outputs."""

    def build(
        self,
        *,
        trade_date: str,
        calculated_at: str = "",
        theme_states: Iterable[ThemeStateSnapshot],
        role_decisions: Iterable[StockRoleDecision],
        expansion_plan: FocusedExpansionPlan | None = None,
    ) -> ThemeBoardViewSnapshot:
        states = sorted(list(theme_states), key=lambda item: item.theme_score, reverse=True)
        decisions = list(role_decisions)
        expansion_plan = expansion_plan or FocusedExpansionPlan()
        condition_count = sum(
            1
            for decision in decisions
            for source in list(getattr(decision.signal, "source_types", ()) or ())
            if str(source) == "condition_include"
        )
        data_wait = Counter(state.data_quality_reason for state in states if state.data_quality_reason)
        return ThemeBoardViewSnapshot(
            trade_date=trade_date,
            calculated_at=calculated_at,
            top_themes=tuple(_theme_item(state, index) for index, state in enumerate(states[:5], start=1)),
            leaders_by_theme=tuple(_leaders_for_state(state, decisions) for state in states[:5]),
            excluded_late_laggard_count=sum(1 for decision in decisions if decision.raw_role == RawStockRole.LATE_LAGGARD.value),
            excluded_overheated_count=sum(1 for decision in decisions if decision.raw_role == RawStockRole.OVERHEATED.value),
            condition_booster_inflow_count=condition_count,
            focused_expansion_count=expansion_plan.focused_expansion_count,
            data_wait_reasons=dict(data_wait),
            source_counts=dict(
                Counter(
                    source
                    for decision in decisions
                    for source in list(getattr(decision.signal, "source_types", ()) or ())
                    if str(source)
                )
            ),
            reason_codes=tuple(dict.fromkeys([*list(expansion_plan.reason_codes), "THEME_BOARD_VIEW_OBSERVE_ONLY"])),
        )


def _theme_item(state: ThemeStateSnapshot, rank: int) -> dict[str, Any]:
    cohort = state.cohort
    return {
        "rank": rank,
        "theme_id": state.theme_id,
        "theme_name": state.theme_name,
        "theme_state": state.theme_state,
        "theme_score": state.theme_score,
        "strong_count": getattr(cohort, "strong_count", 0) if cohort is not None else 0,
        "leader_count": getattr(cohort, "leader_count", 0) if cohort is not None else 0,
        "theme_turnover_krw": getattr(cohort, "theme_turnover_krw", 0.0) if cohort is not None else 0.0,
        "leader_symbol": state.leader_symbol,
        "co_leader_symbols": list(state.co_leader_symbols),
        "data_quality_reason": state.data_quality_reason,
    }


def _leaders_for_state(state: ThemeStateSnapshot, decisions: list[StockRoleDecision]) -> dict[str, Any]:
    leaders = [
        decision
        for decision in decisions
        if decision.theme_id == state.theme_id
        and decision.raw_role in {RawStockRole.LEADER.value, RawStockRole.CO_LEADER.value}
    ]
    return {
        "theme_id": state.theme_id,
        "theme_name": state.theme_name,
        "leaders": [
            {
                "code": decision.code,
                "name": decision.name,
                "raw_role": decision.raw_role,
                "trade_role": decision.trade_role,
                "role_score": decision.role_score,
            }
            for decision in sorted(leaders, key=lambda item: item.role_score, reverse=True)
        ],
    }


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


__all__ = [
    "ThemeBoardView",
    "ThemeBoardViewSnapshot",
]
