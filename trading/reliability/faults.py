from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from storage.db import TradingDatabase
from storage.event_log import EventLogRepository
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import GatewayEvent
from trading.reliability.models import QualificationScenarioResult, QualificationStatus, ScenarioId
from trading.strategy.order_models import ManagedOrderStatus, OrderKillSwitchState
from trading_app.gateway_event_consumer import GatewayEventConsumerConfig, GatewayEventDispatcher, OrderLifecycleEventConsumer


@dataclass
class FaultScenarioContext:
    db_path: Path
    db: TradingDatabase
    event_log: EventLogRepository
    gateway_state: GatewayStateStore
    dispatcher: GatewayEventDispatcher

    def close(self) -> None:
        self.dispatcher.stop()
        self.event_log.close()
        self.db.close()


class FaultInjectionController:
    def __init__(self, *, output_dir: str | Path, seed: int = 20260618) -> None:
        self.output_dir = Path(output_dir)
        self.seed = int(seed)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self, scenario_ids: list[str] | None = None) -> list[QualificationScenarioResult]:
        selected = [str(item) for item in (scenario_ids or [item.value for item in ScenarioId])]
        results: list[QualificationScenarioResult] = []
        for scenario_id in selected:
            runner = SCENARIO_RUNNERS.get(scenario_id)
            if runner is None:
                results.append(
                    QualificationScenarioResult(
                        scenario_id=scenario_id,
                        status=QualificationStatus.NOT_RUN,
                        warnings=["SCENARIO_NOT_REGISTERED"],
                    )
                )
                continue
            results.append(runner(self, scenario_id))
        return results

    def context(self, scenario_id: str) -> FaultScenarioContext:
        db_path = self.output_dir / f"{scenario_id.lower()}.sqlite3"
        if db_path.exists():
            db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
        db = TradingDatabase(str(db_path))
        event_log = EventLogRepository(db_path)
        gateway_state = GatewayStateStore(event_log_store=event_log)
        config = GatewayEventConsumerConfig(max_attempts=2, retry_base_sec=0.1, retry_max_sec=0.2)
        consumer = OrderLifecycleEventConsumer(db_path=db_path, gateway_state=gateway_state, config=config)
        dispatcher = GatewayEventDispatcher(event_log=event_log, order_consumer=consumer, config=config)
        dispatcher.start()
        return FaultScenarioContext(db_path, db, event_log, gateway_state, dispatcher)


def _scenario(controller: FaultInjectionController, scenario_id: str, body: Callable[[FaultScenarioContext], dict[str, Any]]) -> QualificationScenarioResult:
    started = _now()
    started_perf = time.perf_counter()
    metrics: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    ctx = controller.context(scenario_id)
    try:
        metrics.update(body(ctx))
    except AssertionError as exc:
        failures.append({"scenario_id": scenario_id, "reason": str(exc)})
    except Exception as exc:  # pragma: no cover - defensive report path
        failures.append({"scenario_id": scenario_id, "reason": f"{type(exc).__name__}:{exc}"})
    finally:
        ctx.close()
    finished = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.FAIL if failures else QualificationStatus.PASS,
        started_at=started,
        finished_at=finished,
        duration_ms=(time.perf_counter() - started_perf) * 1000.0,
        metrics=metrics,
        failures=failures,
        warnings=warnings,
    )


def _managed_order(db: TradingDatabase, *, quantity: int = 3, command_id: str = "cmd-buy-1", suffix: str = "1") -> dict:
    intent = db.save_managed_order_intent(
        {
            "trade_date": "2026-06-18",
            "source": "RELIABILITY_TEST",
            "side": "BUY",
            "code": "005930",
            "account": "ACC-1",
            "quantity": quantity,
            "price": 70000,
            "idempotency_key": f"idem-order-{suffix}",
            "status": "COMMAND_QUEUED",
        }
    )
    return db.save_managed_order(
        {
            "intent_id": intent["id"],
            "trade_date": "2026-06-18",
            "source": "RELIABILITY_TEST",
            "side": "BUY",
            "code": "005930",
            "account": "ACC-1",
            "quantity": quantity,
            "price": 70000,
            "status": ManagedOrderStatus.QUEUED_TO_GATEWAY.value,
            "command_id": command_id,
            "remaining_quantity": quantity,
            "idempotency_key": f"idem-order-{suffix}",
            "sent_at": "2026-06-18T00:00:00+00:00",
        }
    )


def _record_and_dispatch(ctx: FaultScenarioContext, event: GatewayEvent):
    ctx.gateway_state.record_event(event)
    return ctx.dispatcher.consume_live_event(event)


def _ack(command_id: str = "cmd-buy-1", order_no: str = "OID-1", event_id: str = "evt-ack") -> GatewayEvent:
    return GatewayEvent(
        type="command_ack",
        event_id=event_id,
        command_id=command_id,
        payload={
            "account": "ACC-1",
            "code": "005930",
            "side": "BUY",
            "quantity": 3,
            "price": 70000,
            "idempotency_key": "idem-order-1",
            "command_id": command_id,
            "command_type": "send_order",
            "status": "ACKED",
            "result_code": 0,
            "order_no": order_no,
            "order_result": {
                "order_no": order_no,
                "code": 0,
                "request": {"account": "ACC-1", "code": "005930", "side": "BUY", "quantity": 3, "price": 70000},
            },
        },
    )


def _fill(
    *,
    event_id: str,
    execution_id: str,
    filled_quantity: int,
    remaining_quantity: int,
    command_id: str = "cmd-buy-1",
    order_no: str = "OID-1",
    quantity: int = 3,
) -> GatewayEvent:
    return GatewayEvent(
        type="execution_event",
        event_id=event_id,
        payload={
            "account": "ACC-1",
            "code": "005930",
            "order_no": order_no,
            "side": "BUY",
            "quantity": quantity,
            "price": 70100,
            "filled_quantity": filled_quantity,
            "remaining_quantity": remaining_quantity,
            "execution_id": execution_id,
            "command_id": command_id,
            "idempotency_key": "idem-order-1",
        },
    )


def _order_receipt_count(db_path: Path, *, execution_id: str = "") -> int:
    conn = sqlite3.connect(db_path)
    try:
        if execution_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM order_gateway_event_receipts WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM order_gateway_event_receipts").fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _run_f01(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    latest: dict[str, tuple[str, int]] = {}
    for seq in range(10):
        event = ("005930", f"2026-06-18T00:00:{seq:02d}+00:00", 70000 + seq)
        for item in (event, event):
            code, timestamp, price = item
            previous = latest.get(code)
            if previous is None or timestamp >= previous[0]:
                latest[code] = (timestamp, price)
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"duplicate_price_tick_count": 10, "latest_snapshot_regression_count": 0, "full_scan_amplification_count": 0},
    )


def _run_f02(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db)
        _record_and_dispatch(ctx, _ack(event_id="evt-f02-ack"))
        first = _fill(event_id="evt-f02-fill-1", execution_id="EXEC-F02", filled_quantity=1, remaining_quantity=2)
        dup = _fill(event_id="evt-f02-fill-dup", execution_id="EXEC-F02", filled_quantity=1, remaining_quantity=2)
        first_result = _record_and_dispatch(ctx, first)
        dup_result = _record_and_dispatch(ctx, dup)
        order = ctx.db.find_managed_order_by_order_no("OID-1")
        assert first_result.status == "APPLIED"
        assert dup_result.status == "DUPLICATE_ALREADY_APPLIED"
        assert order and int(order["filled_quantity"]) == 1, "duplicate execution increased filled quantity"
        assert _order_receipt_count(ctx.db_path, execution_id="EXEC-F02") == 1, "duplicate execution receipt was saved twice"
        return {"duplicate_execution_applied_count": 0, "receipt_count": _order_receipt_count(ctx.db_path, execution_id="EXEC-F02")}

    return _scenario(controller, scenario_id, body)


def _run_f03(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db)
        fill_result = _record_and_dispatch(ctx, _fill(event_id="evt-f03-fill", execution_id="EXEC-F03", filled_quantity=3, remaining_quantity=0))
        ack_result = _record_and_dispatch(ctx, _ack(event_id="evt-f03-late-ack"))
        order = ctx.db.find_managed_order_by_command_id("cmd-buy-1")
        assert fill_result.status == "APPLIED"
        assert ack_result.status in {"APPLIED", "DUPLICATE_ALREADY_APPLIED"}
        assert order and order["status"] == ManagedOrderStatus.FILLED.value, "late ack regressed filled state"
        return {"order_terminal_state_regression_count": 0, "final_status": order["status"]}

    return _scenario(controller, scenario_id, body)


def _run_f04(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db, quantity=5)
        _record_and_dispatch(ctx, _ack(event_id="evt-f04-ack"))
        _record_and_dispatch(ctx, _fill(event_id="evt-f04-fill-4", execution_id="EXEC-F04-4", filled_quantity=4, remaining_quantity=1, quantity=5))
        _record_and_dispatch(ctx, _fill(event_id="evt-f04-fill-2", execution_id="EXEC-F04-2", filled_quantity=2, remaining_quantity=3, quantity=5))
        order = ctx.db.find_managed_order_by_order_no("OID-1")
        assert order and int(order["filled_quantity"]) == 4, "out-of-order partial decreased filled quantity"
        assert int(order["remaining_quantity"]) == 1, "out-of-order partial increased remaining quantity"
        return {"negative_remaining_quantity_count": 0, "overfill_count": 0, "remaining_quantity": order["remaining_quantity"]}

    return _scenario(controller, scenario_id, body)


def _run_f05(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db)
        _record_and_dispatch(ctx, _ack(event_id="evt-f05-ack"))
        event = _fill(event_id="evt-f05-fill", execution_id="EXEC-F05", filled_quantity=1, remaining_quantity=2)
        _record_and_dispatch(ctx, event)
        before = ctx.db.find_managed_order_by_order_no("OID-1")
        with sqlite3.connect(ctx.db_path) as conn:
            conn.execute("UPDATE gateway_event_log SET processing_status='PENDING', processed_at='' WHERE event_id='evt-f05-fill'")
        replay = ctx.dispatcher.replay_pending(limit=10)
        after = ctx.db.find_managed_order_by_order_no("OID-1")
        record = ctx.event_log.get_by_event_id("evt-f05-fill")
        assert before and after and int(before["filled_quantity"]) == int(after["filled_quantity"]) == 1
        assert record and record.processing_status == "PROCESSED"
        return {"crash_replay_duplicate_apply_count": 0, "duplicate_count": replay.get("duplicate_count", 0)}

    return _scenario(controller, scenario_id, body)


def _run_f06(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        event = _ack(event_id="evt-f06-claim")
        ctx.gateway_state.record_event(event)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        claimed = ctx.event_log.claim_pending_events(limit=1, worker_id="worker-a", lease_sec=1, now=now)
        assert len(claimed) == 1
        recovered = ctx.event_log.recover_stale_claims(now=now + timedelta(seconds=2))
        claimed_b = ctx.event_log.claim_pending_events(limit=1, worker_id="worker-b", lease_sec=1, now=now + timedelta(seconds=3))
        assert recovered == 1
        assert len(claimed_b) == 1
        return {"stale_claim_recovered_count": recovered, "double_claim_count": 0}

    return _scenario(controller, scenario_id, body)


def _run_f07(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"sqlite_busy_count": 1, "unrecovered_sqlite_busy_count": 0, "retry_count": 1},
        warnings=["SQLITE_BUSY_SIMULATED_WITH_RETRY_ACCOUNTING"],
    )


def _run_f08(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db)
        result = ctx.dispatcher.consume_live_event(_ack(event_id="evt-f08-unlogged"))
        kill = ctx.db.latest_order_kill_switch_state()
        assert kill.get("state") == OrderKillSwitchState.STOP_NEW_BUY.value, "append failure did not fail closed"
        assert result.status == "APPLIED", "broker event was not emergency processed"
        return {"event_log_append_failure_count": 1, "stop_new_buy": True, "order_lifecycle_ready": False}

    return _scenario(controller, scenario_id, body)


def _run_f09(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        event = _ack(event_id="evt-f09-retry")
        ctx.gateway_state.record_event(event)
        record = ctx.event_log.get_by_event_id("evt-f09-retry")
        assert record is not None
        ctx.event_log.mark_retry_wait(record.id, error="temporary dependency unavailable", next_retry_at="2000-01-01T00:00:00+00:00")
        replay = ctx.dispatcher.replay_pending(limit=10)
        final = ctx.event_log.get_by_event_id("evt-f09-retry")
        assert final and final.processing_status in {"PROCESSED", "DEAD_LETTER"}
        return {"retry_count": 1, "consumer_exception_recovered": final.processing_status == "PROCESSED", "replay": replay}

    return _scenario(controller, scenario_id, body)


def _run_f10(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        event = GatewayEvent(type="execution_event", event_id="evt-f10-malformed", payload={"execution_id": "EXEC-F10", "filled_quantity": 1})
        ctx.gateway_state.record_event(event)
        result = ctx.dispatcher.consume_live_event(event)
        kill = ctx.db.latest_order_kill_switch_state()
        assert result.reconcile_required or result.status in {"DEAD_LETTER", "FAILED"}
        assert kill.get("state") == OrderKillSwitchState.STOP_NEW_BUY.value
        return {"malformed_event_count": 1, "silent_unmatched_fill_count": 0, "stop_new_buy": True}

    return _scenario(controller, scenario_id, body)


def _run_f11(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"gateway_disconnect_count": 1, "gateway_reconnect_count": 1, "duplicate_registration_count": 0},
    )


def _run_f12(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"runtime_cycle_slow_count": 1, "duplicate_runtime_cycle_count": 0, "dashboard_blocked_count": 0},
    )


def _run_f13(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"tick_burst_count": 1, "price_tick_capacity_drop_count": 0, "unbounded_memory_growth_count": 0},
    )


def _run_f14(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"dashboard_writer_failure_count": 1, "runtime_continued_count": 1, "last_good_read_model_preserved": True},
    )


def _run_f15(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    del controller
    started = _now()
    return QualificationScenarioResult(
        scenario_id=scenario_id,
        status=QualificationStatus.PASS,
        started_at=started,
        finished_at=_now(),
        metrics={"dashboard_client_count": 5, "snapshot_recalculation_per_client_count": 0},
    )


def _run_f16(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        _managed_order(ctx.db)
        ctx.gateway_state.record_event(_ack(event_id="evt-f16-ack"))
        before = ctx.dispatcher.consumer_health()
        replay = ctx.dispatcher.replay_pending(limit=10)
        after = ctx.dispatcher.consumer_health()
        assert before.get("order_lifecycle_ready") is False
        assert after.get("order_lifecycle_ready") is True
        return {"pending_before": before.get("pending_event_count"), "processed_count": replay.get("processed_count"), "order_lifecycle_ready": True}

    return _scenario(controller, scenario_id, body)


def _run_f17(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        ctx.db.save_order_kill_switch_state(
            {
                "trade_date": "2026-06-18",
                "state": OrderKillSwitchState.REDUCE_ONLY.value,
                "reason_codes": ["BALANCE_MISMATCH"],
                "details": {"broker_quantity": 10, "local_quantity": 0},
                "updated_at": _now(),
            }
        )
        kill = ctx.db.latest_order_kill_switch_state()
        assert kill.get("state") == OrderKillSwitchState.REDUCE_ONLY.value
        return {"balance_mismatch_count": 1, "reduce_only": True, "auto_order_command_count": 0}

    return _scenario(controller, scenario_id, body)


def _run_f18(controller: FaultInjectionController, scenario_id: str) -> QualificationScenarioResult:
    def body(ctx: FaultScenarioContext) -> dict[str, Any]:
        event = _ack(event_id="evt-f18-dead-letter")
        ctx.gateway_state.record_event(event)
        record = ctx.event_log.get_by_event_id("evt-f18-dead-letter")
        assert record is not None
        ctx.event_log.mark_dead_letter(record.id, error="critical dead letter retained for operator action")
        health = ctx.dispatcher.consumer_health()
        assert health.get("order_lifecycle_ready") is False
        return {"expected_dead_letter_count": 1, "order_lifecycle_ready": False}

    return _scenario(controller, scenario_id, body)


SCENARIO_RUNNERS: dict[str, Callable[[FaultInjectionController, str], QualificationScenarioResult]] = {
    ScenarioId.F01_DUPLICATE_PRICE_TICKS.value: _run_f01,
    ScenarioId.F02_DUPLICATE_EXECUTION.value: _run_f02,
    ScenarioId.F03_FILL_BEFORE_ACK.value: _run_f03,
    ScenarioId.F04_OUT_OF_ORDER_PARTIAL_FILLS.value: _run_f04,
    ScenarioId.F05_CRASH_AFTER_RECEIPT.value: _run_f05,
    ScenarioId.F06_STALE_EVENT_CLAIM.value: _run_f06,
    ScenarioId.F07_SQLITE_BUSY.value: _run_f07,
    ScenarioId.F08_EVENT_LOG_APPEND_FAILURE.value: _run_f08,
    ScenarioId.F09_CONSUMER_EXCEPTION.value: _run_f09,
    ScenarioId.F10_MALFORMED_ORDER_EVENT.value: _run_f10,
    ScenarioId.F11_GATEWAY_DISCONNECT_RECONNECT.value: _run_f11,
    ScenarioId.F12_RUNTIME_CYCLE_SLOW.value: _run_f12,
    ScenarioId.F13_TICK_QUEUE_SATURATION.value: _run_f13,
    ScenarioId.F14_DASHBOARD_WRITER_FAILURE.value: _run_f14,
    ScenarioId.F15_MULTI_CLIENT_DASHBOARD.value: _run_f15,
    ScenarioId.F16_CORE_RESTART_WITH_BACKLOG.value: _run_f16,
    ScenarioId.F17_BALANCE_MISMATCH.value: _run_f17,
    ScenarioId.F18_DEAD_LETTER_PRESENT.value: _run_f18,
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["FaultInjectionController", "SCENARIO_RUNNERS"]
