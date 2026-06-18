from apps.kiwoom_gateway import GatewayRuntime, _wire_kiwoom_signals
from kiwoom.client import KiwoomClient
from trading.broker.models import Signal


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

    def poll_commands(self, *, wait_sec=0.0, limit=20):
        return []


class FakeOcx:
    def __init__(self, values):
        self.values = {int(key): str(value) for key, value in values.items()}

    def dynamicCall(self, signature, *args):
        if signature.startswith("GetChejanData"):
            return self.values.get(int(args[0]), "")
        if signature.startswith("GetLoginInfo"):
            return "1"
        return ""


def _client(values):
    client = object.__new__(KiwoomClient)
    client.ocx = FakeOcx(values)
    client.execution_received = Signal()
    client.chejan_event_received = Signal()
    client.message_received = Signal()
    return client


def test_kiwoom_client_chejan_callback_emits_parser_result_not_legacy_execution(monkeypatch):
    monkeypatch.setenv("TRADING_KIWOOM_CHEJAN_EMIT_LEGACY_EXECUTION_EVENT", "false")
    client = _client(
        {
            9201: "ACC_TOKEN_SYNTHETIC",
            9203: "OID-1",
            9001: "A005930",
            913: "체결",
            900: "3",
            901: "70000",
            902: "2",
            905: "+매수",
            907: "2",
            908: "090001",
            909: "EXEC-1",
            910: "70100",
            911: "1",
            915: "1",
            920: "7000",
        }
    )
    parsed = []
    legacy = []
    client.chejan_event_received.connect(parsed.append)
    client.execution_received.connect(legacy.append)

    client._on_receive_chejan_data("0", 14, "9201;9203;9001;913;900;901;902;905;907;908;909;910;911;915;920")

    assert len(parsed) == 1
    assert legacy == []
    assert parsed[0].gateway_event_type == "kiwoom_order_chejan"
    assert parsed[0].canonical_payload["screen_no"] == "7000"
    assert parsed[0].canonical_payload["legacy_tag"] == ""


def test_gateway_runtime_wires_chejan_parser_result_to_raw_gateway_event():
    runtime = GatewayRuntime(FakeCoreClient())

    class Client:
        def __init__(self):
            self.connected = Signal()
            self.price_received = Signal()
            self.price_tick_received = Signal()
            self.order_result = Signal()
            self.execution_received = Signal()
            self.chejan_event_received = Signal()
            self.message_received = Signal()
            self.condition_load_result = Signal()
            self.condition_loaded = Signal()
            self.condition_real_received = Signal()
            self.condition_tr_received = Signal()

    client = Client()
    _wire_kiwoom_signals(client, runtime)
    parsed = _client(
        {
            9201: "ACC_TOKEN_SYNTHETIC",
            9203: "OID-2",
            9001: "A005930",
            913: "접수",
            900: "3",
            901: "70000",
            902: "3",
        }
    )
    captured = []
    parsed.chejan_event_received.connect(captured.append)
    parsed._on_receive_chejan_data("0", 7, "9201;9203;9001;913;900;901;902")

    client.chejan_event_received.emit(captured[0])
    event = runtime.events.drain()[0]
    assert event.type == "kiwoom_order_chejan"
    assert event.payload["order_no"] == "OID-2"
