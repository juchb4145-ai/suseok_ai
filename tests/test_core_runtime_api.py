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

    monkeypatch.setattr(api, "build_dashboard_snapshot", fake_snapshot)
    monkeypatch.setattr(api, "DASHBOARD_SNAPSHOT_CACHE_TTL_SEC", 60.0)
    api._dashboard_snapshot_cache_payload = None
    api._dashboard_snapshot_cache_db_path = ""
    api._dashboard_snapshot_cache_monotonic = 0.0

    first = api._build_dashboard_snapshot_payload()
    second = api._build_dashboard_snapshot_payload()
    forced = api._build_dashboard_snapshot_payload(force=True)

    assert first is second
    assert len(calls) == 2
    assert first["runtime"]["call_count"] == 1
    assert forced["runtime"]["call_count"] == 2


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
