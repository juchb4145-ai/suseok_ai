import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "1")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "1")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def test_gateway_event_ingest_creates_transport_latency_sample(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}

    response = client.post(
        "/api/gateway/events",
        headers=headers,
        json={
            "type": "condition_event",
            "event_id": "evt-latency-condition",
            "source": "mock_gateway",
            "payload": {
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "transport_trace": {
                    "trace_id": "trace-event-1",
                    "gateway_event_created_at_utc": "2026-05-30T09:00:00.000+00:00",
                    "gateway_event_post_end_at_utc": "2026-05-30T09:00:00.010+00:00",
                },
            },
        },
    )

    assert response.status_code == 200
    db = TradingDatabase(str(db_path))
    try:
        samples = db.list_gateway_transport_latency_samples(event_id="evt-latency-condition")
    finally:
        db.close()
    assert samples
    assert samples[0]["direction"] == "gateway_to_core"


def test_command_long_poll_empty_response_records_metric(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)

    response = client.get("/api/gateway/commands?wait_sec=0", headers={"X-Local-Token": "test-token"})

    assert response.status_code == 200
    db = TradingDatabase(str(db_path))
    try:
        samples = db.list_gateway_transport_latency_samples(message_type="command_poll_empty")
    finally:
        db.close()
    assert samples
    assert samples[0]["direction"] == "core_to_gateway"


def test_command_dispatch_and_ack_create_transport_metrics(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}
    client.post("/api/gateway/commands", json={"type": "login", "command_id": "cmd-transport"}, headers=headers)

    batch = client.get("/api/gateway/commands?wait_sec=0", headers=headers).json()
    command = batch["commands"][0]
    assert command["payload"]["transport_trace"]["core_command_long_poll_response_at_utc"]

    ack = client.post(
        "/api/gateway/events",
        headers=headers,
        json={
            "type": "command_ack",
            "event_id": "evt-transport-ack",
            "command_id": "cmd-transport",
            "payload": {
                "command_id": "cmd-transport",
                "command_type": "login",
                "status": "ACKED",
                "message": "ok",
                "result_code": 0,
                "transport_trace": {
                    **command["payload"]["transport_trace"],
                    "gateway_command_ack_created_at_utc": "2026-05-30T09:00:01.000+00:00",
                },
            },
        },
    )
    assert ack.status_code == 200

    db = TradingDatabase(str(db_path))
    try:
        command_samples = db.list_gateway_transport_latency_samples(command_id="cmd-transport")
    finally:
        db.close()
    assert any(sample["direction"] == "core_to_gateway" for sample in command_samples)
    assert any(sample["direction"] == "gateway_ack_to_core" for sample in command_samples)


def test_transport_latency_report_api_and_export(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    db = TradingDatabase(str(db_path))
    try:
        db.save_gateway_transport_latency_sample(
            {
                "sample_id": "sample-api-1",
                "trace_id": "trace-api-1",
                "trade_date": "2026-05-30",
                "direction": "core_to_gateway",
                "message_type": "tr_request",
                "success": True,
                "total_wall_ms": 1200,
                "long_poll_wait_ms": 900,
                "created_at": "2026-05-30T09:00:00.000+00:00",
            }
        )
    finally:
        db.close()

    status = client.get("/api/gateway/transport/status").json()
    assert status["latest_summary"]["count"] >= 1

    summary = client.get("/api/gateway/transport/latency/summary?trade_date=2026-05-30").json()
    assert summary["summary"]["command_latency_p95_ms"] >= 0

    rebuild = client.post(
        "/api/gateway/transport/latency/rebuild?trade_date=2026-05-30&persist=true&export=true",
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert rebuild["saved"]["report_id"]
    assert Path(rebuild["export_paths"]["md"]).exists()

    decision = client.get("/api/gateway/transport/websocket-decision?trade_date=2026-05-30").json()
    assert "recommendation" in decision

    snapshot = client.get("/api/snapshot").json()
    assert "transport" in snapshot
