from kiwoom.client import MockKiwoomClient
from storage.db import TradingDatabase
from trading.engine import TradingEngine
from trading.models import BuyLeg, LegStatus, WatchItem


def make_engine(tmp_path):
    client = MockKiwoomClient()
    db = TradingDatabase(str(tmp_path / "test.sqlite3"))
    engine = TradingEngine(client, db)
    engine.set_account("1234567890")
    engine.set_ordering_enabled(True)
    return engine, client


def test_three_legs_order_once_each(tmp_path):
    engine, client = make_engine(tmp_path)
    item = WatchItem(
        code="005930",
        name="삼성전자",
        budget=3_000_000,
        stop_loss_price=55_000,
        tick_threshold=1,
        auto_buy_enabled=True,
        legs=[
            BuyLeg(1, 60_000, 30.0),
            BuyLeg(2, 59_000, 30.0),
            BuyLeg(3, 58_000, 40.0),
        ],
    )
    ok, message = engine.add_or_update_item(item)
    assert ok, message

    client.emit_price("005930", 60_000)
    client.emit_price("005930", 60_000)
    client.emit_price("005930", 59_000)
    client.emit_price("005930", 58_000)

    buy_orders = [order for order in client.orders if order.side == "buy"]
    assert len(buy_orders) == 3
    assert [order.price for order in buy_orders] == [60_000, 59_000, 58_000]
    engine.db.close()


def test_execution_updates_average_price_and_leg_status(tmp_path):
    engine, client = make_engine(tmp_path)
    item = WatchItem(
        code="005930",
        name="삼성전자",
        budget=1_000_000,
        stop_loss_price=55_000,
        tick_threshold=1,
        auto_buy_enabled=True,
        legs=[BuyLeg(1, 50_000, 50.0), BuyLeg(2, 49_000, 0.0), BuyLeg(3, 48_000, 0.0)],
    )
    engine.add_or_update_item(item)

    client.emit_price("005930", 50_000)
    client.emit_execution("005930", "buy", 10, 50_000, remaining_quantity=0, tag="BUY1_005930")

    saved = engine.items["005930"]
    assert saved.holding_quantity == 10
    assert saved.average_price == 50_000
    assert saved.leg(1).status == LegStatus.FILLED
    engine.db.close()


def test_take_profit_sells_configured_percent_once(tmp_path):
    engine, client = make_engine(tmp_path)
    item = WatchItem(
        code="005930",
        name="삼성전자",
        budget=1_000_000,
        stop_loss_price=45_000,
        tick_threshold=1,
        take_profit_rate=5.0,
        take_profit_sell_percent=70.0,
        auto_buy_enabled=False,
        auto_sell_enabled=True,
        average_price=50_000,
        holding_quantity=10,
        legs=[BuyLeg(1, 50_000, 100.0), BuyLeg(2), BuyLeg(3)],
    )
    engine.add_or_update_item(item)

    client.emit_price("005930", 52_400)
    client.emit_price("005930", 52_500)
    client.emit_price("005930", 53_000)

    sell_orders = [order for order in client.orders if order.side == "sell"]
    assert len(sell_orders) == 1
    assert sell_orders[0].quantity == 7
    engine.db.close()
