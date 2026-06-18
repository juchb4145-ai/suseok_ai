from trading.theme_engine.expansion import FocusedExpansionConfig, FocusedExpansionPlanner
from trading.theme_engine.roles import RawStockRole, StockRoleDecision, TradeStockRole
from trading.theme_engine.signals import LiveSeedSignal
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


def test_focused_expansion_planner_selects_confirmed_roles_for_eligible_themes_only():
    theme_state = _theme_state(ThemeCoreState.SPREADING_THEME.value, theme_id="ai")
    watch_state = _theme_state(ThemeCoreState.WATCH_THEME.value, theme_id="weak")
    decisions = [
        _decision("000001", TradeStockRole.LEADER_CONFIRMED.value, RawStockRole.LEADER.value, theme_state, score=90),
        _decision("000002", TradeStockRole.LATE_LAGGARD_BLOCKED.value, RawStockRole.LATE_LAGGARD.value, theme_state, score=50),
        _decision("000003", TradeStockRole.LEADER_CONFIRMED.value, RawStockRole.LEADER.value, watch_state, score=80),
    ]

    plan = FocusedExpansionPlanner().plan([theme_state, watch_state], decisions)

    assert [target.code for target in plan.targets] == ["000001"]
    assert {target.code for target in plan.excluded} == {"000002", "000003"}
    assert any("TRADE_ROLE_BLOCKED" in target.reason_codes for target in plan.excluded if target.code == "000002")
    assert any("THEME_NOT_EXPANSION_ELIGIBLE" in target.reason_codes for target in plan.excluded if target.code == "000003")


def test_focused_expansion_planner_limits_per_theme_and_kosdaq_risk():
    theme_state = _theme_state(ThemeCoreState.LEADING_THEME.value)
    decisions = [
        _decision("000001", TradeStockRole.LEADER_CONFIRMED.value, RawStockRole.LEADER.value, theme_state, score=90),
        _decision("000002", TradeStockRole.CO_LEADER_CONFIRMED.value, RawStockRole.CO_LEADER.value, theme_state, score=80),
        _decision(
            "000003",
            TradeStockRole.CO_LEADER_CONFIRMED.value,
            RawStockRole.CO_LEADER.value,
            theme_state,
            score=70,
            market="KOSDAQ",
        ),
    ]

    plan = FocusedExpansionPlanner(FocusedExpansionConfig(max_per_theme=2)).plan(
        [theme_state],
        decisions,
        kosdaq_risk_state="RISK_OFF",
    )

    assert [target.code for target in plan.targets] == ["000001", "000002"]
    assert plan.excluded[0].code == "000003"
    assert "KOSDAQ_RISK_EXPANSION_REDUCED" in plan.excluded[0].reason_codes


def _theme_state(state: str, *, theme_id: str = "ai") -> ThemeStateSnapshot:
    return ThemeStateSnapshot(theme_id=theme_id, theme_name=theme_id.title(), theme_state=state, leader_symbol="000001")


def _decision(
    code: str,
    trade_role: str,
    raw_role: str,
    state: ThemeStateSnapshot,
    *,
    score: float,
    market: str = "KOSPI",
) -> StockRoleDecision:
    return StockRoleDecision(
        code=code,
        name=f"stock-{code}",
        theme_id=state.theme_id,
        theme_name=state.theme_name,
        raw_role=raw_role,
        trade_role=trade_role,
        role_score=score,
        signal=LiveSeedSignal(code=code, market=market),
        theme_state=state,
    )
