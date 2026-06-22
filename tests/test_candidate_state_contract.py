from datetime import datetime

from storage.db import TradingDatabase
from trading.strategy.candidate_ingestion import CandidateIngestionService, CandidateSourceEvent
from trading.strategy.candidate_state_contract import (
    CandidateEvaluationEligibility,
    CandidateLifecycleReadiness,
    CandidateStateContractService,
)
from trading.strategy.models import Candidate, CandidateState


TRADE_DATE = "2026-06-22"
NOW = datetime(2026, 6, 22, 9, 5, 0)


def test_wait_data_with_completed_hydration_recovers_to_watching(tmp_path):
    db = TradingDatabase(str(tmp_path / "contract.db"))
    candidate = _ingested_candidate(db, "014950")
    candidate.state = CandidateState.WAIT_DATA
    candidate.metadata["candidate_hydration"] = _complete_hydration()
    db.save_candidate(candidate)

    snapshot = CandidateStateContractService(db, clock=lambda: NOW).reconcile_candidate(candidate, now=NOW)
    reloaded = db.load_candidate(TRADE_DATE, "014950")

    assert snapshot.evaluation_eligible is True
    assert snapshot.lifecycle_readiness == CandidateLifecycleReadiness.HYDRATION_COMPLETE.value
    assert snapshot.evaluation_eligibility == CandidateEvaluationEligibility.ELIGIBLE.value
    assert reloaded.state == CandidateState.WATCHING
    assert reloaded.metadata["candidate_state_contract"]["reconcile_reason"] == "LEGACY_WAIT_DATA_CONTRACT_REPAIR"
    assert [event.event_type for event in db.list_candidate_events(reloaded.id)][-1] == "candidate_wait_data_recovered"
    transitions = db.list_candidate_state_transitions(candidate_id=reloaded.id)
    assert transitions[-1]["from_state"] == CandidateState.WAIT_DATA.value
    assert transitions[-1]["to_state"] == CandidateState.WATCHING.value


def test_retry_wait_candidate_is_not_recovered_even_with_stale_complete_flag(tmp_path):
    db = TradingDatabase(str(tmp_path / "retry.db"))
    candidate = _ingested_candidate(db, "014910")
    candidate.state = CandidateState.WAIT_DATA
    candidate.metadata["candidate_hydration"] = {
        **_complete_hydration(),
        "status": "RETRY_WAIT",
        "basic_hydration_complete": True,
    }
    db.save_candidate(candidate)

    snapshot = CandidateStateContractService(db, clock=lambda: NOW).reconcile_candidate(candidate, now=NOW)
    reloaded = db.load_candidate(TRADE_DATE, "014910")

    assert snapshot.evaluation_eligible is False
    assert snapshot.lifecycle_readiness == CandidateLifecycleReadiness.HYDRATION_RETRY_WAIT.value
    assert snapshot.evaluation_eligibility == CandidateEvaluationEligibility.HYDRATION_RETRY_WAIT.value
    assert reloaded.state == CandidateState.WAIT_DATA


def test_completed_hydration_without_active_source_is_not_entry_eligible(tmp_path):
    db = TradingDatabase(str(tmp_path / "no-source.db"))
    candidate = db.save_candidate(
        Candidate(
            trade_date=TRADE_DATE,
            code="099999",
            name="No Source",
            state=CandidateState.WAIT_DATA,
            metadata={"candidate_hydration": _complete_hydration()},
        )
    )

    snapshot = CandidateStateContractService(db, clock=lambda: NOW).reconcile_candidate(candidate, now=NOW)
    reloaded = db.load_candidate(TRADE_DATE, "099999")

    assert snapshot.evaluation_eligible is False
    assert snapshot.evaluation_eligibility == CandidateEvaluationEligibility.NO_ACTIVE_SOURCE.value
    assert reloaded.state == CandidateState.WAIT_DATA


def test_terminal_candidate_is_never_recovered(tmp_path):
    db = TradingDatabase(str(tmp_path / "terminal.db"))
    candidate = _ingested_candidate(db, "088888")
    candidate.state = CandidateState.REMOVED
    candidate.metadata["candidate_hydration"] = _complete_hydration()
    db.save_candidate(candidate)

    snapshot = CandidateStateContractService(db, clock=lambda: NOW).reconcile_candidate(candidate, now=NOW)
    reloaded = db.load_candidate(TRADE_DATE, "088888")

    assert snapshot.evaluation_eligible is False
    assert snapshot.evaluation_eligibility == CandidateEvaluationEligibility.TERMINAL.value
    assert reloaded.state == CandidateState.REMOVED


def test_reconcile_trade_date_reports_recovered_and_waiting_counts(tmp_path):
    db = TradingDatabase(str(tmp_path / "summary.db"))
    recovered = _ingested_candidate(db, "000001")
    recovered.state = CandidateState.WAIT_DATA
    recovered.metadata["candidate_hydration"] = _complete_hydration()
    db.save_candidate(recovered)
    retry = _ingested_candidate(db, "000002")
    retry.state = CandidateState.WAIT_DATA
    retry.metadata["candidate_hydration"] = {**_complete_hydration(), "status": "RETRY_WAIT"}
    db.save_candidate(retry)

    summary = CandidateStateContractService(db, clock=lambda: NOW).reconcile_trade_date(TRADE_DATE)

    assert summary["scanned_count"] == 2
    assert summary["recovered_to_watching_count"] == 1
    assert summary["kept_retry_wait_count"] == 1
    assert summary["evaluation_eligible_count"] == 1
    assert summary["changed_codes"] == ["000001"]


def test_reconcile_candidate_is_idempotent_after_recovery(tmp_path):
    db = TradingDatabase(str(tmp_path / "idempotent.db"))
    candidate = _ingested_candidate(db, "000003")
    candidate.state = CandidateState.WAIT_DATA
    candidate.metadata["candidate_hydration"] = _complete_hydration()
    candidate = db.save_candidate(candidate)
    service = CandidateStateContractService(db, clock=lambda: NOW)

    service.reconcile_candidate(candidate, now=NOW)
    reloaded = db.load_candidate(TRADE_DATE, "000003")
    event_count = len(db.list_candidate_events(reloaded.id))
    transition_count = len(db.list_candidate_state_transitions(candidate_id=reloaded.id))

    service.reconcile_candidate(reloaded, now=NOW)

    assert len(db.list_candidate_events(reloaded.id)) == event_count
    assert len(db.list_candidate_state_transitions(candidate_id=reloaded.id)) == transition_count


def _ingested_candidate(db: TradingDatabase, code: str) -> Candidate:
    candidate = CandidateIngestionService(db).ingest(
        CandidateSourceEvent(
            trade_date=TRADE_DATE,
            code=code,
            name=f"Stock {code}",
            source_type="condition_search",
            source_id=f"condition:{code}",
            source_score=50.0,
            theme_id="theme-a",
            theme_name="Theme A",
            detected_at=f"{TRADE_DATE}T09:01:00",
        )
    ).candidate
    return candidate


def _complete_hydration() -> dict:
    return {
        "status": "ACKED",
        "basic_hydration_complete": True,
        "basic_hydration_completed_at": NOW.isoformat(),
        "parsed": {
            "code": "014950",
            "current_price": 1000,
            "change_rate": 1.2,
            "prev_close": 988,
        },
    }
