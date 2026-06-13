from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping


MODE_OBSERVE_ONLY = "observe_only"
MODE_LIVE_SIM_GUARDED = "live_sim_guarded"

STATUS_NO_EVIDENCE = "NO_EVIDENCE"
STATUS_OBSERVE_ONLY = "OBSERVE_ONLY"
STATUS_PROMOTED = "PROMOTED"
STATUS_BLOCKED = "BLOCKED"

READY_SHADOW_SMALL_ENTRY = "READY_SHADOW_SMALL_ENTRY"
WAIT_SHADOW_SMALL_ENTRY_CANDIDATE = "WAIT_SHADOW_SMALL_ENTRY_CANDIDATE"
SHADOW_OBSERVE_ONLY_REASON = "SHADOW_SMALL_ENTRY_PROMOTION_OBSERVE_ONLY"
SHADOW_CANDIDATE_REASON = "SHADOW_SMALL_ENTRY_PROMOTION_CANDIDATE"
REVIEW_REASON = "REVIEW_FOR_SMALL_ENTRY"


DEFAULT_BLOCKED_REASON_CODES = (
    "LATE_CHASE",
    "LATE_CHASE_TEMP_WAIT",
    "CHASE_HIGH",
    "CHASE_RISK",
    "HIGH_CHASE_RISK",
    "VWAP_OVEREXTENDED",
    "BREAKOUT_CONTINUATION",
    "VI_ACTIVE",
    "VI_COOLDOWN",
    "UPPER_LIMIT_NEAR",
    "UPPER_LIMIT_HARD_NEAR",
    "HIGH_RETURN_FOLLOWER",
    "HIGH_RETURN_LATE_LAGGARD",
    "EXTREME_RISK_OFF",
    "THEME_WEAK",
    "WEAK_THEME",
    "THEME_STALE",
    "CORE_BLOCKING",
    "BACKFILL_ONLY_OBSERVE",
    "DATA_INSUFFICIENT",
    "MISSING_CURRENT_PRICE",
    "STALE_QUOTE",
)


@dataclass(frozen=True)
class ShadowSmallEntryPromotionConfig:
    enabled: bool = True
    mode: str = MODE_OBSERVE_ONLY
    order_enabled: bool = False
    source_reports: tuple[str, ...] = ("conservative_reason_outcomes", "theme_lab_shadow_ab")
    min_report_trade_days: int = 1
    max_report_age_days: int = 3
    min_sample_count: int = 10
    strong_sample_count: int = 30
    min_missed_opportunity_rate: float = 0.35
    max_risk_avoided_rate: float = 0.20
    max_good_block_rate: float = 0.30
    min_avg_mfe_15m_pct: float = 2.5
    max_avg_mae_15m_pct: float = -1.8
    min_confidence: float = 0.55
    allowed_recommendations: tuple[str, ...] = ("REVIEW_FOR_SMALL_ENTRY",)
    allowed_reason_groups: tuple[str, ...] = ("DATA_QUALITY_RISK", "BREADTH_RISK", "PRICE_LOCATION_WAIT")
    allowed_reason_codes: tuple[str, ...] = (
        "WARMUP_OPTIONAL",
        "WARMUP_OPTIONAL_ONLY",
        "WAIT_DATA_EARLY_SMALL_CANDIDATE",
        "LEADER_ONLY_THEME",
        "LEADER_ONLY_THEME_LAGGARD_BLOCK",
        "WAIT_PRICE_LOCATION_PROVISIONAL",
        "WAIT_PRICE_LOCATION_WARMUP",
        "WAIT_DATA_SUPPORT_NOT_READY",
    )
    blocked_reason_codes: tuple[str, ...] = DEFAULT_BLOCKED_REASON_CODES
    allowed_roles: tuple[str, ...] = ("LEADER", "CO_LEADER")
    allowed_price_locations: tuple[str, ...] = ("GOOD_PULLBACK", "PULLBACK_RECLAIM", "VWAP_RECLAIM")
    allowed_risk_levels: tuple[str, ...] = ("PASS", "RISK_ADJUST")
    allowed_theme_statuses: tuple[str, ...] = ("LEADING_THEME", "SPREADING_THEME", "WATCH_THEME", "")
    require_latest_tick_ready: bool = True
    require_current_price: bool = True
    require_trade_value: bool = False
    require_vwap_or_support: bool = True
    require_support_ready: bool = True
    require_exit_guard_ready: bool = True
    require_live_sim_audit_ok: bool = True
    require_reconcile_not_required: bool = True
    max_position_size_multiplier: float = 0.15
    max_position_size_multiplier_strong: float = 0.25
    thin_position_size_multiplier: float = 0.10
    max_promotions_per_cycle: int = 1
    max_promotions_per_day: int = 3
    max_promotions_per_code_per_day: int = 1
    max_notional_per_day: int = 300_000
    cancel_unfilled_after_sec: int = 45
    exit_stop_loss_pct: float = -1.2
    exit_take_profit_pct: float = 1.8
    exit_max_hold_minutes: int = 20
    observe_only_reason_code: str = SHADOW_OBSERVE_ONLY_REASON
    ops_status: str = "OBSERVE_ONLY"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ShadowSmallEntryPromotionConfig":
        data = dict(raw or {})
        defaults = cls()
        return cls(
            enabled=_bool(data.get("enabled"), True),
            mode=_mode(data.get("mode")),
            order_enabled=_bool(data.get("order_enabled"), False),
            source_reports=_tuple(data.get("source_reports"), defaults.source_reports),
            min_report_trade_days=max(0, _int(data.get("min_report_trade_days"), 1)),
            max_report_age_days=max(0, _int(data.get("max_report_age_days"), 3)),
            min_sample_count=max(0, _int(data.get("min_sample_count"), 10)),
            strong_sample_count=max(0, _int(data.get("strong_sample_count"), 30)),
            min_missed_opportunity_rate=_float(data.get("min_missed_opportunity_rate"), 0.35),
            max_risk_avoided_rate=_float(data.get("max_risk_avoided_rate"), 0.20),
            max_good_block_rate=_float(data.get("max_good_block_rate"), 0.30),
            min_avg_mfe_15m_pct=_float(data.get("min_avg_mfe_15m_pct"), 2.5),
            max_avg_mae_15m_pct=_float(data.get("max_avg_mae_15m_pct"), -1.8),
            min_confidence=_float(data.get("min_confidence"), 0.55),
            allowed_recommendations=_upper_tuple(data.get("allowed_recommendations"), defaults.allowed_recommendations),
            allowed_reason_groups=_upper_tuple(data.get("allowed_reason_groups"), defaults.allowed_reason_groups),
            allowed_reason_codes=_upper_tuple(data.get("allowed_reason_codes"), defaults.allowed_reason_codes),
            blocked_reason_codes=_upper_tuple(data.get("blocked_reason_codes"), defaults.blocked_reason_codes),
            allowed_roles=_upper_tuple(data.get("allowed_roles"), defaults.allowed_roles),
            allowed_price_locations=_upper_tuple(data.get("allowed_price_locations"), defaults.allowed_price_locations),
            allowed_risk_levels=_upper_tuple(data.get("allowed_risk_levels"), defaults.allowed_risk_levels),
            allowed_theme_statuses=_upper_tuple(data.get("allowed_theme_statuses"), defaults.allowed_theme_statuses),
            require_latest_tick_ready=_bool(data.get("require_latest_tick_ready"), True),
            require_current_price=_bool(data.get("require_current_price"), True),
            require_trade_value=_bool(data.get("require_trade_value"), False),
            require_vwap_or_support=_bool(data.get("require_vwap_or_support"), True),
            require_support_ready=_bool(data.get("require_support_ready"), True),
            require_exit_guard_ready=_bool(data.get("require_exit_guard_ready"), True),
            require_live_sim_audit_ok=_bool(data.get("require_live_sim_audit_ok"), True),
            require_reconcile_not_required=_bool(data.get("require_reconcile_not_required"), True),
            max_position_size_multiplier=_cap_multiplier(data.get("max_position_size_multiplier"), 0.15),
            max_position_size_multiplier_strong=_cap_multiplier(data.get("max_position_size_multiplier_strong"), 0.25),
            thin_position_size_multiplier=_cap_multiplier(data.get("thin_position_size_multiplier"), 0.10),
            max_promotions_per_cycle=max(0, _int(data.get("max_promotions_per_cycle"), 1)),
            max_promotions_per_day=max(0, _int(data.get("max_promotions_per_day"), 3)),
            max_promotions_per_code_per_day=max(0, _int(data.get("max_promotions_per_code_per_day"), 1)),
            max_notional_per_day=max(0, _int(data.get("max_notional_per_day"), 300_000)),
            cancel_unfilled_after_sec=max(1, _int(data.get("cancel_unfilled_after_sec"), 45)),
            exit_stop_loss_pct=_float(data.get("exit_stop_loss_pct"), -1.2),
            exit_take_profit_pct=_float(data.get("exit_take_profit_pct"), 1.8),
            exit_max_hold_minutes=max(1, _int(data.get("exit_max_hold_minutes"), 20)),
            observe_only_reason_code=str(data.get("observe_only_reason_code") or SHADOW_OBSERVE_ONLY_REASON),
            ops_status=str(data.get("ops_status") or data.get("current_status") or "OBSERVE_ONLY").upper(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShadowSmallEntryPromotionEvaluation:
    candidate: dict[str, Any]
    promoted: bool
    promotion_status: str
    ready_type: str
    final_status: str
    strategy_eligible: bool
    order_eligibility: str
    final_grade: str
    position_size_multiplier: float
    rejected_reason: str
    reason_codes: list[str]
    evidence: dict[str, Any] = field(default_factory=dict)
    operator_message_ko: str = ""
    cancel_condition: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def config_from_settings(settings: Any | None) -> ShadowSmallEntryPromotionConfig:
    raw: Any = None
    ops_raw: Any = {}
    if settings is not None and hasattr(settings, "value"):
        raw = settings.value("shadow_small_entry_promotion", {})
        ops_raw = settings.value("shadow_small_entry_ops", {})
    elif isinstance(settings, Mapping):
        raw = settings.get("shadow_small_entry_promotion", settings)
        ops_raw = settings.get("shadow_small_entry_ops", {})
    merged = dict(raw if isinstance(raw, Mapping) else {})
    if isinstance(ops_raw, Mapping):
        merged["ops_status"] = ops_raw.get("current_status") or ops_raw.get("status") or ops_raw.get("default_status") or merged.get("ops_status")
    return ShadowSmallEntryPromotionConfig.from_mapping(merged)


def evaluate_shadow_small_entry_promotion(
    *,
    candidate: Any = None,
    gate_decision: Any = None,
    watch: Any = None,
    theme: Any = None,
    trace: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    settings: Any | None = None,
    config: ShadowSmallEntryPromotionConfig | None = None,
    live_sim_audit: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
    promotion_available: bool = True,
) -> ShadowSmallEntryPromotionEvaluation:
    cfg = config or config_from_settings(settings)
    trace = dict(trace or {})
    evidence_payload = dict(evidence or {})
    candidate_payload = _candidate_payload(candidate, gate_decision, watch, theme, trace)
    reason_codes = _combined_reason_codes(candidate, gate_decision, watch, theme, trace)
    reason_set = set(reason_codes)
    matched = _matched_evidence(reason_codes, candidate_payload, evidence_payload)
    evidence_status = str(evidence_payload.get("status") or "").upper()
    available = bool(evidence_payload.get("available"))
    base_codes = _dedupe([SHADOW_CANDIDATE_REASON, REVIEW_REASON, *reason_codes])

    if not cfg.enabled:
        return _evaluation(
            candidate_payload,
            STATUS_BLOCKED,
            "SHADOW_SMALL_ENTRY_PROMOTION_DISABLED",
            base_codes + ["SHADOW_SMALL_ENTRY_PROMOTION_DISABLED"],
            cfg,
            matched,
            "소액 승격 정책이 비활성화되어 있습니다.",
        )
    if not available:
        return _evaluation(
            candidate_payload,
            STATUS_NO_EVIDENCE,
            evidence_status or "NO_EVIDENCE",
            reason_codes + ["SHADOW_SMALL_ENTRY_NO_EVIDENCE"],
            cfg,
            matched,
            "승격에 사용할 outcome 리포트 근거가 없습니다.",
        )
    if not matched:
        return _evaluation(
            candidate_payload,
            STATUS_NO_EVIDENCE,
            "NO_MATCHING_EVIDENCE",
            reason_codes + ["SHADOW_SMALL_ENTRY_NO_MATCHING_EVIDENCE"],
            cfg,
            {},
            "현재 후보의 reason/group과 일치하는 소액 승격 근거가 없습니다.",
        )

    matched = {
        **matched,
        "source_report_id": matched.get("source_report_id")
        or matched.get("report_id")
        or evidence_payload.get("source_report_id")
        or evidence_payload.get("report_id")
        or "",
        "report_id": matched.get("report_id")
        or evidence_payload.get("report_id")
        or evidence_payload.get("source_report_id")
        or "",
        "source_report_trade_date": matched.get("source_report_trade_date")
        or evidence_payload.get("source_report_trade_date")
        or evidence_payload.get("trade_date")
        or "",
    }

    blocker = _candidate_blocker(candidate_payload, reason_set, cfg, live_sim_audit=live_sim_audit, usage=usage, promotion_available=promotion_available)
    if blocker:
        reason, codes, message = blocker
        return _evaluation(
            candidate_payload,
            STATUS_BLOCKED,
            reason,
            base_codes + codes,
            cfg,
            matched,
            message,
        )

    sample_count = _int(matched.get("sample_count") or matched.get("labeled_count") or matched.get("event_count"), 0)
    confidence = _float(matched.get("confidence"), 0.0)
    sample_quality = str(matched.get("sample_quality") or _sample_quality(sample_count, cfg)).upper()
    if sample_count < cfg.min_sample_count or confidence < cfg.min_confidence:
        multiplier = cfg.thin_position_size_multiplier
        return _evaluation(
            candidate_payload,
            STATUS_OBSERVE_ONLY,
            "INSUFFICIENT_SAMPLE_OR_CONFIDENCE",
            base_codes + ["SHADOW_SMALL_ENTRY_THIN_SAMPLE", cfg.observe_only_reason_code],
            cfg,
            {**matched, "sample_quality": sample_quality, "position_size_multiplier": multiplier},
            "표본이 낮아 0.10배 관측 후보입니다.",
            multiplier=multiplier,
        )

    multiplier = _promotion_multiplier(sample_count, matched, cfg)
    if cfg.mode != MODE_LIVE_SIM_GUARDED or not cfg.order_enabled:
        return _evaluation(
            candidate_payload,
            STATUS_OBSERVE_ONLY,
            cfg.observe_only_reason_code,
            base_codes + [cfg.observe_only_reason_code],
            cfg,
            {**matched, "sample_quality": sample_quality, "position_size_multiplier": multiplier},
            "리포트 근거는 있으나 order_enabled=false라 주문하지 않습니다.",
            multiplier=multiplier,
        )
    ops_status = str(cfg.ops_status or "").upper()
    if ops_status != "LIVE_SIM_ACTIVE":
        reason = _ops_block_reason(ops_status)
        return _evaluation(
            candidate_payload,
            STATUS_OBSERVE_ONLY,
            reason,
            base_codes + [reason, cfg.observe_only_reason_code],
            cfg,
            {**matched, "sample_quality": sample_quality, "position_size_multiplier": multiplier},
            "운영 상태가 LIVE_SIM_ACTIVE가 아니어서 관측 전용으로 유지합니다.",
            multiplier=multiplier,
        )

    return _evaluation(
        candidate_payload,
        STATUS_PROMOTED,
        "PASS",
        base_codes + [READY_SHADOW_SMALL_ENTRY, "BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED"],
        cfg,
        {**matched, "sample_quality": sample_quality, "position_size_multiplier": multiplier},
        "조건 충족: 1차 leg만 LIVE_SIM 주문 가능합니다.",
        multiplier=multiplier,
    )


def _evaluation(
    candidate: dict[str, Any],
    status: str,
    reason: str,
    reason_codes: Iterable[Any],
    cfg: ShadowSmallEntryPromotionConfig,
    evidence: Mapping[str, Any],
    operator_message_ko: str,
    *,
    multiplier: float | None = None,
) -> ShadowSmallEntryPromotionEvaluation:
    promoted = status == STATUS_PROMOTED
    observe = status == STATUS_OBSERVE_ONLY
    multiplier_value = float(multiplier if multiplier is not None else evidence.get("position_size_multiplier") or 0.0)
    final_status = READY_SHADOW_SMALL_ENTRY if promoted else (WAIT_SHADOW_SMALL_ENTRY_CANDIDATE if observe else str(candidate.get("status") or ""))
    ready_type = READY_SHADOW_SMALL_ENTRY if promoted else (WAIT_SHADOW_SMALL_ENTRY_CANDIDATE if observe else "")
    return ShadowSmallEntryPromotionEvaluation(
        candidate=candidate,
        promoted=promoted,
        promotion_status=status,
        ready_type=ready_type,
        final_status=final_status,
        strategy_eligible=promoted,
        order_eligibility="BUY_ELIGIBLE_SHADOW_SMALL_ENTRY_GUARDED" if promoted else "NOT_ELIGIBLE_OBSERVE",
        final_grade="B_SHADOW" if promoted else "C",
        position_size_multiplier=round(max(0.0, min(0.25, multiplier_value)), 4),
        rejected_reason="" if promoted else str(reason or status),
        reason_codes=_dedupe(reason_codes),
        evidence=dict(evidence or {}),
        operator_message_ko=operator_message_ko,
        cancel_condition={
            "shadow_small_entry_promotion": True,
            "shadow_small_entry_promotion_status": status,
            "shadow_small_entry_promotion_reason": reason,
            "shadow_small_entry_promotion_order_enabled": bool(cfg.order_enabled),
            "shadow_small_entry_promotion_mode": cfg.mode,
            "shadow_small_entry_first_leg_only": True,
            "shadow_small_entry_position_size_multiplier": round(max(0.0, min(0.25, multiplier_value)), 4),
            "shadow_small_entry_cancel_unfilled_after_sec": cfg.cancel_unfilled_after_sec,
            "shadow_small_entry_stop_loss_pct": cfg.exit_stop_loss_pct,
            "shadow_small_entry_take_profit_pct": cfg.exit_take_profit_pct,
            "shadow_small_entry_max_hold_minutes": cfg.exit_max_hold_minutes,
            "shadow_small_entry_max_promotions_per_cycle": cfg.max_promotions_per_cycle,
            "shadow_small_entry_max_promotions_per_day": cfg.max_promotions_per_day,
            "shadow_small_entry_max_promotions_per_code_per_day": cfg.max_promotions_per_code_per_day,
            "shadow_small_entry_max_notional_per_day": cfg.max_notional_per_day,
            "operator_message_ko": operator_message_ko,
        },
    )


def _candidate_blocker(
    candidate: Mapping[str, Any],
    reason_set: set[str],
    cfg: ShadowSmallEntryPromotionConfig,
    *,
    live_sim_audit: Mapping[str, Any] | None,
    usage: Mapping[str, Any] | None,
    promotion_available: bool,
) -> tuple[str, list[str], str] | None:
    blocked = reason_set & set(cfg.blocked_reason_codes)
    if blocked:
        reason = sorted(blocked)[0]
        if reason in {"LATE_CHASE", "LATE_CHASE_TEMP_WAIT", "CHASE_HIGH", "CHASE_RISK", "HIGH_CHASE_RISK", "VWAP_OVEREXTENDED"}:
            return reason, [reason, "SHADOW_SMALL_ENTRY_CHASE_BLOCKED"], "추격 위험 사유가 있어 소액 승격 대상에서 제외됐습니다."
        return reason, [reason, "SHADOW_SMALL_ENTRY_RISK_BLOCKED"], "고위험 차단 사유가 있어 소액 승격 대상에서 제외됐습니다."
    if str(candidate.get("status") or "").upper() not in {"WAIT", "OBSERVE", "BLOCKED", "READY_SMALL", ""}:
        return "STATUS_NOT_PROMOTABLE", ["SHADOW_SMALL_ENTRY_STATUS_NOT_PROMOTABLE"], "현재 후보 상태는 소액 승격 검토 대상이 아닙니다."
    if str(candidate.get("stock_role") or "").upper() not in set(cfg.allowed_roles):
        return "ROLE_NOT_ALLOWED", ["SHADOW_SMALL_ENTRY_ROLE_NOT_ALLOWED"], "주도주/공동주도주가 아니어서 소액 승격에서 제외됐습니다."
    if str(candidate.get("price_location_status") or "").upper() not in set(cfg.allowed_price_locations):
        return "PRICE_LOCATION_NOT_ALLOWED", ["SHADOW_SMALL_ENTRY_PRICE_LOCATION_NOT_ALLOWED"], "가격 위치가 소액 승격 조건을 충족하지 않습니다."
    if str(candidate.get("risk_level") or "").upper() not in set(cfg.allowed_risk_levels):
        return "RISK_LEVEL_NOT_ALLOWED", ["SHADOW_SMALL_ENTRY_RISK_LEVEL_NOT_ALLOWED"], "리스크 레벨이 소액 승격 조건을 충족하지 않습니다."
    theme_status = str(candidate.get("theme_status") or "").upper()
    if theme_status and theme_status not in set(cfg.allowed_theme_statuses):
        return "THEME_STATUS_NOT_ALLOWED", ["SHADOW_SMALL_ENTRY_THEME_STATUS_NOT_ALLOWED"], "테마 상태가 소액 승격 조건을 충족하지 않습니다."
    if cfg.require_latest_tick_ready and candidate.get("latest_tick_ready") is False:
        return "LATEST_TICK_NOT_READY", ["SHADOW_SMALL_ENTRY_LATEST_TICK_NOT_READY"], "실시간 틱이 오래되어 주문 보류합니다."
    if cfg.require_current_price and _float(candidate.get("current_price"), 0.0) <= 0:
        return "CURRENT_PRICE_NOT_READY", ["SHADOW_SMALL_ENTRY_CURRENT_PRICE_NOT_READY"], "현재가가 없어 소액 승격을 차단했습니다."
    if cfg.require_trade_value and _float(candidate.get("trade_value"), 0.0) <= 0:
        return "TRADE_VALUE_NOT_READY", ["SHADOW_SMALL_ENTRY_TRADE_VALUE_NOT_READY"], "거래대금 확인 전이라 소액 승격을 차단했습니다."
    if cfg.require_support_ready and candidate.get("support_ready") is False:
        return "SUPPORT_NOT_READY", ["SHADOW_SMALL_ENTRY_SUPPORT_NOT_READY"], "지지선 미확정으로 진단 전용입니다."
    if cfg.require_vwap_or_support and not bool(candidate.get("vwap_ready") or candidate.get("support_ready") or candidate.get("recent_support_ready")):
        return "VWAP_OR_SUPPORT_NOT_READY", ["SHADOW_SMALL_ENTRY_VWAP_OR_SUPPORT_NOT_READY"], "VWAP 또는 지지선 확인 전이라 소액 승격을 차단했습니다."
    if not promotion_available:
        return "SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED", ["SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED"], "이번 cycle 소액 승격 한도를 초과했습니다."
    usage = dict(usage or {})
    if cfg.max_promotions_per_cycle and _int(usage.get("used_promotions_this_cycle"), 0) >= cfg.max_promotions_per_cycle:
        return "SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED", ["SHADOW_SMALL_ENTRY_MAX_PER_CYCLE_EXCEEDED"], "이번 cycle 소액 승격 한도를 초과했습니다."
    if cfg.max_promotions_per_day and _int(usage.get("used_promotions_today"), 0) >= cfg.max_promotions_per_day:
        return "SHADOW_SMALL_ENTRY_MAX_PER_DAY_EXCEEDED", ["SHADOW_SMALL_ENTRY_MAX_PER_DAY_EXCEEDED"], "오늘 소액 승격 한도를 초과했습니다."
    if cfg.max_promotions_per_code_per_day and _int(usage.get("used_promotions_for_code_today"), 0) >= cfg.max_promotions_per_code_per_day:
        return "SHADOW_SMALL_ENTRY_CODE_ALREADY_PROMOTED", ["SHADOW_SMALL_ENTRY_CODE_ALREADY_PROMOTED"], "이 종목은 오늘 이미 소액 승격 후보로 처리됐습니다."
    if cfg.max_notional_per_day and _float(usage.get("used_notional_today"), 0.0) >= cfg.max_notional_per_day:
        return "SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT_EXCEEDED", ["SHADOW_SMALL_ENTRY_NOTIONAL_LIMIT_EXCEEDED"], "오늘 소액 승격 주문 금액 한도를 초과했습니다."
    audit = dict(live_sim_audit or {})
    audit_status = str(audit.get("status") or "").upper()
    summary = dict(audit.get("summary") or {})
    if cfg.require_live_sim_audit_ok and audit_status in {"BROKEN", "RECONCILE_REQUIRED"}:
        return "SHADOW_SMALL_ENTRY_LIVE_SIM_AUDIT_BLOCK", ["SHADOW_SMALL_ENTRY_LIVE_SIM_AUDIT_BLOCK"], "LIVE_SIM audit이 BROKEN이라 소액 승격을 차단했습니다."
    if cfg.require_reconcile_not_required and int(summary.get("reconcile_required_order_count") or summary.get("reconcile_required_count") or 0) > 0:
        return "SHADOW_SMALL_ENTRY_RECONCILE_REQUIRED", ["SHADOW_SMALL_ENTRY_RECONCILE_REQUIRED"], "reconcile 필요 상태라 소액 승격을 차단했습니다."
    if cfg.require_exit_guard_ready and audit.get("exit_guard_ready") is False:
        return "SHADOW_SMALL_ENTRY_EXIT_GUARD_NOT_READY", ["SHADOW_SMALL_ENTRY_EXIT_GUARD_NOT_READY"], "exit guard 준비 전이라 소액 승격을 차단했습니다."
    return None


def _matched_evidence(reason_codes: list[str], candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    reason_set = set(reason_codes)
    eligible_reasons = {str(item).upper() for item in evidence.get("eligible_reason_codes") or []}
    eligible_groups = {str(item).upper() for item in evidence.get("eligible_reason_groups") or []}
    rows = []
    rows.extend(list(evidence.get("reason_code_rows") or evidence.get("by_reason_code") or []))
    rows.extend(list(evidence.get("group_rows") or evidence.get("by_group") or []))
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_reason = str(row.get("reason_code") or "").upper()
        row_group = str(row.get("group") or row.get("reason_group") or "").upper()
        if row_reason and row_reason in reason_set:
            return _evidence_row(row, reason_code=row_reason, reason_group=row_group)
        if row_group and (row_group in eligible_groups or row_group == str(candidate.get("reason_group") or "").upper()):
            return _evidence_row(row, reason_code=row_reason, reason_group=row_group)
    if reason_set & eligible_reasons:
        return _evidence_row(evidence, reason_code=sorted(reason_set & eligible_reasons)[0], reason_group=str(candidate.get("reason_group") or ""))
    if eligible_groups and str(candidate.get("reason_group") or "").upper() in eligible_groups:
        return _evidence_row(evidence, reason_code="", reason_group=str(candidate.get("reason_group") or ""))
    if evidence.get("best_scenario"):
        return _evidence_row(dict(evidence.get("best_scenario") or {}), reason_code="", reason_group=str(candidate.get("reason_group") or ""))
    return {}


def _evidence_row(row: Mapping[str, Any], *, reason_code: str, reason_group: str) -> dict[str, Any]:
    sample = _int(row.get("labeled_count") or row.get("sample_count") or row.get("candidate_count") or row.get("event_count"), 0)
    confidence = row.get("confidence")
    if confidence is None:
        confidence = min(0.95, sample / 30.0) if sample else 0.0
    return {
        **dict(row),
        "reason_code": reason_code or str(row.get("reason_code") or ""),
        "reason_group": reason_group or str(row.get("group") or ""),
        "sample_count": sample,
        "confidence": round(_float(confidence, 0.0), 4),
        "sample_quality": str(row.get("sample_quality") or _sample_quality(sample, ShadowSmallEntryPromotionConfig())),
    }


def _candidate_payload(candidate: Any, gate_decision: Any, watch: Any, theme: Any, trace: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": _first(_get(candidate, "code"), _get(gate_decision, "symbol"), _get(watch, "symbol"), trace.get("code")),
        "name": _first(_get(candidate, "name"), _get(watch, "name"), trace.get("name")),
        "candidate_instance_id": _first(
            _deep_get(candidate, "metadata.candidate_instance_id"),
            trace.get("candidate_instance_id"),
            _get(gate_decision, "candidate_instance_id"),
        ),
        "status": _text(_first(_get(gate_decision, "status"), trace.get("status"), trace.get("gate_status"))).upper(),
        "stock_role": _text(_first(_get(watch, "stock_role"), trace.get("stock_role"))).upper(),
        "price_location_status": _text(_first(_get(gate_decision, "price_location_status"), _get(watch, "price_location_status"), trace.get("price_location_status"))).upper(),
        "price_location_readiness": _text(_first(_get(watch, "price_location_readiness"), trace.get("price_location_readiness"))).upper(),
        "risk_level": _text(_first(_get(gate_decision, "risk_level"), trace.get("risk_level"))).upper(),
        "theme_status": _text(_first(_get(theme, "theme_status"), _get(theme, "status"), trace.get("theme_status"))).upper(),
        "reason_group": _text(_first(trace.get("primary_group"), trace.get("reason_group"))).upper(),
        "current_price": _first(trace.get("current_price"), trace.get("price"), _get(gate_decision, "current_price"), _get(watch, "current_price")),
        "trade_value": _first(trace.get("trade_value"), _get(watch, "trade_value")),
        "latest_tick_ready": _first(trace.get("latest_tick_ready"), True),
        "latest_tick_age_sec": trace.get("latest_tick_age_sec"),
        "support_ready": _first(trace.get("support_ready"), trace.get("selected_support_ready"), True),
        "recent_support_ready": bool(trace.get("recent_support_ready")),
        "vwap_ready": bool(trace.get("vwap_ready")),
        "reason_codes": _combined_reason_codes(candidate, gate_decision, watch, theme, trace),
    }


def _combined_reason_codes(candidate: Any, gate_decision: Any, watch: Any, theme: Any, trace: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for source, attr in (
        (trace, "reason_codes"),
        (gate_decision, "reason_codes"),
        (gate_decision, "risk_reason_codes"),
        (gate_decision, "price_location_reason_codes"),
        (watch, "price_location_readiness_reason_codes"),
        (theme, "reason_codes"),
    ):
        raw = source.get(attr) if isinstance(source, Mapping) else getattr(source, attr, None)
        if isinstance(raw, str):
            values.append(raw)
        else:
            values.extend(list(raw or []))
    for key in ("primary_reason", "primary_block_reason", "data_quality_bucket", "data_quality_action"):
        if trace.get(key):
            values.append(trace.get(key))
    return _dedupe(_text(item).upper() for item in values if _text(item))


def _promotion_multiplier(sample_count: int, row: Mapping[str, Any], cfg: ShadowSmallEntryPromotionConfig) -> float:
    suggested = _float(row.get("suggested_position_size_multiplier"), 0.0)
    if sample_count < cfg.min_sample_count:
        return cfg.thin_position_size_multiplier
    if suggested > 0:
        return min(cfg.max_position_size_multiplier_strong, suggested)
    if sample_count >= cfg.strong_sample_count:
        return cfg.max_position_size_multiplier_strong
    return cfg.max_position_size_multiplier


def _sample_quality(sample_count: int, cfg: ShadowSmallEntryPromotionConfig) -> str:
    if sample_count <= 0:
        return "NO_DATA"
    if sample_count < cfg.min_sample_count:
        return "LOW"
    if sample_count < cfg.strong_sample_count:
        return "MEDIUM"
    return "HIGH"


def _ops_block_reason(status: str) -> str:
    status = str(status or "").upper()
    if status in {"", "OBSERVE_ONLY", "DISABLED"}:
        return "SHADOW_SMALL_ENTRY_OPS_OBSERVE_ONLY"
    if status == "LIVE_SIM_ARMED":
        return "SHADOW_SMALL_ENTRY_OPS_NOT_ACTIVE"
    if status == "PAUSED_BY_RISK":
        return "SHADOW_SMALL_ENTRY_OPS_PAUSED_BY_RISK"
    if status == "PAUSED_BY_AUDIT":
        return "SHADOW_SMALL_ENTRY_OPS_PAUSED_BY_AUDIT"
    if status == "PAUSED_BY_RECONCILE":
        return "SHADOW_SMALL_ENTRY_OPS_RECONCILE_REQUIRED"
    if status == "ROLLED_BACK":
        return "SHADOW_SMALL_ENTRY_OPS_ROLLED_BACK"
    if status == "BROKEN":
        return "SHADOW_SMALL_ENTRY_OPS_PREFLIGHT_FAILED"
    return "SHADOW_SMALL_ENTRY_OPS_NOT_ACTIVE"


def _get(obj: Any, name: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _deep_get(obj: Any, path: str, default: Any = "") -> Any:
    current = obj
    for part in path.split("."):
        current = _get(current, part, None)
        if current is None:
            return default
    return current


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _text(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip()


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value).upper()
        if text and text not in result:
            result.append(text)
    return result


def _tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(default)


def _upper_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item.upper() for item in _tuple(value, default))


def _mode(value: Any) -> str:
    mode = str(value or MODE_OBSERVE_ONLY).strip().lower()
    return mode if mode in {MODE_OBSERVE_ONLY, MODE_LIVE_SIM_GUARDED} else MODE_OBSERVE_ONLY


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cap_multiplier(value: Any, default: float) -> float:
    return max(0.0, min(0.25, _float(value, default)))


def trace_payload_from_evaluation(
    evaluation: ShadowSmallEntryPromotionEvaluation | Mapping[str, Any],
    *,
    stage: str,
    trade_date: str = "",
    runtime_cycle_id: str = "",
    decision_cycle_id: str = "",
    decision_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    payload = evaluation.to_dict() if hasattr(evaluation, "to_dict") else dict(evaluation or {})
    candidate = dict(payload.get("candidate") or {})
    evidence = dict(payload.get("evidence") or {})
    created = created_at or datetime.now().isoformat(timespec="seconds")
    passed = stage.endswith("PROMOTED") or stage.endswith("ORDER_SUBMITTED")
    if payload.get("promotion_status") == STATUS_OBSERVE_ONLY and stage.endswith("OBSERVE_ONLY"):
        passed = False
    return {
        "trace_id": f"shadow_small_entry:{trade_date}:{candidate.get('candidate_instance_id') or candidate.get('code') or ''}:{stage}:{decision_id or created}",
        "trade_date": trade_date or created[:10],
        "runtime_cycle_id": runtime_cycle_id,
        "decision_cycle_id": decision_cycle_id,
        "decision_id": decision_id,
        "candidate_instance_id": candidate.get("candidate_instance_id") or "",
        "code": candidate.get("code") or "",
        "name": candidate.get("name") or "",
        "stage": stage,
        "stage_status": payload.get("promotion_status") or "",
        "pass_fail": "PASS" if passed else "FAIL",
        "passed": passed,
        "primary_block_reason": "" if passed else str(payload.get("rejected_reason") or ""),
        "reason_codes": list(payload.get("reason_codes") or []),
        "gate_status": payload.get("final_status") or candidate.get("status") or "",
        "stock_role": candidate.get("stock_role") or "",
        "price_location_status": candidate.get("price_location_status") or "",
        "price_location_readiness": candidate.get("price_location_readiness") or "",
        "latest_tick_ready": candidate.get("latest_tick_ready"),
        "latest_tick_age_sec": candidate.get("latest_tick_age_sec"),
        "support_ready": candidate.get("support_ready"),
        "vwap_ready": candidate.get("vwap_ready"),
        "operator_message_ko": payload.get("operator_message_ko") or "",
        "promotion_status": payload.get("promotion_status") or "",
        "promotion_reason": payload.get("rejected_reason") or "",
        "promotion_reason_codes": list(payload.get("reason_codes") or []),
        "source_report_id": evidence.get("source_report_id") or evidence.get("report_id") or "",
        "source_report_trade_date": evidence.get("source_report_trade_date") or evidence.get("trade_date") or "",
        "reason_group": evidence.get("reason_group") or candidate.get("reason_group") or "",
        "reason_code": evidence.get("reason_code") or "",
        "sample_count": evidence.get("sample_count"),
        "missed_opportunity_rate": evidence.get("missed_opportunity_rate"),
        "risk_avoided_rate": evidence.get("risk_avoided_rate"),
        "good_block_rate": evidence.get("good_block_rate"),
        "avg_mfe_15m_pct": evidence.get("avg_mfe_15m_pct"),
        "avg_mae_15m_pct": evidence.get("avg_mae_15m_pct"),
        "position_size_multiplier": payload.get("position_size_multiplier"),
        "order_enabled": (payload.get("cancel_condition") or {}).get("shadow_small_entry_promotion_order_enabled"),
        "mode": (payload.get("cancel_condition") or {}).get("shadow_small_entry_promotion_mode"),
        "max_promotions_per_cycle": (payload.get("cancel_condition") or {}).get("shadow_small_entry_max_promotions_per_cycle"),
        "max_promotions_per_day": (payload.get("cancel_condition") or {}).get("shadow_small_entry_max_promotions_per_day"),
        "details": {"shadow_small_entry_promotion": payload},
        "created_at": created,
    }
