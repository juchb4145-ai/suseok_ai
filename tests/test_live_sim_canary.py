from __future__ import annotations

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent, utc_timestamp
from trading.strategy.hybrid_validation import HybridValidationEvent, HybridValidationRepository
from trading.strategy.runtime_settings import LEGACY_DEFAULT_SETTINGS, StrategyRuntimeSettings, legacy_profile_payload
from trading_app.dependencies import CoreSettings
from trading_app.live_sim_canary import evaluate_live_sim_canary
from trading_app.order_enqueue_service import OrderEnqueueService, RuntimeOrderIntentRequest
from trading_app.runtime_order_sink import LiveSimRuntimeOrderSink


def test_canary_default_config_is_disabled_and_not_eligible():
    settings = StrategyRuntimeSettings.legacy_default()

    decision = evaluate_live_sim_canary(
        runtime_settings=settings,
        preflight_snapshot=_preflight("GO"),
        metadata=_ready_metadata(),
        counters=_empty_counters(),
        limit_price=10000,
        current_price=10000,
    )

    assert decision.status == "CONFIG_DISABLED"
    assert decision.eligible is False
    assert "CANARY_CONFIG_DISABLED" in decision.reason_codes


def test_canary_observe_only_when_order_disabled():
    decision = evaluate_live_sim_canary(
        canary_config={**_canary_config(), "order_enabled": False},
        preflight_snapshot=_preflight("GO"),
        metadata=_ready_metadata(),
        counters=_empty_counters(),
        limit_price=10000,
        current_price=10000,
    )

    assert decision.status == "OBSERVE_ONLY"
    assert decision.eligible is False
    assert decision.quantity == 1
    assert "CANARY_ORDER_DISABLED_OBSERVE_ONLY" in decision.warning_reasons


def test_canary_requires_preflight_go_and_blocks_warnings_by_default():
    for status in ["GO_WITH_WARNINGS", "INSUFFICIENT_DATA", "NO_GO", "FAIL_CLOSED"]:
        decision = evaluate_live_sim_canary(
            canary_config=_canary_config(),
            preflight_snapshot=_preflight(status),
            metadata=_ready_metadata(),
            counters=_empty_counters(),
            limit_price=10000,
            current_price=10000,
        )

        assert decision.status == "BLOCKED"
        assert decision.eligible is False

    assert "PREFLIGHT_GO_WITH_WARNINGS_BLOCKED" in evaluate_live_sim_canary(
        canary_config=_canary_config(),
        preflight_snapshot=_preflight("GO_WITH_WARNINGS"),
        metadata=_ready_metadata(),
        counters=_empty_counters(),
        limit_price=10000,
        current_price=10000,
    ).blocking_reasons


def test_canary_hybrid_and_risk_blocks():
    cases = [
        ({"hybrid_status": "WAIT"}, "HYBRID_STATUS_NOT_READY"),
        ({"hybrid_position_tier": "small_first_entry"}, "HYBRID_POSITION_TIER_NOT_ALLOWED"),
        ({"dynamic_theme_status": "WATCH"}, "THEME_STATUS_NOT_ALLOWED"),
        ({"stock_role": "FOLLOWER"}, "STOCK_ROLE_NOT_ALLOWED"),
        ({"price_location_readiness": "WAIT"}, "PRICE_LOCATION_NOT_READY"),
        ({"latest_tick_ready": False}, "LATEST_TICK_NOT_READY"),
        ({"support_ready": False}, "SUPPORT_NOT_READY"),
        ({"vwap_or_recent_support_ready": False}, "VWAP_OR_RECENT_SUPPORT_NOT_READY"),
        ({"gate_usable": False}, "GATE_USABLE_FALSE"),
        ({"reason_codes": ["TR_BACKFILL_ONLY"]}, "TR_BACKFILL_ONLY_BLOCKED"),
        ({"reason_codes": ["MARKET_RISK_OFF"]}, "MARKET_RISK_OFF_BLOCKED"),
        ({"reason_codes": ["CHASE_RISK"]}, "CHASE_RISK_BLOCKED"),
        ({"reason_codes": ["LATE_LAGGARD"]}, "LATE_LAGGARD_BLOCKED"),
        ({"reason_codes": ["LOW_BREADTH"]}, "LOW_BREADTH_BLOCKED"),
        ({"reason_codes": ["LEADER_ONLY_THEME_LAGGARD_BLOCK"]}, "LEADER_ONLY_LAGGARD_BLOCKED"),
        ({"reason_codes": ["ENTRY_RISK_TEMP_WAIT"]}, "ENTRY_RISK_TEMP_WAIT_BLOCKED"),
        ({"reason_codes": ["VI_ACTIVE"]}, "ENTRY_RISK_FINAL_BLOCKED"),
    ]

    for patch, reason in cases:
        metadata = {**_ready_metadata(), **patch}
        if "reason_codes" in patch:
            metadata["reason_codes"] = patch["reason_codes"]
        decision = evaluate_live_sim_canary(
            canary_config=_canary_config(),
            preflight_snapshot=_preflight("GO"),
            metadata=metadata,
            counters=_empty_counters(),
            limit_price=10000,
            current_price=10000,
        )

        assert reason in decision.blocking_reasons
        assert decision.eligible is False


def test_canary_order_limits_quantity_and_slippage_blocks():
    base = {
        "canary_config": _canary_config(),
        "preflight_snapshot": _preflight("GO"),
        "metadata": _ready_metadata(),
    }

    assert "MAX_ORDERS_PER_DAY_EXCEEDED" in evaluate_live_sim_canary(
        **base,
        counters={**_empty_counters(), "orders_per_day": 1},
        limit_price=10000,
        current_price=10000,
    ).blocking_reasons
    assert "SAME_CODE_OPEN_ORDER_EXISTS" in evaluate_live_sim_canary(
        **base,
        counters={**_empty_counters(), "has_open_order_for_code": True},
        limit_price=10000,
        current_price=10000,
    ).blocking_reasons
    assert "SAME_CODE_POSITION_EXISTS" in evaluate_live_sim_canary(
        **base,
        counters={**_empty_counters(), "has_position_for_code": True},
        limit_price=10000,
        current_price=10000,
    ).blocking_reasons
    assert "BLOCKED_QUANTITY_BELOW_MIN" in evaluate_live_sim_canary(
        canary_config={**_canary_config(), "max_position_amount_krw": 1000},
        preflight_snapshot=_preflight("GO"),
        metadata=_ready_metadata(),
        counters=_empty_counters(),
        limit_price=10000,
        current_price=10000,
    ).blocking_reasons
    assert "LIMIT_PRICE_SLIPPAGE_EXCEEDED" in evaluate_live_sim_canary(
        **base,
        counters=_empty_counters(),
        limit_price=10200,
        current_price=10000,
    ).blocking_reasons


def test_canary_decision_db_summary_and_filters(tmp_path):
    db = TradingDatabase(str(tmp_path / "trader.sqlite3"))
    try:
        blocked = evaluate_live_sim_canary(
            canary_config=_canary_config(),
            preflight_snapshot=_preflight("NO_GO"),
            metadata=_ready_metadata(),
            counters=_empty_counters(),
            limit_price=10000,
            current_price=10000,
        )
        saved_blocked = db.save_live_sim_canary_decision(blocked.to_dict())
        eligible = evaluate_live_sim_canary(
            canary_config=_canary_config(),
            preflight_snapshot=_preflight("GO"),
            metadata=_ready_metadata(code="000660"),
            counters=_empty_counters(),
            limit_price=10000,
            current_price=10000,
        )
        db.save_live_sim_canary_decision({**eligible.to_dict(), "order_intent_id": "live_sim_intent_test"})

        rows = db.list_live_sim_canary_decisions(trade_date="2026-06-16", reason_code="PREFLIGHT")
        summary = db.live_sim_canary_summary(trade_date="2026-06-16")

        assert saved_blocked["status"] == "BLOCKED"
        assert rows
        assert summary["blocked_count"] == 1
        assert summary["eligible_count"] == 1
        assert summary["submitted_count"] == 1
        assert summary["blocked_reason_top"][0]["reason"] == "PREFLIGHT_NOT_GO"
    finally:
        db.close()


def test_canary_sink_observe_only_records_decision_without_gateway_command(tmp_path):
    sink, gateway_state = _sink(tmp_path, canary_config={**_canary_config(), "order_enabled": False})
    payload = sink._submit_live_sim_from_dry_payload(_dry_payload(), phase="entry")

    db = TradingDatabase(str(Path(tmp_path) / "trader.sqlite3"))
    try:
        decisions = db.list_live_sim_canary_decisions(trade_date="2026-06-16")
        orders = db.list_live_sim_orders(limit=10)
    finally:
        db.close()

    assert payload["status"] == "OBSERVE_ONLY"
    assert decisions[0]["status"] == "OBSERVE_ONLY"
    assert orders == []
    assert gateway_state.command_snapshot()["queued_count"] == 0


def test_canary_sink_order_enabled_queues_live_sim_safe_path(tmp_path):
    sink, gateway_state = _sink(tmp_path, canary_config=_canary_config())
    payload = sink._submit_live_sim_from_dry_payload(_dry_payload(), phase="entry")

    db = TradingDatabase(str(Path(tmp_path) / "trader.sqlite3"))
    try:
        decisions = db.list_live_sim_canary_decisions(trade_date="2026-06-16")
        orders = db.list_live_sim_orders(limit=10)
    finally:
        db.close()

    assert payload["accepted"] is True
    assert payload["command"]["type"] == "send_order"
    assert payload["request"]["hoga"] == "00"
    assert payload["request"]["price"] == 10000
    assert payload["request"]["quantity"] == 1
    assert payload["request"]["metadata"]["source"] == "live_sim_hybrid_ready_canary"
    assert decisions[0]["order_intent_id"] == payload["intent_id"]
    assert orders[0]["order_status"] == "SUBMITTED"
    assert gateway_state.command_snapshot()["queued_count"] == 1


def test_canary_api_summary_decisions_detail_and_rebuild_no_order(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "trader.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_RUNTIME_ENABLED", "0")
    _save_settings(db_path, canary_config=_canary_config())
    db = TradingDatabase(str(db_path))
    try:
        decision = evaluate_live_sim_canary(
            canary_config=_canary_config(),
            preflight_snapshot=_preflight("GO"),
            metadata=_ready_metadata(),
            counters=_empty_counters(),
            limit_price=10000,
            current_price=10000,
        )
        saved = db.save_live_sim_canary_decision(decision.to_dict())
        HybridValidationRepository(db).save_event(
            HybridValidationEvent(
                ts="2026-06-16T09:01:00",
                trade_date="2026-06-16",
                stock_code="005930",
                hybrid_status="READY",
                hybrid_score=88,
                hybrid_position_tier="normal_first_entry",
                hybrid_reason_codes=["STRONG_ACTIVE_THEME"],
                theme_name="AI",
                theme_status="ACTIVE",
                theme_score=90,
                leader_type="LEADER",
                details_json={"base_price": 10000},
            )
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        summary = client.get("/api/runtime/live-sim/canary/summary?trade_date=2026-06-16")
        listing = client.get("/api/runtime/live-sim/canary/decisions?trade_date=2026-06-16&limit=5")
        detail = client.get(f"/api/runtime/live-sim/canary/decisions/{saved['decision_id']}")
        rebuild = client.post(
            "/api/runtime/live-sim/canary/rebuild",
            headers={"X-Local-Token": "test-token"},
            json={"trade_date": "2026-06-16", "limit": 10},
        )

    assert summary.status_code == 200
    assert summary.json()["eligible_count"] == 1
    assert listing.status_code == 200
    assert listing.json()["pagination"]["limit"] == 5
    assert detail.status_code == 200
    assert detail.json()["decision_id"] == saved["decision_id"]
    assert rebuild.status_code == 200
    assert rebuild.json()["order_created"] is False
    assert rebuild.json()["gateway_command_created"] is False

    db = TradingDatabase(str(db_path))
    try:
        assert db.list_live_sim_orders(limit=10) == []
    finally:
        db.close()


def _canary_config() -> dict:
    return {
        "enabled": True,
        "order_enabled": True,
        "require_preflight_go": True,
        "allow_go_with_warnings": False,
        "require_dry_run_go_no_go": True,
        "min_trade_days": 5,
        "min_accepted_entry_lifecycles": 30,
        "min_net_expectancy_pct": 0.0,
        "max_bad_ready_rate": 0.25,
        "max_stale_tick_rate": 0.10,
        "max_latency_risk_rate": 0.10,
        "max_orders_per_day": 1,
        "max_orders_per_cycle": 1,
        "max_new_positions_per_day": 1,
        "max_position_amount_krw": 100000,
        "position_size_multiplier": 0.10,
        "allowed_hybrid_statuses": ["READY"],
        "allowed_position_tiers": ["normal_first_entry"],
        "allowed_theme_statuses": ["ACTIVE", "LEADING_THEME", "SPREADING_THEME"],
        "allowed_stock_roles": ["LEADER", "CO_LEADER"],
        "allowed_price_location_readiness": ["READY"],
        "allowed_risk_levels": ["PASS"],
        "require_latest_tick_ready": True,
        "require_support_ready": True,
        "require_vwap_or_recent_support_ready": True,
        "require_gate_usable_true": True,
        "block_if_backfill_source_only": True,
        "block_if_market_risk_off": True,
        "block_if_chase_risk": True,
        "block_if_late_laggard": True,
        "block_if_low_breadth": True,
        "block_if_leader_only_laggard": True,
        "block_if_entry_risk_temp_wait": True,
        "block_if_entry_risk_final_block": True,
        "block_if_load_guard_not_ok": True,
        "order_ttl_sec": 30,
        "limit_price_policy": "safe_limit",
        "max_entry_slippage_bp": 10,
        "submit_first_leg_only": True,
        "reason_code": "LIVE_SIM_HYBRID_READY_CANARY",
    }


def _ready_metadata(*, code: str = "005930") -> dict:
    return {
        "trade_date": "2026-06-16",
        "code": code,
        "candidate_id": 1,
        "candidate_instance_id": "ci-1",
        "candidate_generation_seq": 7,
        "hybrid_status": "READY",
        "hybrid_position_tier": "normal_first_entry",
        "hybrid_score": 88.0,
        "dynamic_theme_status": "ACTIVE",
        "theme_name": "AI",
        "theme_score": 91.0,
        "stock_role": "LEADER",
        "price_location_status": "PULLBACK_RECLAIM",
        "price_location_readiness": "READY",
        "risk_level": "PASS",
        "latest_tick_ready": True,
        "support_ready": True,
        "vwap_or_recent_support_ready": True,
        "gate_usable": True,
        "current_price": 10000,
        "reason_codes": ["STRONG_ACTIVE_THEME"],
    }


def _preflight(status: str) -> dict:
    return {
        "snapshot_id": "pf-1",
        "status": status,
        "performance_summary": {
            "trade_day_count": 5,
            "accepted_completed_lifecycle_count": 35,
            "net_expectancy": 0.02,
            "bad_ready_rate": 0.05,
            "stale_tick_rate": 0.01,
            "latency_distortion_rate": 0.01,
            "go_no_go": {"decision": "GO", "readiness": "READY", "blocked_by": []},
        },
        "backfill_summary": {"load_guard": {"load_guard_status": "OK"}},
    }


def _empty_counters() -> dict:
    return {
        "orders_per_day": 0,
        "orders_per_cycle": 0,
        "new_positions_per_day": 0,
        "has_open_order_for_code": False,
        "has_position_for_code": False,
    }


def _save_settings(db_path: Path, *, canary_config: dict) -> None:
    raw = json.loads(json.dumps(LEGACY_DEFAULT_SETTINGS))
    raw["order_execution"] = {
        **raw["order_execution"],
        "mode": "LIVE_SIM",
        "live_sim_enabled": True,
        "live_real_enabled": False,
        "allowed_account_numbers": [],
        "fail_closed_on_account_unknown": False,
        "min_order_amount_krw": 0,
        "max_orders_per_day": 5,
        "max_new_positions_per_day": 3,
        "max_order_amount_krw": 300000,
        "max_position_amount_krw": 300000,
        "max_total_exposure_krw": 1000000,
        "cash_based_limits_enabled": False,
        "cash_based_auto_size_enabled": False,
        "allow_market_order": False,
        "kill_switch_active": False,
    }
    raw["live_sim_hybrid_ready_canary"] = {**raw["live_sim_hybrid_ready_canary"], **canary_config}
    db = TradingDatabase(str(db_path))
    try:
        payload = legacy_profile_payload()
        payload["settings_json"] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        payload["config_json"] = payload["settings_json"]
        db.save_strategy_runtime_settings_profile(payload)
    finally:
        db.close()


def _sink(tmp_path, *, canary_config: dict):
    db_path = Path(tmp_path) / "trader.sqlite3"
    _save_settings(db_path, canary_config=canary_config)
    settings = CoreSettings(
        db_path=db_path,
        local_token="test-token",
        mode="OBSERVE",
        allow_live=False,
        runtime_mode="DRY_RUN",
        runtime_allow_dry_run_orders=True,
        runtime_dry_run_account="1234567890",
        runtime_dry_run_position_amount=100000,
    )
    gateway_state = GatewayStateStore()
    gateway_state.record_event(
        GatewayEvent(
            type="heartbeat",
            timestamp=utc_timestamp(),
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": "1234567890",
                "broker_env": "SIMULATION",
                "server_mode": "SIMULATION",
                "account_mode": "SIMULATION",
            },
        )
    )
    db = TradingDatabase(str(db_path))
    try:
        row = db.load_strategy_runtime_settings_profile("OBSERVE_DREAMROAD", "legacy_default", "v1")
        runtime_settings = StrategyRuntimeSettings.from_settings_json(json.loads(row["settings_json"]))
    finally:
        db.close()
    service = OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=db_path)
    return LiveSimRuntimeOrderSink(settings=settings, service=service, runtime_settings=runtime_settings), gateway_state


def _dry_payload() -> dict:
    request = RuntimeOrderIntentRequest(
        source="strategy_runtime",
        dry_run=True,
        account="1234567890",
        code="005930",
        side="buy",
        quantity=10,
        price=10000,
        order_type=1,
        hoga="00",
        tag="runtime:canary",
        strategy_name="KOSDAQ_THEME_PROFILE",
        candidate_id=1,
        entry_plan_id=2,
        virtual_order_id=3,
        leg_index=1,
        entry_type="pullback",
        order_phase="entry",
        gate_status="READY",
        gate_reason="READY_PULLBACK",
        hybrid_score=88.0,
        theme_name="AI",
        theme_score=91.0,
        runtime_cycle_at="2026-06-16T09:01:00+09:00",
        metadata={**_ready_metadata(), "live_sim_preflight": _preflight("GO")},
    )
    return {
        "accepted": True,
        "mode": "DRY_RUN",
        "dry_run": True,
        "intent_id": "dry-intent-1",
        "status": "DRY_RUN_ACCEPTED",
        "reason": "DRY_RUN_ORDER_INTENT_RECORDED",
        "request": request.to_dict(),
        "response": {"created_at": "2026-06-16T09:01:00+09:00"},
        "record": {"trade_date": "2026-06-16"},
    }
