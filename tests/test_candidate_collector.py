import json
from datetime import datetime, timedelta

import pytest

from kiwoom.client import ConditionCandidateEvent, MockKiwoomClient
from storage.db import TradingDatabase
from trading.strategy.candidates import CandidateCollector, CandidateLifecycle
from trading.strategy.models import CandidateSourceType, CandidateState, StrategyProfile


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def make_collector(tmp_path, clock=None):
    client = MockKiwoomClient()
    db = TradingDatabase(str(tmp_path / "test.sqlite3"))
    clock = clock or MutableClock(datetime(2026, 5, 29, 9, 0, 0))
    collector = CandidateCollector(db, client=client, clock=clock, default_ttl_minutes=5)
    return collector, client, db, clock


def event_types(db, candidate_id):
    return [event.event_type for event in db.list_candidate_events(candidate_id)]


def test_condition_include_creates_detected_candidate_and_event(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "코스닥테마주")])

    client.emit_condition_include("코스닥테마주", "A412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate is not None
    assert candidate.state == CandidateState.DETECTED
    assert candidate.sources == [CandidateSourceType.CONDITION]
    assert candidate.condition_names == ["코스닥테마주"]
    assert candidate.metadata["condition_indices"] == {"코스닥테마주": 1}
    assert event_types(db, candidate.id) == ["candidate_detected"]
    db.close()


def test_theme_discovery_condition_is_metadata_only_entry_excluded(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)

    collector.handle_condition_include(
        ConditionCandidateEvent(
            condition_name="주도테마_넓은후보",
            code="A412350",
            condition_index=7,
            strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE.value,
            purpose="theme_broad_candidate",
        )
    )

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate is not None
    assert candidate.strategy_profile == StrategyProfile.THEME_DISCOVERY_PROFILE
    assert candidate.metadata["condition_profiles"] == {
        "주도테마_넓은후보": StrategyProfile.THEME_DISCOVERY_PROFILE.value
    }
    assert candidate.metadata["condition_purposes"] == {"주도테마_넓은후보": "theme_broad_candidate"}
    assert candidate.metadata["theme_discovery_condition_names"] == ["주도테마_넓은후보"]
    assert candidate.metadata["entry_condition_names"] == []
    assert candidate.metadata["entry_excluded"] is True
    assert db.list_candidate_events(candidate.id)[0].payload["condition_name"] == "주도테마_넓은후보"
    assert db.list_candidate_events(candidate.id)[0].payload["purpose"] == "theme_broad_candidate"
    db.close()


def test_entry_condition_clears_broad_condition_entry_exclusion(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    collector.handle_condition_include(
        ConditionCandidateEvent(
            condition_name="주도테마_넓은후보",
            code="412350",
            condition_index=7,
            strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE.value,
            purpose="theme_broad_candidate",
        )
    )
    collector.handle_condition_include(
        ConditionCandidateEvent(
            condition_name="코스닥_테마주_눌림",
            code="412350",
            condition_index=8,
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            purpose="kosdaq_pullback_entry",
        )
    )

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.strategy_profile == StrategyProfile.KOSDAQ_THEME_PROFILE
    assert candidate.metadata["entry_excluded"] is False
    assert candidate.metadata["theme_discovery_condition_names"] == ["주도테마_넓은후보"]
    assert candidate.metadata["entry_condition_names"] == ["코스닥_테마주_눌림"]

    collector.handle_condition_remove(
        ConditionCandidateEvent(
            condition_name="코스닥_테마주_눌림",
            code="412350",
            condition_index=8,
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            purpose="kosdaq_pullback_entry",
        )
    )

    reloaded = db.load_candidate("2026-05-29", "412350")
    assert reloaded.metadata["entry_excluded"] is True
    assert reloaded.metadata["entry_condition_names"] == []
    db.close()


def test_duplicate_condition_includes_merge_into_single_candidate(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마"), (2, "코스닥테마주")])

    client.emit_condition_include("주도테마", "412350")
    client.emit_condition_include("주도테마", "412350")
    client.emit_condition_include("코스닥테마주", "412350")

    candidates = db.list_candidates("2026-05-29")
    assert len(candidates) == 1
    assert candidates[0].condition_names == ["주도테마", "코스닥테마주"]
    assert candidates[0].metadata["condition_indices"] == {"주도테마": 1, "코스닥테마주": 2}
    assert event_types(db, candidates[0].id) == [
        "candidate_detected",
        "candidate_merged",
    ]
    db.close()


def test_invalid_condition_code_is_rejected_without_candidate_row(tmp_path):
    collector, _client, db, _clock = make_collector(tmp_path)

    result = collector.handle_condition_include(
        ConditionCandidateEvent(
            condition_name="코스닥테마주",
            code="0007C0",
            condition_index=1,
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE.value,
            purpose="kosdaq_pullback_candidate",
        )
    )

    rows = db.conn.execute("SELECT * FROM candidate_events").fetchall()
    assert result is None
    assert db.list_candidates("2026-05-29") == []
    assert rows[0]["candidate_id"] is None
    assert rows[0]["event_type"] == "candidate_rejected"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["raw_code"] == "0007C0"
    assert payload["normalized_code"] == "0007C0"
    assert "INVALID_CONDITION_CODE:코스닥테마주:0007C0" in collector.warnings
    db.close()


def test_partial_condition_remove_keeps_candidate(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마"), (2, "코스닥테마주")])
    client.emit_condition_include("주도테마", "412350")
    client.emit_condition_include("코스닥테마주", "412350")

    client.emit_condition_remove("주도테마", "412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.DETECTED
    assert candidate.sources == [CandidateSourceType.CONDITION]
    assert candidate.condition_names == ["코스닥테마주"]
    assert event_types(db, candidate.id)[-1] == "condition_removed"
    db.close()


def test_all_condition_sources_removed_marks_removed(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    client.emit_condition_include("주도테마", "412350")

    client.emit_condition_remove("주도테마", "412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.REMOVED
    assert candidate.sources == []
    assert candidate.condition_names == []
    assert event_types(db, candidate.id) == [
        "candidate_detected",
        "condition_removed",
        "candidate_removed",
    ]
    db.close()


def test_manual_debug_source_keeps_candidate_when_condition_removed(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    collector.add_manual_debug_candidate("412350", "레이저쎌")
    client.emit_condition_include("주도테마", "412350")

    client.emit_condition_remove("주도테마", "412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.DETECTED
    assert candidate.sources == [CandidateSourceType.MANUAL_DEBUG]
    assert candidate.condition_names == []
    assert db.load_watch_items() == []
    assert client.orders == []
    db.close()


def test_removed_and_expired_candidates_reactivate_on_include(tmp_path):
    collector, client, db, clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    client.emit_condition_include("주도테마", "412350")
    client.emit_condition_remove("주도테마", "412350")
    client.emit_condition_include("주도테마", "412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.DETECTED
    assert "candidate_reactivated" in event_types(db, candidate.id)

    clock.advance(minutes=10)
    collector.expire_stale()
    client.emit_condition_include("주도테마", "412350")

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.DETECTED
    assert event_types(db, candidate.id).count("candidate_reactivated") == 2
    db.close()


def test_expire_stale_does_not_duplicate_expired_events(tmp_path):
    collector, client, db, clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    client.emit_condition_include("주도테마", "412350")

    clock.advance(minutes=10)
    collector.expire_stale()
    collector.expire_stale()

    candidate = db.load_candidate("2026-05-29", "412350")
    assert candidate.state == CandidateState.EXPIRED
    assert event_types(db, candidate.id).count("candidate_expired") == 1
    db.close()


def test_mark_watching_and_forbidden_transitions(tmp_path):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    client.emit_condition_include("주도테마", "412350")

    candidate = collector.mark_watching("412350")

    assert candidate.state == CandidateState.WATCHING
    assert event_types(db, candidate.id)[-1] == "state_changed"
    CandidateLifecycle.validate_transition(candidate.state, CandidateState.READY)
    with pytest.raises(ValueError):
        CandidateLifecycle.validate_transition(CandidateState.DETECTED, CandidateState.READY)
    for state in [
        CandidateState.ORDER_DECIDED,
        CandidateState.ORDER_SENT,
        CandidateState.FILLED,
        CandidateState.CANCELLED,
    ]:
        with pytest.raises(ValueError):
            CandidateLifecycle.validate_transition(candidate.state, state)
    db.close()


def test_candidate_and_event_are_saved_transactionally(tmp_path, monkeypatch):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    client.emit_condition_include("주도테마", "412350")
    original = db.load_candidate("2026-05-29", "412350")
    assert original.state == CandidateState.DETECTED

    def fail_event_insert(event):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(db, "_save_candidate_event_no_commit", fail_event_insert)
    with pytest.raises(RuntimeError):
        collector.mark_watching("412350")

    reloaded = db.load_candidate("2026-05-29", "412350")
    assert reloaded.state == CandidateState.DETECTED
    assert event_types(db, reloaded.id) == ["candidate_detected"]
    db.close()


def test_collector_never_calls_send_order(tmp_path, monkeypatch):
    collector, client, db, _clock = make_collector(tmp_path)
    client.set_conditions([(1, "주도테마")])
    calls = []

    def fail_send_order(request):
        calls.append(request)
        raise AssertionError("CandidateCollector must not call send_order")

    monkeypatch.setattr(client, "send_order", fail_send_order)
    client.emit_condition_include("주도테마", "412350")
    collector.add_manual_debug_candidate("005930")
    collector.mark_watching("412350")

    assert calls == []
    assert client.orders == []
    db.close()
