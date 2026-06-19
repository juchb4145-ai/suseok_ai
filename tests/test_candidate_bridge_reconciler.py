from trading.theme_engine.candidate_bridge_reconciler import CandidateBridgeSourceReconciler
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, TradeStockRole
from trading.theme_engine.signals import LiveSeedSignal
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


def test_candidate_bridge_reconciler_emits_include_then_source_remove_only():
    reconciler = CandidateBridgeSourceReconciler()
    state = ThemeStateSnapshot(theme_id="ai", theme_name="AI", theme_state=ThemeCoreState.LEADING_THEME.value)

    first = reconciler.reconcile(
        [_decision("000001", TradeStockRole.LEADER_CONFIRMED.value, RawStockRole.LEADER.value, state)],
        trade_date="2026-06-19",
        detected_at="2026-06-19T09:20:00",
    )
    second = reconciler.reconcile([], trade_date="2026-06-19", detected_at="2026-06-19T09:21:00")

    assert first.included_count == 1
    assert first.include_events[0].raw_payload["event_type"] == "include"
    assert first.include_events[0].raw_payload["ready_allowed"] is False
    assert second.removed_count == 1
    assert second.remove_events[0].raw_payload["event_type"] == "remove"
    assert second.remove_events[0].raw_payload["order_intent_allowed"] is False


def test_candidate_bridge_reconciler_blocks_leader_only_followers():
    reconciler = CandidateBridgeSourceReconciler()
    state = ThemeStateSnapshot(theme_id="ai", theme_name="AI", theme_state=ThemeCoreState.LEADER_ONLY_THEME.value)

    result = reconciler.reconcile(
        [_decision("000002", TradeStockRole.FOLLOWER_BLOCKED_LEADER_ONLY.value, RawStockRole.FOLLOWER.value, state)],
        trade_date="2026-06-19",
    )

    assert result.included_count == 0
    assert result.active_state == ()


def _decision(code: str, trade_role: str, raw_role: str, state: ThemeStateSnapshot) -> StockRoleDecision:
    return StockRoleDecision(
        code=code,
        name=f"stock-{code}",
        theme_id=state.theme_id,
        theme_name=state.theme_name,
        raw_role=raw_role,
        trade_role=trade_role,
        role_score=90.0,
        signal=LiveSeedSignal(code=code),
        theme_state=state,
    )
