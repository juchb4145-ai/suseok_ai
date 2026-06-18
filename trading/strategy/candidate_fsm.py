from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from trading.runtime_ports import CandidateRuntimeState, CandidateStateTransition
from trading.strategy.market_data_service import MarketDataService
from trading.strategy.models import Candidate, CandidateState


class CandidateBlockingStage(str, Enum):
    NONE = "NONE"
    DATA = "DATA"
    THEME = "THEME"
    MARKET = "MARKET"
    ROLE = "ROLE"
    PRICE = "PRICE"
    RISK = "RISK"
    ORDER = "ORDER"
    SYSTEM = "SYSTEM"


class CandidateReasonCode(str, Enum):
    LATEST_TICK_MISSING = "LATEST_TICK_MISSING"
    LATEST_TICK_STALE = "LATEST_TICK_STALE"
    TR_BACKFILL_PRICE_ONLY = "TR_BACKFILL_PRICE_ONLY"
    HYDRATION_PENDING = "HYDRATION_PENDING"
    THEME_NOT_READY = "THEME_NOT_READY"
    MARKET_RISK_OFF = "MARKET_RISK_OFF"
    ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
    PRICE_TIMING_NOT_READY = "PRICE_TIMING_NOT_READY"
    CHASE_RISK = "CHASE_RISK"
    VWAP_OVEREXTENDED = "VWAP_OVEREXTENDED"
    ORDER_RISK_BLOCKED = "ORDER_RISK_BLOCKED"
    GATEWAY_UNHEALTHY = "GATEWAY_UNHEALTHY"
    CONDITION_INCLUDE = "CONDITION_INCLUDE"
    CONDITION_REMOVE = "CONDITION_REMOVE"
    HYDRATION_REQUESTED = "HYDRATION_REQUESTED"
    HYDRATION_RESULT = "HYDRATION_RESULT"
    REALTIME_TICK_FRESH = "REALTIME_TICK_FRESH"
    OBSERVE_READY_ORDER_DISABLED = "OBSERVE_READY_ORDER_DISABLED"


NEXT_REQUIRED_ACTION = {
    CandidateReasonCode.LATEST_TICK_MISSING.value: "WAIT_REALTIME_TICK",
    CandidateReasonCode.LATEST_TICK_STALE.value: "WAIT_FRESH_REALTIME_TICK",
    CandidateReasonCode.TR_BACKFILL_PRICE_ONLY.value: "WAIT_REALTIME_TICK",
    CandidateReasonCode.HYDRATION_PENDING.value: "WAIT_TR_BACKFILL",
    CandidateReasonCode.THEME_NOT_READY.value: "WAIT_THEME_STRENGTH",
    CandidateReasonCode.MARKET_RISK_OFF.value: "WAIT_MARKET_RECOVERY",
    CandidateReasonCode.ROLE_NOT_ALLOWED.value: "WAIT_ROLE_ALLOWED",
    CandidateReasonCode.PRICE_TIMING_NOT_READY.value: "WAIT_PRICE_OR_VWAP_RECOVERY",
    CandidateReasonCode.ORDER_RISK_BLOCKED.value: "CHECK_ORDER_RISK",
    CandidateReasonCode.GATEWAY_UNHEALTHY.value: "CHECK_GATEWAY_HEALTH",
    CandidateReasonCode.OBSERVE_READY_ORDER_DISABLED.value: "WAIT_ORDER_MANAGER_PR",
}


LEGACY_TO_V2_STATE = {
    CandidateState.DETECTED: CandidateRuntimeState.DISCOVERED,
    CandidateState.HYDRATING: CandidateRuntimeState.HYDRATING,
    CandidateState.WATCHING: CandidateRuntimeState.WATCHING,
    CandidateState.WAIT_DATA: CandidateRuntimeState.WATCHING,
    CandidateState.READY: CandidateRuntimeState.TIMING_READY,
    CandidateState.ORDER_DECIDED: CandidateRuntimeState.ORDER_INTENT_CREATED,
    CandidateState.ORDER_SENT: CandidateRuntimeState.ORDER_PENDING,
    CandidateState.FILLED: CandidateRuntimeState.POSITION_OPEN,
    CandidateState.BLOCKED: CandidateRuntimeState.WATCHING,
    CandidateState.EXPIRED: CandidateRuntimeState.EXPIRED,
    CandidateState.REMOVED: CandidateRuntimeState.REMOVED,
    CandidateState.CANCELLED: CandidateRuntimeState.CLOSED,
}


@dataclass(frozen=True)
class CandidateFsmSnapshot:
    candidate_id: str
    trade_date: str
    code: str
    v2_state: str
    blocking_stage: str = CandidateBlockingStage.NONE.value
    primary_reason_code: str = ""
    reason_codes: tuple[str, ...] = ()
    next_required_action: str = ""
    source_event_ids: tuple[str, ...] = ()
    last_transition_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason_codes"] = list(self.reason_codes)
        data["source_event_ids"] = list(self.source_event_ids)
        return data


@dataclass
class CandidateFsmService:
    db: Any | None = None
    clock: Any = datetime.now

    def on_condition_include(self, event_or_payload: Any) -> CandidateStateTransition | None:
        candidate = self._candidate_from_input(event_or_payload)
        if candidate is None:
            return None
        payload = self._payload(event_or_payload)
        return self.promote(
            candidate,
            CandidateRuntimeState.DISCOVERED,
            CandidateReasonCode.CONDITION_INCLUDE.value,
            source_event_id=str(payload.get("source_event_id") or payload.get("event_id") or ""),
            source_event_type="condition_include",
            source_component="CandidateIngestionService",
            details=payload,
        )

    def on_condition_remove(self, event_or_payload: Any) -> CandidateStateTransition | None:
        candidate = self._candidate_from_input(event_or_payload)
        if candidate is None:
            return None
        payload = self._payload(event_or_payload)
        target = CandidateRuntimeState.REMOVED if candidate.state == CandidateState.REMOVED else self.v2_state(candidate)
        return self.promote(
            candidate,
            target,
            CandidateReasonCode.CONDITION_REMOVE.value,
            source_event_id=str(payload.get("source_event_id") or payload.get("event_id") or ""),
            source_event_type="condition_remove",
            source_component="CandidateIngestionService",
            details=payload,
        )

    def on_hydration_requested(self, candidate: Candidate) -> CandidateStateTransition:
        return self.promote(
            candidate,
            CandidateRuntimeState.HYDRATING,
            CandidateReasonCode.HYDRATION_REQUESTED.value,
            blocking_stage=CandidateBlockingStage.DATA,
            source_event_type="hydration_request",
            source_component="CandidateHydrator",
        )

    def on_hydration_result(self, candidate: Candidate, result: Mapping[str, Any] | None = None) -> CandidateStateTransition:
        result_payload = dict(result or {})
        if self._has_fresh_realtime_tick(candidate):
            return self.promote(
                candidate,
                CandidateRuntimeState.WATCHING,
                CandidateReasonCode.REALTIME_TICK_FRESH.value,
                source_event_id=str(result_payload.get("source_event_id") or ""),
                source_event_type="hydration_result",
                source_component="CandidateHydrator",
                details=result_payload,
            )
        reason = CandidateReasonCode.TR_BACKFILL_PRICE_ONLY.value if result_payload.get("market_data_merged") else CandidateReasonCode.LATEST_TICK_MISSING.value
        return self.apply_blocking_reason(
            candidate,
            CandidateBlockingStage.DATA,
            reason,
            details=result_payload,
            source_event_id=str(result_payload.get("source_event_id") or ""),
            source_event_type="hydration_result",
            source_component="CandidateHydrator",
        )

    def on_realtime_tick(self, candidate: Candidate, market_snapshot: Any) -> CandidateStateTransition:
        payload = _snapshot_payload(market_snapshot)
        if bool(payload.get("is_fresh")) and str(payload.get("price_source") or "REALTIME").upper() != "TR_BACKFILL":
            return self.promote(
                candidate,
                CandidateRuntimeState.WATCHING,
                CandidateReasonCode.REALTIME_TICK_FRESH.value,
                source_event_id=str(payload.get("source_event_id") or ""),
                source_event_type="price_tick",
                source_component="MarketDataService",
                details=payload,
            )
        reason = CandidateReasonCode.LATEST_TICK_STALE.value if payload else CandidateReasonCode.LATEST_TICK_MISSING.value
        if str(payload.get("price_source") or "").upper() == "TR_BACKFILL":
            reason = CandidateReasonCode.TR_BACKFILL_PRICE_ONLY.value
        return self.apply_blocking_reason(
            candidate,
            CandidateBlockingStage.DATA,
            reason,
            details=payload,
            source_event_id=str(payload.get("source_event_id") or ""),
            source_event_type="price_tick",
            source_component="MarketDataService",
        )

    def apply_blocking_reason(
        self,
        candidate: Candidate,
        blocking_stage: CandidateBlockingStage | str,
        reason_code: CandidateReasonCode | str,
        details: Mapping[str, Any] | None = None,
        *,
        source_event_id: str = "",
        source_event_type: str = "",
        source_component: str = "CandidateFsmService",
    ) -> CandidateStateTransition:
        current = legacy_to_v2_state(candidate.state) if candidate.state in {CandidateState.WAIT_DATA, CandidateState.WATCHING} else self.v2_state(candidate)
        stage = _enum_value(blocking_stage)
        reason = _enum_value(reason_code)
        target = current
        if target in {CandidateRuntimeState.SETUP_READY, CandidateRuntimeState.TIMING_READY} and stage == CandidateBlockingStage.DATA.value:
            target = CandidateRuntimeState.WATCHING
        return self._apply(
            candidate,
            target,
            reason,
            blocking_stage=stage,
            reason_codes=(reason,),
            source_event_id=source_event_id,
            source_event_type=source_event_type,
            source_component=source_component,
            details=dict(details or {}),
        )

    def promote(
        self,
        candidate: Candidate,
        target_state: CandidateRuntimeState | str,
        reason_code: CandidateReasonCode | str,
        source_event_id: str = "",
        *,
        blocking_stage: CandidateBlockingStage | str = CandidateBlockingStage.NONE,
        source_event_type: str = "",
        source_component: str = "CandidateFsmService",
        details: Mapping[str, Any] | None = None,
    ) -> CandidateStateTransition:
        target = CandidateRuntimeState(_enum_value(target_state))
        reason = _enum_value(reason_code)
        stage = _enum_value(blocking_stage)
        if target in {CandidateRuntimeState.SETUP_READY, CandidateRuntimeState.TIMING_READY} and not self._has_fresh_realtime_tick(candidate):
            target = CandidateRuntimeState.WATCHING
            stage = CandidateBlockingStage.DATA.value
            reason = CandidateReasonCode.LATEST_TICK_MISSING.value
        return self._apply(
            candidate,
            target,
            reason,
            blocking_stage=stage,
            reason_codes=(reason,),
            source_event_id=source_event_id,
            source_event_type=source_event_type,
            source_component=source_component,
            details=dict(details or {}),
        )

    def transition_snapshot(self, candidate: Candidate) -> CandidateFsmSnapshot:
        metadata = _fsm_metadata(candidate)
        return CandidateFsmSnapshot(
            candidate_id=str(candidate.id or ""),
            trade_date=candidate.trade_date,
            code=candidate.code,
            v2_state=str(metadata.get("v2_state") or self.v2_state(candidate).value),
            blocking_stage=str(metadata.get("blocking_stage") or CandidateBlockingStage.NONE.value),
            primary_reason_code=str(metadata.get("primary_reason_code") or ""),
            reason_codes=tuple(str(item) for item in list(metadata.get("reason_codes") or [])),
            next_required_action=str(metadata.get("next_required_action") or ""),
            source_event_ids=tuple(str(item) for item in list(metadata.get("source_event_ids") or [])),
            last_transition_at=str(metadata.get("last_transition_at") or ""),
        )

    def v2_state(self, candidate: Candidate) -> CandidateRuntimeState:
        metadata = _fsm_metadata(candidate)
        raw = str(metadata.get("v2_state") or "")
        if raw:
            try:
                return CandidateRuntimeState(raw)
            except ValueError:
                pass
        return legacy_to_v2_state(candidate.state)

    def summary(self, *, trade_date: str | None = None) -> dict[str, Any]:
        if self.db is None:
            return {"status": "UNAVAILABLE"}
        candidates = list(self.db.list_candidates(trade_date=trade_date) if trade_date else self.db.list_candidates())
        state_counts = Counter()
        stage_counts = Counter()
        reasons = Counter()
        for candidate in candidates:
            snap = self.transition_snapshot(candidate)
            state_counts[snap.v2_state] += 1
            stage_counts[snap.blocking_stage] += 1
            if snap.primary_reason_code:
                reasons[snap.primary_reason_code] += 1
        transition_count = 0
        last_transition_at = ""
        loader = getattr(self.db, "candidate_fsm_summary", None)
        if callable(loader):
            stored = dict(loader(trade_date=trade_date) or {})
            transition_count = int(stored.get("transition_count") or 0)
            last_transition_at = str(stored.get("last_transition_at") or "")
        return {
            "status": "OK",
            "state_counts": dict(state_counts),
            "blocking_stage_counts": dict(stage_counts),
            "top_reason_codes": [{"reason": key, "count": count} for key, count in reasons.most_common(10)],
            "transition_count": transition_count,
            "last_transition_at": last_transition_at,
        }

    def _apply(
        self,
        candidate: Candidate,
        target_state: CandidateRuntimeState,
        reason_code: str,
        *,
        blocking_stage: str,
        reason_codes: tuple[str, ...],
        source_event_id: str,
        source_event_type: str,
        source_component: str,
        details: dict[str, Any],
    ) -> CandidateStateTransition:
        previous = self.v2_state(candidate)
        occurred_at = _format_time(self.clock())
        metadata = dict(candidate.metadata or {})
        fsm = dict(metadata.get("candidate_fsm") or {})
        existing_sources = [str(item) for item in list(fsm.get("source_event_ids") or []) if str(item)]
        if source_event_id and source_event_id not in existing_sources:
            existing_sources.append(source_event_id)
        fsm.update(
            {
                "v2_state": target_state.value,
                "blocking_stage": blocking_stage,
                "primary_reason_code": reason_code,
                "reason_codes": _dedupe([*list(fsm.get("reason_codes") or []), *reason_codes]),
                "next_required_action": NEXT_REQUIRED_ACTION.get(reason_code, ""),
                "source_event_ids": existing_sources[-20:],
                "last_transition_at": occurred_at,
            }
        )
        if "is_fresh" in details:
            fsm["latest_tick_fresh"] = "true" if bool(details.get("is_fresh")) else "false"
        if details.get("freshness_status"):
            fsm["freshness_status"] = str(details.get("freshness_status") or "")
        if details.get("price_source"):
            fsm["price_source"] = str(details.get("price_source") or "")
        metadata["candidate_fsm"] = fsm
        candidate.metadata = metadata
        transition = CandidateStateTransition(
            transition_id=f"cand_tr_{uuid4().hex}",
            trade_date=candidate.trade_date,
            candidate_id=str(candidate.id or ""),
            code=candidate.code,
            from_state=previous,
            to_state=target_state,
            blocking_stage=blocking_stage,
            reason_code=reason_code,
            reason_codes=tuple(reason_codes),
            next_required_action=fsm["next_required_action"],
            source_event_id=source_event_id,
            source_event_type=source_event_type,
            source_component=source_component,
            occurred_at=occurred_at,
            details=details,
            metadata=dict(fsm),
        )
        saver = getattr(self.db, "save_candidate_state_transition", None) if self.db is not None else None
        if callable(saver):
            saver(transition)
        return transition

    @staticmethod
    def _candidate_from_input(value: Any) -> Candidate | None:
        if isinstance(value, Candidate):
            return value
        if isinstance(value, Mapping):
            candidate = value.get("candidate")
            return candidate if isinstance(candidate, Candidate) else None
        candidate = getattr(value, "candidate", None)
        return candidate if isinstance(candidate, Candidate) else None

    @staticmethod
    def _payload(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return dict(to_dict())
        return dict(getattr(value, "__dict__", {}) or {})

    @staticmethod
    def _has_fresh_realtime_tick(candidate: Candidate) -> bool:
        fsm = _fsm_metadata(candidate)
        if str(fsm.get("latest_tick_fresh") or "").lower() == "true":
            return True
        if str(fsm.get("price_source") or "").upper() == "REALTIME" and str(fsm.get("freshness_status") or "").upper() == "FRESH":
            return True
        metadata = dict(candidate.metadata or {})
        if str(metadata.get("entry_timing_source") or "").upper() == "REALTIME_TICK":
            return True
        return bool(metadata.get("gate_usable_for_entry")) and str(metadata.get("entry_timing_source") or "").upper() == "REALTIME_TICK"


def legacy_to_v2_state(state: CandidateState | str | None) -> CandidateRuntimeState:
    if isinstance(state, CandidateState):
        return LEGACY_TO_V2_STATE.get(state, CandidateRuntimeState.DISCOVERED)
    try:
        return LEGACY_TO_V2_STATE.get(CandidateState(str(state or "")), CandidateRuntimeState.DISCOVERED)
    except ValueError:
        return CandidateRuntimeState.DISCOVERED


def build_candidate_fsm_summary(db: Any, *, trade_date: str | None = None) -> dict[str, Any]:
    return CandidateFsmService(db).summary(trade_date=trade_date)


def _fsm_metadata(candidate: Candidate) -> dict[str, Any]:
    metadata = dict(candidate.metadata or {})
    return dict(metadata.get("candidate_fsm") or {})


def _snapshot_payload(snapshot: Any) -> dict[str, Any]:
    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    if hasattr(snapshot, "__dict__"):
        return dict(snapshot.__dict__)
    return {}


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _format_time(value: datetime | None = None) -> str:
    return (value or datetime.now()).replace(microsecond=0).isoformat()


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "CandidateBlockingStage",
    "CandidateFsmService",
    "CandidateFsmSnapshot",
    "CandidateReasonCode",
    "build_candidate_fsm_summary",
    "legacy_to_v2_state",
]
