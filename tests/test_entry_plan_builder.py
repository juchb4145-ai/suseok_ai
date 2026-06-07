from copy import deepcopy
from datetime import datetime

from trading.strategy.entry import EntryPlanBuilder, TickSizeProvider
from trading.strategy.models import GateDecision, IndicatorSnapshot, StrategyProfile, VirtualOrderStatus
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.virtual_orders import VirtualOrderService


class FixedTickProvider(TickSizeProvider):
    def tick_size(self, price: int) -> int:
        return 7


def gate_result(
    strategy_eligible=True,
    profile=StrategyProfile.KOSDAQ_THEME_PROFILE,
    support_price=9_700,
    price=9_750,
    final_grade="A",
    support_candidates=None,
    ready_type="",
    support_ready=True,
    support_ready_reason="",
):
    return GatePipelineResult(
        candidate_id=1,
        code="111111",
        theme_id="robot",
        final_grade=final_grade,
        final_score=88.0,
        strategy_eligible=strategy_eligible,
        decisions=[
            GateDecision(
                gate_name="StockPullbackEntryGate",
                passed=True,
                details={
                    "profile": profile.value,
                    "nearest_support": "vwap",
                    "nearest_support_price": support_price,
                    "selected_support_source": "vwap",
                    "selected_support_price": support_price or 0,
                    "selected_support_ready": support_ready,
                    "selected_support_ready_reason": support_ready_reason,
                    "support_readiness_reason_codes": [support_ready_reason] if support_ready_reason else [],
                    "support_distance_pct": 0.2,
                    "support_candidates": support_candidates or {},
                    "ready_type": ready_type,
                },
            )
        ],
        snapshot=IndicatorSnapshot(candidate_id=1, code="111111", price=price),
        details={"theme_name": "Robot", "cap_rules_applied": [], "ready_type": ready_type},
    )


def test_strategy_ineligible_result_does_not_create_entry_plan():
    assert EntryPlanBuilder().build(gate_result(strategy_eligible=False)) is None


def test_entry_risk_temp_or_final_result_does_not_create_entry_plan():
    temp = gate_result(strategy_eligible=False)
    temp.details["sub_status"] = "ENTRY_RISK_TEMP_WAIT"
    temp.details["entry_risk_reason_codes"] = ["VI_COOLDOWN", "ENTRY_RISK_TEMP_WAIT"]
    final = gate_result(strategy_eligible=False)
    final.details["sub_status"] = "ENTRY_RISK_FINAL_BLOCK"
    final.details["entry_risk_reason_codes"] = ["VI_ACTIVE", "ENTRY_RISK_FINAL_BLOCK"]

    assert EntryPlanBuilder().build(temp) is None
    assert EntryPlanBuilder().build(final) is None


def test_entry_risk_recovered_scales_split_weight_only_after_recovery():
    result = gate_result()
    result.details["entry_risk_action"] = "recovered"
    result.details["entry_risk_reason_codes"] = ["ENTRY_RISK_RECOVERED", "RISK_ADJUST_POSITION_SIZE"]
    result.details["position_size_multiplier"] = 0.25
    result.decisions[0].details["entry_risk_action"] = "recovered"
    result.decisions[0].details["entry_risk_reason_codes"] = ["ENTRY_RISK_RECOVERED", "RISK_ADJUST_POSITION_SIZE"]

    plan = EntryPlanBuilder().build(result)

    assert plan is not None
    assert [leg["weight_pct"] for leg in plan.split_plan] == [10.0, 7.5, 7.5]
    assert plan.cancel_condition["entry_risk_action"] == "recovered"
    assert plan.cancel_condition["split_policy"]["position_size_multiplier"] == 0.25


def test_support_missing_plan_is_diagnostic_only_and_not_submittable():
    result = gate_result(support_price=None)

    plan = EntryPlanBuilder().build(result, datetime(2026, 5, 29, 9, 0))

    assert plan is not None
    assert plan.base_price_source == "current_price_fallback"
    assert plan.cancel_condition["submittable"] is False
    assert plan.cancel_condition["diagnostic_only"] is True
    assert plan.cancel_condition["reason"] == "SUPPORT_DATA_MISSING"
    assert plan.cancel_condition["support_missing_reason"] == "SUPPORT_DATA_MISSING"


def test_support_structurally_missing_is_split_from_data_missing():
    result = gate_result(support_price=None)
    result.decisions[0].details["vwap"] = 9_700
    result.decisions[0].details["vwap_ready"] = True

    plan = EntryPlanBuilder().build(result, datetime(2026, 5, 29, 9, 0))

    assert plan.cancel_condition["submittable"] is False
    assert plan.cancel_condition["diagnostic_only"] is True
    assert plan.cancel_condition["reason"] == "SUPPORT_STRUCTURALLY_MISSING"


def test_max_chase_exceeded_marks_plan_not_submittable():
    result = gate_result(support_price=9_000, price=10_000)

    plan = EntryPlanBuilder().build(result)

    assert plan.cancel_condition["submittable"] is False
    assert plan.cancel_condition["reason"] == "max_chase_exceeded"
    assert plan.cancel_condition["limit_vs_current_pct"] > plan.max_chase_pct


def test_profile_specific_timeout_and_max_chase_values():
    kosdaq = EntryPlanBuilder().build(gate_result(profile=StrategyProfile.KOSDAQ_THEME_PROFILE))
    kospi = EntryPlanBuilder().build(gate_result(profile=StrategyProfile.KOSPI_LEADER_PROFILE))
    signal = EntryPlanBuilder().build(gate_result(profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE))

    assert kosdaq.max_chase_pct == 0.7
    assert kosdaq.order_timeout_sec == 300
    assert kospi.max_chase_pct == 0.4
    assert kospi.order_timeout_sec == 180
    assert signal.max_chase_pct == 0.4
    assert signal.order_timeout_sec == 180


def test_tick_provider_is_used_for_limit_price():
    plan = EntryPlanBuilder(FixedTickProvider()).build(gate_result(support_price=9_700))

    assert plan.limit_price == 9_707
    assert plan.tick_offset == 1


def test_split_plan_uses_grade_weights_and_lower_supports():
    plan = EntryPlanBuilder().build(
        gate_result(
            final_grade="B+",
            support_price=9_700,
            support_candidates={
                "vwap": 9_700,
                "base_line_120": 9_600,
                "envelope_mid": 9_500,
            },
        )
    )

    assert [leg["weight_pct"] for leg in plan.split_plan] == [50, 30, 20]
    assert [leg["support_name"] for leg in plan.split_plan] == ["vwap", "base_line_120", "envelope_mid"]
    assert all(leg["submittable"] for leg in plan.split_plan)
    assert plan.split_plan[1]["requires_previous_leg"] is True


def test_split_plan_marks_later_legs_unsubmittable_when_supports_are_missing():
    plan = EntryPlanBuilder().build(gate_result(final_grade="B", support_candidates={"vwap": 9_700}))

    assert [leg["weight_pct"] for leg in plan.split_plan] == [60, 25, 15]
    assert plan.split_plan[0]["submittable"] is True
    assert plan.split_plan[1]["submittable"] is False
    assert plan.split_plan[2]["reason"] == "SUPPORT_STRUCTURALLY_MISSING"


def test_support_not_ready_plan_is_diagnostic_only_and_not_submittable():
    result = gate_result(support_ready=False, support_ready_reason="VWAP_NOT_READY")

    plan = EntryPlanBuilder().build(result)

    assert plan.cancel_condition["submittable"] is False
    assert plan.cancel_condition["diagnostic_only"] is True
    assert plan.cancel_condition["reason"] == "VWAP_NOT_READY"
    assert plan.split_plan[0]["submittable"] is False
    assert plan.split_plan[0]["reason"] == "VWAP_NOT_READY"


def test_ready_early_small_only_first_leg_is_submittable():
    plan = EntryPlanBuilder().build(
        gate_result(
            final_grade="A",
            ready_type="READY_EARLY_SMALL",
            support_candidates={
                "vwap": 9_700,
                "base_line_120": 9_600,
                "envelope_mid": 9_500,
            },
        )
    )

    assert plan.cancel_condition["ready_type"] == "READY_EARLY_SMALL"
    assert plan.split_plan[0]["submittable"] is True
    assert plan.split_plan[1]["submittable"] is False
    assert plan.split_plan[1]["pending_after_first_fill"] is True
    assert plan.split_plan[2]["reason"] == "early_small_later_leg_pending"


def test_ready_early_small_does_not_submit_second_leg_after_first_fill():
    plan = EntryPlanBuilder().build(
        gate_result(
            ready_type="READY_EARLY_SMALL",
            support_candidates={
                "vwap": 9_700,
                "base_line_120": 9_600,
                "envelope_mid": 9_500,
            },
        )
    )
    service = VirtualOrderService()
    first = service.submit_virtual_order(plan, datetime(2026, 5, 29, 9, 0))
    first.order.status = VirtualOrderStatus.FILLED

    second = service.submit_virtual_order(plan, datetime(2026, 5, 29, 9, 5))

    assert first.submitted is True
    assert second.submitted is False
    assert second.rejected_reason == "no_submittable_leg"


def test_entry_plan_contains_review_context_and_does_not_mutate_result():
    result = gate_result()
    before_snapshot = result.snapshot.to_dict()
    before_decision_details = deepcopy(result.decisions[0].details)

    plan = EntryPlanBuilder().build(result, datetime(2026, 5, 29, 9, 0))

    assert plan.cancel_condition["theme_id"] == "robot"
    assert plan.cancel_condition["theme_name"] == "Robot"
    assert plan.cancel_condition["strategy_profile"] == StrategyProfile.KOSDAQ_THEME_PROFILE.value
    assert plan.cancel_condition["final_grade"] == "A"
    assert plan.cancel_condition["current_price_at_plan"] == 9_750
    assert plan.cancel_condition["support_price"] == 9_700
    assert plan.cancel_condition["gate_result_key"] == "1:111111:robot:A"
    assert result.snapshot.to_dict() == before_snapshot
    assert result.decisions[0].details == before_decision_details


def test_entry_plan_carries_late_chase_comparison_metadata():
    result = gate_result()
    result.decisions[0].details["late_chase_diagnostics"] = {
        "late_chase_score": 100.0,
        "late_chase_level": "soft_block",
        "reason_codes": ["LATE_CHASE", "SOFT_BLOCK_ONLY"],
    }
    result.decisions[0].details["late_chase_score"] = 100.0
    result.decisions[0].details["late_chase_level"] = "soft_block"
    result.decisions[0].details["comparison_reason_codes"] = ["LATE_CHASE", "SOFT_BLOCK_ONLY"]

    plan = EntryPlanBuilder().build(result)

    assert plan.cancel_condition["late_chase_diagnostics"]["late_chase_level"] == "soft_block"
    assert plan.cancel_condition["late_chase_score"] == 100.0
    assert plan.cancel_condition["comparison_reason_codes"] == ["LATE_CHASE", "SOFT_BLOCK_ONLY"]
