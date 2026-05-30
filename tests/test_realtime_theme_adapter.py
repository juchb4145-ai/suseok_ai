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
