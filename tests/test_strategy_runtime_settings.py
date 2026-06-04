import json

from storage.db import TradingDatabase
from trading.strategy.entry import EntryPlanBuilder
from trading.strategy.models import BlockType, GateDecision, IndicatorSnapshot, StrategyProfile
from trading.strategy.pipeline import GatePipelineResult, _final_score
from trading.strategy.runtime_settings import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_PROFILE_VERSION,
    DEFAULT_STRATEGY_NAME,
    LEGACY_DEFAULT_SETTINGS,
    StrategyRuntimeSettings,
    StrategyRuntimeSettingsRepository,
    legacy_profile_payload,
)


def test_legacy_default_profile_is_seeded_and_loaded_from_strategy_runtime_settings(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))

    settings = StrategyRuntimeSettingsRepository(db).load()

    assert settings.strategy_name == DEFAULT_STRATEGY_NAME
    assert settings.profile_name == DEFAULT_PROFILE_NAME
    assert settings.profile_version == DEFAULT_PROFILE_VERSION
    assert settings.mode == "legacy"
    assert settings.loaded_from == "strategy_runtime_settings"
    assert settings.fallback_used is False
    assert settings.number("gate_weights.market", 0) == 0.15
    assert settings.range_pair("pullback_thresholds.kosdaq_range", (0, 0)) == (-5.0, -2.0)
    db.close()


def test_missing_settings_profile_falls_back_to_legacy_default(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    db.conn.execute("DELETE FROM strategy_runtime_settings")
    db.conn.commit()

    settings = StrategyRuntimeSettingsRepository(db).load()

    assert settings.profile_name == "legacy_default"
    assert settings.loaded_from == "legacy_default"
    assert settings.fallback_used is True
    assert "strategy_runtime_settings" in settings.missing_keys
    assert settings.number("entry_plan_thresholds.max_chase_pct.kosdaq", 0) == 0.7
    db.close()


def test_legacy_profile_keeps_gate_final_score_equal_to_hardcoded_weights(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    settings = StrategyRuntimeSettingsRepository(db).load()
    decisions = [
        GateDecision(gate_name="MarketIndexGate", score=100),
        GateDecision(gate_name="ThemeStrengthGate", score=80),
        GateDecision(gate_name="ThemePullbackGate", score=55),
        GateDecision(gate_name="StockLeadershipGate", score=70),
        GateDecision(gate_name="StockPullbackEntryGate", score=100),
    ]

    assert _final_score(decisions, settings) == 81.25
    assert _final_score(decisions, StrategyRuntimeSettings.legacy_default()) == 81.25
    db.close()


def test_partial_settings_fallback_records_missing_keys_in_entry_plan_details(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    payload = legacy_profile_payload()
    partial_settings = {"gate_weights": dict(LEGACY_DEFAULT_SETTINGS["gate_weights"])}
    payload["settings_json"] = json.dumps(partial_settings)
    payload["config_json"] = payload["settings_json"]
    db.save_strategy_runtime_settings_profile(payload)
    settings = StrategyRuntimeSettingsRepository(db).load()

    plan = EntryPlanBuilder(settings=settings).build(_gate_result())

    assert plan.max_chase_pct == 0.7
    assert plan.cancel_condition["settings_fallback_used"] is True
    assert "market_thresholds" in plan.cancel_condition["settings_missing_keys"]
    assert "SETTINGS_KEY_MISSING" in plan.cancel_condition["comparison_reason_codes"]
    db.close()


def test_invalid_setting_type_falls_back_without_crashing(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    payload = legacy_profile_payload()
    broken = dict(LEGACY_DEFAULT_SETTINGS)
    broken["entry_plan_thresholds"] = {
        **LEGACY_DEFAULT_SETTINGS["entry_plan_thresholds"],
        "max_chase_pct": {"kosdaq": "bad", "kospi": 0.4, "semiconductor_signal": 0.4},
    }
    payload["settings_json"] = json.dumps(broken)
    payload["config_json"] = payload["settings_json"]
    db.save_strategy_runtime_settings_profile(payload)
    settings = StrategyRuntimeSettingsRepository(db).load()

    plan = EntryPlanBuilder(settings=settings).build(_gate_result())

    assert plan.max_chase_pct == 0.7
    assert "entry_plan_thresholds.max_chase_pct.kosdaq" in settings.invalid_keys
    assert plan.cancel_condition["settings_fallback_used"] is True
    db.close()


def test_invalid_gate_weight_sum_warns_and_uses_legacy_weights():
    raw = json.loads(json.dumps(LEGACY_DEFAULT_SETTINGS))
    raw["gate_weights"]["market"] = 0.50

    settings = StrategyRuntimeSettings.from_settings_json(raw)

    assert "GATE_WEIGHTS_SUM_INVALID_FALLBACK_TO_LEGACY" in settings.validation_warnings
    assert settings.number("gate_weights.market", 0) == 0.15


def test_empty_default_lists_allow_variable_length_values(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    payload = legacy_profile_payload()
    raw = json.loads(json.dumps(LEGACY_DEFAULT_SETTINGS))
    raw["order_execution"]["mode"] = "LIVE_SIM"
    raw["order_execution"]["live_sim_enabled"] = True
    raw["order_execution"]["allowed_account_numbers"] = ["1234567890"]
    raw["market_session"]["holidays"] = ["2026-01-01"]
    payload["settings_json"] = json.dumps(raw)
    payload["config_json"] = payload["settings_json"]
    db.save_strategy_runtime_settings_profile(payload)

    settings = StrategyRuntimeSettingsRepository(db).load()

    assert settings.value("order_execution.allowed_account_numbers") == ["1234567890"]
    assert settings.value("market_session.holidays") == ["2026-01-01"]
    assert "order_execution.allowed_account_numbers" not in settings.invalid_keys
    assert "market_session.holidays" not in settings.invalid_keys
    db.close()


def test_runtime_settings_module_does_not_reference_real_order_path():
    source = open("trading/strategy/runtime_settings.py", encoding="utf-8").read()

    assert "OrderRequest" not in source
    assert "send_order" not in source
    assert "KiwoomClient" not in source


def _gate_result():
    return GatePipelineResult(
        candidate_id=1,
        code="111111",
        theme_id="robot",
        final_grade="A",
        final_score=88.0,
        strategy_eligible=True,
        block_type=BlockType.NONE,
        decisions=[
            GateDecision(
                gate_name="StockPullbackEntryGate",
                passed=True,
                score=100,
                details={
                    "profile": StrategyProfile.KOSDAQ_THEME_PROFILE.value,
                    "nearest_support": "vwap",
                    "nearest_support_price": 9700,
                    "support_candidates": {"vwap": 9700},
                },
            )
        ],
        snapshot=IndicatorSnapshot(candidate_id=1, code="111111", price=9800),
        details={"theme_name": "Robot"},
    )
