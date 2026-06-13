from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from storage.db import TradingDatabase
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.shadow_small_entry_ops import ShadowSmallEntryOpsConfig, ShadowSmallEntryOpsService
from trading_app.shadow_small_entry_promotion import ShadowSmallEntryPromotionAnalyzer


STATUS_PLANNED = "PLANNED"
STATUS_PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
STATUS_ARMED = "ARMED"
STATUS_ACTIVE = "ACTIVE"
STATUS_PAUSED = "PAUSED"
STATUS_ROLLED_BACK = "ROLLED_BACK"
STATUS_COMPLETED = "COMPLETED"
STATUS_ABORTED = "ABORTED"
STATUS_FAILED = "FAILED"
STATUS_REVIEW_READY = "REVIEW_READY"

RECOMMEND_CONTINUE_OBSERVE_ONLY = "CONTINUE_OBSERVE_ONLY"
RECOMMEND_CONTINUE_LIVE_SIM_GUARDED = "CONTINUE_LIVE_SIM_GUARDED"
RECOMMEND_REDUCE_SIZE = "REDUCE_SIZE"
RECOMMEND_REDUCE_FREQUENCY = "REDUCE_FREQUENCY"
RECOMMEND_KEEP_DISABLED = "KEEP_DISABLED"
RECOMMEND_ROLLBACK_TO_OBSERVE_ONLY = "ROLLBACK_TO_OBSERVE_ONLY"
RECOMMEND_INVESTIGATE_ORDER_LIFECYCLE = "INVESTIGATE_ORDER_LIFECYCLE"
RECOMMEND_INVESTIGATE_RECONCILE = "INVESTIGATE_RECONCILE"
RECOMMEND_INVESTIGATE_DATA_QUALITY = "INVESTIGATE_DATA_QUALITY"
RECOMMEND_INVESTIGATE_EXIT_LOGIC = "INVESTIGATE_EXIT_LOGIC"

REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "shadow_small_entry_pilot"

PILOT_EVENT_TYPES = {
    "PREFLIGHT_RUN",
    "ARMED",
    "ACTIVATED",
    "PROMOTION_CANDIDATE",
    "PROMOTION_OBSERVE_ONLY",
    "PROMOTED",
    "PROMOTION_BLOCKED",
    "ORDER_SUBMITTED",
    "ORDER_BLOCKED",
    "ORDER_ACCEPTED",
    "PARTIAL_FILLED",
    "FILLED",
    "CANCEL_DUE",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "EXIT_DECISION_CREATED",
    "EXIT_ORDER_SUBMITTED",
    "EXIT_FILLED",
    "POSITION_OPENED",
    "POSITION_CLOSED",
    "RISK_CHECK",
    "AUTO_PAUSED",
    "OPERATOR_PAUSED",
    "ROLLED_BACK",
    "RECONCILE_REQUIRED",
    "AUDIT_WARNING",
    "AUDIT_BROKEN",
    "PILOT_COMPLETED",
}

SUMMARY_KEYS = [
    "candidate_count",
    "observe_only_candidate_count",
    "promoted_count",
    "blocked_count",
    "submitted_order_count",
    "accepted_order_count",
    "rejected_order_count",
    "duplicate_block_count",
    "filled_order_count",
    "partial_fill_count",
    "cancelled_order_count",
    "open_position_count",
    "closed_position_count",
    "total_notional_krw",
    "realized_pnl_krw",
    "unrealized_pnl_krw",
    "total_pnl_krw",
    "avg_return_pct",
    "win_count",
    "loss_count",
    "win_rate",
    "max_mfe_pct",
    "max_mae_pct",
    "avg_mfe_pct",
    "avg_mae_pct",
    "max_drawdown_pct",
    "consecutive_losing_trades",
    "auto_pause_count",
    "operator_pause_count",
    "rollback_count",
    "reconcile_required_count",
    "audit_warning_count",
    "audit_broken_count",
    "unknown_submit_count",
    "cancel_requested_stale_count",
    "exit_guard_block_count",
    "kill_switch_block_count",
    "gateway_disconnect_count",
]


class ShadowSmallEntryPilotService:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        gateway_state: Any | None = None,
        now_provider: Any | None = None,
        report_root: Path | None = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.now_provider = now_provider or (lambda: datetime.now().replace(microsecond=0))
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def pilot_id_for_date(self, trade_date: str | None = None) -> str:
        return f"shadow_small_entry_pilot:{trade_date or self._today()}"

    def start(
        self,
        *,
        trade_date: str | None = None,
        operator: str = "operator",
        operator_note: str = "",
        source_report_trade_date: str = "",
    ) -> dict[str, Any]:
        today = trade_date or self._today()
        now = self._now()
        settings = StrategyRuntimeSettingsRepository(self.db).load()
        ops = ShadowSmallEntryOpsService(
            self.db,
            gateway_state=self.gateway_state,
            now_provider=self.now_provider,
            report_root=self.report_root.parent / "shadow_small_entry_ops",
        )
        ops_status = ops.status(trade_date=today)
        preflight = ops.preflight(trade_date=today, persist=True)
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        promotion_snapshot = dict(settings.value("shadow_small_entry_promotion", {}) or {})
        ops_snapshot = dict(settings.value("shadow_small_entry_ops", {}) or {})
        status = STATUS_PREFLIGHT_PASSED if bool(preflight.get("ok")) else STATUS_PLANNED
        run = self.db.save_shadow_small_entry_pilot_run(
            {
                "pilot_id": self.pilot_id_for_date(today),
                "trade_date": today,
                "created_at": now,
                "started_at": now,
                "status": status,
                "mode": str(ops_status.get("mode") or promotion_snapshot.get("mode") or "observe_only"),
                "order_enabled_at_start": bool(ops_status.get("order_enabled") or promotion_snapshot.get("order_enabled")),
                "operator": operator,
                "operator_note": operator_note,
                "source_report_trade_date": source_report_trade_date or today,
                "runtime_settings_hash": _hash_payload(settings.settings_json),
                "promotion_policy_snapshot": promotion_snapshot,
                "ops_policy_snapshot": ops_snapshot,
                "risk_limit_snapshot": {"daily_limits": cfg.daily_limits, "risk_limits": cfg.risk_limits},
                "preflight_snapshot": preflight,
                "operator_message_ko": "파일럿 preflight가 통과했습니다. 실제 runtime 설정은 변경하지 않았습니다."
                if preflight.get("ok")
                else "파일럿 preflight가 실패했습니다. observe_only 상태를 유지하세요.",
            }
        )
        self.db.save_shadow_small_entry_pilot_events(
            [
                self._event(
                    "PREFLIGHT_RUN",
                    pilot_id=run["pilot_id"],
                    trade_date=today,
                    event_at=now,
                    severity="PASS" if preflight.get("ok") else "FAIL",
                    reason_codes=preflight.get("blocking_reasons") or [],
                    details={"preflight": preflight},
                    operator_message_ko=run.get("operator_message_ko") or "",
                )
            ]
        )
        return {"ok": bool(preflight.get("ok")), "run": run, "preflight": preflight}

    def complete(
        self,
        *,
        trade_date: str | None = None,
        operator: str = "operator",
        operator_note: str = "",
        export: bool = False,
        fmt: str = "all",
    ) -> dict[str, Any]:
        today = trade_date or self._today()
        report = self.build_report(trade_date=today, persist=True)
        run = dict(report.get("run") or {})
        now = self._now()
        run = self.db.save_shadow_small_entry_pilot_run(
            {
                **run,
                "trade_date": today,
                "ended_at": now,
                "status": STATUS_REVIEW_READY,
                "operator": operator or run.get("operator") or "",
                "operator_note": operator_note or run.get("operator_note") or "",
                "summary": report.get("summary") or {},
                "recommendation": report.get("recommendation") or "",
                "recommendation_reason_codes": report.get("recommendation_reason_codes") or [],
                "operator_message_ko": report.get("operator_message_ko") or "",
                "updated_at": now,
            }
        )
        self.db.save_shadow_small_entry_pilot_events(
            [
                self._event(
                    "PILOT_COMPLETED",
                    pilot_id=run["pilot_id"],
                    trade_date=today,
                    event_at=now,
                    severity="OK",
                    details={"recommendation": report.get("recommendation"), "summary": report.get("summary") or {}},
                    operator_message_ko="파일럿 리포트가 REVIEW_READY 상태로 준비됐습니다.",
                )
            ]
        )
        report = {**report, "run": run, "status": run.get("status") or STATUS_REVIEW_READY}
        exports = self.export_report(report, fmt=fmt) if export else {}
        return {"ok": True, "report": report, "exports": exports}

    def status(self, *, trade_date: str | None = None) -> dict[str, Any]:
        today = trade_date or self._today()
        run = self.db.latest_shadow_small_entry_pilot_run(trade_date=today)
        if not run:
            return empty_payload(today)
        summary = dict(run.get("summary") or {})
        if int(summary.get("candidate_count") or 0) <= 0:
            try:
                report = self.build_report(trade_date=today, pilot_id=str(run.get("pilot_id") or ""), persist=False, limit=5000)
                summary = dict(report.get("summary") or summary)
                run = dict(report.get("run") or run)
            except Exception:
                summary = dict(run.get("summary") or {})
        return {
            "available": True,
            "pilot_id": str(run.get("pilot_id") or ""),
            "trade_date": today,
            "status": str(run.get("status") or STATUS_PLANNED),
            "mode": str(run.get("mode") or "observe_only"),
            "order_enabled_at_start": bool(run.get("order_enabled_at_start")),
            "started_at": str(run.get("started_at") or ""),
            "ended_at": str(run.get("ended_at") or ""),
            "recommendation": str(run.get("recommendation") or ""),
            "recommendation_reason_codes": list(run.get("recommendation_reason_codes") or []),
            "summary": _summary_defaults(summary),
            "operator_message_ko": str(run.get("operator_message_ko") or "파일럿 리포트 생성 전입니다."),
            "last_updated_at": str(run.get("updated_at") or run.get("created_at") or ""),
        }

    def build_report(
        self,
        *,
        trade_date: str | None = None,
        pilot_id: str = "",
        persist: bool = False,
        limit: int = 50000,
    ) -> dict[str, Any]:
        today = trade_date or self._today()
        run = self.db.get_shadow_small_entry_pilot_run(pilot_id) if pilot_id else self.db.latest_shadow_small_entry_pilot_run(trade_date=today)
        if not run:
            run = self._implicit_run(today)
            if persist:
                run = self.db.save_shadow_small_entry_pilot_run(run)
        pilot_id = str(run.get("pilot_id") or self.pilot_id_for_date(today))
        promotion_report = self._promotion_report(today, limit=limit)
        live_audit = self._live_audit(today, limit=limit)
        orders = [row for row in self.db.list_live_sim_orders(trade_date=today, limit=limit) if _is_shadow_order(row)]
        fills = [row for row in self.db.list_live_sim_fill_events(trade_date=today, limit=limit) if _fill_is_shadow(row, orders)]
        positions = [row for row in self.db.list_live_sim_positions(limit=limit) if _is_shadow_position(row) or _position_matches_orders(row, orders)]
        cancels = [row for row in self.db.list_live_sim_cancel_orders(trade_date=today, limit=limit) if _cancel_matches_orders(row, orders)]
        ops_audit = self.db.list_shadow_small_entry_ops_audit_log(trade_date=today, limit=limit)
        traces = self.db.list_buy_zero_trace_events(trade_date=today, limit=min(limit, 5000))

        generated_events = self._collect_events(
            pilot_id=pilot_id,
            trade_date=today,
            promotion_report=promotion_report,
            live_audit=live_audit,
            orders=orders,
            fills=fills,
            positions=positions,
            cancels=cancels,
            ops_audit=ops_audit,
            traces=traces,
        )
        if persist:
            self.db.save_shadow_small_entry_pilot_events(generated_events)
        persisted_events = self.db.list_shadow_small_entry_pilot_events(pilot_id=pilot_id, trade_date=today, limit=limit)
        all_events = _merge_events(persisted_events, generated_events)
        items = self._candidate_items(
            promotion_report=promotion_report,
            traces=traces,
            orders=orders,
            fills=fills,
            positions=positions,
            cancels=cancels,
            live_audit=live_audit,
        )
        summary = self._summary(
            run=run,
            items=items,
            events=all_events,
            promotion_report=promotion_report,
            live_audit=live_audit,
            orders=orders,
            fills=fills,
            positions=positions,
            cancels=cancels,
            ops_audit=ops_audit,
        )
        recommendation, reason_codes, message = self._recommend(summary, run=run, promotion_report=promotion_report, live_audit=live_audit)
        safety = self._safety_checks(summary=summary, events=all_events, live_audit=live_audit)
        comparison = _observe_live_comparison(items)
        report = {
            "available": bool(items or all_events or orders or positions or (promotion_report.get("available"))),
            "pilot_id": pilot_id,
            "trade_date": today,
            "status": str(run.get("status") or STATUS_PLANNED),
            "generated_at": self._now(),
            "run": run,
            "summary": summary,
            "recommendation": recommendation,
            "recommendation_reason_codes": reason_codes,
            "operator_message_ko": message,
            "safety_checklist": safety,
            "observe_live_comparison": comparison,
            "items": items[:500],
            "events": all_events[:1000],
            "live_sim_audit": live_audit,
            "promotion_summary": promotion_report.get("summary") or {},
            "disclaimer_ko": "이 리포트는 관측/검증 전용입니다. LIVE_REAL, guard, threshold, order_enabled 기본값을 변경하지 않습니다.",
            "last_updated_at": _last_updated_at(all_events, orders, fills, positions, ops_audit),
        }
        if persist:
            saved = self.db.save_shadow_small_entry_pilot_run(
                {
                    **run,
                    "pilot_id": pilot_id,
                    "trade_date": today,
                    "summary": summary,
                    "recommendation": recommendation,
                    "recommendation_reason_codes": reason_codes,
                    "operator_message_ko": message,
                    "updated_at": report["generated_at"],
                }
            )
            report["run"] = saved
            report["status"] = str(saved.get("status") or report["status"])
        return report

    def items(
        self,
        *,
        trade_date: str | None = None,
        pilot_id: str = "",
        status: str = "",
        recommendation: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        report = self.build_report(trade_date=trade_date, pilot_id=pilot_id, persist=False, limit=50000)
        rows = list(report.get("items") or [])
        if status:
            rows = [row for row in rows if str(row.get("pilot_status") or row.get("promotion_status") or "").upper() == status.upper()]
        if recommendation:
            rows = [row for row in rows if str(row.get("recommendation") or "").upper() == recommendation.upper()]
        total = len(rows)
        sliced = rows[max(0, int(offset or 0)) : max(0, int(offset or 0)) + max(1, int(limit or 200))]
        return {
            "pilot_id": report.get("pilot_id") or "",
            "trade_date": report.get("trade_date") or "",
            "items": sliced,
            "pagination": {"limit": limit, "offset": offset, "count": len(sliced), "total": total},
            "filters": {"status": status, "recommendation": recommendation},
        }

    def export_report(self, report: dict[str, Any], *, fmt: str = "all") -> dict[str, str]:
        trade_date = str(report.get("trade_date") or self._today())
        target = self.report_root / trade_date
        target.mkdir(parents=True, exist_ok=True)
        stem = f"shadow_small_entry_pilot_{trade_date}"
        normalized = "md" if fmt == "markdown" else fmt
        exports: dict[str, str] = {}
        if normalized in {"json", "all"}:
            path = target / f"{stem}.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
            exports["json"] = str(path)
        if normalized in {"csv", "all"}:
            path = target / f"{stem}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=_item_csv_columns())
                writer.writeheader()
                for row in report.get("items") or []:
                    writer.writerow({key: _csv_value(row.get(key)) for key in writer.fieldnames})
            exports["csv"] = str(path)
        if normalized in {"md", "all"}:
            path = target / f"{stem}.md"
            path.write_text(_markdown_report(report), encoding="utf-8")
            exports["md"] = str(path)
        return exports

    def _collect_events(
        self,
        *,
        pilot_id: str,
        trade_date: str,
        promotion_report: dict[str, Any],
        live_audit: dict[str, Any],
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        cancels: list[dict[str, Any]],
        ops_audit: list[dict[str, Any]],
        traces: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in promotion_report.get("candidates") or []:
            status = str(row.get("promotion_status") or row.get("final_status") or "").upper()
            event_type = "PROMOTED" if status == "PROMOTED" else "PROMOTION_OBSERVE_ONLY" if status == "OBSERVE_ONLY" else "PROMOTION_BLOCKED" if status == "BLOCKED" else "PROMOTION_CANDIDATE"
            events.append(
                self._event(
                    event_type,
                    pilot_id=pilot_id,
                    trade_date=trade_date,
                    event_at=str(row.get("created_at") or row.get("evaluated_at") or self._now()),
                    source_id=str(row.get("candidate_instance_id") or row.get("code") or event_type),
                    code=row.get("code"),
                    name=row.get("name"),
                    candidate_instance_id=row.get("candidate_instance_id"),
                    theme_name=row.get("theme_name"),
                    reason_group=row.get("reason_group"),
                    reason_code=row.get("reason_code") or row.get("rejected_reason"),
                    gate_status=status,
                    price_location_status=row.get("price_location_status"),
                    stock_role=row.get("stock_role"),
                    reason_codes=row.get("reason_codes") or [],
                    details={"candidate": row},
                    operator_message_ko=_promotion_message(event_type, row),
                )
            )
        for trace in traces:
            stage = str(trace.get("stage") or "")
            if stage in {"SHADOW_SMALL_ENTRY_OPS_ARMED", "SHADOW_SMALL_ENTRY_OPS_ACTIVATED", "SHADOW_SMALL_ENTRY_OPS_PAUSED", "SHADOW_SMALL_ENTRY_OPS_ROLLED_BACK", "SHADOW_SMALL_ENTRY_OPS_RISK_CHECK"}:
                events.append(self._event(_event_type_from_trace_stage(stage, trace), pilot_id=pilot_id, trade_date=trade_date, event_at=str(trace.get("created_at") or self._now()), source_id=str(trace.get("trace_id") or trace.get("id") or stage), code=trace.get("code"), candidate_instance_id=trace.get("candidate_instance_id"), reason_code=trace.get("primary_block_reason"), reason_codes=trace.get("reason_codes") or [], severity="FAIL" if trace.get("pass_fail") == "FAIL" else "OK", details={"trace": trace}, operator_message_ko=str(trace.get("operator_message_ko") or "")))
        for row in ops_audit:
            events.append(self._event(_event_type_from_ops(row), pilot_id=pilot_id, trade_date=trade_date, event_at=str(row.get("created_at") or self._now()), source_id=str(row.get("audit_id") or row.get("id") or ""), severity="OK", reason_code=row.get("reason"), reason_codes=row.get("reason_codes") or [], details={"ops_audit": row}, operator_message_ko=_ops_message(row)))
        for order in orders:
            events.extend(self._order_events(pilot_id=pilot_id, trade_date=trade_date, order=order))
        for fill in fills:
            events.append(self._fill_event(pilot_id=pilot_id, trade_date=trade_date, fill=fill))
            if str(fill.get("side") or "").lower() == "sell":
                events.append(self._fill_event(pilot_id=pilot_id, trade_date=trade_date, fill=fill, event_type="EXIT_FILLED"))
        for cancel in cancels:
            events.append(self._cancel_event(pilot_id=pilot_id, trade_date=trade_date, cancel=cancel))
        for position in positions:
            events.append(self._position_event(pilot_id=pilot_id, trade_date=trade_date, position=position))
        for issue in live_audit.get("issues") or []:
            severity = str(issue.get("severity") or "").upper()
            event_type = "AUDIT_BROKEN" if severity == "BROKEN" else "RECONCILE_REQUIRED" if severity == "RECONCILE_REQUIRED" else "AUDIT_WARNING"
            events.append(
                self._event(
                    event_type,
                    pilot_id=pilot_id,
                    trade_date=trade_date,
                    event_at=self._now(),
                    source_id=str(issue.get("issue_type") or issue.get("order_intent_id") or event_type),
                    code=issue.get("code"),
                    order_intent_id=issue.get("order_intent_id"),
                    broker_order_id=issue.get("broker_order_id"),
                    position_id=issue.get("position_id"),
                    reason_code=issue.get("issue_type"),
                    reason_codes=[issue.get("issue_type")],
                    severity=severity or "WARN",
                    details={"live_sim_issue": issue},
                    operator_message_ko=str(issue.get("operator_message_ko") or ""),
                )
            )
        return [event for event in events if str(event.get("event_type") or "") in PILOT_EVENT_TYPES]

    def _order_events(self, *, pilot_id: str, trade_date: str, order: dict[str, Any]) -> list[dict[str, Any]]:
        status = str(order.get("order_status") or "").upper()
        source_id = str(order.get("order_intent_id") or order.get("command_id") or "")
        base = {
            "pilot_id": pilot_id,
            "trade_date": trade_date,
            "event_at": str(order.get("updated_at") or order.get("accepted_at") or order.get("submitted_at") or self._now()),
            "source_id": source_id,
            "code": order.get("code"),
            "name": order.get("name"),
            "candidate_instance_id": order.get("candidate_instance_id"),
            "order_intent_id": order.get("order_intent_id"),
            "live_sim_order_intent_id": order.get("order_intent_id"),
            "command_id": order.get("command_id"),
            "broker_order_id": order.get("broker_order_id"),
            "quantity": int(order.get("submitted_qty") or order.get("requested_qty") or 0),
            "price": float(order.get("submitted_price") or order.get("requested_price") or 0),
            "notional_krw": float(int(order.get("submitted_qty") or order.get("requested_qty") or 0) * int(order.get("submitted_price") or order.get("requested_price") or 0)),
            "reason_codes": order.get("reason_codes") or [],
            "details": {"order": order},
        }
        if str(order.get("side") or "").lower() == "sell":
            events = [self._event("EXIT_ORDER_SUBMITTED", **base, operator_message_ko="청산 주문이 LIVE_SIM으로 제출됐습니다.")]
            if order.get("exit_decision_id"):
                events.insert(0, self._event("EXIT_DECISION_CREATED", **base, operator_message_ko="청산 결정이 생성됐습니다."))
            return events
        if status in {"BLOCKED", "DUPLICATE", "REJECTED", "FAILED"}:
            return [self._event("ORDER_BLOCKED", **base, severity="FAIL", reason_code=_first_reason(order, status), operator_message_ko="Shadow Small Entry 주문이 guard에서 차단됐습니다.")]
        events = [self._event("ORDER_SUBMITTED", **base, operator_message_ko="Shadow Small Entry LIVE_SIM 주문 command가 제출됐습니다.")]
        if status in {"ACCEPTED", "PARTIAL_FILLED", "FILLED", "CANCEL_REQUESTED", "CANCELLED"}:
            events.append(self._event("ORDER_ACCEPTED", **base, operator_message_ko="브로커 주문번호가 연결됐거나 주문 접수가 확인됐습니다."))
        if status == "PARTIAL_FILLED":
            events.append(self._event("PARTIAL_FILLED", **base, operator_message_ko="부분체결이 확인됐습니다."))
        if status == "FILLED":
            events.append(self._event("FILLED", **base, operator_message_ko="완전체결이 확인됐습니다."))
        if status == "CANCEL_REQUESTED":
            events.append(self._event("CANCEL_REQUESTED", **base, operator_message_ko="미체결 또는 잔량 취소 요청이 생성됐습니다."))
        if status == "CANCELLED":
            events.append(self._event("CANCELLED", **base, operator_message_ko="취소 결과가 확인됐습니다."))
        if status == "RECONCILE_REQUIRED":
            events.append(self._event("RECONCILE_REQUIRED", **base, severity="RECONCILE_REQUIRED", operator_message_ko="주문 상태 확인을 위한 reconcile이 필요합니다."))
        return events

    def _fill_event(self, *, pilot_id: str, trade_date: str, fill: dict[str, Any], event_type: str = "") -> dict[str, Any]:
        fill_type = event_type or ("FILLED" if int(fill.get("remaining_qty") or 0) <= 0 else "PARTIAL_FILLED")
        return self._event(
            fill_type,
            pilot_id=pilot_id,
            trade_date=trade_date,
            event_at=str(fill.get("received_at") or fill.get("event_time") or self._now()),
            source_id=str(fill.get("fill_id") or fill.get("event_id") or ""),
            code=fill.get("code"),
            candidate_instance_id=fill.get("candidate_instance_id"),
            order_intent_id=fill.get("order_intent_id"),
            broker_order_id=fill.get("broker_order_id"),
            quantity=int(fill.get("fill_qty") or 0),
            price=float(fill.get("fill_price") or 0),
            notional_krw=float(fill.get("fill_amount") or 0),
            details={"fill": fill},
            operator_message_ko="체결 이벤트가 파일럿 리포트에 연결됐습니다.",
        )

    def _cancel_event(self, *, pilot_id: str, trade_date: str, cancel: dict[str, Any]) -> dict[str, Any]:
        status = str(cancel.get("status") or "").upper()
        event_type = "CANCELLED" if status in {"CANCELLED", "ACCEPTED"} else "CANCEL_REQUESTED"
        return self._event(
            event_type,
            pilot_id=pilot_id,
            trade_date=trade_date,
            event_at=str(cancel.get("updated_at") or cancel.get("submitted_at") or cancel.get("created_at") or self._now()),
            source_id=str(cancel.get("cancel_intent_id") or ""),
            code=cancel.get("code"),
            candidate_instance_id=cancel.get("candidate_instance_id"),
            order_intent_id=cancel.get("original_order_id"),
            command_id=cancel.get("command_id"),
            broker_order_id=cancel.get("broker_order_id"),
            quantity=int(cancel.get("cancel_qty") or 0),
            reason_code=cancel.get("cancel_reason"),
            reason_codes=cancel.get("reason_codes") or [],
            details={"cancel": cancel},
            operator_message_ko="취소 lifecycle 이벤트가 파일럿 리포트에 연결됐습니다.",
        )

    def _position_event(self, *, pilot_id: str, trade_date: str, position: dict[str, Any]) -> dict[str, Any]:
        event_type = "POSITION_CLOSED" if str(position.get("status") or "").upper() == "CLOSED" else "POSITION_OPENED"
        return self._event(
            event_type,
            pilot_id=pilot_id,
            trade_date=trade_date,
            event_at=str(position.get("updated_at") or position.get("closed_at") or position.get("opened_at") or self._now()),
            source_id=str(position.get("position_id") or ""),
            code=position.get("code"),
            name=position.get("name"),
            candidate_instance_id=position.get("candidate_instance_id"),
            position_id=position.get("position_id"),
            quantity=int(position.get("current_qty") or position.get("entry_qty") or 0),
            price=float(position.get("entry_avg_price") or 0),
            realized_pnl_krw=float(position.get("realized_pnl") or 0),
            unrealized_pnl_krw=float(position.get("unrealized_pnl") or 0),
            return_pct=float(position.get("realized_pnl_pct") or position.get("unrealized_pnl_pct") or 0),
            details={"position": position},
            operator_message_ko="파일럿 포지션 상태가 주문/체결 결과와 연결됐습니다.",
        )

    def _candidate_items(
        self,
        *,
        promotion_report: dict[str, Any],
        traces: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        cancels: list[dict[str, Any]],
        live_audit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = _candidate_seed_rows(promotion_report, traces)
        if not candidates and orders:
            candidates = [_candidate_from_order(order) for order in orders]
        orders_by_key = _rows_by_candidate_key(orders)
        fills_by_order = defaultdict(list)
        for fill in fills:
            fills_by_order[str(fill.get("order_intent_id") or "")].append(fill)
        positions_by_key = _rows_by_candidate_key(positions)
        cancel_by_order = defaultdict(list)
        for cancel in cancels:
            cancel_by_order[str(cancel.get("original_order_id") or "")].append(cancel)
        issue_by_order = defaultdict(list)
        for issue in live_audit.get("issues") or []:
            issue_by_order[str(issue.get("order_intent_id") or "")].append(issue)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = _candidate_key(candidate)
            matched_orders = orders_by_key.get(key) or orders_by_key.get(("code", str(candidate.get("code") or ""))) or []
            matched_positions = positions_by_key.get(key) or positions_by_key.get(("code", str(candidate.get("code") or ""))) or []
            row = _item_from_candidate(candidate)
            if matched_orders:
                order = matched_orders[0]
                row.update(_order_item_fields(order))
                order_fills = fills_by_order.get(str(order.get("order_intent_id") or ""), [])
                row.update(_fill_item_fields(order_fills))
                row["cancel_count"] = len(cancel_by_order.get(str(order.get("order_intent_id") or ""), []))
                row["audit_issues"] = issue_by_order.get(str(order.get("order_intent_id") or ""), [])
            if matched_positions:
                row.update(_position_item_fields(matched_positions[0]))
            row["pilot_status"] = _pilot_item_status(row)
            row["recommendation"] = _item_recommendation(row)
            seen.add(str(row.get("candidate_instance_id") or row.get("code") or len(rows)))
            rows.append(row)
        for order in orders:
            key = str(order.get("candidate_instance_id") or order.get("code") or "")
            if key and key in seen:
                continue
            row = _candidate_from_order(order)
            row.update(_order_item_fields(order))
            order_fills = fills_by_order.get(str(order.get("order_intent_id") or ""), [])
            row.update(_fill_item_fields(order_fills))
            row["audit_issues"] = issue_by_order.get(str(order.get("order_intent_id") or ""), [])
            row["pilot_status"] = _pilot_item_status(row)
            row["recommendation"] = _item_recommendation(row)
            rows.append(row)
        rows.sort(key=lambda row: (str(row.get("pilot_status") or ""), str(row.get("code") or "")))
        return rows

    def _summary(
        self,
        *,
        run: dict[str, Any],
        items: list[dict[str, Any]],
        events: list[dict[str, Any]],
        promotion_report: dict[str, Any],
        live_audit: dict[str, Any],
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        cancels: list[dict[str, Any]],
        ops_audit: list[dict[str, Any]],
    ) -> dict[str, Any]:
        order_counts = Counter(str(order.get("order_status") or "") for order in orders)
        event_counts = Counter(str(event.get("event_type") or "") for event in events)
        live_summary = dict(live_audit.get("summary") or {})
        returns = [_float(row.get("return_pct")) for row in items if row.get("return_pct") is not None]
        mfe_values = [_float(row.get("mfe_15m_pct", row.get("max_favorable_excursion_pct"))) for row in items if row.get("mfe_15m_pct") is not None or row.get("max_favorable_excursion_pct") is not None]
        mae_values = [_float(row.get("mae_15m_pct", row.get("max_adverse_excursion_pct"))) for row in items if row.get("mae_15m_pct") is not None or row.get("max_adverse_excursion_pct") is not None]
        realized = sum(float(position.get("realized_pnl") or 0) for position in positions)
        unrealized = sum(float(position.get("unrealized_pnl") or 0) for position in positions)
        promotion_summary = dict(promotion_report.get("summary") or {})
        loss_order = [value for value in returns if value < 0]
        win_order = [value for value in returns if value > 0]
        summary = {
            "pilot_id": str(run.get("pilot_id") or ""),
            "trade_date": str(run.get("trade_date") or ""),
            "status": str(run.get("status") or STATUS_PLANNED),
            "started_at": str(run.get("started_at") or ""),
            "ended_at": str(run.get("ended_at") or ""),
            "duration_min": _duration_min(run.get("started_at"), run.get("ended_at") or self._now()),
            "candidate_count": max(len(items), int(promotion_summary.get("candidate_count") or 0), event_counts.get("PROMOTION_CANDIDATE", 0)),
            "observe_only_candidate_count": max(int(promotion_summary.get("observe_only_count") or 0), event_counts.get("PROMOTION_OBSERVE_ONLY", 0)),
            "promoted_count": max(int(promotion_summary.get("promoted_count") or 0), event_counts.get("PROMOTED", 0)),
            "blocked_count": max(int(promotion_summary.get("blocked_count") or 0), event_counts.get("PROMOTION_BLOCKED", 0), order_counts.get("BLOCKED", 0)),
            "submitted_order_count": len([order for order in orders if str(order.get("order_status") or "") not in {"BLOCKED", "DUPLICATE", "REJECTED", "FAILED"}]),
            "accepted_order_count": sum(order_counts.get(status, 0) for status in {"ACCEPTED", "PARTIAL_FILLED", "FILLED", "CANCEL_REQUESTED", "CANCELLED"}),
            "rejected_order_count": order_counts.get("REJECTED", 0) + order_counts.get("FAILED", 0),
            "duplicate_block_count": order_counts.get("DUPLICATE", 0),
            "filled_order_count": order_counts.get("FILLED", 0) or len([row for row in items if int(row.get("fill_qty") or 0) > 0 and int(row.get("remaining_qty") or 0) == 0]),
            "partial_fill_count": order_counts.get("PARTIAL_FILLED", 0),
            "cancelled_order_count": order_counts.get("CANCELLED", 0) + len([cancel for cancel in cancels if str(cancel.get("status") or "") in {"CANCELLED", "ACCEPTED"}]),
            "open_position_count": len([position for position in positions if str(position.get("status") or "").upper() in {"OPEN", "PARTIAL", "EXIT_ORDERED", "RECONCILE_REQUIRED"}]),
            "closed_position_count": len([position for position in positions if str(position.get("status") or "").upper() == "CLOSED"]),
            "total_notional_krw": sum(float(row.get("notional_krw") or 0) for row in items) or sum(int(order.get("submitted_qty") or order.get("requested_qty") or 0) * int(order.get("submitted_price") or order.get("requested_price") or 0) for order in orders),
            "realized_pnl_krw": realized,
            "unrealized_pnl_krw": unrealized,
            "total_pnl_krw": realized + unrealized,
            "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "win_count": len(win_order),
            "loss_count": len(loss_order),
            "win_rate": round(len(win_order) / len(returns), 4) if returns else None,
            "max_mfe_pct": max(mfe_values) if mfe_values else None,
            "max_mae_pct": min(mae_values) if mae_values else None,
            "avg_mfe_pct": round(sum(mfe_values) / len(mfe_values), 4) if mfe_values else None,
            "avg_mae_pct": round(sum(mae_values) / len(mae_values), 4) if mae_values else None,
            "max_drawdown_pct": min(mae_values or [0]),
            "consecutive_losing_trades": _consecutive_losses(returns),
            "auto_pause_count": event_counts.get("AUTO_PAUSED", 0),
            "operator_pause_count": event_counts.get("OPERATOR_PAUSED", 0),
            "rollback_count": event_counts.get("ROLLED_BACK", 0),
            "reconcile_required_count": int(live_summary.get("reconcile_required_order_count") or 0) + event_counts.get("RECONCILE_REQUIRED", 0),
            "audit_warning_count": event_counts.get("AUDIT_WARNING", 0),
            "audit_broken_count": event_counts.get("AUDIT_BROKEN", 0) + int((live_summary.get("severity_counts") or {}).get("BROKEN", 0)),
            "unknown_submit_count": int(live_summary.get("unknown_submit_count") or 0) + order_counts.get("UNKNOWN_SUBMIT", 0),
            "cancel_requested_stale_count": int(live_summary.get("cancel_requested_stale_count") or 0),
            "exit_guard_block_count": _count_reason(events, "EXIT_GUARD"),
            "kill_switch_block_count": _count_reason(events, "KILL_SWITCH"),
            "gateway_disconnect_count": _count_reason(events, "GATEWAY_DISCONNECTED"),
        }
        return _summary_defaults(summary)

    def _recommend(
        self,
        summary: dict[str, Any],
        *,
        run: dict[str, Any],
        promotion_report: dict[str, Any],
        live_audit: dict[str, Any],
    ) -> tuple[str, list[str], str]:
        reasons: list[str] = []
        if int(summary.get("audit_broken_count") or 0) > 0 or str(live_audit.get("status") or "") == "BROKEN":
            reasons.append("LIVE_SIM_AUDIT_BROKEN")
            return RECOMMEND_ROLLBACK_TO_OBSERVE_ONLY, reasons, "LIVE_SIM audit가 BROKEN입니다. observe_only로 되돌리고 원인 확인이 필요합니다."
        if int(summary.get("reconcile_required_count") or 0) > 0:
            reasons.append("RECONCILE_REQUIRED")
            return RECOMMEND_INVESTIGATE_RECONCILE, reasons, "주문/체결/포지션 reconcile이 필요합니다."
        if int(summary.get("unknown_submit_count") or 0) > 0 or int(summary.get("rejected_order_count") or 0) > 0:
            reasons.append("ORDER_LIFECYCLE_ISSUE")
            return RECOMMEND_INVESTIGATE_ORDER_LIFECYCLE, reasons, "UNKNOWN_SUBMIT 또는 주문 거절이 있어 주문 lifecycle 확인이 필요합니다."
        if int(summary.get("exit_guard_block_count") or 0) > 0:
            reasons.append("EXIT_GUARD_BLOCK")
            return RECOMMEND_INVESTIGATE_EXIT_LOGIC, reasons, "청산 guard 관련 차단이 있어 exit logic 확인이 필요합니다."
        if int(summary.get("filled_order_count") or 0) < 3:
            reasons.append("FILLED_SAMPLE_INSUFFICIENT")
            if not bool(run.get("order_enabled_at_start")):
                reasons.append("ORDER_ENABLED_FALSE_AT_START")
            return RECOMMEND_CONTINUE_OBSERVE_ONLY, reasons, "체결 표본이 아직 부족합니다. observe_only 또는 guarded 상태에서 추가 관측하세요."
        if float(summary.get("total_pnl_krw") or 0) < 0 or int(summary.get("consecutive_losing_trades") or 0) > 1:
            reasons.append("LOSS_LIMIT_REVIEW")
            return RECOMMEND_REDUCE_SIZE, reasons, "손실 또는 연속 손실이 있어 size 축소 검토가 필요합니다."
        if (summary.get("win_rate") is not None and float(summary.get("win_rate") or 0) >= 0.5) and float(summary.get("avg_mae_pct") or 0) >= -1.5:
            reasons.append("PILOT_HEALTHY")
            return RECOMMEND_CONTINUE_LIVE_SIM_GUARDED, reasons, "주문 lifecycle과 수익/MAE 지표가 안정적입니다. guarded 상태의 추가 파일럿을 검토할 수 있습니다."
        if not promotion_report.get("available") and int(summary.get("candidate_count") or 0) <= 0:
            reasons.append("DATA_QUALITY_OR_PROMOTION_EVIDENCE_NOT_READY")
            return RECOMMEND_INVESTIGATE_DATA_QUALITY, reasons, "promotion evidence 또는 후보 데이터가 부족합니다."
        reasons.append("KEEP_GUARDED_OBSERVATION")
        return RECOMMEND_CONTINUE_OBSERVE_ONLY, reasons, "큰 lifecycle 문제는 없지만 추가 관측이 필요합니다."

    def _safety_checks(self, *, summary: dict[str, Any], events: list[dict[str, Any]], live_audit: dict[str, Any]) -> list[dict[str, Any]]:
        def check(key: str, status: str, message: str, evidence: Iterable[str] = ()) -> dict[str, Any]:
            return {
                "check_id": key,
                "status": status,
                "evidence_event_ids": list(evidence),
                "operator_message_ko": message,
            }

        event_by_type = defaultdict(list)
        for event in events:
            event_by_type[str(event.get("event_type") or "")].append(str(event.get("event_id") or ""))
        return [
            check("live_real_disabled", "PASS", "LIVE_REAL 주문 활성화는 이 파일럿 리포트에서 변경하지 않습니다."),
            check(
                "order_lifecycle_audit_clean",
                "FAIL" if int(summary.get("audit_broken_count") or 0) else "WARN" if int(summary.get("audit_warning_count") or 0) else "PASS",
                "LIVE_SIM 주문 lifecycle audit 경고/깨짐 여부입니다.",
                event_by_type.get("AUDIT_BROKEN", []) + event_by_type.get("AUDIT_WARNING", []),
            ),
            check(
                "reconcile_clean",
                "WARN" if int(summary.get("reconcile_required_count") or 0) else "PASS",
                "reconcile required 주문/체결/포지션 여부입니다.",
                event_by_type.get("RECONCILE_REQUIRED", []),
            ),
            check(
                "unknown_submit_zero",
                "WARN" if int(summary.get("unknown_submit_count") or 0) else "PASS",
                "주문번호 없는 accepted/unknown submit 여부입니다.",
            ),
            check(
                "cancel_stale_zero",
                "WARN" if int(summary.get("cancel_requested_stale_count") or 0) else "PASS",
                "취소 요청 장기 미확인 여부입니다.",
            ),
            check(
                "loss_limit",
                "WARN" if float(summary.get("total_pnl_krw") or 0) < 0 else "PASS",
                "파일럿 손익이 손실 구간인지 확인합니다.",
            ),
            check(
                "exit_guard_preserved",
                "WARN" if int(summary.get("exit_guard_block_count") or 0) else "PASS",
                "기존 exit guard/청산 로직 보존 상태입니다.",
            ),
            check(
                "gateway_health",
                "WARN" if int(summary.get("gateway_disconnect_count") or 0) else "PASS",
                "파일럿 중 gateway disconnect 기록 여부입니다.",
            ),
            check(
                "filled_sample",
                "NOT_TESTED" if int(summary.get("filled_order_count") or 0) <= 0 else "PASS",
                "체결 표본이 실제로 발생했는지 확인합니다.",
            ),
            check(
                "auto_pause_validation",
                "NOT_TESTED" if int(summary.get("auto_pause_count") or 0) <= 0 else "PASS",
                "자동 중단 조건이 발생했을 때 이벤트로 남는지 확인합니다.",
                event_by_type.get("AUTO_PAUSED", []),
            ),
        ]

    def _implicit_run(self, trade_date: str) -> dict[str, Any]:
        settings = StrategyRuntimeSettingsRepository(self.db).load()
        promotion = dict(settings.value("shadow_small_entry_promotion", {}) or {})
        ops = dict(settings.value("shadow_small_entry_ops", {}) or {})
        cfg = ShadowSmallEntryOpsConfig.from_settings(settings)
        now = self._now()
        return {
            "pilot_id": self.pilot_id_for_date(trade_date),
            "trade_date": trade_date,
            "created_at": now,
            "started_at": "",
            "status": STATUS_PLANNED,
            "mode": str(promotion.get("mode") or "observe_only"),
            "order_enabled_at_start": bool(promotion.get("order_enabled")),
            "runtime_settings_hash": _hash_payload(settings.settings_json),
            "promotion_policy_snapshot": promotion,
            "ops_policy_snapshot": ops,
            "risk_limit_snapshot": {"daily_limits": cfg.daily_limits, "risk_limits": cfg.risk_limits},
            "operator_message_ko": "파일럿 run이 아직 시작되지 않았습니다. 리포트는 현재 원장 기준으로 미리보기만 생성됩니다.",
        }

    def _promotion_report(self, trade_date: str, *, limit: int) -> dict[str, Any]:
        try:
            return ShadowSmallEntryPromotionAnalyzer(self.db).build_report(trade_date=trade_date, limit=limit, include_traces=True)
        except Exception as exc:
            return {"available": False, "status": "ERROR", "summary": {}, "candidates": [], "traces": [], "warnings": [str(exc)]}

    def _live_audit(self, trade_date: str, *, limit: int) -> dict[str, Any]:
        try:
            return LiveSimLifecycleAuditor(self.db, gateway_state=self.gateway_state).build_report(trade_date=trade_date, limit=min(limit, 5000))
        except Exception as exc:
            return {"available": False, "status": "ERROR", "summary": {}, "issues": [], "operator": {"top_actions": []}, "error": str(exc)}

    def _event(self, event_type: str, *, pilot_id: str, trade_date: str, event_at: str = "", source_id: str = "", **payload: Any) -> dict[str, Any]:
        details = dict(payload.pop("details", {}) or {})
        details.setdefault("source_id", source_id)
        raw = {
            "pilot_id": pilot_id,
            "trade_date": trade_date,
            "event_type": event_type,
            "event_at": event_at or self._now(),
            **payload,
            "details": details,
        }
        raw["event_id"] = _stable_event_id(pilot_id, event_type, source_id or json.dumps(raw, sort_keys=True, default=str))
        return raw

    def _today(self) -> str:
        return self.now_provider().date().isoformat()

    def _now(self) -> str:
        return self.now_provider().isoformat(timespec="seconds")


def snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    data.setdefault("available", bool(data.get("pilot_id")))
    data["summary"] = _summary_defaults(dict(data.get("summary") or {}))
    return data


def empty_payload(trade_date: str = "", error: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "pilot_id": "",
        "trade_date": trade_date,
        "status": "NO_DATA",
        "mode": "observe_only",
        "order_enabled_at_start": False,
        "started_at": "",
        "ended_at": "",
        "recommendation": "",
        "recommendation_reason_codes": [],
        "summary": _summary_defaults({}),
        "operator_message_ko": error or "아직 Shadow Small Entry pilot run 데이터가 없습니다. PR 적용 이후 이벤트부터 쌓입니다.",
        "last_updated_at": "",
    }


def _summary_defaults(summary: dict[str, Any]) -> dict[str, Any]:
    payload = {key: 0 for key in SUMMARY_KEYS}
    payload.update(summary or {})
    for key in ("avg_return_pct", "win_rate", "max_mfe_pct", "max_mae_pct", "avg_mfe_pct", "avg_mae_pct"):
        payload.setdefault(key, None)
    return payload


def _candidate_seed_rows(promotion_report: dict[str, Any], traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in promotion_report.get("candidates") or []]
    seen = {str(row.get("candidate_instance_id") or row.get("code") or "") for row in rows}
    for trace in traces:
        stage = str(trace.get("stage") or "")
        if not stage.startswith("SHADOW_SMALL_ENTRY") and stage not in {"CANDIDATE_GENERATED", "HYBRID_GATE_EVALUATED"}:
            continue
        key = str(trace.get("candidate_instance_id") or trace.get("code") or "")
        if not key or key in seen:
            continue
        rows.append(
            {
                "trade_date": trace.get("trade_date"),
                "code": trace.get("code"),
                "name": trace.get("name"),
                "candidate_instance_id": trace.get("candidate_instance_id"),
                "theme_name": trace.get("theme_name"),
                "promotion_status": trace.get("stage_status") or trace.get("gate_status") or "",
                "reason_codes": trace.get("reason_codes") or [],
                "reason_code": trace.get("primary_block_reason") or "",
                "stock_role": trace.get("stock_role") or "",
                "price_location_status": trace.get("price_location_status") or "",
            }
        )
        seen.add(key)
    return rows


def _item_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": str(candidate.get("trade_date") or ""),
        "code": str(candidate.get("code") or ""),
        "name": str(candidate.get("name") or ""),
        "candidate_instance_id": str(candidate.get("candidate_instance_id") or ""),
        "theme_name": str(candidate.get("theme_name") or ""),
        "promotion_status": str(candidate.get("promotion_status") or candidate.get("final_status") or ""),
        "reason_group": str(candidate.get("reason_group") or candidate.get("primary_group") or ""),
        "reason_code": str(candidate.get("reason_code") or candidate.get("rejected_reason") or ""),
        "reason_codes": list(candidate.get("reason_codes") or []),
        "gate_status": str(candidate.get("gate_status") or ""),
        "stock_role": str(candidate.get("stock_role") or ""),
        "price_location_status": str(candidate.get("price_location_status") or ""),
        "base_price": candidate.get("base_price") or candidate.get("current_price"),
        "mfe_15m_pct": candidate.get("mfe_15m_pct") or candidate.get("avg_mfe_15m_pct"),
        "mae_15m_pct": candidate.get("mae_15m_pct") or candidate.get("avg_mae_15m_pct"),
        "missed_opportunity": bool(candidate.get("missed_opportunity")),
        "good_block": bool(candidate.get("good_block")),
    }


def _candidate_from_order(order: Mapping[str, Any]) -> dict[str, Any]:
    details = dict(order.get("details") or {})
    return {
        "trade_date": str(order.get("trade_date") or ""),
        "code": str(order.get("code") or ""),
        "name": str(order.get("name") or ""),
        "candidate_instance_id": str(order.get("candidate_instance_id") or ""),
        "theme_name": str(details.get("theme_name") or ""),
        "promotion_status": str(details.get("shadow_small_entry_promotion_status") or details.get("ready_type") or ""),
        "reason_codes": list(order.get("reason_codes") or []),
    }


def _order_item_fields(order: Mapping[str, Any]) -> dict[str, Any]:
    qty = int(order.get("submitted_qty") or order.get("requested_qty") or 0)
    price = int(order.get("submitted_price") or order.get("requested_price") or 0)
    return {
        "order_intent_id": str(order.get("order_intent_id") or ""),
        "command_id": str(order.get("command_id") or ""),
        "broker_order_id": str(order.get("broker_order_id") or ""),
        "side": str(order.get("side") or ""),
        "order_status": str(order.get("order_status") or ""),
        "requested_qty": int(order.get("requested_qty") or 0),
        "submitted_qty": qty,
        "submitted_price": price,
        "notional_krw": qty * price,
        "order_updated_at": str(order.get("updated_at") or order.get("submitted_at") or ""),
    }


def _fill_item_fields(fills: list[dict[str, Any]]) -> dict[str, Any]:
    if not fills:
        return {"fill_qty": 0, "cumulative_fill_qty": 0, "remaining_qty": None}
    rows = sorted(fills, key=lambda row: str(row.get("received_at") or row.get("event_time") or ""))
    latest = rows[-1]
    return {
        "fill_qty": sum(int(row.get("fill_qty") or 0) for row in rows),
        "cumulative_fill_qty": int(latest.get("cumulative_fill_qty") or 0),
        "remaining_qty": int(latest.get("remaining_qty") or 0),
        "avg_fill_price": round(sum(int(row.get("fill_qty") or 0) * int(row.get("fill_price") or 0) for row in rows) / max(1, sum(int(row.get("fill_qty") or 0) for row in rows)), 2),
        "last_fill_at": str(latest.get("received_at") or latest.get("event_time") or ""),
    }


def _position_item_fields(position: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "position_id": str(position.get("position_id") or ""),
        "position_status": str(position.get("status") or ""),
        "position_qty": int(position.get("current_qty") or 0),
        "realized_pnl_krw": float(position.get("realized_pnl") or 0),
        "unrealized_pnl_krw": float(position.get("unrealized_pnl") or 0),
        "return_pct": float(position.get("realized_pnl_pct") or position.get("unrealized_pnl_pct") or 0),
        "max_favorable_excursion_pct": float(position.get("max_favorable_excursion_pct") or 0),
        "max_adverse_excursion_pct": float(position.get("max_adverse_excursion_pct") or 0),
    }


def _pilot_item_status(row: Mapping[str, Any]) -> str:
    order_status = str(row.get("order_status") or "").upper()
    if order_status:
        if order_status in {"BLOCKED", "DUPLICATE", "REJECTED", "FAILED"}:
            return "ORDER_BLOCKED"
        if order_status == "FILLED":
            return "FILLED"
        return "ORDER_SUBMITTED"
    status = str(row.get("promotion_status") or "").upper()
    if status in {"PROMOTED", "READY_SHADOW_SMALL_ENTRY"}:
        return "PROMOTED_NOT_ORDERED"
    if status == "OBSERVE_ONLY":
        return "OBSERVE_ONLY"
    if status == "BLOCKED":
        return "PROMOTION_BLOCKED"
    return status or "CANDIDATE"


def _item_recommendation(row: Mapping[str, Any]) -> str:
    status = str(row.get("pilot_status") or "")
    order_status = str(row.get("order_status") or "")
    if order_status in {"UNKNOWN_SUBMIT", "RECONCILE_REQUIRED"}:
        return RECOMMEND_INVESTIGATE_RECONCILE
    if order_status in {"REJECTED", "FAILED"}:
        return RECOMMEND_INVESTIGATE_ORDER_LIFECYCLE
    if status == "FILLED":
        return RECOMMEND_CONTINUE_LIVE_SIM_GUARDED
    if status in {"OBSERVE_ONLY", "PROMOTED_NOT_ORDERED"}:
        return RECOMMEND_CONTINUE_OBSERVE_ONLY
    return RECOMMEND_KEEP_DISABLED


def _observe_live_comparison(items: list[dict[str, Any]]) -> dict[str, Any]:
    observe = [row for row in items if str(row.get("pilot_status") or "") in {"OBSERVE_ONLY", "PROMOTED_NOT_ORDERED", "PROMOTION_BLOCKED"}]
    live = [row for row in items if row.get("order_intent_id")]
    return {
        "observe_only_count": len(observe),
        "live_sim_count": len(live),
        "observe_avg_mfe_15m_pct": _avg([row.get("mfe_15m_pct") for row in observe]),
        "observe_avg_mae_15m_pct": _avg([row.get("mae_15m_pct") for row in observe]),
        "live_avg_return_pct": _avg([row.get("return_pct") for row in live]),
        "live_win_rate": _rate([row for row in live if row.get("return_pct") is not None], lambda row: float(row.get("return_pct") or 0) > 0),
    }


def _rows_by_candidate_key(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        candidate_id = str(row.get("candidate_instance_id") or "")
        code = str(row.get("code") or "")
        if candidate_id:
            grouped[("candidate", candidate_id)].append(dict(row))
        if code:
            grouped[("code", code)].append(dict(row))
    return grouped


def _candidate_key(row: Mapping[str, Any]) -> tuple[str, str]:
    candidate_id = str(row.get("candidate_instance_id") or "")
    return ("candidate", candidate_id) if candidate_id else ("code", str(row.get("code") or ""))


def _is_shadow_order(row: Mapping[str, Any]) -> bool:
    details = dict(row.get("details") or {})
    values = [
        details.get("ready_type"),
        details.get("order_eligibility"),
        details.get("shadow_small_entry_promotion_status"),
        details.get("shadow_small_entry_promotion"),
        row.get("order_eligibility"),
    ]
    return any(str(value or "").upper() in {"READY_SHADOW_SMALL_ENTRY", "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED", "PROMOTED", "TRUE"} for value in values)


def _is_shadow_position(row: Mapping[str, Any]) -> bool:
    details = dict(row.get("details") or {})
    return bool(details.get("shadow_small_entry_promotion") or details.get("shadow_small_entry_promotion_status"))


def _fill_is_shadow(fill: Mapping[str, Any], orders: list[dict[str, Any]]) -> bool:
    order_ids = {str(order.get("order_intent_id") or "") for order in orders}
    broker_ids = {str(order.get("broker_order_id") or "") for order in orders if order.get("broker_order_id")}
    return str(fill.get("order_intent_id") or "") in order_ids or str(fill.get("broker_order_id") or "") in broker_ids


def _position_matches_orders(position: Mapping[str, Any], orders: list[dict[str, Any]]) -> bool:
    keys = {(str(order.get("code") or ""), str(order.get("candidate_instance_id") or "")) for order in orders}
    return (str(position.get("code") or ""), str(position.get("candidate_instance_id") or "")) in keys


def _cancel_matches_orders(cancel: Mapping[str, Any], orders: list[dict[str, Any]]) -> bool:
    order_ids = {str(order.get("order_intent_id") or "") for order in orders}
    return str(cancel.get("original_order_id") or "") in order_ids


def _event_type_from_trace_stage(stage: str, trace: Mapping[str, Any]) -> str:
    if stage.endswith("ARMED"):
        return "ARMED"
    if stage.endswith("ACTIVATED"):
        return "ACTIVATED"
    if stage.endswith("ROLLED_BACK"):
        return "ROLLED_BACK"
    if stage.endswith("RISK_CHECK"):
        return "RISK_CHECK"
    if "PAUSED" in stage:
        return "AUTO_PAUSED" if "RISK" in str(trace.get("reason_codes") or "") else "OPERATOR_PAUSED"
    return "AUDIT_WARNING"


def _event_type_from_ops(row: Mapping[str, Any]) -> str:
    event_type = str(row.get("event_type") or "").lower()
    next_status = str(row.get("next_status") or "").upper()
    reason = str(row.get("reason") or "").upper()
    if event_type == "arm":
        return "ARMED"
    if event_type == "confirm":
        return "ACTIVATED"
    if event_type == "rollback":
        return "ROLLED_BACK"
    if event_type == "risk_check":
        return "RISK_CHECK"
    if event_type == "pause":
        return "AUTO_PAUSED" if "RISK" in next_status or "AUTO" in reason else "OPERATOR_PAUSED"
    return "AUDIT_WARNING"


def _promotion_message(event_type: str, row: Mapping[str, Any]) -> str:
    if event_type == "PROMOTED":
        return "Shadow Small Entry 승격 후보입니다. 주문 가능 여부는 ops/preflight/live_sim guard가 별도로 결정합니다."
    if event_type == "PROMOTION_OBSERVE_ONLY":
        return "좋은 후보지만 observe_only 설정 또는 guard 때문에 주문하지 않고 추적합니다."
    if event_type == "PROMOTION_BLOCKED":
        return "Shadow Small Entry 승격 guard에서 차단됐습니다."
    return "Shadow Small Entry 파일럿 후보가 관측됐습니다."


def _ops_message(row: Mapping[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    if event_type == "arm":
        return "운영자가 LIVE_SIM guarded 활성화를 준비했습니다. 아직 order_enabled를 자동 변경하지 않습니다."
    if event_type == "confirm":
        return "운영자가 LIVE_SIM guarded를 확인했습니다."
    if event_type == "pause":
        return "파일럿 신규 진입이 중단됐습니다."
    if event_type == "rollback":
        return "observe_only로 rollback됐습니다."
    return "운영 이벤트가 파일럿 리포트에 연결됐습니다."


def _first_reason(row: Mapping[str, Any], fallback: str) -> str:
    for reason in list(row.get("reason_codes") or []):
        if reason:
            return str(reason)
    return fallback


def _count_reason(events: list[dict[str, Any]], needle: str) -> int:
    text = needle.upper()
    count = 0
    for event in events:
        haystack = " ".join([str(event.get("reason_code") or ""), " ".join(str(item) for item in event.get("reason_codes") or [])]).upper()
        if text in haystack:
            count += 1
    return count


def _merge_events(existing: list[dict[str, Any]], generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in [*existing, *generated]:
        event_id = str(event.get("event_id") or "")
        if event_id:
            by_id.setdefault(event_id, dict(event))
    rows = list(by_id.values())
    rows.sort(key=lambda row: (str(row.get("event_at") or ""), str(row.get("event_id") or "")))
    return rows


def _stable_event_id(pilot_id: str, event_type: str, source_id: str) -> str:
    raw = f"{pilot_id}:{event_type}:{source_id}"
    return f"sse_pilot_evt_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _last_updated_at(*groups: Iterable[Mapping[str, Any]]) -> str:
    values: list[str] = []
    for rows in groups:
        for row in rows:
            for key in ("updated_at", "event_at", "created_at", "received_at", "event_time", "last_updated_at"):
                value = str(row.get(key) or "")
                if value:
                    values.append(value)
                    break
    return max(values) if values else ""


def _duration_min(start: Any, end: Any) -> int | None:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() // 60))


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _consecutive_losses(values: list[float]) -> int:
    best = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _avg(values: Iterable[Any]) -> float | None:
    nums = [_float(value) for value in values if value not in (None, "")]
    return round(sum(nums) / len(nums), 4) if nums else None


def _rate(values: list[Any], predicate: Any) -> float | None:
    if not values:
        return None
    return round(sum(1 for value in values if predicate(value)) / len(values), 4)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _item_csv_columns() -> list[str]:
    return [
        "trade_date",
        "code",
        "name",
        "candidate_instance_id",
        "theme_name",
        "promotion_status",
        "pilot_status",
        "recommendation",
        "reason_code",
        "reason_codes",
        "order_intent_id",
        "order_status",
        "broker_order_id",
        "submitted_qty",
        "submitted_price",
        "fill_qty",
        "remaining_qty",
        "position_status",
        "position_qty",
        "realized_pnl_krw",
        "unrealized_pnl_krw",
        "return_pct",
    ]


def _markdown_report(report: dict[str, Any]) -> str:
    summary = dict(report.get("summary") or {})
    lines = [
        f"# Shadow Small Entry Pilot ({report.get('trade_date') or ''})",
        "",
        f"- status: {report.get('status')}",
        f"- recommendation: {report.get('recommendation')}",
        f"- candidate_count: {summary.get('candidate_count', 0)}",
        f"- submitted_order_count: {summary.get('submitted_order_count', 0)}",
        f"- filled_order_count: {summary.get('filled_order_count', 0)}",
        f"- total_pnl_krw: {summary.get('total_pnl_krw', 0)}",
        f"- message: {report.get('operator_message_ko') or ''}",
        "",
        "## Safety Checklist",
    ]
    for check in report.get("safety_checklist") or []:
        lines.append(f"- {check.get('status')} {check.get('check_id')}: {check.get('operator_message_ko')}")
    lines.append("")
    lines.append("## Items")
    for item in (report.get("items") or [])[:50]:
        lines.append(f"- {item.get('code')} {item.get('name')} {item.get('pilot_status')} {item.get('order_status') or '-'} pnl={item.get('realized_pnl_krw', 0)}")
    return "\n".join(lines) + "\n"
