from __future__ import annotations

from pathlib import Path

from storage.db import TradingDatabase
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import BrokerExecutionEvent, BrokerOrderRequest, BrokerOrderResult, GatewayEvent, utc_timestamp
from trading.strategy.runtime_settings import StrategyRuntimeSettings
from trading_app.dependencies import CoreSettings
from trading_app.order_enqueue_service import OrderEnqueueService, RuntimeOrderIntentRequest
from trading_app.runtime_factory import _build_order_sink
from trading_app.runtime_order_sink import DryRunRuntimeOrderSink, LiveSimRuntimeOrderSink, NoopRuntimeOrderSink


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


def _state(*, account="1234567890", broker_env="SIMULATION") -> GatewayStateStore:
    state = GatewayStateStore()
    state.record_event(
        GatewayEvent(
            type="heartbeat",
            timestamp=utc_timestamp(),
            payload={
                "kiwoom_logged_in": True,
                "orderable": True,
                "account": account,
                "broker_env": broker_env,
                "server_mode": broker_env,
                "mode": broker_env,
            },
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


def _service(tmp_path: Path, *, state: GatewayStateStore | None = None):
    settings = _settings(tmp_path)
    gateway_state = state or _state()
    return OrderEnqueueService(settings=settings, gateway_state=gateway_state, db_path=settings.db_path), settings, gateway_state


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
    assert unknown.reason in {"ACCOUNT_GUARD_FAILED_SERVER_MODE_UNKNOWN", "ACCOUNT_GUARD_FAILED_UNKNOWN_ACCOUNT_MODE"}
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
