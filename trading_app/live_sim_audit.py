from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage.db import TradingDatabase
from trading.live_sim.lifecycle import (
    LIVE_SIM_ORDER_STATUSES,
    LIVE_SIM_TERMINAL_ORDER_STATUSES,
    transition_warning_codes,
    validate_live_sim_transition,
)


ACTIVE_ORDER_STATUSES = {"SUBMITTED", "UNKNOWN_SUBMIT", "ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "RECONCILE_REQUIRED"}
OPEN_POSITION_STATUSES = {"OPEN", "PARTIAL", "EXIT_ORDERED", "EXIT_SUBMITTING", "RECONCILE_REQUIRED"}
ISSUE_SEVERITY_RANK = {"OK": 0, "WARN": 1, "RECONCILE_REQUIRED": 2, "BROKEN": 3}


class LiveSimLifecycleAuditor:
    def __init__(self, db: TradingDatabase, gateway_state: Any | None = None) -> None:
        self.db = db
        self.gateway_state = gateway_state

    def build_report(
        self,
        *,
        trade_date: str | None = None,
        now: str | None = None,
        limit: int = 1000,
        cancel_stale_sec: int = 180,
    ) -> dict[str, Any]:
        resolved_trade_date = trade_date or _today()
        current = now or _now()
        orders = self.db.list_live_sim_orders(trade_date=resolved_trade_date, limit=limit)
        cancels = self.db.list_live_sim_cancel_orders(trade_date=resolved_trade_date, limit=limit)
        fills = self.db.list_live_sim_fill_events(trade_date=resolved_trade_date, limit=limit)
        positions = self.db.list_live_sim_positions(limit=limit)
        events = self.db.list_live_sim_order_events(trade_date=resolved_trade_date, limit=limit * 2)
        reconciles = self.db.list_live_sim_reconcile_events(trade_date=resolved_trade_date, limit=limit)
        health = self.db.list_live_sim_runtime_health(limit=100)
        commands = self._live_sim_commands(trade_date=resolved_trade_date, limit=limit)

        order_by_id = {str(order.get("order_intent_id") or ""): order for order in orders}
        fills_by_order = _group_by(fills, "order_intent_id")
        command_rows = self._command_rows(commands, order_by_id=order_by_id)

        transition_warnings = self._transition_warnings(events)
        reconcile_issues = self._reconcile_issues(orders, fills, reconciles, health, transition_warnings, current=current)
        position_issues = self._position_issues(orders, fills, positions, current=current)
        cancel_issues = self._cancel_issues(orders, cancels, current=current, stale_sec=cancel_stale_sec)
        command_issues = self._command_issues(command_rows, orders, current=current)

        issues = [*reconcile_issues, *position_issues, *cancel_issues, *command_issues, *transition_warnings]
        status = _overall_status(issues)
        summary = self._summary(
            orders=orders,
            fills=fills,
            positions=positions,
            cancels=cancels,
            reconciles=reconciles,
            issues=issues,
            health=health,
        )
        open_orders = [
            self._open_order_row(order, fills_by_order.get(str(order.get("order_intent_id") or ""), []), cancels)
            for order in orders
            if str(order.get("order_status") or "") in ACTIVE_ORDER_STATUSES
        ]
        return {
            "available": bool(orders or cancels or fills or positions or events or reconciles or commands),
            "status": status,
            "trade_date": resolved_trade_date,
            "summary": summary,
            "order_funnel": _order_funnel(orders, events, commands),
            "command_audit": command_rows[:200],
            "open_orders": open_orders[:200],
            "reconcile_issues": reconcile_issues[:200],
            "position_issues": position_issues[:200],
            "cancel_issues": cancel_issues[:200],
            "command_issues": command_issues[:200],
            "transition_warnings": transition_warnings[:200],
            "issues": issues[:300],
            "last_updated_at": _last_updated_at(orders, cancels, fills, positions, events, reconciles, commands, health),
            "operator": {
                "status_message_ko": _status_message_ko(status),
                "top_actions": _top_operator_actions(issues),
                "reconcile_block_new_buy": bool(summary.get("reconcile_block_new_buy")),
            },
        }

    def _live_sim_commands(self, *, trade_date: str, limit: int) -> list[dict[str, Any]]:
        if self.gateway_state is None:
            return []
        rows: list[dict[str, Any]] = []
        for command_type in ("send_order", "cancel_order"):
            try:
                rows.extend(
                    self.gateway_state.list_commands(
                        command_type=command_type,
                        trade_date=trade_date,
                        include_finished=True,
                        limit=limit,
                    )
                )
            except TypeError:
                rows.extend(self.gateway_state.list_commands(command_type=command_type, include_finished=True, limit=limit))
            except Exception:
                continue
        return [row for row in rows if _is_live_sim_command(row, trade_date)]

    def _command_rows(self, commands: list[dict[str, Any]], *, order_by_id: dict[str, dict]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for command in commands:
            command_body = dict(command.get("command") or {})
            payload = dict(command_body.get("payload") or {})
            metadata = dict(command.get("metadata") or {})
            command_id = str(command.get("command_id") or command_body.get("command_id") or "")
            command_type = str(command.get("command_type") or command_body.get("type") or "")
            order_intent_id = str(payload.get("live_sim_order_intent_id") or metadata.get("order_intent_id") or "")
            cancel_intent_id = str(payload.get("cancel_intent_id") or metadata.get("cancel_intent_id") or "")
            original_order_id = str(payload.get("original_order_id") or metadata.get("original_order_id") or "")
            linked_order = order_by_id.get(order_intent_id) or order_by_id.get(original_order_id) or {}
            events = []
            if self.gateway_state is not None and command_id:
                try:
                    events = self.gateway_state.command_events(command_id, limit=50)
                except Exception:
                    events = []
            rows.append(
                {
                    "trade_date": str(command.get("trade_date") or _trade_date(command.get("created_at")) or ""),
                    "candidate_instance_id": str(linked_order.get("candidate_instance_id") or payload.get("candidate_instance_id") or ""),
                    "code": str(payload.get("code") or linked_order.get("code") or ""),
                    "side": str(payload.get("side") or linked_order.get("side") or ""),
                    "order_phase": str(payload.get("order_phase") or ("exit" if str(payload.get("side") or "").lower() == "sell" else "entry")),
                    "order_intent_id": order_intent_id or original_order_id,
                    "live_sim_order_intent_id": order_intent_id,
                    "cancel_intent_id": cancel_intent_id,
                    "command_id": command_id,
                    "command_type": command_type,
                    "idempotency_key": str(command.get("idempotency_key") or command_body.get("idempotency_key") or ""),
                    "dedupe_key": str(command.get("dedupe_key") or ""),
                    "broker_order_id": str(linked_order.get("broker_order_id") or payload.get("broker_order_id") or ""),
                    "original_order_no": str(payload.get("original_order_no") or ""),
                    "runtime_cycle_id": str(payload.get("runtime_cycle_id") or ""),
                    "decision_cycle_id": str(payload.get("decision_cycle_id") or ""),
                    "status": str(command.get("status") or ""),
                    "created_at": str(command.get("created_at") or ""),
                    "dispatched_at": str(command.get("dispatched_at") or ""),
                    "acked_at": str(command.get("acked_at") or ""),
                    "finished_at": str(command.get("finished_at") or ""),
                    "attempts": int(command.get("attempts") or 0),
                    "max_attempts": int(command.get("max_attempts") or 0),
                    "expired": str(command.get("status") or "") == "EXPIRED",
                    "last_error": str(command.get("last_error") or ""),
                    "events": events,
                }
            )
        return rows

    def _transition_warnings(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        for item in transition_warning_codes(events):
            event = dict(item.get("event") or {})
            warnings.append(
                _issue(
                    issue_type=str(item.get("warning_code") or "LIVE_SIM_INVALID_STATUS_TRANSITION"),
                    severity="WARN",
                    operator_message_ko=str(item.get("operator_message_ko") or "허용되지 않은 주문 상태 전이입니다."),
                    order_intent_id=str(event.get("order_intent_id") or ""),
                    code=str(event.get("code") or ""),
                    side=str(event.get("side") or ""),
                    order_status=str(item.get("status_to") or ""),
                    details={"event": event, "status_from": item.get("status_from"), "status_to": item.get("status_to")},
                )
            )
        return warnings

    def _reconcile_issues(
        self,
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        reconciles: list[dict[str, Any]],
        health: list[dict[str, Any]],
        transition_warnings: list[dict[str, Any]],
        *,
        current: str,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for order in orders:
            status = str(order.get("order_status") or "")
            broker_order_id = str(order.get("broker_order_id") or "")
            if status == "UNKNOWN_SUBMIT":
                issues.append(
                    _issue(
                        issue_type="UNKNOWN_SUBMIT",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="주문번호가 없어 재조회가 필요합니다.",
                        order=order,
                    )
                )
            if status in {"ACCEPTED", "PARTIAL_FILLED", "CANCEL_REQUESTED", "UNKNOWN_SUBMIT"} and not broker_order_id:
                issues.append(
                    _issue(
                        issue_type="BROKER_ORDER_ID_MISSING",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="주문번호가 없어 재조회가 필요합니다.",
                        order=order,
                    )
                )
            if status == "RECONCILE_REQUIRED":
                issues.append(
                    _issue(
                        issue_type=_first_reason(order, "RECONCILE_REQUIRED"),
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="주문 상태가 재조회 필요로 표시되어 있습니다.",
                        order=order,
                    )
                )
                if not broker_order_id:
                    issues.append(
                        _issue(
                            issue_type="BROKER_ORDER_ID_MISSING",
                            severity="RECONCILE_REQUIRED",
                            operator_message_ko="주문번호가 없어 재조회가 필요합니다.",
                            order=order,
                        )
                    )
        for fill in fills:
            if not fill.get("order_intent_id"):
                issues.append(
                    _issue(
                        issue_type="ORPHAN_EXECUTION",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="주문 원장이 없는 체결 이벤트가 있습니다.",
                        code=str(fill.get("code") or ""),
                        side=str(fill.get("side") or ""),
                        broker_order_id=str(fill.get("broker_order_id") or ""),
                        details={"fill": fill},
                    )
                )
        for row in health:
            status = str(row.get("status") or "")
            if status in {"RECONCILE_REQUIRED", "UNHEALTHY", "BROKEN"}:
                issues.append(
                    _issue(
                        issue_type=str(row.get("reason") or "LIVE_SIM_RUNTIME_HEALTH_UNHEALTHY"),
                        severity="RECONCILE_REQUIRED" if status == "RECONCILE_REQUIRED" else "WARN",
                        operator_message_ko="재기동 후 broker snapshot 확인 전까지 신규 매수를 차단하는 것이 안전합니다.",
                        details={"health": row, "current": current},
                    )
                )
        for event in reconciles:
            if str(event.get("status") or "") == "FAILED":
                issues.append(
                    _issue(
                        issue_type="LIVE_SIM_RECONCILE_FAILED",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="재기동 reconcile이 실패했습니다.",
                        details={"reconcile_event": event},
                    )
                )
        return issues

    def _position_issues(self, orders: list[dict[str, Any]], fills: list[dict[str, Any]], positions: list[dict[str, Any]], *, current: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        positions_by_key = {_position_key(position): position for position in positions}
        fills_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for fill in fills:
            fills_by_key[_fill_position_key(fill)].append(fill)
        for order in orders:
            if str(order.get("side") or "").lower() == "buy" and str(order.get("order_status") or "") == "FILLED":
                key = _order_position_key(order)
                if key not in positions_by_key:
                    issues.append(
                        _issue(
                            issue_type="FILLED_BUY_ORDER_POSITION_MISSING",
                            severity="BROKEN",
                            operator_message_ko="체결 완료된 매수 주문이 있지만 포지션이 없습니다.",
                            order=order,
                        )
                    )
        open_by_account_code: Counter[tuple[str, str]] = Counter()
        for position in positions:
            status = str(position.get("status") or "")
            qty = int(position.get("current_qty") or 0)
            key = _position_key(position)
            if status in OPEN_POSITION_STATUSES:
                open_by_account_code[(str(position.get("account_id_masked") or ""), str(position.get("code") or ""))] += 1
            if status == "CLOSED" and qty > 0:
                issues.append(
                    _issue(
                        issue_type="CLOSED_POSITION_HAS_QTY",
                        severity="BROKEN",
                        operator_message_ko="CLOSED 포지션인데 현재 수량이 남아 있습니다.",
                        position=position,
                    )
                )
            if status in OPEN_POSITION_STATUSES and qty <= 0:
                issues.append(
                    _issue(
                        issue_type="OPEN_POSITION_NON_POSITIVE_QTY",
                        severity="BROKEN",
                        operator_message_ko="OPEN 포지션인데 현재 수량이 0 이하입니다.",
                        position=position,
                    )
                )
            if status in OPEN_POSITION_STATUSES and not position.get("candidate_instance_id"):
                issues.append(
                    _issue(
                        issue_type="POSITION_CANDIDATE_INSTANCE_ID_MISSING",
                        severity="WARN",
                        operator_message_ko="포지션에 candidate_instance_id가 없습니다.",
                        position=position,
                    )
                )
            if status in OPEN_POSITION_STATUSES and (
                int(position.get("stop_loss_price") or 0) <= 0
                or int(position.get("take_profit_price") or 0) <= 0
                or not str(position.get("max_hold_exit_at") or "")
            ):
                issues.append(
                    _issue(
                        issue_type="POSITION_EXIT_GUARD_FIELD_MISSING",
                        severity="WARN",
                        operator_message_ko="손절/익절/최대보유시간 값이 누락된 포지션입니다.",
                        position=position,
                    )
                )
            related_fills = fills_by_key.get(key, [])
            if related_fills:
                expected_qty = _net_position_qty(related_fills)
                if expected_qty != qty:
                    issues.append(
                        _issue(
                            issue_type="POSITION_QTY_MISMATCH",
                            severity="RECONCILE_REQUIRED",
                            operator_message_ko="DB 포지션 수량과 체결 합계가 일치하지 않습니다.",
                            position=position,
                            details={"expected_qty_from_fills": expected_qty, "current_qty": qty, "fills": related_fills[:20], "current": current},
                        )
                    )
                expected_avg = _avg_buy_price(related_fills)
                if expected_avg > 0 and int(position.get("entry_avg_price") or 0) > 0 and abs(expected_avg - int(position.get("entry_avg_price") or 0)) > 1:
                    issues.append(
                        _issue(
                            issue_type="POSITION_AVG_PRICE_MISMATCH",
                            severity="WARN",
                            operator_message_ko="평균단가가 체결 내역으로 계산한 값과 다릅니다.",
                            position=position,
                            details={"expected_avg_price": expected_avg, "entry_avg_price": position.get("entry_avg_price")},
                        )
                    )
            elif status in OPEN_POSITION_STATUSES:
                issues.append(
                    _issue(
                        issue_type="OPEN_POSITION_WITHOUT_FILL_EVENT",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="열린 포지션이 있지만 연결된 체결 이벤트가 없습니다.",
                        position=position,
                    )
                )
        for (account, code), count in open_by_account_code.items():
            if count > 1:
                issues.append(
                    _issue(
                        issue_type="DUPLICATE_OPEN_POSITION",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="같은 계좌/종목에 열린 포지션이 둘 이상입니다.",
                        code=code,
                        details={"account_id_masked": account, "open_position_count": count},
                    )
                )
        return issues

    def _cancel_issues(self, orders: list[dict[str, Any]], cancels: list[dict[str, Any]], *, current: str, stale_sec: int) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        active_cancels_by_order: Counter[str] = Counter()
        for cancel in cancels:
            if str(cancel.get("status") or "") in {"CREATED", "QUEUED", "SUBMITTED", "CANCEL_REQUESTED", "UNKNOWN_SUBMIT"}:
                active_cancels_by_order[str(cancel.get("original_order_id") or "")] += 1
                age = _age_sec(cancel.get("submitted_at") or cancel.get("created_at"), current)
                if age is not None and age >= stale_sec:
                    issues.append(
                        _issue(
                            issue_type="CANCEL_REQUESTED_STALE",
                            severity="RECONCILE_REQUIRED",
                            operator_message_ko="부분체결 후 잔량 취소 결과가 확인되지 않았습니다.",
                            cancel=cancel,
                            details={"age_sec": age, "stale_sec": stale_sec},
                        )
                    )
            if str(cancel.get("status") or "") == "RECONCILE_REQUIRED":
                issues.append(
                    _issue(
                        issue_type=_first_reason(cancel, "LIVE_SIM_CANCEL_RECONCILE_REQUIRED"),
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="취소 주문이 재조회 필요 상태입니다.",
                        cancel=cancel,
                    )
                )
        orders_by_id = {str(order.get("order_intent_id") or ""): order for order in orders}
        for order_id, count in active_cancels_by_order.items():
            if count > 1:
                issues.append(
                    _issue(
                        issue_type="DUPLICATE_CANCEL_ORDER",
                        severity="WARN",
                        operator_message_ko="동일 원주문에 활성 취소 주문이 중복으로 있습니다.",
                        order=orders_by_id.get(order_id, {}),
                        details={"active_cancel_count": count},
                    )
                )
        for order in orders:
            status = str(order.get("order_status") or "")
            if status == "CANCEL_REQUESTED":
                age = _age_sec(order.get("updated_at") or order.get("accepted_at") or order.get("submitted_at"), current)
                if age is not None and age >= stale_sec:
                    issues.append(
                        _issue(
                            issue_type="CANCEL_REQUESTED_STALE",
                            severity="RECONCILE_REQUIRED",
                            operator_message_ko="취소 요청 후 장시간 결과가 확인되지 않았습니다.",
                            order=order,
                            details={"age_sec": age, "stale_sec": stale_sec},
                        )
                    )
            if status in LIVE_SIM_TERMINAL_ORDER_STATUSES and active_cancels_by_order.get(str(order.get("order_intent_id") or ""), 0) > 0:
                issues.append(
                    _issue(
                        issue_type="TERMINAL_ORDER_HAS_ACTIVE_CANCEL",
                        severity="WARN",
                        operator_message_ko="터미널 상태 주문에 활성 취소 주문이 연결되어 있습니다.",
                        order=order,
                    )
                )
        return issues

    def _command_issues(self, commands: list[dict[str, Any]], orders: list[dict[str, Any]], *, current: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        orders_by_command = {str(order.get("command_id") or ""): order for order in orders if order.get("command_id")}
        for command in commands:
            status = str(command.get("status") or "")
            order = orders_by_command.get(str(command.get("command_id") or ""), {})
            if status == "EXPIRED":
                issues.append(
                    _issue(
                        issue_type="COMMAND_EXPIRED",
                        severity="RECONCILE_REQUIRED" if command.get("command_type") == "send_order" else "WARN",
                        operator_message_ko="주문 command가 만료되어 전송 여부 재확인이 필요합니다.",
                        order=order,
                        details={"command": command, "current": current},
                    )
                )
            if status in {"FAILED", "REJECTED"}:
                issues.append(
                    _issue(
                        issue_type="COMMAND_REJECTED",
                        severity="WARN",
                        operator_message_ko="gateway command가 거절되거나 실패했습니다.",
                        order=order,
                        details={"command": command},
                    )
                )
            if command.get("command_type") == "send_order" and status in {"ACKED", "DISPATCHED"} and order and not order.get("broker_order_id"):
                issues.append(
                    _issue(
                        issue_type="COMMAND_ACKED_BUT_BROKER_ORDER_ID_MISSING",
                        severity="RECONCILE_REQUIRED",
                        operator_message_ko="command ack 이후 주문번호가 저장되지 않았습니다.",
                        order=order,
                        details={"command": command},
                    )
                )
        return issues

    def _summary(
        self,
        *,
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        cancels: list[dict[str, Any]],
        reconciles: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        health: list[dict[str, Any]],
    ) -> dict[str, Any]:
        order_counts = Counter(str(order.get("order_status") or "") for order in orders)
        issue_counts = Counter(str(issue.get("issue_type") or "") for issue in issues)
        last_reconcile = reconciles[0] if reconciles else {}
        reconcile_health = next((row for row in health if str(row.get("component") or "") == "reconcile"), {})
        return {
            "order_status_counts": dict(order_counts),
            "open_live_sim_order_count": sum(order_counts.get(status, 0) for status in ACTIVE_ORDER_STATUSES),
            "unknown_submit_count": order_counts.get("UNKNOWN_SUBMIT", 0),
            "reconcile_required_order_count": order_counts.get("RECONCILE_REQUIRED", 0),
            "cancel_requested_stale_count": issue_counts.get("CANCEL_REQUESTED_STALE", 0),
            "broker_order_id_missing_count": issue_counts.get("BROKER_ORDER_ID_MISSING", 0)
            + issue_counts.get("COMMAND_ACKED_BUT_BROKER_ORDER_ID_MISSING", 0),
            "orphan_execution_count": issue_counts.get("ORPHAN_EXECUTION", 0),
            "orphan_position_count": issue_counts.get("OPEN_POSITION_WITHOUT_FILL_EVENT", 0),
            "position_qty_mismatch_count": issue_counts.get("POSITION_QTY_MISMATCH", 0),
            "duplicate_open_position_count": issue_counts.get("DUPLICATE_OPEN_POSITION", 0),
            "reconcile_block_new_buy": bool(
                order_counts.get("UNKNOWN_SUBMIT", 0)
                or order_counts.get("RECONCILE_REQUIRED", 0)
                or any(str(row.get("status") or "") == "RECONCILE_REQUIRED" for row in health)
            ),
            "last_reconcile_at": str(last_reconcile.get("completed_at") or last_reconcile.get("started_at") or reconcile_health.get("updated_at") or ""),
            "last_reconcile_status": str(last_reconcile.get("status") or reconcile_health.get("status") or ""),
            "last_reconcile_error": str(last_reconcile.get("reason") if str(last_reconcile.get("status") or "") == "FAILED" else reconcile_health.get("reason") or ""),
            "order_count": len(orders),
            "fill_event_count": len(fills),
            "cancel_order_count": len(cancels),
            "position_count": len(positions),
            "issue_counts": dict(issue_counts),
            "severity_counts": dict(Counter(str(issue.get("severity") or "") for issue in issues)),
        }

    def _open_order_row(self, order: dict[str, Any], fills: list[dict[str, Any]], cancels: list[dict[str, Any]]) -> dict[str, Any]:
        related_cancels = [cancel for cancel in cancels if str(cancel.get("original_order_id") or "") == str(order.get("order_intent_id") or "")]
        latest_cancel = related_cancels[0] if related_cancels else {}
        return {
            "order_intent_id": str(order.get("order_intent_id") or ""),
            "candidate_instance_id": str(order.get("candidate_instance_id") or ""),
            "code": str(order.get("code") or ""),
            "name": str(order.get("name") or ""),
            "side": str(order.get("side") or ""),
            "order_status": str(order.get("order_status") or ""),
            "broker_order_id": str(order.get("broker_order_id") or ""),
            "command_id": str(order.get("command_id") or ""),
            "requested_qty": int(order.get("requested_qty") or 0),
            "cumulative_fill_qty": max([int(fill.get("cumulative_fill_qty") or 0) for fill in fills] or [0]),
            "remaining_qty": _remaining_qty_from_order(order, fills),
            "cancel_due": str(order.get("order_status") or "") in {"ACCEPTED", "PARTIAL_FILLED", "SUBMITTED"},
            "cancel_due_reason": _cancel_due_reason_from_order(order),
            "cancel_qty": _remaining_qty_from_order(order, fills),
            "cancel_command_id": str(latest_cancel.get("command_id") or ""),
            "cancel_status": str(latest_cancel.get("status") or ""),
            "cancel_attempts": int(latest_cancel.get("attempts") or 0),
            "cancel_blocked_reason": str((latest_cancel.get("details") or {}).get("reason") or ""),
            "cancel_reconcile_required": str(latest_cancel.get("status") or "") == "RECONCILE_REQUIRED"
            or "LIVE_SIM_CANCEL_RECONCILE_REQUIRED" in list(latest_cancel.get("reason_codes") or []),
            "updated_at": str(order.get("updated_at") or order.get("created_at") or ""),
        }


def build_live_sim_lifecycle_audit(
    db: TradingDatabase,
    *,
    gateway_state: Any | None = None,
    trade_date: str | None = None,
    now: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    return LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(
        trade_date=trade_date,
        now=now,
        limit=limit,
    )


def _order_funnel(orders: list[dict[str, Any]], events: list[dict[str, Any]], commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order_counts = Counter(str(order.get("order_status") or "") for order in orders)
    event_counts = Counter(str(event.get("event_type") or "") for event in events)
    command_counts = Counter(str(command.get("status") or "") for command in commands)
    rows = [{"stage": status, "count": order_counts.get(status, 0), "source": "live_sim_orders"} for status in LIVE_SIM_ORDER_STATUSES]
    rows.extend(
        [
            {"stage": "COMMAND_QUEUED", "count": command_counts.get("QUEUED", 0), "source": "gateway_commands"},
            {"stage": "COMMAND_DISPATCHED", "count": command_counts.get("DISPATCHED", 0), "source": "gateway_commands"},
            {"stage": "COMMAND_ACKED", "count": command_counts.get("ACKED", 0), "source": "gateway_commands"},
            {"stage": "COMMAND_REJECTED", "count": command_counts.get("REJECTED", 0) + command_counts.get("FAILED", 0), "source": "gateway_commands"},
            {"stage": "CANCEL_DUE", "count": event_counts.get("cancel_due", 0), "source": "live_sim_order_events"},
            {"stage": "POSITION_OPENED", "count": event_counts.get("position_opened", 0), "source": "live_sim_order_events"},
            {"stage": "POSITION_CLOSED", "count": event_counts.get("position_closed", 0), "source": "live_sim_order_events"},
        ]
    )
    return rows


def _is_live_sim_command(command: dict[str, Any], trade_date: str) -> bool:
    command_body = dict(command.get("command") or {})
    payload = dict(command_body.get("payload") or {})
    metadata = dict(command.get("metadata") or {})
    explicit_trade_date = str(command.get("trade_date") or "")
    if explicit_trade_date and explicit_trade_date != trade_date:
        return False
    return str(payload.get("order_mode") or "") == "LIVE_SIM" or str(metadata.get("runtime") or "") == "LIVE_SIM"


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "")].append(row)
    return grouped


def _issue(
    *,
    issue_type: str,
    severity: str,
    operator_message_ko: str,
    order: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
    cancel: dict[str, Any] | None = None,
    order_intent_id: str = "",
    code: str = "",
    side: str = "",
    order_status: str = "",
    broker_order_id: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = order or position or cancel or {}
    return {
        "order_intent_id": order_intent_id or str(source.get("order_intent_id") or source.get("original_order_id") or ""),
        "position_id": str(source.get("position_id") or ""),
        "cancel_intent_id": str(source.get("cancel_intent_id") or ""),
        "code": code or str(source.get("code") or ""),
        "side": side or str(source.get("side") or ""),
        "order_status": order_status or str(source.get("order_status") or source.get("status") or ""),
        "broker_order_id": broker_order_id or str(source.get("broker_order_id") or ""),
        "issue_type": issue_type,
        "severity": severity,
        "suggested_action": _suggested_action(issue_type),
        "operator_message_ko": operator_message_ko,
        "details": details or {},
    }


def _overall_status(issues: list[dict[str, Any]]) -> str:
    rank = 0
    status = "OK"
    for issue in issues:
        severity = str(issue.get("severity") or "OK")
        if ISSUE_SEVERITY_RANK.get(severity, 0) > rank:
            rank = ISSUE_SEVERITY_RANK.get(severity, 0)
            status = severity
    return status


def _position_key(position: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(position.get("account_id_masked") or ""),
        str(position.get("code") or ""),
        str(position.get("candidate_instance_id") or ""),
    )


def _fill_position_key(fill: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(fill.get("account_id_masked") or ""),
        str(fill.get("code") or ""),
        str(fill.get("candidate_instance_id") or ""),
    )


def _order_position_key(order: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(order.get("account_id_masked") or ""),
        str(order.get("code") or ""),
        str(order.get("candidate_instance_id") or ""),
    )


def _net_position_qty(fills: list[dict[str, Any]]) -> int:
    total = 0
    for fill in fills:
        qty = int(fill.get("fill_qty") or 0)
        if str(fill.get("side") or "").lower() == "sell":
            total -= qty
        else:
            total += qty
    return total


def _avg_buy_price(fills: list[dict[str, Any]]) -> int:
    qty = 0
    amount = 0
    for fill in fills:
        if str(fill.get("side") or "").lower() != "buy":
            continue
        fill_qty = int(fill.get("fill_qty") or 0)
        qty += fill_qty
        amount += fill_qty * int(fill.get("fill_price") or 0)
    return int(round(amount / qty)) if qty > 0 else 0


def _remaining_qty_from_order(order: dict[str, Any], fills: list[dict[str, Any]]) -> int:
    if fills:
        return max(0, int(sorted(fills, key=lambda item: str(item.get("received_at") or item.get("event_time") or ""))[-1].get("remaining_qty") or 0))
    return max(0, int(order.get("submitted_qty") or order.get("requested_qty") or 0))


def _cancel_due_reason_from_order(order: dict[str, Any]) -> str:
    status = str(order.get("order_status") or "")
    side = str(order.get("side") or "").lower()
    if status == "PARTIAL_FILLED":
        return "partial_remainder"
    if side == "sell":
        return "unfilled_sell"
    return "unfilled_buy"


def _first_reason(row: dict[str, Any], fallback: str) -> str:
    for item in list(row.get("reason_codes") or []):
        text = str(item or "")
        if "RECONCILE" in text or "MISSING" in text or "MISMATCH" in text or "FAILED" in text:
            return text
    return fallback


def _suggested_action(issue_type: str) -> str:
    if issue_type in {"UNKNOWN_SUBMIT", "BROKER_ORDER_ID_MISSING", "COMMAND_ACKED_BUT_BROKER_ORDER_ID_MISSING"}:
        return "REFRESH_BROKER_ORDER_SNAPSHOT"
    if "POSITION" in issue_type or "ORPHAN" in issue_type:
        return "RUN_LIVE_SIM_RECONCILE_AND_VERIFY_POSITION"
    if "CANCEL" in issue_type:
        return "CHECK_CANCEL_RESULT_AND_RECONCILE"
    if "COMMAND" in issue_type:
        return "CHECK_GATEWAY_COMMAND_QUEUE"
    return "REVIEW_LIVE_SIM_AUDIT_DETAIL"


def _status_message_ko(status: str) -> str:
    if status == "OK":
        return "LIVE_SIM 주문 lifecycle 정상"
    if status == "BROKEN":
        return "포지션 수량 불일치 또는 깨진 상태가 있습니다."
    if status == "RECONCILE_REQUIRED":
        return "주문번호 누락 또는 reconcile 필요 상태가 있습니다."
    return "미체결 취소 대기 또는 확인이 필요한 경고가 있습니다."


def _top_operator_actions(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(issue.get("issue_type") or "") for issue in issues)
    return [
        {
            "issue_type": issue_type,
            "count": count,
            "suggested_action": _suggested_action(issue_type),
            "operator_message_ko": next((str(issue.get("operator_message_ko") or "") for issue in issues if issue.get("issue_type") == issue_type), ""),
        }
        for issue_type, count in counts.most_common(5)
    ]


def _last_updated_at(*groups: list[dict[str, Any]]) -> str:
    candidates: list[str] = []
    for rows in groups:
        for row in rows:
            for key in ("updated_at", "created_at", "received_at", "event_time", "completed_at", "started_at", "acked_at", "finished_at"):
                value = str(row.get(key) or "")
                if value:
                    candidates.append(value)
                    break
    return max(candidates) if candidates else ""


def _age_sec(start: object, current: str) -> int | None:
    start_dt = _parse_time(start)
    current_dt = _parse_time(current)
    if start_dt is None or current_dt is None:
        return None
    return max(0, int((current_dt - start_dt).total_seconds()))


def _parse_time(value: object) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _trade_date(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        text = str(value or "")
        return text[:10] if len(text) >= 10 else ""
    return parsed.astimezone().date().isoformat()


def _today() -> str:
    return datetime.now().date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
