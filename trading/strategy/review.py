from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candles import Candle, CandleBuilder, minute_start
from trading.strategy.exit import SUPPORT_LOSS, TAKE_PROFIT, TIME_EXIT
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateState,
    EntryPlan,
    ExitDecision,
    GateDecision,
    ReviewFinalStatus,
    TradeReview,
    VirtualOrder,
    VirtualOrderStatus,
    VirtualPosition,
)
from trading.strategy.pipeline import GatePipelineResult


FALSE_NEGATIVE_RALLY_THRESHOLD_PCT = 3.0
FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT = -3.0


class TradeReviewService:
    def build_review(
        self,
        candidate: Candidate,
        gate_result: Optional[GatePipelineResult] = None,
        entry_plan: Optional[EntryPlan] = None,
        virtual_order: Optional[VirtualOrder] = None,
        virtual_position: Optional[VirtualPosition] = None,
        exit_decisions: Optional[list[ExitDecision]] = None,
        candle_builder: Optional[CandleBuilder] = None,
        created_at: Optional[datetime] = None,
    ) -> TradeReview:
        if candidate.id is None:
            raise ValueError("candidate.id is required to build TradeReview")
        now = (created_at or datetime.now()).replace(microsecond=0)
        decisions = list(exit_decisions or [])
        final_status = _final_status(candidate, gate_result, entry_plan, virtual_order, virtual_position, decisions)
        theme_id = _theme_id(candidate, gate_result, entry_plan)
        gate_result_key = _gate_result_key(candidate, gate_result, entry_plan)
        review_key = gate_result_key or f"{candidate.id}:{theme_id}:phase1"
        details = {
            "gate_decisions_snapshot": _gate_snapshot(gate_result.decisions if gate_result else []),
            "candidate_state": candidate.state.value,
            "candidate_block_type": candidate.block_type.value,
            "entry_plan_created": entry_plan is not None,
            "horizon_start_at": "",
            "horizon_start_reason": "",
        }
        details.update(_partial_exit_details(virtual_position, decisions))

        horizon_start, horizon_reason = _horizon_start(candidate, gate_result, entry_plan, virtual_order, virtual_position, final_status)
        details["horizon_start_at"] = horizon_start.isoformat() if horizon_start else ""
        details["horizon_start_reason"] = horizon_reason
        base_price = _base_price(gate_result, entry_plan, virtual_order, virtual_position)
        metrics = _horizon_metrics(candle_builder, candidate.code, horizon_start, base_price)
        if final_status == ReviewFinalStatus.EXPIRED.value and candidate.detected_at:
            detected_metrics = _horizon_metrics(candle_builder, candidate.code, _parse_time(candidate.detected_at), base_price)
            details["detected_at_metrics"] = detected_metrics
        if final_status == ReviewFinalStatus.VIRTUAL_UNFILLED.value and virtual_order and entry_plan:
            timeout_at = _parse_time(virtual_order.submitted_at) + timedelta(seconds=max(0, entry_plan.order_timeout_sec))
            details["timeout_at_metrics"] = _horizon_metrics(candle_builder, candidate.code, timeout_at, base_price)

        false_negative_type = _false_negative_type(final_status, metrics)
        details["false_negative_type"] = false_negative_type
        false_positive = _false_positive(final_status, virtual_position, details, metrics)
        final_grade = gate_result.final_grade if gate_result else ""
        exit_reason = _exit_reason(final_status, virtual_position, decisions)
        review = TradeReview(
            candidate_id=candidate.id,
            trade_date=candidate.trade_date,
            code=candidate.code,
            name=candidate.name,
            market=candidate.market,
            theme_id=theme_id,
            theme_name=_theme_name(gate_result, entry_plan),
            strategy_profile=_strategy_profile(candidate, gate_result, entry_plan),
            gate_result_key=gate_result_key,
            review_key=review_key,
            entry_plan_id=entry_plan.id if entry_plan else None,
            virtual_order_id=virtual_order.id if virtual_order else None,
            virtual_position_id=virtual_position.id if virtual_position else None,
            final_grade=final_grade,
            final_status=final_status,
            virtual_order_status=virtual_order.status.value if virtual_order else "",
            exit_reason=exit_reason,
            entry_price=virtual_position.entry_price if virtual_position else _plan_or_order_price(entry_plan, virtual_order),
            exit_price=virtual_position.close_price if virtual_position else 0,
            max_return_5m=metrics["max_return_5m"],
            max_return_10m=metrics["max_return_10m"],
            max_return_20m=metrics["max_return_20m"],
            max_drawdown_20m=metrics["max_drawdown_20m"],
            missed_reason=_missed_reason(final_status, false_negative_type),
            false_negative_flag=bool(false_negative_type),
            false_positive_flag=false_positive,
            expired_but_later_rallied=false_negative_type == "EXPIRED_LATER_RALLIED",
            blocked_but_later_rallied=false_negative_type == "BLOCKED_LATER_RALLIED",
            details=details,
            created_at=now.isoformat(),
        )
        return review


def _final_status(
    candidate: Candidate,
    gate_result: Optional[GatePipelineResult],
    entry_plan: Optional[EntryPlan],
    virtual_order: Optional[VirtualOrder],
    virtual_position: Optional[VirtualPosition],
    exit_decisions: list[ExitDecision],
) -> str:
    if candidate.state == CandidateState.EXPIRED:
        return ReviewFinalStatus.EXPIRED.value
    full_exit = _latest_full_exit(exit_decisions)
    if full_exit is not None:
        if full_exit.decision_type == SUPPORT_LOSS:
            return ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value
        if full_exit.decision_type == TIME_EXIT:
            return ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value
        return ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value
    if any(decision.decision_type == TAKE_PROFIT and decision.details.get("partial_exit") for decision in exit_decisions):
        return ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value
    if virtual_order is not None:
        if virtual_order.status == VirtualOrderStatus.SUBMITTED:
            return ReviewFinalStatus.VIRTUAL_SUBMITTED.value
        if virtual_order.status == VirtualOrderStatus.FILLED:
            return ReviewFinalStatus.VIRTUAL_FILLED.value
        if virtual_order.status == VirtualOrderStatus.UNFILLED:
            return ReviewFinalStatus.VIRTUAL_UNFILLED.value
        if virtual_order.status == VirtualOrderStatus.CANCELLED:
            return ReviewFinalStatus.VIRTUAL_CANCELLED.value
    if entry_plan is None and gate_result is not None:
        if _data_insufficient(gate_result):
            return ReviewFinalStatus.DATA_INSUFFICIENT.value
        if gate_result.block_type == BlockType.TEMPORARY:
            return ReviewFinalStatus.BLOCKED_TEMP.value
        if gate_result.block_type == BlockType.FINAL:
            return ReviewFinalStatus.BLOCKED_FINAL.value
        return ReviewFinalStatus.PLAN_NOT_CREATED.value
    if virtual_position is not None:
        return ReviewFinalStatus.VIRTUAL_FILLED.value
    return ReviewFinalStatus.PLAN_NOT_CREATED.value


def _horizon_start(
    candidate: Candidate,
    gate_result: Optional[GatePipelineResult],
    entry_plan: Optional[EntryPlan],
    virtual_order: Optional[VirtualOrder],
    virtual_position: Optional[VirtualPosition],
    final_status: str,
) -> tuple[Optional[datetime], str]:
    if virtual_position is not None and virtual_position.opened_at:
        return _parse_time(virtual_position.opened_at), "position_opened_at"
    if virtual_order is not None:
        if final_status == ReviewFinalStatus.VIRTUAL_CANCELLED.value and virtual_order.cancelled_at:
            return _parse_time(virtual_order.cancelled_at), "virtual_order_cancelled_at"
        if virtual_order.submitted_at:
            return _parse_time(virtual_order.submitted_at), "virtual_order_submitted_at"
    if candidate.state == CandidateState.EXPIRED and candidate.expires_at:
        return _parse_time(candidate.expires_at), "candidate_expired_at"
    gate_time = _gate_evaluated_at(gate_result)
    if gate_time is not None:
        return gate_time, "gate_evaluated_at"
    if entry_plan is not None and entry_plan.created_at:
        return _parse_time(entry_plan.created_at), "entry_plan_created_at"
    if candidate.detected_at:
        return _parse_time(candidate.detected_at), "candidate_detected_at"
    return None, "unknown"


def _horizon_metrics(
    candle_builder: Optional[CandleBuilder],
    code: str,
    start_at: Optional[datetime],
    base_price: int,
) -> dict[str, Optional[float]]:
    empty = {
        "max_return_5m": None,
        "max_return_10m": None,
        "max_return_20m": None,
        "max_drawdown_20m": None,
    }
    if candle_builder is None or start_at is None or base_price <= 0:
        return empty
    candles = [
        candle
        for candle in candle_builder.completed_candles(code, 1)
        if candle.start_at > minute_start(start_at)
    ]
    return {
        "max_return_5m": _max_return(candles, start_at, base_price, 5),
        "max_return_10m": _max_return(candles, start_at, base_price, 10),
        "max_return_20m": _max_return(candles, start_at, base_price, 20),
        "max_drawdown_20m": _max_drawdown(candles, start_at, base_price, 20),
    }


def _max_return(candles: list[Candle], start_at: datetime, base_price: int, minutes: int) -> Optional[float]:
    selected = _candles_until(candles, start_at, minutes)
    if not selected:
        return None
    return _return_pct(max(candle.high for candle in selected), base_price)


def _max_drawdown(candles: list[Candle], start_at: datetime, base_price: int, minutes: int) -> Optional[float]:
    selected = _candles_until(candles, start_at, minutes)
    if not selected:
        return None
    return _return_pct(min(candle.low for candle in selected), base_price)


def _candles_until(candles: list[Candle], start_at: datetime, minutes: int) -> list[Candle]:
    end_at = start_at + timedelta(minutes=minutes)
    return [candle for candle in candles if candle.start_at <= end_at]


def _gate_snapshot(decisions: list[GateDecision]) -> list[dict]:
    return [
        {
            "gate_name": decision.gate_name,
            "score": decision.score,
            "grade": decision.grade,
            "passed": decision.passed,
            "block_type": decision.block_type.value,
            "reason_codes": list(decision.reason_codes),
            "details": dict(decision.details),
        }
        for decision in decisions
    ]


def _partial_exit_details(virtual_position: Optional[VirtualPosition], decisions: list[ExitDecision]) -> dict:
    partial = next((decision for decision in decisions if decision.decision_type == TAKE_PROFIT and decision.details.get("partial_exit")), None)
    full = _latest_full_exit(decisions)
    details = {
        "partial_take_profit_hit": partial is not None,
        "partial_exit_return_pct": None,
        "full_close_return_pct": virtual_position.realized_return_pct if virtual_position and virtual_position.closed_at else None,
        "weighted_virtual_return_pct": None,
    }
    if partial is not None:
        partial_return = partial.details.get("target_return_pct")
        if partial_return is None and virtual_position and virtual_position.entry_price > 0:
            partial_return = _return_pct(partial.trigger_price, virtual_position.entry_price)
        partial_return = float(partial_return or 0.0)
        exit_percent = float(partial.details.get("exit_percent") or 70.0)
        full_return = details["full_close_return_pct"]
        if full_return is None and full is not None and virtual_position and virtual_position.entry_price > 0:
            full_return = _return_pct(full.trigger_price, virtual_position.entry_price)
        weighted = partial_return * (exit_percent / 100.0)
        if full_return is not None:
            weighted += float(full_return) * ((100.0 - exit_percent) / 100.0)
        details["partial_exit_return_pct"] = partial_return
        details["full_close_return_pct"] = full_return
        details["weighted_virtual_return_pct"] = round(weighted, 6)
    return details


def _false_negative_type(final_status: str, metrics: dict[str, Optional[float]]) -> str:
    max_return = metrics.get("max_return_20m")
    if max_return is None or max_return < FALSE_NEGATIVE_RALLY_THRESHOLD_PCT:
        return ""
    if final_status in {ReviewFinalStatus.BLOCKED_TEMP.value, ReviewFinalStatus.BLOCKED_FINAL.value, ReviewFinalStatus.DATA_INSUFFICIENT.value}:
        return "BLOCKED_LATER_RALLIED"
    if final_status == ReviewFinalStatus.EXPIRED.value:
        return "EXPIRED_LATER_RALLIED"
    if final_status == ReviewFinalStatus.VIRTUAL_UNFILLED.value:
        return "UNFILLED_LATER_RALLIED"
    if final_status == ReviewFinalStatus.PLAN_NOT_CREATED.value:
        return "PLAN_NOT_CREATED_LATER_RALLIED"
    return ""


def _false_positive(
    final_status: str,
    virtual_position: Optional[VirtualPosition],
    details: dict,
    metrics: dict[str, Optional[float]],
) -> bool:
    if virtual_position is None:
        return False
    if details.get("partial_take_profit_hit"):
        weighted = details.get("weighted_virtual_return_pct")
        return weighted is not None and float(weighted) < 0
    if virtual_position.closed_at and virtual_position.realized_return_pct < 0:
        return True
    drawdown = metrics.get("max_drawdown_20m")
    return drawdown is not None and drawdown <= FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT


def _missed_reason(final_status: str, false_negative_type: str) -> str:
    if false_negative_type:
        return false_negative_type
    if final_status in {
        ReviewFinalStatus.BLOCKED_TEMP.value,
        ReviewFinalStatus.BLOCKED_FINAL.value,
        ReviewFinalStatus.EXPIRED.value,
        ReviewFinalStatus.VIRTUAL_UNFILLED.value,
        ReviewFinalStatus.PLAN_NOT_CREATED.value,
    }:
        return final_status
    return ""


def _theme_id(candidate: Candidate, gate_result: Optional[GatePipelineResult], entry_plan: Optional[EntryPlan]) -> str:
    if gate_result is not None:
        return gate_result.theme_id or ""
    if entry_plan is not None:
        return str(entry_plan.cancel_condition.get("theme_id") or "")
    return candidate.theme_ids[0] if candidate.theme_ids else ""


def _theme_name(gate_result: Optional[GatePipelineResult], entry_plan: Optional[EntryPlan]) -> str:
    if gate_result is not None:
        return str(gate_result.details.get("theme_name") or "")
    if entry_plan is not None:
        return str(entry_plan.cancel_condition.get("theme_name") or "")
    return ""


def _strategy_profile(candidate: Candidate, gate_result: Optional[GatePipelineResult], entry_plan: Optional[EntryPlan]) -> str:
    if entry_plan is not None and entry_plan.cancel_condition.get("strategy_profile"):
        return str(entry_plan.cancel_condition.get("strategy_profile"))
    if candidate.strategy_profile is not None:
        return candidate.strategy_profile.value
    if gate_result is not None and gate_result.snapshot is not None:
        return str(gate_result.snapshot.metadata.get("strategy_profile") or "")
    return ""


def _gate_result_key(candidate: Candidate, gate_result: Optional[GatePipelineResult], entry_plan: Optional[EntryPlan]) -> str:
    if entry_plan is not None and entry_plan.cancel_condition.get("gate_result_key"):
        return str(entry_plan.cancel_condition.get("gate_result_key"))
    if gate_result is not None:
        return f"{gate_result.candidate_id}:{gate_result.code}:{gate_result.theme_id}:{gate_result.final_grade}"
    return f"{candidate.id}:{candidate.code}:{_theme_id(candidate, gate_result, entry_plan)}"


def _base_price(
    gate_result: Optional[GatePipelineResult],
    entry_plan: Optional[EntryPlan],
    virtual_order: Optional[VirtualOrder],
    virtual_position: Optional[VirtualPosition],
) -> int:
    if virtual_position is not None and virtual_position.entry_price > 0:
        return virtual_position.entry_price
    if virtual_order is not None and virtual_order.limit_price > 0:
        return virtual_order.limit_price
    if entry_plan is not None and entry_plan.limit_price > 0:
        return entry_plan.limit_price
    if gate_result is not None and gate_result.snapshot is not None:
        return gate_result.snapshot.price
    return 0


def _plan_or_order_price(entry_plan: Optional[EntryPlan], virtual_order: Optional[VirtualOrder]) -> int:
    if virtual_order is not None and virtual_order.limit_price:
        return virtual_order.limit_price
    if entry_plan is not None:
        return entry_plan.limit_price
    return 0


def _exit_reason(final_status: str, virtual_position: Optional[VirtualPosition], decisions: list[ExitDecision]) -> str:
    if virtual_position is not None and virtual_position.close_reason:
        return virtual_position.close_reason
    full = _latest_full_exit(decisions)
    if full is not None:
        return full.decision_type
    if final_status == ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value:
        return TAKE_PROFIT
    return ""


def _latest_full_exit(decisions: list[ExitDecision]) -> Optional[ExitDecision]:
    full = [
        decision
        for decision in decisions
        if decision.filled and decision.details.get("position_closed") is True
    ]
    return full[-1] if full else None


def _data_insufficient(gate_result: GatePipelineResult) -> bool:
    return any(
        "DATA_INSUFFICIENT" in decision.reason_codes or decision.details.get("sub_status") == "DATA_INSUFFICIENT"
        for decision in gate_result.decisions
    )


def _gate_evaluated_at(gate_result: Optional[GatePipelineResult]) -> Optional[datetime]:
    if gate_result is None:
        return None
    for decision in reversed(gate_result.decisions):
        if decision.created_at:
            return _parse_time(decision.created_at)
    if gate_result.snapshot is not None and gate_result.snapshot.created_at:
        return _parse_time(gate_result.snapshot.created_at)
    return None


def _return_pct(price: int, base_price: int) -> float:
    if base_price <= 0:
        return 0.0
    return round(((price - base_price) / base_price) * 100.0, 6)


def _parse_time(value: str) -> datetime:
    if not value:
        return datetime.min
    return datetime.fromisoformat(value)
