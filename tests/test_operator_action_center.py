from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase


def test_operator_action_db_logs_status_and_summary(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        action = db.save_operator_action(
            {
                "action_id": "act-direct",
                "trade_date": "2026-06-08",
                "requested_at": "2026-06-08T09:02:00+09:00",
                "action_type": "CHECK_GATEWAY_STATUS",
                "status": "RUNNING",
                "symbol": "000001",
                "request_payload": {"source": "test"},
            }
        )
        duplicate = db.save_operator_action({**action, "status": "FAILED"})
        assert duplicate["action_id"] == "act-direct"
        assert duplicate["status"] == "RUNNING"

        updated = db.update_operator_action_status("act-direct", "SUCCESS", response={"ok": True})
        assert updated is not None
        assert updated["status"] == "SUCCESS"
        assert updated["response_payload"] == {"ok": True}

        actions = db.list_operator_actions("2026-06-08", action_type="CHECK_GATEWAY_STATUS", status="SUCCESS")
        assert [item["action_id"] for item in actions] == ["act-direct"]
        summary = db.summarize_operator_actions("2026-06-08")
        assert summary["total_count"] == 1
        assert summary["success_count"] == 1
        assert summary["by_action_type"] == {"CHECK_GATEWAY_STATUS": 1}
    finally:
        db.close()


def test_operator_action_catalog_recommendations_execute_and_logs(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    db = TradingDatabase(str(db_path))
    try:
        db.save_operator_event(
            {
                "event_id": "evt-gateway-down",
                "trade_date": "2026-06-08",
                "occurred_at": "2026-06-08T09:01:00+09:00",
                "event_type": "GATEWAY_DISCONNECTED",
                "severity": "CRITICAL",
                "category": "gateway",
                "message_ko": "Gateway 연결 끊김",
            }
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        catalog = client.get("/api/themelab/operator-actions/catalog").json()
        action_types = {item["action_type"] for item in catalog["actions"]}
        disabled_types = {item["action_type"] for item in catalog["disabled_actions"]}
        assert {"RUNTIME_CYCLE_ONCE", "CHECK_GATEWAY_STATUS", "START_KIWOOM_GATEWAY", "ADD_OPERATOR_NOTE", "OPEN_RUNBOOK"}.issubset(action_types)
        assert {"LIVE_BUY", "LIVE_SELL", "CANCEL_LIVE_ORDER", "OVERRIDE_LIVE_GUARD"}.issubset(disabled_types)
        assert next(item for item in catalog["disabled_actions"] if item["action_type"] == "LIVE_BUY")["enabled"] is False

        recommendations = client.get(
            "/api/themelab/operator-actions/recommendations",
            params={"trade_date": "2026-06-08", "event_id": "evt-gateway-down"},
        ).json()
        recommended_types = [item["action_type"] for item in recommendations["recommendations"]]
        assert recommended_types[:3] == ["CHECK_GATEWAY_STATUS", "START_KIWOOM_GATEWAY", "OPEN_RUNBOOK"]
        assert recommendations["runbook"]["key"] == "GATEWAY_DISCONNECTED"

        blocked = client.post(
            "/api/themelab/operator-actions/execute",
            json={"action_id": "act-live-buy", "action_type": "LIVE_BUY", "event_id": "evt-gateway-down", "confirm": True},
        )
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "BLOCKED"

        pending = client.post(
            "/api/themelab/operator-actions/execute",
            json={"action_id": "act-runtime-cycle", "action_type": "RUNTIME_CYCLE_ONCE", "event_id": "evt-gateway-down", "confirm": False},
        ).json()
        assert pending["status"] == "PENDING"
        assert pending["confirmation_required"] is True

        gateway = client.post(
            "/api/themelab/operator-actions/execute",
            json={"action_id": "act-gateway-status", "action_type": "CHECK_GATEWAY_STATUS", "event_id": "evt-gateway-down", "confirm": True},
        ).json()
        assert gateway["status"] == "SUCCESS"
        assert "commands" in gateway["result"]

        duplicate = client.post(
            "/api/themelab/operator-actions/execute",
            json={"action_id": "act-gateway-status", "action_type": "CHECK_GATEWAY_STATUS", "event_id": "evt-gateway-down", "confirm": True},
        ).json()
        assert duplicate["duplicate"] is True
        assert duplicate["status"] == "SUCCESS"

        actions = client.get("/api/themelab/operator-actions", params={"trade_date": "2026-06-08", "limit": 10}).json()["actions"]
        statuses = {item["action_id"]: item["status"] for item in actions}
        assert statuses["act-live-buy"] == "BLOCKED"
        assert statuses["act-runtime-cycle"] == "PENDING"
        assert statuses["act-gateway-status"] == "SUCCESS"

        summary = client.get("/api/themelab/operator-actions/summary", params={"trade_date": "2026-06-08"}).json()
        assert summary["total_count"] == 3
        assert summary["success_count"] == 1
        assert summary["blocked_count"] == 1
        assert summary["pending_count"] == 1

        events = client.get(
            "/api/themelab/operator-events",
            params={"trade_date": "2026-06-08", "include_acknowledged": True},
        ).json()["events"]
        event_types = {event["event_type"] for event in events}
        assert {"ACTION_BLOCKED", "ACTION_EXECUTED"}.issubset(event_types)


def test_operator_action_token_required_action_logs_failed_without_token(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        response = client.post(
            "/api/themelab/operator-actions/execute",
            json={"action_id": "act-start-no-token", "action_type": "RUNTIME_START", "confirm": True},
        )
        assert response.status_code == 401
        actions = client.get("/api/themelab/operator-actions", params={"limit": 10}).json()["actions"]
        failed = next(item for item in actions if item["action_id"] == "act-start-no-token")
        assert failed["status"] == "FAILED"
        assert "invalid local gateway token" in failed["error_message"]
