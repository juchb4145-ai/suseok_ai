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
