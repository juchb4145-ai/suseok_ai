import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, *, mode="OBSERVE", allow_live="0"):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setenv("TRADING_ALLOW_LIVE", allow_live)
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def _order_payload(**overrides):
    payload = {
        "account": "1234567890",
        "code": "005930",
        "side": "buy",
        "quantity": 1,
        "price": 70000,
        "order_type": 1,
        "hoga": "00",
        "tag": "T1",
        "strategy_name": "test",
        "candidate_id": 1,
        "reason": "test order",
    }
    payload.update(overrides)
    return payload


def _healthy_gateway(client):
    headers = {"X-Local-Token": "test-token"}
    client.post(
        "/api/gateway/events",
        json={
            "type": "heartbeat",
            "event_id": "evt-heartbeat",
            "source": "test-gateway",
            "payload": {
                "kiwoom_logged_in": True,
                "orderable": True,
                "mode": "LIVE",
                "account": "1234567890",
            },
        },
        headers=headers,
    )


def test_observe_mode_does_not_enqueue_send_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="OBSERVE")
    _healthy_gateway(client)

    response = client.post("/api/orders/enqueue", json=_order_payload())

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "OBSERVE_MODE"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0


def test_live_mode_requires_explicit_allow_live(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="0")
    _healthy_gateway(client)

    response = client.post("/api/orders/enqueue", json=_order_payload())

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "LIVE_REQUIRES_TRADING_ALLOW_LIVE"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0


def test_gateway_unhealthy_rejects_live_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")

    response = client.post("/api/orders/enqueue", json=_order_payload())

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] in {"GATEWAY_NOT_CONNECTED", "GATEWAY_HEARTBEAT_STALE"}


def test_live_order_enqueue_creates_gateway_command_and_blocks_duplicate(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")
    _healthy_gateway(client)

    first = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once")).json()
    second = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once")).json()

    assert first["accepted"] is True
    assert first["command"]["type"] == "send_order"
    assert second["accepted"] is False
    assert second["reason"] == "DUPLICATE_ORDER_COMMAND"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 1
