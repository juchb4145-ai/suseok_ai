from __future__ import annotations

from trading_app.dashboard_read_model import DashboardReadModelService


def test_dashboard_read_model_exposes_order_lifecycle_health():
    service = DashboardReadModelService(repository=None)
    payload = service.build_from_runtime(
        runtime_snapshot={},
        gateway_snapshot={"connected": True, "heartbeat_ok": True},
        command_snapshot={},
        core_status={
            "running": True,
            "order_event_consumer": {
                "status": "DEGRADED",
                "consumer_enabled": True,
                "consumer_running": True,
                "order_lifecycle_ready": False,
                "pending_event_count": 1,
                "retry_wait_count": 0,
                "failed_count": 0,
                "dead_letter_count": 1,
                "oldest_pending_age_sec": 12.0,
                "processed_count": 3,
                "duplicate_applied_count": 1,
                "unmatched_event_count": 1,
                "reconcile_required_count": 1,
                "last_event_type": "execution_event",
                "last_event_at": "2026-06-18T00:00:00+00:00",
                "last_processed_at": "2026-06-18T00:00:01+00:00",
                "last_error": "ORDER_EVENT_DEAD_LETTER_PRESENT",
                "replay_status": "OK",
                "replay_duration_ms": 3.2,
            },
        },
    )

    lifecycle = payload["order_lifecycle"]
    assert lifecycle["order_lifecycle_ready"] is False
    assert payload["system_health"]["order_lifecycle"]["dead_letter_count"] == 1
    banner_reasons = {item["reason_code"] for item in payload["safety_banners"]}
    assert "ORDER_LIFECYCLE_NOT_READY" in banner_reasons
    assert "ORDER_EVENT_DEAD_LETTER" in banner_reasons
    assert "UNMATCHED_ORDER_EVENT" in banner_reasons
