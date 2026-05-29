import sqlite3

from storage.db import TradingDatabase
from trading.strategy.models import Candidate, StrategyProfile
from trading.strategy.themes import ThemeMapping, ThemeRepository


def test_theme_mappings_migration_unique_and_seed_idempotency(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)

    repo.seed_minimal_defaults()
    repo.seed_minimal_defaults()

    mappings = db.list_theme_mappings()
    assert len(mappings) == 4
    assert {mapping.code for mapping in mappings} == {"005930", "000660", "005380", "000270"}

    try:
        db.conn.execute(
            """
            INSERT INTO theme_mappings(code, theme_id, name)
            VALUES (?, ?, ?)
            """,
            ("005930", "semiconductor", "duplicate"),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("UNIQUE(code, theme_id) should reject duplicate rows")
    db.close()


def test_disabled_mapping_is_ignored_by_repository(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    repo.upsert_mapping(
        ThemeMapping(
            code="123456",
            name="Disabled",
            market="KOSDAQ",
            theme_id="disabled_theme",
            theme_name="Disabled Theme",
            enabled=False,
        )
    )

    candidate = Candidate(code="123456")
    enriched = repo.enrich_candidate(candidate)

    assert repo.themes_for_code("123456") == []
    assert enriched.theme_ids == []
    db.close()


def test_repository_normalizes_code_and_enriches_without_saving_candidate(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = ThemeRepository(db)
    repo.upsert_mapping(
        ThemeMapping(
            code="A005930",
            name="Samsung Electronics",
            market="KOSPI",
            theme_id="semiconductor",
            theme_name="Semiconductor",
            strategy_profile=StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE,
            base_priority=100,
            is_signal_stock=True,
        )
    )

    enriched = repo.enrich_candidate(Candidate(code="A005930"))

    assert enriched.code == "005930"
    assert enriched.theme_ids == ["semiconductor"]
    assert enriched.market == "KOSPI"
    assert enriched.strategy_profile == StrategyProfile.SEMICONDUCTOR_SIGNAL_PROFILE
    assert db.load_candidate("", "005930") is None
    db.close()
