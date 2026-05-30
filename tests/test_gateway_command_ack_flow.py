import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def test_command_ack_marks_record_acked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}

    response = client.post(
        "/api/gateway/commands",
        json={"type": "login", "command_id": "cmd-ack"},
        headers=headers,
    )
    assert response.json()["accepted"] is True

    dispatched = client.get("/api/gateway/commands", headers=headers).json()
    assert dispatched["commands"][0]["command_id"] == "cmd-ack"

    ack = client.post(
        "/api/gateway/events",
        json={
            "type": "command_ack",
            "event_id": "evt-ack",
            "payload": {
                "command_id": "cmd-ack",
                "command_type": "login",
                "status": "ACKED",
                "message": "ok",
                "result_code": 0,
            },
        },
        headers=headers,
    )
    assert ack.status_code == 200

    status = client.get("/api/gateway/commands/status").json()
    assert status["acked_count"] == 1
    history = client.get("/api/gateway/commands/history?status=ACKED").json()
    assert history["items"][0]["command_id"] == "cmd-ack"


def test_command_failed_marks_record_failed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}
    client.post("/api/gateway/commands", json={"type": "tr_request", "command_id": "cmd-fail"}, headers=headers)
    client.get("/api/gateway/commands", headers=headers)

    response = client.post(
        "/api/gateway/events",
        json={
            "type": "command_failed",
            "event_id": "evt-fail",
            "payload": {
                "command_id": "cmd-fail",
                "command_type": "tr_request",
                "error": "boom",
                "retryable": False,
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    status = client.get("/api/gateway/commands/status").json()
    assert status["failed_count"] == 1
    history = client.get("/api/gateway/commands/history?status=FAILED").json()
    assert history["items"][0]["last_error"] == "boom"


def test_mock_gateway_command_ack_flow(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}
    client.post(
        "/api/gateway/commands",
        json={
            "type": "send_order",
            "command_id": "cmd-mock-order",
            "payload": {
                "account": "1234567890",
                "code": "005930",
                "side": "buy",
                "quantity": 1,
                "price": 70000,
                "order_type": 1,
                "tag": "MOCK",
            },
        },
        headers=headers,
    )

    command = client.get("/api/gateway/commands", headers=headers).json()["commands"][0]
    assert command["command_id"] == "cmd-mock-order"
    ack_payload = {
        "command_id": "cmd-mock-order",
        "command_type": "send_order",
        "status": "ACKED",
        "result_code": 0,
        "message": "mock send_order accepted",
        "order_result": {
            "ok": True,
            "code": 0,
            "message": "mock send_order accepted",
            "request": command["payload"],
            "command_id": "cmd-mock-order",
            "raw": {"mock": True},
        },
    }
    client.post("/api/gateway/events", json={"type": "command_ack", "event_id": "evt-mock-ack", "payload": ack_payload}, headers=headers)

    orders = client.get("/api/orders").json()
    assert orders["summary"]["order_result_count"] == 1
    assert client.get("/api/gateway/commands/status").json()["acked_count"] == 1
