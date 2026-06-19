from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from trading.broker.kiwoom_reconcile_tr import ReconcileTrParser, build_reconcile_tr_command
from trading.broker.models import new_message_id
from trading.broker.reconcile_tr_models import (
    BrokerReconcileDiscrepancy,
    ReconcileDiscrepancySeverity,
    ReconcileRunStatus,
    ReconcileSourceStatus,
    ReconcileSourceType,
    ReconcileTrParseResult,
    account_token,
)


CRITICAL_SEVERITIES = {
    ReconcileDiscrepancySeverity.STOP_NEW_BUY.value,
    ReconcileDiscrepancySeverity.REDUCE_ONLY.value,
    ReconcileDiscrepancySeverity.KILL_SWITCH_RECOMMENDED.value,
}


@dataclass(frozen=True)
class BrokerReconcileConfig:
    service_enabled: bool = True
    dispatch_enabled: bool = False
    simulation_only: bool = True
    startup_enabled: bool = False
    reconnect_enabled: bool = False
    periodic_enabled: bool = False
    periodic_interval_sec: int = 60
    event_trigger_enabled: bool = True
    event_trigger_debounce_sec: float = 2.0
    timeout_sec: int = 30
    max_pages: int = 20
    max_attempts: int = 2
    snapshot_stale_sec: int = 120
    grace_sec: int = 5
    cash_required: bool = False
    fail_closed: bool = True
    auto_heal_enabled: bool = False
    auto_clear_stop_new_buy: bool = False
    actual_fixture_required_for_pass: bool = True

    @classmethod
    def from_env(cls) -> "BrokerReconcileConfig":
        return cls(
            service_enabled=_env_bool("TRADING_RECONCILE_TR_SERVICE_ENABLED", True),
            dispatch_enabled=_env_bool("TRADING_RECONCILE_TR_DISPATCH_ENABLED", False),
            simulation_only=_env_bool("TRADING_RECONCILE_TR_SIMULATION_ONLY", True),
            startup_enabled=_env_bool("TRADING_RECONCILE_TR_STARTUP_ENABLED", False),
            reconnect_enabled=_env_bool("TRADING_RECONCILE_TR_RECONNECT_ENABLED", False),
            periodic_enabled=_env_bool("TRADING_RECONCILE_TR_PERIODIC_ENABLED", False),
            periodic_interval_sec=max(1, _env_int("TRADING_RECONCILE_TR_PERIODIC_INTERVAL_SEC", 60)),
            event_trigger_enabled=_env_bool("TRADING_RECONCILE_TR_EVENT_TRIGGER_ENABLED", True),
            event_trigger_debounce_sec=max(0.1, _env_float("TRADING_RECONCILE_TR_EVENT_TRIGGER_DEBOUNCE_SEC", 2.0)),
            timeout_sec=max(1, _env_int("TRADING_RECONCILE_TR_TIMEOUT_SEC", 30)),
            max_pages=max(1, _env_int("TRADING_RECONCILE_TR_MAX_PAGES", 20)),
            max_attempts=max(1, _env_int("TRADING_RECONCILE_TR_MAX_ATTEMPTS", 2)),
            snapshot_stale_sec=max(1, _env_int("TRADING_RECONCILE_TR_SNAPSHOT_STALE_SEC", 120)),
            grace_sec=max(0, _env_int("TRADING_RECONCILE_TR_GRACE_SEC", 5)),
            cash_required=_env_bool("TRADING_RECONCILE_TR_CASH_REQUIRED", False),
            fail_closed=_env_bool("TRADING_RECONCILE_TR_FAIL_CLOSED", True),
            auto_heal_enabled=_env_bool("TRADING_RECONCILE_TR_AUTO_HEAL_ENABLED", False),
            auto_clear_stop_new_buy=_env_bool("TRADING_RECONCILE_TR_AUTO_CLEAR_STOP_NEW_BUY", False),
            actual_fixture_required_for_pass=_env_bool("TRADING_RECONCILE_TR_ACTUAL_FIXTURE_REQUIRED_FOR_PASS", True),
        )


@dataclass(frozen=True)
class BrokerTruthSnapshot:
    run_id: str
    account_token: str
    snapshot_complete: bool
    broker_truth_ready: bool
    reconcile_clean: bool
    open_orders: tuple[dict[str, Any], ...] = ()
    positions: tuple[dict[str, Any], ...] = ()
    cash: dict[str, Any] | None = None
    discrepancies: tuple[dict[str, Any], ...] = ()
    snapshot_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "account_token": self.account_token,
            "snapshot_complete": self.snapshot_complete,
            "broker_truth_ready": self.broker_truth_ready,
            "reconcile_clean": self.reconcile_clean,
            "open_orders": list(self.open_orders),
            "positions": list(self.positions),
            "cash": dict(self.cash or {}),
            "discrepancies": list(self.discrepancies),
            "snapshot_at": self.snapshot_at,
        }


class BrokerReconcileComparator:
    def __init__(self, db: Any, *, config: BrokerReconcileConfig | None = None) -> None:
        self.db = db
        self.config = config or BrokerReconcileConfig.from_env()

    def compare_snapshots(self, *, run_id: str, account_token_value: str) -> list[BrokerReconcileDiscrepancy]:
        tr_orders = {str(row.get("order_no") or ""): row for row in self._tr_open_orders(run_id) if row.get("order_no")}
        tr_positions = {str(row.get("code") or ""): row for row in self._tr_positions(run_id) if row.get("code")}
        discrepancies: list[BrokerReconcileDiscrepancy] = []
        for broker_order in self._broker_orders():
            order_no = str(broker_order.get("order_no") or "")
            if not order_no:
                continue
            if order_no not in tr_orders and int(broker_order.get("remaining_qty") or 0) > 0:
                discrepancies.append(
                    _discrepancy(
                        "LOCAL_ONLY_PENDING_ORDER",
                        ReconcileDiscrepancySeverity.STOP_NEW_BUY,
                        "order",
                        order_no,
                        account_token_value,
                        local=broker_order,
                        reason_codes=("ORDER_NOT_PRESENT_IN_TR_OPEN_ORDERS",),
                        order_no=order_no,
                        code=str(broker_order.get("code") or ""),
                    )
                )
        for order_no, tr_order in tr_orders.items():
            broker_order = self._broker_order_by_order_no(order_no)
            if broker_order is None:
                discrepancies.append(
                    _discrepancy(
                        "BROKER_ONLY_OPEN_ORDER",
                        ReconcileDiscrepancySeverity.STOP_NEW_BUY,
                        "order",
                        order_no,
                        account_token_value,
                        tr_snapshot=tr_order,
                        reason_codes=("TR_OPEN_ORDER_NOT_LINKED_TO_LOCAL_PROJECTION",),
                        order_no=order_no,
                        code=str(tr_order.get("code") or ""),
                    )
                )
                continue
            if int(broker_order.get("remaining_qty") or 0) != int(tr_order.get("remaining_quantity") or 0):
                discrepancies.append(
                    _discrepancy(
                        "REMAINING_QUANTITY_MISMATCH",
                        ReconcileDiscrepancySeverity.STOP_NEW_BUY,
                        "order",
                        order_no,
                        account_token_value,
                        local=broker_order,
                        tr_snapshot=tr_order,
                        reason_codes=("BROKER_ORDER_REMAINING_DIFFERS_FROM_TR",),
                        order_no=order_no,
                        code=str(tr_order.get("code") or broker_order.get("code") or ""),
                    )
                )
        for code, tr_position in tr_positions.items():
            broker_position = self._broker_position_by_code(code)
            if broker_position is None and int(tr_position.get("quantity") or 0) > 0:
                discrepancies.append(
                    _discrepancy(
                        "BROKER_ONLY_POSITION",
                        ReconcileDiscrepancySeverity.REDUCE_ONLY,
                        "position",
                        code,
                        account_token_value,
                        tr_snapshot=tr_position,
                        reason_codes=("TR_POSITION_NOT_LINKED_TO_LOCAL_PROJECTION",),
                        code=code,
                    )
                )
                continue
            if broker_position and int(broker_position.get("quantity") or 0) != int(tr_position.get("quantity") or 0):
                discrepancies.append(
                    _discrepancy(
                        "POSITION_QUANTITY_MISMATCH",
                        ReconcileDiscrepancySeverity.REDUCE_ONLY,
                        "position",
                        code,
                        account_token_value,
                        local=broker_position,
                        tr_snapshot=tr_position,
                        reason_codes=("BROKER_POSITION_QUANTITY_DIFFERS_FROM_TR",),
                        code=code,
                    )
                )
        return discrepancies

    def _tr_open_orders(self, run_id: str) -> list[dict[str, Any]]:
        loader = getattr(self.db, "list_broker_reconcile_open_orders", None)
        return list(loader(run_id) if callable(loader) else [])

    def _tr_positions(self, run_id: str) -> list[dict[str, Any]]:
        loader = getattr(self.db, "list_broker_reconcile_positions", None)
        return list(loader(run_id) if callable(loader) else [])

    def _broker_orders(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM broker_order_state ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _broker_order_by_order_no(self, order_no: str) -> dict[str, Any] | None:
        row = self.db.conn.execute("SELECT * FROM broker_order_state WHERE order_no = ? ORDER BY id DESC LIMIT 1", (str(order_no),)).fetchone()
        return dict(row) if row else None

    def _broker_position_by_code(self, code: str) -> dict[str, Any] | None:
        row = self.db.conn.execute("SELECT * FROM broker_position_state WHERE code = ? ORDER BY id DESC LIMIT 1", (str(code),)).fetchone()
        return dict(row) if row else None


class BrokerReconcileOrchestrator:
    def __init__(
        self,
        *,
        db: Any,
        gateway_state: Any = None,
        config: BrokerReconcileConfig | None = None,
        parser: ReconcileTrParser | None = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.config = config or BrokerReconcileConfig.from_env()
        self.parser = parser or ReconcileTrParser()
        self.current_run_id = ""
        self.last_error = ""
        self.last_success_at = ""
        self.last_failure_at = ""
        self.last_trigger = ""
        self._lock = RLock()

    def request_manual_reconcile(self, *, account: str, broker_env: str = "SIMULATION", sources: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            return self._start_run(account=account, broker_env=broker_env, trigger="MANUAL_PILOT", sources=sources)

    def request_startup_reconcile(self, *, account: str, broker_env: str = "SIMULATION") -> dict[str, Any]:
        with self._lock:
            if not self.config.startup_enabled:
                return {"status": ReconcileRunStatus.DISABLED.value, "reason": "STARTUP_RECONCILE_DISABLED"}
            return self._start_run(account=account, broker_env=broker_env, trigger="STARTUP")

    def request_reconnect_reconcile(self, *, account: str, broker_env: str = "SIMULATION") -> dict[str, Any]:
        with self._lock:
            if not self.config.reconnect_enabled:
                return {"status": ReconcileRunStatus.DISABLED.value, "reason": "RECONNECT_RECONCILE_DISABLED"}
            return self._start_run(account=account, broker_env=broker_env, trigger="RECONNECT")

    def request_periodic_reconcile(self, *, account: str, broker_env: str = "SIMULATION") -> dict[str, Any]:
        with self._lock:
            if not self.config.periodic_enabled:
                return {"status": ReconcileRunStatus.DISABLED.value, "reason": "PERIODIC_RECONCILE_DISABLED"}
            return self._start_run(account=account, broker_env=broker_env, trigger="PERIODIC")

    def handle_gateway_event(self, event: Any) -> dict[str, Any]:
        with self._lock:
            payload = _broker_reconcile_payload(event)
            if str(payload.get("purpose") or "") != "broker_reconcile":
                return {"status": "IGNORED", "reason": "NOT_BROKER_RECONCILE"}
            try:
                result = self.parser.parse_command_ack(payload)
                self.apply_source_result(result, command_id=str(getattr(event, "command_id", "") or payload.get("command_id") or ""))
                return {"status": "PROCESSED", "run_id": result.run_id, "logical_source": result.logical_source}
            except Exception as exc:
                self.last_error = str(exc)
                self.last_failure_at = _now()
                return {"status": "FAILED", "error": str(exc)}

    def apply_source_result(self, result: ReconcileTrParseResult, *, command_id: str = "") -> dict[str, Any]:
        with self._lock:
            parsed = result.to_dict()
            self.db.save_broker_reconcile_source_result(
                {
                    "run_id": result.run_id,
                    "logical_source": result.logical_source,
                    "tr_code": result.tr_code,
                    "rq_name": result.rq_name,
                    "command_id": command_id,
                    "status": ReconcileSourceStatus.VALID_EMPTY.value if result.valid_empty else ReconcileSourceStatus.PARSED.value if result.complete else ReconcileSourceStatus.INVALID.value,
                    "complete": result.complete,
                    "page_count": result.page_count,
                    "row_count": result.row_count,
                    "parser_version": result.parser_version,
                    "parser_status": parsed.get("parser_status"),
                    "parser_warnings": parsed.get("warnings"),
                    "parser_errors": parsed.get("errors"),
                    "raw_checksum": result.raw_checksum,
                    "details": parsed,
                }
            )
            if result.logical_source == ReconcileSourceType.OPEN_ORDERS.value:
                self.db.replace_broker_reconcile_snapshots(run_id=result.run_id, open_orders=[item.to_dict() for item in result.open_orders])
            elif result.logical_source == ReconcileSourceType.ACCOUNT_POSITIONS.value:
                self.db.replace_broker_reconcile_snapshots(run_id=result.run_id, positions=[item.to_dict() for item in result.positions])
            elif result.logical_source == ReconcileSourceType.ACCOUNT_CASH.value and result.cash:
                self.db.replace_broker_reconcile_snapshots(run_id=result.run_id, cash=result.cash.to_dict())
            self._update_run_after_source_result(result)
            return parsed

    def finalize_run(self, run_id: str) -> BrokerTruthSnapshot:
        with self._lock:
            run = self.db.get_broker_reconcile_run(run_id) or {}
            token = str(run.get("account_token") or "")
            discrepancies = BrokerReconcileComparator(self.db, config=self.config).compare_snapshots(run_id=run_id, account_token_value=token)
            discrepancy_dicts = [item.to_dict() for item in discrepancies]
            self.db.save_broker_reconcile_discrepancies(run_id, discrepancy_dicts)
            critical_count = sum(1 for item in discrepancy_dicts if str(item.get("severity") or "") in CRITICAL_SEVERITIES)
            clean = not discrepancy_dicts
            status = ReconcileRunStatus.CLEAN.value if clean else ReconcileRunStatus.RECONCILE_REQUIRED.value
            self.db.save_broker_reconcile_run(
                {
                    **run,
                    "run_id": run_id,
                    "status": status,
                    "snapshot_complete": True,
                    "broker_truth_ready": clean,
                    "reconcile_clean": clean,
                    "discrepancy_count": len(discrepancy_dicts),
                    "critical_discrepancy_count": critical_count,
                    "completed_at": _now(),
                }
            )
            if clean:
                self.last_success_at = _now()
            else:
                self.last_failure_at = _now()
            return BrokerTruthSnapshot(
                run_id=run_id,
                account_token=token,
                snapshot_complete=True,
                broker_truth_ready=clean,
                reconcile_clean=clean,
                open_orders=tuple(self.db.list_broker_reconcile_open_orders(run_id)),
                positions=tuple(self.db.list_broker_reconcile_positions(run_id)),
                discrepancies=tuple(discrepancy_dicts),
            )

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            run = self.db.get_broker_reconcile_run(self.current_run_id) if self.current_run_id else None
            discrepancies = self.db.list_broker_reconcile_discrepancies(run_id=self.current_run_id, limit=100) if self.current_run_id else []
            critical = [item for item in discrepancies if str(item.get("severity") or "") in CRITICAL_SEVERITIES]
            return {
                "enabled": self.config.service_enabled,
                "dispatch_enabled": self.config.dispatch_enabled,
                "status": str((run or {}).get("status") or ("DISABLED" if not self.config.service_enabled else "IDLE")),
                "current_run_id": self.current_run_id,
                "trigger": self.last_trigger,
                "account_masked": str((run or {}).get("account_token") or ""),
                "required_sources": list((run or {}).get("required_sources") or []),
                "completed_sources": list((run or {}).get("completed_sources") or []),
                "snapshot_complete": bool((run or {}).get("snapshot_complete")),
                "broker_truth_ready": bool((run or {}).get("broker_truth_ready")),
                "reconcile_clean": bool((run or {}).get("reconcile_clean")),
                "discrepancy_count": len(discrepancies),
                "critical_discrepancy_count": len(critical),
                "stop_new_buy": any(str(item.get("severity") or "") == ReconcileDiscrepancySeverity.STOP_NEW_BUY.value for item in discrepancies),
                "reduce_only": any(str(item.get("severity") or "") == ReconcileDiscrepancySeverity.REDUCE_ONLY.value for item in discrepancies),
                "last_success_at": self.last_success_at,
                "last_failure_at": self.last_failure_at,
                "last_error": self.last_error,
                "warnings": list((run or {}).get("warnings") or []),
            }

    def latest_complete_snapshot(self, account_token_value: str) -> dict[str, Any]:
        with self._lock:
            rows = [
                row for row in self.db.list_broker_reconcile_runs(limit=100)
                if row.get("account_token") == account_token_value and row.get("snapshot_complete")
            ]
            return rows[0] if rows else {}

    def mark_stale(self) -> None:
        with self._lock:
            if not self.current_run_id:
                return
            run = self.db.get_broker_reconcile_run(self.current_run_id)
            if run:
                self.db.save_broker_reconcile_run({**run, "status": ReconcileRunStatus.STALE.value, "broker_truth_ready": False})

    def cancel_pending_run(self, reason: str) -> None:
        with self._lock:
            if not self.current_run_id:
                return
            run = self.db.get_broker_reconcile_run(self.current_run_id)
            if run:
                self.db.save_broker_reconcile_run({**run, "status": ReconcileRunStatus.CANCELLED.value, "errors": [str(reason or "")]})

    def _start_run(self, *, account: str, broker_env: str, trigger: str, sources: list[str] | None = None) -> dict[str, Any]:
        if not self.config.service_enabled:
            return {"status": ReconcileRunStatus.DISABLED.value}
        if self.config.simulation_only and str(broker_env or "").upper() == "REAL":
            return {"status": ReconcileRunStatus.FAILED.value, "reason": "REAL_BROKER_RECONCILE_FORBIDDEN"}
        token = account_token(account)
        if not token:
            return {"status": ReconcileRunStatus.WAIT_ACCOUNT.value, "reason": "ACCOUNT_REQUIRED"}
        run_id = new_message_id("reconcile")
        required = list(sources or [ReconcileSourceType.OPEN_ORDERS.value, ReconcileSourceType.ACCOUNT_POSITIONS.value])
        if self.config.cash_required and ReconcileSourceType.ACCOUNT_CASH.value not in required:
            required.append(ReconcileSourceType.ACCOUNT_CASH.value)
        status = ReconcileRunStatus.QUEUED.value if self.config.dispatch_enabled else ReconcileRunStatus.WAIT_GATEWAY.value
        self.current_run_id = run_id
        self.last_trigger = trigger
        saved = self.db.save_broker_reconcile_run(
            {
                "run_id": run_id,
                "account_token": token,
                "broker_env": str(broker_env or ""),
                "trigger": trigger,
                "status": status,
                "required_sources": required,
                "completed_sources": [],
                "snapshot_complete": False,
                "broker_truth_ready": False,
                "reconcile_clean": False,
                "warnings": [] if self.config.dispatch_enabled else ["RECONCILE_TR_DISPATCH_DISABLED"],
            }
        )
        if self.config.dispatch_enabled:
            dispatch_summary = self._dispatch_reconcile_commands(account=account, run_id=run_id, sources=required)
            warnings = list(saved.get("warnings") or [])
            errors = list(saved.get("errors") or [])
            warnings.extend(dispatch_summary.get("warnings") or [])
            errors.extend(dispatch_summary.get("errors") or [])
            saved = self.db.save_broker_reconcile_run(
                {
                    **saved,
                    "status": ReconcileRunStatus.RUNNING.value
                    if dispatch_summary.get("enqueued_count") == len(required) and not errors
                    else ReconcileRunStatus.FAILED.value,
                    "warnings": warnings,
                    "errors": errors,
                }
            )
        return saved

    def _dispatch_reconcile_commands(self, *, account: str, run_id: str, sources: list[str]) -> dict[str, Any]:
        if self.gateway_state is None:
            return {"enqueued_count": 0, "warnings": [], "errors": ["GATEWAY_STATE_UNAVAILABLE"]}
        enqueued = 0
        warnings: list[str] = []
        errors: list[str] = []
        for source in sources:
            try:
                command = build_reconcile_tr_command(
                    account=account,
                    logical_source=source,
                    run_id=run_id,
                    max_pages=self.config.max_pages,
                )
                result = self.gateway_state.enqueue_command(
                    command,
                    priority="NORMAL",
                    ttl_sec=self.config.timeout_sec,
                    max_attempts=self.config.max_attempts,
                    metadata={
                        "command_class": "BROKER_RECONCILE",
                        "reconcile_run_id": run_id,
                        "logical_source": source,
                    },
                )
                if getattr(result, "accepted", False):
                    enqueued += 1
                else:
                    errors.append(f"RECONCILE_TR_COMMAND_REJECTED:{source}:{getattr(result, 'reason', '')}")
            except Exception as exc:
                errors.append(f"RECONCILE_TR_COMMAND_ENQUEUE_FAILED:{source}:{exc}")
        if enqueued and enqueued < len(sources):
            warnings.append("RECONCILE_TR_PARTIAL_DISPATCH")
        return {"enqueued_count": enqueued, "warnings": warnings, "errors": errors}

    def _update_run_after_source_result(self, result: ReconcileTrParseResult) -> None:
        run = self.db.get_broker_reconcile_run(result.run_id) or {}
        if not run:
            return
        required = [str(item) for item in list(run.get("required_sources") or [])]
        completed = [str(item) for item in list(run.get("completed_sources") or [])]
        errors = list(run.get("errors") or [])
        warnings = list(run.get("warnings") or [])
        for warning in result.warnings:
            text = str(warning or "")
            if text and text not in warnings:
                warnings.append(text)
        for error in result.errors:
            text = str(error or "")
            if text and text not in errors:
                errors.append(text)
        if result.complete and not result.errors and result.logical_source not in completed:
            completed.append(result.logical_source)
        all_required_complete = bool(required) and all(source in completed for source in required)
        self.db.save_broker_reconcile_run(
            {
                **run,
                "status": ReconcileRunStatus.COMPLETE.value if all_required_complete else ReconcileRunStatus.PARTIAL.value,
                "completed_sources": completed,
                "warnings": warnings,
                "errors": errors,
            }
        )
        if all_required_complete:
            self.finalize_run(result.run_id)


def _broker_reconcile_payload(event: Any) -> dict[str, Any]:
    payload = dict(getattr(event, "payload", {}) or {})
    if str(payload.get("purpose") or "") == "broker_reconcile":
        return payload
    nested = payload.get("payload")
    if isinstance(nested, dict) and str(nested.get("purpose") or "") == "broker_reconcile":
        return dict(nested)
    return payload


def _discrepancy(
    category: str,
    severity: ReconcileDiscrepancySeverity,
    entity_type: str,
    entity_key: str,
    account_token_value: str,
    *,
    local: dict[str, Any] | None = None,
    tr_snapshot: dict[str, Any] | None = None,
    reason_codes: tuple[str, ...] = (),
    order_no: str = "",
    code: str = "",
) -> BrokerReconcileDiscrepancy:
    return BrokerReconcileDiscrepancy(
        category=category,
        severity=severity,
        entity_type=entity_type,
        entity_key=entity_key,
        account_token=account_token_value,
        code=code,
        order_no=order_no,
        local=dict(local or {}),
        tr_snapshot=dict(tr_snapshot or {}),
        reason_codes=reason_codes,
        operator_action_required=True,
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return float(default)
