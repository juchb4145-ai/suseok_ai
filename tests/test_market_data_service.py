from datetime import datetime, timedelta

from trading.broker.models import GatewayEvent
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_data_service import DirtyCodeQueue, DirtyReason, MarketDataService, MarketDataServiceConfig
from trading.strategy.market_index import MarketIndexStore
from trading_app.runtime_adapters import GatewayEventMarketDataBridge


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


def _service(clock: MutableClock, *, flush_store=None, batch_flush_enabled=False) -> MarketDataService:
    return MarketDataService(
        MarketDataStore(),
        CandleBuilder(),
        MarketIndexStore(),
        config=MarketDataServiceConfig(
            enabled=True,
            dirty_queue_enabled=True,
            batch_flush_enabled=batch_flush_enabled,
            max_tick_age_sec=10,
            dirty_debounce_ms=0,
        ),
        clock=clock,
        flush_store=flush_store,
    )


def test_price_tick_payload_normalizes_to_market_data_snapshot() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 1))
    service = _service(clock)

    assert service.handle_price_tick(
        {
            "code": "A005930",
            "name": "삼성전자",
            "price": "-70000",
            "change_rate": "1.2",
            "cum_volume": "1000",
            "trade_value": "70000000",
            "execution_strength": "123.4",
            "best_ask": "70100",
            "best_bid": "70000",
            "spread_ticks": "1",
            "day_high": "71000",
            "day_low": "69000",
            "open_price": "69500",
        },
        source_event_id="evt-1",
    ) is True

    snapshot = service.latest_snapshot("005930")
    assert snapshot is not None
    assert snapshot.code == "005930"
    assert snapshot.name == "삼성전자"
    assert snapshot.price == 70000
    assert snapshot.change_rate == 1.2
    assert snapshot.trade_value == 70_000_000
    assert snapshot.cum_volume == 1000
    assert snapshot.execution_strength == 123.4
    assert snapshot.best_ask == 70100
    assert snapshot.best_bid == 70000
    assert snapshot.spread_ticks == 1
    assert snapshot.day_high == 71000
    assert snapshot.day_low == 69000
    assert snapshot.open_price == 69500
    assert snapshot.freshness_status == "FRESH"
    assert snapshot.data_quality_status == "OK"
    assert snapshot.source_event_id == "evt-1"
    assert snapshot.price_source == "REALTIME"


def test_market_data_service_updates_existing_market_data_store_and_candles() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 1))
    market_data = MarketDataStore()
    candle_builder = CandleBuilder()
    service = MarketDataService(
        market_data,
        candle_builder,
        MarketIndexStore(),
        config=MarketDataServiceConfig(dirty_debounce_ms=0),
        clock=clock,
    )

    service.handle_price_tick({"code": "005930", "price": 70000, "cum_volume": 1000, "trade_value": 70_000_000})
    clock.set(datetime(2026, 6, 18, 9, 1, 0))
    service.handle_price_tick({"code": "005930", "price": 70100, "cum_volume": 1100, "trade_value": 77_110_000})

    assert market_data.latest_tick("005930").price == 70100
    assert candle_builder.completed_candles("005930", 1)[0].close == 70000


def test_dirty_code_queue_marks_price_tick_and_candle_boundary() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 1))
    service = _service(clock)
    service.handle_price_tick({"code": "005930", "price": 70000, "cum_volume": 1000, "trade_value": 70_000_000})
    clock.set(datetime(2026, 6, 18, 9, 1, 0))
    service.handle_price_tick({"code": "005930", "price": 70100, "cum_volume": 1100, "trade_value": 77_110_000})

    queue = service.dirty_queue.snapshot()

    assert queue["dirty_count"] == 1
    assert queue["dirty_codes"] == ["005930"]
    assert queue["reason_counts"][DirtyReason.PRICE_TICK.value] == 1
    assert queue["reason_counts"][DirtyReason.CANDLE_BOUNDARY.value] == 1


def test_dirty_code_queue_api_and_debounce() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 0))
    queue = DirtyCodeQueue(clock=clock, debounce_ms=200)

    assert queue.mark_dirty("A005930", DirtyReason.PRICE_TICK, source_event_id="evt-1") is True
    assert queue.mark_dirty("005930", DirtyReason.PRICE_TICK, source_event_id="evt-2") is False
    clock.set(datetime(2026, 6, 18, 9, 0, 1))
    assert queue.mark_dirty("005930", DirtyReason.PRICE_TICK, source_event_id="evt-3") is True
    assert queue.peek_dirty_count() == 1
    assert queue.pop_dirty(limit=1)[0].code == "005930"
    assert queue.peek_dirty_count() == 0
    queue.mark_dirty("000660", DirtyReason.MARKET_REGIME_CHANGED)
    queue.clear()
    assert queue.snapshot()["dirty_count"] == 0


def test_stale_tick_is_classified_as_data_wait() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 0))
    service = _service(clock)
    service.handle_price_tick({"code": "005930", "price": 70000, "cum_volume": 1000, "trade_value": 70_000_000})
    clock.set(datetime(2026, 6, 18, 9, 0, 20))

    snapshot = service.latest_snapshot("005930")
    refreshed = service._snapshot_from_tick(service.market_data.latest_tick("005930"), {"code": "005930"}, source_event_id="evt")

    assert snapshot.freshness_status == "FRESH"
    assert refreshed.freshness_status == "STALE_TICK"
    assert refreshed.data_quality_status == "DATA_WAIT"
    assert "STALE_TICK" in refreshed.reason_codes


def test_tr_backfill_price_source_is_distinct_from_realtime() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 0))
    service = _service(clock)

    service.handle_price_tick(
        {
            "code": "005930",
            "price": 70000,
            "cum_volume": 1000,
            "trade_value": 70_000_000,
            "price_source": "TR_BACKFILL",
        }
    )

    snapshot = service.latest_snapshot("005930")
    assert snapshot.price_source == "TR_BACKFILL"
    assert snapshot.data_quality_status == "DATA_WAIT"
    assert "TR_BACKFILL_PRICE_ONLY" in snapshot.reason_codes


def test_invalid_price_gets_data_quality_warning_without_crashing() -> None:
    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 0))
    service = _service(clock)

    assert service.handle_price_tick({"code": "005930", "price": 0, "cum_volume": 1000}) is True

    snapshot = service.latest_snapshot("005930")
    assert snapshot.price == 0
    assert snapshot.data_quality_status == "DATA_WAIT"
    assert "MISSING_PRICE" in snapshot.reason_codes


def test_gateway_event_market_data_bridge_public_api_is_preserved() -> None:
    market_data = MarketDataStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), MarketIndexStore())
    event = GatewayEvent(type="price_tick", event_id="evt-bridge", payload={"code": "005930", "price": 70000, "volume": 1000})

    assert bridge.handle_event(event) is True
    assert bridge.handle_price_tick({"code": "000660", "price": 120000, "volume": 2000}) is True
    assert bridge.data_quality_snapshot()["total_price_ticks"] == 2
    assert bridge.latest_snapshot("005930").source_event_id == "evt-bridge"
    assert "005930" in bridge.pop_dirty_codes(limit=10)


def test_batch_flush_disabled_does_not_write() -> None:
    class Store:
        def __init__(self) -> None:
            self.calls = 0

        def save_market_data_snapshots_batch(self, rows):
            self.calls += 1
            return len(rows)

    clock = MutableClock(datetime(2026, 6, 18, 9, 0, 0))
    store = Store()
    service = _service(clock, flush_store=store, batch_flush_enabled=False)
    service.handle_price_tick({"code": "005930", "price": 70000, "volume": 1000})

    result = service.flush_batch()

    assert result["status"] == "DISABLED"
    assert store.calls == 0
