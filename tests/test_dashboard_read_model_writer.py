from datetime import datetime, timedelta, timezone

from storage.dashboard_read_model import DashboardReadModelRepository
from trading_app.dashboard_read_model import (
    DashboardReadModelConfig,
    DashboardReadModelService,
    DashboardReadModelWriter,
    compare_dashboard_v2_snapshots,
)


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
        "theme_board": {"enabled": True, "status": "OK", "top_themes": []},
        "entry_engine": {"enabled": True, "status": "OK", "decisions": []},
        "order_manager_v2": {
            "status": "OK",
            "enabled": False,
            "observe_only": True,
            "intent_enabled": False,
            "send_order_allowed": False,
            "kill_switch_state": "NORMAL",
        },
    }


def _service(tmp_path, clock):
    return DashboardReadModelService(
        DashboardReadModelRepository(tmp_path / "read_model.sqlite3"),
        config=DashboardReadModelConfig(write_interval_sec=1, stale_after_sec=5, skip_unchanged=True),
        clock=clock,
    )


def test_read_model_normalizes_disabled_order_manager_warning_in_observe_mode(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    runtime = _runtime_snapshot()
    runtime["order_manager_v2"] = {
        **runtime["order_manager_v2"],
        "mode": "OBSERVE",
        "warnings": ["ORDER_MANAGER_DISABLED"],
    }

    payload = service.build_from_runtime(
        runtime,
        {"heartbeat_ok": True},
        {"queued_count": 0},
        {"running": True, "cycle_count": 1, "last_cycle_at": "2026-06-18T00:00:00+00:00"},
    )

    reasons = {item["reason_code"] for item in payload["wait_block_reasons"]["items"]}
    assert payload["order_manager"]["warnings"] == ["ORDER_MANAGER_OBSERVE_ONLY"]
    assert "ORDER_MANAGER_OBSERVE_ONLY" in reasons
    assert "ORDER_MANAGER_DISABLED" not in reasons


def test_writer_coalesces_many_dirty_signals_within_one_second(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    writer = DashboardReadModelWriter(
        service,
        runtime_snapshot=_runtime_snapshot,
        gateway_snapshot=lambda: {"heartbeat_ok": True},
        command_snapshot=lambda: {"queued_count": 0},
        core_status=lambda: {"running": True, "cycle_count": 1, "last_cycle_at": "2026-06-18T00:00:00+00:00"},
        clock=clock,
    )

    for index in range(100):
        writer.mark_dirty(f"signal:{index}")
    first = writer.write_if_due()
    second = writer.write_if_due()

    assert first["status"] == "OK"
    assert first["generation"] == 1
    assert second["status"] == "SKIPPED"
    assert second["reason"] == "NOT_DIRTY"
    assert service.metrics["write_count"] == 1


def test_writer_skips_until_write_interval_elapsed(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    writer = DashboardReadModelWriter(
        service,
        runtime_snapshot=_runtime_snapshot,
        gateway_snapshot=lambda: {"heartbeat_ok": True},
        command_snapshot=lambda: {},
        core_status=lambda: {"running": True, "cycle_count": 1},
        clock=clock,
    )

    writer.mark_dirty("runtime_cycle")
    writer.write_if_due()
    writer.mark_dirty("gateway_health")
    coalesced = writer.write_if_due()
    clock.advance(1)
    due = writer.write_if_due()

    assert coalesced["status"] == "COALESCED"
    assert due["status"] == "OK"
    assert service.metrics["write_count"] == 1
    assert service.metrics["unchanged_skip_count"] == 1


def test_writer_running_guard_skips_concurrent_execution(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    service.metrics["writer_status"] = "RUNNING"
    service.mark_dirty("runtime_cycle")

    result = service.write_if_due(
        runtime_snapshot=_runtime_snapshot(),
        gateway_snapshot={},
        command_snapshot={},
        core_status={},
        now=clock(),
    )

    assert result["status"] == "SKIPPED"
    assert result["reason"] == "WRITER_RUNNING"
    assert service.metrics["concurrent_write_skip_count"] == 1


def test_writer_callback_failure_is_fail_soft(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    writer = DashboardReadModelWriter(
        service,
        runtime_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("snapshot boom")),
        gateway_snapshot=lambda: {},
        command_snapshot=lambda: {},
        core_status=lambda: {},
        clock=clock,
    )

    result = writer.write_if_due(force=True)

    assert result["status"] == "FAILED"
    assert "snapshot boom" in result["error"]
    assert service.metrics["writer_status"] == "FAILED"


def test_stale_snapshot_returns_banner_without_live_rebuild(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    writer = DashboardReadModelWriter(
        service,
        runtime_snapshot=_runtime_snapshot,
        gateway_snapshot=lambda: {"heartbeat_ok": True},
        command_snapshot=lambda: {},
        core_status=lambda: {"running": True, "cycle_count": 1, "last_cycle_at": "2026-06-18T00:00:00+00:00"},
        clock=clock,
    )
    writer.mark_dirty("runtime_cycle")
    writer.write_if_due()
    clock.advance(6)

    payload = service.read_main_snapshot()

    assert payload["read_model"]["stale"] is True
    assert "READ_MODEL_STALE" in payload["read_model"]["warnings"]
    assert any(item["reason_code"] == "READ_MODEL_STALE" for item in payload["safety_banners"])


def test_shadow_compare_reports_section_mismatch():
    read_model = {
        "v2_status": {"status_label": "관찰전용"},
        "market_overview": {"global_status": "SELECTIVE"},
        "leading_themes": {"items": [{"theme_name": "AI", "leader_symbol": "000001"}]},
        "entry_candidates": {"bucket_counts": {"WAIT": 1}},
        "position_risk": {"open_position_count": 0, "exit_now_count": 0, "scale_out_count": 0},
        "order_manager": {"risk_state": "NORMAL", "kill_switch_state": "NORMAL", "reconcile_required_count": 0},
        "wait_block_reasons": {"items": [{"reason_code": "LATEST_TICK_MISSING"}]},
        "system_health": {"summary_status": "정상"},
    }
    legacy = {
        **read_model,
        "market_overview": {"global_status": "RISK_OFF"},
    }

    result = compare_dashboard_v2_snapshots(read_model, legacy, compared_at="2026-06-18T00:00:00+00:00")

    assert result["matched"] is False
    assert result["section_mismatch_count"] == 1
    assert result["mismatched_sections"] == ["market_overview"]
