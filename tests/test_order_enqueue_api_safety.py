import importlib

from fastapi.testclient import TestClient


HEADERS = {"X-Local-Token": "test-token"}


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


def test_observe_mode_does_not_enqueue_send_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="OBSERVE")
    _healthy_gateway(client)

    response = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS)

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "OBSERVE_MODE"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0


def test_live_mode_requires_explicit_allow_live(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="0")
    _healthy_gateway(client)

    response = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS)

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "LIVE_REQUIRES_TRADING_ALLOW_LIVE"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0


def test_gateway_unhealthy_rejects_live_order(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")

    response = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS)

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] in {"GATEWAY_NOT_CONNECTED", "GATEWAY_HEARTBEAT_STALE"}


def test_live_order_enqueue_creates_gateway_command_and_blocks_duplicate(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")
    _healthy_gateway(client)

    first = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once"), headers=HEADERS).json()
    second = client.post("/api/orders/enqueue", json=_order_payload(idempotency_key="order-once"), headers=HEADERS).json()

    assert first["accepted"] is True
    assert first["command"]["type"] == "send_order"
    assert second["accepted"] is False
    assert second["reason"] == "DUPLICATE_ORDER_COMMAND"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 1


def test_live_daily_order_limit_uses_persistent_trade_date_count(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MAX_DAILY_ORDERS_PER_CODE", "1")
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")
    _healthy_gateway(client)

    first = client.post(
        "/api/orders/enqueue",
        json=_order_payload(idempotency_key="daily-1", candidate_id=1),
        headers=HEADERS,
    ).json()
    second = client.post(
        "/api/orders/enqueue",
        json=_order_payload(idempotency_key="daily-2", candidate_id=2),
        headers=HEADERS,
    ).json()

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["reason"] == "DAILY_CODE_ORDER_LIMIT"


def test_order_enqueue_requires_valid_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="DRY_RUN")

    missing = client.post("/api/orders/enqueue", json=_order_payload(dry_run=True))
    wrong = client.post("/api/orders/enqueue", json=_order_payload(dry_run=True), headers={"X-Local-Token": "wrong"})

    assert missing.status_code in {401, 403}
    assert wrong.status_code in {401, 403}


def test_gateway_command_api_rejects_order_command_types(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for command_type in ["send_order", "cancel_order", "modify_order"]:
        response = client.post("/api/gateway/commands", json={"type": command_type, "command_id": f"cmd-{command_type}"}, headers=HEADERS)

        assert response.status_code == 400
        assert response.json()["detail"] == "ORDER_COMMAND_REQUIRES_ORDER_ENQUEUE"


def test_live_buy_price_zero_is_rejected_and_cannot_bypass_amount_limit(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, mode="LIVE", allow_live="1")
    _healthy_gateway(client)

    response = client.post(
        "/api/orders/enqueue",
        json=_order_payload(quantity=1_000_000, price=0, idempotency_key="zero-price"),
        headers=HEADERS,
    )

    payload = response.json()
    assert payload["accepted"] is False
    assert payload["reason"] == "PRICE_INVALID"
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0


def test_command_mutation_and_payload_views_require_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/gateway/commands", json={"type": "login", "command_id": "cmd-protected"}, headers=HEADERS)

    assert client.post("/api/gateway/commands/prune?older_than_sec=0").status_code in {401, 403}
    assert client.post("/api/gateway/commands/cmd-protected/cancel").status_code in {401, 403}
    assert client.get("/api/gateway/commands/history?include_payload=true").status_code in {401, 403}
    assert client.get("/api/gateway/commands/cmd-protected?include_payload=true").status_code in {401, 403}

    assert client.get("/api/gateway/commands/history").status_code == 200
    detail = client.get("/api/gateway/commands/cmd-protected").json()
    assert detail["found"] is True
    assert "payload" not in detail["record"]["command"]
