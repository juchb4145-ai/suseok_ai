from pathlib import Path

from storage.db import TradingDatabase
from trading.strategy.models import ExitDecision, FillPolicy, PositionContextSnapshot, ReviewFinalStatus, TradeReview, VirtualPosition
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
    assert report["summary"]["weak_code_date_fallback_count"] >= 1


def test_candidate_instance_id_separates_same_code_same_day_lifecycles(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-morning",
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={
                    "candidate_instance_id": "ci-morning",
                    "candidate_generation_seq": 1,
                    "session_bucket": "OPEN",
                    "generation_reason": "initial_generation",
                },
            )
        )
        db.save_runtime_order_intent(
            _intent(
                "entry-afternoon",
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={
                    "candidate_instance_id": "ci-afternoon",
                    "candidate_generation_seq": 2,
                    "session_bucket": "AFTERNOON",
                    "generation_reason": "stale_re_detected",
                },
                created_at="2026-05-30T13:01:00",
            )
        )
        db.save_trade_review(
            _review(
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                details={"candidate_instance_id": "ci-afternoon", "candidate_generation_seq": 2, "session_bucket": "AFTERNOON"},
                review_key="r-afternoon",
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    by_instance = {item["candidate_instance_id"]: item for item in report["items"]}
    assert set(by_instance) == {"ci-morning", "ci-afternoon"}
    assert by_instance["ci-morning"]["entry_intent_id"] == "entry-morning"
    assert by_instance["ci-afternoon"]["entry_intent_id"] == "entry-afternoon"
    assert by_instance["ci-afternoon"]["trade_review_id"] is not None
    assert report["summary"]["exact_candidate_instance_match_count"] == 2
    assert report["summary"]["multi_generation_code_count"] == 1
    assert report["summary"]["avg_generation_per_code"] == 2
    assert report["summary"]["max_generation_per_code"] == 2
    assert report["summary"]["stale_re_detect_count"] == 1


def test_ambiguous_code_date_fallback_does_not_merge_multiple_instances(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-a",
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={"candidate_instance_id": "ci-a", "candidate_generation_seq": 1},
            )
        )
        db.save_runtime_order_intent(
            _intent(
                "entry-b",
                candidate_id=7,
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={"candidate_instance_id": "ci-b", "candidate_generation_seq": 2},
            )
        )
        db.save_runtime_order_intent(
            _intent(
                "exit-ambiguous",
                side="sell",
                order_phase="exit",
                candidate_id=None,
                virtual_order_id=None,
                virtual_position_id=None,
                trade_review_id=None,
                exit_decision_type="TRAILING_STOP",
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    ambiguous = [item for item in report["items"] if "AMBIGUOUS_CANDIDATE_LINK" in item["data_quality_issues"]]
    assert len(ambiguous) == 1
    assert ambiguous[0]["exit_intent_ids"] == ["exit-ambiguous"]
    assert ambiguous[0]["entry_intent_id"] == ""
    assert report["summary"]["ambiguous_candidate_link_count"] == 1


def test_virtual_position_id_link_has_priority_over_candidate_instance(tmp_path):
    db = _db(tmp_path)
    try:
        position = db.save_virtual_position(
            VirtualPosition(
                candidate_id=1,
                virtual_order_id=20,
                entry_price=10000,
                quantity=10,
                opened_at="2026-05-30T09:02:00",
                details={"candidate_instance_id": "ci-position", "candidate_instance_ids": ["ci-position"]},
            )
        )
        db.save_runtime_order_intent(
            _intent(
                "entry-ci-a",
                virtual_position_id=position.id,
                metadata={"candidate_instance_id": "ci-a", "candidate_generation_seq": 1},
            )
        )
        db.save_trade_review(
            _review(
                virtual_position_id=position.id,
                details={"candidate_instance_id": "ci-b", "candidate_generation_seq": 2},
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    assert report["summary"]["total_lifecycle_count"] == 1
    assert item["matched_by"] == "virtual_position_id"
    assert item["link_confidence"] == "HIGH"


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


def test_support_data_missing_and_coverage_are_reported(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-support-data-missing",
                status="DRY_RUN_REJECTED",
                reason="SUPPORT_DATA_MISSING",
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={
                    "support_missing_reason": "SUPPORT_DATA_MISSING",
                    "support_coverage": {
                        "recent_support_price_present": False,
                        "vwap_present": False,
                        "vwap_ready": False,
                        "minute_bar_present": False,
                        "minute_bar_count": 0,
                    },
                },
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    item = report["items"][0]
    quality = report["summary"]["data_quality"]
    assert item["details"]["support_missing_reason"] == "SUPPORT_DATA_MISSING"
    assert "SUPPORT_DATA_MISSING" in item["data_quality_issues"]
    assert quality["support_missing_reasons"][0]["reason"] == "SUPPORT_DATA_MISSING"
    assert quality["support_coverage"]["sample_count"] == 1
    assert quality["support_coverage"]["vwap_present_count"] == 0
    assert quality["support_vwap_coverage"]["support_missing_count_by_reason"][0]["reason"] == "SUPPORT_DATA_MISSING"


def test_support_diagnostic_only_later_rallied_is_split_by_reason(tmp_path):
    db = _db(tmp_path)
    try:
        db.save_runtime_order_intent(
            _intent(
                "entry-support-structural",
                status="DRY_RUN_REJECTED",
                reason="SUPPORT_STRUCTURALLY_MISSING",
                virtual_order_id=None,
                virtual_position_id=None,
                metadata={
                    "support_missing_reason": "SUPPORT_STRUCTURALLY_MISSING",
                    "support_coverage": {
                        "vwap_present": True,
                        "vwap_ready": True,
                        "minute_bar_present": True,
                        "minute_bar_quality_status": "VALID_RECENT_MINUTE_BARS",
                        "support_source_presence": {"vwap": True},
                    },
                },
            )
        )
        db.save_trade_review(
            _review(
                virtual_order_id=None,
                virtual_position_id=None,
                max_return_20m=3.5,
                details={"candidate_instance_id": "", "support_missing_reason": "SUPPORT_STRUCTURALLY_MISSING"},
            )
        )
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    coverage = report["summary"]["data_quality"]["support_vwap_coverage"]
    assert coverage["diagnostic_only_due_to_support_count"] == 1
    assert coverage["diagnostic_only_later_rallied_count"] == 1
    assert coverage["SUPPORT_STRUCTURALLY_MISSING_AND_RALLIED"] == 1


def test_position_context_history_is_reported(tmp_path):
    db = _db(tmp_path)
    try:
        position = db.save_virtual_position(
            VirtualPosition(
                candidate_id=1,
                virtual_order_id=20,
                entry_price=10000,
                quantity=10,
                opened_at="2026-05-30T09:02:00",
                closed_at="2026-05-30T09:20:00",
                close_price=9700,
                close_reason="THEME_WEAK_EXIT",
                realized_return_pct=-3.0,
            )
        )
        for reason, captured_at in [
            ("ENTRY", "2026-05-30T09:02:00"),
            ("HOLDING_EVAL", "2026-05-30T09:10:00"),
            ("EXIT_EVAL", "2026-05-30T09:20:00"),
        ]:
            db.save_position_context_snapshot(
                PositionContextSnapshot(
                    position_id=position.id,
                    candidate_id=1,
                    candidate_instance_id="ci-context",
                    code="005930",
                    trade_date="2026-05-30",
                    captured_at=captured_at,
                    capture_reason=reason,
                    theme_id="theme-ai",
                    theme_name="AI",
                    theme_score=60.0,
                    theme_status="WEAK_THEME",
                    leader_count=1,
                    strong_count=2,
                    breadth_status="WEAK",
                    index_market="KOSPI",
                    index_status="INDEX_WEAK",
                )
            )
        db.save_exit_decision(
            ExitDecision(
                virtual_position_id=position.id,
                decision_type="THEME_WEAK_EXIT",
                trigger_price=9700,
                filled=True,
                fill_policy=FillPolicy.NORMAL,
                reason_codes=["THEME_WEAK"],
                details={
                    "exit_confidence": "HIGH",
                    "context_history_count": 2,
                    "theme_score_delta": -12.0,
                    "leader_count_delta": -2,
                    "index_status_deterioration": True,
                    "context_limited_reason": "",
                },
                created_at="2026-05-30T09:20:00",
            )
        )
        db.save_runtime_order_intent(_intent("entry-context", virtual_position_id=position.id))
        db.save_trade_review(_review(virtual_position_id=position.id))
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    summary = report["summary"]
    assert summary["positions_with_entry_context_count"] == 1
    assert summary["positions_with_holding_context_count"] == 1
    assert summary["positions_with_exit_context_count"] == 1
    assert summary["position_context_coverage_pct"] == 1.0
    assert summary["context_history_count_distribution"][0]["history_count"] == "3"
    assert summary["context_risk_exit_confidence_by_type"]["THEME_WEAK_EXIT"][0]["confidence"] == "HIGH"
    assert summary["theme_score_delta_distribution"][0]["bucket"] == "<=-10"
    assert summary["leader_count_delta_distribution"][0]["bucket"] == "-2..-1"
    assert summary["index_status_deterioration_count"] == 1


def test_position_context_data_limited_and_pruning_are_reported(tmp_path):
    db = _db(tmp_path)
    try:
        position = db.save_virtual_position(
            VirtualPosition(
                candidate_id=1,
                virtual_order_id=20,
                entry_price=10000,
                quantity=10,
                opened_at="2026-05-30T09:02:00",
            )
        )
        db.save_position_context_snapshot(
            PositionContextSnapshot(
                position_id=position.id,
                candidate_id=1,
                code="005930",
                trade_date="2026-05-30",
                captured_at="2026-04-01T09:00:00",
                capture_reason="ENTRY",
            )
        )
        db.save_exit_decision(
            ExitDecision(
                virtual_position_id=position.id,
                decision_type="LEADER_COLLAPSE_EXIT",
                trigger_price=9900,
                filled=False,
                fill_policy=FillPolicy.NORMAL,
                reason_codes=["LEADER_COLLAPSE", "DATA_LIMITED_CONTEXT"],
                details={
                    "exit_confidence": "LOW",
                    "context_limited_reason": "DATA_LIMITED_CONTEXT",
                    "context_history_count": 0,
                },
                created_at="2026-05-30T09:20:00",
            )
        )
        db.save_runtime_order_intent(_intent("entry-prune", virtual_position_id=position.id))
        db.save_trade_review(_review(virtual_position_id=position.id))
        prune = db.prune_position_context_history(cutoff_at="2026-05-01T00:00:00", batch_size=1000, created_at="2026-05-30T16:00:00")
        report = DryRunPerformanceAnalyzer(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    assert prune["pruned_context_history_rows"] == 1
    assert report["summary"]["data_limited_context_count"] == 1
    assert report["summary"]["low_confidence_exit_count"] == 1
    assert report["summary"]["context_history_prune"]["pruned_context_history_rows"] == 1
    assert report["summary"]["context_history_prune"]["retained_context_history_rows"] == 0
