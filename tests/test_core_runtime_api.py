import asyncio
import importlib
import time

from fastapi.testclient import TestClient
from storage.db import TradingDatabase
from trading.broker.models import GatewayEvent
from trading.strategy.models import BlockType, Candidate, CandidateState
from tests.theme_naver_helpers import naver_source


def _client(tmp_path, monkeypatch, *, enabled="0", auto_start="0"):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_RUNTIME_ENABLED", enabled)
    monkeypatch.setenv("TRADING_RUNTIME_AUTO_START", auto_start)
    monkeypatch.setenv("TRADING_RUNTIME_EVALUATION_INTERVAL_SEC", "60")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app)


def test_runtime_disabled_status_and_start(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="0") as client:
        status = client.get("/api/runtime/status").json()
        started = client.post("/api/runtime/start", headers={"X-Local-Token": "test-token"}).json()

    assert status["enabled"] is False
    assert started["running"] is False


def test_start_kiwoom_gateway_skips_when_gateway_connected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="0") as client:
        client.post(
            "/api/gateway/events",
            json={"type": "heartbeat", "event_id": "evt-gateway-online", "payload": {"kiwoom_logged_in": True}},
            headers={"X-Local-Token": "test-token"},
        )
        response = client.post("/api/gateway/kiwoom/start", headers={"X-Local-Token": "test-token"}).json()

    assert response["started"] is False
    assert response["reason"] == "ALREADY_CONNECTED"
    assert response["gateway"]["connected"] is True


def test_start_kiwoom_gateway_launches_process_when_disconnected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="0") as client:
        import trading_app.api as api

        monkeypatch.setattr(api, "_find_kiwoom_gateway_processes", lambda: [])
        monkeypatch.setattr(
            api,
            "_start_kiwoom_gateway_process",
            lambda: {"pid": 1234, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"},
        )
        response = client.post("/api/gateway/kiwoom/start", headers={"X-Local-Token": "test-token"}).json()

    assert response["started"] is True
    assert response["reason"] == "STARTED"
    assert response["processes"][0]["pid"] == 1234


def test_start_kiwoom_gateway_starts_when_connected_state_is_stale(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="0") as client:
        import trading_app.api as api

        api.gateway_state.record_event(
            GatewayEvent(
                type="heartbeat",
                event_id="evt-gateway-stale",
                timestamp="2000-01-01T00:00:00+00:00",
                payload={"kiwoom_logged_in": True},
            )
        )
        monkeypatch.setattr(api, "_find_kiwoom_gateway_processes", lambda: [])
        monkeypatch.setattr(
            api,
            "_start_kiwoom_gateway_process",
            lambda: {"pid": 2345, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"},
        )
        response = client.post("/api/gateway/kiwoom/start", headers={"X-Local-Token": "test-token"}).json()

    assert response["started"] is True
    assert response["reason"] == "STARTED_STALE_STATE"
    assert response["gateway"]["stale_for_start"] is True
    assert response["stale_recovery"]["stale"] is True
    assert response["processes"][0]["pid"] == 2345


def test_start_kiwoom_gateway_restarts_stale_running_process(tmp_path, monkeypatch):
    stale_process = {"pid": 3456, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"}
    launched_process = {"pid": 4567, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"}

    with _client(tmp_path, monkeypatch, enabled="0") as client:
        import trading_app.api as api

        api.gateway_state.record_event(
            GatewayEvent(
                type="heartbeat",
                event_id="evt-gateway-stale-running",
                timestamp="2000-01-01T00:00:00+00:00",
                payload={"kiwoom_logged_in": True},
            )
        )
        process_checks = [[stale_process], []]
        monkeypatch.setattr(api, "_find_kiwoom_gateway_processes", lambda: process_checks.pop(0))
        monkeypatch.setattr(api, "_stop_kiwoom_gateway_processes", lambda processes: list(processes))
        monkeypatch.setattr(api, "_start_kiwoom_gateway_process", lambda: launched_process)
        response = client.post("/api/gateway/kiwoom/start", headers={"X-Local-Token": "test-token"}).json()

    assert response["started"] is True
    assert response["reason"] == "RESTARTED_STALE"
    assert response["stale_recovery"]["stopped_processes"][0]["pid"] == 3456
    assert response["processes"][0]["pid"] == 4567


def test_start_kiwoom_gateway_restarts_orphan_running_process_without_heartbeat(tmp_path, monkeypatch):
    orphan_process = {"pid": 5678, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"}
    launched_process = {"pid": 6789, "name": "python.exe", "command_line": "python apps/kiwoom_gateway.py"}

    with _client(tmp_path, monkeypatch, enabled="0") as client:
        import trading_app.api as api

        process_checks = [[orphan_process], []]
        monkeypatch.setattr(api, "_find_kiwoom_gateway_processes", lambda: process_checks.pop(0))
        monkeypatch.setattr(api, "_stop_kiwoom_gateway_processes", lambda processes: list(processes))
        monkeypatch.setattr(api, "_start_kiwoom_gateway_process", lambda: launched_process)
        response = client.post("/api/gateway/kiwoom/start", headers={"X-Local-Token": "test-token"}).json()

    assert response["started"] is True
    assert response["reason"] == "RESTARTED_ORPHAN"
    assert response["stale_recovery"]["orphan"] is True
    assert response["stale_recovery"]["stopped_processes"][0]["pid"] == 5678
    assert response["processes"][0]["pid"] == 6789


def test_kiwoom_gateway_defaults_to_single_base_python(monkeypatch):
    import trading_app.api as api

    monkeypatch.delenv("TRADING_KIWOOM_GATEWAY_PYTHON", raising=False)
    assert str(api._kiwoom_gateway_python_exe()).replace("\\", "/").endswith("Python39-32/python.exe")

    env = {}
    api._apply_kiwoom_gateway_runtime_env(env)
    assert "venv_32" in env.get("PYTHONPATH", "")
    assert "site-packages" in env.get("PYTHONPATH", "")
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" in env


def test_runtime_start_cycle_stop_api(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    with _client(tmp_path, monkeypatch, enabled="1") as client:
        started = client.post("/api/runtime/start", headers={"X-Local-Token": "test-token"}).json()
        cycled = client.post("/api/runtime/cycle", headers={"X-Local-Token": "test-token"}).json()
        commands = client.get(
            "/api/gateway/commands/history?include_finished=true&include_payload=true",
            headers={"X-Local-Token": "test-token"},
        ).json()
        snapshot = client.get("/api/runtime/snapshot").json()
        stopped = client.post("/api/runtime/stop", headers={"X-Local-Token": "test-token"}).json()

    assert started["running"] is True
    assert cycled["cycle_count"] == 1
    assert snapshot["started"] is True
    assert all(item["command_type"] != "send_order" for item in commands["items"])
    assert stopped["running"] is False
    db = TradingDatabase(str(db_path))
    try:
        cycles = db.latest_runtime_cycles(limit=5)
        logs = "\n".join(db.recent_logs(limit=20))
    finally:
        db.close()
    assert cycles[0]["status"] == "ok"
    assert "runtime" in logs


def test_runtime_snapshot_is_in_dashboard_and_websocket(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="1") as client:
        snapshot = client.get("/api/snapshot").json()
        with client.websocket_connect("/ws/dashboard") as websocket:
            ws_payload = websocket.receive_json()

    assert "runtime" in snapshot
    assert "runtime" in ws_payload["snapshot"]


def test_candidates_api_uses_dynamic_theme_score_alias(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code="000001",
                state=CandidateState.WATCHING,
                detected_at="2026-06-01T09:00:00",
                last_seen_at="2026-06-01T09:01:00",
                metadata={
                    "gate_results_by_theme": {
                        "theme-a": {
                            "theme_id": "theme-a",
                            "dynamic_theme_score": 72.5,
                            "membership_score": 0.88,
                            "hybrid_score": 64.2,
                            "score": 12.3,
                            "reason_codes": ["CHASE_RISK"],
                            "primary_reason_code": "CHASE_RISK_CAP",
                            "comparison_reason_codes": ["INPUT_MISSING"],
                            "secondary_reason_codes": ["INPUT_MISSING"],
                        }
                    }
                },
            )
        )
    finally:
        db.close()

    with _client(tmp_path, monkeypatch) as client:
        payload = client.get("/api/candidates?trade_date=2026-06-01&limit=1").json()

    item = payload["items"][0]
    assert item["theme_score"] == 72.5
    assert item["membership_score"] == 0.88
    assert item["hybrid_score"] == 64.2
    assert item["reason_codes"] == ["CHASE_RISK", "CHASE_RISK_CAP"]


def test_candidates_api_normalizes_quality_reason(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code="000002",
                state=CandidateState.WATCHING,
                detected_at="2026-06-01T09:00:00",
                last_seen_at="2026-06-01T09:01:00",
                metadata={
                    "quality_reason": "no_active_dynamic_theme",
                    "gate_results_by_theme": {
                        "theme-a": {
                            "theme_id": "theme-a",
                            "reason_codes": ["NO_ACTIVE_THEME"],
                        }
                    },
                },
            )
        )
    finally:
        db.close()

    with _client(tmp_path, monkeypatch) as client:
        payload = client.get("/api/candidates?trade_date=2026-06-01&limit=1").json()

    assert payload["items"][0]["reason_codes"] == ["NO_ACTIVE_THEME"]


def test_candidates_api_counts_recoverable_blocks_as_wait(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code="000003",
                state=CandidateState.BLOCKED,
                block_type=BlockType.TEMPORARY,
                can_recover=True,
                detected_at="2026-06-01T09:00:00",
                last_seen_at="2026-06-01T09:01:00",
                metadata={
                    "gate_results_by_theme": {
                        "theme-a": {
                            "theme_id": "theme-a",
                            "reason_codes": ["INDEX_WEAK", "MARKET_INDEX_TEMPORARY_CAP"],
                        }
                    }
                },
            )
        )
        db.save_candidate(
            Candidate(
                trade_date="2026-06-01",
                code="000004",
                state=CandidateState.BLOCKED,
                block_type=BlockType.FINAL,
                can_recover=False,
                detected_at="2026-06-01T09:00:00",
                last_seen_at="2026-06-01T09:02:00",
            )
        )
    finally:
        db.close()

    with _client(tmp_path, monkeypatch) as client:
        payload = client.get("/api/candidates?trade_date=2026-06-01&limit=2").json()

    assert payload["summary"]["wait"] == 1
    assert payload["summary"]["blocked"] == 1
    items_by_code = {item["code"]: item for item in payload["items"]}
    assert items_by_code["000003"]["state"] == "BLOCKED"
    assert items_by_code["000003"]["display_state"] == "WAIT"
    assert items_by_code["000004"]["display_state"] == "BLOCKED"


def test_dashboard_event_push_is_coalesced(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    calls = []

    class FakeDashboardConnections:
        @property
        def client_count(self):
            return 1

        async def broadcast_json(self, payload):
            calls.append(payload)

    async def run_scenario():
        monkeypatch.setattr(api, "dashboard_connections", FakeDashboardConnections())
        monkeypatch.setattr(api, "_build_dashboard_snapshot_payload", lambda: {"gateway": {}})
        monkeypatch.setattr(api, "DASHBOARD_EVENT_PUSH_MIN_INTERVAL_SEC", 0.01)
        api._dashboard_snapshot_task = None
        api._dashboard_snapshot_last_sent_monotonic = 0.0

        await api._schedule_dashboard_snapshot_broadcast()
        await api._schedule_dashboard_snapshot_broadcast()
        await asyncio.sleep(0.05)

    asyncio.run(run_scenario())

    assert len(calls) == 1
    assert calls[0]["snapshot"]["gateway"]["dashboard_ws_client_count"] == 1


def test_dashboard_snapshot_payload_uses_shared_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.delenv("TRADING_DASHBOARD_SNAPSHOT_CACHE_TTL_SEC", raising=False)
    import trading_app.api as api

    api = importlib.reload(api)
    calls = []

    def fake_snapshot(_db, *, detail="slim"):
        calls.append(time.monotonic())
        return {"snapshot_detail": detail, "gateway": {}, "runtime": {"call_count": len(calls)}}

    refreshes = []
    monkeypatch.setattr(api, "build_dashboard_snapshot", fake_snapshot)
    monkeypatch.setattr(api, "_submit_dashboard_snapshot_refresh", lambda db_path, detail: refreshes.append((db_path, detail)))
    monkeypatch.setattr(api, "DASHBOARD_SNAPSHOT_CACHE_TTL_SEC", 60.0)
    api._dashboard_snapshot_cache_payload = None
    api._dashboard_snapshot_cache_db_path = ""
    api._dashboard_snapshot_cache_monotonic = 0.0

    first = api._build_dashboard_snapshot_payload()
    second = api._build_dashboard_snapshot_payload()
    forced = api._build_dashboard_snapshot_payload(force=True)

    assert first is second
    assert forced is first
    assert len(calls) == 1
    assert len(refreshes) == 1
    assert first["runtime"]["call_count"] == 1


def test_dashboard_snapshot_cache_returns_stale_and_schedules_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))
            return None

    stale_payload = {"snapshot_detail": "slim", "value": "stale"}
    cache_key = f"{api._dashboard_database_cache_key()}:{api.DASHBOARD_SNAPSHOT_DETAIL_SLIM}"
    monkeypatch.setattr(api, "_dashboard_snapshot_refresh_executor_instance", lambda: FakeExecutor())
    monkeypatch.setattr(
        api,
        "_build_dashboard_snapshot_payload_uncached",
        lambda *, detail="slim": {"snapshot_detail": detail, "value": "fresh"},
    )
    monkeypatch.setattr(api, "DASHBOARD_SNAPSHOT_CACHE_TTL_SEC", 0.001)
    with api._dashboard_snapshot_cache_lock:
        api._dashboard_snapshot_cache_payload = stale_payload
        api._dashboard_snapshot_cache_db_path = cache_key
        api._dashboard_snapshot_cache_monotonic = time.monotonic() - 10.0
        api._dashboard_snapshot_cache_refreshing.clear()

    first = api._build_dashboard_snapshot_payload()
    second = api._build_dashboard_snapshot_payload()
    fn, args = submitted[0]
    fn(*args)

    with api._dashboard_snapshot_cache_lock:
        refreshed = api._dashboard_snapshot_cache_payload
        refreshing = set(api._dashboard_snapshot_cache_refreshing)

    assert first is stale_payload
    assert second is stale_payload
    assert len(submitted) == 1
    assert refreshed == {"snapshot_detail": "slim", "value": "fresh"}
    assert refreshing == set()


def test_dashboard_slim_strategy_replay_payload_drops_heavy_report_detail():
    import trading_app.api as api

    payload = {
        "latest": {
            "report_id": "replay-1",
            "trade_date": "2026-06-15",
            "generated_at": "2026-06-15T09:00:00+09:00",
            "status": "OK",
            "summary": {"total_count": 100, "warnings": ["late"]},
            "recommendations": [{"policy_id": f"p{i}", "raw": "x" * 1000} for i in range(20)],
            "items": [{"raw": "x" * 1000}],
        },
        "recent_runs": [{"replay_id": f"run-{i}", "raw": "x" * 1000} for i in range(8)],
        "summary": {"total_count": 100, "candidate_count": 20, "raw": "x" * 1000},
        "shadow_ranking": [{"policy_id": f"p{i}", "raw": "x" * 1000} for i in range(20)],
        "data_quality": ["late"] * 20,
        "diff_summary": {"changed": 3},
    }

    slim = api._dashboard_slim_strategy_replay_payload(payload)

    assert slim["latest"] == {
        "report_id": "replay-1",
        "trade_date": "2026-06-15",
        "generated_at": "2026-06-15T09:00:00+09:00",
        "status": "OK",
    }
    assert len(slim["recent_runs"]) == 5
    assert len(slim["shadow_ranking"]) == 5
    assert len(slim["data_quality"]) == 10
    assert "recommendations" not in slim["latest"]
    assert "items" not in slim["latest"]
    assert "raw" not in slim["summary"]
    assert "raw" not in slim["shadow_ranking"][0]


def test_dashboard_slim_buy_zero_rca_payload_preserves_operator_keys_and_trims_items():
    import trading_app.api as api

    payload = {
        "available": True,
        "summary": {"total": 12},
        "stage_funnel": {"READY": 12},
        "ready_not_ordered_report": {"items": [{"id": i} for i in range(20)], "count": 20},
        "observe_blocked_after_rally": {"items": [{"id": i} for i in range(20)], "count": 20},
        "live_sim_blocked": {"items": [{"id": i} for i in range(20)], "count": 20},
        "data_quality_blocks": {"reasons": [{"reason": "DATA"}]},
        "data_quality_taxonomy": {"warmup_optional_count": 1},
        "early_small_candidates": [{"id": i} for i in range(20)],
        "ready_not_ordered_items": [{"classification": "READY_BUT_LIVE_SIM_BLOCKED", "id": i} for i in range(20)],
    }

    slim = api._dashboard_slim_buy_zero_rca_payload(payload)

    for key in (
        "available",
        "summary",
        "stage_funnel",
        "ready_not_ordered_report",
        "observe_blocked_after_rally",
        "live_sim_blocked",
        "data_quality_blocks",
        "data_quality_taxonomy",
    ):
        assert key in slim
    assert len(slim["ready_not_ordered_items"]) == 10
    assert len(slim["early_small_candidates"]) == 10
    assert len(slim["ready_not_ordered_report"]["items"]) == 10
    assert slim["ready_not_ordered_items"][0]["classification"] == "READY_BUT_LIVE_SIM_BLOCKED"


def test_transport_dashboard_payload_trims_heavy_error_and_pilot_detail():
    import trading_app.api as api

    error_items = [
        {
            "id": i,
            "sample_id": f"sample-{i}",
            "trace_id": f"trace-{i}",
            "created_at": "2026-06-15T09:00:00+09:00",
            "trade_date": "2026-06-15",
            "direction": "gateway_to_core",
            "message_type": "condition_event",
            "event_id": f"event-{i}",
            "source": "kiwoom_gateway",
            "success": False,
            "error": "STALE_CONDITION_INCLUDE_QUEUE_WAIT",
            "transport_mode": "websocket_real_pilot",
            "metadata": {"raw": "x" * 5000},
            "metadata_json": "x" * 5000,
            "stage_ms": {"raw": "x" * 500},
            "stage_ms_json": "x" * 500,
        }
        for i in range(8)
    ]
    payload = {
        "transport_mode": "websocket_real_pilot",
        "metrics_enabled": True,
        "latest_summary": {
            "count": 8,
            "live_window_sec": 30,
            "historical_sample_count": 100,
            "event_latency_p95_ms": 10,
            "command_latency_p95_ms": 20,
            "ack_latency_p95_ms": 30,
            "warning_flags": ["SLOW"],
        },
        "websocket_recommendation": {
            "should_switch": True,
            "recommendation": "SWITCH_TO_WEBSOCKET",
            "reasons": ["command latency improved"],
        },
        "gateway": {"reconnect_count": 2},
        "latest_report_id": "report-1",
        "recent_errors": error_items,
        "real_gateway_websocket_pilot": {
            "enabled": True,
            "connected": True,
            "state": "CONNECTED",
            "ws_session_id": "session-1",
            "reconnect_count": 3,
            "fallback_reason": "",
            "blocked_order_command_count": 1,
            "session_loss_count": 0,
            "duplicate_ack_count": 2,
            "unknown_ack_count": 4,
            "price_tick_sample_rate": 0.1,
            "price_tick_sampled_count": 5,
            "price_tick_fallback_count": 6,
            "event_fallback_count": 7,
            "last_ws_event_at": "2026-06-15T09:01:00+09:00",
            "last_ws_ack_at": "2026-06-15T09:01:01+09:00",
            "core_ws_event_control_queue_sizes": [1, 2, 3],
            "core_condition_event_queue_sizes_by_worker": [4, 5, 6],
        },
    }

    dashboard = api._transport_dashboard_payload(payload)

    assert len(dashboard["recent_errors"]) == 5
    assert dashboard["recent_errors"][0]["sample_id"] == "sample-0"
    assert dashboard["recent_errors"][0]["error"] == "STALE_CONDITION_INCLUDE_QUEUE_WAIT"
    assert "metadata" not in dashboard["recent_errors"][0]
    assert "metadata_json" not in dashboard["recent_errors"][0]
    assert "stage_ms" not in dashboard["recent_errors"][0]
    assert dashboard["real_gateway_websocket_pilot"]["ws_session_id"] == "session-1"
    assert dashboard["real_gateway_websocket_pilot"]["duplicate_ack_count"] == 2
    assert "core_ws_event_control_queue_sizes" not in dashboard["real_gateway_websocket_pilot"]
    assert dashboard["real_pilot_duplicate_ack_count"] == 2


def test_dashboard_slim_runtime_payload_reduces_duplicate_sections_but_keeps_summaries():
    import trading_app.api as api

    runtime = {
        "enabled": True,
        "running": True,
        "mode": "DRY_RUN",
        "last_cycle_at": "2026-06-15T09:00:00+09:00",
        "live_sim_audit": {
            "available": True,
            "status": "WARN",
            "summary": {"unknown_submit_count": 1},
            "open_orders": [{"raw": "x" * 1000}],
            "reconcile_issues": [{"raw": "x" * 1000}],
            "operator": {"status_message_ko": "check"},
        },
        "buy_zero_rca": {
            "available": True,
            "summary": {"ready_not_ordered_count": 3},
            "stage_funnel": {"READY": 3},
            "ready_not_ordered_items": [{"raw": "x" * 1000}],
        },
        "shadow_small_entry_promotion": {
            "available": True,
            "status": "READY",
            "summary": {"candidate_count": 10},
            "candidate_count": 10,
            "candidates": [{"raw": "x" * 1000}],
            "evidence": {"raw": "x" * 1000},
        },
        "dry_run_orders": {"summary": {"total": 2}, "items": [{"raw": "x" * 1000}]},
        "strategy_replay": {
            "latest": {"report_id": "r1"},
            "summary": {"total_count": 5},
            "funnel": {"READY": 1},
            "shadow_ranking": [{"raw": "x" * 1000}],
        },
        "threshold_ab": {
            "summary": {"candidate_count": 4},
            "recommendations": [{"metric": "a"}, {"metric": "b"}],
            "candidates": [{"raw": "x" * 1000}],
        },
    }

    slim = api._dashboard_slim_runtime_payload(runtime)

    assert slim["running"] is True
    assert slim["live_sim_audit"]["summary"]["unknown_submit_count"] == 1
    assert "open_orders" not in slim["live_sim_audit"]
    assert "reconcile_issues" not in slim["live_sim_audit"]
    assert slim["buy_zero_rca"]["summary"]["ready_not_ordered_count"] == 3
    assert "ready_not_ordered_items" not in slim["buy_zero_rca"]
    assert slim["shadow_small_entry_promotion"]["candidate_count"] == 10
    assert "candidates" not in slim["shadow_small_entry_promotion"]
    assert "evidence" not in slim["shadow_small_entry_promotion"]
    assert slim["dry_run_orders"] == {"summary": {"total": 2}}
    assert "shadow_ranking" not in slim["strategy_replay"]
    assert len(slim["threshold_ab"]["recommendations"]) == 1


def test_dashboard_dry_run_performance_trade_date_uses_latest_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        for trade_date, intent_id in (("2026-06-10", "older"), ("2026-06-15", "latest")):
            db.save_runtime_order_intent(
                {
                    "intent_id": intent_id,
                    "trade_date": trade_date,
                    "source": "strategy_runtime",
                    "mode": "DRY_RUN",
                    "dry_run": True,
                    "status": "DRY_RUN_ACCEPTED",
                    "reason": "",
                    "code": "005930",
                    "side": "buy",
                    "quantity": 1,
                    "price": 10000,
                    "order_amount": 10000,
                    "order_type": 1,
                    "hoga": "00",
                    "tag": "runtime",
                    "strategy_name": "KOSDAQ_THEME_PROFILE",
                    "order_phase": "entry",
                    "idempotency_key": intent_id,
                    "dedupe_key": f"dedupe:{intent_id}",
                    "created_at": f"{trade_date}T09:00:00",
                    "updated_at": f"{trade_date}T09:00:00",
                }
            )

        assert api._dashboard_dry_run_performance_trade_date(db) == "2026-06-15"
    finally:
        db.close()


def test_theme_lab_snapshot_cache_returns_stale_and_schedules_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))
            return None

    calls = {"builder": 0, "refresh": 0}

    def builder():
        calls["builder"] += 1
        return {"value": "sync"}

    def refresh_builder():
        calls["refresh"] += 1
        return {"value": "fresh"}

    try:
        cache_key = (api._dashboard_database_cache_key(db), "theme_lab:test")
        with api._theme_lab_dashboard_snapshot_cache_lock:
            api._theme_lab_dashboard_snapshot_cache.clear()
            api._theme_lab_dashboard_snapshot_refreshing.clear()
            api._theme_lab_dashboard_snapshot_cache[cache_key] = (time.monotonic() - 10.0, {"value": "stale"})
        monkeypatch.setattr(api, "_theme_lab_dashboard_snapshot_refresh_executor_instance", lambda: FakeExecutor())
        monkeypatch.setattr(
            api,
            "_defer_theme_lab_dashboard_snapshot_refresh",
            lambda cache_key, refresh_builder: api._submit_theme_lab_dashboard_snapshot_refresh(cache_key, refresh_builder),
        )

        first = api._cached_theme_lab_dashboard_snapshot(
            db,
            "theme_lab:test",
            builder,
            refresh_builder=refresh_builder,
            ttl_sec=0.001,
        )
        second = api._cached_theme_lab_dashboard_snapshot(
            db,
            "theme_lab:test",
            builder,
            refresh_builder=refresh_builder,
            ttl_sec=0.001,
        )

        fn, args = submitted[0]
        fn(*args)
        with api._theme_lab_dashboard_snapshot_cache_lock:
            refreshed = api._theme_lab_dashboard_snapshot_cache[cache_key][1]
    finally:
        db.close()
        with api._theme_lab_dashboard_snapshot_cache_lock:
            api._theme_lab_dashboard_snapshot_cache.clear()
            api._theme_lab_dashboard_snapshot_refreshing.clear()

    assert first == {"value": "stale"}
    assert second == {"value": "stale"}
    assert calls == {"builder": 0, "refresh": 1}
    assert len(submitted) == 1
    assert refreshed == {"value": "fresh"}


def test_dashboard_snapshot_defaults_to_slim_detail(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="0") as client:
        import trading_app.api as api

        api.gateway_state.record_event(
            GatewayEvent(
                type="heartbeat",
                event_id="evt-dashboard-slim-heartbeat",
                payload={
                    "kiwoom_logged_in": True,
                    "orderable": True,
                    "transport_mode": "websocket_real_pilot",
                    "ws_session_id": "session-slim",
                    "large_raw_blob": "x" * 1000,
                },
            )
        )

        def fake_theme_lab_snapshot(*args, **kwargs):
            return {
                "available": True,
                "source": "test",
                "created_at": "2026-06-10T09:00:00+09:00",
                "calculated_at": "2026-06-10T09:00:01+09:00",
                "last_updated_at": "09:00:02",
                "summary": {"ready_count": 1, "operation_status": "READY_TO_TRADE"},
                "data_quality": {"status": "OK", "message": "ok", "watchset_size": 1},
                "watchset": [{"symbol": "000001", "large_raw_blob": "x" * 1000}],
                "ranked_themes": [{"theme_id": "theme-a", "large_raw_blob": "x" * 1000}],
                "entry_candidates": [{"symbol": "000001", "large_raw_blob": "x" * 1000}],
            }

        monkeypatch.setattr(api, "build_theme_lab_dashboard_snapshot", fake_theme_lab_snapshot)
        slim = client.get("/api/snapshot?refresh=true").json()
        full = client.get("/api/snapshot?refresh=true&detail=full").json()

    assert slim["snapshot_detail"] == "slim"
    assert "last_heartbeat_payload" not in slim["gateway"]
    assert slim["gateway"]["last_heartbeat_summary"]["ws_session_id"] == "session-slim"
    assert "watchset" not in slim["theme_lab"]
    assert "ranked_themes" not in slim["theme_lab"]
    assert full["snapshot_detail"] == "full"
    assert full["gateway"]["last_heartbeat_payload"]["large_raw_blob"]
    assert full["theme_lab"]["watchset"][0]["large_raw_blob"]


def test_dashboard_logs_display_kst(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(api, "LOG_LIVE_WINDOW_SEC", 10**9)
    db = TradingDatabase(str(db_path))
    try:
        db.conn.execute(
            "INSERT INTO logs(created_at, message) VALUES (?, ?)",
            ("2026-05-31 09:41:37", "[gateway][rate_limited] register_realtime"),
        )
        db.conn.commit()
        api.gateway_state.record_event(
            GatewayEvent(
                type="command_ack",
                timestamp="2026-05-31T09:41:38+00:00",
                payload={"command_type": "register_realtime", "status": "ACKED"},
            )
        )

        logs = api.build_logs_snapshot(db, limit=10)
    finally:
        db.close()

    assert logs["timezone"] == "Asia/Seoul"
    assert logs["core"][0].startswith("2026-05-31 18:41:37 KST ")
    assert logs["gateway"][0]["timestamp"] == "2026-05-31 18:41:38 KST"
    assert logs["items"][0]["source"] == "gateway"
    assert logs["items"][0]["line"] == "2026-05-31 18:41:38 KST [gateway_event] command_ack"


def test_dashboard_logs_hide_stale_lines(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(db_path))
    try:
        db.conn.execute(
            "INSERT INTO logs(created_at, message) VALUES (?, ?)",
            ("2026-05-31 09:41:37", "[gateway][rate_limited] stale"),
        )
        db.conn.commit()

        logs = api.build_logs_snapshot(db, limit=10)
    finally:
        db.close()

    assert logs["core"] == []
    assert logs["stale_core_log_count"] == 1


def test_dashboard_logs_hide_gateway_heartbeat(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(api, "LOG_LIVE_WINDOW_SEC", 10**9)
    db = TradingDatabase(str(db_path))
    try:
        api.gateway_state.record_event(
            GatewayEvent(
                type="heartbeat",
                timestamp="2026-05-31T09:41:38+00:00",
                payload={"kiwoom_logged_in": True},
            )
        )

        logs = api.build_logs_snapshot(db, limit=10)
    finally:
        db.close()

    assert logs["gateway"] == []
    assert logs["items"] == []
    assert logs["hidden_gateway_event_counts"] == {"heartbeat": 1}


def test_dashboard_logs_hide_price_tick_noise(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(api, "LOG_LIVE_WINDOW_SEC", 10**9)
    db = TradingDatabase(str(db_path))
    try:
        api.gateway_state.record_event(
            GatewayEvent(
                type="price_tick",
                timestamp="2026-05-31T09:41:38+00:00",
                payload={"code": "005930", "price": 70000},
            )
        )
        api.gateway_state.record_event(
            GatewayEvent(
                type="command_ack",
                timestamp="2026-05-31T09:41:39+00:00",
                payload={"command_type": "register_realtime", "status": "ACKED"},
            )
        )

        logs = api.build_logs_snapshot(db, limit=10)
    finally:
        db.close()

    assert logs["gateway"][0]["type"] == "command_ack"
    assert [item["type"] for item in logs["items"]] == ["command_ack"]
    assert logs["hidden_gateway_event_counts"] == {"price_tick": 1}


def test_dashboard_slim_logs_payload_strips_gateway_event_raw_payload():
    import trading_app.api as api

    raw_event = {
        "type": "command_ack",
        "timestamp": "2026-05-31T09:41:38+00:00",
        "event_id": "evt-ack",
        "source": "kiwoom_gateway",
        "payload": {
            "command_type": "register_realtime",
            "status": "ACKED",
            "transport_mode": "websocket_real_pilot",
            "transport_trace": {"raw": "x" * 10000},
            "raw_blob": "y" * 10000,
        },
    }
    payload = {
        "core": ["2026-05-31 18:41:37 KST [gateway][rate_limited] register_realtime"],
        "gateway": [raw_event],
        "items": [
            {
                "source": "gateway",
                "timestamp": "2026-05-31 18:41:38 KST",
                "timestamp_utc": "2026-05-31T09:41:38+00:00",
                "type": "command_ack",
                "line": "2026-05-31 18:41:38 KST [gateway_event] command_ack",
                "event": raw_event,
            }
        ],
        "warnings": [],
    }

    slim = api._dashboard_slim_logs_payload(payload)

    assert slim["items"][0]["line"] == "2026-05-31 18:41:38 KST [gateway_event] command_ack"
    assert slim["gateway"][0]["payload"] == {
        "command_type": "register_realtime",
        "status": "ACKED",
        "transport_mode": "websocket_real_pilot",
    }
    assert "transport_trace" not in slim["gateway"][0]["payload"]
    assert "raw_blob" not in slim["items"][0]["event"]["payload"]


def test_runtime_start_requires_token(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled="1") as client:
        response = client.post("/api/runtime/start")

    assert response.status_code == 401


def test_naver_theme_sync_api_requires_token_and_syncs_universe(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(api, "NaverThemeUniverseSource", lambda max_pages=20: naver_source())
    with TestClient(api.app) as client:
        rejected = client.post("/api/themes/sync/naver")
        accepted = client.post("/api/themes/sync/naver", headers={"X-Local-Token": "test-token"})

    assert rejected.status_code == 401
    payload = accepted.json()
    assert payload["source"] == "naver_theme_universe"
    assert payload["status"] == "success"
    assert payload["member_count"] == 5
