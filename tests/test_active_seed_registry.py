from datetime import datetime, timedelta

from trading.theme_engine.signal_registry import ActiveSeedRegistry, ActiveSeedSource
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType


def test_active_seed_registry_expires_stale_sources_by_ttl():
    now = datetime(2026, 6, 19, 9, 20, 0)
    registry = ActiveSeedRegistry(ttl_sec=60)

    registry.merge(
        LiveSeedSignal(
            code="000001",
            source_types=(SeedSourceType.OPT10032.value,),
            change_rate_pct=5.0,
            turnover_krw=10_000_000_000,
            observed_at=now.isoformat(),
            last_seen_at=now.isoformat(),
        ),
        now=now,
    )

    active = registry.snapshot(now=now + timedelta(seconds=30))
    expired = registry.snapshot(now=now + timedelta(seconds=61))

    assert active.active_count == 1
    assert expired.active_count == 0
    assert expired.expired_count == 1
    assert "SEED_TTL_EXPIRED" in expired.expired_signals[0].reason_codes


def test_active_seed_registry_remove_source_does_not_remove_other_sources():
    now = datetime(2026, 6, 19, 9, 20, 0)
    registry = ActiveSeedRegistry(ttl_sec=600)

    registry.merge(
        {
            "code": "000001",
            "source_type": ActiveSeedSource.CONDITION.value,
            "source_id": "cond:leader",
            "observed_at": now.isoformat(),
        },
        now=now,
    )
    registry.merge(
        {
            "code": "000001",
            "source_type": ActiveSeedSource.INTRADAY.value,
            "source_id": "opt10032:09:20",
            "observed_at": now.isoformat(),
        },
        now=now,
    )

    removed = registry.remove_source(
        "000001",
        ActiveSeedSource.CONDITION.value,
        "cond:leader",
        now=now + timedelta(seconds=1),
    )
    snapshot = registry.snapshot(now=now + timedelta(seconds=1))

    assert removed is True
    assert snapshot.active_count == 1
    assert snapshot.active_signals[0].source_type == ActiveSeedSource.INTRADAY.value
