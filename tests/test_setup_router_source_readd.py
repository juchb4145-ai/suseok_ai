from datetime import datetime

from kiwoom.client import MockKiwoomClient
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.subscription_readiness import RealtimeSubscriptionReadinessProvider


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _tick(code: str, at: datetime) -> StrategyTick:
    return StrategyTick.from_realtime(
        code,
        price=1000,
        change_rate=3.0,
        cum_volume=1000,
        trade_value=1_000_000,
        execution_strength=120,
        timestamp=at,
        metadata={"price_source": "REALTIME"},
    )


def test_subscription_source_readd_refreshes_source_epoch_and_generation():
    clock = _Clock(datetime(2026, 6, 22, 9, 0, 0))
    manager = RealTimeSubscriptionManager(MockKiwoomClient(), max_codes=10, clock=clock)

    record = manager.ensure_subscription("000001", "reboot_v2_candidate")
    first_added = record.source_added_at_by_source["reboot_v2_candidate"]
    assert record.source_generation_by_source["reboot_v2_candidate"] == 1

    clock.value = datetime(2026, 6, 22, 9, 1, 0)
    record = manager.ensure_subscription("000001", "reboot_v2_candidate")
    assert record.source_added_at_by_source["reboot_v2_candidate"] == first_added
    assert record.source_generation_by_source["reboot_v2_candidate"] == 1

    clock.value = datetime(2026, 6, 22, 9, 2, 0)
    manager.remove_subscription("000001", "reboot_v2_candidate")
    assert "000001" not in manager.records

    clock.value = datetime(2026, 6, 22, 9, 3, 0)
    record = manager.ensure_subscription("000001", "reboot_v2_candidate")

    assert record.source_added_at_by_source["reboot_v2_candidate"] == "2026-06-22T09:03:00"
    assert record.source_generation_by_source["reboot_v2_candidate"] == 2


def test_readd_before_tick_is_not_post_subscription_tick():
    clock = _Clock(datetime(2026, 6, 22, 9, 0, 0))
    manager = RealTimeSubscriptionManager(MockKiwoomClient(), max_codes=10, clock=clock)
    manager.ensure_subscription("000001", "reboot_v2_candidate")
    manager.ensure_subscription("000001", "reboot_v2_opening_seed")
    manager.sync()

    clock.value = datetime(2026, 6, 22, 9, 2, 0)
    manager.remove_subscription("000001", "reboot_v2_candidate")
    clock.value = datetime(2026, 6, 22, 9, 3, 0)
    manager.ensure_subscription("000001", "reboot_v2_candidate")

    market_data = MarketDataStore()
    market_data.update_tick(_tick("000001", datetime(2026, 6, 22, 9, 2, 30)))
    provider = RealtimeSubscriptionReadinessProvider(manager, market_data=market_data, clock=clock, max_tick_age_sec=120)
    stale = provider.snapshot("000001", now=datetime(2026, 6, 22, 9, 3, 10))

    assert stale["readiness_relevant_source"] == "reboot_v2_candidate"
    assert stale["readiness_relevant_source_generation"] == 2
    assert stale["relevant_source_added_at"] == "2026-06-22T09:03:00"
    assert stale["post_subscription_tick_verified"] is False

    market_data.update_tick(_tick("000001", datetime(2026, 6, 22, 9, 3, 1)))
    fresh = provider.snapshot("000001", now=datetime(2026, 6, 22, 9, 3, 10))

    assert fresh["post_subscription_tick_verified"] is True


def test_general_multisource_uses_candidate_source_before_expansion_source():
    clock = _Clock(datetime(2026, 6, 22, 9, 2, 0))
    manager = RealTimeSubscriptionManager(MockKiwoomClient(), max_codes=10, clock=clock)
    manager.ensure_subscription("000001", "reboot_v2_candidate")
    manager.sync()
    clock.value = datetime(2026, 6, 22, 9, 10, 0)
    manager.ensure_subscription("000001", "reboot_v2_theme_expansion")

    market_data = MarketDataStore()
    market_data.update_tick(_tick("000001", datetime(2026, 6, 22, 9, 8, 0)))
    provider = RealtimeSubscriptionReadinessProvider(manager, market_data=market_data, clock=clock, max_tick_age_sec=300)
    snapshot = provider.snapshot("000001", selected_theme_id="ai", now=datetime(2026, 6, 22, 9, 10, 10))

    assert snapshot["readiness_relevant_source"] == "reboot_v2_candidate"
    assert snapshot["baseline_source_type"] == "reboot_v2_candidate"
    assert snapshot["post_subscription_tick_verified"] is True
