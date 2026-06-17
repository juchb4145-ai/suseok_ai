from trading.theme_engine.leadership import MarketPhase, StockLeadershipRole, ThemeLeadershipStatus
from trading.theme_engine.models import StockSnapshot, ThemeMembership
from trading.theme_engine.opening_burst import (
    OpeningReturnGrade,
    OpeningThemeBurstEngine,
    OpeningTurnoverSeedCollector,
)


def test_opt10032_rolling_seed_unions_and_dedupes_top_100_rows():
    collector = OpeningTurnoverSeedCollector()
    seeds = collector.collect(
        [
            [_seed("000001", 1, 1_000_000_000), _seed("000002", 2, 900_000_000), _seed("999999", 3, 800_000_000, is_etf=True)],
            [_seed("000002", 1, 1_500_000_000), _seed("000003", 2, 700_000_000)],
        ]
    )

    assert collector.rolling_schedule() == ("09:03", "09:06", "09:09", "09:12", "09:15")
    assert [seed.stock_code for seed in seeds] == ["000002", "000001", "000003"]
    assert next(seed for seed in seeds if seed.stock_code == "000002").seed_times == ("09:03", "09:06")
    assert "999999" not in {seed.stock_code for seed in seeds}


def test_opening_theme_burst_ranks_theme_without_condition_profiles():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[
            ("ai", "AI", [_member("ai", "000001"), _member("ai", "000002"), _member("ai", "000003"), _member("ai", "000004")]),
            ("bio", "BIO", [_member("bio", "100001"), _member("bio", "100002"), _member("bio", "100003")]),
        ],
        seed_batches=[
            [
                _seed("000001", 1, 9_000_000_000),
                _seed("000002", 2, 7_000_000_000),
                _seed("000003", 3, 4_000_000_000),
                _seed("100001", 20, 900_000_000),
            ]
        ],
        snapshots=[
            _snapshot("000001", 6.0, turnover=9_000_000_000, speed=1_500_000_000, execution=155, momentum=1.0),
            _snapshot("000002", 5.0, turnover=7_000_000_000, speed=1_100_000_000, execution=145, momentum=0.8),
            _snapshot("000003", 3.2, turnover=4_000_000_000, speed=800_000_000, execution=130, momentum=0.6),
            _snapshot("100001", 1.0, turnover=900_000_000, speed=100_000_000, execution=90),
        ],
    )

    top = result.ranked_themes[0]
    assert top.theme_id == "ai"
    assert top.status in {ThemeLeadershipStatus.LEADING_THEME, ThemeLeadershipStatus.SPREADING_THEME}
    assert top.snapshot is not None
    assert top.snapshot.strong_count == 3
    assert result.output_mode == "OBSERVE"
    assert result.ready_allowed is False
    assert result.order_intent_allowed is False
    assert result.selected_symbols


def test_single_plus_seven_percent_stock_is_not_recognized_as_leading_theme():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[("solo", "Solo", [_member("solo", "000001"), _member("solo", "000002"), _member("solo", "000003")])],
        seed_batches=[[_seed("000001", 1, 5_000_000_000)]],
        snapshots=[_snapshot("000001", 7.5, turnover=5_000_000_000, speed=1_000_000_000, execution=160, momentum=1.0)],
    )
    theme = result.ranked_themes[0].snapshot

    assert theme is not None
    assert theme.stocks[0].return_grade == OpeningReturnGrade.BURST
    assert theme.status == ThemeLeadershipStatus.WATCH_THEME
    assert "SINGLE_BURST_NOT_THEME" in theme.reason_codes
    assert result.selected_symbols == ()


def test_small_theme_with_two_strong_members_can_be_spreading_candidate():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[("small", "Small", [_member("small", "000001"), _member("small", "000002"), _member("small", "000003")])],
        seed_batches=[[_seed("000001", 1, 5_000_000_000), _seed("000002", 2, 3_000_000_000)]],
        snapshots=[
            _snapshot("000001", 5.5, turnover=5_000_000_000, speed=1_000_000_000, execution=150, momentum=0.8),
            _snapshot("000002", 3.6, turnover=3_000_000_000, speed=700_000_000, execution=130, momentum=0.5),
        ],
    )

    assert result.ranked_themes[0].status in {ThemeLeadershipStatus.LEADING_THEME, ThemeLeadershipStatus.SPREADING_THEME}
    assert result.ranked_themes[0].snapshot is not None
    assert result.ranked_themes[0].snapshot.cohesion_passed is True


def test_highest_leader_score_not_highest_return_becomes_leader():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[("robot", "Robot", [_member("robot", "000001"), _member("robot", "000002"), _member("robot", "000003")])],
        seed_batches=[[_seed("000001", 10, 600_000_000), _seed("000002", 1, 9_000_000_000), _seed("000003", 2, 5_000_000_000)]],
        snapshots=[
            _snapshot("000001", 9.0, turnover=600_000_000, speed=80_000_000, execution=95, momentum=0.1),
            _snapshot("000002", 5.2, turnover=9_000_000_000, speed=1_500_000_000, execution=165, momentum=1.0),
            _snapshot("000003", 3.4, turnover=5_000_000_000, speed=900_000_000, execution=130, momentum=0.6),
        ],
    )
    theme = result.ranked_themes[0].snapshot

    assert theme is not None
    assert theme.leader_symbol == "000002"
    leader = next(stock for stock in theme.stocks if stock.role == StockLeadershipRole.LEADER)
    highest_return = max(theme.stocks, key=lambda stock: stock.change_rate_pct)
    assert leader.stock_code != highest_return.stock_code
    assert leader.leader_score > highest_return.leader_score


def test_leader_only_theme_excludes_follower_late_laggard_and_overheated_from_watchset():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[
            (
                "two_top",
                "TwoTop",
                [
                    _member("two_top", "000001"),
                    _member("two_top", "000002"),
                    _member("two_top", "000003"),
                    _member("two_top", "000004"),
                    _member("two_top", "000005"),
                ],
            )
        ],
        seed_batches=[
            [
                _seed("000001", 1, 12_000_000_000),
                _seed("000002", 2, 9_000_000_000),
                _seed("000003", 5, 3_000_000_000),
                _seed("000004", 8, 1_000_000_000),
                _seed("000005", 9, 800_000_000),
            ]
        ],
        snapshots=[
            _snapshot("000001", 6.0, turnover=12_000_000_000, speed=1_400_000_000, execution=160, momentum=1.0),
            _snapshot("000002", 5.5, turnover=9_000_000_000, speed=1_100_000_000, execution=150, momentum=0.8),
            _snapshot("000003", 2.0, turnover=3_000_000_000, speed=500_000_000, execution=120, momentum=0.4),
            _snapshot("000004", 0.5, turnover=1_000_000_000, speed=150_000_000, execution=90, momentum=-0.1),
            _snapshot("000005", 8.0, turnover=800_000_000, speed=100_000_000, execution=100, momentum=0.2, pullback=0.0),
        ],
        market_phase=MarketPhase.EXPANSION,
    )
    theme = result.ranked_themes[0].snapshot

    assert theme is not None
    assert theme.status == ThemeLeadershipStatus.LEADER_ONLY_THEME
    assert any(stock.role == StockLeadershipRole.LATE_LAGGARD for stock in theme.stocks)
    assert any(stock.role == StockLeadershipRole.OVERHEATED for stock in theme.stocks)
    assert result.selected_symbols == ("000001", "000002")
    assert all(stock.role in {StockLeadershipRole.LEADER, StockLeadershipRole.CO_LEADER} for stock in result.selected)


def test_vi_or_upper_limit_near_stock_is_wait_or_blocked_not_ready():
    result = OpeningThemeBurstEngine().run(
        theme_inputs=[("risk", "Risk", [_member("risk", "000001"), _member("risk", "000002"), _member("risk", "000003")])],
        seed_batches=[[_seed("000001", 1, 8_000_000_000), _seed("000002", 2, 5_000_000_000), _seed("000003", 3, 4_000_000_000)]],
        snapshots=[
            _snapshot("000001", 8.0, turnover=8_000_000_000, speed=1_500_000_000, execution=170, momentum=1.0, vi_active=True),
            _snapshot("000002", 5.0, turnover=5_000_000_000, speed=900_000_000, execution=140, momentum=0.6),
            _snapshot("000003", 3.5, turnover=4_000_000_000, speed=800_000_000, execution=130, momentum=0.5),
        ],
    )
    theme = result.ranked_themes[0].snapshot
    risky = next(stock for stock in theme.stocks if stock.stock_code == "000001")

    assert risky.timing_status in {"WAIT", "BLOCKED"}
    assert risky.ready_allowed is False
    assert risky.order_intent_allowed is False
    assert risky.role == StockLeadershipRole.OVERHEATED


def _member(theme_id: str, code: str) -> ThemeMembership:
    return ThemeMembership(theme_id=theme_id, stock_code=code, stock_name=f"stock-{code}", membership_score=0.85, active=True, trade_eligible=True)


def _seed(code: str, rank: int, turnover: float, **raw) -> dict:
    return {
        "stock_code": code,
        "stock_name": f"stock-{code}",
        "rank": rank,
        "turnover_krw": turnover,
        **raw,
    }


def _snapshot(
    code: str,
    change_rate: float,
    *,
    turnover: float,
    speed: float,
    execution: float,
    momentum: float = 0.0,
    current_price: float = 100.0,
    pullback: float = 2.0,
    vi_active: bool = False,
) -> StockSnapshot:
    high = current_price / (1.0 - pullback / 100.0) if pullback < 100 else current_price
    return StockSnapshot(
        stock_code=code,
        stock_name=f"stock-{code}",
        current_price=current_price,
        change_rate=change_rate,
        volume=100_000,
        turnover=turnover,
        execution_strength=execution,
        best_bid=current_price - 0.5,
        best_ask=current_price + 0.5,
        session_high=high,
        momentum_1m=momentum,
        momentum_3m=momentum,
        momentum_5m=momentum,
        metadata={
            "opening_turnover_speed_krw_per_min": speed,
            "avg_turnover_20d_krw": 20_000_000_000,
            "minutes_since_open": 5,
            "pullback_from_high_pct": pullback,
            "upper_limit_gap_pct": 10.0,
            "vi_active": vi_active,
            "vwap": 98.0,
        },
    )
