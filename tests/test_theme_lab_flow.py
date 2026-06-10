from __future__ import annotations

from trading.theme_engine.lab import (
    InstrumentMetadata,
    LabGateStatus,
    LiquidityFilterConfig,
    MarketStatus,
    MarketSide,
    MarketSideGateConfig,
    MarketSideBreadthConfig,
    MarketSideGateConfirmationConfig,
    MarketStrengthSnapshot,
    MarketStrengthEngine,
    PositionAdjustmentConfig,
    PriceLocationConfig,
    PriceLocationEvaluator,
    PriceLocationInput,
    PriceLocationReadiness,
    PriceLocationResult,
    PriceLocationStatus,
    RiskOffEntryConfig,
    StockRole,
    ThemeBreadthEngine,
    ThemeConditionSnapshot,
    ThemeLabConditionClassifier,
    ThemeLabConfig,
    ThemeLabFlowEngine,
    ThemeLabHybridGate,
    ThemeLabThemeStatus,
    ThemeStatusThresholds,
    TradeabilityRiskConfig,
    TradeabilityRiskFilter,
    TradeabilityRiskInput,
    TradeabilityRiskLevel,
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
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
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


def test_watchset_retains_recently_demoted_symbols_for_subscription_stability():
    manager = WatchSetManager(
        WatchSetLimits(
            max_watchset_size=5,
            max_watch_per_theme=5,
            top_theme_count=2,
            retain_cycles_after_demotion=2,
        )
    )
    members = [("t1", "theme1", [_member("t1", "000001"), _member("t1", "000002")])]
    first_themes = ThemeBreadthEngine().calculate(
        members,
        [_snapshot("000001", 5.5, turnover=3_000_000), _snapshot("000002", 0.2, turnover=1_000_000)],
    )
    first = manager.build(first_themes, [_snapshot("000001", 5.5), _snapshot("000002", 0.2)])

    second_themes = ThemeBreadthEngine().calculate(
        members,
        [_snapshot("000001", 0.5, turnover=3_000_000), _snapshot("000002", 0.2, turnover=1_000_000)],
    )
    second = manager.build(second_themes, [_snapshot("000001", 0.5), _snapshot("000002", 0.2)])
    third = manager.build(second_themes, [_snapshot("000001", 0.4), _snapshot("000002", 0.2)])
    fourth = manager.build(second_themes, [_snapshot("000001", 0.3), _snapshot("000002", 0.2)])

    assert [item.symbol for item in first] == ["000001"]
    assert [item.symbol for item in second] == ["000001"]
    assert second[0].watchset_retained is True
    assert second[0].condition_level == 1
    assert second[0].watchset_retention_cycles == 1
    assert second[0].watch_reason == "WATCHSET_RETAINED_AFTER_DEMOTION"
    assert third[0].watchset_retention_cycles == 2
    assert fourth == []


def test_hybrid_gate_waits_risk_off_and_blocks_late_laggard():
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

    risk_off = gate.evaluate(
        market=MarketStrengthSnapshot(MarketStatus.RISK_OFF),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )
    late = gate.evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=WatchSetSnapshot(**{**watch.__dict__, "stock_role": StockRole.LATE_LAGGARD}),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert risk_off.status == LabGateStatus.WAIT
    assert "GLOBAL_MARKET_RISK_OFF" in risk_off.reason_codes
    assert late.status == LabGateStatus.BLOCKED


def test_market_strength_calculates_side_statuses_separately():
    market = MarketStrengthEngine().calculate(
        [_snapshot("000001", 1.0), _snapshot("000002", -0.2)],
        kospi_return_pct=0.2,
        kosdaq_return_pct=-1.2,
    )

    assert market.market_status == MarketStatus.CHOPPY
    assert market.kospi_status == MarketStatus.CHOPPY
    assert market.kosdaq_status == MarketStatus.WEAK
    assert "KOSDAQ_MARKET_WEAK" in market.side_statuses[MarketSide.KOSDAQ.value]["reason_codes"]
    assert "SIDE_BREADTH_FALLBACK_GLOBAL" in market.side_breadth_reason_codes


def test_kosdaq_side_breadth_weak_blocks_kosdaq_candidate_even_when_index_is_flat():
    engine = MarketStrengthEngine(
        side_breadth_config=MarketSideBreadthConfig(
            min_sample_count_kosdaq=4,
            min_sample_count_kospi=2,
            breadth_weak_pct=0.38,
            breadth_risk_off_pct=0.20,
        )
    )
    market = engine.calculate(
        [
            _snapshot("000001", 5.0, current_price=105, metadata={"market": "KOSDAQ"}),
            _snapshot("000002", -1.0, current_price=99, metadata={"market": "KOSDAQ"}),
            _snapshot("000003", -1.2, current_price=98.8, metadata={"market": "KOSDAQ"}),
            _snapshot("000004", -0.7, current_price=99.3, metadata={"market": "KOSDAQ"}),
            _snapshot("100001", 0.5, current_price=100.5, metadata={"market": "KOSPI"}),
            _snapshot("100002", 0.2, current_price=100.2, metadata={"market": "KOSPI"}),
        ],
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.0,
    )
    decision = ThemeLabHybridGate().evaluate(
        market=market,
        theme=_leading_theme(),
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000001",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.KOSDAQ.value,
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert market.kosdaq_breadth_ready is True
    assert market.kosdaq_breadth_pct == 0.25
    assert market.kosdaq_status == MarketStatus.WEAK
    assert decision.status == LabGateStatus.WAIT
    assert "KOSDAQ_SIDE_BREADTH_WEAK" in decision.reason_codes
    assert decision.candidate_breadth_ready is True


def test_kosdaq_side_breadth_weak_does_not_block_kospi_candidate():
    market = MarketStrengthEngine(
        side_breadth_config=MarketSideBreadthConfig(
            min_sample_count_kosdaq=4,
            min_sample_count_kospi=2,
            breadth_risk_off_pct=0.20,
        )
    ).calculate(
        [
            _snapshot("000001", 5.0, current_price=105, metadata={"market": "KOSDAQ"}),
            _snapshot("000002", -1.0, current_price=99, metadata={"market": "KOSDAQ"}),
            _snapshot("000003", -1.2, current_price=98.8, metadata={"market": "KOSDAQ"}),
            _snapshot("000004", -0.7, current_price=99.3, metadata={"market": "KOSDAQ"}),
            _snapshot("100001", 1.0, current_price=101, metadata={"market": "KOSPI"}),
            _snapshot("100002", 0.2, current_price=100.2, metadata={"market": "KOSPI"}),
        ],
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.0,
    )
    decision = ThemeLabHybridGate().evaluate(
        market=market,
        theme=_leading_theme(),
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="100001",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.KOSPI.value,
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert market.kosdaq_status == MarketStatus.WEAK
    assert market.kospi_status in {MarketStatus.EXPANSION, MarketStatus.SELECTIVE}
    assert decision.status == LabGateStatus.READY


def test_side_breadth_sample_too_small_falls_back_to_index_return():
    market = MarketStrengthEngine(
        side_breadth_config=MarketSideBreadthConfig(min_sample_count_kosdaq=5)
    ).calculate(
        [
            _snapshot("000001", -2.0, current_price=98, metadata={"market": "KOSDAQ"}),
            _snapshot("000002", -1.5, current_price=98.5, metadata={"market": "KOSDAQ"}),
        ],
        kosdaq_return_pct=0.1,
    )

    assert market.kosdaq_breadth_ready is False
    assert "SIDE_BREADTH_SAMPLE_TOO_SMALL" in market.side_breadth_data_quality_flags
    assert "SIDE_BREADTH_FALLBACK_INDEX_RETURN" in market.side_breadth_reason_codes
    assert market.kosdaq_status == MarketStatus.CHOPPY


def test_side_breadth_valid_quote_ratio_low_falls_back_to_index_return():
    market = MarketStrengthEngine(
        side_breadth_config=MarketSideBreadthConfig(min_sample_count_kosdaq=2, valid_quote_ratio_min=0.75)
    ).calculate(
        [
            _snapshot("000001", 2.0, current_price=102, metadata={"market": "KOSDAQ", "quote_age_sec": 10}),
            _snapshot("000002", 1.5, current_price=101.5, metadata={"market": "KOSDAQ", "quote_age_sec": 10}),
            _snapshot("000003", -2.0, current_price=98, metadata={"market": "KOSDAQ", "quote_age_sec": 120}),
            _snapshot("000004", -2.5, current_price=97.5, metadata={"market": "KOSDAQ", "quote_age_sec": 120}),
        ],
        kosdaq_return_pct=0.2,
    )

    assert market.kosdaq_breadth_ready is False
    assert market.kosdaq_valid_quote_ratio == 0.5
    assert "SIDE_BREADTH_VALID_QUOTE_RATIO_LOW" in market.side_breadth_data_quality_flags
    assert market.kosdaq_status == MarketStatus.CHOPPY


def test_candidate_universe_side_breadth_is_diagnostic_only_not_gate_usable():
    market = MarketStrengthEngine(
        side_breadth_config=MarketSideBreadthConfig(min_sample_count_kosdaq=2, valid_quote_ratio_min=0.5)
    ).calculate(
        [
            _snapshot("000001", -2.0, current_price=98, metadata={"market": "KOSDAQ", "breadth_source": "candidate_universe"}),
            _snapshot("000002", -1.5, current_price=98.5, metadata={"market": "KOSDAQ", "breadth_source": "candidate_universe"}),
        ],
        kosdaq_return_pct=0.2,
    )

    assert market.kosdaq_breadth_ready is True
    assert market.kosdaq_breadth_source == "candidate_universe"
    assert market.kosdaq_breadth_trust_level == "DIAGNOSTIC_ONLY"
    assert market.kosdaq_breadth_gate_usable is False
    assert market.kosdaq_breadth_diagnostic_only is True
    assert "SIDE_BREADTH_DIAGNOSTIC_ONLY" in market.side_breadth_reason_codes
    assert market.kosdaq_status == MarketStatus.CHOPPY


def test_candidate_side_market_gate_blocks_only_matching_weak_side():
    theme = _leading_theme()
    market = MarketStrengthSnapshot(
        MarketStatus.SELECTIVE,
        kospi_return_pct=0.3,
        kosdaq_return_pct=-1.2,
        kospi_status=MarketStatus.SELECTIVE,
        kosdaq_status=MarketStatus.WEAK,
        kospi_index_return_pct=0.3,
        kosdaq_index_return_pct=-1.2,
    )
    gate = ThemeLabHybridGate()

    kosdaq = gate.evaluate(
        market=market,
        theme=theme,
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000001",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.KOSDAQ.value,
            candidate_market_source="snapshot.metadata.market",
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )
    kospi = gate.evaluate(
        market=market,
        theme=theme,
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000002",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.KOSPI.value,
            candidate_market_source="snapshot.metadata.market",
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert kosdaq.status == LabGateStatus.WAIT
    assert "CANDIDATE_MARKET_WEAK" in kosdaq.reason_codes
    assert "KOSDAQ_MARKET_WEAK" in kosdaq.reason_codes
    assert kosdaq.candidate_market_status == MarketStatus.WEAK.value
    assert kospi.status == LabGateStatus.READY
    assert kospi.candidate_market_status == MarketStatus.SELECTIVE.value


def test_unknown_market_strict_fallback_waits_when_either_side_is_weak():
    theme = _leading_theme()
    market = MarketStrengthSnapshot(
        MarketStatus.SELECTIVE,
        kospi_status=MarketStatus.SELECTIVE,
        kosdaq_status=MarketStatus.WEAK,
    )

    decision = ThemeLabHybridGate().evaluate(
        market=market,
        theme=theme,
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000001",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.UNKNOWN.value,
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert decision.status == LabGateStatus.WAIT
    assert "MARKET_CLASSIFICATION_MISSING" in decision.reason_codes
    assert "MARKET_CLASSIFICATION_FALLBACK_STRICT" in decision.reason_codes


def test_unknown_market_records_missing_but_can_pass_when_both_sides_are_healthy():
    theme = _leading_theme()
    market = MarketStrengthSnapshot(
        MarketStatus.SELECTIVE,
        kospi_status=MarketStatus.SELECTIVE,
        kosdaq_status=MarketStatus.CHOPPY,
    )

    decision = ThemeLabHybridGate().evaluate(
        market=market,
        theme=theme,
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000001",
            primary_theme="t",
            return_pct=5.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.UNKNOWN.value,
        ),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
    )

    assert decision.status == LabGateStatus.READY
    assert "MARKET_CLASSIFICATION_MISSING" not in decision.reason_codes
    assert "MARKET_CLASSIFICATION_MISSING" in decision.market_side_reason_codes


def test_ready_small_is_blocked_when_candidate_side_market_is_weak():
    theme = _leading_theme()
    market = MarketStrengthSnapshot(
        MarketStatus.SELECTIVE,
        kospi_status=MarketStatus.SELECTIVE,
        kosdaq_status=MarketStatus.WEAK,
    )

    decision = ThemeLabHybridGate().evaluate(
        market=market,
        theme=theme,
        watch=WatchSetSnapshot(
            calculated_at="",
            symbol="000001",
            primary_theme="t",
            return_pct=9.0,
            condition_level=3,
            stock_role=StockRole.LEADER,
            candidate_market=MarketSide.KOSDAQ.value,
        ),
        price_location=_price_location(PriceLocationStatus.BREAKOUT_CONTINUATION, reason_codes=("BREAKOUT_CONTINUATION_READY_SMALL",)),
        snapshot=_snapshot("000001", 9.0, turnover=5_000_000_000, metadata={"pullback_from_high_pct": 1.0}),
    )

    assert decision.status == LabGateStatus.WAIT
    assert "KOSDAQ_MARKET_WEAK" in decision.reason_codes


def test_market_side_weak_first_cycle_waits_for_confirmation():
    engine = ThemeLabFlowEngine(
        ThemeLabConfig(
            watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(
                min_sample_count_kosdaq=4,
                min_sample_count_kospi=2,
                breadth_weak_pct=0.38,
                breadth_risk_off_pct=0.20,
            ),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(weak_confirm_cycles=2, recover_confirm_cycles=2),
        )
    )

    result = engine.run_pipeline(
        theme_inputs=[("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])],
        snapshots=_kosdaq_weak_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.0,
        calculated_at="2026-06-01T09:05:00",
    )
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert result.market.kosdaq_raw_status == MarketStatus.WEAK
    assert result.market.kosdaq_confirmed_status != MarketStatus.WEAK
    assert decision.status == LabGateStatus.WAIT
    assert decision.candidate_market_confirmation_pending is True
    assert "WAIT_MARKET_CONFIRMATION_PENDING" in decision.reason_codes
    assert "CANDIDATE_MARKET_WEAK_UNCONFIRMED" in decision.reason_codes
    assert "WAIT_CANDIDATE_MARKET_WEAK" not in decision.reason_codes


def test_market_side_weak_second_cycle_confirms_candidate_wait():
    engine = ThemeLabFlowEngine(
        ThemeLabConfig(
            watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(
                min_sample_count_kosdaq=4,
                min_sample_count_kospi=2,
                breadth_weak_pct=0.38,
                breadth_risk_off_pct=0.20,
            ),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(weak_confirm_cycles=2, recover_confirm_cycles=2),
        )
    )
    engine.run_pipeline(
        theme_inputs=[("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])],
        snapshots=_kosdaq_weak_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.0,
        calculated_at="2026-06-01T09:05:00",
    )

    result = engine.run_pipeline(
        theme_inputs=[("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])],
        snapshots=_kosdaq_weak_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.0,
        calculated_at="2026-06-01T09:06:00",
    )
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert result.market.kosdaq_confirmed_status == MarketStatus.WEAK
    assert result.market.side_confirmation_states[MarketSide.KOSDAQ.value]["weak_consecutive_cycles"] == 2
    assert decision.status == LabGateStatus.WAIT
    assert decision.candidate_market_confirmation_pending is False
    assert "WAIT_CANDIDATE_MARKET_WEAK" in decision.reason_codes
    assert "MARKET_WEAK_CONFIRMED" in decision.reason_codes


def test_market_side_index_hard_risk_off_confirms_immediately():
    engine = ThemeLabFlowEngine(
        ThemeLabConfig(
            watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(min_sample_count_kosdaq=4, min_sample_count_kospi=2),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(risk_off_confirm_cycles=2),
        )
    )

    result = engine.run_pipeline(
        theme_inputs=[("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])],
        snapshots=_kosdaq_weak_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=-3.0,
        calculated_at="2026-06-01T09:05:00",
    )
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert result.market.kosdaq_confirmed_status == MarketStatus.RISK_OFF
    assert decision.candidate_market_confirmation_pending is False
    assert "WAIT_CANDIDATE_MARKET_RISK_OFF" in decision.reason_codes
    assert "MARKET_RISK_OFF_CONFIRMED" in decision.reason_codes


def test_risk_off_small_entry_allows_only_strong_leader_when_enabled():
    gate = ThemeLabHybridGate(
        risk_off_entry_config=RiskOffEntryConfig(enabled=True, observe_only=False),
        market_side_confirmation_config=MarketSideGateConfirmationConfig(),
    )

    decision = gate.evaluate(
        market=_risk_off_market(),
        theme=_risk_off_leading_theme(),
        watch=_risk_off_watch(),
        price_location=_price_location(PriceLocationStatus.VWAP_RECLAIM),
        snapshot=_risk_off_snapshot(),
    )

    assert decision.status == LabGateStatus.READY_SMALL
    assert "RISK_OFF_SMALL_ENTRY" in decision.reason_codes
    assert "RISK_OFF_RELATIVE_STRENGTH" in decision.reason_codes
    assert "RISK_OFF_BREADTH_FILTER_PASS" in decision.reason_codes
    assert "GLOBAL_MARKET_RISK_OFF" not in decision.reason_codes
    assert decision.position_size_multiplier == 0.25
    assert decision.risk_off_entry_details["risk_off_entry_allowed"] is True
    assert decision.risk_off_entry_details["risk_off_relative_strength_pct"] >= 4.0


def test_risk_off_small_entry_blocks_extreme_breadth():
    gate = ThemeLabHybridGate(
        risk_off_entry_config=RiskOffEntryConfig(enabled=True, observe_only=False),
        market_side_confirmation_config=MarketSideGateConfirmationConfig(),
    )

    decision = gate.evaluate(
        market=_risk_off_market(breadth_pct=0.10),
        theme=_risk_off_leading_theme(),
        watch=_risk_off_watch(),
        price_location=_price_location(PriceLocationStatus.VWAP_RECLAIM),
        snapshot=_risk_off_snapshot(),
    )

    assert decision.status == LabGateStatus.WAIT
    assert "GLOBAL_MARKET_RISK_OFF" in decision.reason_codes
    assert decision.risk_off_entry_details["risk_off_entry_rejected_reason"] == "EXTREME_RISK_OFF"


def test_risk_off_small_entry_blocks_chase_locations():
    gate = ThemeLabHybridGate(
        risk_off_entry_config=RiskOffEntryConfig(enabled=True, observe_only=False),
        market_side_confirmation_config=MarketSideGateConfirmationConfig(),
    )

    decision = gate.evaluate(
        market=_risk_off_market(),
        theme=_risk_off_leading_theme(),
        watch=_risk_off_watch(),
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH),
        snapshot=_risk_off_snapshot(),
    )

    assert decision.status == LabGateStatus.WAIT
    assert "RISK_OFF_SMALL_ENTRY" not in decision.reason_codes
    assert decision.risk_off_entry_details["risk_off_entry_rejected_reason"] == "PRICE_LOCATION_NOT_ALLOWED"


def test_risk_off_small_entry_blocks_stale_quote_data():
    gate = ThemeLabHybridGate(
        risk_off_entry_config=RiskOffEntryConfig(enabled=True, observe_only=False),
        market_side_confirmation_config=MarketSideGateConfirmationConfig(),
    )

    decision = gate.evaluate(
        market=_risk_off_market(),
        theme=_risk_off_leading_theme(),
        watch=_risk_off_watch(),
        price_location=_price_location(PriceLocationStatus.VWAP_RECLAIM),
        snapshot=_snapshot(
            "000001",
            6.0,
            turnover=5_000_000_000,
            current_price=10000,
            metadata={
                "prev_close": 9434,
                "vwap": 9950,
                "vwap_ready": True,
                "latest_tick_stale": True,
            },
        ),
    )

    assert decision.status == LabGateStatus.WAIT
    assert decision.risk_off_entry_details["risk_off_entry_rejected_reason"] == "STALE_QUOTE"


def test_market_side_recovery_requires_confirmed_healthy_cycles():
    engine = ThemeLabFlowEngine(
        ThemeLabConfig(
            watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(
                min_sample_count_kosdaq=4,
                min_sample_count_kospi=2,
                breadth_weak_pct=0.38,
                breadth_risk_off_pct=0.20,
            ),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(weak_confirm_cycles=2, recover_confirm_cycles=2),
        )
    )
    theme_inputs = [("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])]
    engine.run_pipeline(theme_inputs=theme_inputs, snapshots=_kosdaq_weak_snapshots(), calculated_at="2026-06-01T09:05:00")
    engine.run_pipeline(theme_inputs=theme_inputs, snapshots=_kosdaq_weak_snapshots(), calculated_at="2026-06-01T09:06:00")

    pending = engine.run_pipeline(
        theme_inputs=theme_inputs,
        snapshots=_kosdaq_healthy_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.2,
        calculated_at="2026-06-01T09:07:00",
    )
    pending_decision = next(item for item in pending.gate_decisions if item.symbol == "000001")
    recovered = engine.run_pipeline(
        theme_inputs=theme_inputs,
        snapshots=_kosdaq_healthy_snapshots(),
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.2,
        calculated_at="2026-06-01T09:08:00",
    )
    recovered_decision = next(item for item in recovered.gate_decisions if item.symbol == "000001")

    assert pending_decision.status == LabGateStatus.WAIT
    assert pending_decision.candidate_market_recovery_pending is True
    assert "WAIT_MARKET_RECOVERY_PENDING" in pending_decision.reason_codes
    assert "MARKET_WAIT_HYSTERESIS_HOLD" in pending_decision.reason_codes
    assert recovered.market.kosdaq_confirmed_status in {MarketStatus.EXPANSION, MarketStatus.SELECTIVE, MarketStatus.CHOPPY}
    assert recovered_decision.candidate_market_recovery_pending is False
    assert "WAIT_MARKET_RECOVERY_PENDING" not in recovered_decision.reason_codes
    assert "MARKET_RECOVERY_CONFIRMED" in recovered_decision.market_side_reason_codes
    assert recovered_decision.market_side_last_recovered_at == "2026-06-01T09:08:00"


def test_side_breadth_source_conflict_blocks_entry_as_confirmation_pending():
    engine = ThemeLabFlowEngine(
        ThemeLabConfig(
            watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
            theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
            market_side_breadth=MarketSideBreadthConfig(
                min_sample_count_kosdaq=4,
                min_sample_count_kospi=2,
                side_breadth_source_conflict_threshold_pct=0.15,
            ),
            market_side_gate_confirmation=MarketSideGateConfirmationConfig(source_conflict_blocks_entry=True),
        )
    )
    metadata = {
        f"00000{i}": InstrumentMetadata(
            symbol=f"00000{i}",
            raw={
                "market": "KOSDAQ",
                "current_price": 100,
                "change_rate": -1.0,
                "turnover": 1_000_000,
            },
        )
        for i in range(1, 5)
    }

    result = engine.run_pipeline(
        theme_inputs=[("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")])],
        snapshots=_kosdaq_healthy_snapshots(),
        metadata_by_symbol=metadata,
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.2,
        calculated_at="2026-06-01T09:05:00",
    )
    decision = next(item for item in result.gate_decisions if item.symbol == "000001")

    assert "SIDE_BREADTH_SOURCE_CONFLICT" in result.market.side_breadth_reason_codes
    assert decision.status == LabGateStatus.WAIT
    assert decision.candidate_market_confirmation_pending is True
    assert "WAIT_MARKET_CONFIRMATION_PENDING" in decision.reason_codes
    assert "SIDE_BREADTH_SOURCE_CONFLICT" in decision.reason_codes


def test_watchset_infers_candidate_market_from_snapshot_metadata():
    config = ThemeLabConfig(
        theme_status=ThemeStatusThresholds(
            min_eligible_members=1,
            min_strong_count_for_leading=1,
            min_leader_count_for_leading=1,
        )
    )
    theme = ThemeBreadthEngine(config).calculate(
        [("t", "테마", [_member("t", "000001")])],
        [_snapshot("000001", 5.5, turnover=3_000_000, metadata={"market": "KQ"})],
    )[0]

    watchset = WatchSetManager().build([theme], [_snapshot("000001", 5.5, metadata={"market": "KQ"})])

    assert watchset[0].candidate_market == MarketSide.KOSDAQ.value
    assert watchset[0].candidate_market_source == "snapshot.metadata.market"


def test_unknown_market_passive_metrics_are_recorded_in_pipeline_data_quality():
    config = ThemeLabConfig(
        watchset_limits=WatchSetLimits(max_watchset_size=10, max_watch_per_theme=5, top_theme_count=3),
        theme_status=ThemeStatusThresholds(min_strong_count_for_leading=1, min_leader_count_for_leading=1),
    )
    result = ThemeLabFlowEngine(config).run_pipeline(
        theme_inputs=[
            ("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003")]),
        ],
        snapshots=[
            _snapshot("000001", 6, turnover=5_000_000, current_price=106, metadata={"prev_close": 100}),
            _snapshot("000002", 4, turnover=4_000_000, current_price=104, metadata={"prev_close": 100, "market": "KOSDAQ"}),
            _snapshot("000003", 0, turnover=3_000_000, current_price=100, metadata={"prev_close": 100, "market": "KOSPI"}),
        ],
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.4,
    )

    assert result.data_quality["market_classification_total_count"] >= 2
    assert result.data_quality["market_classification_unknown_count"] >= 1
    assert result.data_quality["market_classification_unknown_ratio"] > 0


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
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.UNKNOWN, data_quality_flags=("MISSING_CURRENT_PRICE",)),
    )

    assert decision.status == LabGateStatus.BLOCKED
    assert "DATA_QUALITY_BLOCK" in decision.reason_codes


def test_pipeline_does_not_hard_block_watch_member_for_theme_wide_missing_price_flags():
    config = ThemeLabConfig(
        watchset_limits=WatchSetLimits(max_watchset_size=5, max_watch_per_theme=5, top_theme_count=1),
        theme_status=ThemeStatusThresholds(
            min_eligible_members=1,
            min_strong_count_for_leading=1,
            min_leader_count_for_leading=1,
        ),
        market_side_gate=MarketSideGateConfig(enabled=False),
        market_side_gate_confirmation=MarketSideGateConfirmationConfig(enabled=False),
    )
    leader = _snapshot(
        "000001",
        6.0,
        turnover=5_000_000_000,
        current_price=106,
        session_high=108,
        metadata={
            "market": "KOSDAQ",
            "prev_close": 100,
            "vwap": 104,
            "upper_limit_price": 130,
            "breakout_level": 105,
            "recent_support_price": 103,
            "recent_candles_1m": [{"high": 108, "low": 105, "close": 106}],
        },
    )
    leader.momentum_1m = 0.5
    leader.momentum_3m = 0.3
    leader.execution_strength = 120

    result = ThemeLabFlowEngine(config).run_pipeline(
        theme_inputs=[("t", "T", [_member("t", "000001"), _member("t", "000002")])],
        snapshots=[leader],
        kospi_return_pct=0.2,
        kosdaq_return_pct=0.4,
    )

    watch = result.watchset[0]

    assert "MISSING_CURRENT_PRICE" in result.themes[0].data_quality_flags
    assert watch.symbol == "000001"
    assert watch.gate_status == LabGateStatus.READY
    assert watch.risk_level != TradeabilityRiskLevel.HARD_BLOCK
    assert "DATA_QUALITY_BLOCK" not in watch.risk_reason_codes
    assert "MISSING_CURRENT_PRICE" not in watch.price_location_data_quality_flags


def test_theme_breadth_uses_tick_change_rate_when_prev_close_missing():
    theme = ThemeBreadthEngine().calculate(
        [("t", "테마", [_member("t", "000001")])],
        [_snapshot("000001", 4.2, current_price=104.2, metadata={})],
    )[0]

    assert theme.alive_count == 1
    assert theme.strong_count == 1
    assert theme.leader_count == 0
    assert theme.member_hits[0].return_pct == 4.2


def test_vi_active_is_always_hard_block():
    result = TradeabilityRiskFilter().evaluate(
        _risk_input(stock_role=StockRole.LEADER, vi_active=True, return_pct=7.0)
    )

    assert result.risk_level == TradeabilityRiskLevel.HARD_BLOCK
    assert result.position_size_multiplier == 0.0
    assert "VI_ACTIVE" in result.reason_codes


def test_vi_cooldown_leader_with_turnover_is_not_blocked():
    result = TradeabilityRiskFilter().evaluate(
        _risk_input(
            stock_role=StockRole.LEADER,
            seconds_since_vi_release=60,
            turnover_krw=5_000_000_000,
            return_pct=9.0,
        )
    )

    assert result.risk_level in {TradeabilityRiskLevel.SOFT_BLOCK, TradeabilityRiskLevel.RISK_ADJUST}
    assert result.risk_level != TradeabilityRiskLevel.HARD_BLOCK
    assert result.recheck_after_sec == 30


def test_twelve_pct_leader_can_be_ready_small_when_quality_is_good():
    theme = _leading_theme()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000001",
        primary_theme="t",
        return_pct=12.0,
        turnover_krw=5_000_000_000,
        condition_level=3,
        stock_role=StockRole.LEADER,
    )
    snapshot = _snapshot("000001", 12.0, turnover=5_000_000_000, metadata={"prev_close": 100}, current_price=112)
    snapshot.momentum_3m = 1.2
    gate = ThemeLabHybridGate(
        TradeabilityRiskFilter(TradeabilityRiskConfig(leader_max_buy_return_pct=12.0))
    ).evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
        snapshot=snapshot,
    )

    assert gate.status == LabGateStatus.READY_SMALL
    assert gate.risk_level == TradeabilityRiskLevel.RISK_ADJUST
    assert 0 < gate.position_size_multiplier < 1.0


def test_twelve_pct_follower_cannot_be_ready():
    theme = _leading_theme()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000002",
        primary_theme="t",
        return_pct=12.0,
        turnover_krw=2_000_000_000,
        condition_level=3,
        stock_role=StockRole.FOLLOWER,
    )
    gate = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH),
        snapshot=_snapshot("000002", 12.0, turnover=2_000_000_000),
    )

    assert gate.status in {LabGateStatus.WAIT, LabGateStatus.BLOCKED}
    assert gate.status not in {LabGateStatus.READY, LabGateStatus.READY_SMALL}


def test_high_chase_leader_with_momentum_can_be_ready_small():
    theme = _leading_theme()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000001",
        primary_theme="t",
        return_pct=9.0,
        turnover_krw=5_000_000_000,
        condition_level=3,
        stock_role=StockRole.LEADER,
    )
    snapshot = _snapshot(
        "000001",
        9.0,
        turnover=5_000_000_000,
        current_price=109,
        metadata={"prev_close": 100, "pullback_from_high_pct": 0.1},
    )
    snapshot.momentum_1m = 0.5
    gate = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH, reason_codes=("HIGH_CHASE_LEADER",)),
        snapshot=snapshot,
    )

    assert gate.status == LabGateStatus.READY_SMALL
    assert "HIGH_CHASE_LEADER" in gate.risk_reason_codes


def test_high_chase_follower_waits_or_blocks():
    theme = _leading_theme()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000002",
        primary_theme="t",
        return_pct=7.0,
        turnover_krw=1_000_000_000,
        condition_level=3,
        stock_role=StockRole.FOLLOWER,
    )
    gate = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH),
        snapshot=_snapshot("000002", 7.0, metadata={"pullback_from_high_pct": 0.1}),
    )

    assert gate.status in {LabGateStatus.WAIT, LabGateStatus.BLOCKED}
    assert gate.status not in {LabGateStatus.READY, LabGateStatus.READY_SMALL}


def test_late_laggard_cannot_be_ready_small():
    theme = _leading_theme()
    watch = WatchSetSnapshot(
        calculated_at="",
        symbol="000009",
        primary_theme="t",
        return_pct=6.0,
        turnover_krw=1_000_000_000,
        condition_level=3,
        stock_role=StockRole.LATE_LAGGARD,
    )
    gate = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
        snapshot=_snapshot("000009", 6.0),
    )

    assert gate.status == LabGateStatus.BLOCKED
    assert gate.position_size_multiplier == 0.0
    assert gate.status != LabGateStatus.READY_SMALL


def test_risk_adjust_multiplier_is_smaller_than_one():
    result = TradeabilityRiskFilter(TradeabilityRiskConfig(leader_max_buy_return_pct=12.0)).evaluate(
        _risk_input(
            stock_role=StockRole.LEADER,
            return_pct=12.0,
            momentum_3m=1.0,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
        )
    )

    assert result.risk_level == TradeabilityRiskLevel.RISK_ADJUST
    assert 0 < result.position_size_multiplier < 1.0


def test_price_location_leader_good_pullback_can_be_ready():
    theme = _leading_theme()
    watch = _watch(role=StockRole.LEADER, return_pct=6.0)
    price = _price_location(PriceLocationStatus.GOOD_PULLBACK)
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=theme,
        watch=watch,
        price_location=price,
        snapshot=_snapshot("000001", 6.0, current_price=106, session_high=108, metadata={"prev_close": 100}),
    )

    assert decision.status == LabGateStatus.READY


def test_price_location_leader_breakout_continuation_can_be_ready_small():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=9.0),
        price_location=_price_location(PriceLocationStatus.BREAKOUT_CONTINUATION),
        snapshot=_snapshot("000001", 9.0, current_price=109, session_high=111, metadata={"prev_close": 100}),
    )

    assert decision.status == LabGateStatus.READY_SMALL
    assert decision.position_size_multiplier < 1.0


def test_price_location_leader_chase_high_is_not_hard_block_when_flow_is_good():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=9.0),
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH),
        snapshot=_snapshot("000001", 9.0, current_price=109, turnover=5_000_000_000, metadata={"prev_close": 100}),
    )

    assert decision.status in {LabGateStatus.READY_SMALL, LabGateStatus.WAIT}
    assert decision.status != LabGateStatus.BLOCKED


def test_price_location_follower_chase_high_cannot_be_ready():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.FOLLOWER, return_pct=7.0),
        price_location=_price_location(PriceLocationStatus.CHASE_HIGH),
        snapshot=_snapshot("000002", 7.0),
    )

    assert decision.status not in {LabGateStatus.READY, LabGateStatus.READY_SMALL}


def test_price_location_follower_vwap_overextended_cannot_be_ready():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.FOLLOWER, return_pct=7.0),
        price_location=_price_location(PriceLocationStatus.VWAP_OVEREXTENDED),
        snapshot=_snapshot("000002", 7.0),
    )

    assert decision.status not in {LabGateStatus.READY, LabGateStatus.READY_SMALL}


def test_price_location_follower_good_pullback_can_be_ready_small():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.FOLLOWER, return_pct=4.0),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
        snapshot=_snapshot("000002", 4.0),
    )

    assert decision.status == LabGateStatus.READY_SMALL
    assert decision.position_size_multiplier < 1.0


def test_price_location_late_laggard_blocks_even_good_pullback():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LATE_LAGGARD, return_pct=4.0),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK),
        snapshot=_snapshot("000009", 4.0),
    )

    assert decision.status == LabGateStatus.BLOCKED


def test_price_location_failed_breakout_with_negative_momentum_waits_or_blocks():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=99,
            return_pct=5,
            session_high=105,
            vwap=100,
            breakout_level=100,
            recent_candles_1m=({"high": 105, "low": 98, "close": 99},),
            momentum_1m=-0.5,
            momentum_3m=-0.2,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=5.0),
        price_location=price,
        snapshot=_snapshot("000001", 5.0),
    )

    assert price.status == PriceLocationStatus.FAILED_BREAKOUT
    assert decision.status in {LabGateStatus.WAIT, LabGateStatus.BLOCKED}


def test_price_location_flat_recent_candle_is_neutral_not_invalid():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=3,
            turnover_krw=1_000_000_000,
            session_high=102,
            vwap=99,
            upper_limit_price=130,
            breakout_level=101,
            recent_support_price=99,
            recent_candles_1m=({"high": 100, "low": 100, "close": 100},),
            momentum_1m=0.2,
            momentum_3m=0.1,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.upper_wick_risk is False
    assert "INVALID_RECENT_CANDLE" not in price.data_quality_flags


def test_price_location_ready_when_core_intraday_context_is_complete():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=3,
            session_high=102,
            vwap=99,
            vwap_ready=True,
            recent_support_price=99,
            recent_support_ready=True,
            recent_support_candle_count=3,
            completed_minute_bar_count=3,
            minute_bar_present=True,
            recent_candles_1m=(
                {"high": 101, "low": 99, "close": 100, "completed": True},
                {"high": 102, "low": 99, "close": 100, "completed": True},
            ),
            momentum_1m=0.2,
            momentum_3m=0.1,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.readiness == PriceLocationReadiness.READY
    assert price.readiness_reason_codes == ()
    assert price.provisional is False


def test_price_location_deep_pullback_below_vwap_with_weak_momentum_is_not_ready():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=94,
            return_pct=3,
            session_high=100,
            vwap=96,
            momentum_1m=-0.1,
            momentum_3m=-0.5,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=3.0),
        price_location=price,
        snapshot=_snapshot("000001", 3.0),
    )

    assert price.status == PriceLocationStatus.DEEP_PULLBACK
    assert decision.status not in {LabGateStatus.READY, LabGateStatus.READY_SMALL}


def test_price_location_missing_vwap_does_not_calculate_vwap_gap():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=3,
            session_high=102,
            momentum_1m=0.2,
            momentum_3m=0.1,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.vwap_gap_pct is None
    assert "MISSING_VWAP" in price.data_quality_flags


def test_price_location_missing_session_high_keeps_pullback_unknown():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=3,
            vwap=99,
            momentum_1m=0.2,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.pullback_from_high_pct is None
    assert price.status == PriceLocationStatus.UNKNOWN
    assert price.readiness == PriceLocationReadiness.MISSING_CORE
    assert "PRICE_LOCATION_SESSION_HIGH_MISSING" in price.readiness_reason_codes
    assert "MISSING_SESSION_HIGH" in price.data_quality_flags


def test_price_location_active_minute_support_is_provisional_during_open_warmup():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=4,
            session_high=101,
            vwap=99.5,
            recent_support_price=99,
            recent_support_source="active_1m_low_provisional",
            recent_support_ready=False,
            completed_minute_bar_count=0,
            minute_bar_present=True,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.readiness == PriceLocationReadiness.PROVISIONAL
    assert price.provisional is True
    assert "PRICE_LOCATION_ACTIVE_1M_SUPPORT_PROVISIONAL" in price.readiness_reason_codes
    assert "PRICE_LOCATION_PROVISIONAL" in price.reason_codes


def test_price_location_no_minute_bar_is_warmup_not_generic_unknown():
    price = PriceLocationEvaluator().evaluate(
        PriceLocationInput(
            symbol="000001",
            current_price=100,
            return_pct=4,
            session_high=101,
            stock_role=StockRole.LEADER,
            theme_status=ThemeLabThemeStatus.LEADING_THEME,
            market_status=MarketStatus.SELECTIVE,
        )
    )

    assert price.readiness == PriceLocationReadiness.WARMUP
    assert price.provisional is False
    assert "PRICE_LOCATION_WARMUP" in price.reason_codes
    assert "PRICE_LOCATION_NO_MINUTE_BAR" in price.readiness_reason_codes


def test_unknown_price_location_waits_or_observes_not_always_blocked():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=5.0),
        price_location=_price_location(PriceLocationStatus.UNKNOWN, reason_codes=("PRICE_LOCATION_UNKNOWN",)),
        snapshot=_snapshot("000001", 5.0),
    )

    assert decision.status in {LabGateStatus.WAIT, LabGateStatus.OBSERVE}


def test_price_location_score_alone_does_not_make_ready():
    weak_theme = ThemeBreadthEngine().calculate(
        [("t", "테마", [_member("t", "000001"), _member("t", "000002")])],
        [_snapshot("000001", 1.0), _snapshot("000002", -2.0)],
    )[0]
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=weak_theme,
        watch=_watch(role=StockRole.LEADER, return_pct=6.0),
        price_location=_price_location(PriceLocationStatus.GOOD_PULLBACK, score=95.0),
        snapshot=_snapshot("000001", 6.0),
    )

    assert decision.status != LabGateStatus.READY


def test_ready_small_has_position_multiplier_below_one():
    decision = ThemeLabHybridGate().evaluate(
        market=MarketStrengthSnapshot(MarketStatus.SELECTIVE),
        theme=_leading_theme(),
        watch=_watch(role=StockRole.LEADER, return_pct=8.0),
        price_location=_price_location(PriceLocationStatus.BREAKOUT_CONTINUATION),
        snapshot=_snapshot("000001", 8.0),
    )

    assert decision.status == LabGateStatus.READY_SMALL
    assert 0 < decision.position_size_multiplier < 1.0


def _kosdaq_weak_snapshots() -> list[StockSnapshot]:
    return [
        _snapshot("000001", 5.0, current_price=105, session_high=108, turnover=5_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000002", -1.0, current_price=99, turnover=4_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000003", -1.2, current_price=98.8, turnover=3_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000004", -0.7, current_price=99.3, turnover=2_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("100001", 0.5, current_price=100.5, metadata={"market": "KOSPI", "prev_close": 100}),
        _snapshot("100002", 0.2, current_price=100.2, metadata={"market": "KOSPI", "prev_close": 100}),
    ]


def _kosdaq_healthy_snapshots() -> list[StockSnapshot]:
    return [
        _snapshot("000001", 5.0, current_price=105, session_high=108, turnover=5_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000002", 1.0, current_price=101, turnover=4_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000003", 1.2, current_price=101.2, turnover=3_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("000004", 0.7, current_price=100.7, turnover=2_000_000, metadata={"market": "KOSDAQ", "prev_close": 100}),
        _snapshot("100001", 0.5, current_price=100.5, metadata={"market": "KOSPI", "prev_close": 100}),
        _snapshot("100002", 0.2, current_price=100.2, metadata={"market": "KOSPI", "prev_close": 100}),
    ]


def _member(theme_id: str, code: str) -> ThemeMembership:
    return ThemeMembership(theme_id=theme_id, stock_code=code, stock_name=f"종목{code}", active=True, trade_eligible=True)


def _snapshot(
    code: str,
    change_rate: float,
    *,
    name: str = "",
    turnover: float = 1_000_000,
    current_price: float = 0.0,
    session_high: float | None = None,
    metadata: dict | None = None,
) -> StockSnapshot:
    return StockSnapshot(
        stock_code=code,
        stock_name=name or f"종목{code}",
        current_price=current_price,
        change_rate=change_rate,
        turnover=turnover,
        volume=1000,
        session_high=current_price if session_high is None else session_high,
        metadata=dict(metadata or {}),
    )


def _leading_theme():
    return ThemeBreadthEngine(
        ThemeLabConfig(theme_status=ThemeStatusThresholds(min_strong_count_for_leading=2, min_leader_count_for_leading=1))
    ).calculate(
        [("t", "테마", [_member("t", "000001"), _member("t", "000002"), _member("t", "000003")])],
        [_snapshot("000001", 12.0, turnover=5_000_000_000), _snapshot("000002", 6.0), _snapshot("000003", 4.0)],
    )[0]


def _watch(*, role: StockRole, return_pct: float, symbol: str = "000001") -> WatchSetSnapshot:
    return WatchSetSnapshot(
        calculated_at="",
        symbol=symbol,
        primary_theme="t",
        return_pct=return_pct,
        turnover_krw=5_000_000_000,
        condition_level=3 if return_pct >= 5 else 2,
        stock_role=role,
    )


def _risk_off_market(*, breadth_pct: float = 0.50, turnover_weighted_return_pct: float = 0.5) -> MarketStrengthSnapshot:
    side_detail = {
        "status": MarketStatus.RISK_OFF.value,
        "raw_status": MarketStatus.RISK_OFF.value,
        "confirmed_status": MarketStatus.RISK_OFF.value,
        "index_return_pct": -3.2,
        "breadth_pct": breadth_pct,
        "breadth_ready": True,
        "breadth_sample_count": 140,
        "breadth_gate_usable": True,
        "turnover_weighted_return_pct": turnover_weighted_return_pct,
        "reason_codes": ["KOSDAQ_MARKET_RISK_OFF", "MARKET_RISK_OFF_CONFIRMED"],
        "data_quality_flags": [],
    }
    return MarketStrengthSnapshot(
        MarketStatus.RISK_OFF,
        kospi_return_pct=0.1,
        kosdaq_return_pct=-3.2,
        kosdaq_status=MarketStatus.RISK_OFF,
        kosdaq_confirmed_status=MarketStatus.RISK_OFF,
        kosdaq_index_return_pct=-3.2,
        side_statuses={MarketSide.KOSDAQ.value: side_detail},
        side_confirmation_states={
            MarketSide.KOSDAQ.value: {
                "confirmed_status": MarketStatus.RISK_OFF.value,
                "current_raw_status": MarketStatus.RISK_OFF.value,
                "confirmation_pending": False,
                "recovery_pending": False,
                "reason_codes": ["MARKET_RISK_OFF_CONFIRMED"],
            }
        },
    )


def _risk_off_watch() -> WatchSetSnapshot:
    return WatchSetSnapshot(
        calculated_at="2026-06-01T09:05:00",
        symbol="000001",
        primary_theme="t",
        return_pct=6.0,
        turnover_krw=5_000_000_000,
        condition_level=3,
        stock_role=StockRole.LEADER,
        candidate_market=MarketSide.KOSDAQ.value,
        candidate_market_status=MarketStatus.RISK_OFF.value,
        candidate_market_confirmed_status=MarketStatus.RISK_OFF.value,
        candidate_index_return_pct=-3.2,
        candidate_breadth_pct=0.50,
        candidate_breadth_ready=True,
        candidate_breadth_sample_count=140,
        candidate_breadth_gate_usable=True,
        vwap=9950,
        recent_support_price=9900,
        recent_support_ready=True,
    )


def _risk_off_leading_theme() -> ThemeConditionSnapshot:
    return ThemeConditionSnapshot(
        calculated_at="2026-06-01T09:05:00",
        theme_id="t",
        theme_name="AI",
        raw_total_members=5,
        eligible_total_members=5,
        alive_count=5,
        strong_count=3,
        leader_count=2,
        alive_ratio=1.0,
        strong_ratio=0.60,
        leader_ratio=0.40,
        condition_score=85.0,
        theme_status=ThemeLabThemeStatus.LEADING_THEME,
    )


def _risk_off_snapshot() -> StockSnapshot:
    return _snapshot(
        "000001",
        6.0,
        turnover=5_000_000_000,
        current_price=10000,
        session_high=10300,
        metadata={
            "prev_close": 9434,
            "vwap": 9950,
            "vwap_ready": True,
            "recent_support_price": 9900,
            "recent_support_ready": True,
            "recent_support_candle_count": 4,
            "quote_ts": "2026-06-01T09:05:00",
        },
    )


def _price_location(
    status: PriceLocationStatus,
    *,
    score: float | None = None,
    reason_codes: tuple[str, ...] = (),
    data_quality_flags: tuple[str, ...] = (),
) -> PriceLocationResult:
    return PriceLocationResult(
        symbol="000001",
        status=status,
        score=score if score is not None else 80.0,
        reason_codes=reason_codes or (status.value,),
        data_quality_flags=data_quality_flags,
    )


def _risk_input(
    *,
    stock_role: StockRole,
    theme_status: ThemeLabThemeStatus = ThemeLabThemeStatus.LEADING_THEME,
    return_pct: float = 0.0,
    vi_active: bool = False,
    seconds_since_vi_release: int = 0,
    upper_limit_gap_pct: float = 100.0,
    pullback_from_high_pct: float = 100.0,
    momentum_1m: float = 0.0,
    momentum_3m: float = 0.0,
    turnover_krw: float = 1_000_000_000,
    trade_strength: float = 120.0,
    leader_momentum_status: str = "",
) -> TradeabilityRiskInput:
    return TradeabilityRiskInput(
        market_status=MarketStatus.SELECTIVE,
        theme_status=theme_status,
        stock_role=stock_role,
        return_pct=return_pct,
        condition_level=3,
        vi_active=vi_active,
        seconds_since_vi_release=seconds_since_vi_release,
        upper_limit_gap_pct=upper_limit_gap_pct,
        pullback_from_high_pct=pullback_from_high_pct,
        momentum_1m=momentum_1m,
        momentum_3m=momentum_3m,
        turnover_krw=turnover_krw,
        trade_strength=trade_strength,
    )
