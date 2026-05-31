import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.models import ReviewFinalStatus, TradeReview


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "threshold_api.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    monkeypatch.setenv("TRADING_THRESHOLD_AB_MIN_SAMPLE_COUNT", "1")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def _save_entry(db: TradingDatabase, intent_id: str, *, status: str = "DRY_RUN_ACCEPTED", gate_reason: str = "LATE_CHASE", theme_score: float = 42.0) -> None:
    db.save_runtime_order_intent(
        {
            "intent_id": intent_id,
            "trade_date": "2026-05-30",
            "source": "strategy_runtime",
            "mode": "DRY_RUN",
            "dry_run": True,
            "status": status,
            "reason": "OK" if status == "DRY_RUN_ACCEPTED" else "GATE_BLOCKED",
            "account": "dryrun-account",
            "code": "005930",
            "side": "buy",
            "quantity": 10,
            "price": 10000,
            "order_amount": 100000,
            "order_type": 1,
            "hoga": "00",
            "tag": "runtime",
            "strategy_name": "KOSDAQ_THEME_PROFILE",
            "candidate_id": 1,
            "entry_plan_id": 10,
            "virtual_order_id": 20,
            "virtual_position_id": 30 if status == "DRY_RUN_ACCEPTED" else None,
            "order_phase": "entry",
            "gate_reason": gate_reason,
            "gate_status": "READY" if status == "DRY_RUN_ACCEPTED" else "BLOCKED",
            "idempotency_key": intent_id,
            "dedupe_key": f"dedupe:{intent_id}",
            "safety": {"ok": status == "DRY_RUN_ACCEPTED", "reason": "" if status == "DRY_RUN_ACCEPTED" else "LOW_BREADTH"},
            "live_safety": {"ok": status == "DRY_RUN_ACCEPTED", "reason": "" if status == "DRY_RUN_ACCEPTED" else "GATEWAY_NOT_CONNECTED"},
            "request": {"theme_name": "AI", "theme_score": theme_score, "gate_score": theme_score, "hybrid_score": theme_score},
            "metadata": {"session_bucket": "OPEN"},
            "created_at": "2026-05-30T09:01:00",
            "updated_at": "2026-05-30T09:01:00",
        }
    )


def _save_review(db: TradingDatabase, *, final_status: str, max_return_20m: float, drawdown: float, fp: bool, fn: bool = False) -> None:
    db.save_trade_review(
        TradeReview(
            candidate_id=1,
            trade_date="2026-05-30",
            code="005930",
            name="Samsung",
            theme_name="AI",
            strategy_profile="KOSDAQ_THEME_PROFILE",
            gate_result_key="g1",
            review_key=f"review-{final_status}-{max_return_20m}",
            entry_plan_id=10,
            virtual_order_id=20,
            virtual_position_id=30 if fp else None,
            final_status=final_status,
            entry_price=10000,
            exit_price=9800 if fp else 0,
            max_return_5m=max_return_20m / 2,
            max_return_10m=max_return_20m / 1.5,
            max_return_20m=max_return_20m,
            max_drawdown_20m=drawdown,
            false_positive_flag=fp,
            false_negative_flag=fn,
            blocked_but_later_rallied=fn,
            details={"session_bucket": "OPEN", "theme_score": 42.0, "hybrid_score": 42.0, "gate_score": 42.0},
            created_at="2026-05-30T09:30:00",
        )
    )


def _seed(db_path: Path) -> None:
    db = TradingDatabase(str(db_path))
    try:
        _save_entry(db, "late-entry", status="DRY_RUN_ACCEPTED", gate_reason="LATE_CHASE", theme_score=40)
        _save_review(
            db,
            final_status=ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
            max_return_20m=0.8,
            drawdown=-4.0,
            fp=True,
        )
        _save_entry(db, "low-breadth-entry", status="DRY_RUN_REJECTED", gate_reason="LOW_BREADTH", theme_score=82)
        _save_review(
            db,
            final_status=ReviewFinalStatus.BLOCKED_FINAL.value,
            max_return_20m=5.0,
            drawdown=-0.3,
            fp=False,
            fn=True,
        )
    finally:
        db.close()


def test_threshold_ab_api_filters_persists_exports_and_snapshot(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed(db_path)

    report = client.get("/api/runtime/threshold-ab/dry-run?trade_date=2026-05-30&limit=50").json()
    assert report["summary"]["candidate_count"] >= 1
    assert report["pagination"]["total"] == report["total_candidates"]
    assert report["disclaimer_ko"].startswith("이 리포트는 DRY_RUN")

    risk = client.get("/api/runtime/threshold-ab/dry-run?trade_date=2026-05-30&category=risk&limit=50").json()
    assert risk["candidates"]
    assert all(item["category"] == "risk" for item in risk["candidates"])

    rebuild = client.post(
        "/api/runtime/threshold-ab/dry-run/rebuild?trade_date=2026-05-30&persist=true&export=true&format=all",
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert rebuild["persisted"] is True
    assert set(rebuild["exported"]) == {"json", "csv", "md"}
    assert Path(rebuild["exported"]["md"]).exists()

    report_id = rebuild["report_id"]
    saved_reports = client.get("/api/runtime/threshold-ab/dry-run/reports").json()
    assert saved_reports["items"][0]["report_id"] == report_id

    saved_detail = client.get(f"/api/runtime/threshold-ab/dry-run/reports/{report_id}").json()
    assert saved_detail["found"] is True
    assert saved_detail["candidate_rows"]

    candidate_id = saved_detail["candidate_rows"][0]["candidate_id"]
    candidate_detail = client.get(f"/api/runtime/threshold-ab/dry-run/candidates/{candidate_id}?report_id={report_id}").json()
    assert candidate_detail["found"] is True
    assert candidate_detail["disclaimer_ko"].startswith("실제 적용")

    snapshot = client.get("/api/snapshot").json()
    assert "threshold_ab" in snapshot
    assert snapshot["threshold_ab"]["summary"]["candidate_count"] >= 1


def test_threshold_ab_rebuild_does_not_modify_strategy_runtime_settings(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed(db_path)
    db = TradingDatabase(str(db_path))
    try:
        before = db.conn.execute("SELECT COUNT(*) AS count FROM strategy_runtime_settings").fetchone()["count"]
    finally:
        db.close()

    client.post(
        "/api/runtime/threshold-ab/dry-run/rebuild?trade_date=2026-05-30&persist=true",
        headers={"X-Local-Token": "test-token"},
    )

    db = TradingDatabase(str(db_path))
    try:
        after = db.conn.execute("SELECT COUNT(*) AS count FROM strategy_runtime_settings").fetchone()["count"]
    finally:
        db.close()
    assert before == after
