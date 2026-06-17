from datetime import datetime, timezone

from trading.strategy.reboot_v2 import (
    CandidateV2State,
    ConditionEventType,
    ConditionHit,
    ConditionLevel,
    HydrationPriority,
    build_tr_hydration_idempotency_key,
)


def test_condition_hit_normalizes_code_and_counts_seen_again():
    seen_at = datetime(2026, 6, 17, 9, 1, tzinfo=timezone.utc)
    hit = ConditionHit.create(
        code="A005930",
        condition_name="theme_alive",
        condition_level=ConditionLevel.ALIVE,
        event_type=ConditionEventType.INCLUDE,
        seen_at=seen_at,
    )

    assert hit.code == "005930"
    assert hit.condition_level == ConditionLevel.ALIVE
    assert hit.event_type == ConditionEventType.INCLUDE
    assert hit.hit_count == 1

    next_hit = hit.seen_again(datetime(2026, 6, 17, 9, 2, tzinfo=timezone.utc))

    assert next_hit.first_seen_at == seen_at
    assert next_hit.last_seen_at > hit.last_seen_at
    assert next_hit.hit_count == 2


def test_hydration_idempotency_key_is_stable_for_sorted_inputs():
    first = build_tr_hydration_idempotency_key(
        trade_date="2026-06-17",
        priority=HydrationPriority.P1,
        tr_code="opt10001",
        rq_name="candidate_basic",
        inputs={"code": "005930", "market": "KOSPI"},
    )
    second = build_tr_hydration_idempotency_key(
        trade_date="2026-06-17",
        priority=HydrationPriority.P1,
        tr_code="opt10001",
        rq_name="candidate_basic",
        inputs={"market": "KOSPI", "code": "005930"},
    )

    assert first == second
    assert first.startswith("tr:2026-06-17:P1:opt10001:candidate_basic:")


def test_candidate_v2_states_include_reboot_flow():
    assert {state.value for state in CandidateV2State} == {
        "DETECTED",
        "HYDRATING",
        "WATCHING",
        "SETUP_READY",
        "TIMING_READY",
        "ORDER_PENDING",
        "OPEN",
        "EXITING",
        "CLOSED",
        "WAIT",
        "HARD_BLOCK",
    }
