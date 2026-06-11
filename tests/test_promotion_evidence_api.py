from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.promotion_evidence import PromotionEvidenceAdapter


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def test_promotion_adapter_builds_evidence_from_runtime_database(tmp_path):
    db_path = Path(tmp_path) / "trader.sqlite3"
    _seed_promotion_sample(db_path)
    db = TradingDatabase(str(db_path))
    try:
        payload = PromotionEvidenceAdapter(db).evaluate(
            trade_date="2026-06-01",
            current_stage="observe",
        )
    finally:
        db.close()

    assert payload["action"] == "PROMOTE"
    assert payload["recommended_stage"] == "dry_run"
    assert payload["evidence"]["decision_count"] == 55
    assert payload["evidence"]["trade_day_count"] == 1
    assert payload["decision"]["metrics"]["realtime_high_ratio"] >= 0.80


def test_promotion_adapter_recovers_bucket_from_joined_decision_details(tmp_path):
    db_path = Path(tmp_path) / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        events = []
        outcomes = []
        for idx in range(5):
            decision_id = f"nested-high-{idx}"
            event = _decision_event_for_date(
                decision_id,
                idx,
                ["READY_PULLBACK"],
                gate_status="READY",
                trade_date="2026-06-03",
            )
            event["details"] = {
                "gate_details": {
                    "realtime_reliability_bucket": "HIGH",
                    "realtime_reliability_score": 96.0,
                }
            }
            outcome = _outcome_for_date(
                decision_id,
                idx,
                "GOOD_READY",
                bucket="",
                current_return_pct=0.3,
                trade_date="2026-06-03",
            )
            outcome["details"] = {"decision": {"reason_codes": ["READY_PULLBACK"]}}
            events.append(event)
            outcomes.append(outcome)
        db.save_strategy_decision_events(events)
        db.save_strategy_decision_outcomes(outcomes, force=True)

        evidence = PromotionEvidenceAdapter(db).build_evidence(trade_date="2026-06-03")
    finally:
        db.close()

    assert evidence.realtime_bucket_counts["HIGH"] == 5
    assert "NO_DATA" not in evidence.realtime_bucket_counts


def test_promotion_api_exposes_evidence_and_decision(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed_promotion_sample(db_path)

    evidence = client.get("/api/runtime/promotion/evidence?trade_date=2026-06-01&current_stage=observe").json()
    assert evidence["evidence"]["decision_count"] == 55
    assert evidence["evidence"]["realtime_bucket_counts"]["HIGH"] == 45
    assert evidence["filters"]["current_stage"] == "observe"

    decision = client.get("/api/runtime/promotion/decision?trade_date=2026-06-01&current_stage=observe").json()
    assert decision["action"] == "PROMOTE"
    assert decision["recommended_stage"] == "dry_run"
    assert decision["decision"]["eligible"] is True
    assert decision["decision"]["blockers"] == []
    assert [row["stage"] for row in decision["stage_matrix"]["rows"]] == ["observe", "dry_run", "live_sim", "real_micro"]
    assert decision["stage_matrix"]["rows"][0]["action"] == "PROMOTE"

    matrix = client.get("/api/runtime/promotion/matrix?trade_date=2026-06-01&current_stage=observe").json()
    assert matrix["stage_matrix"]["current_stage"] == "observe"
    assert len(matrix["stage_matrix"]["rows"]) == 4
    live_sim_row = {row["stage"]: row for row in matrix["stage_matrix"]["rows"]}["live_sim"]
    assert "REAL_MICRO_REQUIRES_OPERATOR_APPROVAL" in live_sim_row["blockers"]
    assert live_sim_row["failed_checks"]


def test_promotion_drilldown_returns_blocker_evidence_rows(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed_realtime_low_blocker_sample(db_path)

    decision = client.get("/api/runtime/promotion/decision?trade_date=2026-06-02&current_stage=observe").json()
    assert decision["action"] == "HOLD"
    assert "REALTIME_HIGH_RATIO_LOW" in decision["decision"]["blockers"]

    drilldown = client.get(
        "/api/runtime/promotion/drilldown",
        params={
            "trade_date": "2026-06-02",
            "current_stage": "observe",
            "blocker": "REALTIME_HIGH_RATIO_LOW",
            "detail_limit": 5,
        },
    ).json()
    assert drilldown["selected_blocker"] == "REALTIME_HIGH_RATIO_LOW"
    assert drilldown["sections"][0]["summary"]["matching_count"] >= 50
    assert len(drilldown["items"]) == 5
    assert {item["realtime_bucket"] for item in drilldown["items"]} == {"LOW"}
    assert drilldown["items"][0]["source_type"] == "decision_outcome"


def test_promotion_drilldown_groups_duplicate_symbol_rows(tmp_path):
    db_path = Path(tmp_path) / "trader.sqlite3"
    db = TradingDatabase(str(db_path))
    try:
        events = []
        outcomes = []
        for idx, (code, horizon) in enumerate(
            [
                ("000001", 60),
                ("000001", 180),
                ("000001", 300),
                ("000002", 60),
                ("000002", 180),
            ]
        ):
            decision_id = f"group-low-{idx}"
            event = _decision_event_for_date(
                decision_id,
                idx,
                ["REALTIME_RELIABILITY_LOW"],
                gate_status="READY",
                trade_date="2026-06-04",
            )
            event["code"] = code
            outcome = _outcome_for_date(
                decision_id,
                idx,
                "GOOD_READY",
                bucket="LOW",
                current_return_pct=-0.2,
                trade_date="2026-06-04",
            )
            outcome["code"] = code
            outcome["horizon_sec"] = horizon
            outcome["outcome_id"] = f"outcome:{decision_id}:{horizon}"
            events.append(event)
            outcomes.append(outcome)
        db.save_strategy_decision_events(events)
        db.save_strategy_decision_outcomes(outcomes, force=True)

        drilldown = PromotionEvidenceAdapter(db).drilldown(
            trade_date="2026-06-04",
            current_stage="observe",
            blocker="REALTIME_HIGH_RATIO_LOW",
            detail_limit=5,
        )
    finally:
        db.close()

    section = drilldown["sections"][0]
    assert section["summary"]["matching_count"] == 5
    assert section["summary"]["group_count"] == 2
    groups = {item["code"]: item for item in section["grouped_items"]}
    assert groups["000001"]["row_count"] == 3
    assert groups["000001"]["horizons_sec"] == [60, 180, 300]
    assert groups["000001"]["bucket_counts"] == {"LOW": 3}
    assert groups["000002"]["row_count"] == 2


def _seed_promotion_sample(db_path: Path) -> None:
    db = TradingDatabase(str(db_path))
    try:
        events = []
        outcomes = []
        for idx in range(45):
            decision_id = f"good-{idx}"
            events.append(_decision_event(decision_id, idx, ["READY_PULLBACK"], gate_status="READY"))
            outcomes.append(_outcome(decision_id, idx, "GOOD_READY", bucket="HIGH", current_return_pct=0.4))
        for idx in range(10):
            decision_id = f"block-{idx}"
            events.append(_decision_event(decision_id, idx + 45, ["REALTIME_RELIABILITY_LOW"], gate_status="BLOCKED"))
            outcomes.append(_outcome(decision_id, idx + 45, "GOOD_BLOCK", bucket="LOW", current_return_pct=-0.2))
        db.save_strategy_decision_events(events)
        db.save_strategy_decision_outcomes(outcomes, force=True)
    finally:
        db.close()


def _seed_realtime_low_blocker_sample(db_path: Path) -> None:
    db = TradingDatabase(str(db_path))
    try:
        events = []
        outcomes = []
        for idx in range(60):
            decision_id = f"low-{idx}"
            events.append(_decision_event_for_date(decision_id, idx, ["REALTIME_RELIABILITY_LOW"], gate_status="READY", trade_date="2026-06-02"))
            outcomes.append(
                _outcome_for_date(
                    decision_id,
                    idx,
                    "GOOD_READY",
                    bucket="LOW",
                    current_return_pct=-0.2,
                    trade_date="2026-06-02",
                )
            )
        db.save_strategy_decision_events(events)
        db.save_strategy_decision_outcomes(outcomes, force=True)
    finally:
        db.close()


def _decision_event(decision_id: str, idx: int, reason_codes: list[str], *, gate_status: str) -> dict:
    return _decision_event_for_date(decision_id, idx, reason_codes, gate_status=gate_status, trade_date="2026-06-01")


def _decision_event_for_date(decision_id: str, idx: int, reason_codes: list[str], *, gate_status: str, trade_date: str) -> dict:
    timestamp = f"{trade_date}T09:{idx % 60:02d}:00"
    return {
        "decision_id": decision_id,
        "trade_date": trade_date,
        "created_at": timestamp,
        "decision_at": timestamp,
        "candidate_id": idx + 1,
        "code": f"{idx + 1:06d}",
        "name": f"Sample {idx}",
        "theme_name": "AI",
        "strategy_name": "KOSDAQ_THEME_PROFILE",
        "gate_status": gate_status,
        "gate_reason": reason_codes[0],
        "reason_status": gate_status,
        "reason_family": "promotion_test",
        "reason_codes": reason_codes,
        "action_type": "ENTRY_ORDER_INTENT" if gate_status == "READY" else "BLOCK",
        "action_result": "ACCEPTED" if gate_status == "READY" else "BLOCKED",
        "details": {"realtime_reliability_bucket": "HIGH" if gate_status == "READY" else "LOW"},
    }


def _outcome(decision_id: str, idx: int, label: str, *, bucket: str, current_return_pct: float) -> dict:
    return _outcome_for_date(decision_id, idx, label, bucket=bucket, current_return_pct=current_return_pct, trade_date="2026-06-01")


def _outcome_for_date(
    decision_id: str,
    idx: int,
    label: str,
    *,
    bucket: str,
    current_return_pct: float,
    trade_date: str,
) -> dict:
    timestamp = f"{trade_date}T09:{idx % 60:02d}:30"
    return {
        "outcome_id": f"outcome:{decision_id}:900",
        "decision_id": decision_id,
        "trade_date": trade_date,
        "code": f"{idx + 1:06d}",
        "candidate_id": idx + 1,
        "decision_at": f"{trade_date}T09:{idx % 60:02d}:00",
        "evaluated_at": timestamp,
        "horizon_sec": 900,
        "price_at_decision": 10000,
        "price_at_horizon": 10040,
        "current_return_pct": current_return_pct,
        "max_return_pct": max(0.0, current_return_pct),
        "outcome_label": label,
        "label_confidence": 0.9,
        "data_status": "OK",
        "details": {"realtime_reliability_bucket": bucket},
        "created_at": timestamp,
        "updated_at": timestamp,
    }
