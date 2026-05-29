import sqlite3

from storage.db import TradingDatabase
from trading.strategy.gates import ThemeStrengthGate
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import Candidate, CandidateState, StrategyProfile
from trading.strategy.themes import ThemeMapping, ThemeRepository, import_theme_mappings_csv


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

    assert db.list_theme_mappings(enabled=None)[0].enabled is False
    assert repo.themes_for_code("123456") == []
    assert enriched.theme_ids == []
    assert ThemeStrengthGate(repo, MarketDataStore()).evaluate([Candidate(code="123456", state=CandidateState.WATCHING)]) == []
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


def test_import_theme_mappings_csv_validates_and_upserts_idempotently(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    csv_path = tmp_path / "theme_mappings.csv"
    csv_path.write_text(
        "\n".join(
            [
                "code,name,market,theme_id,theme_name,strategy_profile,enabled,sub_theme,is_large_cap,is_leader_candidate,base_priority,is_signal_stock,memo",
                "A005930,Samsung,KOSPI,semiconductor,Semiconductor,SEMICONDUCTOR_SIGNAL_PROFILE,Y,leader,true,1,100,N,signal",
                "123456,Theme Stock,KOSDAQ,robot,Robot,KOSDAQ_THEME_PROFILE,0,,0,false,75,Y,disabled row",
                "ABC,Bad,KOSDAQ,bad,Bad,KOSDAQ_THEME_PROFILE,1,,0,0,50,0,bad",
                "654321,Too High,KOSDAQ,bad2,Bad2,KOSDAQ_THEME_PROFILE,1,,0,0,101,0,bad",
            ]
        ),
        encoding="utf-8-sig",
    )

    first = import_theme_mappings_csv(db, csv_path)
    second = import_theme_mappings_csv(db, csv_path)

    assert first.total_rows == 4
    assert first.inserted == 2
    assert first.updated == 0
    assert first.skipped == 2
    assert first.disabled == 1
    assert any("invalid code" in error for error in first.errors)
    assert any("base_priority out of range" in error for error in first.errors)
    assert second.inserted == 0
    assert second.updated == 2
    assert second.skipped == 2
    assert len(db.list_theme_mappings(enabled=None)) == 2
    samsung = db.theme_mappings_for_code("005930", enabled=None)[0]
    disabled = db.theme_mappings_for_code("123456", enabled=None)[0]
    assert samsung.code == "005930"
    assert samsung.market == "KOSPI"
    assert samsung.is_large_cap is True
    assert samsung.is_leader_candidate is True
    assert samsung.is_signal_stock is False
    assert samsung.base_priority == 100
    assert disabled.enabled is False
    db.close()


def test_import_theme_mappings_csv_left_pads_excel_stripped_codes(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    csv_path = tmp_path / "theme_mappings.csv"
    csv_path.write_text(
        "\n".join(
            [
                "code,name,market,theme_id,theme_name,strategy_profile,enabled,sub_theme,is_large_cap,is_leader_candidate,base_priority,is_signal_stock,memo",
                "5930,Samsung,KOSPI,semiconductor,Semiconductor,SEMICONDUCTOR_SIGNAL_PROFILE,1,,1,1,100,1,excel stripped",
                "70,Samyang,KOSPI,food,Food,KOSPI_LEADER_PROFILE,1,,1,0,70,0,excel stripped",
                "5930.0,Samsung Robot,KOSPI,robot,Robot,KOSPI_LEADER_PROFILE,1,,1,0,70,0,spreadsheet float",
                "1234567,Bad,KOSDAQ,bad,Bad,KOSDAQ_THEME_PROFILE,1,,0,0,50,0,bad",
            ]
        ),
        encoding="utf-8-sig",
    )

    result = import_theme_mappings_csv(db, csv_path)

    assert result.total_rows == 4
    assert result.inserted == 3
    assert result.skipped == 1
    assert "short numeric codes were left-padded to 6 digits" in result.warnings
    assert any("invalid code '1234567'" in error for error in result.errors)
    assert {mapping.code for mapping in db.list_theme_mappings(enabled=None)} == {"005930", "000070"}
    assert {mapping.theme_id for mapping in db.theme_mappings_for_code("005930", enabled=None)} == {
        "semiconductor",
        "robot",
    }
    db.close()


def test_import_theme_mappings_csv_reports_missing_required_columns(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    csv_path = tmp_path / "theme_mappings.csv"
    csv_path.write_text("code,name\n005930,Samsung\n", encoding="utf-8-sig")

    result = import_theme_mappings_csv(db, csv_path)

    assert result.total_rows == 0
    assert result.inserted == 0
    assert any("missing required columns" in error for error in result.errors)
    assert db.list_theme_mappings(enabled=None) == []
    db.close()
