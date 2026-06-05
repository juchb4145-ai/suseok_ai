from apps.kiwoom_gateway import GatewayRuntime, _wire_kiwoom_signals
from trading.broker.models import BrokerPriceTick, ConditionInfo, Signal


class FakeCoreClient:
    transport_mode = "rest_long_poll"
    last_poll_error = ""
    last_poll_ms = 0.0
    last_event_post_ms = 0.0
    poll_count = 0
    empty_poll_count = 0
    post_count = 0
    post_error_count = 0
    last_poll_command_count = 0

    def snapshot(self):
        return {}


class SignalClient:
    def __init__(self, *, rich: bool) -> None:
        self.connected = Signal()
        self.price_received = Signal()
        if rich:
            self.price_tick_received = Signal()
        self.order_result = Signal()
        self.execution_received = Signal()
        self.message_received = Signal()
        self.condition_load_result = Signal()
        self.condition_loaded = Signal()
        self.condition_real_received = Signal()
        self.condition_tr_received = Signal()
        self.market_codes = {"0": ["A005930", "084670"], "10": ["035720"]}

    def get_code_list_by_market(self, market_code: str) -> list[str]:
        return list(self.market_codes.get(str(market_code), []))


def test_gateway_runtime_uses_rich_price_tick_signal_payload():
    runtime = GatewayRuntime(FakeCoreClient())
    client = SignalClient(rich=True)
    _wire_kiwoom_signals(client, runtime)

    client.price_tick_received.emit(
        BrokerPriceTick(
            code="005930",
            price=70000,
            change_rate=1.2,
            volume=1200,
            best_ask=70100,
            best_bid=70000,
            trade_value=84_000_000,
            execution_strength=123.4,
            spread_ticks=1,
            day_high=71000,
            day_low=69000,
            trade_time="093015",
            metadata={"reason_codes": ["SPREAD_APPROXIMATED"], "raw_fids_present": [10, 14, 228]},
        )
    )

    event = runtime.events.drain()[0]
    payload = event.payload
    assert event.type == "price_tick"
    assert payload["volume"] == 1200
    assert payload["cum_volume"] == 1200
    assert payload["trade_value"] == 84_000_000
    assert payload["execution_strength"] == 123.4
    assert payload["spread_ticks"] == 1
    assert payload["day_high"] == 71000
    assert payload["day_low"] == 69000
    assert payload["trade_time"] == "093015"
    assert payload["metadata"]["reason_codes"] == ["SPREAD_APPROXIMATED"]
    assert runtime.data_quality.snapshot()["total_price_ticks"] == 1


def test_gateway_runtime_keeps_old_price_received_fallback_path():
    runtime = GatewayRuntime(FakeCoreClient())
    client = SignalClient(rich=False)
    _wire_kiwoom_signals(client, runtime)

    client.price_received.emit("005930", 70000, 1.2, 1200, 70100, 70000)

    event = runtime.events.drain()[0]
    payload = event.payload
    assert payload["code"] == "005930"
    assert payload["price"] == 70000
    assert payload["volume"] == 1200
    assert payload["cum_volume"] == 1200
    assert payload["best_ask"] == 70100
    assert payload["best_bid"] == 70000
    assert payload["trade_value"] == 0.0


def test_gateway_runtime_emits_market_symbols_after_login_success():
    runtime = GatewayRuntime(FakeCoreClient())
    client = SignalClient(rich=True)
    _wire_kiwoom_signals(client, runtime)

    client.connected.emit(True, 0, "ok")

    events = runtime.events.drain()
    assert [event.type for event in events] == ["login_status", "market_symbols"]
    assert events[1].payload["markets"] == [
        {"market_code": "0", "market": "KOSPI", "symbols": ["005930", "084670"]},
        {"market_code": "10", "market": "KOSDAQ", "symbols": ["035720"]},
    ]


def test_gateway_runtime_emits_condition_load_events():
    runtime = GatewayRuntime(FakeCoreClient())
    client = SignalClient(rich=True)
    _wire_kiwoom_signals(client, runtime)

    client.condition_load_result.emit(True, "ok")
    client.condition_loaded.emit([ConditionInfo(index=1, name="테마랩_생존_-1")])

    events = runtime.events.drain()
    assert [event.type for event in events] == ["condition_load_result", "condition_loaded"]
    assert events[0].payload["success"] is True
    assert events[1].payload["conditions"] == [{"index": 1, "name": "테마랩_생존_-1"}]
