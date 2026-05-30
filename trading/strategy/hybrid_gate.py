from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional

from trading.strategy.models import BlockType, Candidate, GateDecision
from trading.strategy.reason_codes import normalize_reason_codes
from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings
from trading.theme_engine.models import (
    StockLeadershipResult,
    ThemeActivitySnapshot,
    ThemeContext,
    ThemeStatus,
    ThemeStrengthResult,
)


class HybridGateStatus(str, Enum):
    READY = "READY"
    WAIT = "WAIT"
    BLOCKED = "BLOCKED"
    OBSERVE = "OBSERVE"


class HybridPositionTier(str, Enum):
    NONE = "none"
    OBSERVE_ONLY = "observe_only"
    SMALL_FIRST_ENTRY = "small_first_entry"
    NORMAL_FIRST_ENTRY = "normal_first_entry"
    BLOCKED = "blocked"


@dataclass
class HybridGateComponent:
    name: str
    score: float = 0.0
    status: str = ""
    reason_codes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["score"] = round(_clamp(self.score), 4)
        payload["reason_codes"] = normalize_reason_codes(self.reason_codes)
        return payload


@dataclass
class HybridGateConfig:
    hybrid_gate_enabled: bool = True
    hybrid_gate_observe_only: bool = True
    hybrid_min_ready_score: float = 75.0
    hybrid_min_small_entry_score: float = 65.0
    hybrid_theme_weight: float = 0.30
    hybrid_leadership_weight: float = 0.20
    hybrid_entry_weight: float = 0.25
    hybrid_market_weight: float = 0.15
    hybrid_risk_weight: float = 0.10
    watch_theme_allows_small_entry: bool = False
    leader_only_blocks_laggards: bool = True
    min_membership_score: float = 0.55
    min_theme_breadth: float = 0.35
    max_rank_in_theme_for_ready: int = 5

    @classmethod
    def from_settings(cls, settings: Optional[StrategyRuntimeSettings] = None) -> "HybridGateConfig":
        active = settings or legacy_strategy_runtime_settings()
        return cls(
            hybrid_gate_enabled=_bool_setting(active, "hybrid_gate.enabled", True),
            hybrid_gate_observe_only=_bool_setting(active, "hybrid_gate.observe_only", True),
            hybrid_min_ready_score=active.number("hybrid_gate.min_ready_score", 75.0),
            hybrid_min_small_entry_score=active.number("hybrid_gate.min_small_entry_score", 65.0),
            hybrid_theme_weight=active.number("hybrid_gate.weights.dynamic_theme", 0.30),
            hybrid_leadership_weight=active.number("hybrid_gate.weights.stock_leadership", 0.20),
            hybrid_entry_weight=active.number("hybrid_gate.weights.entry_timing", 0.25),
            hybrid_market_weight=active.number("hybrid_gate.weights.market_session", 0.15),
            hybrid_risk_weight=active.number("hybrid_gate.weights.risk_liquidity", 0.10),
            watch_theme_allows_small_entry=_bool_setting(active, "hybrid_gate.watch_theme_allows_small_entry", False),
            leader_only_blocks_laggards=_bool_setting(active, "hybrid_gate.leader_only_blocks_laggards", True),
            min_membership_score=active.number("hybrid_gate.min_membership_score", 0.55),
            min_theme_breadth=active.number("hybrid_gate.min_theme_breadth", 0.35),
            max_rank_in_theme_for_ready=active.integer("hybrid_gate.max_rank_in_theme_for_ready", 5),
        )

    def normalized_weights(self) -> dict[str, float]:
        weights = {
            "dynamic_theme": max(0.0, float(self.hybrid_theme_weight)),
            "stock_leadership": max(0.0, float(self.hybrid_leadership_weight)),
            "entry_timing": max(0.0, float(self.hybrid_entry_weight)),
            "market": max(0.0, float(self.hybrid_market_weight)),
            "risk": max(0.0, float(self.hybrid_risk_weight)),
        }
        total = sum(weights.values()) or 1.0
        return {key: value / total for key, value in weights.items()}


@dataclass
class HybridGateDecision:
    status: HybridGateStatus | str
    score: float
    position_tier: HybridPositionTier | str
    primary_reason: str = ""
    reason_codes: list[str] = field(default_factory=list)
    dynamic_theme_component: HybridGateComponent = field(default_factory=lambda: HybridGateComponent("dynamic_theme"))
    stock_leadership_component: HybridGateComponent = field(default_factory=lambda: HybridGateComponent("stock_leadership"))
    entry_timing_component: HybridGateComponent = field(default_factory=lambda: HybridGateComponent("entry_timing"))
    market_component: HybridGateComponent = field(default_factory=lambda: HybridGateComponent("market_session"))
    risk_component: HybridGateComponent = field(default_factory=lambda: HybridGateComponent("risk_liquidity"))
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = _value(self.status)
        position_tier = _value(self.position_tier)
        reason_codes = normalize_reason_codes(self.reason_codes)
        return {
            "status": status,
            "score": round(_clamp(self.score), 4),
            "position_tier": position_tier,
            "primary_reason": self.primary_reason or (reason_codes[0] if reason_codes else status),
            "reason_codes": reason_codes,
            "dynamic_theme_component": self.dynamic_theme_component.to_dict(),
            "stock_leadership_component": self.stock_leadership_component.to_dict(),
            "entry_timing_component": self.entry_timing_component.to_dict(),
            "market_component": self.market_component.to_dict(),
            "risk_component": self.risk_component.to_dict(),
            "details": dict(self.details),
        }


class HybridDynamicThemeGate:
    def __init__(self, config: Optional[HybridGateConfig] = None) -> None:
        self.config = config or HybridGateConfig()

    def evaluate(
        self,
        *,
        candidate: Candidate,
        theme_context: ThemeContext,
        theme_result: ThemeStrengthResult,
        leadership_result: StockLeadershipResult,
        market_decision: GateDecision,
        theme_strength_decision: GateDecision,
        theme_pullback_decision: GateDecision,
        leadership_decision: GateDecision,
        stock_pullback_decision: GateDecision,
    ) -> HybridGateDecision:
        activity = theme_context.activity
        dynamic_theme = _dynamic_theme_component(theme_context, theme_result, theme_strength_decision)
        leadership = _stock_leadership_component(theme_context, leadership_result, leadership_decision)
        entry = _entry_timing_component(stock_pullback_decision)
        market = _market_component(market_decision)
        risk = _risk_component(stock_pullback_decision, market_decision)
        score = _weighted_score(self.config, dynamic_theme, leadership, entry, market, risk)

        reason_codes = normalize_reason_codes(
            list(dynamic_theme.reason_codes)
            + list(leadership.reason_codes)
            + list(entry.reason_codes)
            + list(market.reason_codes)
            + list(risk.reason_codes)
        )
        hard_guard_codes = _hard_guard_codes(market_decision, stock_pullback_decision)
        reason_codes = normalize_reason_codes(reason_codes + hard_guard_codes)
        status, tier, decision_codes = self._status_and_tier(
            theme_context=theme_context,
            activity=activity,
            score=score,
            dynamic_theme=dynamic_theme,
            leadership=leadership,
            entry=entry,
            risk=risk,
            hard_guard_codes=hard_guard_codes,
        )
        reason_codes = normalize_reason_codes(decision_codes + reason_codes)
        details = _flat_details(
            candidate=candidate,
            theme_context=theme_context,
            activity=activity,
            leadership_result=leadership_result,
            stock_pullback_decision=stock_pullback_decision,
            config=self.config,
            hard_guard_codes=hard_guard_codes,
        )
        details["component_weights"] = self.config.normalized_weights()
        details["legacy_gate_snapshot"] = {
            "market": _decision_digest(market_decision),
            "theme_strength": _decision_digest(theme_strength_decision),
            "theme_pullback": _decision_digest(theme_pullback_decision),
            "stock_leadership": _decision_digest(leadership_decision),
            "stock_pullback": _decision_digest(stock_pullback_decision),
        }
        return HybridGateDecision(
            status=status,
            score=score,
            position_tier=tier,
            primary_reason=reason_codes[0] if reason_codes else _value(status),
            reason_codes=reason_codes,
            dynamic_theme_component=dynamic_theme,
            stock_leadership_component=leadership,
            entry_timing_component=entry,
            market_component=market,
            risk_component=risk,
            details=details,
        )

    def _status_and_tier(
        self,
        *,
        theme_context: ThemeContext,
        activity: Optional[ThemeActivitySnapshot],
        score: float,
        dynamic_theme: HybridGateComponent,
        leadership: HybridGateComponent,
        entry: HybridGateComponent,
        risk: HybridGateComponent,
        hard_guard_codes: list[str],
    ) -> tuple[HybridGateStatus, HybridPositionTier, list[str]]:
        config = self.config
        theme_status = _value(theme_context.status).upper()
        role = str(leadership.details.get("leader_type") or "")
        rank_in_theme = int(leadership.details.get("rank_in_theme") or 0)
        membership_score = float(leadership.details.get("membership_score") or 0.0)
        relation_type = str(leadership.details.get("relation_type") or "")
        breadth = float(dynamic_theme.details.get("breadth") or 0.0)
        theme_reason_codes = set(dynamic_theme.reason_codes)
        chase_risk = str(entry.details.get("chase_risk") or "").lower() == "true" or "CHASE_RISK" in entry.reason_codes
        good_entry = entry.score >= config.hybrid_min_small_entry_score and not chase_risk
        leader_or_co = role in {"leader", "co_leader"}

        if hard_guard_codes:
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, list(hard_guard_codes)
        if theme_status == ThemeStatus.STALE.value:
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, ["THEME_STALE"]
        if membership_score < config.min_membership_score:
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, ["LOW_MEMBERSHIP_SCORE"]
        if relation_type in {"rumor", "unknown"} or not bool(leadership.details.get("trade_eligible")):
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, ["THEME_MEMBER_NOT_TRADE_ELIGIBLE"]
        if config.leader_only_blocks_laggards and "LEADER_ONLY_THEME" in theme_reason_codes and not leader_or_co:
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, ["LEADER_ONLY_THEME_LAGGARD_BLOCK"]
        if rank_in_theme >= 6 or role == "late_laggard":
            return HybridGateStatus.BLOCKED, HybridPositionTier.BLOCKED, ["LATE_LAGGARD"]
        if theme_status == ThemeStatus.CANDIDATE.value:
            return HybridGateStatus.OBSERVE, HybridPositionTier.OBSERVE_ONLY, ["CANDIDATE_THEME_OBSERVE_ONLY"]
        if chase_risk:
            return HybridGateStatus.WAIT, HybridPositionTier.NONE, ["CHASE_RISK"]
        if breadth < config.min_theme_breadth or "LOW_BREADTH" in theme_reason_codes:
            return HybridGateStatus.WAIT, HybridPositionTier.NONE, ["LOW_BREADTH"]
        if dynamic_theme.score < 55.0 and entry.score >= config.hybrid_min_ready_score:
            return HybridGateStatus.WAIT, HybridPositionTier.NONE, ["WEAK_THEME_STRONG_ENTRY_WAIT"]
        if dynamic_theme.score >= config.hybrid_min_ready_score and entry.score < config.hybrid_min_small_entry_score:
            return HybridGateStatus.WAIT, HybridPositionTier.NONE, ["STRONG_THEME_ENTRY_NOT_READY"]

        if theme_status == ThemeStatus.ACTIVE.value:
            if score >= config.hybrid_min_ready_score and good_entry:
                return HybridGateStatus.READY, HybridPositionTier.NORMAL_FIRST_ENTRY, ["STRONG_ACTIVE_THEME"]
            if score >= config.hybrid_min_small_entry_score:
                return HybridGateStatus.WAIT, HybridPositionTier.NONE, ["HYBRID_SCORE_WAIT"]
            return HybridGateStatus.OBSERVE, HybridPositionTier.OBSERVE_ONLY, ["HYBRID_SCORE_LOW"]

        if theme_status == ThemeStatus.WATCH.value:
            if leader_or_co and good_entry and score >= config.hybrid_min_small_entry_score:
                if config.watch_theme_allows_small_entry:
                    return HybridGateStatus.READY, HybridPositionTier.SMALL_FIRST_ENTRY, ["WATCH_THEME_SMALL_ENTRY"]
                return HybridGateStatus.OBSERVE, HybridPositionTier.OBSERVE_ONLY, ["WATCH_THEME_OBSERVE_ONLY"]
            return HybridGateStatus.OBSERVE, HybridPositionTier.OBSERVE_ONLY, ["WATCH_THEME_OBSERVE_ONLY"]

        return HybridGateStatus.OBSERVE, HybridPositionTier.OBSERVE_ONLY, ["THEME_CONTEXT_OBSERVE_ONLY"]


def hybrid_decision_flat_fields(decision: HybridGateDecision) -> dict[str, Any]:
    payload = decision.to_dict()
    details = dict(payload.get("details") or {})
    return {
        "hybrid_status": payload["status"],
        "hybrid_score": payload["score"],
        "hybrid_position_tier": payload["position_tier"],
        "hybrid_primary_reason": payload["primary_reason"],
        "hybrid_reason_codes": list(payload["reason_codes"]),
        "dynamic_theme_id": details.get("dynamic_theme_id", ""),
        "dynamic_theme_name": details.get("dynamic_theme_name", ""),
        "dynamic_theme_status": details.get("dynamic_theme_status", ""),
        "dynamic_theme_score": details.get("dynamic_theme_score", 0.0),
        "dynamic_theme_rank": details.get("dynamic_theme_rank", 0),
        "theme_breadth": details.get("theme_breadth", 0.0),
        "leader_gap": details.get("leader_gap", 0.0),
        "top3_concentration": details.get("top3_concentration", 0.0),
        "rank_in_theme": details.get("rank_in_theme", 0),
        "membership_score": details.get("membership_score", 0.0),
        "entry_timing_score": details.get("entry_timing_score", 0.0),
        "chase_risk": details.get("chase_risk", False),
        "hybrid_observe_only": details.get("hybrid_observe_only", True),
    }


def summarize_hybrid_gate_reviews(reviews: Iterable[Any]) -> dict[str, Any]:
    items = [review for review in reviews if _review_details(review).get("hybrid_status")]
    status_counts = _counter(items, lambda review: _review_details(review).get("hybrid_status"))
    reason_counts = _reason_counter(items)
    leader_only_blocked = [
        getattr(review, "code", "")
        for review in items
        if _review_details(review).get("hybrid_status") == HybridGateStatus.BLOCKED.value
        and "LEADER_ONLY_THEME_LAGGARD_BLOCK" in (_review_details(review).get("hybrid_reason_codes") or [])
    ]
    watch_small_entry = [
        getattr(review, "code", "")
        for review in items
        if _review_details(review).get("hybrid_position_tier") == HybridPositionTier.SMALL_FIRST_ENTRY.value
    ]
    return {
        "candidate_count": len(items),
        "status_counts": status_counts,
        "ready_but_legacy_not_bought": [
            getattr(review, "code", "")
            for review in items
            if _review_details(review).get("hybrid_status") == HybridGateStatus.READY.value
            and not _review_ready(review)
        ],
        "legacy_ready_but_hybrid_blocked": [
            getattr(review, "code", "")
            for review in items
            if _review_ready(review)
            and _review_details(review).get("hybrid_status") == HybridGateStatus.BLOCKED.value
        ],
        "wait_reason_top": reason_counts.get(HybridGateStatus.WAIT.value, []),
        "leader_only_blocked": leader_only_blocked,
        "watch_small_entry_candidates": watch_small_entry,
        "theme_score_buckets": _bucket_counts(items, "dynamic_theme_score"),
        "membership_score_buckets": _bucket_counts(items, "membership_score", scale=1.0),
    }


def _dynamic_theme_component(
    theme_context: ThemeContext,
    theme_result: ThemeStrengthResult,
    theme_strength_decision: GateDecision,
) -> HybridGateComponent:
    activity = theme_context.activity
    score = activity.theme_score if activity else theme_result.score
    details = {
        "theme_id": theme_context.theme_id,
        "theme_name": theme_context.theme_name or theme_result.theme_name,
        "theme_status": _value(theme_context.status),
        "theme_rank": theme_context.rank,
        "theme_grade": theme_result.grade,
        "breadth": activity.breadth if activity else theme_result.details.get("rising_ratio", 0.0),
        "rising_count": activity.rising_count if activity else 0,
        "leader_gap": activity.leader_gap if activity else 0.0,
        "top3_concentration": activity.top3_concentration if activity else 0.0,
    }
    reason_codes = list(theme_strength_decision.reason_codes)
    if activity:
        reason_codes.extend(activity.details.get("reason_codes") or [])
        if activity.breadth < 0.35:
            reason_codes.append("LOW_BREADTH")
    if _value(theme_context.status).upper() == ThemeStatus.STALE.value:
        reason_codes.append("THEME_STALE")
    return HybridGateComponent(
        name="dynamic_theme",
        score=_clamp(score),
        status=_value(theme_context.status),
        reason_codes=reason_codes,
        details=details,
    )


def _stock_leadership_component(
    theme_context: ThemeContext,
    leadership_result: StockLeadershipResult,
    leadership_decision: GateDecision,
) -> HybridGateComponent:
    relation_type = _value(theme_context.relation_type)
    details = {
        "rank_in_theme": theme_context.rank_in_theme or leadership_result.leadership_rank,
        "leader_type": leadership_result.leadership_role,
        "membership_score": theme_context.membership_score,
        "relation_type": relation_type,
        "source_count": theme_context.source_count,
        "trade_eligible": theme_context.trade_eligible,
    }
    reason_codes = list(leadership_decision.reason_codes)
    reason_codes.extend(leadership_result.details.get("comparison_reason_codes") or [])
    if theme_context.membership_score < 0.55:
        reason_codes.append("LOW_MEMBERSHIP_SCORE")
    if relation_type in {"rumor", "unknown"} or not theme_context.trade_eligible:
        reason_codes.append("THEME_MEMBER_NOT_TRADE_ELIGIBLE")
    if int(details["rank_in_theme"] or 0) >= 6 or leadership_result.leadership_role == "late_laggard":
        reason_codes.append("LATE_LAGGARD")
    return HybridGateComponent(
        name="stock_leadership",
        score=_clamp(leadership_result.score),
        status=leadership_result.leadership_role,
        reason_codes=reason_codes,
        details=details,
    )


def _entry_timing_component(stock_pullback_decision: GateDecision) -> HybridGateComponent:
    details = dict(stock_pullback_decision.details or {})
    chase_risk = bool(details.get("chase_risk")) or details.get("late_chase_level") == "soft_block"
    reason_codes = list(stock_pullback_decision.reason_codes)
    if chase_risk:
        reason_codes.append("CHASE_RISK")
    return HybridGateComponent(
        name="entry_timing",
        score=_clamp(stock_pullback_decision.score),
        status=str(details.get("sub_status") or ("PASS" if stock_pullback_decision.passed else "WAIT")),
        reason_codes=reason_codes,
        details={
            "pullback_status": details.get("sub_status", ""),
            "support_distance_pct": details.get("support_distance_pct"),
            "support_touched": bool(details.get("support_touched")),
            "support_reclaimed": bool(details.get("support_reclaimed")),
            "volume_reaccel": bool(details.get("volume_reaccel")),
            "failed_low_break_rebound": bool(details.get("failed_low_break_rebound")),
            "chase_risk": chase_risk,
            "late_chase_level": details.get("late_chase_level", ""),
        },
    )


def _market_component(market_decision: GateDecision) -> HybridGateComponent:
    return HybridGateComponent(
        name="market_session",
        score=_clamp(market_decision.score),
        status=str(market_decision.details.get("sub_status") or ("PASS" if market_decision.passed else "WAIT")),
        reason_codes=list(market_decision.reason_codes),
        details=dict(market_decision.details or {}),
    )


def _risk_component(stock_pullback_decision: GateDecision, market_decision: GateDecision) -> HybridGateComponent:
    reason_codes: list[str] = []
    risk_penalty = 0.0
    if stock_pullback_decision.block_type == BlockType.FINAL:
        risk_penalty += 100.0
        reason_codes.extend(stock_pullback_decision.reason_codes)
    if market_decision.block_type == BlockType.FINAL:
        risk_penalty += 100.0
        reason_codes.extend(market_decision.reason_codes)
    if bool(stock_pullback_decision.details.get("chase_risk")):
        risk_penalty += 100.0
        reason_codes.append("CHASE_RISK")
    late_chase_score = stock_pullback_decision.details.get("late_chase_score")
    try:
        risk_penalty += min(30.0, max(0.0, float(late_chase_score or 0.0) * 0.3))
    except (TypeError, ValueError):
        pass
    return HybridGateComponent(
        name="risk_liquidity",
        score=_clamp(100.0 - risk_penalty),
        status="PASS" if risk_penalty <= 0 else "RISK",
        reason_codes=reason_codes,
        details={
            "stock_block_type": _value(stock_pullback_decision.block_type),
            "market_block_type": _value(market_decision.block_type),
            "late_chase_score": stock_pullback_decision.details.get("late_chase_score"),
            "chase_risk": bool(stock_pullback_decision.details.get("chase_risk")),
        },
    )


def _weighted_score(
    config: HybridGateConfig,
    dynamic_theme: HybridGateComponent,
    leadership: HybridGateComponent,
    entry: HybridGateComponent,
    market: HybridGateComponent,
    risk: HybridGateComponent,
) -> float:
    weights = config.normalized_weights()
    score = (
        dynamic_theme.score * weights["dynamic_theme"]
        + leadership.score * weights["stock_leadership"]
        + entry.score * weights["entry_timing"]
        + market.score * weights["market"]
        + risk.score * weights["risk"]
    )
    if "LOW_BREADTH" in dynamic_theme.reason_codes:
        score -= 8.0
    if "LATE_LAGGARD" in leadership.reason_codes:
        score -= 12.0
    if "THEME_MEMBER_NOT_TRADE_ELIGIBLE" in leadership.reason_codes:
        score -= 20.0
    return round(_clamp(score), 4)


def _hard_guard_codes(market_decision: GateDecision, stock_pullback_decision: GateDecision) -> list[str]:
    codes: list[str] = []
    if market_decision.block_type == BlockType.FINAL:
        codes.extend(market_decision.reason_codes or ["MARKET_HARD_GUARD"])
    stock_codes = normalize_reason_codes(stock_pullback_decision.reason_codes)
    stock_is_chase_only = bool(stock_codes) and set(stock_codes) <= {"CHASE_RISK"}
    if stock_pullback_decision.block_type == BlockType.FINAL and not stock_is_chase_only:
        codes.extend(stock_pullback_decision.reason_codes or ["ENTRY_HARD_GUARD"])
    return normalize_reason_codes(codes)


def _flat_details(
    *,
    candidate: Candidate,
    theme_context: ThemeContext,
    activity: Optional[ThemeActivitySnapshot],
    leadership_result: StockLeadershipResult,
    stock_pullback_decision: GateDecision,
    config: HybridGateConfig,
    hard_guard_codes: list[str],
) -> dict[str, Any]:
    stock_details = dict(stock_pullback_decision.details or {})
    return {
        "candidate_id": candidate.id,
        "code": candidate.code,
        "dynamic_theme_id": theme_context.theme_id,
        "dynamic_theme_name": theme_context.theme_name,
        "dynamic_theme_status": _value(theme_context.status),
        "dynamic_theme_score": activity.theme_score if activity else 0.0,
        "dynamic_theme_rank": theme_context.rank,
        "theme_breadth": activity.breadth if activity else 0.0,
        "leader_gap": activity.leader_gap if activity else 0.0,
        "top3_concentration": activity.top3_concentration if activity else 0.0,
        "rank_in_theme": theme_context.rank_in_theme or leadership_result.leadership_rank,
        "leader_type": leadership_result.leadership_role,
        "membership_score": theme_context.membership_score,
        "relation_type": _value(theme_context.relation_type),
        "source_count": theme_context.source_count,
        "trade_eligible": theme_context.trade_eligible,
        "entry_timing_score": stock_pullback_decision.score,
        "chase_risk": bool(stock_details.get("chase_risk")) or stock_details.get("late_chase_level") == "soft_block",
        "late_chase_level": stock_details.get("late_chase_level", ""),
        "hard_guard_codes": list(hard_guard_codes),
        "hybrid_observe_only": config.hybrid_gate_observe_only,
        "hybrid_gate_enabled": config.hybrid_gate_enabled,
    }


def _decision_digest(decision: GateDecision) -> dict[str, Any]:
    return {
        "gate_name": decision.gate_name,
        "passed": decision.passed,
        "score": decision.score,
        "block_type": _value(decision.block_type),
        "reason_codes": list(decision.reason_codes),
        "sub_status": decision.details.get("sub_status", ""),
    }


def _bool_setting(settings: StrategyRuntimeSettings, path: str, default: bool) -> bool:
    value = settings.value(path, default)
    return value if type(value) is bool else bool(default)


def _value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(minimum, min(maximum, number))


def _review_details(review: Any) -> dict[str, Any]:
    details = getattr(review, "details", {}) or {}
    return dict(details) if isinstance(details, dict) else {}


def _review_ready(review: Any) -> bool:
    status = str(getattr(review, "final_status", "") or "").upper()
    return status.startswith("VIRTUAL_") and status not in {"VIRTUAL_UNFILLED", "VIRTUAL_CANCELLED"}


def _counter(items: list[Any], key_fn) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        key = str(key_fn(item) or "")
        if key:
            result[key] = result.get(key, 0) + 1
    return result


def _reason_counter(items: list[Any]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, dict[str, int]] = {}
    for item in items:
        details = _review_details(item)
        status = str(details.get("hybrid_status") or "")
        if not status:
            continue
        bucket = buckets.setdefault(status, {})
        for code in details.get("hybrid_reason_codes") or []:
            text = str(code or "")
            if text:
                bucket[text] = bucket.get(text, 0) + 1
    return {
        status: [
            {"reason_code": reason, "count": count}
            for reason, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
        ]
        for status, values in buckets.items()
    }


def _bucket_counts(items: list[Any], field: str, *, scale: float = 100.0) -> dict[str, int]:
    buckets = {"0-39": 0, "40-64": 0, "65-74": 0, "75+": 0}
    for item in items:
        raw = _review_details(item).get(field)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if scale == 1.0:
            value *= 100.0
        if value >= 75:
            buckets["75+"] += 1
        elif value >= 65:
            buckets["65-74"] += 1
        elif value >= 40:
            buckets["40-64"] += 1
        else:
            buckets["0-39"] += 1
    return buckets
