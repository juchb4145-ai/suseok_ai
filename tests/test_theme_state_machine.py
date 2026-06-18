from trading.theme_engine.cohort import ThemeCohortSnapshot
from trading.theme_engine.signals import ThemeDataWaitReason
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateMachine


def test_leading_theme_requires_two_persistent_cycles():
    machine = ThemeStateMachine()
    cohort = _cohort()

    first = machine.apply([cohort])[0]
    second = machine.apply([cohort])[0]

    assert first.theme_state == ThemeCoreState.SPREADING_THEME.value
    assert "LEADING_REQUIRES_PERSISTENCE" in first.reason_codes
    assert second.theme_state == ThemeCoreState.LEADING_THEME.value
    assert "LEADING_PERSISTENCE_CONFIRMED" in second.reason_codes


def test_data_wait_theme_stays_data_wait_not_weak_theme():
    machine = ThemeStateMachine()
    cohort = ThemeCohortSnapshot(
        theme_id="display",
        theme_name="Display",
        member_count=4,
        seed_member_count=1,
        realtime_valid_count=0,
        data_quality_reason=ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value,
        reason_codes=(ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value,),
    )

    state = machine.apply([cohort])[0]

    assert state.theme_state == ThemeCoreState.DATA_WAIT.value
    assert state.theme_state != ThemeCoreState.WEAK_THEME.value
    assert state.data_quality_reason == ThemeDataWaitReason.REALTIME_COVERAGE_LOW.value


def test_single_leader_cohort_classifies_as_leader_only_not_leading():
    machine = ThemeStateMachine()
    cohort = _cohort(
        strong_count=1,
        leader_count=1,
        cohesion_passed=False,
        leader_only_candidate=True,
        theme_turnover_krw=30_000_000_000,
    )

    state = machine.apply([cohort])[0]

    assert state.theme_state == ThemeCoreState.LEADER_ONLY_THEME.value
    assert state.theme_state != ThemeCoreState.LEADING_THEME.value


def _cohort(
    *,
    strong_count: int = 3,
    leader_count: int = 2,
    cohesion_passed: bool = True,
    leader_only_candidate: bool = False,
    theme_turnover_krw: float = 30_000_000_000,
) -> ThemeCohortSnapshot:
    return ThemeCohortSnapshot(
        theme_id="ai",
        theme_name="AI",
        member_count=5,
        seed_member_count=5,
        realtime_valid_count=5,
        strong_count=strong_count,
        leader_count=leader_count,
        strong_ratio=strong_count / 5,
        leader_ratio=leader_count / 5,
        theme_turnover_krw=theme_turnover_krw,
        weighted_return_pct=5.5,
        coverage_ratio=1.0,
        leader_symbol="000001",
        co_leader_symbols=("000002",),
        cohesion_passed=cohesion_passed,
        leader_only_candidate=leader_only_candidate,
    )
