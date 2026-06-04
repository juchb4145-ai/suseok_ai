from datetime import datetime, timedelta

from kiwoom.client import MockKiwoomClient
from trading.strategy.bridge import StrategyMarketDataBridge
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_index import IndexCodeMapper, MarketIndexStore
from trading.strategy.market_data import MarketDataStore


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def test_bridge_normalizes_mock_tick_and_updates_market_data_and_candles():
    client = MockKiwoomClient()
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    store = MarketDataStore()
    builder = CandleBuilder()
    bridge = StrategyMarketDataBridge(store, builder, clock=clock)
    bridge.attach(client)

    client.emit_price("A005930", -80_000, change_rate=1.2, volume=1_000)
    clock.set(datetime(2026, 5, 29, 9, 1, 0))
    client.emit_price("005930", -80_500, change_rate=1.3, volume=1_100)

    latest = store.latest_tick("005930")
    assert latest.price == 80_500
    assert latest.cum_volume == 1_100
    assert builder.completed_candles("005930", 1)[0].close == 80_000


def test_bridge_does_not_call_send_order(monkeypatch):
    client = MockKiwoomClient()
    calls = []

    def fail_send_order(request):
        calls.append(request)
        raise AssertionError("bridge must not call send_order")

    monkeypatch.setattr(client, "send_order", fail_send_order)
    bridge = StrategyMarketDataBridge(MarketDataStore(), CandleBuilder(), clock=lambda: datetime(2026, 5, 29, 9, 0))
    bridge.attach(client)

    client.emit_price("005930", 80_000, volume=1_000)

    assert calls == []
    assert client.orders == []


def test_bridge_routes_index_ticks_only_to_market_index_store():
    client = MockKiwoomClient()
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    stock_store = MarketDataStore()
    stock_builder = CandleBuilder()
    index_store = MarketIndexStore()
    bridge = StrategyMarketDataBridge(stock_store, stock_builder, index_store, IndexCodeMapper(), clock=clock)
    bridge.attach(client)

    client.emit_price("101", 950, change_rate=0.5, volume=0, instrument_type="index", name="KOSDAQ")

    assert index_store.state("KOSDAQ").price == 950
    assert stock_store.latest_tick("101") is None
    assert stock_builder.completed_candles("101", 1) == []


def test_bridge_routes_stock_ticks_only_to_market_data_store():
    client = MockKiwoomClient()
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    stock_store = MarketDataStore()
    stock_builder = CandleBuilder()
    index_store = MarketIndexStore()
    bridge = StrategyMarketDataBridge(stock_store, stock_builder, index_store, clock=clock)
    bridge.attach(client)

    client.emit_price("005930", 80_000, change_rate=1.2, volume=1_000, instrument_type="stock")

    assert stock_store.latest_tick("005930").price == 80_000
    assert index_store.state("005930").price == 0


def test_bridge_preserves_rich_tick_metadata_and_realtime_features():
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    stock_store = MarketDataStore()
    bridge = StrategyMarketDataBridge(stock_store, CandleBuilder(), clock=clock)

    assert bridge.on_realtime_tick(
        "005930",
        price=80_000,
        change_rate=1.2,
        cum_volume=1_000,
        best_ask=80_100,
        best_bid=80_000,
        trade_value=80_000_000,
        execution_strength=123.4,
        spread_ticks=1,
        day_high=81_000,
        day_low=79_500,
        trade_time="093001",
        metadata={"reason_codes": ["SPREAD_APPROXIMATED"], "raw_fids_present": [10, 14, 228]},
    ) is True

    latest = stock_store.latest_tick("005930")
    assert latest.trade_value == 80_000_000
    assert latest.execution_strength == 123.4
    assert latest.spread_ticks == 1
    assert latest.metadata["session_high"] == 81_000
    assert latest.metadata["session_low"] == 79_500
    assert latest.metadata["trade_time"] == "093001"
    assert latest.metadata["raw_fids_present"] == [10, 14, 228]
    assert latest.metadata["momentum_1m"] == 0.0
    assert latest.metadata["turnover_strength"] == 1.0
    assert latest.metadata["minute_bar_present"] is True
    assert latest.metadata["recent_candles_1m"][0]["completed"] is False
    assert latest.metadata["recent_support_source"] == "active_1m_low_provisional"
    assert "MOMENTUM_WARMUP" in latest.metadata["reason_codes"]
    assert "SPREAD_APPROXIMATED" in latest.metadata["reason_codes"]

    quality = bridge.data_quality_snapshot()
    assert quality["total_price_ticks"] == 1
    assert quality["field_coverage"]["trade_value"] == 1.0
    assert quality["field_coverage"]["momentum"] == 1.0


def test_bridge_uses_previous_valid_price_for_zero_price_candles():
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    stock_store = MarketDataStore()
    builder = CandleBuilder()
    bridge = StrategyMarketDataBridge(stock_store, builder, clock=clock)

    assert bridge.on_realtime_tick("005930", price=1000, cum_volume=100, trade_value=100000) is True
    clock.set(datetime(2026, 5, 29, 9, 0, 20))
    assert bridge.on_realtime_tick("005930", price=0, cum_volume=120, trade_value=120000) is True

    active = builder.active_candle("005930")
    latest = stock_store.latest_tick("005930")
    assert active is not None
    assert active.low == 1000
    assert active.close == 1000
    assert latest is not None
    assert latest.price == 1000
    assert latest.metadata["merged_from_previous_price_tick"] is True


def test_bridge_can_infer_index_type_from_mapper_without_polluting_stock_candles():
    client = MockKiwoomClient()
    clock = MutableClock(datetime(2026, 5, 29, 9, 0, 1))
    stock_store = MarketDataStore()
    stock_builder = CandleBuilder()
    index_store = MarketIndexStore()
    bridge = StrategyMarketDataBridge(stock_store, stock_builder, index_store, clock=clock)
    bridge.attach(client)

    client.emit_price("001", 2800, change_rate=-0.2, volume=0, instrument_type=None, name="KOSPI")

    assert index_store.state("KOSPI").price == 2800
    assert stock_store.latest_tick("001") is None
