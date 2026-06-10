from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable


STAGES = ("observe", "dry_run", "live_sim", "real_micro")
PROMOTE_ACTION = "PROMOTE"
HOLD_ACTION = "HOLD"
DEMOTE_ACTION = "DEMOTE"
BLOCK_ACTION = "BLOCK"

OPPORTUNITY_LOSS_LABELS = {
    "EARLY_OPPORTUNITY_LOSS",
    "MISSED_OPPORTUNITY",
    "WAIT_RESOLVED_TO_READY",
}
FALSE_POSITIVE_LABELS = {
    "EARLY_FALSE_POSITIVE",
    "FALSE_POSITIVE",
    "BAD_READY",
    "PROTECTED_FROM_CHASE",
}
GOOD_READY_LABELS = {
    "GOOD_READY",
    "GOOD_ENTRY",
    "WIN",
}
GOOD_BLOCK_LABELS = {
    "GOOD_BLOCK",
    "PROTECTED_FROM_LOSS",
    "RISK_BLOCK_EFFECTIVE",
}
DATA_INSUFFICIENT_LABELS = {
    "DATA_INSUFFICIENT",
    "REVIEW_NEEDED",
}
RISK_CASE_LABELS = FALSE_POSITIVE_LABELS | {"RISK_CASE", "LOSS"}
REALTIME_LOW_REASONS = {
    "REALTIME_RELIABILITY_LOW",
    "WAIT_DATA_REALTIME_RELIABILITY_LOW",
    "REALTIME_RELIABILITY_BUCKET_LOW",
    "REALTIME_RELIABILITY_BUCKET_BROKEN",
}


@dataclass(frozen=True)
class PromotionThresholds:
    min_decision_count: int
    min_trade_day_count: int
    min_order_count: int = 0
    min_live_sim_order_count: int = 0
    min_fill_count: int = 0
    max_false_positive_rate: float = 0.25
    max_opportunity_loss_rate: float = 0.20
    max_risk_case_rate: float = 0.25
    max_data_insufficient_rate: float = 0.25
    max_order_error_rate: float = 0.02
    max_duplicate_order_count: int = 0
    min_avg_return_pct: float = 0.0
    min_realtime_high_ratio: float = 0.60
    max_realtime_low_missed_rate: float = 0.25

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionControllerConfig:
    enabled: bool = True
    rolling_decision_limit: int = 100
    default_current_stage: str = "observe"
    dry_run: PromotionThresholds = field(
        default_factory=lambda: PromotionThresholds(
            min_decision_count=50,
            min_trade_day_count=1,
            max_false_positive_rate=0.35,
            max_opportunity_loss_rate=0.35,
            max_data_insufficient_rate=0.40,
            min_realtime_high_ratio=0.45,
        )
    )
    live_sim: PromotionThresholds = field(
        default_factory=lambda: PromotionThresholds(
            min_decision_count=100,
            min_trade_day_count=2,
            min_order_count=30,
            max_false_positive_rate=0.25,
            max_opportunity_loss_rate=0.25,
            max_risk_case_rate=0.25,
            max_data_insufficient_rate=0.25,
            max_order_error_rate=0.02,
            min_avg_return_pct=0.0,
            min_realtime_high_ratio=0.60,
            max_realtime_low_missed_rate=0.20,
        )
    )
    real_micro: PromotionThresholds = field(
        default_factory=lambda: PromotionThresholds(
            min_decision_count=150,
            min_trade_day_count=3,
            min_order_count=50,
            min_live_sim_order_count=20,
            min_fill_count=20,
            max_false_positive_rate=0.20,
            max_opportunity_loss_rate=0.20,
            max_risk_case_rate=0.20,
            max_data_insufficient_rate=0.20,
            max_order_error_rate=0.0,
            max_duplicate_order_count=0,
            min_avg_return_pct=0.05,
            min_realtime_high_ratio=0.70,
            max_realtime_low_missed_rate=0.10,
        )
    )
    kill_switch_active: bool = False
    max_consecutive_error_count: int = 1
    allow_real_micro: bool = False

    def threshold_for(self, target_stage: str) -> PromotionThresholds:
        target = normalize_stage(target_stage)
        if target == "dry_run":
            return self.dry_run
        if target == "live_sim":
            return self.live_sim
        if target == "real_micro":
            return self.real_micro
        return self.dry_run

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["dry_run"] = self.dry_run.to_dict()
        payload["live_sim"] = self.live_sim.to_dict()
        payload["real_micro"] = self.real_micro.to_dict()
        return payload


@dataclass(frozen=True)
class PromotionEvidence:
    policy_id: str
    current_stage: str = "observe"
    decision_count: int = 0
    trade_day_count: int = 0
    order_count: int = 0
    live_sim_order_count: int = 0
    fill_count: int = 0
    avg_return_pct: float = 0.0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    realtime_bucket_counts: dict[str, int] = field(default_factory=dict)
    gate_reason_counts: dict[str, int] = field(default_factory=dict)
    order_error_count: int = 0
    duplicate_order_count: int = 0
    consecutive_error_count: int = 0
    realtime_low_missed_count: int = 0
    source_ids: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PromotionDecision:
    policy_id: str
    current_stage: str
    target_stage: str
    recommended_stage: str
    action: str
    eligible: bool
    confidence: float
    blockers: list[str]
    warnings: list[str]
    metrics: dict[str, Any]
    rollout_plan: dict[str, Any]
    rollback_plan: dict[str, Any]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromotionController:
    def __init__(self, *, config: PromotionControllerConfig | None = None) -> None:
        self.config = config or PromotionControllerConfig()

    def evaluate(self, evidence: PromotionEvidence | dict[str, Any]) -> PromotionDecision:
        ev = evidence if isinstance(evidence, PromotionEvidence) else evidence_from_summary(evidence)
        current_stage = normalize_stage(ev.current_stage or self.config.default_current_stage)
        target_stage = next_stage(current_stage)
        metrics = _metrics(ev)
        blockers: list[str] = []
        warnings: list[str] = []

        if not self.config.enabled:
            blockers.append("PROMOTION_CONTROLLER_DISABLED")
        if self.config.kill_switch_active:
            blockers.append("KILL_SWITCH_ACTIVE")
        if ev.consecutive_error_count > self.config.max_consecutive_error_count:
            blockers.append("CONSECUTIVE_ORDER_ERRORS")
        if current_stage == "real_micro":
            return self._decision(
                ev,
                current_stage=current_stage,
                target_stage=current_stage,
                recommended_stage=current_stage if not blockers else "live_sim",
                action=HOLD_ACTION if not blockers else DEMOTE_ACTION,
                blockers=blockers,
                warnings=warnings,
                metrics=metrics,
            )
        if target_stage == "real_micro" and not self.config.allow_real_micro:
            blockers.append("REAL_MICRO_REQUIRES_OPERATOR_APPROVAL")

        thresholds = self.config.threshold_for(target_stage)
        blockers.extend(_threshold_blockers(ev, metrics, thresholds, target_stage))
        warnings.extend(_threshold_warnings(ev, metrics, thresholds, target_stage))

        eligible = not blockers
        action = PROMOTE_ACTION if eligible else HOLD_ACTION
        recommended_stage = target_stage if eligible else current_stage
        confidence = _confidence(metrics, blockers, warnings)
        return self._decision(
            ev,
            current_stage=current_stage,
            target_stage=target_stage,
            recommended_stage=recommended_stage,
            action=action,
            blockers=blockers,
            warnings=warnings,
            metrics=metrics,
            confidence=confidence,
        )

    def _decision(
        self,
        evidence: PromotionEvidence,
        *,
        current_stage: str,
        target_stage: str,
        recommended_stage: str,
        action: str,
        blockers: list[str],
        warnings: list[str],
        metrics: dict[str, Any],
        confidence: float | None = None,
    ) -> PromotionDecision:
        now = datetime.now().isoformat(timespec="seconds")
        return PromotionDecision(
            policy_id=evidence.policy_id,
            current_stage=current_stage,
            target_stage=target_stage,
            recommended_stage=recommended_stage,
            action=action,
            eligible=not blockers and action == PROMOTE_ACTION,
            confidence=_confidence(metrics, blockers, warnings) if confidence is None else confidence,
            blockers=list(dict.fromkeys(blockers)),
            warnings=list(dict.fromkeys(warnings)),
            metrics=metrics,
            rollout_plan=_rollout_plan(target_stage),
            rollback_plan=_rollback_plan(current_stage, target_stage),
            generated_at=now,
        )


def build_promotion_evidence(
    *,
    policy_id: str,
    current_stage: str = "observe",
    decision_outcomes: Iterable[dict[str, Any]] = (),
    runtime_order_intents: Iterable[dict[str, Any]] = (),
    live_sim_orders: Iterable[dict[str, Any]] = (),
) -> PromotionEvidence:
    outcomes = [dict(item or {}) for item in decision_outcomes or []]
    intents = [dict(item or {}) for item in runtime_order_intents or []]
    live_orders = [dict(item or {}) for item in live_sim_orders or []]
    outcome_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    trade_days: set[str] = set()
    returns: list[float] = []
    low_missed = 0
    source_ids: list[str] = []

    for row in outcomes:
        label = _upper(row.get("outcome_label") or row.get("label"))
        if label:
            outcome_counts[label] += 1
        trade_date = str(row.get("trade_date") or "")[:10]
        if trade_date:
            trade_days.add(trade_date)
        value = _number(
            row.get("current_return_pct"),
            row.get("return_pct"),
            row.get("max_return_pct"),
        )
        if value is not None:
            returns.append(value)
        reason_codes = _reason_codes_from_row(row)
        for reason in reason_codes:
            reason_counts[reason] += 1
        bucket = _realtime_bucket(row)
        if bucket:
            bucket_counts[bucket] += 1
        if label in OPPORTUNITY_LOSS_LABELS and any(reason in REALTIME_LOW_REASONS for reason in reason_codes):
            low_missed += 1
        source_id = str(row.get("outcome_id") or row.get("decision_id") or "")
        if source_id:
            source_ids.append(source_id)

    order_error_count = 0
    duplicate_order_count = 0
    for intent in intents:
        status = _upper(intent.get("status") or intent.get("result_status") or intent.get("order_status"))
        metadata = _metadata(intent)
        reason_codes = _reason_codes_from_row(intent) + _reason_codes_from_row(metadata)
        if status in {"ERROR", "FAILED", "REJECTED"} or "ORDER_REJECTED" in reason_codes:
            order_error_count += 1
        if _bool(intent.get("duplicate")) or _bool(metadata.get("duplicate")) or "DUPLICATE_ORDER" in reason_codes:
            duplicate_order_count += 1
        bucket = _realtime_bucket(intent) or _realtime_bucket(metadata)
        if bucket:
            bucket_counts[bucket] += 1

    live_error_count = 0
    fill_count = 0
    for order in live_orders:
        status = _upper(order.get("status") or order.get("order_status"))
        if status in {"FILLED", "PARTIAL_FILLED", "PARTIAL"} or _number(order.get("filled_qty"), order.get("fill_qty")):
            fill_count += 1
        if status in {"ERROR", "FAILED", "REJECTED", "CANCEL_FAILED"}:
            live_error_count += 1

    decision_count = len(outcomes)
    avg_return = round(sum(returns) / len(returns), 4) if returns else 0.0
    return PromotionEvidence(
        policy_id=policy_id,
        current_stage=normalize_stage(current_stage),
        decision_count=decision_count,
        trade_day_count=len(trade_days),
        order_count=len(intents),
        live_sim_order_count=len(live_orders),
        fill_count=fill_count,
        avg_return_pct=avg_return,
        outcome_counts=dict(outcome_counts),
        realtime_bucket_counts=dict(bucket_counts),
        gate_reason_counts=dict(reason_counts),
        order_error_count=order_error_count + live_error_count,
        duplicate_order_count=duplicate_order_count,
        consecutive_error_count=_tail_error_count(intents + live_orders),
        realtime_low_missed_count=low_missed,
        source_ids=list(dict.fromkeys(source_ids))[:50],
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )


def evidence_from_summary(payload: dict[str, Any]) -> PromotionEvidence:
    data = dict(payload or {})
    return PromotionEvidence(
        policy_id=str(data.get("policy_id") or "unknown"),
        current_stage=normalize_stage(data.get("current_stage") or data.get("stage") or "observe"),
        decision_count=int(data.get("decision_count") or data.get("sample_count") or 0),
        trade_day_count=int(data.get("trade_day_count") or data.get("trade_days") or 0),
        order_count=int(data.get("order_count") or data.get("dry_run_intent_count") or 0),
        live_sim_order_count=int(data.get("live_sim_order_count") or 0),
        fill_count=int(data.get("fill_count") or 0),
        avg_return_pct=float(data.get("avg_return_pct") or 0.0),
        outcome_counts=dict(data.get("outcome_counts") or data.get("by_outcome_label") or {}),
        realtime_bucket_counts=dict(data.get("realtime_bucket_counts") or {}),
        gate_reason_counts=dict(data.get("gate_reason_counts") or {}),
        order_error_count=int(data.get("order_error_count") or 0),
        duplicate_order_count=int(data.get("duplicate_order_count") or 0),
        consecutive_error_count=int(data.get("consecutive_error_count") or 0),
        realtime_low_missed_count=int(data.get("realtime_low_missed_count") or 0),
        source_ids=list(data.get("source_ids") or []),
        generated_at=str(data.get("generated_at") or ""),
    )


def normalize_stage(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in STAGES else "observe"


def next_stage(current_stage: str) -> str:
    stage = normalize_stage(current_stage)
    index = STAGES.index(stage)
    return STAGES[min(index + 1, len(STAGES) - 1)]


def _threshold_blockers(
    evidence: PromotionEvidence,
    metrics: dict[str, Any],
    thresholds: PromotionThresholds,
    target_stage: str,
) -> list[str]:
    blockers: list[str] = []
    if evidence.decision_count < thresholds.min_decision_count:
        blockers.append("INSUFFICIENT_DECISION_SAMPLE")
    if evidence.trade_day_count < thresholds.min_trade_day_count:
        blockers.append("INSUFFICIENT_TRADE_DAYS")
    if evidence.order_count < thresholds.min_order_count:
        blockers.append("INSUFFICIENT_DRY_RUN_ORDERS")
    if evidence.live_sim_order_count < thresholds.min_live_sim_order_count:
        blockers.append("INSUFFICIENT_LIVE_SIM_ORDERS")
    if evidence.fill_count < thresholds.min_fill_count:
        blockers.append("INSUFFICIENT_FILL_SAMPLE")
    if metrics["false_positive_rate"] > thresholds.max_false_positive_rate:
        blockers.append("FALSE_POSITIVE_RATE_HIGH")
    if metrics["opportunity_loss_rate"] > thresholds.max_opportunity_loss_rate:
        blockers.append("OPPORTUNITY_LOSS_RATE_HIGH")
    if metrics["risk_case_rate"] > thresholds.max_risk_case_rate:
        blockers.append("RISK_CASE_RATE_HIGH")
    if metrics["data_insufficient_rate"] > thresholds.max_data_insufficient_rate:
        blockers.append("DATA_INSUFFICIENT_RATE_HIGH")
    if metrics["order_error_rate"] > thresholds.max_order_error_rate:
        blockers.append("ORDER_ERROR_RATE_HIGH")
    if evidence.duplicate_order_count > thresholds.max_duplicate_order_count:
        blockers.append("DUPLICATE_ORDER_DETECTED")
    if evidence.avg_return_pct < thresholds.min_avg_return_pct:
        blockers.append("EXPECTANCY_BELOW_THRESHOLD")
    if metrics["realtime_high_ratio"] < thresholds.min_realtime_high_ratio:
        blockers.append("REALTIME_HIGH_RATIO_LOW")
    if metrics["realtime_low_missed_rate"] > thresholds.max_realtime_low_missed_rate:
        blockers.append("REALTIME_LOW_MISSED_RATE_HIGH")
    if target_stage == "real_micro" and evidence.order_error_count > 0:
        blockers.append("REAL_MICRO_REQUIRES_ZERO_ORDER_ERRORS")
    return blockers


def _threshold_warnings(
    evidence: PromotionEvidence,
    metrics: dict[str, Any],
    thresholds: PromotionThresholds,
    target_stage: str,
) -> list[str]:
    warnings: list[str] = []
    if evidence.decision_count < thresholds.min_decision_count * 1.5:
        warnings.append("SAMPLE_STILL_THIN")
    if metrics["realtime_high_ratio"] < min(0.85, thresholds.min_realtime_high_ratio + 0.10):
        warnings.append("REALTIME_QUALITY_MARGIN_THIN")
    if target_stage == "real_micro":
        warnings.append("REAL_MICRO_USE_ONE_SYMBOL_AND_SMALL_NOTIONAL")
    return warnings


def _metrics(evidence: PromotionEvidence) -> dict[str, Any]:
    outcome_counts = {str(key).upper(): int(value or 0) for key, value in (evidence.outcome_counts or {}).items()}
    decision_count = max(1, int(evidence.decision_count or sum(outcome_counts.values()) or 0))
    false_positive = sum(outcome_counts.get(label, 0) for label in FALSE_POSITIVE_LABELS)
    opportunity_loss = sum(outcome_counts.get(label, 0) for label in OPPORTUNITY_LOSS_LABELS)
    data_insufficient = sum(outcome_counts.get(label, 0) for label in DATA_INSUFFICIENT_LABELS)
    risk_case = sum(outcome_counts.get(label, 0) for label in RISK_CASE_LABELS)
    good_ready = sum(outcome_counts.get(label, 0) for label in GOOD_READY_LABELS)
    good_block = sum(outcome_counts.get(label, 0) for label in GOOD_BLOCK_LABELS)
    bucket_counts = {str(key).upper(): int(value or 0) for key, value in (evidence.realtime_bucket_counts or {}).items()}
    bucket_total = max(1, sum(bucket_counts.values()))
    order_total = max(1, evidence.order_count + evidence.live_sim_order_count)
    low_bucket_count = bucket_counts.get("LOW", 0) + bucket_counts.get("BROKEN", 0)
    return {
        "decision_count": evidence.decision_count,
        "trade_day_count": evidence.trade_day_count,
        "order_count": evidence.order_count,
        "live_sim_order_count": evidence.live_sim_order_count,
        "fill_count": evidence.fill_count,
        "avg_return_pct": evidence.avg_return_pct,
        "false_positive_count": false_positive,
        "false_positive_rate": round(false_positive / decision_count, 4),
        "opportunity_loss_count": opportunity_loss,
        "opportunity_loss_rate": round(opportunity_loss / decision_count, 4),
        "risk_case_count": risk_case,
        "risk_case_rate": round(risk_case / decision_count, 4),
        "data_insufficient_count": data_insufficient,
        "data_insufficient_rate": round(data_insufficient / decision_count, 4),
        "good_ready_count": good_ready,
        "good_block_count": good_block,
        "order_error_count": evidence.order_error_count,
        "order_error_rate": round(evidence.order_error_count / order_total, 4),
        "duplicate_order_count": evidence.duplicate_order_count,
        "realtime_high_ratio": round(bucket_counts.get("HIGH", 0) / bucket_total, 4),
        "realtime_low_ratio": round(low_bucket_count / bucket_total, 4),
        "realtime_low_missed_count": evidence.realtime_low_missed_count,
        "realtime_low_missed_rate": round(evidence.realtime_low_missed_count / max(1, low_bucket_count), 4),
        "outcome_counts": outcome_counts,
        "realtime_bucket_counts": bucket_counts,
        "gate_reason_counts": dict(evidence.gate_reason_counts or {}),
    }


def _confidence(metrics: dict[str, Any], blockers: list[str], warnings: list[str]) -> float:
    if blockers:
        return 0.0
    sample_score = min(1.0, float(metrics.get("decision_count") or 0) / 150.0)
    quality_score = min(1.0, max(0.0, float(metrics.get("realtime_high_ratio") or 0.0)))
    risk_penalty = min(
        0.8,
        float(metrics.get("false_positive_rate") or 0.0)
        + float(metrics.get("risk_case_rate") or 0.0)
        + float(metrics.get("order_error_rate") or 0.0),
    )
    warning_penalty = min(0.2, len(warnings) * 0.05)
    return round(max(0.0, min(1.0, 0.45 * sample_score + 0.45 * quality_score + 0.10 - risk_penalty - warning_penalty)), 4)


def _rollout_plan(target_stage: str) -> dict[str, Any]:
    stage = normalize_stage(target_stage)
    if stage == "dry_run":
        return {"stage": stage, "mode": "no_broker_order", "order_notional_krw": 0, "max_symbols": 0}
    if stage == "live_sim":
        return {"stage": stage, "mode": "kiwoom_simulation_account", "order_notional_krw": 0, "max_symbols": 3}
    if stage == "real_micro":
        return {
            "stage": stage,
            "mode": "real_order_micro",
            "order_notional_krw": 50000,
            "max_symbols": 1,
            "daily_loss_limit_pct": 0.5,
            "requires_operator_approval": True,
        }
    return {"stage": stage, "mode": "observe_only", "order_notional_krw": 0, "max_symbols": 0}


def _rollback_plan(current_stage: str, target_stage: str) -> dict[str, Any]:
    return {
        "from_stage": normalize_stage(target_stage),
        "to_stage": normalize_stage(current_stage),
        "triggers": [
            "order_error_detected",
            "false_positive_rate_above_threshold",
            "realtime_high_ratio_below_threshold",
            "kill_switch_active",
        ],
    }


def _reason_codes_from_row(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("reason_codes", "reason_codes_json", "gate_reason", "primary_reason_code"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.replace("|", ",").split(",")
        elif isinstance(raw, Iterable):
            parsed = list(raw)
        else:
            parsed = [raw]
        if isinstance(parsed, dict):
            parsed = parsed.values()
        values.extend(_upper(item) for item in parsed if _upper(item))
    metadata = _metadata(row)
    if metadata and metadata is not row:
        values.extend(_reason_codes_from_row(metadata))
    return list(dict.fromkeys(values))


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata") or row.get("metadata_json") or row.get("details") or row.get("details_json") or {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _realtime_bucket(row: dict[str, Any]) -> str:
    metadata = _metadata(row)
    for source in (row, metadata):
        value = source.get("realtime_reliability_bucket") or source.get("gateway_realtime_reliability_bucket")
        if value:
            return _upper(value)
        gate = source.get("realtime_reliability_gate")
        if isinstance(gate, dict) and gate.get("bucket"):
            return _upper(gate.get("bucket"))
    return ""


def _tail_error_count(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in reversed(rows):
        status = _upper(row.get("status") or row.get("order_status") or row.get("result_status"))
        if status in {"ERROR", "FAILED", "REJECTED", "CANCEL_FAILED"}:
            count += 1
            continue
        break
    return count


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()
