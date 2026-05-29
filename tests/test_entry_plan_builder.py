from copy import deepcopy
from datetime import datetime

from trading.strategy.entry import EntryPlanBuilder, TickSizeProvider
from trading.strategy.models import GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.pipeline import GatePipelineResult


class FixedTickProvider(TickSizeProvider):
    def tick_size(self, price: int) -> int:
        return 7


def gate_result(strategy_eligible=True, profile=StrategyProfile.KOSDAQ_THEME_PROFILE, support_price=9_700, price=9_750):
    return GatePipelineResult(
        candidate_id=1,
        code="111111",
        theme_id="robot",
        final_grade="A",
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
                    "support_distance_pct": 0.2,
                },
            )
        ],
        snapshot=IndicatorSnapshot(candidate_id=1, code="111111", price=price),
        details={"theme_name": "Robot", "cap_rules_applied": []},
    )


def test_strategy_ineligible_result_does_not_create_entry_plan():
    assert EntryPlanBuilder().build(gate_result(strategy_eligible=False)) is None


def test_support_missing_plan_is_diagnostic_only_and_not_submittable():
    result = gate_result(support_price=None)

    plan = EntryPlanBuilder().build(result, datetime(2026, 5, 29, 9, 0))

    assert plan is not None
    assert plan.base_price_source == "current_price_fallback"
    assert plan.cancel_condition["submittable"] is False
    assert plan.cancel_condition["diagnostic_only"] is True
    assert plan.cancel_condition["reason"] == "support_missing"


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
