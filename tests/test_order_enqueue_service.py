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


def test_api_dry_run_order_sizes_down_instead_of_rejecting_amount_limit(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/orders/enqueue",
        json=_order_payload(idempotency_key="sized-down", code="096770", quantity=53, price=113000),
        headers=HEADERS,
    )
    payload = response.json()

    assert payload["accepted"] is True
    assert payload["status"] == "DRY_RUN_ACCEPTED"
    assert payload["request"]["quantity"] == 26
    assert payload["request"]["price"] == 113000

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        row = db.get_runtime_order_intent(payload["intent_id"])
    finally:
        db.close()
    assert row["status"] == "DRY_RUN_ACCEPTED"
    assert row["quantity"] == 26
    assert row["order_amount"] == 2_938_000
    assert row["safety"]["ok"] is True
    assert row["metadata"]["dry_run_quantity_adjusted_by_order_amount_limit"] is True
    assert row["metadata"]["dry_run_original_quantity"] == 53
    assert row["metadata"]["dry_run_original_order_amount"] == 5_989_000
    assert row["metadata"]["dry_run_adjusted_quantity"] == 26
    assert row["metadata"]["dry_run_adjusted_order_amount"] == 2_938_000
    validation = row["metadata"]["strategy_validation"]
    assert validation["execution_decision"] == "SIZED_DOWN"
    assert validation["hypothetical_qty"] == 53
    assert validation["actual_qty"] == 26


def test_api_dry_run_sized_order_can_retry_prior_amount_limit_rejection(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_runtime_order_intent(
            {
                "intent_id": "old-rejected",
                "trade_date": "2026-06-15",
                "source": "themelab_flow",
                "mode": "DRY_RUN",
                "dry_run": True,
                "status": "DRY_RUN_REJECTED",
                "reason": "ORDER_AMOUNT_LIMIT",
                "account": "dryrun-account",
                "code": "096770",
                "side": "buy",
                "quantity": 53,
                "price": 113000,
                "order_amount": 5_989_000,
                "order_type": 1,
                "hoga": "00",
                "tag": "DRY",
                "order_phase": "entry",
                "idempotency_key": "retry-sized",
                "dedupe_key": "old-original-quantity-dedupe",
                "safety": {"ok": False, "reason": "ORDER_AMOUNT_LIMIT"},
                "created_at": "2026-06-15T01:10:06+00:00",
                "updated_at": "2026-06-15T01:10:06+00:00",
            }
        )
    finally:
        db.close()

    payload = client.post(
        "/api/orders/enqueue",
        json=_order_payload(idempotency_key="retry-sized", code="096770", quantity=53, price=113000),
        headers=HEADERS,
    ).json()

    assert payload["accepted"] is True
    assert payload["status"] == "DRY_RUN_ACCEPTED"
    assert payload["duplicate_of"] == ""
    assert payload["intent_id"] != "old-rejected"

    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        row = db.get_runtime_order_intent(payload["intent_id"])
    finally:
        db.close()
    assert row["quantity"] == 26
    assert row["metadata"]["dry_run_quantity_adjusted_by_order_amount_limit"] is True


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
