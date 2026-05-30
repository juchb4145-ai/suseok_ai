import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def test_dry_run_sell_api_summary_and_filters(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    buy = {
        "account": "1234567890",
        "code": "005930",
        "side": "buy",
        "quantity": 1,
        "price": 70000,
        "order_type": 1,
        "hoga": "00",
        "tag": "BUY",
        "strategy_name": "test",
        "idempotency_key": "buy-once",
        "dry_run": True,
    }
    sell = {
        **buy,
        "side": "sell",
        "quantity": 1,
        "price": 73500,
        "order_type": 2,
        "tag": "SELL",
        "idempotency_key": "sell-once",
    }

    buy_response = client.post("/api/orders/enqueue", json=buy).json()
    sell_response = client.post("/api/orders/enqueue", json=sell).json()

    assert buy_response["command"] is None
    assert sell_response["command"] is None
    summary = client.get("/api/runtime/orders/dry-run/summary").json()
    assert summary["buy_total"] == 1
    assert summary["sell_total"] == 1
    assert summary["entry_total"] == 1
    assert summary["exit_total"] == 1

    sell_rows = client.get("/api/runtime/orders/dry-run?side=sell&order_phase=exit").json()
    assert sell_rows["items"][0]["intent_id"] == sell_response["intent_id"]
    assert sell_rows["items"][0]["side"] == "sell"
    assert sell_rows["items"][0]["order_phase"] == "exit"

    snapshot = client.get("/api/snapshot").json()
    assert snapshot["dry_run_orders"]["summary"]["sell_total"] == 1
