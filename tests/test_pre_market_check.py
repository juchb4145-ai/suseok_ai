from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from trading_app.dashboard_v2 import build_dashboard_v2_snapshot
from trading_app.pre_market_check import (
    PreMarketCheckConfig,
    build_pre_market_check_report,
)


FIXED_NOW = datetime(2026, 6, 18, 8, 55, tzinfo=timezone.utc)


def _config(mode: str = "live-sim") -> PreMarketCheckConfig:
    return PreMarketCheckConfig(
        requested_mode=mode,
        strategy_reboot_v2_enabled=True,
        dashboard_v2_enabled=True,
        dry_run_entry_intents_enabled=True,
        dry_run_exit_sell_intents_enabled=True,
        order_manager_enabled=True,
        order_manager_mode="LIVE_SIM" if mode in {"live-sim", "LIVE_SIM_LIMITED"} else "DRY_RUN",
        allow_live_sim_orders=mode in {"live-sim", "LIVE_SIM_LIMITED"},
        account_whitelist=("SIM12345",),
        max_order_quantity=1,
        max_order_amount=100_000,
        max_daily_buy_orders=3,
        max_daily_sell_orders=10,
        max_open_positions=3,
        cancel_unfilled_after_sec=45,
        live_sim_max_order_quantity_ceiling=1,
        live_sim_max_order_amount_ceiling=100_000,
        opening_burst_configured=True,
    )


def _snapshot() -> dict:
    return {
        "trade_date": "2026-06-18",
        "core": {"service": "trading-core-api", "mode": "OBSERVE"},
        "runtime": {"last_error": ""},
        "dashboard_v2_available": True,
        "sqlite": {"writable": True, "status": "OK"},
        "gateway": {
            "connected": True,
            "heartbeat_ok": True,
            "heartbeat_age_sec": 1.0,
            "kiwoom_logged_in": True,
            "orderable": True,
            "account": "SIM12345",
            "last_heartbeat_payload": {"server_gubun": "1"},
        },
        "commands": {"queued_count": 0, "dispatched_count": 0, "stale_dispatched_count": 0},
        "order_manager": {
            "enabled": True,
            "mode": "LIVE_SIM",
            "live_sim_orders_allowed": True,
            "broker_env": "SIMULATION",
            "account": "SIM12345",
            "account_whitelisted": True,
            "kill_switch_state": "NORMAL",
            "open_order_count": 0,
            "unmanaged_pending_order_count": 0,
        },
        "data_preload": {
            "theme_membership_loaded": True,
            "symbol_master_loaded": True,
            "prev_close_loaded": True,
            "avg_turnover_loaded": True,
            "warehouse_preload_status": "OK",
            "local_cache_available": True,
        },
        "theme_board": {"status": "OK", "calculated_at": "2026-06-18T08:54:00+09:00", "top_themes": [{"theme_name": "A"}]},
        "market_regime": {"status": "OK", "global_status": "EXPANSION", "index_watch_codes_configured": True},
        "risk": {"daily_loss_state_loaded": True},
    }


def _report(snapshot: dict | None = None, config: PreMarketCheckConfig | None = None) -> dict:
    return build_pre_market_check_report(snapshot or _snapshot(), config=config or _config(), now=FIXED_NOW).to_dict()


def test_all_required_pass_returns_go_live_sim_limited():
    report = _report()

    assert report["go_no_go"] == "GO_LIVE_SIM_LIMITED"
    assert report["summary_status"] == "PASS"
    assert report["fail_count"] == 0
    assert report["blocking_reasons"] == []


def test_real_broker_is_no_go():
    snapshot = _snapshot()
    snapshot["order_manager"]["broker_env"] = "REAL"

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "REAL_BROKER_DETECTED" in report["blocking_reasons"]


def test_unknown_broker_live_sim_is_no_go():
    snapshot = _snapshot()
    snapshot["order_manager"]["broker_env"] = "UNKNOWN"
    snapshot["gateway"]["last_heartbeat_payload"] = {}

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "BROKER_ENV_UNKNOWN_FOR_LIVE_SIM" in report["blocking_reasons"]


def test_account_whitelist_failure_is_no_go():
    snapshot = _snapshot()
    snapshot["gateway"]["account"] = "OTHER"
    snapshot["order_manager"]["account"] = "OTHER"
    snapshot["order_manager"]["account_whitelisted"] = False

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "ACCOUNT_WHITELIST_FAIL" in report["blocking_reasons"]


def test_gateway_heartbeat_stale_is_no_go():
    snapshot = _snapshot()
    snapshot["gateway"]["heartbeat_ok"] = False
    snapshot["gateway"]["heartbeat_age_sec"] = 60

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "GATEWAY_HEARTBEAT_STALE" in report["blocking_reasons"]


def test_sqlite_unhealthy_is_no_go():
    snapshot = _snapshot()
    snapshot["sqlite"] = {"writable": False, "status": "FAIL", "error": "readonly"}

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "SQLITE_OPERATIONAL_STORE_UNHEALTHY" in report["blocking_reasons"]


def test_kill_switch_active_is_no_go():
    snapshot = _snapshot()
    snapshot["order_manager"]["kill_switch_state"] = "KILL_SWITCH_ACTIVE"

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "KILL_SWITCH_ACTIVE" in report["blocking_reasons"]


def test_order_manager_disabled_observe_can_go_observe():
    snapshot = _snapshot()
    snapshot["order_manager"]["enabled"] = False
    snapshot["order_manager"]["mode"] = "OBSERVE"
    snapshot["order_manager"]["live_sim_orders_allowed"] = False

    config = _config("observe")
    config = PreMarketCheckConfig(**{**config.__dict__, "order_manager_enabled": False, "order_manager_mode": "OBSERVE", "allow_live_sim_orders": False})
    report = _report(snapshot, config)

    assert report["go_no_go"] == "GO_OBSERVE"


def test_dry_run_flags_can_go_dry_run():
    snapshot = _snapshot()
    snapshot["order_manager"]["enabled"] = False
    snapshot["order_manager"]["mode"] = "DRY_RUN"
    snapshot["order_manager"]["live_sim_orders_allowed"] = False

    config = _config("dry-run")
    config = PreMarketCheckConfig(
        **{
            **config.__dict__,
            "order_manager_enabled": False,
            "order_manager_mode": "DRY_RUN",
            "allow_live_sim_orders": False,
        }
    )
    report = _report(snapshot, config)

    assert report["go_no_go"] == "GO_DRY_RUN"


def test_pending_unmanaged_order_requires_no_go_or_manual_review():
    snapshot = _snapshot()
    snapshot["order_manager"]["unmanaged_pending_order_count"] = 1

    report = _report(snapshot)

    assert report["go_no_go"] == "NO_GO"
    assert "UNMANAGED_PENDING_ORDER_RECONCILE_REQUIRED" in report["blocking_reasons"]


def test_partial_preload_failure_with_local_cache_requires_manual_review():
    snapshot = _snapshot()
    snapshot["data_preload"]["warehouse_preload_status"] = "FAILED"
    snapshot["data_preload"]["local_cache_available"] = True

    report = _report(snapshot)

    assert report["go_no_go"] == "MANUAL_REVIEW_REQUIRED"
    assert "WAREHOUSE_PRELOAD_FAILED_LOCAL_CACHE_AVAILABLE" in report["warning_reasons"]


def test_ops_pre_market_check_endpoint_stable_schema_and_rebuild_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        payload = client.get("/api/ops/pre-market-check?mode=observe").json()
        rejected = client.post("/api/ops/pre-market-check/rebuild?mode=observe")

    assert payload["schema_version"] == "pre_market_check.v1"
    assert payload["requested_mode"] == "OBSERVE"
    assert set(payload) >= {
        "trade_date",
        "checked_at",
        "go_no_go",
        "summary_status",
        "items",
        "blocking_reasons",
        "operator_message_ko",
        "recommended_action_ko",
    }
    assert rejected.status_code == 401


def test_dashboard_v2_snapshot_contains_pre_market_check_and_banner(monkeypatch):
    monkeypatch.setenv("TRADING_DASHBOARD_V2_ENABLED", "1")
    report = _report({**_snapshot(), "sqlite": {"writable": False, "status": "FAIL"}})

    payload = build_dashboard_v2_snapshot({"pre_market_check": report})

    assert payload["pre_market_check"]["go_no_go"] == "NO_GO"
    assert any(item["reason_code"] == "PRE_MARKET_NO_GO" for item in payload["safety_banners"])


def test_cli_returns_exit_code_2_for_no_go(tmp_path):
    report_path = tmp_path / "no_go.json"
    report = _report({**_snapshot(), "sqlite": {"writable": False, "status": "FAIL"}})
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "tools/pre_market_check.py", "--input-json", str(report_path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "NO_GO" in completed.stdout
