import threading
import time

from apps.kiwoom_gateway import GatewayRuntime, _execute_command, _kiwoom_heartbeat_payload, _request_kiwoom_login, _wire_kiwoom_signals
from trading.broker.models import BrokerPriceTick, ConditionInfo, GatewayCommand, Signal


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

    def __init__(self):
        self.posted_events = []

    def snapshot(self):
        return {}

    def post_event(self, event):
        self.posted_events.append(event)
        self.post_count += 1
        return {"ok": True}

    def poll_commands(self, *, wait_sec=0.0, limit=20):
        self.poll_count += 1
        return []


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
    assert payload["metadata"]["gateway_realtime_reliability_bucket"] == "HIGH"
    assert payload["gateway_realtime_reliability_score"] >= 90.0
    assert runtime.data_quality.snapshot()["total_price_ticks"] == 1
    assert runtime.data_quality.snapshot()["reliability"]["bucket_counts"]["HIGH"] == 1


def test_gateway_runtime_marks_known_index_tick_before_reliability_assessment():
    runtime = GatewayRuntime(FakeCoreClient())
    client = SignalClient(rich=True)
    _wire_kiwoom_signals(client, runtime)

    client.price_tick_received.emit(
        BrokerPriceTick(
            code="001",
            price=330000,
            change_rate=0.8,
            volume=1000,
            trade_value=10_000_000,
            instrument_type="stock",
            name="KOSPI",
            metadata={
                "real_type": "업종등락",
                "reason_codes": ["BEST_BID_ASK_MISSING", "EXECUTION_STRENGTH_MISSING"],
            },
        )
    )

    event = runtime.events.drain()[0]
    payload = event.payload
    reliability = payload["gateway_realtime_reliability"]
    assert payload["instrument_type"] == "index"
    assert payload["metadata"]["instrument_type"] == "index"
    assert reliability["bucket"] == "HIGH"
    assert "BEST_BID_ASK_MISSING" not in reliability["reasons"]
    assert "EXECUTION_STRENGTH_MISSING" not in reliability["reasons"]


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
    runtime.command_polling_paused = True
    client = SignalClient(rich=True)
    _wire_kiwoom_signals(client, runtime)

    client.connected.emit(True, 0, "ok")

    assert runtime.command_polling_paused is False
    events = runtime.events.drain()
    assert [event.type for event in events] == ["login_status", "market_symbols"]
    assert events[1].payload["markets"] == [
        {"market_code": "0", "market": "KOSPI", "symbols": ["005930", "084670"]},
        {"market_code": "10", "market": "KOSDAQ", "symbols": ["035720"]},
    ]


def test_kiwoom_heartbeat_payload_reports_simulation_broker_environment():
    class Client:
        def get_accounts(self):
            return ["1234567890"]

        def get_server_gubun(self):
            return "1"

    runtime = GatewayRuntime(FakeCoreClient())
    payload = _kiwoom_heartbeat_payload(Client(), runtime)

    assert payload["kiwoom_logged_in"] is True
    assert payload["account"] == "1234567890"
    assert payload["broker_name"] == "KIWOOM"
    assert payload["broker_env"] == "SIMULATION"
    assert payload["server_mode"] == "SIMULATION"
    assert payload["account_mode"] == "SIMULATION"
    assert payload["server_gubun"] == "1"


def test_kiwoom_heartbeat_payload_includes_chejan_parser_metrics():
    class Metrics:
        def to_dict(self):
            return {
                "total_count": 3,
                "by_status": {"OK": 2, "DEGRADED": 1},
                "by_gateway_event_type": {"kiwoom_order_chejan": 3},
            }

    class Client:
        chejan_parser_metrics = Metrics()

        def get_accounts(self):
            return ["1234567890"]

        def get_server_gubun(self):
            return "1"

    runtime = GatewayRuntime(FakeCoreClient())
    payload = _kiwoom_heartbeat_payload(Client(), runtime)

    assert payload["kiwoom_chejan_parser"]["total_count"] == 3
    assert payload["kiwoom_chejan_parser"]["by_status"]["DEGRADED"] == 1


def test_kiwoom_heartbeat_payload_keeps_unknown_environment_fail_closed():
    class Client:
        def get_accounts(self):
            return ["1234567890"]

        def get_server_gubun(self):
            return ""

    runtime = GatewayRuntime(FakeCoreClient())
    payload = _kiwoom_heartbeat_payload(Client(), runtime)

    assert payload["kiwoom_logged_in"] is True
    assert payload["broker_env"] == "UNKNOWN"
    assert payload["server_mode"] == "UNKNOWN"
    assert payload["account_mode"] == "UNKNOWN"


def test_threaded_login_does_not_block_gateway_heartbeat_payload():
    started = threading.Event()
    release = threading.Event()

    class Client:
        def login(self):
            started.set()
            release.wait(timeout=2)
            return 0

        def get_accounts(self):
            raise AssertionError("heartbeat must not query ActiveX while login is in progress")

    runtime = GatewayRuntime(FakeCoreClient())

    _request_kiwoom_login(Client(), runtime, threaded=True)
    assert started.wait(timeout=1)

    payload = _kiwoom_heartbeat_payload(Client(), runtime)

    assert payload["kiwoom_logged_in"] is False
    assert payload["login_in_progress"] is True
    assert payload["command_polling_paused"] is False

    release.set()


def test_gateway_network_loop_drains_events_while_command_polling_paused():
    core = FakeCoreClient()
    runtime = GatewayRuntime(core)
    runtime.command_polling_paused = True
    runtime.start_network_worker(interval_sec=0.05)
    try:
        runtime.emit("heartbeat", {"kiwoom_logged_in": False, "orderable": False})
        deadline = time.time() + 1.0
        while time.time() < deadline and not core.posted_events:
            time.sleep(0.02)
    finally:
        runtime.stop()

    assert [event.type for event in core.posted_events] == ["heartbeat"]
    assert core.poll_count == 0


def test_gateway_send_condition_failure_message_includes_result_code():
    class Client:
        def send_condition(self, screen_no, condition_name, condition_index, realtime=True, search_type=None):
            return 0

    result = _execute_command(
        Client(),
        GatewayCommand(
            type="send_condition",
            command_id="cmd-cond-fail",
            payload={
                "screen_no": "7602",
                "condition_name": "테마랩_주도_5",
                "condition_index": 85,
                "realtime": True,
                "search_type": 1,
            },
        ),
    )

    assert result["status"] == "FAILED"
    assert result["result_code"] == 0
    assert result["message"] == "condition send failed: result_code=0 expected=1"


def test_gateway_send_condition_success_keeps_operator_message():
    class Client:
        def send_condition(self, screen_no, condition_name, condition_index, realtime=True, search_type=None):
            return 1

    result = _execute_command(
        Client(),
        GatewayCommand(
            type="send_condition",
            command_id="cmd-cond-ok",
            payload={"condition_name": "테마랩_주도_5", "condition_index": 85},
        ),
    )

    assert result["status"] == "ACKED"
    assert result["result_code"] == 1
    assert result["message"] == "condition sent"


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
