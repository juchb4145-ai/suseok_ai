from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Iterable, Mapping

from trading.runtime_ports import CandidateRuntimeState, EntryDecisionEnvelope, EntryEvaluationStep, EntryStep, StepResult
from trading.strategy.candidate_fsm import CandidateBlockingStage, CandidateFsmService, CandidateReasonCode
from trading.strategy.entry_engine import EntryCheckStatus, EntryDecision, EntryDecisionResult, EntryDecisionStatus, EntryEngine
from trading.strategy.market_data_service import DirtyCodeEvent, DirtyReason, MarketDataService
from trading.strategy.models import Candidate, CandidateState


@dataclass(frozen=True)
class DirtyStrategyEvaluatorConfig:
    enabled: bool = True
    shadow_mode: bool = True
    max_codes_per_cycle: int = 50
    max_candidates_per_cycle: int = 100
    debounce_ms: int = 200
    fallback_full_scan: bool = True
    theme_cadence_sec: int = 1
    market_cadence_sec: int = 1
    save_decisions: bool = True
    order_intent_enabled: bool = False

    @classmethod
    def from_env(cls) -> "DirtyStrategyEvaluatorConfig":
        return cls(
            enabled=_env_bool("TRADING_DIRTY_EVALUATOR_ENABLED", True),
            shadow_mode=_env_bool("TRADING_DIRTY_EVALUATOR_SHADOW_MODE", True),
            max_codes_per_cycle=max(1, _env_int("TRADING_DIRTY_EVALUATOR_MAX_CODES_PER_CYCLE", 50)),
            max_candidates_per_cycle=max(1, _env_int("TRADING_DIRTY_EVALUATOR_MAX_CANDIDATES_PER_CYCLE", 100)),
            debounce_ms=max(0, _env_int("TRADING_DIRTY_EVALUATOR_DEBOUNCE_MS", 200)),
            fallback_full_scan=_env_bool("TRADING_DIRTY_EVALUATOR_FALLBACK_FULL_SCAN", True),
            theme_cadence_sec=max(1, _env_int("TRADING_DIRTY_EVALUATOR_THEME_CADENCE_SEC", 1)),
            market_cadence_sec=max(1, _env_int("TRADING_DIRTY_EVALUATOR_MARKET_CADENCE_SEC", 1)),
            save_decisions=_env_bool("TRADING_DIRTY_EVALUATOR_SAVE_DECISIONS", True),
            order_intent_enabled=_env_bool("TRADING_DIRTY_EVALUATOR_ORDER_INTENT_ENABLED", False),
        )


@dataclass(frozen=True)
class DirtyStrategyEvaluatorResult:
    status: str
    enabled: bool
    shadow_mode: bool
    dirty_code_count: int = 0
    evaluated_code_count: int = 0
    evaluated_candidate_count: int = 0
    debounced_count: int = 0
    skipped_count: int = 0
    saved_decision_count: int = 0
    full_scan_fallback_used: bool = False
    last_evaluated_at: str = ""
    duration_ms: int = 0
    top_dirty_reasons: tuple[dict[str, Any], ...] = ()
    blocking_stage_counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    order_intent_enabled: bool = False
    order_intent_created_count: int = 0
    shadow_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["top_dirty_reasons"] = list(self.top_dirty_reasons)
        data["warnings"] = list(self.warnings)
        return data


class DirtyStrategyEvaluator:
    def __init__(
        self,
        *,
        db: Any,
        market_data_service: MarketDataService,
        entry_engine: EntryEngine,
        config: DirtyStrategyEvaluatorConfig | None = None,
        fsm: CandidateFsmService | None = None,
        clock=None,
    ) -> None:
        self.db = db
        self.market_data_service = market_data_service
        self.entry_engine = entry_engine
        self.config = config or DirtyStrategyEvaluatorConfig.from_env()
        self.clock = clock or datetime.now
        self.fsm = fsm or CandidateFsmService(db, clock=self.clock)
        self.last_result = DirtyStrategyEvaluatorResult(status="DISABLED" if not self.config.enabled else "IDLE", enabled=self.config.enabled, shadow_mode=self.config.shadow_mode)
        self._last_evaluated_at_by_candidate: dict[str, datetime] = {}
        self._last_context_id_by_candidate: dict[str, str] = {}

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        return self.evaluate_dirty(now=now).to_dict()

    def evaluate_dirty(self, *, now: datetime | None = None) -> DirtyStrategyEvaluatorResult:
        started = perf_counter()
        current = (now or self.clock()).replace(microsecond=0)
        if not self.config.enabled:
            self.last_result = DirtyStrategyEvaluatorResult(status="DISABLED", enabled=False, shadow_mode=self.config.shadow_mode)
            return self.last_result
        dirty_events = self._pop_dirty_events(limit=self.config.max_codes_per_cycle)
        if not dirty_events:
            self.last_result = DirtyStrategyEvaluatorResult(
                status="IDLE",
                enabled=True,
                shadow_mode=self.config.shadow_mode,
                duration_ms=int(round((perf_counter() - started) * 1000)),
                last_evaluated_at=str(self.last_result.last_evaluated_at or ""),
                order_intent_enabled=False,
            )
            return self.last_result
        candidates, debounced_count, skipped_count = self._candidates_for_dirty_events(dirty_events, current)
        result = self._evaluate_candidates(candidates, dirty_events=dirty_events, now=current)
        stage_counts = self._sync_candidate_fsm(result.decisions, dirty_events=dirty_events, now=current)
        shadow = self._shadow_comparison(result, trade_date=current.date().isoformat(), now=current)
        reason_counter = Counter()
        for event in dirty_events:
            for reason in _split_reasons(event.reason):
                reason_counter[reason] += 1
        warnings: list[str] = []
        if self.config.order_intent_enabled:
            warnings.append("ORDER_INTENT_DISABLED_BY_PR5_CONTRACT")
        self.last_result = DirtyStrategyEvaluatorResult(
            status="OK",
            enabled=True,
            shadow_mode=self.config.shadow_mode,
            dirty_code_count=len(dirty_events),
            evaluated_code_count=len({event.code for event in dirty_events}),
            evaluated_candidate_count=result.evaluated_count,
            debounced_count=debounced_count,
            skipped_count=skipped_count,
            saved_decision_count=len(result.decisions) if result.saved else 0,
            full_scan_fallback_used=False,
            last_evaluated_at=current.isoformat(),
            duration_ms=int(round((perf_counter() - started) * 1000)),
            top_dirty_reasons=tuple({"reason": key, "count": count} for key, count in reason_counter.most_common(10)),
            blocking_stage_counts=dict(stage_counts),
            warnings=tuple(warnings),
            order_intent_enabled=False,
            order_intent_created_count=0,
            shadow_comparison=shadow,
        )
        return self.last_result

    def evaluate_dirty_codes(self, codes: Iterable[str], *, now: str) -> tuple[EntryDecisionEnvelope, ...]:
        current = _parse_time(now) or self.clock().replace(microsecond=0)
        events = tuple(DirtyCodeEvent(code=_normalize_code(code), reason=DirtyReason.PRICE_TICK.value) for code in codes)
        candidates, _debounced, _skipped = self._candidates_for_dirty_events(events, current, apply_debounce=False)
        result = self._evaluate_candidates(candidates, dirty_events=events, now=current)
        return tuple(self._envelope(decision, self._reason_by_code(events).get(decision.code, "")) for decision in result.decisions)

    def _pop_dirty_events(self, *, limit: int) -> tuple[DirtyCodeEvent, ...]:
        queue = getattr(self.market_data_service, "dirty_queue", None)
        pop_dirty = getattr(queue, "pop_dirty", None)
        if callable(pop_dirty):
            return tuple(pop_dirty(limit=limit))
        dirty_codes = getattr(self.market_data_service, "dirty_codes", None)
        if callable(dirty_codes):
            return tuple(DirtyCodeEvent(code=code, reason=DirtyReason.PRICE_TICK.value) for code in dirty_codes(limit=limit))
        return ()

    def _candidates_for_dirty_events(
        self,
        dirty_events: Iterable[DirtyCodeEvent],
        now: datetime,
        *,
        apply_debounce: bool = True,
    ) -> tuple[tuple[Candidate, ...], int, int]:
        trade_date = now.date().isoformat()
        reason_by_code = self._reason_by_code(dirty_events)
        candidates: list[Candidate] = []
        debounced = 0
        skipped = 0
        broad_reasons = {
            DirtyReason.MARKET_REGIME_CHANGED.value,
            DirtyReason.THEME_STATE_CHANGED.value,
            DirtyReason.THEME_LEADER_CHANGED.value,
            DirtyReason.THEME_ROLE_CHANGED.value,
            DirtyReason.STRATEGY_CONTEXT_CHANGED.value,
        }
        if any(set(_split_reasons(reason)) & broad_reasons for reason in reason_by_code.values()):
            pool = list(self.db.list_candidates(trade_date=trade_date) or [])
        else:
            pool = []
            for code in reason_by_code:
                candidate = self.db.load_candidate(trade_date, code)
                if candidate is not None:
                    pool.append(candidate)
        seen: set[str] = set()
        for candidate in pool:
            if candidate.state != CandidateState.WATCHING:
                skipped += 1
                continue
            key = str(candidate.id or candidate.code)
            if key in seen:
                continue
            if apply_debounce and self._debounced(key, now):
                debounced += 1
                continue
            context_id = str(dict(candidate.metadata or {}).get("strategy_context_id") or "")
            if (
                apply_debounce
                and context_id
                and DirtyReason.STRATEGY_CONTEXT_CHANGED.value in _split_reasons(reason_by_code.get(candidate.code, ""))
                and self._last_context_id_by_candidate.get(key) == context_id
            ):
                debounced += 1
                continue
            seen.add(key)
            candidates.append(candidate)
            self._last_evaluated_at_by_candidate[key] = now
            if context_id:
                self._last_context_id_by_candidate[key] = context_id
            if len(candidates) >= self.config.max_candidates_per_cycle:
                break
        return tuple(candidates), debounced, skipped

    def _evaluate_candidates(
        self,
        candidates: Iterable[Candidate],
        *,
        dirty_events: Iterable[DirtyCodeEvent],
        now: datetime,
    ) -> EntryDecisionResult:
        return self.entry_engine.evaluate_candidates(
            candidates,
            trade_date=now.date().isoformat(),
            now=now,
            save=bool(self.config.save_decisions),
        )

    def _sync_candidate_fsm(
        self,
        decisions: Iterable[EntryDecision],
        *,
        dirty_events: Iterable[DirtyCodeEvent],
        now: datetime,
    ) -> Counter:
        reason_by_code = self._reason_by_code(dirty_events)
        stage_counts: Counter = Counter()
        for decision in decisions:
            candidate = self.db.load_candidate(decision.trade_date, decision.code)
            if candidate is None:
                continue
            snapshot = self.market_data_service.latest_snapshot(decision.code)
            if snapshot is not None:
                self.fsm.on_realtime_tick(candidate, snapshot)
            stage, reason, target = _fsm_target(decision, snapshot)
            stage_counts[stage] += 1
            if target is not None:
                self._promote_if_changed(candidate, target, reason, dirty_reason=reason_by_code.get(decision.code, ""), decision=decision)
            else:
                self._block_if_changed(candidate, stage, reason, dirty_reason=reason_by_code.get(decision.code, ""), decision=decision)
            self.db.save_candidate(candidate)
        return stage_counts

    def _promote_if_changed(
        self,
        candidate: Candidate,
        target: CandidateRuntimeState,
        reason: str,
        *,
        dirty_reason: str,
        decision: EntryDecision,
    ) -> None:
        snap = self.fsm.transition_snapshot(candidate)
        if snap.v2_state == target.value and snap.primary_reason_code == reason and snap.blocking_stage == CandidateBlockingStage.NONE.value:
            return
        self.fsm.promote(
            candidate,
            target,
            reason,
            blocking_stage=CandidateBlockingStage.NONE,
            source_event_type="dirty_strategy_evaluation",
            source_component="DirtyStrategyEvaluator",
            details={"dirty_reason": dirty_reason, "entry_decision": decision.to_dict()},
        )

    def _block_if_changed(
        self,
        candidate: Candidate,
        stage: str,
        reason: str,
        *,
        dirty_reason: str,
        decision: EntryDecision,
    ) -> None:
        snap = self.fsm.transition_snapshot(candidate)
        if snap.blocking_stage == stage and snap.primary_reason_code == reason:
            return
        self.fsm.apply_blocking_reason(
            candidate,
            stage,
            reason,
            details={"dirty_reason": dirty_reason, "entry_decision": decision.to_dict()},
            source_event_type="dirty_strategy_evaluation",
            source_component="DirtyStrategyEvaluator",
        )

    def _shadow_comparison(self, incremental: EntryDecisionResult, *, trade_date: str, now: datetime) -> dict[str, Any]:
        if not self.config.shadow_mode or not self.config.fallback_full_scan:
            return {"enabled": bool(self.config.shadow_mode), "full_scan_fallback_used": False}
        full_result = self.entry_engine.build(trade_date=trade_date, now=now, save=False)
        full_by_code = {decision.code: decision for decision in full_result.decisions}
        inc_by_code = {decision.code: decision for decision in incremental.decisions}
        shared = sorted(set(full_by_code) & set(inc_by_code))
        return {
            "enabled": True,
            "full_scan_fallback_used": False,
            "full_scan_candidate_count": len(full_by_code),
            "incremental_candidate_count": len(inc_by_code),
            "matching_decision_count": sum(1 for code in shared if full_by_code[code].entry_status == inc_by_code[code].entry_status),
            "missing_in_incremental_count": len(set(full_by_code) - set(inc_by_code)),
            "extra_in_incremental_count": len(set(inc_by_code) - set(full_by_code)),
            "status_mismatch_count": sum(1 for code in shared if full_by_code[code].entry_status != inc_by_code[code].entry_status),
            "reason_mismatch_count": sum(1 for code in shared if tuple(full_by_code[code].reason_codes) != tuple(inc_by_code[code].reason_codes)),
        }

    def _envelope(self, decision: EntryDecision, dirty_reason: str) -> EntryDecisionEnvelope:
        stage, _reason, target = _fsm_target(decision, self.market_data_service.latest_snapshot(decision.code))
        return EntryDecisionEnvelope(
            decision=decision,
            candidate_state=(target or CandidateRuntimeState.WATCHING),
            blocking_stage=stage,
            steps=tuple(_decision_steps(decision)),
            dirty_reason=dirty_reason,
            next_required_action="",
        )

    def _reason_by_code(self, dirty_events: Iterable[DirtyCodeEvent]) -> dict[str, str]:
        result: dict[str, str] = {}
        for event in dirty_events:
            code = _normalize_code(event.code)
            if not code:
                continue
            existing = result.get(code, "")
            result[code] = event.reason if not existing else ",".join(_dedupe([*existing.split(","), *_split_reasons(event.reason)]))
        return result

    def _debounced(self, key: str, now: datetime) -> bool:
        previous = self._last_evaluated_at_by_candidate.get(key)
        if previous is None or self.config.debounce_ms <= 0:
            return False
        return (now - previous).total_seconds() * 1000.0 < self.config.debounce_ms


class DirtyStrategyEvaluatorRuntimePipeline:
    def __init__(self, evaluator: DirtyStrategyEvaluator) -> None:
        self.evaluator = evaluator
        self.config = evaluator.config

    def run_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        return self.evaluator.run_if_due(now)


def _fsm_target(decision: EntryDecision, snapshot: Any) -> tuple[str, str, CandidateRuntimeState | None]:
    if _snapshot_is_not_fresh_realtime(snapshot):
        return CandidateBlockingStage.DATA.value, CandidateReasonCode.LATEST_TICK_MISSING.value, None
    if decision.data_ready_status != EntryCheckStatus.PASS:
        return CandidateBlockingStage.DATA.value, _primary_reason(decision, default=CandidateReasonCode.LATEST_TICK_MISSING.value), None
    if decision.theme_ready_status != EntryCheckStatus.PASS:
        return CandidateBlockingStage.THEME.value, _primary_reason(decision, default=CandidateReasonCode.THEME_NOT_READY.value), None
    if decision.market_ready_status != EntryCheckStatus.PASS:
        return CandidateBlockingStage.MARKET.value, _primary_reason(decision, default=CandidateReasonCode.MARKET_RISK_OFF.value), None
    if decision.role_ready_status != EntryCheckStatus.PASS:
        return CandidateBlockingStage.ROLE.value, _primary_reason(decision, default=CandidateReasonCode.ROLE_NOT_ALLOWED.value), None
    if decision.price_timing_status != EntryCheckStatus.PASS:
        return CandidateBlockingStage.PRICE.value, _primary_reason(decision, default=CandidateReasonCode.PRICE_TIMING_NOT_READY.value), CandidateRuntimeState.SETUP_READY
    if decision.entry_status == EntryDecisionStatus.OBSERVE_READY:
        return CandidateBlockingStage.NONE.value, CandidateReasonCode.OBSERVE_READY_ORDER_DISABLED.value, CandidateRuntimeState.TIMING_READY
    return CandidateBlockingStage.PRICE.value, _primary_reason(decision, default=CandidateReasonCode.PRICE_TIMING_NOT_READY.value), None


def _decision_steps(decision: EntryDecision) -> list[EntryEvaluationStep]:
    return [
        EntryEvaluationStep(EntryStep.DATA_READY, _step_result(decision.data_ready_status), reason_codes=tuple(decision.reason_codes)),
        EntryEvaluationStep(EntryStep.THEME_READY, _step_result(decision.theme_ready_status), reason_codes=tuple(decision.reason_codes)),
        EntryEvaluationStep(EntryStep.MARKET_ALLOWED, _step_result(decision.market_ready_status), reason_codes=tuple(decision.reason_codes)),
        EntryEvaluationStep(EntryStep.ROLE_ALLOWED, _step_result(decision.role_ready_status), reason_codes=tuple(decision.reason_codes)),
        EntryEvaluationStep(EntryStep.PRICE_TIMING_READY, _step_result(decision.price_timing_status), reason_codes=tuple(decision.reason_codes)),
        EntryEvaluationStep(EntryStep.RISK_PRECHECK, StepResult.PASS if decision.entry_status == EntryDecisionStatus.OBSERVE_READY else StepResult.WAIT),
    ]


def _step_result(status: EntryCheckStatus) -> StepResult:
    if status == EntryCheckStatus.PASS:
        return StepResult.PASS
    if status == EntryCheckStatus.DATA_WAIT:
        return StepResult.DATA_WAIT
    if status == EntryCheckStatus.BLOCK:
        return StepResult.BLOCK
    return StepResult.WAIT


def _primary_reason(decision: EntryDecision, *, default: str) -> str:
    for reason in decision.reason_codes:
        text = str(reason or "")
        if text and not text.endswith("_READY"):
            return text
    return default


def _snapshot_is_not_fresh_realtime(snapshot: Any) -> bool:
    if snapshot is None:
        return True
    data = snapshot if isinstance(snapshot, Mapping) else getattr(snapshot, "__dict__", {})
    return not bool(data.get("is_fresh")) or str(data.get("price_source") or "REALTIME").upper() == "TR_BACKFILL"


def _split_reasons(value: Any) -> list[str]:
    return [text for text in (str(value or "").split(",")) if text]


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "DirtyStrategyEvaluator",
    "DirtyStrategyEvaluatorConfig",
    "DirtyStrategyEvaluatorResult",
    "DirtyStrategyEvaluatorRuntimePipeline",
]
