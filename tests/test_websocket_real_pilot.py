import importlib
from argparse import Namespace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.kiwoom_gateway import RestCoreClient, _build_core_client, _websocket_pilot_command_rejection
from trading.broker.gateway_transport import WebSocketRealCoreClient
from trading.broker.models import GatewayCommand
from trading.broker.transport_metrics import TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
from trading.broker.ws_messages import GatewayWsMessage


def _args(**overrides):
    data = {
        "transport": "rest",
        "core_url": "http://127.0.0.1:8000",
        "token": "test-token",
        "metrics_enabled": True,
        "ws_url": "ws://127.0.0.1:8000/ws/gateway/transport",
        "mock": False,
    }
    data.update(overrides)
    return Namespace(**data)


def test_gateway_default_transport_is_rest(monkeypatch):
    monkeypatch.delenv("TRADING_GATEWAY_TRANSPORT", raising=False)
    client = _build_core_client(_args())

    assert isinstance(client, RestCoreClient)
    assert client.transport_mode == "rest_long_poll"


def test_websocket_pilot_without_feature_flags_falls_back_to_rest(monkeypatch):
    monkeypatch.delenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", raising=False)
    monkeypatch.delenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", raising=False)
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST", "1")

    client = _build_core_client(_args(transport="websocket-pilot"))

    assert isinstance(client, RestCoreClient)
    assert client.transport_mode == "rest_long_poll"


def test_websocket_pilot_feature_flags_select_real_client(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")

    client = _build_core_client(_args(transport="websocket-pilot"))

    assert isinstance(client, WebSocketRealCoreClient)
    assert client.transport_mode == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
    assert client.snapshot()["ws_pilot_live_order_blocked"] is True


def test_websocket_real_client_receives_command_batch_without_ack(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))
    message = GatewayWsMessage(
        type="core_command_batch",
        payload={
            "commands": [
                {
                    "type": "login",
                    "command_id": "cmd-ws-real",
                    "payload": {"transport_trace": {"trace_id": "trace-ws-real"}},
                }
            ]
        },
        metadata={"transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT},
        sequence=3,
    )

    client._handle_incoming(message)
    try:
        commands = client.poll_commands(limit=1, wait_sec=0)
    finally:
        client.stop()

    assert commands[0].command_id == "cmd-ws-real"
    assert commands[0].payload["transport_trace"]["transport_mode"] == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
    assert client.snapshot()["ws_command_queue_size"] == 0


def test_websocket_pilot_blocks_order_commands_by_default(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))

    class Runtime:
        core_client = client

    rejection = _websocket_pilot_command_rejection(
        Runtime(),
        GatewayCommand(type="send_order", command_id="cmd-order", payload={"code": "005930"}),
    )

    assert rejection == "WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED"


def test_core_ws_endpoint_accepts_real_pilot_mode_and_status(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "1")
    import trading_app.api as api

    api = importlib.reload(api)
    client = TestClient(api.app)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="hello",
                source="kiwoom_gateway",
                payload={
                    "transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
                    "reconnect_count": 2,
                    "pilot_enabled": True,
                    "live_order_enabled": False,
                },
                metadata={"transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT},
                sequence=1,
            ).to_dict()
        )
        hello_ack = ws.receive_json()
        assert hello_ack["payload"]["transport_mode"] == TRANSPORT_MODE_WEBSOCKET_REAL_PILOT
        ws.send_json(
            GatewayWsMessage(
                type="heartbeat",
                source="kiwoom_gateway",
                payload={
                    "transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
                    "ws_pilot_enabled": True,
                    "ws_connection_state": "AUTHENTICATED",
                    "ws_reconnect_count": 2,
                    "pilot_blocked_order_command_count": 1,
                    "kiwoom_logged_in": True,
                    "orderable": False,
                    "mode": "OBSERVE",
                },
                metadata={"transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT},
                sequence=2,
            ).to_dict()
        )
        ws.receive_json()

    status = client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]
    assert status["enabled"] is True
    assert status["reconnect_count"] == 2
    assert status["blocked_order_command_count"] == 1

    decision = client.get("/api/gateway/transport/websocket-decision").json()
    assert "real_pilot_summary" in decision
    assert decision["switch_to_websocket_ready"] is False
