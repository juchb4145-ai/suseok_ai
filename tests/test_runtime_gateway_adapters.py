from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerPriceTick, GatewayEvent, utc_timestamp
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import StrategyProfile
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventMarketDataBridge,
)


def test_gateway_price_tick_updates_stock_market_data():
    market_data = MarketDataStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), MarketIndexStore())
    event = GatewayEvent(
        type="price_tick",
        payload=BrokerPriceTick(code="005930", price=70000, change_rate=1.2, volume=1000).to_dict(),
    )

    assert bridge.handle_event(event) is True

    tick = market_data.latest_tick("005930")
    assert tick is not None
    assert tick.price == 70000
    assert tick.cum_volume == 1000


def test_gateway_price_tick_updates_index_store():
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(MarketDataStore(), CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "001", "price": 330000, "instrument_type": "index", "name": "KOSPI"}) is True

    assert market_index_store.state("KOSPI").price == 330000


def test_realtime_adapter_enqueues_gateway_commands():
    state = GatewayStateStore()
    client = GatewayCommandRealtimeClient(state)

    client.register_realtime(["005930"], screen_no="7000")
    client.remove_realtime(["005930"], screen_no="7000")

    history = state.list_commands(limit=10, include_finished=True)
    assert [item["command_type"] for item in history] == ["remove_realtime", "register_realtime"]


def test_condition_adapter_warns_when_index_is_not_ready(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime.sqlite3"))
    try:
        repository = ConditionProfileRepository(db)
        repository.upsert_profile(
            ConditionProfile(
                condition_name="entry",
                strategy_profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
                enabled=True,
                priority=100,
                purpose="kosdaq_pullback_candidate",
                last_resolved_index=None,
            )
        )
        state = GatewayStateStore()
        state.status.connected = True
        state.status.kiwoom_logged_in = True
        state.status.last_heartbeat_at = utc_timestamp()
        adapter = GatewayCommandConditionAdapter(state, repository)

        warnings = adapter.start()

        assert any(warning.startswith("CONDITION_INDEX_NOT_READY") for warning in warnings)
        commands = state.list_commands(limit=10, include_finished=True)
        assert any(item["command_type"] == "load_conditions" for item in commands)
    finally:
        db.close()
