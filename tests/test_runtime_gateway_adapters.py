from storage.db import TradingDatabase
from tests.theme_naver_helpers import repo_with_naver_fixture
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerPriceTick, GatewayEvent, utc_timestamp
from trading.strategy.candles import CandleBuilder
from trading.strategy.conditions import ConditionProfile, ConditionProfileRepository
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.models import StrategyProfile
from trading.theme_engine.runtime import RealTimeThemeRuntime
from trading_app.runtime_adapters import (
    GatewayCommandConditionAdapter,
    GatewayCommandRealtimeClient,
    GatewayEventMarketDataBridge,
    GatewayEventThemeRuntimeBridge,
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


def test_gateway_price_tick_passes_rich_payload_to_strategy_tick():
    market_data = MarketDataStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), MarketIndexStore())

    assert bridge.handle_price_tick(
        {
            "code": "005930",
            "price": 70000,
            "change_rate": 1.2,
            "volume": 1200,
            "cum_volume": 1200,
            "trade_value": 84_000_000,
            "execution_strength": 123.4,
            "best_ask": 70100,
            "best_bid": 70000,
            "spread_ticks": 1,
            "day_high": 71000,
            "day_low": 69000,
            "trade_time": "093015",
            "metadata": {"reason_codes": ["SPREAD_APPROXIMATED"], "raw_fids_present": [10, 14, 228]},
        }
    ) is True

    tick = market_data.latest_tick("005930")
    assert tick.trade_value == 84_000_000
    assert tick.execution_strength == 123.4
    assert tick.spread_ticks == 1
    assert tick.metadata["session_high"] == 71000
    assert tick.metadata["session_low"] == 69000
    assert tick.metadata["trade_time"] == "093015"
    assert tick.metadata["raw_fids_present"] == [10, 14, 228]
    assert "SPREAD_APPROXIMATED" in tick.metadata["reason_codes"]

    quality = bridge.data_quality_snapshot()
    assert quality["total_price_ticks"] == 1
    assert quality["field_coverage"]["execution_strength"] == 1.0


def test_gateway_price_tick_updates_index_store():
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(MarketDataStore(), CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "001", "price": 330000, "instrument_type": "index", "name": "KOSPI"}) is True

    assert market_index_store.state("KOSPI").price == 330000


def test_gateway_price_tick_routes_known_index_even_when_payload_says_stock():
    market_data = MarketDataStore()
    market_index_store = MarketIndexStore()
    bridge = GatewayEventMarketDataBridge(market_data, CandleBuilder(), market_index_store)

    assert bridge.handle_price_tick({"code": "101", "price": 950, "instrument_type": "stock", "name": "KOSDAQ"}) is True

    assert market_index_store.state("KOSDAQ").price == 950
    assert market_data.latest_tick("101") is None


def test_gateway_price_tick_updates_theme_runtime_from_kiwoom_tick(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    try:
        runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
        bridge = GatewayEventThemeRuntimeBridge(runtime)

        assert bridge.handle_price_tick(
            {
                "code": "000001",
                "price": 1000,
                "change_rate": 8.0,
                "cum_volume": 1000,
                "trade_value": 1000000,
                "execution_strength": 150,
            }
        ) is True

        assert runtime.realtime_adapter.latest_snapshot("000001") is not None
        assert runtime.get_latest_rank(1)[0].leader_code == "000001"
    finally:
        db.close()


def test_theme_runtime_ignores_known_index_even_when_payload_says_stock(tmp_path):
    db, repo = repo_with_naver_fixture(tmp_path)
    try:
        runtime = RealTimeThemeRuntime(repo, scoring_interval_sec=0, db_snapshot_interval_sec=0, ws_push_interval_sec=0)
        bridge = GatewayEventThemeRuntimeBridge(runtime)

        assert bridge.handle_price_tick({"code": "001", "price": 330000, "instrument_type": "stock"}) is False

        assert runtime.realtime_adapter.latest_snapshot("001") is None
    finally:
        db.close()


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
