from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.dashboard_postmarket_review import classify_event_outcome, summarize_review_items


def test_classify_event_outcome_expected_buckets():
    direct_quality = {"base": {"source": "payload_direct"}, "future": {"source": "payload_direct"}}

    missed = classify_event_outcome(
        {"event_type": "BUY_READY_NEW", "candidate_instance_id": "candidate-ready", "has_order": False},
        {"return_3m_pct": 1.6, "return_5m_pct": 2.1, "return_close_or_last_pct": 2.0},
        direct_quality,
    )
    assert missed["outcome_label"] == "MISSED_OPPORTUNITY"
    assert missed["confidence"] == "HIGH"

    protected = classify_event_outcome(
        {"event_type": "CHASE_RISK_BLOCKED", "candidate_instance_id": "candidate-chase"},
        {"return_3m_pct": -0.2, "return_5m_pct": -0.8, "return_close_or_last_pct": -1.1},
        direct_quality,
    )
    assert protected["outcome_label"] == "PROTECTED_FROM_CHASE"

    good_block = classify_event_outcome(
        {"event_type": "MARKET_WAIT_STARTED", "candidate_instance_id": "candidate-market"},
        {"return_3m_pct": -1.2, "return_5m_pct": -0.6, "return_close_or_last_pct": -0.3},
        direct_quality,
    )
    assert good_block["outcome_label"] == "GOOD_BLOCK"

    review_needed = classify_event_outcome(
        {"event_type": "DATA_QUALITY_DEGRADED", "candidate_instance_id": "candidate-data"},
        {"return_3m_pct": 1.7, "return_5m_pct": 1.2, "return_close_or_last_pct": 0.5},
        direct_quality,
    )
    assert review_needed["outcome_label"] == "REVIEW_NEEDED"

    insufficient = classify_event_outcome(
        {"event_type": "READY_TO_WAIT"},
        {"return_3m_pct": None, "return_5m_pct": None, "return_close_or_last_pct": None},
        {"base": {"source": "missing"}, "future": {"source": "missing"}},
    )
    assert insufficient["outcome_label"] == "DATA_INSUFFICIENT"
    assert insufficient["confidence"] == "LOW"


def test_summarize_review_items_counts_outcomes_and_reasons():
    items = [
        _review_item("MISSED_OPPORTUNITY", "BUY_READY_NEW", symbol="000001", block_reason="LIVE_GUARD"),
        _review_item("GOOD_BLOCK", "MARKET_WAIT_STARTED", symbol="000002", block_reason="MARKET_WAIT"),
        _review_item("REVIEW_NEEDED", "DATA_QUALITY_DEGRADED", symbol="000002", block_reason="STALE_TICK"),
    ]

    summary = summarize_review_items(items)

    assert summary["total_count"] == 3
    assert summary["ready_count"] == 1
    assert summary["missed_opportunity_count"] == 1
    assert summary["good_block_count"] == 1
    assert summary["review_needed_count"] == 1
    assert summary["by_event_type"]["BUY_READY_NEW"] == 1
    assert summary["by_block_reason"][0]["count"] == 1
    assert summary["by_symbol"][0]["symbol"] == "000002"


def test_postmarket_review_db_rebuild_persists_unique_items(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_operator_events(
            [
                _operator_event(
                    "evt-ready",
                    "BUY_READY_NEW",
                    "000001",
                    "candidate-ready",
                    {"current_price": 1000, "price_3m": 1016, "price_5m": 1022},
                ),
                _operator_event(
                    "evt-chase",
                    "CHASE_RISK_BLOCKED",
                    "000002",
                    "candidate-chase",
                    {"current_price": 1000, "price_3m": 996, "price_5m": 990, "risk_reason_codes": ["CHASE_RISK"]},
                ),
                _operator_event("evt-data-missing", "DATA_QUALITY_DEGRADED", "000003", "candidate-data", {}),
            ]
        )

        result = db.rebuild_postmarket_reviews("2026-06-08")
        duplicate = db.rebuild_postmarket_reviews("2026-06-08")
        items = db.list_postmarket_review_items("2026-06-08", limit=10)
        summary = db.summarize_postmarket_reviews("2026-06-08")
    finally:
        db.close()

    assert result["generated_count"] == 3
    assert result["inserted_count"] == 3
    assert duplicate["duplicate_count"] == 3
    assert {item["outcome_label"] for item in items} == {"MISSED_OPPORTUNITY", "PROTECTED_FROM_CHASE", "DATA_INSUFFICIENT"}
    assert summary["missed_opportunity_count"] == 1
    assert summary["protected_from_chase_count"] == 1
    assert summary["data_insufficient_count"] == 1


def test_postmarket_review_api_rebuild_list_summary_export_and_action_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")

    db = TradingDatabase(str(db_path))
    try:
        db.save_operator_events(
            [
                _operator_event(
                    "evt-api-ready",
                    "BUY_READY_NEW",
                    "000011",
                    "candidate-api-ready",
                    {"current_price": 2000, "price_3m": 2035, "price_5m": 2050},
                ),
                _operator_event(
                    "evt-api-market-wait",
                    "MARKET_WAIT_STARTED",
                    "000012",
                    "candidate-api-market",
                    {"current_price": 1000, "price_3m": 986, "price_5m": 984, "market_wait_reason": "MARKET_WEAK"},
                ),
            ]
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    headers = {"X-Local-Token": "test-token"}
    with TestClient(api.app) as client:
        catalog = client.get("/api/themelab/operator-actions/catalog").json()
        assert "REBUILD_POSTMARKET_REVIEW" in {item["action_type"] for item in catalog["actions"]}

        rebuild = client.post(
            "/api/themelab/postmarket-review/rebuild",
            headers=headers,
            json={"trade_date": "2026-06-08", "review_scope": "postmarket", "force": True},
        )
        assert rebuild.status_code == 200
        assert rebuild.json()["generated_count"] == 2

        listed = client.get("/api/themelab/postmarket-review", params={"trade_date": "2026-06-08"}).json()
        assert len(listed["items"]) == 2
        assert listed["pagination"]["has_next"] is False

        summary = client.get("/api/themelab/postmarket-review/summary", params={"trade_date": "2026-06-08"}).json()
        assert summary["missed_opportunity_count"] == 1
        assert summary["good_block_count"] == 1

        exported = client.get("/api/themelab/postmarket-review/export", params={"trade_date": "2026-06-08", "format": "csv"})
        assert exported.status_code == 200
        assert "review_id,trade_date" in exported.text
        assert "MISSED_OPPORTUNITY" in exported.text

        action = client.post(
            "/api/themelab/operator-actions/execute",
            headers=headers,
            json={
                "action_id": "act-postmarket-review",
                "action_type": "REBUILD_POSTMARKET_REVIEW",
                "trade_date": "2026-06-08",
                "review_scope": "postmarket",
                "force": True,
                "confirm": True,
            },
        ).json()
        assert action["status"] == "SUCCESS"
        assert action["result"]["generated_count"] == 2


def _operator_event(event_id: str, event_type: str, symbol: str, candidate_instance_id: str, payload: dict) -> dict:
    return {
        "event_id": event_id,
        "trade_date": "2026-06-08",
        "occurred_at": "2026-06-08T15:05:00+09:00",
        "source": "themelab_dashboard",
        "event_type": event_type,
        "severity": "OPPORTUNITY" if event_type == "BUY_READY_NEW" else "WARNING",
        "category": "opportunity" if event_type == "BUY_READY_NEW" else "risk",
        "symbol": symbol,
        "stock_name": f"종목{symbol}",
        "primary_theme": "전력기기",
        "stock_role": "LEADER",
        "candidate_instance_id": candidate_instance_id,
        "from_status": "WAIT",
        "to_status": "READY",
        "gate_status": "READY" if event_type == "BUY_READY_NEW" else "WAIT",
        "display_status": event_type,
        "message_ko": f"{event_type} {symbol}",
        "payload": {"event_type": event_type, "symbol": symbol, "candidate_instance_id": candidate_instance_id, **payload},
    }


def _review_item(outcome: str, event_type: str, *, symbol: str, block_reason: str) -> dict:
    return {
        "outcome_label": outcome,
        "event_type": event_type,
        "symbol": symbol,
        "stock_name": f"종목{symbol}",
        "primary_theme": "전력기기",
        "block_reason": block_reason,
        "payload": {"has_order": False},
    }
