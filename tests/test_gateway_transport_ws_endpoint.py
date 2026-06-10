import asyncio
import importlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading.broker.ws_messages import GatewayWsMessage


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "1")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED", "1")
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


def _wait_for_latency_samples(db_path: Path, event_id: str, *, timeout_sec: float = 2.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        db = TradingDatabase(str(db_path))
        try:
            samples = db.list_gateway_transport_latency_samples(event_id=event_id)
        finally:
            db.close()
        if samples:
            return samples
        time.sleep(0.05)
    return []


def _wait_for_latency_sample_stage(db_path: Path, event_id: str, stage_key: str, *, timeout_sec: float = 2.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        samples = _wait_for_latency_samples(db_path, event_id, timeout_sec=0.2)
        if samples and stage_key in dict(samples[0].get("stage_ms") or {}):
            return samples[0]
        time.sleep(0.05)
    return None


def _wait_for_command_status(client: TestClient, key: str, expected: int, *, timeout_sec: float = 2.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        status = client.get("/api/gateway/commands/status").json()
        if int(status.get(key) or 0) >= expected:
            return status
        time.sleep(0.05)
    return client.get("/api/gateway/commands/status").json()


def _wait_for_pilot_status(client: TestClient, key: str, expected: int, *, timeout_sec: float = 2.0):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        status = client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]
        if int(status.get(key) or 0) >= expected:
            return status
        time.sleep(0.05)
    return client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]


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
        assert _wait_for_command_status(client, "acked_count", 1)["acked_count"] == 1

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


def test_gateway_transport_ws_send_completed_diagnostic_updates_latency_sample(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        hello = ws.receive_json()
        session_id = hello["payload"]["websocket_session_id"]
        ws.send_json(
            GatewayWsMessage(
                type="heartbeat",
                message_id="ws-heartbeat-send-complete",
                trace_id="trace-heartbeat-send-complete",
                source="kiwoom_gateway",
                event_id="evt-heartbeat-send-complete",
                payload={
                    "transport_mode": "websocket_real_pilot",
                    "transport_trace": {
                        "trace_id": "trace-heartbeat-send-complete",
                        "gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00",
                        "gateway_ws_send_queued_at_utc": "2026-06-08T00:00:00.005+00:00",
                        "gateway_ws_send_started_at_utc": "2026-06-08T00:00:00.010+00:00",
                    },
                },
                metadata={"transport_mode": "websocket_real_pilot", "ws_session_id": session_id},
                sequence=10,
            ).to_dict()
        )
        assert _recv_until(ws, "event_ack")["payload"]["accepted"] is True
        assert _wait_for_latency_samples(db_path, "evt-heartbeat-send-complete")
        ws.send_json(
            GatewayWsMessage(
                type="transport_send_completed",
                trace_id="trace-heartbeat-send-complete",
                source="kiwoom_gateway",
                event_id="evt-heartbeat-send-complete",
                payload={
                    "original_message_id": "ws-heartbeat-send-complete",
                    "original_trace_id": "trace-heartbeat-send-complete",
                    "original_type": "heartbeat",
                    "sample_message_type": "heartbeat",
                    "original_event_id": "evt-heartbeat-send-complete",
                    "original_sequence": 10,
                    "gateway_ws_send_started_at_utc": "2026-06-08T00:00:00.010+00:00",
                    "gateway_ws_send_completed_at_utc": "2026-06-08T00:00:00.030+00:00",
                    "gateway_ws_send_duration_ms": 20.0,
                    "ws_session_id": session_id,
                },
                metadata={"transport_mode": "websocket_real_pilot", "ws_session_id": session_id},
                sequence=11,
            ).to_dict()
        )
        diagnostic_ack = _recv_until(ws, "event_ack")

    assert diagnostic_ack["payload"]["accepted"] is True
    assert diagnostic_ack["payload"]["queued"] is True
    sample = _wait_for_latency_sample_stage(
        db_path,
        "evt-heartbeat-send-complete",
        "gateway_ws_send_start_to_send_complete_ms",
    )
    assert sample is not None
    assert sample["stage_ms"]["gateway_ws_send_start_to_send_complete_ms"] == 20.0
    assert "gateway_ws_send_complete_to_core_receive_ms" in sample["stage_ms"]
    assert sample["metadata"]["gateway_ws_send_completed_at_utc"] == "2026-06-08T00:00:00.030+00:00"


def test_gateway_transport_ws_event_ack_does_not_wait_for_event_processing(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "1")

    import trading_app.api as api

    api = importlib.reload(api)

    async def slow_process_gateway_event(event):
        await asyncio.sleep(0.4)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "type": event.type,
            "transport": {"core_receive_ms": 0.0, "core_persist_ms": 0.0, "runtime_forward_ms": 0.0},
        }

    monkeypatch.setattr(api, "_process_gateway_event", slow_process_gateway_event)
    with TestClient(api.app) as client:
        with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
            ws.receive_json()
            started = time.perf_counter()
            ws.send_json(
                GatewayWsMessage(
                    type="heartbeat",
                    event_id="evt-slow-heartbeat",
                    payload={"transport_mode": "websocket_real_pilot"},
                    metadata={"transport_mode": "websocket_real_pilot"},
                    sequence=1,
                ).to_dict()
            )
            ack = _recv_until(ws, "event_ack")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            assert ack["payload"]["accepted"] is True
            assert ack["payload"]["queued"] is True
            assert elapsed_ms < 250

            ws.send_json(GatewayWsMessage(type="ping", sequence=2).to_dict())
            assert _recv_until(ws, "pong")["type"] == "pong"


def test_gateway_transport_ws_core_event_worker_coalesces_price_ticks_by_code(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "1")

    import trading_app.api as api

    api = importlib.reload(api)
    processed: list[GatewayEvent] = []

    async def slow_process_gateway_event(event):
        processed.append(event)
        if event.type == "market_status":
            await asyncio.sleep(0.3)
        return {
            "accepted": True,
            "event_id": event.event_id,
            "type": event.type,
            "transport": {"core_receive_ms": 0.0, "core_persist_ms": 0.0, "runtime_forward_ms": 0.0},
        }

    def event_message(event: GatewayEvent, sequence: int) -> dict:
        return GatewayWsMessage(
            type="gateway_event",
            source="kiwoom_gateway",
            payload={"event": event.to_dict()},
            metadata={"transport_mode": "websocket_real_pilot"},
            sequence=sequence,
        ).to_dict()

    monkeypatch.setattr(api, "_process_gateway_event", slow_process_gateway_event)
    with TestClient(api.app) as client:
        with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
            ws.receive_json()
            ws.send_json(event_message(GatewayEvent(type="market_status", event_id="evt-slow-data"), 1))
            ack = _recv_until(ws, "event_ack")
            assert ack["payload"]["queued"] is True

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not processed:
                time.sleep(0.01)
            assert processed and processed[0].type == "market_status"

            ticks = [
                GatewayEvent(type="price_tick", event_id="evt-price-coalesce-1", payload={"code": "005930", "price": 70000}),
                GatewayEvent(type="price_tick", event_id="evt-price-coalesce-2", payload={"code": "005930", "price": 70100}),
                GatewayEvent(type="price_tick", event_id="evt-price-coalesce-final", payload={"code": "005930", "price": 70200}),
            ]
            for index, event in enumerate(ticks, start=2):
                ws.send_json(event_message(event, index))
                price_ack = _recv_until(ws, "event_ack")
                assert price_ack["payload"]["queued"] is True

            status = _wait_for_pilot_status(client, "core_ws_price_tick_processed_count", 1)

    price_events = [event for event in processed if event.type == "price_tick"]
    assert len(price_events) == 1
    assert price_events[0].event_id == "evt-price-coalesce-final"
    assert price_events[0].payload["price"] == 70200
    assert status["core_ws_price_tick_received_count"] >= 3
    assert status["core_ws_price_tick_queued_count"] >= 1
    assert status["core_ws_price_tick_coalesced_count"] >= 2
    assert status["core_ws_price_tick_pending_key_count"] == 0
    assert status["core_ws_event_queue_size"] == 0


def test_gateway_transport_ws_condition_event_batch_persists_individual_samples(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    events = [
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-batch-1",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
            },
        ),
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-batch-2",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "000660",
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.010+00:00"},
            },
        ),
    ]

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="condition_event_batch",
                source="kiwoom_gateway",
                payload={
                    "batch_id": "batch-cond-1",
                    "events": [event.to_dict() for event in events],
                    "count": len(events),
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        ack = _recv_until(ws, "event_ack")
        sample_1 = _wait_for_latency_samples(db_path, "evt-cond-batch-1")
        sample_2 = _wait_for_latency_samples(db_path, "evt-cond-batch-2")

    assert ack["payload"]["type"] == "condition_event_batch"
    assert ack["payload"]["count"] == 2
    assert ack["payload"]["accepted_count"] == 2
    assert ack["payload"]["queued"] is True
    assert ack["payload"]["queued_count"] == 2
    assert "queue_batch_count" in ack["payload"]
    assert sample_1 and sample_1[0]["message_type"] == "condition_event"
    assert sample_2 and sample_2[0]["message_type"] == "condition_event"
    assert sample_1[0]["transport_mode"] == "websocket_real_pilot"
    status = client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]
    assert status["core_condition_event_async_enabled"] is True
    assert status["core_condition_event_queued_count"] >= 2
    assert status["core_condition_event_processed_count"] >= 2
    assert status["core_condition_event_failed_count"] == 0
    assert status["core_condition_event_queue_size"] == 0
    assert status["core_condition_event_queue_batch_count"] == 0
    assert status["core_condition_event_worker_count"] == 4
    assert status["core_condition_event_batch_chunk_size"] == 64
    assert status["core_condition_event_queue_sizes_by_worker"] == [0, 0, 0, 0]
    assert status["core_condition_event_queue_batch_counts_by_worker"] == [0, 0, 0, 0]
    assert status["core_condition_event_last_drained_batch_count"] >= 1
    assert 1 <= status["core_condition_event_last_batch_size"] <= 2


def test_gateway_transport_ws_condition_event_chunks_preserve_worker_order(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_CORE_WS_CONDITION_EVENT_BATCH_CHUNK_SIZE", "2")

    import trading_app.api as api

    api = importlib.reload(api)
    events = [
        GatewayEvent(
            type="condition_event",
            event_id=f"evt-cond-chunk-{index}",
            payload={"condition_name": "mock", "condition_index": 1, "code": f"00000{index}", "event_type": "include"},
        )
        for index in range(5)
    ]

    chunks = api._chunk_condition_event_worker_batches({0: events})

    assert [[event.event_id for event in chunk] for chunk in chunks[0]] == [
        ["evt-cond-chunk-0", "evt-cond-chunk-1"],
        ["evt-cond-chunk-2", "evt-cond-chunk-3"],
        ["evt-cond-chunk-4"],
    ]


def test_gateway_transport_ws_condition_event_batch_coalesces_duplicate_keys(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    events = [
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-coalesce-1",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
            },
        ),
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-coalesce-2",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.010+00:00"},
            },
        ),
        GatewayEvent(
            type="condition_event",
            event_id="evt-cond-coalesce-final",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "remove",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.020+00:00"},
            },
        ),
    ]

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="condition_event_batch",
                source="kiwoom_gateway",
                payload={
                    "batch_id": "batch-cond-coalesce",
                    "events": [event.to_dict() for event in events],
                    "count": len(events),
                },
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        ack = _recv_until(ws, "event_ack")
        final_sample = _wait_for_latency_samples(db_path, "evt-cond-coalesce-final")

    assert ack["payload"]["type"] == "condition_event_batch"
    assert ack["payload"]["count"] == 3
    assert ack["payload"]["accepted_count"] == 1
    assert ack["payload"]["queued_count"] == 1
    assert ack["payload"]["coalesced_count"] == 2
    assert final_sample and final_sample[0]["message_type"] == "condition_event"
    assert _wait_for_latency_samples(db_path, "evt-cond-coalesce-1", timeout_sec=0.2) == []
    assert _wait_for_latency_samples(db_path, "evt-cond-coalesce-2", timeout_sec=0.2) == []
    status = _wait_for_pilot_status(client, "core_condition_event_processed_count", 1)
    assert status["core_condition_event_received_count"] >= 3
    assert status["core_condition_event_queued_count"] >= 1
    assert status["core_condition_event_coalesced_count"] >= 2
    assert status["core_condition_event_last_received_count"] == 3
    assert status["core_condition_event_last_queued_count"] == 1
    assert status["core_condition_event_last_coalesced_count"] == 2


def test_gateway_transport_ws_stale_condition_include_is_rejected_before_candidate_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CORE_WS_CONDITION_EVENT_STALE_INCLUDE_SKIP_MS", "0.000001")
    client, db_path = _client(tmp_path, monkeypatch)
    event = GatewayEvent(
        type="condition_event",
        event_id="evt-cond-stale-include",
        payload={
            "condition_name": "mock",
            "condition_index": 1,
            "code": "005930",
            "event_type": "include",
            "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
        },
    )

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="condition_event_batch",
                source="kiwoom_gateway",
                payload={"batch_id": "batch-cond-stale-include", "events": [event.to_dict()], "count": 1},
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        ack = _recv_until(ws, "event_ack")
        sample = _wait_for_latency_sample_stage(db_path, "evt-cond-stale-include", "core_condition_event_stale_include_skip_ms")

    assert ack["payload"]["queued"] is True
    assert sample is not None
    assert sample["success"] is False
    assert sample["error"] == "STALE_CONDITION_INCLUDE_QUEUE_WAIT"
    assert sample["stage_ms"]["core_condition_event_stale_include_skip_ms"] > 0
    status = _wait_for_pilot_status(client, "core_condition_event_stale_queue_wait_skipped_count", 1)
    assert status["core_condition_event_stale_queue_wait_skipped_count"] >= 1
    assert status["core_condition_event_last_stale_queue_wait_ms"] > 0

    db = TradingDatabase(str(db_path))
    try:
        candidate_count = db.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        rows = db.conn.execute("SELECT event_type, reason, payload_json FROM candidate_events").fetchall()
    finally:
        db.close()
    assert candidate_count == 0
    assert len(rows) == 1
    assert rows[0]["event_type"] == "candidate_rejected"
    assert rows[0]["reason"] == "stale condition include queue wait"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["reject_reason"] == "stale condition include queue wait"


def test_gateway_transport_ws_stale_condition_guard_still_processes_remove_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CORE_WS_CONDITION_EVENT_STALE_INCLUDE_SKIP_MS", "0.000001")
    client, db_path = _client(tmp_path, monkeypatch)
    event = GatewayEvent(
        type="condition_event",
        event_id="evt-cond-stale-remove",
        payload={
            "condition_name": "mock",
            "condition_index": 1,
            "code": "005930",
            "event_type": "remove",
            "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
        },
    )

    with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
        ws.receive_json()
        ws.send_json(
            GatewayWsMessage(
                type="condition_event_batch",
                source="kiwoom_gateway",
                payload={"batch_id": "batch-cond-stale-remove", "events": [event.to_dict()], "count": 1},
                metadata={"transport_mode": "websocket_real_pilot"},
                sequence=1,
            ).to_dict()
        )
        ack = _recv_until(ws, "event_ack")
        sample = _wait_for_latency_sample_stage(db_path, "evt-cond-stale-remove", "core_condition_event_queue_wait_ms")

    assert ack["payload"]["queued"] is True
    assert sample is not None
    assert sample["success"] is True
    status = client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]
    assert status["core_condition_event_stale_queue_wait_skipped_count"] == 0
    assert status["core_condition_event_processed_count"] >= 1


def test_core_ws_event_worker_prioritizes_control_events_over_price_ticks(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_PRIORITY_ENABLED", "1")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED", "0")

    import trading_app.api as api

    api = importlib.reload(api)
    processed: list[str] = []

    async def fake_process_core_ws_event_work_item(item, *, queue_wait_ms: float):
        processed.append(api._core_ws_event_work_message_type(item.kind, event=item.event, message=item.message))
        return {"accepted": True}

    monkeypatch.setattr(api, "_process_core_ws_event_work_item", fake_process_core_ws_event_work_item)

    async def run_priority_check() -> None:
        queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=10)
        api._core_ws_event_queue = queue
        api._core_ws_event_worker_loop = asyncio.get_running_loop()
        api._core_ws_event_worker_task = asyncio.create_task(api._core_ws_event_worker_loop_main())
        queued_at = api.utc_now_ms()
        queued_monotonic = api.monotonic_ms()
        price_event = GatewayEvent(
            type="price_tick",
            event_id="evt-priority-price",
            payload={"code": "005930", "price": 70000},
        )
        ack_event = GatewayEvent(
            type="command_ack",
            event_id="evt-priority-ack",
            command_id="cmd-priority",
            payload={"command_id": "cmd-priority", "status": "ACKED"},
        )
        price_item = api._build_core_ws_event_work_item(
            kind="gateway_event",
            metadata={},
            queue_size=1,
            queued_at=queued_at,
            queued_monotonic=queued_monotonic,
            event=price_event,
        )
        ack_item = api._build_core_ws_event_work_item(
            kind="gateway_event",
            metadata={},
            queue_size=2,
            queued_at=queued_at,
            queued_monotonic=queued_monotonic,
            event=ack_event,
        )
        queue.put_nowait(api._core_ws_event_queue_item(price_item))
        queue.put_nowait(api._core_ws_event_queue_item(ack_item))
        await asyncio.wait_for(queue.join(), timeout=2.0)
        api._core_ws_event_worker_task.cancel()
        await asyncio.gather(api._core_ws_event_worker_task, return_exceptions=True)
        api._core_ws_event_worker_task = None
        api._core_ws_event_queue = None

    asyncio.run(run_priority_check())

    assert processed[:2] == ["command_ack", "price_tick"]


def test_core_ws_event_workers_split_control_from_data_worker(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_PRIORITY_ENABLED", "1")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED", "1")

    import trading_app.api as api

    api = importlib.reload(api)
    processed: list[str] = []

    async def fake_process_core_ws_event_work_item(item, *, queue_wait_ms: float):
        message_type = api._core_ws_event_work_message_type(item.kind, event=item.event, message=item.message)
        processed.append(f"start:{message_type}")
        if message_type == "price_tick":
            await asyncio.sleep(0.1)
        processed.append(f"end:{message_type}")
        return {"accepted": True}

    monkeypatch.setattr(api, "_process_core_ws_event_work_item", fake_process_core_ws_event_work_item)

    async def wait_for(predicate) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition was not met")

    async def run_split_check() -> None:
        control_queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=10)
        data_queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=10)
        api._core_ws_event_queue = None
        api._core_ws_event_control_queue = control_queue
        api._core_ws_event_data_queue = data_queue
        api._core_ws_event_worker_loop = asyncio.get_running_loop()
        control_task = asyncio.create_task(api._core_ws_event_worker_loop_main("control"))
        data_task = asyncio.create_task(api._core_ws_event_worker_loop_main("data"))
        api._core_ws_event_worker_task = control_task
        api._core_ws_event_control_worker_task = control_task
        api._core_ws_event_data_worker_task = data_task
        try:
            queued_at = api.utc_now_ms()
            queued_monotonic = api.monotonic_ms()
            price_event = GatewayEvent(
                type="price_tick",
                event_id="evt-split-price",
                payload={"code": "005930", "price": 70000},
            )
            ack_event = GatewayEvent(
                type="command_ack",
                event_id="evt-split-ack",
                command_id="cmd-split",
                payload={"command_id": "cmd-split", "status": "ACKED"},
            )
            price_item = api._build_core_ws_event_work_item(
                kind="gateway_event",
                metadata={},
                queue_size=1,
                queued_at=queued_at,
                queued_monotonic=queued_monotonic,
                event=price_event,
            )
            ack_item = api._build_core_ws_event_work_item(
                kind="gateway_event",
                metadata={},
                queue_size=1,
                queued_at=queued_at,
                queued_monotonic=queued_monotonic,
                event=ack_event,
            )
            data_queue.put_nowait(api._core_ws_event_queue_item(price_item))
            await wait_for(lambda: "start:price_tick" in processed)
            control_queue.put_nowait(api._core_ws_event_queue_item(ack_item))
            await asyncio.wait_for(asyncio.gather(control_queue.join(), data_queue.join()), timeout=2.0)
        finally:
            control_task.cancel()
            data_task.cancel()
            await asyncio.gather(control_task, data_task, return_exceptions=True)
            api._core_ws_event_worker_task = None
            api._core_ws_event_control_worker_task = None
            api._core_ws_event_data_worker_task = None
            api._core_ws_event_control_queue = None
            api._core_ws_event_data_queue = None

    asyncio.run(run_split_check())

    assert processed.index("start:command_ack") < processed.index("end:price_tick")
    assert processed.index("end:command_ack") < processed.index("end:price_tick")


def test_core_ws_event_control_workers_shard_by_command_id_and_preserve_order(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_PRIORITY_ENABLED", "1")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED", "1")
    monkeypatch.setenv("TRADING_CORE_WS_EVENT_CONTROL_WORKERS", "2")

    import trading_app.api as api

    api = importlib.reload(api)
    processed: list[str] = []
    cmd_a = "cmd-shard-a"

    def command_event(event_type: str, command_id: str) -> GatewayEvent:
        return GatewayEvent(
            type=event_type,
            event_id=f"evt-{event_type}-{command_id}",
            command_id=command_id,
            payload={"command_id": command_id, "status": "ACKED"},
        )

    index_a = api._core_ws_event_control_worker_index("gateway_event", event=command_event("command_started", cmd_a), worker_count=2)
    cmd_b = ""
    index_b = index_a
    for value in range(100):
        candidate = f"cmd-shard-b-{value}"
        index_b = api._core_ws_event_control_worker_index(
            "gateway_event",
            event=command_event("command_started", candidate),
            worker_count=2,
        )
        if index_b != index_a:
            cmd_b = candidate
            break
    assert cmd_b
    assert index_b != index_a

    async def fake_process_core_ws_event_work_item(item, *, queue_wait_ms: float):
        event = item.event
        assert event is not None
        command_id = str(event.payload.get("command_id") or event.command_id or "")
        marker = f"{event.type}:{command_id}"
        processed.append(f"start:{marker}")
        if command_id == cmd_a and event.type == "command_started":
            await asyncio.sleep(0.1)
        processed.append(f"end:{marker}")
        return {"accepted": True}

    monkeypatch.setattr(api, "_process_core_ws_event_work_item", fake_process_core_ws_event_work_item)

    async def wait_for(predicate) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition was not met")

    async def enqueue_control_event(event: GatewayEvent) -> None:
        queued_at = api.utc_now_ms()
        queued_monotonic = api.monotonic_ms()
        priority = api._core_ws_event_priority("gateway_event", event=event)
        queue_name = api._core_ws_event_target_queue_name("gateway_event", priority=priority, event=event)
        queue = api._core_ws_event_queue_for_name(queue_name)
        assert queue is not None
        item = api._build_core_ws_event_work_item(
            kind="gateway_event",
            metadata={},
            queue_size=queue.qsize() + 1,
            queued_at=queued_at,
            queued_monotonic=queued_monotonic,
            event=event,
        )
        queue.put_nowait(api._core_ws_event_queue_item(item))

    async def run_shard_check() -> None:
        queues = [asyncio.PriorityQueue(maxsize=10), asyncio.PriorityQueue(maxsize=10)]
        api._core_ws_event_queue = None
        api._core_ws_event_control_queues = queues
        api._core_ws_event_control_queue = queues[0]
        api._core_ws_event_data_queue = asyncio.PriorityQueue(maxsize=10)
        api._core_ws_event_worker_loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(api._core_ws_event_worker_loop_main("control", index)) for index in range(2)]
        api._core_ws_event_control_worker_tasks = tasks
        api._core_ws_event_control_worker_task = tasks[0]
        api._core_ws_event_worker_task = tasks[0]
        try:
            await enqueue_control_event(command_event("command_started", cmd_a))
            await wait_for(lambda: f"start:command_started:{cmd_a}" in processed)
            await enqueue_control_event(command_event("command_ack", cmd_a))
            await enqueue_control_event(command_event("command_started", cmd_b))
            await asyncio.wait_for(asyncio.gather(*(queue.join() for queue in queues)), timeout=2.0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            api._core_ws_event_worker_task = None
            api._core_ws_event_control_worker_task = None
            api._core_ws_event_control_worker_tasks = []
            api._core_ws_event_control_queue = None
            api._core_ws_event_control_queues = []
            api._core_ws_event_data_queue = None

    asyncio.run(run_shard_check())

    assert processed.index(f"end:command_started:{cmd_b}") < processed.index(f"end:command_started:{cmd_a}")
    assert processed.index(f"start:command_ack:{cmd_a}") > processed.index(f"end:command_started:{cmd_a}")
    assert processed.index(f"end:command_ack:{cmd_a}") > processed.index(f"end:command_started:{cmd_a}")


def test_gateway_transport_ws_condition_event_workers_process_shards_in_parallel(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_CORE_WS_CONDITION_EVENT_WORKERS", "2")

    import trading_app.api as api

    api = importlib.reload(api)
    code_by_worker: dict[int, str] = {}
    for value in range(1, 1000):
        code = f"{value:06d}"
        event = GatewayEvent(
            type="condition_event",
            event_id=f"evt-cond-shard-{code}",
            payload={"condition_name": "mock", "condition_index": 1, "code": code, "event_type": "include"},
        )
        code_by_worker.setdefault(api._gateway_condition_event_worker_index(event, 2), code)
        if len(code_by_worker) == 2:
            break
    assert len(code_by_worker) == 2

    processed_batches: list[list[str]] = []

    def slow_process_condition_event_batch(events):
        processed_batches.append([str(event.payload.get("code") or "") for event in events])
        time.sleep(0.4)
        return {
            "processed_count": len(events),
            "accepted_count": len(events),
            "failed_count": 0,
            "stale_skipped_count": 0,
            "results": [{"accepted": True, "event_id": event.event_id, "type": event.type} for event in events],
        }

    monkeypatch.setattr(api, "_process_condition_event_batch_in_worker", slow_process_condition_event_batch)

    def condition_batch_message(code: str, sequence: int) -> dict:
        event = GatewayEvent(
            type="condition_event",
            event_id=f"evt-cond-parallel-{code}",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": code,
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
            },
        )
        return GatewayWsMessage(
            type="condition_event_batch",
            source="kiwoom_gateway",
            payload={"batch_id": f"batch-cond-parallel-{code}", "events": [event.to_dict()], "count": 1},
            metadata={"transport_mode": "websocket_real_pilot"},
            sequence=sequence,
        ).to_dict()

    with TestClient(api.app) as client:
        with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
            ws.receive_json()
            for sequence, code in enumerate(code_by_worker.values(), start=1):
                ws.send_json(condition_batch_message(code, sequence))
                ack = _recv_until(ws, "event_ack")
                assert ack["payload"]["queued"] is True

            active = _wait_for_pilot_status(client, "core_condition_event_active_worker_count", 2, timeout_sec=1.0)
            assert active["core_condition_event_worker_count"] == 2
            assert active["core_condition_event_active_worker_count"] >= 2
            status = _wait_for_pilot_status(client, "core_condition_event_processed_count", 2, timeout_sec=2.0)

    assert status["core_condition_event_processed_count"] >= 2
    assert status["core_condition_event_failed_count"] == 0
    assert status["core_condition_event_queue_size"] == 0
    assert len(processed_batches) == 2


def test_gateway_transport_ws_condition_event_worker_survives_ws_disconnect_with_lifespan(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE", "1")
    monkeypatch.setenv("TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE", "1")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        event = GatewayEvent(
            type="condition_event",
            event_id="evt-cond-lifespan",
            payload={
                "condition_name": "mock",
                "condition_index": 1,
                "code": "005930",
                "event_type": "include",
                "transport_trace": {"gateway_event_created_at_utc": "2026-06-08T00:00:00.000+00:00"},
            },
        )
        with client.websocket_connect("/ws/gateway/transport?token=test-token") as ws:
            ws.receive_json()
            ws.send_json(
                GatewayWsMessage(
                    type="condition_event_batch",
                    source="kiwoom_gateway",
                    payload={"batch_id": "batch-cond-lifespan", "events": [event.to_dict()], "count": 1},
                    metadata={"transport_mode": "websocket_real_pilot"},
                    sequence=1,
                ).to_dict()
            )
            ack = _recv_until(ws, "event_ack")
        assert ack["payload"]["queued"] is True
        samples = _wait_for_latency_samples(db_path, "evt-cond-lifespan")
        assert samples and samples[0]["message_type"] == "condition_event"
        status = client.get("/api/gateway/transport/status").json()["real_gateway_websocket_pilot"]
        assert status["core_condition_event_processed_count"] >= 1


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
        assert _wait_for_command_status(client, "failed_count", 1)["failed_count"] == 1



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
