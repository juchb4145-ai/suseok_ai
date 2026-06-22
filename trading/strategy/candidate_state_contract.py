from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from trading.runtime_ports import CandidateRuntimeState
from trading.strategy.candidate_fsm import CandidateBlockingStage, legacy_to_v2_state
from trading.strategy.candidates import normalize_code
from trading.strategy.models import Candidate, CandidateEvent, CandidateState


CONTRACT_VERSION = "candidate_state_contract.v2"


class CandidateStateContractVersion(str, Enum):
    V2 = CONTRACT_VERSION


class CandidateLifecycleReadiness(str, Enum):
    DISCOVERED = "DISCOVERED"
    HYDRATING = "HYDRATING"
    HYDRATION_COMPLETE = "HYDRATION_COMPLETE"
    HYDRATION_RETRY_WAIT = "HYDRATION_RETRY_WAIT"
    HYDRATION_FAILED = "HYDRATION_FAILED"
    TERMINAL = "TERMINAL"


class CandidateEvaluationEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    TERMINAL = "TERMINAL"
    NO_ACTIVE_SOURCE = "NO_ACTIVE_SOURCE"
    HYDRATION_INCOMPLETE = "HYDRATION_INCOMPLETE"
    HYDRATION_RETRY_WAIT = "HYDRATION_RETRY_WAIT"
    HYDRATION_FAILED = "HYDRATION_FAILED"
    V2_STATE_NOT_EVALUABLE = "V2_STATE_NOT_EVALUABLE"


@dataclass(frozen=True)
class CandidateStateContractSnapshot:
    code: str
    trade_date: str
    contract_version: str = CONTRACT_VERSION
    durable_state: str = ""
    projected_state: str = ""
    v2_state: str = ""
    blocking_stage: str = ""
    primary_reason_code: str = ""
    lifecycle_readiness: str = CandidateLifecycleReadiness.DISCOVERED.value
    evaluation_eligibility: str = CandidateEvaluationEligibility.HYDRATION_INCOMPLETE.value
    evaluation_eligible: bool = False
    hydration_complete: bool = False
    active_source_exists: bool = False
    terminal: bool = False
    reconciled: bool = False
    reconcile_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateStateContractService:
    db: Any | None = None
    clock: Any = datetime.now
    enabled: bool | None = None

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = _env_bool("TRADING_CANDIDATE_STATE_CONTRACT_V2_ENABLED", True)

    def hydration_complete(self, candidate: Candidate) -> bool:
        metadata = dict(candidate.metadata or {})
        hydration = dict(metadata.get("candidate_hydration") or {})
        if candidate.state == CandidateState.WATCHING and not hydration:
            return True
        if _bool(hydration.get("basic_hydration_complete")):
            return True
        status = str(hydration.get("status") or "").upper()
        if status not in {"ACKED", "MERGED", "OK"}:
            return False
        parsed = dict(hydration.get("parsed") or {})
        return _basic_hydration_payload_complete(candidate, parsed)

    def active_source_exists(self, candidate: Candidate) -> bool:
        metadata = dict(candidate.metadata or {})
        ingestion = dict(metadata.get("candidate_ingestion") or {})
        if candidate.state == CandidateState.WATCHING and not ingestion and not candidate.sources:
            return True
        active_types = [str(item or "") for item in list(ingestion.get("active_source_types") or []) if str(item or "")]
        if active_types:
            return True
        source_map = dict(ingestion.get("source_map") or {})
        if source_map:
            return any(bool(dict(item or {}).get("active", True)) for item in source_map.values() if isinstance(item, Mapping))
        if candidate.sources:
            return True
        return bool(metadata.get("primary_source") or ingestion.get("primary_source"))

    def v2_state(self, candidate: Candidate) -> CandidateRuntimeState:
        fsm = dict(dict(candidate.metadata or {}).get("candidate_fsm") or {})
        raw = str(fsm.get("v2_state") or "")
        if raw:
            try:
                fsm_state = CandidateRuntimeState(raw)
                if candidate.state in {CandidateState.WAIT_DATA, CandidateState.WATCHING} and fsm_state in {
                    CandidateRuntimeState.DISCOVERED,
                    CandidateRuntimeState.HYDRATING,
                }:
                    return legacy_to_v2_state(candidate.state)
                return fsm_state
            except ValueError:
                pass
        return legacy_to_v2_state(candidate.state)

    def blocking_stage(self, candidate: Candidate) -> str:
        fsm = dict(dict(candidate.metadata or {}).get("candidate_fsm") or {})
        return str(fsm.get("blocking_stage") or CandidateBlockingStage.NONE.value)

    def is_terminal(self, candidate: Candidate) -> bool:
        return candidate.state in {CandidateState.REMOVED, CandidateState.EXPIRED, CandidateState.CANCELLED}

    def is_evaluation_eligible(self, candidate: Candidate) -> bool:
        return self.snapshot(candidate).evaluation_eligible

    def project_legacy_state(self, candidate: Candidate) -> CandidateState:
        if self.is_terminal(candidate):
            return candidate.state
        state = self.v2_state(candidate)
        hydrated = self.hydration_complete(candidate)
        readiness = self._lifecycle_readiness(candidate, hydrated=hydrated, terminal=False)
        if state == CandidateRuntimeState.DISCOVERED:
            return CandidateState.DETECTED
        if state == CandidateRuntimeState.HYDRATING:
            return CandidateState.HYDRATING
        if state in {CandidateRuntimeState.WATCHING, CandidateRuntimeState.SETUP_READY, CandidateRuntimeState.TIMING_READY}:
            return CandidateState.WATCHING if readiness == CandidateLifecycleReadiness.HYDRATION_COMPLETE else CandidateState.WAIT_DATA
        if state == CandidateRuntimeState.REMOVED:
            return CandidateState.REMOVED
        if state == CandidateRuntimeState.EXPIRED:
            return CandidateState.EXPIRED
        if state == CandidateRuntimeState.CLOSED:
            return CandidateState.CANCELLED
        return candidate.state

    def snapshot(self, candidate: Candidate) -> CandidateStateContractSnapshot:
        v2_state = self.v2_state(candidate)
        terminal = self.is_terminal(candidate)
        active = self.active_source_exists(candidate)
        hydrated = self.hydration_complete(candidate)
        readiness = self._lifecycle_readiness(candidate, hydrated=hydrated, terminal=terminal)
        eligibility = self._eligibility(candidate, v2_state=v2_state, hydrated=hydrated, active=active, terminal=terminal)
        projected = self.project_legacy_state(candidate)
        fsm = dict(dict(candidate.metadata or {}).get("candidate_fsm") or {})
        return CandidateStateContractSnapshot(
            code=normalize_code(candidate.code),
            trade_date=candidate.trade_date,
            durable_state=str(candidate.state.value if isinstance(candidate.state, CandidateState) else candidate.state or ""),
            projected_state=str(projected.value if isinstance(projected, CandidateState) else projected or ""),
            v2_state=v2_state.value,
            blocking_stage=str(fsm.get("blocking_stage") or CandidateBlockingStage.NONE.value),
            primary_reason_code=str(fsm.get("primary_reason_code") or ""),
            lifecycle_readiness=readiness.value,
            evaluation_eligibility=eligibility.value,
            evaluation_eligible=eligibility == CandidateEvaluationEligibility.ELIGIBLE,
            hydration_complete=hydrated,
            active_source_exists=active,
            terminal=terminal,
        )

    def reconcile_candidate(self, candidate: Candidate, market_snapshot: Any = None, now: datetime | None = None) -> CandidateStateContractSnapshot:
        snapshot = self.snapshot(candidate)
        if not self.enabled or snapshot.terminal:
            return snapshot
        target = self.project_legacy_state(candidate)
        if candidate.state != target and self._state_change_allowed(candidate, target, snapshot):
            previous = candidate.state
            candidate.state = target
            metadata = dict(candidate.metadata or {})
            contract = dict(metadata.get("candidate_state_contract") or {})
            reason = _reconcile_reason(previous, target)
            occurred_at = _format_time(now or self.clock())
            contract.update({**snapshot.to_dict(), "reconciled_at": occurred_at, "reconcile_reason": reason})
            metadata["candidate_state_contract"] = contract
            candidate.metadata = metadata
            candidate.last_seen_at = candidate.last_seen_at or occurred_at
            self._save_reconciliation(candidate, previous, target, reason, occurred_at, snapshot)
            reconciled = self.snapshot(candidate)
            return CandidateStateContractSnapshot(**{**reconciled.to_dict(), "reconciled": True, "reconcile_reason": reason})
        return snapshot

    def reconcile_trade_date(self, trade_date: str, market_data_service: Any = None, limit: int | None = None) -> dict[str, Any]:
        candidates = list(self.db.list_candidates(trade_date=trade_date) or []) if self.db is not None else []
        if limit is not None:
            candidates = candidates[: max(0, int(limit))]
        summary = {
            "status": "OK" if self.enabled else "DISABLED",
            "contract_version": CONTRACT_VERSION,
            "scanned_count": 0,
            "recovered_to_watching_count": 0,
            "kept_retry_wait_count": 0,
            "kept_failed_count": 0,
            "terminal_skipped_count": 0,
            "no_active_source_count": 0,
            "evaluation_eligible_count": 0,
            "hydration_incomplete_wait_data_count": 0,
            "changed_codes": [],
            "top_reasons": {},
        }
        reasons: dict[str, int] = {}
        for candidate in candidates:
            before = candidate.state
            snapshot = self.reconcile_candidate(candidate)
            summary["scanned_count"] += 1
            if snapshot.evaluation_eligible:
                summary["evaluation_eligible_count"] += 1
            if snapshot.terminal:
                summary["terminal_skipped_count"] += 1
            if not snapshot.active_source_exists:
                summary["no_active_source_count"] += 1
            if snapshot.lifecycle_readiness == CandidateLifecycleReadiness.HYDRATION_RETRY_WAIT.value:
                summary["kept_retry_wait_count"] += 1
            if snapshot.lifecycle_readiness == CandidateLifecycleReadiness.HYDRATION_FAILED.value:
                summary["kept_failed_count"] += 1
            if before == CandidateState.WAIT_DATA and not snapshot.hydration_complete:
                summary["hydration_incomplete_wait_data_count"] += 1
            if before != candidate.state:
                summary["changed_codes"].append(candidate.code)
                if candidate.state == CandidateState.WATCHING:
                    summary["recovered_to_watching_count"] += 1
            reason = snapshot.evaluation_eligibility
            reasons[reason] = reasons.get(reason, 0) + 1
        summary["top_reasons"] = dict(sorted(reasons.items(), key=lambda item: item[1], reverse=True)[:10])
        return summary

    def _lifecycle_readiness(self, candidate: Candidate, *, hydrated: bool, terminal: bool) -> CandidateLifecycleReadiness:
        if terminal:
            return CandidateLifecycleReadiness.TERMINAL
        hydration = dict(dict(candidate.metadata or {}).get("candidate_hydration") or {})
        status = str(hydration.get("status") or "").upper()
        if status == "RETRY_WAIT":
            return CandidateLifecycleReadiness.HYDRATION_RETRY_WAIT
        if status == "FAILED":
            return CandidateLifecycleReadiness.HYDRATION_FAILED
        if candidate.state == CandidateState.HYDRATING or status == "PENDING":
            return CandidateLifecycleReadiness.HYDRATING
        if hydrated:
            return CandidateLifecycleReadiness.HYDRATION_COMPLETE
        return CandidateLifecycleReadiness.DISCOVERED

    def _eligibility(
        self,
        candidate: Candidate,
        *,
        v2_state: CandidateRuntimeState,
        hydrated: bool,
        active: bool,
        terminal: bool,
    ) -> CandidateEvaluationEligibility:
        if terminal:
            return CandidateEvaluationEligibility.TERMINAL
        readiness = self._lifecycle_readiness(candidate, hydrated=hydrated, terminal=terminal)
        if readiness == CandidateLifecycleReadiness.HYDRATION_RETRY_WAIT:
            return CandidateEvaluationEligibility.HYDRATION_RETRY_WAIT
        if readiness == CandidateLifecycleReadiness.HYDRATION_FAILED:
            return CandidateEvaluationEligibility.HYDRATION_FAILED
        if not active:
            return CandidateEvaluationEligibility.NO_ACTIVE_SOURCE
        if not hydrated:
            return CandidateEvaluationEligibility.HYDRATION_INCOMPLETE
        if v2_state not in {CandidateRuntimeState.WATCHING, CandidateRuntimeState.SETUP_READY, CandidateRuntimeState.TIMING_READY}:
            return CandidateEvaluationEligibility.V2_STATE_NOT_EVALUABLE
        return CandidateEvaluationEligibility.ELIGIBLE

    def _state_change_allowed(self, candidate: Candidate, target: CandidateState, snapshot: CandidateStateContractSnapshot) -> bool:
        if self.is_terminal(candidate):
            return False
        if target == CandidateState.WATCHING:
            return (
                snapshot.lifecycle_readiness == CandidateLifecycleReadiness.HYDRATION_COMPLETE.value
                and snapshot.hydration_complete
                and snapshot.active_source_exists
            )
        if target == CandidateState.WAIT_DATA:
            return candidate.state in {CandidateState.DETECTED, CandidateState.HYDRATING, CandidateState.WAIT_DATA}
        return False

    def _save_reconciliation(
        self,
        candidate: Candidate,
        previous: CandidateState,
        target: CandidateState,
        reason: str,
        occurred_at: str,
        snapshot: CandidateStateContractSnapshot,
    ) -> None:
        if self.db is None:
            return
        event_type = "candidate_wait_data_recovered" if previous == CandidateState.WAIT_DATA and target == CandidateState.WATCHING else "candidate_legacy_state_projected"
        event = CandidateEvent(
            candidate_id=candidate.id,
            event_type=event_type,
            from_state=previous,
            to_state=target,
            source=None,
            reason=reason,
            created_at=occurred_at,
            payload=snapshot.to_dict(),
        )
        if hasattr(self.db, "save_candidate_with_events"):
            saved = self.db.save_candidate_with_events(candidate, [event])
            candidate.id = saved.id
        elif hasattr(self.db, "save_candidate"):
            self.db.save_candidate(candidate)
        saver = getattr(self.db, "save_candidate_state_transition", None)
        if callable(saver):
            saver(
                {
                    "candidate_id": candidate.id,
                    "trade_date": candidate.trade_date,
                    "code": candidate.code,
                    "from_state": previous,
                    "to_state": target,
                    "blocking_stage": snapshot.blocking_stage,
                    "reason_code": reason,
                    "reason_codes": [reason],
                    "source_event_type": event_type,
                    "source_component": "CandidateStateContractReconciler",
                    "details": snapshot.to_dict(),
                    "occurred_at": occurred_at,
                }
            )


class CandidateStateContractReconciler(CandidateStateContractService):
    pass


def _basic_hydration_payload_complete(candidate: Candidate, parsed: Mapping[str, Any]) -> bool:
    code = normalize_code(candidate.code or parsed.get("code"))
    price = _float(parsed.get("current_price") or parsed.get("price"))
    prev_close = _float(parsed.get("prev_close"))
    change_rate = _float(parsed.get("change_rate") or parsed.get("change_rate_pct"))
    return bool(code and price > 0 and (change_rate != 0.0 or prev_close > 0))


def _reconcile_reason(previous: CandidateState, target: CandidateState) -> str:
    if previous == CandidateState.WAIT_DATA and target == CandidateState.WATCHING:
        return "LEGACY_WAIT_DATA_CONTRACT_REPAIR"
    if target == CandidateState.WATCHING:
        return "HYDRATION_COMPLETE_EVALUATION_ELIGIBLE"
    return "CANDIDATE_STATE_CONTRACT_PROJECTED"


def _format_time(value: datetime | None = None) -> str:
    return (value or datetime.now()).replace(microsecond=0).isoformat()


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").strip().replace(",", "").replace("+", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = [
    "CONTRACT_VERSION",
    "CandidateEvaluationEligibility",
    "CandidateLifecycleReadiness",
    "CandidateStateContractReconciler",
    "CandidateStateContractService",
    "CandidateStateContractSnapshot",
    "CandidateStateContractVersion",
]
