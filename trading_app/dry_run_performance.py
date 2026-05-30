from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase
from trading.broker.models import new_message_id, utc_timestamp
from trading.strategy.models import ReviewFinalStatus


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "dry_run_performance"


@dataclass(frozen=True)
class DryRunPerformanceConfig:
    fp_loss_threshold_pct: float = -1.0
    fp_drawdown_threshold_pct: float = -3.0
    fn_rally_threshold_pct: float = 3.0
    good_trade_threshold_pct: float = 2.0
    min_hold_minutes_for_final: int = 20
    pending_grace_minutes: int = 30


@dataclass
class DryRunTradeLifecycle:
    lifecycle_id: str
    trade_date: str = ""
    code: str = ""
    name: str = ""
    strategy_name: str = ""
    candidate_id: Optional[int] = None
    theme_name: str = ""
    theme_score: Optional[float] = None
    gate_status: str = ""
    gate_reason: str = ""
    gate_score: Optional[float] = None
    hybrid_score: Optional[float] = None
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
    review_false_positive_mismatch: bool = False
    review_false_negative_mismatch: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        if lifecycle.blocked_but_later_rallied:
            false_negative_type = false_negative_type or "GATE_BLOCKED_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "GATE_BLOCKED_BUT_RALLIED"
            signal_classification = "false_negative"
        if lifecycle.expired_but_later_rallied:
            false_negative_type = false_negative_type or "EXPIRED_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "EXPIRED_BUT_RALLIED"
            signal_classification = "false_negative"
        if not entry_exists and lifecycle.trade_review_id and max_return is not None and max_return >= self.config.fn_rally_threshold_pct:
            false_negative_type = false_negative_type or "NO_ENTRY_INTENT_BUT_RALLIED"
            opportunity_loss_type = opportunity_loss_type or "NO_ENTRY_INTENT_BUT_RALLIED"
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
        recommendations = self.recommendations(summary, grouped, false_signal_summary)
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        return {
            "report_id": report_id,
            "status": "READY",
            "generated_at": generated_at,
            "trade_date": trade_date or "",
            "filters": filters,
            "summary": {**summary, "data_quality": data_quality},
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
        decisions_by_position: dict[int, list[Any]] = {}
        for decision in self.db.list_exit_decisions_for_analysis():
            if decision.virtual_position_id is not None:
                decisions_by_position.setdefault(decision.virtual_position_id, []).append(decision)
        candidates = {
            candidate.id: candidate
            for candidate in self.db.list_candidates(trade_date=trade_date)
            if candidate.id is not None
        }
        groups: dict[str, dict[str, list[Any]]] = {}

        def bucket(key: str) -> dict[str, list[Any]]:
            return groups.setdefault(key, {"intents": [], "reviews": []})

        for intent in intents:
            bucket(_lifecycle_key_for_intent(intent, position_by_order))["intents"].append(intent)
        for review in reviews:
            bucket(_lifecycle_key_for_review(review, position_by_order))["reviews"].append(review)

        lifecycles: list[DryRunTradeLifecycle] = []
        for key, group in groups.items():
            lifecycle = self._build_lifecycle(
                key,
                list(group.get("intents") or []),
                list(group.get("reviews") or []),
                positions,
                position_by_order,
                decisions_by_position,
                candidates,
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
        decisions_by_position: dict[int, list[Any]],
        candidates: dict[int, Any],
    ) -> DryRunTradeLifecycle:
        entry_intents = [intent for intent in intents if _intent_phase(intent) == "entry" or str(intent.get("side")) == "buy"]
        exit_intents = [intent for intent in intents if _intent_phase(intent) == "exit" or str(intent.get("side")) == "sell"]
        entry_intents.sort(key=lambda item: int(item.get("id") or 0))
        exit_intents.sort(key=lambda item: int(item.get("id") or 0))
        review = sorted(reviews, key=lambda item: int(item.id or 0), reverse=True)[0] if reviews else None
        entry = entry_intents[0] if entry_intents else None

        position = _position_for_group(entry, exit_intents, review, positions, position_by_order)
        candidate_id = _first_not_none(
            entry.get("candidate_id") if entry else None,
            review.candidate_id if review else None,
            position.candidate_id if position else None,
            *[intent.get("candidate_id") for intent in exit_intents],
        )
        candidate = candidates.get(int(candidate_id)) if candidate_id is not None else None
        position_decisions = decisions_by_position.get(int(position.id), []) if position and position.id is not None else []
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
        lifecycle = DryRunTradeLifecycle(
            lifecycle_id=key,
            trade_date=trade_date,
            code=code,
            name=str(_first_not_none(review.name if review else None, candidate.name if candidate else None) or ""),
            strategy_name=str(_first_not_none(entry.get("strategy_name") if entry else None, review.strategy_profile if review else None) or ""),
            candidate_id=int(candidate_id) if candidate_id is not None else None,
            theme_name=str(_first_not_none(entry_request.get("theme_name"), review.theme_name if review else None, entry_metadata.get("theme_name")) or ""),
            theme_score=_optional_float(_first_not_none(entry_request.get("theme_score"), entry_metadata.get("theme_score"), review_details.get("theme_score"))),
            gate_status=str(_first_not_none(entry.get("gate_status") if entry else None, review_details.get("gate_status")) or ""),
            gate_reason=str(_first_not_none(entry.get("gate_reason") if entry else None, review_details.get("primary_reason_code"), review_details.get("gate_reason")) or ""),
            gate_score=_optional_float(_first_not_none(entry_request.get("gate_score"), entry_metadata.get("gate_score"), review_details.get("gate_score"))),
            hybrid_score=_optional_float(_first_not_none(entry_request.get("hybrid_score"), entry_metadata.get("hybrid_score"), review_details.get("hybrid_score"))),
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
            score_bucket=_score_bucket(_optional_float(_first_not_none(entry_request.get("hybrid_score"), entry_request.get("gate_score"), review_details.get("hybrid_score")))),
            details={
                "entry_intent_count": len(entry_intents),
                "exit_intent_count": len(exit_intents),
                "entry_intent_ids": [intent.get("intent_id") for intent in entry_intents],
                "review_false_positive_flag": bool(review.false_positive_flag) if review else False,
                "review_false_negative_flag": bool(review.false_negative_flag) if review else False,
                "review_false_positive_type": review_details.get("false_positive_type", "") if review else "",
                "review_false_negative_type": review_details.get("false_negative_type", "") if review else "",
            },
        )
        lifecycle.data_quality_issues = _data_quality_issues(lifecycle, bool(entry), bool(exit_intents), bool(review), bool(position), position_decisions)
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
        return {
            "total_lifecycle_count": len(items),
            "entry_intent_count": sum(1 for item in items if item.get("entry_intent_id")),
            "exit_intent_count": sum(len(item.get("exit_intent_ids") or []) for item in items),
            "completed_lifecycle_count": len(completed),
            "open_lifecycle_count": sum(1 for item in items if item.get("quality_bucket") in {"PENDING", "INSUFFICIENT_DATA"}),
            "orphan_entry_count": sum(1 for item in items if "ORPHAN_ENTRY" in item.get("data_quality_issues", [])),
            "orphan_exit_count": sum(1 for item in items if "ORPHAN_EXIT" in item.get("data_quality_issues", [])),
            "review_missing_count": sum(1 for item in items if "REVIEW_MISSING" in item.get("data_quality_issues", [])),
            "position_missing_count": sum(1 for item in items if "POSITION_MISSING" in item.get("data_quality_issues", [])),
            "avg_realized_return_pct": _avg(realized_values),
            "median_realized_return_pct": median(realized_values) if realized_values else None,
            "win_rate": _ratio(len(wins), len(completed)),
            "loss_rate": _ratio(len(losses), len(completed)),
            "avg_max_return_5m": _avg([item.get("max_return_5m") for item in items]),
            "avg_max_return_10m": _avg([item.get("max_return_10m") for item in items]),
            "avg_max_return_20m": _avg([item.get("max_return_20m") for item in items]),
            "avg_max_drawdown_20m": _avg([item.get("max_drawdown_20m") for item in items]),
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
            "false_positive_count": sum(1 for item in items if item.get("dry_run_false_positive_type")),
            "false_negative_count": sum(1 for item in items if item.get("dry_run_false_negative_type")),
            "true_positive_count": sum(1 for item in items if item.get("signal_classification") == "true_positive"),
            "true_negative_count": sum(1 for item in items if item.get("signal_classification") == "true_negative"),
            "pending_count": sum(1 for item in items if item.get("signal_classification") in {"pending", "insufficient_data"}),
            "false_positive_rate": _ratio(sum(1 for item in items if item.get("dry_run_false_positive_type")), len(items)),
            "false_negative_rate": _ratio(sum(1 for item in items if item.get("dry_run_false_negative_type")), len(items)),
            "opportunity_loss_count": sum(1 for item in items if item.get("opportunity_loss_type")),
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
            "by_gate_status": _group_stats(items, "gate_status"),
            "by_session_bucket": _group_stats(items, "session_bucket"),
            "by_code": _group_stats(items, "code"),
            "by_live_reject_reason": _group_stats(items, "entry_live_reject_reason"),
            "by_decision_safety_reason": _group_stats(items, "entry_decision_safety_reason"),
            "by_score_bucket": _group_stats(items, "score_bucket"),
            "by_exit_decision_type": _multi_group_stats(items, "exit_decision_types", "decision_type"),
        }

    def data_quality(self, items: list[dict]) -> dict[str, Any]:
        issues: dict[str, dict[str, Any]] = {}
        for item in items:
            for issue in item.get("data_quality_issues") or []:
                bucket = issues.setdefault(issue, {"count": 0, "samples": []})
                bucket["count"] += 1
                if len(bucket["samples"]) < 5:
                    bucket["samples"].append(
                        {
                            "code": item.get("code", ""),
                            "intent_id": item.get("entry_intent_id") or (item.get("exit_intent_ids") or [""])[0],
                            "lifecycle_id": item.get("lifecycle_id", ""),
                        }
                    )
        return {
            "issues": [
                {"issue": issue, **payload}
                for issue, payload in sorted(issues.items(), key=lambda pair: pair[1]["count"], reverse=True)
            ],
            "missing_review_count": issues.get("REVIEW_MISSING", {}).get("count", 0),
            "missing_position_count": issues.get("POSITION_MISSING", {}).get("count", 0),
            "orphan_entry_count": issues.get("ORPHAN_ENTRY", {}).get("count", 0),
            "orphan_exit_count": issues.get("ORPHAN_EXIT", {}).get("count", 0),
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
            "hybrid_score",
            "theme_score",
        ]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in report.get("items", []):
                row = dict(item)
                row["exit_intent_ids"] = ",".join(str(value) for value in row.get("exit_intent_ids") or [])
                row["exit_decision_types"] = ",".join(str(value) for value in row.get("exit_decision_types") or [])
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
            "",
            "## DRY_RUN Intent Summary",
            f"- Entry intents: {summary.get('entry_intent_count', 0)}",
            f"- Exit intents: {summary.get('exit_intent_count', 0)}",
            f"- Live would pass: {summary.get('live_would_pass_count', 0)}",
            f"- Live would reject: {summary.get('live_would_reject_count', 0)}",
            "",
            "## False Signals",
            f"- False positives: {summary.get('false_positive_count', 0)}",
            f"- False negatives: {summary.get('false_negative_count', 0)}",
            f"- Opportunity loss: {summary.get('opportunity_loss_count', 0)}",
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
            "## Theme Performance",
            *_markdown_group_lines(grouped.get("by_theme_name", [])[:10]),
            "",
            "## Session Bucket Performance",
            *_markdown_group_lines(grouped.get("by_session_bucket", [])[:10]),
            "",
            "## Exit Decision Type Performance",
            *_markdown_group_lines(grouped.get("by_exit_decision_type", [])[:10]),
            "",
            "## Recommendations",
            *[f"- {item}" for item in report.get("recommendations", [])],
            "",
            "## Data Quality",
            *_markdown_quality_lines(summary.get("data_quality", {})),
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
    )


def _lifecycle_key_for_intent(intent: dict, position_by_order: dict[int, Any]) -> str:
    virtual_position_id = intent.get("virtual_position_id")
    if virtual_position_id is not None:
        return f"vp:{virtual_position_id}"
    virtual_order_id = intent.get("virtual_order_id")
    if virtual_order_id is not None and int(virtual_order_id) in position_by_order:
        return f"vp:{position_by_order[int(virtual_order_id)].id}"
    if virtual_order_id is not None:
        return f"vo:{virtual_order_id}"
    trade_review_id = intent.get("trade_review_id")
    if trade_review_id is not None:
        return f"review:{trade_review_id}"
    candidate_id = intent.get("candidate_id")
    if candidate_id is not None or intent.get("code"):
        return f"cand:{intent.get('trade_date', '')}:{candidate_id or ''}:{intent.get('code', '')}"
    prefix = "orphan_exit" if _intent_phase(intent) == "exit" or intent.get("side") == "sell" else "orphan_entry"
    return f"{prefix}:{intent.get('intent_id', '')}"


def _lifecycle_key_for_review(review: Any, position_by_order: dict[int, Any]) -> str:
    if review.virtual_position_id is not None:
        return f"vp:{review.virtual_position_id}"
    if review.virtual_order_id is not None and int(review.virtual_order_id) in position_by_order:
        return f"vp:{position_by_order[int(review.virtual_order_id)].id}"
    if review.candidate_id is not None or review.code:
        return f"cand:{review.trade_date}:{review.candidate_id or ''}:{review.code}"
    return f"review:{review.id}"


def _position_for_group(
    entry: Optional[dict],
    exit_intents: list[dict],
    review: Any,
    positions: dict[int, Any],
    position_by_order: dict[int, Any],
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
    return None


def _intent_phase(intent: dict) -> str:
    return str(intent.get("order_phase") or ("exit" if str(intent.get("side") or "") == "sell" else "entry"))


def _data_quality_issues(
    lifecycle: DryRunTradeLifecycle,
    has_entry: bool,
    has_exit: bool,
    has_review: bool,
    has_position: bool,
    position_decisions: list[Any],
) -> list[str]:
    issues: list[str] = []
    if has_entry and not has_review:
        issues.append("REVIEW_MISSING")
    if has_entry and not has_position:
        issues.append("POSITION_MISSING")
    if has_entry and not has_exit and lifecycle.final_status in {
        ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value,
        ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
    }:
        issues.append("EXIT_INTENT_MISSING")
    if has_exit and not has_entry:
        issues.append("ORPHAN_EXIT")
    if has_entry and not has_position and not has_review:
        issues.append("ORPHAN_ENTRY")
    if has_exit and not position_decisions:
        issues.append("EXIT_DECISION_MISSING")
    if lifecycle.entry_price <= 0:
        issues.append("MISSING_PRICE")
    if has_entry and lifecycle.entry_quantity <= 0:
        issues.append("MISSING_QUANTITY")
    if has_entry and lifecycle.entry_live_reject_reason == "" and not lifecycle.entry_live_would_pass:
        issues.append("MISSING_LIVE_SAFETY_JSON")
    if has_review and lifecycle.max_return_20m is None:
        issues.append("MISSING_HORIZON_METRICS")
    if has_position and not lifecycle.closed_at and has_entry:
        issues.append("STALE_OPEN_POSITION")
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
    return {
        "key": key,
        "count": len(values),
        "completed": len(completed),
        "win_rate": _ratio(len(wins), len(completed)),
        "avg_realized_return_pct": _avg([item.get("realized_return_pct") for item in completed]),
        "avg_max_return_20m": _avg([item.get("max_return_20m") for item in values]),
        "avg_max_drawdown_20m": _avg([item.get("max_drawdown_20m") for item in values]),
        "false_positive_count": sum(1 for item in values if item.get("dry_run_false_positive_type")),
        "false_positive_rate": _ratio(sum(1 for item in values if item.get("dry_run_false_positive_type")), len(values)),
        "false_negative_count": sum(1 for item in values if item.get("dry_run_false_negative_type")),
        "opportunity_loss_count": sum(1 for item in values if item.get("opportunity_loss_type")),
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
        key = row.get("key", row.get("decision_type", "UNKNOWN"))
        output.append(
            f"- {key}: count={row.get('count', 0)}, win_rate={_pct(row.get('win_rate'))}, "
            f"avg_return={_num(row.get('avg_realized_return_pct'))}, FP={row.get('false_positive_count', 0)}, "
            f"FN={row.get('false_negative_count', 0)}"
        )
    return output


def _markdown_quality_lines(data_quality: dict) -> list[str]:
    issues = data_quality.get("issues") or []
    if not issues:
        return ["- no major data quality issue"]
    return [f"- {item.get('issue')}: {item.get('count')}" for item in issues]
