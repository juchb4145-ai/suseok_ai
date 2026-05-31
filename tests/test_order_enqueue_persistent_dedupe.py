import importlib

from fastapi.testclient import TestClient


HEADERS = {"X-Local-Token": "test-token"}


def _client(tmp_path, monkeypatch, *, mode="LIVE", allow_live="1"):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setenv("TRADING_ALLOW_LIVE", allow_live)
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def _healthy_gateway(client):
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
        headers=HEADERS,
    )


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


def test_same_idempotency_key_is_rejected_after_core_reload(tmp_path, monkeypatch):
    first_client = _client(tmp_path, monkeypatch)
    _healthy_gateway(first_client)
    first = first_client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once"), headers=HEADERS).json()
    assert first["accepted"] is True

    second_client = _client(tmp_path, monkeypatch)
    _healthy_gateway(second_client)
    second = second_client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once"), headers=HEADERS).json()

    assert second["accepted"] is False
    assert second["reason"] == "DUPLICATE_ORDER_COMMAND"
    assert second["duplicate_of"] == first["command_id"]


def test_same_deterministic_dedupe_key_is_rejected_after_core_reload(tmp_path, monkeypatch):
    first_client = _client(tmp_path, monkeypatch)
    _healthy_gateway(first_client)
    first = first_client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS).json()
    assert first["accepted"] is True

    second_client = _client(tmp_path, monkeypatch)
    _healthy_gateway(second_client)
    second = second_client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS).json()

    assert second["accepted"] is False
    assert second["reason"] == "DUPLICATE_ORDER_COMMAND"
    assert second["duplicate_of"] == first["command_id"]


def test_command_history_detail_and_events_are_db_backed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _healthy_gateway(client)
    command_id = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="inspect"), headers=HEADERS).json()["command_id"]
    command = client.get("/api/gateway/commands", headers=HEADERS).json()["commands"][0]
    client.post(
        "/api/gateway/events",
        json={
            "type": "command_ack",
            "event_id": "evt-ack-inspect",
            "payload": {
                "command_id": command["command_id"],
                "command_type": "send_order",
                "status": "ACKED",
                "message": "ok",
                "result_code": 0,
            },
        },
        headers=HEADERS,
    )

    reloaded = _client(tmp_path, monkeypatch)
    history = reloaded.get("/api/gateway/commands/history?status=ACKED&include_payload=false").json()
    detail = reloaded.get(f"/api/gateway/commands/{command_id}").json()
    events = reloaded.get(f"/api/gateway/commands/{command_id}/events").json()
    status = reloaded.get("/api/gateway/commands/status").json()

    assert history["items"][0]["command_id"] == command_id
    assert "payload_summary" in history["items"][0]["command"]
    assert detail["found"] is True
    assert detail["record"]["status"] == "ACKED"
    assert events["events"][-1]["event_type"] == "command_ack"
    assert status["acked_count"] == 1


def test_prune_api_removes_finished_history_but_keeps_order_dedupe(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _healthy_gateway(client)
    first = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="prune-safe"), headers=HEADERS).json()
    command = client.get("/api/gateway/commands", headers=HEADERS).json()["commands"][0]
    client.post(
        "/api/gateway/events",
        json={
            "type": "command_ack",
            "event_id": "evt-ack-prune",
            "payload": {
                "command_id": command["command_id"],
                "command_type": "send_order",
                "status": "ACKED",
                "message": "ok",
            },
        },
        headers=HEADERS,
    )

    pruned = client.post("/api/gateway/commands/prune?older_than_sec=0", headers=HEADERS).json()
    duplicate = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="prune-safe"), headers=HEADERS).json()

    assert pruned["removed"] == 1
    assert duplicate["accepted"] is False
    assert duplicate["duplicate_of"] == first["command_id"]
