from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from trading.strategy.candidate_ingestion import CandidateSourceEvent, CandidateSourceEventType
from trading.theme_engine.roles import StockRoleDecision


@dataclass(frozen=True)
class CandidateBridgeSourceState:
    code: str
    theme_id: str
    source_id: str
    trade_role: str = ""
    leadership_status: str = ""
    role_score: float = 0.0


@dataclass(frozen=True)
class CandidateBridgeReconcileResult:
    include_events: tuple[CandidateSourceEvent, ...] = ()
    remove_events: tuple[CandidateSourceEvent, ...] = ()
    active_state: tuple[CandidateBridgeSourceState, ...] = ()
    included_count: int = 0
    removed_count: int = 0
    unchanged_count: int = 0
    reason_codes: tuple[str, ...] = ()


class CandidateBridgeSourceReconciler:
    def __init__(self, *, source_type: str = CandidateSourceEventType.THEME_BOARD.value, source_id_prefix: str = "theme_core_v3") -> None:
        self.source_type = source_type
        self.source_id_prefix = source_id_prefix
        self._active: dict[tuple[str, str], CandidateBridgeSourceState] = {}

    def reconcile(
        self,
        decisions: Iterable[StockRoleDecision],
        *,
        trade_date: str,
        detected_at: str | None = None,
    ) -> CandidateBridgeReconcileResult:
        timestamp = detected_at or datetime.now().replace(microsecond=0).isoformat()
        eligible = {_key_from_decision(decision): _state_from_decision(decision, prefix=self.source_id_prefix) for decision in decisions if _eligible(decision)}
        include: list[CandidateSourceEvent] = []
        remove: list[CandidateSourceEvent] = []
        unchanged = 0
        for key, state in eligible.items():
            previous = self._active.get(key)
            if previous == state:
                unchanged += 1
                continue
            include.append(_event_from_state(state, trade_date=trade_date, detected_at=timestamp, source_type=self.source_type, event_type="include"))
        for key, previous in list(self._active.items()):
            if key in eligible:
                continue
            remove.append(_event_from_state(previous, trade_date=trade_date, detected_at=timestamp, source_type=self.source_type, event_type="remove"))
        self._active = dict(eligible)
        return CandidateBridgeReconcileResult(
            include_events=tuple(include),
            remove_events=tuple(remove),
            active_state=tuple(sorted(self._active.values(), key=lambda item: (item.theme_id, item.code))),
            included_count=len(include),
            removed_count=len(remove),
            unchanged_count=unchanged,
            reason_codes=("CANDIDATE_BRIDGE_RECONCILE_OBSERVE_ONLY",),
        )


def _eligible(decision: StockRoleDecision) -> bool:
    if decision.trade_role not in {"LEADER_CONFIRMED", "CO_LEADER_CONFIRMED", "FOLLOWER_ALLOWED"}:
        return False
    state = str(getattr(decision.theme_state, "theme_state", "") or "")
    return state in {"LEADING_THEME", "SPREADING_THEME", "LEADER_ONLY_THEME"}


def _key_from_decision(decision: StockRoleDecision) -> tuple[str, str]:
    return (decision.code, decision.theme_id)


def _state_from_decision(decision: StockRoleDecision, *, prefix: str) -> CandidateBridgeSourceState:
    status = str(getattr(decision.theme_state, "theme_state", "") or "")
    return CandidateBridgeSourceState(
        code=decision.code,
        theme_id=decision.theme_id,
        source_id=f"{prefix}:{decision.theme_id}:{decision.code}",
        trade_role=decision.trade_role,
        leadership_status=status,
        role_score=decision.role_score,
    )


def _event_from_state(
    state: CandidateBridgeSourceState,
    *,
    trade_date: str,
    detected_at: str,
    source_type: str,
    event_type: str,
) -> CandidateSourceEvent:
    reason = "THEME_CORE_V3_CANDIDATE_BRIDGE" if event_type == "include" else "THEME_NO_LONGER_ELIGIBLE"
    return CandidateSourceEvent(
        trade_date=trade_date,
        code=state.code,
        source_type=source_type,
        source_id=state.source_id,
        source_score=state.role_score,
        theme_id=state.theme_id,
        stock_role=state.trade_role,
        reason_codes=[reason, "OBSERVE_ONLY"],
        raw_payload={
            "event_type": event_type,
            "trade_role": state.trade_role,
            "leadership_status": state.leadership_status,
            "ready_allowed": False,
            "order_intent_allowed": False,
        },
        detected_at=detected_at,
    )


__all__ = [
    "CandidateBridgeReconcileResult",
    "CandidateBridgeSourceReconciler",
    "CandidateBridgeSourceState",
]
