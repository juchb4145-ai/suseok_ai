import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.strategy.models import ReviewFinalStatus, TradeReview


def _client(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    import trading_app.api as api

    api = importlib.reload(api)
    return TestClient(api.app), db_path


def _seed(db_path: Path) -> None:
    db = TradingDatabase(str(db_path))
    try:
        db.save_runtime_order_intent(
            {
                "intent_id": "entry-api",
                "trade_date": "2026-05-30",
                "source": "strategy_runtime",
                "mode": "DRY_RUN",
                "dry_run": True,
                "status": "DRY_RUN_REJECTED",
                "reason": "PRICE_INVALID",
                "account": "dryrun-account",
                "code": "005930",
                "side": "buy",
                "quantity": 0,
                "price": 0,
                "order_amount": 0,
                "order_type": 1,
                "hoga": "00",
                "tag": "runtime",
                "strategy_name": "KOSDAQ_THEME_PROFILE",
                "candidate_id": 1,
                "order_phase": "entry",
                "gate_reason": "LOW_BREADTH",
                "gate_status": "BLOCKED",
                "idempotency_key": "entry-api",
                "dedupe_key": "dedupe:entry-api",
                "safety": {"ok": False, "reason": "PRICE_INVALID"},
                "live_safety": {"ok": False, "reason": "GATEWAY_NOT_CONNECTED"},
                "request": {"theme_name": "AI", "theme_score": 30, "hybrid_score": 42},
                "metadata": {"session_bucket": "OPEN"},
                "created_at": "2026-05-30T09:01:00",
                "updated_at": "2026-05-30T09:01:00",
            }
        )
        db.save_trade_review(
            TradeReview(
                candidate_id=1,
                trade_date="2026-05-30",
                code="005930",
                name="Samsung",
                theme_name="AI",
                strategy_profile="KOSDAQ_THEME_PROFILE",
                gate_result_key="g1",
                review_key="r1",
                final_status=ReviewFinalStatus.BLOCKED_FINAL.value,
                entry_price=10000,
                max_return_5m=1.0,
                max_return_10m=2.0,
                max_return_20m=4.0,
                max_drawdown_20m=-0.3,
                blocked_but_later_rallied=True,
                false_negative_flag=True,
                details={"session_bucket": "OPEN"},
                created_at="2026-05-30T09:30:00",
            )
        )
    finally:
        db.close()


def test_performance_api_report_false_signals_rebuild_and_export(tmp_path, monkeypatch):
    client, db_path = _client(tmp_path, monkeypatch)
    _seed(db_path)

    report = client.get("/api/runtime/performance/dry-run?trade_date=2026-05-30").json()
    assert report["summary"]["total_lifecycle_count"] == 1
    assert report["summary"]["false_negative_count"] == 1
    assert report["items"][0]["dry_run_false_negative_type"] == "DRY_RUN_REJECTED_BUT_RALLIED"

    signals = client.get("/api/runtime/performance/dry-run/false-signals?trade_date=2026-05-30&type=false_negative").json()
    assert signals["total"] == 1

    rebuild = client.post(
        "/api/runtime/performance/dry-run/rebuild?trade_date=2026-05-30&persist=true",
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert rebuild["persisted"] is True
    report_id = rebuild["report_id"]

    reports = client.get("/api/runtime/performance/dry-run/reports").json()
    assert reports["items"][0]["report_id"] == report_id

    detail = client.get(f"/api/runtime/performance/dry-run/reports/{report_id}").json()
    assert detail["found"] is True
    assert detail["items"]

    exported = client.get(
        "/api/runtime/performance/dry-run/export?trade_date=2026-05-30&format=md",
        headers={"X-Local-Token": "test-token"},
    ).json()
    assert Path(exported["exports"]["md"]).exists()

    snapshot = client.get("/api/snapshot").json()
    assert "dry_run_performance" in snapshot
    assert snapshot["dry_run_performance"]["false_negative_count"] == 1

