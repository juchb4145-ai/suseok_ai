from trading.theme_engine.leadership import (
    MarketPhase,
    StockLeadershipRole,
    ThemeLeadershipRanker,
    ThemeLeadershipStatus,
    WatchsetSelector,
)
from trading.theme_engine.models import StockSnapshot, ThemeMembership


def test_rt_tls_ranks_themes_without_condition_profiles():
    ranks = ThemeLeadershipRanker().rank(
        [
            ("ai", "AI", [_member("ai", "000001", 0.95), _member("ai", "000002", 0.88), _member("ai", "000003", 0.80), _member("ai", "000004", 0.72)]),
            ("bio", "BIO", [_member("bio", "100001", 0.90), _member("bio", "100002", 0.80), _member("bio", "100003", 0.75)]),
        ],
        [
            _snapshot("000001", 7.5, turnover=8_000_000_000, execution_strength=160, momentum=1.2),
            _snapshot("000002", 5.8, turnover=5_000_000_000, execution_strength=145, momentum=0.9),
            _snapshot("000003", 3.4, turnover=2_000_000_000, execution_strength=130, momentum=0.5),
            _snapshot("000004", 1.2, turnover=800_000_000, execution_strength=110, momentum=0.2),
            _snapshot("100001", 1.2, turnover=700_000_000, execution_strength=100),
            _snapshot("100002", -0.3, turnover=300_000_000, execution_strength=90),
            _snapshot("100003", 0.2, turnover=200_000_000, execution_strength=85),
        ],
    )

    assert ranks[0].theme_id == "ai"
    assert ranks[0].snapshot is not None
    assert ranks[0].snapshot.theme_score > 0
    assert ranks[0].snapshot.status in {ThemeLeadershipStatus.LEADING_THEME, ThemeLeadershipStatus.SPREADING_THEME}
    assert ranks[0].snapshot.condition_boost_count == 0
    assert ranks[0].snapshot.output_mode == "OBSERVE"


def test_condition_include_is_recorded_as_boost_only_not_ready_or_order_intent():
    ranks = ThemeLeadershipRanker().rank(
        [("ai", "AI", [_member("ai", "000001", 0.95), _member("ai", "000002", 0.88), _member("ai", "000003", 0.80)])],
        [
            _snapshot("000001", 6.0, turnover=5_000_000_000, execution_strength=150, momentum=0.8),
            _snapshot("000002", 4.2, turnover=4_000_000_000, execution_strength=140, momentum=0.7),
            _snapshot("000003", 1.0, turnover=1_000_000_000, execution_strength=100),
        ],
        condition_boosts={"A000002": "kiwoom_strong_include"},
    )
    theme = ranks[0].snapshot
    assert theme is not None
    boosted = next(stock for stock in theme.stocks if stock.stock_code == "000002")

    assert boosted.condition_boost > 0
    assert boosted.condition_include_count == 1
    assert "condition_search" in boosted.discovery_sources
    assert "kiwoom_strong_include" in boosted.discovery_sources
    assert boosted.ready_allowed is False
    assert boosted.order_intent_allowed is False

    result = WatchsetSelector().select(ranks, market_phase=MarketPhase.SELECTIVE)

    assert result.output_mode == "OBSERVE"
    assert result.ready_allowed is False
    assert result.order_intent_allowed is False


def test_watchset_filters_leader_only_non_leaders_late_laggards_and_overheated_members():
    ranks = ThemeLeadershipRanker().rank(
        [
            (
                "robot",
                "Robot",
                [
                    _member("robot", "000001", 0.95),
                    _member("robot", "000002", 0.82),
                    _member("robot", "000003", 0.60),
                    _member("robot", "000004", 0.70),
                    _member("robot", "000005", 0.68),
                ],
            )
        ],
        [
            _snapshot("000001", 10.0, turnover=20_000_000_000, execution_strength=165, momentum=1.0, current_price=110, high=113, vwap=105),
            _snapshot("000002", 1.2, turnover=3_000_000_000, execution_strength=100, momentum=-0.1, current_price=101.2, high=104, vwap=100),
            _snapshot("000003", 12.5, turnover=200_000_000, execution_strength=180, momentum=1.5, current_price=112.5, high=112.5, vwap=100),
            _snapshot("000004", -0.5, turnover=700_000_000, execution_strength=80, current_price=99.5, high=101, vwap=100),
            _snapshot("000005", 0.2, turnover=500_000_000, execution_strength=85, current_price=100.2, high=101, vwap=100),
        ],
    )
    theme = ranks[0].snapshot
    assert theme is not None
    assert theme.status == ThemeLeadershipStatus.LEADER_ONLY_THEME
    assert any(stock.role == StockLeadershipRole.LATE_LAGGARD for stock in theme.stocks)
    assert any(stock.role == StockLeadershipRole.OVERHEATED for stock in theme.stocks)

    result = WatchsetSelector().select(ranks, market_phase=MarketPhase.SELECTIVE)

    assert result.selected_symbols == ("000001",)
    assert all(stock.role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER} for stock in result.selected)
    assert all(stock.role not in {StockLeadershipRole.LATE_LAGGARD, StockLeadershipRole.OVERHEATED} for stock in result.selected)
    assert result.excluded_late_laggard_count >= 1
    assert result.excluded_overheated_count >= 1


def test_data_shortage_stays_data_wait_not_weak_theme():
    ranks = ThemeLeadershipRanker().rank(
        [
            (
                "space",
                "Space",
                [
                    _member("space", "000001", 0.90),
                    _member("space", "000002", 0.85),
                    _member("space", "000003", 0.80),
                    _member("space", "000004", 0.75),
                    _member("space", "000005", 0.70),
                ],
            )
        ],
        [_snapshot("000001", 8.0, turnover=5_000_000_000, execution_strength=150, momentum=1.0)],
    )

    assert ranks[0].snapshot is not None
    assert ranks[0].snapshot.status == ThemeLeadershipStatus.DATA_WAIT
    assert ranks[0].snapshot.status != ThemeLeadershipStatus.WEAK_THEME
    assert "LOW_SNAPSHOT_COVERAGE" in ranks[0].snapshot.data_quality_flags


def _member(theme_id: str, code: str, score: float) -> ThemeMembership:
    return ThemeMembership(
        theme_id=theme_id,
        stock_code=code,
        stock_name=f"stock-{code}",
        membership_score=score,
        source_count=2,
        active=True,
        trade_eligible=True,
    )


def _snapshot(
    code: str,
    change_rate: float,
    *,
    turnover: float,
    execution_strength: float,
    momentum: float = 0.0,
    current_price: float = 100.0,
    high: float | None = None,
    vwap: float = 99.0,
) -> StockSnapshot:
    return StockSnapshot(
        stock_code=code,
        stock_name=f"stock-{code}",
        current_price=current_price,
        change_rate=change_rate,
        volume=100_000,
        turnover=turnover,
        execution_strength=execution_strength,
        best_bid=current_price - 1,
        best_ask=current_price + 1,
        session_high=high if high is not None else current_price + 2,
        session_low=current_price - 4,
        momentum_1m=momentum,
        momentum_3m=momentum,
        momentum_5m=momentum,
        metadata={"vwap": vwap, "prev_close": 100, "day_high": high if high is not None else current_price + 2},
    )
