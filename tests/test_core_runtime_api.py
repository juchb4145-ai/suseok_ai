import importlib

from fastapi.testclient import TestClient
from storage.db import TradingDatabase
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


def test_runtime_start_cycle_stop_api(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    with _client(tmp_path, monkeypatch, enabled="1") as client:
        started = client.post("/api/runtime/start", headers={"X-Local-Token": "test-token"}).json()
        cycled = client.post("/api/runtime/cycle", headers={"X-Local-Token": "test-token"}).json()
        commands = client.get("/api/gateway/commands/history?include_finished=true&include_payload=true").json()
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
