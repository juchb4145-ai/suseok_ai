from __future__ import annotations

import json
import importlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.strategy.runtime_settings import LEGACY_DEFAULT_SETTINGS, legacy_profile_payload
from trading_app.dependencies import CoreSettings
from trading_app.live_sim_preflight import (
    PREFLIGHT_STATUS_FAIL_CLOSED,
    PREFLIGHT_STATUS_GO,
    PREFLIGHT_STATUS_INSUFFICIENT_DATA,
    PREFLIGHT_STATUS_NO_GO,
    LiveSimPreflightService,
)


def test_live_sim_preflight_go_masks_account_and_checks_core_guards(tmp_path):
    db = _db(tmp_path)
    _save_settings(db, allowed_account_numbers=["1234567890"])
    state = _gateway(account="1234567890", broker_env="SIMULATION", account_mode="SIMULATION")

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=_go_report(),
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_GO
    assert snapshot["blocking_reasons"] == []
    assert snapshot["account_mode_summary"]["account_masked"] == "12******90"
    assert snapshot["account_mode_summary"]["simulation_confirmed"] is True
    assert snapshot["safety_summary"]["live_real_enabled"] is False
    assert "1234567890" not in json.dumps(snapshot, ensure_ascii=False)
    db.close()


def test_live_sim_preflight_ignores_old_cumulative_rate_limit_count(tmp_path):
    db = _db(tmp_path)
    _save_settings(db)
    state = _gateway()
    state.record_event(
        GatewayEvent(
            type="rate_limited",
            event_id="evt-preflight-old-rate-limit",
            timestamp=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"),
            payload={"command_type": "tr_request", "wait_time_sec": 0.25},
        )
    )

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=_go_report(),
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_GO
    assert "GATEWAY_RATE_LIMIT_RECENT" not in snapshot["warning_reasons"]
    assert snapshot["gateway_load_summary"]["recent_rate_limit_count"] == 0
    assert snapshot["gateway_load_summary"]["total_rate_limited_count"] == 1
    db.close()


def test_live_sim_preflight_insufficient_data_blocks_startup(tmp_path):
    db = _db(tmp_path)
    _save_settings(db)
    state = _gateway()

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report={"summary": {}, "items": []},
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_INSUFFICIENT_DATA
    assert "PERFORMANCE_REPORT_MISSING_OR_EMPTY" in snapshot["blocking_reasons"]
    db.close()


def test_live_sim_preflight_no_go_for_negative_performance(tmp_path):
    db = _db(tmp_path)
    _save_settings(db)
    state = _gateway()
    report = _go_report()
    report["summary"]["go_no_go"] = {
        "decision": "NO_GO",
        "readiness": "READY",
        "blocked_by": ["NET_EXPECTANCY_NOT_POSITIVE", "BAD_READY_RATE_TOO_HIGH"],
    }

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=report,
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_NO_GO
    assert "NET_EXPECTANCY_NOT_POSITIVE" in snapshot["blocking_reasons"]
    assert "BAD_READY_RATE_TOO_HIGH" in snapshot["blocking_reasons"]
    db.close()


def test_live_sim_preflight_fail_closed_for_real_or_live_real_modes(tmp_path):
    db = _db(tmp_path)
    _save_settings(db, mode="LIVE_REAL", live_real_enabled=True)
    state = _gateway(broker_env="REAL", account_mode="REAL")

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=_go_report(),
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_FAIL_CLOSED
    assert "BROKER_ACCOUNT_REAL_DETECTED" in snapshot["blocking_reasons"]
    assert "LIVE_REAL_EXECUTION_CONFIG_DETECTED" in snapshot["blocking_reasons"]
    assert "LIVE_REAL_ENABLED_TRUE" in snapshot["blocking_reasons"]
    db.close()


def test_live_sim_preflight_fail_closed_when_tr_backfill_caused_ready(tmp_path):
    db = _db(tmp_path)
    _save_settings(db)
    state = _gateway()

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=_go_report(),
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(tr_backfill_caused_ready_count=1),
    )

    assert snapshot["status"] == PREFLIGHT_STATUS_FAIL_CLOSED
    assert "THEME_BACKFILL_CAUSED_READY_DETECTED" in snapshot["blocking_reasons"]
    db.close()


def test_live_sim_preflight_rebuild_persists_history(tmp_path):
    db = _db(tmp_path)
    _save_settings(db)
    state = _gateway()

    snapshot = _service(db, state, tmp_path).build_snapshot(
        performance_report=_go_report(),
        transport_status=_transport(),
        theme_lab_snapshot=_theme_lab(),
        persist=True,
    )
    history = db.list_live_sim_preflight_snapshots(limit=5)

    assert history[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert history[0]["status"] == PREFLIGHT_STATUS_GO
    assert db.latest_live_sim_preflight_snapshot()["snapshot_id"] == snapshot["snapshot_id"]
    db.close()


def test_live_sim_preflight_api_get_rebuild_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_RUNTIME_ENABLED", "0")
    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        current = client.get("/api/runtime/live-sim/preflight?include_details=false")
        rebuilt = client.post(
            "/api/runtime/live-sim/preflight/rebuild?include_details=false",
            headers={"X-Local-Token": "test-token"},
        )
        history = client.get("/api/runtime/live-sim/preflight/history?limit=5&offset=0")

    assert current.status_code == 200
    assert rebuilt.status_code == 200
    rebuilt_payload = rebuilt.json()
    history_payload = history.json()
    assert rebuilt_payload["status"] in {
        PREFLIGHT_STATUS_GO,
        PREFLIGHT_STATUS_INSUFFICIENT_DATA,
        PREFLIGHT_STATUS_NO_GO,
        PREFLIGHT_STATUS_FAIL_CLOSED,
        "GO_WITH_WARNINGS",
    }
    assert history_payload["items"][0]["snapshot_id"] == rebuilt_payload["snapshot_id"]


def _db(tmp_path) -> TradingDatabase:
    return TradingDatabase(str(tmp_path / "trader.sqlite3"))


def _service(db: TradingDatabase, state: GatewayStateStore, tmp_path) -> LiveSimPreflightService:
    return LiveSimPreflightService(
        db,
        state,
        settings=CoreSettings(db_path=tmp_path / "trader.sqlite3", local_token="test-token", runtime_mode="DRY_RUN"),
    )


def _save_settings(db: TradingDatabase, **execution_overrides) -> None:
    raw = json.loads(json.dumps(LEGACY_DEFAULT_SETTINGS))
    raw["order_execution"] = {
        **raw["order_execution"],
        "mode": "LIVE_SIM",
        "live_sim_enabled": True,
        "live_real_enabled": False,
        "kill_switch_active": False,
        "require_simulated_account": True,
        "allowed_account_mode": "SIMULATION",
        "block_real_account": True,
        "fail_closed_on_account_unknown": True,
        **execution_overrides,
    }
    payload = legacy_profile_payload()
    payload["settings_json"] = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    payload["config_json"] = payload["settings_json"]
    db.save_strategy_runtime_settings_profile(payload)


def _gateway(*, account: str = "1234567890", broker_env: str = "SIMULATION", account_mode: str = "SIMULATION") -> GatewayStateStore:
    state = GatewayStateStore()
    state.record_event(
        GatewayEvent(
            type="heartbeat",
            event_id=f"evt-preflight-{broker_env}-{account_mode}",
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": account,
                "broker_env": broker_env,
                "account_mode": account_mode,
                "server_mode": broker_env,
            },
        )
    )
    return state


def _go_report() -> dict:
    return {
        "report_id": "dry_run_perf_test",
        "status": "OK",
        "generated_at": "2026-06-16T09:00:00+09:00",
        "trade_date": "2026-06-16",
        "summary": {
            "total_lifecycle_count": 50,
            "trade_day_count": 5,
            "accepted_completed_lifecycle_count": 35,
            "dry_run_accepted_count": 35,
            "net_expectancy": 0.012,
            "cost_adjusted_bad_ready_rate": 0.05,
            "cost_adjusted_opportunity_loss_rate": 0.03,
            "execution_realism": {
                "stale_tick_rate": 0.02,
                "gateway_latency_high_rate": 0.01,
            },
            "go_no_go": {
                "decision": "GO",
                "readiness": "READY",
                "blocked_by": [],
                "criteria": [],
            },
        },
    }


def _transport() -> dict:
    return {"latest_summary": {"command_latency_p95_ms": 50.0, "command_latency_p99_ms": 80.0}}


def _theme_lab(*, tr_backfill_caused_ready_count: int = 0) -> dict:
    return {
        "theme_backfill_runtime": {
            "enabled": True,
            "observe_only": True,
            "queued_count": 0,
            "dispatched_count": 0,
            "parser_miss_ratio": 0.0,
            "tr_backfill_caused_ready_count": tr_backfill_caused_ready_count,
            "load_guard": {"load_guard_status": "OK", "paused_backfill": False, "pause_reason_codes": []},
        }
    }
