from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from trading.strategy.candidates import CandidateLifecycle, is_valid_stock_code, normalize_code
from trading.strategy.candidate_identity import CandidateGenerationConfig, CandidateInstanceDecision, build_candidate_instance_id, decide_candidate_instance, identity_metadata
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
from trading.strategy.support_readiness import (
    BASE_LINE_120,
    EARLY_READY_SUPPORT_SOURCES,
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
LATE_CHASE_SOFT_BLOCK_CODES = {"HIGH_CHASE_RISK", "LATE_CHASE", "CHASE_RISK"}
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
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.default_ttl_minutes = max(1, int(default_ttl_minutes or 30))
        self.settings = settings or legacy_strategy_runtime_settings()
        self.generation_config = generation_config or CandidateGenerationConfig.from_env()

    def build(
        self,
        result: ThemeLabFlowResult,
        *,
        trade_date: str,
        now: datetime,
    ) -> ThemeLabBridgeBuildResult:
        warnings: list[str] = []
        candidate_saves = 0
        decision_cycle_id = f"{SOURCE}:{trade_date}:{now.replace(microsecond=0).isoformat()}"
        watch_by_code = {normalize_code(item.symbol): item for item in result.watchset}
        themes_by_id = {str(item.theme_id): item for item in result.themes}
        gate_results: list[GatePipelineResult] = []

        for decision in result.gate_decisions:
            code = normalize_code(decision.symbol)
            if not is_valid_stock_code(code):
                warnings.append(f"THEME_LAB_BRIDGE_INVALID_CODE:{decision.symbol}")
                continue
            watch = watch_by_code.get(code)
            if watch is None:
                warnings.append(f"THEME_LAB_BRIDGE_WATCHSET_MISSING:{code}")
                continue
            theme_id = str(watch.primary_theme or (watch.themes[0] if watch.themes else ""))
            theme = themes_by_id.get(theme_id)
            candidate, saved = self._ensure_candidate(
                code,
                watch,
                theme,
                trade_date=trade_date,
                now=now,
                decision_cycle_id=decision_cycle_id,
            )
            if saved:
                candidate_saves += 1
            if candidate.id is None:
                warnings.append(f"THEME_LAB_BRIDGE_CANDIDATE_ID_MISSING:{code}")
                continue
            gate_results.append(self._gate_result(candidate, decision, watch, theme, now, decision_cycle_id=decision_cycle_id))

        return ThemeLabBridgeBuildResult(
            gate_results=gate_results,
            candidate_save_count=candidate_saves,
            warnings=warnings,
        )

    def _ensure_candidate(
        self,
        code: str,
        watch: WatchSetSnapshot,
        theme: ThemeConditionSnapshot | None,
        *,
        trade_date: str,
        now: datetime,
        decision_cycle_id: str,
    ) -> tuple[Candidate, bool]:
        now_text = now.replace(microsecond=0).isoformat()
        expires_at = (now.replace(microsecond=0) + timedelta(minutes=self.default_ttl_minutes)).isoformat()
        existing = self.db.load_candidate(trade_date, code)
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
                **_market_side_fields(decision, watch),
            },
        )

    def _latest_tick_metadata(self, code: str) -> dict[str, Any]:
        tick = self.market_data.latest_tick(code) if self.market_data is not None else None
        return dict(tick.metadata or {}) if tick is not None else {}


def _map_decision(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
    *,
    tick_metadata: dict[str, Any] | None = None,
    tick: Any = None,
    now: datetime | None = None,
    settings: StrategyRuntimeSettings | None = None,
) -> ThemeLabBridgeMapping:
    metadata = dict(tick_metadata or {})
    active_settings = settings or legacy_strategy_runtime_settings()
    reason_codes = _reason_codes(decision, watch, theme)
    recheck_after_sec = int(decision.recheck_after_sec or 60)
    risk_allowed = decision.risk_level in BUY_ALLOWED_RISKS
    price_location = decision.price_location_status
    role = watch.stock_role
    latest = latest_tick_readiness(tick, now or datetime.now(), active_settings)

    if _has_any(reason_codes, DATA_INSUFFICIENT_CODES):
        return ThemeLabBridgeMapping(
            "WAIT_DATA",
            "NOT_ELIGIBLE_DATA",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "C",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(["DATA_INSUFFICIENT", "WAIT_DATA"] + reason_codes),
            latest_tick_ready=latest.ready,
            latest_tick_age_sec=latest.age_sec,
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
        )
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
        )
    support = _selected_support_profile(price_location, _support_candidates(metadata), metadata)
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
            )
        base_line_ready = support_source_readiness(BASE_LINE_120, metadata).ready
        ready_type = READY_FULL
        final_status = "READY_PULLBACK"
        order_eligibility = "BUY_ELIGIBLE_PULLBACK"
        if not base_line_ready and support["source"] in EARLY_READY_SUPPORT_SOURCES:
            ready_type = READY_EARLY_SMALL
            final_status = READY_EARLY_SMALL
            order_eligibility = "BUY_ELIGIBLE_EARLY_SMALL"
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        return ThemeLabBridgeMapping(
            final_status,
            order_eligibility,
            True,
            BlockType.NONE,
            False,
            0,
            "A",
            max(80.0, float(decision.price_location_score or 0.0)),
            _dedupe([final_status, ready_type] + support_reason_codes + reason_codes),
            ready_type=ready_type,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
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
            )
        support_reason_codes = [SUPPORT_SOURCE_FALLBACK_USED] if support["fallback_used"] else []
        return ThemeLabBridgeMapping(
            "READY_SMALL_PULLBACK",
            "BUY_ELIGIBLE_SMALL_PULLBACK",
            True,
            BlockType.NONE,
            False,
            0,
            "B+",
            max(70.0, float(decision.price_location_score or 0.0)),
            _dedupe(["READY_SMALL_PULLBACK", READY_EARLY_SMALL] + support_reason_codes + reason_codes),
            ready_type=READY_EARLY_SMALL,
            selected_support_source=support["source"],
            selected_support_price=int(support["price"]),
            selected_support_ready=True,
            support_source_fallback_used=bool(support["fallback_used"]),
            latest_tick_ready=True,
            latest_tick_age_sec=latest.age_sec,
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
    )


def _wait_data_for_support(
    decision: LabGateDecision,
    recheck_after_sec: int,
    reason_codes: list[str],
    support: dict[str, Any],
    latest,
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
    )


def _is_late_chase_soft_block(reason_codes: Iterable[str]) -> bool:
    return bool({str(code).upper() for code in reason_codes} & LATE_CHASE_SOFT_BLOCK_CODES)


def _market_wait_status(reason_codes: Iterable[str]) -> str:
    upper_codes = {str(code).upper() for code in reason_codes}
    if upper_codes & MARKET_CLASSIFICATION_WAIT_CODES and "MARKET_CLASSIFICATION_FALLBACK_STRICT" in upper_codes:
        return "WAIT_MARKET_CLASSIFICATION_UNKNOWN"
    if upper_codes & MARKET_RECOVERY_PENDING_CODES:
        return "WAIT_MARKET_RECOVERY_PENDING"
    if upper_codes & MARKET_CONFIRMATION_PENDING_CODES:
        return "WAIT_MARKET_CONFIRMATION_PENDING"
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
    position_size_multiplier = max(0.0, float(decision.position_size_multiplier or 0.0))
    if position_size_multiplier <= 0:
        position_size_multiplier = 1.0
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
        "minute_bar_present": coverage["minute_bar_present"],
        "minute_bar_count": coverage["minute_bar_count"],
        "ready_type": mapping.ready_type,
        "latest_tick_ready": mapping.latest_tick_ready,
        "latest_tick_age_sec": mapping.latest_tick_age_sec,
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
        "stock_role": watch.stock_role.value,
        **market_fields,
        "position_size_multiplier": position_size_multiplier,
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
        "comparison_reason_codes": list(mapping.reason_codes),
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
        "order_eligibility": mapping.order_eligibility,
        "ready_type": mapping.ready_type,
        "lab_gate_status": decision.status.value,
        "price_location_status": decision.price_location_status.value,
        "price_location_score": decision.price_location_score,
        "price_location_reason_codes": list(decision.price_location_reason_codes),
        "risk_level": decision.risk_level.value,
        "risk_reason_codes": list(decision.risk_reason_codes),
        **market_fields,
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
        "minute_bar_present": bool(stock_details.get("minute_bar_present")),
        "minute_bar_count": stock_details.get("minute_bar_count", 0),
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
        "position_size_multiplier": stock_details.get("position_size_multiplier", 1.0),
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
            "order_eligibility": mapping.order_eligibility,
            "ready_type": mapping.ready_type,
            "price_location_status": decision.price_location_status.value,
            "risk_level": decision.risk_level.value,
            "risk_reason_codes": list(decision.risk_reason_codes),
            **market_fields,
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
            "minute_bar_present": bool(stock_details.get("minute_bar_present")),
            "minute_bar_count": stock_details.get("minute_bar_count", 0),
            "late_chase_level": stock_details.get("late_chase_level", ""),
            "late_chase_block_type": stock_details.get("late_chase_block_type", ""),
            "late_chase_recoverable": bool(stock_details.get("late_chase_recoverable")),
            "late_chase_recheck_after_sec": stock_details.get("late_chase_recheck_after_sec", 0),
            "risk_soft_block": bool(stock_details.get("risk_soft_block")),
            "risk_soft_block_reason_codes": list(stock_details.get("risk_soft_block_reason_codes") or []),
            "position_size_multiplier": stock_details.get("position_size_multiplier", 1.0),
        },
    }
    return standardize_details(
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


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0
