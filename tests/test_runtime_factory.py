from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.runtime import StrategyRuntime
from trading.theme_engine.core_v3_runtime import ThemeCoreV3RuntimePipeline
from trading_app.dependencies import CoreSettings
from trading_app.runtime_factory import (
    _cached_report_provider,
    _provider_cache_ttl_sec,
    build_core_strategy_runtime,
)


def test_cached_report_provider_reuses_trade_date_payload():
    calls: list[str] = []

    def loader(trade_date: str) -> dict:
        calls.append(trade_date)
        return {"trade_date": trade_date, "calls": len(calls)}

    provider = _cached_report_provider(loader, ttl_sec=60)

    first = provider("2026-06-15")
    second = provider("2026-06-15")
    third = provider("2026-06-16")

    assert first == second
    assert third["calls"] == 2
    assert calls == ["2026-06-15", "2026-06-16"]


def test_cached_report_provider_ttl_zero_disables_cache():
    calls = 0

    def loader(trade_date: str) -> dict:
        nonlocal calls
        calls += 1
        return {"trade_date": trade_date, "calls": calls}

    provider = _cached_report_provider(loader, ttl_sec=0)

    assert provider("2026-06-15")["calls"] == 1
    assert provider("2026-06-15")["calls"] == 2


def test_provider_cache_ttl_accepts_policy_aliases():
    assert _provider_cache_ttl_sec({}, default=300) == 300
    assert _provider_cache_ttl_sec({"cache_ttl_sec": "15"}, default=60) == 15
    assert _provider_cache_ttl_sec({"report_cache_ttl_sec": "30"}, default=60) == 30
    assert _provider_cache_ttl_sec({"evidence_cache_ttl_sec": "45"}, default=60) == 45
    assert _provider_cache_ttl_sec({"cache_ttl_sec": "bad"}, default=60) == 60


def test_build_core_runtime_defaults_to_reboot_v2_observe(tmp_path, monkeypatch):
    monkeypatch.delenv("STRATEGY_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_PROFILE", raising=False)
    monkeypatch.delenv("STRATEGY_REBOOT_V2_ENABLED", raising=False)
    db = TradingDatabase(str(tmp_path / "v2-default.db"))

    bundle = build_core_strategy_runtime(db, GatewayStateStore(), settings=_settings(tmp_path))

    assert bundle.runtime_profile == "V2_OBSERVE"
    assert isinstance(bundle.runtime, RebootV2Runtime)
    assert isinstance(bundle.theme_board_pipeline, ThemeCoreV3RuntimePipeline)


def test_build_core_runtime_routes_to_legacy_only_when_profile_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "LEGACY")
    db = TradingDatabase(str(tmp_path / "legacy.db"))

    bundle = build_core_strategy_runtime(db, GatewayStateStore(), settings=_settings(tmp_path))

    assert bundle.runtime_profile == "LEGACY"
    assert isinstance(bundle.runtime, StrategyRuntime)
    assert bundle.candidate_ingestion_service is None
    assert bundle.candidate_hydrator is None
    assert bundle.theme_board_pipeline is None
    assert bundle.entry_engine_pipeline is None


def test_build_core_runtime_routes_to_reboot_v2_observe_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "V2_OBSERVE")
    monkeypatch.setenv("TRADING_ORDER_MANAGER_ENABLED", "0")
    db = TradingDatabase(str(tmp_path / "v2.db"))

    bundle = build_core_strategy_runtime(db, GatewayStateStore(), settings=_settings(tmp_path))

    assert bundle.runtime_profile == "V2_OBSERVE"
    assert isinstance(bundle.runtime, RebootV2Runtime)
    assert bundle.candidate_ingestion_service is not None
    assert bundle.candidate_hydrator is not None
    assert bundle.theme_board_pipeline is not None
    assert isinstance(bundle.theme_board_pipeline, ThemeCoreV3RuntimePipeline)
    assert bundle.entry_engine_pipeline is not None
    assert bundle.dirty_strategy_evaluator is not None
    assert bundle.theme_board_pipeline.config.enabled is True
    assert bundle.theme_board_pipeline.config.interval_sec == 1
    assert bundle.market_regime_pipeline.config.enabled is True
    assert bundle.market_regime_pipeline.config.interval_sec == 1
    assert bundle.entry_engine_pipeline.config.enabled is True
    assert bundle.dirty_strategy_evaluator.config.enabled is True
    assert bundle.dirty_strategy_evaluator.config.shadow_mode is True
    assert bundle.dirty_strategy_evaluator.config.order_intent_enabled is False
    assert bundle.exit_engine_reboot_pipeline.config.enabled is True
    assert bundle.position_risk_pipeline.config.enabled is True
    assert bundle.order_sink is None
    assert bundle.order_manager_pipeline is None
    assert not hasattr(bundle.runtime, "gate_pipeline")
    assert not hasattr(bundle.runtime, "entry_plan_builder")


def test_build_core_runtime_accepts_theme_core_v3_profile_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "THEME_CORE_V3")
    db = TradingDatabase(str(tmp_path / "theme-core-v3.db"))

    bundle = build_core_strategy_runtime(db, GatewayStateStore(), settings=_settings(tmp_path))

    assert bundle.runtime_profile == "THEME_CORE_V3"
    assert isinstance(bundle.runtime, RebootV2Runtime)
    assert isinstance(bundle.theme_board_pipeline, ThemeCoreV3RuntimePipeline)
    assert bundle.theme_board_pipeline.config.enabled is True
    assert bundle.theme_board_pipeline.config.ingest_candidate_source_events is False


def test_build_core_runtime_respects_explicit_reboot_v2_component_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNTIME_PROFILE", "V2_OBSERVE")
    monkeypatch.setenv("TRADING_THEME_BOARD_ENABLED", "0")
    db = TradingDatabase(str(tmp_path / "v2-disabled-component.db"))

    bundle = build_core_strategy_runtime(db, GatewayStateStore(), settings=_settings(tmp_path))

    assert bundle.runtime_profile == "V2_OBSERVE"
    assert bundle.theme_board_pipeline.config.enabled is False
    assert bundle.market_regime_pipeline.config.enabled is True


def _settings(tmp_path):
    return CoreSettings(
        db_path=tmp_path / "factory.db",
        local_token="test-token",
        mode="OBSERVE",
        runtime_mode="OBSERVE",
        runtime_enabled=True,
        runtime_auto_start=False,
        runtime_allow_dry_run_orders=False,
        runtime_allow_live_orders=False,
    )
