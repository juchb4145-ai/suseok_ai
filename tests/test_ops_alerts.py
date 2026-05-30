import importlib
from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def test_ops_alerts_reports_disconnected_gateway(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    payload = client.get("/api/ops/alerts").json()

    assert payload["summary"]["critical"] >= 1
    assert payload["summary"]["safe_to_collect_data"] is False
    assert any(item["id"] == "GATEWAY_DISCONNECTED" for item in payload["alerts"])


def test_ops_alerts_allows_data_collection_after_healthy_heartbeat(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}

    response = client.post(
        "/api/gateway/events",
        headers=headers,
        json={
            "type": "heartbeat",
            "source": "kiwoom_gateway",
            "payload": {
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": "1234567890",
                "mode": "OBSERVE",
                "transport_mode": "websocket_real_pilot",
            },
        },
    )
    assert response.status_code == 200

    payload = client.get("/api/ops/alerts").json()

    assert payload["summary"]["critical"] == 0
    assert payload["summary"]["safe_to_collect_data"] is True
    assert payload["summary"]["safe_to_live_order"] is False


def test_dashboard_snapshot_contains_ops_alerts(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    snapshot = client.get("/api/snapshot").json()

    assert "ops_alerts" in snapshot
    assert "summary" in snapshot["ops_alerts"]
    assert "alerts" in snapshot["ops_alerts"]
