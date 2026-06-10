from __future__ import annotations

from trading_app.promotion_controller import (
    HOLD_ACTION,
    PROMOTE_ACTION,
    PromotionController,
    PromotionControllerConfig,
    build_promotion_evidence,
)


def test_build_evidence_counts_realtime_low_missed_and_order_errors():
    outcomes = [
        _outcome("d1", "2026-06-01", "EARLY_OPPORTUNITY_LOSS", ["WAIT_DATA_REALTIME_RELIABILITY_LOW"], bucket="LOW", ret=4.0),
        _outcome("d2", "2026-06-01", "GOOD_READY", ["READY_PULLBACK"], bucket="HIGH", ret=1.2),
        _outcome("d3", "2026-06-02", "EARLY_FALSE_POSITIVE", ["READY_PULLBACK"], bucket="HIGH", ret=-1.5),
    ]
    intents = [
        {"intent_id": "i1", "trade_date": "2026-06-01", "status": "ACCEPTED", "metadata": {"realtime_reliability_bucket": "HIGH"}},
        {"intent_id": "i2", "trade_date": "2026-06-01", "status": "REJECTED", "metadata": {"reason_codes": ["ORDER_REJECTED"]}},
    ]

    evidence = build_promotion_evidence(
        policy_id="theme_lab_realtime_reliability_gate",
        current_stage="dry_run",
        decision_outcomes=outcomes,
        runtime_order_intents=intents,
    )

    assert evidence.decision_count == 3
    assert evidence.trade_day_count == 2
    assert evidence.realtime_low_missed_count == 1
    assert evidence.order_error_count == 1
    assert evidence.outcome_counts["EARLY_OPPORTUNITY_LOSS"] == 1
    assert evidence.realtime_bucket_counts["HIGH"] == 3


def test_observe_promotes_to_dry_run_with_rolling_intraday_evidence():
    outcomes = [_outcome(f"good-{idx}", "2026-06-01", "GOOD_READY", ["READY_PULLBACK"], bucket="HIGH", ret=0.4) for idx in range(45)]
    outcomes += [_outcome(f"block-{idx}", "2026-06-01", "GOOD_BLOCK", ["REALTIME_RELIABILITY_LOW"], bucket="LOW", ret=-0.2) for idx in range(10)]

    evidence = build_promotion_evidence(
        policy_id="theme_lab_realtime_reliability_gate",
        current_stage="observe",
        decision_outcomes=outcomes,
    )
    decision = PromotionController().evaluate(evidence)

    assert decision.action == PROMOTE_ACTION
    assert decision.recommended_stage == "dry_run"
    assert decision.eligible is True
    assert decision.blockers == []
    assert decision.metrics["decision_count"] == 55


def test_dry_run_holds_when_medium_policy_has_too_many_missed_realtime_low_cases():
    outcomes = [_outcome(f"good-{idx}", "2026-06-01", "GOOD_READY", ["READY_PULLBACK"], bucket="HIGH", ret=0.3) for idx in range(90)]
    outcomes += [
        _outcome(
            f"missed-{idx}",
            "2026-06-02",
            "EARLY_OPPORTUNITY_LOSS",
            ["WAIT_DATA_REALTIME_RELIABILITY_LOW"],
            bucket="LOW",
            ret=3.5,
        )
        for idx in range(15)
    ]
    intents = [{"intent_id": f"i{idx}", "status": "ACCEPTED", "metadata": {"realtime_reliability_bucket": "HIGH"}} for idx in range(35)]

    evidence = build_promotion_evidence(
        policy_id="theme_lab_realtime_reliability_gate",
        current_stage="dry_run",
        decision_outcomes=outcomes,
        runtime_order_intents=intents,
    )
    decision = PromotionController().evaluate(evidence)

    assert decision.action == HOLD_ACTION
    assert decision.recommended_stage == "dry_run"
    assert "REALTIME_LOW_MISSED_RATE_HIGH" in decision.blockers
    assert decision.metrics["realtime_low_missed_count"] == 15


def test_live_sim_to_real_micro_requires_operator_approval_even_when_metrics_pass():
    outcomes = [
        _outcome(f"good-{idx}", f"2026-06-{idx % 3 + 1:02d}", "GOOD_READY", ["READY_PULLBACK"], bucket="HIGH", ret=0.4)
        for idx in range(180)
    ]
    intents = [{"intent_id": f"i{idx}", "status": "ACCEPTED", "metadata": {"realtime_reliability_bucket": "HIGH"}} for idx in range(60)]
    live_orders = [{"order_id": f"l{idx}", "order_status": "FILLED", "filled_qty": 1} for idx in range(25)]

    evidence = build_promotion_evidence(
        policy_id="theme_lab_realtime_reliability_gate",
        current_stage="live_sim",
        decision_outcomes=outcomes,
        runtime_order_intents=intents,
        live_sim_orders=live_orders,
    )
    blocked = PromotionController().evaluate(evidence)
    approved = PromotionController(config=PromotionControllerConfig(allow_real_micro=True)).evaluate(evidence)

    assert blocked.action == HOLD_ACTION
    assert "REAL_MICRO_REQUIRES_OPERATOR_APPROVAL" in blocked.blockers
    assert approved.action == PROMOTE_ACTION
    assert approved.recommended_stage == "real_micro"
    assert approved.rollout_plan["order_notional_krw"] == 50000
    assert approved.rollout_plan["requires_operator_approval"] is True


def test_kill_switch_blocks_promotion():
    evidence = build_promotion_evidence(
        policy_id="theme_lab_realtime_reliability_gate",
        current_stage="observe",
        decision_outcomes=[
            _outcome(f"good-{idx}", "2026-06-01", "GOOD_READY", ["READY_PULLBACK"], bucket="HIGH", ret=0.4)
            for idx in range(60)
        ],
    )

    decision = PromotionController(config=PromotionControllerConfig(kill_switch_active=True)).evaluate(evidence)

    assert decision.action == HOLD_ACTION
    assert "KILL_SWITCH_ACTIVE" in decision.blockers
    assert decision.confidence == 0.0


def _outcome(
    decision_id: str,
    trade_date: str,
    label: str,
    reason_codes: list[str],
    *,
    bucket: str,
    ret: float,
) -> dict:
    return {
        "decision_id": decision_id,
        "outcome_id": f"outcome:{decision_id}:900",
        "trade_date": trade_date,
        "outcome_label": label,
        "current_return_pct": ret,
        "reason_codes": reason_codes,
        "metadata": {
            "realtime_reliability_bucket": bucket,
            "reason_codes": reason_codes,
        },
    }
