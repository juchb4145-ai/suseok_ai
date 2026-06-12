import importlib
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.buy_zero_rca import BuyZeroRCAAnalyzer


START = datetime(2026, 6, 1, 9, 1, 0)


def test_decision_events_project_to_buy_zero_trace_and_summary_reasons(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_strategy_decision_events(
            [
                {
                    "decision_id": "d-wait",
                    "runtime_cycle_id": "cycle-1",
                    "trade_date": "2026-06-01",
                    "decision_at": "2026-06-01T09:01:00",
                    "candidate_instance_id": "ci-wait",
                    "code": "000001",
                    "name": "waiter",
                    "theme_name": "AI",
                    "gate_status": "WAIT",
                    "gate_reason": "DATA_INSUFFICIENT",
                    "reason_codes": ["DATA_INSUFFICIENT", "WAIT_DATA_SUPPORT_NOT_READY"],
                    "action_type": "EVALUATE",
                    "action_result": "SKIPPED",
                    "details": {
                        "gate_details": {
                            "theme_id": "ai",
                            "theme_name": "AI",
                            "support_ready": False,
                            "selected_support_source": "recent_support_price",
                            "selected_support_price": 0,
                        }
                    },
                }
            ]
        )

        traces = db.list_buy_zero_trace_events(trade_date="2026-06-01", limit=20)
        summary = BuyZeroRCAAnalyzer(db).build_summary(trade_date="2026-06-01")

        assert any(row["stage"] == "THEMELAB_GATE_EVALUATED" for row in traces)
        assert summary["total_candidates"] == 1
        assert summary["gate_evaluated_count"] == 1
        assert {"reason": "DATA_INSUFFICIENT", "count": 1} in summary["top_block_reasons"]
        assert summary["top_data_insufficient_reasons"][0]["reason"] == "DATA_INSUFFICIENT"
        assert summary["available"] is True
        assert summary["stage_funnel"][0]["stage"] == "CANDIDATE_GENERATED"
        assert any(row["stage"] == "THEMELAB_GATE_EVALUATED" and row["failed"] == 1 for row in summary["stage_funnel"])
        assert summary["data_quality_blocks"]["reasons"][0]["reason"] == "DATA_INSUFFICIENT"
    finally:
        db.close()


def test_ready_not_ordered_classifies_hybrid_observe_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-hybrid", "000001", "HYBRID_GATE_EVALUATED", "READY", False, "READY_BUT_HYBRID_OBSERVE_ONLY"),
                _trace("ci-hybrid", "000001", "LIFECYCLE_UPDATED", "READY", True, ""),
            ]
        )

        report = BuyZeroRCAAnalyzer(db).ready_not_ordered_report(trade_date="2026-06-01")

        assert report["summary"]["ready_not_ordered_count"] == 1
        assert report["items"][0]["classification"] == "READY_BUT_HYBRID_OBSERVE_ONLY"
    finally:
        db.close()


def test_ready_not_ordered_classifies_support_diagnostic_only(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-support", "000002", "LIFECYCLE_UPDATED", "READY", True, ""),
                {
                    **_trace("ci-support", "000002", "ENTRY_PLAN_CREATED", "ACCEPTED", True, ""),
                    "entry_plan_id": 7,
                    "entry_plan_submittable": False,
                    "entry_plan_diagnostic_only": True,
                    "support_ready": False,
                    "primary_block_reason": "WAIT_DATA_SUPPORT_NOT_READY",
                    "reason_codes": ["WAIT_DATA_SUPPORT_NOT_READY", "SUPPORT_NOT_READY"],
                },
            ]
        )

        report = BuyZeroRCAAnalyzer(db).ready_not_ordered_report(trade_date="2026-06-01")

        assert report["items"][0]["classification"] == "READY_BUT_ENTRY_PLAN_DIAGNOSTIC_ONLY"
    finally:
        db.close()


def test_ready_not_ordered_classifies_dry_run_accepted_live_sim_blocked(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-live", "000003", "LIFECYCLE_UPDATED", "READY", True, ""),
                {
                    **_trace("ci-live", "000003", "DRY_RUN_INTENT_CREATED", "DRY_RUN_ACCEPTED", True, ""),
                    "dry_run_intent_id": "dry-1",
                    "dry_run_status": "DRY_RUN_ACCEPTED",
                },
                {
                    **_trace("ci-live", "000003", "LIVE_SIM_BLOCKED", "BLOCKED", False, "LIVE_SIM_EXIT_GUARD_DISABLED"),
                    "dry_run_intent_id": "dry-1",
                    "live_sim_intent_id": "live-1",
                    "live_sim_status": "BLOCKED",
                    "live_sim_reason": "LIVE_SIM_EXIT_GUARD_DISABLED",
                    "reason_codes": ["LIVE_SIM_EXIT_GUARD_DISABLED"],
                },
            ]
        )

        summary = BuyZeroRCAAnalyzer(db).build_summary(trade_date="2026-06-01")
        report = BuyZeroRCAAnalyzer(db).ready_not_ordered_report(trade_date="2026-06-01")

        assert summary["dry_run_intent_count"] == 1
        assert summary["live_sim_blocked_count"] == 1
        assert report["items"][0]["classification"] == "READY_BUT_LIVE_SIM_BLOCKED"
    finally:
        db.close()


def test_ready_not_ordered_classifies_gate_result_key_mismatch(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-key", "000004", "THEMELAB_GATE_EVALUATED", "READY", True, ""),
                _trace("ci-key", "000004", "ENTRY_PLAN_SKIPPED", "SKIPPED", False, "GATE_RESULT_KEY_MISMATCH"),
            ]
        )

        report = BuyZeroRCAAnalyzer(db).ready_not_ordered_report(trade_date="2026-06-01")

        assert report["items"][0]["classification"] == "READY_BUT_GATE_RESULT_KEY_MISMATCH"
    finally:
        db.close()


def test_wait_observe_outcome_links_missed_opportunity_to_trace(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        db.save_buy_zero_trace_events(
            [_trace("ci-observe", "000005", "THEMELAB_GATE_EVALUATED", "OBSERVE", False, "PRICE_LOCATION_PROVISIONAL")]
        )
        _save_snapshot(
            db,
            START,
            [
                {
                    "symbol": "000005",
                    "name": "observe-rally",
                    "current_price": 100,
                    "final_gate_status": "OBSERVE",
                    "price_location_status": "PULLBACK_RECLAIM",
                    "price_location_readiness": "PROVISIONAL",
                    "price_location_readiness_reason_codes": ["PRICE_LOCATION_PROVISIONAL"],
                }
            ],
        )
        db.save_theme_lab_outcome_observations(
            [
                {
                    "observed_at": (START + timedelta(minutes=15)).isoformat(),
                    "trade_date": "2026-06-01",
                    "stock_code": "000005",
                    "price": 104,
                    "source": "theme_lab_outcome_tracking",
                }
            ]
        )

        report = BuyZeroRCAAnalyzer(db).missed_opportunity_report(trade_date="2026-06-01")

        assert report["summary"]["missed_opportunity_count"] == 1
        assert report["top_observe_then_rally_candidates"][0]["trace_id"]
        assert report["reason_code_missed_opportunity_ranking"][0]["reason_code"] == "PRICE_LOCATION_PROVISIONAL"
    finally:
        db.close()


def test_buy_zero_rca_api_exposes_operator_summary(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-api", "000006", "LIFECYCLE_UPDATED", "READY", True, ""),
                _trace("ci-api", "000006", "DRY_RUN_INTENT_REJECTED", "DRY_RUN_REJECTED", False, "PRICE_INVALID"),
            ]
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        summary = client.get("/api/runtime/buy-zero/summary?trade_date=2026-06-01").json()
        traces = client.get("/api/runtime/buy-zero/traces?trade_date=2026-06-01&stage=DRY_RUN_INTENT_REJECTED").json()
        ready = client.get("/api/runtime/buy-zero/ready-not-ordered?trade_date=2026-06-01").json()

    assert summary["summary"]["ready_count"] == 1
    assert summary["operator"]["today_buy_zero_top3_causes"][0]["reason"] == "PRICE_INVALID"
    assert traces["pagination"]["total"] == 1
    assert ready["items"][0]["classification"] == "READY_BUT_DRY_RUN_REJECTED"


def test_buy_zero_rca_snapshot_exposes_dashboard_fields_without_trace(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        snapshot = client.get("/api/snapshot?refresh=true").json()

    rca = snapshot["buy_zero_rca"]
    assert rca["available"] is False
    assert "summary" in rca
    assert "stage_funnel" in rca
    assert "ready_not_ordered_report" in rca
    assert "observe_blocked_after_rally" in rca
    assert "live_sim_blocked" in rca
    assert "data_quality_blocks" in rca
    assert snapshot["runtime"]["buy_zero_rca"]["available"] is False


def test_buy_zero_rca_snapshot_lists_ready_live_sim_blocked_and_data_quality(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    today = date.today().isoformat()
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        db.save_buy_zero_trace_events(
            [
                {**_trace("ci-data", "000101", "THEMELAB_GATE_EVALUATED", "WAIT", False, "DATA_INSUFFICIENT"), "trade_date": today},
                {
                    **_trace("ci-taxonomy", "000103", "THEMELAB_GATE_EVALUATED", "WAIT_DATA_EARLY_SMALL_CANDIDATE", False, "BASE_LINE_120_INSUFFICIENT_CANDLES"),
                    "trade_date": today,
                    "data_quality_bucket": "WARMUP_OPTIONAL",
                    "data_quality_action": "ALLOW_EARLY_SMALL_CANDIDATE",
                    "missing_optional_fields": ["BASE_LINE_120_INSUFFICIENT_CANDLES"],
                    "early_small_candidate": True,
                    "early_small_order_enabled": False,
                    "early_small_position_size_multiplier": 0.15,
                    "early_small_rejected_reason": "EARLY_SMALL_OBSERVE_ONLY",
                    "operator_message_ko": "보조지표만 부족해 관찰합니다.",
                    "reason_codes": ["DATA_INSUFFICIENT", "WARMUP_OPTIONAL_ONLY", "BASE_LINE_120_INSUFFICIENT_CANDLES"],
                },
                {**_trace("ci-live-snapshot", "000102", "LIFECYCLE_UPDATED", "READY", True, ""), "trade_date": today},
                {
                    **_trace("ci-live-snapshot", "000102", "LIVE_SIM_BLOCKED", "BLOCKED", False, "LIVE_SIM_BLOCKED"),
                    "trade_date": today,
                    "live_sim_status": "BLOCKED",
                    "live_sim_reason": "LIVE_SIM_BLOCKED",
                    "reason_codes": ["LIVE_SIM_BLOCKED"],
                },
            ]
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        snapshot = client.get("/api/snapshot?refresh=true").json()
        ready = client.get(f"/api/runtime/buy-zero/ready-not-ordered?trade_date={today}").json()

    rca = snapshot["buy_zero_rca"]
    assert rca["available"] is True
    assert rca["data_quality_blocks"]["reasons"][0]["reason"] == "DATA_INSUFFICIENT"
    assert {"bucket": "WARMUP_OPTIONAL", "count": 1} in rca["data_quality_blocks"]["buckets"]
    assert {"action": "ALLOW_EARLY_SMALL_CANDIDATE", "count": 1} in rca["data_quality_blocks"]["actions"]
    assert rca["data_quality_taxonomy"]["warmup_optional_count"] == 1
    assert rca["data_quality_taxonomy"]["early_small_candidate_count"] == 1
    assert rca["early_small_candidates"][0]["early_small_rejected_reason"] == "EARLY_SMALL_OBSERVE_ONLY"
    assert rca["live_sim_blocked"]["count"] == 1
    assert ready["items"][0]["classification"] == "READY_BUT_LIVE_SIM_BLOCKED"
    assert rca["ready_not_ordered_items"][0]["classification"] == "READY_BUT_LIVE_SIM_BLOCKED"


def test_buy_zero_trace_detail_api_filters_by_code_and_candidate_instance_id(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        db.save_buy_zero_trace_events(
            [
                _trace("ci-filter-a", "000201", "CANDIDATE_GENERATED", "GENERATED", True, ""),
                _trace("ci-filter-b", "000202", "CANDIDATE_GENERATED", "GENERATED", True, ""),
                _trace("ci-filter-a", "000201", "LIVE_SIM_BLOCKED", "BLOCKED", False, "LIVE_SIM_BLOCKED"),
            ]
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        by_code = client.get("/api/runtime/buy-zero/traces?trade_date=2026-06-01&code=000201").json()
        by_candidate = client.get(
            "/api/runtime/buy-zero/traces?trade_date=2026-06-01&candidate_instance_id=ci-filter-a"
        ).json()

    assert by_code["pagination"]["total"] == 2
    assert {item["code"] for item in by_candidate["items"]} == {"000201"}
    assert {item["candidate_instance_id"] for item in by_candidate["items"]} == {"ci-filter-a"}


def _trace(candidate_instance_id: str, code: str, stage: str, status: str, passed: bool, reason: str) -> dict:
    return {
        "trace_id": f"{candidate_instance_id}:{stage}:{status}:{reason or 'pass'}",
        "trade_date": "2026-06-01",
        "runtime_cycle_id": "cycle-1",
        "decision_cycle_id": "decision-cycle-1",
        "candidate_instance_id": candidate_instance_id,
        "candidate_generation_seq": 1,
        "code": code,
        "name": f"name-{code}",
        "theme_id": "ai",
        "theme_name": "AI",
        "stage": stage,
        "stage_status": status,
        "pass_fail": "PASS" if passed else "FAIL",
        "passed": passed,
        "primary_block_reason": reason,
        "reason_codes": [reason] if reason else [],
        "gate_status": status if status in {"READY", "READY_SMALL", "WAIT", "OBSERVE", "BLOCKED"} else "READY",
        "created_at": "2026-06-01T09:01:00",
    }


def _save_snapshot(db: TradingDatabase, at: datetime, watchset: list[dict]) -> None:
    db.save_theme_lab_flow_result(
        at.isoformat(),
        {
            "market_status": {"market_status": "CHOPPY"},
            "theme_rankings": [],
            "theme_condition_snapshots": [],
            "condition_hit_snapshots": [],
            "watchset_snapshots": watchset,
            "gate_decisions": [],
            "data_quality": {},
        },
    )
