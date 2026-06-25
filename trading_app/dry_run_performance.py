from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase
from trading.broker.models import new_message_id, utc_timestamp
from trading.strategy.costs import RoundTripCostConfig, round_trip_cost_pct
from trading.strategy.models import ReviewFinalStatus
from trading.strategy.reason_taxonomy import normalize_reason_status, reason_status_family, reason_summary
from trading_app.fill_simulator import FillSimulationConfig, simulate_fill, summarize_fill_simulations


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "dry_run_performance"


@dataclass(frozen=True)
class DryRunPerformanceConfig:
    fp_loss_threshold_pct: float = -1.0
    fp_drawdown_threshold_pct: float = -3.0
    fn_rally_threshold_pct: float = 3.0
    good_trade_threshold_pct: float = 2.0
    min_hold_minutes_for_final: int = 20
    pending_grace_minutes: int = 30
    commission_bp_per_side: float = 1.5
    sell_tax_bp: float = 15.0
    slippage_scenarios_bp: tuple[float, ...] = (0.0, 10.0, 20.0, 30.0)
    entry_delay_scenarios_sec: tuple[int, ...] = (0, 1, 3, 5)
    primary_slippage_bp: float = 10.0
    primary_entry_delay_sec: int = 0
    min_go_trade_days: int = 5
    min_go_accepted_entry_lifecycles: int = 30
    max_go_bad_ready_rate: float = 0.25
    max_go_opportunity_loss_rate: float = 0.25
    max_go_stale_tick_rate: float = 0.2
    max_go_latency_distortion_rate: float = 0.2
    stale_tick_age_sec: float = 3.0
    gateway_latency_warn_ms: float = 1000.0


@dataclass
class DryRunTradeLifecycle:
    lifecycle_id: str
    trade_date: str = ""
    code: str = ""
    name: str = ""
    strategy_name: str = ""
    candidate_id: Optional[int] = None
    candidate_instance_id: str = ""
    candidate_generation_seq: Optional[int] = None
    matched_by: str = ""
    link_confidence: str = ""
    attribution_confidence: str = ""
    legacy_low_confidence_sample: bool = False
    theme_name: str = ""
    theme_score: Optional[float] = None
    gate_status: str = ""
    gate_reason: str = ""
    reason_status: str = ""
    reason_family: str = ""
    gate_score: Optional[float] = None
    hybrid_score: Optional[float] = None
    hybrid_status: str = ""
    hybrid_position_tier: str = ""
    hybrid_reason_codes: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    membership_score: Optional[float] = None
    theme_score_bucket: str = ""
    membership_score_bucket: str = ""
    stock_role: str = ""
    session_bucket: str = ""
    entry_intent_id: str = ""
    entry_intent_status: str = ""
    entry_live_would_pass: bool = False
    entry_live_reject_reason: str = ""
    entry_decision_safety_reason: str = ""
    entry_price: int = 0
    entry_quantity: int = 0
    entry_amount: int = 0
    virtual_order_id: Optional[int] = None
    virtual_position_id: Optional[int] = None
    position_entry_price: Optional[int] = None
    position_quantity: Optional[int] = None
    opened_at: str = ""
    closed_at: str = ""
    exit_intent_ids: list[str] = field(default_factory=list)
    exit_decision_ids: list[int] = field(default_factory=list)
    exit_decision_types: list[str] = field(default_factory=list)
    exit_reasons: list[str] = field(default_factory=list)
    exit_price: int = 0
    exit_quantity_total: int = 0
    exit_amount_total: int = 0
    realized_return_pct: Optional[float] = None
    net_return_pct: Optional[float] = None
    net_expectancy_component: Optional[float] = None
    net_opportunity_return_pct: Optional[float] = None
    cost_adjusted_classification: str = ""
    net_bad_ready_type: str = ""
    net_opportunity_type: str = ""
    cost_scenarios: list[dict[str, Any]] = field(default_factory=list)
    max_return_5m: Optional[float] = None
    max_return_10m: Optional[float] = None
    max_return_20m: Optional[float] = None
    max_drawdown_20m: Optional[float] = None
    max_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    final_status: str = ""
    trade_review_id: Optional[int] = None
    false_positive_flag: bool = False
    false_negative_flag: bool = False
    blocked_but_later_rallied: bool = False
    expired_but_later_rallied: bool = False
    dry_run_false_positive_type: str = ""
    dry_run_false_negative_type: str = ""
    opportunity_loss_type: str = ""
    signal_classification: str = ""
    quality_bucket: str = ""
    score_bucket: str = ""
    hold_minutes: Optional[float] = None
    data_status: str = "OK"
    data_quality_issues: list[str] = field(default_factory=list)
    data_quality_issue_reasons: dict[str, str] = field(default_factory=dict)
    review_false_positive_mismatch: bool = False
    review_false_negative_mismatch: bool = False
    limit_price_hit: Optional[bool] = None
    partial_fill_risk: str = ""
    spread_risk: str = ""
    liquidity_bucket: str = ""
    entry_tick_age_sec: Optional[float] = None
    gateway_command_latency_ms: Optional[float] = None
    execution_realism: dict[str, Any] = field(default_factory=dict)
    fill_simulation: dict[str, Any] = field(default_factory=dict)
    fill_price: Optional[float] = None
    requested_price: Optional[float] = None
    slippage_bps: Optional[float] = None
    fill_ratio: Optional[float] = None
    partial_fill: bool = False
    simulated_latency_ms: Optional[float] = None
    reject_or_skip_reason: str = ""
    stale_tick: bool = False
    fill_adjusted_return_pct: Optional[float] = None
    fill_adjusted_net_return_pct: Optional[float] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AttributionLink:
    key: str
    matched_by: str
    link_confidence: str
    issue: str = ""
    candidate_instance_id: str = ""
    candidate_generation_seq: Optional[int] = None


class DryRunFalseSignalClassifier:
    def __init__(self, config: Optional[DryRunPerformanceConfig] = None) -> None:
        self.config = config or DryRunPerformanceConfig()

    def classify(self, lifecycle: DryRunTradeLifecycle) -> dict[str, Any]:
        entry_exists = bool(lifecycle.entry_intent_id)
        entry_accepted = lifecycle.entry_intent_status in {"DRY_RUN_ACCEPTED", "ACCEPTED"}
        entry_rejected = lifecycle.entry_intent_status in {"DRY_RUN_REJECTED", "REJECTED", "LIVE_BLOCKED", "DUPLICATE"}
        live_rejected = entry_exists and not lifecycle.entry_live_would_pass
        max_return = _first_float(lifecycle.max_return_20m, lifecycle.max_return_10m, lifecycle.max_return_5m)
        drawdown = _first_float(lifecycle.max_drawdown_20m, lifecycle.max_drawdown_pct)
        realized = _first_float(lifecycle.realized_return_pct)
        final_status = lifecycle.final_status
        reason_text = " ".join(
            [
                lifecycle.gate_reason,
                lifecycle.entry_decision_safety_reason,
                lifecycle.entry_live_reject_reason,
                " ".join(lifecycle.exit_reasons),
            ]
        ).upper()

        false_positive_type = ""
        false_negative_type = ""
        opportunity_loss_type = ""
        signal_classification = "pending"

        if entry_exists and entry_accepted:
            if lifecycle.entry_live_would_pass and final_status == ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value:
                false_positive_type = "LIVE_WOULD_PASS_BUT_SUPPORT_LOSS"
            elif drawdown is not None and drawdown <= self.config.fp_drawdown_threshold_pct:
                false_positive_type = "LIVE_WOULD_PASS_BUT_DRAWDOWN" if lifecycle.entry_live_would_pass else "DRY_RUN_ACCEPTED_BUT_DRAWDOWN"
            elif realized is not None and realized <= self.config.fp_loss_threshold_pct:
                false_positive_type = "LIVE_WOULD_PASS_BUT_NEGATIVE_RETURN" if lifecycle.entry_live_would_pass else "DRY_RUN_ACCEPTED_BUT_NEGATIVE_RETURN"
            elif not lifecycle.exit_intent_ids and drawdown is not None and drawdown <= self.config.fp_drawdown_threshold_pct:
                false_positive_type = "DRY_RUN_ACCEPTED_BUT_NO_EXIT_AND_DRAWDOWN"
            elif final_status == ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value and (realized or 0.0) < self.config.good_trade_threshold_pct:
                false_positive_type = "ENTRY_ACCEPTED_BUT_TIME_EXIT_WEAK"
            elif any(token in reason_text for token in ("LATE_CHASE", "LATE_LAGGARD", "CHASE_RISK")) and (
                (realized is not None and realized <= 0.0)
                or (drawdown is not None and drawdown <= self.config.fp_drawdown_threshold_pct / 2.0)
            ):
                false_positive_type = "LATE_CHASE_FALSE_POSITIVE"

            if false_positive_type:
                signal_classification = "false_positive"
            elif _is_take_profit_status(final_status, lifecycle.exit_decision_types) or (
                realized is not None and realized >= self.config.good_trade_threshold_pct
            ):
                signal_classification = "true_positive"
            elif lifecycle.data_status != "OK":
                signal_classification = "pending"

        if entry_exists and entry_rejected and max_return is not None and max_return >= self.config.fn_rally_threshold_pct:
            false_negative_type = "DRY_RUN_REJECTED_BUT_RALLIED"
            opportunity_loss_type = "SAFETY_REJECT_REASON_OPPORTUNITY_LOSS"
            signal_classification = "false_negative"
        if live_rejected and max_return is not None and max_return >= self.config.fn_rally_threshold_pct:
            false_negative_type = false_negative_type or "LIVE_REJECTED_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "LIVE_REJECTED_BUT_RALLIED"
            signal_classification = "false_negative"
        if not entry_exists and lifecycle.trade_review_id and max_return is not None and max_return >= self.config.fn_rally_threshold_pct:
            false_negative_type = false_negative_type or "NO_ENTRY_INTENT_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "NO_ENTRY_INTENT_BUT_RALLIED"
            signal_classification = "false_negative"
        if lifecycle.blocked_but_later_rallied and entry_exists:
            false_negative_type = false_negative_type or "GATE_BLOCKED_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "GATE_BLOCKED_BUT_RALLIED"
            signal_classification = "false_negative"
        if lifecycle.expired_but_later_rallied:
            false_negative_type = false_negative_type or "EXPIRED_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "EXPIRED_BUT_RALLIED"
            signal_classification = "false_negative"
        if signal_classification == "pending":
            if lifecycle.data_status == "OK" and (not entry_exists or entry_rejected) and (max_return is None or max_return < self.config.fn_rally_threshold_pct):
                signal_classification = "true_negative"
            elif lifecycle.data_status != "OK":
                signal_classification = "insufficient_data"

        if false_positive_type and lifecycle.false_positive_flag is False:
            lifecycle.review_false_positive_mismatch = True
        if false_negative_type and lifecycle.false_negative_flag is False:
            lifecycle.review_false_negative_mismatch = True

        return {
            "dry_run_false_positive_type": false_positive_type,
            "dry_run_false_negative_type": false_negative_type,
            "opportunity_loss_type": opportunity_loss_type,
            "signal_classification": signal_classification,
            "quality_bucket": _quality_bucket(signal_classification, false_positive_type, false_negative_type, lifecycle.data_status),
        }


class DryRunPerformanceAnalyzer:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: Optional[DryRunPerformanceConfig] = None,
        report_root: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.config = config or DryRunPerformanceConfig()
        self.classifier = DryRunFalseSignalClassifier(self.config)
        self.report_root = report_root or REPORT_ROOT

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        strategy_name: Optional[str] = None,
        code: Optional[str] = None,
        theme_name: Optional[str] = None,
        session_bucket: Optional[str] = None,
        side: Optional[str] = None,
        order_phase: Optional[str] = None,
        include_rejected: bool = True,
        include_duplicates: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        generated_at = utc_timestamp()
        report_id = new_message_id("dryrun_report")
        filters = {
            "trade_date": trade_date or "",
            "strategy_name": strategy_name or "",
            "code": code or "",
            "theme_name": theme_name or "",
            "session_bucket": session_bucket or "",
            "side": side or "",
            "order_phase": order_phase or "",
            "include_rejected": include_rejected,
            "include_duplicates": include_duplicates,
            "limit": int(limit or 100),
            "offset": int(offset or 0),
        }
        lifecycles = self.build_lifecycles(
            trade_date=trade_date,
            strategy_name=strategy_name,
            code=code,
            side=side,
            order_phase=order_phase,
            include_rejected=include_rejected,
            include_duplicates=include_duplicates,
        )
        items = [item.to_dict() for item in lifecycles]
        items = [
            item
            for item in items
            if (not theme_name or item.get("theme_name") == theme_name)
            and (not session_bucket or item.get("session_bucket") == session_bucket)
        ]
        summary = self.aggregate_summary(items)
        grouped = self.aggregate_grouped(items)
        false_signal_summary = self.aggregate_false_signals(items)
        data_quality = self.data_quality(items)
        prune_summary = self.db.latest_position_context_prune_summary() if hasattr(self.db, "latest_position_context_prune_summary") else {}
        recommendations = self.recommendations(summary, grouped, false_signal_summary)
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "report_id": report_id,
            "status": "READY",
            "review_only": True,
            "safety_scope": {
                "live_real_order_activation": False,
                "gateway_send_order_created": False,
                "strategy_settings_auto_change": False,
            },
            "disclaimer_ko": "분석 전용 리포트입니다. 추천/진단은 자동 적용하지 않으며 LIVE_REAL 주문, Gateway send_order, 전략 설정 변경을 생성하지 않습니다.",
            "generated_at": generated_at,
            "trade_date": trade_date or "",
            "filters": filters,
            "summary": {**summary, "data_quality": data_quality, "context_history_prune": prune_summary},
            "false_signal_summary": false_signal_summary,
            "grouped": grouped,
            "recommendations": recommendations,
            "items": items[start:end],
            "total_items": len(items),
        }

    def build_lifecycles(
        self,
        *,
        trade_date: Optional[str] = None,
        strategy_name: Optional[str] = None,
        code: Optional[str] = None,
        side: Optional[str] = None,
        order_phase: Optional[str] = None,
        include_rejected: bool = True,
        include_duplicates: bool = False,
    ) -> list[DryRunTradeLifecycle]:
        intents = self.db.list_runtime_order_intents_for_analysis(
            trade_date=trade_date,
            strategy_name=strategy_name,
            code=code,
            side=side,
            order_phase=order_phase,
            include_rejected=include_rejected,
            include_duplicates=include_duplicates,
            limit=10000,
        )
        reviews = self.db.list_trade_reviews_for_analysis(
            trade_date=trade_date,
            strategy_name=strategy_name,
            code=code,
            limit=10000,
        )
        positions = {position.id: position for position in self.db.list_virtual_positions_for_analysis() if position.id is not None}
        position_by_order = {
            position.virtual_order_id: position
            for position in positions.values()
            if position.virtual_order_id is not None
        }
        positions_by_candidate: dict[int, list[Any]] = {}
        for position in positions.values():
            if position.candidate_id is not None:
                positions_by_candidate.setdefault(int(position.candidate_id), []).append(position)
        decisions_by_position: dict[int, list[Any]] = {}
        for decision in self.db.list_exit_decisions_for_analysis():
            if decision.virtual_position_id is not None:
                decisions_by_position.setdefault(decision.virtual_position_id, []).append(decision)
        hybrid_events_by_code_date = _hybrid_events_by_code_date(
            self.db,
            trade_date=trade_date,
            code=code,
            limit=20000,
        )
        ticks_by_code_date = _gateway_price_ticks_by_code_date(
            self.db,
            trade_date=trade_date,
            code=code,
            limit=50000,
        )
        context_history_by_position: dict[int, list[Any]] = {}
        if hasattr(self.db, "list_position_context_history_for_analysis"):
            histories = self.db.list_position_context_history_for_analysis(
                trade_date=trade_date,
                position_ids=positions.keys(),
                limit=10000,
            )
            for snapshot in histories:
                if snapshot.position_id is not None:
                    context_history_by_position.setdefault(int(snapshot.position_id), []).append(snapshot)
        candidates = {
            candidate.id: candidate
            for candidate in self.db.list_candidates(trade_date=trade_date)
            if candidate.id is not None
        }
        instance_ids_by_code_date = _candidate_instances_by_code_date(intents, reviews, positions.values(), candidates.values())
        candidate_key_by_code_date = _single_candidate_key_by_code_date(intents, reviews, candidates.values())
        groups: dict[str, dict[str, list[Any]]] = {}

        def bucket(link: AttributionLink) -> dict[str, list[Any]]:
            group = groups.setdefault(link.key, {"intents": [], "reviews": [], "links": []})
            group["links"].append(link)
            return group

        review_key_by_id = {
            int(review.id): _lifecycle_link_for_review(review, position_by_order, instance_ids_by_code_date, candidate_key_by_code_date).key
            for review in reviews
            if review.id is not None
        }

        for intent in intents:
            bucket(_lifecycle_link_for_intent(intent, position_by_order, review_key_by_id, instance_ids_by_code_date, candidate_key_by_code_date))["intents"].append(intent)
        for review in reviews:
            bucket(_lifecycle_link_for_review(review, position_by_order, instance_ids_by_code_date, candidate_key_by_code_date))["reviews"].append(review)

        lifecycles: list[DryRunTradeLifecycle] = []
        for key, group in groups.items():
            lifecycle = self._build_lifecycle(
                key,
                list(group.get("intents") or []),
                list(group.get("reviews") or []),
                positions,
                position_by_order,
                positions_by_candidate,
                decisions_by_position,
                hybrid_events_by_code_date,
                ticks_by_code_date,
                context_history_by_position,
                candidates,
                list(group.get("links") or []),
            )
            classification = self.classifier.classify(lifecycle)
            lifecycle.dry_run_false_positive_type = classification["dry_run_false_positive_type"]
            lifecycle.dry_run_false_negative_type = classification["dry_run_false_negative_type"]
            lifecycle.opportunity_loss_type = classification["opportunity_loss_type"]
            lifecycle.signal_classification = classification["signal_classification"]
            lifecycle.quality_bucket = classification["quality_bucket"]
            lifecycles.append(lifecycle)
        lifecycles.sort(key=lambda item: (item.trade_date, item.code, item.lifecycle_id), reverse=True)
        return lifecycles

    def _build_lifecycle(
        self,
        key: str,
        intents: list[dict],
        reviews: list[Any],
        positions: dict[int, Any],
        position_by_order: dict[int, Any],
        positions_by_candidate: dict[int, list[Any]],
        decisions_by_position: dict[int, list[Any]],
        hybrid_events_by_code_date: dict[str, list[dict[str, Any]]],
        ticks_by_code_date: dict[str, list[dict[str, Any]]],
        context_history_by_position: dict[int, list[Any]],
        candidates: dict[int, Any],
        attribution_links: list[AttributionLink],
    ) -> DryRunTradeLifecycle:
        entry_intents = [intent for intent in intents if _intent_phase(intent) == "entry" or str(intent.get("side")) == "buy"]
        exit_intents = [intent for intent in intents if _intent_phase(intent) == "exit" or str(intent.get("side")) == "sell"]
        entry_intents.sort(key=lambda item: int(item.get("id") or 0))
        exit_intents.sort(key=lambda item: int(item.get("id") or 0))
        review = sorted(reviews, key=lambda item: int(item.id or 0), reverse=True)[0] if reviews else None
        entry = entry_intents[0] if entry_intents else None

        position = _position_for_group(entry, exit_intents, review, positions, position_by_order, positions_by_candidate)
        candidate_id = _first_not_none(
            entry.get("candidate_id") if entry else None,
            review.candidate_id if review else None,
            position.candidate_id if position else None,
            *[intent.get("candidate_id") for intent in exit_intents],
        )
        candidate = candidates.get(int(candidate_id)) if candidate_id is not None else None
        position_decisions = decisions_by_position.get(int(position.id), []) if position and position.id is not None else []
        position_context_history = context_history_by_position.get(int(position.id), []) if position and position.id is not None else []
        context_capture_reasons = _unique(str(snapshot.capture_reason or "") for snapshot in position_context_history)
        context_risk_exit_details = [
            {
                "decision_type": str(decision.decision_type or ""),
                "exit_confidence": str((decision.details or {}).get("exit_confidence") or ""),
                "context_limited_reason": str((decision.details or {}).get("context_limited_reason") or ""),
                "context_history_count": _first_int((decision.details or {}).get("context_history_count")),
                "theme_score_delta": _optional_float((decision.details or {}).get("theme_score_delta")),
                "leader_count_delta": _first_int((decision.details or {}).get("leader_count_delta")),
                "index_status_deterioration": bool((decision.details or {}).get("index_status_deterioration")),
                "required_confirmation_cycles": _first_int((decision.details or {}).get("required_confirmation_cycles")),
                "observed_confirmation_cycles": _first_int((decision.details or {}).get("observed_confirmation_cycles")),
                "confirmation_passed": bool((decision.details or {}).get("confirmation_passed")),
                "config_source": str((decision.details or {}).get("config_source") or ""),
                "config_fallback_reasons": list((decision.details or {}).get("config_fallback_reasons") or []),
                "reason_codes": list(decision.reason_codes or []),
            }
            for decision in position_decisions
            if _is_context_risk_exit(str(decision.decision_type or ""), dict(decision.details or {}))
        ]
        exit_decision_types = _unique(
            [str(intent.get("exit_decision_type") or "") for intent in exit_intents]
            + [str(decision.decision_type or "") for decision in position_decisions]
        )
        exit_decision_ids = _unique_ints(
            [intent.get("exit_decision_id") for intent in exit_intents]
            + [decision.id for decision in position_decisions if decision.id is not None]
        )
        exit_reasons = _unique(
            [str(intent.get("exit_reason") or intent.get("reason") or "") for intent in exit_intents]
            + [",".join(decision.reason_codes or []) or decision.decision_type for decision in position_decisions]
        )
        entry_live_safety = dict(entry.get("live_safety") or {}) if entry else {}
        entry_safety = dict(entry.get("safety") or {}) if entry else {}
        entry_request = dict(entry.get("request") or {}) if entry else {}
        entry_metadata = dict(entry.get("metadata") or {}) if entry else {}
        review_details = dict(review.details or {}) if review else {}
        position_details = dict(position.details or {}) if position else {}
        candidate_metadata = dict(candidate.metadata or {}) if candidate else {}
        best_link = _best_attribution_link(attribution_links)
        candidate_instance_id = _first_text(
            best_link.candidate_instance_id if best_link else "",
            entry_metadata.get("candidate_instance_id"),
            review_details.get("candidate_instance_id"),
            position_details.get("candidate_instance_id"),
            candidate_metadata.get("candidate_instance_id"),
            *[_payload_candidate_instance_id(intent) for intent in exit_intents],
        )
        candidate_generation_seq = _first_int(
            best_link.candidate_generation_seq if best_link else None,
            entry_metadata.get("candidate_generation_seq"),
            review_details.get("candidate_generation_seq"),
            position_details.get("candidate_generation_seq"),
            candidate_metadata.get("candidate_generation_seq"),
        )
        generation_reason = _first_text(
            entry_metadata.get("generation_reason"),
            entry_metadata.get("candidate_generation_reason"),
            review_details.get("generation_reason"),
            review_details.get("candidate_generation_reason"),
            position_details.get("generation_reason"),
            position_details.get("candidate_generation_reason"),
            candidate_metadata.get("generation_reason"),
            candidate_metadata.get("candidate_generation_reason"),
        )
        trade_date = str(
            _first_not_none(
                entry.get("trade_date") if entry else None,
                review.trade_date if review else None,
                candidate.trade_date if candidate else None,
                *(intent.get("trade_date") for intent in exit_intents),
            )
            or ""
        )
        code = str(
            _first_not_none(
                entry.get("code") if entry else None,
                review.code if review else None,
                candidate.code if candidate else None,
                *(intent.get("code") for intent in exit_intents),
            )
            or ""
        )
        code_date_key = _code_date_key(trade_date, code)
        hybrid_event = _match_hybrid_event(
            hybrid_events_by_code_date.get(code_date_key, []),
            candidate_instance_id=candidate_instance_id,
            reference_at=str(entry.get("created_at") if entry else review.created_at if review else ""),
        )
        hybrid_details = dict((hybrid_event or {}).get("details") or {})
        gate_reason = str(_first_not_none(entry.get("gate_reason") if entry else None, review_details.get("primary_reason_code"), review_details.get("gate_reason")) or "")
        taxonomy_codes = [
            gate_reason,
            str(entry_safety.get("reason") or ""),
            str(entry_live_safety.get("reason") or ""),
            *[str(value) for value in (review_details.get("blocking_reason_codes") or [])],
            *[str(value) for value in (review_details.get("secondary_reason_codes") or [])],
        ]
        hybrid_reason_codes = _unique(
            [
                *((hybrid_event or {}).get("hybrid_reason_codes") or []),
                (hybrid_event or {}).get("hybrid_primary_reason"),
                *taxonomy_codes,
            ]
        )
        theme_score_value = _optional_float(
            _first_not_none(
                (hybrid_event or {}).get("theme_score"),
                hybrid_details.get("theme_score"),
                entry_request.get("theme_score"),
                entry_metadata.get("theme_score"),
                review_details.get("theme_score"),
            )
        )
        hybrid_score_value = _optional_float(
            _first_not_none(
                (hybrid_event or {}).get("hybrid_score"),
                entry_request.get("hybrid_score"),
                entry_metadata.get("hybrid_score"),
                review_details.get("hybrid_score"),
            )
        )
        membership_score_value = _optional_float(
            _first_not_none(
                (hybrid_event or {}).get("membership_score"),
                hybrid_details.get("membership_score"),
                entry_request.get("membership_score"),
                entry_metadata.get("membership_score"),
                review_details.get("membership_score"),
            )
        )
        stock_role = _first_text(
            (hybrid_event or {}).get("leader_type"),
            (hybrid_event or {}).get("relation_type"),
            entry_metadata.get("stock_role"),
            entry_request.get("stock_role"),
            review_details.get("stock_role"),
        )
        hybrid_status = _first_text(
            entry_metadata.get("hybrid_status"),
            entry_request.get("hybrid_status"),
            review_details.get("hybrid_status"),
            (hybrid_event or {}).get("hybrid_status"),
        )
        hybrid_position_tier = _first_text(
            entry_metadata.get("hybrid_position_tier"),
            entry_request.get("hybrid_position_tier"),
            review_details.get("hybrid_position_tier"),
            (hybrid_event or {}).get("hybrid_position_tier"),
        )
        reason_status = normalize_reason_status(
            reason_codes=taxonomy_codes,
            display_state=str(_first_not_none(entry.get("gate_status") if entry else None, review_details.get("gate_status")) or ""),
            existing_status=str(entry_metadata.get("sub_status") or review_details.get("sub_status") or ""),
        )
        lifecycle = DryRunTradeLifecycle(
            lifecycle_id=key,
            trade_date=trade_date,
            code=code,
            name=str(_first_not_none(review.name if review else None, candidate.name if candidate else None) or ""),
            strategy_name=str(_first_not_none(entry.get("strategy_name") if entry else None, review.strategy_profile if review else None) or ""),
            candidate_id=int(candidate_id) if candidate_id is not None else None,
            candidate_instance_id=candidate_instance_id,
            candidate_generation_seq=candidate_generation_seq,
            matched_by=best_link.matched_by if best_link else "",
            link_confidence=best_link.link_confidence if best_link else "",
            attribution_confidence=_attribution_confidence(best_link),
            legacy_low_confidence_sample=_legacy_low_confidence_sample(candidate_instance_id, best_link),
            theme_name=str(_first_not_none(entry_request.get("theme_name"), review.theme_name if review else None, entry_metadata.get("theme_name")) or ""),
            theme_score=theme_score_value,
            gate_status=str(_first_not_none(entry.get("gate_status") if entry else None, review_details.get("gate_status")) or ""),
            gate_reason=gate_reason,
            reason_status=reason_status,
            reason_family=reason_status_family(reason_status),
            gate_score=_optional_float(_first_not_none(entry_request.get("gate_score"), entry_metadata.get("gate_score"), review_details.get("gate_score"))),
            hybrid_score=hybrid_score_value,
            hybrid_status=hybrid_status,
            hybrid_position_tier=hybrid_position_tier,
            hybrid_reason_codes=hybrid_reason_codes,
            reason_codes=hybrid_reason_codes,
            membership_score=membership_score_value,
            theme_score_bucket=_score_bucket(theme_score_value),
            membership_score_bucket=_membership_score_bucket(membership_score_value),
            stock_role=stock_role,
            session_bucket=str(_first_not_none(review_details.get("session_bucket"), entry_metadata.get("session_bucket")) or ""),
            entry_intent_id=str(entry.get("intent_id") or "") if entry else "",
            entry_intent_status=str(entry.get("status") or "") if entry else "",
            entry_live_would_pass=bool(entry.get("live_would_pass")) if entry else False,
            entry_live_reject_reason=str(entry.get("live_reject_reason") or entry_live_safety.get("reason") or "") if entry else "",
            entry_decision_safety_reason=str(entry_safety.get("reason") or ""),
            entry_price=int(entry.get("price") or 0) if entry else int(review.entry_price if review else 0),
            entry_quantity=sum(int(intent.get("quantity") or 0) for intent in entry_intents),
            entry_amount=sum(int(intent.get("order_amount") or (int(intent.get("price") or 0) * int(intent.get("quantity") or 0))) for intent in entry_intents),
            virtual_order_id=_first_int(entry.get("virtual_order_id") if entry else None, review.virtual_order_id if review else None, position.virtual_order_id if position else None),
            virtual_position_id=_first_int(position.id if position else None, review.virtual_position_id if review else None),
            position_entry_price=position.entry_price if position else None,
            position_quantity=position.quantity if position else None,
            opened_at=position.opened_at if position else "",
            closed_at=position.closed_at if position else "",
            exit_intent_ids=[str(intent.get("intent_id") or "") for intent in exit_intents if intent.get("intent_id")],
            exit_decision_ids=exit_decision_ids,
            exit_decision_types=exit_decision_types,
            exit_reasons=exit_reasons,
            exit_price=_last_int([intent.get("price") for intent in exit_intents] + ([review.exit_price] if review else [])),
            exit_quantity_total=sum(int(intent.get("quantity") or 0) for intent in exit_intents),
            exit_amount_total=sum(int(intent.get("order_amount") or (int(intent.get("price") or 0) * int(intent.get("quantity") or 0))) for intent in exit_intents),
            realized_return_pct=_optional_float(_first_not_none(position.realized_return_pct if position else None, review_details.get("realized_return_pct"))),
            max_return_5m=_optional_float(review.max_return_5m if review else None),
            max_return_10m=_optional_float(review.max_return_10m if review else None),
            max_return_20m=_optional_float(review.max_return_20m if review else None),
            max_drawdown_20m=_optional_float(review.max_drawdown_20m if review else None),
            max_return_pct=_optional_float(position.max_return_pct if position else None),
            max_drawdown_pct=_optional_float(position.max_drawdown_pct if position else None),
            final_status=str(review.final_status if review else ""),
            trade_review_id=review.id if review else None,
            false_positive_flag=bool(review.false_positive_flag) if review else False,
            false_negative_flag=bool(review.false_negative_flag) if review else False,
            blocked_but_later_rallied=bool(review.blocked_but_later_rallied) if review else False,
            expired_but_later_rallied=bool(review.expired_but_later_rallied) if review else False,
            hold_minutes=_hold_minutes(position.opened_at, position.closed_at) if position else None,
            score_bucket=_score_bucket(_optional_float(_first_not_none(hybrid_score_value, entry_request.get("gate_score"), review_details.get("hybrid_score")))),
            details={
                "entry_intent_count": len(entry_intents),
                "exit_intent_count": len(exit_intents),
                "entry_intent_ids": [intent.get("intent_id") for intent in entry_intents],
                "support_missing_reason": _first_text(entry_metadata.get("support_missing_reason"), entry_metadata.get("support_taxonomy"), review_details.get("support_missing_reason"), review_details.get("support_taxonomy")),
                "support_coverage": _support_coverage_details(entry_metadata, review_details),
                "support_reclaimed": bool(_first_not_none(entry_metadata.get("support_reclaimed"), review_details.get("support_reclaimed"), False)),
                "generation_reason": generation_reason,
                "previous_candidate_instance_id": _first_text(
                    entry_metadata.get("previous_candidate_instance_id"),
                    review_details.get("previous_candidate_instance_id"),
                    position_details.get("previous_candidate_instance_id"),
                    candidate_metadata.get("previous_candidate_instance_id"),
                ),
                "previous_seen_at": _first_text(
                    entry_metadata.get("previous_seen_at"),
                    review_details.get("previous_seen_at"),
                    position_details.get("previous_seen_at"),
                    candidate_metadata.get("previous_seen_at"),
                ),
                "minutes_since_previous_signal": _first_float(
                    entry_metadata.get("minutes_since_previous_signal"),
                    review_details.get("minutes_since_previous_signal"),
                    position_details.get("minutes_since_previous_signal"),
                    candidate_metadata.get("minutes_since_previous_signal"),
                ),
                "blocked_generation_reason": _first_text(
                    entry_metadata.get("blocked_generation_reason"),
                    review_details.get("blocked_generation_reason"),
                    position_details.get("blocked_generation_reason"),
                    candidate_metadata.get("blocked_generation_reason"),
                ),
                "excessive_generation_blocked": bool(
                    _first_not_none(
                        entry_metadata.get("excessive_generation_blocked"),
                        review_details.get("excessive_generation_blocked"),
                        position_details.get("excessive_generation_blocked"),
                        candidate_metadata.get("excessive_generation_blocked"),
                        False,
                    )
                ),
                "review_false_positive_flag": bool(review.false_positive_flag) if review else False,
                "review_false_negative_flag": bool(review.false_negative_flag) if review else False,
                "review_false_positive_type": review_details.get("false_positive_type", "") if review else "",
                "review_false_negative_type": review_details.get("false_negative_type", "") if review else "",
                "candidate_instance_ids": _unique(
                    [candidate_instance_id]
                    + [str(value) for value in position_details.get("candidate_instance_ids") or []]
                    + [_payload_candidate_instance_id(intent) for intent in intents]
                    + ([str(review_details.get("candidate_instance_id") or "")] if review else [])
                ),
                "attribution_links": [asdict(link) for link in attribution_links],
                "position_context_history_count": len(position_context_history),
                "position_context_capture_reasons": context_capture_reasons,
                "position_context_has_entry": "ENTRY" in context_capture_reasons,
                "position_context_has_holding": "HOLDING_EVAL" in context_capture_reasons,
                "position_context_has_exit": "EXIT_EVAL" in context_capture_reasons,
                "context_risk_exit_details": context_risk_exit_details,
                "hybrid_validation_event_id": (hybrid_event or {}).get("id"),
                "hybrid_validation_event_created_at": (hybrid_event or {}).get("created_at", ""),
            },
        )
        tick_rows = ticks_by_code_date.get(code_date_key, [])
        lifecycle.execution_realism = _execution_realism(lifecycle, entry, tick_rows, self.config)
        lifecycle.limit_price_hit = lifecycle.execution_realism.get("limit_price_hit")
        lifecycle.partial_fill_risk = str(lifecycle.execution_realism.get("partial_fill_risk") or "")
        lifecycle.spread_risk = str(lifecycle.execution_realism.get("spread_risk") or "")
        lifecycle.liquidity_bucket = str(lifecycle.execution_realism.get("liquidity_bucket") or "")
        lifecycle.entry_tick_age_sec = _optional_float(lifecycle.execution_realism.get("entry_tick_age_sec"))
        lifecycle.gateway_command_latency_ms = _optional_float(lifecycle.execution_realism.get("gateway_command_latency_ms"))
        if entry:
            fill_result = simulate_fill(
                entry,
                tick_rows,
                config=FillSimulationConfig(stale_tick_age_sec=self.config.stale_tick_age_sec),
            ).to_dict()
            lifecycle.fill_simulation = fill_result
            lifecycle.fill_price = _optional_float(fill_result.get("fill_price"))
            lifecycle.requested_price = _optional_float(fill_result.get("requested_price"))
            lifecycle.slippage_bps = _optional_float(fill_result.get("slippage_bps"))
            lifecycle.fill_ratio = _optional_float(fill_result.get("fill_ratio"))
            lifecycle.partial_fill = bool(fill_result.get("partial_fill"))
            lifecycle.simulated_latency_ms = _optional_float(fill_result.get("simulated_latency_ms"))
            lifecycle.reject_or_skip_reason = str(fill_result.get("reject_or_skip_reason") or "")
            lifecycle.stale_tick = bool(fill_result.get("stale_tick"))
            lifecycle.fill_adjusted_return_pct = _fill_adjusted_return_pct(lifecycle)
            lifecycle.fill_adjusted_net_return_pct = _fill_adjusted_net_return_pct(lifecycle, self.config)
        cost_payload = _cost_adjusted_payload(lifecycle, entry, tick_rows, self.config)
        lifecycle.cost_scenarios = list(cost_payload.get("cost_scenarios") or [])
        lifecycle.net_return_pct = _optional_float(cost_payload.get("net_return_pct"))
        lifecycle.net_expectancy_component = lifecycle.net_return_pct
        lifecycle.net_opportunity_return_pct = _optional_float(cost_payload.get("net_opportunity_return_pct"))
        lifecycle.cost_adjusted_classification = str(cost_payload.get("cost_adjusted_classification") or "")
        lifecycle.net_bad_ready_type = str(cost_payload.get("net_bad_ready_type") or "")
        lifecycle.net_opportunity_type = str(cost_payload.get("net_opportunity_type") or "")
        lifecycle.details["cost_assumptions"] = cost_payload.get("cost_assumptions", {})
        lifecycle.details["execution_realism"] = dict(lifecycle.execution_realism)
        lifecycle.details["fill_simulation"] = dict(lifecycle.fill_simulation)
        lifecycle.data_quality_issue_reasons = _data_quality_issue_reasons(
            lifecycle,
            entry,
            exit_intents,
            review,
            position,
            position_decisions,
        )
        lifecycle.data_quality_issues = list(lifecycle.data_quality_issue_reasons)
        lifecycle.data_status = "OK" if not lifecycle.data_quality_issues else lifecycle.data_quality_issues[0]
        return lifecycle

    def aggregate_summary(self, items: list[dict]) -> dict[str, Any]:
        completed = [item for item in items if _is_completed(item)]
        realized_values = [_optional_float(item.get("realized_return_pct")) for item in completed]
        realized_values = [value for value in realized_values if value is not None]
        wins = [item for item in completed if (_optional_float(item.get("realized_return_pct")) or 0.0) > 0.0]
        losses = [item for item in completed if (_optional_float(item.get("realized_return_pct")) or 0.0) <= 0.0]
        live_pass_items = [item for item in items if bool(item.get("entry_live_would_pass"))]
        live_pass_completed = [item for item in live_pass_items if _is_completed(item)]
        live_pass_wins = [item for item in live_pass_completed if (_optional_float(item.get("realized_return_pct")) or 0.0) > 0.0]
        accepted_completed = [
            item
            for item in completed
            if item.get("entry_intent_status") in {"DRY_RUN_ACCEPTED", "ACCEPTED"}
        ]
        net_values = [_optional_float(item.get("net_return_pct")) for item in accepted_completed]
        net_values = [value for value in net_values if value is not None]
        net_wins = [value for value in net_values if value > 0.0]
        fill_net_values = [_optional_float(item.get("fill_adjusted_net_return_pct")) for item in accepted_completed]
        fill_net_values = [value for value in fill_net_values if value is not None]
        generation_summary = _generation_summary(items)
        position_context_summary = _position_context_history_summary(items)
        execution_realism = _execution_realism_summary(items, self.config)
        fill_quality = _fill_quality_summary(items)
        cost_adjusted = _cost_adjusted_summary(items)
        go_no_go = _go_no_go_summary(items, self.config, net_values, cost_adjusted, execution_realism)
        opportunity_loss_count = sum(1 for item in items if item.get("opportunity_loss_type"))
        false_positive_count = sum(1 for item in items if item.get("dry_run_false_positive_type"))
        false_negative_count = sum(1 for item in items if item.get("dry_run_false_negative_type"))
        return {
            "total_lifecycle_count": len(items),
            "trade_day_count": len({str(item.get("trade_date") or "") for item in items if item.get("trade_date")}),
            "entry_intent_count": sum(1 for item in items if item.get("entry_intent_id")),
            "exit_intent_count": sum(len(item.get("exit_intent_ids") or []) for item in items),
            "completed_lifecycle_count": len(completed),
            "open_lifecycle_count": sum(1 for item in items if item.get("quality_bucket") in {"PENDING", "INSUFFICIENT_DATA"}),
            "orphan_entry_count": sum(1 for item in items if "ORPHAN_ENTRY" in item.get("data_quality_issues", [])),
            "orphan_exit_count": sum(1 for item in items if "ORPHAN_EXIT" in item.get("data_quality_issues", [])),
            "exact_candidate_instance_match_count": _count_lifecycles_with_match(items, "candidate_instance_id"),
            "candidate_id_match_count": _count_lifecycles_with_match(items, "candidate_id"),
            "weak_code_date_fallback_count": _count_lifecycles_with_match(items, "weak_code_date_fallback"),
            "ambiguous_candidate_link_count": sum(1 for item in items if "AMBIGUOUS_CANDIDATE_LINK" in item.get("data_quality_issues", [])),
            "link_confidence_distribution": _top_counts(((item.get("link_confidence") or "UNKNOWN") for item in items), key="confidence"),
            "attribution_confidence_distribution": _top_counts(((item.get("attribution_confidence") or "UNKNOWN") for item in items), key="confidence"),
            "legacy_low_confidence_sample_count": sum(1 for item in items if item.get("legacy_low_confidence_sample")),
            "multi_generation_code_count": _multi_generation_code_count(items),
            **generation_summary,
            **position_context_summary,
            "review_missing_count": sum(1 for item in items if "REVIEW_MISSING" in item.get("data_quality_issues", [])),
            "position_missing_count": sum(1 for item in items if "POSITION_MISSING" in item.get("data_quality_issues", [])),
            "avg_realized_return_pct": _avg(realized_values),
            "median_realized_return_pct": median(realized_values) if realized_values else None,
            "win_rate": _ratio(len(wins), len(completed)),
            "loss_rate": _ratio(len(losses), len(completed)),
            "profit_factor": _profit_factor(net_values or realized_values),
            "expectancy_pct": _avg(net_values or realized_values),
            "accepted_completed_lifecycle_count": len(accepted_completed),
            "net_expectancy": _avg(net_values),
            "avg_net_return_pct": _avg(net_values),
            "median_net_return_pct": median(net_values) if net_values else None,
            "net_win_rate": _ratio(len(net_wins), len(net_values)),
            "net_loss_rate": _ratio(len(net_values) - len(net_wins), len(net_values)),
            "fill_adjusted_expectancy_pct": _avg(fill_net_values),
            "fill_adjusted_profit_factor": _profit_factor(fill_net_values),
            "fill_adjusted_win_rate": _ratio(sum(1 for value in fill_net_values if value > 0.0), len(fill_net_values)),
            "primary_cost_assumption": _primary_cost_assumption(self.config),
            "cost_scenario_expectancy": _aggregate_cost_scenarios(items),
            "cost_adjusted": cost_adjusted,
            "execution_realism": execution_realism,
            "fill_quality": fill_quality,
            "avg_slippage_bps": fill_quality.get("avg_slippage_bps"),
            "median_slippage_bps": fill_quality.get("median_slippage_bps"),
            "partial_fill_rate": fill_quality.get("partial_fill_rate"),
            "stale_tick_rate": fill_quality.get("stale_tick_rate"),
            "go_no_go": go_no_go,
            "avg_max_return_5m": _avg([item.get("max_return_5m") for item in items]),
            "avg_max_return_10m": _avg([item.get("max_return_10m") for item in items]),
            "avg_max_return_20m": _avg([item.get("max_return_20m") for item in items]),
            "avg_max_drawdown_20m": _avg([item.get("max_drawdown_20m") for item in items]),
            "max_drawdown_pct": _min_float([item.get("max_drawdown_20m") for item in items] + [item.get("max_drawdown_pct") for item in items]),
            "take_profit_count": sum(1 for item in items if "TAKE_PROFIT" in item.get("exit_decision_types", [])),
            "support_loss_count": sum(1 for item in items if "SUPPORT_LOSS" in item.get("exit_decision_types", [])),
            "time_exit_count": sum(1 for item in items if "TIME_EXIT" in item.get("exit_decision_types", [])),
            "trailing_stop_count": sum(1 for item in items if "TRAILING_STOP" in item.get("exit_decision_types", [])),
            "partial_take_profit_count": sum(1 for item in items if item.get("final_status") == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value),
            "avg_hold_minutes": _avg([item.get("hold_minutes") for item in completed]),
            "dry_run_accepted_count": sum(1 for item in items if item.get("entry_intent_status") in {"DRY_RUN_ACCEPTED", "ACCEPTED"}),
            "dry_run_rejected_count": sum(1 for item in items if item.get("entry_intent_status") in {"DRY_RUN_REJECTED", "REJECTED", "LIVE_BLOCKED"}),
            "dry_run_duplicate_count": sum(1 for item in items if item.get("entry_intent_status") == "DUPLICATE"),
            "live_would_pass_count": len(live_pass_items),
            "live_would_reject_count": sum(1 for item in items if item.get("entry_intent_id") and not item.get("entry_live_would_pass")),
            "live_would_pass_win_rate": _ratio(len(live_pass_wins), len(live_pass_completed)),
            "live_would_reject_but_rallied_count": sum(1 for item in items if item.get("dry_run_false_negative_type") == "LIVE_REJECTED_BUT_RALLIED"),
            "rejected_but_rallied_count": sum(1 for item in items if item.get("dry_run_false_negative_type") in {"DRY_RUN_REJECTED_BUT_RALLIED", "LIVE_REJECTED_BUT_RALLIED"}),
            "false_positive_count": false_positive_count,
            "false_negative_count": false_negative_count,
            "true_positive_count": sum(1 for item in items if item.get("signal_classification") == "true_positive"),
            "true_negative_count": sum(1 for item in items if item.get("signal_classification") == "true_negative"),
            "pending_count": sum(1 for item in items if item.get("signal_classification") in {"pending", "insufficient_data"}),
            "false_positive_rate": _ratio(false_positive_count, len(items)),
            "false_negative_rate": _ratio(false_negative_count, len(items)),
            "opportunity_loss_count": opportunity_loss_count,
            "missed_opportunity_rate": _ratio(opportunity_loss_count, len(items)),
            "cost_adjusted_bad_ready_count": cost_adjusted.get("bad_ready_count", 0),
            "cost_adjusted_bad_ready_rate": cost_adjusted.get("bad_ready_rate"),
            "cost_adjusted_opportunity_loss_count": cost_adjusted.get("opportunity_loss_count", 0),
            "cost_adjusted_opportunity_loss_rate": cost_adjusted.get("opportunity_loss_rate"),
        }

    def aggregate_false_signals(self, items: list[dict]) -> dict[str, Any]:
        fp = _top_counts(item.get("dry_run_false_positive_type") for item in items if item.get("dry_run_false_positive_type"))
        fn = _top_counts(item.get("dry_run_false_negative_type") for item in items if item.get("dry_run_false_negative_type"))
        reject_rally = _top_counts(
            (
                item.get("entry_live_reject_reason") or item.get("entry_decision_safety_reason") or "UNKNOWN"
                for item in items
                if item.get("opportunity_loss_type")
            ),
            key="reason",
        )
        return {
            "false_positive_count": sum(count["count"] for count in fp),
            "false_negative_count": sum(count["count"] for count in fn),
            "opportunity_loss_count": sum(1 for item in items if item.get("opportunity_loss_type")),
            "top_false_positive_types": fp,
            "top_false_negative_types": fn,
            "top_live_reject_reasons_with_rally": reject_rally,
        }

    def aggregate_grouped(self, items: list[dict]) -> dict[str, list[dict]]:
        return {
            "by_strategy_name": _group_stats(items, "strategy_name"),
            "by_theme_name": _group_stats(items, "theme_name"),
            "by_gate_reason": _group_stats(items, "gate_reason"),
            "by_reason_status": _group_stats(items, "reason_status"),
            "by_reason_family": _group_stats(items, "reason_family"),
            "by_gate_status": _group_stats(items, "gate_status"),
            "by_hybrid_status": _group_stats(items, "hybrid_status"),
            "by_hybrid_position_tier": _group_stats(items, "hybrid_position_tier"),
            "by_session_bucket": _group_stats(items, "session_bucket"),
            "by_code": _group_stats(items, "code"),
            "by_live_reject_reason": _group_stats(items, "entry_live_reject_reason"),
            "by_decision_safety_reason": _group_stats(items, "entry_decision_safety_reason"),
            "by_score_bucket": _group_stats(items, "score_bucket"),
            "by_theme_score_bucket": _group_stats(items, "theme_score_bucket"),
            "by_membership_score_bucket": _group_stats(items, "membership_score_bucket"),
            "by_stock_role": _group_stats(items, "stock_role"),
            "by_reason_code": _multi_group_stats(items, "reason_codes", "reason_code"),
            "by_exit_decision_type": _multi_group_stats(items, "exit_decision_types", "decision_type"),
            "reason_summary": reason_summary(items),
        }

    def data_quality(self, items: list[dict]) -> dict[str, Any]:
        issues: dict[str, dict[str, Any]] = {}
        reason_counts: dict[str, dict[str, int]] = {}
        for item in items:
            issue_reasons = dict(item.get("data_quality_issue_reasons") or {})
            for issue in item.get("data_quality_issues") or []:
                bucket = issues.setdefault(issue, {"count": 0, "samples": []})
                bucket["count"] += 1
                reason = str(issue_reasons.get(issue) or "UNKNOWN")
                reason_bucket = reason_counts.setdefault(issue, {})
                reason_bucket[reason] = reason_bucket.get(reason, 0) + 1
                if len(bucket["samples"]) < 5:
                    bucket["samples"].append(
                        {
                            "code": item.get("code", ""),
                            "intent_id": item.get("entry_intent_id") or (item.get("exit_intent_ids") or [""])[0],
                            "lifecycle_id": item.get("lifecycle_id", ""),
                            "reason": reason,
                        }
                    )
        return {
            "issues": [
                {
                    "issue": issue,
                    **payload,
                    "reasons": [
                        {"reason": reason, "count": count}
                        for reason, count in sorted(reason_counts.get(issue, {}).items(), key=lambda pair: pair[1], reverse=True)
                    ],
                }
                for issue, payload in sorted(issues.items(), key=lambda pair: pair[1]["count"], reverse=True)
            ],
            "missing_review_count": issues.get("REVIEW_MISSING", {}).get("count", 0),
            "missing_position_count": issues.get("POSITION_MISSING", {}).get("count", 0),
            "orphan_entry_count": issues.get("ORPHAN_ENTRY", {}).get("count", 0),
            "orphan_exit_count": issues.get("ORPHAN_EXIT", {}).get("count", 0),
            "missing_price_reasons": _issue_reasons(reason_counts, "MISSING_PRICE"),
            "missing_quantity_reasons": _issue_reasons(reason_counts, "MISSING_QUANTITY"),
            "orphan_entry_reasons": _issue_reasons(reason_counts, "ORPHAN_ENTRY"),
            "orphan_exit_reasons": _issue_reasons(reason_counts, "ORPHAN_EXIT"),
            "support_missing_reasons": _support_missing_reason_counts(items),
            "support_coverage": _support_coverage_summary(items),
            "support_vwap_coverage": _support_vwap_coverage_summary(items, rally_threshold_pct=self.config.fn_rally_threshold_pct),
        }

    def recommendations(self, summary: dict, grouped: dict, false_signal_summary: dict) -> list[str]:
        recommendations: list[str] = []
        for row in grouped.get("by_gate_reason", []):
            key = str(row.get("key") or "").upper()
            rate = float(row.get("false_positive_rate") or 0.0)
            if any(token in key for token in ("LATE_CHASE", "LATE_LAGGARD")) and rate >= 0.3 and row.get("count", 0) >= 3:
                recommendations.append("LATE_CHASE/LATE_LAGGARD reason의 false positive 비율이 높아 진입 차단 또는 점수 감점 강화를 검토하세요.")
        for row in grouped.get("by_gate_reason", []):
            key = str(row.get("key") or "").upper()
            if "LOW_BREADTH" in key and int(row.get("false_negative_count") or 0) > 0:
                recommendations.append("LOW_BREADTH 차단 후 상승 사례가 있어 완전 차단 대신 WATCH/소액 DRY_RUN 허용을 검토하세요.")
        for row in grouped.get("by_score_bucket", []):
            if row.get("key") in {"0-20", "20-40"} and (row.get("win_rate") or 0.0) < 0.4 and row.get("count", 0) >= 3:
                recommendations.append(f"{row.get('key')} score bucket의 win rate가 낮아 entry 제한 또는 추가 confirmation을 검토하세요.")
        if int(summary.get("live_would_reject_but_rallied_count") or 0) > 0:
            recommendations.append("live_would_reject 상태에서 상승한 사례가 있어 SafetyGuard/Gateway 상태로 인한 기회손실을 확인하세요.")
        if int(summary.get("support_loss_count") or 0) > int(summary.get("take_profit_count") or 0):
            recommendations.append("SUPPORT_LOSS 비중이 TAKE_PROFIT보다 높아 손절폭, 진입가격, 첫 눌림 기준 재검토를 권장합니다.")
        if any(item.get("type") == "ENTRY_ACCEPTED_BUT_TIME_EXIT_WEAK" for item in false_signal_summary.get("top_false_positive_types", [])):
            recommendations.append("TIME_EXIT 약수익/약손실 사례가 있어 보유시간 또는 exit policy 조정을 검토하세요.")
        if not recommendations:
            recommendations.append("현재 표본에서는 강한 자동 조정 신호가 제한적입니다. 표본을 더 쌓은 뒤 threshold 조정을 검토하세요.")
        return recommendations

    def persist_report(self, report: dict) -> dict:
        return self.db.save_dry_run_performance_report(report)

    def export_json(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "lifecycle_id",
            "trade_date",
            "code",
            "name",
            "strategy_name",
            "theme_name",
            "session_bucket",
            "candidate_id",
            "candidate_instance_id",
            "candidate_generation_seq",
            "matched_by",
            "link_confidence",
            "entry_intent_id",
            "entry_intent_status",
            "entry_live_would_pass",
            "entry_live_reject_reason",
            "entry_price",
            "entry_quantity",
            "virtual_position_id",
            "exit_intent_ids",
            "exit_decision_types",
            "final_status",
            "realized_return_pct",
            "net_return_pct",
            "fill_adjusted_return_pct",
            "fill_adjusted_net_return_pct",
            "cost_adjusted_classification",
            "net_bad_ready_type",
            "net_opportunity_type",
            "net_opportunity_return_pct",
            "max_return_5m",
            "max_return_10m",
            "max_return_20m",
            "max_drawdown_20m",
            "dry_run_false_positive_type",
            "dry_run_false_negative_type",
            "opportunity_loss_type",
            "quality_bucket",
            "gate_status",
            "gate_reason",
            "reason_status",
            "reason_family",
            "reason_codes",
            "hybrid_score",
            "hybrid_status",
            "hybrid_position_tier",
            "theme_score",
            "theme_score_bucket",
            "membership_score",
            "membership_score_bucket",
            "stock_role",
            "limit_price_hit",
            "requested_price",
            "fill_price",
            "slippage_bps",
            "fill_ratio",
            "partial_fill",
            "simulated_latency_ms",
            "reject_or_skip_reason",
            "stale_tick",
            "partial_fill_risk",
            "spread_risk",
            "liquidity_bucket",
            "entry_tick_age_sec",
            "gateway_command_latency_ms",
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in report.get("items", []):
                row = dict(item)
                row["exit_intent_ids"] = ",".join(str(value) for value in row.get("exit_intent_ids") or [])
                row["exit_decision_types"] = ",".join(str(value) for value in row.get("exit_decision_types") or [])
                row["reason_codes"] = ",".join(str(value) for value in row.get("reason_codes") or [])
                writer.writerow({column: row.get(column, "") for column in columns})
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = report.get("summary", {})
        false_summary = report.get("false_signal_summary", {})
        grouped = report.get("grouped", {})
        lines = [
            f"# DRY_RUN Performance Report {report.get('trade_date') or ''}".strip(),
            "",
            f"- Generated at: {report.get('generated_at', '')}",
            f"- Report ID: {report.get('report_id', '')}",
            f"- Total lifecycles: {summary.get('total_lifecycle_count', 0)}",
            f"- Completed lifecycles: {summary.get('completed_lifecycle_count', 0)}",
            f"- Win rate: {_pct(summary.get('win_rate'))}",
            f"- Avg realized return: {_num(summary.get('avg_realized_return_pct'))}",
            f"- Profit factor: {summary.get('profit_factor') if summary.get('profit_factor') is not None else '-'}",
            f"- Expectancy: {_num(summary.get('expectancy_pct'))}",
            f"- Net expectancy: {_num(summary.get('net_expectancy'))}",
            f"- Net win rate: {_pct(summary.get('net_win_rate'))}",
            f"- Review-only: {bool(report.get('review_only', True))}",
            "",
            "## Go/No-Go",
            *_markdown_go_no_go_lines(summary.get("go_no_go", {})),
            "",
            "## Cost And Slippage",
            *_markdown_cost_assumption_lines(summary.get("primary_cost_assumption", {})),
            "### Scenario Expectancy",
            *_markdown_cost_scenario_lines(summary.get("cost_scenario_expectancy", [])),
            "",
            "## Fill Simulation",
            *_markdown_fill_quality_lines(summary.get("fill_quality", {})),
            "",
            "## Execution Realism",
            *_markdown_execution_realism_lines(summary.get("execution_realism", {})),
            "",
            "## DRY_RUN Intent Summary",
            f"- Entry intents: {summary.get('entry_intent_count', 0)}",
            f"- Exit intents: {summary.get('exit_intent_count', 0)}",
            f"- Live would pass: {summary.get('live_would_pass_count', 0)}",
            f"- Live would reject: {summary.get('live_would_reject_count', 0)}",
            "",
            "## Attribution Quality",
            f"- Exact candidate instance matches: {summary.get('exact_candidate_instance_match_count', 0)}",
            f"- Candidate ID matches: {summary.get('candidate_id_match_count', 0)}",
            f"- Weak code/date fallbacks: {summary.get('weak_code_date_fallback_count', 0)}",
            f"- Ambiguous candidate links: {summary.get('ambiguous_candidate_link_count', 0)}",
            f"- Multi-generation codes: {summary.get('multi_generation_code_count', 0)}",
            f"- Legacy low-confidence samples: {summary.get('legacy_low_confidence_sample_count', 0)}",
            *_markdown_count_lines(summary.get("link_confidence_distribution", []), "confidence"),
            *_markdown_count_lines(summary.get("attribution_confidence_distribution", []), "confidence"),
            "",
            "## Candidate Generation Summary",
            f"- Multi-generation codes: {summary.get('multi_generation_code_count', 0)}",
            f"- Avg generation per code: {summary.get('avg_generation_per_code', '-')}",
            f"- Max generation per code: {summary.get('max_generation_per_code', 0)}",
            f"- Stale re-detect generations: {summary.get('stale_re_detect_count', 0)}",
            f"- Theme-change generations: {summary.get('theme_change_generation_count', 0)}",
            f"- Source-change generations: {summary.get('source_change_generation_count', 0)}",
            f"- Strategy-change generations: {summary.get('strategy_change_generation_count', 0)}",
            f"- Previous-lifecycle-closed generations: {summary.get('previous_lifecycle_closed_generation_count', 0)}",
            f"- Excessive generation blocks: {summary.get('excessive_generation_count', 0)}",
            "",
            "## Position Context History",
            f"- Positions with ENTRY context: {summary.get('positions_with_entry_context_count', 0)}",
            f"- Positions with HOLDING context: {summary.get('positions_with_holding_context_count', 0)}",
            f"- Positions with EXIT context: {summary.get('positions_with_exit_context_count', 0)}",
            f"- Context coverage: {_pct(summary.get('position_context_coverage_pct'))}",
            f"- DATA_LIMITED_CONTEXT: {summary.get('data_limited_context_count', 0)}",
            f"- LOW_CONFIDENCE_EXIT: {summary.get('low_confidence_exit_count', 0)}",
            f"- Index deterioration: {summary.get('index_status_deterioration_count', 0)}",
            "### Context History Count Distribution",
            *_markdown_count_lines(summary.get("context_history_count_distribution", []), "history_count"),
            "### Context Risk Exit Confidence",
            *_markdown_context_confidence_lines(summary.get("context_risk_exit_confidence_by_type", {})),
            "### Context History Pruning",
            *_markdown_prune_lines(summary.get("context_history_prune", {})),
            "",
            "## False Signals",
            f"- False positives: {summary.get('false_positive_count', 0)}",
            f"- False negatives: {summary.get('false_negative_count', 0)}",
            f"- Opportunity loss: {summary.get('opportunity_loss_count', 0)}",
            f"- Cost-adjusted bad-ready: {summary.get('cost_adjusted_bad_ready_count', 0)}",
            f"- Cost-adjusted opportunity loss: {summary.get('cost_adjusted_opportunity_loss_count', 0)}",
            "",
            "### Top False Positive Types",
            *_markdown_count_lines(false_summary.get("top_false_positive_types", []), "type"),
            "",
            "### Top False Negative Types",
            *_markdown_count_lines(false_summary.get("top_false_negative_types", []), "type"),
            "",
            "## Gate Reason Performance",
            *_markdown_group_lines(grouped.get("by_gate_reason", [])[:10]),
            "",
            "## Runtime Reason Taxonomy",
            *_markdown_group_lines(grouped.get("by_reason_status", [])[:10]),
            "",
            "## Theme Performance",
            *_markdown_group_lines(grouped.get("by_theme_name", [])[:10]),
            "",
            "## Session Bucket Performance",
            *_markdown_group_lines(grouped.get("by_session_bucket", [])[:10]),
            "",
            "## Hybrid Status Performance",
            *_markdown_group_lines(grouped.get("by_hybrid_status", [])[:10]),
            "",
            "## Hybrid Position Tier Performance",
            *_markdown_group_lines(grouped.get("by_hybrid_position_tier", [])[:10]),
            "",
            "## Reason Code Performance",
            *_markdown_group_lines(grouped.get("by_reason_code", [])[:10]),
            "",
            "## Theme Score Bucket Performance",
            *_markdown_group_lines(grouped.get("by_theme_score_bucket", [])[:10]),
            "",
            "## Membership Score Bucket Performance",
            *_markdown_group_lines(grouped.get("by_membership_score_bucket", [])[:10]),
            "",
            "## Stock Role Performance",
            *_markdown_group_lines(grouped.get("by_stock_role", [])[:10]),
            "",
            "## Exit Decision Type Performance",
            *_markdown_group_lines(grouped.get("by_exit_decision_type", [])[:10]),
            "",
            "## Recommendations",
            *[f"- {item}" for item in report.get("recommendations", [])],
            "",
            "## Data Quality",
            *_markdown_quality_lines(summary.get("data_quality", {})),
            "",
            "## Support/VWAP Coverage",
            *_markdown_support_vwap_coverage_lines((summary.get("data_quality", {}) or {}).get("support_vwap_coverage", {})),
        ]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    def export_report(self, report: dict, *, fmt: str = "json") -> dict[str, str]:
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        report_dir = self.report_root / trade_date
        stem = f"dry_run_performance_{trade_date}"
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


def config_from_settings(settings: Any) -> DryRunPerformanceConfig:
    return DryRunPerformanceConfig(
        fp_loss_threshold_pct=float(getattr(settings, "dry_run_fp_loss_threshold_pct", -1.0)),
        fp_drawdown_threshold_pct=float(getattr(settings, "dry_run_fp_drawdown_threshold_pct", -3.0)),
        fn_rally_threshold_pct=float(getattr(settings, "dry_run_fn_rally_threshold_pct", 3.0)),
        good_trade_threshold_pct=float(getattr(settings, "dry_run_good_trade_threshold_pct", 2.0)),
        min_hold_minutes_for_final=int(getattr(settings, "dry_run_min_hold_minutes_for_final", 20)),
        pending_grace_minutes=int(getattr(settings, "dry_run_pending_grace_minutes", 30)),
        commission_bp_per_side=float(getattr(settings, "dry_run_commission_bp_per_side", 1.5)),
        sell_tax_bp=float(getattr(settings, "dry_run_sell_tax_bp", 15.0)),
        slippage_scenarios_bp=_float_tuple(getattr(settings, "dry_run_slippage_scenarios_bp", "0,10,20,30"), (0.0, 10.0, 20.0, 30.0)),
        entry_delay_scenarios_sec=_int_tuple(getattr(settings, "dry_run_entry_delay_scenarios_sec", "0,1,3,5"), (0, 1, 3, 5)),
        primary_slippage_bp=float(getattr(settings, "dry_run_primary_slippage_bp", 10.0)),
        primary_entry_delay_sec=int(getattr(settings, "dry_run_primary_entry_delay_sec", 0)),
        min_go_trade_days=int(getattr(settings, "dry_run_go_min_trade_days", 5)),
        min_go_accepted_entry_lifecycles=int(getattr(settings, "dry_run_go_min_accepted_entry_lifecycles", 30)),
        max_go_bad_ready_rate=float(getattr(settings, "dry_run_go_max_bad_ready_rate", 0.25)),
        max_go_opportunity_loss_rate=float(getattr(settings, "dry_run_go_max_opportunity_loss_rate", 0.25)),
        max_go_stale_tick_rate=float(getattr(settings, "dry_run_go_max_stale_tick_rate", 0.2)),
        max_go_latency_distortion_rate=float(getattr(settings, "dry_run_go_max_latency_distortion_rate", 0.2)),
        stale_tick_age_sec=float(getattr(settings, "dry_run_stale_tick_age_sec", 3.0)),
        gateway_latency_warn_ms=float(getattr(settings, "dry_run_gateway_latency_warn_ms", 1000.0)),
    )


def _lifecycle_key_for_intent(
    intent: dict,
    position_by_order: dict[int, Any],
    review_key_by_id: Optional[dict[int, str]] = None,
) -> str:
    return _lifecycle_link_for_intent(intent, position_by_order, review_key_by_id, {}).key


def _lifecycle_link_for_intent(
    intent: dict,
    position_by_order: dict[int, Any],
    review_key_by_id: Optional[dict[int, str]] = None,
    instance_ids_by_code_date: Optional[dict[str, set[str]]] = None,
    candidate_key_by_code_date: Optional[dict[str, str]] = None,
) -> AttributionLink:
    virtual_position_id = intent.get("virtual_position_id")
    if virtual_position_id is not None:
        return AttributionLink(f"vp:{virtual_position_id}", "virtual_position_id", "HIGH", candidate_instance_id=_payload_candidate_instance_id(intent), candidate_generation_seq=_payload_candidate_generation_seq(intent))
    virtual_order_id = intent.get("virtual_order_id")
    if virtual_order_id is not None and int(virtual_order_id) in position_by_order:
        return AttributionLink(f"vp:{position_by_order[int(virtual_order_id)].id}", "virtual_order_to_position", "HIGH", candidate_instance_id=_payload_candidate_instance_id(intent), candidate_generation_seq=_payload_candidate_generation_seq(intent))
    if virtual_order_id is not None:
        return AttributionLink(f"vo:{virtual_order_id}", "virtual_order_id", "MEDIUM", candidate_instance_id=_payload_candidate_instance_id(intent), candidate_generation_seq=_payload_candidate_generation_seq(intent))
    trade_review_id = intent.get("trade_review_id")
    if trade_review_id is not None and review_key_by_id and int(trade_review_id) in review_key_by_id:
        return AttributionLink(review_key_by_id[int(trade_review_id)], "trade_review_id", "HIGH", candidate_instance_id=_payload_candidate_instance_id(intent), candidate_generation_seq=_payload_candidate_generation_seq(intent))
    if trade_review_id is not None:
        return AttributionLink(f"review:{trade_review_id}", "trade_review_id", "MEDIUM", candidate_instance_id=_payload_candidate_instance_id(intent), candidate_generation_seq=_payload_candidate_generation_seq(intent))
    candidate_instance_id = _payload_candidate_instance_id(intent)
    if candidate_instance_id:
        return AttributionLink(f"ci:{candidate_instance_id}", "candidate_instance_id", "HIGH", candidate_instance_id=candidate_instance_id, candidate_generation_seq=_payload_candidate_generation_seq(intent))
    candidate_id = intent.get("candidate_id")
    if candidate_id is not None:
        return AttributionLink(f"cand_id:{intent.get('trade_date') or ''}:{candidate_id}", "candidate_id", "MEDIUM")
    candidate_link = _candidate_fallback_link(intent.get("trade_date"), intent.get("code"), intent.get("candidate_id"), intent, instance_ids_by_code_date or {}, candidate_key_by_code_date or {})
    if candidate_link:
        return candidate_link
    prefix = "orphan_exit" if _intent_phase(intent) == "exit" or intent.get("side") == "sell" else "orphan_entry"
    return AttributionLink(f"{prefix}:{intent.get('intent_id', '')}", prefix, "LOW")


def _lifecycle_key_for_review(review: Any, position_by_order: dict[int, Any]) -> str:
    return _lifecycle_link_for_review(review, position_by_order, {}).key


def _lifecycle_link_for_review(
    review: Any,
    position_by_order: dict[int, Any],
    instance_ids_by_code_date: Optional[dict[str, set[str]]] = None,
    candidate_key_by_code_date: Optional[dict[str, str]] = None,
) -> AttributionLink:
    if review.virtual_position_id is not None:
        return AttributionLink(f"vp:{review.virtual_position_id}", "virtual_position_id", "HIGH", candidate_instance_id=_review_candidate_instance_id(review), candidate_generation_seq=_review_candidate_generation_seq(review))
    if review.virtual_order_id is not None and int(review.virtual_order_id) in position_by_order:
        return AttributionLink(f"vp:{position_by_order[int(review.virtual_order_id)].id}", "virtual_order_to_position", "HIGH", candidate_instance_id=_review_candidate_instance_id(review), candidate_generation_seq=_review_candidate_generation_seq(review))
    candidate_instance_id = _review_candidate_instance_id(review)
    if candidate_instance_id:
        return AttributionLink(f"ci:{candidate_instance_id}", "candidate_instance_id", "HIGH", candidate_instance_id=candidate_instance_id, candidate_generation_seq=_review_candidate_generation_seq(review))
    if review.candidate_id is not None:
        return AttributionLink(f"cand_id:{review.trade_date or ''}:{review.candidate_id}", "candidate_id", "MEDIUM")
    candidate_link = _candidate_fallback_link(review.trade_date, review.code, review.candidate_id, {"review_id": review.id}, instance_ids_by_code_date or {}, candidate_key_by_code_date or {})
    if candidate_link:
        return candidate_link
    return AttributionLink(f"review:{review.id}", "trade_review_id", "MEDIUM")


def _candidate_code_date_key(trade_date: Any, code: Any, candidate_id: Any = None) -> str:
    date_text = str(trade_date or "")
    code_text = str(code or "")
    if date_text and candidate_id is not None:
        return f"cand_id:{date_text}:{candidate_id}"
    if candidate_id is not None:
        return f"cand_id::{candidate_id}"
    if date_text and code_text:
        return f"cand_code:{date_text}:{code_text}"
    return ""


def _candidate_fallback_link(
    trade_date: Any,
    code: Any,
    candidate_id: Any,
    payload: dict,
    instance_ids_by_code_date: dict[str, set[str]],
    candidate_key_by_code_date: dict[str, str],
) -> Optional[AttributionLink]:
    date_text = str(trade_date or "")
    code_text = str(code or "")
    if date_text and candidate_id is not None:
        return AttributionLink(f"cand_id:{date_text}:{candidate_id}", "candidate_id", "MEDIUM")
    if candidate_id is not None:
        return AttributionLink(f"cand_id::{candidate_id}", "candidate_id", "MEDIUM")
    if not date_text or not code_text:
        return None
    code_date = _code_date_key(date_text, code_text)
    instance_ids = instance_ids_by_code_date.get(code_date, set())
    if len(instance_ids) > 1:
        unique_id = payload.get("intent_id") or payload.get("review_id") or payload.get("id") or ""
        return AttributionLink(
            f"ambiguous:{date_text}:{code_text}:{unique_id}",
            "ambiguous_code_date_fallback",
            "LOW",
            issue="AMBIGUOUS_CANDIDATE_LINK",
        )
    candidate_key = candidate_key_by_code_date.get(code_date, "")
    if candidate_key:
        return AttributionLink(candidate_key, "weak_code_date_fallback", "LOW")
    window_key = _time_window_fallback_key(payload, date_text, code_text)
    if window_key:
        return AttributionLink(window_key, "code_date_time_window_source_strategy", "LOW_MEDIUM")
    return AttributionLink(f"cand_code:{date_text}:{code_text}", "weak_code_date_fallback", "LOW")


def _position_for_group(
    entry: Optional[dict],
    exit_intents: list[dict],
    review: Any,
    positions: dict[int, Any],
    position_by_order: dict[int, Any],
    positions_by_candidate: Optional[dict[int, list[Any]]] = None,
):
    ids = []
    if entry:
        ids.append(entry.get("virtual_position_id"))
    ids.extend(intent.get("virtual_position_id") for intent in exit_intents)
    if review:
        ids.append(review.virtual_position_id)
    for value in ids:
        if value is not None and int(value) in positions:
            return positions[int(value)]
    order_ids = []
    if entry:
        order_ids.append(entry.get("virtual_order_id"))
    if review:
        order_ids.append(review.virtual_order_id)
    order_ids.extend(intent.get("virtual_order_id") for intent in exit_intents)
    for value in order_ids:
        if value is not None and int(value) in position_by_order:
            return position_by_order[int(value)]
    candidate_ids = []
    if entry:
        candidate_ids.append(entry.get("candidate_id"))
    if review:
        candidate_ids.append(review.candidate_id)
    candidate_ids.extend(intent.get("candidate_id") for intent in exit_intents)
    for value in candidate_ids:
        if value is None or positions_by_candidate is None:
            continue
        candidates = positions_by_candidate.get(int(value)) or []
        if candidates:
            return sorted(candidates, key=lambda item: int(item.id or 0), reverse=True)[0]
    return None


def _intent_phase(intent: dict) -> str:
    return str(intent.get("order_phase") or ("exit" if str(intent.get("side") or "") == "sell" else "entry"))


def _data_quality_issue_reasons(
    lifecycle: DryRunTradeLifecycle,
    entry: Optional[dict],
    exit_intents: list[dict],
    review: Any,
    position: Any,
    position_decisions: list[Any],
) -> dict[str, str]:
    issues: dict[str, str] = {}
    has_entry = entry is not None
    has_exit = bool(exit_intents)
    has_review = review is not None
    has_position = position is not None
    entry_metadata = dict(entry.get("metadata") or {}) if entry else {}
    entry_safety = dict(entry.get("safety") or {}) if entry else {}
    entry_request = dict(entry.get("request") or {}) if entry else {}
    if has_entry and not has_review:
        issues["REVIEW_MISSING"] = "ENTRY_INTENT_WITHOUT_TRADE_REVIEW"
    if has_entry and not has_position:
        if entry and entry.get("virtual_order_id") is None:
            issues["POSITION_MISSING"] = "ENTRY_INTENT_WITHOUT_VIRTUAL_ORDER_ID"
        elif entry and str(entry.get("status") or "") in {"DRY_RUN_REJECTED", "REJECTED", "DUPLICATE"}:
            issues["POSITION_MISSING"] = "ENTRY_NOT_ACCEPTED_NO_POSITION_EXPECTED"
        else:
            issues["POSITION_MISSING"] = "NO_POSITION_MATCHED_BY_POSITION_OR_ORDER_OR_CANDIDATE"
    if has_entry and not has_exit and lifecycle.final_status in {
        ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
    }:
        issues["EXIT_INTENT_MISSING"] = "CLOSED_REVIEW_WITHOUT_EXIT_INTENT"
    if has_exit and not has_entry:
        if any(intent.get("virtual_position_id") for intent in exit_intents):
            issues["ORPHAN_EXIT"] = "EXIT_INTENT_POSITION_HAS_NO_ENTRY_INTENT"
        elif any(intent.get("candidate_id") or intent.get("code") for intent in exit_intents):
            issues["ORPHAN_EXIT"] = "EXIT_INTENT_ONLY_CANDIDATE_FALLBACK"
        else:
            issues["ORPHAN_EXIT"] = "EXIT_INTENT_WITHOUT_LINK_KEYS"
    if has_entry and not has_position and not has_review:
        if entry and entry.get("virtual_order_id") is None:
            issues["ORPHAN_ENTRY"] = "ENTRY_INTENT_WITHOUT_VIRTUAL_ORDER_OR_REVIEW"
        else:
            issues["ORPHAN_ENTRY"] = "ENTRY_INTENT_UNMATCHED_TO_POSITION_AND_REVIEW"
    if has_exit and not position_decisions:
        if any(intent.get("exit_decision_id") for intent in exit_intents):
            issues["EXIT_DECISION_MISSING"] = "EXIT_DECISION_ID_NOT_FOUND"
        else:
            issues["EXIT_DECISION_MISSING"] = "EXIT_INTENT_WITHOUT_EXIT_DECISION_ID"
    if lifecycle.entry_price <= 0:
        issues["MISSING_PRICE"] = _missing_price_reason(entry, entry_metadata, entry_request)
    if has_entry and lifecycle.entry_quantity <= 0:
        issues["MISSING_QUANTITY"] = _missing_quantity_reason(entry, entry_metadata, entry_safety)
    if has_entry and lifecycle.entry_live_reject_reason == "" and not lifecycle.entry_live_would_pass:
        issues["MISSING_LIVE_SAFETY_JSON"] = "LIVE_SAFETY_EMPTY_OR_MISSING_REASON"
    if has_review and lifecycle.max_return_20m is None:
        issues["MISSING_HORIZON_METRICS"] = "TRADE_REVIEW_MAX_RETURN_20M_MISSING"
    if has_position and not lifecycle.closed_at and has_entry:
        issues["STALE_OPEN_POSITION"] = "POSITION_OPEN_WITH_ENTRY_INTENT"
    if lifecycle.matched_by == "ambiguous_code_date_fallback":
        issues["AMBIGUOUS_CANDIDATE_LINK"] = "MULTIPLE_CANDIDATE_INSTANCES_FOR_CODE_DATE"
    support_reason = str((lifecycle.details or {}).get("support_missing_reason") or "")
    if support_reason == "SUPPORT_DATA_MISSING":
        issues["SUPPORT_DATA_MISSING"] = "SUPPORT_DIAGNOSTIC_ONLY_DATA_MISSING"
    return issues


def _group_stats(items: list[dict], field: str) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        key = str(item.get(field) or "UNKNOWN")
        grouped.setdefault(key, []).append(item)
    return [_stats_for_group(key, values) for key, values in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True)]


def _multi_group_stats(items: list[dict], field: str, key_name: str) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        values = item.get(field) or ["UNKNOWN"]
        for value in values:
            grouped.setdefault(str(value or "UNKNOWN"), []).append(item)
    return [
        {key_name: key, **{k: v for k, v in _stats_for_group(key, values).items() if k != "key"}}
        for key, values in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True)
    ]


def _stats_for_group(key: str, values: list[dict]) -> dict[str, Any]:
    completed = [item for item in values if _is_completed(item)]
    wins = [item for item in completed if (_optional_float(item.get("realized_return_pct")) or 0.0) > 0.0]
    accepted_completed = [
        item
        for item in completed
        if item.get("entry_intent_status") in {"DRY_RUN_ACCEPTED", "ACCEPTED"}
    ]
    net_values = [_optional_float(item.get("net_return_pct")) for item in accepted_completed]
    net_values = [value for value in net_values if value is not None]
    return {
        "key": key,
        "count": len(values),
        "completed": len(completed),
        "win_rate": _ratio(len(wins), len(completed)),
        "avg_realized_return_pct": _avg([item.get("realized_return_pct") for item in completed]),
        "net_expectancy": _avg(net_values),
        "avg_net_return_pct": _avg(net_values),
        "net_win_rate": _ratio(sum(1 for value in net_values if value > 0.0), len(net_values)),
        "avg_max_return_20m": _avg([item.get("max_return_20m") for item in values]),
        "avg_max_drawdown_20m": _avg([item.get("max_drawdown_20m") for item in values]),
        "false_positive_count": sum(1 for item in values if item.get("dry_run_false_positive_type")),
        "false_positive_rate": _ratio(sum(1 for item in values if item.get("dry_run_false_positive_type")), len(values)),
        "false_negative_count": sum(1 for item in values if item.get("dry_run_false_negative_type")),
        "opportunity_loss_count": sum(1 for item in values if item.get("opportunity_loss_type")),
        "cost_adjusted_bad_ready_count": sum(1 for item in values if item.get("net_bad_ready_type")),
        "cost_adjusted_opportunity_loss_count": sum(1 for item in values if item.get("net_opportunity_type")),
        "live_would_pass_count": sum(1 for item in values if item.get("entry_live_would_pass")),
        "live_would_reject_count": sum(1 for item in values if item.get("entry_intent_id") and not item.get("entry_live_would_pass")),
    }


def _top_counts(values: Iterable[Any], *, key: str = "type", limit: int = 10) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value or "")
        if not text:
            continue
        counts[text] = counts.get(text, 0) + 1
    return [{key: value, "count": count} for value, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]]


def _support_coverage_details(*payloads: dict) -> dict[str, Any]:
    for payload in payloads:
        coverage = payload.get("support_coverage") if isinstance(payload, dict) else None
        if isinstance(coverage, dict):
            return dict(coverage)
    merged: dict[str, Any] = {}
    for key in ("recent_support_price_present", "vwap_present", "vwap_ready", "minute_bar_present", "minute_bar_count"):
        for payload in payloads:
            if isinstance(payload, dict) and payload.get(key) not in (None, ""):
                merged[key] = payload.get(key)
                break
    return merged


def _support_missing_reason_counts(items: list[dict]) -> list[dict[str, Any]]:
    return _top_counts(
        (
            (item.get("details") or {}).get("support_missing_reason")
            for item in items
            if (item.get("details") or {}).get("support_missing_reason")
        ),
        key="reason",
    )


def _support_coverage_summary(items: list[dict]) -> dict[str, Any]:
    rows = [dict((item.get("details") or {}).get("support_coverage") or {}) for item in items]
    total = len(rows)
    return {
        "sample_count": total,
        "recent_support_price_present_count": sum(1 for row in rows if row.get("recent_support_price_present")),
        "vwap_present_count": sum(1 for row in rows if row.get("vwap_present")),
        "vwap_ready_count": sum(1 for row in rows if row.get("vwap_ready")),
        "stale_vwap_count": sum(1 for row in rows if row.get("vwap_stale")),
        "minute_bar_present_count": sum(1 for row in rows if row.get("minute_bar_present")),
        "support_reclaimed_count": sum(1 for item in items if (item.get("details") or {}).get("support_reclaimed")),
        "minute_bar_quality_status_counts": _top_counts((row.get("minute_bar_quality_status") for row in rows), key="status"),
        "support_source_distribution": _support_source_distribution(rows),
    }


def _support_vwap_coverage_summary(items: list[dict], *, rally_threshold_pct: float) -> dict[str, Any]:
    rows = [dict((item.get("details") or {}).get("support_coverage") or {}) for item in items]
    total = len(rows)
    support_metadata_count = sum(1 for row in rows if row.get("support_source_present_count", 0) or row.get("support_candidate_count", 0))
    vwap_count = sum(1 for row in rows if row.get("vwap_present"))
    minute_count = sum(1 for row in rows if row.get("minute_bar_present"))
    support_reasons = _support_missing_reason_counts(items)
    diagnostic_items = [item for item in items if _support_missing_reason(item)]
    rallied = [item for item in diagnostic_items if (_optional_float(item.get("max_return_20m")) or 0.0) >= float(rally_threshold_pct)]
    reason_rallied_counts = {
        "SUPPORT_STRUCTURALLY_MISSING_AND_RALLIED": sum(1 for item in rallied if _support_missing_reason(item) == "SUPPORT_STRUCTURALLY_MISSING"),
        "SUPPORT_DATA_MISSING_AND_RALLIED": sum(1 for item in rallied if _support_missing_reason(item) == "SUPPORT_DATA_MISSING"),
        "SUPPORT_NOT_READY_AND_RALLIED": sum(1 for item in rallied if _support_missing_reason(item) == "SUPPORT_NOT_READY"),
    }
    return {
        "sample_count": total,
        "support_metadata_coverage_pct": _ratio(support_metadata_count, total),
        "vwap_metadata_coverage_pct": _ratio(vwap_count, total),
        "minute_bar_coverage_pct": _ratio(minute_count, total),
        "support_missing_count_by_reason": support_reasons,
        "support_source_distribution": _support_source_distribution(rows),
        "minute_bar_quality_status_counts": _top_counts((row.get("minute_bar_quality_status") for row in rows), key="status"),
        "stale_vwap_count": sum(1 for row in rows if row.get("vwap_stale")),
        "diagnostic_only_due_to_support_count": len(diagnostic_items),
        "diagnostic_only_later_rallied_count": len(rallied),
        **reason_rallied_counts,
    }


def _support_missing_reason(item: dict) -> str:
    return str((item.get("details") or {}).get("support_missing_reason") or "")


def _support_source_distribution(rows: list[dict]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        presence = row.get("support_source_presence")
        if isinstance(presence, dict):
            for source, present in presence.items():
                if present:
                    counts[str(source)] = counts.get(str(source), 0) + 1
            continue
        for source in row.get("support_source_present") or []:
            counts[str(source)] = counts.get(str(source), 0) + 1
    return [{"source": source, "count": count} for source, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)]


def _hybrid_events_by_code_date(
    db: TradingDatabase,
    *,
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    limit: int = 20000,
) -> dict[str, list[dict[str, Any]]]:
    clauses: list[str] = []
    params: list[Any] = []
    if trade_date:
        clauses.append("trade_date = ?")
        params.append(str(trade_date))
    if code:
        clauses.append("stock_code = ?")
        params.append(str(code))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.conn.execute(
        f"""
        SELECT *
        FROM hybrid_gate_validation_events
        {where}
        ORDER BY trade_date ASC, stock_code ASC, created_at ASC, id ASC
        LIMIT ?
        """,
        tuple(params + [max(1, int(limit or 20000))]),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row)
        data["hybrid_reason_codes"] = _safe_json(data.get("hybrid_reason_codes_json"), [])
        data["details"] = _safe_json(data.get("details_json"), {})
        grouped.setdefault(_code_date_key(str(data.get("trade_date") or ""), str(data.get("stock_code") or "")), []).append(data)
    return grouped


def _gateway_price_ticks_by_code_date(
    db: TradingDatabase,
    *,
    trade_date: Optional[str] = None,
    code: Optional[str] = None,
    limit: int = 50000,
) -> dict[str, list[dict[str, Any]]]:
    clauses: list[str] = []
    params: list[Any] = []
    if trade_date:
        clauses.append("trade_date = ?")
        params.append(str(trade_date))
    if code:
        clauses.append("code = ?")
        params.append(str(code))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.conn.execute(
        f"""
        SELECT id, event_id, trade_date, timestamp, received_at, code, name, price,
               trade_value, execution_strength, best_bid, best_ask, spread_ticks,
               source, transport_mode, raw_payload_json, metadata_json, created_at
        FROM gateway_price_ticks
        {where}
        ORDER BY trade_date ASC, code ASC, timestamp ASC, id ASC
        LIMIT ?
        """,
        tuple(params + [max(1, int(limit or 50000))]),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        data = dict(row)
        data["raw_payload"] = _safe_json(data.get("raw_payload_json"), {})
        data["metadata"] = _safe_json(data.get("metadata_json"), {})
        grouped.setdefault(_code_date_key(str(data.get("trade_date") or ""), str(data.get("code") or "")), []).append(data)
    return grouped


def _match_hybrid_event(
    events: list[dict[str, Any]],
    *,
    candidate_instance_id: str = "",
    reference_at: str = "",
) -> dict[str, Any]:
    if not events:
        return {}
    if candidate_instance_id:
        for event in reversed(events):
            if _payload_contains_value(event, candidate_instance_id):
                return event
    reference = _parse_time(reference_at)
    if reference is None:
        return events[-1]
    before_or_equal = [
        event
        for event in events
        if (_parse_time(str(event.get("created_at") or "")) is not None)
        and _time_delta_seconds(event.get("created_at"), reference_at) is not None
        and (_time_delta_seconds(event.get("created_at"), reference_at) or 0.0) >= 0.0
    ]
    if before_or_equal:
        return before_or_equal[-1]
    return events[0]


def _payload_contains_value(payload: Any, needle: str) -> bool:
    if not needle:
        return False
    if isinstance(payload, dict):
        return any(_payload_contains_value(value, needle) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_contains_value(value, needle) for value in payload)
    return str(payload or "") == str(needle)


def _execution_realism(
    lifecycle: DryRunTradeLifecycle,
    entry: Optional[dict],
    ticks: list[dict[str, Any]],
    config: DryRunPerformanceConfig,
) -> dict[str, Any]:
    if entry is None:
        return {
            "limit_price_hit": None,
            "partial_fill_risk": "UNKNOWN",
            "spread_risk": "UNKNOWN",
            "liquidity_bucket": "UNKNOWN",
            "data_status": "NO_ENTRY_INTENT",
        }
    entry_time = _first_text(entry.get("created_at"), entry.get("updated_at"), lifecycle.opened_at)
    limit_price = _first_float(lifecycle.entry_price, entry.get("price"))
    tick_at_entry = _first_tick_at_or_after(ticks, entry_time)
    previous_tick = _latest_tick_at_or_before(ticks, entry_time)
    observed_tick = tick_at_entry or previous_tick or {}
    sources = _analysis_sources(entry, observed_tick, lifecycle.details)
    trade_value = _first_float(_recursive_first(sources, "trade_value"), observed_tick.get("trade_value"))
    spread_ticks = _first_int(_recursive_first(sources, "spread_ticks"), observed_tick.get("spread_ticks"))
    best_bid = _first_float(_recursive_first(sources, "best_bid"), observed_tick.get("best_bid"))
    best_ask = _first_float(_recursive_first(sources, "best_ask"), observed_tick.get("best_ask"))
    observed_price = _first_float(observed_tick.get("price"), _recursive_first(sources, "price"))
    entry_tick_age_sec = _first_float(
        _recursive_first(sources, "entry_tick_age_sec"),
        _recursive_first(sources, "latest_tick_age_sec"),
        _recursive_first(sources, "tick_age_sec"),
    )
    if entry_tick_age_sec is None and previous_tick:
        entry_tick_age_sec = _time_delta_seconds(previous_tick.get("timestamp") or previous_tick.get("received_at"), entry_time)
    latency_ms = _first_float(
        _recursive_first(sources, "gateway_command_latency_ms"),
        _recursive_first(sources, "command_latency_ms"),
        _recursive_first(sources, "total_wall_ms"),
        _recursive_first(sources, "ack_round_trip_ms"),
        _recursive_first(sources, "core_dispatch_wait_ms"),
    )
    explicit_hit = _recursive_first(sources, "limit_price_hit")
    limit_price_hit = _bool_or_none(explicit_hit)
    if limit_price_hit is None:
        limit_price_hit = _limit_price_hit(str(entry.get("side") or "buy"), limit_price, observed_price, best_bid, best_ask)
    liquidity_bucket = _liquidity_bucket(trade_value)
    spread_risk = _spread_risk(spread_ticks)
    partial_fill_risk = _partial_fill_risk(
        order_amount=_first_float(lifecycle.entry_amount, entry.get("order_amount")),
        trade_value=trade_value,
        limit_price_hit=limit_price_hit,
    )
    stale_tick = entry_tick_age_sec is not None and entry_tick_age_sec > config.stale_tick_age_sec
    high_latency = latency_ms is not None and latency_ms > config.gateway_latency_warn_ms
    return {
        "limit_price_hit": limit_price_hit,
        "partial_fill_risk": partial_fill_risk,
        "spread_risk": spread_risk,
        "liquidity_bucket": liquidity_bucket,
        "trade_value": trade_value,
        "order_amount": _first_float(lifecycle.entry_amount, entry.get("order_amount")),
        "entry_tick_age_sec": entry_tick_age_sec,
        "gateway_command_latency_ms": latency_ms,
        "spread_ticks": spread_ticks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "observed_price": observed_price,
        "observed_tick_id": observed_tick.get("event_id") or observed_tick.get("id"),
        "tick_data_available": bool(observed_tick),
        "stale_tick": stale_tick,
        "high_gateway_latency": high_latency,
        "data_status": "OK" if observed_tick else "TICK_MISSING",
    }


def _cost_adjusted_payload(
    lifecycle: DryRunTradeLifecycle,
    entry: Optional[dict],
    ticks: list[dict[str, Any]],
    config: DryRunPerformanceConfig,
) -> dict[str, Any]:
    cost_scenarios: list[dict[str, Any]] = []
    primary_net: Optional[float] = None
    primary_cost_pct = _round_trip_cost_pct(config, config.primary_slippage_bp)
    baseline_gross = _gross_return_pct(lifecycle)
    for slippage_bp in config.slippage_scenarios_bp:
        cost_pct = _round_trip_cost_pct(config, slippage_bp)
        for delay_sec in config.entry_delay_scenarios_sec:
            delayed_price = _delayed_entry_price(entry, ticks, delay_sec)
            gross = _gross_return_pct(lifecycle, entry_price_override=delayed_price)
            if delay_sec == 0 and gross is None:
                gross = baseline_gross
            net = round(gross - cost_pct, 4) if gross is not None else None
            row = {
                "slippage_bp": float(slippage_bp),
                "entry_delay_sec": int(delay_sec),
                "round_trip_cost_pct": round(cost_pct, 4),
                "gross_return_pct": round(gross, 4) if gross is not None else None,
                "net_return_pct": net,
                "entry_price": delayed_price,
                "tick_data_available": delayed_price is not None if delay_sec else bool(delayed_price or lifecycle.entry_price),
            }
            if float(slippage_bp) == float(config.primary_slippage_bp) and int(delay_sec) == int(config.primary_entry_delay_sec):
                primary_net = net
            cost_scenarios.append(row)
    if primary_net is None and baseline_gross is not None:
        primary_net = round(baseline_gross - primary_cost_pct, 4)
    net_opportunity = None
    max_return = _first_float(lifecycle.max_return_20m, lifecycle.max_return_10m, lifecycle.max_return_5m)
    if max_return is not None:
        net_opportunity = round(max_return - primary_cost_pct, 4)
    classification, bad_ready_type, opportunity_type = _cost_adjusted_classification(
        lifecycle,
        net_return_pct=primary_net,
        net_opportunity_return_pct=net_opportunity,
        config=config,
    )
    return {
        "cost_assumptions": _primary_cost_assumption(config),
        "cost_scenarios": cost_scenarios,
        "net_return_pct": primary_net,
        "net_opportunity_return_pct": net_opportunity,
        "cost_adjusted_classification": classification,
        "net_bad_ready_type": bad_ready_type,
        "net_opportunity_type": opportunity_type,
    }


def _cost_adjusted_classification(
    lifecycle: DryRunTradeLifecycle,
    *,
    net_return_pct: Optional[float],
    net_opportunity_return_pct: Optional[float],
    config: DryRunPerformanceConfig,
) -> tuple[str, str, str]:
    status = str(lifecycle.hybrid_status or lifecycle.gate_status or "").upper()
    entry_accepted = lifecycle.entry_intent_status in {"DRY_RUN_ACCEPTED", "ACCEPTED"}
    bad_ready_type = ""
    opportunity_type = ""
    if status in {"READY", "READY_SMALL", "READY_SHADOW_SMALL_ENTRY"} and entry_accepted and net_return_pct is not None:
        if net_return_pct < 0.0:
            bad_ready_type = "NET_BAD_READY"
            return "bad_ready", bad_ready_type, opportunity_type
        return "good_ready_after_cost" if net_return_pct >= config.good_trade_threshold_pct else "weak_ready_after_cost", "", ""
    if status in {"WAIT", "BLOCKED", "OBSERVE"} or not entry_accepted:
        max_return = _first_float(lifecycle.max_return_20m, lifecycle.max_return_10m, lifecycle.max_return_5m)
        if (
            net_opportunity_return_pct is not None
            and net_opportunity_return_pct > 0.0
            and (max_return is None or max_return >= config.fn_rally_threshold_pct or lifecycle.opportunity_loss_type)
        ):
            if status == "WAIT":
                opportunity_type = "NET_MISSED_WAIT"
            elif status == "BLOCKED":
                opportunity_type = "NET_BLOCKED_OPPORTUNITY"
            else:
                opportunity_type = "NET_MISSED_OPPORTUNITY"
            return "missed_opportunity_after_cost", "", opportunity_type
    if net_return_pct is not None and entry_accepted:
        return "accepted_after_cost", "", ""
    return "neutral_after_cost", "", ""


def _gross_return_pct(lifecycle: DryRunTradeLifecycle, *, entry_price_override: Optional[float] = None) -> Optional[float]:
    entry_price = _first_float(entry_price_override, lifecycle.position_entry_price, lifecycle.entry_price)
    exit_price = _first_float(lifecycle.exit_price)
    if entry_price and exit_price:
        return round(((exit_price / entry_price) - 1.0) * 100.0, 4)
    return _optional_float(lifecycle.realized_return_pct)


def _fill_adjusted_return_pct(lifecycle: DryRunTradeLifecycle) -> Optional[float]:
    fill_price = _first_float(lifecycle.fill_price)
    exit_price = _first_float(lifecycle.exit_price)
    fill_ratio = _first_float(lifecycle.fill_ratio)
    if not fill_price or fill_price <= 0 or not exit_price or fill_ratio is None or fill_ratio <= 0:
        return None
    return round(((exit_price / fill_price) - 1.0) * 100.0, 4)


def _fill_adjusted_net_return_pct(lifecycle: DryRunTradeLifecycle, config: DryRunPerformanceConfig) -> Optional[float]:
    gross = _fill_adjusted_return_pct(lifecycle)
    if gross is None:
        return None
    return round(gross - _round_trip_cost_pct(config, 0.0), 4)


def _round_trip_cost_pct(config: DryRunPerformanceConfig, slippage_bp: float) -> float:
    return round_trip_cost_pct(
        RoundTripCostConfig(
            commission_bp_per_side=float(config.commission_bp_per_side),
            sell_tax_bp=float(config.sell_tax_bp),
            entry_slippage_bp=float(slippage_bp),
            exit_slippage_bp=float(slippage_bp),
        )
    )


def _primary_cost_assumption(config: DryRunPerformanceConfig) -> dict[str, Any]:
    return {
        "commission_bp_per_side": float(config.commission_bp_per_side),
        "sell_tax_bp": float(config.sell_tax_bp),
        "primary_slippage_bp": float(config.primary_slippage_bp),
        "primary_entry_delay_sec": int(config.primary_entry_delay_sec),
        "primary_round_trip_cost_pct": round(_round_trip_cost_pct(config, config.primary_slippage_bp), 4),
        "slippage_scenarios_bp": [float(value) for value in config.slippage_scenarios_bp],
        "entry_delay_scenarios_sec": [int(value) for value in config.entry_delay_scenarios_sec],
    }


def _delayed_entry_price(entry: Optional[dict], ticks: list[dict[str, Any]], delay_sec: int) -> Optional[float]:
    if entry is None:
        return None
    if int(delay_sec) <= 0:
        return _first_float(entry.get("price"))
    entry_time = _parse_time(_first_text(entry.get("created_at"), entry.get("updated_at")))
    if entry_time is None:
        return None
    target = entry_time + timedelta(seconds=int(delay_sec))
    tick = _first_tick_at_or_after(ticks, target.isoformat())
    return _first_float((tick or {}).get("price"))


def _first_tick_at_or_after(ticks: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
    target = _parse_time(timestamp)
    if target is None:
        return {}
    for tick in ticks:
        tick_time = _parse_time(str(tick.get("timestamp") or tick.get("received_at") or tick.get("created_at") or ""))
        if tick_time is None:
            continue
        delta = _datetime_delta_seconds(target, tick_time)
        if delta is not None and delta >= 0:
            return tick
    return {}


def _latest_tick_at_or_before(ticks: list[dict[str, Any]], timestamp: str) -> dict[str, Any]:
    target = _parse_time(timestamp)
    if target is None:
        return {}
    selected: dict[str, Any] = {}
    for tick in ticks:
        tick_time = _parse_time(str(tick.get("timestamp") or tick.get("received_at") or tick.get("created_at") or ""))
        if tick_time is None:
            continue
        delta = _datetime_delta_seconds(tick_time, target)
        if delta is not None and delta >= 0:
            selected = tick
    return selected


def _limit_price_hit(side: str, limit_price: Optional[float], observed_price: Optional[float], best_bid: Optional[float], best_ask: Optional[float]) -> Optional[bool]:
    if not limit_price or limit_price <= 0:
        return None
    normalized = str(side or "").lower()
    if normalized == "sell":
        reference = _first_float(best_bid, observed_price)
        return None if reference is None else reference >= limit_price
    reference = _first_float(best_ask, observed_price)
    return None if reference is None else reference <= limit_price


def _liquidity_bucket(trade_value: Optional[float]) -> str:
    if trade_value is None:
        return "UNKNOWN"
    if trade_value < 10_000_000:
        return "LOW_LT_10M"
    if trade_value < 100_000_000:
        return "MEDIUM_10M_100M"
    if trade_value < 1_000_000_000:
        return "HIGH_100M_1B"
    return "VERY_HIGH_GE_1B"


def _partial_fill_risk(order_amount: Optional[float], trade_value: Optional[float], limit_price_hit: Optional[bool]) -> str:
    if limit_price_hit is False:
        return "HIGH"
    if not order_amount or not trade_value or trade_value <= 0:
        return "UNKNOWN"
    ratio = float(order_amount) / float(trade_value)
    if ratio >= 0.1:
        return "VERY_HIGH"
    if ratio >= 0.03:
        return "HIGH"
    if ratio >= 0.01:
        return "MEDIUM"
    return "LOW"


def _spread_risk(spread_ticks: Optional[int]) -> str:
    if spread_ticks is None:
        return "UNKNOWN"
    if spread_ticks <= 1:
        return "LOW"
    if spread_ticks <= 3:
        return "MEDIUM"
    return "HIGH"


def _cost_adjusted_summary(items: list[dict]) -> dict[str, Any]:
    ready_samples = [
        item
        for item in items
        if str(item.get("hybrid_status") or item.get("gate_status") or "").upper() in {"READY", "READY_SMALL", "READY_SHADOW_SMALL_ENTRY"}
        and item.get("entry_intent_status") in {"DRY_RUN_ACCEPTED", "ACCEPTED"}
    ]
    bad_ready = [item for item in ready_samples if item.get("net_bad_ready_type")]
    opportunity = [item for item in items if item.get("net_opportunity_type")]
    return {
        "bad_ready_count": len(bad_ready),
        "bad_ready_rate": _ratio(len(bad_ready), len(ready_samples)),
        "ready_sample_count": len(ready_samples),
        "opportunity_loss_count": len(opportunity),
        "opportunity_loss_rate": _ratio(len(opportunity), len(items)),
        "top_bad_ready_types": _top_counts((item.get("net_bad_ready_type") for item in bad_ready), key="type"),
        "top_opportunity_types": _top_counts((item.get("net_opportunity_type") for item in opportunity), key="type"),
    }


def _execution_realism_summary(items: list[dict], config: DryRunPerformanceConfig) -> dict[str, Any]:
    limit_samples = [item for item in items if item.get("limit_price_hit") is not None]
    limit_hits = [item for item in limit_samples if item.get("limit_price_hit") is True]
    high_partial = [item for item in items if str(item.get("partial_fill_risk") or "") in {"HIGH", "VERY_HIGH"}]
    high_spread = [item for item in items if str(item.get("spread_risk") or "") == "HIGH"]
    stale = [
        item
        for item in items
        if _optional_float(item.get("entry_tick_age_sec")) is not None
        and (_optional_float(item.get("entry_tick_age_sec")) or 0.0) > config.stale_tick_age_sec
    ]
    high_latency = [
        item
        for item in items
        if _optional_float(item.get("gateway_command_latency_ms")) is not None
        and (_optional_float(item.get("gateway_command_latency_ms")) or 0.0) > config.gateway_latency_warn_ms
    ]
    return {
        "limit_price_hit_sample_count": len(limit_samples),
        "limit_price_hit_count": len(limit_hits),
        "limit_price_hit_rate": _ratio(len(limit_hits), len(limit_samples)),
        "partial_fill_high_risk_count": len(high_partial),
        "partial_fill_high_risk_rate": _ratio(len(high_partial), len(items)),
        "spread_high_risk_count": len(high_spread),
        "spread_high_risk_rate": _ratio(len(high_spread), len(items)),
        "stale_tick_count": len(stale),
        "stale_tick_rate": _ratio(len(stale), len(items)),
        "gateway_latency_high_count": len(high_latency),
        "gateway_latency_high_rate": _ratio(len(high_latency), len(items)),
        "avg_entry_tick_age_sec": _avg([item.get("entry_tick_age_sec") for item in items]),
        "avg_gateway_command_latency_ms": _avg([item.get("gateway_command_latency_ms") for item in items]),
        "by_liquidity_bucket": _top_counts((item.get("liquidity_bucket") for item in items), key="bucket"),
        "by_partial_fill_risk": _top_counts((item.get("partial_fill_risk") for item in items), key="risk"),
        "by_spread_risk": _top_counts((item.get("spread_risk") for item in items), key="risk"),
    }


def _fill_quality_summary(items: list[dict]) -> dict[str, Any]:
    summary = summarize_fill_simulations(
        item.get("fill_simulation") or {}
        for item in items
        if item.get("fill_simulation")
    )
    fill_net_values = [_optional_float(item.get("fill_adjusted_net_return_pct")) for item in items]
    fill_net_values = [value for value in fill_net_values if value is not None]
    summary.update(
        {
            "fill_adjusted_expectancy_pct": _avg(fill_net_values),
            "fill_adjusted_profit_factor": _profit_factor(fill_net_values),
            "fill_adjusted_win_rate": _ratio(sum(1 for value in fill_net_values if value > 0.0), len(fill_net_values)),
        }
    )
    return summary


def _aggregate_cost_scenarios(items: list[dict]) -> list[dict[str, Any]]:
    grouped: dict[tuple[float, int], list[dict[str, Any]]] = {}
    for item in items:
        if item.get("entry_intent_status") not in {"DRY_RUN_ACCEPTED", "ACCEPTED"} or not _is_completed(item):
            continue
        for scenario in item.get("cost_scenarios") or []:
            if not isinstance(scenario, dict):
                continue
            key = (float(scenario.get("slippage_bp") or 0.0), int(scenario.get("entry_delay_sec") or 0))
            grouped.setdefault(key, []).append(scenario)
    rows: list[dict[str, Any]] = []
    for (slippage_bp, delay_sec), values in sorted(grouped.items()):
        net_values = [_optional_float(row.get("net_return_pct")) for row in values]
        net_values = [value for value in net_values if value is not None]
        rows.append(
            {
                "slippage_bp": slippage_bp,
                "entry_delay_sec": delay_sec,
                "sample_count": len(net_values),
                "net_expectancy": _avg(net_values),
                "net_win_rate": _ratio(sum(1 for value in net_values if value > 0.0), len(net_values)),
                "avg_gross_return_pct": _avg([row.get("gross_return_pct") for row in values]),
                "tick_data_available_count": sum(1 for row in values if row.get("tick_data_available")),
            }
        )
    return rows


def _go_no_go_summary(
    items: list[dict],
    config: DryRunPerformanceConfig,
    net_values: list[float],
    cost_adjusted: dict[str, Any],
    execution_realism: dict[str, Any],
) -> dict[str, Any]:
    trade_day_count = len({str(item.get("trade_date") or "") for item in items if item.get("trade_date")})
    accepted_entry_count = sum(1 for item in items if item.get("entry_intent_status") in {"DRY_RUN_ACCEPTED", "ACCEPTED"})
    net_expectancy = _avg(net_values)
    criteria = [
        _criterion("MIN_5_TRADE_DAYS", trade_day_count >= config.min_go_trade_days, trade_day_count, f">={config.min_go_trade_days}"),
        _criterion(
            "MIN_30_ACCEPTED_ENTRY_LIFECYCLES",
            accepted_entry_count >= config.min_go_accepted_entry_lifecycles,
            accepted_entry_count,
            f">={config.min_go_accepted_entry_lifecycles}",
        ),
        _criterion("POSITIVE_NET_EXPECTANCY", net_expectancy is not None and net_expectancy > 0.0, net_expectancy, ">0"),
        _criterion(
            "BAD_READY_RATE_WITHIN_LIMIT",
            (cost_adjusted.get("bad_ready_rate") is not None and float(cost_adjusted.get("bad_ready_rate") or 0.0) <= config.max_go_bad_ready_rate),
            cost_adjusted.get("bad_ready_rate"),
            f"<={config.max_go_bad_ready_rate}",
        ),
        _criterion(
            "OPPORTUNITY_LOSS_WITHIN_LIMIT",
            (cost_adjusted.get("opportunity_loss_rate") is not None and float(cost_adjusted.get("opportunity_loss_rate") or 0.0) <= config.max_go_opportunity_loss_rate),
            cost_adjusted.get("opportunity_loss_rate"),
            f"<={config.max_go_opportunity_loss_rate}",
        ),
        _criterion(
            "STALE_TICK_DISTORTION_WITHIN_LIMIT",
            (execution_realism.get("stale_tick_rate") is not None and float(execution_realism.get("stale_tick_rate") or 0.0) <= config.max_go_stale_tick_rate),
            execution_realism.get("stale_tick_rate"),
            f"<={config.max_go_stale_tick_rate}",
        ),
        _criterion(
            "LATENCY_DISTORTION_WITHIN_LIMIT",
            (
                execution_realism.get("gateway_latency_high_rate") is None
                or float(execution_realism.get("gateway_latency_high_rate") or 0.0) <= config.max_go_latency_distortion_rate
            ),
            execution_realism.get("gateway_latency_high_rate"),
            f"<={config.max_go_latency_distortion_rate}",
        ),
    ]
    passed = all(item["passed"] for item in criteria)
    insufficient = trade_day_count < config.min_go_trade_days or accepted_entry_count < config.min_go_accepted_entry_lifecycles
    return {
        "decision": "GO" if passed else "NO_GO",
        "readiness": "INSUFFICIENT_DATA" if insufficient else "EVALUATED",
        "review_only": True,
        "criteria": criteria,
        "blocked_by": [item["code"] for item in criteria if not item["passed"]],
    }


def _criterion(code: str, passed: bool, value: Any, threshold: str) -> dict[str, Any]:
    return {"code": code, "passed": bool(passed), "value": value, "threshold": threshold}


def _analysis_sources(*payloads: Any) -> list[Any]:
    sources: list[Any] = []
    for payload in payloads:
        if isinstance(payload, dict):
            sources.append(payload)
            for key in ("metadata", "request", "response", "details", "safety", "live_safety", "raw_payload"):
                value = payload.get(key)
                if isinstance(value, dict):
                    sources.append(value)
    return sources


def _recursive_first(sources: list[Any], key: str) -> Any:
    for source in sources:
        value = _recursive_find(source, key)
        if value not in (None, ""):
            return value
    return None


def _recursive_find(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
        for value in payload.values():
            found = _recursive_find(value, key)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _recursive_find(value, key)
            if found not in (None, ""):
                return found
    return None


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "hit", "pass"}:
        return True
    if text in {"0", "false", "no", "n", "miss", "fail"}:
        return False
    return None


def _time_delta_seconds(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_time(str(start or ""))
    end_dt = _parse_time(str(end or ""))
    if start_dt is None or end_dt is None:
        return None
    return _datetime_delta_seconds(start_dt, end_dt)


def _datetime_delta_seconds(start: datetime, end: datetime) -> Optional[float]:
    try:
        if start.tzinfo is not None and end.tzinfo is None:
            start = start.replace(tzinfo=None)
        if end.tzinfo is not None and start.tzinfo is None:
            end = end.replace(tzinfo=None)
        return (end - start).total_seconds()
    except Exception:
        return None


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return default
    return parsed if parsed is not None else default


def _membership_score_bucket(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    for label, low, high in [
        ("0_0_55", 0.0, 0.55),
        ("0_55_0_65", 0.55, 0.65),
        ("0_65_0_80", 0.65, 0.80),
        ("0_80_1_00", 0.80, 1.0001),
    ]:
        if low <= float(value) < high:
            return label
    return "UNKNOWN"


def _candidate_instances_by_code_date(
    intents: Iterable[dict],
    reviews: Iterable[Any],
    positions: Iterable[Any],
    candidates: Iterable[Any],
) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for intent in intents:
        instance_id = _payload_candidate_instance_id(intent)
        if instance_id:
            _add_instance(grouped, intent.get("trade_date"), intent.get("code"), instance_id)
    for review in reviews:
        instance_id = _review_candidate_instance_id(review)
        if instance_id:
            _add_instance(grouped, review.trade_date, review.code, instance_id)
    for position in positions:
        details = dict(position.details or {})
        for instance_id in [details.get("candidate_instance_id"), *(details.get("candidate_instance_ids") or [])]:
            if instance_id:
                _add_instance(grouped, getattr(position, "trade_date", ""), getattr(position, "code", ""), str(instance_id))
    for candidate in candidates:
        metadata = dict(candidate.metadata or {})
        instance_id = str(metadata.get("candidate_instance_id") or "")
        if instance_id:
            _add_instance(grouped, candidate.trade_date, candidate.code, instance_id)
    return grouped


def _single_candidate_key_by_code_date(intents: Iterable[dict], reviews: Iterable[Any], candidates: Iterable[Any]) -> dict[str, str]:
    grouped: dict[str, set[str]] = {}
    for intent in intents:
        candidate_id = intent.get("candidate_id")
        if candidate_id is not None:
            grouped.setdefault(_code_date_key(str(intent.get("trade_date") or ""), str(intent.get("code") or "")), set()).add(f"cand_id:{intent.get('trade_date') or ''}:{candidate_id}")
    for review in reviews:
        if review.candidate_id is not None:
            grouped.setdefault(_code_date_key(str(review.trade_date or ""), str(review.code or "")), set()).add(f"cand_id:{review.trade_date or ''}:{review.candidate_id}")
    for candidate in candidates:
        if candidate.id is not None:
            grouped.setdefault(_code_date_key(str(candidate.trade_date or ""), str(candidate.code or "")), set()).add(f"cand_id:{candidate.trade_date or ''}:{candidate.id}")
    return {
        key: next(iter(values))
        for key, values in grouped.items()
        if key != ":" and len(values) == 1
    }


def _add_instance(grouped: dict[str, set[str]], trade_date: Any, code: Any, instance_id: str) -> None:
    date_text = str(trade_date or "")
    code_text = str(code or "")
    if date_text and code_text and instance_id:
        grouped.setdefault(_code_date_key(date_text, code_text), set()).add(str(instance_id))


def _code_date_key(trade_date: str, code: str) -> str:
    return f"{trade_date}:{code}"


def _payload_candidate_instance_id(payload: dict | None) -> str:
    for source in _payload_dicts(payload):
        value = source.get("candidate_instance_id")
        if value:
            return str(value)
        bridge = source.get("theme_lab_bridge")
        if isinstance(bridge, dict) and bridge.get("candidate_instance_id"):
            return str(bridge.get("candidate_instance_id"))
    return ""


def _payload_candidate_generation_seq(payload: dict | None) -> Optional[int]:
    for source in _payload_dicts(payload):
        value = source.get("candidate_generation_seq")
        if value not in (None, ""):
            return _first_int(value)
        bridge = source.get("theme_lab_bridge")
        if isinstance(bridge, dict) and bridge.get("candidate_generation_seq") not in (None, ""):
            return _first_int(bridge.get("candidate_generation_seq"))
    return None


def _payload_dicts(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    result = [payload]
    for key in ("metadata", "request", "details", "safety", "live_safety"):
        value = payload.get(key)
        if isinstance(value, dict):
            result.append(value)
    return result


def _review_candidate_instance_id(review: Any) -> str:
    return _payload_candidate_instance_id({"details": dict(getattr(review, "details", {}) or {})})


def _review_candidate_generation_seq(review: Any) -> Optional[int]:
    return _payload_candidate_generation_seq({"details": dict(getattr(review, "details", {}) or {})})


def _time_window_fallback_key(payload: dict, trade_date: str, code: str) -> str:
    source = _first_text(payload.get("source"), _nested_value(payload, "metadata", "source"), _nested_value(payload, "request", "source"))
    strategy = _first_text(payload.get("strategy_name"), _nested_value(payload, "metadata", "strategy_name"), _nested_value(payload, "request", "strategy_name"))
    created_at = _first_text(payload.get("created_at"), _nested_value(payload, "response", "created_at"), _nested_value(payload, "metadata", "runtime_cycle_at"), _nested_value(payload, "request", "runtime_cycle_at"))
    if not (source and strategy and created_at):
        return ""
    return f"cand_window:{trade_date}:{code}:{source}:{strategy}:{created_at[:13]}"


def _nested_value(payload: dict, key: str, nested: str) -> Any:
    value = payload.get(key)
    if isinstance(value, dict):
        return value.get(nested)
    return None


def _best_attribution_link(links: list[AttributionLink]) -> Optional[AttributionLink]:
    if not links:
        return None
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW_MEDIUM": 1, "LOW": 0}
    return sorted(links, key=lambda item: rank.get(item.link_confidence, -1), reverse=True)[0]


def _attribution_confidence(link: Optional[AttributionLink]) -> str:
    if link is None:
        return "LOW"
    if link.matched_by == "ambiguous_code_date_fallback" or link.issue == "AMBIGUOUS_CANDIDATE_LINK":
        return "AMBIGUOUS"
    if link.matched_by in {"candidate_instance_id", "virtual_position_id", "virtual_order_to_position"}:
        return "HIGH"
    if link.matched_by == "candidate_id":
        return "MEDIUM"
    if link.matched_by == "weak_code_date_fallback":
        return "LOW"
    return str(link.link_confidence or "LOW").upper()


def _legacy_low_confidence_sample(candidate_instance_id: str, link: Optional[AttributionLink]) -> bool:
    if candidate_instance_id:
        return False
    matched_by = str(link.matched_by if link else "")
    return matched_by in {"", "weak_code_date_fallback", "code_date_time_window_source_strategy", "candidate_id"}


def _multi_generation_code_count(items: list[dict]) -> int:
    grouped: dict[str, set[str]] = {}
    for item in items:
        key = _code_date_key(str(item.get("trade_date") or ""), str(item.get("code") or ""))
        instance_id = str(item.get("candidate_instance_id") or "")
        if key != ":" and instance_id:
            grouped.setdefault(key, set()).add(instance_id)
    return sum(1 for values in grouped.values() if len(values) > 1)


def _generation_summary(items: list[dict]) -> dict[str, Any]:
    generations_by_code: dict[str, set[int]] = {}
    generation_reasons: list[str] = []
    excessive_count = 0
    for item in items:
        key = _code_date_key(str(item.get("trade_date") or ""), str(item.get("code") or ""))
        seq = _first_int(item.get("candidate_generation_seq"))
        if key != ":" and seq is not None:
            generations_by_code.setdefault(key, set()).add(seq)
        details = dict(item.get("details") or {})
        reason = str(details.get("generation_reason") or details.get("candidate_generation_reason") or "")
        if reason:
            generation_reasons.append(reason)
        if details.get("excessive_generation_blocked") or reason in {"same_generation_min_gap_guardrail", "same_generation_max_generation_guardrail"}:
            excessive_count += 1
    generation_counts = [len(values) for values in generations_by_code.values()]
    return {
        "avg_generation_per_code": round(sum(generation_counts) / len(generation_counts), 4) if generation_counts else None,
        "max_generation_per_code": max(generation_counts) if generation_counts else 0,
        "stale_re_detect_count": sum(1 for reason in generation_reasons if reason == "stale_re_detected"),
        "theme_change_generation_count": sum(1 for reason in generation_reasons if reason == "theme_changed"),
        "source_change_generation_count": sum(1 for reason in generation_reasons if reason == "source_changed"),
        "strategy_change_generation_count": sum(1 for reason in generation_reasons if reason == "strategy_changed"),
        "previous_lifecycle_closed_generation_count": sum(1 for reason in generation_reasons if reason == "previous_lifecycle_closed"),
        "manual_reset_generation_count": sum(1 for reason in generation_reasons if reason == "manual_reset"),
        "session_reset_generation_count": sum(1 for reason in generation_reasons if reason == "session_reset"),
        "excessive_generation_count": excessive_count,
    }


def _position_context_history_summary(items: list[dict]) -> dict[str, Any]:
    position_items = [item for item in items if item.get("virtual_position_id") is not None]
    context_items = [item for item in position_items if int(((item.get("details") or {}).get("position_context_history_count") or 0)) > 0]
    context_exit_details = [
        detail
        for item in items
        for detail in ((item.get("details") or {}).get("context_risk_exit_details") or [])
        if isinstance(detail, dict)
    ]
    confidence_by_type: dict[str, list[dict[str, Any]]] = {}
    for decision_type in ("THEME_WEAK_EXIT", "LEADER_COLLAPSE_EXIT", "INDEX_WEAK_EXIT", "MARKET_RISK_OFF_EXIT", "BREADTH_COLLAPSE_EXIT"):
        confidence_by_type[decision_type] = _top_counts(
            (detail.get("exit_confidence") or "UNKNOWN" for detail in context_exit_details if detail.get("decision_type") == decision_type),
            key="confidence",
        )
    data_limited_count = sum(
        1
        for detail in context_exit_details
        if detail.get("context_limited_reason") == "DATA_LIMITED_CONTEXT"
        or "DATA_LIMITED_CONTEXT" in [str(value) for value in detail.get("reason_codes") or []]
    )
    low_confidence_count = sum(
        1
        for detail in context_exit_details
        if detail.get("context_limited_reason") == "LOW_CONFIDENCE_EXIT" or str(detail.get("exit_confidence") or "").upper() == "LOW"
    )
    return {
        "positions_with_entry_context_count": sum(1 for item in position_items if (item.get("details") or {}).get("position_context_has_entry")),
        "positions_with_holding_context_count": sum(1 for item in position_items if (item.get("details") or {}).get("position_context_has_holding")),
        "positions_with_exit_context_count": sum(1 for item in position_items if (item.get("details") or {}).get("position_context_has_exit")),
        "position_context_coverage_pct": _ratio(len(context_items), len(position_items)),
        "data_limited_context_count": data_limited_count,
        "low_confidence_exit_count": low_confidence_count,
        "context_history_count_distribution": _top_counts(
            ((item.get("details") or {}).get("position_context_history_count", 0) for item in position_items),
            key="history_count",
        ),
        "context_risk_exit_confidence_distribution": _top_counts(
            (detail.get("exit_confidence") or "UNKNOWN" for detail in context_exit_details),
            key="confidence",
        ),
        "context_risk_exit_confidence_by_type": confidence_by_type,
        "theme_score_delta_distribution": _top_counts(
            (_theme_score_delta_bucket(detail.get("theme_score_delta")) for detail in context_exit_details),
            key="bucket",
        ),
        "leader_count_delta_distribution": _top_counts(
            (_leader_count_delta_bucket(detail.get("leader_count_delta")) for detail in context_exit_details),
            key="bucket",
        ),
        "index_status_deterioration_count": sum(1 for detail in context_exit_details if detail.get("index_status_deterioration")),
        "market_risk_off_exit_count": sum(1 for detail in context_exit_details if detail.get("decision_type") == "MARKET_RISK_OFF_EXIT"),
    }


def _is_context_risk_exit(decision_type: str, details: dict[str, Any]) -> bool:
    return decision_type in {
        "THEME_WEAK_EXIT",
        "LEADER_COLLAPSE_EXIT",
        "INDEX_WEAK_EXIT",
        "MARKET_RISK_OFF_EXIT",
        "BREADTH_COLLAPSE_EXIT",
    } or bool(details.get("exit_confidence") or details.get("context_limited_reason"))


def _theme_score_delta_bucket(value: Any) -> str:
    parsed = _optional_float(value)
    if parsed is None:
        return "UNKNOWN"
    if parsed <= -10:
        return "<=-10"
    if parsed <= -5:
        return "-10..-5"
    if parsed < 0:
        return "-5..0"
    return ">=0"


def _leader_count_delta_bucket(value: Any) -> str:
    parsed = _first_int(value)
    if parsed is None:
        return "UNKNOWN"
    if parsed <= -3:
        return "<=-3"
    if parsed < 0:
        return "-2..-1"
    if parsed == 0:
        return "0"
    return ">=1"


def _count_lifecycles_with_match(items: list[dict], matched_by: str) -> int:
    count = 0
    for item in items:
        if item.get("matched_by") == matched_by:
            count += 1
            continue
        links = ((item.get("details") or {}).get("attribution_links") or [])
        if any(link.get("matched_by") == matched_by for link in links if isinstance(link, dict)):
            count += 1
    return count


def _issue_reasons(reason_counts: dict[str, dict[str, int]], issue: str) -> list[dict[str, Any]]:
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.get(issue, {}).items(), key=lambda pair: pair[1], reverse=True)
    ]


def _missing_price_reason(entry: Optional[dict], metadata: dict, request: dict) -> str:
    if entry is None:
        return "NO_ENTRY_INTENT_PRICE_SOURCE"
    for key in ("price_source", "quantity_calculation_reason", "reject_reason"):
        value = metadata.get(key) or request.get(key)
        if value:
            label = "QUANTITY_CALCULATION" if key == "quantity_calculation_reason" else key.upper()
            return f"{label}:{value}"
    safety = dict(entry.get("safety") or {})
    if safety.get("reason"):
        return f"SAFETY:{safety.get('reason')}"
    if entry.get("reason"):
        return f"INTENT_REASON:{entry.get('reason')}"
    return "ENTRY_INTENT_PRICE_ZERO"


def _missing_quantity_reason(entry: Optional[dict], metadata: dict, safety: dict) -> str:
    if entry is None:
        return "NO_ENTRY_INTENT_QUANTITY_SOURCE"
    quantity_reason = str(metadata.get("quantity_calculation_reason") or "")
    if quantity_reason:
        return f"QUANTITY_CALCULATION:{quantity_reason}"
    if safety.get("reason"):
        return f"SAFETY:{safety.get('reason')}"
    if entry.get("reason"):
        return f"INTENT_REASON:{entry.get('reason')}"
    return "ENTRY_INTENT_QUANTITY_ZERO"


def _is_completed(item: dict) -> bool:
    return str(item.get("final_status") or "") in {
        ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
        ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value,
    } or bool(item.get("closed_at"))


def _is_take_profit_status(final_status: str, exit_decision_types: list[str]) -> bool:
    return final_status in {
        ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value,
    } or "TAKE_PROFIT" in exit_decision_types


def _quality_bucket(classification: str, fp_type: str, fn_type: str, data_status: str) -> str:
    if fp_type:
        return "FALSE_POSITIVE"
    if fn_type:
        return "FALSE_NEGATIVE"
    if classification == "true_positive":
        return "TRUE_POSITIVE"
    if classification == "true_negative":
        return "TRUE_NEGATIVE"
    if data_status != "OK":
        return "INSUFFICIENT_DATA"
    return "PENDING"


def _score_bucket(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    for start, end in [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]:
        if start <= value < end:
            return f"{start}-{end if end < 101 else 100}"
    return "UNKNOWN"


def _hold_minutes(opened_at: str, closed_at: str) -> Optional[float]:
    if not opened_at or not closed_at:
        return None
    opened = _parse_time(opened_at)
    closed = _parse_time(closed_at)
    if opened is None or closed is None:
        return None
    return max(0.0, (closed - opened).total_seconds() / 60.0)


def _parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _first_not_none(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values) -> str:
    value = _first_not_none(*values)
    return str(value) if value is not None else ""


def _first_float(*values) -> Optional[float]:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return None


def _first_int(*values) -> Optional[int]:
    for value in values:
        if value is not None and value != "":
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _last_int(values: Iterable[Any]) -> int:
    parsed = 0
    for value in values:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number:
            parsed = number
    return parsed


def _float_tuple(value: Any, default: tuple[float, ...]) -> tuple[float, ...]:
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = str(value or "").split(",")
    parsed: list[float] = []
    for item in raw_values:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) if parsed else default


def _int_tuple(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = str(value or "").split(",")
    parsed: list[int] = []
    for item in raw_values:
        try:
            parsed.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) if parsed else default


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable[Any]) -> Optional[float]:
    parsed = [_optional_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(sum(parsed) / len(parsed), 4)


def _min_float(values: Iterable[Any]) -> Optional[float]:
    parsed = [_optional_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(min(parsed), 4)


def _profit_factor(values: Iterable[Any]) -> Optional[float]:
    parsed = [_optional_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    gross_profit = sum(value for value in parsed if value > 0.0)
    gross_loss = abs(sum(value for value in parsed if value < 0.0))
    if gross_loss == 0.0:
        return None if gross_profit == 0.0 else round(gross_profit, 4)
    return round(gross_profit / gross_loss, 4)


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _unique(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _unique_ints(values: Iterable[Any]) -> list[int]:
    output: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value is None or value == "":
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        output.append(number)
    return output


def _num(value: Any) -> str:
    parsed = _optional_float(value)
    return "-" if parsed is None else f"{parsed:.2f}%"


def _pct(value: Any) -> str:
    parsed = _optional_float(value)
    return "-" if parsed is None else f"{parsed * 100:.1f}%"


def _markdown_count_lines(rows: list[dict], key: str) -> list[str]:
    if not rows:
        return ["- none"]
    return [f"- {row.get(key, '')}: {row.get('count', 0)}" for row in rows]


def _markdown_group_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["- none"]
    output = []
    for row in rows:
        key = row.get("key", row.get("decision_type", row.get("reason_code", "UNKNOWN")))
        output.append(
            f"- {key}: count={row.get('count', 0)}, win_rate={_pct(row.get('win_rate'))}, "
            f"avg_return={_num(row.get('avg_realized_return_pct'))}, net={_num(row.get('net_expectancy'))}, FP={row.get('false_positive_count', 0)}, "
            f"FN={row.get('false_negative_count', 0)}"
        )
    return output


def _markdown_go_no_go_lines(go_no_go: dict) -> list[str]:
    if not go_no_go:
        return ["- decision: NO_GO", "- reason: no go/no-go data"]
    lines = [
        f"- decision: {go_no_go.get('decision', 'NO_GO')}",
        f"- readiness: {go_no_go.get('readiness', 'INSUFFICIENT_DATA')}",
        f"- review_only: {bool(go_no_go.get('review_only', True))}",
    ]
    for item in go_no_go.get("criteria") or []:
        status = "PASS" if item.get("passed") else "FAIL"
        lines.append(f"- {item.get('code')}: {status} (value={item.get('value')}, threshold={item.get('threshold')})")
    return lines


def _markdown_cost_assumption_lines(assumption: dict) -> list[str]:
    if not assumption:
        return ["- no cost assumption"]
    return [
        f"- commission_bp_per_side: {assumption.get('commission_bp_per_side')}",
        f"- sell_tax_bp: {assumption.get('sell_tax_bp')}",
        f"- primary_slippage_bp: {assumption.get('primary_slippage_bp')}",
        f"- primary_entry_delay_sec: {assumption.get('primary_entry_delay_sec')}",
        f"- primary_round_trip_cost_pct: {_num(assumption.get('primary_round_trip_cost_pct'))}",
    ]


def _markdown_cost_scenario_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return ["- none"]
    return [
        "- slip={slip}bp delay={delay}s samples={samples} net={net} win={win}".format(
            slip=row.get("slippage_bp"),
            delay=row.get("entry_delay_sec"),
            samples=row.get("sample_count", 0),
            net=_num(row.get("net_expectancy")),
            win=_pct(row.get("net_win_rate")),
        )
        for row in rows[:20]
    ]


def _markdown_fill_quality_lines(fill_quality: dict) -> list[str]:
    if not fill_quality:
        return ["- no fill simulation data"]
    return [
        f"- samples: {fill_quality.get('sample_count', 0)}",
        f"- filled: {fill_quality.get('filled_count', 0)} ({_pct(fill_quality.get('filled_rate'))})",
        f"- skipped: {fill_quality.get('skipped_count', 0)} ({_pct(fill_quality.get('skipped_rate'))})",
        f"- partial_fill_rate: {_pct(fill_quality.get('partial_fill_rate'))}",
        f"- stale_tick_rate: {_pct(fill_quality.get('stale_tick_rate'))}",
        f"- avg_slippage_bps: {fill_quality.get('avg_slippage_bps')}",
        f"- median_slippage_bps: {fill_quality.get('median_slippage_bps')}",
        f"- fill_adjusted_expectancy: {_num(fill_quality.get('fill_adjusted_expectancy_pct'))}",
        f"- fill_adjusted_profit_factor: {fill_quality.get('fill_adjusted_profit_factor') if fill_quality.get('fill_adjusted_profit_factor') is not None else '-'}",
    ]


def _markdown_execution_realism_lines(realism: dict) -> list[str]:
    if not realism:
        return ["- no execution realism data"]
    return [
        f"- limit_price_hit_rate: {_pct(realism.get('limit_price_hit_rate'))}",
        f"- partial_fill_high_risk: {realism.get('partial_fill_high_risk_count', 0)} ({_pct(realism.get('partial_fill_high_risk_rate'))})",
        f"- spread_high_risk: {realism.get('spread_high_risk_count', 0)} ({_pct(realism.get('spread_high_risk_rate'))})",
        f"- stale_tick: {realism.get('stale_tick_count', 0)} ({_pct(realism.get('stale_tick_rate'))})",
        f"- high_gateway_latency: {realism.get('gateway_latency_high_count', 0)} ({_pct(realism.get('gateway_latency_high_rate'))})",
        f"- avg_entry_tick_age_sec: {realism.get('avg_entry_tick_age_sec')}",
        f"- avg_gateway_command_latency_ms: {realism.get('avg_gateway_command_latency_ms')}",
    ]


def _markdown_quality_lines(data_quality: dict) -> list[str]:
    issues = data_quality.get("issues") or []
    output = [
        f"- Missing review: {data_quality.get('missing_review_count', 0)}",
        f"- Missing position: {data_quality.get('missing_position_count', 0)}",
        f"- Orphan entry: {data_quality.get('orphan_entry_count', 0)}",
        f"- Orphan exit: {data_quality.get('orphan_exit_count', 0)}",
        "",
        "### Support Coverage",
        *_markdown_support_coverage_lines(data_quality),
        "",
        "### Data Quality Issues",
    ]
    if not issues:
        output.append("- no major data quality issue")
        return output
    for item in issues:
        output.append(f"- {item.get('issue')}: {item.get('count')}")
        for reason in (item.get("reasons") or [])[:5]:
            output.append(f"  - reason={reason.get('reason')}: {reason.get('count')}")
        samples = item.get("samples") or []
        if samples:
            sample_text = ", ".join(
                f"{sample.get('code') or '?'}:{sample.get('intent_id') or sample.get('lifecycle_id') or '?'}"
                for sample in samples[:3]
            )
            output.append(f"  - samples: {sample_text}")
    return output


def _markdown_support_coverage_lines(data_quality: dict) -> list[str]:
    coverage = dict(data_quality.get("support_coverage") or {})
    reasons = data_quality.get("support_missing_reasons") or []
    output = [
        f"- sample_count: {coverage.get('sample_count', 0)}",
        f"- recent_support_price_present: {coverage.get('recent_support_price_present_count', 0)}",
        f"- vwap_present: {coverage.get('vwap_present_count', 0)}",
        f"- vwap_ready: {coverage.get('vwap_ready_count', 0)}",
        f"- minute_bar_present: {coverage.get('minute_bar_present_count', 0)}",
        f"- support_reclaimed: {coverage.get('support_reclaimed_count', 0)}",
    ]
    if reasons:
        output.append("- support_missing_reasons: " + ", ".join(f"{row.get('reason')}={row.get('count', 0)}" for row in reasons))
    return output


def _markdown_support_vwap_coverage_lines(summary: dict) -> list[str]:
    if not summary:
        return ["- no support coverage samples"]
    output = [
        f"- support_metadata_coverage_pct: {_pct(summary.get('support_metadata_coverage_pct'))}",
        f"- vwap_metadata_coverage_pct: {_pct(summary.get('vwap_metadata_coverage_pct'))}",
        f"- minute_bar_coverage_pct: {_pct(summary.get('minute_bar_coverage_pct'))}",
        f"- stale_vwap_count: {summary.get('stale_vwap_count', 0)}",
        f"- diagnostic_only_due_to_support_count: {summary.get('diagnostic_only_due_to_support_count', 0)}",
        f"- diagnostic_only_later_rallied_count: {summary.get('diagnostic_only_later_rallied_count', 0)}",
        f"- SUPPORT_STRUCTURALLY_MISSING_AND_RALLIED: {summary.get('SUPPORT_STRUCTURALLY_MISSING_AND_RALLIED', 0)}",
        f"- SUPPORT_DATA_MISSING_AND_RALLIED: {summary.get('SUPPORT_DATA_MISSING_AND_RALLIED', 0)}",
        f"- SUPPORT_NOT_READY_AND_RALLIED: {summary.get('SUPPORT_NOT_READY_AND_RALLIED', 0)}",
        "### Support Missing Reasons",
        *_markdown_count_lines(summary.get("support_missing_count_by_reason", []), "reason"),
        "### Support Source Distribution",
        *_markdown_count_lines(summary.get("support_source_distribution", []), "source"),
        "### Minute Bar Quality",
        *_markdown_count_lines(summary.get("minute_bar_quality_status_counts", []), "status"),
    ]
    return output


def _markdown_context_confidence_lines(by_type: dict) -> list[str]:
    if not by_type:
        return ["- none"]
    output: list[str] = []
    for decision_type, rows in by_type.items():
        if not rows:
            output.append(f"- {decision_type}: none")
            continue
        values = ", ".join(f"{row.get('confidence')}={row.get('count', 0)}" for row in rows)
        output.append(f"- {decision_type}: {values}")
    return output


def _markdown_prune_lines(summary: dict) -> list[str]:
    if not summary:
        return ["- no prune run recorded"]
    return [
        f"- pruned_context_history_rows: {summary.get('pruned_context_history_rows', 0)}",
        f"- retained_context_history_rows: {summary.get('retained_context_history_rows', 0)}",
        f"- oldest_retained_context_at: {summary.get('oldest_retained_context_at', '')}",
        f"- prune_error_count: {summary.get('prune_error_count', 0)}",
    ]
