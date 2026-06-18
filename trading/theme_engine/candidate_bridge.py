from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from trading.strategy.candidate_ingestion import CandidateSourceEvent, CandidateSourceEventType
from trading.theme_engine.roles import StockRoleDecision, TradeStockRole
from trading.theme_engine.state_machine import ThemeCoreState


@dataclass(frozen=True)
class CandidateBridgeConfig:
    source_type: str = CandidateSourceEventType.THEME_BOARD.value
    source_id_prefix: str = "theme_core_v3"
    allow_states: tuple[str, ...] = (
        ThemeCoreState.SPREADING_THEME.value,
        ThemeCoreState.LEADING_THEME.value,
        ThemeCoreState.LEADER_ONLY_THEME.value,
    )
    allow_trade_roles: tuple[str, ...] = (
        TradeStockRole.LEADER_CONFIRMED.value,
        TradeStockRole.CO_LEADER_CONFIRMED.value,
        TradeStockRole.FOLLOWER_ALLOWED.value,
    )


@dataclass(frozen=True)
class CandidateBridgeResult:
    events: tuple[CandidateSourceEvent, ...] = ()
    excluded: tuple[StockRoleDecision, ...] = ()
    output_mode: str = "OBSERVE"
    ready_allowed: bool = False
    order_intent_allowed: bool = False
    reason_codes: tuple[str, ...] = ()


class CandidateBridge:
    """Creates OBSERVE source events only after theme state and trade role are confirmed."""

    def __init__(self, config: CandidateBridgeConfig | None = None, *, clock=None) -> None:
        self.config = config or CandidateBridgeConfig()
        self.clock = clock or datetime.now

    def build_events(
        self,
        decisions: Iterable[StockRoleDecision],
        *,
        trade_date: str,
        detected_at: str | None = None,
    ) -> CandidateBridgeResult:
        timestamp = detected_at or self.clock().replace(microsecond=0).isoformat()
        events: list[CandidateSourceEvent] = []
        excluded: list[StockRoleDecision] = []
        reasons: list[str] = []
        allowed_states = set(self.config.allow_states)
        allowed_roles = set(self.config.allow_trade_roles)
        for decision in decisions:
            state = str(getattr(decision.theme_state, "theme_state", "") or "")
            if state not in allowed_states:
                excluded.append(decision)
                reasons.append("THEME_STATE_NOT_CANDIDATE_ELIGIBLE")
                continue
            if decision.trade_role not in allowed_roles:
                excluded.append(decision)
                reasons.append("TRADE_ROLE_NOT_CANDIDATE_ELIGIBLE")
                continue
            events.append(_event(decision, self.config, trade_date=trade_date, detected_at=timestamp))
        return CandidateBridgeResult(
            events=tuple(events),
            excluded=tuple(excluded),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


def _event(
    decision: StockRoleDecision,
    config: CandidateBridgeConfig,
    *,
    trade_date: str,
    detected_at: str,
) -> CandidateSourceEvent:
    theme_state = str(getattr(decision.theme_state, "theme_state", "") or "")
    return CandidateSourceEvent(
        trade_date=trade_date,
        code=decision.code,
        name=decision.name,
        source_type=config.source_type,
        source_id=f"{config.source_id_prefix}:{decision.theme_id}:{decision.code}",
        source_rank=max(0, int(decision.source_rank or 0)),
        source_score=max(0.0, float(decision.role_score or 0.0)),
        theme_id=decision.theme_id,
        theme_name=decision.theme_name,
        stock_role=decision.trade_role,
        reason_codes=[
            *list(decision.reason_codes or ()),
            "THEME_CORE_V3_CANDIDATE_BRIDGE",
            "OBSERVE_ONLY",
        ],
        raw_payload={
            "theme_state": theme_state,
            "raw_role": decision.raw_role,
            "trade_role": decision.trade_role,
            "role_score": decision.role_score,
            "ready_allowed": False,
            "order_intent_allowed": False,
        },
        detected_at=detected_at,
    )


__all__ = [
    "CandidateBridge",
    "CandidateBridgeConfig",
    "CandidateBridgeResult",
]
