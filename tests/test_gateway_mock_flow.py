import importlib

from fastapi.testclient import TestClient

from trading.broker.models import BrokerExecutionEvent, BrokerPriceTick


def test_gateway_events_update_core_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)
    headers = {"X-Local-Token": "test-token"}

    assert client.get("/health").json()["ok"] is True
    assert client.get("/").status_code == 200

    events = [
        {
            "type": "heartbeat",
            "event_id": "test-heartbeat-1",
            "source": "mock_gateway_test",
            "payload": {"kiwoom_logged_in": True, "orderable": False, "mode": "OBSERVE"},
        },
        {
            "type": "price_tick",
            "event_id": "test-price-1",
            "source": "mock_gateway_test",
            "payload": BrokerPriceTick(code="005930", price=73200, change_rate=1.2, volume=1000).to_dict(),
        },
        {
            "type": "condition_event",
            "event_id": "test-condition-1",
            "source": "mock_gateway_test",
            "payload": {
                "condition_name": "mock_condition",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "source": "condition",
            },
        },
        {
            "type": "execution_event",
            "event_id": "test-execution-1",
            "source": "mock_gateway_test",
            "payload": BrokerExecutionEvent(
                code="005930",
                order_no="M000001",
                side="buy",
                quantity=1,
                price=73200,
                filled_quantity=1,
                remaining_quantity=0,
                tag="MOCK",
            ).to_dict(),
        },
    ]

    for event in events:
        response = client.post("/api/gateway/events", json=event, headers=headers)
        assert response.status_code == 200
        assert response.json()["accepted"] is True

    snapshot = client.get("/api/snapshot").json()

    assert snapshot["gateway"]["connection_state"] == "CONNECTED"
    assert snapshot["gateway"]["kiwoom_logged_in"] is True
    assert snapshot["candidates"]["summary"]["total"] == 1
    assert snapshot["candidates"]["items"][0]["code"] == "005930"
    assert snapshot["orders"]["summary"]["execution_count"] == 1
    assert snapshot["market_data"]["latest_ticks"][0]["code"] == "005930"


def test_gateway_command_long_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)
    headers = {"X-Local-Token": "test-token"}
    response = client.post(
        "/api/gateway/commands",
        json={"type": "login", "command_id": "cmd-test-login", "idempotency_key": "login-once"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True

    commands = client.get("/api/gateway/commands", headers=headers).json()

    assert commands["count"] == 1
    assert commands["commands"][0]["type"] == "login"
    assert commands["commands"][0]["command_id"] == "cmd-test-login"
