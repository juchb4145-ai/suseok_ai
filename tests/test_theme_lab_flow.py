from __future__ import annotations

from trading.theme_engine.lab import (
    InstrumentMetadata,
    LabGateStatus,
    LiquidityFilterConfig,
    MarketStatus,
    MarketStrengthSnapshot,
    StockRole,
    ThemeBreadthEngine,
    ThemeLabConditionClassifier,
    ThemeLabConfig,
    ThemeLabFlowEngine,
    ThemeLabHybridGate,
    ThemeLabThemeStatus,
    ThemeStatusThresholds,
    WatchSetLimits,
    WatchSetManager,
    WatchSetSnapshot,
)
from trading.theme_engine.models import StockSnapshot, ThemeMembership


def test_condition_thresholds_are_inclusive_and_cumulative():
    classifier = ThemeLabConditionClassifier()

    assert classifier.classify(symbol="000001", change_rate_pct=-1.00).alive_hit is True
    assert classifier.classify(symbol="000001", change_rate_pct=-1.01).alive_hit is False

    strong = classifier.classify(symbol="000001", change_rate_pct=3.00)
    assert strong.strong_hit is True
    assert strong.alive_hit is True
    assert classifier.classify(symbol="000001", change_rate_pct=2.99).strong_hit is False

    leader = classifier.classify(symbol="000001", change_rate_pct=5.00)
    assert leader.leader_hit is True
    assert leader.strong_hit is True
    assert leader.alive_hit is True
    assert classifier.classify(symbol="000001", change_rate_pct=4.99).leader_hit is False


def test_exclusion_filter_removes_members_from_theme_denominator_and_counts():
    config = ThemeLabConfig(liquidity_filter=LiquidityFilterConfig(min_today_turnover_krw=1_000_000))
    engine = ThemeBreadthEngine(config)
    memberships = [
        _member("t", "000001"),
        _member("t", "000002"),
        _member("t", "000003"),
        _member("t", "000004"),
        _member("t", "000005"),
        _member("t", "000006"),
    ]
    snapshots = [
        _snapshot("000001", 5.5, turnover=2_000_000),
        _snapshot("000002", 5.5, turnover=2_000_000, metadata={"instrument_type": "ETF"}),
        _snapshot("000003", 5.5, turnover=2_000_000, metadata={"instrument_type": "ETN"}),
        _snapshot("000004", 5.5, name="테스트우", turnover=2_000_000),
        _snapshot("000005", 5.5, turnover=2_000_000, metadata={"is_suspended": True}),
        _snapshot("000006", 5.5, turnover=100),
    ]

    result = engine.calculate([("t", "테마", memberships)], snapshots)[0]

    assert result.raw_total_members == 6
    assert result.eligible_total_members == 1
    assert result.alive_count == 1
    assert result.strong_count == 1
    assert result.leader_count == 1
    excluded_reasons = {hit.excluded_reason for hit in result.member_hits if hit.excluded}
    assert {"ETF", "ETN", "PREFERRED_STOCK", "TRADING_SUSPENDED", "LOW_TODAY_TURNOVER"} <= excluded_reasons


def test_theme_breadth_ratios_and_score_formula():
    engine = ThemeBreadthEngine()
    memberships = [_member("a", f"00000{i}") for i in range(10)]
    returns = [6, 5, 4, 3, 1, 0, -0.5, -1.0, -1.01, -2.0]
    snapshots = [_snapshot(f"00000{i}", returns[i], turnover=1_000_000) for i in range(10)]

    result = engine.calculate([("a", "A", memberships)], snapshots)[0]

    assert result.eligible_total_members == 10
    assert result.alive_count == 8
    assert result.strong_count == 4
    assert result.leader_count == 2
    assert result.alive_ratio == 0.8
    assert result.strong_ratio == 0.4
    assert result.leader_ratio == 0.2
    assert result.condition_score == 0.8 * 20 + 0.4 * 35 + 0.2 * 45


def test_leader_count_is_cumulative_into_strong_and_alive():
    engine = ThemeBreadthEngine()
    result = engine.calculate(
        [("t", "테마", [_member("t", "000001")])],
        [_snapshot("000001", 5.2, turnover=1_000_000)],
    )[0]

    assert result.leader_count == 1
    assert result.strong_count == 1
    assert result.alive_count == 1


def test_leader_only_theme_blocks_laggard_ready():
    config = ThemeLabConfig(
        theme_status=ThemeStatusThresholds(
            min_eligible_members=3,
            max_strong_ratio_for_leader_only=0.34,
            max_strong_count_for_leader_only=1,
        )
    )
    result = ThemeBreadthEngine(config).calculate(
        [("t", "테마", [_member("t", "000001"), _member("t", "000002"), _member("t", "000003")])],
        [_snapshot("000001", 5.2, turnover=1_000_000), _snapshot("000002", 0.2), _snapshot("000003", -0.2)],
    )[0]
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000002",
        primary_theme="t",
        return_pct=3.1,
        condition_level=2,
        stock_role=StockRole.FOLLOWER,
    )
    gate = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=result,
        watch=watch,
    )

    assert result.theme_status == ThemeLabThemeStatus.LEADER_ONLY_THEME
    assert gate.status == LabGateStatus.BLOCKED
    assert "LEADER_ONLY_THEME_LAGGARD_BLOCK" in gate.reason_codes


def test_watchset_promotes_condition_two_or_three_and_respects_limits():
    manager = WatchSetManager(WatchSetLimits(max_watchset_size=3, max_watch_per_theme=2, top_theme_count=2))
    themes = ThemeBreadthEngine().calculate(
        [
            ("t1", "테마1", [_member("t1", "000001"), _member("t1", "000002"), _member("t1", "000003")]),
            ("t2", "테마2", [_member("t2", "000004"), _member("t2", "000005")]),
        ],
        [
            _snapshot("000001", 5.5, turnover=3_000_000),
            _snapshot("000002", 3.1, turnover=2_000_000),
            _snapshot("000003", -0.5, turnover=100_000_000),
            _snapshot("000004", 3.2, turnover=1_000_000),
            _snapshot("000005", -0.3, turnover=1_000_000),
        ],
    )

    watchset = manager.build(themes, [_snapshot("000001", 5.5), _snapshot("000002", 3.1), _snapshot("000004", 3.2)])

    assert [item.symbol for item in watchset] == ["000001", "000002", "000004"]
    assert "000003" not in {item.symbol for item in watchset}
    assert all(item.condition_level >= 2 for item in watchset)


def test_hybrid_gate_blocks_risk_off_and_late_laggard():
    theme = ThemeBreadthEngine().calculate(
        [("t", "테마", [_member("t", "000001"), _member("t", "000002"), _member("t", "000003")])],
        [_snapshot("000001", 6), _snapshot("000002", 4), _snapshot("000003", 3)],
    )[0]
    gate = ThemeLabHybridGate()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000001",
        primary_theme="t",
        return_pct=6,
        condition_level=3,
        stock_role=StockRole.LEADER,
    )

    risk_off = gate.evaluate(market=MarketStrengthSnapshot(MarketStatus.RISK_OFF), theme=theme, watch=watch)
    late = gate.evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=WatchSetSnapshot(**{**watch.__dict__, "stock_role": StockRole.LATE_LAGGARD}),
    )

    assert risk_off.status == LabGateStatus.BLOCKED
    assert late.status == LabGateStatus.BLOCKED


def test_pipeline_outputs_market_theme_watchset_gate_and_quality_summary():
    config = ThemeLabConfig(
        watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
        theme_status=ThemeStatusThresholds(min_strong_count_for_leading=2, min_leader_count_for_leading=1),
    )
    result = ThemeLabFlowEngine(config).run_pipeline(
        theme_inputs=[
            ("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003")]),
            ("weak", "약세", [_member("weak", "000004")]),
        ],
        snapshots=[
            _snapshot("000001", 6, turnover=5_000_000, current_price=106, metadata={"prev_close": 100}),
            _snapshot("000002", 4, turnover=4_000_000, current_price=104, metadata={"prev_close": 100}),
            _snapshot("000003", 0, turnover=3_000_000, current_price=100, metadata={"prev_close": 100}),
            _snapshot("000004", -2, turnover=1_000_000, current_price=98, metadata={"prev_close": 100}),
        ],
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.4,
    )

    assert result.market.market_status in {MarketStatus.CHOPPY, MarketStatus.SELECTIVE, MarketStatus.EXPANSION}
    assert result.themes[0].theme_id == "ai"
    assert result.watchset
    assert result.gate_decisions
    assert result.data_quality["excluded_count"] == 0


def test_missing_prev_close_without_change_rate_records_quality_and_blocks_ready():
    hit = ThemeLabConditionClassifier().classify(symbol="000001", current_price=100, prev_close=None, change_rate_pct=None)

    assert hit.alive_hit is False
    assert "MISSING_PREV_CLOSE" in hit.data_quality_flags

    theme = ThemeBreadthEngine().calculate(
        [("t", "테마", [_member("t", "000001")])],
        [_snapshot("000001", 0, current_price=100, metadata={})],
    )[0]
    watch = WatchSetSnapshot(calculated_at="", symbol="000001", primary_theme="t", condition_level=3, stock_role=StockRole.LEADER)
    decision = ThemeLabHybridGate().evaluate(market=MarketStrengthSnapshot(MarketStatus.SELECTIVE), theme=theme, watch=watch)

    assert decision.status == LabGateStatus.BLOCKED
    assert "DATA_QUALITY_BLOCK" in decision.reason_codes


def _member(theme_id: str, code: str) -> ThemeMembership:
    return ThemeMembership(theme_id=theme_id, stock_code=code, stock_name=f"종목{code}", active=True, trade_eligible=True)


def _snapshot(
    code: str,
    change_rate: float,
    *,
    name: str = "",
    turnover: float = 1_000_000,
    current_price: float = 0.0,
    metadata: dict | None = None,
) -> StockSnapshot:
    return StockSnapshot(
        stock_code=code,
        stock_name=name or f"종목{code}",
        current_price=current_price,
        change_rate=change_rate,
        turnover=turnover,
        volume=1000,
        session_high=current_price,
        metadata=dict(metadata or {}),
    )
