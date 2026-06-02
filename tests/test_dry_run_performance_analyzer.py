from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import ReviewFinalStatus, TradeReview, VirtualPosition
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer


def _db(tmp_path) -> TradingDatabase:
    return TradingDatabase(str(Path(tmp_path) / "perf.sqlite3"))


def _intent(intent_id: str, **overrides) -> dict:
    side = overrides.pop("side", "buy")
    order_phase = overrides.pop("order_phase", "entry" if side == "buy" else "exit")
    status = overrides.pop("status", "DRY_RUN_ACCEPTED")
    payload = {
        "intent_id": intent_id,
        "trade_date": "2026-05-30",
        "source": "strategy_runtime",
        "mode": "DRY_RUN",
        "dry_run": True,
        "status": status,
        "reason": overrides.pop("reason", "OK"),
        "account": "dryrun-account",
        "code": "005930",
        "side": side,
        "quantity": overrides.pop("quantity", 10),
        "price": overrides.pop("price", 10000),
        "order_amount": overrides.pop("order_amount", 100000),
        "order_type": 1 if side == "buy" else 2,
        "hoga": "00",
        "tag": "runtime",
        "strategy_name": "KOSDAQ_THEME_PROFILE",
        "candidate_id": 1,
        "entry_plan_id": 10 if side == "buy" else None,
        "virtual_order_id": 20,
        "virtual_position_id": 30,
        "order_phase": order_phase,
        "exit_decision_id": None,
        "exit_decision_type": "",
        "exit_reason": "",
        "gate_reason": "READY",
        "gate_status": "READY",
        "idempotency_key": intent_id,
        "dedupe_key": f"dedupe:{intent_id}",
        "safety": {"ok": status == "DRY_RUN_ACCEPTED", "reason": "" if status == "DRY_RUN_ACCEPTED" else "PRICE_INVALID"},
        "live_safety": {"ok": True, "reason": ""},
        "request": {"theme_name": "AI", "theme_score": 75.0, "gate_score": 80.0, "hybrid_score": 82.0},
        "metadata": {"session_bucket": "OPEN", "gate_result_key": "g1"},
        "created_at": "2026-05-30T09:01:00",
        "updated_at": "2026-05-30T09:01:00",
    }
    payload.update(overrides)
    return payload


def _review(**overrides) -> TradeReview:
    payload = {
        "candidate_id": 1,
        "trade_date": "2026-05-30",
        "code": "005930",
        "name": "Samsung",
        "market": "KOSPI",
        "theme_id": "theme-ai",
        "theme_name": "AI",
        "strategy_profile": "KOSDAQ_THEME_PROFILE",
        "gate_result_key": "g1",
        "review_key": "r1",
        "entry_plan_id": 10,
        "virtual_order_id": 20,
        "virtual_position_id": 30,
        "final_status": ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
        "entry_price": 10000,
        "exit_price": 9800,
        "max_return_5m": 0.5,
        "max_return_10m": 1.0,
        "max_return_20m": 1.2,
        "max_drawdown_20m": -3.5,
        "false_negative_flag": False,
        "false_positive_flag": True,
        "details": {"session_bucket": "OPEN", "hybrid_score": 82.0, "theme_score": 75.0},
        "created_at": "2026-05-30T09:30:00",
    }
    payload.update(overrides)
    return TradeReview(**payload)


def test_lifecycle_links_entry_and_exit_by_virtual_position_and_marks_support_loss_fp(tmp_path):
    db = _db(tmp_path)
    try:
        position = db.save_virtual_position(
            VirtualPosition(
                candidate_id=1,
                virtual_order_id=20,
                entry_price=10000,
                quantity=10,
                opened_at="2026-05-30T09:02:00",
                closed_at="2026-05-30T09:15:00",
                close_price=9800,
                close_reason="SUPPORT_LOSS",
                max_return_pct=1.2,
                max_drawdown_pct=-3.5,
                realized_return_pct=-2.0,
            )
        )
        db.save_runtime_order_intent(_intent("entry-1", virtual_position_id=position.id))
        db.save_runtime_order_intent(
            _intent(
                "exit-1",
                side="sell",
                order_phase="exit",
                price=9800,
                quantity=10,
                virtual_position_id=position.id,
                exit_decision_id=77,
                exit_decision_type="SUPPORT_LOSS",
                exit_reason="SUPPORT_LOSS_CONFIRMED",
            )
        )
        saved_review = db.save_trade_review(_review(virtual_position_id=position.id))
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["entry_intent_id"] == "entry-1"
    assert item["exit_intent_ids"] == ["exit-1"]
    assert item["trade_review_id"] == saved_review.id
    assert item["dry_run_false_positive_type"] == "LIVE_WOULD_PASS_BUT_SUPPORT_LOSS"
    assert report["summary"]["false_positive_count"] == 1
    assert report["grouped"]["by_exit_decision_type"][0]["decision_type"] == "SUPPORT_LOSS"


def test_rejected_entry_that_rallies_is_false_negative_and_opportunity_loss(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-rejected",
                status="DRY_RUN_REJECTED",
                reason="QUANTITY_ZERO",
                safety={"ok": False, "reason": "QUANTITY_ZERO"},
                live_safety={"ok": False, "reason": "GATEWAY_NOT_CONNECTED"},
                virtual_position_id=None,
                virtual_order_id=None,
            )
        )
        db.save_trade_review(
            _review(
                virtual_position_id=None,
                virtual_order_id=None,
                final_status=ReviewFinalStatus.PLAN_NOT_CREATED.value,
                max_return_20m=4.2,
                max_drawdown_20m=-0.5,
                false_positive_flag=False,
                false_negative_flag=False,
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["dry_run_false_negative_type"] == "DRY_RUN_REJECTED_BUT_RALLIED"
    assert item["opportunity_loss_type"] == "SAFETY_REJECT_REASON_OPPORTUNITY_LOSS"
    assert report["summary"]["opportunity_loss_count"] == 1
    assert report["false_signal_summary"]["top_live_reject_reasons_with_rally"][0]["reason"] == "GATEWAY_NOT_CONNECTED"


def test_no_entry_intent_but_review_rallied_is_false_negative(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_trade_review(
            _review(
                virtual_position_id=None,
                virtual_order_id=None,
                final_status=ReviewFinalStatus.BLOCKED_FINAL.value,
                max_return_20m=5.0,
                max_drawdown_20m=-0.2,
                blocked_but_later_rallied=True,
                false_negative_flag=True,
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["entry_intent_id"] == ""
    assert item["dry_run_false_negative_type"] == "NO_ENTRY_INTENT_BUT_RALLIED"
    assert report["summary"]["false_negative_count"] == 1


def test_take_profit_accepted_entry_is_true_positive(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(_intent("entry-tp"))
        db.save_runtime_order_intent(
            _intent(
                "exit-tp",
                side="sell",
                order_phase="exit",
                price=10300,
                quantity=7,
                exit_decision_type="TAKE_PROFIT",
            )
        )
        db.save_trade_review(
            _review(
                final_status=ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
                exit_price=10300,
                max_return_20m=3.5,
                max_drawdown_20m=-0.4,
                false_positive_flag=False,
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["signal_classification"] == "true_positive"
    assert report["summary"]["true_positive_count"] == 1


def test_orphan_exit_and_missing_review_are_reported_as_data_quality(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "orphan-exit",
                side="sell",
                order_phase="exit",
                candidate_id=None,
                virtual_order_id=None,
                virtual_position_id=None,
                trade_review_id=None,
                exit_decision_id=None,
                exit_decision_type="TRAILING_STOP",
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert "ORPHAN_EXIT" in item["data_quality_issues"]
    assert item["data_quality_issue_reasons"]["ORPHAN_EXIT"] == "EXIT_INTENT_ONLY_CANDIDATE_FALLBACK"
    assert report["summary"]["data_quality"]["orphan_exit_count"] == 1


def test_candidate_code_trade_date_fallback_links_entry_exit_and_review(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-fallback",
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                trade_review_id=None,
            )
        )
        db.save_runtime_order_intent(
            _intent(
                "exit-fallback",
                side="sell",
                order_phase="exit",
                candidate_id=None,
                virtual_order_id=None,
                virtual_position_id=None,
                trade_review_id=None,
                exit_decision_type="TRAILING_STOP",
            )
        )
        db.save_trade_review(
            _review(
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                final_status=ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    assert report["summary"]["total_lifecycle_count"] == 1
    item = report["items"][0]
    assert item["entry_intent_id"] == "entry-fallback"
    assert item["exit_intent_ids"] == ["exit-fallback"]
    assert item["trade_review_id"] is not None


def test_virtual_position_candidate_fallback_prevents_position_missing(tmp_path):
    db = _db(tmp_path)
    try:
        position = db.save_virtual_position(
            VirtualPosition(
                candidate_id=1,
                virtual_order_id=None,
                entry_price=10000,
                quantity=10,
                opened_at="2026-05-30T09:02:00",
                closed_at="2026-05-30T09:15:00",
                close_price=10300,
                close_reason="TAKE_PROFIT",
                max_return_pct=3.0,
                max_drawdown_pct=-0.5,
                realized_return_pct=3.0,
            )
        )
        db.save_runtime_order_intent(_intent("entry-position-fallback", virtual_order_id=None, virtual_position_id=None))
        db.save_trade_review(_review(virtual_order_id=None, virtual_position_id=position.id, final_status=ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value))
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["virtual_position_id"] == position.id
    assert "POSITION_MISSING" not in item["data_quality_issues"]


def test_missing_price_and_quantity_are_split_by_reason(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-bad-fields",
                status="DRY_RUN_REJECTED",
                reason="PRICE_INVALID",
                price=0,
                quantity=0,
                order_amount=0,
                safety={"ok": False, "reason": "PRICE_INVALID"},
                metadata={
                    "session_bucket": "OPEN",
                    "quantity_calculation_reason": "PRICE_INVALID",
                    "source": "themelab_flow",
                    "order_eligibility": "BUY_ELIGIBLE_PULLBACK",
                },
                virtual_order_id=None,
                virtual_position_id=None,
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert item["data_quality_issue_reasons"]["MISSING_PRICE"].startswith("QUANTITY_CALCULATION:")
    assert item["data_quality_issue_reasons"]["MISSING_QUANTITY"].startswith("QUANTITY_CALCULATION:")
    assert report["summary"]["data_quality"]["missing_price_reasons"][0]["reason"] == "QUANTITY_CALCULATION:PRICE_INVALID"
