import importlib
import asyncio
import json
from argparse import Namespace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.kiwoom_gateway import RestCoreClient, _build_core_client, _prioritize_gateway_events, _websocket_pilot_command_rejection
from storage.db import TradingDatabase
from trading.broker.gateway_transport import WebSocketPilotPolicy, WebSocketRealCoreClient
from trading.broker.models import GatewayCommand, GatewayEvent
from trading.broker.transport_metrics import TRANSPORT_MODE_WEBSOCKET_REAL_PILOT, trace_from_payload
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


def test_websocket_pilot_snapshot_reflects_order_command_allow_policy(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS", "0")
    client = _build_core_client(_args(transport="websocket-pilot"))

    snapshot = client.snapshot()
    client.stop()

    assert snapshot["ws_pilot_live_order_blocked"] is False
    assert snapshot["ws_pilot_order_commands_allowed"] is True


def test_websocket_transport_keepalive_is_not_kiwoom_status(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))

    message = client._heartbeat_message()
    client.stop()

    assert message.type == "transport_heartbeat"
    assert message.payload["transport_keepalive"] is True
    assert message.payload["ws_heartbeat_compact"] is True
    assert "kiwoom_logged_in" not in message.payload
    assert "orderable" not in message.payload
    assert "account" not in message.payload


def test_websocket_real_client_compacts_status_heartbeat_payload(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))
    client.ws_priority_price_tick_codes = {"005930": {"holding"}}

    message = client._message_from_event(
        GatewayEvent(
            type="heartbeat",
            event_id="evt-heartbeat-compact",
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "mode": "DRY_RUN",
                "account": "1234567890",
                "accounts": ["1234567890", "2222222222", "3333333333", "4444444444", "5555555555", "6666666666"],
                "broker_name": "KIWOOM",
                "broker_env": "SIMULATION",
                "server_mode": "SIMULATION",
                "account_mode": "SIMULATION",
                "server_gubun": "1",
                "rate_limit": {
                    "commands": {
                        "tr_request": {"allowed_count": 3, "limited_count": 1, "wait_time_sec": 0.8},
                        "login": {"allowed_count": 1, "limited_count": 0, "wait_time_sec": 0.0},
                    }
                },
                "fallback": {"transport_mode": "rest_long_poll", "gateway_last_poll_ms": 123},
                "ws_priority_price_tick_codes": ["005930"] * 100,
                "transport_trace": {"trace_id": "trace-heartbeat-compact"},
            },
        )
    )
    client.stop()

    assert message.type == "heartbeat"
    assert message.payload["kiwoom_logged_in"] is True
    assert message.payload["orderable"] is True
    assert message.payload["account"] == "1234567890"
    assert message.payload["accounts"] == ["1234567890", "2222222222", "3333333333", "4444444444", "5555555555"]
    assert message.payload["broker_env"] == "SIMULATION"
    assert message.payload["server_mode"] == "SIMULATION"
    assert message.payload["account_mode"] == "SIMULATION"
    assert message.payload["server_gubun"] == "1"
    assert message.payload["ws_heartbeat_compact"] is True
    assert "rate_limit" not in message.payload
    assert "fallback" not in message.payload
    assert "ws_priority_price_tick_codes" not in message.payload
    assert message.payload["rate_limit_summary"]["command_count"] == 2
    assert message.payload["rate_limit_summary"]["limited_count"] == 1
    assert message.payload["rate_limit_summary"]["max_wait_time_sec"] == 0.8
    assert set(message.payload["ws_heartbeat_omitted_fields"]) == {
        "rate_limit",
        "fallback",
        "ws_priority_price_tick_codes",
    }
    assert trace_from_payload(message.payload)["trace_id"] == "trace-heartbeat-compact"


def test_websocket_send_injects_send_started_trace(monkeypatch):
    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    client = _build_core_client(_args(transport="websocket-pilot"))
    fake_ws = FakeWebSocket()
    message = client._message_from_event(
        GatewayEvent(
            type="heartbeat",
            event_id="evt-heartbeat-send-trace",
            payload={"transport_trace": {"gateway_ws_send_queued_at_utc": "2026-06-08T00:00:00.000+00:00"}},
        )
    )

    asyncio.run(client._send_ws(fake_ws, message))
    client.stop()

    sent = GatewayWsMessage.from_dict(json.loads(fake_ws.sent[0]))
    trace = trace_from_payload(sent.payload)
    assert trace["gateway_ws_send_queued_at_utc"]
    assert trace["gateway_ws_send_started_at_utc"]
    assert trace["gateway_ws_send_started_monotonic_ms"] > 0
    assert sent.metadata["gateway_ws_send_started_at_utc"] == trace["gateway_ws_send_started_at_utc"]
    diagnostic = GatewayWsMessage.from_dict(json.loads(fake_ws.sent[1]))
    assert diagnostic.type == "transport_send_completed"
    assert diagnostic.payload["original_message_id"] == sent.message_id
    assert diagnostic.payload["original_type"] == "heartbeat"
    assert diagnostic.payload["sample_message_type"] == "heartbeat"
    assert diagnostic.payload["original_sequence"] == sent.sequence
    assert diagnostic.payload["gateway_ws_send_completed_at_utc"]
    assert diagnostic.payload["gateway_ws_send_duration_ms"] >= 0
    snapshot = client.snapshot()
    assert snapshot["ws_last_send_ms"] >= 0
    assert snapshot["ws_last_send_completed_message_id"] == sent.message_id
    assert snapshot["ws_last_send_completed_message_type"] == "heartbeat"


def test_websocket_connection_loop_drains_incoming_while_send_is_blocked():
    class SlowSendWebSocket:
        def __init__(self):
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.recv_count = 0
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)
            self.send_started.set()
            await self.release_send.wait()

        async def recv(self):
            if self.recv_count == 0:
                self.recv_count += 1
                return json.dumps(
                    GatewayWsMessage(
                        type="core_command_batch",
                        payload={
                            "commands": [
                                {
                                    "type": "login",
                                    "command_id": "cmd-drain-while-send",
                                    "payload": {"transport_trace": {"trace_id": "trace-drain-while-send"}},
                                }
                            ],
                            "count": 1,
                        },
                        sequence=42,
                    ).to_dict()
                )
            await asyncio.Event().wait()

    async def scenario():
        client = WebSocketRealCoreClient(
            core_url="http://127.0.0.1:8000",
            ws_url="ws://127.0.0.1:8000/ws/gateway/transport",
            token="test-token",
            policy=WebSocketPilotPolicy(enabled=True, allow_real=True, heartbeat_interval_sec=60.0),
        )
        fake_ws = SlowSendWebSocket()
        client._outbound.put(
            client._message_from_event(GatewayEvent(type="price_tick", event_id="evt-blocked-send", payload={"code": "005930"}))
        )
        task = asyncio.create_task(client._connection_loop(fake_ws))
        await asyncio.wait_for(fake_ws.send_started.wait(), timeout=1.0)
        deadline = asyncio.get_running_loop().time() + 1.0
        while client._commands.empty() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert not client._commands.empty()
        assert client._commands.get_nowait().command_id == "cmd-drain-while-send"
        client._stop.set()
        fake_ws.release_send.set()
        await asyncio.wait_for(task, timeout=1.0)
        client.stop()

    asyncio.run(scenario())


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
    assert client._control_outbound.qsize() == 1
    assert client._control_outbound.get_nowait().type == "command_ack"
    snapshot = client.snapshot()
    assert snapshot["ws_price_tick_sampled_count"] == 0
    assert snapshot["ws_price_tick_fallback_count"] == 1


def test_websocket_real_client_batches_condition_events_and_keeps_control_priority(monkeypatch):
    class Fallback:
        transport_mode = "rest_long_poll"
        last_poll_error = ""

        def post_event(self, event):
            return {"accepted": True, "transport_mode": self.transport_mode}

        def poll_commands(self, *, limit=20, wait_sec=1.0):
            return []

        def start(self):
            return None

        def stop(self):
            return None

        def snapshot(self):
            return {"transport_mode": self.transport_mode}

    client = WebSocketRealCoreClient(
        core_url="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/gateway/transport",
        token="test-token",
        fallback_client=Fallback(),
        policy=WebSocketPilotPolicy(
            enabled=True,
            allow_real=True,
            condition_event_batch_enabled=True,
            condition_event_batch_max_size=10,
            condition_event_batch_max_wait_ms=0,
        ),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.post_event(
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-1",
            payload={"condition_name": "entry", "condition_index": 7, "code": "005930", "event_type": "include"},
        )
    )
    client.post_event(
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-2",
            payload={"condition_name": "entry", "condition_index": 7, "code": "000660", "event_type": "include"},
        )
    )
    client.post_event(GatewayEvent(type="command_ack", command_id="cmd-1", payload={"status": "ACKED"}))

    pending = []
    started = client._drain_condition_events(pending, batch_started=0.0)
    batch = client._condition_batch_message(pending)
    control = client._next_control_message()
    snapshot = client.snapshot()
    client.stop()

    assert started > 0
    assert control is not None
    assert control.type == "command_ack"
    assert batch.type == "condition_event_batch"
    assert batch.payload["count"] == 2
    assert [item["event_id"] for item in batch.payload["events"]] == ["evt-cond-1", "evt-cond-2"]
    assert snapshot["ws_condition_event_batch_queued_count"] == 2
    assert snapshot["ws_condition_event_batch_sent_count"] == 1
    assert snapshot["ws_condition_event_batched_count"] == 2


def test_websocket_real_client_coalesces_duplicate_condition_events_inside_batch(monkeypatch):
    client = WebSocketRealCoreClient(
        core_url="http://127.0.0.1:8000",
        ws_url="ws://127.0.0.1:8000/ws/gateway/transport",
        token="test-token",
        policy=WebSocketPilotPolicy(enabled=True, allow_real=True, condition_event_batch_max_wait_ms=0),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.post_event(
        GatewayEvent(
            type="condition_event",
            event_id="evt-old",
            payload={
                "condition_name": "entry",
                "condition_index": 7,
                "code": "A005930",
                "event_type": "include",
                "sequence": 1,
            },
        )
    )
    client.post_event(
        GatewayEvent(
            type="condition_event",
            event_id="evt-new",
            payload={
                "condition_name": "entry",
                "condition_index": 7,
                "code": "005930",
                "event_type": "include",
                "sequence": 2,
            },
        )
    )

    pending = []
    client._drain_condition_events(pending, batch_started=0.0)
    batch = client._condition_batch_message(pending)
    client.stop()

    assert batch.payload["count"] == 1
    assert batch.payload["events"][0]["event_id"] == "evt-new"
    assert batch.payload["events"][0]["payload"]["sequence"] == 2
    assert client.snapshot()["ws_condition_event_batch_coalesced_count"] == 1


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


def test_websocket_real_client_prioritizes_watchset_and_holding_ticks_to_ws(monkeypatch):
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
        policy=WebSocketPilotPolicy(enabled=True, allow_real=True, price_tick_sample_rate=0.0),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.apply_realtime_subscription_update(
        "register_realtime",
        {
            "codes": ["000001", "000270", "005930"],
            "code_sources": {
                "000001": ["theme_lab_watchset"],
                "000270": ["holding"],
                "005930": ["candidate_watch"],
            },
        },
    )
    client.post_event(GatewayEvent(type="price_tick", payload={"code": "000001"}))
    client.post_event(GatewayEvent(type="price_tick", payload={"code": "000270"}))
    client.post_event(GatewayEvent(type="price_tick", payload={"code": "005930"}))
    client.stop()

    assert [event.payload["code"] for event in fallback.events] == ["005930"]
    assert [client._outbound.get_nowait().payload["type"] for _ in range(client._outbound.qsize())] == [
        "price_tick",
        "price_tick",
    ]
    snapshot = client.snapshot()
    assert snapshot["ws_priority_price_tick_code_count"] == 2
    assert snapshot["ws_priority_price_tick_sampled_count"] == 2
    assert snapshot["ws_price_tick_fallback_count"] == 1


def test_websocket_real_client_removes_priority_tick_code_after_realtime_remove(monkeypatch):
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
        policy=WebSocketPilotPolicy(enabled=True, allow_real=True, price_tick_sample_rate=0.0),
    )
    monkeypatch.setattr(client, "start", lambda: None)

    client.apply_realtime_subscription_update("register_realtime", {"codes": ["000001"], "code_sources": {"000001": ["theme_lab_watchset"]}})
    client.apply_realtime_subscription_update("remove_realtime", {"codes": ["000001"]})
    client.post_event(GatewayEvent(type="price_tick", payload={"code": "000001"}))
    client.stop()

    assert [event.payload["code"] for event in fallback.events] == ["000001"]
    assert client.snapshot()["ws_priority_price_tick_code_count"] == 0


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
        last_heartbeat_payload = {"broker_env": "SIMULATION"}

    rejection = _websocket_pilot_command_rejection(
        Runtime(),
        GatewayCommand(type="send_order", command_id="cmd-order", payload={"code": "005930"}),
    )

    assert rejection == "WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED"


def test_websocket_pilot_allows_order_commands_only_for_simulation(monkeypatch):
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_REAL_PILOT", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS", "1")
    monkeypatch.setenv("TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS", "0")
    client = _build_core_client(_args(transport="websocket-pilot"))

    class Runtime:
        core_client = client

        def __init__(self, broker_env):
            self.last_heartbeat_payload = {"broker_env": broker_env}

    command = GatewayCommand(type="send_order", command_id="cmd-order", payload={"code": "005930"})

    assert _websocket_pilot_command_rejection(Runtime("SIMULATION"), command) == ""
    assert (
        _websocket_pilot_command_rejection(Runtime("REAL"), command)
        == "WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED_REAL_ACCOUNT"
    )
    assert (
        _websocket_pilot_command_rejection(Runtime(""), command)
        == "WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED_ACCOUNT_UNKNOWN"
    )
    client.stop()


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
    assert status["core_ws_outbound_queue_size"] == 0
    assert status["core_ws_outbound_queue_max_size"] >= 1
    assert status["core_ws_outbound_queued_count"] >= 3
    assert status["core_ws_outbound_sent_count"] >= 3
    assert status["core_ws_outbound_dropped_count"] == 0
    assert status["core_ws_last_send_json_ms"] >= 0.0
    assert status["core_ws_last_send_queue_wait_ms"] >= 0.0
    assert status["core_ws_last_send_json_type"] in {"hello_ack", "event_ack"}
    assert status["core_ws_receive_loop_gap_ms"] >= 0.0
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
