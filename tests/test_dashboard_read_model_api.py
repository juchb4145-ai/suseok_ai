import importlib
from datetime import datetime, timedelta, timezone

from storage.dashboard_read_model import DashboardReadModelRepository
from trading_app.dashboard_read_model import DashboardReadModelConfig, DashboardReadModelService


class _Clock:
    def __init__(self):
        self.value = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _runtime_snapshot():
    return {
        "runtime_profile": "V2_OBSERVE",
        "reboot_v2_enabled": True,
        "market_regime": {"enabled": True, "status": "OK", "global_status": "SELECTIVE"},
        "theme_board": {"enabled": True, "status": "OK", "top_themes": [{"theme_name": "AI", "theme_score": 91}]},
        "entry_engine": {"enabled": True, "status": "OK", "decisions": []},
        "order_manager_v2": {
            "status": "OK",
            "enabled": False,
            "observe_only": True,
            "intent_enabled": False,
            "local_order_enabled": False,
            "gateway_command_enqueue_enabled": False,
            "send_order_allowed": False,
            "risk_state": "NORMAL",
            "kill_switch_state": "STOP_NEW_BUY",
            "created_intent_count": 1,
            "risk_approved_count": 0,
            "risk_rejected_count": 1,
            "local_order_created_count": 0,
            "command_blocked_observe_only_count": 0,
            "queued_command_count": 0,
            "reconcile_required_count": 1,
            "stop_new_buy": True,
            "reduce_only": False,
            "last_reject_reason": "STALE_TICK",
        },
    }


def _install_service(api, service):
    api._dashboard_read_model_service = service
    api._dashboard_read_model_db_path = api._dashboard_database_cache_key()
    api.dashboard_read_model_writer = api._build_dashboard_read_model_writer(service)
    api.runtime_supervisor.set_dashboard_read_model_writer(api.dashboard_read_model_writer)


def _service(tmp_path, clock):
    return DashboardReadModelService(
        DashboardReadModelRepository(tmp_path / "read_model.sqlite3"),
        config=DashboardReadModelConfig(write_interval_sec=1, stale_after_sec=5, skip_unchanged=True),
        clock=clock,
    )


def test_dashboard_v2_snapshot_reads_read_model_without_live_builder(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    clock = _Clock()
    service = _service(tmp_path, clock)
    payload = service.build_from_runtime(
        _runtime_snapshot(),
        {"heartbeat_ok": True},
        {"queued_count": 0},
        {"running": True, "cycle_count": 3, "last_cycle_at": "2026-06-18T00:00:00+00:00"},
    )
    service.save_snapshot(payload)
    _install_service(api, service)
    monkeypatch.setattr(api, "_build_dashboard_snapshot_payload", lambda **_: (_ for _ in ()).throw(AssertionError("live builder called")))

    result = api.dashboard_v2_snapshot(refresh=False, detail="slim")

    assert result["read_model"]["source"] == "READ_MODEL"
    assert result["read_model"]["generation"] == 1
    assert result["market_overview"]["global_status"] == "SELECTIVE"
    assert result["order_manager"]["stop_new_buy"] is True
    assert result["order_manager"]["reconcile_required_count"] == 1


def test_snapshot_view_v2_returns_same_read_model_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.save_snapshot(
        service.build_from_runtime(_runtime_snapshot(), {"heartbeat_ok": True}, {}, {"cycle_count": 4}),
    )
    _install_service(api, service)

    direct = api.dashboard_v2_snapshot(refresh=False, detail="slim")
    via_snapshot = api.snapshot(refresh=False, detail="slim", view="v2")

    assert direct["read_model"]["generation"] == via_snapshot["read_model"]["generation"]
    assert direct["read_model"]["checksum"] == via_snapshot["read_model"]["checksum"]


def test_stale_read_model_does_not_trigger_live_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.save_snapshot(
        service.build_from_runtime(_runtime_snapshot(), {"heartbeat_ok": True}, {}, {"cycle_count": 4}),
    )
    clock.advance(6)
    _install_service(api, service)
    monkeypatch.setattr(api, "_build_dashboard_snapshot_payload", lambda **_: (_ for _ in ()).throw(AssertionError("live builder called")))

    result = api.dashboard_v2_snapshot(refresh=False, detail="slim")

    assert result["read_model"]["stale"] is True
    assert result["read_model"]["fallback_used"] is False
    assert any(item["reason_code"] == "READ_MODEL_STALE" for item in result["safety_banners"])


def test_missing_read_model_uses_marked_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    service = _service(tmp_path, _Clock())
    _install_service(api, service)
    monkeypatch.setattr(
        api,
        "_build_dashboard_snapshot_payload",
        lambda **_: {"gateway": {"heartbeat_ok": True}, "runtime": {"runtime_profile": "V2_OBSERVE"}},
    )

    result = api.dashboard_v2_snapshot(refresh=False, detail="slim")

    assert result["read_model"]["source"] == "FALLBACK_LIVE_BUILD"
    assert result["read_model"]["fallback_used"] is True
