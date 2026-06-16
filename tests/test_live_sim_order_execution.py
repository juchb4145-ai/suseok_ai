from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerExecutionEvent, BrokerOrderRequest, BrokerOrderResult, GatewayEvent, utc_timestamp
from trading.strategy.runtime import StrategyRuntime, StrategyRuntimeSnapshot
from trading.strategy.runtime_settings import StrategyRuntimeSettings
from trading_app.dependencies import CoreSettings
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.order_enqueue_service import OrderEnqueueService, RuntimeOrderIntentRequest
from trading_app.runtime_factory import _build_order_sink
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, LiveSimRuntimeOrderSink, NoopRuntimeOrderSink
from tools.repair_live_sim_positions import analyze_repairs, apply_repairs


def _settings(tmp_path: Path) -> CoreSettings:
    return CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        mode="OBSERVE",
        allow_live=False,
        runtime_mode="DRY_RUN",
        runtime_allow_dry_run_orders=True,
        runtime_dry_run_account="",
        runtime_dry_run_position_amount=1_000_000,
    )


def _live_sim_execution(**overrides):
    payload = {
        "mode": "LIVE_SIM",
        "live_sim_enabled": True,
        "live_real_enabled": False,
        "allowed_account_numbers": [],
        "fail_closed_on_account_unknown": True,
        "submit_first_leg_only": True,
        "max_orders_per_day": 5,
        "max_rejected_orders_per_day": 3,
        "max_order_amount_krw": 300_000,
        "min_order_amount_krw": 0,
        "allow_market_order": False,
        "kill_switch_enabled": True,
        "kill_switch_active": False,
    }
    payload.update(overrides)
    return payload


def _exit_guard(**overrides):
    payload = {
        "enabled": True,
        "stop_loss_pct": -2.0,
        "take_profit_pct": 5.0,
        "max_hold_minutes": 60,
    }
    payload.update(overrides)
    return payload


def _state(*, account="1234567890", broker_env="SIMULATION", available_cash_krw=0) -> GatewayStateStore:
    payload = {
        "kiwoom_logged_in": True,
        "orderable": True,
        "account": account,
        "broker_env": broker_env,
        "server_mode": broker_env,
        "mode": broker_env,
    }
    if available_cash_krw:
        payload["available_cash_krw"] = available_cash_krw
    state = GatewayStateStore()
    state.record_event(
        GatewayEvent(
            type="heartbeat",
            timestamp=utc_timestamp(),
            payload=payload,
        )
    )
    return state


def _request(**overrides) -> RuntimeOrderIntentRequest:
    payload = {
        "source": "strategy_runtime",
        "dry_run": False,
        "account": "1234567890",
        "code": "005930",
        "side": "buy",
        "quantity": 3,
        "price": 70000,
        "order_type": 1,
        "hoga": "00",
        "tag": "runtime:pullback",
        "candidate_id": 11,
        "entry_plan_id": 21,
        "virtual_order_id": 31,
        "leg_index": 1,
        "entry_type": "pullback",
        "gate_status": "READY",
        "gate_reason": "READY_PULLBACK",
        "runtime_cycle_at": "2026-05-30T09:02:00",
        "metadata": {
            "candidate_instance_id": "ci-1",
            "support_ready": True,
            "latest_tick_ready": True,
            "reason_codes": ["READY_PULLBACK"],
        },
    }
    payload.update(overrides)
    return RuntimeOrderIntentRequest(**payload)


def _service(tmp_path: Path, *, state: GatewayStateStore | None = None, clock=None):
    settings = _settings(tmp_path)
    gateway_state = state or _state()
    return OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path, clock=clock), settings, gateway_state


def _submit_accepted_order(tmp_path: Path, *, clock=None, quantity=3):
    service, settings, gateway_state = _service(tmp_path, clock=clock)
    execution = _live_sim_execution(max_order_amount_krw=max(300_000, int(quantity) * 70_000))
    submit = service.enqueue_live_sim_order(
        _request(quantity=quantity),
        execution_config=execution,
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="accepted",
                request=BrokerOrderRequest.from_dict(dict(submit.command["payload"])),
                order_no="A0001",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
    finally:
        db.close()
    return service, settings, gateway_state, submit


def _lifecycle(**overrides):
    payload = {
        "enabled": True,
        "cancel_unfilled_buy_after_sec": 60,
        "cancel_unfilled_sell_after_sec": 60,
        "cancel_partial_remainder_after_sec": 90,
        "max_cancel_attempts": 2,
        "block_new_order_when_cancel_pending": True,
        "block_new_buy_if_cancel_scheduler_unhealthy": True,
    }
    payload.update(overrides)
    return payload


def _reconcile(**overrides):
    payload = {
        "enabled": True,
        "reconcile_on_startup": True,
        "reconcile_on_reconnect": True,
        "max_reconcile_failures": 3,
        "block_new_buy_on_reconcile_failure": True,
    }
    payload.update(overrides)
    return payload


def test_factory_default_order_execution_stays_dry_run(tmp_path):
    settings = _settings(tmp_path)
    runtime_settings = StrategyRuntimeSettings.legacy_default()
    sink = _build_order_sink(settings, _state(), None, runtime_settings=runtime_settings)

    assert isinstance(sink, DryRunRuntimeOrderSink)
    assert not isinstance(sink, LiveSimRuntimeOrderSink)


def test_factory_live_sim_requires_explicit_runtime_setting(tmp_path):
    settings = _settings(tmp_path)
    runtime_settings = StrategyRuntimeSettings.from_settings_json(
        {"order_execution": _live_sim_execution(), "live_sim_exit_guard": _exit_guard()}
    )
    sink = _build_order_sink(settings, _state(), None, runtime_settings=runtime_settings)

    assert isinstance(sink, LiveSimRuntimeOrderSink)


def test_factory_live_sim_preflight_not_go_uses_dry_run_collection_sink(tmp_path):
    settings = replace(
        _settings(tmp_path),
        runtime_live_sim_require_preflight_go_for_order_sink=True,
    )
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_preflight_snapshot(
            {
                "snapshot_id": "preflight-insufficient",
                "checked_at": "2026-05-30T09:00:00+09:00",
                "status": "INSUFFICIENT_DATA",
                "blocking_reasons": ["MIN_5_TRADE_DAYS"],
                "warning_reasons": [],
            }
        )
    finally:
        db.close()
    runtime_settings = StrategyRuntimeSettings.from_settings_json(
        {"order_execution": _live_sim_execution(), "live_sim_exit_guard": _exit_guard()}
    )
    warnings: list[str] = []

    sink = _build_order_sink(settings, _state(), warnings.append, runtime_settings=runtime_settings)

    assert isinstance(sink, DryRunRuntimeOrderSink)
    assert not isinstance(sink, LiveSimRuntimeOrderSink)
    assert "LIVE_SIM_ORDER_SINK_PREFLIGHT_NOT_GO:INSUFFICIENT_DATA" in warnings


def test_strategy_runtime_snapshot_includes_live_sim_order_sink_fields(tmp_path):
    service, _, _ = _service(tmp_path)
    runtime_settings = StrategyRuntimeSettings.from_settings_json(
        {"order_execution": _live_sim_execution(), "live_sim_exit_guard": _exit_guard()}
    )
    sink = LiveSimRuntimeOrderSink(settings=_settings(tmp_path), service=service, runtime_settings=runtime_settings)
    runtime = object.__new__(StrategyRuntime)
    runtime.order_sink = sink
    snapshot = StrategyRuntimeSnapshot()

    StrategyRuntime._apply_order_sink_snapshot(runtime, snapshot)

    assert snapshot.live_sim_order_sink_enabled is True
    assert snapshot.live_sim_order_policy == "LIVE_SIM_FIRST_LEG_GUARDED"
    assert snapshot.live_sim_order_intent_count == 0
    assert snapshot.live_sim_summary["submitted_order_count"] == 0


def test_factory_blocks_live_real_even_when_runtime_live_orders_requested(tmp_path):
    settings = CoreSettings(
        db_path=Path(tmp_path) / "runtime.sqlite3",
        local_token="test-token",
        mode="LIVE",
        allow_live=True,
        runtime_mode="DRY_RUN",
        runtime_allow_dry_run_orders=True,
        runtime_allow_live_orders=True,
    )
    runtime_settings = StrategyRuntimeSettings.from_settings_json(
        {"order_execution": {"mode": "LIVE_REAL", "live_real_enabled": True}}
    )
    warnings: list[str] = []
    sink = _build_order_sink(settings, _state(), warnings.append, runtime_settings=runtime_settings)

    assert isinstance(sink, NoopRuntimeOrderSink)
    assert sink.reason == "LIVE_REAL_ORDER_BLOCKED"
    assert "LIVE_REAL_ORDER_BLOCKED" in warnings


def test_live_sim_sim_account_passes_and_enqueues_one_send_order(tmp_path):
    service, settings, gateway_state = _service(tmp_path)

    result = service.enqueue_live_sim_order(
        _request(),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
    )

    assert result.accepted is True
    assert result.status == "SUBMITTED"
    assert result.command is not None
    assert result.command["type"] == "send_order"
    assert gateway_state.command_snapshot()["queued_count"] == 1

    db = TradingDatabase(str(settings.db_path))
    try:
        row = db.get_live_sim_order(result.intent_id)
    finally:
        db.close()
    assert row["order_status"] == "SUBMITTED"
    assert row["account_id_masked"] == "12******90"
    assert "LIVE_SIM_ORDER_ALLOWED" in row["reason_codes"]
    assert "ACCOUNT_GUARD_PASSED_SIMULATION" in row["reason_codes"]


def test_live_sim_blocks_real_or_unknown_account_environment(tmp_path):
    real_service, _, real_state = _service(tmp_path / "real", state=_state(broker_env="REAL"))
    unknown_service, _, unknown_state = _service(tmp_path / "unknown", state=_state(broker_env=""))

    real = real_service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())
    unknown = unknown_service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())

    assert real.accepted is False
    assert real.reason == "ACCOUNT_GUARD_FAILED_REAL_ACCOUNT"
    assert real_state.command_snapshot()["queued_count"] == 0
    assert unknown.accepted is False
    assert unknown.reason in {"BROKER_SERVER_MODE_UNKNOWN", "BROKER_ENV_UNKNOWN"}
    assert unknown_state.command_snapshot()["queued_count"] == 0


def test_live_sim_blocks_late_chase_market_wait_chase_risk_and_missing_support(tmp_path):
    cases = [
        (_request(metadata={"support_ready": False, "support_missing_reason": "NO_SUPPORT"}), "DATA_INSUFFICIENT"),
        (_request(metadata={"support_ready": True, "latest_tick_ready": True, "late_chase_level": "soft_block"}), "LATE_CHASE_TEMP_WAIT"),
        (_request(metadata={"support_ready": True, "latest_tick_ready": True, "reason_codes": ["WAIT_MARKET_CONFIRMATION_PENDING"]}), "WAIT_MARKET_CONFIRMATION_PENDING"),
        (_request(metadata={"support_ready": True, "latest_tick_ready": True, "reason_codes": ["CHASE_RISK"]}), "CHASE_RISK"),
    ]
    for index, (request, expected_reason) in enumerate(cases):
        service, _, state = _service(tmp_path / str(index))
        result = service.enqueue_live_sim_order(request, execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())

        assert result.accepted is False
        assert result.reason == expected_reason
        assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_blocks_second_third_leg_and_duplicate_first_leg(tmp_path):
    service, _, state = _service(tmp_path)

    second_leg = service.enqueue_live_sim_order(
        _request(leg_index=2, virtual_order_id=32),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
    )
    first = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())
    duplicate = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())

    assert second_leg.accepted is False
    assert second_leg.reason == "SECOND_THIRD_LEG_BLOCKED_BEFORE_FIRST_FILL"
    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.status == "DUPLICATE"
    assert state.command_snapshot()["queued_count"] == 1


def test_live_sim_exit_guard_disabled_or_kill_switch_blocks_buy(tmp_path):
    service, _, state = _service(tmp_path)

    no_exit = service.enqueue_live_sim_order(
        _request(),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(enabled=False),
    )
    killed = service.enqueue_live_sim_order(
        _request(virtual_order_id=32),
        execution_config=_live_sim_execution(kill_switch_active=True),
        exit_guard_config=_exit_guard(),
    )

    assert no_exit.accepted is False
    assert no_exit.reason == "EXIT_GUARD_NOT_READY_BUY_BLOCKED"
    assert killed.accepted is False
    assert killed.reason == "LIVE_SIM_KILL_SWITCH_ACTIVE"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_order_result_and_fills_update_order_position_and_ignore_duplicate_fill(tmp_path):
    service, settings, _ = _service(tmp_path)
    submit = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())
    request = BrokerOrderRequest.from_dict(dict(submit.command["payload"]))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="accepted",
                request=request,
                order_no="A0001",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        accepted = db.get_live_sim_order(submit.intent_id)
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=1,
                remaining_quantity=2,
                execution_id="fill-1",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=1,
                remaining_quantity=2,
                execution_id="fill-1",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        partial = db.get_live_sim_order(submit.intent_id)
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=2,
                remaining_quantity=0,
                execution_id="fill-2",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        filled = db.get_live_sim_order(submit.intent_id)
        summary = db.live_sim_summary()
    finally:
        db.close()

    assert accepted["order_status"] == "ACCEPTED"
    assert partial["order_status"] == "PARTIAL_FILLED"
    assert filled["order_status"] == "FILLED"
    assert summary["filled_order_count"] == 1
    assert summary["opened_position_count"] == 1
    assert filled["details"]["position"]["current_qty"] == 3
    assert filled["details"]["position"]["stop_loss_price"] == 68600
    assert filled["details"]["position"]["take_profit_price"] == 73500
    assert filled["details"]["position"]["max_hold_exit_at"]


def test_live_sim_cumulative_execution_events_apply_only_delta_to_position(tmp_path):
    service, settings, _ = _service(tmp_path)
    submit = service.enqueue_live_sim_order(
        _request(quantity=10),
        execution_config=_live_sim_execution(max_order_amount_krw=1_000_000),
        exit_guard_config=_exit_guard(),
    )
    request = BrokerOrderRequest.from_dict(dict(submit.command["payload"]))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="accepted",
                request=request,
                order_no="A0001",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        for execution_id, filled, remaining in [
            ("ack-0", 0, 10),
            ("partial-2", 2, 8),
            ("final-10", 10, 0),
        ]:
            db.save_execution(
                BrokerExecutionEvent(
                    code="005930",
                    order_no="A0001",
                    side="buy",
                    quantity=10,
                    price=70000,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    execution_id=execution_id,
                    command_id=submit.command_id,
                    idempotency_key=submit.idempotency_key,
                )
            )
        filled_order = db.get_live_sim_order(submit.intent_id)
        fills = db.conn.execute(
            """
            SELECT fill_id, fill_qty, cumulative_fill_qty, remaining_qty
            FROM live_sim_fill_events
            WHERE order_intent_id = ?
            ORDER BY id ASC
            """,
            (submit.intent_id,),
        ).fetchall()
    finally:
        db.close()

    assert filled_order["order_status"] == "FILLED"
    assert filled_order["details"]["position"]["current_qty"] == 10
    assert [(row["fill_id"], row["fill_qty"], row["cumulative_fill_qty"], row["remaining_qty"]) for row in fills] == [
        ("ack-0", 0, 0, 10),
        ("partial-2", 2, 2, 8),
        ("final-10", 8, 10, 0),
    ]


def test_live_sim_execution_links_pending_order_when_order_result_has_no_order_no(tmp_path):
    service, settings, _ = _service(tmp_path)
    submit = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())
    request = BrokerOrderRequest.from_dict(dict(submit.command["payload"]))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="accepted",
                request=request,
                order_no="",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=3,
                remaining_quantity=0,
                execution_id="fill-no-order-result-order-no",
            )
        )
        filled = db.get_live_sim_order(submit.intent_id)
        summary = db.live_sim_summary()
    finally:
        db.close()

    assert filled["broker_order_id"] == "A0001"
    assert filled["order_status"] == "FILLED"
    assert summary["filled_order_count"] == 1


def test_live_sim_exit_uses_open_position_quantity_instead_of_virtual_quantity(tmp_path):
    service, settings, state = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-1",
                "candidate_instance_id": "ci-1",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 10,
                "entry_avg_price": 70000,
                "current_qty": 10,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_order(
        _request(
            side="sell",
            quantity=1,
            price=69000,
            order_type=2,
            order_phase="exit",
            exit_percent=100.0,
            exit_quantity=1,
            position_quantity=1,
            remaining_quantity=0,
            metadata={"candidate_instance_id": "ci-1", "full_exit": True, "position_closed": True},
        ),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )
    commands = state.dispatch_commands(limit=10)
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(result.intent_id)
    finally:
        db.close()

    assert result.accepted is True
    assert result.request["quantity"] == 10
    assert order["requested_qty"] == 10
    assert order["details"]["exit_quantity_resolution"]["current_qty"] == 10
    assert commands[0].payload["quantity"] == 10
    assert commands[0].payload["live_sim_exit_requested_qty"] == 1
    assert commands[0].payload["live_sim_exit_resolved_qty"] == 10


def test_live_sim_exit_blocks_when_open_position_quantity_is_unknown(tmp_path):
    service, _, state = _service(tmp_path)

    result = service.enqueue_live_sim_order(
        _request(
            side="sell",
            quantity=1,
            price=69000,
            order_type=2,
            order_phase="exit",
            exit_percent=100.0,
            metadata={"candidate_instance_id": "ci-missing", "full_exit": True},
        ),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "LIVE_SIM_EXIT_POSITION_NOT_FOUND"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_unfilled_buy_auto_cancel_creates_cancel_order(tmp_path):
    service, settings, gateway_state, submit = _submit_accepted_order(tmp_path, clock=lambda: "2026-05-30T09:00:00")
    db = TradingDatabase(str(settings.db_path))
    try:
        db.update_live_sim_order(submit.intent_id, {"submitted_at": "2026-05-30T09:00:00", "accepted_at": "2026-05-30T09:00:01"})
    finally:
        db.close()

    service.clock = lambda: "2026-05-30T09:02:10"
    result = service.run_live_sim_order_lifecycle(
        execution_config=_live_sim_execution(),
        lifecycle_config=_lifecycle(cancel_unfilled_buy_after_sec=60),
    )

    assert result["status"] == "HEALTHY"
    assert len(result["cancelled"]) == 1
    assert result["cancelled"][0]["command"]["type"] == "cancel_order"
    assert gateway_state.command_snapshot()["queued_count"] == 2
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(submit.intent_id)
        cancels = db.list_live_sim_cancel_orders()
    finally:
        db.close()
    assert order["order_status"] == "CANCEL_REQUESTED"
    assert "LIVE_SIM_UNFILLED_BUY_CANCEL_DUE" in order["reason_codes"]
    assert "LIVE_SIM_CANCEL_ORDER_QUEUED" in cancels[0]["reason_codes"]


def test_strategy_runtime_live_sim_maintenance_cancels_stale_sell_order(tmp_path):
    service, settings, gateway_state = _service(tmp_path, clock=lambda: "2026-05-30T09:00:00")
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "sell-1",
                "trade_date": "2026-05-30",
                "code": "005930",
                "side": "sell",
                "order_status": "ACCEPTED",
                "broker_order_id": "S0001",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-1",
                "requested_qty": 10,
                "requested_price": 69000,
                "submitted_qty": 10,
                "submitted_price": 69000,
                "submitted_at": "2026-05-30T09:00:00",
                "accepted_at": "2026-05-30T09:00:01",
                "created_at": "2026-05-30T09:00:00",
                "updated_at": "2026-05-30T09:00:01",
            }
        )
    finally:
        db.close()
    service.clock = lambda: "2026-05-30T09:02:10"
    runtime_settings = StrategyRuntimeSettings.from_settings_json(
        {
            "order_execution": _live_sim_execution(max_order_amount_krw=1_000_000),
            "live_sim_exit_guard": _exit_guard(),
            "live_sim_order_lifecycle": _lifecycle(cancel_unfilled_sell_after_sec=60),
        }
    )
    sink = LiveSimRuntimeOrderSink(settings=settings, service=service, runtime_settings=runtime_settings)
    runtime = object.__new__(StrategyRuntime)
    runtime.order_sink = sink
    snapshot = StrategyRuntimeSnapshot()

    StrategyRuntime._run_order_sink_maintenance(runtime, snapshot, datetime.fromisoformat("2026-05-30T09:02:10"))

    assert snapshot.live_sim_maintenance_summary["lifecycle"]["status"] == "HEALTHY"
    assert len(snapshot.live_sim_maintenance_summary["lifecycle"]["cancelled"]) == 1
    assert gateway_state.command_snapshot()["queued_count"] == 1
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order("sell-1")
        cancel = db.list_live_sim_cancel_orders()[0]
    finally:
        db.close()
    assert order["order_status"] == "CANCEL_REQUESTED"
    assert cancel["cancel_qty"] == 10
    assert "LIVE_SIM_UNFILLED_SELL_CANCEL_DUE" in cancel["reason_codes"]


def test_live_sim_partial_remainder_cancel_keeps_filled_position_qty(tmp_path):
    service, settings, _, submit = _submit_accepted_order(tmp_path, clock=lambda: "2026-05-30T09:00:00", quantity=100)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=100,
                price=70000,
                filled_quantity=40,
                remaining_quantity=60,
                execution_id="partial-1",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
                timestamp="2026-05-30T09:00:10",
            )
        )
    finally:
        db.close()

    service.clock = lambda: "2026-05-30T09:02:00"
    result = service.run_live_sim_order_lifecycle(
        execution_config=_live_sim_execution(max_order_amount_krw=10_000_000),
        lifecycle_config=_lifecycle(cancel_partial_remainder_after_sec=90),
    )

    assert len(result["cancelled"]) == 1
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(submit.intent_id)
        position = order["details"]["position"]
        cancel = db.list_live_sim_cancel_orders()[0]
    finally:
        db.close()
    assert position["current_qty"] == 40
    assert cancel["cancel_qty"] == 60
    assert "LIVE_SIM_PARTIAL_REMAINDER_CANCEL_DUE" in cancel["reason_codes"]


def test_live_sim_cancel_duplicate_is_blocked(tmp_path):
    service, settings, _, submit = _submit_accepted_order(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(submit.intent_id)
    finally:
        db.close()

    first = service.enqueue_live_sim_cancel_order(
        order,
        cancel_qty=3,
        cancel_reason="unfilled_buy",
        execution_config=_live_sim_execution(),
        lifecycle_config=_lifecycle(),
    )
    second = service.enqueue_live_sim_cancel_order(
        order,
        cancel_qty=3,
        cancel_reason="unfilled_buy",
        execution_config=_live_sim_execution(),
        lifecycle_config=_lifecycle(),
    )

    assert first.accepted is True
    assert second.accepted is False
    assert second.reason == "LIVE_SIM_CANCEL_DUPLICATE_BLOCKED"


def test_live_sim_stop_loss_take_profit_and_max_hold_exit_monitor(tmp_path):
    service, settings, gateway_state = _service(tmp_path, clock=lambda: "2026-05-30T09:00:00")
    db = TradingDatabase(str(settings.db_path))
    try:
        for suffix, opened_at in [("stop", "2026-05-30T09:00:00"), ("take", "2026-05-30T09:00:00"), ("hold", "2026-05-30T07:50:00")]:
            db.save_live_sim_position(
                {
                    "position_id": f"LIVE_SIM:12******90:{suffix}",
                    "candidate_instance_id": suffix,
                    "code": suffix,
                    "account_id_masked": "12******90",
                    "opened_at": opened_at,
                    "entry_qty": 10,
                    "entry_avg_price": 10000,
                    "current_qty": 10,
                    "stop_loss_price": 9800,
                    "take_profit_price": 10500,
                    "max_hold_exit_at": "2026-05-30T08:50:00" if suffix == "hold" else "2026-05-30T10:30:00",
                    "status": "OPEN",
                    "updated_at": opened_at,
                }
            )
    finally:
        db.close()

    result = service.run_live_sim_exit_monitor(
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
        latest_ticks={
            "stop": {"price": 9800, "timestamp": "2026-05-30T09:00:00"},
            "take": {"price": 10500, "timestamp": "2026-05-30T09:00:00"},
            "hold": {"price": 10000, "timestamp": "2026-05-30T09:00:00"},
        },
        now="2026-05-30T09:00:00",
    )

    assert result["status"] == "HEALTHY"
    assert len(result["orders"]) == 3
    assert gateway_state.command_snapshot()["queued_count"] == 3
    reasons = {item["reason"] for item in result["orders"]}
    assert reasons == {"LIVE_SIM_ORDER_ALLOWED"}
    db = TradingDatabase(str(settings.db_path))
    try:
        rows = db.list_live_sim_orders(side="sell", limit=10)
    finally:
        db.close()
    flattened = {code for row in rows for code in row["reason_codes"]}
    assert "LIVE_SIM_EXIT_ORDER_SUBMITTED" in flattened


def test_live_sim_exit_tick_stale_marks_unhealthy_and_blocks_buy(tmp_path):
    service, _, state = _service(tmp_path)
    db = TradingDatabase(str(service.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:stale",
                "candidate_instance_id": "stale",
                "code": "stale",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 1,
                "entry_avg_price": 10000,
                "current_qty": 1,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    monitor = service.run_live_sim_exit_monitor(
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(require_latest_tick_ready_for_exit=True, max_exit_tick_age_sec=10),
        latest_ticks={"stale": {"price": 10000, "timestamp": "2026-05-30T09:00:00"}},
        now="2026-05-30T09:01:00",
    )
    buy = service.enqueue_live_sim_order(
        _request(virtual_order_id=99),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(block_new_buy_if_exit_loop_unhealthy=True),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert monitor["status"] == "UNHEALTHY"
    assert buy.accepted is False
    assert buy.reason == "LIVE_SIM_BUY_BLOCKED_EXIT_MONITOR_UNHEALTHY"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_startup_reconcile_filled_order_updates_position(tmp_path):
    service, settings, _, submit = _submit_accepted_order(tmp_path)

    result = service.run_live_sim_reconcile(
        reconcile_config=_reconcile(),
        trigger="startup",
        broker_snapshot={
            "fills": [
                {
                    "broker_order_id": "A0001",
                    "code": "005930",
                    "side": "buy",
                    "quantity": 3,
                    "price": 70000,
                    "filled_quantity": 3,
                    "remaining_quantity": 0,
                    "execution_id": "broker-fill-1",
                }
            ]
        },
    )

    assert result["status"] == "COMPLETED"
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(submit.intent_id)
        summary = db.live_sim_summary()
    finally:
        db.close()
    assert order["order_status"] == "FILLED"
    assert "LIVE_SIM_RECONCILE_ORDER_FILLED_FROM_BROKER" in result["event"]["reason_codes"]
    assert summary["filled_order_count"] == 1


def test_live_sim_reconcile_external_position_blocks_new_buy(tmp_path):
    service, _, state = _service(tmp_path)

    reconcile = service.run_live_sim_reconcile(
        reconcile_config=_reconcile(),
        trigger="startup",
        broker_snapshot={"positions": [{"code": "005930", "quantity": 5, "avg_price": 70000, "account": "1234567890"}]},
    )
    buy = service.enqueue_live_sim_order(
        _request(virtual_order_id=77),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert reconcile["status"] == "COMPLETED"
    assert buy.accepted is False
    assert buy.reason == "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_reconcile_matching_broker_position_clears_position_reconcile_required(tmp_path):
    service, settings, _ = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-1",
                "candidate_instance_id": "ci-1",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 5,
                "entry_avg_price": 70000,
                "current_qty": 5,
                "status": "RECONCILE_REQUIRED",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.run_live_sim_reconcile(
        reconcile_config=_reconcile(),
        trigger="startup",
        broker_snapshot={"positions": [{"code": "005930", "quantity": 5, "avg_price": 70000, "account": "1234567890"}]},
    )

    db = TradingDatabase(str(settings.db_path))
    try:
        position = db.get_live_sim_position("LIVE_SIM:12******90:005930:ci-1")
        health = db.get_live_sim_runtime_health("reconcile")
    finally:
        db.close()

    assert result["status"] == "COMPLETED"
    assert position["status"] == "OPEN"
    assert health["status"] == "HEALTHY"


def test_live_sim_unknown_submit_blocks_new_buy(tmp_path):
    service, settings, state = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "unknown-1",
                "trade_date": "2026-05-30",
                "code": "005930",
                "account_id_masked": "12******90",
                "side": "buy",
                "order_status": "UNKNOWN_SUBMIT",
                "candidate_instance_id": "ci-1",
                "requested_qty": 1,
                "requested_price": 70000,
                "submitted_qty": 1,
                "submitted_price": 70000,
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_order(
        _request(virtual_order_id=88),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "LIVE_SIM_BUY_BLOCKED_UNKNOWN_SUBMIT"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_manual_sell_execution_updates_matching_position_and_flags_reconcile(tmp_path):
    service, settings, _ = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-1",
                "candidate_instance_id": "ci-1",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 5,
                "entry_avg_price": 70000,
                "current_qty": 5,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="MANUAL1",
                side="sell",
                quantity=5,
                price=71000,
                filled_quantity=5,
                remaining_quantity=0,
                account="1234567890",
                execution_id="manual-sell-1",
            )
        )
        position = db.get_live_sim_position("LIVE_SIM:12******90:005930:ci-1")
        health = db.get_live_sim_runtime_health("reconcile")
        summary = db.live_sim_summary()
    finally:
        db.close()

    assert position["status"] == "CLOSED"
    assert position["current_qty"] == 0
    assert position["details"]["manual_intervention"] is True
    assert health["status"] == "RECONCILE_REQUIRED"
    assert summary["manual_intervention_count"] == 1


def test_live_sim_reconcile_required_anywhere_blocks_new_buy(tmp_path):
    service, settings, state = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:000660:ci-risk",
                "candidate_instance_id": "ci-risk",
                "code": "000660",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 1,
                "entry_avg_price": 200000,
                "current_qty": 1,
                "status": "RECONCILE_REQUIRED",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_order(
        _request(code="005930", virtual_order_id=901),
        execution_config=_live_sim_execution(),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "LIVE_SIM_BUY_BLOCKED_RECONCILE_REQUIRED"
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_data_quality_blocks_entry_unless_ready_early_small_cap_passes(tmp_path):
    service, _, state = _service(tmp_path)
    bad_data = {
        "candidate_instance_id": "ci-bad",
        "support_ready": True,
        "latest_tick_ready": True,
        "candidate_breadth_ready": False,
        "market_side_data_quality_flags": ["SIDE_BREADTH_SAMPLE_TOO_SMALL", "STALE_QUOTE"],
        "reason_codes": ["READY_PULLBACK"],
    }
    blocked = service.enqueue_live_sim_order(
        _request(metadata=bad_data, virtual_order_id=902),
        execution_config=_live_sim_execution(max_order_amount_krw=300_000),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )
    early_capped = service.enqueue_live_sim_order(
        _request(
            quantity=3,
            price=70000,
            gate_reason="READY_EARLY_SMALL",
            metadata={**bad_data, "candidate_instance_id": "ci-early", "reason_codes": ["READY_EARLY_SMALL"]},
            virtual_order_id=903,
        ),
        execution_config=_live_sim_execution(max_order_amount_krw=300_000),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )
    early_allowed = service.enqueue_live_sim_order(
        _request(
            quantity=2,
            price=70000,
            gate_reason="READY_EARLY_SMALL",
            metadata={**bad_data, "candidate_instance_id": "ci-early-small", "reason_codes": ["READY_EARLY_SMALL"]},
            virtual_order_id=904,
        ),
        execution_config=_live_sim_execution(max_order_amount_krw=300_000),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert blocked.accepted is False
    assert blocked.reason == "DATA_INSUFFICIENT"
    assert early_capped.accepted is False
    assert early_capped.reason == "ORDER_AMOUNT_LIMIT"
    assert early_capped.safety["details"]["early_small_cap_applied"] is True
    assert early_allowed.accepted is True
    assert state.command_snapshot()["queued_count"] == 1


def test_live_sim_cash_based_limits_resize_high_price_shadow_entry_to_one_share(tmp_path):
    service, _, state = _service(tmp_path, state=_state(available_cash_krw=46_000_000))
    metadata = {
        "candidate_instance_id": "ci-hynix",
        "support_ready": True,
        "latest_tick_ready": True,
        "reason_codes": ["READY_EARLY_SMALL"],
        "shadow_small_entry_dry_run_promoted": True,
        "shadow_small_entry_promotion_mode": "live_sim_guarded",
        "shadow_small_entry_promotion_order_enabled": True,
        "shadow_small_entry_position_size_multiplier": 0.1,
    }

    result = service.enqueue_live_sim_order(
        _request(code="000660", quantity=13, price=2_150_000, gate_reason="READY_EARLY_SMALL", metadata=metadata),
        execution_config=_live_sim_execution(
            cash_based_limits_enabled=True,
            available_cash_krw=46_000_000,
            daily_turnover_limit_pct=0.50,
            per_order_limit_pct=0.05,
            min_lot_exception_pct=0.06,
            max_order_amount_krw=300_000,
        ),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is True
    assert result.command["payload"]["quantity"] == 1
    assert result.command["payload"]["live_sim_quantity_adjusted_by_cash_limit"] is True
    assert result.command["payload"]["live_sim_original_quantity"] == 13
    assert result.command["payload"]["live_sim_min_lot_exception_applied"] is True
    assert result.record["requested_qty"] == 1
    assert result.record["details"]["cash_sizing"]["quantity_adjusted"] is True
    assert state.command_snapshot()["queued_count"] == 1


def test_live_sim_cash_based_amount_block_preserves_strategy_validation_metadata(tmp_path):
    service, _, state = _service(tmp_path, state=_state(available_cash_krw=46_000_000))
    metadata = {
        "candidate_instance_id": "ci-expensive",
        "support_ready": True,
        "latest_tick_ready": True,
        "reason_codes": ["READY_EARLY_SMALL"],
        "shadow_small_entry_dry_run_promoted": True,
        "shadow_small_entry_promotion_mode": "live_sim_guarded",
        "shadow_small_entry_promotion_order_enabled": True,
        "shadow_small_entry_position_size_multiplier": 0.1,
    }

    result = service.enqueue_live_sim_order(
        _request(code="999999", quantity=2, price=3_000_000, gate_reason="READY_EARLY_SMALL", metadata=metadata),
        execution_config=_live_sim_execution(
            cash_based_limits_enabled=True,
            available_cash_krw=46_000_000,
            daily_turnover_limit_pct=0.50,
            per_order_limit_pct=0.05,
            min_lot_exception_pct=0.06,
            max_order_amount_krw=300_000,
        ),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "ORDER_AMOUNT_LIMIT"
    validation = result.record["details"]["strategy_validation"]
    assert validation["signal_decision"] == "PASS"
    assert validation["execution_decision"] == "BLOCKED"
    assert validation["counterfactual_tracking_required"] is True
    assert validation["hypothetical_qty"] == 2
    assert validation["hypothetical_order_amount"] == 6_000_000
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_cash_based_total_exposure_blocks_order_and_preserves_validation(tmp_path):
    service, settings, state = _service(tmp_path, state=_state(available_cash_krw=46_000_000))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-total",
                "candidate_instance_id": "ci-total",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 150,
                "entry_avg_price": 70_000,
                "current_qty": 150,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_order(
        _request(
            code="000660",
            quantity=20,
            price=70_000,
            metadata={"candidate_instance_id": "ci-new", "support_ready": True, "latest_tick_ready": True, "reason_codes": ["READY_PULLBACK"]},
        ),
        execution_config=_live_sim_execution(
            cash_based_limits_enabled=True,
            available_cash_krw=46_000_000,
            per_order_limit_pct=0.05,
            total_exposure_limit_pct=0.25,
            per_symbol_exposure_limit_pct=0.07,
        ),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "LIVE_SIM_TOTAL_EXPOSURE_LIMIT"
    assert result.record["details"]["projected_total_exposure_krw"] == 11_900_000
    validation = result.record["details"]["strategy_validation"]
    assert validation["execution_decision"] == "BLOCKED"
    assert validation["block_reason"] == "LIVE_SIM_TOTAL_EXPOSURE_LIMIT"
    assert validation["hypothetical_order_amount"] == 1_400_000
    assert state.command_snapshot()["queued_count"] == 0


def test_live_sim_cash_based_symbol_exposure_blocks_order_and_preserves_validation(tmp_path):
    service, settings, state = _service(tmp_path, state=_state(available_cash_krw=46_000_000))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-symbol",
                "candidate_instance_id": "ci-symbol",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 40,
                "entry_avg_price": 70_000,
                "current_qty": 40,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_order(
        _request(
            code="005930",
            quantity=10,
            price=70_000,
            metadata={"candidate_instance_id": "ci-same-symbol", "support_ready": True, "latest_tick_ready": True, "reason_codes": ["READY_PULLBACK"]},
        ),
        execution_config=_live_sim_execution(
            cash_based_limits_enabled=True,
            available_cash_krw=46_000_000,
            per_order_limit_pct=0.05,
            total_exposure_limit_pct=0.25,
            per_symbol_exposure_limit_pct=0.07,
        ),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )

    assert result.accepted is False
    assert result.reason == "LIVE_SIM_SYMBOL_EXPOSURE_LIMIT"
    assert result.record["details"]["projected_symbol_exposure_krw"] == 3_500_000
    validation = result.record["details"]["strategy_validation"]
    assert validation["execution_decision"] == "BLOCKED"
    assert validation["block_reason"] == "LIVE_SIM_SYMBOL_EXPOSURE_LIMIT"
    assert validation["hypothetical_order_amount"] == 700_000
    assert state.command_snapshot()["queued_count"] == 0


def test_repair_live_sim_positions_corrects_cumulative_delta_overcount(tmp_path):
    service, settings, _ = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.save_live_sim_order(
            {
                "order_intent_id": "live-buy-1",
                "trade_date": "2026-05-30",
                "code": "005930",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-1",
                "side": "buy",
                "order_status": "FILLED",
                "requested_qty": 10,
                "requested_price": 70000,
                "submitted_qty": 10,
                "submitted_price": 70000,
                "broker_order_id": "A0001",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        db.save_live_sim_fill_event(
            {
                "order_intent_id": order["order_intent_id"],
                "broker_order_id": "A0001",
                "fill_id": "final-10",
                "code": "005930",
                "side": "buy",
                "account_id_masked": "12******90",
                "fill_qty": 8,
                "fill_price": 70000,
                "cumulative_fill_qty": 10,
                "remaining_qty": 0,
                "event_time": "2026-05-30T09:00:00",
                "received_at": "2026-05-30T09:00:00",
            }
        )
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-1",
                "candidate_instance_id": "ci-1",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 12,
                "entry_avg_price": 70000,
                "current_qty": 12,
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        report = analyze_repairs(db, trade_date="2026-05-30")
        applied = apply_repairs(db, report)
        position = db.get_live_sim_position("LIVE_SIM:12******90:005930:ci-1")
    finally:
        db.close()

    assert report["repair_count"] == 1
    assert report["position_repairs"][0]["corrected_qty"] == 10
    assert applied["applied_count"] == 1
    assert position["entry_qty"] == 10
    assert position["current_qty"] == 10
    assert position["status"] == "RECONCILE_REQUIRED"


def test_live_sim_audit_reports_submitted_command_and_order_funnel(tmp_path):
    service, settings, gateway_state = _service(tmp_path)
    submit = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())

    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.get_live_sim_order(submit.intent_id)
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=order["trade_date"])
    finally:
        db.close()

    assert submit.accepted is True
    assert report["available"] is True
    assert report["summary"]["order_status_counts"]["SUBMITTED"] == 1
    assert any(row["stage"] == "SUBMITTED" and row["count"] == 1 for row in report["order_funnel"])
    assert report["command_audit"][0]["command_type"] == "send_order"
    assert report["command_audit"][0]["live_sim_order_intent_id"] == submit.intent_id


def test_live_sim_order_result_without_order_no_marks_unknown_submit_and_rca_trace(tmp_path):
    service, settings, gateway_state = _service(tmp_path)
    submit = service.enqueue_live_sim_order(_request(), execution_config=_live_sim_execution(), exit_guard_config=_exit_guard())
    request = BrokerOrderRequest.from_dict(dict(submit.command["payload"]))
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="accepted without order no",
                request=request,
                order_no="",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        order = db.get_live_sim_order(submit.intent_id)
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=order["trade_date"])
        traces = db.list_buy_zero_trace_events(trade_date=order["trade_date"], code="005930", limit=20)
    finally:
        db.close()

    assert order["order_status"] == "UNKNOWN_SUBMIT"
    assert "LIVE_SIM_ORDER_NO_MISSING" in order["reason_codes"]
    assert order["details"]["order_result_link_status"] == "LINKED"
    assert report["summary"]["unknown_submit_count"] == 1
    assert any(issue["issue_type"] == "UNKNOWN_SUBMIT" for issue in report["reconcile_issues"])
    assert any(trace["stage"] == "LIVE_SIM_UNKNOWN_SUBMIT" for trace in traces)


def test_live_sim_audit_tracks_partial_final_fill_and_position_open_trace(tmp_path):
    service, settings, gateway_state, submit = _submit_accepted_order(tmp_path, quantity=3)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=1,
                remaining_quantity=2,
                execution_id="partial-audit-1",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=3,
                remaining_quantity=0,
                execution_id="final-audit-3",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
        order = db.get_live_sim_order(submit.intent_id)
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=order["trade_date"])
        traces = db.list_buy_zero_trace_events(trade_date=order["trade_date"], code="005930", limit=50)
    finally:
        db.close()

    stages = {trace["stage"] for trace in traces}
    assert order["order_status"] == "FILLED"
    assert report["summary"]["position_qty_mismatch_count"] == 0
    assert {"LIVE_SIM_COMMAND_QUEUED", "BROKER_ORDER_ACCEPTED", "PARTIAL_FILLED", "FILLED", "POSITION_OPENED"} <= stages


def test_live_sim_sell_fill_decreases_position_and_full_exit_closes_position(tmp_path):
    service, settings, gateway_state, submit = _submit_accepted_order(tmp_path, quantity=3)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="A0001",
                side="buy",
                quantity=3,
                price=70000,
                filled_quantity=3,
                remaining_quantity=0,
                execution_id="buy-fill-for-exit",
                command_id=submit.command_id,
                idempotency_key=submit.idempotency_key,
            )
        )
    finally:
        db.close()

    exit_submit = service.enqueue_live_sim_order(
        _request(
            side="sell",
            quantity=3,
            price=71000,
            order_type=2,
            order_phase="exit",
            metadata={"candidate_instance_id": "ci-1", "full_exit": True},
            virtual_order_id=777,
        ),
        execution_config=_live_sim_execution(max_order_amount_krw=1_000_000),
        exit_guard_config=_exit_guard(),
        lifecycle_config=_lifecycle(),
        reconcile_config=_reconcile(),
    )
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_order_result(
            BrokerOrderResult(
                ok=True,
                code=0,
                message="sell accepted",
                request=BrokerOrderRequest.from_dict(dict(exit_submit.command["payload"])),
                order_no="S0001",
                command_id=exit_submit.command_id,
                idempotency_key=exit_submit.idempotency_key,
            )
        )
        db.save_execution(
            BrokerExecutionEvent(
                code="005930",
                order_no="S0001",
                side="sell",
                quantity=3,
                price=71000,
                filled_quantity=3,
                remaining_quantity=0,
                execution_id="sell-fill-close",
                command_id=exit_submit.command_id,
                idempotency_key=exit_submit.idempotency_key,
            )
        )
        position = db.get_live_sim_position("LIVE_SIM:12******90:005930:ci-1")
        order = db.get_live_sim_order(exit_submit.intent_id)
        traces = db.list_buy_zero_trace_events(trade_date=order["trade_date"], code="005930", limit=100)
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=order["trade_date"])
    finally:
        db.close()

    assert position["status"] == "CLOSED"
    assert position["current_qty"] == 0
    assert order["order_status"] == "FILLED"
    assert any(trace["stage"] == "EXIT_FILLED" for trace in traces)
    assert any(trace["stage"] == "POSITION_CLOSED" for trace in traces)
    assert report["status"] in {"OK", "WARN"}


def test_live_sim_cancel_without_broker_order_id_goes_to_reconcile_audit(tmp_path):
    service, settings, gateway_state = _service(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        order = db.save_live_sim_order(
            {
                "order_intent_id": "cancel-missing-broker",
                "trade_date": "2026-05-30",
                "code": "005930",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-1",
                "side": "buy",
                "order_status": "ACCEPTED",
                "requested_qty": 3,
                "requested_price": 70000,
                "submitted_qty": 3,
                "submitted_price": 70000,
                "updated_at": "2026-05-30T09:00:00",
            }
        )
    finally:
        db.close()

    result = service.enqueue_live_sim_cancel_order(
        order,
        cancel_qty=3,
        cancel_reason="unfilled_buy",
        execution_config=_live_sim_execution(),
        lifecycle_config=_lifecycle(),
    )
    db = TradingDatabase(str(settings.db_path))
    try:
        updated = db.get_live_sim_order("cancel-missing-broker")
        report = LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    assert result.accepted is False
    assert result.status == "RECONCILE_REQUIRED"
    assert updated["order_status"] == "RECONCILE_REQUIRED"
    assert report["summary"]["broker_order_id_missing_count"] >= 1
    assert any(issue["issue_type"] in {"BROKER_ORDER_ID_MISSING", "LIVE_SIM_CANCEL_RECONCILE_REQUIRED"} for issue in report["issues"])


def test_live_sim_audit_flags_stale_cancel_requested(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "stale-cancel-order",
                "trade_date": "2026-05-30",
                "code": "005930",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-1",
                "side": "buy",
                "order_status": "CANCEL_REQUESTED",
                "broker_order_id": "A0001",
                "requested_qty": 3,
                "submitted_qty": 3,
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        db.save_live_sim_cancel_order(
            {
                "cancel_intent_id": "cancel-stale-1",
                "original_order_id": "stale-cancel-order",
                "broker_order_id": "A0001",
                "trade_date": "2026-05-30",
                "code": "005930",
                "side": "buy",
                "cancel_qty": 3,
                "cancel_reason": "unfilled_buy",
                "status": "SUBMITTED",
                "submitted_at": "2026-05-30T09:00:00",
                "created_at": "2026-05-30T09:00:00",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        report = LiveSimLifecycleAuditor(db).build_report(
            trade_date="2026-05-30",
            now="2026-05-30T09:10:00",
        )
    finally:
        db.close()

    assert report["summary"]["cancel_requested_stale_count"] >= 1
    assert any(issue["issue_type"] == "CANCEL_REQUESTED_STALE" for issue in report["cancel_issues"])


def test_live_sim_position_qty_mismatch_marks_reconcile_required(tmp_path):
    settings = _settings(tmp_path)
    db = TradingDatabase(str(settings.db_path))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "mismatch-buy",
                "trade_date": "2026-05-30",
                "code": "005930",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-1",
                "side": "buy",
                "order_status": "FILLED",
                "broker_order_id": "A0001",
                "requested_qty": 5,
                "submitted_qty": 5,
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        db.save_live_sim_fill_event(
            {
                "order_intent_id": "mismatch-buy",
                "broker_order_id": "A0001",
                "fill_id": "fill-5",
                "code": "005930",
                "side": "buy",
                "account_id_masked": "12******90",
                "fill_qty": 5,
                "fill_price": 70000,
                "cumulative_fill_qty": 5,
                "remaining_qty": 0,
                "event_time": "2026-05-30T09:00:00",
                "received_at": "2026-05-30T09:00:00",
            }
        )
        db.save_live_sim_position(
            {
                "position_id": "LIVE_SIM:12******90:005930:ci-1",
                "candidate_instance_id": "ci-1",
                "code": "005930",
                "account_id_masked": "12******90",
                "opened_at": "2026-05-30T09:00:00",
                "entry_qty": 7,
                "entry_avg_price": 70000,
                "current_qty": 7,
                "stop_loss_price": 68600,
                "take_profit_price": 73500,
                "max_hold_exit_at": "2026-05-30T10:00:00",
                "status": "OPEN",
                "updated_at": "2026-05-30T09:00:00",
            }
        )
        report = LiveSimLifecycleAuditor(db).build_report(trade_date="2026-05-30")
    finally:
        db.close()

    assert report["status"] == "RECONCILE_REQUIRED"
    assert report["summary"]["position_qty_mismatch_count"] == 1
    assert report["position_issues"][0]["issue_type"] == "POSITION_QTY_MISMATCH"


def test_live_sim_audit_api_and_snapshot_include_summary(tmp_path, monkeypatch):
    db_path = Path(tmp_path) / "runtime.sqlite3"
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setenv("TRADING_CORE_TOKEN", "test-token")
    monkeypatch.setenv("TRADING_MODE", "OBSERVE")
    db = TradingDatabase(str(db_path))
    try:
        db.save_live_sim_order(
            {
                "order_intent_id": "api-live-sim-order",
                "trade_date": datetime.now().date().isoformat(),
                "code": "005930",
                "account_id_masked": "12******90",
                "candidate_instance_id": "ci-api",
                "side": "buy",
                "order_status": "UNKNOWN_SUBMIT",
                "requested_qty": 1,
                "submitted_qty": 1,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    finally:
        db.close()

    import trading_app.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        audit = client.get(f"/api/runtime/live-sim/audit?trade_date={datetime.now().date().isoformat()}").json()
        snapshot = client.get("/api/snapshot?refresh=true").json()

    assert audit["summary"]["unknown_submit_count"] == 1
    assert "live_sim_audit" in snapshot
    assert snapshot["live_sim_audit"]["summary"]["unknown_submit_count"] == 1
    assert snapshot["runtime"]["live_sim_audit"]["summary"]["unknown_submit_count"] == 1
