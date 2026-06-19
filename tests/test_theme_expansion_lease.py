from datetime import datetime, timedelta

from trading.theme_engine.expansion import FocusedExpansionTarget
from trading.theme_engine.expansion_lease import ExpansionLeaseManager, ExpansionLeaseStatus


def test_expansion_lease_respects_minimum_hold_before_removal():
    manager = ExpansionLeaseManager()
    now = datetime(2026, 6, 19, 9, 20, 0)
    target = FocusedExpansionTarget(code="000001", theme_id="ai", minimum_hold_sec=60, subscription_ttl_sec=90)

    manager.reconcile([target], now=now)
    snapshot = manager.reconcile([], now=now + timedelta(seconds=30))

    assert snapshot.active_lease_count == 1
    assert snapshot.leases[0].status == ExpansionLeaseStatus.HOLDING.value
    assert manager.removable_codes() == []


def test_expansion_lease_expires_after_ttl_when_not_eligible():
    manager = ExpansionLeaseManager()
    now = datetime(2026, 6, 19, 9, 20, 0)
    target = FocusedExpansionTarget(code="000001", theme_id="ai", minimum_hold_sec=10, subscription_ttl_sec=30)

    manager.reconcile([target], now=now)
    snapshot = manager.reconcile([], now=now + timedelta(seconds=31))

    assert snapshot.expired_count == 1
    assert snapshot.leases[0].status == ExpansionLeaseStatus.EXPIRED.value
    assert manager.removable_codes() == ["000001"]


def test_expansion_lease_retains_code_while_any_theme_lease_is_active_after_restore():
    now = datetime(2026, 6, 19, 9, 20, 0)
    ai = FocusedExpansionTarget(code="000001", theme_id="ai", minimum_hold_sec=10, subscription_ttl_sec=30)
    robot = FocusedExpansionTarget(code="000001", theme_id="robot", minimum_hold_sec=10, subscription_ttl_sec=90)
    manager = ExpansionLeaseManager()

    first = manager.reconcile([ai, robot], now=now)
    restored = ExpansionLeaseManager()
    restored.restore([lease.__dict__ for lease in first.leases])
    snapshot = restored.reconcile([robot], now=now + timedelta(seconds=31))

    statuses = {(lease.theme_id, lease.status) for lease in snapshot.leases}
    assert ("ai", ExpansionLeaseStatus.EXPIRED.value) in statuses
    assert ("robot", ExpansionLeaseStatus.ACTIVE.value) in statuses
    assert restored.removable_codes() == []


def test_expansion_lease_rejects_pre_subscription_tick_as_first_fresh_tick():
    manager = ExpansionLeaseManager()
    now = datetime(2026, 6, 19, 9, 20, 0)
    target = FocusedExpansionTarget(code="000001", theme_id="ai", minimum_hold_sec=10, subscription_ttl_sec=90)

    snapshot = manager.reconcile(
        [target],
        now=now,
        active_codes=["000001"],
        fresh_tick_events={"000001": {"tick_at": "2026-06-19T09:19:59", "source_event_id": "old-tick"}},
    )

    lease = snapshot.leases[0]
    assert lease.first_active_at == now.isoformat()
    assert lease.first_fresh_tick_at == ""
    assert "THEME_EXPANSION_TICK_READY" not in lease.reason_codes


def test_expansion_lease_records_first_post_subscription_fresh_tick_once():
    manager = ExpansionLeaseManager()
    now = datetime(2026, 6, 19, 9, 20, 0)
    target = FocusedExpansionTarget(code="000001", theme_id="ai", minimum_hold_sec=10, subscription_ttl_sec=90)

    manager.reconcile([target], now=now, active_codes=["000001"])
    first = manager.reconcile(
        [target],
        now=now + timedelta(seconds=1),
        active_codes=["000001"],
        fresh_tick_events={"000001": {"tick_at": "2026-06-19T09:20:01", "source_event_id": "tick-1"}},
    )
    second = manager.reconcile(
        [target],
        now=now + timedelta(seconds=2),
        active_codes=["000001"],
        fresh_tick_events={"000001": {"tick_at": "2026-06-19T09:20:02", "source_event_id": "tick-2"}},
    )

    assert first.leases[0].first_fresh_tick_at == "2026-06-19T09:20:01"
    assert first.leases[0].first_post_subscription_tick_at == "2026-06-19T09:20:01"
    assert first.leases[0].first_tick_source_event_id == "tick-1"
    assert second.leases[0].first_fresh_tick_at == "2026-06-19T09:20:01"
    assert second.leases[0].first_tick_source_event_id == "tick-1"
