import pytest

from kiwoom.client import MockKiwoomClient
from main import build_observe_runtime
from storage.db import TradingDatabase
from trading.strategy.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG_KEY,
    StrategyRuntimeConfigRepository,
)
from trading.strategy.models import FillPolicy
from trading.strategy.runtime import StrategyRuntimeConfig


def test_config_row_missing_returns_default(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)

    result = repo.load()

    assert result.used_default is True
    assert result.config_key == DEFAULT_CONFIG_KEY
    assert result.config_version == CONFIG_VERSION
    assert result.config.order_mode.value == "OBSERVE"
    assert result.warnings == []
    db.close()


def test_config_save_load_round_trip_and_version(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)
    config = StrategyRuntimeConfig(
        evaluation_interval_sec=12,
        condition_profiles_enabled=False,
        leader_watch_codes=["A005930", "000660", "005930", ""],
        semiconductor_signal_codes=["000660"],
        holding_watch_codes=["035420"],
        index_watch_codes={"KOSPI": "001", "KOSDAQ": "101"},
        virtual_fill_policy=FillPolicy.CONSERVATIVE,
        review_save_enabled=False,
        max_candidates_to_watch=33,
        realtime_subscription_limit=44,
    )

    saved = repo.save(config)
    row = db.load_strategy_runtime_setting(DEFAULT_CONFIG_KEY)
    loaded = repo.load()

    assert row["config_key"] == DEFAULT_CONFIG_KEY
    assert row["config_version"] == CONFIG_VERSION
    assert saved.config.leader_watch_codes == ["005930", "000660"]
    assert loaded.config.evaluation_interval_sec == 12
    assert loaded.config.condition_profiles_enabled is False
    assert loaded.config.virtual_fill_policy == FillPolicy.CONSERVATIVE
    assert loaded.config.holding_watch_codes == ["035420"]
    db.close()


def test_old_config_merges_defaults_and_unknown_field_warns(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    db.save_strategy_runtime_setting(
        DEFAULT_CONFIG_KEY,
        1,
        '{"evaluation_interval_sec": 9, "unknown_future_field": true}',
    )

    result = StrategyRuntimeConfigRepository(db).load()

    assert result.config.evaluation_interval_sec == 9
    assert result.config.leader_watch_codes == ["005930", "000660"]
    assert "CONFIG_UNKNOWN_FIELD_IGNORED:unknown_future_field" in result.warnings
    db.close()


def test_invalid_json_and_tampered_auto_mode_fallback_to_default(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)
    db.save_strategy_runtime_setting(DEFAULT_CONFIG_KEY, 1, "{")

    invalid_json = repo.load()

    assert invalid_json.used_default is True
    assert any(warning.startswith("CONFIG_JSON_INVALID") for warning in invalid_json.warnings)

    db.save_strategy_runtime_setting(DEFAULT_CONFIG_KEY, 1, '{"order_mode": "AUTO_A"}')
    tampered = repo.load()

    assert tampered.used_default is True
    assert tampered.config.order_mode.value == "OBSERVE"
    assert any("CONFIG_INVALID_FALLBACK" in warning for warning in tampered.warnings)
    db.close()


def test_invalid_save_preserves_existing_config(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)
    repo.save(StrategyRuntimeConfig(evaluation_interval_sec=7))
    original = db.load_strategy_runtime_setting(DEFAULT_CONFIG_KEY)["config_json"]

    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(leader_watch_codes=["KOSPI"]))

    assert db.load_strategy_runtime_setting(DEFAULT_CONFIG_KEY)["config_json"] == original
    assert repo.load().config.evaluation_interval_sec == 7
    db.close()


def test_validation_rejects_invalid_values_and_warns_on_protected_limit(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)

    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(evaluation_interval_sec=0))
    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(evaluation_interval_sec=3601))
    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(virtual_fill_policy="bad"))
    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(condition_profiles_enabled=1))
    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(leader_watch_codes=["ABCDEF"]))
    with pytest.raises(ValueError):
        repo.save(StrategyRuntimeConfig(leader_watch_codes=["KOSDAQ"]))

    saved = repo.save(StrategyRuntimeConfig(realtime_subscription_limit=1, max_candidates_to_watch=1))

    assert "PROTECTED_WATCH_COUNT_EXCEEDS_REALTIME_LIMIT" in saved.warnings
    db.close()


def test_config_db_read_error_falls_back_to_default(tmp_path, monkeypatch):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    repo = StrategyRuntimeConfigRepository(db)

    monkeypatch.setattr(db, "load_strategy_runtime_setting", lambda key: (_ for _ in ()).throw(RuntimeError("db boom")))

    result = repo.load()

    assert result.used_default is True
    assert result.config.order_mode.value == "OBSERVE"
    assert "CONFIG_DB_READ_FAILED:db boom" in result.warnings
    db.close()


def test_main_build_observe_runtime_uses_db_config(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    StrategyRuntimeConfigRepository(db).save(
        StrategyRuntimeConfig(
            evaluation_interval_sec=17,
            leader_watch_codes=["035420"],
            holding_watch_codes=["000270"],
            virtual_fill_policy=FillPolicy.OPTIMISTIC,
            realtime_subscription_limit=77,
        )
    )

    runtime = build_observe_runtime(MockKiwoomClient(), db)

    assert runtime.config.evaluation_interval_sec == 17
    assert runtime.config.leader_watch_codes == ["035420"]
    assert runtime.config.holding_watch_codes == ["000270"]
    assert runtime.config.virtual_fill_policy == FillPolicy.OPTIMISTIC
    assert runtime.subscription_manager.max_codes == 77
    db.close()
