from __future__ import annotations

import importlib
import json
from datetime import datetime

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.strategy_replay import (
    ReplayClock,
    ReplayGatewayEventFeeder,
    StrategyReplayBundle,
    StrategyReplayBundleExporter,
    StrategyReplayManifest,
    StrategyRuntimeReplayRunner,
)


def _decision(decision_id: str, *, code: str = "000001", gate_status: str = "READY", action_type: str = "READY", reason_codes=None) -> dict:
    return {
        "decision_id": decision_id,
        "runtime_cycle_id": "cycle-1",
        "trade_date": "2026-06-01",
        "decision_at": "2026-06-01T09:00:00",
        "candidate_id": 1,
        "candidate_instance_id": f"ci-{decision_id}",
        "candidate_generation_seq": 1,
        "code": code,
        "name": "Alpha",
        "theme_name": "Robot",
        "strategy_name": "baseline",
        "strategy_version": "test",
        "gate_status": gate_status,
        "action_type": action_type,
        "action_result": "ACCEPTED",
        "reason_codes": list(reason_codes or ["PASS"]),
        "price": 100.0,
        "change_rate": 3.0,
        "trade_value": 1_000_000.0,
        "execution_strength": 120.0,
        "theme_score": 82.0,
        "hybrid_score": 75.0,
        "gate_score": 74.0,
        "details": {"gate_details": {"stock_role": "LEADER", "market_support": True}},
    }


def _source_db(tmp_path, decisions=None):
    db_path = tmp_path / "source.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        if decisions:
            db.save_strategy_decision_events(decisions)
    finally:
        db.close()
    return db_path


def test_bundle_manifest_and_partial_warnings(tmp_path):
    db_path = _source_db(tmp_path)

    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")
    manifest = json.loads((bundle.path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "PARTIAL_BUNDLE"
    assert manifest["trade_date"] == "2026-06-01"
    assert manifest["data_files"]["ticks"] == "ticks.csv"
    assert "MISSING_TICK_HISTORY" in manifest["warnings"]
    assert "MISSING_CANDIDATE_EVENTS" in manifest["warnings"]


def test_bundle_export_reconstructs_from_decision_events(tmp_path):
    db_path = _source_db(
        tmp_path,
        [
            _decision("ready"),
            _decision("wait", code="000002", gate_status="WAIT", action_type="WAIT", reason_codes=["DATA_INSUFFICIENT"]),
        ],
    )

    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")

    assert bundle.manifest.data_quality["tick_count"] == 2
    assert bundle.manifest.data_quality["candidate_event_count"] == 2
    assert bundle.manifest.data_quality["decision_event_count"] == 2
    assert "TICK_HISTORY_RECONSTRUCTED_FROM_DECISIONS" in bundle.manifest.warnings
    assert (bundle.path / "decision_events.jsonl").read_text(encoding="utf-8").count("\n") == 2


def test_data_only_replay_processes_ticks_and_is_deterministic(tmp_path):
    db_path = _source_db(tmp_path, [_decision("ready")])
    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")
    runner = StrategyRuntimeReplayRunner(
        source_db_path=db_path,
        replay_db_root=tmp_path / "replay",
        bundle_root=tmp_path / "bundles",
    )

    first = runner.run(bundle_path=bundle.path, mode="data_only", replay_db=tmp_path / "replay" / "first.sqlite3")
    second = runner.run(bundle_path=bundle.path, mode="data_only", replay_db=tmp_path / "replay" / "second.sqlite3")

    assert first.status == "OK"
    assert first.summary["processed_tick_count"] == 1
    assert first.summary["generated_candle_count"] == second.summary["generated_candle_count"]
    assert first.summary["processed_tick_count"] == second.summary["processed_tick_count"]


def test_decision_led_replay_uses_separate_db_and_builds_report(tmp_path):
    db_path = _source_db(
        tmp_path,
        [
            _decision("risk-off", gate_status="WAIT", action_type="WAIT", reason_codes=["RISK_OFF"]),
            _decision("late", code="000002", gate_status="READY", action_type="READY", reason_codes=["LATE_CHASE"]),
        ],
    )
    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")
    replay_db = tmp_path / "replay" / "decision.sqlite3"

    result = StrategyRuntimeReplayRunner(source_db_path=db_path, replay_db_root=tmp_path / "replay").run(
        bundle_path=bundle.path,
        mode="decision_led",
        replay_db=replay_db,
    )

    assert result.status == "OK"
    assert result.summary["outcome_labeled_count"] >= 1
    assert result.summary["shadow_evaluation_count"] >= 1
    assert result.report["recommendations"]
    source = TradingDatabase(str(db_path))
    replay = TradingDatabase(str(replay_db))
    try:
        assert source.conn.execute("SELECT COUNT(*) AS count FROM strategy_replay_runs").fetchone()["count"] == 0
        assert replay.conn.execute("SELECT COUNT(*) AS count FROM strategy_replay_runs").fetchone()["count"] == 1
        assert replay.conn.execute("SELECT COUNT(*) AS count FROM runtime_order_intents").fetchone()["count"] == 0
    finally:
        source.close()
        replay.close()


def test_full_runtime_replay_is_partial_and_has_no_live_side_effects(tmp_path):
    db_path = _source_db(tmp_path, [_decision("ready")])
    bundle = StrategyReplayBundleExporter(db_path, output_root=tmp_path / "bundles").export_bundle("2026-06-01")
    replay_db = tmp_path / "replay" / "full.sqlite3"

    result = StrategyRuntimeReplayRunner(source_db_path=db_path, replay_db_root=tmp_path / "replay").run(
        bundle_path=bundle.path,
        mode="full_runtime",
        replay_db=replay_db,
    )

    replay = TradingDatabase(str(replay_db))
    try:
        assert result.status == "PARTIAL_REPLAY"
        assert any("FULL_RUNTIME" in warning for warning in result.warnings)
        assert replay.conn.execute("SELECT COUNT(*) AS count FROM gateway_commands").fetchone()["count"] == 0
        assert replay.conn.execute("SELECT COUNT(*) AS count FROM runtime_order_intents").fetchone()["count"] == 0
        run = replay.get_strategy_replay_run(result.replay_id)
        assert run["metadata"]["live_order_enabled"] is False
        assert run["metadata"]["runtime_allow_live_orders"] is False
    finally:
        replay.close()


def test_replay_clock_feeder_does_not_expose_future_events(tmp_path):
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "ticks.csv").write_text(
        "timestamp,code,price,change_rate,cum_volume,trade_value,execution_strength,best_bid,best_ask,spread_ticks,source,row_type\n"
        "2026-06-01T09:00:00,000001,100,0,1,100,100,99,101,1,test,tick\n"
        "2026-06-01T09:01:00,000001,101,1,2,201,110,100,102,1,test,tick\n",
        encoding="utf-8",
    )
    for name in ("candidate_events.jsonl", "theme_snapshots.jsonl", "market_status.jsonl", "decision_events.jsonl"):
        (bundle_path / name).write_text("", encoding="utf-8")
    manifest = StrategyReplayManifest(
        replay_id="replay-test",
        trade_date="2026-06-01",
        source_db_path="source.sqlite3",
        created_at="2026-06-01T16:00:00",
        session_start="2026-06-01T09:00:00",
        session_end="2026-06-01T09:02:00",
        codes=["000001"],
        theme_names=[],
        runtime_config_hash="hash",
        strategy_version="test",
        data_files={
            "ticks": "ticks.csv",
            "candidate_events": "candidate_events.jsonl",
            "theme_snapshots": "theme_snapshots.jsonl",
            "market_status": "market_status.jsonl",
            "decision_events": "decision_events.jsonl",
        },
        data_quality={"status": "PARTIAL_BUNDLE"},
    )
    (bundle_path / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    feeder = ReplayGatewayEventFeeder(StrategyReplayBundle(bundle_path, manifest))
    clock = ReplayClock(datetime.fromisoformat("2026-06-01T09:00:00"))

    first = feeder.available_events(clock)
    assert [event["price"] for event in first] == ["100"]
    assert feeder.remaining_count() == 1
    clock.advance_to("2026-06-01T09:01:00")
    assert [event["price"] for event in feeder.available_events(clock)] == ["101"]


def test_replay_post_api_requires_local_token(tmp_path, monkeypatch):
    db_path = _source_db(tmp_path)
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "secret-token")
    import trading_app.api as api

    api = importlib.reload(api)
    api.DEFAULT_BUNDLE_ROOT = tmp_path / "bundles"
    api.DEFAULT_REPLAY_DB_ROOT = tmp_path / "replay"
    with TestClient(api.app) as client:
        response = client.post("/api/runtime/replay/bundles/export", params={"trade_date": "2026-06-01"})

    assert response.status_code == 401
