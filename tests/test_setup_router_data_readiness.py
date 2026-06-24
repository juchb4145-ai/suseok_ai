from datetime import datetime
from types import SimpleNamespace

from trading.strategy.setup_data_readiness import (
    GENERAL_SUBSCRIPTION_READY,
    SELECTED_THEME_LEASE_NOT_REQUIRED,
    SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING,
    SUBSCRIPTION_BUDGET_DEFERRED,
    SetupDataReadinessStatus,
    build_setup_data_readiness,
)

from tests.test_setup_router_v3 import TRADE_DATE, _context


def _candidate(**metadata):
    return SimpleNamespace(
        id=1,
        trade_date=TRADE_DATE,
        code="000001",
        name="테스트",
        metadata=dict(metadata),
        detected_at="2026-06-22T09:03:00",
    )


def _subscription(**overrides):
    base = {
        "code": "000001",
        "subscription_selected": True,
        "subscription_active": True,
        "subscription_sources": ["opening_burst", "theme_board"],
        "subscription_primary_source": "opening_burst",
        "subscription_generation": 1,
        "subscription_active_since": "2026-06-22T09:04:00",
        "relevant_source_added_at": "2026-06-22T09:04:00",
        "coverage_type": "MULTI_SOURCE",
        "latest_tick_at": "2026-06-22T09:05:05",
        "latest_tick_age_sec": 5.0,
        "latest_tick_source": "REALTIME",
    }
    base.update(overrides)
    return base


def _readiness(candidate=None, subscription=None, exact_lease=None, context=None, **kwargs):
    return build_setup_data_readiness(
        trade_date=TRADE_DATE,
        calculated_at=datetime(2026, 6, 22, 9, 5, 10),
        candidate=candidate or _candidate(),
        candidate_instance_id="ci-000001",
        selected_theme_id="ai",
        context=context or _context(),
        subscription=subscription or _subscription(),
        exact_lease=exact_lease or {},
        other_theme_lease_count=kwargs.pop("other_theme_lease_count", 0),
        evaluation_eligible=True,
        max_tick_age_sec=10,
        min_completed_1m_candles=3,
        completed_1m_count=3,
        **kwargs,
    )


def test_general_realtime_subscription_ready_without_selected_theme_lease():
    snapshot = _readiness(other_theme_lease_count=2)

    assert snapshot.readiness_status == SetupDataReadinessStatus.READY.value
    assert snapshot.readiness_ready is True
    assert snapshot.expansion_lease_required is False
    assert snapshot.post_subscription_tick_verified is True
    assert SELECTED_THEME_LEASE_NOT_REQUIRED in snapshot.informational_reason_codes
    assert GENERAL_SUBSCRIPTION_READY in snapshot.informational_reason_codes


def test_expansion_only_candidate_requires_exact_active_selected_theme_lease():
    snapshot = _readiness(candidate=_candidate(expansion_only=True))

    assert snapshot.readiness_status == SetupDataReadinessStatus.WAIT_SELECTED_THEME_LEASE.value
    assert snapshot.readiness_ready is False
    assert snapshot.expansion_lease_required is True
    assert SETUP_SELECTED_THEME_ACTIVE_LEASE_MISSING in snapshot.reason_codes


def test_subscription_budget_deferred_is_not_misclassified_as_tick_stale():
    snapshot = _readiness(subscription=_subscription(subscription_budget_deferred=True))

    assert snapshot.readiness_status == SetupDataReadinessStatus.WAIT_SUBSCRIPTION_BUDGET.value
    assert snapshot.reason_codes == (SUBSCRIPTION_BUDGET_DEFERRED,)


def test_active_subscription_requires_post_subscription_realtime_tick():
    snapshot = _readiness(
        subscription=_subscription(
            subscription_active_since="2026-06-22T09:05:08",
            relevant_source_added_at="2026-06-22T09:05:08",
            latest_tick_at="2026-06-22T09:05:05",
            latest_tick_age_sec=5.0,
        )
    )

    assert snapshot.readiness_status == SetupDataReadinessStatus.WAIT_POST_SUBSCRIPTION_TICK.value
    assert snapshot.post_subscription_tick_verified is False
    assert "ACTIVE_SUBSCRIPTION_NO_POST_ACTIVE_TICK" in snapshot.reason_codes
