from datetime import datetime, timedelta

from trading.theme_engine.leadership_handover import (
    LeadershipHandoverConfig,
    LeadershipHandoverEngine,
    ThemeLeadershipSnapshot,
    ThemeLeadershipStatus,
)
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


def test_leadership_handover_requires_persistent_challenger_confirmation():
    engine = LeadershipHandoverEngine(
        LeadershipHandoverConfig(min_confirm_sec=30, min_confirm_cycles=3, min_score_advantage=5.0, min_flow_share_advantage=0.01)
    )
    now = datetime(2026, 6, 19, 9, 20, 0)

    first, _ = engine.apply([_leader("ai", score=70, share=0.2)], now=now)
    second, _ = engine.apply(
        [_leader("battery", score=82, share=0.28), _leader("ai", score=70, share=0.2)],
        now=now + timedelta(seconds=5),
    )
    third, _ = engine.apply(
        [_leader("battery", score=83, share=0.29), _leader("ai", score=70, share=0.19)],
        now=now + timedelta(seconds=36),
    )
    fourth, _ = engine.apply(
        [_leader("battery", score=84, share=0.3), _leader("ai", score=70, share=0.18)],
        now=now + timedelta(seconds=66),
    )

    assert first[0].status == ThemeLeadershipStatus.INCUMBENT.value
    assert second[0].status == ThemeLeadershipStatus.TAKEOVER_PENDING.value
    assert third[0].status == ThemeLeadershipStatus.TAKEOVER_PENDING.value
    assert fourth[0].status == ThemeLeadershipStatus.TAKEOVER_CONFIRMED.value


def test_data_wait_theme_cannot_takeover_even_with_high_score():
    engine = LeadershipHandoverEngine(LeadershipHandoverConfig(min_score_advantage=1.0, min_flow_share_advantage=0.0))
    now = datetime(2026, 6, 19, 9, 20, 0)

    engine.apply([_leader("ai", score=60, share=0.2)], now=now)
    snapshots, _ = engine.apply(
        [
            _leader("data_wait", score=99, share=0.5, state=ThemeCoreState.DATA_WAIT.value),
            _leader("ai", score=60, share=0.2),
        ],
        now=now + timedelta(seconds=10),
    )

    by_theme = {item.theme_id: item for item in snapshots}
    assert by_theme["data_wait"].status == ThemeLeadershipStatus.NEUTRAL.value
    assert by_theme["ai"].status == ThemeLeadershipStatus.INCUMBENT.value


def test_incumbent_one_cycle_flow_collapse_stays_in_grace_before_losing():
    engine = LeadershipHandoverEngine(
        LeadershipHandoverConfig(
            losing_confirm_cycles=2,
            losing_confirm_sec=20,
            rotated_out_cooldown_sec=60,
            min_score_advantage=5.0,
            min_flow_share_advantage=0.01,
        )
    )
    now = datetime(2026, 6, 19, 9, 20, 0)

    engine.apply([_leader("ai", score=80, share=0.3, flow_delta=0.0)], now=now)
    grace, _ = engine.apply([_leader("ai", score=79, share=0.22, flow_delta=-0.06)], now=now + timedelta(seconds=5))
    losing, _ = engine.apply([_leader("ai", score=78, share=0.2, flow_delta=-0.07)], now=now + timedelta(seconds=25))

    assert grace[0].status == ThemeLeadershipStatus.INCUMBENT.value
    assert "INCUMBENT_GRACE_ACTIVE" in grace[0].handover_reason_codes
    assert losing[0].status == ThemeLeadershipStatus.LOSING_LEADERSHIP.value
    assert losing[0].losing_cycle_count >= 2


def test_losing_leadership_rotates_out_only_after_cooldown():
    engine = LeadershipHandoverEngine(
        LeadershipHandoverConfig(losing_confirm_cycles=1, losing_confirm_sec=0, rotated_out_cooldown_sec=30)
    )
    now = datetime(2026, 6, 19, 9, 20, 0)

    engine.apply([_leader("ai", score=80, share=0.3, flow_delta=0.0)], now=now)
    losing, _ = engine.apply([_leader("ai", score=60, share=0.2, flow_delta=-0.08)], now=now + timedelta(seconds=1))
    still_losing, _ = engine.apply([_leader("ai", score=0, share=0.0, flow_delta=-0.08, state=ThemeCoreState.DATA_WAIT.value)], now=now + timedelta(seconds=10))
    rotated, _ = engine.apply([_leader("ai", score=0, share=0.0, flow_delta=-0.08, state=ThemeCoreState.DATA_WAIT.value)], now=now + timedelta(seconds=35))

    assert losing[0].status == ThemeLeadershipStatus.LOSING_LEADERSHIP.value
    assert still_losing[0].status == ThemeLeadershipStatus.LOSING_LEADERSHIP.value
    assert rotated[0].status == ThemeLeadershipStatus.ROTATED_OUT.value


def _leader(theme_id: str, *, score: float, share: float, state: str = ThemeCoreState.SPREADING_THEME.value, flow_delta: float = 0.0):
    return ThemeLeadershipSnapshot(
        theme_id=theme_id,
        theme_name=theme_id,
        current_rank=1,
        leadership_score=score,
        flow_share=share,
        flow_share_delta=flow_delta,
        recent_flow_score=score,
        theme_state=ThemeStateSnapshot(theme_id=theme_id, theme_name=theme_id, theme_state=state, theme_score=score),
    )
