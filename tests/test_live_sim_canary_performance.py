import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading_app.live_sim_canary_performance import LiveSimCanaryPerformanceAnalyzer


class _DryRunStub:
    def __init__(self, items):
        self.items = items

    def build_report(self, **kwargs):
        return {"items": list(self.items)}


def _db(tmp_path: Path) -> TradingDatabase:
    return TradingDatabase(str(tmp_path / "trader.sqlite3"))


def _save_decision(db: TradingDatabase, **updates):
    payload = {
        "decision_id": "canary-1",
        "trade_date": "2026-06-17",
        "code": "005930",
        "candidate_id": 1,
        "candidate_instance_id": "ci-1",
        "hybrid_status": "READY",
        "hybrid_score": 82.5,
        "theme_name": "AI",
        "eligible": True,
        "status": "SUBMITTED",
        "limit_price": 10000,
        "quantity": 10,
        "order_intent_id": "live-entry-1",
        "gateway_command_id": "cmd-entry-1",
        "created_at": "2026-06-17T09:00:00+09:00",
    }
    payload.update(updates)
    return db.save_live_sim_canary_decision(payload)


def _save_entry_order(db: TradingDatabase, **updates):
    payload = {
        "order_intent_id": "live-entry-1",
        "command_id": "cmd-entry-1",
        "candidate_id": 1,
        "candidate_instance_id": "ci-1",
        "trade_date": "2026-06-17",
        "code": "005930",
        "name": "Samsung",
        "account_id_masked": "12****90",
        "side": "buy",
        "requested_qty": 10,
        "requested_price": 10000,
        "submitted_qty": 10,
        "submitted_price": 10000,
        "broker_order_id": "ord-entry-1",
        "order_status": "FILLED",
        "submitted_at": "2026-06-17T09:00:01+09:00",
        "accepted_at": "2026-06-17T09:00:01.200000+09:00",
        "first_fill_at": "2026-06-17T09:00:02+09:00",
        "last_fill_at": "2026-06-17T09:00:02+09:00",
        "updated_at": "2026-06-17T09:00:02+09:00",
        "idempotency_key": "idem-entry-1",
    }
    payload.update(updates)
    return db.save_live_sim_order(payload)


def _save_fill(db: TradingDatabase, **updates):
    payload = {
        "order_intent_id": "live-entry-1",
        "broker_order_id": "ord-entry-1",
        "fill_id": "fill-entry-1",
        "event_id": "evt-entry-1",
        "code": "005930",
        "side": "buy",
        "account_id_masked": "12****90",
        "fill_qty": 10,
        "fill_price": 10010,
        "cumulative_fill_qty": 10,
        "remaining_qty": 0,
        "event_time": "2026-06-17T09:00:02+09:00",
        "received_at": "2026-06-17T09:00:02+09:00",
    }
    payload.update(updates)
    return db.save_live_sim_fill_event(payload)


def test_lifecycle_links_by_command_id_and_calculates_entry_quality(tmp_path):
    db = _db(tmp_path)
    try:
        _save_decision(db)
        _save_entry_order(db)
        _save_fill(db)
        analyzer = LiveSimCanaryPerformanceAnalyzer(db, dry_run_analyzer=_DryRunStub([]), report_root=tmp_path / "reports")

        report = analyzer.build_report(trade_date="2026-06-17")

        assert report["summary"]["total_lifecycle_count"] == 1
        case = report["items"][0]
        assert case["matched_by"] == "gateway_command_id"
        assert case["filled_quantity"] == 10
        assert case["fill_ratio"] == 1
        assert case["entry_slippage_bp"] == 10
        assert case["fill_quality_grade"] == "GOOD"
        assert "EXIT_NOT_SUBMITTED" in case["issue_types"]
    finally:
        db.close()


def test_dry_run_vs_live_sim_classifies_live_better(tmp_path):
    db = _db(tmp_path)
    try:
        _save_decision(db)
        _save_entry_order(db, requested_price=10000)
        _save_fill(db, fill_price=10000)
        db.save_live_sim_order(
            {
                "order_intent_id": "live-exit-1",
                "command_id": "cmd-exit-1",
                "candidate_id": 1,
                "candidate_instance_id": "ci-1",
                "trade_date": "2026-06-17",
                "code": "005930",
                "name": "Samsung",
                "account_id_masked": "12****90",
                "side": "sell",
                "requested_qty": 10,
                "requested_price": 10100,
                "submitted_qty": 10,
                "submitted_price": 10100,
                "broker_order_id": "ord-exit-1",
                "order_status": "FILLED",
                "submitted_at": "2026-06-17T09:10:00+09:00",
                "accepted_at": "2026-06-17T09:10:00.100000+09:00",
                "first_fill_at": "2026-06-17T09:10:01+09:00",
                "last_fill_at": "2026-06-17T09:10:01+09:00",
                "updated_at": "2026-06-17T09:10:01+09:00",
                "details": {"exit_reason": "TAKE_PROFIT"},
            }
        )
        _save_fill(
            db,
            order_intent_id="live-exit-1",
            broker_order_id="ord-exit-1",
            fill_id="fill-exit-1",
            event_id="evt-exit-1",
            side="sell",
            fill_price=10200,
            event_time="2026-06-17T09:10:01+09:00",
            received_at="2026-06-17T09:10:01+09:00",
        )
        analyzer = LiveSimCanaryPerformanceAnalyzer(
            db,
            dry_run_analyzer=_DryRunStub(
                [
                    {
                        "candidate_instance_id": "ci-1",
                        "trade_date": "2026-06-17",
                        "code": "005930",
                        "entry_price": 10000,
                        "net_return_pct": 1.0,
                        "exit_reasons": ["TIME_EXIT"],
                    }
                ]
            ),
            report_root=tmp_path / "reports",
        )

        case = analyzer.build_report(trade_date="2026-06-17")["items"][0]

        assert case["outcome_match"] == "LIVE_BETTER"
        assert case["net_return_diff_pct"] == 1.0
        assert case["exit_reason_changed"] is True
        assert case["final_status"] == "CLOSED"
    finally:
        db.close()


def test_no_fill_cancel_and_orphan_execution_are_reported(tmp_path):
    db = _db(tmp_path)
    try:
        _save_decision(db, decision_id="canary-cancel", order_intent_id="live-cancel", gateway_command_id="cmd-cancel")
        _save_entry_order(
            db,
            order_intent_id="live-cancel",
            command_id="cmd-cancel",
            broker_order_id="ord-cancel",
            order_status="CANCELLED",
            accepted_at="2026-06-17T09:00:02+09:00",
            first_fill_at="",
            last_fill_at="",
            cancelled_at="2026-06-17T09:01:00+09:00",
            updated_at="2026-06-17T09:01:00+09:00",
        )
        _save_fill(
            db,
            order_intent_id="",
            broker_order_id="orphan-order",
            fill_id="orphan-fill",
            event_id="orphan-event",
            fill_qty=3,
        )
        analyzer = LiveSimCanaryPerformanceAnalyzer(db, dry_run_analyzer=_DryRunStub([]), report_root=tmp_path / "reports")

        report = analyzer.build_report(trade_date="2026-06-17")
        by_case = {item["case_id"]: item for item in report["items"]}

        cancel_case = by_case["canary-cancel"]
        assert cancel_case["fill_quality_grade"] == "NO_FILL"
        assert cancel_case["final_status"] == "CANCELLED"
        assert "CANCELLED_BEFORE_FILL" in cancel_case["issue_types"]
        assert any("ORPHAN_EXECUTION" in item["issue_types"] for item in report["items"])
    finally:
        db.close()


def test_api_rebuild_persists_exports_and_never_creates_gateway_order(tmp_path, monkeypatch):
    db_path = tmp_path / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    db = TradingDatabase(str(db_path))
    try:
        _save_decision(db)
        _save_entry_order(db)
        _save_fill(db)
    finally:
        db.close()
    client = TestClient(api.app)

    report = client.get("/api/runtime/live-sim/canary/performance?trade_date=2026-06-17").json()
    assert report["summary"]["total_lifecycle_count"] == 1

    rebuild = client.post(
        "/api/runtime/live-sim/canary/performance/rebuild",
        json={"trade_date": "2026-06-17", "persist": True, "export": "all"},
        headers={"X-Local-Token": "test-token"},
    ).json()

    assert rebuild["persisted"] is True
    assert rebuild["safety_scope"]["gateway_send_order_created"] is False
    assert Path(rebuild["exported"]["json"]).exists()
    assert Path(rebuild["exported"]["csv"]).exists()
    assert Path(rebuild["exported"]["md"]).exists()

    reports = client.get("/api/runtime/live-sim/canary/performance/reports").json()
    assert reports["items"][0]["report_id"] == rebuild["report_id"]
    cases = client.get("/api/runtime/live-sim/canary/performance/cases?trade_date=2026-06-17").json()
    assert cases["pagination"]["count"] >= 1

    db = TradingDatabase(str(db_path))
    try:
        command_count = db.conn.execute("SELECT COUNT(*) AS count FROM gateway_commands").fetchone()["count"]
        assert command_count == 0
    finally:
        db.close()
