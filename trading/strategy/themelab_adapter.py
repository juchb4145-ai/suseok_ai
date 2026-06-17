from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any, Callable, Iterable, Optional

from trading.strategy.candidates import CandidateLifecycle, is_valid_stock_code, normalize_code
from trading.strategy.candidate_identity import CandidateGenerationConfig, CandidateInstanceDecision, build_candidate_instance_id, decide_candidate_instance, identity_metadata
from trading.strategy.data_quality_taxonomy import (
    ACTION_ALLOW_EARLY_SMALL_CANDIDATE,
    BUCKET_BACKFILL_ONLY_OBSERVE,
    BUCKET_CORE_BLOCKING,
    BUCKET_ENTRY_BLOCKING,
    BUCKET_WARMUP_OPTIONAL,
    DataQualityClassification,
    classify_entry_data_quality,
    data_quality_action_for_candidate,
)
from trading.strategy.models import (
    BlockType,
    Candidate,
    CandidateEvent,
    CandidateSourceType,
    CandidateState,
    GateDecision,
    IndicatorSnapshot,
    StrategyProfile,
)
from trading.strategy.pipeline import GatePipelineResult
from trading.strategy.reason_codes import normalize_reason_codes, standardize_details
from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings
from trading.strategy.shadow_small_entry_promotion import (
    STATUS_BLOCKED as SHADOW_PROMOTION_BLOCKED,
    STATUS_NO_EVIDENCE as SHADOW_PROMOTION_NO_EVIDENCE,
    STATUS_OBSERVE_ONLY as SHADOW_PROMOTION_OBSERVE_ONLY,
    STATUS_PROMOTED as SHADOW_PROMOTION_PROMOTED,
    evaluate_shadow_small_entry_promotion,
)
from trading.strategy.trade_setup_classifier import attach_trade_setup_details
from trading.strategy.support_readiness import (
    LATEST_TICK_MISSING,
    OBSERVE,
    READY_EARLY_SMALL,
    READY_FULL,
    SUPPORT_NOT_READY,
    SUPPORT_DATA_MISSING,
    SUPPORT_STRUCTURALLY_MISSING,
    SUPPORT_SOURCE_FALLBACK_USED,
    WAIT_DATA,
    WAIT_DATA_SUPPORT_NOT_READY,
    latest_tick_readiness,
    support_metadata,
    support_coverage,
    support_missing_taxonomy,
    support_source_readiness,
)
from trading.theme_engine.lab import (
    LabGateDecision,
    LabGateStatus,
    MarketSide,
    PriceLocationStatus,
    StockRole,
    ThemeConditionSnapshot,
    ThemeLabFlowResult,
    ThemeLabThemeStatus,
    TradeabilityRiskLevel,
    WatchSetSnapshot,
    normalize_market_side,
)


SOURCE = "themelab_flow"
ORDER_PHASE_ENTRY = "entry"
LATE_CHASE_TEMP_WAIT = "LATE_CHASE_TEMP_WAIT"
RISK_SOFT_BLOCK_TEMP_WAIT = "RISK_SOFT_BLOCK_TEMP_WAIT"
READY_SHADOW_SMALL_ENTRY = "READY_SHADOW_SMALL_ENTRY"
SHADOW_SMALL_ENTRY_REASON_CODE = "SHADOW_SMALL_ENTRY_DRY_RUN"
WAIT_DATA_REALTIME_RELIABILITY_LOW = "WAIT_DATA_REALTIME_RELIABILITY_LOW"
REALTIME_RELIABILITY_REASON_CODE = "REALTIME_RELIABILITY_LOW"
REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE = "REALTIME_RELIABILITY_SIZE_REDUCED"
REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE = "REALTIME_RELIABILITY_NOT_HIGH"
ENTRY_RISK_TEMP_WAIT = "ENTRY_RISK_TEMP_WAIT"
ENTRY_RISK_FINAL_BLOCK = "ENTRY_RISK_FINAL_BLOCK"
ENTRY_RISK_RECOVERY_PENDING = "ENTRY_RISK_RECOVERY_PENDING"
ENTRY_RISK_RECOVERED = "ENTRY_RISK_RECOVERED"
RISK_ADJUST_POSITION_SIZE = "RISK_ADJUST_POSITION_SIZE"
LATE_CHASE_SOFT_BLOCK_CODES = {"HIGH_CHASE_RISK", "LATE_CHASE", "CHASE_RISK"}
ENTRY_RISK_CODES = {
    "VI_ACTIVE",
    "VI_COOLDOWN",
    "VI_UNKNOWN_LIMIT_RISK",
    "UPPER_LIMIT_NEAR",
    "UPPER_LIMIT_HARD_NEAR",
    "HIGH_RETURN_LEADER",
    "HIGH_RETURN_CO_LEADER",
    "HIGH_RETURN_FOLLOWER",
    "HIGH_RETURN_LATE_LAGGARD",
    "HIGH_RETURN_UNKNOWN_ROLE",
    ENTRY_RISK_TEMP_WAIT,
    ENTRY_RISK_FINAL_BLOCK,
    ENTRY_RISK_RECOVERY_PENDING,
    ENTRY_RISK_RECOVERED,
    RISK_ADJUST_POSITION_SIZE,
}
ENTRY_RISK_FINAL_CODES = {
    "VI_ACTIVE",
    "UPPER_LIMIT_HARD_NEAR",
    "HIGH_RETURN_FOLLOWER",
    "HIGH_RETURN_LATE_LAGGARD",
    "HIGH_RETURN_UNKNOWN_ROLE",
    ENTRY_RISK_FINAL_BLOCK,
}
ENTRY_RISK_TEMP_CODES = {
    "VI_COOLDOWN",
    "VI_UNKNOWN_LIMIT_RISK",
    "UPPER_LIMIT_NEAR",
    "HIGH_RETURN_LEADER",
    "HIGH_RETURN_CO_LEADER",
    ENTRY_RISK_TEMP_WAIT,
    ENTRY_RISK_RECOVERY_PENDING,
}
MARKET_WEAK_WAIT_CODES = {"CANDIDATE_MARKET_WEAK", "KOSDAQ_MARKET_WEAK", "KOSPI_MARKET_WEAK"}
MARKET_RISK_OFF_WAIT_CODES = {
    "GLOBAL_MARKET_RISK_OFF",
    "CANDIDATE_MARKET_RISK_OFF",
    "KOSDAQ_MARKET_RISK_OFF",
    "KOSPI_MARKET_RISK_OFF",
}
MARKET_CONFIRMATION_PENDING_CODES = {
    "WAIT_MARKET_CONFIRMATION_PENDING",
    "MARKET_WEAK_CONFIRMATION_PENDING",
    "MARKET_RISK_OFF_CONFIRMATION_PENDING",
    "CANDIDATE_MARKET_WEAK_UNCONFIRMED",
    "CANDIDATE_MARKET_RISK_OFF_UNCONFIRMED",
    "SIDE_BREADTH_SOURCE_CONFLICT",
}
MARKET_RECOVERY_PENDING_CODES = {
    "WAIT_MARKET_RECOVERY_PENDING",
    "MARKET_RECOVERY_CONFIRMATION_PENDING",
    "MARKET_WAIT_HYSTERESIS_HOLD",
}
MARKET_CLASSIFICATION_WAIT_CODES = {"MARKET_CLASSIFICATION_MISSING", "MARKET_CLASSIFICATION_FALLBACK_STRICT"}
RISK_OFF_SMALL_ENTRY_CODES = {"RISK_OFF_SMALL_ENTRY", "READY_RISK_OFF_SMALL", "OBSERVE_RISK_OFF_SMALL_ENTRY"}
RISK_OFF_SMALL_ENTRY_BLOCKING_CODES = {
    "EXTREME_RISK_OFF",
    "WAIT_MARKET_CONFIRMATION_PENDING",
    "MARKET_RISK_OFF_CONFIRMATION_PENDING",
    "CANDIDATE_MARKET_RISK_OFF_UNCONFIRMED",
    "WAIT_MARKET_RECOVERY_PENDING",
    "MARKET_RECOVERY_CONFIRMATION_PENDING",
    "MARKET_WAIT_HYSTERESIS_HOLD",
    "SIDE_BREADTH_SOURCE_CONFLICT",
    "DATA_INSUFFICIENT",
    "INDICATOR_DATA_INSUFFICIENT",
    "STALE_QUOTE",
    "MISSING_CURRENT_PRICE",
    "SUPPORT_NOT_READY",
    "WAIT_DATA_SUPPORT_NOT_READY",
    "MARKET_CONFIRMATION_PENDING",
    "MARKET_RECOVERY_PENDING",
}

READY_PULLBACK_LOCATIONS = {
    PriceLocationStatus.GOOD_PULLBACK,
    PriceLocationStatus.PULLBACK_RECLAIM,
    PriceLocationStatus.VWAP_RECLAIM,
}
READY_SMALL_PULLBACK_LOCATIONS = {
    PriceLocationStatus.GOOD_PULLBACK,
    PriceLocationStatus.PULLBACK_RECLAIM,
}
OBSERVE_ONLY_LOCATIONS = {
    PriceLocationStatus.CHASE_HIGH,
    PriceLocationStatus.BREAKOUT_CONTINUATION,
    PriceLocationStatus.VWAP_OVEREXTENDED,
}
LEADER_ROLES = {StockRole.LEADER, StockRole.CO_LEADER}
BUY_ALLOWED_RISKS = {TradeabilityRiskLevel.PASS, TradeabilityRiskLevel.RISK_ADJUST}
DATA_INSUFFICIENT_CODES = {
    "DATA_INSUFFICIENT",
    "INDICATOR_DATA_INSUFFICIENT",
    "MISSING_CURRENT_PRICE",
    "MISSING_PREV_CLOSE",
    "STALE_QUOTE",
}
THEME_WEAK_CODES = {"THEME_WEAK", "WEAK_THEME"}


@dataclass(frozen=True)
class ThemeLabBridgeBuildResult:
    gate_results: list[GatePipelineResult]
    candidate_save_count: int = 0
    warnings: list[str] | None = None
    timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ThemeLabBridgeMapping:
    final_gate_status: str
    order_eligibility: str
    strategy_eligible: bool
    block_type: BlockType
    can_recover: bool
    recheck_after_sec: int
    final_grade: str
    final_score: float
    reason_codes: list[str]
    ready_type: str = ""
    selected_support_source: str = ""
    selected_support_price: int = 0
    selected_support_ready: bool = False
    selected_support_ready_reason: str = ""
    support_source_fallback_used: bool = False
    latest_tick_ready: bool = True
    latest_tick_age_sec: float | None = None
    position_size_multiplier: float = 0.0
    shadow_small_entry_guard: dict[str, Any] = field(default_factory=dict)
    realtime_reliability_guard: dict[str, Any] = field(default_factory=dict)
    data_quality_bucket: str = ""
    data_quality_action: str = ""
    missing_core_fields: list[str] = field(default_factory=list)
    missing_entry_fields: list[str] = field(default_factory=list)
    missing_optional_fields: list[str] = field(default_factory=list)
    data_quality_confidence: str = ""
    operator_message_ko: str = ""
    early_small_candidate: bool = False
    early_small_order_enabled: bool = False
    early_small_position_size_multiplier: float = 0.0
    early_small_rejected_reason: str = ""


class ThemeLabDryRunLifecycleBridge:
    """Translate ThemeLab scanner decisions into the existing DRY_RUN lifecycle model."""

    def __init__(
        self,
        *,
        db,
        market_data,
        default_ttl_minutes: int = 30,
        settings: StrategyRuntimeSettings | None = None,
        generation_config: CandidateGenerationConfig | None = None,
        shadow_ab_provider: Callable[[str], dict[str, Any]] | None = None,
        shadow_small_entry_promotion_provider: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.default_ttl_minutes = max(1, int(default_ttl_minutes or 30))
        self.settings = settings or legacy_strategy_runtime_settings()
        self.generation_config = generation_config or CandidateGenerationConfig.from_env()
        self.shadow_ab_provider = shadow_ab_provider
        self.shadow_small_entry_promotion_provider = shadow_small_entry_promotion_provider

    def build(
        self,
        result: ThemeLabFlowResult,
        *,
        trade_date: str,
        now: datetime,
    ) -> ThemeLabBridgeBuildResult:
        total_started = perf_counter()
        timings: dict[str, float] = {}
        counters: dict[str, int] = {}

        def timed(label: str, callback):
            started = perf_counter()
            try:
                return callback()
            finally:
                timings[label] = round(timings.get(label, 0.0) + (perf_counter() - started), 6)

        warnings: list[str] = []
        candidate_saves = 0
        decision_cycle_id = f"{SOURCE}:{trade_date}:{now.replace(microsecond=0).isoformat()}"
        watch_by_code = timed("index_watchset", lambda: {normalize_code(item.symbol): item for item in result.watchset})
        themes_by_id = timed("index_themes", lambda: {str(item.theme_id): item for item in result.themes})
        gate_results: list[GatePipelineResult] = []
        shadow_policy = timed("shadow_small_entry_policy", lambda: self._shadow_small_entry_policy(trade_date))
        promotion_evidence = timed("shadow_small_entry_promotion_evidence", lambda: self._shadow_small_entry_promotion_evidence(trade_date))
        shadow_promotions = 0
        shadow_max_promotions = int(shadow_policy.get("max_promotions_per_cycle") or 0)
        candidate_codes = _bridge_candidate_codes(result.gate_decisions, watch_by_code)
        existing_by_code = timed("prefetch_candidates", lambda: self._load_existing_candidates(trade_date, candidate_codes))
        counters["decision_count"] = len(result.gate_decisions)
        counters["watchset_count"] = len(result.watchset)
        counters["prefetch_candidate_count"] = len(existing_by_code)

        for decision in result.gate_decisions:
            code = normalize_code(decision.symbol)
            if not is_valid_stock_code(code):
                warnings.append(f"THEME_LAB_BRIDGE_INVALID_CODE:{decision.symbol}")
                counters["invalid_code_count"] = counters.get("invalid_code_count", 0) + 1
                continue
            watch = watch_by_code.get(code)
            if watch is None:
                warnings.append(f"THEME_LAB_BRIDGE_WATCHSET_MISSING:{code}")
                counters["watchset_missing_count"] = counters.get("watchset_missing_count", 0) + 1
                continue
            theme_id = str(watch.primary_theme or (watch.themes[0] if watch.themes else ""))
            theme = themes_by_id.get(theme_id)
            candidate, saved = timed(
                "ensure_candidate",
                lambda code=code, watch=watch, theme=theme: self._ensure_candidate(
                    code,
                    watch,
                    theme,
                    trade_date=trade_date,
                    now=now,
                    decision_cycle_id=decision_cycle_id,
                    existing_candidate=existing_by_code.get(code),
                    existing_loaded=True,
                ),
            )
            existing_by_code[code] = candidate
            if saved:
                candidate_saves += 1
            if candidate.id is None:
                warnings.append(f"THEME_LAB_BRIDGE_CANDIDATE_ID_MISSING:{code}")
                counters["candidate_id_missing_count"] = counters.get("candidate_id_missing_count", 0) + 1
                continue
            gate_result = timed(
                "gate_result",
                lambda candidate=candidate, decision=decision, watch=watch, theme=theme: self._gate_result(
                    candidate,
                    decision,
                    watch,
                    theme,
                    now,
                    decision_cycle_id=decision_cycle_id,
                    shadow_policy=shadow_policy,
                    shadow_promotion_available=shadow_promotions < shadow_max_promotions,
                    shadow_promotion_evidence=promotion_evidence,
                ),
            )
            if gate_result.details.get("shadow_small_entry_dry_run_promoted"):
                shadow_promotions += 1
            gate_results.append(gate_result)

        counters["candidate_save_count"] = candidate_saves
        counters["gate_result_count"] = len(gate_results)
        counters["shadow_promotion_count"] = shadow_promotions
        timings["total"] = round(perf_counter() - total_started, 6)
        return ThemeLabBridgeBuildResult(
            gate_results=gate_results,
            candidate_save_count=candidate_saves,
            warnings=warnings,
            timings=timings,
            counters=counters,
        )

    def _load_existing_candidates(self, trade_date: str, codes: Iterable[str]) -> dict[str, Candidate]:
        clean_codes = sorted({normalize_code(code) for code in codes if normalize_code(code)})
        if not clean_codes:
            return {}
        loader = getattr(self.db, "load_candidates_by_codes", None)
        if callable(loader):
            return {
                normalize_code(candidate.code): candidate
                for candidate in loader(trade_date, clean_codes)
                if normalize_code(candidate.code)
            }
        result: dict[str, Candidate] = {}
        for code in clean_codes:
            candidate = self.db.load_candidate(trade_date, code)
            if candidate is not None:
                result[code] = candidate
        return result

    def _shadow_small_entry_policy(self, trade_date: str) -> dict[str, Any]:
        config = _shadow_small_entry_settings(self.settings)
        if not config["enabled"]:
            return {"enabled": False, "active": False, "status": "DISABLED", **config}
        if self.shadow_ab_provider is None:
            return {"enabled": True, "active": False, "status": "NO_AB_PROVIDER", **config}
        try:
            report = self.shadow_ab_provider(trade_date)
        except Exception as exc:
            return {"enabled": True, "active": False, "status": "AB_PROVIDER_ERROR", "error": str(exc), **config}
        return {**config, **_shadow_small_entry_policy_from_report(report, config)}

    def _shadow_small_entry_promotion_evidence(self, trade_date: str) -> dict[str, Any]:
        if self.shadow_small_entry_promotion_provider is None:
            return {"available": False, "status": "NO_PROMOTION_PROVIDER"}
        try:
            evidence = self.shadow_small_entry_promotion_provider(trade_date)
        except Exception as exc:
            return {"available": False, "status": "PROMOTION_PROVIDER_ERROR", "error": str(exc)}
        return dict(evidence or {})

    def _ensure_candidate(
        self,
        code: str,
        watch: WatchSetSnapshot,
        theme: ThemeConditionSnapshot | None,
        *,
        trade_date: str,
        now: datetime,
        decision_cycle_id: str,
        existing_candidate: Candidate | None = None,
        existing_loaded: bool = False,
    ) -> tuple[Candidate, bool]:
        now_text = now.replace(microsecond=0).isoformat()
        expires_at = (now.replace(microsecond=0) + timedelta(minutes=self.default_ttl_minutes)).isoformat()
        existing = existing_candidate if existing_loaded else self.db.load_candidate(trade_date, code)
        strategy_profile = _strategy_profile_from_watch(watch, self._latest_tick_metadata(code))
        candidate_market = _candidate_market_for_candidate(watch)
        theme_id = str(watch.primary_theme or (watch.themes[0] if watch.themes else ""))
        base_metadata = _candidate_bridge_metadata(watch, theme)
        existing_metadata = dict(existing.metadata or {}) if existing is not None else {}
        if existing is not None and existing.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
            existing_metadata["candidate_generation_force_new_reason"] = "previous_lifecycle_closed"
        first_seen_at = str(existing_metadata.get("candidate_instance_first_seen_at") or (existing.detected_at if existing else "") or now_text)
        identity = decide_candidate_instance(
            trade_date=trade_date,
            code=code,
            source=SOURCE,
            strategy_name=strategy_profile.value if strategy_profile else "",
            theme_id=theme_id,
            first_seen_at=first_seen_at,
            theme_name=theme.theme_name if theme is not None else "",
            existing_metadata=existing_metadata,
            now=now,
            config=self.generation_config,
        )
        generation_changed = identity.generation_reason not in {"initial_generation", "same_generation", "same_generation_min_gap_guardrail", "same_generation_max_generation_guardrail"}
        if generation_changed:
            first_seen_at = now_text
            identity = CandidateInstanceDecision(
                candidate_instance_id=build_candidate_instance_id(
                    trade_date=trade_date,
                    code=code,
                    source=SOURCE,
                    strategy_name=strategy_profile.value if strategy_profile else "",
                    theme_id=theme_id,
                    first_seen_at=first_seen_at,
                    candidate_generation_seq=identity.candidate_generation_seq,
                ),
                candidate_generation_seq=identity.candidate_generation_seq,
                generation_reason=identity.generation_reason,
                previous_candidate_instance_id=identity.previous_candidate_instance_id,
                previous_seen_at=identity.previous_seen_at,
                minutes_since_previous_signal=identity.minutes_since_previous_signal,
                blocked_generation_reason=identity.blocked_generation_reason,
                excessive_generation_blocked=identity.excessive_generation_blocked,
            )
        metadata = {
            **base_metadata,
            **identity_metadata(
                identity,
                source=SOURCE,
                strategy_name=strategy_profile.value if strategy_profile else "",
                theme_id=theme_id,
                first_seen_at=first_seen_at,
                last_seen_at=now_text,
                theme_name=theme.theme_name if theme is not None else "",
                config=self.generation_config,
            ),
            "decision_cycle_id": decision_cycle_id,
        }
        if existing is None:
            candidate = Candidate(
                trade_date=trade_date,
                code=code,
                name=watch.name,
                market=candidate_market,
                strategy_profile=strategy_profile,
                sources=[CandidateSourceType.THEME_WATCH],
                state=CandidateState.DETECTED,
                detected_at=now_text,
                last_seen_at=now_text,
                expires_at=expires_at,
                metadata=metadata,
            )
            CandidateLifecycle.validate_transition(None, candidate.state)
            CandidateLifecycle.transition(candidate, CandidateState.WATCHING)
            saved = self.db.save_candidate_with_events(
                candidate,
                [
                    _candidate_event(
                        "candidate_detected",
                        candidate,
                        None,
                        CandidateState.DETECTED,
                        "theme lab watchset candidate",
                        metadata,
                        now_text,
                    ),
                    _candidate_event(
                        "state_changed",
                        candidate,
                        CandidateState.DETECTED,
                        CandidateState.WATCHING,
                        "theme lab watchset registered",
                        metadata,
                        now_text,
                    ),
                ],
            )
            return saved, True

        changed = False
        if existing.state in {CandidateState.REMOVED, CandidateState.EXPIRED}:
            CandidateLifecycle.transition(existing, CandidateState.DETECTED)
            CandidateLifecycle.transition(existing, CandidateState.WATCHING)
            existing.block_type = BlockType.NONE
            existing.can_recover = False
            existing.recheck_after_sec = 0
            changed = True
        elif existing.state == CandidateState.DETECTED:
            CandidateLifecycle.transition(existing, CandidateState.WATCHING)
            changed = True
        if CandidateSourceType.THEME_WATCH not in existing.sources:
            existing.sources.append(CandidateSourceType.THEME_WATCH)
            changed = True
        if watch.name and existing.name != watch.name:
            existing.name = watch.name
            changed = True
        if existing.strategy_profile is None:
            existing.strategy_profile = strategy_profile
            changed = True
        if candidate_market and existing.market != candidate_market:
            existing.market = candidate_market
            changed = True
        existing.last_seen_at = now_text
        existing.expires_at = expires_at
        merged_metadata = dict(existing.metadata or {})
        merged_metadata.update(metadata)
        if merged_metadata != existing.metadata:
            existing.metadata = merged_metadata
            changed = True
        if changed:
            if generation_changed:
                saved = self.db.save_candidate_with_events(
                    existing,
                    [
                        _candidate_event(
                            "candidate_generation_changed",
                            existing,
                            existing.state,
                            existing.state,
                            identity.generation_reason,
                            metadata,
                            now_text,
                        )
                    ],
                )
                return saved, True
            return self.db.save_candidate(existing), True
        return existing, False

    def _gate_result(
        self,
        candidate: Candidate,
        decision: LabGateDecision,
        watch: WatchSetSnapshot,
        theme: ThemeConditionSnapshot | None,
        now: datetime,
        *,
        decision_cycle_id: str,
        shadow_policy: dict[str, Any] | None = None,
        shadow_promotion_available: bool = True,
        shadow_promotion_evidence: dict[str, Any] | None = None,
    ) -> GatePipelineResult:
        tick = self.market_data.latest_tick(candidate.code) if self.market_data is not None else None
        tick_metadata = dict(tick.metadata or {}) if tick is not None else {}
        mapping = _map_decision(
            decision,
            watch,
            theme,
            tick_metadata=tick_metadata,
            tick=tick,
            now=now,
            settings=self.settings,
            shadow_policy=shadow_policy,
            shadow_promotion_available=shadow_promotion_available,
            shadow_promotion_evidence=shadow_promotion_evidence,
        )
        snapshot = self._indicator_snapshot(candidate, decision, watch, now)
        stock_details = _stock_pullback_details(candidate, decision, watch, snapshot, tick_metadata, mapping)
        created_at = now.replace(microsecond=0).isoformat()
        lab_details = _base_details(candidate, decision, watch, theme, mapping, stock_details, created_at, decision_cycle_id=decision_cycle_id)
        lab_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="ThemeLabBridgeGate",
            passed=mapping.strategy_eligible or mapping.block_type == BlockType.NONE,
            score=mapping.final_score,
            grade=mapping.final_gate_status,
            block_type=mapping.block_type,
            can_recover=mapping.can_recover,
            recheck_after_sec=mapping.recheck_after_sec,
            reason_codes=mapping.reason_codes,
            details=lab_details,
            created_at=created_at,
        )
        stock_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="StockPullbackEntryGate",
            passed=mapping.strategy_eligible,
            score=decision.price_location_score,
            grade=decision.price_location_status.value,
            block_type=mapping.block_type,
            can_recover=mapping.can_recover,
            recheck_after_sec=mapping.recheck_after_sec,
            reason_codes=[] if mapping.strategy_eligible else mapping.reason_codes,
            details=standardize_details(
                dict(stock_details),
                [] if mapping.strategy_eligible else mapping.reason_codes,
                passed=mapping.strategy_eligible,
                score=decision.price_location_score,
                created_at=created_at,
            ),
            created_at=created_at,
        )
        final_decision = GateDecision(
            candidate_id=candidate.id,
            gate_name="FinalGrade",
            passed=mapping.strategy_eligible,
            score=mapping.final_score,
            grade=mapping.final_grade,
            block_type=mapping.block_type,
            can_recover=mapping.can_recover,
            recheck_after_sec=mapping.recheck_after_sec,
            reason_codes=mapping.reason_codes,
            details=lab_details,
            created_at=created_at,
        )
        return GatePipelineResult(
            candidate_id=candidate.id,
            code=candidate.code,
            theme_id=str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
            final_grade=mapping.final_grade,
            final_score=mapping.final_score,
            strategy_eligible=mapping.strategy_eligible,
            block_type=mapping.block_type,
            can_recover=mapping.can_recover,
            recheck_after_sec=mapping.recheck_after_sec,
            decisions=[lab_decision, stock_decision, final_decision],
            snapshot=snapshot,
            details=lab_details,
        )

    def _indicator_snapshot(
        self,
        candidate: Candidate,
        decision: LabGateDecision,
        watch: WatchSetSnapshot,
        now: datetime,
    ) -> IndicatorSnapshot:
        tick = self.market_data.latest_tick(candidate.code) if self.market_data is not None else None
        metadata = dict(tick.metadata or {}) if tick is not None else {}
        candidate_metadata = dict(candidate.metadata or {})
        day_high, day_low = self.market_data.day_high_low(candidate.code) if self.market_data is not None else (0, 0)
        return IndicatorSnapshot(
            candidate_id=candidate.id,
            code=candidate.code,
            created_at=now.replace(microsecond=0).isoformat(),
            price=int(tick.price or 0) if tick is not None else 0,
            vwap=_float_or_none(metadata.get("vwap")),
            ema20_5m=_float_or_none(metadata.get("ema20_5m")),
            base_line_120=_float_or_none(metadata.get("base_line_120")),
            envelope_mid=_float_or_none(metadata.get("envelope_mid")),
            day_high=int(_float_or_none(metadata.get("day_high")) or day_high or 0),
            day_low=int(_float_or_none(metadata.get("day_low")) or day_low or 0),
            day_mid=_float_or_none(metadata.get("day_mid")),
            prev_high=int(_float_or_none(metadata.get("prev_high")) or 0),
            prev_low=int(_float_or_none(metadata.get("prev_low")) or 0),
            pullback_pct=_float_or_none(metadata.get("pullback_pct")),
            volume_reaccel=bool(metadata.get("volume_reaccel")),
            failed_low_break_rebound=bool(metadata.get("failed_low_break_rebound")),
            chase_risk=decision.price_location_status in OBSERVE_ONLY_LOCATIONS,
            metadata={
                **metadata,
                "source": SOURCE,
                "candidate_instance_id": candidate_metadata.get("candidate_instance_id", ""),
                "candidate_generation_seq": candidate_metadata.get("candidate_generation_seq", 0),
                "decision_cycle_id": candidate_metadata.get("decision_cycle_id", ""),
                "watch_return_pct": watch.return_pct,
                "price_location_status": decision.price_location_status.value,
                "price_location_reason_codes": list(decision.price_location_reason_codes),
                "price_location_readiness": watch.price_location_readiness.value,
                "price_location_readiness_reason_codes": list(watch.price_location_readiness_reason_codes),
                "price_location_provisional": bool(watch.price_location_provisional),
                **_market_side_fields(decision, watch),
            },
        )

    def _latest_tick_metadata(self, code: str) -> dict[str, Any]:
        tick = self.market_data.latest_tick(code) if self.market_data is not None else None
        return dict(tick.metadata or {}) if tick is not None else {}


def _bridge_candidate_codes(
    decisions: Iterable[LabGateDecision],
    watch_by_code: dict[str, WatchSetSnapshot],
) -> list[str]:
    result: list[str] = []
    for decision in decisions:
        code = normalize_code(decision.symbol)
        if is_valid_stock_code(code) and code in watch_by_code and code not in result:
            result.append(code)
    return result


def _map_decision(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
    *,
    tick_metadata: dict[str, Any] | None = None,
    tick: Any = None,
    now: datetime | None = None,
    settings: StrategyRuntimeSettings | None = None,
    shadow_policy: dict[str, Any] | None = None,
    shadow_promotion_available: bool = True,
    shadow_promotion_evidence: dict[str, Any] | None = None,
) -> ThemeLabBridgeMapping:
    metadata = dict(tick_metadata or {})
    active_settings = settings or legacy_strategy_runtime_settings()
    reason_codes = _reason_codes(decision, watch, theme)
    recheck_after_sec = int(decision.recheck_after_sec or 60)
    risk_allowed = decision.risk_level in BUY_ALLOWED_RISKS
    price_location = decision.price_location_status
    role = watch.stock_role
    latest = latest_tick_readiness(tick, now or datetime.now(), active_settings)
    reliability_policy = _realtime_reliability_settings(active_settings)
    reliability_guard = _realtime_reliability_guard(metadata, reliability_policy)
    support = _selected_support_profile(price_location, _support_candidates(metadata), metadata)
    data_quality = _bridge_data_quality_classification(
        decision,
        watch,
        theme,
        tick=tick,
        metadata=metadata,
        support=support,
        latest=latest,
        reason_codes=reason_codes,
        settings=active_settings,
        now=now or datetime.now(),
    )
    promotion_mapping = _shadow_small_entry_promotion_mapping(
        decision,
        watch,
        theme,
        metadata,
        support,
        latest,
        data_quality,
        active_settings,
        shadow_promotion_evidence,
        promotion_available=shadow_promotion_available,
        recheck_after_sec=recheck_after_sec,
    )
    if promotion_mapping is not None:
        return promotion_mapping

    if data_quality.bucket == BUCKET_BACKFILL_ONLY_OBSERVE:
        return ThemeLabBridgeMapping(
            "OBSERVE_BACKFILL_ONLY",
            "NOT_ELIGIBLE_DATA",
            False,
            BlockType.NONE,
            False,
            recheck_after_sec,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(data_quality.reason_codes + reason_codes),
            ready_type=OBSERVE,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
            **_data_quality_mapping_fields(data_quality),
        )
    if data_quality.bucket == BUCKET_CORE_BLOCKING:
        return ThemeLabBridgeMapping(
            WAIT_DATA,
            "NOT_ELIGIBLE_DATA",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(data_quality.reason_codes + reason_codes),
            ready_type=WAIT_DATA,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
            **_data_quality_mapping_fields(data_quality),
        )
    if _is_theme_weak(reason_codes, theme):
        return ThemeLabBridgeMapping(
            "BLOCK_THEME",
            "NOT_ELIGIBLE_THEME_WEAK",
            False,
            BlockType.FINAL,
            False,
            0,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(["THEME_WEAK", "BLOCK_THEME"] + reason_codes),
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
            **_data_quality_mapping_fields(data_quality),
        )
    if not latest.ready:
        return ThemeLabBridgeMapping(
            WAIT_DATA,
            "NOT_ELIGIBLE_DATA",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(["DATA_INSUFFICIENT", WAIT_DATA, latest.reason or LATEST_TICK_MISSING] + list(latest.reason_codes) + reason_codes),
            ready_type=WAIT_DATA,
            latest_tick_ready=False,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
            **_data_quality_mapping_fields(data_quality),
        )
    market_wait_status = _market_wait_status(reason_codes)
    if market_wait_status:
        return ThemeLabBridgeMapping(
            market_wait_status,
            "NOT_ELIGIBLE_MARKET",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "B",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe([market_wait_status, "NOT_ELIGIBLE_MARKET"] + reason_codes),
            ready_type=market_wait_status,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
            **_data_quality_mapping_fields(data_quality),
        )
    entry_risk_mapping = _entry_risk_mapping(decision, watch, reason_codes, latest, active_settings)
    if entry_risk_mapping is not None:
        return entry_risk_mapping
    if price_location in OBSERVE_ONLY_LOCATIONS:
        final_status = {
            PriceLocationStatus.CHASE_HIGH: "OBSERVE_CHASE",
            PriceLocationStatus.BREAKOUT_CONTINUATION: "OBSERVE_BREAKOUT_CONTINUATION",
            PriceLocationStatus.VWAP_OVEREXTENDED: "OBSERVE_VWAP_OVEREXTENDED",
        }[price_location]
        return ThemeLabBridgeMapping(
            final_status,
            "NOT_ELIGIBLE_CHASE_OR_EXTENSION",
            False,
            BlockType.NONE,
            False,
            recheck_after_sec,
            "B",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe([final_status, price_location.value] + reason_codes),
            ready_type=OBSERVE,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
        )
    shadow_guard = _shadow_small_entry_guard(decision, watch, shadow_policy, promotion_available=shadow_promotion_available)
    if _realtime_reliability_waits(reliability_guard):
        blocked_shadow_guard = (
            _shadow_guard_with_realtime_reliability_block(shadow_guard, reliability_guard)
            if shadow_guard.get("promoted")
            else shadow_guard
        )
        return _wait_data_for_realtime_reliability(
            decision,
            recheck_after_sec,
            reason_codes,
            latest,
            reliability_guard,
            shadow_small_entry_guard=blocked_shadow_guard,
        )
    risk_off_small_entry = _is_risk_off_small_entry(reason_codes)
    risk_off_details = dict(getattr(decision, "risk_off_entry_details", {}) or {})
    risk_off_observe_only = bool(risk_off_details.get("risk_off_entry_observe_only")) or "OBSERVE_RISK_OFF_SMALL_ENTRY" in {
        str(code).upper() for code in reason_codes
    }
    if risk_off_small_entry and decision.status in {LabGateStatus.READY_SMALL, LabGateStatus.OBSERVE}:
        if not support["ready"]:
            return _wait_data_for_support(
                decision,
                recheck_after_sec,
                reason_codes,
                support,
                latest,
                realtime_reliability_guard=reliability_guard,
            )
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        final_status = "OBSERVE_RISK_OFF_SMALL_ENTRY" if risk_off_observe_only or decision.status == LabGateStatus.OBSERVE else "READY_RISK_OFF_SMALL"
        strategy_eligible = final_status == "READY_RISK_OFF_SMALL"
        ready_type = "READY_RISK_OFF_SMALL" if strategy_eligible else "OBSERVE_RISK_OFF_SMALL_ENTRY"
        return ThemeLabBridgeMapping(
            final_status,
            "BUY_ELIGIBLE_RISK_OFF_SMALL" if strategy_eligible else "NOT_ELIGIBLE_OBSERVE",
            strategy_eligible,
            BlockType.NONE,
            False,
            0 if strategy_eligible else recheck_after_sec,
            "B_RISK_OFF",
            min(75.0, max(65.0, float(decision.price_location_score or 65.0))),
            _dedupe([final_status, ready_type] + support_reason_codes + _realtime_reliability_reason_codes(reliability_guard) + reason_codes),
            ready_type=ready_type,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
            position_size_multiplier=_realtime_reliability_apply_multiplier(
                _positive_float(decision.position_size_multiplier) or 1.0,
                reliability_guard,
            ),
            realtime_reliability_guard=reliability_guard,
        )
    if data_quality.bucket == BUCKET_ENTRY_BLOCKING:
        return _wait_data_for_data_quality(
            decision,
            recheck_after_sec,
            reason_codes,
            data_quality,
            support,
            latest,
            realtime_reliability_guard=reliability_guard,
        )
    if data_quality.bucket == BUCKET_WARMUP_OPTIONAL:
        if data_quality.action == ACTION_ALLOW_EARLY_SMALL_CANDIDATE:
            return _data_quality_early_small_mapping(
                decision,
                recheck_after_sec,
                reason_codes,
                data_quality,
                support,
                latest,
                realtime_reliability_guard=reliability_guard,
            )
        return _wait_data_for_data_quality(
            decision,
            recheck_after_sec,
            reason_codes,
            data_quality,
            support,
            latest,
            realtime_reliability_guard=reliability_guard,
        )
    if shadow_guard.get("promoted"):
        if not _realtime_reliability_allows_shadow_small_entry(reliability_guard, reliability_policy):
            blocked_guard = _shadow_guard_with_realtime_reliability_block(shadow_guard, reliability_guard)
            return _wait_data_for_realtime_reliability(
                decision,
                recheck_after_sec,
                reason_codes,
                latest,
                reliability_guard,
                shadow_small_entry_guard=blocked_guard,
            )
        if not support["ready"]:
            blocked_guard = {
                **shadow_guard,
                "promoted": False,
                "status": "BLOCKED",
                "reason": "SUPPORT_NOT_READY",
            }
            return _wait_data_for_support(
                decision,
                recheck_after_sec,
                reason_codes,
                support,
                latest,
                shadow_small_entry_guard=blocked_guard,
                realtime_reliability_guard=reliability_guard,
            )
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        multiplier = _shadow_position_size_multiplier(shadow_guard, shadow_policy)
        return ThemeLabBridgeMapping(
            READY_SHADOW_SMALL_ENTRY,
            "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY",
            True,
            BlockType.NONE,
            False,
            0,
            "B_SHADOW",
            min(75.0, max(65.0, float(decision.price_location_score or 65.0))),
            _dedupe(
                [
                    READY_SHADOW_SMALL_ENTRY,
                    SHADOW_SMALL_ENTRY_REASON_CODE,
                    "SHADOW_AB_PROMOTED",
                    _shadow_scenario_reason_code(shadow_guard.get("scenario_id")),
                ]
                + support_reason_codes
                + reason_codes
            ),
            ready_type=READY_SHADOW_SMALL_ENTRY,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
            position_size_multiplier=multiplier,
            shadow_small_entry_guard=shadow_guard,
            realtime_reliability_guard=reliability_guard,
        )
    if (
        decision.status == LabGateStatus.READY
        and price_location in READY_PULLBACK_LOCATIONS
        and risk_allowed
    ):
        if not support["ready"]:
            return _wait_data_for_support(
                decision,
                recheck_after_sec,
                reason_codes,
                support,
                latest,
                realtime_reliability_guard=reliability_guard,
            )
        ready_type = READY_FULL
        final_status = "READY_PULLBACK"
        order_eligibility = "BUY_ELIGIBLE_PULLBACK"
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        position_size_multiplier = _realtime_reliability_apply_multiplier(
            _positive_float(decision.position_size_multiplier) or 1.0,
            reliability_guard,
        )
        return ThemeLabBridgeMapping(
            final_status,
            order_eligibility,
            True,
            BlockType.NONE,
            False,
            0,
            "A",
            max(80.0, float(decision.price_location_score or 0.0)),
            _dedupe([final_status, ready_type] + support_reason_codes + _realtime_reliability_reason_codes(reliability_guard) + reason_codes),
            ready_type=ready_type,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
            position_size_multiplier=position_size_multiplier,
            realtime_reliability_guard=reliability_guard,
        )
    if (
        decision.status == LabGateStatus.READY_SMALL
        and price_location in READY_SMALL_PULLBACK_LOCATIONS
        and role in LEADER_ROLES
        and risk_allowed
    ):
        if not support["ready"]:
            return _wait_data_for_support(
                decision,
                recheck_after_sec,
                reason_codes,
                support,
                latest,
                realtime_reliability_guard=reliability_guard,
            )
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        position_size_multiplier = _realtime_reliability_apply_multiplier(
            _positive_float(decision.position_size_multiplier) or 1.0,
            reliability_guard,
        )
        return ThemeLabBridgeMapping(
            "READY_SMALL_PULLBACK",
            "BUY_ELIGIBLE_SMALL_PULLBACK",
            True,
            BlockType.NONE,
            False,
            0,
            "B+",
            max(70.0, float(decision.price_location_score or 0.0)),
            _dedupe(["READY_SMALL_PULLBACK", READY_EARLY_SMALL] + support_reason_codes + _realtime_reliability_reason_codes(reliability_guard) + reason_codes),
            ready_type=READY_EARLY_SMALL,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
            position_size_multiplier=position_size_multiplier,
            realtime_reliability_guard=reliability_guard,
        )
    if decision.status == LabGateStatus.WAIT or decision.risk_level == TradeabilityRiskLevel.SOFT_BLOCK:
        wait_reasons = ["WAIT"] + reason_codes
        final_status = "WAIT"
        if decision.risk_level == TradeabilityRiskLevel.SOFT_BLOCK:
            if _is_late_chase_soft_block(reason_codes):
                final_status = LATE_CHASE_TEMP_WAIT
                wait_reasons.extend(["LATE_CHASE", "SOFT_BLOCK_ONLY", LATE_CHASE_TEMP_WAIT])
            else:
                final_status = RISK_SOFT_BLOCK_TEMP_WAIT
                wait_reasons.extend(["SOFT_BLOCK_ONLY", RISK_SOFT_BLOCK_TEMP_WAIT])
        return ThemeLabBridgeMapping(
            final_status,
            "NOT_ELIGIBLE_WAIT",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "B",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(wait_reasons),
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            shadow_small_entry_guard=shadow_guard,
            realtime_reliability_guard=reliability_guard,
        )
    if decision.status == LabGateStatus.BLOCKED or decision.risk_level == TradeabilityRiskLevel.HARD_BLOCK:
        return ThemeLabBridgeMapping(
            "BLOCKED",
            "NOT_ELIGIBLE_BLOCKED",
            False,
            BlockType.FINAL,
            False,
            0,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(["BLOCKED"] + reason_codes),
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
            realtime_reliability_guard=reliability_guard,
        )
    return ThemeLabBridgeMapping(
        "OBSERVE",
        "NOT_ELIGIBLE_OBSERVE",
        False,
        BlockType.NONE,
        False,
        recheck_after_sec,
        "B",
        max(0.0, float(decision.price_location_score or 0.0)),
        _dedupe(["OBSERVE"] + reason_codes),
        ready_type=OBSERVE,
        latest_tick_ready=latest.ready,
        latest_tick_age_sec=latest.age_sec,
        shadow_small_entry_guard=shadow_guard,
        realtime_reliability_guard=reliability_guard,
    )


def _shadow_small_entry_promotion_mapping(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
    metadata: dict[str, Any],
    support: dict[str, Any],
    latest,
    data_quality: DataQualityClassification,
    settings: StrategyRuntimeSettings,
    evidence: dict[str, Any] | None,
    *,
    promotion_available: bool,
    recheck_after_sec: int,
) -> ThemeLabBridgeMapping | None:
    if not isinstance(evidence, dict) or not evidence.get("available"):
        return None
    reason_codes = _reason_codes(decision, watch, theme)
    trace = {
        "status": decision.status.value,
        "reason_codes": reason_codes + [data_quality.bucket, data_quality.action],
        "stock_role": watch.stock_role.value,
        "price_location_status": decision.price_location_status.value,
        "price_location_readiness": watch.price_location_readiness.value,
        "risk_level": decision.risk_level.value,
        "current_price": metadata.get("current_price") or metadata.get("price") or getattr(watch, "current_price", 0),
        "trade_value": metadata.get("trade_value") or getattr(watch, "trade_value", 0),
        "latest_tick_ready": latest.ready,
        "latest_tick_age_sec": latest.age_sec,
        "support_ready": support.get("ready"),
        "selected_support_source": support.get("source"),
        "selected_support_price": support.get("price"),
        "recent_support_ready": metadata.get("recent_support_ready"),
        "vwap_ready": metadata.get("vwap_ready") or support.get("source") == "vwap",
        "data_quality_bucket": data_quality.bucket,
        "data_quality_action": data_quality.action,
    }
    evaluation = evaluate_shadow_small_entry_promotion(
        gate_decision=decision,
        watch=watch,
        theme=theme,
        trace=trace,
        evidence=evidence,
        settings=settings,
        promotion_available=promotion_available,
    ).to_dict()
    status = str(evaluation.get("promotion_status") or "")
    if status == SHADOW_PROMOTION_NO_EVIDENCE:
        return None
    if status == SHADOW_PROMOTION_PROMOTED:
        return _shadow_promotion_ready_mapping(decision, support, latest, evaluation)
    if status == SHADOW_PROMOTION_OBSERVE_ONLY:
        return _shadow_promotion_observe_mapping(decision, support, latest, evaluation, recheck_after_sec)
    if status == SHADOW_PROMOTION_BLOCKED and str(evaluation.get("rejected_reason") or "") in {
        "SUPPORT_NOT_READY",
        "VWAP_OR_SUPPORT_NOT_READY",
        "LATEST_TICK_NOT_READY",
        "CURRENT_PRICE_NOT_READY",
        "TRADE_VALUE_NOT_READY",
    }:
        return _shadow_promotion_block_mapping(decision, support, latest, evaluation, recheck_after_sec)
    return None


def _shadow_promotion_ready_mapping(
    decision: LabGateDecision,
    support: dict[str, Any],
    latest,
    evaluation: dict[str, Any],
) -> ThemeLabBridgeMapping:
    reason_codes = _dedupe(list(evaluation.get("reason_codes") or []))
    return ThemeLabBridgeMapping(
        READY_SHADOW_SMALL_ENTRY,
        "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED",
        True,
        BlockType.NONE,
        False,
        0,
        "B_SHADOW",
        min(75.0, max(65.0, float(decision.price_location_score or 65.0))),
        reason_codes,
        ready_type=READY_SHADOW_SMALL_ENTRY,
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(support.get("price") or 0),
        selected_support_ready=True,
        support_source_fallback_used=bool(support.get("fallback_used")),
        latest_tick_ready=True,
        latest_tick_age_sec=latest.age_sec,
        position_size_multiplier=float(evaluation.get("position_size_multiplier") or 0.15),
        shadow_small_entry_guard=_shadow_promotion_guard_payload(evaluation),
    )


def _shadow_promotion_observe_mapping(
    decision: LabGateDecision,
    support: dict[str, Any],
    latest,
    evaluation: dict[str, Any],
    recheck_after_sec: int,
) -> ThemeLabBridgeMapping:
    reason_codes = _dedupe(list(evaluation.get("reason_codes") or []))
    return ThemeLabBridgeMapping(
        "WAIT_SHADOW_SMALL_ENTRY_CANDIDATE",
        "NOT_ELIGIBLE_OBSERVE",
        False,
        BlockType.TEMPORARY,
        True,
        recheck_after_sec,
        "B_SHADOW",
        min(70.0, max(55.0, float(decision.price_location_score or 55.0))),
        reason_codes,
        ready_type="WAIT_SHADOW_SMALL_ENTRY_CANDIDATE",
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(support.get("price") or 0),
        selected_support_ready=bool(support.get("ready")),
        support_source_fallback_used=bool(support.get("fallback_used")),
        latest_tick_ready=bool(latest.ready),
        latest_tick_age_sec=latest.age_sec,
        position_size_multiplier=float(evaluation.get("position_size_multiplier") or 0.0),
        shadow_small_entry_guard=_shadow_promotion_guard_payload(evaluation),
        operator_message_ko=str(evaluation.get("operator_message_ko") or ""),
    )


def _shadow_promotion_block_mapping(
    decision: LabGateDecision,
    support: dict[str, Any],
    latest,
    evaluation: dict[str, Any],
    recheck_after_sec: int,
) -> ThemeLabBridgeMapping:
    reason_codes = _dedupe(list(evaluation.get("reason_codes") or []))
    return ThemeLabBridgeMapping(
        "WAIT_SHADOW_SMALL_ENTRY_BLOCKED",
        "NOT_ELIGIBLE_DATA",
        False,
        BlockType.TEMPORARY,
        True,
        recheck_after_sec,
        "C",
        max(0.0, float(decision.price_location_score or 0.0)),
        reason_codes,
        ready_type="WAIT_SHADOW_SMALL_ENTRY_BLOCKED",
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(support.get("price") or 0),
        selected_support_ready=bool(support.get("ready")),
        latest_tick_ready=bool(latest.ready),
        latest_tick_age_sec=latest.age_sec,
        shadow_small_entry_guard=_shadow_promotion_guard_payload(evaluation),
        operator_message_ko=str(evaluation.get("operator_message_ko") or ""),
    )


def _shadow_promotion_guard_payload(evaluation: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(evaluation.get("evidence") or {})
    cancel = dict(evaluation.get("cancel_condition") or {})
    return {
        "enabled": True,
        "candidate": True,
        "promoted": bool(evaluation.get("promoted")),
        "status": str(evaluation.get("promotion_status") or ""),
        "reason": str(evaluation.get("rejected_reason") or "PASS"),
        "promotion_status": str(evaluation.get("promotion_status") or ""),
        "promotion_reason": str(evaluation.get("rejected_reason") or ""),
        "promotion_reason_codes": list(evaluation.get("reason_codes") or []),
        "source_report_id": str(evidence.get("source_report_id") or evidence.get("report_id") or ""),
        "source_report_trade_date": str(evidence.get("source_report_trade_date") or evidence.get("trade_date") or ""),
        "reason_group": str(evidence.get("reason_group") or ""),
        "reason_code": str(evidence.get("reason_code") or ""),
        "sample_count": evidence.get("sample_count"),
        "missed_opportunity_rate": evidence.get("missed_opportunity_rate"),
        "risk_avoided_rate": evidence.get("risk_avoided_rate"),
        "good_block_rate": evidence.get("good_block_rate"),
        "avg_mfe_15m_pct": evidence.get("avg_mfe_15m_pct"),
        "avg_mae_15m_pct": evidence.get("avg_mae_15m_pct"),
        "position_size_multiplier": evaluation.get("position_size_multiplier"),
        "mode": cancel.get("shadow_small_entry_promotion_mode"),
        "order_enabled": cancel.get("shadow_small_entry_promotion_order_enabled"),
        "max_promotions_per_cycle": cancel.get("shadow_small_entry_max_promotions_per_cycle"),
        "max_promotions_per_day": cancel.get("shadow_small_entry_max_promotions_per_day"),
        "operator_message_ko": str(evaluation.get("operator_message_ko") or ""),
        "evaluation": evaluation,
    }


def _entry_risk_mapping(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    reason_codes: list[str],
    latest,
    settings: StrategyRuntimeSettings,
) -> ThemeLabBridgeMapping | None:
    upper_codes = {str(code).upper() for code in reason_codes}
    if not (upper_codes & ENTRY_RISK_CODES):
        return None
    recheck_after_sec = settings.integer("entry_risk_gate.risk_recheck_after_sec", 30)
    if recheck_after_sec <= 0:
        recheck_after_sec = int(decision.recheck_after_sec or 30)
    role = watch.stock_role
    final = bool(upper_codes & ENTRY_RISK_FINAL_CODES)
    temporary = bool(upper_codes & ENTRY_RISK_TEMP_CODES)
    if "UPPER_LIMIT_NEAR" in upper_codes and role not in LEADER_ROLES:
        final = True
    if "VI_UNKNOWN_LIMIT_RISK" in upper_codes and role not in LEADER_ROLES and not temporary:
        final = True
    if final:
        final_status = ENTRY_RISK_FINAL_BLOCK
        return ThemeLabBridgeMapping(
            final_status,
            "NOT_ELIGIBLE_ENTRY_RISK_FINAL",
            False,
            BlockType.FINAL,
            False,
            0,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe([final_status] + reason_codes + [ENTRY_RISK_FINAL_BLOCK]),
            ready_type=final_status,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
        )
    if temporary:
        final_status = ENTRY_RISK_TEMP_WAIT
        return ThemeLabBridgeMapping(
            final_status,
            "NOT_ELIGIBLE_ENTRY_RISK_TEMP",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "B",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe([final_status] + reason_codes + [ENTRY_RISK_TEMP_WAIT]),
            ready_type=final_status,
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
        )
    return None


def _wait_data_for_support(
    decision: LabGateDecision,
    recheck_after_sec: int,
    reason_codes: list[str],
    support: dict[str, Any],
    latest,
    *,
    shadow_small_entry_guard: dict[str, Any] | None = None,
    realtime_reliability_guard: dict[str, Any] | None = None,
) -> ThemeLabBridgeMapping:
    support_reason_codes = list(support.get("reason_codes") or [])
    reason = str(support.get("reason") or SUPPORT_NOT_READY)
    return ThemeLabBridgeMapping(
        WAIT_DATA,
        "NOT_ELIGIBLE_SUPPORT_DATA",
        False,
        BlockType.TEMPORARY,
        True,
        recheck_after_sec,
        "C",
        max(0.0, float(decision.price_location_score or 0.0)),
        _dedupe(["DATA_INSUFFICIENT", WAIT_DATA, WAIT_DATA_SUPPORT_NOT_READY, reason] + support_reason_codes + reason_codes),
        ready_type=WAIT_DATA,
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(float(support.get("price") or 0)),
        selected_support_ready=False,
        selected_support_ready_reason=reason,
        support_source_fallback_used=bool(support.get("fallback_used")),
        latest_tick_ready=latest.ready,
        latest_tick_age_sec=latest.age_sec,
        shadow_small_entry_guard=dict(shadow_small_entry_guard or {}),
        realtime_reliability_guard=dict(realtime_reliability_guard or {}),
    )


def _wait_data_for_data_quality(
    decision: LabGateDecision,
    recheck_after_sec: int,
    reason_codes: list[str],
    data_quality: DataQualityClassification,
    support: dict[str, Any],
    latest,
    *,
    realtime_reliability_guard: dict[str, Any] | None = None,
) -> ThemeLabBridgeMapping:
    reason = data_quality.reason_codes[0] if data_quality.reason_codes else "DATA_INSUFFICIENT"
    return ThemeLabBridgeMapping(
        WAIT_DATA,
        "NOT_ELIGIBLE_DATA",
        False,
        BlockType.TEMPORARY,
        True,
        recheck_after_sec,
        "C",
        max(0.0, float(decision.price_location_score or 0.0)),
        _dedupe(data_quality.reason_codes + [WAIT_DATA, reason] + reason_codes),
        ready_type=WAIT_DATA,
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(float(support.get("price") or 0)),
        selected_support_ready=bool(support.get("ready")),
        selected_support_ready_reason=str(support.get("reason") or ""),
        support_source_fallback_used=bool(support.get("fallback_used")),
        latest_tick_ready=latest.ready,
        latest_tick_age_sec=latest.age_sec,
        realtime_reliability_guard=dict(realtime_reliability_guard or {}),
        **_data_quality_mapping_fields(data_quality),
    )


def _data_quality_early_small_mapping(
    decision: LabGateDecision,
    recheck_after_sec: int,
    reason_codes: list[str],
    data_quality: DataQualityClassification,
    support: dict[str, Any],
    latest,
    *,
    realtime_reliability_guard: dict[str, Any] | None = None,
) -> ThemeLabBridgeMapping:
    support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support.get("fallback_used") else []
    order_enabled = bool(data_quality.early_small_order_enabled)
    final_status = READY_EARLY_SMALL if order_enabled else "WAIT_DATA_EARLY_SMALL_CANDIDATE"
    return ThemeLabBridgeMapping(
        final_status,
        "BUY_ELIGIBLE_EARLY_SMALL_DATA_WARMUP" if order_enabled else "NOT_ELIGIBLE_EARLY_SMALL_OBSERVE_ONLY",
        order_enabled,
        BlockType.NONE if order_enabled else BlockType.TEMPORARY,
        not order_enabled,
        0 if order_enabled else recheck_after_sec,
        "B_EARLY_SMALL" if order_enabled else "B",
        min(75.0, max(65.0, float(decision.price_location_score or 65.0))),
        _dedupe([final_status, "WARMUP_OPTIONAL_ONLY"] + support_reason_codes + data_quality.reason_codes + reason_codes),
        ready_type=READY_EARLY_SMALL if order_enabled else final_status,
        selected_support_source=str(support.get("source") or ""),
        selected_support_price=int(float(support.get("price") or 0)),
        selected_support_ready=bool(support.get("ready")),
        support_source_fallback_used=bool(support.get("fallback_used")),
        latest_tick_ready=latest.ready,
        latest_tick_age_sec=latest.age_sec,
        position_size_multiplier=float(data_quality.early_small_position_size_multiplier if order_enabled else 0.0),
        realtime_reliability_guard=dict(realtime_reliability_guard or {}),
        **_data_quality_mapping_fields(data_quality),
    )


def _bridge_data_quality_classification(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
    *,
    tick: Any,
    metadata: dict[str, Any],
    support: dict[str, Any],
    latest,
    reason_codes: list[str],
    settings: StrategyRuntimeSettings,
    now: datetime,
) -> DataQualityClassification:
    classification = classify_entry_data_quality(
        reason_codes=reason_codes,
        tick=tick,
        metadata=metadata,
        support=support,
        latest_tick_ready=latest.ready,
        latest_tick_reason=latest.reason,
        now=now,
        settings=settings,
    )
    current_price = float(getattr(tick, "price", 0) or 0)
    trade_value = float(getattr(tick, "trade_value", 0) or metadata.get("trade_value") or metadata.get("turnover_krw") or 0)
    theme_status = theme.theme_status.value if theme is not None else ""
    return data_quality_action_for_candidate(
        classification,
        settings=settings,
        status=decision.status.value,
        stock_role=watch.stock_role.value,
        theme_status=theme_status,
        price_location_status=decision.price_location_status.value,
        risk_level=decision.risk_level.value,
        latest_tick_ready=latest.ready,
        current_price=current_price,
        trade_value=trade_value,
        vwap_ready=bool(metadata.get("vwap_ready") or support.get("source") == "vwap" and support.get("ready")),
        recent_support_ready=bool(metadata.get("recent_support_ready") or support.get("source") in {"recent_support_price", "support_price", "recent_swing_low"} and support.get("ready")),
        reason_codes=reason_codes,
        candidate_market_status=_first_market_status(decision, watch),
    )


def _data_quality_mapping_fields(data_quality: DataQualityClassification | None) -> dict[str, Any]:
    if data_quality is None:
        return {}
    return {
        "data_quality_bucket": data_quality.bucket,
        "data_quality_action": data_quality.action,
        "missing_core_fields": list(data_quality.missing_core_fields),
        "missing_entry_fields": list(data_quality.missing_entry_fields),
        "missing_optional_fields": list(data_quality.missing_optional_fields),
        "data_quality_confidence": data_quality.confidence,
        "operator_message_ko": data_quality.operator_message_ko,
        "early_small_candidate": bool(data_quality.early_small_candidate),
        "early_small_order_enabled": bool(data_quality.early_small_order_enabled),
        "early_small_position_size_multiplier": float(data_quality.early_small_position_size_multiplier or 0.0),
        "early_small_rejected_reason": data_quality.early_small_rejected_reason,
    }


def _first_market_status(decision: LabGateDecision, watch: WatchSetSnapshot) -> str:
    for value in (
        getattr(decision, "candidate_market_confirmed_status", ""),
        getattr(decision, "candidate_market_status", ""),
        getattr(watch, "candidate_market_confirmed_status", ""),
        getattr(watch, "candidate_market_status", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _wait_data_for_realtime_reliability(
    decision: LabGateDecision,
    recheck_after_sec: int,
    reason_codes: list[str],
    latest,
    reliability_guard: dict[str, Any],
    *,
    shadow_small_entry_guard: dict[str, Any] | None = None,
) -> ThemeLabBridgeMapping:
    wait_status = str(reliability_guard.get("wait_status") or WAIT_DATA_REALTIME_RELIABILITY_LOW)
    guard_reason_codes = _realtime_reliability_reason_codes(reliability_guard)
    recheck = int(reliability_guard.get("recheck_after_sec") or recheck_after_sec or 30)
    return ThemeLabBridgeMapping(
        wait_status,
        "NOT_ELIGIBLE_DATA",
        False,
        BlockType.TEMPORARY,
        True,
        recheck,
        "C",
        max(0.0, float(decision.price_location_score or 0.0)),
        _dedupe(["DATA_INSUFFICIENT", WAIT_DATA, wait_status] + guard_reason_codes + reason_codes),
        ready_type=WAIT_DATA,
        latest_tick_ready=latest.ready,
        latest_tick_age_sec=latest.age_sec,
        shadow_small_entry_guard=dict(shadow_small_entry_guard or {}),
        realtime_reliability_guard=dict(reliability_guard or {}),
    )


def _is_late_chase_soft_block(reason_codes: Iterable[str]) -> bool:
    return bool({str(code).upper() for code in reason_codes} & LATE_CHASE_SOFT_BLOCK_CODES)


def _is_risk_off_small_entry(reason_codes: Iterable[str]) -> bool:
    return bool({str(code).upper() for code in reason_codes} & RISK_OFF_SMALL_ENTRY_CODES)


def _realtime_reliability_settings(settings: StrategyRuntimeSettings | None) -> dict[str, Any]:
    active_settings = settings or legacy_strategy_runtime_settings()
    raw = dict(active_settings.value("theme_lab_realtime_reliability_gate", {}) or {})
    return {
        "enabled": _shadow_bool(raw.get("enabled"), True),
        "wait_buckets": _shadow_upper_tuple(raw.get("wait_buckets") or ("LOW", "BROKEN")),
        "scale_buckets": _shadow_upper_tuple(raw.get("scale_buckets") or ("MEDIUM",)),
        "medium_position_size_multiplier": max(0.0, min(1.0, _shadow_float(raw.get("medium_position_size_multiplier"), 0.50))),
        "min_ready_score": max(0.0, _shadow_float(raw.get("min_ready_score"), 55.0)),
        "recheck_after_sec": max(1, _shadow_int(raw.get("recheck_after_sec"), 30)),
        "wait_status": str(raw.get("wait_status") or WAIT_DATA_REALTIME_RELIABILITY_LOW).strip() or WAIT_DATA_REALTIME_RELIABILITY_LOW,
        "reason_code": str(raw.get("reason_code") or REALTIME_RELIABILITY_REASON_CODE).strip() or REALTIME_RELIABILITY_REASON_CODE,
        "size_reduced_reason_code": str(raw.get("size_reduced_reason_code") or REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE).strip()
        or REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE,
        "require_high_for_shadow_small_entry": _shadow_bool(raw.get("require_high_for_shadow_small_entry"), True),
        "shadow_allowed_buckets": _shadow_upper_tuple(raw.get("shadow_allowed_buckets") or ("HIGH",)),
        "min_shadow_small_entry_score": max(0.0, _shadow_float(raw.get("min_shadow_small_entry_score"), 90.0)),
        "shadow_block_reason_code": str(raw.get("shadow_block_reason_code") or REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE).strip()
        or REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE,
    }


def _realtime_reliability_guard(metadata: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    reliability = dict(metadata.get("realtime_reliability") or {})
    score = _number_or_none(metadata.get("realtime_reliability_score"))
    if score is None:
        score = _number_or_none(reliability.get("score"))
    bucket = str(metadata.get("realtime_reliability_bucket") or reliability.get("bucket") or "").strip().upper()
    present = score is not None or bool(bucket) or bool(reliability)
    guard = {
        "enabled": bool(policy.get("enabled")),
        "present": present,
        "status": "PASS",
        "reason": "PASS",
        "score": score,
        "bucket": bucket,
        "reasons": list(metadata.get("realtime_reliability_reasons") or reliability.get("reasons") or []),
        "missing_fields": list(metadata.get("realtime_reliability_missing_fields") or reliability.get("missing_fields") or []),
        "field_score": _number_or_none(metadata.get("realtime_reliability_field_score") or reliability.get("field_score")),
        "penalty": _number_or_none(metadata.get("realtime_reliability_penalty") or reliability.get("penalty")),
        "transport_latency_ms": _number_or_none(metadata.get("realtime_transport_latency_ms") or reliability.get("transport_latency_ms")),
        "transport_latency_bucket": str(metadata.get("realtime_transport_latency_bucket") or reliability.get("transport_latency_bucket") or ""),
        "position_size_multiplier": 1.0,
        "wait_status": str(policy.get("wait_status") or WAIT_DATA_REALTIME_RELIABILITY_LOW),
        "recheck_after_sec": int(policy.get("recheck_after_sec") or 30),
        "reason_code": str(policy.get("reason_code") or REALTIME_RELIABILITY_REASON_CODE),
        "size_reduced_reason_code": str(policy.get("size_reduced_reason_code") or REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE),
        "shadow_block_reason_code": str(policy.get("shadow_block_reason_code") or REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE),
    }
    if not guard["enabled"]:
        return {**guard, "status": "DISABLED", "reason": "DISABLED"}
    if not present:
        return {**guard, "status": "NO_SIGNAL", "reason": "NO_SIGNAL"}

    wait_buckets = set(_shadow_upper_tuple(policy.get("wait_buckets") or ()))
    min_ready_score = _shadow_float(policy.get("min_ready_score"), 55.0)
    if bucket in wait_buckets or (score is not None and score < min_ready_score):
        return {**guard, "status": "WAIT", "reason": str(policy.get("reason_code") or REALTIME_RELIABILITY_REASON_CODE)}

    scale_buckets = set(_shadow_upper_tuple(policy.get("scale_buckets") or ()))
    if bucket in scale_buckets:
        multiplier = max(0.0, min(1.0, _shadow_float(policy.get("medium_position_size_multiplier"), 0.50)))
        return {
            **guard,
            "status": "SIZE_REDUCED",
            "reason": str(policy.get("size_reduced_reason_code") or REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE),
            "position_size_multiplier": multiplier,
        }
    return guard


def _realtime_reliability_waits(guard: dict[str, Any]) -> bool:
    return bool(guard.get("enabled")) and bool(guard.get("present")) and str(guard.get("status") or "").upper() == "WAIT"


def _realtime_reliability_reason_codes(guard: dict[str, Any]) -> list[str]:
    if not bool(guard.get("enabled")) or not bool(guard.get("present")):
        return []
    status = str(guard.get("status") or "").upper()
    if status not in {"WAIT", "SIZE_REDUCED", "BLOCKED"}:
        return []
    reason = str(guard.get("reason") or "")
    bucket = str(guard.get("bucket") or "").upper()
    values = [reason]
    if status == "WAIT":
        values.append(str(guard.get("wait_status") or WAIT_DATA_REALTIME_RELIABILITY_LOW))
        values.append(str(guard.get("reason_code") or REALTIME_RELIABILITY_REASON_CODE))
    elif status == "SIZE_REDUCED":
        values.append(str(guard.get("size_reduced_reason_code") or REALTIME_RELIABILITY_SIZE_REDUCED_REASON_CODE))
    elif status == "BLOCKED":
        values.append(str(guard.get("shadow_block_reason_code") or REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE))
    if bucket:
        values.append(f"REALTIME_RELIABILITY_BUCKET_{bucket}")
    latency_bucket = str(guard.get("transport_latency_bucket") or "").upper()
    if latency_bucket in {"DEGRADED", "BROKEN"}:
        values.append(f"REALTIME_TRANSPORT_LATENCY_{latency_bucket}")
    return _dedupe(values)


def _realtime_reliability_apply_multiplier(base_multiplier: float, guard: dict[str, Any]) -> float:
    multiplier = max(0.0, min(1.0, float(base_multiplier or 1.0)))
    if str(guard.get("status") or "").upper() != "SIZE_REDUCED":
        return multiplier
    reliability_multiplier = max(0.0, min(1.0, _shadow_float(guard.get("position_size_multiplier"), multiplier)))
    if reliability_multiplier <= 0:
        return multiplier
    return min(multiplier, reliability_multiplier)


def _realtime_reliability_allows_shadow_small_entry(guard: dict[str, Any], policy: dict[str, Any]) -> bool:
    if not bool(policy.get("require_high_for_shadow_small_entry")):
        return True
    if not bool(guard.get("enabled")) or not bool(guard.get("present")):
        return True
    allowed_buckets = set(_shadow_upper_tuple(policy.get("shadow_allowed_buckets") or ("HIGH",)))
    bucket = str(guard.get("bucket") or "").upper()
    score = _number_or_none(guard.get("score"))
    min_score = _shadow_float(policy.get("min_shadow_small_entry_score"), 90.0)
    if bucket and bucket not in allowed_buckets:
        return False
    if score is not None and score < min_score:
        return False
    return True


def _shadow_guard_with_realtime_reliability_block(
    shadow_guard: dict[str, Any],
    reliability_guard: dict[str, Any],
) -> dict[str, Any]:
    reason = str(reliability_guard.get("shadow_block_reason_code") or REALTIME_RELIABILITY_SHADOW_BLOCK_REASON_CODE)
    return {
        **dict(shadow_guard or {}),
        "promoted": False,
        "status": "BLOCKED",
        "reason": reason,
        "realtime_reliability_gate_status": reliability_guard.get("status", ""),
        "realtime_reliability_score": reliability_guard.get("score"),
        "realtime_reliability_bucket": reliability_guard.get("bucket", ""),
        "realtime_transport_latency_ms": reliability_guard.get("transport_latency_ms"),
        "realtime_transport_latency_bucket": reliability_guard.get("transport_latency_bucket", ""),
    }


def _realtime_reliability_details(guard: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(guard or {})
    return {
        "realtime_reliability_gate": payload,
        "realtime_reliability_gate_enabled": bool(payload.get("enabled")),
        "realtime_reliability_gate_present": bool(payload.get("present")),
        "realtime_reliability_gate_status": str(payload.get("status") or ""),
        "realtime_reliability_gate_reason": str(payload.get("reason") or ""),
        "realtime_reliability_score": payload.get("score"),
        "realtime_reliability_bucket": str(payload.get("bucket") or ""),
        "realtime_reliability_reasons": list(payload.get("reasons") or []),
        "realtime_reliability_missing_fields": list(payload.get("missing_fields") or []),
        "realtime_reliability_field_score": payload.get("field_score"),
        "realtime_reliability_penalty": payload.get("penalty"),
        "realtime_transport_latency_ms": payload.get("transport_latency_ms"),
        "realtime_transport_latency_bucket": str(payload.get("transport_latency_bucket") or ""),
        "realtime_reliability_position_size_multiplier": payload.get("position_size_multiplier"),
    }


def _shadow_small_entry_settings(settings: StrategyRuntimeSettings | None) -> dict[str, Any]:
    active_settings = settings or legacy_strategy_runtime_settings()
    raw = dict(active_settings.value("theme_lab_shadow_small_entry", {}) or {})
    return {
        "enabled": _shadow_bool(raw.get("enabled"), False),
        "allowed_recommendations": _shadow_upper_tuple(raw.get("allowed_recommendations") or ("PROMISING_SHADOW",)),
        "active_scenario_id": str(raw.get("active_scenario_id") or "").strip(),
        "report_trade_date": str(raw.get("report_trade_date") or "").strip(),
        "min_labeled_count": max(0, _shadow_int(raw.get("min_labeled_count"), 10)),
        "min_win_rate_15m": max(0.0, _shadow_float(raw.get("min_win_rate_15m"), 0.55)),
        "max_risk_case_rate_15m": max(0.0, _shadow_float(raw.get("max_risk_case_rate_15m"), 0.15)),
        "min_net_shadow_score": _shadow_float(raw.get("min_net_shadow_score"), 35.0),
        "max_position_size_multiplier": max(0.0, min(1.0, _shadow_float(raw.get("max_position_size_multiplier"), 0.25))),
        "max_promotions_per_cycle": max(0, _shadow_int(raw.get("max_promotions_per_cycle"), 1)),
        "require_support_ready": _shadow_bool(raw.get("require_support_ready"), True),
        "require_latest_tick_ready": _shadow_bool(raw.get("require_latest_tick_ready"), True),
        "reason_code": str(raw.get("reason_code") or SHADOW_SMALL_ENTRY_REASON_CODE).strip() or SHADOW_SMALL_ENTRY_REASON_CODE,
    }


def _shadow_small_entry_policy_from_report(report: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {"active": False, "status": "NO_AB_REPORT", "reason": "NO_AB_REPORT", "scenario": {}}
    ab = report.get("shadow_small_entry_ab")
    if not isinstance(ab, dict):
        return {"active": False, "status": "NO_AB_REPORT", "reason": "NO_AB_REPORT", "scenario": {}}
    rows: list[dict[str, Any]] = []
    seen_scenarios: set[str] = set()
    for source_rows in (ab.get("best_scenarios") or (), ab.get("scenarios") or ()):
        for row in source_rows or ():
            if not isinstance(row, dict):
                continue
            scenario_id = str(row.get("scenario_id") or len(rows))
            if scenario_id in seen_scenarios:
                continue
            seen_scenarios.add(scenario_id)
            rows.append(dict(row))
    if not rows:
        return {"active": False, "status": "NO_SHADOW_SCENARIO", "reason": "NO_SHADOW_SCENARIO", "scenario": {}}

    active_scenario_id = str(config.get("active_scenario_id") or "").strip()
    rejection: tuple[dict[str, Any], str] | None = None
    for row in rows:
        scenario = _shadow_scenario_from_row(row)
        if active_scenario_id and str(scenario.get("scenario_id") or "") != active_scenario_id:
            continue
        passed, reason = _shadow_policy_row_passes(scenario, config)
        if passed:
            return {
                "active": True,
                "status": "SCENARIO_READY",
                "reason": "PASS",
                "scenario": scenario,
            }
        if rejection is None or active_scenario_id:
            rejection = (scenario, reason)
        if active_scenario_id:
            break

    if rejection is not None:
        scenario, reason = rejection
        return {
            "active": False,
            "status": "SCENARIO_POLICY_BLOCKED",
            "reason": reason,
            "scenario": scenario,
        }
    return {
        "active": False,
        "status": "NO_MATCHING_SCENARIO",
        "reason": "NO_MATCHING_SCENARIO",
        "scenario": {},
    }


def _shadow_policy_row_passes(row: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str]:
    recommendation = str(row.get("recommendation") or "").strip().upper()
    allowed_recommendations = set(_shadow_upper_tuple(config.get("allowed_recommendations") or ("PROMISING_SHADOW",)))
    if recommendation not in allowed_recommendations:
        return False, recommendation or "RECOMMENDATION_NOT_ALLOWED"
    if _shadow_int(row.get("labeled_count"), 0) < _shadow_int(config.get("min_labeled_count"), 10):
        return False, "INSUFFICIENT_SAMPLE"
    if _shadow_float(row.get("win_rate_15m"), 0.0) < _shadow_float(config.get("min_win_rate_15m"), 0.55):
        return False, "WIN_RATE_BELOW_THRESHOLD"
    if _shadow_float(row.get("risk_case_rate_15m"), 1.0) > _shadow_float(config.get("max_risk_case_rate_15m"), 0.15):
        return False, "RISK_CASE_RATE_ABOVE_THRESHOLD"
    if _shadow_float(row.get("net_shadow_score"), 0.0) < _shadow_float(config.get("min_net_shadow_score"), 35.0):
        return False, "NET_SHADOW_SCORE_BELOW_THRESHOLD"
    return True, "PASS"


def _shadow_small_entry_guard(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    policy: dict[str, Any] | None,
    *,
    promotion_available: bool = True,
) -> dict[str, Any]:
    active_policy = dict(policy or {})
    scenario = dict(active_policy.get("scenario") or {})
    guard = _shadow_guard_base(active_policy, scenario)
    if not guard["enabled"]:
        return {**guard, "status": "DISABLED", "reason": "DISABLED"}
    if not scenario:
        status = str(active_policy.get("status") or "NO_ACTIVE_SCENARIO")
        return {**guard, "status": status, "reason": str(active_policy.get("reason") or status)}

    basic_passed, basic_reason = _shadow_basic_candidate_passes(decision, watch)
    if not basic_passed:
        return {**guard, "status": "REJECTED", "reason": basic_reason}
    matched, match_reason = _shadow_scenario_matches(decision, watch, scenario)
    if not matched:
        return {**guard, "status": "REJECTED", "reason": match_reason}
    guard["candidate"] = True

    if not bool(active_policy.get("active")):
        status = str(active_policy.get("status") or "BLOCKED")
        return {**guard, "status": "BLOCKED", "reason": str(active_policy.get("reason") or status)}
    if not promotion_available:
        return {**guard, "status": "BLOCKED", "reason": "MAX_PROMOTIONS_PER_CYCLE"}
    return {**guard, "status": "PROMOTED", "reason": "PASS", "promoted": True}


def _shadow_basic_candidate_passes(decision: LabGateDecision, watch: WatchSetSnapshot) -> tuple[bool, str]:
    if decision.status not in {LabGateStatus.WAIT, LabGateStatus.OBSERVE}:
        return False, "STATUS_NOT_WAIT_OR_OBSERVE"
    readiness = _shadow_text(getattr(watch, "price_location_readiness", ""))
    if readiness != "PROVISIONAL" and not bool(getattr(watch, "price_location_provisional", False)):
        return False, "PRICE_LOCATION_NOT_PROVISIONAL"
    return True, "PASS"


def _shadow_scenario_matches(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    scenario: dict[str, Any],
) -> tuple[bool, str]:
    statuses = set(_shadow_upper_tuple(scenario.get("statuses") or ()))
    if statuses and _shadow_text(decision.status) not in statuses:
        return False, "STATUS_NOT_IN_SCENARIO"
    roles = set(_shadow_upper_tuple(scenario.get("roles") or ()))
    if roles and _shadow_text(watch.stock_role) not in roles:
        return False, "ROLE_NOT_IN_SCENARIO"
    if int(getattr(watch, "condition_level", 0) or 0) < _shadow_int(scenario.get("min_condition_level"), 0):
        return False, "CONDITION_LEVEL_BELOW_SCENARIO"
    risks = set(_shadow_upper_tuple(scenario.get("allowed_risks") or ()))
    if risks and _shadow_text(decision.risk_level) not in risks:
        return False, "RISK_NOT_IN_SCENARIO"
    market_status = _shadow_market_status(decision, watch)
    excluded_markets = set(_shadow_upper_tuple(scenario.get("excluded_market_statuses") or ()))
    if market_status and market_status in excluded_markets:
        return False, "MARKET_STATUS_EXCLUDED"
    allowed_markets = set(_shadow_upper_tuple(scenario.get("allowed_market_statuses") or ()))
    if allowed_markets and market_status not in allowed_markets:
        return False, "MARKET_STATUS_NOT_ALLOWED"
    return True, "PASS"


def _shadow_position_size_multiplier(guard: dict[str, Any], policy: dict[str, Any] | None) -> float:
    max_multiplier = _shadow_float((policy or {}).get("max_position_size_multiplier"), 0.25)
    scenario_multiplier = _shadow_float(guard.get("position_size_multiplier"), max_multiplier)
    if scenario_multiplier <= 0:
        scenario_multiplier = max_multiplier
    return max(0.0, min(1.0, scenario_multiplier, max_multiplier))


def _shadow_scenario_reason_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    safe = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    return f"SHADOW_SCENARIO_{safe}" if safe else "SHADOW_SCENARIO"


def _shadow_guard_base(policy: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(policy.get("enabled")),
        "active": bool(policy.get("active")),
        "status": str(policy.get("status") or ""),
        "reason": str(policy.get("reason") or ""),
        "candidate": False,
        "promoted": False,
        "scenario_id": str(scenario.get("scenario_id") or ""),
        "label": str(scenario.get("label") or ""),
        "recommendation": str(scenario.get("recommendation") or ""),
        "position_size_multiplier": _shadow_float(scenario.get("position_size_multiplier"), 0.0),
        "candidate_count": _shadow_int(scenario.get("candidate_count"), 0),
        "labeled_count": _shadow_int(scenario.get("labeled_count"), 0),
        "win_rate_15m": _shadow_float(scenario.get("win_rate_15m"), 0.0),
        "risk_case_rate_15m": _shadow_float(scenario.get("risk_case_rate_15m"), 0.0),
        "net_shadow_score": _shadow_float(scenario.get("net_shadow_score"), 0.0),
    }


def _shadow_scenario_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **dict(row),
        "scenario_id": str(row.get("scenario_id") or "").strip(),
        "label": str(row.get("label") or "").strip(),
        "recommendation": str(row.get("recommendation") or "").strip().upper(),
        "position_size_multiplier": _shadow_float(row.get("position_size_multiplier"), 0.0),
        "candidate_count": _shadow_int(row.get("candidate_count"), 0),
        "labeled_count": _shadow_int(row.get("labeled_count"), 0),
        "win_rate_15m": _shadow_float(row.get("win_rate_15m"), 0.0),
        "risk_case_rate_15m": _shadow_float(row.get("risk_case_rate_15m"), 0.0),
        "net_shadow_score": _shadow_float(row.get("net_shadow_score"), 0.0),
        "statuses": _shadow_upper_tuple(row.get("statuses") or ()),
        "roles": _shadow_upper_tuple(row.get("roles") or ()),
        "min_condition_level": _shadow_int(row.get("min_condition_level"), 0),
        "allowed_risks": _shadow_upper_tuple(row.get("allowed_risks") or ()),
        "allowed_market_statuses": _shadow_upper_tuple(row.get("allowed_market_statuses") or ()),
        "excluded_market_statuses": _shadow_upper_tuple(row.get("excluded_market_statuses") or ()),
    }


def _shadow_market_status(decision: LabGateDecision, watch: WatchSetSnapshot) -> str:
    for item in (
        getattr(decision, "candidate_market_confirmed_status", ""),
        getattr(decision, "candidate_market_status", ""),
        getattr(decision, "candidate_market_raw_status", ""),
        getattr(watch, "candidate_market_confirmed_status", ""),
        getattr(watch, "candidate_market_status", ""),
        getattr(watch, "candidate_market_raw_status", ""),
    ):
        text = _shadow_text(item)
        if text:
            return text
    return ""


def _shadow_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _shadow_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _shadow_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(default)


def _shadow_upper_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        raw_values = value.replace("|", ",").split(",")
    elif isinstance(value, Iterable):
        raw_values = list(value)
    else:
        raw_values = [value]
    return tuple(_dedupe(_shadow_text(item) for item in raw_values if _shadow_text(item)))


def _shadow_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().upper()


def _market_wait_status(reason_codes: Iterable[str]) -> str:
    upper_codes = {str(code).upper() for code in reason_codes}
    if upper_codes & MARKET_CLASSIFICATION_WAIT_CODES and "MARKET_CLASSIFICATION_FALLBACK_STRICT" in upper_codes:
        return "WAIT_MARKET_CLASSIFICATION_UNKNOWN"
    if upper_codes & MARKET_RECOVERY_PENDING_CODES:
        return "WAIT_MARKET_RECOVERY_PENDING"
    if upper_codes & MARKET_CONFIRMATION_PENDING_CODES:
        return "WAIT_MARKET_CONFIRMATION_PENDING"
    if upper_codes & RISK_OFF_SMALL_ENTRY_CODES and not (upper_codes & RISK_OFF_SMALL_ENTRY_BLOCKING_CODES):
        return ""
    if upper_codes & MARKET_RISK_OFF_WAIT_CODES:
        return "WAIT_CANDIDATE_MARKET_RISK_OFF"
    if upper_codes & MARKET_WEAK_WAIT_CODES:
        return "WAIT_CANDIDATE_MARKET_WEAK"
    return ""


def _selected_support_profile(
    price_location: PriceLocationStatus,
    support_candidates: dict[str, float],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    preferred = ["recent_support_price", "support_price", "recent_swing_low", "opening_range", "prev_day_level"]
    if price_location == PriceLocationStatus.VWAP_RECLAIM:
        preferred = ["vwap"] + preferred
    preferred += ["vwap", "base_line_120", "envelope_mid", "day_mid", "ema20_5m"]
    first_existing: dict[str, Any] | None = None
    saw_not_ready = False
    for name in _dedupe(preferred):
        price = support_candidates.get(name)
        if not price or price <= 0:
            continue
        readiness = support_source_readiness(name, metadata)
        profile = {
            "source": name,
            "price": float(price),
            "ready": readiness.ready,
            "reason": readiness.reason,
            "reason_codes": list(readiness.reason_codes),
            "fallback_used": saw_not_ready,
        }
        if first_existing is None:
            first_existing = profile
        if readiness.ready:
            return profile
        saw_not_ready = True
    if first_existing is not None:
        return first_existing
    taxonomy = support_missing_taxonomy(metadata, support_candidates)
    return {
        "source": "",
        "price": 0.0,
        "ready": False,
        "reason": taxonomy,
        "reason_codes": [taxonomy],
        "fallback_used": False,
    }


def _candidate_market_for_candidate(watch: WatchSetSnapshot) -> str:
    side = normalize_market_side(watch.candidate_market)
    return side.value if side != MarketSide.UNKNOWN else ""


def _market_side_fields(decision: LabGateDecision | None, watch: WatchSetSnapshot) -> dict[str, Any]:
    def _value(name: str, default: Any = "") -> Any:
        if decision is not None:
            value = getattr(decision, name, None)
            if value not in (None, "", ()):
                return value
        return getattr(watch, name, default)

    return {
        "candidate_market": str(_value("candidate_market", "")),
        "candidate_market_source": str(_value("candidate_market_source", "")),
        "candidate_market_status": str(_value("candidate_market_status", "")),
        "candidate_market_action": str(_value("candidate_market_action", "")),
        "candidate_index_return_pct": _value("candidate_index_return_pct", None),
        "global_market_status": str(_value("global_market_status", "")),
        "kospi_market_status": str(_value("kospi_market_status", "")),
        "kosdaq_market_status": str(_value("kosdaq_market_status", "")),
        "kospi_return_pct": _value("kospi_return_pct", None),
        "kosdaq_return_pct": _value("kosdaq_return_pct", None),
        "candidate_breadth_pct": _value("candidate_breadth_pct", None),
        "candidate_breadth_ready": bool(_value("candidate_breadth_ready", False)),
        "candidate_breadth_sample_count": int(_value("candidate_breadth_sample_count", 0) or 0),
        "candidate_breadth_source": str(_value("candidate_breadth_source", "")),
        "candidate_valid_quote_ratio": _value("candidate_valid_quote_ratio", None),
        "candidate_breadth_trust_level": str(_value("candidate_breadth_trust_level", "")),
        "candidate_breadth_gate_usable": bool(_value("candidate_breadth_gate_usable", False)),
        "candidate_breadth_diagnostic_only": bool(_value("candidate_breadth_diagnostic_only", False)),
        "candidate_market_raw_status": str(_value("candidate_market_raw_status", "")),
        "candidate_market_confirmed_status": str(_value("candidate_market_confirmed_status", "")),
        "candidate_market_confirmation_pending": bool(_value("candidate_market_confirmation_pending", False)),
        "candidate_market_recovery_pending": bool(_value("candidate_market_recovery_pending", False)),
        "market_side_weak_consecutive_cycles": int(_value("market_side_weak_consecutive_cycles", 0) or 0),
        "market_side_risk_off_consecutive_cycles": int(_value("market_side_risk_off_consecutive_cycles", 0) or 0),
        "market_side_healthy_consecutive_cycles": int(_value("market_side_healthy_consecutive_cycles", 0) or 0),
        "market_side_wait_started_at": str(_value("market_side_wait_started_at", "")),
        "market_side_cycle_id": str(_value("market_side_cycle_id", "")),
        "market_side_last_confirmed_at": str(_value("market_side_last_confirmed_at", "")),
        "market_side_last_recovered_at": str(_value("market_side_last_recovered_at", "")),
        "market_side_recovered_at": str(_value("market_side_recovered_at", "")),
        "market_side_cycles_to_recover": int(_value("market_side_cycles_to_recover", 0) or 0),
        "market_side_recovered_to_ready": bool(_value("market_side_recovered_to_ready", False)),
        "market_side_never_recovered": bool(_value("market_side_never_recovered", False)),
        "market_side_blocked_buy_intent_count": int(_value("market_side_blocked_buy_intent_count", 0) or 0),
        "market_side_recheck_after_sec": int(_value("market_side_recheck_after_sec", 0) or 0),
        "market_confirmation_state_persisted": bool(_value("market_confirmation_state_persisted", False)),
        "market_confirmation_state_restored": bool(_value("market_confirmation_state_restored", False)),
        "market_confirmation_state_restore_reason": str(_value("market_confirmation_state_restore_reason", "")),
        "market_confirmation_state_last_updated_at": str(_value("market_confirmation_state_last_updated_at", "")),
        "market_confirmation_state_age_sec": _value("market_confirmation_state_age_sec", None),
        "market_confirmation_state_version": int(_value("market_confirmation_state_version", 0) or 0),
        "market_confirmation_state_source": str(_value("market_confirmation_state_source", "memory")),
        "market_confirmation_state_reset_reason": str(_value("market_confirmation_state_reset_reason", "")),
        "market_confirmation_state_restore_skipped": bool(_value("market_confirmation_state_restore_skipped", False)),
        "market_confirmation_state_max_restore_age_sec": int(_value("market_confirmation_state_max_restore_age_sec", 0) or 0),
        "market_confirmation_state_expires_at": str(_value("market_confirmation_state_expires_at", "")),
        "market_confirmation_state_reset_count": int(_value("market_confirmation_state_reset_count", 0) or 0),
        "market_confirmation_transition_type": str(_value("market_confirmation_transition_type", "")),
        "market_session_id": str(_value("market_session_id", "")),
        "market_session_type": str(_value("market_session_type", "")),
        "market_trade_date": str(_value("market_trade_date", "")),
        "market_timezone": str(_value("market_timezone", "")),
        "market_schedule_source": str(_value("market_schedule_source", "")),
        "market_schedule_known": bool(_value("market_schedule_known", True)),
        "market_is_regular_session": bool(_value("market_is_regular_session", True)),
        "market_restore_allowed": bool(_value("market_restore_allowed", True)),
        "market_reset_required": bool(_value("market_reset_required", False)),
        "market_reset_reason": str(_value("market_reset_reason", "")),
        "market_session_reason_codes": list(_value("market_session_reason_codes", ())),
        "market_confirmation_metrics": dict(_value("market_confirmation_metrics", {}) or {}),
        "kospi_breadth_pct": _value("kospi_breadth_pct", None),
        "kosdaq_breadth_pct": _value("kosdaq_breadth_pct", None),
        "kospi_breadth_ready": bool(_value("kospi_breadth_ready", False)),
        "kosdaq_breadth_ready": bool(_value("kosdaq_breadth_ready", False)),
        "kospi_breadth_sample_count": int(_value("kospi_breadth_sample_count", 0) or 0),
        "kosdaq_breadth_sample_count": int(_value("kosdaq_breadth_sample_count", 0) or 0),
        "kospi_valid_quote_ratio": _value("kospi_valid_quote_ratio", None),
        "kosdaq_valid_quote_ratio": _value("kosdaq_valid_quote_ratio", None),
        "market_side_reason_codes": list(_value("market_side_reason_codes", ())),
        "market_side_data_quality_flags": list(_value("market_side_data_quality_flags", ())),
    }


def _stock_pullback_details(
    candidate: Candidate,
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    snapshot: IndicatorSnapshot,
    tick_metadata: dict[str, Any],
    mapping: ThemeLabBridgeMapping,
) -> dict[str, Any]:
    support_candidates = _support_candidates(tick_metadata)
    coverage = support_coverage(tick_metadata, support_candidates)
    is_late_chase_wait = mapping.final_gate_status == LATE_CHASE_TEMP_WAIT
    is_risk_soft_block_wait = mapping.final_gate_status == RISK_SOFT_BLOCK_TEMP_WAIT
    is_entry_risk_wait = mapping.final_gate_status == ENTRY_RISK_TEMP_WAIT
    is_entry_risk_final = mapping.final_gate_status == ENTRY_RISK_FINAL_BLOCK
    entry_risk_reason_codes = [code for code in mapping.reason_codes if str(code).upper() in ENTRY_RISK_CODES]
    market_fields = _market_side_fields(decision, watch)
    if mapping.selected_support_source:
        nearest_support = mapping.selected_support_source
        nearest_support_price = mapping.selected_support_price
    else:
        nearest_support, nearest_support_price = _nearest_support(decision.price_location_status, support_candidates)
    support_details = support_metadata(
        source=nearest_support,
        price=nearest_support_price,
        metadata=tick_metadata,
        fallback_used=mapping.support_source_fallback_used,
    ) if nearest_support else {
        "selected_support_source": "",
        "selected_support_price": 0,
        "selected_support_ready": False,
        "selected_support_ready_reason": support_missing_taxonomy(tick_metadata, support_candidates),
        "support_ready": False,
        "support_ready_reason": support_missing_taxonomy(tick_metadata, support_candidates),
        "support_readiness_reason_codes": [support_missing_taxonomy(tick_metadata, support_candidates)],
        "support_source_fallback_used": False,
    }
    if mapping.selected_support_source:
        support_details["selected_support_ready"] = mapping.selected_support_ready
        support_details["support_ready"] = mapping.selected_support_ready
        support_details["selected_support_ready_reason"] = mapping.selected_support_ready_reason
        support_details["support_ready_reason"] = mapping.selected_support_ready_reason
    position_size_multiplier = max(0.0, float(mapping.position_size_multiplier or decision.position_size_multiplier or 0.0))
    if position_size_multiplier <= 0:
        position_size_multiplier = 1.0
    risk_off_details = dict(getattr(decision, "risk_off_entry_details", {}) or {})
    shadow_guard = dict(mapping.shadow_small_entry_guard or {})
    realtime_fields = _realtime_reliability_details(mapping.realtime_reliability_guard)
    risk_off_shadow_entry = dict(risk_off_details.get("risk_off_shadow_entry") or {})
    return {
        "source": SOURCE,
        "profile": candidate.strategy_profile.value if candidate.strategy_profile else StrategyProfile.KOSDAQ_THEME_PROFILE.value,
        "nearest_support": nearest_support,
        "nearest_support_price": nearest_support_price,
        "support_candidates": support_candidates,
        "support_coverage": coverage,
        "support_missing_reason": "" if nearest_support_price else support_missing_taxonomy(tick_metadata, support_candidates),
        "recent_support_price_present": coverage["recent_support_price_present"],
        "vwap_present": coverage["vwap_present"],
        "vwap_ready": coverage["vwap_ready"],
        "base_line_120_ready": bool(tick_metadata.get("base_line_120_ready")),
        "base_line_120_candle_count": int(tick_metadata.get("base_line_120_candle_count") or 0),
        "recent_support_ready": bool(tick_metadata.get("recent_support_ready")),
        "minute_bar_present": coverage["minute_bar_present"],
        "minute_bar_count": coverage["minute_bar_count"],
        "ready_type": mapping.ready_type,
        "data_quality_bucket": mapping.data_quality_bucket,
        "data_quality_action": mapping.data_quality_action,
        "missing_core_fields": list(mapping.missing_core_fields),
        "missing_entry_fields": list(mapping.missing_entry_fields),
        "missing_optional_fields": list(mapping.missing_optional_fields),
        "data_quality_confidence": mapping.data_quality_confidence,
        "data_quality_operator_message_ko": mapping.operator_message_ko,
        "early_small_candidate": bool(mapping.early_small_candidate),
        "early_small_order_enabled": bool(mapping.early_small_order_enabled),
        "early_small_position_size_multiplier": mapping.early_small_position_size_multiplier,
        "early_small_rejected_reason": mapping.early_small_rejected_reason,
        "latest_tick_ready": mapping.latest_tick_ready,
        "latest_tick_age_sec": mapping.latest_tick_age_sec,
        **realtime_fields,
        **support_details,
        "support_reclaimed": decision.price_location_status in READY_PULLBACK_LOCATIONS,
        "support_touched": decision.price_location_status in READY_PULLBACK_LOCATIONS,
        "failed_low_break_rebound": bool(tick_metadata.get("failed_low_break_rebound")),
        "volume_reaccel": bool(tick_metadata.get("volume_reaccel")),
        "chase_risk": decision.price_location_status in OBSERVE_ONLY_LOCATIONS,
        "current_price": snapshot.price,
        "price_location_status": decision.price_location_status.value,
        "price_location_score": decision.price_location_score,
        "price_location_reason_codes": list(decision.price_location_reason_codes),
        "price_location_readiness": watch.price_location_readiness.value,
        "price_location_readiness_reason_codes": list(watch.price_location_readiness_reason_codes),
        "price_location_provisional": bool(watch.price_location_provisional),
        "stock_role": watch.stock_role.value,
        "shadow_small_entry_dry_run": shadow_guard,
        "shadow_small_entry_dry_run_enabled": bool(shadow_guard.get("enabled")),
        "shadow_small_entry_dry_run_candidate": bool(shadow_guard.get("candidate")),
        "shadow_small_entry_dry_run_promoted": bool(shadow_guard.get("promoted")),
        "shadow_small_entry_guard_status": str(shadow_guard.get("status") or ""),
        "shadow_small_entry_guard_reason": str(shadow_guard.get("reason") or ""),
        "shadow_small_entry_scenario_id": str(shadow_guard.get("scenario_id") or ""),
        "shadow_small_entry_recommendation": str(shadow_guard.get("recommendation") or ""),
        "shadow_small_entry_net_score": shadow_guard.get("net_shadow_score"),
        "shadow_small_entry_win_rate_15m": shadow_guard.get("win_rate_15m"),
        "shadow_small_entry_risk_case_rate_15m": shadow_guard.get("risk_case_rate_15m"),
        "shadow_small_entry_labeled_count": shadow_guard.get("labeled_count"),
        "shadow_small_entry_promotion_status": shadow_guard.get("promotion_status") or shadow_guard.get("status", ""),
        "shadow_small_entry_promotion_reason": shadow_guard.get("promotion_reason") or shadow_guard.get("reason", ""),
        "shadow_small_entry_promotion_reason_codes": list(shadow_guard.get("promotion_reason_codes") or []),
        "shadow_small_entry_source_report_id": shadow_guard.get("source_report_id", ""),
        "shadow_small_entry_source_report_trade_date": shadow_guard.get("source_report_trade_date", ""),
        "shadow_small_entry_reason_group": shadow_guard.get("reason_group", ""),
        "shadow_small_entry_reason_code": shadow_guard.get("reason_code", ""),
        "shadow_small_entry_sample_count": shadow_guard.get("sample_count"),
        "shadow_small_entry_missed_opportunity_rate": shadow_guard.get("missed_opportunity_rate"),
        "shadow_small_entry_risk_avoided_rate": shadow_guard.get("risk_avoided_rate"),
        "shadow_small_entry_good_block_rate": shadow_guard.get("good_block_rate"),
        "shadow_small_entry_avg_mfe_15m_pct": shadow_guard.get("avg_mfe_15m_pct"),
        "shadow_small_entry_avg_mae_15m_pct": shadow_guard.get("avg_mae_15m_pct"),
        "shadow_small_entry_position_size_multiplier": shadow_guard.get("position_size_multiplier"),
        "shadow_small_entry_promotion_mode": shadow_guard.get("mode", ""),
        "shadow_small_entry_order_enabled": shadow_guard.get("order_enabled"),
        "shadow_small_entry_max_promotions_per_cycle": shadow_guard.get("max_promotions_per_cycle"),
        "shadow_small_entry_max_promotions_per_day": shadow_guard.get("max_promotions_per_day"),
        "shadow_small_entry_max_promotions_per_code_per_day": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_max_promotions_per_code_per_day") if isinstance(shadow_guard.get("evaluation"), dict) else 1,
        "shadow_small_entry_max_notional_per_day": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_max_notional_per_day") if isinstance(shadow_guard.get("evaluation"), dict) else 300000,
        "shadow_small_entry_cancel_unfilled_after_sec": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_cancel_unfilled_after_sec") if isinstance(shadow_guard.get("evaluation"), dict) else 45,
        "shadow_small_entry_stop_loss_pct": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_stop_loss_pct") if isinstance(shadow_guard.get("evaluation"), dict) else -1.2,
        "shadow_small_entry_take_profit_pct": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_take_profit_pct") if isinstance(shadow_guard.get("evaluation"), dict) else 1.8,
        "shadow_small_entry_max_hold_minutes": shadow_guard.get("evaluation", {}).get("cancel_condition", {}).get("shadow_small_entry_max_hold_minutes") if isinstance(shadow_guard.get("evaluation"), dict) else 20,
        "shadow_small_entry_operator_message_ko": shadow_guard.get("operator_message_ko", ""),
        **market_fields,
        "position_size_multiplier": position_size_multiplier,
        "risk_off_entry": risk_off_details,
        "risk_off_entry_enabled": bool(risk_off_details.get("risk_off_entry_enabled")),
        "risk_off_entry_observe_only": bool(risk_off_details.get("risk_off_entry_observe_only")),
        "risk_off_entry_allowed": bool(risk_off_details.get("risk_off_entry_allowed")),
        "risk_off_entry_rejected_reason": str(risk_off_details.get("risk_off_entry_rejected_reason") or ""),
        "risk_off_entry_failed_checks": list(risk_off_details.get("risk_off_entry_failed_checks") or []),
        "risk_off_entry_passed_checks": list(risk_off_details.get("risk_off_entry_passed_checks") or []),
        "risk_off_entry_blocking_data_flags": list(risk_off_details.get("risk_off_entry_blocking_data_flags") or []),
        "risk_off_shadow_entry": risk_off_shadow_entry,
        "risk_off_relative_strength_pct": risk_off_details.get("risk_off_relative_strength_pct"),
        "risk_off_candidate_breadth_pct": risk_off_details.get("risk_off_candidate_breadth_pct"),
        "risk_off_candidate_index_return_pct": risk_off_details.get("risk_off_candidate_index_return_pct"),
        "risk_off_max_position_size_multiplier": risk_off_details.get("risk_off_max_position_size_multiplier"),
        "risk_off_exit_hint": dict(risk_off_details.get("risk_off_exit_hint") or {}),
        "dynamic_pullback_policy": {"source": SOURCE},
        "late_chase_diagnostics": {
            "source": SOURCE,
            "price_location_status": decision.price_location_status.value,
            "risk_level": decision.risk_level.value,
        },
        "late_chase_level": "soft_block" if is_late_chase_wait or decision.price_location_status in OBSERVE_ONLY_LOCATIONS else "",
        "late_chase_score": 100.0 if is_late_chase_wait or decision.price_location_status in OBSERVE_ONLY_LOCATIONS else 0.0,
        "late_chase_block_type": "temporary_wait" if is_late_chase_wait else ("observe_only" if decision.price_location_status in OBSERVE_ONLY_LOCATIONS else ""),
        "late_chase_recoverable": is_late_chase_wait,
        "late_chase_recheck_after_sec": int(decision.recheck_after_sec or 60) if is_late_chase_wait else 0,
        "late_chase_recovery_conditions": [
            "support_distance_no_longer_excessive",
            "volume_reacceleration_confirmed",
            "not_after_large_candle_or_new_pullback_confirmed",
            "selected_support_ready",
            "latest_tick_ready",
        ] if is_late_chase_wait else [],
        "risk_soft_block": is_risk_soft_block_wait,
        "risk_soft_block_reason_codes": list(mapping.reason_codes) if is_risk_soft_block_wait else [],
        "entry_risk_diagnostics": _entry_risk_details_from_bridge(decision, watch, tick_metadata, mapping),
        "entry_risk_feature_version": "entry_risk_diagnostics_v1",
        "entry_risk_action": "final_block" if is_entry_risk_final else ("temporary_wait" if is_entry_risk_wait else ""),
        "entry_risk_level": "final" if is_entry_risk_final else ("temporary" if is_entry_risk_wait else ""),
        "entry_risk_score": 100.0 if is_entry_risk_final else (60.0 if is_entry_risk_wait else 0.0),
        "entry_risk_reason_codes": entry_risk_reason_codes,
        "entry_risk_recovery_checks": {},
        "vi_status": str(tick_metadata.get("vi_status") or ("ACTIVE" if "VI_ACTIVE" in entry_risk_reason_codes else ("COOLDOWN" if "VI_COOLDOWN" in entry_risk_reason_codes else "UNKNOWN"))),
        "vi_signal_source": str(tick_metadata.get("vi_signal_source") or "unknown"),
        "seconds_since_vi_release": tick_metadata.get("seconds_since_vi_release"),
        "upper_limit_price": tick_metadata.get("upper_limit_price"),
        "upper_limit_gap_pct": tick_metadata.get("upper_limit_gap_pct"),
        "change_rate": tick_metadata.get("change_rate", watch.return_pct),
        "pullback_from_high_pct": tick_metadata.get("pullback_from_high_pct"),
        "leadership_role": watch.stock_role.value,
        "comparison_reason_codes": list(mapping.reason_codes),
    }


def _entry_risk_details_from_bridge(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    tick_metadata: dict[str, Any],
    mapping: ThemeLabBridgeMapping,
) -> dict[str, Any]:
    reason_codes = [code for code in mapping.reason_codes if str(code).upper() in ENTRY_RISK_CODES]
    action = "none"
    level = "none"
    if mapping.final_gate_status == ENTRY_RISK_FINAL_BLOCK:
        action = "final_block"
        level = "final"
    elif mapping.final_gate_status == ENTRY_RISK_TEMP_WAIT:
        action = "temporary_wait"
        level = "temporary"
    return {
        "feature_version": "entry_risk_diagnostics_v1",
        "source": SOURCE,
        "entry_risk_action": action,
        "entry_risk_level": level,
        "entry_risk_score": 100.0 if level == "final" else (60.0 if level == "temporary" else 0.0),
        "entry_risk_reason_codes": reason_codes,
        "entry_risk_recovery_checks": {},
        "vi_status": str(
            tick_metadata.get("vi_status")
            or ("ACTIVE" if "VI_ACTIVE" in reason_codes else ("COOLDOWN" if "VI_COOLDOWN" in reason_codes else "UNKNOWN"))
        ),
        "vi_signal_source": str(tick_metadata.get("vi_signal_source") or "unknown"),
        "seconds_since_vi_release": tick_metadata.get("seconds_since_vi_release"),
        "upper_limit_price": tick_metadata.get("upper_limit_price"),
        "upper_limit_gap_pct": tick_metadata.get("upper_limit_gap_pct"),
        "change_rate": tick_metadata.get("change_rate", watch.return_pct),
        "pullback_from_high_pct": tick_metadata.get("pullback_from_high_pct"),
        "leadership_role": watch.stock_role.value,
        "stock_role": watch.stock_role.value,
        "position_size_multiplier": decision.position_size_multiplier,
    }


def _base_details(
    candidate: Candidate,
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
    mapping: ThemeLabBridgeMapping,
    stock_details: dict[str, Any],
    created_at: str,
    *,
    decision_cycle_id: str = "",
) -> dict[str, Any]:
    theme_name = theme.theme_name if theme is not None else ""
    theme_score = theme.condition_score if theme is not None else 0.0
    support_price = int(stock_details.get("nearest_support_price") or 0)
    metadata = dict(candidate.metadata or {})
    candidate_instance_id = str(metadata.get("candidate_instance_id") or "")
    candidate_generation_seq = metadata.get("candidate_generation_seq", 0)
    generation_reason = str(metadata.get("generation_reason") or metadata.get("candidate_generation_reason") or "")
    decision_cycle_id = str(decision_cycle_id or metadata.get("decision_cycle_id") or "")
    market_fields = _market_side_fields(decision, watch)
    observability = _observability_status_fields(mapping, stock_details, market_fields)
    realtime_fields = _realtime_reliability_details(mapping.realtime_reliability_guard)
    shadow_fields = {
        "shadow_small_entry_dry_run": dict(stock_details.get("shadow_small_entry_dry_run") or {}),
        "shadow_small_entry_dry_run_enabled": bool(stock_details.get("shadow_small_entry_dry_run_enabled")),
        "shadow_small_entry_dry_run_candidate": bool(stock_details.get("shadow_small_entry_dry_run_candidate")),
        "shadow_small_entry_dry_run_promoted": bool(stock_details.get("shadow_small_entry_dry_run_promoted")),
        "shadow_small_entry_guard_status": str(stock_details.get("shadow_small_entry_guard_status") or ""),
        "shadow_small_entry_guard_reason": str(stock_details.get("shadow_small_entry_guard_reason") or ""),
        "shadow_small_entry_scenario_id": str(stock_details.get("shadow_small_entry_scenario_id") or ""),
        "shadow_small_entry_recommendation": str(stock_details.get("shadow_small_entry_recommendation") or ""),
        "shadow_small_entry_net_score": stock_details.get("shadow_small_entry_net_score"),
        "shadow_small_entry_win_rate_15m": stock_details.get("shadow_small_entry_win_rate_15m"),
        "shadow_small_entry_risk_case_rate_15m": stock_details.get("shadow_small_entry_risk_case_rate_15m"),
        "shadow_small_entry_labeled_count": stock_details.get("shadow_small_entry_labeled_count"),
        "shadow_small_entry_promotion_status": stock_details.get("shadow_small_entry_promotion_status", ""),
        "shadow_small_entry_promotion_reason": stock_details.get("shadow_small_entry_promotion_reason", ""),
        "shadow_small_entry_promotion_reason_codes": list(stock_details.get("shadow_small_entry_promotion_reason_codes") or []),
        "shadow_small_entry_source_report_id": stock_details.get("shadow_small_entry_source_report_id", ""),
        "shadow_small_entry_source_report_trade_date": stock_details.get("shadow_small_entry_source_report_trade_date", ""),
        "shadow_small_entry_reason_group": stock_details.get("shadow_small_entry_reason_group", ""),
        "shadow_small_entry_reason_code": stock_details.get("shadow_small_entry_reason_code", ""),
        "shadow_small_entry_sample_count": stock_details.get("shadow_small_entry_sample_count"),
        "shadow_small_entry_missed_opportunity_rate": stock_details.get("shadow_small_entry_missed_opportunity_rate"),
        "shadow_small_entry_risk_avoided_rate": stock_details.get("shadow_small_entry_risk_avoided_rate"),
        "shadow_small_entry_good_block_rate": stock_details.get("shadow_small_entry_good_block_rate"),
        "shadow_small_entry_avg_mfe_15m_pct": stock_details.get("shadow_small_entry_avg_mfe_15m_pct"),
        "shadow_small_entry_avg_mae_15m_pct": stock_details.get("shadow_small_entry_avg_mae_15m_pct"),
        "shadow_small_entry_position_size_multiplier": stock_details.get("shadow_small_entry_position_size_multiplier"),
        "shadow_small_entry_promotion_mode": stock_details.get("shadow_small_entry_promotion_mode", ""),
        "shadow_small_entry_order_enabled": stock_details.get("shadow_small_entry_order_enabled"),
        "shadow_small_entry_max_promotions_per_cycle": stock_details.get("shadow_small_entry_max_promotions_per_cycle"),
        "shadow_small_entry_max_promotions_per_day": stock_details.get("shadow_small_entry_max_promotions_per_day"),
        "shadow_small_entry_max_promotions_per_code_per_day": stock_details.get("shadow_small_entry_max_promotions_per_code_per_day"),
        "shadow_small_entry_max_notional_per_day": stock_details.get("shadow_small_entry_max_notional_per_day"),
        "shadow_small_entry_cancel_unfilled_after_sec": stock_details.get("shadow_small_entry_cancel_unfilled_after_sec"),
        "shadow_small_entry_stop_loss_pct": stock_details.get("shadow_small_entry_stop_loss_pct"),
        "shadow_small_entry_take_profit_pct": stock_details.get("shadow_small_entry_take_profit_pct"),
        "shadow_small_entry_max_hold_minutes": stock_details.get("shadow_small_entry_max_hold_minutes"),
        "shadow_small_entry_operator_message_ko": stock_details.get("shadow_small_entry_operator_message_ko", ""),
    }
    details = {
        "source": SOURCE,
        "candidate_instance_id": candidate_instance_id,
        "candidate_generation_seq": candidate_generation_seq,
        "generation_reason": generation_reason,
        "candidate_generation_reason": generation_reason,
        "previous_candidate_instance_id": metadata.get("previous_candidate_instance_id", ""),
        "previous_seen_at": metadata.get("previous_seen_at", ""),
        "minutes_since_previous_signal": metadata.get("minutes_since_previous_signal"),
        "blocked_generation_reason": metadata.get("blocked_generation_reason", ""),
        "excessive_generation_blocked": bool(metadata.get("excessive_generation_blocked")),
        "decision_cycle_id": decision_cycle_id,
        "theme_id": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
        "theme_name": theme_name,
        "theme_score": theme_score,
        "dynamic_theme_score": theme_score,
        "final_gate_status": mapping.final_gate_status,
        **observability,
        "order_eligibility": mapping.order_eligibility,
        "ready_type": mapping.ready_type,
        "lab_gate_status": decision.status.value,
        "price_location_status": decision.price_location_status.value,
        "price_location_score": decision.price_location_score,
        "price_location_reason_codes": list(decision.price_location_reason_codes),
        "price_location_readiness": watch.price_location_readiness.value,
        "price_location_readiness_reason_codes": list(watch.price_location_readiness_reason_codes),
        "price_location_provisional": bool(watch.price_location_provisional),
        "risk_level": decision.risk_level.value,
        "risk_reason_codes": list(decision.risk_reason_codes),
        **market_fields,
        **realtime_fields,
        "reason_codes": list(mapping.reason_codes),
        "cap_rules_applied": list(mapping.reason_codes),
        "primary_reason_code": mapping.reason_codes[0] if mapping.reason_codes else "",
        "secondary_reason_codes": list(mapping.reason_codes[1:]),
        "sub_status": mapping.final_gate_status,
        "actual_order_allowed": False,
        "entry_plan_created": False,
        "gate_result_key": _bridge_gate_result_key(candidate, watch, mapping),
        "base_price": stock_details.get("current_price", 0),
        "support_price": support_price,
        "nearest_support": stock_details.get("nearest_support", ""),
        "nearest_support_price": support_price,
        "selected_support_source": stock_details.get("selected_support_source", ""),
        "selected_support_price": stock_details.get("selected_support_price", support_price),
        "selected_support_ready": bool(stock_details.get("selected_support_ready")),
        "selected_support_ready_reason": stock_details.get("selected_support_ready_reason", ""),
        "support_ready": bool(stock_details.get("support_ready")),
        "support_ready_reason": stock_details.get("support_ready_reason", ""),
        "support_readiness_reason_codes": list(stock_details.get("support_readiness_reason_codes") or []),
        "support_source_fallback_used": bool(stock_details.get("support_source_fallback_used")),
        "latest_tick_ready": bool(stock_details.get("latest_tick_ready", True)),
        "latest_tick_age_sec": stock_details.get("latest_tick_age_sec"),
        "support_candidates": dict(stock_details.get("support_candidates") or {}),
        "support_coverage": dict(stock_details.get("support_coverage") or {}),
        "support_missing_reason": stock_details.get("support_missing_reason", ""),
        "recent_support_price_present": bool(stock_details.get("recent_support_price_present")),
        "vwap_present": bool(stock_details.get("vwap_present")),
        "vwap_ready": bool(stock_details.get("vwap_ready")),
        "base_line_120_ready": bool(stock_details.get("base_line_120_ready")),
        "base_line_120_candle_count": stock_details.get("base_line_120_candle_count", 0),
        "recent_support_ready": bool(stock_details.get("recent_support_ready")),
        "minute_bar_present": bool(stock_details.get("minute_bar_present")),
        "minute_bar_count": stock_details.get("minute_bar_count", 0),
        "data_quality_bucket": stock_details.get("data_quality_bucket", ""),
        "data_quality_action": stock_details.get("data_quality_action", ""),
        "missing_core_fields": list(stock_details.get("missing_core_fields") or []),
        "missing_entry_fields": list(stock_details.get("missing_entry_fields") or []),
        "missing_optional_fields": list(stock_details.get("missing_optional_fields") or []),
        "data_quality_confidence": stock_details.get("data_quality_confidence", ""),
        "data_quality_operator_message_ko": stock_details.get("data_quality_operator_message_ko", ""),
        "early_small_candidate": bool(stock_details.get("early_small_candidate")),
        "early_small_order_enabled": bool(stock_details.get("early_small_order_enabled")),
        "early_small_position_size_multiplier": stock_details.get("early_small_position_size_multiplier"),
        "early_small_rejected_reason": stock_details.get("early_small_rejected_reason", ""),
        "late_chase_diagnostics": dict(stock_details.get("late_chase_diagnostics") or {}),
        "late_chase_level": stock_details.get("late_chase_level", ""),
        "late_chase_score": stock_details.get("late_chase_score"),
        "late_chase_block_type": stock_details.get("late_chase_block_type", ""),
        "late_chase_recoverable": bool(stock_details.get("late_chase_recoverable")),
        "late_chase_recheck_after_sec": stock_details.get("late_chase_recheck_after_sec", 0),
        "late_chase_recovery_conditions": list(stock_details.get("late_chase_recovery_conditions") or []),
        "risk_soft_block": bool(stock_details.get("risk_soft_block")),
        "risk_soft_block_reason_codes": list(stock_details.get("risk_soft_block_reason_codes") or []),
        "stock_role": watch.stock_role.value,
        "entry_risk_diagnostics": dict(stock_details.get("entry_risk_diagnostics") or {}),
        "entry_risk_feature_version": stock_details.get("entry_risk_feature_version", ""),
        "entry_risk_action": stock_details.get("entry_risk_action", ""),
        "entry_risk_level": stock_details.get("entry_risk_level", ""),
        "entry_risk_score": stock_details.get("entry_risk_score"),
        "entry_risk_reason_codes": list(stock_details.get("entry_risk_reason_codes") or []),
        "entry_risk_recovery_checks": dict(stock_details.get("entry_risk_recovery_checks") or {}),
        "vi_status": stock_details.get("vi_status", "UNKNOWN"),
        "vi_signal_source": stock_details.get("vi_signal_source", "unknown"),
        "seconds_since_vi_release": stock_details.get("seconds_since_vi_release"),
        "upper_limit_price": stock_details.get("upper_limit_price"),
        "upper_limit_gap_pct": stock_details.get("upper_limit_gap_pct"),
        "change_rate": stock_details.get("change_rate"),
        "pullback_from_high_pct": stock_details.get("pullback_from_high_pct"),
        "leadership_role": stock_details.get("leadership_role", watch.stock_role.value),
        "position_size_multiplier": stock_details.get("position_size_multiplier", 1.0),
        **shadow_fields,
        "risk_off_entry": dict(stock_details.get("risk_off_entry") or {}),
        "risk_off_entry_enabled": bool(stock_details.get("risk_off_entry_enabled")),
        "risk_off_entry_observe_only": bool(stock_details.get("risk_off_entry_observe_only")),
        "risk_off_entry_allowed": bool(stock_details.get("risk_off_entry_allowed")),
        "risk_off_entry_rejected_reason": stock_details.get("risk_off_entry_rejected_reason", ""),
        "risk_off_entry_failed_checks": list(stock_details.get("risk_off_entry_failed_checks") or []),
        "risk_off_entry_passed_checks": list(stock_details.get("risk_off_entry_passed_checks") or []),
        "risk_off_entry_blocking_data_flags": list(stock_details.get("risk_off_entry_blocking_data_flags") or []),
        "risk_off_shadow_entry": dict(stock_details.get("risk_off_shadow_entry") or {}),
        "risk_off_relative_strength_pct": stock_details.get("risk_off_relative_strength_pct"),
        "risk_off_candidate_breadth_pct": stock_details.get("risk_off_candidate_breadth_pct"),
        "risk_off_candidate_index_return_pct": stock_details.get("risk_off_candidate_index_return_pct"),
        "risk_off_max_position_size_multiplier": stock_details.get("risk_off_max_position_size_multiplier"),
        "risk_off_exit_hint": dict(stock_details.get("risk_off_exit_hint") or {}),
        "theme_lab_bridge": {
            "source": SOURCE,
            "code": candidate.code,
            "trade_date": candidate.trade_date,
            "candidate_id": candidate.id,
            "candidate_instance_id": candidate_instance_id,
            "candidate_generation_seq": candidate_generation_seq,
            "generation_reason": generation_reason,
            "previous_candidate_instance_id": metadata.get("previous_candidate_instance_id", ""),
            "previous_seen_at": metadata.get("previous_seen_at", ""),
            "minutes_since_previous_signal": metadata.get("minutes_since_previous_signal"),
            "blocked_generation_reason": metadata.get("blocked_generation_reason", ""),
            "excessive_generation_blocked": bool(metadata.get("excessive_generation_blocked")),
            "decision_cycle_id": decision_cycle_id,
            "theme_id": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
            "theme_name": theme_name,
            "lab_gate_status": decision.status.value,
            "final_gate_status": mapping.final_gate_status,
            **observability,
            "order_eligibility": mapping.order_eligibility,
            "ready_type": mapping.ready_type,
            "price_location_status": decision.price_location_status.value,
            "price_location_readiness": watch.price_location_readiness.value,
            "price_location_readiness_reason_codes": list(watch.price_location_readiness_reason_codes),
            "price_location_provisional": bool(watch.price_location_provisional),
            "risk_level": decision.risk_level.value,
            "risk_reason_codes": list(decision.risk_reason_codes),
            **market_fields,
            **realtime_fields,
            "reason_codes": list(mapping.reason_codes),
            "support_price": support_price,
            "selected_support_source": stock_details.get("selected_support_source", ""),
            "selected_support_ready": bool(stock_details.get("selected_support_ready")),
            "support_ready_reason": stock_details.get("support_ready_reason", ""),
            "support_missing_reason": stock_details.get("support_missing_reason", ""),
            "support_coverage": dict(stock_details.get("support_coverage") or {}),
            "support_reclaimed": bool(stock_details.get("support_reclaimed")),
            "recent_support_price_present": bool(stock_details.get("recent_support_price_present")),
            "vwap_present": bool(stock_details.get("vwap_present")),
            "vwap_ready": bool(stock_details.get("vwap_ready")),
            "base_line_120_ready": bool(stock_details.get("base_line_120_ready")),
            "base_line_120_candle_count": stock_details.get("base_line_120_candle_count", 0),
            "recent_support_ready": bool(stock_details.get("recent_support_ready")),
            "minute_bar_present": bool(stock_details.get("minute_bar_present")),
            "minute_bar_count": stock_details.get("minute_bar_count", 0),
            "data_quality_bucket": stock_details.get("data_quality_bucket", ""),
            "data_quality_action": stock_details.get("data_quality_action", ""),
            "missing_core_fields": list(stock_details.get("missing_core_fields") or []),
            "missing_entry_fields": list(stock_details.get("missing_entry_fields") or []),
            "missing_optional_fields": list(stock_details.get("missing_optional_fields") or []),
            "data_quality_confidence": stock_details.get("data_quality_confidence", ""),
            "data_quality_operator_message_ko": stock_details.get("data_quality_operator_message_ko", ""),
            "early_small_candidate": bool(stock_details.get("early_small_candidate")),
            "early_small_order_enabled": bool(stock_details.get("early_small_order_enabled")),
            "early_small_position_size_multiplier": stock_details.get("early_small_position_size_multiplier"),
            "early_small_rejected_reason": stock_details.get("early_small_rejected_reason", ""),
            "late_chase_level": stock_details.get("late_chase_level", ""),
            "late_chase_block_type": stock_details.get("late_chase_block_type", ""),
            "late_chase_recoverable": bool(stock_details.get("late_chase_recoverable")),
            "late_chase_recheck_after_sec": stock_details.get("late_chase_recheck_after_sec", 0),
            "risk_soft_block": bool(stock_details.get("risk_soft_block")),
            "risk_soft_block_reason_codes": list(stock_details.get("risk_soft_block_reason_codes") or []),
            "entry_risk_diagnostics": dict(stock_details.get("entry_risk_diagnostics") or {}),
            "entry_risk_feature_version": stock_details.get("entry_risk_feature_version", ""),
            "entry_risk_action": stock_details.get("entry_risk_action", ""),
            "entry_risk_level": stock_details.get("entry_risk_level", ""),
            "entry_risk_score": stock_details.get("entry_risk_score"),
            "entry_risk_reason_codes": list(stock_details.get("entry_risk_reason_codes") or []),
            "entry_risk_recovery_checks": dict(stock_details.get("entry_risk_recovery_checks") or {}),
            "vi_status": stock_details.get("vi_status", "UNKNOWN"),
            "vi_signal_source": stock_details.get("vi_signal_source", "unknown"),
            "seconds_since_vi_release": stock_details.get("seconds_since_vi_release"),
            "upper_limit_price": stock_details.get("upper_limit_price"),
            "upper_limit_gap_pct": stock_details.get("upper_limit_gap_pct"),
            "change_rate": stock_details.get("change_rate"),
            "pullback_from_high_pct": stock_details.get("pullback_from_high_pct"),
            "leadership_role": stock_details.get("leadership_role", watch.stock_role.value),
            "stock_role": watch.stock_role.value,
            "position_size_multiplier": stock_details.get("position_size_multiplier", 1.0),
            **shadow_fields,
            "risk_off_entry": dict(stock_details.get("risk_off_entry") or {}),
            "risk_off_entry_enabled": bool(stock_details.get("risk_off_entry_enabled")),
            "risk_off_entry_observe_only": bool(stock_details.get("risk_off_entry_observe_only")),
            "risk_off_entry_allowed": bool(stock_details.get("risk_off_entry_allowed")),
            "risk_off_entry_rejected_reason": stock_details.get("risk_off_entry_rejected_reason", ""),
            "risk_off_entry_failed_checks": list(stock_details.get("risk_off_entry_failed_checks") or []),
            "risk_off_entry_passed_checks": list(stock_details.get("risk_off_entry_passed_checks") or []),
            "risk_off_entry_blocking_data_flags": list(stock_details.get("risk_off_entry_blocking_data_flags") or []),
            "risk_off_shadow_entry": dict(stock_details.get("risk_off_shadow_entry") or {}),
            "risk_off_relative_strength_pct": stock_details.get("risk_off_relative_strength_pct"),
            "risk_off_candidate_breadth_pct": stock_details.get("risk_off_candidate_breadth_pct"),
            "risk_off_candidate_index_return_pct": stock_details.get("risk_off_candidate_index_return_pct"),
            "risk_off_max_position_size_multiplier": stock_details.get("risk_off_max_position_size_multiplier"),
            "risk_off_exit_hint": dict(stock_details.get("risk_off_exit_hint") or {}),
        },
    }
    details = standardize_details(
        details,
        mapping.reason_codes,
        passed=mapping.strategy_eligible,
        score=mapping.final_score,
        created_at=created_at,
        legacy_result=False,
        new_result=mapping.strategy_eligible,
        legacy_score=0.0,
        new_score=mapping.final_score,
    )
    return attach_trade_setup_details(details)


def _observability_status_fields(
    mapping: ThemeLabBridgeMapping,
    stock_details: dict[str, Any],
    market_fields: dict[str, Any],
) -> dict[str, Any]:
    reason_codes = set(str(reason) for reason in mapping.reason_codes)
    support_reason = str(stock_details.get("support_ready_reason") or stock_details.get("support_missing_reason") or "")
    price_location = str(stock_details.get("price_location_status") or "")
    late_chase_level = str(stock_details.get("late_chase_level") or "")
    late_chase_block_type = str(stock_details.get("late_chase_block_type") or "")
    market_pending = bool(market_fields.get("candidate_market_confirmation_pending"))
    market_recovery = bool(market_fields.get("candidate_market_recovery_pending"))
    market_status = str(market_fields.get("candidate_market_confirmed_status") or market_fields.get("candidate_market_status") or "")
    restore_reason = str(market_fields.get("market_confirmation_state_restore_reason") or "")
    reset_reason = str(market_fields.get("market_confirmation_state_reset_reason") or "")
    session_type = str(market_fields.get("market_session_type") or "")

    normalized = str(mapping.final_gate_status or mapping.final_grade or "")
    display = normalized
    risk_off_small_entry_status = normalized in {"READY_RISK_OFF_SMALL", "OBSERVE_RISK_OFF_SMALL_ENTRY"}
    if stock_details.get("chase_risk") or "CHASE_RISK" in reason_codes:
        normalized = "CHASE_RISK_BLOCKED"
        display = "CHASE_RISK_BLOCKED"
    elif mapping.final_gate_status == WAIT_DATA_REALTIME_RELIABILITY_LOW or REALTIME_RELIABILITY_REASON_CODE in reason_codes:
        normalized = WAIT_DATA_REALTIME_RELIABILITY_LOW
        display = WAIT_DATA_REALTIME_RELIABILITY_LOW
    elif late_chase_level == "soft_block" or mapping.final_gate_status == LATE_CHASE_TEMP_WAIT:
        normalized = "LATE_CHASE_TEMP_WAIT"
        display = "LATE_CHASE_TEMP_WAIT"
    elif mapping.final_gate_status in {ENTRY_RISK_TEMP_WAIT, ENTRY_RISK_FINAL_BLOCK}:
        normalized = str(mapping.final_gate_status)
        display = str(mapping.final_gate_status)
    elif restore_reason == "MARKET_CONFIRMATION_STATE_DB_ERROR" or "MARKET_CONFIRMATION_STATE_CONSERVATIVE_FALLBACK" in reason_codes:
        normalized = "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"
        display = "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"
    elif market_recovery:
        normalized = "WAIT_MARKET_RECOVERY_PENDING"
        display = "WAIT_MARKET_RECOVERY_PENDING"
    elif market_pending:
        normalized = "WAIT_MARKET_CONFIRMATION_PENDING"
        display = "WAIT_MARKET_CONFIRMATION_PENDING"
    elif risk_off_small_entry_status:
        normalized = str(mapping.final_gate_status)
        display = str(mapping.final_gate_status)
    elif market_status == "RISK_OFF":
        normalized = "WAIT_CANDIDATE_MARKET_RISK_OFF"
        display = "WAIT_CANDIDATE_MARKET_RISK_OFF"
    elif market_status == "WEAK":
        normalized = "WAIT_CANDIDATE_MARKET_WEAK"
        display = "WAIT_CANDIDATE_MARKET_WEAK"
    elif not bool(stock_details.get("support_ready", True)) or support_reason:
        normalized = "WAIT_DATA_SUPPORT_NOT_READY"
        display = "WAIT_DATA_SUPPORT_NOT_READY"
    elif not bool(stock_details.get("latest_tick_ready", True)):
        normalized = "WAIT_DATA_LATEST_TICK_STALE"
        display = "WAIT_DATA_LATEST_TICK_STALE"

    return {
        "normalized_status": normalized,
        "display_status": display,
        "display_status_source": "themelab_observability",
        "display_status_reason": str(stock_details.get("realtime_reliability_gate_reason") or "")
        or support_reason
        or restore_reason
        or reset_reason
        or late_chase_block_type
        or price_location,
        "late_chase_temp_wait": normalized == "LATE_CHASE_TEMP_WAIT",
        "chase_risk_reason": "CHASE_RISK" if normalized == "CHASE_RISK_BLOCKED" else "",
        "price_location_block_reason": price_location if normalized in {"CHASE_RISK_BLOCKED", "LATE_CHASE_TEMP_WAIT"} else "",
        "market_wait_reason": normalized if normalized.startswith("WAIT_MARKET") or normalized.startswith("WAIT_CANDIDATE_MARKET") else "",
        "session_boundary_status": session_type,
    }


def themelab_entry_idempotency_key(
    *,
    trade_date: str,
    code: str,
    candidate_id: int | None,
    order_phase: str,
    leg_index: int | None,
    candidate_instance_id: str = "",
) -> str:
    identity = str(candidate_instance_id or candidate_id or "")
    return f"{SOURCE}:{trade_date}:{normalize_code(code)}:{identity}:{order_phase}:{leg_index or ''}"


def _candidate_bridge_metadata(watch: WatchSetSnapshot, theme: ThemeConditionSnapshot | None) -> dict[str, Any]:
    return {
        "theme_lab_bridge_source": SOURCE,
        "theme_lab_primary_theme": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
        "theme_lab_themes": list(watch.themes),
        "theme_lab_stock_role": watch.stock_role.value,
        "theme_lab_condition_level": int(watch.condition_level or 0),
        "theme_lab_theme_status": theme.theme_status.value if theme is not None else "",
        "theme_lab_last_seen": watch.calculated_at,
        **_market_side_fields(None, watch),
    }


def _candidate_event(
    event_type: str,
    candidate: Candidate,
    from_state: CandidateState | None,
    to_state: CandidateState | None,
    reason: str,
    payload: dict[str, Any],
    created_at: str,
) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=candidate.id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        source=CandidateSourceType.THEME_WATCH,
        reason=reason,
        created_at=created_at,
        payload={**dict(payload or {}), "source": CandidateSourceType.THEME_WATCH.value},
    )


def _strategy_profile_from_watch(watch: WatchSetSnapshot, metadata: dict[str, Any]) -> StrategyProfile:
    raw = str(metadata.get("strategy_profile") or metadata.get("profile") or "")
    if raw:
        try:
            return StrategyProfile(raw)
        except ValueError:
            pass
    market = normalize_market_side(metadata.get("market") or metadata.get("market_type") or watch.candidate_market)
    if market == MarketSide.KOSPI:
        return StrategyProfile.KOSPI_LEADER_PROFILE
    if watch.stock_role in LEADER_ROLES:
        return StrategyProfile.KOSDAQ_THEME_PROFILE
    return StrategyProfile.KOSDAQ_THEME_PROFILE


def _reason_codes(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
) -> list[str]:
    values: list[str] = []
    values.extend(str(code) for code in decision.reason_codes)
    values.extend(str(code) for code in decision.risk_reason_codes)
    values.extend(str(code) for code in decision.price_location_reason_codes)
    if decision.blocked_reason:
        values.append(str(decision.blocked_reason))
    values.append(decision.status.value)
    values.append(decision.price_location_status.value)
    values.append(decision.risk_level.value)
    values.append(watch.stock_role.value)
    if theme is not None:
        values.append(theme.theme_status.value)
        values.extend(str(code) for code in theme.data_quality_flags)
    values.extend(str(code) for code in watch.price_location_data_quality_flags)
    return normalize_reason_codes(values)


def _has_any(values: Iterable[str], targets: set[str]) -> bool:
    upper_values = {str(value).upper() for value in values}
    return bool(upper_values & targets)


def _is_theme_weak(reason_codes: list[str], theme: ThemeConditionSnapshot | None) -> bool:
    if _has_any(reason_codes, THEME_WEAK_CODES):
        return True
    return theme is not None and theme.theme_status == ThemeLabThemeStatus.WEAK_THEME


def _support_candidates(metadata: dict[str, Any]) -> dict[str, float]:
    candidates: dict[str, float] = {}
    for name in (
        "recent_support_price",
        "support_price",
        "recent_swing_low",
        "opening_range",
        "opening_range_price",
        "opening_range_low",
        "prev_day_level",
        "vwap",
        "base_line_120",
        "envelope_mid",
        "day_mid",
        "ema20_5m",
        "manual_support",
    ):
        value = _positive_float(metadata.get(name))
        if value > 0:
            canonical_name = "opening_range" if name in {"opening_range_price", "opening_range_low"} else name
            candidates.setdefault(canonical_name, value)
    return candidates


def _nearest_support(
    price_location: PriceLocationStatus,
    support_candidates: dict[str, float],
) -> tuple[str, int]:
    preferred = ["recent_support_price", "support_price", "recent_swing_low", "opening_range", "prev_day_level"]
    if price_location == PriceLocationStatus.VWAP_RECLAIM:
        preferred = ["vwap"] + preferred
    for name in preferred + ["vwap", "base_line_120", "envelope_mid", "day_mid", "ema20_5m"]:
        value = support_candidates.get(name)
        if value and value > 0:
            return name, int(value)
    return "", 0


def _bridge_gate_result_key(candidate: Candidate, watch: WatchSetSnapshot, mapping: ThemeLabBridgeMapping) -> str:
    theme_id = str(watch.primary_theme or (watch.themes[0] if watch.themes else ""))
    return f"{SOURCE}:{candidate.id}:{candidate.code}:{theme_id}:{mapping.final_gate_status}"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _float_or_none(value: Any) -> float | None:
    number = _positive_float(value)
    return number if number > 0 else None


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0
