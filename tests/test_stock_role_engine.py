from trading.theme_engine.cohort import ThemeCohortSnapshot
from trading.theme_engine.roles import RawStockRole, StockRoleEngine, TradeStockRole
from trading.theme_engine.signals import LiveSeedSignal
from trading.theme_engine.state_machine import ThemeCoreState, ThemeStateSnapshot


def test_stock_role_engine_splits_raw_role_and_trade_role_in_leader_only_theme():
    state = _state(
        ThemeCoreState.LEADER_ONLY_THEME.value,
        [
            _signal("000001", change=5.5, turnover=10_000_000_000, execution=160),
            _signal("000002", change=5.1, turnover=8_000_000_000, execution=150),
            _signal("000003", change=3.4, turnover=4_000_000_000, execution=130),
        ],
        co_leaders=("000002",),
    )

    roles = {decision.code: decision for decision in StockRoleEngine().classify(state)}

    assert roles["000001"].raw_role == RawStockRole.LEADER.value
    assert roles["000001"].trade_role == TradeStockRole.LEADER_CONFIRMED.value
    assert roles["000002"].trade_role == TradeStockRole.CO_LEADER_CONFIRMED.value
    assert roles["000003"].raw_role == RawStockRole.FOLLOWER.value
    assert roles["000003"].trade_role == TradeStockRole.FOLLOWER_BLOCKED_LEADER_ONLY.value


def test_stock_role_engine_blocks_late_laggard_and_overheated_symbols():
    state = _state(
        ThemeCoreState.SPREADING_THEME.value,
        [
            _signal("000001", change=6.0, turnover=10_000_000_000, execution=160),
            _signal("000004", change=1.0, turnover=1_000_000_000, execution=95),
            _signal("000005", change=8.0, turnover=5_000_000_000, execution=170, overheated=True),
        ],
    )

    roles = {decision.code: decision for decision in StockRoleEngine().classify(state, market_phase="EXPANSION")}

    assert roles["000004"].raw_role == RawStockRole.LATE_LAGGARD.value
    assert roles["000004"].trade_role == TradeStockRole.LATE_LAGGARD_BLOCKED.value
    assert roles["000005"].raw_role == RawStockRole.OVERHEATED.value
    assert roles["000005"].trade_role == TradeStockRole.OVERHEATED_BLOCKED.value


def test_follower_is_allowed_only_during_expansion_for_spreading_theme():
    state = _state(
        ThemeCoreState.SPREADING_THEME.value,
        [
            _signal("000001", change=5.5, turnover=10_000_000_000, execution=160),
            _signal("000003", change=3.4, turnover=4_000_000_000, execution=130),
        ],
    )

    selective = {decision.code: decision for decision in StockRoleEngine().classify(state, market_phase="SELECTIVE")}
    expansion = {decision.code: decision for decision in StockRoleEngine().classify(state, market_phase="EXPANSION")}

    assert selective["000003"].trade_role == TradeStockRole.WEAK_MEMBER_BLOCKED.value
    assert expansion["000003"].trade_role == TradeStockRole.FOLLOWER_ALLOWED.value


def _state(state: str, signals: list[LiveSeedSignal], *, co_leaders: tuple[str, ...] = ()) -> ThemeStateSnapshot:
    return ThemeStateSnapshot(
        theme_id="ai",
        theme_name="AI",
        theme_state=state,
        leader_symbol="000001",
        co_leader_symbols=co_leaders,
        cohort=ThemeCohortSnapshot(theme_id="ai", theme_name="AI", signals=tuple(signals)),
    )


def _signal(
    code: str,
    *,
    change: float,
    turnover: float,
    execution: float,
    overheated: bool = False,
) -> LiveSeedSignal:
    return LiveSeedSignal(
        code=code,
        name=f"stock-{code}",
        change_rate_pct=change,
        turnover_krw=turnover,
        turnover_speed=turnover / 5,
        execution_strength=execution,
        realtime_valid=True,
        overheated=overheated,
    )
