from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import ReviewFinalStatus, TradeReview
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer


def _intent() -> dict:
    return {
        "intent_id": "entry-export",
        "trade_date": "2026-05-30",
        "source": "strategy_runtime",
        "mode": "DRY_RUN",
        "dry_run": True,
        "status": "DRY_RUN_ACCEPTED",
        "reason": "OK",
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
        "virtual_position_id": None,
        "order_phase": "entry",
        "gate_reason": "READY",
        "gate_status": "READY",
        "idempotency_key": "entry-export",
        "dedupe_key": "dedupe:entry-export",
        "safety": {"ok": True, "reason": ""},
        "live_safety": {"ok": True, "reason": ""},
        "request": {"theme_name": "AI", "theme_score": 70, "hybrid_score": 80},
        "metadata": {"session_bucket": "OPEN"},
        "created_at": "2026-05-30T09:01:00",
        "updated_at": "2026-05-30T09:01:00",
    }


def _review() -> TradeReview:
    return TradeReview(
        candidate_id=1,
        trade_date="2026-05-30",
        code="005930",
        name="Samsung",
        theme_name="AI",
        strategy_profile="KOSDAQ_THEME_PROFILE",
        gate_result_key="g1",
        review_key="r1",
        virtual_order_id=20,
        final_status=ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        entry_price=10000,
        exit_price=10300,
        max_return_5m=1.0,
        max_return_10m=2.0,
        max_return_20m=3.0,
        max_drawdown_20m=-0.5,
        details={"session_bucket": "OPEN"},
        created_at="2026-05-30T09:30:00",
    )


def test_report_persistence_and_exports(tmp_path):
    db = TradingDatabase(str(Path(tmp_path) / "perf.sqlite3"))
    try:
        db.save_runtime_order_intent(_intent())
        db.save_trade_review(_review())
        analyzer = DryRunPerformanceAnalyzer(db, report_root=Path(tmp_path) / "reports")
        report = analyzer.build_report(trade_date="2026-05-30", limit=1000)
        saved = analyzer.persist_report(report)
        exports = analyzer.export_report(report, fmt="all")

        assert saved["report_id"] == report["report_id"]
        assert db.get_dry_run_performance_report(report["report_id"])["items"]
        assert db.list_dry_run_performance_reports()[0]["report_id"] == report["report_id"]
        assert set(exports) == {"json", "csv", "md"}
        for path in exports.values():
            assert Path(path).exists()
        assert "lifecycle_id" in Path(exports["csv"]).read_text(encoding="utf-8-sig")
        assert "DRY_RUN Performance Report" in Path(exports["md"]).read_text(encoding="utf-8")
    finally:
        db.close()

