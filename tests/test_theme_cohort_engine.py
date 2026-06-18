from trading.theme_engine.cohort import ThemeCohortEngine
from trading.theme_engine.models import ThemeMembership
from trading.theme_engine.signals import LiveSeedSignal, SeedSourceType, ThemeDataWaitReason


def test_cohort_engine_calculates_theme_cohesion_without_condition_profiles():
    cohorts = ThemeCohortEngine().build(
        [_theme("robot", ["000001", "000002", "000003", "000004", "000005"])],
        [
            _signal("000001", 6.0, 10_000_000_000),
            _signal("000002", 5.3, 8_000_000_000),
            _signal("000003", 3.4, 4_000_000_000),
        ],
    )

    cohort = cohorts[0]
    assert cohort.theme_id == "robot"
    assert cohort.seed_member_count == 3
    assert cohort.strong_count == 3
    assert cohort.leader_count == 2
    assert cohort.cohesion_passed is True
    assert cohort.data_quality_reason == ""


def test_single_burst_stock_is_not_theme_cohesion():
    cohort = ThemeCohortEngine().build(
        [_theme("bio", ["000001", "000002", "000003", "000004", "000005"])],
        [_signal("000001", 8.1, 12_000_000_000)],
    )[0]

    assert cohort.leader_count == 1
    assert cohort.cohesion_passed is False
    assert cohort.leader_only_candidate is True
    assert "SINGLE_LEADER_ONLY_CANDIDATE" in cohort.reason_codes


def test_realtime_coverage_shortage_is_data_wait_reason_not_weak_evidence():
    cohort = ThemeCohortEngine().build(
        [_theme("display", ["000001", "000002", "000003", "000004"])],
        [
            LiveSeedSignal(
                code="000001",
                source_types=(SeedSourceType.OPT10032.value,),
                change_rate_pct=4.0,
                turnover_krw=2_000_000_000,
                realtime_valid=False,
                tr_backfill_valid=True,
            )
        ],
    )[0]

    assert cohort.realtime_valid_count == 0
    assert cohort.data_quality_reason == ThemeDataWaitReason.TR_BACKFILL_ONLY.value


def _theme(theme_id: str, codes: list[str]):
    return (
        theme_id,
        theme_id.title(),
        [
            ThemeMembership(
                theme_id=theme_id,
                stock_code=code,
                stock_name=f"stock-{code}",
                membership_score=0.9,
                active=True,
                trade_eligible=True,
            )
            for code in codes
        ],
    )


def _signal(code: str, change: float, turnover: float) -> LiveSeedSignal:
    return LiveSeedSignal(
        code=code,
        name=f"stock-{code}",
        source_types=(SeedSourceType.OPT10032.value, SeedSourceType.REALTIME_TICK.value),
        change_rate_pct=change,
        turnover_krw=turnover,
        turnover_speed=turnover / 5,
        execution_strength=150,
        realtime_valid=True,
    )
