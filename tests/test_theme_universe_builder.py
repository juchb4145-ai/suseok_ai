from storage.db import TradingDatabase
from trading.theme_engine.models import CanonicalTheme, ThemeMembership, ThemeStatus
from trading.theme_engine.repository import ThemeEngineRepository
from trading.theme_engine.universe import ThemeUniverseBuilder, ThemeUniverseConfig


def test_universe_builder_filters_active_trade_eligible_and_max_size(tmp_path):
    db = TradingDatabase(str(tmp_path / "theme.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(CanonicalTheme("active_theme", "A", "A", status=ThemeStatus.ACTIVE, trade_eligible=True))
    repo.upsert_canonical_theme(CanonicalTheme("candidate_theme", "C", "C", status=ThemeStatus.CANDIDATE))
    repo.upsert_current_membership(ThemeMembership("active_theme", "000001", membership_score=0.9, source_count=3, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("active_theme", "000002", membership_score=0.4, source_count=1, active=True, trade_eligible=True))
    repo.upsert_current_membership(ThemeMembership("candidate_theme", "000003", membership_score=0.9, active=True, trade_eligible=True))

    builder = ThemeUniverseBuilder(repo, ThemeUniverseConfig(max_size=1, min_membership_score=0.55))

    assert builder.build_active_universe() == ["000001"]
    assert builder.build_trade_eligible_universe() == ["000001"]
    assert builder.themes_by_stock("A000001")[0].theme_id == "active_theme"
    assert builder.stocks_by_theme("active_theme")[0].stock_code == "000001"
    db.close()
