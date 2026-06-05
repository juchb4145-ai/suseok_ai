import importlib
from argparse import Namespace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.kiwoom_gateway import RestCoreClient, _build_core_client, _prioritize_gateway_events, _websocket_pilot_command_rejection
from storage.db import TradingDatabase
from trading.broker.gateway_transport import WebSocketPilotPolicy, WebSocketRealCoreClient
from trading.broker.models import GatewayCommand, GatewayEvent
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


def test_websocket_transport_keepalive_is_not_kiwoom_status(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))

    message = client._heartbeat_message()
    client.stop()

    assert message.type == "transport_heartbeat"
    assert message.payload["transport_keepalive"] is True
    assert "kiwoom_logged_in" not in message.payload
    assert "orderable" not in message.payload
    assert "account" not in message.payload


def test_websocket_real_client_snapshots_error_diagnostics(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))

    client._record_ws_error(RuntimeError("connect failed token=super-secret"), stage="connect")
    client._maybe_fallback("reconnect_limit", "connect failed token=super-secret")
    snapshot = client.snapshot()
    client.stop()

    assert snapshot["ws_last_error_type"] == "RuntimeError"
    assert snapshot["ws_last_error_stage"] == "connect"
    assert snapshot["ws_fallback_reason"] == "reconnect_limit"
    assert "super-secret" not in snapshot["ws_last_error"]
    assert "super-secret" not in snapshot["ws_fallback_detail"]


def test_websocket_real_client_clears_stale_error_after_auth(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))

    client.connection_state = "CONNECTED"
    client._record_ws_error(ConnectionRefusedError("connect refused"), stage="connect")
    client._handle_incoming(
        GatewayWsMessage(
            type="hello_ack",
            payload={
                "transport_mode": TRANSPORT_MODE_WEBSOCKET_REAL_PILOT,
                "websocket_session_id": "ws-session-ok",
            },
        )
    )
    snapshot = client.snapshot()
    client.stop()

    assert snapshot["ws_connection_state"] == "AUTHENTICATED"
    assert snapshot["ws_last_error"] == ""
    assert snapshot["ws_last_error_type"] == ""
    assert snapshot["ws_last_error_stage"] == ""


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


def test_websocket_real_client_routes_only_control_events_to_ws(monkeypatch):
    class Fallback:
        transport_mode = "rest_long_poll"

        def __init__(self):
            self.events = []
            self.last_poll_error = ""

        def post_event(self, event):
            self.events.append(event)
            return {"accepted": True, "transport_mode": self.transport_mode}

        def poll_commands(self, *, limit=20, wait_sec=1.0):
            return []

        def start(self):
            return None

        def stop(self):
            return None

        def snapshot(self):
            return {"transport_mode": self.transport_mode}

    fallback = Fallback()
    client = WebSocketRealCoreClient(
        core_url="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/gateway/transport",
        token="test-token",
        fallback_client=fallback,
        policy=WebSocketPilotPolicy(enabled=True, allow_real=True),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.post_event(GatewayEvent(type="price_tick", payload={"code": "005930"}))
    client.post_event(GatewayEvent(type="command_ack", command_id="cmd-1", payload={"status": "ACKED"}))
    client.stop()

    assert [event.type for event in fallback.events] == ["price_tick"]
    assert client._outbound.qsize() == 1
    assert client._outbound.get_nowait().type == "command_ack"
    snapshot = client.snapshot()
    assert snapshot["ws_price_tick_sampled_count"] == 0
    assert snapshot["ws_price_tick_fallback_count"] == 1


def test_websocket_real_client_can_sample_price_ticks_to_ws(monkeypatch):
    class Fallback:
        transport_mode = "rest_long_poll"
        last_poll_error = ""

        def __init__(self):
            self.events = []

        def post_event(self, event):
            self.events.append(event)
            return {"accepted": True, "transport_mode": self.transport_mode}

        def poll_commands(self, *, limit=20, wait_sec=1.0):
            return []

        def start(self):
            return None

        def stop(self):
            return None

        def snapshot(self):
            return {"transport_mode": self.transport_mode}

    fallback = Fallback()
    client = WebSocketRealCoreClient(
        core_url="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/gateway/transport",
        token="test-token",
        fallback_client=fallback,
        policy=WebSocketPilotPolicy(enabled=True, allow_real=True, price_tick_sample_rate=1.0),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.post_event(GatewayEvent(type="price_tick", payload={"code": "005930"}))
    client.stop()

    assert fallback.events == []
    assert client._outbound.get_nowait().type == "gateway_event"
    snapshot = client.snapshot()
    assert snapshot["ws_price_tick_sample_rate"] == 1.0
    assert snapshot["ws_price_tick_sampled_count"] == 1
    assert snapshot["ws_price_tick_fallback_count"] == 0


def test_gateway_network_prioritizes_control_events_before_price_ticks():
    events = [
        GatewayEvent(type="price_tick", payload={"code": "005930"}),
        GatewayEvent(type="heartbeat", payload={"kiwoom_logged_in": True}),
        GatewayEvent(type="price_tick", payload={"code": "000660"}),
        GatewayEvent(type="command_ack", command_id="cmd-1", payload={"status": "ACKED"}),
    ]

    prioritized = _prioritize_gateway_events(events)

    assert [event.type for event in prioritized] == ["heartbeat", "command_ack", "price_tick", "price_tick"]


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
                    "ws_fallback_reason": "reconnect_limit",
                    "ws_fallback_detail": "server closed connection",
                    "ws_last_error": "server closed connection",
                    "ws_last_error_type": "ConnectionClosedError",
                    "ws_last_error_stage": "recv",
                    "ws_last_error_at": "2026-06-01T00:00:01.000+00:00",
                    "ws_last_close_code": "1006",
                    "pilot_blocked_order_command_count": 1,
                    "ws_price_tick_sample_rate": 0.01,
                    "ws_price_tick_sampled_count": 7,
                    "ws_price_tick_fallback_count": 701,
                    "ws_event_fallback_count": 2,
                    "last_ws_event_at": "2026-06-01T00:00:02.000+00:00",
                    "last_ws_ack_at": "2026-06-01T00:00:03.000+00:00",
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
    assert status["fallback_reason"] == "reconnect_limit"
    assert status["last_error_type"] == "ConnectionClosedError"
    assert status["last_error_stage"] == "recv"
    assert status["last_close_code"] == "1006"
    assert status["blocked_order_command_count"] == 1
    assert status["price_tick_sample_rate"] == 0.01
    assert status["price_tick_sampled_count"] == 7
    assert status["price_tick_fallback_count"] == 701
    assert status["event_fallback_count"] == 2
    assert status["last_ws_event_at"] == "2026-06-01T00:00:02.000+00:00"
    assert status["last_ws_ack_at"] == "2026-06-01T00:00:03.000+00:00"
    db = TradingDatabase(str(db_path))
    try:
        logs = "\n".join(db.recent_logs(limit=20))
    finally:
        db.close()
    assert "[gateway][ws_real_pilot][WARN]" in logs
    assert "stage=recv" in logs

    decision = client.get("/api/gateway/transport/websocket-decision").json()
    assert "real_pilot_summary" in decision
    assert decision["switch_to_websocket_ready"] is False
