from datetime import datetime

from storage.db import TradingDatabase
from trading.theme_engine.cluster_detector import DynamicThemeClusterDetector
from trading.theme_engine.models import CanonicalTheme, StockSnapshot, ThemeMembership
from trading.theme_engine.repository import ThemeEngineRepository


def _snap(code: str, change: float = 2.0) -> StockSnapshot:
    return StockSnapshot(stock_code=code, change_rate=change, momentum_5m=change, turnover_strength=2.5)


def test_dynamic_cluster_created_for_intraday_co_movement(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)

    clusters = DynamicThemeClusterDetector(repo).detect(
        [_snap("000001"), _snap("000002"), _snap("000003")],
        now=datetime(2026, 5, 30, 9, 10),
    )

    assert len(clusters) == 1
    assert clusters[0].matched_theme_id.startswith("dynamic_20260530_0910")
    assert len(clusters[0].stock_codes) == 3
    db.close()


def test_dynamic_cluster_matches_existing_theme_by_membership_overlap(tmp_path):
    db = TradingDatabase(str(tmp_path / "themes.sqlite3"))
    repo = ThemeEngineRepository(db)
    repo.upsert_canonical_theme(CanonicalTheme("furiosa_ai", "퓨리오사AI", "퓨리오사AI"))
    repo.upsert_current_membership(ThemeMembership("furiosa_ai", "000001", membership_score=0.9))
    repo.upsert_current_membership(ThemeMembership("furiosa_ai", "000002", membership_score=0.9))

    cluster = DynamicThemeClusterDetector(repo).detect([_snap("000001"), _snap("000002"), _snap("000003")])[0]

    assert cluster.matched_theme_id == "furiosa_ai"
    db.close()
