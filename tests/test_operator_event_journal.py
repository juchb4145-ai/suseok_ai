from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase


def test_operator_event_journal_persists_filters_ack_and_summary(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        ready = _operator_event(
            event_id="evt-ready-000001",
            event_type="BUY_READY_NEW",
            severity="OPPORTUNITY",
            category="opportunity",
            symbol="000001",
            message_ko="READY 발생: 종목[000001]",
        )
        warning = _operator_event(
            event_id="evt-data-broken",
            event_type="DATA_QUALITY_DEGRADED",
            severity="CRITICAL",
            category="data",
            message_ko="데이터 품질 저하: BROKEN",
        )

        assert db.save_operator_event(ready) is True
        assert db.save_operator_event(ready) is False
        assert db.save_operator_events([warning, {"event_id": "bad"}]) == {
            "inserted_count": 1,
            "duplicate_count": 0,
            "rejected_count": 1,
        }

        all_events = db.list_operator_events("2026-06-08")
        assert [event["event_id"] for event in all_events] == ["evt-data-broken", "evt-ready-000001"]
        assert db.list_operator_events("2026-06-08", severity="CRITICAL")[0]["event_type"] == "DATA_QUALITY_DEGRADED"
        assert db.list_operator_events("2026-06-08", category="opportunity")[0]["symbol"] == "000001"

        summary = db.summarize_operator_events("2026-06-08")
        assert summary["total_count"] == 2
        assert summary["critical_count"] == 1
        assert summary["opportunity_count"] == 1
        assert summary["ready_event_count"] == 1
        assert summary["data_quality_degraded_count"] == 1
        assert summary["by_symbol"] == [{"symbol": "000001", "count": 1}]

        assert db.acknowledge_operator_events(["evt-ready-000001"], acknowledged_by="tester") == 1
        assert [event["event_id"] for event in db.list_operator_events("2026-06-08", include_acknowledged=False)] == ["evt-data-broken"]

        assert db.hide_operator_event("evt-ready-000001") == 1
        visible_ids = [event["event_id"] for event in db.list_operator_events("2026-06-08")]
        assert visible_ids == ["evt-data-broken"]
        hidden = db.list_operator_events("2026-06-08", include_hidden=True, symbol="000001")
        assert hidden[0]["hidden"] is True
        assert hidden[0]["acknowledged"] is True
    finally:
        db.close()


def test_theme_lab_operator_event_api_round_trips_without_snapshot_side_effects(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    import trading_app.api as api

    api = importlib.reload(api)
    event = _operator_event(
        event_id="evt-order-intent",
        event_type="ORDER_INTENT_CREATED",
        severity="INFO",
        category="order",
        symbol="000002",
        message_ko="주문 의도 생성: 종목[000002]",
    )

    with TestClient(api.app) as client:
        snapshot = client.get("/api/themelab/snapshot")
        assert snapshot.status_code == 200
        assert client.get("/api/themelab/operator-events", params={"trade_date": "2026-06-08"}).json()["events"] == []

        ingest = client.post("/api/themelab/operator-events", json={"events": [event, event]}).json()
        assert ingest == {"inserted_count": 1, "duplicate_count": 1, "rejected_count": 0}
        rejected = client.post(
            "/api/themelab/operator-events",
            json={"events": [{**event, "event_id": "bad-event", "event_type": "NOT_ALLOWED"}]},
        ).json()
        assert rejected == {"inserted_count": 0, "duplicate_count": 0, "rejected_count": 1}

        listed = client.get("/api/themelab/operator-events", params={"trade_date": "2026-06-08"}).json()
        assert listed["events"][0]["event_id"] == "evt-order-intent"
        assert listed["events"][0]["type"] == "ORDER_INTENT_CREATED"
        assert listed["events"][0]["message"] == "주문 의도 생성: 종목[000002]"
        assert listed["events"][0]["payload"]["event_type"] == "ORDER_INTENT_CREATED"

        ack = client.post("/api/themelab/operator-events/ack", json={"event_ids": ["evt-order-intent"], "acknowledged_by": "tester"}).json()
        assert ack == {"updated_count": 1}
        assert client.get(
            "/api/themelab/operator-events",
            params={"trade_date": "2026-06-08", "include_acknowledged": False},
        ).json()["events"] == []

        summary = client.get("/api/themelab/operator-events/summary", params={"trade_date": "2026-06-08"}).json()
        assert summary["total_count"] == 1
        assert summary["order_intent_created_count"] == 1
        assert summary["info_count"] == 1

        hide = client.post("/api/themelab/operator-events/hide", json={"event_ids": ["evt-order-intent"]}).json()
        assert hide == {"updated_count": 1}
        assert client.get("/api/themelab/operator-events", params={"trade_date": "2026-06-08"}).json()["events"] == []
        hidden = client.get(
            "/api/themelab/operator-events",
            params={"trade_date": "2026-06-08", "include_hidden": True},
        ).json()["events"]
        assert hidden[0]["hidden"] is True


def _operator_event(
    *,
    event_id: str,
    event_type: str,
    severity: str,
    category: str,
    symbol: str = "",
    message_ko: str,
) -> dict:
    return {
        "event_id": event_id,
        "trade_date": "2026-06-08",
        "occurred_at": "2026-06-08T09:01:00+09:00",
        "source": "themelab_dashboard",
        "event_type": event_type,
        "severity": severity,
        "category": category,
        "symbol": symbol,
        "stock_name": "종목",
        "primary_theme": "전력기기",
        "stock_role": "LEADER",
        "candidate_instance_id": f"candidate-{symbol or event_id}",
        "from_status": "WAIT",
        "to_status": "READY",
        "gate_status": "READY",
        "display_status": "READY",
        "message_ko": message_ko,
        "payload": {"event_type": event_type, "symbol": symbol},
    }
