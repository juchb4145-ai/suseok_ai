from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase
from trading.broker.models import new_message_id, utc_timestamp
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "live_sim_canary"

ENTRY_ORDER_STATUSES = {"CREATED", "SUBMITTING", "SUBMITTED", "UNKNOWN_SUBMIT", "ACCEPTED", "PARTIAL_FILLED", "FILLED", "CANCEL_REQUESTED", "CANCELLED", "RECONCILE_REQUIRED"}
TERMINAL_CANCEL_STATUSES = {"CANCELLED", "CANCEL_REQUESTED", "REJECTED"}
OPEN_POSITION_STATUSES = {"OPEN", "PARTIAL", "EXIT_ORDERED", "EXIT_SUBMITTING", "RECONCILE_REQUIRED"}


@dataclass(frozen=True)
class LiveSimCanaryPerformanceConfig:
    good_entry_slippage_bp: float = 10.0
    acceptable_entry_slippage_bp: float = 30.0
    bad_entry_slippage_bp: float = 50.0
    good_exit_slippage_bp: float = -10.0
    acceptable_exit_slippage_bp: float = -30.0
    high_latency_ms: float = 1000.0
    stale_tick_age_sec: float = 3.0
    match_tolerance_pct: float = 0.05
    commission_bp_per_side: float = 1.5
    sell_tax_bp: float = 15.0


ISSUE_CATALOG: dict[str, dict[str, Any]] = {
    "NO_BROKER_ACK": {
        "severity": "CRITICAL",
        "operator_message_ko": "Gateway command 이후 브로커 접수 시간이 확인되지 않습니다.",
        "recommended_action_ko": "Gateway command event와 키움 주문 결과 로그를 대조하세요.",
        "evidence_fields": ["gateway_command_id", "submitted_at", "broker_accepted_at"],
    },
    "ACK_BUT_NO_FILL": {
        "severity": "WARNING",
        "operator_message_ko": "브로커 접수 후 체결 이벤트가 없습니다.",
        "recommended_action_ko": "호가 정책과 주문번호 기준 미체결 상태를 확인하세요.",
        "evidence_fields": ["broker_order_id", "broker_accepted_at", "requested_price"],
    },
    "PARTIAL_FILL_TIMEOUT": {
        "severity": "WARNING",
        "operator_message_ko": "부분 체결 후 잔량이 남아 있습니다.",
        "recommended_action_ko": "잔량 취소/리컨실 여부를 확인하고 limit_price_policy를 검토하세요.",
        "evidence_fields": ["filled_quantity", "unfilled_quantity", "fill_ratio"],
    },
    "CANCEL_FAILED": {
        "severity": "CRITICAL",
        "operator_message_ko": "취소 요청이 실패했거나 리컨실이 필요합니다.",
        "recommended_action_ko": "취소 주문 결과와 브로커 원장을 대조하세요.",
        "evidence_fields": ["cancel_orders", "broker_order_id"],
    },
    "CANCELLED_BEFORE_FILL": {
        "severity": "INFO",
        "operator_message_ko": "체결 전 취소로 종료되었습니다.",
        "recommended_action_ko": "미체결 사유가 반복되는지 장후 표본에서 확인하세요.",
        "evidence_fields": ["cancelled_at", "filled_quantity"],
    },
    "EXIT_NOT_SUBMITTED": {
        "severity": "CRITICAL",
        "operator_message_ko": "진입 체결 이후 청산 주문 제출이 확인되지 않습니다.",
        "recommended_action_ko": "exit guard, position status, runtime sell intent 생성을 점검하세요.",
        "evidence_fields": ["filled_quantity", "exit_order_count", "final_status"],
    },
    "EXIT_NO_FILL": {
        "severity": "WARNING",
        "operator_message_ko": "청산 주문은 제출됐지만 청산 체결이 없습니다.",
        "recommended_action_ko": "청산 호가와 브로커 미체결 원장을 확인하세요.",
        "evidence_fields": ["exit_order_count", "exit_fill_ratio", "exit_requested_price"],
    },
    "STOP_LOSS_DELAYED": {
        "severity": "WARNING",
        "operator_message_ko": "손절 청산 품질이 좋지 않습니다.",
        "recommended_action_ko": "손절 트리거와 체결 지연 표본을 검토하세요.",
        "evidence_fields": ["exit_reason", "exit_slippage_pct"],
    },
    "TAKE_PROFIT_NOT_CAPTURED": {
        "severity": "WARNING",
        "operator_message_ko": "익절 조건 대비 실제 청산 성과가 약합니다.",
        "recommended_action_ko": "익절 트리거와 실제 청산 가격 차이를 검토하세요.",
        "evidence_fields": ["exit_reason", "exit_avg_fill_price", "max_mfe_pct_after_entry"],
    },
    "TIME_EXIT_WEAK": {
        "severity": "INFO",
        "operator_message_ko": "시간 청산 성과가 약합니다.",
        "recommended_action_ko": "충분한 표본이 쌓인 뒤 max_hold_minutes 검토 후보로만 기록하세요.",
        "evidence_fields": ["exit_reason", "hold_minutes", "net_return_pct"],
    },
    "LIVE_WORSE_THAN_DRY_RUN": {
        "severity": "WARNING",
        "operator_message_ko": "LIVE_SIM 실제 성과가 DRY_RUN 이론 성과보다 낮습니다.",
        "recommended_action_ko": "슬리피지, 지연, 청산 사유 변경을 분리해 리뷰하세요.",
        "evidence_fields": ["dry_run_net_return_pct", "live_sim_net_return_pct", "net_return_diff_pct"],
    },
    "SLIPPAGE_HIGH": {
        "severity": "WARNING",
        "operator_message_ko": "진입 슬리피지가 높습니다.",
        "recommended_action_ko": "자동 적용 없이 max_entry_slippage_bp 또는 호가 정책 검토 후보로만 남기세요.",
        "evidence_fields": ["entry_slippage_bp", "requested_price", "avg_fill_price"],
    },
    "LATENCY_HIGH": {
        "severity": "WARNING",
        "operator_message_ko": "제출부터 접수/체결까지 지연이 큽니다.",
        "recommended_action_ko": "Gateway latency report와 같은 시간대의 큐 상태를 확인하세요.",
        "evidence_fields": ["submit_to_ack_ms", "submit_to_first_fill_ms"],
    },
    "STALE_TICK_ENTRY": {
        "severity": "WARNING",
        "operator_message_ko": "진입 판단에 사용된 tick이 오래됐습니다.",
        "recommended_action_ko": "장중 tick 수집 지연과 데이터 품질을 확인하세요.",
        "evidence_fields": ["entry_tick_age_sec", "submitted_at"],
    },
    "RECONCILE_REQUIRED": {
        "severity": "CRITICAL",
        "operator_message_ko": "주문/체결/포지션 원장 간 리컨실이 필요합니다.",
        "recommended_action_ko": "LIVE_SIM reconcile을 실행하고 원장 불일치를 수동 확인하세요.",
        "evidence_fields": ["final_status", "order_status", "position_status"],
    },
    "ORPHAN_EXECUTION": {
        "severity": "CRITICAL",
        "operator_message_ko": "연결되는 주문 원장이 없는 체결 이벤트가 있습니다.",
        "recommended_action_ko": "broker_order_id, order_intent_id, command_id 기준으로 원장을 대조하세요.",
        "evidence_fields": ["broker_order_id", "execution_ids"],
    },
    "ORPHAN_ORDER_RESULT": {
        "severity": "WARNING",
        "operator_message_ko": "Canary decision과 연결되지 않은 LIVE_SIM 주문 원장이 있습니다.",
        "recommended_action_ko": "candidate_instance_id와 idempotency_key 기준으로 생성 경로를 확인하세요.",
        "evidence_fields": ["order_intent_id", "gateway_command_id", "candidate_instance_id"],
    },
    "POSITION_QTY_MISMATCH": {
        "severity": "CRITICAL",
        "operator_message_ko": "체결 합계와 포지션 수량이 일치하지 않습니다.",
        "recommended_action_ko": "브로커 잔고 스냅샷으로 live_sim_positions를 리컨실하세요.",
        "evidence_fields": ["filled_quantity", "exit_filled_quantity", "position_current_qty"],
    },
    "UNKNOWN_FINAL_STATUS": {
        "severity": "WARNING",
        "operator_message_ko": "최종 상태를 확정할 수 없습니다.",
        "recommended_action_ko": "원장 누락 여부와 주문/포지션 상태 전이를 확인하세요.",
        "evidence_fields": ["final_status", "order_status", "position_status"],
    },
}


class LiveSimCanaryPerformanceAnalyzer:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: Optional[LiveSimCanaryPerformanceConfig] = None,
        report_root: Optional[Path] = None,
        gateway_state: Any | None = None,
        dry_run_analyzer: DryRunPerformanceAnalyzer | None = None,
    ) -> None:
        self.db = db
        self.config = config or LiveSimCanaryPerformanceConfig()
        self.report_root = report_root or REPORT_ROOT
        self.gateway_state = gateway_state
        self.dry_run_analyzer = dry_run_analyzer or DryRunPerformanceAnalyzer(db)

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        code: Optional[str] = None,
        final_status: Optional[str] = None,
        fill_quality_grade: Optional[str] = None,
        exit_quality_grade: Optional[str] = None,
        outcome_match: Optional[str] = None,
        issue_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        resolved_trade_date = trade_date or datetime.now().date().isoformat()
        filters = {
            "trade_date": resolved_trade_date,
            "code": code or "",
            "final_status": final_status or "",
            "fill_quality_grade": fill_quality_grade or "",
            "exit_quality_grade": exit_quality_grade or "",
            "outcome_match": outcome_match or "",
            "issue_type": issue_type or "",
            "limit": int(limit or 100),
            "offset": int(offset or 0),
        }
        cases = self.build_lifecycles(trade_date=resolved_trade_date, code=code)
        cases = [
            item
            for item in cases
            if (not final_status or item.get("final_status") == final_status)
            and (not fill_quality_grade or item.get("fill_quality_grade") == fill_quality_grade)
            and (not exit_quality_grade or item.get("exit_quality_grade") == exit_quality_grade)
            and (not outcome_match or item.get("outcome_match") == outcome_match)
            and (not issue_type or issue_type in [issue.get("issue_type") for issue in item.get("issues", [])])
        ]
        summary = self.aggregate_summary(cases, trade_date=resolved_trade_date)
        grouped = self.aggregate_grouped(cases)
        recommendations = self.recommendations(summary, grouped)
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "report_id": new_message_id("live_sim_canary_perf"),
            "status": "READY",
            "review_only": True,
            "analysis_only": True,
            "safety_scope": {
                "live_real_order_activation": False,
                "gateway_send_order_created": False,
                "strategy_settings_auto_change": False,
                "hybrid_threshold_auto_change": False,
            },
            "disclaimer_ko": "LIVE_SIM Canary 사후 분석 리포트입니다. 주문 생성, LIVE_REAL 활성화, threshold/전략 설정 자동 변경을 수행하지 않습니다.",
            "generated_at": utc_timestamp(),
            "trade_date": resolved_trade_date,
            "filters": filters,
            "summary": summary,
            "grouped": grouped,
            "recommendations": recommendations,
            "issue_catalog": ISSUE_CATALOG,
            "items": cases[start:end],
            "total_items": len(cases),
        }

    def build_lifecycles(self, *, trade_date: str, code: Optional[str] = None) -> list[dict[str, Any]]:
        decisions = self.db.list_live_sim_canary_decisions(trade_date=trade_date, code=code, limit=10000)
        orders = self.db.list_live_sim_orders(trade_date=trade_date, code=code, limit=10000)
        fills = self.db.list_live_sim_fill_events(trade_date=trade_date, code=code, limit=20000)
        cancels = self.db.list_live_sim_cancel_orders(trade_date=trade_date, code=code, limit=10000)
        positions = self.db.list_live_sim_positions(code=code, limit=10000)
        intents = self.db.list_runtime_order_intents_for_analysis(trade_date=trade_date, code=code, limit=20000)
        dry_run_items = self._dry_run_items(trade_date=trade_date, code=code)
        ticks_by_code = self._price_ticks_by_code(trade_date=trade_date, code=code)
        commands = self._gateway_commands(trade_date=trade_date, limit=10000)
        command_events = self._gateway_command_events(trade_date=trade_date, limit=20000)
        hybrid_events = self._hybrid_events(trade_date=trade_date, code=code, limit=20000)

        order_index = _OrderIndex(orders)
        fills_by_order = _fills_by_order(fills, orders)
        cancels_by_order = _group_by(cancels, "original_order_id")
        positions_by_key = _positions_by_key(positions)
        intents_by_id = {str(item.get("intent_id") or ""): item for item in intents}
        dry_run_index = _DryRunIndex(dry_run_items)
        command_by_id = {str(item.get("command_id") or ""): item for item in commands}
        command_events_by_id = _group_by(command_events, "command_id")
        hybrid_by_key = _latest_hybrid_by_key(hybrid_events)

        cases: list[dict[str, Any]] = []
        consumed_order_ids: set[str] = set()
        for decision in decisions:
            linked = order_index.find_for_decision(decision)
            linked_orders = list(linked)
            for extra in order_index.related_orders(decision, linked_orders):
                if extra not in linked_orders:
                    linked_orders.append(extra)
            for order in linked_orders:
                consumed_order_ids.add(str(order.get("order_intent_id") or ""))
            cases.append(
                self._build_case(
                    decision=decision,
                    orders=linked_orders,
                    fills_by_order=fills_by_order,
                    cancels_by_order=cancels_by_order,
                    positions_by_key=positions_by_key,
                    intents_by_id=intents_by_id,
                    dry_run_index=dry_run_index,
                    ticks_by_code=ticks_by_code,
                    command_by_id=command_by_id,
                    command_events_by_id=command_events_by_id,
                    hybrid_by_key=hybrid_by_key,
                    link=order_index.last_link,
                )
            )

        for order in orders:
            order_id = str(order.get("order_intent_id") or "")
            if not order_id or order_id in consumed_order_ids or str(order.get("side") or "").lower() != "buy":
                continue
            decision = _decision_from_orphan_order(order)
            case = self._build_case(
                decision=decision,
                orders=[order, *order_index.related_orders(decision, [order])],
                fills_by_order=fills_by_order,
                cancels_by_order=cancels_by_order,
                positions_by_key=positions_by_key,
                intents_by_id=intents_by_id,
                dry_run_index=dry_run_index,
                ticks_by_code=ticks_by_code,
                command_by_id=command_by_id,
                command_events_by_id=command_events_by_id,
                hybrid_by_key=hybrid_by_key,
                link={"matched_by": "orphan_order", "link_confidence": "LOW"},
            )
            case["issues"].append(_issue("ORPHAN_ORDER_RESULT", case, {"order": _public_order(order)}))
            case["issue_types"] = _issue_types(case["issues"])
            cases.append(case)

        known_order_ids = {str(order.get("order_intent_id") or "") for order in orders if order.get("order_intent_id")}
        known_broker_ids = {str(order.get("broker_order_id") or "") for order in orders if order.get("broker_order_id")}
        for fill in fills:
            order_id = str(fill.get("order_intent_id") or "")
            broker_id = str(fill.get("broker_order_id") or "")
            if (order_id and order_id in known_order_ids) or (broker_id and broker_id in known_broker_ids):
                continue
            case = _orphan_execution_case(fill, trade_date=trade_date)
            cases.append(case)

        cases.sort(key=lambda item: (str(item.get("submitted_at") or item.get("created_at") or ""), str(item.get("case_id") or "")))
        return cases

    def _build_case(
        self,
        *,
        decision: dict[str, Any],
        orders: list[dict[str, Any]],
        fills_by_order: dict[str, list[dict[str, Any]]],
        cancels_by_order: dict[str, list[dict[str, Any]]],
        positions_by_key: dict[tuple[str, str], list[dict[str, Any]]],
        intents_by_id: dict[str, dict[str, Any]],
        dry_run_index: "_DryRunIndex",
        ticks_by_code: dict[str, list[dict[str, Any]]],
        command_by_id: dict[str, dict[str, Any]],
        command_events_by_id: dict[str, list[dict[str, Any]]],
        hybrid_by_key: dict[tuple[str, str], dict[str, Any]],
        link: dict[str, Any],
    ) -> dict[str, Any]:
        entry_orders = [order for order in orders if str(order.get("side") or "").lower() == "buy"]
        exit_orders = [order for order in orders if str(order.get("side") or "").lower() == "sell"]
        entry_order = _best_entry_order(entry_orders)
        code = str(decision.get("code") or (entry_order or {}).get("code") or "")
        trade_date = str(decision.get("trade_date") or (entry_order or {}).get("trade_date") or "")
        candidate_instance_id = str(decision.get("candidate_instance_id") or (entry_order or {}).get("candidate_instance_id") or "")
        candidate_id = _first_present(decision.get("candidate_id"), (entry_order or {}).get("candidate_id"))
        position = _best_position(positions_by_key, code=code, candidate_instance_id=candidate_instance_id)
        dry_run_item = dry_run_index.find(decision=decision, entry_order=entry_order, candidate_id=candidate_id, code=code, trade_date=trade_date)
        entry_fills = _dedupe_fills(_fills_for_orders(entry_orders, fills_by_order))
        exit_fills = _dedupe_fills(_fills_for_orders(exit_orders, fills_by_order))
        all_cancels = [cancel for order in orders for cancel in cancels_by_order.get(str(order.get("order_intent_id") or ""), [])]
        requested_price = _first_int((entry_order or {}).get("requested_price"), decision.get("limit_price"))
        requested_qty = _first_int((entry_order or {}).get("requested_qty"), decision.get("quantity"))
        entry_metrics = self._entry_metrics(entry_order, entry_fills, requested_price=requested_price, requested_qty=requested_qty, dry_run_item=dry_run_item, ticks=ticks_by_code.get(code, []))
        exit_metrics = self._exit_metrics(exit_orders, exit_fills, entry_metrics=entry_metrics, position=position, intents_by_id=intents_by_id)
        performance = self._performance_metrics(entry_metrics, exit_metrics, position, entry_fills, exit_fills)
        compare = self._compare_dry_run(dry_run_item, entry_metrics, exit_metrics, performance)
        command = command_by_id.get(str((entry_order or {}).get("command_id") or decision.get("gateway_command_id") or ""))
        command_events = command_events_by_id.get(str((entry_order or {}).get("command_id") or decision.get("gateway_command_id") or ""), [])
        hybrid = hybrid_by_key.get((candidate_instance_id, code)) or hybrid_by_key.get(("", code)) or {}

        case = {
            "case_id": _case_id(decision, entry_order, code, trade_date),
            "lifecycle_id": _case_id(decision, entry_order, code, trade_date),
            "trade_date": trade_date,
            "code": code,
            "name": str(decision.get("name") or (entry_order or {}).get("name") or ""),
            "theme": str(decision.get("theme_name") or _nested(dry_run_item, "theme_name") or ""),
            "hybrid_score": _first_float(decision.get("hybrid_score"), hybrid.get("hybrid_score"), _nested(dry_run_item, "hybrid_score")),
            "hybrid_status": str(decision.get("hybrid_status") or hybrid.get("hybrid_status") or _nested(dry_run_item, "hybrid_status") or ""),
            "candidate_id": candidate_id,
            "candidate_instance_id": candidate_instance_id,
            "canary_decision_id": str(decision.get("decision_id") or ""),
            "order_intent_id": str((entry_order or {}).get("order_intent_id") or decision.get("order_intent_id") or ""),
            "gateway_command_id": str((entry_order or {}).get("command_id") or decision.get("gateway_command_id") or ""),
            "broker_order_id": str((entry_order or {}).get("broker_order_id") or ""),
            "matched_by": str(link.get("matched_by") or ""),
            "link_confidence": str(link.get("link_confidence") or ""),
            "entry_order_count": len(entry_orders),
            "exit_orders": [_public_order(order) for order in exit_orders],
            "entry_fills": [_public_fill(fill) for fill in entry_fills],
            "exit_fills": [_public_fill(fill) for fill in exit_fills],
            "cancel_orders": [_public_cancel(cancel) for cancel in all_cancels],
            "order_timeline": _timeline_for_orders(entry_orders + exit_orders, command_events),
            "fill_timeline": _timeline_for_fills(entry_fills + exit_fills),
            "exit_timeline": _timeline_for_orders(exit_orders, []),
            "linked_ids": {
                "canary_decision_id": str(decision.get("decision_id") or ""),
                "intent_id": str((entry_order or {}).get("order_intent_id") or decision.get("order_intent_id") or ""),
                "command_id": str((entry_order or {}).get("command_id") or decision.get("gateway_command_id") or ""),
                "order_no": str((entry_order or {}).get("broker_order_id") or ""),
                "execution_ids": [str(fill.get("fill_id") or fill.get("event_id") or "") for fill in entry_fills + exit_fills],
            },
            "raw_metadata": _redact_sensitive(
                {
                    "canary_decision": decision,
                    "entry_order": entry_order or {},
                    "position": position or {},
                    "gateway_command": command or {},
                    "dry_run_item": dry_run_item or {},
                }
            ),
            **entry_metrics,
            **exit_metrics,
            **performance,
            **compare,
        }
        case["final_status"] = self._final_status(case, entry_order, position, entry_fills, exit_fills)
        case["issues"] = self._classify_issues(case, entry_order, position)
        case["issue_types"] = _issue_types(case["issues"])
        if any(issue in case["issue_types"] for issue in ("RECONCILE_REQUIRED", "POSITION_QTY_MISMATCH", "ORPHAN_EXECUTION")):
            case["final_status"] = "RECONCILE_REQUIRED"
        return case

    def _entry_metrics(
        self,
        entry_order: Optional[dict[str, Any]],
        fills: list[dict[str, Any]],
        *,
        requested_price: int,
        requested_qty: int,
        dry_run_item: Optional[dict[str, Any]],
        ticks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        submitted_at = str((entry_order or {}).get("submitted_at") or (entry_order or {}).get("created_at") or "")
        accepted_at = str((entry_order or {}).get("accepted_at") or "")
        first_fill_at = _first_time(fill.get("event_time") or fill.get("received_at") for fill in fills)
        full_fill_at = _last_time(fill.get("event_time") or fill.get("received_at") for fill in fills if int(fill.get("remaining_qty") or 0) == 0) or ""
        fill_qty = sum(max(0, int(fill.get("fill_qty") or 0)) for fill in fills)
        fill_amount = sum(max(0, int(fill.get("fill_qty") or 0)) * max(0, int(fill.get("fill_price") or 0)) for fill in fills)
        avg_fill_price = round(fill_amount / fill_qty, 4) if fill_qty > 0 else None
        fill_ratio = _ratio(fill_qty, requested_qty) if requested_qty > 0 else None
        unfilled_qty = max(0, requested_qty - fill_qty) if requested_qty else 0
        cancelled_before_fill = fill_qty == 0 and str((entry_order or {}).get("order_status") or "") in TERMINAL_CANCEL_STATUSES
        no_fill = fill_qty == 0 and bool(entry_order)
        partial_fill = fill_qty > 0 and requested_qty > 0 and fill_qty < requested_qty
        entry_slippage_pct = _price_diff_pct(avg_fill_price, requested_price)
        entry_slippage_bp = _bp(entry_slippage_pct)
        dry_run_price = _first_float(_nested(dry_run_item, "entry_price"), _nested(dry_run_item, "dry_run_expected_entry_price"))
        tick = _nearest_tick(ticks, submitted_at)
        tick_price = _first_float((tick or {}).get("price"))
        tick_age = _age_sec((tick or {}).get("timestamp") or (tick or {}).get("received_at"), submitted_at) if tick else None
        spread = _spread(tick)
        grade = self._entry_grade(
            fill_qty=fill_qty,
            requested_qty=requested_qty,
            slippage_bp=entry_slippage_bp,
            tick_age_sec=tick_age,
            order_status=str((entry_order or {}).get("order_status") or ""),
        )
        return {
            "submitted_at": submitted_at,
            "broker_accepted_at": accepted_at,
            "first_fill_at": first_fill_at,
            "full_fill_at": full_fill_at,
            "submit_to_ack_ms": _duration_ms(submitted_at, accepted_at),
            "submit_to_first_fill_ms": _duration_ms(submitted_at, first_fill_at),
            "submit_to_full_fill_ms": _duration_ms(submitted_at, full_fill_at),
            "requested_price": requested_price,
            "requested_quantity": requested_qty,
            "filled_quantity": fill_qty,
            "avg_fill_price": avg_fill_price,
            "unfilled_quantity": unfilled_qty,
            "fill_ratio": fill_ratio,
            "partial_fill": partial_fill,
            "no_fill": no_fill,
            "cancelled_before_fill": cancelled_before_fill,
            "entry_slippage_pct": entry_slippage_pct,
            "entry_slippage_bp": entry_slippage_bp,
            "entry_price_vs_dry_run_price_pct": _price_diff_pct(avg_fill_price, dry_run_price),
            "entry_price_vs_delayed_tick_pct": _price_diff_pct(avg_fill_price, tick_price),
            "entry_tick_age_sec": tick_age,
            "spread_at_entry": spread,
            "liquidity_bucket": _liquidity_bucket(tick),
            "fill_quality_grade": grade,
        }

    def _exit_metrics(
        self,
        exit_orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        *,
        entry_metrics: dict[str, Any],
        position: Optional[dict[str, Any]],
        intents_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        requested_price = _first_int(*[order.get("requested_price") for order in exit_orders])
        requested_qty = sum(max(0, int(order.get("requested_qty") or order.get("submitted_qty") or 0)) for order in exit_orders)
        fill_qty = sum(max(0, int(fill.get("fill_qty") or 0)) for fill in fills)
        fill_amount = sum(max(0, int(fill.get("fill_qty") or 0)) * max(0, int(fill.get("fill_price") or 0)) for fill in fills)
        avg_fill_price = round(fill_amount / fill_qty, 4) if fill_qty > 0 else None
        exit_reason = _exit_reason(exit_orders, intents_by_id, position)
        fill_ratio = _ratio(fill_qty, requested_qty) if requested_qty > 0 else (1.0 if fill_qty > 0 else None)
        slippage_pct = _exit_slippage_pct(avg_fill_price, requested_price)
        grade = self._exit_grade(exit_orders=exit_orders, fill_qty=fill_qty, requested_qty=requested_qty, slippage_bp=_bp(slippage_pct), position=position)
        return {
            "exit_order_count": len(exit_orders),
            "first_exit_submitted_at": _first_time(order.get("submitted_at") or order.get("created_at") for order in exit_orders),
            "first_exit_fill_at": _first_time(fill.get("event_time") or fill.get("received_at") for fill in fills),
            "final_exit_fill_at": _last_time(fill.get("event_time") or fill.get("received_at") for fill in fills),
            "exit_requested_price": requested_price,
            "exit_avg_fill_price": avg_fill_price,
            "exit_filled_quantity": fill_qty,
            "exit_fill_ratio": fill_ratio,
            "exit_slippage_pct": slippage_pct,
            "exit_slippage_bp": _bp(slippage_pct),
            "exit_reason": exit_reason,
            "stop_loss_triggered": "STOP" in exit_reason.upper() or "LOSS" in exit_reason.upper(),
            "take_profit_triggered": "TAKE" in exit_reason.upper() or "PROFIT" in exit_reason.upper(),
            "time_exit_triggered": "TIME" in exit_reason.upper(),
            "context_risk_exit_triggered": any(token in exit_reason.upper() for token in ("THEME", "LEADER", "INDEX", "MARKET", "BREADTH", "CONTEXT")),
            "market_close_exit_triggered": "CLOSE" in exit_reason.upper() or "CLOSING" in exit_reason.upper(),
            "exit_quality_grade": grade,
        }

    def _performance_metrics(
        self,
        entry: dict[str, Any],
        exit_: dict[str, Any],
        position: Optional[dict[str, Any]],
        entry_fills: list[dict[str, Any]],
        exit_fills: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry_avg = _first_float(entry.get("avg_fill_price"), (position or {}).get("entry_avg_price"))
        exit_avg = _first_float(exit_.get("exit_avg_fill_price"))
        qty = _first_int(exit_.get("exit_filled_quantity"), (position or {}).get("realized_qty"), entry.get("filled_quantity"))
        gross_return_pct = _price_diff_pct(exit_avg, entry_avg)
        realized_pnl = _first_float((position or {}).get("realized_pnl"))
        if realized_pnl is None and entry_avg and exit_avg and qty:
            realized_pnl = round((exit_avg - entry_avg) * qty, 4)
        fees = sum(_first_float(fill.get("commission")) or 0.0 for fill in entry_fills + exit_fills)
        taxes = sum(_first_float(fill.get("tax")) or 0.0 for fill in entry_fills + exit_fills)
        total_cost = fees + taxes
        basis = (entry_avg or 0) * max(1, qty or 0)
        net_return_pct = None
        if gross_return_pct is not None:
            cost_pct = (total_cost / basis * 100.0) if basis > 0 else 0.0
            net_return_pct = round(gross_return_pct - cost_pct, 6)
        elif _first_float((position or {}).get("realized_pnl_pct")) is not None:
            net_return_pct = _first_float((position or {}).get("realized_pnl_pct"))
        return {
            "gross_return_pct": gross_return_pct,
            "net_return_pct": net_return_pct,
            "realized_pnl_krw": realized_pnl,
            "estimated_fee_krw": round(fees, 4),
            "estimated_tax_krw": round(taxes, 4),
            "total_cost_krw": round(total_cost, 4),
            "max_mfe_pct_after_entry": _first_float((position or {}).get("max_favorable_excursion_pct")),
            "max_mae_pct_after_entry": _first_float((position or {}).get("max_adverse_excursion_pct")),
            "hold_minutes": _hold_minutes(entry.get("first_fill_at") or (position or {}).get("opened_at"), exit_.get("final_exit_fill_at") or (position or {}).get("closed_at")),
        }

    def _compare_dry_run(
        self,
        dry_run_item: Optional[dict[str, Any]],
        entry: dict[str, Any],
        exit_: dict[str, Any],
        performance: dict[str, Any],
    ) -> dict[str, Any]:
        dry_price = _first_float(_nested(dry_run_item, "entry_price"), _nested(dry_run_item, "dry_run_expected_entry_price"))
        dry_net = _first_float(_nested(dry_run_item, "net_return_pct"), _nested(dry_run_item, "realized_return_pct"))
        live_net = _first_float(performance.get("net_return_pct"))
        net_diff = _diff(live_net, dry_net)
        dry_exit_reason = _first_text(*(_nested(dry_run_item, "exit_reasons") or [])) if isinstance(_nested(dry_run_item, "exit_reasons"), list) else _first_text(_nested(dry_run_item, "exit_reason"), _nested(dry_run_item, "final_status"))
        live_exit_reason = str(exit_.get("exit_reason") or "")
        outcome = "INCOMPARABLE"
        if dry_run_item and not entry.get("no_fill") and dry_net is not None and live_net is not None:
            if abs(net_diff or 0.0) <= self.config.match_tolerance_pct:
                outcome = "MATCH"
            elif (net_diff or 0.0) > 0:
                outcome = "LIVE_BETTER"
            else:
                outcome = "LIVE_WORSE"
        return {
            "dry_run_expected_entry_price": dry_price,
            "live_sim_avg_entry_price": entry.get("avg_fill_price"),
            "entry_price_diff_pct": _price_diff_pct(entry.get("avg_fill_price"), dry_price),
            "dry_run_net_return_pct": dry_net,
            "live_sim_net_return_pct": live_net,
            "net_return_diff_pct": net_diff,
            "dry_run_exit_reason": dry_exit_reason,
            "live_sim_exit_reason": live_exit_reason,
            "exit_reason_changed": bool(dry_exit_reason and live_exit_reason and dry_exit_reason != live_exit_reason),
            "dry_run_fill_assumption": _dry_run_fill_assumption(dry_run_item),
            "live_sim_fill_result": _live_fill_result(entry),
            "fill_assumption_broken": bool(dry_run_item and (entry.get("no_fill") or entry.get("partial_fill"))),
            "slippage_assumption_broken": bool((entry.get("entry_slippage_bp") or 0) >= self.config.acceptable_entry_slippage_bp),
            "delay_assumption_broken": bool((entry.get("submit_to_first_fill_ms") or 0) >= self.config.high_latency_ms),
            "exit_assumption_broken": bool(dry_exit_reason and live_exit_reason and dry_exit_reason != live_exit_reason),
            "outcome_match": outcome,
        }

    def _entry_grade(self, *, fill_qty: int, requested_qty: int, slippage_bp: Optional[float], tick_age_sec: Optional[float], order_status: str) -> str:
        if fill_qty <= 0:
            return "NO_FILL" if order_status else "UNKNOWN"
        if tick_age_sec is not None and tick_age_sec > self.config.stale_tick_age_sec:
            return "BAD"
        if slippage_bp is None:
            return "ACCEPTABLE" if requested_qty <= 0 or fill_qty >= requested_qty else "BAD"
        if fill_qty >= requested_qty > 0 and slippage_bp <= self.config.good_entry_slippage_bp:
            return "GOOD"
        if slippage_bp <= self.config.acceptable_entry_slippage_bp:
            return "ACCEPTABLE"
        return "BAD"

    def _exit_grade(self, *, exit_orders: list[dict[str, Any]], fill_qty: int, requested_qty: int, slippage_bp: Optional[float], position: Optional[dict[str, Any]]) -> str:
        if not exit_orders:
            if position and int(position.get("current_qty") or 0) > 0:
                return "BAD"
            return "UNKNOWN"
        if fill_qty <= 0:
            return "NO_FILL"
        if requested_qty > 0 and fill_qty < requested_qty:
            return "BAD"
        if slippage_bp is None:
            return "ACCEPTABLE"
        if slippage_bp >= self.config.good_exit_slippage_bp:
            return "GOOD"
        if slippage_bp >= self.config.acceptable_exit_slippage_bp:
            return "ACCEPTABLE"
        return "BAD"

    def _final_status(
        self,
        case: dict[str, Any],
        entry_order: Optional[dict[str, Any]],
        position: Optional[dict[str, Any]],
        entry_fills: list[dict[str, Any]],
        exit_fills: list[dict[str, Any]],
    ) -> str:
        if any(issue.get("issue_type") == "RECONCILE_REQUIRED" for issue in case.get("issues", [])):
            return "RECONCILE_REQUIRED"
        if position:
            status = str(position.get("status") or "")
            current_qty = int(position.get("current_qty") or 0)
            if status == "RECONCILE_REQUIRED":
                return "RECONCILE_REQUIRED"
            if status == "CLOSED" or (current_qty <= 0 and exit_fills):
                return "CLOSED"
            if current_qty > 0 and exit_fills:
                return "PARTIAL_OPEN"
            if current_qty > 0:
                return "OPEN"
        if case.get("cancelled_before_fill"):
            return "CANCELLED"
        if not entry_fills:
            return "CANCELLED" if str((entry_order or {}).get("order_status") or "") in TERMINAL_CANCEL_STATUSES else "UNKNOWN"
        if entry_fills and exit_fills:
            return "CLOSED"
        if entry_fills:
            return "OPEN"
        return "UNKNOWN"

    def _classify_issues(self, case: dict[str, Any], entry_order: Optional[dict[str, Any]], position: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if entry_order and not case.get("broker_accepted_at") and str(entry_order.get("order_status") or "") in ENTRY_ORDER_STATUSES:
            issues.append(_issue("NO_BROKER_ACK", case))
        if case.get("broker_accepted_at") and case.get("no_fill") and not case.get("cancelled_before_fill"):
            issues.append(_issue("ACK_BUT_NO_FILL", case))
        if case.get("partial_fill"):
            issues.append(_issue("PARTIAL_FILL_TIMEOUT", case))
        if case.get("cancelled_before_fill"):
            issues.append(_issue("CANCELLED_BEFORE_FILL", case))
        if any(str(cancel.get("status") or "") in {"FAILED", "RECONCILE_REQUIRED"} for cancel in case.get("cancel_orders", [])):
            issues.append(_issue("CANCEL_FAILED", case))
        if case.get("filled_quantity", 0) > 0 and case.get("exit_order_count", 0) == 0 and case.get("final_status") in {"OPEN", "PARTIAL_OPEN", "UNKNOWN"}:
            issues.append(_issue("EXIT_NOT_SUBMITTED", case))
        if case.get("exit_order_count", 0) > 0 and not case.get("exit_filled_quantity"):
            issues.append(_issue("EXIT_NO_FILL", case))
        if case.get("stop_loss_triggered") and str(case.get("exit_quality_grade") or "") == "BAD":
            issues.append(_issue("STOP_LOSS_DELAYED", case))
        if case.get("take_profit_triggered") and (case.get("net_return_pct") or 0) <= 0:
            issues.append(_issue("TAKE_PROFIT_NOT_CAPTURED", case))
        if case.get("time_exit_triggered") and (case.get("net_return_pct") or 0) <= 0:
            issues.append(_issue("TIME_EXIT_WEAK", case))
        if case.get("outcome_match") == "LIVE_WORSE":
            issues.append(_issue("LIVE_WORSE_THAN_DRY_RUN", case))
        if (case.get("entry_slippage_bp") or 0) >= self.config.bad_entry_slippage_bp:
            issues.append(_issue("SLIPPAGE_HIGH", case))
        if (case.get("submit_to_ack_ms") or 0) >= self.config.high_latency_ms or (case.get("submit_to_first_fill_ms") or 0) >= self.config.high_latency_ms:
            issues.append(_issue("LATENCY_HIGH", case))
        if case.get("entry_tick_age_sec") is not None and case.get("entry_tick_age_sec") > self.config.stale_tick_age_sec:
            issues.append(_issue("STALE_TICK_ENTRY", case))
        if str((entry_order or {}).get("order_status") or "") == "RECONCILE_REQUIRED" or str((position or {}).get("status") or "") == "RECONCILE_REQUIRED":
            issues.append(_issue("RECONCILE_REQUIRED", case))
        if position:
            expected_current = max(0, int(case.get("filled_quantity") or 0) - int(case.get("exit_filled_quantity") or 0))
            actual_current = int(position.get("current_qty") or 0)
            if expected_current != actual_current:
                issues.append(_issue("POSITION_QTY_MISMATCH", case, {"expected_current_qty": expected_current, "position_current_qty": actual_current}))
        if not issues and case.get("final_status") == "UNKNOWN":
            issues.append(_issue("UNKNOWN_FINAL_STATUS", case))
        return issues

    def aggregate_summary(self, items: list[dict[str, Any]], *, trade_date: str) -> dict[str, Any]:
        total = len(items)
        issue_counts = Counter(issue.get("issue_type") for item in items for issue in item.get("issues", []))
        final_counts = Counter(str(item.get("final_status") or "UNKNOWN") for item in items)
        fill_counts = Counter(str(item.get("fill_quality_grade") or "UNKNOWN") for item in items)
        exit_counts = Counter(str(item.get("exit_quality_grade") or "UNKNOWN") for item in items)
        outcome_counts = Counter(str(item.get("outcome_match") or "INCOMPARABLE") for item in items)
        submitted_count = sum(1 for item in items if item.get("submitted_at"))
        accepted_count = sum(1 for item in items if item.get("broker_accepted_at"))
        partial_count = sum(1 for item in items if item.get("partial_fill"))
        full_count = sum(1 for item in items if (item.get("fill_ratio") or 0) >= 1)
        no_fill_count = sum(1 for item in items if item.get("no_fill"))
        cancelled_count = sum(1 for item in items if item.get("cancelled_before_fill") or item.get("final_status") == "CANCELLED")
        closed_count = sum(1 for item in items if item.get("final_status") == "CLOSED")
        live_worse_count = outcome_counts.get("LIVE_WORSE", 0)
        live_better_count = outcome_counts.get("LIVE_BETTER", 0)
        incomparable_count = outcome_counts.get("INCOMPARABLE", 0)
        avg_slippage = _avg(item.get("entry_slippage_bp") for item in items)
        avg_net_diff = _avg(item.get("net_return_diff_pct") for item in items)
        return {
            "trade_date": trade_date,
            "total_lifecycle_count": total,
            "today_canary_order_count": total,
            "submitted_count": submitted_count,
            "broker_accepted_count": accepted_count,
            "partial_fill_count": partial_count,
            "full_fill_count": full_count,
            "no_fill_count": no_fill_count,
            "cancelled_count": cancelled_count,
            "closed_count": closed_count,
            "reconcile_required_count": final_counts.get("RECONCILE_REQUIRED", 0),
            "orphan_case_count": issue_counts.get("ORPHAN_EXECUTION", 0) + issue_counts.get("ORPHAN_ORDER_RESULT", 0),
            "avg_fill_ratio": _avg(item.get("fill_ratio") for item in items),
            "avg_entry_slippage_bp": avg_slippage,
            "avg_net_return_pct": _avg(item.get("net_return_pct") for item in items),
            "avg_live_vs_dry_run_net_diff_pct": avg_net_diff,
            "live_worse_count": live_worse_count,
            "live_better_count": live_better_count,
            "live_worse_rate": _ratio(live_worse_count, total),
            "live_better_rate": _ratio(live_better_count, total),
            "no_fill_rate": _ratio(no_fill_count, total),
            "partial_fill_rate": _ratio(partial_count, total),
            "incomparable_rate": _ratio(incomparable_count, total),
            "entry_fill_quality_counts": dict(fill_counts),
            "exit_quality_counts": dict(exit_counts),
            "final_status_counts": dict(final_counts),
            "outcome_match_counts": dict(outcome_counts),
            "issue_counts": dict(issue_counts),
            "assumption_break_top": _counter_rows(
                Counter(
                    reason
                    for item in items
                    for reason in [
                        "fill_assumption_broken" if item.get("fill_assumption_broken") else "",
                        "slippage_assumption_broken" if item.get("slippage_assumption_broken") else "",
                        "delay_assumption_broken" if item.get("delay_assumption_broken") else "",
                        "exit_assumption_broken" if item.get("exit_assumption_broken") else "",
                    ]
                    if reason
                )
            ),
            "bad_ready_after_real_fill_count": sum(
                1
                for item in items
                if item.get("filled_quantity", 0) > 0
                and str(item.get("hybrid_status") or "").upper() == "READY"
                and (item.get("outcome_match") == "LIVE_WORSE" or item.get("fill_quality_grade") == "BAD")
            ),
        }

    def aggregate_grouped(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "by_fill_quality_grade": _group_counts(items, "fill_quality_grade"),
            "by_exit_quality_grade": _group_counts(items, "exit_quality_grade"),
            "by_outcome_match": _group_counts(items, "outcome_match"),
            "by_final_status": _group_counts(items, "final_status"),
            "by_issue_type": _issue_group_counts(items),
            "worst_net_diff": sorted(
                [
                    {
                        "case_id": item.get("case_id"),
                        "code": item.get("code"),
                        "net_return_diff_pct": item.get("net_return_diff_pct"),
                        "issue_types": item.get("issue_types", []),
                    }
                    for item in items
                    if item.get("net_return_diff_pct") is not None
                ],
                key=lambda row: float(row.get("net_return_diff_pct") or 0),
            )[:10],
        }

    def recommendations(self, summary: dict[str, Any], grouped: dict[str, Any]) -> list[str]:
        recs: list[str] = []
        if (summary.get("avg_entry_slippage_bp") or 0) >= self.config.acceptable_entry_slippage_bp:
            recs.append("entry slippage가 높습니다. 자동 변경 없이 max_entry_slippage_bp와 limit_price_policy 검토 후보로만 기록하세요.")
        if (summary.get("no_fill_rate") or 0) >= 0.2:
            recs.append("NO_FILL 비율이 높습니다. 호가 정책과 주문 제출 시점의 spread/liquidity를 장후 리뷰하세요.")
        if (summary.get("partial_fill_rate") or 0) >= 0.2:
            recs.append("부분체결 비율이 높습니다. 잔량 취소/리컨실 운영 절차와 체결 대기 시간을 확인하세요.")
        if (summary.get("live_worse_rate") or 0) >= 0.3:
            recs.append("LIVE_SIM이 DRY_RUN보다 나쁜 표본이 많습니다. 슬리피지, 지연, 청산 사유 변경을 분리해 다음 PR 검토 자료로 남기세요.")
        if summary.get("reconcile_required_count", 0) > 0 or summary.get("orphan_case_count", 0) > 0:
            recs.append("리컨실 또는 orphan 케이스가 있습니다. 설정 변경 전에 원장 연결 품질을 먼저 복구하세요.")
        if not recs:
            recs.append("현재 표본에서는 자동 설정 변경 신호를 만들지 않습니다. 누적 표본으로만 다음 PR 검토 후보를 판단하세요.")
        return recs

    def persist_report(self, report: dict) -> dict:
        return self.db.save_live_sim_canary_performance_report(report)

    def export_json(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "case_id",
            "trade_date",
            "code",
            "name",
            "theme",
            "hybrid_score",
            "requested_price",
            "avg_fill_price",
            "fill_ratio",
            "entry_slippage_bp",
            "exit_reason",
            "net_return_pct",
            "dry_run_net_return_pct",
            "net_return_diff_pct",
            "outcome_match",
            "final_status",
            "fill_quality_grade",
            "exit_quality_grade",
            "issue_types",
            "canary_decision_id",
            "order_intent_id",
            "gateway_command_id",
            "broker_order_id",
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in report.get("items", []):
                row = dict(item)
                row["issue_types"] = ",".join(str(value) for value in row.get("issue_types") or [])
                writer.writerow({column: row.get(column, "") for column in columns})
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        grouped = dict(report.get("grouped") or {})
        lines = [
            f"# LIVE_SIM Canary Post-Trade Report {report.get('trade_date') or ''}".strip(),
            "",
            "## 1. LIVE_SIM Canary Post-Trade Summary",
            f"- Generated at: {report.get('generated_at', '')}",
            f"- Report ID: {report.get('report_id', '')}",
            f"- Total lifecycles: {summary.get('total_lifecycle_count', 0)}",
            f"- Submitted / accepted / closed: {summary.get('submitted_count', 0)} / {summary.get('broker_accepted_count', 0)} / {summary.get('closed_count', 0)}",
            f"- Avg fill ratio: {_fmt(summary.get('avg_fill_ratio'))}",
            f"- Avg entry slippage bp: {_fmt(summary.get('avg_entry_slippage_bp'))}",
            f"- Avg LIVE_SIM net return pct: {_fmt(summary.get('avg_net_return_pct'))}",
            "",
            "## 2. Go/No-Go Context",
            "- This report is review-only and does not change LIVE_REAL, Hybrid thresholds, or strategy settings.",
            f"- Safety scope: {json.dumps(report.get('safety_scope', {}), ensure_ascii=False, sort_keys=True)}",
            "",
            "## 3. Entry Fill Quality",
            *_markdown_counts(grouped.get("by_fill_quality_grade", []), "key"),
            "",
            "## 4. Exit Fill Quality",
            *_markdown_counts(grouped.get("by_exit_quality_grade", []), "key"),
            "",
            "## 5. DRY_RUN vs LIVE_SIM Gap",
            *_markdown_counts(grouped.get("by_outcome_match", []), "key"),
            f"- Avg net_return_diff_pct: {_fmt(summary.get('avg_live_vs_dry_run_net_diff_pct'))}",
            "",
            "## 6. Slippage / Delay Reality Check",
            f"- SLIPPAGE_HIGH: {summary.get('issue_counts', {}).get('SLIPPAGE_HIGH', 0)}",
            f"- LATENCY_HIGH: {summary.get('issue_counts', {}).get('LATENCY_HIGH', 0)}",
            f"- STALE_TICK_ENTRY: {summary.get('issue_counts', {}).get('STALE_TICK_ENTRY', 0)}",
            "",
            "## 7. No-Fill / Partial-Fill Cases",
            f"- NO_FILL rate: {_pct(summary.get('no_fill_rate'))}",
            f"- PARTIAL_FILL rate: {_pct(summary.get('partial_fill_rate'))}",
            "",
            "## 8. Reconcile / Unknown / Orphan Cases",
            f"- RECONCILE_REQUIRED: {summary.get('reconcile_required_count', 0)}",
            f"- Orphan cases: {summary.get('orphan_case_count', 0)}",
            f"- UNKNOWN final: {summary.get('final_status_counts', {}).get('UNKNOWN', 0)}",
            "",
            "## 9. Bad READY After Real Fill",
            f"- Count: {summary.get('bad_ready_after_real_fill_count', 0)}",
            "",
            "## 10. Recommendations for Review Only",
            *[f"- {item}" for item in report.get("recommendations", [])],
        ]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    def export_report(self, report: dict, *, fmt: str = "json") -> dict[str, str]:
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        report_dir = self.report_root / trade_date
        stem = f"live_sim_canary_performance_{trade_date}"
        exports: dict[str, str] = {}
        formats = ["json", "csv", "md"] if fmt == "all" else [fmt]
        for item in formats:
            if item == "json":
                exports["json"] = str(self.export_json(report, report_dir / f"{stem}.json"))
            elif item == "csv":
                exports["csv"] = str(self.export_csv(report, report_dir / f"{stem}.csv"))
            elif item in {"md", "markdown"}:
                exports["md"] = str(self.export_markdown(report, report_dir / f"{stem}.md"))
        return exports

    def _dry_run_items(self, *, trade_date: str, code: Optional[str]) -> list[dict[str, Any]]:
        try:
            report = self.dry_run_analyzer.build_report(trade_date=trade_date, code=code, limit=10000)
            return list(report.get("items") or [])
        except Exception:
            return []

    def _price_ticks_by_code(self, *, trade_date: str, code: Optional[str]) -> dict[str, list[dict[str, Any]]]:
        clauses = ["trade_date = ?"]
        params: list[object] = [trade_date]
        if code:
            clauses.append("code = ?")
            params.append(code)
        rows = _select_dicts(
            self.db,
            f"""
            SELECT *
            FROM gateway_price_ticks
            WHERE {' AND '.join(clauses)}
            ORDER BY timestamp ASC, id ASC
            LIMIT 50000
            """,
            tuple(params),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            row["raw_payload"] = _safe_json_loads(row.get("raw_payload_json"), {})
            row["metadata"] = _safe_json_loads(row.get("metadata_json"), {})
            grouped[str(row.get("code") or "")].append(row)
        return grouped

    def _gateway_commands(self, *, trade_date: str, limit: int) -> list[dict[str, Any]]:
        rows = _select_dicts(
            self.db,
            """
            SELECT *
            FROM gateway_commands
            WHERE trade_date = ?
              AND command_type IN ('send_order', 'cancel_order')
            ORDER BY id DESC
            LIMIT ?
            """,
            (trade_date, max(1, int(limit or 1000))),
        )
        for row in rows:
            row["payload"] = _safe_json_loads(row.get("payload_json"), {})
            row["command"] = _safe_json_loads(row.get("command_json"), {})
            row["metadata"] = _safe_json_loads(row.get("metadata_json"), {})
            row["result_payload"] = _safe_json_loads(row.get("result_payload_json"), {})
        return rows

    def _gateway_command_events(self, *, trade_date: str, limit: int) -> list[dict[str, Any]]:
        rows = _select_dicts(
            self.db,
            """
            SELECT e.*
            FROM gateway_command_events e
            LEFT JOIN gateway_commands c ON c.command_id = e.command_id
            WHERE c.trade_date = ? OR substr(e.created_at, 1, 10) = ?
            ORDER BY e.id ASC
            LIMIT ?
            """,
            (trade_date, trade_date, max(1, int(limit or 1000))),
        )
        for row in rows:
            row["payload"] = _safe_json_loads(row.get("payload_json"), {})
        return rows

    def _hybrid_events(self, *, trade_date: str, code: Optional[str], limit: int) -> list[dict[str, Any]]:
        clauses = ["trade_date = ?"]
        params: list[object] = [trade_date]
        if code:
            clauses.append("stock_code = ?")
            params.append(code)
        rows = _select_dicts(
            self.db,
            f"""
            SELECT *
            FROM hybrid_gate_validation_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            LIMIT ?
            """,
            tuple(params + [max(1, int(limit or 1000))]),
        )
        for row in rows:
            row["details"] = _safe_json_loads(row.get("details_json"), {})
            row["hybrid_reason_codes"] = _safe_json_loads(row.get("hybrid_reason_codes_json"), [])
        return rows


class _OrderIndex:
    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self.orders = orders
        self.last_link: dict[str, Any] = {"matched_by": "", "link_confidence": ""}
        self.by_command = _first_map(orders, "command_id")
        self.by_intent = _first_map(orders, "order_intent_id")
        self.by_idempotency = _first_map(orders, "idempotency_key")
        self.by_broker = _first_map(orders, "broker_order_id")

    def find_for_decision(self, decision: dict[str, Any]) -> list[dict[str, Any]]:
        checks = [
            ("gateway_command_id", "gateway_command_id", self.by_command, "HIGH"),
            ("order_intent_id", "order_intent_id", self.by_intent, "HIGH"),
            ("idempotency_key", "idempotency_key", self.by_idempotency, "MEDIUM"),
            ("broker_order_no", "broker_order_no", self.by_broker, "MEDIUM"),
            ("order_no", "order_no", self.by_broker, "MEDIUM"),
        ]
        details = dict(decision.get("details") or decision.get("metadata") or {})
        for matched_by, key, index, confidence in checks:
            value = str(decision.get(key) or details.get(key) or "")
            if value and value in index:
                self.last_link = {"matched_by": matched_by, "link_confidence": confidence}
                return [index[value]]
        code = str(decision.get("code") or "")
        trade_date = str(decision.get("trade_date") or "")
        candidate_instance_id = str(decision.get("candidate_instance_id") or "")
        if candidate_instance_id:
            matches = [
                order
                for order in self.orders
                if str(order.get("candidate_instance_id") or "") == candidate_instance_id
                and str(order.get("code") or "") == code
                and str(order.get("trade_date") or "") == trade_date
                and str(order.get("side") or "").lower() == "buy"
            ]
            if matches:
                self.last_link = {"matched_by": "candidate_instance_id_code_trade_date", "link_confidence": "MEDIUM"}
                return matches[:1]
        candidate_id = decision.get("candidate_id")
        if candidate_id is not None:
            matches = [
                order
                for order in self.orders
                if str(order.get("candidate_id") or "") == str(candidate_id)
                and str(order.get("code") or "") == code
                and str(order.get("trade_date") or "") == trade_date
                and str(order.get("side") or "").lower() == "buy"
            ]
            if matches:
                self.last_link = {"matched_by": "candidate_id_code_trade_date", "link_confidence": "LOW"}
                return matches[:1]
        self.last_link = {"matched_by": "unlinked", "link_confidence": "NONE"}
        return []

    def related_orders(self, decision: dict[str, Any], linked_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        code = str(decision.get("code") or (linked_orders[0].get("code") if linked_orders else "") or "")
        trade_date = str(decision.get("trade_date") or (linked_orders[0].get("trade_date") if linked_orders else "") or "")
        candidate_instance_id = str(decision.get("candidate_instance_id") or (linked_orders[0].get("candidate_instance_id") if linked_orders else "") or "")
        candidate_id = decision.get("candidate_id") if decision.get("candidate_id") is not None else (linked_orders[0].get("candidate_id") if linked_orders else None)
        related = []
        for order in self.orders:
            if str(order.get("trade_date") or "") != trade_date or str(order.get("code") or "") != code:
                continue
            if candidate_instance_id and str(order.get("candidate_instance_id") or "") == candidate_instance_id:
                related.append(order)
            elif candidate_id is not None and str(order.get("candidate_id") or "") == str(candidate_id):
                related.append(order)
        return related


class _DryRunIndex:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.by_candidate_instance = _first_map(items, "candidate_instance_id")
        self.by_entry_intent = _first_map(items, "entry_intent_id")
        self.by_candidate_code_date: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in items:
            key = (str(item.get("candidate_id") or ""), str(item.get("code") or ""), str(item.get("trade_date") or ""))
            if key not in self.by_candidate_code_date:
                self.by_candidate_code_date[key] = item

    def find(
        self,
        *,
        decision: dict[str, Any],
        entry_order: Optional[dict[str, Any]],
        candidate_id: Any,
        code: str,
        trade_date: str,
    ) -> Optional[dict[str, Any]]:
        for value in (
            str(decision.get("candidate_instance_id") or ""),
            str((entry_order or {}).get("candidate_instance_id") or ""),
        ):
            if value and value in self.by_candidate_instance:
                return self.by_candidate_instance[value]
        for value in (
            str(decision.get("order_intent_id") or ""),
            str((entry_order or {}).get("order_intent_id") or ""),
            str((decision.get("details") or {}).get("dry_run_order_intent_id") or ""),
        ):
            if value and value in self.by_entry_intent:
                return self.by_entry_intent[value]
        return self.by_candidate_code_date.get((str(candidate_id or ""), code, trade_date))


def _issue(issue_type: str, case: dict[str, Any], details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    spec = ISSUE_CATALOG.get(issue_type, ISSUE_CATALOG["UNKNOWN_FINAL_STATUS"])
    return {
        "issue_type": issue_type,
        "severity": spec["severity"],
        "operator_message_ko": spec["operator_message_ko"],
        "recommended_action_ko": spec["recommended_action_ko"],
        "linked_evidence_fields": list(spec.get("evidence_fields") or []),
        "evidence": {
            key: case.get(key)
            for key in list(spec.get("evidence_fields") or [])
            if key in case
        },
        "details": details or {},
    }


def _orphan_execution_case(fill: dict[str, Any], *, trade_date: str) -> dict[str, Any]:
    code = str(fill.get("code") or "")
    case = {
        "case_id": f"orphan_execution:{trade_date}:{code}:{fill.get('broker_order_id') or fill.get('fill_id') or fill.get('id')}",
        "lifecycle_id": f"orphan_execution:{trade_date}:{code}:{fill.get('broker_order_id') or fill.get('fill_id') or fill.get('id')}",
        "trade_date": trade_date,
        "code": code,
        "name": "",
        "candidate_instance_id": str(fill.get("candidate_instance_id") or ""),
        "canary_decision_id": "",
        "order_intent_id": str(fill.get("order_intent_id") or ""),
        "gateway_command_id": "",
        "broker_order_id": str(fill.get("broker_order_id") or ""),
        "matched_by": "orphan_execution",
        "link_confidence": "NONE",
        "submitted_at": "",
        "broker_accepted_at": "",
        "first_fill_at": str(fill.get("event_time") or fill.get("received_at") or ""),
        "requested_price": 0,
        "requested_quantity": 0,
        "filled_quantity": int(fill.get("fill_qty") or 0),
        "avg_fill_price": _first_float(fill.get("fill_price")),
        "fill_ratio": None,
        "partial_fill": False,
        "no_fill": False,
        "cancelled_before_fill": False,
        "fill_quality_grade": "UNKNOWN",
        "exit_quality_grade": "UNKNOWN",
        "outcome_match": "INCOMPARABLE",
        "final_status": "RECONCILE_REQUIRED",
        "entry_fills": [_public_fill(fill)],
        "exit_fills": [],
        "issues": [],
        "raw_metadata": _redact_sensitive({"fill": fill}),
        "linked_ids": {
            "canary_decision_id": "",
            "intent_id": str(fill.get("order_intent_id") or ""),
            "command_id": "",
            "order_no": str(fill.get("broker_order_id") or ""),
            "execution_ids": [str(fill.get("fill_id") or fill.get("event_id") or "")],
        },
    }
    case["issues"] = [_issue("ORPHAN_EXECUTION", case, {"fill": _public_fill(fill)})]
    case["issue_types"] = _issue_types(case["issues"])
    return case


def _decision_from_orphan_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": "",
        "trade_date": str(order.get("trade_date") or ""),
        "code": str(order.get("code") or ""),
        "name": str(order.get("name") or ""),
        "candidate_id": order.get("candidate_id"),
        "candidate_instance_id": str(order.get("candidate_instance_id") or ""),
        "order_intent_id": str(order.get("order_intent_id") or ""),
        "gateway_command_id": str(order.get("command_id") or ""),
        "limit_price": int(order.get("requested_price") or 0),
        "quantity": int(order.get("requested_qty") or 0),
        "details": {"orphan_order": True},
    }


def _best_entry_order(orders: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not orders:
        return None
    return sorted(orders, key=lambda item: str(item.get("submitted_at") or item.get("created_at") or ""))[0]


def _fills_for_orders(orders: list[dict[str, Any]], fills_by_order: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    for order in orders:
        order_id = str(order.get("order_intent_id") or "")
        broker_id = str(order.get("broker_order_id") or "")
        fills.extend(fills_by_order.get(order_id, []))
        if broker_id:
            fills.extend(fills_by_order.get(f"broker:{broker_id}", []))
    return fills


def _fills_by_order(fills: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_broker = {str(order.get("broker_order_id") or ""): str(order.get("order_intent_id") or "") for order in orders if order.get("broker_order_id")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        order_id = str(fill.get("order_intent_id") or "") or by_broker.get(str(fill.get("broker_order_id") or ""), "")
        if order_id:
            grouped[order_id].append(fill)
        broker_id = str(fill.get("broker_order_id") or "")
        if broker_id:
            grouped[f"broker:{broker_id}"].append(fill)
    return grouped


def _dedupe_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda item: (str(item.get("event_time") or item.get("received_at") or ""), int(item.get("id") or 0))):
        key = (
            str(fill.get("broker_order_id") or ""),
            str(fill.get("fill_id") or fill.get("event_id") or ""),
            str(fill.get("id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(fill)
    return result


def _positions_by_key(positions: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for position in positions:
        grouped[(str(position.get("candidate_instance_id") or ""), str(position.get("code") or ""))].append(position)
    return grouped


def _best_position(grouped: dict[tuple[str, str], list[dict[str, Any]]], *, code: str, candidate_instance_id: str) -> Optional[dict[str, Any]]:
    candidates = grouped.get((candidate_instance_id, code), [])
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: str(item.get("updated_at") or item.get("opened_at") or ""), reverse=True)[0]


def _latest_hybrid_by_key(events: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        details = dict(event.get("details") or {})
        candidate_instance_id = str(details.get("candidate_instance_id") or event.get("candidate_instance_id") or "")
        code = str(event.get("stock_code") or event.get("code") or "")
        result[(candidate_instance_id, code)] = event
        result[("", code)] = event
    return result


def _exit_reason(exit_orders: list[dict[str, Any]], intents_by_id: dict[str, dict[str, Any]], position: Optional[dict[str, Any]]) -> str:
    for order in exit_orders:
        intent = intents_by_id.get(str(order.get("order_intent_id") or ""))
        for value in (
            (intent or {}).get("exit_reason"),
            (intent or {}).get("exit_decision_type"),
            order.get("exit_reason"),
            (order.get("details") or {}).get("exit_reason"),
            (order.get("details") or {}).get("exit_decision_type"),
        ):
            if value:
                return str(value)
    details = dict((position or {}).get("details") or {})
    return str(details.get("exit_reason") or details.get("last_exit_reason") or "")


def _timeline_for_orders(orders: list[dict[str, Any]], command_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in orders:
        for key, label in (
            ("created_at", "ORDER_CREATED"),
            ("submitted_at", "ORDER_SUBMITTED"),
            ("accepted_at", "BROKER_ACCEPTED"),
            ("first_fill_at", "FIRST_FILL_RECORDED"),
            ("last_fill_at", "LAST_FILL_RECORDED"),
            ("cancelled_at", "ORDER_CANCELLED"),
            ("updated_at", "ORDER_UPDATED"),
        ):
            if order.get(key):
                rows.append({"at": str(order.get(key)), "type": label, "order_intent_id": order.get("order_intent_id"), "status": order.get("order_status")})
    for event in command_events:
        rows.append({"at": str(event.get("created_at") or ""), "type": f"COMMAND_{event.get('event_type')}", "command_id": event.get("command_id"), "status": event.get("status_to")})
    return sorted(rows, key=lambda item: str(item.get("at") or ""))


def _timeline_for_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "at": str(fill.get("event_time") or fill.get("received_at") or ""),
            "type": "FILL",
            "side": fill.get("side"),
            "fill_qty": fill.get("fill_qty"),
            "fill_price": fill.get("fill_price"),
            "remaining_qty": fill.get("remaining_qty"),
            "fill_id": fill.get("fill_id"),
        }
        for fill in fills
    ]


def _public_order(order: dict[str, Any]) -> dict[str, Any]:
    return _redact_sensitive(
        {
            key: order.get(key)
            for key in (
                "order_intent_id",
                "command_id",
                "candidate_instance_id",
                "trade_date",
                "code",
                "name",
                "side",
                "requested_qty",
                "requested_price",
                "submitted_qty",
                "submitted_price",
                "broker_order_id",
                "order_status",
                "submitted_at",
                "accepted_at",
                "first_fill_at",
                "last_fill_at",
                "cancelled_at",
                "updated_at",
                "reason_codes",
                "details",
            )
        }
    )


def _public_fill(fill: dict[str, Any]) -> dict[str, Any]:
    return _redact_sensitive(
        {
            key: fill.get(key)
            for key in (
                "order_intent_id",
                "broker_order_id",
                "fill_id",
                "event_id",
                "code",
                "side",
                "fill_qty",
                "fill_price",
                "cumulative_fill_qty",
                "remaining_qty",
                "commission",
                "tax",
                "event_time",
                "received_at",
            )
        }
    )


def _public_cancel(cancel: dict[str, Any]) -> dict[str, Any]:
    return _redact_sensitive(
        {
            key: cancel.get(key)
            for key in (
                "cancel_intent_id",
                "original_order_id",
                "broker_order_id",
                "command_id",
                "trade_date",
                "code",
                "side",
                "cancel_qty",
                "cancel_reason",
                "status",
                "attempts",
                "created_at",
                "submitted_at",
                "accepted_at",
                "updated_at",
                "reason_codes",
                "details",
            )
        }
    )


def _case_id(decision: dict[str, Any], entry_order: Optional[dict[str, Any]], code: str, trade_date: str) -> str:
    return (
        str(decision.get("decision_id") or "")
        or str((entry_order or {}).get("order_intent_id") or "")
        or str((entry_order or {}).get("command_id") or "")
        or f"live_sim_canary:{trade_date}:{code}:{decision.get('candidate_instance_id') or decision.get('candidate_id') or 'unlinked'}"
    )


def _select_dicts(db: TradingDatabase, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        rows = db.conn.execute(query, params).fetchall()
    except Exception:
        return []
    return [dict(row) for row in rows]


def _safe_json_loads(value: object, default):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _redact_sensitive(value: Any) -> Any:
    sensitive = ("account", "token", "secret", "password", "authorization", "credential")
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(token in key_text.lower() for token in sensitive):
                redacted[key_text] = _mask_text(item)
            else:
                redacted[key_text] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _mask_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


def _first_map(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if value and value not in result:
            result[value] = row
    return result


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "")].append(row)
    return grouped


def _group_counts(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counter = Counter(str(item.get(key) or "UNKNOWN") for item in items)
    return [{"key": value, "count": int(count)} for value, count in counter.most_common()]


def _issue_group_counts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter = Counter(issue.get("issue_type") for item in items for issue in item.get("issues", []))
    return [{"issue_type": value, "count": int(count)} for value, count in counter.most_common() if value]


def _counter_rows(counter: Counter[str], limit: int = 10) -> list[dict[str, Any]]:
    return [{"reason": key, "count": int(count)} for key, count in counter.most_common(limit) if key]


def _issue_types(issues: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for issue in issues:
        issue_type = str(issue.get("issue_type") or "")
        if issue_type and issue_type not in result:
            result.append(issue_type)
    return result


def _first_time(values: Iterable[Any]) -> str:
    parsed = sorted(str(value) for value in values if value)
    return parsed[0] if parsed else ""


def _last_time(values: Iterable[Any]) -> str:
    parsed = sorted(str(value) for value in values if value)
    return parsed[-1] if parsed else ""


def _duration_ms(start: Any, end: Any) -> Optional[int]:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() * 1000))


def _age_sec(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds()), 3)


def _hold_minutes(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds() / 60.0), 3)


def _parse_time(value: Any) -> Optional[datetime]:
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


def _first_int(*values: Any) -> int:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            continue
    return 0


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "")
        if text:
            return text
    return ""


def _nested(item: Optional[dict[str, Any]], key: str) -> Any:
    return (item or {}).get(key)


def _price_diff_pct(actual: Any, base: Any) -> Optional[float]:
    actual_value = _first_float(actual)
    base_value = _first_float(base)
    if actual_value is None or base_value is None or base_value == 0:
        return None
    return round((actual_value - base_value) / base_value * 100.0, 6)


def _exit_slippage_pct(actual: Any, requested: Any) -> Optional[float]:
    return _price_diff_pct(actual, requested)


def _bp(pct: Any) -> Optional[float]:
    value = _first_float(pct)
    if value is None:
        return None
    return round(value * 100.0, 4)


def _ratio(numerator: int | float, denominator: int | float) -> Optional[float]:
    if denominator is None or float(denominator) <= 0:
        return None
    return round(float(numerator) / float(denominator), 6)


def _diff(left: Any, right: Any) -> Optional[float]:
    left_value = _first_float(left)
    right_value = _first_float(right)
    if left_value is None or right_value is None:
        return None
    return round(left_value - right_value, 6)


def _avg(values: Iterable[Any]) -> Optional[float]:
    parsed = [_first_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(sum(parsed) / len(parsed), 6)


def _nearest_tick(ticks: list[dict[str, Any]], submitted_at: str) -> Optional[dict[str, Any]]:
    submitted = _parse_time(submitted_at)
    if submitted is None:
        return ticks[-1] if ticks else None
    before = []
    after = []
    for tick in ticks:
        tick_time = _parse_time(tick.get("timestamp") or tick.get("received_at"))
        if tick_time is None:
            continue
        if tick_time <= submitted:
            before.append((tick_time, tick))
        else:
            after.append((tick_time, tick))
    if before:
        return sorted(before, key=lambda pair: pair[0])[-1][1]
    if after:
        return sorted(after, key=lambda pair: pair[0])[0][1]
    return None


def _spread(tick: Optional[dict[str, Any]]) -> Optional[float]:
    if not tick:
        return None
    if _first_float(tick.get("spread_ticks")) is not None:
        return _first_float(tick.get("spread_ticks"))
    bid = _first_float(tick.get("best_bid"))
    ask = _first_float(tick.get("best_ask"))
    if bid is None or ask is None or ask <= 0 or bid <= 0:
        return None
    return round(max(0.0, ask - bid), 6)


def _liquidity_bucket(tick: Optional[dict[str, Any]]) -> str:
    if not tick:
        return "UNKNOWN"
    value = _first_float(tick.get("trade_value"), tick.get("cum_volume"))
    if value is None:
        return "UNKNOWN"
    if value >= 10_000_000_000:
        return "HIGH"
    if value >= 1_000_000_000:
        return "MEDIUM"
    return "LOW"


def _dry_run_fill_assumption(item: Optional[dict[str, Any]]) -> str:
    if not item:
        return ""
    realism = dict(item.get("execution_realism") or {})
    if realism:
        return json.dumps(
            {
                "limit_price_hit": realism.get("limit_price_hit"),
                "partial_fill_risk": realism.get("partial_fill_risk"),
                "spread_risk": realism.get("spread_risk"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return "ASSUMED_FILLED" if item.get("entry_intent_id") else ""


def _live_fill_result(entry: dict[str, Any]) -> str:
    if entry.get("no_fill"):
        return "NO_FILL"
    if entry.get("partial_fill"):
        return "PARTIAL_FILL"
    if (entry.get("fill_ratio") or 0) >= 1:
        return "FULL_FILL"
    return "UNKNOWN"


def _fmt(value: Any) -> str:
    parsed = _first_float(value)
    return "-" if parsed is None else f"{parsed:.4f}"


def _pct(value: Any) -> str:
    parsed = _first_float(value)
    return "-" if parsed is None else f"{parsed * 100.0:.2f}%"


def _markdown_counts(rows: list[dict[str, Any]], key: str) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get(key) or row.get('issue_type')}: {row.get('count', 0)}" for row in rows]
