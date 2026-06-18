from trading.runtime_ports import (
    BlockingStage,
    CandidateRuntimeState,
    CandidateStateTransition,
    CoreEvent,
    CoreEventType,
    EntryEvaluationStep,
    EntryStep,
    EventLogRecord,
    MarketDataSnapshot,
    StepResult,
)


def test_runtime_ports_import_and_construct_smoke() -> None:
    record = EventLogRecord(
        event_id="evt_1",
        event_type="price_tick",
        dedupe_key="tick:005930:1",
        received_at="2026-06-18T09:00:00+09:00",
        payload_json="{}",
    )
    snapshot = MarketDataSnapshot(
        code="005930",
        price=70000,
        tick_at="2026-06-18T09:00:00+09:00",
        received_at=record.received_at,
        source_event_id=record.event_id,
        is_fresh=True,
    )
    transition = CandidateStateTransition(
        candidate_id="candidate-1",
        code=snapshot.code,
        from_state=CandidateRuntimeState.DISCOVERED,
        to_state=CandidateRuntimeState.HYDRATING,
        occurred_at=record.received_at,
        reason_code="CONDITION_INCLUDE",
        blocking_stage=BlockingStage.WAIT_DATA,
        source_event_id=record.event_id,
    )
    step = EntryEvaluationStep(
        step=EntryStep.DATA_READY,
        result=StepResult.DATA_WAIT,
        reason_codes=("LATEST_TICK_STALE",),
        next_required_action="WAIT_FRESH_TICK",
    )
    core_event = CoreEvent(
        type=CoreEventType.MARKET_DATA_UPDATED,
        event_id="core_1",
        occurred_at=record.received_at,
        payload={"code": snapshot.code, "step": step.step},
        source_event_id=record.event_id,
    )

    assert snapshot.is_fresh is True
    assert transition.blocking_stage == BlockingStage.WAIT_DATA
    assert core_event.source_event_id == "evt_1"
