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


def test_subscription_readiness_provider_reports_active_fresh_realtime_tick():
    clock = _Clock(datetime(2026, 6, 22, 9, 4, 0))
    manager = RealTimeSubscriptionManager(MockKiwoomClient(), max_codes=10, clock=clock)
    manager.ensure_subscription("000001", "reboot_v2_candidate")
    manager.sync()

    market_data = MarketDataStore()
    market_data.update_tick(
        StrategyTick.from_realtime(
            "000001",
            price=1000,
            change_rate=4.5,
            cum_volume=1000,
            trade_value=1_000_000,
            execution_strength=130,
            timestamp=datetime(2026, 6, 22, 9, 5, 5),
            metadata={"price_source": "REALTIME"},
        )
    )

    provider = RealtimeSubscriptionReadinessProvider(manager, market_data=market_data, clock=clock, max_tick_age_sec=10)
    snapshot = provider.snapshot("000001", now=datetime(2026, 6, 22, 9, 5, 10))

    assert snapshot["subscription_selected"] is True
    assert snapshot["subscription_active"] is True
    assert snapshot["subscription_generation"] == 1
    assert snapshot["subscription_active_since"] == "2026-06-22T09:04:00"
    assert snapshot["relevant_source_added_at"] == "2026-06-22T09:04:00"
    assert snapshot["latest_tick_at"] == "2026-06-22T09:05:05"
    assert snapshot["latest_tick_age_sec"] == 5.0
    assert snapshot["latest_tick_source"] == "REALTIME"
    assert snapshot["post_subscription_tick_verified"] is True
    assert snapshot["coverage_type"] == "CANDIDATE"


def test_subscription_readiness_provider_separates_budget_deferred_from_stale_tick():
    clock = _Clock(datetime(2026, 6, 22, 9, 4, 0))
    manager = RealTimeSubscriptionManager(MockKiwoomClient(), max_codes=1, clock=clock)
    manager.ensure_subscription("000001", "reboot_v2_index")
    manager.ensure_subscription("000002", "reboot_v2_candidate")
    manager.sync()

    provider = RealtimeSubscriptionReadinessProvider(manager, market_data=MarketDataStore(), clock=clock, max_tick_age_sec=10)
    snapshot = provider.snapshot("000002", now=datetime(2026, 6, 22, 9, 5, 10))

    assert snapshot["subscription_selected"] is True
    assert snapshot["subscription_active"] is False
    assert snapshot["subscription_budget_deferred"] is True
    assert snapshot["latest_tick_at"] == ""
