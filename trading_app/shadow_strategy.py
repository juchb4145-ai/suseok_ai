from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase


DEFAULT_POLICY_IDS = (
    "relaxed_risk_off_leader",
    "strict_late_chase",
    "strict_entry_risk",
    "relaxed_data_wait_for_leader",
    "fast_theme_exit_shadow",
)


@dataclass(frozen=True)
class ShadowPolicyRule:
    key: str
    value: Any
    operator: str = "eq"


@dataclass(frozen=True)
class ShadowPolicy:
    policy_id: str
    policy_name: str
    label_ko: str
    description: str
    enabled: bool = True
    observe_only: bool = True
    category: str = "gate"
    priority: int = 100
    rules: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShadowEvaluation:
    evaluation_id: str
    decision_id: str
    policy_id: str
    changed_decision: bool
    change_type: str


@dataclass(frozen=True)
class ShadowComparisonResult:
    baseline_gate_status: str
    shadow_gate_status: str
    baseline_action_type: str
    shadow_action_type: str
    changed_decision: bool
    change_type: str


@dataclass(frozen=True)
class ShadowStrategyConfig:
    enabled: bool = True
    policy_ids: tuple[str, ...] = DEFAULT_POLICY_IDS
    max_batch_size: int = 500
    runtime_hook_enabled: bool = True
    rebuild_limit: int = 10000
    min_theme_score: float = 70.0
    min_hybrid_score: float = 65.0
    ready_small_multiplier: float = 0.3
    observe_only: bool = True
    allow_apply: bool = False


class ShadowStrategyEvaluator:
    def __init__(self, db: TradingDatabase, *, config: Optional[ShadowStrategyConfig] = None) -> None:
        self.db = db
        self.config = config or ShadowStrategyConfig()

    def load_policies(self, policy_id: Optional[str] = None, *, include_baseline: bool = False) -> list[ShadowPolicy]:
        policies = default_shadow_policies(self.config)
        enabled_ids = set(self.config.policy_ids or DEFAULT_POLICY_IDS)
        result = []
        for policy in policies:
            if policy.policy_id == "baseline":
                if include_baseline and (policy_id in (None, "", "baseline")):
                    result.append(policy)
                continue
            if policy.policy_id not in enabled_ids:
                continue
            if policy_id and policy.policy_id != policy_id:
                continue
            if policy.enabled:
                result.append(policy)
        return sorted(result, key=lambda item: item.priority)

    def evaluate_decision(self, decision_event: dict[str, Any], policy: ShadowPolicy) -> dict[str, Any]:
        decision = dict(decision_event or {})
        baseline_gate = str(decision.get("gate_status") or "").upper()
        baseline_action = str(decision.get("action_type") or "").upper()
        reason_codes = _reason_codes(decision)
        shadow_gate = baseline_gate
        shadow_action = baseline_action
        shadow_reasons = list(reason_codes)
        shadow_score = _baseline_score(decision)
        shadow_multiplier = _baseline_multiplier(decision)
        expected_effect = "no_change"
        expected_risk = "none"
        details: dict[str, Any] = {
            "policy": policy.to_dict(),
            "observe_only": True,
            "allow_apply": False,
            "baseline_decision_id": decision.get("decision_id"),
            "matched_conditions": [],
        }

        if policy.policy_id == "relaxed_risk_off_leader" and self._matches_relaxed_risk_off_leader(decision, reason_codes):
            shadow_gate = "OBSERVE_READY" if self.config.observe_only else "READY_SMALL"
            shadow_action = "SHADOW_ENTRY_CANDIDATE"
            shadow_reasons = _append_reason(reason_codes, "SHADOW_RELAXED_RISK_OFF_LEADER")
            shadow_multiplier = self.config.ready_small_multiplier
            shadow_score = max(shadow_score, self.config.min_hybrid_score)
            expected_effect = "opportunity_loss_reduction"
            expected_risk = "false_positive_possible"
            details["matched_conditions"].append("leader_market_risk_relaxation")
        elif policy.policy_id == "strict_late_chase" and self._matches_strict_late_chase(decision, reason_codes):
            shadow_gate = "BLOCKED"
            shadow_action = "BLOCK"
            shadow_reasons = _append_reason(reason_codes, "SHADOW_STRICT_LATE_CHASE")
            shadow_multiplier = 0.0
            expected_effect = "false_positive_reduction"
            expected_risk = "opportunity_loss_possible"
            details["matched_conditions"].append("late_chase_block")
        elif policy.policy_id == "strict_entry_risk" and self._matches_strict_entry_risk(decision, reason_codes):
            shadow_gate = "BLOCKED"
            shadow_action = "BLOCK"
            shadow_reasons = _append_reason(reason_codes, "SHADOW_STRICT_ENTRY_RISK")
            shadow_multiplier = 0.0
            expected_effect = "risk_block_effective"
            expected_risk = "opportunity_loss_possible"
            details["matched_conditions"].append("entry_risk_block")
        elif policy.policy_id == "relaxed_data_wait_for_leader" and self._matches_relaxed_data_wait(decision, reason_codes):
            shadow_gate = "OBSERVE_READY"
            shadow_action = "SHADOW_ENTRY_CANDIDATE"
            shadow_reasons = _append_reason(reason_codes, "SHADOW_RELAXED_DATA_WAIT_LEADER")
            shadow_multiplier = self.config.ready_small_multiplier
            expected_effect = "data_wait_opportunity_check"
            expected_risk = "data_quality_false_positive_possible"
            details["matched_conditions"].append("leader_data_wait_relaxation")
        elif policy.policy_id == "fast_theme_exit_shadow" and self._matches_fast_theme_exit(decision):
            shadow_gate = baseline_gate or "EXIT"
            shadow_action = "SHADOW_EXIT"
            shadow_reasons = _append_reason(reason_codes, "SHADOW_FAST_THEME_EXIT")
            shadow_multiplier = 0.0
            expected_effect = "reduce_giveback"
            expected_risk = "exit_too_early_possible"
            details["matched_conditions"].append("fast_theme_exit")

        comparison = self.compare_with_baseline(
            decision,
            shadow_gate_status=shadow_gate,
            shadow_action_type=shadow_action,
        )
        now = datetime.now().isoformat(timespec="seconds")
        details["comparison"] = asdict(comparison)
        return {
            "evaluation_id": f"shadow:{decision.get('decision_id') or ''}:{policy.policy_id}",
            "trade_date": decision.get("trade_date") or "",
            "evaluated_at": now,
            "runtime_cycle_id": decision.get("runtime_cycle_id") or "",
            "decision_id": decision.get("decision_id") or "",
            "policy_id": policy.policy_id,
            "policy_name": policy.policy_name,
            "candidate_id": decision.get("candidate_id"),
            "candidate_instance_id": decision.get("candidate_instance_id") or "",
            "candidate_generation_seq": decision.get("candidate_generation_seq") or 0,
            "code": decision.get("code") or "",
            "name": decision.get("name") or "",
            "theme_name": decision.get("theme_name") or "",
            "baseline_gate_status": baseline_gate,
            "baseline_action_type": baseline_action,
            "baseline_reason_codes": reason_codes,
            "shadow_gate_status": shadow_gate,
            "shadow_action_type": shadow_action,
            "shadow_reason_codes": shadow_reasons,
            "baseline_score": _baseline_score(decision),
            "shadow_score": shadow_score,
            "baseline_position_size_multiplier": _baseline_multiplier(decision),
            "shadow_position_size_multiplier": shadow_multiplier,
            "changed_decision": comparison.changed_decision,
            "change_type": comparison.change_type,
            "expected_effect": expected_effect,
            "expected_risk": expected_risk,
            "data_status": decision.get("data_status") or "",
            "data_quality_issues": decision.get("data_quality_issues") or [],
            "details": details,
        }

    def evaluate_batch(self, decisions: Iterable[dict[str, Any]], policies: Iterable[ShadowPolicy]) -> list[dict[str, Any]]:
        evaluations: list[dict[str, Any]] = []
        for decision in decisions or []:
            for policy in policies or []:
                if policy.policy_id == "baseline":
                    continue
                evaluations.append(self.evaluate_decision(decision, policy))
        return evaluations

    def compare_with_baseline(
        self,
        decision_event: dict[str, Any],
        *,
        shadow_gate_status: str,
        shadow_action_type: str,
    ) -> ShadowComparisonResult:
        baseline_gate = str(decision_event.get("gate_status") or "").upper()
        baseline_action = str(decision_event.get("action_type") or "").upper()
        shadow_gate = str(shadow_gate_status or "").upper()
        shadow_action = str(shadow_action_type or "").upper()
        changed = baseline_gate != shadow_gate or baseline_action != shadow_action
        change_type = "NO_CHANGE"
        if changed:
            if _ready_like(shadow_gate) and baseline_gate == "BLOCKED":
                change_type = "BLOCK_TO_READY"
            elif _ready_like(shadow_gate) and baseline_gate in {"WAIT", "OBSERVE", ""}:
                change_type = "WAIT_TO_READY"
            elif _ready_like(baseline_gate) and shadow_gate in {"BLOCKED", "WAIT"}:
                change_type = "READY_TO_BLOCK"
            elif baseline_action == "HOLD" and shadow_action == "SHADOW_EXIT":
                change_type = "HOLD_TO_EXIT"
            elif baseline_action == "EXIT_DECISION" and shadow_action == "HOLD":
                change_type = "EXIT_TO_HOLD"
        return ShadowComparisonResult(
            baseline_gate_status=baseline_gate,
            shadow_gate_status=shadow_gate,
            baseline_action_type=baseline_action,
            shadow_action_type=shadow_action,
            changed_decision=changed,
            change_type=change_type,
        )

    def persist_evaluations(self, evaluations: Iterable[dict[str, Any]], *, force: bool = False) -> int:
        return self.db.save_shadow_strategy_evaluations(list(evaluations or []), force=force)

    def build_summary(
        self,
        *,
        trade_date: Optional[str] = None,
        window_sec: Optional[int] = None,
        horizon_sec: Optional[int] = None,
        policy_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.db.shadow_strategy_summary(
            trade_date=trade_date,
            window_sec=window_sec,
            horizon_sec=horizon_sec,
            policy_id=policy_id,
        )

    def rebuild(
        self,
        *,
        trade_date: Optional[str] = None,
        policy_id: Optional[str] = None,
        force: bool = False,
        limit: Optional[int] = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "DISABLED", "evaluated_count": 0, "persisted_count": 0, "items": []}
        policies = self.load_policies(policy_id=policy_id)
        policy_ids = [policy.policy_id for policy in policies]
        decisions = self.db.list_strategy_decision_events_due_for_shadow(
            policy_ids=policy_ids,
            trade_date=trade_date,
            limit=max(1, int(limit or self.config.rebuild_limit)),
            force=force,
        )
        evaluations = self.evaluate_batch(decisions, policies)
        persisted_count = self.persist_evaluations(evaluations, force=force) if persist else 0
        return {
            "status": "OK",
            "trade_date": trade_date or "",
            "policy_id": policy_id or "",
            "force": bool(force),
            "persist": bool(persist),
            "decision_count": len(decisions),
            "evaluated_count": len(evaluations),
            "persisted_count": persisted_count,
            "items": evaluations[:100],
            "disclaimer_ko": "Shadow 결과는 장중 진단용이며 실제 전략 설정에 자동 적용되지 않습니다.",
        }

    def _matches_relaxed_risk_off_leader(self, decision: dict[str, Any], reason_codes: list[str]) -> bool:
        if str(decision.get("gate_status") or "").upper() not in {"WAIT", "BLOCKED", "OBSERVE"}:
            return False
        if not _has_any(reason_codes, ("RISK_OFF", "MARKET_WEAK", "WAIT_CANDIDATE_MARKET_RISK_OFF", "WAIT_MARKET_CONFIRMATION_PENDING")):
            return False
        return (
            _is_leader(decision)
            and _float(decision.get("theme_score")) >= self.config.min_theme_score
            and max(_float(decision.get("hybrid_score")), _float(decision.get("gate_score"))) >= self.config.min_hybrid_score
            and _has_market_support(decision)
        )

    def _matches_strict_late_chase(self, decision: dict[str, Any], reason_codes: list[str]) -> bool:
        if not (_ready_like(decision.get("gate_status")) or str(decision.get("action_type") or "").upper() in {"READY", "ENTRY_PLAN", "ENTRY_ORDER_INTENT"}):
            return False
        return _has_any(reason_codes, ("LATE_CHASE", "LATE_LAGGARD", "CHASE_RISK", "HIGH_RETURN")) or (
            _detail_bool(decision, "near_session_high") and _float(_detail_value(decision, "pullback_from_high_pct")) < 1.0
        )

    def _matches_strict_entry_risk(self, decision: dict[str, Any], reason_codes: list[str]) -> bool:
        if not (_ready_like(decision.get("gate_status")) or str(decision.get("action_type") or "").upper() in {"READY", "ENTRY_PLAN", "ENTRY_ORDER_INTENT"}):
            return False
        if _has_any(reason_codes, ("VI_ACTIVE", "VI_COOLDOWN", "VI_UNKNOWN_LIMIT_RISK", "UPPER_LIMIT_NEAR", "UPPER_LIMIT_HARD_NEAR")):
            return True
        upper_gap = _float(_detail_value(decision, "upper_limit_gap_pct"))
        return upper_gap > 0 and upper_gap <= 2.0 and _float(decision.get("change_rate")) >= 20.0

    def _matches_relaxed_data_wait(self, decision: dict[str, Any], reason_codes: list[str]) -> bool:
        if str(decision.get("gate_status") or "").upper() not in {"WAIT", "BLOCKED", "OBSERVE"}:
            return False
        if not _has_any(reason_codes, ("DATA_INSUFFICIENT", "WAIT_DATA_SUPPORT_NOT_READY", "WAIT_PRICE_LOCATION_UNKNOWN")):
            return False
        return _is_leader(decision) and _float(decision.get("theme_score")) >= self.config.min_theme_score and _has_market_support(decision)

    def _matches_fast_theme_exit(self, decision: dict[str, Any]) -> bool:
        if str(decision.get("action_type") or "").upper() != "HOLD":
            return False
        current_return = _float(_detail_value(decision, "current_return_pct"))
        max_return = max(_float(_detail_value(decision, "max_return_pct")), _float(_detail_value(decision, "position_max_return_pct")))
        giveback = current_return - max_return
        leader_delta = _float(_detail_value(decision, "leader_count_delta"))
        theme_score_delta = _float(_detail_value(decision, "theme_score_delta"))
        below_vwap = _positive_float(decision.get("price")) > 0 and _positive_float(decision.get("vwap")) > 0 and _float(decision.get("price")) < _float(decision.get("vwap"))
        momentum_weak = _float(decision.get("momentum_1m")) < 0 or _float(decision.get("momentum_3m")) < 0
        return giveback <= -2.0 or leader_delta < 0 or theme_score_delta <= -5.0 or (below_vwap and momentum_weak)


def default_shadow_policies(config: Optional[ShadowStrategyConfig] = None) -> list[ShadowPolicy]:
    config = config or ShadowStrategyConfig()
    now = "builtin"
    observe_only = bool(config.observe_only)
    return [
        ShadowPolicy(
            policy_id="baseline",
            policy_name="baseline",
            label_ko="현행 baseline",
            description="현재 runtime/gate decision을 비교 기준으로 사용합니다.",
            enabled=True,
            observe_only=True,
            category="gate",
            priority=0,
            rules=[],
            created_at=now,
            updated_at=now,
        ),
        ShadowPolicy(
            policy_id="relaxed_risk_off_leader",
            policy_name="relaxed_risk_off_leader",
            label_ko="Risk-off leader 완화",
            description="시장 약세 gate에서도 leader/co-leader와 충분한 테마/체결 강도를 OBSERVE_READY로 평가합니다.",
            enabled=True,
            observe_only=observe_only,
            category="risk",
            priority=10,
            rules=[{"min_theme_score": config.min_theme_score}, {"min_hybrid_score": config.min_hybrid_score}],
            created_at=now,
            updated_at=now,
        ),
        ShadowPolicy(
            policy_id="strict_late_chase",
            policy_name="strict_late_chase",
            label_ko="Late chase 강화 차단",
            description="LATE_CHASE/HIGH_RETURN 계열 READY를 shadow BLOCK으로 평가합니다.",
            enabled=True,
            observe_only=True,
            category="risk",
            priority=20,
            rules=[{"reason_prefix": "LATE_CHASE|CHASE_RISK|HIGH_RETURN"}],
            created_at=now,
            updated_at=now,
        ),
        ShadowPolicy(
            policy_id="strict_entry_risk",
            policy_name="strict_entry_risk",
            label_ko="VI/상한가 근접 보수화",
            description="VI/상한가 근접 entry risk를 shadow BLOCK으로 평가합니다.",
            enabled=True,
            observe_only=True,
            category="risk",
            priority=30,
            rules=[{"reason_prefix": "VI_|UPPER_LIMIT"}],
            created_at=now,
            updated_at=now,
        ),
        ShadowPolicy(
            policy_id="relaxed_data_wait_for_leader",
            policy_name="relaxed_data_wait_for_leader",
            label_ko="데이터 대기 leader 완화",
            description="데이터 부족만 문제인 leader/co-leader를 실제 READY가 아닌 OBSERVE_READY로 평가합니다.",
            enabled=True,
            observe_only=True,
            category="data_quality",
            priority=40,
            rules=[{"reason": "DATA_INSUFFICIENT"}, {"observe_only": True}],
            created_at=now,
            updated_at=now,
        ),
        ShadowPolicy(
            policy_id="fast_theme_exit_shadow",
            policy_name="fast_theme_exit_shadow",
            label_ko="테마 약화 빠른 청산",
            description="HOLD 중 테마/leader 약화와 수익 반납을 빠른 shadow EXIT로 평가합니다.",
            enabled=True,
            observe_only=True,
            category="exit",
            priority=50,
            rules=[{"giveback_pct": -2.0}],
            created_at=now,
            updated_at=now,
        ),
    ]


def config_from_settings(settings: Any) -> ShadowStrategyConfig:
    return ShadowStrategyConfig(
        enabled=bool(getattr(settings, "shadow_strategy_enabled", True)),
        policy_ids=_normalize_policy_ids(getattr(settings, "shadow_strategy_policies", ",".join(DEFAULT_POLICY_IDS))),
        max_batch_size=max(1, int(getattr(settings, "shadow_strategy_max_batch_size", 500))),
        runtime_hook_enabled=bool(getattr(settings, "shadow_strategy_runtime_hook_enabled", True)),
        rebuild_limit=max(1, int(getattr(settings, "shadow_strategy_rebuild_limit", 10000))),
        min_theme_score=float(getattr(settings, "shadow_strategy_min_theme_score", 70.0)),
        min_hybrid_score=float(getattr(settings, "shadow_strategy_min_hybrid_score", 65.0)),
        ready_small_multiplier=float(getattr(settings, "shadow_strategy_ready_small_multiplier", 0.3)),
        observe_only=bool(getattr(settings, "shadow_strategy_observe_only", True)),
        allow_apply=False,
    )


def _normalize_policy_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [str(item).strip() for item in value or []]
    result = [item for item in items if item and item != "baseline"]
    return tuple(dict.fromkeys(result)) or DEFAULT_POLICY_IDS


def _reason_codes(decision: dict[str, Any]) -> list[str]:
    raw = decision.get("reason_codes") or []
    if isinstance(raw, str):
        return [part.strip().upper() for part in raw.replace(";", ",").split(",") if part.strip()]
    return [str(item).upper() for item in raw if str(item)]


def _has_any(reason_codes: list[str], needles: Iterable[str]) -> bool:
    normalized_needles = [str(needle).upper() for needle in needles]
    return any(any(needle in reason for needle in normalized_needles) for reason in reason_codes)


def _is_leader(decision: dict[str, Any]) -> bool:
    role = str(
        _detail_value(decision, "stock_role")
        or _detail_value(decision, "candidate_role")
        or _detail_value(decision, "role")
        or ""
    ).upper()
    return role in {"LEADER", "CO_LEADER", "CO-LEADER"}


def _has_market_support(decision: dict[str, Any]) -> bool:
    return (
        _float(decision.get("execution_strength")) >= 100.0
        or _float(decision.get("trade_value")) > 0
        or _float(decision.get("momentum_1m")) > 0
        or _float(decision.get("momentum_3m")) > 0
        or _float(decision.get("change_rate")) > 0
    )


def _detail_value(decision: dict[str, Any], key: str) -> Any:
    details = decision.get("details") or {}
    stack = [details]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        if key in current:
            return current.get(key)
        stack.extend(value for value in current.values() if isinstance(value, dict))
    return None


def _detail_bool(decision: dict[str, Any], key: str) -> bool:
    value = _detail_value(decision, key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _append_reason(reason_codes: list[str], reason: str) -> list[str]:
    result = list(reason_codes)
    if reason not in result:
        result.append(reason)
    return result


def _baseline_score(decision: dict[str, Any]) -> float:
    return max(_float(decision.get("gate_score")), _float(decision.get("hybrid_score")), _float(decision.get("theme_score")))


def _baseline_multiplier(decision: dict[str, Any]) -> float:
    if _ready_like(decision.get("gate_status")) or str(decision.get("action_type") or "").upper() in {"ENTRY_PLAN", "ENTRY_ORDER_INTENT"}:
        return 1.0
    return 0.0


def _ready_like(value: Any) -> bool:
    return str(value or "").upper() in {"READY", "READY_SMALL", "OBSERVE_READY"}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _positive_float(value: Any) -> float:
    parsed = _float(value)
    return parsed if parsed > 0 else 0.0
