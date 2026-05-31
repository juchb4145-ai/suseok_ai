from kiwoom.client import MockKiwoomClient
from trading.strategy.models import Candidate, CandidateState
from trading.strategy.realtime import RealTimeSubscriptionManager


def candidate(code, state=CandidateState.DETECTED):
    return Candidate(trade_date="2026-05-29", code=code, state=state)


def test_subscription_registers_unique_candidate_codes():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)

    registered = manager.watch_candidates([candidate("A005930"), candidate("005930")])

    assert registered == ["005930"]
    assert client.registered_codes == {"005930"}
    assert manager.code_to_screen["005930"] == "7000"


def test_subscription_limit_returns_only_successfully_registered_candidate_codes():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=2)
    manager.ensure_subscription("000660", "leading_stock")

    registered = manager.watch_candidates([
        candidate("005930"),
        candidate("035420"),
        candidate("412350"),
    ])

    assert "000660" in client.registered_codes
    assert len(client.registered_codes) == 2
    assert set(registered).issubset(client.registered_codes)
    assert len(registered) == 1


def test_theme_universe_source_is_non_protected_and_below_candidate_priority():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=1)
    manager.ensure_subscription("000001", "theme_universe")

    registered = manager.watch_candidates([candidate("000002")])

    assert registered == ["000002"]
    assert client.registered_codes == {"000002"}
    assert manager.records["000001"].protected is False
    assert manager.records["000001"].priority < manager.records["000002"].priority


def test_expired_candidate_does_not_unregister_remaining_leading_subscription():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)
    manager.ensure_subscription("005930", "leading_stock")
    manager.watch_candidates([candidate("005930")])
    assert "005930" in client.registered_codes

    registered = manager.watch_candidates([candidate("005930", CandidateState.EXPIRED)])

    assert registered == []
    assert "005930" in client.registered_codes
    assert "candidate_watch" not in manager.records["005930"].sources
    assert "leading_stock" in manager.records["005930"].sources


def test_multiple_sources_merge_to_single_protected_record():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)

    manager.ensure_subscription("005930", "leading_stock")
    manager.ensure_subscription("A005930", "semiconductor_signal")
    manager.ensure_subscription("005930", "candidate_watch")
    manager.sync()

    record = manager.records["005930"]
    assert record.sources == {"leading_stock", "semiconductor_signal", "candidate_watch"}
    assert record.priority == 90
    assert record.protected is True
    assert client.registered_codes == {"005930"}

    manager.remove_subscription("005930", "candidate_watch")
    manager.sync()

    assert "005930" in client.registered_codes
    assert manager.records["005930"].sources == {"leading_stock", "semiconductor_signal"}


def test_protected_over_limit_warns_and_candidate_watch_can_drop_to_zero():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=1)
    manager.ensure_subscription("001", "index")
    manager.ensure_subscription("101", "index")

    registered = manager.watch_candidates([candidate("005930")])

    assert registered == []
    assert client.registered_codes == {"001", "101"}
    assert "PROTECTED_SUBSCRIPTION_OVER_LIMIT" in manager.warnings


def test_individual_remove_uses_screen_mapping():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)
    manager.watch_candidates([candidate("005930")])

    manager.remove_subscription("005930", "candidate_watch")
    manager.sync()

    assert "005930" not in client.registered_codes
    assert client.removed_codes == ["005930"]
    assert client.remove_all_count == 0


def test_remove_all_fallback_reregisters_protected_records_when_screen_mapping_missing():
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)
    manager.ensure_subscription("001", "index")
    manager.ensure_subscription("005930", "leading_stock")
    manager.ensure_subscription("035420", "holding")
    manager.sync()
    manager.code_to_screen.pop("005930")

    manager.remove_realtime(["005930"])

    assert client.remove_all_count == 1
    assert client.registered_code_order[:3] == ["001", "005930", "035420"]
    assert "REALTIME_REMOVE_ALL_FALLBACK" in manager.warnings
    assert "005930" in client.registered_codes


def test_realtime_manager_does_not_call_send_order(monkeypatch):
    client = MockKiwoomClient()
    manager = RealTimeSubscriptionManager(client, max_codes=10)
    calls = []

    def fail_send_order(request):
        calls.append(request)
        raise AssertionError("realtime manager must not call send_order")

    monkeypatch.setattr(client, "send_order", fail_send_order)
    manager.watch_candidates([candidate("005930")])

    assert calls == []
    assert client.orders == []
