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


def test_dashboard_v2_source_status_exposes_canonical_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.save_snapshot(
        service.build_from_runtime(_runtime_snapshot(), {"heartbeat_ok": True}, {}, {"cycle_count": 4}),
    )
    _install_service(api, service)

    status = api.dashboard_v2_source_status()

    assert status["canonical_namespace"] == "reboot_v2.main"
    assert status["view_name"] == "reboot_v2.main"
    assert status["generation"] == 1
    assert status["checksum_prefix"]
    assert status["active_rest_contract"] == "direct_dashboard_v2"
    assert status["active_ws_contract"] == "snapshot_wrapper.dashboard_v2"


def test_dashboard_v2_ws_wrapper_includes_core_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(
        api.runtime_supervisor,
        "lightweight_status",
        lambda: {"running": True, "cycle_count": 7, "last_cycle_at": "2026-06-18T00:00:01+00:00"},
    )

    result = api._dashboard_snapshot_wrapper_for_v2(
        {
            "schema_version": "dashboard_v2.reboot_ops.v1",
            "read_model": {"generation": 3, "checksum": "abc", "status": "OK"},
        }
    )

    assert result["core"]["service"] == "trading-core-api"
    assert result["core"]["mode"] == api.get_settings().mode
    assert result["core"]["running"] is True
    assert result["core"]["cycle_count"] == 7
    assert result["runtime"]["lightweight_status"]["running"] is True


def test_dashboard_ws_broadcast_uses_canonical_read_model_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    monkeypatch.setattr(
        api,
        "_dashboard_v2_read_model_or_fallback",
        lambda **_: {
            "schema_version": "dashboard_v2.reboot_ops.v1",
            "snapshot_namespace": "reboot_v2.main",
            "generated_at": "2026-06-18T00:00:00+00:00",
            "market_overview": {"global_status": "SELECTIVE"},
            "read_model": {
                "view_name": "reboot_v2.main",
                "snapshot_namespace": "reboot_v2.main",
                "generation": 7,
                "checksum": "read-model-checksum",
                "status": "OK",
            },
        },
    )
    monkeypatch.setattr(
        api,
        "_build_dashboard_snapshot_payload",
        lambda **_: (_ for _ in ()).throw(AssertionError("legacy live builder called")),
    )

    result = api._dashboard_snapshot_payload_for_ws_client_count(2)

    assert result["dashboard_v2"]["read_model"]["generation"] == 7
    assert result["dashboard_v2"]["read_model"]["checksum"] == "read-model-checksum"
    assert result["dashboard_v2"]["snapshot_namespace"] == "reboot_v2.main"


def test_dashboard_read_model_runtime_snapshot_includes_pre_market_check(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DB_PATH", str(tmp_path / "trader.sqlite3"))
    import trading_app.api as api

    api = importlib.reload(api)
    captured = {}
    monkeypatch.setattr(api.runtime_supervisor, "snapshot", lambda: {"runtime_profile": "V2_OBSERVE"})
    monkeypatch.setattr(
        api,
        "order_manager_dashboard_section",
        lambda *args, **kwargs: {"mode": "OBSERVE", "kill_switch_state": "NORMAL"},
    )
    monkeypatch.setattr(api, "build_candidate_ingestion_snapshot", lambda *args, **kwargs: {"enabled": True})
    monkeypatch.setattr(api, "theme_board_dashboard_section", lambda *args, **kwargs: {"enabled": True, "status": "OK"})
    monkeypatch.setattr(
        api,
        "market_regime_dashboard_section",
        lambda *args, **kwargs: {"enabled": True, "status": "OK", "index_watch_codes_configured": False},
    )
    monkeypatch.setattr(api, "entry_engine_dashboard_section", lambda *args, **kwargs: {"enabled": True, "status": "EMPTY"})
    monkeypatch.setattr(api, "exit_engine_dashboard_section", lambda *args, **kwargs: {"enabled": True, "status": "EMPTY"})
    monkeypatch.setattr(api, "position_risk_dashboard_section", lambda *args, **kwargs: {"enabled": True, "status": "OK"})

    def _pre_market_payload(db, *, requested_mode, base_snapshot=None):
        captured.update(base_snapshot or {})
        return {
            "schema_version": "pre_market_check.v1",
            "requested_mode": requested_mode.upper(),
            "go_no_go": "NO_GO",
            "operator_message_ko": "운영 금지",
            "items": [{"key": "broker_environment", "details": {"broker_env": "SIMULATION"}}],
        }

    monkeypatch.setattr(
        api,
        "_build_pre_market_check_report_payload",
        _pre_market_payload,
    )

    result = api._dashboard_read_model_runtime_snapshot()

    assert result["runtime_profile"] == "V2_OBSERVE"
    assert result["pre_market_check"]["go_no_go"] == "NO_GO"
    assert result["pre_market_check"]["operator_message_ko"] == "운영 금지"
    assert captured["dashboard_v2_available"] is True
    assert captured["order_manager"]["mode"] == "OBSERVE"
    assert captured["theme_board"]["status"] == "OK"
    assert captured["market_regime"]["index_watch_codes_configured"] is False
    assert result["candidate_ingestion"]["enabled"] is True
    assert result["entry_engine"]["status"] == "EMPTY"
    assert result["position_risk"]["status"] == "OK"


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


def test_order_reconcile_warning_does_not_mark_snapshot_stale(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    payload = service.build_from_runtime(
        _runtime_snapshot(),
        {"heartbeat_ok": True},
        {},
        {"cycle_count": 4, "last_cycle_at": "2026-06-18T00:00:00+00:00"},
    )
    service.save_snapshot(payload)

    result = service.read_main_snapshot()

    assert result["read_model"]["stale"] is False
    assert result["read_model"]["status"] == "OK"
    assert "ORDER_RECONCILE_REQUIRED" in result["read_model"]["warnings"]
    messages = [item["message_ko"] for item in result["safety_banners"]]
    assert "Dashboard snapshot이 오래되었습니다. 마지막 정상 데이터를 표시 중입니다." not in messages
    assert any(item["reason_code"] == "ORDER_RECONCILE_REQUIRED" for item in result["safety_banners"])


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
