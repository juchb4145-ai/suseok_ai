import importlib
from pathlib import Path

from fastapi.testclient import TestClient
from trading_app.ops_alerts import build_ops_alerts


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


def test_ops_alerts_downgrades_heartbeat_only_event_latency():
    payload = build_ops_alerts(
        gateway={"connected": True, "heartbeat_ok": True, "kiwoom_logged_in": True, "orderable": True},
        transport={
            "warning_flags": ["EVENT_P95_HIGH"],
            "event_latency_p95_ms": 900,
            "non_heartbeat_event_count": 0,
        },
    )

    assert not any(item["id"] == "TRANSPORT_EVENT_P95_HIGH" for item in payload["alerts"])
    assert any(item["id"] == "TRANSPORT_HEARTBEAT_P95_HIGH" and item["severity"] == "info" for item in payload["alerts"])


def test_ops_alerts_uses_recent_transport_rate_limit_count():
    payload = build_ops_alerts(
        gateway={"connected": True, "heartbeat_ok": True, "kiwoom_logged_in": True, "orderable": True},
        commands={"rate_limited_count": 261},
        transport={"rate_limited_count": 0},
    )

    assert not any(item["id"] == "COMMAND_RATE_LIMITED" for item in payload["alerts"])


def test_ops_alerts_ignore_empty_poll_command_latency():
    payload = build_ops_alerts(
        gateway={"connected": True, "heartbeat_ok": True, "kiwoom_logged_in": True, "orderable": True},
        transport={
            "warning_flags": ["COMMAND_P95_HIGH"],
            "command_latency_p95_ms": 1200,
            "active_command_count": 0,
            "empty_poll_rate": 1.0,
        },
    )

    assert not any(item["id"] == "TRANSPORT_COMMAND_P95_HIGH" for item in payload["alerts"])


def test_ops_alerts_report_command_latency_when_commands_are_active():
    payload = build_ops_alerts(
        gateway={"connected": True, "heartbeat_ok": True, "kiwoom_logged_in": True, "orderable": True},
        transport={
            "warning_flags": ["COMMAND_P95_HIGH"],
            "command_latency_p95_ms": 1200,
            "active_command_count": 2,
        },
    )

    assert any(item["id"] == "TRANSPORT_COMMAND_P95_HIGH" for item in payload["alerts"])
