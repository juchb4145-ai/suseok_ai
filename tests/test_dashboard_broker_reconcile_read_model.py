from trading_app.dashboard_read_model import DashboardReadModelService


def test_dashboard_read_model_exposes_broker_reconcile_health():
    service = DashboardReadModelService(repository=None)

    payload = service.build_from_runtime(
        runtime_snapshot={},
        gateway_snapshot={"connected": True, "heartbeat_ok": True},
        command_snapshot={},
        core_status={
            "broker_reconcile": {
                "enabled": True,
                "dispatch_enabled": False,
                "status": "RECONCILE_REQUIRED",
                "broker_truth_ready": False,
                "reconcile_clean": False,
                "snapshot_complete": True,
                "discrepancy_count": 1,
                "critical_discrepancy_count": 1,
                "stop_new_buy": True,
                "reduce_only": False,
            }
        },
    )

    assert payload["broker_reconcile"]["status"] == "RECONCILE_REQUIRED"
    assert payload["system_health"]["broker_reconcile"]["broker_truth_ready"] is False
    assert any(item.get("reason_code") == "BROKER_RECONCILE_DISCREPANCY" for item in payload["safety_banners"])
