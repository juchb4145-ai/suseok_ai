from trading.theme_engine.models import StockSnapshot, ThemeMembership
from trading.theme_engine.scorer import ThemeScoringEngine


def test_advanced_reason_codes_for_sparse_theme():
    memberships = [ThemeMembership("t", "000001", membership_score=0.9, active=True, trade_eligible=True)]
    snapshot = StockSnapshot(stock_code="000001", change_rate=3.0, turnover=1000, turnover_strength=0.1)

    scored = ThemeScoringEngine().score_theme("t", "Tiny", memberships, [snapshot])

    assert "TOO_FEW_MEMBERS" in scored.details["reason_codes"]
    assert "LOW_TURNOVER" in scored.details["reason_codes"]
    assert 0 <= scored.theme_score <= 100


def test_active_promotion_dry_run_requires_breadth_not_leader_only():
    memberships = [
        ThemeMembership("t", "000001", membership_score=0.9, active=True, trade_eligible=True),
        ThemeMembership("t", "000002", membership_score=0.9, active=True, trade_eligible=True),
        ThemeMembership("t", "000003", membership_score=0.9, active=True, trade_eligible=True),
    ]
    snapshots = [
        StockSnapshot("000001", change_rate=5.0, turnover=10_000_000_000, turnover_strength=3, momentum_5m=4),
        StockSnapshot("000002", change_rate=4.0, turnover=8_000_000_000, turnover_strength=3, momentum_5m=3),
        StockSnapshot("000003", change_rate=3.0, turnover=7_000_000_000, turnover_strength=3, momentum_5m=2),
    ]

    scored = ThemeScoringEngine().score_theme("t", "Broad", memberships, snapshots)

    assert scored.breadth == 1.0
    assert scored.top3_concentration == 1.0
    assert scored.details["active_promotion_dry_run"] == "ACTIVE"
