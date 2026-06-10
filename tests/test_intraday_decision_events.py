from __future__ import annotations

import importlib
from datetime import timedelta

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from tests.test_themelab_dry_run_bridge import (
    NOW,
    LabGateStatus,
    PriceLocationStatus,
    _flow_result,
    _runtime,
)


def test_runtime_cycle_persists_gate_entry_plan_and_order_decision_events(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    candidate = db.load_candidate("2026-06-01", "000001")
    intents = db.list_runtime_order_intents(candidate_id=candidate.id)
    events = db.list_strategy_decision_events(trade_date="2026-06-01", limit=200)
    action_results = {(event["action_type"], event["action_result"]) for event in events}

    assert ("EVALUATE", "ACCEPTED") in action_results
    assert ("READY", "ACCEPTED") in action_results
    assert ("ENTRY_PLAN", "ACCEPTED") in action_results
    assert ("ENTRY_ORDER_INTENT", "ACCEPTED") in action_results
    order_event = next(event for event in events if event["action_type"] == "ENTRY_ORDER_INTENT")
    assert order_event["order_intent_id"] == intents[0]["intent_id"]
    assert order_event["candidate_instance_id"] == candidate.metadata["candidate_instance_id"]
    assert order_event["candidate_generation_seq"] == candidate.metadata["candidate_generation_seq"]


def test_runtime_cycle_records_entry_plan_not_applicable_for_wait_gate(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(
            LabGateStatus.WAIT,
            PriceLocationStatus.GOOD_PULLBACK,
            reasons=("DATA_INSUFFICIENT", "VI_UNKNOWN"),
        ),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))

    events = db.list_strategy_decision_events(trade_date="2026-06-01", action_type="ENTRY_PLAN", limit=50)

    assert any(event["action_result"] == "NOT_APPLICABLE" for event in events)
    assert any("DATA_INSUFFICIENT" in event["reason_codes"] for event in events)
    assert db.list_runtime_order_intents(code="000001") == []


def test_runtime_cycle_records_duplicate_order_intent_event(tmp_path):
    runtime, db, _gateway_state = _runtime(
        tmp_path,
        _flow_result(LabGateStatus.READY, PriceLocationStatus.GOOD_PULLBACK),
        dry_run_orders=True,
    )

    runtime.start(NOW)
    runtime.cycle(NOW + timedelta(seconds=3))
    runtime.cycle(NOW + timedelta(seconds=6))

    events = db.list_strategy_decision_events(trade_date="2026-06-01", action_type="ENTRY_ORDER_INTENT", limit=50)

    assert len(db.list_runtime_order_intents(code="000001")) == 1
    assert any(event["action_result"] == "DUPLICATE" for event in events)


def test_intraday_decision_summary_api_counts_funnel_and_reasons(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        db.save_strategy_decision_events(
            [
                {
                    "decision_id": "d1",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:00",
                    "candidate_instance_id": "ci-ready-rejected",
                    "code": "000001",
                    "gate_status": "READY",
                    "action_type": "READY",
                    "action_result": "ACCEPTED",
                    "reason_codes": ["PASS"],
                },
                {
                    "decision_id": "d2",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:01",
                    "candidate_instance_id": "ci-ready-rejected",
                    "code": "000001",
                    "gate_status": "READY",
                    "action_type": "ENTRY_ORDER_INTENT",
                    "action_result": "REJECTED",
                    "reason_codes": ["LIVE_GUARD_REJECTED"],
                },
                {
                    "decision_id": "d3",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:02",
                    "candidate_instance_id": "ci-wait",
                    "code": "000002",
                    "gate_status": "WAIT",
                    "action_type": "WAIT",
                    "action_result": "SKIPPED",
                    "reason_codes": ["DATA_INSUFFICIENT", "VI_UNKNOWN"],
                    "data_quality_issues": ["VI_UNKNOWN"],
                },
                {
                    "decision_id": "d4",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:03",
                    "candidate_instance_id": "ci-block",
                    "code": "000003",
                    "gate_status": "BLOCKED",
                    "action_type": "BLOCK",
                    "action_result": "REJECTED",
                    "reason_codes": ["RISK_OFF", "LATE_CHASE"],
                },
                {
                    "decision_id": "d5",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:04",
                    "candidate_instance_id": "ci-ready-no-order",
                    "code": "000004",
                    "gate_status": "READY",
                    "action_type": "READY",
                    "action_result": "ACCEPTED",
                    "reason_codes": ["PASS"],
                },
                {
                    "decision_id": "d6",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:00:05",
                    "candidate_instance_id": "ci-ready-rejected",
                    "code": "000001",
                    "action_type": "EXIT_DECISION",
                    "action_result": "ACCEPTED",
                    "exit_decision_id": 3,
                    "virtual_position_id": 7,
                    "reason_codes": ["TAKE_PROFIT"],
                },
            ]
        )
    finally:
        db.close()

    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        summary = client.get("/api/runtime/decisions/summary?trade_date=2026-06-01").json()["summary"]
        rows = client.get("/api/runtime/decisions/intraday?trade_date=2026-06-01&action_type=WAIT").json()

    assert summary["funnel"]["detected"] == 4
    assert summary["funnel"]["ready"] == 2
    assert summary["funnel"]["wait"] == 1
    assert summary["funnel"]["blocked"] == 1
    assert summary["funnel"]["exit_decision"] == 1
    assert summary["ready_without_order_count"] == 1
    assert summary["order_rejected_count"] == 1
    assert {"reason": "DATA_INSUFFICIENT", "count": 1} in summary["major_reason_distribution"]
    assert rows["items"][0]["reason_codes"] == ["DATA_INSUFFICIENT", "VI_UNKNOWN"]
