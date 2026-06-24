from datetime import datetime

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.market_index import MarketIndexStore
from trading.strategy.realtime import RealTimeSubscriptionManager
from trading.strategy.reboot_v2 import RebootV2RuntimeProfile
from trading.strategy.reboot_v2_runtime import RebootV2Runtime
from trading.strategy.runtime import StrategyRuntimeConfig
from trading.strategy.strategy_baseline import (
    GitInfo,
    StrategyBaselineRuntimeConfig,
    StrategyBaselineService,
    apply_baseline_metadata_to_observations,
    build_strategy_baseline_snapshot,
    canonical_config_json,
    config_hash,
    sanitize_config,
    strategy_baseline_role,
)
from trading.strategy.setup_router_v3 import SetupRouterConfig
from trading_app.strategy_change_proposals import StrategyChangeProposalConfig
from trading_app.runtime_adapters import GatewayCommandRealtimeClient


NOW = datetime(2026, 6, 22, 9, 5, 0)


def test_strategy_baseline_config_hash_is_deterministic_for_key_order():
    left = {"b": {"y": 2, "x": 1}, "a": [3, 2, 1]}
    right = {"a": [3, 2, 1], "b": {"x": 1, "y": 2}}

    assert canonical_config_json(left) == canonical_config_json(right)
    assert config_hash(left) == config_hash(right)


def test_strategy_baseline_excludes_sensitive_values_from_snapshot_and_hash_input():
    raw = {
        "runtime": {"account": "12345678", "token": "secret-token", "db_path": "C:/secret/db.sqlite3"},
        "safe": {"threshold": 0.7, "enabled": True},
    }
    sanitized = sanitize_config(raw)
    serialized = canonical_config_json(raw)

    assert sanitized == {"runtime": {}, "safe": {"threshold": 0.7, "enabled": True}}
    assert "12345678" not in serialized
    assert "secret-token" not in serialized
    assert "db.sqlite3" not in serialized


def test_strategy_baseline_champion_and_challenger_mapping():
    assert strategy_baseline_role("LEADER_FIRST_PULLBACK") == "CHAMPION"
    assert strategy_baseline_role("VWAP_RECLAIM") == "CHALLENGER"
    assert strategy_baseline_role("BREAKOUT_RETEST") == "CHALLENGER"
    assert strategy_baseline_role("UNKNOWN") == "OUT_OF_SCOPE"


def test_strategy_baseline_challenger_metadata_is_observe_only_additive():
    observations = [
        {
            "setup_type": "VWAP_RECLAIM",
            "ready_allowed": False,
            "candidate_promotion_allowed": False,
            "opportunity_rank_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "setup_quality_score": 10.0,
        }
    ]

    enriched = apply_baseline_metadata_to_observations(
        observations,
        {"enabled": True, "baseline_id": "leader_first_pullback_v1", "version": "1.0.0", "config_hash": "abc"},
    )

    assert enriched[0]["baseline_role"] == "CHALLENGER"
    assert enriched[0]["baseline_frozen"] is True
    assert enriched[0]["ready_allowed"] is False
    assert enriched[0]["candidate_promotion_allowed"] is False
    assert enriched[0]["opportunity_rank_allowed"] is False
    assert enriched[0]["order_intent_allowed"] is False
    assert enriched[0]["live_order_allowed"] is False
    assert observations[0].get("baseline_role") is None


def test_strategy_baseline_clean_and_drift_detection_records_changed_path(tmp_path):
    db = TradingDatabase(str(tmp_path / "baseline.db"))
    first = _baseline_service(db, _snapshot(leader_pullback_min_pct=0.7))
    clean = first.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=1)

    second = _baseline_service(db, _snapshot(leader_pullback_min_pct=0.9))
    drift = second.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=2)

    assert clean["drift_status"] == "CLEAN"
    assert drift["drift_status"] == "DRIFT_DETECTED"
    assert any(item["path"] == "setup_router_v3.leader_pullback_min_pct" for item in drift["drift_paths"])


def test_strategy_baseline_partial_snapshot_does_not_fake_defaults(tmp_path):
    db = TradingDatabase(str(tmp_path / "partial.db"))
    service = _baseline_service(db, {"runtime_profile": "THEME_CORE_V3"}, missing=["setup_router_v3"])

    result = service.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=1)

    assert result["drift_status"] == "PARTIAL"
    assert result["config_snapshot_completeness"] == "PARTIAL"
    assert result["missing_config_paths"] == ["setup_router_v3"]


def test_strategy_baseline_persistence_is_idempotent_for_definition_and_session(tmp_path):
    db = TradingDatabase(str(tmp_path / "idempotent.db"))
    service = _baseline_service(db, _snapshot())

    first = service.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=1)
    second = service.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=2)

    assert first["session_id"] == second["session_id"]
    assert db.conn.execute("SELECT COUNT(*) FROM strategy_baseline_definitions").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM strategy_baseline_sessions").fetchone()[0] == 1
    session = db.load_strategy_baseline_session(first["session_id"])
    assert session["runtime_cycle_count"] == 2


def test_strategy_baseline_runtime_snapshot_contains_required_section(tmp_path):
    db = TradingDatabase(str(tmp_path / "runtime-snapshot.db"))
    runtime = _runtime(db, strategy_baseline_service=_baseline_service(db, _snapshot()))

    snapshot = runtime.start(NOW)

    section = snapshot["strategy_baseline"]
    assert section["status"] == "FROZEN"
    assert section["baseline_id"] == "leader_first_pullback_v1"
    assert section["version"] == "1.0.0"
    assert section["champion_setup"] == "LEADER_FIRST_PULLBACK"
    assert section["challenger_count"] == 2
    assert section["config_hash_short"]
    assert section["git_sha_short"] == "abcdef12"
    assert section["order_intent_allowed"] is False
    assert section["live_order_allowed"] is False


def test_strategy_baseline_on_off_behavioral_parity_for_empty_runtime_cycle(tmp_path):
    off_db = TradingDatabase(str(tmp_path / "off.db"))
    on_db = TradingDatabase(str(tmp_path / "on.db"))
    off_runtime = _runtime(off_db)
    on_runtime = _runtime(on_db, strategy_baseline_service=_baseline_service(on_db, _snapshot()))

    off_runtime.start(NOW)
    on_runtime.start(NOW)
    off_snapshot = _strip_baseline_only_fields(off_runtime.cycle(NOW))
    on_snapshot = _strip_baseline_only_fields(on_runtime.cycle(NOW))

    assert off_snapshot == on_snapshot


def test_strategy_baseline_order_safety_counts_remain_zero(tmp_path):
    db = TradingDatabase(str(tmp_path / "order-safety.db"))
    gateway = GatewayStateStore()
    runtime = _runtime(db, gateway=gateway, strategy_baseline_service=_baseline_service(db, _snapshot()))

    runtime.start(NOW)
    runtime.cycle(NOW)

    assert _count(db, "runtime_order_intents") == 0
    assert _count(db, "managed_order_intents") == 0
    assert _count(db, "managed_orders") == 0
    assert _gateway_count(db, "send_order") == 0
    assert _gateway_count(db, "cancel_order") == 0
    assert _gateway_count(db, "modify_order") == 0
    assert [row for row in gateway.list_commands(include_finished=True, limit=50) if row["command_type"] in {"send_order", "cancel_order", "modify_order"}] == []


def test_strategy_baseline_drift_does_not_auto_apply_strategy_settings(tmp_path):
    db = TradingDatabase(str(tmp_path / "auto-apply.db"))
    assert StrategyChangeProposalConfig().allow_auto_apply is False
    original_config = SetupRouterConfig(enabled=True, leader_pullback_min_pct=0.7)
    first = _baseline_service(db, _snapshot(leader_pullback_min_pct=original_config.leader_pullback_min_pct))
    first.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=1)

    second = _baseline_service(db, _snapshot(leader_pullback_min_pct=1.2))
    result = second.check(now=NOW, runtime_started_at=NOW.isoformat(), runtime_cycle_count=2)

    assert result["drift_status"] == "DRIFT_DETECTED"
    assert original_config.leader_pullback_min_pct == 0.7


def test_strategy_baseline_builder_marks_missing_registry_paths_partial():
    snapshot, missing = build_strategy_baseline_snapshot(
        runtime_profile="THEME_CORE_V3",
        setup_router_config=SetupRouterConfig(enabled=True),
    )

    assert snapshot["setup_router_v3"]["leader_pullback_min_pct"] == 0.7
    assert "entry_engine" in missing
    assert "market_regime" in missing


def _baseline_service(db, snapshot, missing=None):
    return StrategyBaselineService(
        db=db,
        runtime_profile="THEME_CORE_V3",
        config=StrategyBaselineRuntimeConfig(enabled=True),
        config_snapshot_provider=lambda: (snapshot, list(missing or [])),
        git_info_provider=lambda: GitInfo(git_sha="abcdef1234567890", git_dirty_or_unknown=False),
    )


def _snapshot(*, leader_pullback_min_pct=0.7):
    return {
        "runtime_profile": "THEME_CORE_V3",
        "setup_router_v3": {
            "enabled": True,
            "observe_only": True,
            "leader_pullback_min_pct": leader_pullback_min_pct,
            "vwap_reclaim_above_min_pct": 0.05,
            "breakout_buffer_pct": 0.25,
        },
        "entry_engine": {"enabled": True, "observe_only": True, "use_strategy_context_v3": True},
        "market_regime": {"enabled": True, "weak_kospi_pct": -0.8, "risk_off_kospi_pct": -2.0},
        "theme_core_v3": {"enabled": True, "observe_only": True, "top_theme_count": 5},
        "market_data": {"max_tick_age_sec": 10, "dirty_queue_enabled": True},
        "position_risk": {"enabled": True, "portfolio_gross_exposure_limit_krw": 10_000_000},
        "exit_engine": {"enabled": True, "stop_loss_pct": -2.0, "take_profit_1_pct": 5.0},
        "order_manager": {
            "enabled": False,
            "intent_enabled": False,
            "enqueue_gateway_command": False,
            "send_order_allowed": False,
        },
        "runtime_settings": {
            "settings": {
                "data_readiness": {"max_latest_tick_age_sec": 30},
                "entry_plan_thresholds": {"tick_offset": 1},
                "exit_policy_thresholds": {"support_loss_consecutive_closes_below": 2},
            }
        },
        "core_settings": {
            "runtime_mode": "OBSERVE",
            "runtime_allow_dry_run_orders": False,
            "runtime_allow_live_orders": False,
            "change_proposal_allow_auto_apply": False,
        },
        "feature_flags": {
            "TRADING_ENTRY_ALLOW_LEGACY_THEME_CONTEXT_FALLBACK": "0",
            "TRADING_ORDER_INTENT_ENABLED": "false",
        },
    }


def _runtime(db, gateway=None, strategy_baseline_service=None):
    gateway = gateway or GatewayStateStore()
    return RebootV2Runtime(
        db=db,
        subscription_manager=RealTimeSubscriptionManager(GatewayCommandRealtimeClient(gateway), max_codes=20),
        candle_builder=CandleBuilder(),
        market_data=MarketDataStore(),
        market_index_store=MarketIndexStore(),
        config=StrategyRuntimeConfig(max_candidates_to_watch=20, realtime_subscription_limit=20),
        profile=RebootV2RuntimeProfile.THEME_CORE_V3,
        strategy_baseline_service=strategy_baseline_service,
    )


def _strip_baseline_only_fields(snapshot):
    cleaned = dict(snapshot)
    cleaned.pop("strategy_baseline", None)
    cleaned.pop("warnings", None)
    cleaned.pop("cycle_duration_ms", None)
    cleaned.pop("status", None)
    return cleaned


def _count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _gateway_count(db, command_type):
    return db.conn.execute(
        "SELECT COUNT(*) FROM gateway_commands WHERE command_type = ?",
        (command_type,),
    ).fetchone()[0]
