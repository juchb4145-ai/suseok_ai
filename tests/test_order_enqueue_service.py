import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase


HEADERS = {"X-Local-Token": "test-token"}


def _client(tmp_path, monkeypatch, *, mode="OBSERVE"):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setenv("TRADING_ALLOW_LIVE", "0")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), api


def _order_payload(**overrides):
    payload = {
        "account": "1234567890",
        "code": "005930",
        "side": "buy",
        "quantity": 2,
        "price": 70000,
        "order_type": 1,
        "hoga": "00",
        "tag": "DRY",
        "strategy_name": "test",
        "candidate_id": 7,
        "reason": "dry run",
        "idempotency_key": "dry-once",
        "dry_run": True,
    }
    payload.update(overrides)
    return payload


def test_api_dry_run_order_creates_intent_without_gateway_command(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS)
    payload = response.json()

    assert payload["accepted"] is True
    assert payload["status"] == "DRY_RUN_ACCEPTED"
    assert payload["intent_id"]
    assert payload["command"] is None
    assert payload["command_id"] == ""
    assert payload["live_would_pass"] is False
    assert client.get("/api/gateway/commands/status").json()["queued_count"] == 0

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        row = db.get_runtime_order_intent(payload["intent_id"])
    finally:
        db.close()
    assert row is not None
    assert row["idempotency_key"] == "dry-once"
    assert row["dedupe_key"] == payload["dedupe_key"]
    assert row["safety"]["ok"] is True
    assert row["live_safety"]["ok"] is False
    listing = client.get("/api/runtime/orders/dry-run").json()
    assert listing["summary"]["total"] == 1
    assert listing["items"][0]["intent_id"] == payload["intent_id"]
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["dry_run_orders"]["summary"]["total"] == 1


def test_api_dry_run_order_duplicate_survives_core_reload(tmp_path, monkeypatch):
    client, api = _client(tmp_path, monkeypatch)

    first = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS).json()
    assert first["accepted"] is True

    api = importlib.reload(api)
    client = TestClient(api.app)
    second = client.post("/api/orders/enqueue", json=_order_payload(), headers=HEADERS).json()

    assert second["accepted"] is False
    assert second["status"] == "DUPLICATE"
    assert second["duplicate_of"] == first["intent_id"]
    summary = client.get("/api/runtime/orders/dry-run/summary").json()
    assert summary["total"] == 1
    assert summary["duplicate"] >= 1


def test_api_dry_run_rejected_intent_is_stored(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    payload = client.post(
        "/api/orders/enqueue",
        json=_order_payload(idempotency_key="bad-price", price=0),
        headers=HEADERS,
    ).json()

    assert payload["accepted"] is False
    assert payload["status"] == "DRY_RUN_REJECTED"
    assert payload["reason"] == "PRICE_INVALID"
    detail = client.get(f"/api/runtime/orders/dry-run/{payload['intent_id']}").json()
    assert detail["found"] is True
    assert detail["record"]["status"] == "DRY_RUN_REJECTED"
