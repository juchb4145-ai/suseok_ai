from datetime import datetime, timedelta

import pytest

from kiwoom.client import ConditionInfo, MockKiwoomClient, parse_condition_name_list
from storage.db import TradingDatabase
from trading.strategy.candidates import CandidateCollector
from trading.strategy.conditions import (
    DEFAULT_CONDITION_PROFILES,
    ConditionProfile,
    ConditionProfileRepository,
    KiwoomConditionAdapter,
    ensure_default_condition_profiles,
)
from trading.strategy.models import StrategyProfile


NOW = datetime(2026, 5, 29, 9, 0, 0)


class MutableClock:
    def __init__(self, value=NOW) -> None:
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs) -> None:
        self.value = self.value + timedelta(**kwargs)


def make_adapter(tmp_path, *, max_realtime_conditions=10, clock=None):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ConditionProfileRepository(db)
    client = MockKiwoomClient()
    clock = clock or MutableClock()
    adapter = KiwoomConditionAdapter(
        client,
        repo,
        clock=clock,
        max_realtime_conditions=max_realtime_conditions,
        condition_screen_base=7600,
        load_timeout_sec=5,
        dedupe_window_sec=3,
    )
    return adapter, repo, client, db, clock


def upsert_profile(
    repo,
    name,
    priority=0,
    enabled=True,
    strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
    purpose=None,
):
    return repo.upsert_profile(
        ConditionProfile(
            condition_name=name,
            strategy_profile=strategy_profile,
            enabled=enabled,
            priority=priority,
            purpose=purpose if purpose is not None else f"purpose-{name}",
        )
    )


def test_parse_condition_name_list():
    assert parse_condition_name_list("1^leader;2^kosdaq;") == [
        ConditionInfo(1, "leader"),
        ConditionInfo(2, "kosdaq"),
    ]


def test_ensure_default_condition_profiles_seeds_missing_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ConditionProfileRepository(db)
    existing_name = "코스닥_테마주_눌림"
    repo.upsert_profile(
        ConditionProfile(
            condition_name=existing_name,
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=False,
            priority=11,
            purpose="kosdaq_pullback_candidate",
        )
    )

    result = ensure_default_condition_profiles(db)

    profiles = {profile.condition_name: profile for profile in db.list_condition_profiles(enabled=None)}
    assert result.inserted == len(DEFAULT_CONDITION_PROFILES) - 1
    assert result.existing == 1
    assert set(profiles) == {profile.condition_name for profile in DEFAULT_CONDITION_PROFILES}
    assert profiles[existing_name].enabled is False
    assert profiles[existing_name].priority == 11
    db.close()


def test_ensure_default_condition_profiles_repairs_known_profile_drift(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ConditionProfileRepository(db)
    repo.upsert_profile(
        ConditionProfile(
            condition_name="주도테마_넓은후보",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=False,
            priority=12,
            purpose="theme_broad_candidate",
            last_resolved_index=80,
        )
    )

    result = ensure_default_condition_profiles(db)
    saved = {profile.condition_name: profile for profile in db.list_condition_profiles(enabled=None)}["주도테마_넓은후보"]

    assert saved.strategy_profile == StrategyProfile.THEME_DISCOVERY_PROFILE
    assert saved.purpose == "theme_broad_candidate"
    assert saved.enabled is False
    assert saved.priority == 12
    assert saved.last_resolved_index == 80
    assert "CONDITION_PROFILE_DEFAULT_UPDATED:주도테마_넓은후보" in result.warnings
    db.close()


def test_ensure_default_condition_profiles_warns_suspicious_rows(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ConditionProfileRepository(db)
    repo.upsert_profile(
        ConditionProfile(
            condition_name="ì½ìŠ¤ë‹¥",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
            priority=1,
            purpose="",
        )
    )
    repo.upsert_profile(
        ConditionProfile(
            condition_name="unknown-purpose",
            strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
            enabled=True,
            priority=1,
            purpose="unexpected",
        )
    )

    result = ensure_default_condition_profiles(db)

    assert any(warning.startswith("CONDITION_PROFILE_PURPOSE_MISSING") for warning in result.warnings)
    assert any(warning.startswith("CONDITION_PROFILE_NAME_MOJIBAKE_SUSPECTED") for warning in result.warnings)
    assert any(warning.startswith("CONDITION_PROFILE_PURPOSE_UNKNOWN") for warning in result.warnings)
    db.close()


def test_default_condition_names_round_trip_with_unicode_escape(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    ensure_default_condition_profiles(db)

    names = {profile.condition_name for profile in db.list_condition_profiles(enabled=None)}
    for expected in [profile.condition_name for profile in DEFAULT_CONDITION_PROFILES]:
        assert expected in names
        escaped = expected.encode("unicode_escape").decode("ascii")
        assert escaped.encode("ascii").decode("unicode_escape") == expected
    db.close()


def test_theme_lab_condition_profiles_and_purposes_are_seeded(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    ensure_default_condition_profiles(db)

    profiles = {(profile.condition_name, profile.purpose) for profile in db.list_condition_profiles(enabled=None)}
    assert ("테마랩_생존_-1", "theme_lab_alive") in profiles
    assert ("테마랩_강세_3", "theme_lab_strong") in profiles
    assert ("테마랩_주도_5", "theme_lab_leader") in profiles
    db.close()


def test_condition_load_success_required_before_send_condition(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])

    adapter.start(NOW)

    assert client.condition_load_calls == 1
    assert client.send_condition_calls == []

    client.emit_condition_load_result(True, "ok")

    assert len(client.send_condition_calls) == 1
    assert client.send_condition_calls[0]["condition_name"] == "leader"
    assert db.list_condition_profiles(enabled=True)[0].last_resolved_index == 1
    db.close()


def test_korean_condition_profiles_round_trip_and_resolve_indexes(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    wide_name = "주도테마_넓은후보"
    entry_name = "코스닥_테마주_눌림"
    upsert_profile(
        repo,
        wide_name,
        priority=100,
        strategy_profile=StrategyProfile.THEME_DISCOVERY_PROFILE,
        purpose="theme_broad_candidate",
    )
    upsert_profile(
        repo,
        entry_name,
        priority=90,
        strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
        purpose="kosdaq_pullback_entry",
    )
    client.set_conditions([(17, wide_name), (23, entry_name)])

    saved = {profile.condition_name: profile for profile in db.list_condition_profiles(enabled=True)}
    assert set(saved) == {wide_name, entry_name}
    assert saved[wide_name].condition_name == wide_name
    assert saved[entry_name].condition_name == entry_name

    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    resolved = {profile.condition_name: profile for profile in db.list_condition_profiles(enabled=True)}
    assert resolved[wide_name].last_resolved_index == 17
    assert resolved[entry_name].last_resolved_index == 23
    assert [call["condition_name"] for call in client.send_condition_calls] == [wide_name, entry_name]
    db.close()


def test_condition_load_failure_and_timeout_skip_registration(tmp_path):
    adapter, repo, client, db, clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])

    adapter.start(NOW)
    client.emit_condition_load_result(False, "failed")

    assert client.send_condition_calls == []
    assert any("CONDITION_LOAD_FAILED" in warning for warning in adapter.warnings)

    adapter2, repo2, client2, db2, clock2 = make_adapter(tmp_path / "timeout")
    upsert_profile(repo2, "leader")
    client2.set_conditions([(1, "leader")])
    adapter2.start(clock2())
    clock2.advance(seconds=6)
    adapter2.check_load_timeout(clock2())

    assert client2.send_condition_calls == []
    assert "CONDITION_LOAD_TIMEOUT" in adapter2.warnings
    db.close()
    db2.close()


def test_max_realtime_conditions_limits_by_priority(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path, max_realtime_conditions=2)
    for idx, name in enumerate(["low", "top", "mid"], start=1):
        upsert_profile(repo, name, priority={"top": 100, "mid": 50, "low": 1}[name])
        client.set_conditions([(1, "low"), (2, "top"), (3, "mid")])

    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    assert [call["condition_name"] for call in client.send_condition_calls] == ["top", "mid"]
    assert "CONDITION_PROFILE_SKIPPED_LIMIT:low" in adapter.warnings
    db.close()


def test_ambiguous_condition_name_skips_registration(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader"), (2, "leader")])

    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    assert client.send_condition_calls == []
    assert "CONDITION_PROFILE_AMBIGUOUS:leader" in adapter.warnings
    assert db.list_condition_profiles(enabled=True)[0].last_resolved_index is None
    db.close()


def test_tr_condition_emits_include_and_nnext_continuation(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader", strategy_profile=StrategyProfile.KOSPI_LEADER_PROFILE, purpose="leader_entry")
    client.set_conditions([(1, "leader")])
    included = []
    adapter.condition_candidate_included.connect(lambda event: included.append(event))
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    client.emit_tr_condition("7600", "A005930;000660;", "leader", 1, "2")

    assert [event.code for event in included] == ["005930", "000660"]
    assert included[0].strategy_profile == StrategyProfile.KOSPI_LEADER_PROFILE.value
    assert included[0].purpose == "leader_entry"
    assert client.send_condition_calls[-1]["search_type"] == 2
    db.close()


def test_real_condition_validation_and_include_remove_conversion(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    included = []
    removed = []
    adapter.condition_candidate_included.connect(lambda event: included.append(event))
    adapter.condition_candidate_removed.connect(lambda event: removed.append(event))
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    client.emit_real_condition("A005930", "I", "leader", 1)
    client.emit_real_condition("A005930", "D", "leader", 1)
    client.emit_real_condition("000660", "I", "unregistered", 99)
    client.emit_real_condition("000660", "I", "leader", 99)

    assert [event.code for event in included] == ["005930"]
    assert [event.code for event in removed] == ["005930"]
    assert any("UNREGISTERED_CONDITION_EVENT:unregistered:99" == warning for warning in adapter.warnings)
    assert any("CONDITION_EVENT_MISMATCH:leader:99" == warning for warning in adapter.warnings)
    db.close()


def test_initial_tr_and_real_i_duplicate_is_deduped_before_collector(tmp_path):
    adapter, repo, client, db, clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    collector = CandidateCollector(
        db,
        client=adapter,
        clock=clock,
        trade_date_provider=lambda: "2026-05-29",
    )
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    client.emit_tr_condition("7600", "A005930;", "leader", 1, "")
    client.emit_real_condition("005930", "I", "leader", 1)

    candidate = db.load_candidate("2026-05-29", "005930")
    assert candidate is not None
    assert [event.event_type for event in db.list_candidate_events(candidate.id)] == ["candidate_detected"]
    db.close()


def test_invalid_condition_code_warns_and_reaches_collector_rejection(tmp_path):
    adapter, repo, client, db, clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    collector = CandidateCollector(
        db,
        client=adapter,
        clock=clock,
        trade_date_provider=lambda: "2026-05-29",
    )
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    client.emit_tr_condition("7600", "0007C0;A005930;", "leader", 1, "")

    rejected = db.conn.execute("SELECT * FROM candidate_events WHERE event_type = 'candidate_rejected'").fetchall()
    assert db.load_candidate("2026-05-29", "0007C0") is None
    assert db.load_candidate("2026-05-29", "005930") is not None
    assert rejected[0]["candidate_id"] is None
    assert "INVALID_CONDITION_CODE:leader:0007C0" in adapter.warnings
    assert "INVALID_CONDITION_CODE:leader:0007C0" in collector.warnings
    db.close()


def test_send_condition_failure_is_not_registered_or_stopped(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    client.condition_send_failures.add(("leader", 1, 1))

    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")
    adapter.stop()

    assert adapter.registered_conditions == {}
    assert "CONDITION_REGISTER_FAILED:leader:1" in adapter.warnings
    assert client.stop_condition_calls == []
    db.close()


def test_adapter_stop_only_registered_conditions_and_is_idempotent(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    adapter.stop()
    adapter.stop()

    assert client.stop_condition_calls == [
        {"screen_no": "7600", "condition_name": "leader", "condition_index": 1}
    ]
    assert adapter.registered_conditions == {}
    assert adapter.screen_to_condition == {}
    db.close()


def test_condition_screen_range_is_separate_from_quote_realtime_default(tmp_path):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")

    assert client.send_condition_calls[0]["screen_no"] == "7600"
    assert client.send_condition_calls[0]["screen_no"] != "7000"
    db.close()


def test_condition_adapter_does_not_call_send_order(tmp_path, monkeypatch):
    adapter, repo, client, db, _clock = make_adapter(tmp_path)
    upsert_profile(repo, "leader")
    client.set_conditions([(1, "leader")])
    calls = []

    def fail_send_order(request):
        calls.append(request)
        raise AssertionError("condition adapter must not call send_order")

    monkeypatch.setattr(client, "send_order", fail_send_order)
    adapter.start(NOW)
    client.emit_condition_load_result(True, "ok")
    client.emit_tr_condition("7600", "005930;", "leader", 1, "")
    adapter.stop()

    assert calls == []
    assert client.orders == []
    db.close()
