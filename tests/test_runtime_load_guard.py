from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from trading.broker.command_queue import CommandPriority
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayCommand, GatewayEvent
from trading.theme_engine.backfill import ThemeBackfillConfig, ThemeBackfillService
from trading_app.runtime_load_guard import (
    LOAD_GUARD_FAIL_CLOSED,
    LOAD_GUARD_OK,
    LOAD_GUARD_PAUSED,
    build_runtime_load_guard_snapshot,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_runtime_load_guard_ok_when_gateway_and_theme_flow_are_quiet():
    state = _healthy_state()

    snapshot = build_runtime_load_guard_snapshot(
        state,
        raw_theme_lab={"watchset_snapshots": []},
        transport_status={"latest_summary": {"command_latency_p95_ms": 100}},
        backfill_summary={"parser_miss_ratio": 0.0, "tr_backfill_caused_ready_count": 0},
    )

    assert snapshot["load_guard_status"] == LOAD_GUARD_OK
    assert snapshot["paused_backfill"] is False
    assert snapshot["pause_reason_codes"] == []


def test_runtime_load_guard_pauses_for_ready_and_order_pending():
    state = _healthy_state()
    state.enqueue_command(GatewayCommand(type="send_order", command_id="cmd-order"), priority=CommandPriority.HIGH)

    snapshot = build_runtime_load_guard_snapshot(
        state,
        raw_theme_lab={"watchset_snapshots": [{"gate_status": "READY"}]},
        backfill_summary={"parser_miss_ratio": 0.0},
    )

    assert snapshot["load_guard_status"] == LOAD_GUARD_PAUSED
    assert snapshot["paused_backfill"] is True
    assert "READY_OR_READY_SMALL_PRESENT" in snapshot["pause_reason_codes"]
    assert "ORDER_COMMAND_PENDING" in snapshot["pause_reason_codes"]


def test_runtime_load_guard_fail_closed_when_backfill_caused_ready():
    state = _healthy_state()

    snapshot = build_runtime_load_guard_snapshot(
        state,
        raw_theme_lab={"watchset_snapshots": []},
        backfill_summary={"tr_backfill_caused_ready_count": 1},
    )

    assert snapshot["load_guard_status"] == LOAD_GUARD_FAIL_CLOSED
    assert snapshot["paused_backfill"] is True
    assert "TR_BACKFILL_CAUSED_READY" in snapshot["pause_reason_codes"]


def test_theme_backfill_service_stops_new_dispatch_when_load_guard_pauses():
    state = _healthy_state()
    service = ThemeBackfillService(
        state,
        config=ThemeBackfillConfig(enabled=True, max_per_cycle=3, max_pending=5),
        load_guard_provider=lambda _gateway, _result, _summary: {
            "load_guard_status": LOAD_GUARD_PAUSED,
            "paused_backfill": True,
            "pause_reason_codes": ["READY_OR_READY_SMALL_PRESENT"],
            "operator_message_ko": "테스트 pause",
            "affected_services": ["theme_backfill"],
        },
    )

    summary = service.plan_and_enqueue(_result(), NOW)

    assert summary["paused_reason"] == "READY_OR_READY_SMALL_PRESENT"
    assert summary["load_guard_status"] == LOAD_GUARD_PAUSED
    assert summary["paused_backfill"] is True
    assert state.list_commands(limit=10, include_finished=True) == []


def _healthy_state() -> GatewayStateStore:
    state = GatewayStateStore()
    state.record_event(
        GatewayEvent(
            type="heartbeat",
            event_id="evt-load-guard-heartbeat",
            payload={"kiwoom_logged_in": True, "orderable": True, "broker_env": "SIMULATION"},
        )
    )
    return state


def _result():
    hit = SimpleNamespace(
        symbol="000001",
        name="테스트",
        excluded=False,
        current_price=0,
        return_pct=3.0,
        turnover_krw=1_000_000,
        alive_hit=True,
        strong_hit=True,
        leader_hit=True,
        data_quality_flags=("MISSING_CURRENT_PRICE",),
    )
    theme = SimpleNamespace(
        theme_id="theme",
        eligible_total_members=1,
        data_quality_flags=("MISSING_CURRENT_PRICE",),
        member_hits=(hit,),
    )
    return SimpleNamespace(themes=(theme,), watchset=())
