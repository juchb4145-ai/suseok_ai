from trading.strategy.market_data import StrategyTick
from trading.theme_engine.realtime_adapter import KiwoomRealtimeThemeAdapter


def test_strategy_tick_to_stock_snapshot_estimates_turnover():
    adapter = KiwoomRealtimeThemeAdapter()
    tick = StrategyTick.from_realtime("A005930", price=70000, cum_volume=10, trade_value=0)

    snapshot = adapter.from_strategy_tick(tick)

    assert snapshot.stock_code == "005930"
    assert snapshot.turnover == 700000
    assert "TURNOVER_ESTIMATED" in snapshot.metadata["reason_codes"]


def test_kiwoom_real_data_to_stock_snapshot_handles_missing_fields():
    adapter = KiwoomRealtimeThemeAdapter()
    snapshot = adapter.from_kiwoom_real_data(
        "A000001",
        {"현재가": "+1,000", "등락률": "2.5", "누적거래량": "1,000", "체결강도": ""},
    )
    adapter.update_snapshot(snapshot)

    assert snapshot.stock_code == "000001"
    assert snapshot.turnover == 1_000_000
    assert adapter.latest_snapshot("000001") is snapshot
    assert adapter.latest_snapshots(["000001"])["000001"] is snapshot


def test_strategy_tick_metadata_flows_to_stock_snapshot():
    adapter = KiwoomRealtimeThemeAdapter()
    tick = StrategyTick.from_realtime(
        "A005930",
        price=70000,
        change_rate=1.2,
        cum_volume=1200,
        best_ask=70100,
        best_bid=70000,
        trade_value=84_000_000,
        execution_strength=123.4,
        metadata={
            "session_high": 71000,
            "session_low": 69000,
            "momentum_1m": 1.1,
            "momentum_3m": 2.2,
            "momentum_5m": 3.3,
            "turnover_strength": 4.4,
            "reason_codes": ["SPREAD_APPROXIMATED"],
        },
    )

    snapshot = adapter.from_strategy_tick(tick)

    assert snapshot.turnover == 84_000_000
    assert snapshot.execution_strength == 123.4
    assert snapshot.best_bid == 70000
    assert snapshot.best_ask == 70100
    assert snapshot.session_high == 71000
    assert snapshot.session_low == 69000
    assert snapshot.momentum_1m == 1.1
    assert snapshot.momentum_3m == 2.2
    assert snapshot.momentum_5m == 3.3
    assert snapshot.turnover_strength == 4.4
    assert snapshot.metadata["reason_codes"] == ["SPREAD_APPROXIMATED"]
