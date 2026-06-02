from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from trading.strategy.candidates import CandidateLifecycle, is_valid_stock_code, normalize_code
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
from trading.theme_engine.lab import (
    LabGateDecision,
    LabGateStatus,
    PriceLocationStatus,
    StockRole,
    ThemeConditionSnapshot,
    ThemeLabFlowResult,
    ThemeLabThemeStatus,
    TradeabilityRiskLevel,
    WatchSetSnapshot,
)


SOURCE = "themelab_flow"
ORDER_PHASE_ENTRY = "entry"

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


class ThemeLabDryRunLifecycleBridge:
    """Translate ThemeLab scanner decisions into the existing DRY_RUN lifecycle model."""

    def __init__(
        self,
        *,
        db,
        market_data,
        default_ttl_minutes: int = 30,
    ) -> None:
        self.db = db
        self.market_data = market_data
        self.default_ttl_minutes = max(1, int(default_ttl_minutes or 30))

    def build(
        self,
        result: ThemeLabFlowResult,
        *,
        trade_date: str,
        now: datetime,
    ) -> ThemeLabBridgeBuildResult:
        warnings: list[str] = []
        candidate_saves = 0
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
            )
            if saved:
                candidate_saves += 1
            if candidate.id is None:
                warnings.append(f"THEME_LAB_BRIDGE_CANDIDATE_ID_MISSING:{code}")
                continue
            gate_results.append(self._gate_result(candidate, decision, watch, theme, now))

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
    ) -> tuple[Candidate, bool]:
        now_text = now.replace(microsecond=0).isoformat()
        expires_at = (now.replace(microsecond=0) + timedelta(minutes=self.default_ttl_minutes)).isoformat()
        existing = self.db.load_candidate(trade_date, code)
        metadata = _candidate_bridge_metadata(watch, theme)
        if existing is None:
            candidate = Candidate(
                trade_date=trade_date,
                code=code,
                name=watch.name,
                strategy_profile=_strategy_profile_from_watch(watch, self._latest_tick_metadata(code)),
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
            existing.strategy_profile = _strategy_profile_from_watch(watch, self._latest_tick_metadata(code))
            changed = True
        existing.last_seen_at = now_text
        existing.expires_at = expires_at
        merged_metadata = dict(existing.metadata or {})
        merged_metadata.update(metadata)
        if merged_metadata != existing.metadata:
            existing.metadata = merged_metadata
            changed = True
        if changed:
            return self.db.save_candidate(existing), True
        return existing, False

    def _gate_result(
        self,
        candidate: Candidate,
        decision: LabGateDecision,
        watch: WatchSetSnapshot,
        theme: ThemeConditionSnapshot | None,
        now: datetime,
    ) -> GatePipelineResult:
        mapping = _map_decision(decision, watch, theme)
        snapshot = self._indicator_snapshot(candidate, decision, watch, now)
        stock_details = _stock_pullback_details(candidate, decision, watch, snapshot, self._latest_tick_metadata(candidate.code), mapping)
        created_at = now.replace(microsecond=0).isoformat()
        lab_details = _base_details(candidate, decision, watch, theme, mapping, stock_details, created_at)
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
                "source": SOURCE,
                "watch_return_pct": watch.return_pct,
                "price_location_status": decision.price_location_status.value,
                "price_location_reason_codes": list(decision.price_location_reason_codes),
            },
        )

    def _latest_tick_metadata(self, code: str) -> dict[str, Any]:
        tick = self.market_data.latest_tick(code) if self.market_data is not None else None
        return dict(tick.metadata or {}) if tick is not None else {}


def _map_decision(
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    theme: ThemeConditionSnapshot | None,
) -> ThemeLabBridgeMapping:
    reason_codes = _reason_codes(decision, watch, theme)
    recheck_after_sec = int(decision.recheck_after_sec or 60)
    risk_allowed = decision.risk_level in BUY_ALLOWED_RISKS
    price_location = decision.price_location_status
    role = watch.stock_role

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
        )
    if (
        decision.status == LabGateStatus.READY
        and price_location in READY_PULLBACK_LOCATIONS
        and risk_allowed
    ):
        return ThemeLabBridgeMapping(
            "READY_PULLBACK",
            "BUY_ELIGIBLE_PULLBACK",
            True,
            BlockType.NONE,
            False,
            0,
            "A",
            max(80.0, float(decision.price_location_score or 0.0)),
            _dedupe(["READY_PULLBACK"] + reason_codes),
        )
    if (
        decision.status == LabGateStatus.READY_SMALL
        and price_location in READY_SMALL_PULLBACK_LOCATIONS
        and role in LEADER_ROLES
        and risk_allowed
    ):
        return ThemeLabBridgeMapping(
            "READY_SMALL_PULLBACK",
            "BUY_ELIGIBLE_SMALL_PULLBACK",
            True,
            BlockType.NONE,
            False,
            0,
            "B+",
            max(70.0, float(decision.price_location_score or 0.0)),
            _dedupe(["READY_SMALL_PULLBACK"] + reason_codes),
        )
    if decision.status == LabGateStatus.WAIT or decision.risk_level == TradeabilityRiskLevel.SOFT_BLOCK:
        return ThemeLabBridgeMapping(
            "WAIT",
            "NOT_ELIGIBLE_WAIT",
            False,
            BlockType.TEMPORARY,
            True,
            recheck_after_sec,
            "B",
            max(0.0, float(decision.price_location_score or 0.0)),
            _dedupe(["WAIT"] + reason_codes),
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
    )


def _stock_pullback_details(
    candidate: Candidate,
    decision: LabGateDecision,
    watch: WatchSetSnapshot,
    snapshot: IndicatorSnapshot,
    tick_metadata: dict[str, Any],
    mapping: ThemeLabBridgeMapping,
) -> dict[str, Any]:
    support_candidates = _support_candidates(tick_metadata)
    nearest_support, nearest_support_price = _nearest_support(decision.price_location_status, support_candidates)
    position_size_multiplier = max(0.0, float(decision.position_size_multiplier or 0.0))
    if position_size_multiplier <= 0:
        position_size_multiplier = 1.0
    return {
        "source": SOURCE,
        "profile": candidate.strategy_profile.value if candidate.strategy_profile else StrategyProfile.KOSDAQ_THEME_PROFILE.value,
        "nearest_support": nearest_support,
        "nearest_support_price": nearest_support_price,
        "support_candidates": support_candidates,
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
        "position_size_multiplier": position_size_multiplier,
        "dynamic_pullback_policy": {"source": SOURCE},
        "late_chase_diagnostics": {
            "source": SOURCE,
            "price_location_status": decision.price_location_status.value,
            "risk_level": decision.risk_level.value,
        },
        "late_chase_level": "soft_block" if decision.price_location_status in OBSERVE_ONLY_LOCATIONS else "",
        "late_chase_score": 100.0 if decision.price_location_status in OBSERVE_ONLY_LOCATIONS else 0.0,
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
) -> dict[str, Any]:
    theme_name = theme.theme_name if theme is not None else ""
    theme_score = theme.condition_score if theme is not None else 0.0
    support_price = int(stock_details.get("nearest_support_price") or 0)
    details = {
        "source": SOURCE,
        "theme_id": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
        "theme_name": theme_name,
        "theme_score": theme_score,
        "dynamic_theme_score": theme_score,
        "final_gate_status": mapping.final_gate_status,
        "order_eligibility": mapping.order_eligibility,
        "lab_gate_status": decision.status.value,
        "price_location_status": decision.price_location_status.value,
        "price_location_score": decision.price_location_score,
        "price_location_reason_codes": list(decision.price_location_reason_codes),
        "risk_level": decision.risk_level.value,
        "risk_reason_codes": list(decision.risk_reason_codes),
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
        "support_candidates": dict(stock_details.get("support_candidates") or {}),
        "stock_role": watch.stock_role.value,
        "position_size_multiplier": stock_details.get("position_size_multiplier", 1.0),
        "theme_lab_bridge": {
            "source": SOURCE,
            "code": candidate.code,
            "trade_date": candidate.trade_date,
            "candidate_id": candidate.id,
            "theme_id": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
            "theme_name": theme_name,
            "lab_gate_status": decision.status.value,
            "final_gate_status": mapping.final_gate_status,
            "order_eligibility": mapping.order_eligibility,
            "price_location_status": decision.price_location_status.value,
            "risk_level": decision.risk_level.value,
            "risk_reason_codes": list(decision.risk_reason_codes),
            "reason_codes": list(mapping.reason_codes),
            "support_price": support_price,
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
) -> str:
    return f"{SOURCE}:{trade_date}:{normalize_code(code)}:{candidate_id or ''}:{order_phase}:{leg_index or ''}"


def _candidate_bridge_metadata(watch: WatchSetSnapshot, theme: ThemeConditionSnapshot | None) -> dict[str, Any]:
    return {
        "theme_lab_bridge_source": SOURCE,
        "theme_lab_primary_theme": str(watch.primary_theme or (watch.themes[0] if watch.themes else "")),
        "theme_lab_themes": list(watch.themes),
        "theme_lab_stock_role": watch.stock_role.value,
        "theme_lab_condition_level": int(watch.condition_level or 0),
        "theme_lab_theme_status": theme.theme_status.value if theme is not None else "",
        "theme_lab_last_seen": watch.calculated_at,
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
    market = str(metadata.get("market") or metadata.get("market_type") or "").upper()
    if "KOSPI" in market:
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
        "vwap",
        "base_line_120",
        "envelope_mid",
        "day_mid",
        "ema20_5m",
    ):
        value = _positive_float(metadata.get(name))
        if value > 0:
            candidates[name] = value
    return candidates


def _nearest_support(
    price_location: PriceLocationStatus,
    support_candidates: dict[str, float],
) -> tuple[str, int]:
    preferred = ["recent_support_price", "support_price"]
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
