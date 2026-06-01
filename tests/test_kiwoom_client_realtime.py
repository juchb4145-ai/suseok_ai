from kiwoom.client import (
    FID_ACC_TRADE_VALUE,
    FID_ACC_VOLUME,
    FID_BEST_ASK,
    FID_BEST_BID,
    FID_CHANGE_RATE,
    FID_CURRENT_PRICE,
    FID_EXECUTION_STRENGTH,
    FID_HIGH_PRICE,
    FID_LOW_PRICE,
    FID_OPEN_PRICE,
    FID_TRADE_TIME,
    KiwoomClient,
    REALTIME_STOCK_FIDS,
    realtime_stock_fid_string,
)
from trading.broker.models import Signal


def test_realtime_stock_fids_include_rich_quality_fields():
    assert REALTIME_STOCK_FIDS == [
        FID_CURRENT_PRICE,
        FID_CHANGE_RATE,
        FID_ACC_VOLUME,
        FID_ACC_TRADE_VALUE,
        FID_OPEN_PRICE,
        FID_HIGH_PRICE,
        FID_LOW_PRICE,
        FID_TRADE_TIME,
        FID_BEST_ASK,
        FID_BEST_BID,
        FID_EXECUTION_STRENGTH,
    ]
    assert realtime_stock_fid_string() == "10;12;13;14;16;17;18;20;27;28;228"


def test_register_realtime_adds_to_existing_screen():
    class FakeOcx:
        def __init__(self):
            self.calls = []

        def dynamicCall(self, signature, *args):
            self.calls.append((signature, args))
            return 0

    client = object.__new__(KiwoomClient)
    client.ocx = FakeOcx()

    client.register_realtime(["001", "101"], screen_no="7000")
    client.register_realtime(["005930"], screen_no="7000")

    set_real_calls = [args for signature, args in client.ocx.calls if signature.startswith("SetRealReg")]
    assert set_real_calls[0][3] == "0"
    assert set_real_calls[1][3] == "1"


def test_remove_all_realtime_resets_screen_add_mode():
    class FakeOcx:
        def __init__(self):
            self.calls = []

        def dynamicCall(self, signature, *args):
            self.calls.append((signature, args))
            return 0

    client = object.__new__(KiwoomClient)
    client.ocx = FakeOcx()

    client.register_realtime(["001"], screen_no="7000")
    client.remove_all_realtime()
    client.register_realtime(["101"], screen_no="7000")

    set_real_calls = [args for signature, args in client.ocx.calls if signature.startswith("SetRealReg")]
    assert set_real_calls[0][3] == "0"
    assert set_real_calls[1][3] == "0"


def test_kiwoom_real_data_parses_rich_tick_and_keeps_legacy_signal():
    client = object.__new__(KiwoomClient)
    client.price_received = Signal()
    client.price_tick_received = Signal()
    raw_values = {
        FID_CURRENT_PRICE: "-70,000",
        FID_CHANGE_RATE: "+1.25",
        FID_ACC_VOLUME: "1,200",
        FID_ACC_TRADE_VALUE: "84,000,000",
        FID_OPEN_PRICE: "69,500",
        FID_HIGH_PRICE: "71,000",
        FID_LOW_PRICE: "69,000",
        FID_TRADE_TIME: "093015",
        FID_BEST_ASK: "70,100",
        FID_BEST_BID: "70,000",
        FID_EXECUTION_STRENGTH: "123.4",
    }
    client._real_raw = lambda code, fid: raw_values.get(fid, "")
    legacy = []
    rich = []
    client.price_received.connect(lambda *args, **kwargs: legacy.append((args, kwargs)))
    client.price_tick_received.connect(rich.append)

    client._on_receive_real_data("005930", "stock_trade", "")

    assert legacy == [(("005930", 70000, 1.25, 1200, 70100, 70000), {})]
    tick = rich[0]
    assert tick.code == "005930"
    assert tick.price == 70000
    assert tick.volume == 1200
    assert tick.trade_value == 84_000_000
    assert tick.execution_strength == 123.4
    assert tick.open_price == 69500
    assert tick.day_high == 71000
    assert tick.day_low == 69000
    assert tick.trade_time == "093015"
    assert tick.spread_ticks == 1
    assert tick.metadata["real_type"] == "stock_trade"
    assert FID_ACC_TRADE_VALUE in tick.metadata["raw_fids_present"]
    assert "SPREAD_APPROXIMATED" in tick.metadata["reason_codes"]


def test_kiwoom_real_data_records_missing_and_parse_reason_codes():
    client = object.__new__(KiwoomClient)
    client.price_received = Signal()
    client.price_tick_received = Signal()
    raw_values = {
        FID_CURRENT_PRICE: "1,000",
        FID_CHANGE_RATE: "bad",
        FID_ACC_VOLUME: "10",
    }
    client._real_raw = lambda code, fid: raw_values.get(fid, "")
    rich = []
    client.price_tick_received.connect(rich.append)

    client._on_receive_real_data("000001", "stock_trade", "")

    tick = rich[0]
    assert tick.trade_value == 10_000
    assert tick.execution_strength == 0.0
    assert tick.day_high == 0
    assert tick.day_low == 0
    assert "TURNOVER_ESTIMATED" in tick.metadata["reason_codes"]
    assert "EXECUTION_STRENGTH_MISSING" in tick.metadata["reason_codes"]
    assert "DAY_HIGH_LOW_MISSING" in tick.metadata["reason_codes"]
    assert "BEST_BID_ASK_MISSING" in tick.metadata["reason_codes"]
    assert "REAL_PARSE_FALLBACK" in tick.metadata["reason_codes"]
