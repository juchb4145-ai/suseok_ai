from __future__ import annotations

import importlib
import json
import time

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading_app.replay_tick_buffer import ReplayGradeTickBuffer, ReplayTickWriterConfig
from trading_app.strategy_replay import StrategyReplayBundleExporter


def test_replay_tick_buffer_persists_batch_and_redacts_sensitive_payload(tmp_path):
    db_path = tmp_path / "ticks.sqlite3"
    buffer = ReplayGradeTickBuffer(
        db_path,
        ReplayTickWriterConfig(
            enabled=True,
            queue_max_size=10,
            batch_size=10,
            flush_interval_sec=0.05,
            min_interval_ms=0,
        ),
    )
    buffer.start()
    try:
        assert buffer.enqueue_event(
            GatewayEvent(
                type="price_tick",
                event_id="evt-tick-1",
                timestamp="2026-06-01T00:00:01+00:00",
                source="kiwoom_gateway",
                payload={
                    "code": "A005930",
                    "price": 70000,
                    "change_rate": 1.2,
                    "cum_volume": 1200,
                    "trade_value": 84_000_000,
                    "execution_strength": 123.4,
                    "best_bid": 69900,
                    "best_ask": 70000,
                    "spread_ticks": 1,
                    "trade_time": "090001",
                    "account": "1234567890",
                    "metadata": {"api_token": "secret-token", "raw_fids_present": [10, 14, 228]},
                },
            )
        )
    finally:
        buffer.stop()

    db = TradingDatabase(str(db_path))
    try:
        rows = db.conn.execute("SELECT * FROM gateway_price_ticks").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["trade_date"] == "2026-06-01"
        assert row["timestamp"] == "2026-06-01T09:00:01"
        assert row["code"] == "005930"
        assert row["price"] == 70000
        raw = json.loads(row["raw_payload_json"])
        metadata = json.loads(row["metadata_json"])
        assert raw["account"] == "***REDACTED***"
        assert metadata["api_token"] == "***REDACTED***"
        assert metadata["raw_fids_present"] == [10, 14, 228]
    finally:
        db.close()

    snapshot = buffer.snapshot()
    assert snapshot["queued_count"] == 1
    assert snapshot["persisted_count"] == 1
    assert snapshot["dropped_count"] == 0


def test_replay_tick_buffer_throttles_by_code_without_blocking(tmp_path):
    db_path = tmp_path / "ticks.sqlite3"
    buffer = ReplayGradeTickBuffer(
        db_path,
        ReplayTickWriterConfig(
            enabled=True,
            queue_max_size=10,
            batch_size=10,
            flush_interval_sec=0.05,
            min_interval_ms=60_000,
        ),
    )
    buffer.start()
    try:
        first = GatewayEvent(type="price_tick", event_id="evt-1", payload={"code": "005930", "price": 70000})
        second = GatewayEvent(type="price_tick", event_id="evt-2", payload={"code": "005930", "price": 70100})
        assert buffer.enqueue_event(first) is True
        assert buffer.enqueue_event(second) is False
    finally:
        buffer.stop()

    db = TradingDatabase(str(db_path))
    try:
        assert db.conn.execute("SELECT COUNT(*) AS count FROM gateway_price_ticks").fetchone()["count"] == 1
    finally:
        db.close()
    assert buffer.snapshot()["throttled_count"] == 1


def test_replay_bundle_export_prefers_gateway_price_ticks(tmp_path):
    db_path = tmp_path / "source.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_gateway_price_ticks_batch(
            [
                {
                    "event_id": "evt-export-1",
                    "timestamp": "2026-06-01T09:01:00",
                    "received_at": "2026-06-01T00:01:00+00:00",
                    "code": "005930",
                    "price": 70000,
                    "change_rate": 1.0,
                    "cum_volume": 100,
                    "trade_value": 7_000_000,
                    "execution_strength": 110,
                    "best_bid": 69900,
                    "best_ask": 70000,
                    "spread_ticks": 1,
                    "source": "kiwoom_gateway",
                    "raw_payload": {"code": "005930", "price": 70000},
                }
            ]
        )
        db.save_strategy_decision_events(
            [
                {
                    "decision_id": "decision-export-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:01:00",
                    "code": "005930",
                    "gate_status": "READY",
                    "action_type": "READY",
                    "action_result": "ACCEPTED",
                    "price": 69900,
                }
            ]
        )
    finally:
        db.close()

    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")

    assert bundle.manifest.data_quality["tick_source"] == "gateway_price_ticks"
    assert bundle.manifest.data_quality["tick_count"] == 1
    assert "TICK_HISTORY_RECONSTRUCTED_FROM_DECISIONS" not in bundle.manifest.warnings
    ticks = (bundle.path / "ticks.csv").read_text(encoding="utf-8")
    assert "gateway_price_ticks" in ticks
    assert "70000" in ticks


def test_gateway_event_api_queues_replay_tick_history(tmp_path, monkeypatch):
    db_path = tmp_path / "api.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_REPLAY_TICK_HISTORY_ENABLED", "1")
    monkeypatch.setenv("TRADING_REPLAY_TICK_HISTORY_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("TRADING_REPLAY_TICK_HISTORY_FLUSH_INTERVAL_SEC", "0.05")

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        status = client.get("/api/runtime/status").json()
        assert status["replay_tick_history"]["enabled"] is True
        response = client.post(
            "/api/gateway/events",
            headers={"X-Local-Token": "test-token"},
            json={
                "type": "price_tick",
                "event_id": "evt-api-tick",
                "timestamp": "2026-06-01T00:02:00+00:00",
                "source": "kiwoom_gateway",
                "payload": {"code": "005930", "price": 70100, "trade_time": "090200"},
            },
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is True

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            db = TradingDatabase(str(db_path))
            try:
                row = db.conn.execute("SELECT * FROM gateway_price_ticks WHERE event_id = ?", ("evt-api-tick",)).fetchone()
            finally:
                db.close()
            if row is not None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("gateway_price_ticks row was not persisted")

    db = TradingDatabase(str(db_path))
    try:
        row = db.conn.execute("SELECT * FROM gateway_price_ticks WHERE event_id = ?", ("evt-api-tick",)).fetchone()
        assert row is not None
        assert row["timestamp"] == "2026-06-01T09:02:00"
        assert row["price"] == 70100
    finally:
        db.close()
