from __future__ import annotations

from trading.theme_engine.lab import (
    LabGateDecision,
    LabGateStatus,
    MarketStatus,
    MarketStrengthSnapshot,
    PriceLocationStatus,
    StockRole,
    ThemeConditionSnapshot,
    ThemeLabFlowResult,
    ThemeLabThemeStatus,
    TradeabilityRiskLevel,
    WatchSetSnapshot,
)
from ui.themelab_dashboard import (
    CHART_STATUS_NO_CANDLE,
    CHART_STATUS_QUOTE_ONLY,
    DashboardChartConfig,
    build_theme_lab_dashboard_state,
)


def test_dashboard_state_uses_theme_lab_gate_status_without_recalculation():
    result = _result(
        watchset=(
            _watch("000001", gate=LabGateStatus.BLOCKED, role=StockRole.LEADER, return_pct=12.0),
            _watch("000002", gate=LabGateStatus.READY_SMALL, role=StockRole.CO_LEADER, return_pct=6.0),
        )
    )

    state = build_theme_lab_dashboard_state(result)

    assert [item.final_gate_status for item in state.watchset] == [LabGateStatus.READY_SMALL, LabGateStatus.BLOCKED]
    assert [item.symbol for item in state.entry_candidates] == ["000002"]


def test_chart_universe_includes_indices_ready_small_and_positions_but_not_condition1_only():
    result = _result(
        watchset=(
            _watch("000001", gate=LabGateStatus.READY, role=StockRole.LEADER, return_pct=9.0),
            _watch("000002", gate=LabGateStatus.READY_SMALL, role=StockRole.CO_LEADER, return_pct=5.0),
            _watch("000003", gate=LabGateStatus.OBSERVE, role=StockRole.FOLLOWER, condition_level=1, return_pct=0.5),
        )
    )

    state = build_theme_lab_dashboard_state(
        result,
        candle_series_by_symbol={"KOSPI": [{"ts": "09:00"}], "KOSDAQ": [{"ts": "09:00"}], "000001": [{"ts": "09:01"}]},
        quote_only_symbols={"000002"},
        position_symbols={"999999"},
    )

    universe = {item.symbol: item for item in state.chart_universe}
    assert {"KOSPI", "KOSDAQ", "000001", "000002", "999999"}.issubset(universe)
    assert "000003" not in universe
    assert universe["000002"].chart_data_status == CHART_STATUS_QUOTE_ONLY
    assert universe["999999"].chart_data_status == CHART_STATUS_NO_CANDLE


def test_chart_universe_applies_watchset_limit_and_order_candidates_only_ready_statuses():
    watchset = tuple(
        _watch(f"{index:06d}", gate=LabGateStatus.WAIT, role=StockRole.FOLLOWER, return_pct=3.0, turnover=index)
        for index in range(1, 8)
    ) + (
        _watch("100001", gate=LabGateStatus.READY, role=StockRole.LEADER, return_pct=10.0),
        _watch("100002", gate=LabGateStatus.BLOCKED, role=StockRole.LATE_LAGGARD, return_pct=8.0),
    )
    result = _result(watchset=watchset)

    state = build_theme_lab_dashboard_state(
        result,
        config=DashboardChartConfig(max_watchset_chart_symbols=3),
    )

    wait_items = [item for item in state.chart_universe if item.reason == "WATCHSET"]
    assert len(wait_items) == 3
    assert [item.symbol for item in state.entry_candidates] == ["100001"]


def test_missing_data_quality_is_exposed_and_degraded_in_header_model():
    result = _result(data_quality={"candle_missing_count": 12, "vwap_missing_count": 2, "vi_status_supported": 0})

    state = build_theme_lab_dashboard_state(result)

    assert state.data_quality.status == "DEGRADED"
    assert state.data_quality.candle_missing_count == 12
    assert state.data_quality.vwap_missing_count == 2
    assert state.data_quality.vi_status_supported is False


def test_theme_leader_with_candle_is_selected_before_kosdaq_fallback():
    theme = _theme(top_leader_symbol="000001", status=ThemeLabThemeStatus.LEADING_THEME)
    result = _result(themes=(theme,), watchset=(_watch("000001", gate=LabGateStatus.WAIT, role=StockRole.LEADER),))

    state = build_theme_lab_dashboard_state(result, candle_series_by_symbol={"000001": [{"ts": "09:01"}]})

    assert state.selected_chart_symbol == "000001"


def _result(
    *,
    themes: tuple[ThemeConditionSnapshot, ...] | None = None,
    watchset: tuple[WatchSetSnapshot, ...] = (),
    data_quality: dict | None = None,
) -> ThemeLabFlowResult:
    themes = themes if themes is not None else (_theme(),)
    return ThemeLabFlowResult(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE, kospi_return_pct=0.3, kosdaq_return_pct=0.9),
        themes=themes,
        watchset=watchset,
        gate_decisions=tuple(LabGateDecision(symbol=item.symbol, status=item.final_gate_status) for item in watchset),
        data_quality=data_quality or {},
    )


def _theme(
    *,
    top_leader_symbol: str = "000001",
    status: ThemeLabThemeStatus = ThemeLabThemeStatus.LEADING_THEME,
) -> ThemeConditionSnapshot:
    return ThemeConditionSnapshot(
        calculated_at="09:00:00",
        theme_id="theme-1",
        theme_name="전력기기",
        raw_total_members=5,
        eligible_total_members=5,
        alive_count=4,
        strong_count=3,
        leader_count=1,
        alive_ratio=0.8,
        strong_ratio=0.6,
        leader_ratio=0.2,
        condition_score=80.0,
        theme_turnover_krw=12_000_000_000,
        theme_status=status,
        top_leader_symbol=top_leader_symbol,
        top_leader_name="제룡전기",
    )


def _watch(
    symbol: str,
    *,
    gate: LabGateStatus = LabGateStatus.WAIT,
    role: StockRole = StockRole.FOLLOWER,
    condition_level: int = 3,
    return_pct: float = 3.0,
    turnover: float = 5_000_000_000,
) -> WatchSetSnapshot:
    return WatchSetSnapshot(
        calculated_at="09:01:00",
        symbol=symbol,
        name=f"종목{symbol}",
        primary_theme="전력기기",
        return_pct=return_pct,
        turnover_krw=turnover,
        condition_level=condition_level,
        stock_role=role,
        watch_reason="ThemeLabFlow WatchSet",
        gate_status=gate,
        final_gate_status=gate,
        risk_level=TradeabilityRiskLevel.PASS if gate != LabGateStatus.BLOCKED else TradeabilityRiskLevel.HARD_BLOCK,
        price_location_status=PriceLocationStatus.GOOD_PULLBACK,
        price_location_score=70.0,
        risk_reason_codes=("LATE_LAGGARD",) if gate == LabGateStatus.BLOCKED else (),
        position_size_multiplier=0.5 if gate == LabGateStatus.READY_SMALL else 1.0,
    )
