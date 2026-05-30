from trading.strategy.models import TradeReview
from trading.strategy.runtime import _ReviewContext, _attach_dry_run_order_review_details


def test_trade_review_details_keep_entry_and_exit_dry_run_links():
    review = TradeReview(candidate_id=1, trade_date="2026-05-30", code="005930", final_status="OPEN")
    context = _ReviewContext(
        dry_run_entry_order_result={
            "intent_id": "intent-entry",
            "status": "DRY_RUN_ACCEPTED",
            "reason": "entry",
            "dedupe_key": "entry-key",
            "live_would_pass": False,
            "live_reject_reason": "GATEWAY_NOT_CONNECTED",
            "request": {"quantity": 7, "price": 70000},
            "safety": {"ok": True, "reason": "OK"},
        },
        dry_run_exit_order_results=[
            {
                "intent_id": "intent-exit-1",
                "status": "DRY_RUN_ACCEPTED",
                "reason": "TAKE_PROFIT",
                "dedupe_key": "exit-key-1",
                "live_would_pass": False,
                "live_reject_reason": "GATEWAY_NOT_CONNECTED",
                "request": {
                    "quantity": 5,
                    "price": 73500,
                    "exit_decision_id": 10,
                    "exit_decision_type": "TAKE_PROFIT",
                },
            },
            {
                "intent_id": "intent-exit-2",
                "status": "DRY_RUN_ACCEPTED",
                "reason": "TRAILING_STOP",
                "dedupe_key": "exit-key-2",
                "live_would_pass": False,
                "live_reject_reason": "GATEWAY_NOT_CONNECTED",
                "request": {
                    "quantity": 2,
                    "price": 72000,
                    "exit_decision_id": 11,
                    "exit_decision_type": "TRAILING_STOP",
                },
            },
        ],
    )

    _attach_dry_run_order_review_details(review, context)

    assert review.details["dry_run_entry_order_intent_id"] == "intent-entry"
    assert review.details["dry_run_exit_order_intent_ids"] == ["intent-exit-1", "intent-exit-2"]
    assert review.details["dry_run_exit_decision_ids"] == [10, 11]
    assert review.details["dry_run_exit_sell_quantity_total"] == 7
    assert review.details["dry_run_exit_sell_amount_total"] == 511500
    assert review.details["dry_run_order_intent_id"] == "intent-entry"
