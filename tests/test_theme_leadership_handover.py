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


def _leader(theme_id: str, *, score: float, share: float, state: str = ThemeCoreState.SPREADING_THEME.value):
    return ThemeLeadershipSnapshot(
        theme_id=theme_id,
        theme_name=theme_id,
        current_rank=1,
        leadership_score=score,
        flow_share=share,
        recent_flow_score=score,
        theme_state=ThemeStateSnapshot(theme_id=theme_id, theme_name=theme_id, theme_state=state, theme_score=score),
    )
