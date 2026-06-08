import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from storage.db import TradingDatabase
from trading.broker.ws_messages import GatewayWsMessage


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "1")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "1")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def _recv_until(ws, message_type: str, limit: int = 8):
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == message_type:
            return message
    raise AssertionError(f"message {message_type} not received")


def test_gateway_transport_ws_rejects_missing_token(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/gateway/transport"):
            pass


def test_gateway_transport_ws_hello_gateway_event_and_command_ack_flow(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}
    client.post("/api/gateway/commands", json={"type": "login", "command_id": "cmd-ws"}, headers=headers)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        assert ws.receive_json()["type"] == "hello_ack"
        ws.send_json(GatewayWsMessage(type="hello", metadata={"experiment_id": "exp-ws", "scenario": "basic"}).to_dict())
        assert _recv_until(ws, "hello_ack")["type"] == "hello_ack"

        ws.send_json(
            GatewayWsMessage(
                type="gateway_event",
                source="mock_websocket_gateway",
                payload={
                    "type": "condition_event",
                    "payload": {
                        "condition_name": "mock",
                        "condition_index": 1,
                        "code": "005930",
                        "event_type": "include",
                    },
                },
                metadata={"experiment_id": "exp-ws", "scenario": "basic"},
                sequence=1,
            ).to_dict()
        )
        assert _recv_until(ws, "event_ack")["payload"]["accepted"] is True

        ws.send_json(
            GatewayWsMessage(
                type="ready_for_commands",
                payload={"limit": 10},
                metadata={"experiment_id": "exp-ws", "scenario": "basic"},
                sequence=2,
            ).to_dict()
        )
        batch = _recv_until(ws, "core_command_batch")
        command = batch["payload"]["commands"][0]
        assert command["command_id"] == "cmd-ws"
        assert client.get("/api/gateway/commands/status").json()["dispatched_count"] == 1

        ws.send_json(
            GatewayWsMessage(
                type="command_started",
                command_id="cmd-ws",
                payload={
                    "command_id": "cmd-ws",
                    "command_type": "login",
                    "transport_trace": command["payload"]["transport_trace"],
                },
                metadata={"experiment_id": "exp-ws", "scenario": "basic"},
                sequence=3,
            ).to_dict()
        )
        _recv_until(ws, "event_ack")
        ws.send_json(
            GatewayWsMessage(
                type="command_ack",
                command_id="cmd-ws",
                payload={
                    "command_id": "cmd-ws",
                    "command_type": "login",
                    "status": "ACKED",
                    "message": "ok",
                    "result_code": 0,
                    "transport_trace": {
                        **command["payload"]["transport_trace"],
                        "gateway_command_ack_created_at_utc": "2026-05-30T09:00:01.000+00:00",
                    },
                },
                metadata={"experiment_id": "exp-ws", "scenario": "basic"},
                sequence=4,
            ).to_dict()
        )
        _recv_until(ws, "event_ack")

    assert client.get("/api/gateway/commands/status").json()["acked_count"] == 1
    db = TradingDatabase(str(db_path))
    try:
        samples = db.list_gateway_transport_latency_samples(command_id="cmd-ws", transport_mode="websocket_mock")
    finally:
        db.close()
    assert samples
    assert any(sample["experiment_id"] == "exp-ws" for sample in samples)


def test_gateway_transport_ws_real_pilot_price_tick_skips_latency_persistence_by_default(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="gateway_event",
                source="kiwoom_gateway",
                payload={
                    "type": "price_tick",
                    "event_id": "evt-ws-price-no-db",
                    "payload": {
                        "code": "005930",
                        "price": 70000,
                    },
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        assert _recv_until(ws, "event_ack")["payload"]["accepted"] is True

    db = TradingDatabase(str(db_path))
    try:
        samples = db.list_gateway_transport_latency_samples(event_id="evt-ws-price-no-db")
    finally:
        db.close()
    assert samples == []


def test_gateway_transport_ws_real_pilot_price_tick_can_persist_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_PERSIST_WS_PRICE_TICKS", "1")
    client, db_path = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="gateway_event",
                source="kiwoom_gateway",
                payload={
                    "type": "price_tick",
                    "event_id": "evt-ws-price-db",
                    "payload": {
                        "code": "005930",
                        "price": 70000,
                    },
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        assert _recv_until(ws, "event_ack")["payload"]["accepted"] is True

    db = TradingDatabase(str(db_path))
    try:
        samples = db.list_gateway_transport_latency_samples(event_id="evt-ws-price-db")
    finally:
        db.close()
    assert len(samples) == 1
    assert samples[0]["message_type"] == "price_tick"
    assert samples[0]["transport_mode"] == "websocket_real_pilot"


def test_gateway_transport_ws_command_failed_marks_failed(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    headers = {"X-Local-Token": "test-token"}
    client.post("/api/gateway/commands", json={"type": "tr_request", "command_id": "cmd-ws-fail"}, headers=headers)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(GatewayWsMessage(type="ready_for_commands", payload={"limit": 10}).to_dict())
        batch = _recv_until(ws, "core_command_batch")
        command = batch["payload"]["commands"][0]
        ws.send_json(
            GatewayWsMessage(
                type="command_failed",
                command_id="cmd-ws-fail",
                payload={
                    "command_id": "cmd-ws-fail",
                    "command_type": "tr_request",
                    "error": "boom",
                    "retryable": False,
                    "transport_trace": command["payload"]["transport_trace"],
                },
            ).to_dict()
        )
        _recv_until(ws, "event_ack")

    assert client.get("/api/gateway/commands/status").json()["failed_count"] == 1


def test_gateway_transport_ws_rate_limited_log_includes_trace_wait(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="rate_limited",
                command_id="cmd-rate-limited",
                payload={
                    "command_id": "cmd-rate-limited",
                    "command_type": "remove_realtime",
                    "transport_trace": {"wait_time_sec": 0.25},
                },
            ).to_dict()
        )
        _recv_until(ws, "event_ack")

    db = TradingDatabase(str(db_path))
    try:
        logs = "\n".join(db.recent_logs(limit=10))
    finally:
        db.close()
    assert "[gateway][rate_limited] remove_realtime cmd-rate-limited wait=0.25" in logs


def test_gateway_transport_ws_unknown_ack_does_not_crash(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="command_ack",
                command_id="cmd-unknown",
                payload={"command_id": "cmd-unknown", "command_type": "login", "status": "ACKED"},
            ).to_dict()
        )
        assert _recv_until(ws, "event_ack")["payload"]["accepted"] is True


def test_gateway_transport_ws_bad_json_returns_error_and_keeps_connection(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_text("{bad json")
        error = _recv_until(ws, "error")
        assert error["payload"]["accepted"] is False
        assert error["payload"]["code"] == "BAD_MESSAGE"

        ws.send_json(GatewayWsMessage(type="ping", sequence=2).to_dict())
        assert _recv_until(ws, "pong")["type"] == "pong"


def test_gateway_transport_ws_unsupported_type_returns_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(GatewayWsMessage(type="unknown_type", sequence=1).to_dict())
        error = _recv_until(ws, "error")
        assert error["payload"]["accepted"] is False
        assert error["payload"]["code"] == "UNSUPPORTED_MESSAGE_TYPE"


def test_transport_heartbeat_does_not_override_kiwoom_login(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="hello",
                source="kiwoom_gateway",
                payload={
                    "transport_mode": "websocket_real_pilot",
                    "pilot_enabled": True,
                    "live_order_enabled": False,
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        _recv_until(ws, "hello_ack")
        ws.send_json(
            GatewayWsMessage(
                type="heartbeat",
                source="kiwoom_gateway",
                payload={
                    "transport_mode": "websocket_real_pilot",
                    "kiwoom_logged_in": True,
                    "orderable": True,
                    "account": "1234567890",
                    "mode": "DRY_RUN",
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=2,
            ).to_dict()
        )
        _recv_until(ws, "event_ack")
        assert client.get("/api/gateway/status").json()["kiwoom_logged_in"] is True

        ws.send_json(
            GatewayWsMessage(
                type="transport_heartbeat",
                source="kiwoom_gateway",
                payload={
                    "transport_mode": "websocket_real_pilot",
                    "transport_keepalive": True,
                    "kiwoom_logged_in": False,
                    "orderable": False,
                    "account": "",
                    "ws_pilot_enabled": True,
                    "ws_connection_state": "AUTHENTICATED",
                    "ws_reconnect_count": 0,
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=3,
            ).to_dict()
        )
        ack = _recv_until(ws, "event_ack")
        assert ack["payload"]["transport_only"] is True

    status = client.get("/api/gateway/status").json()
    assert status["kiwoom_logged_in"] is True
    assert status["orderable"] is True
    assert status["account"] == "1234567890"
    assert status["last_heartbeat_payload"]["kiwoom_logged_in"] is True
