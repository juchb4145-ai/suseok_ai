from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from storage.db import TradingDatabase
from trading_app.strategy_replay import DEFAULT_REPLAY_DB_ROOT, scan_replay_reports
from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


PROPOSAL_STATUSES = {
    "DRAFT",
    "REVIEW_READY",
    "APPROVED_FOR_OBSERVE",
    "APPROVED_FOR_DRY_RUN",
    "REJECTED",
    "EXPIRED",
    "SUPERSEDED",
}
RECOMMENDATION_GRADES = {
    "STRONG_CANDIDATE",
    "WATCH_CANDIDATE",
    "RISKY_CANDIDATE",
    "DATA_INSUFFICIENT",
    "DO_NOT_APPLY",
}
CHANGE_CATEGORIES = {
    "gate",
    "risk",
    "theme",
    "exit",
    "session",
    "data_quality",
    "order_guard",
    "position_sizing",
}
GRADE_ORDER = {
    "STRONG_CANDIDATE": 0,
    "WATCH_CANDIDATE": 1,
    "RISKY_CANDIDATE": 2,
    "DATA_INSUFFICIENT": 3,
    "DO_NOT_APPLY": 4,
}
FORBIDDEN_KEY_PARTS = (
    "live_order_enabled",
    "runtime_allow_live_orders",
    "trading_allow_live",
    "allow_live",
    "account",
    "token",
    "secret",
    "password",
    "gateway_start",
    "kiwoom_gateway_start",
    "live_order_sink",
    "order_sink_live",
)


@dataclass(frozen=True)
class StrategyChangeProposalConfig:
    enabled: bool = True
    min_sample_count: int = 20
    min_trade_days: int = 2
    min_replay_count: int = 1
    max_fp_increase: int = 1
    max_opportunity_loss_increase: int = 1
    strong_min_confidence: float = 0.7
    allow_auto_apply: bool = False
    default_expire_days: int = 5


@dataclass(frozen=True)
class StrategyChangePatch:
    patch: dict[str, Any]
    baseline_config_hash: str
    candidate_config_hash: str
    baseline_config_snapshot: dict[str, Any]
    diff: list[dict[str, Any]]
    forbidden_keys: list[str] = field(default_factory=list)
    dangerous_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyChangeEvidence:
    evidence_id: str
    proposal_id: str
    source_type: str
    source_id: str
    trade_date: str
    metric_name: str
    metric_value: Optional[float] = None
    metric_unit: str = ""
    baseline_value: Any = ""
    candidate_value: Any = ""
    delta_value: Optional[float] = None
    sample_count: int = 0
    confidence: Optional[float] = None
    evidence_payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyChangeRiskAssessment:
    guardrail_passed: bool
    recommendation_grade: str
    blocked_by_guardrail_reason: str = ""
    data_quality_status: str = "OK"
    data_quality_issues: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyChangeApproval:
    approval_id: str
    proposal_id: str
    action: str
    previous_status: str
    next_status: str
    operator: str = ""
    note: str = ""
    created_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyChangeProposal:
    proposal_id: str
    trade_date: str
    created_at: str
    updated_at: str
    status: str
    recommendation_grade: str
    title: str
    summary_ko: str
    category: str
    target_component: str
    source_type: str
    source_ids: list[str]
    baseline_config_hash: str
    candidate_config_hash: str
    baseline_config_snapshot: dict[str, Any]
    candidate_config_patch: dict[str, Any]
    expected_effect_ko: str
    expected_risk_ko: str
    confidence: float
    net_benefit_score: float
    guardrail_passed: bool
    blocked_by_guardrail_reason: str
    data_quality_status: str
    data_quality_issues: list[str]
    rollout_plan: dict[str, Any]
    rollback_plan: dict[str, Any]
    operator_note: str = ""
    expires_at: str = ""
    superseded_by_proposal_id: str = ""
    evidence: list[StrategyChangeEvidence] = field(default_factory=list)
    config_diff: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = [item.to_dict() if hasattr(item, "to_dict") else item for item in self.evidence]
        return payload


@dataclass(frozen=True)
class StrategyChangeProposalSummary:
    total_count: int
    by_status: dict[str, int]
    by_grade: dict[str, int]
    by_category: dict[str, int]
    top_recommendations: list[dict[str, Any]]
    risky_count: int
    data_insufficient_count: int
    expiring_soon_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyConfigPatchBuilder:
    def __init__(self, db: Optional[TradingDatabase] = None) -> None:
        self.db = db

    def baseline_snapshot(self) -> dict[str, Any]:
        latest = self._latest_runtime_settings()
        if latest:
            return latest
        return {
            "market_gate": {"risk_off": {"leader_ready_small_enabled": False, "max_position_size_multiplier": 0.0, "min_theme_score": 70, "observe_only": True}},
            "entry_gate": {"late_chase": {"block_followers": False, "leader_wait_enabled": False, "min_pullback_from_high_pct": 0.0}},
            "entry_risk_gate": {
                "upper_limit_near": {"block_enabled": False},
                "vi_unknown_limit_risk": {"block_enabled": False},
                "high_return": {"followers_final_block": False},
            },
            "data_quality_gate": {
                "leader_observe_ready_enabled": False,
                "leader_observe_ready_order_enabled": False,
                "min_tick_quality_for_observe_ready": 0.0,
            },
            "exit_engine": {"theme_score_drop_exit_enabled": False, "giveback_exit_enabled": False, "max_giveback_pct": 3.0, "confirmation_cycles": 3},
            "position_sizing": {"ready_small_multiplier": 0.0, "max_position_amount_for_observe_candidate": 0},
            "live_order_enabled": False,
            "runtime_allow_live_orders": False,
        }

    def build_patch(self, patch: dict[str, Any], baseline: Optional[dict[str, Any]] = None) -> StrategyChangePatch:
        baseline_snapshot = copy.deepcopy(baseline or self.baseline_snapshot())
        candidate = copy.deepcopy(baseline_snapshot)
        diff: list[dict[str, Any]] = []
        forbidden: list[str] = []
        dangerous: list[str] = []
        for path, value in sorted((patch or {}).items()):
            before = _get_path(candidate, path)
            _set_path(candidate, path, value)
            risk = _patch_risk_level(path, before, value)
            if _is_forbidden_path(path):
                forbidden.append(path)
                risk = "forbidden"
            elif risk in {"high", "forbidden"}:
                dangerous.append(path)
            diff.append(
                {
                    "path": path,
                    "before": _mask_if_sensitive(path, before),
                    "after": _mask_if_sensitive(path, value),
                    "risk_level": risk,
                    "description_ko": _patch_description(path, value),
                }
            )
        baseline_hash = _hash_payload(baseline_snapshot)
        candidate_hash = _hash_payload(candidate)
        return StrategyChangePatch(
            patch=dict(patch or {}),
            baseline_config_hash=baseline_hash,
            candidate_config_hash=candidate_hash,
            baseline_config_snapshot=baseline_snapshot,
            diff=diff,
            forbidden_keys=forbidden,
            dangerous_keys=dangerous,
        )

    def _latest_runtime_settings(self) -> dict[str, Any]:
        if self.db is None:
            return {}
        try:
            row = self.db.conn.execute(
                """
                SELECT config_json, settings_json
                FROM strategy_runtime_settings
                WHERE enabled = 1
                ORDER BY config_version DESC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            return {}
        if not row:
            return {}
        return {
            "runtime_config": _loads(row["config_json"], {}),
            "settings": _loads(row["settings_json"], {}),
        }


class StrategyChangeProposalGenerator:
    def __init__(
        self,
        db: TradingDatabase,
        *,
        config: Optional[StrategyChangeProposalConfig] = None,
        replay_db_root: str | Path = DEFAULT_REPLAY_DB_ROOT,
        now_provider: Optional[callable] = None,
    ) -> None:
        self.db = db
        self.config = config or StrategyChangeProposalConfig()
        self.replay_db_root = Path(replay_db_root)
        self.now_provider = now_provider or (lambda: datetime.now().replace(microsecond=0))
        self.patch_builder = StrategyConfigPatchBuilder(db)

    def generate_from_intraday_outcomes(self, trade_date: str, window_sec: Optional[int] = None) -> list[StrategyChangeProposal]:
        summary = self.db.strategy_decision_outcome_summary(trade_date=trade_date, window_sec=window_sec)
        proposals: list[StrategyChangeProposal] = []
        sample_count = int(summary.get("outcome_count") or 0)
        data_quality_bucket_metrics = self._theme_lab_data_quality_bucket_metrics(trade_date)
        if int(summary.get("exit_too_late_count") or 0) > 0:
            proposals.append(
                self._build_proposal(
                    trade_date=trade_date,
                    source_type="intraday_outcome",
                    source_ids=[f"intraday_outcome:{trade_date}:exit_too_late"],
                    policy_key="fast_theme_exit_shadow",
                    evidence_metrics={
                        "sample_count": sample_count,
                        "exit_too_late_reduced_count": int(summary.get("exit_too_late_count") or 0),
                        "false_positive_increase_count": 0,
                        "net_benefit_score": float(summary.get("exit_too_late_count") or 0),
                        "confidence": 0.55,
                        **data_quality_bucket_metrics,
                    },
                    raw_source=summary,
                )
            )
        if int(summary.get("wait_block_opportunity_loss_count") or 0) > 0:
            proposals.append(
                self._build_proposal(
                    trade_date=trade_date,
                    source_type="intraday_outcome",
                    source_ids=[f"intraday_outcome:{trade_date}:opportunity_loss"],
                    policy_key="relaxed_data_wait_for_leader",
                    evidence_metrics={
                        "sample_count": sample_count,
                        "opportunity_loss_reduced_count": int(summary.get("wait_block_opportunity_loss_count") or 0),
                        "false_positive_increase_count": 0,
                        "net_benefit_score": float(summary.get("wait_block_opportunity_loss_count") or 0),
                        "confidence": 0.5,
                        **data_quality_bucket_metrics,
                    },
                    raw_source=summary,
                )
            )
        return proposals

    def _theme_lab_data_quality_bucket_metrics(self, trade_date: str) -> dict[str, Any]:
        try:
            report = ThemeLabGateReasonOutcomeAnalyzer(self.db).build_report(trade_date=trade_date or None, limit=10000)
        except Exception:
            return {}
        rows = list(((report.get("summary") or {}).get("by_data_quality_bucket") or [])[:8])
        metrics: dict[str, Any] = {"data_quality_bucket_group_count": len(rows)}
        for row in rows:
            bucket = _metric_key_segment(row.get("data_quality_bucket") or "unknown")
            if not bucket:
                continue
            metrics[f"data_quality_bucket_{bucket}_event_count"] = int(row.get("event_count") or 0)
            metrics[f"data_quality_bucket_{bucket}_missed_opportunity_count"] = int(row.get("missed_opportunity_count") or 0)
            metrics[f"data_quality_bucket_{bucket}_good_block_count"] = int(row.get("good_block_count") or 0)
            if row.get("avg_mfe_15m_pct") is not None:
                metrics[f"data_quality_bucket_{bucket}_avg_mfe_15m_pct"] = float(row.get("avg_mfe_15m_pct") or 0)
            if row.get("avg_mae_15m_pct") is not None:
                metrics[f"data_quality_bucket_{bucket}_avg_mae_15m_pct"] = float(row.get("avg_mae_15m_pct") or 0)
        return metrics

    def generate_from_shadow_summary(self, trade_date: str, horizon_sec: Optional[int] = None) -> list[StrategyChangeProposal]:
        summary = self.db.shadow_strategy_summary(trade_date=trade_date, horizon_sec=horizon_sec)
        proposals: list[StrategyChangeProposal] = []
        for policy in summary.get("policy_ranking") or []:
            policy_id = str(policy.get("policy_id") or "")
            if policy_id not in POLICY_PATCHES:
                continue
            proposals.append(
                self._build_proposal(
                    trade_date=trade_date,
                    source_type="shadow_strategy",
                    source_ids=[f"shadow_strategy:{trade_date}:{policy_id}"],
                    policy_key=policy_id,
                    evidence_metrics={
                        "sample_count": int(policy.get("total_count") or 0),
                        "changed_decision_count": int(policy.get("changed_decision_count") or 0),
                        "opportunity_loss_reduced_count": int(policy.get("opportunity_loss_reduced_count") or 0),
                        "false_positive_increase_count": int(policy.get("false_positive_increase_count") or 0),
                        "risk_block_effective_count": int(policy.get("risk_block_effective_count") or 0),
                        "exit_too_late_reduced_count": int(policy.get("exit_too_late_reduced_count") or 0),
                        "net_benefit_score": float(policy.get("estimated_net_benefit_score") or 0),
                        "confidence": float(policy.get("confidence") or 0),
                        "source_grade": policy.get("recommendation_grade") or "DATA_INSUFFICIENT",
                    },
                    raw_source=policy,
                )
            )
        return proposals

    def generate_from_replay_reports(self, trade_date: str, replay_id: Optional[str] = None) -> list[StrategyChangeProposal]:
        reports = self._load_replay_reports(trade_date=trade_date, replay_id=replay_id)
        proposals: list[StrategyChangeProposal] = []
        for report in reports:
            report_id = str(report.get("report_id") or report.get("replay_id") or "")
            replay_quality = str((report.get("summary") or {}).get("status") or "UNKNOWN")
            for ranking in report.get("recommendations") or []:
                policy_id = str(ranking.get("policy_id") or "")
                if policy_id not in POLICY_PATCHES:
                    continue
                proposals.append(
                    self._build_proposal(
                        trade_date=trade_date,
                        source_type="replay",
                        source_ids=[report_id, f"replay_policy:{policy_id}"],
                        policy_key=policy_id,
                        evidence_metrics={
                            "sample_count": int(ranking.get("changed_decision_count") or 0),
                            "changed_decision_count": int(ranking.get("changed_decision_count") or 0),
                            "opportunity_loss_reduced_count": int(ranking.get("estimated_opportunity_loss_reduced_count") or 0),
                            "false_positive_increase_count": int(ranking.get("estimated_false_positive_increase_count") or 0),
                            "risk_block_effective_count": int(ranking.get("risk_block_effective_count") or 0),
                            "exit_too_late_reduced_count": int(ranking.get("exit_too_late_reduced_count") or 0),
                            "net_benefit_score": float(ranking.get("net_benefit_score") or 0),
                            "confidence": float(ranking.get("confidence") or 0),
                            "source_grade": ranking.get("recommendation_grade") or "DATA_INSUFFICIENT",
                            "replay_count": 1,
                            "replay_data_quality": replay_quality,
                        },
                        raw_source={"report": report, "ranking": ranking},
                    )
                )
        return proposals

    def generate_from_threshold_ab(self, trade_date: str) -> list[StrategyChangeProposal]:
        reports = [
            self.db.get_dry_run_threshold_ab_report(row["report_id"])
            for row in self.db.list_dry_run_threshold_ab_reports(limit=50)
            if not trade_date or row.get("trade_date") == trade_date
        ]
        proposals: list[StrategyChangeProposal] = []
        for report in [item for item in reports if item]:
            report_id = str(report.get("report_id") or "")
            results = report.get("results") or {}
            for candidate in report.get("candidates") or []:
                result = results.get(str(candidate.get("candidate_id") or ""), candidate.get("result") or {})
                recommendation = result.get("recommendation") or {}
                grade = str(recommendation.get("grade") or candidate.get("recommendation_grade") or "")
                if grade not in {"STRONG_CANDIDATE", "WATCH_CANDIDATE"}:
                    continue
                delta = result.get("delta") or {}
                patch = _threshold_candidate_patch(candidate)
                policy_key = _policy_key_for_threshold(candidate)
                template = POLICY_PATCHES.get(policy_key, POLICY_PATCHES["generic_threshold"])
                proposals.append(
                    self._build_proposal(
                        trade_date=trade_date or report.get("trade_date") or "",
                        source_type="dry_run_threshold_ab",
                        source_ids=[report_id, str(candidate.get("candidate_id") or "")],
                        policy_key=policy_key,
                        override_template={**template, "patch": patch, "category": _normalize_category(candidate.get("category")), "target_component": _target_for_category(candidate.get("category"))},
                        evidence_metrics={
                            "sample_count": int(recommendation.get("sample_count") or candidate.get("sample_count") or 0),
                            "false_positive_reduced_count": int(delta.get("avoided_false_positive_count") or 0),
                            "false_positive_increase_count": int(delta.get("newly_created_false_negative_count") or 0),
                            "opportunity_loss_increase_count": max(0, int(delta.get("opportunity_loss_delta") or 0)),
                            "net_benefit_score": float(recommendation.get("expected_net_benefit_score") or candidate.get("expected_net_benefit_score") or 0),
                            "confidence": float(recommendation.get("confidence") or candidate.get("confidence") or 0),
                            "source_grade": grade,
                            "labeled_trade_days": int(recommendation.get("sample_trade_days") or 1),
                        },
                        raw_source={"report_id": report_id, "candidate": candidate, "result": result},
                    )
                )
        return proposals

    def generate_combined_proposals(self, trade_date: str) -> list[StrategyChangeProposal]:
        proposals = []
        proposals.extend(self.generate_from_intraday_outcomes(trade_date))
        proposals.extend(self.generate_from_shadow_summary(trade_date))
        proposals.extend(self.generate_from_replay_reports(trade_date))
        proposals.extend(self.generate_from_threshold_ab(trade_date))
        return _dedupe_proposals(proposals)

    def score_proposal(self, proposal: StrategyChangeProposal, evidence: Iterable[StrategyChangeEvidence]) -> StrategyChangeProposal:
        score = float(proposal.net_benefit_score or 0)
        confidence_values = [float(item.confidence or 0) for item in evidence if item.confidence is not None]
        confidence = max(confidence_values or [proposal.confidence])
        return replace(proposal, net_benefit_score=round(score, 3), confidence=round(confidence, 3))

    def apply_guardrails(
        self,
        proposal: StrategyChangeProposal,
        evidence: Iterable[StrategyChangeEvidence],
    ) -> StrategyChangeProposal:
        assessment = self._assess_guardrails(proposal, list(evidence or []))
        return replace(
            proposal,
            recommendation_grade=assessment.recommendation_grade,
            guardrail_passed=assessment.guardrail_passed,
            blocked_by_guardrail_reason=assessment.blocked_by_guardrail_reason,
            data_quality_status=assessment.data_quality_status,
            data_quality_issues=assessment.data_quality_issues,
        )

    def persist_proposals(self, proposals: Iterable[StrategyChangeProposal]) -> dict[str, Any]:
        proposal_list = list(proposals or [])
        saved = self.db.save_strategy_change_proposals([proposal.to_dict() for proposal in proposal_list])
        evidence_items = []
        for proposal in proposal_list:
            evidence_items.extend(item.to_dict() for item in proposal.evidence)
        saved_evidence = self.db.save_strategy_change_evidence(evidence_items)
        return {"proposal_count": len(proposal_list), "saved_count": saved, "evidence_count": saved_evidence}

    def build_summary(self, trade_date: Optional[str] = None) -> dict[str, Any]:
        return self.db.strategy_change_proposal_summary(trade_date=trade_date)

    def generate(
        self,
        *,
        trade_date: str,
        source_type: str = "combined",
        replay_id: Optional[str] = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "DISABLED", "proposals": [], "persisted": {"proposal_count": 0, "saved_count": 0, "evidence_count": 0}}
        if source_type == "intraday_outcome":
            proposals = self.generate_from_intraday_outcomes(trade_date)
        elif source_type == "shadow_strategy":
            proposals = self.generate_from_shadow_summary(trade_date)
        elif source_type == "replay":
            proposals = self.generate_from_replay_reports(trade_date, replay_id=replay_id)
        elif source_type == "threshold_ab":
            proposals = self.generate_from_threshold_ab(trade_date)
        else:
            proposals = self.generate_combined_proposals(trade_date)
        persisted = self.persist_proposals(proposals) if persist else {"proposal_count": len(proposals), "saved_count": 0, "evidence_count": 0}
        return {
            "status": "OK",
            "trade_date": trade_date,
            "source_type": source_type,
            "proposal_count": len(proposals),
            "proposals": [proposal.to_dict() for proposal in proposals],
            "persisted": persisted,
            "summary": self.build_summary(trade_date) if persist else {},
            "disclaimer_ko": "자동 적용 아님: 승인 상태만 저장하며 runtime config, LIVE, LIVE_SIM, 주문 경로는 변경하지 않습니다.",
        }

    def _build_proposal(
        self,
        *,
        trade_date: str,
        source_type: str,
        source_ids: list[str],
        policy_key: str,
        evidence_metrics: dict[str, Any],
        raw_source: dict[str, Any],
        override_template: Optional[dict[str, Any]] = None,
    ) -> StrategyChangeProposal:
        template = dict(override_template or POLICY_PATCHES[policy_key])
        patch = dict(template.get("patch") or {})
        patch_preview = self.patch_builder.build_patch(patch)
        now = self.now_provider()
        proposal_id = _stable_proposal_id(source_type=source_type, source_ids=source_ids, target_component=template["target_component"], patch=patch)
        evidence = _build_evidence_items(
            proposal_id=proposal_id,
            trade_date=trade_date,
            source_type=source_type,
            source_ids=source_ids,
            metrics=evidence_metrics,
            raw_source=raw_source,
        )
        source_grade = str(evidence_metrics.get("source_grade") or _grade_from_score(evidence_metrics))
        proposal = StrategyChangeProposal(
            proposal_id=proposal_id,
            trade_date=trade_date,
            created_at=now.isoformat(timespec="seconds"),
            updated_at=now.isoformat(timespec="seconds"),
            status="REVIEW_READY",
            recommendation_grade=source_grade,
            title=str(template.get("title") or policy_key),
            summary_ko=str(template.get("summary_ko") or ""),
            category=str(template.get("category") or "gate"),
            target_component=str(template.get("target_component") or ""),
            source_type=source_type,
            source_ids=list(source_ids),
            baseline_config_hash=patch_preview.baseline_config_hash,
            candidate_config_hash=patch_preview.candidate_config_hash,
            baseline_config_snapshot=patch_preview.baseline_config_snapshot,
            candidate_config_patch=patch,
            expected_effect_ko=str(template.get("expected_effect_ko") or ""),
            expected_risk_ko=str(template.get("expected_risk_ko") or ""),
            confidence=float(evidence_metrics.get("confidence") or 0.0),
            net_benefit_score=float(evidence_metrics.get("net_benefit_score") or 0.0),
            guardrail_passed=True,
            blocked_by_guardrail_reason="",
            data_quality_status="OK",
            data_quality_issues=[],
            rollout_plan=_rollout_plan(template, patch_preview),
            rollback_plan=_rollback_plan(patch_preview),
            expires_at=(now + timedelta(days=max(1, int(self.config.default_expire_days or 5)))).isoformat(timespec="seconds"),
            evidence=evidence,
            config_diff=patch_preview.diff,
        )
        proposal = self.score_proposal(proposal, evidence)
        return self.apply_guardrails(proposal, evidence)

    def _assess_guardrails(self, proposal: StrategyChangeProposal, evidence: list[StrategyChangeEvidence]) -> StrategyChangeRiskAssessment:
        grade = proposal.recommendation_grade if proposal.recommendation_grade in RECOMMENDATION_GRADES else "DATA_INSUFFICIENT"
        reasons: list[str] = []
        data_issues: list[str] = list(proposal.data_quality_issues or [])
        metrics = _metrics_from_evidence(evidence)
        patch_preview = self.patch_builder.build_patch(proposal.candidate_config_patch, baseline=proposal.baseline_config_snapshot)
        sample_count = int(metrics.get("sample_count") or 0)
        replay_count = int(metrics.get("replay_count") or 0)
        labeled_trade_days = int(metrics.get("labeled_trade_days") or 1)
        fp_increase = int(metrics.get("false_positive_increase_count") or 0)
        opportunity_loss_increase = int(metrics.get("opportunity_loss_increase_count") or 0)
        replay_quality = str(metrics.get("replay_data_quality") or "")

        if patch_preview.forbidden_keys:
            grade = "DO_NOT_APPLY"
            reasons.append("FORBIDDEN_CONFIG_KEY")
            data_issues.extend(patch_preview.forbidden_keys)
        if _patch_has_observe_only_false(proposal.candidate_config_patch):
            grade = "DO_NOT_APPLY"
            reasons.append("OBSERVE_ONLY_FALSE_NOT_ALLOWED_IN_THIS_PR")
        if _patch_promotes_data_insufficient_to_ready(proposal.candidate_config_patch):
            grade = "DO_NOT_APPLY"
            reasons.append("DATA_INSUFFICIENT_READY_PROMOTION_BLOCKED")
        if sample_count < int(self.config.min_sample_count or 0):
            grade = "DATA_INSUFFICIENT" if grade != "DO_NOT_APPLY" else grade
            reasons.append("MIN_SAMPLE_COUNT_NOT_MET")
        if fp_increase > int(self.config.max_fp_increase or 0):
            grade = "DO_NOT_APPLY" if fp_increase > int(self.config.max_fp_increase or 0) + 1 else "RISKY_CANDIDATE"
            reasons.append("FALSE_POSITIVE_INCREASE_GUARDRAIL")
        if opportunity_loss_increase > int(self.config.max_opportunity_loss_increase or 0):
            grade = _cap_grade(grade, "RISKY_CANDIDATE")
            reasons.append("OPPORTUNITY_LOSS_INCREASE_GUARDRAIL")
        if _patch_increases_order_amount(proposal.candidate_config_patch, proposal.baseline_config_snapshot):
            grade = _cap_grade(grade, "RISKY_CANDIDATE")
            reasons.append("ORDER_AMOUNT_INCREASE_RISK")
        if _patch_relaxes_vi_or_limit(proposal.candidate_config_patch):
            grade = _cap_grade(grade, "WATCH_CANDIDATE")
            reasons.append("VI_OR_UPPER_LIMIT_RELAXATION_NO_STRONG")
        if grade == "STRONG_CANDIDATE":
            if labeled_trade_days < int(self.config.min_trade_days or 1):
                grade = "WATCH_CANDIDATE"
                reasons.append("MIN_TRADE_DAYS_BLOCKS_STRONG")
            if replay_count < int(self.config.min_replay_count or 0):
                grade = "WATCH_CANDIDATE"
                reasons.append("MIN_REPLAY_COUNT_BLOCKS_STRONG")
            if replay_quality.upper() in {"LOW", "PARTIAL_REPLAY", "PARTIAL_BUNDLE"}:
                grade = "WATCH_CANDIDATE"
                reasons.append("REPLAY_QUALITY_BLOCKS_STRONG")
            if float(proposal.confidence or 0) < float(self.config.strong_min_confidence or 0):
                grade = "WATCH_CANDIDATE"
                reasons.append("CONFIDENCE_BLOCKS_STRONG")
        data_quality_status = "LOW" if any("QUALITY" in reason or "FORBIDDEN" in reason for reason in reasons) else "OK"
        if sample_count < int(self.config.min_sample_count or 0):
            data_quality_status = "INSUFFICIENT"
        guardrail_passed = grade not in {"DO_NOT_APPLY", "DATA_INSUFFICIENT"} and not any(reason.startswith("FORBIDDEN") for reason in reasons)
        return StrategyChangeRiskAssessment(
            guardrail_passed=guardrail_passed,
            recommendation_grade=grade,
            blocked_by_guardrail_reason=",".join(dict.fromkeys(reasons)),
            data_quality_status=data_quality_status,
            data_quality_issues=sorted(set(data_issues)),
            risk_notes=reasons,
        )

    def _load_replay_reports(self, *, trade_date: str, replay_id: Optional[str]) -> list[dict[str, Any]]:
        local_reports = self.db.list_strategy_replay_reports(trade_date=trade_date, replay_id=replay_id, limit=50)
        external_reports = scan_replay_reports(self.replay_db_root, trade_date=trade_date, replay_id=replay_id, limit=50)
        seen: set[str] = set()
        reports: list[dict[str, Any]] = []
        for report in [*local_reports, *external_reports]:
            key = str(report.get("report_id") or report.get("replay_id") or id(report))
            if key in seen:
                continue
            seen.add(key)
            reports.append(report)
        return reports


POLICY_PATCHES: dict[str, dict[str, Any]] = {
    "relaxed_risk_off_leader": {
        "title": "RISK_OFF leader READY_SMALL observe proposal",
        "summary_ko": "RISK_OFF 환경에서도 테마 주도주만 observe-only READY_SMALL 후보로 추적하는 제안입니다.",
        "category": "gate",
        "target_component": "market_gate",
        "expected_effect_ko": "주도주 기회손실을 줄이되 첫 단계는 observe-only로 검증합니다.",
        "expected_risk_ko": "약한 장에서 추격성 false positive가 늘 수 있습니다.",
        "patch": {
            "market_gate.risk_off.leader_ready_small_enabled": True,
            "market_gate.risk_off.max_position_size_multiplier": 0.3,
            "market_gate.risk_off.min_theme_score": 70,
            "market_gate.risk_off.observe_only": True,
        },
    },
    "strict_late_chase": {
        "title": "Late chase block tightening proposal",
        "summary_ko": "LATE_CHASE/CHASE_RISK 후보의 추격 진입을 더 보수적으로 차단하는 제안입니다.",
        "category": "risk",
        "target_component": "entry_gate",
        "expected_effect_ko": "상투 추격성 false positive를 줄입니다.",
        "expected_risk_ko": "강한 주도주 재가속 구간을 놓칠 수 있습니다.",
        "patch": {
            "entry_gate.late_chase.block_followers": True,
            "entry_gate.late_chase.leader_wait_enabled": True,
            "entry_gate.late_chase.min_pullback_from_high_pct": 1.0,
        },
    },
    "strict_entry_risk": {
        "title": "Entry risk gate conservative proposal",
        "summary_ko": "VI/상한가 근접/고수익률 후보에 대해 entry risk gate를 보수화하는 제안입니다.",
        "category": "risk",
        "target_component": "entry_risk_gate",
        "expected_effect_ko": "급등 말미의 drawdown 리스크를 줄입니다.",
        "expected_risk_ko": "일부 급등 지속 후보의 기회손실이 생길 수 있습니다.",
        "patch": {
            "entry_risk_gate.upper_limit_near.block_enabled": True,
            "entry_risk_gate.vi_unknown_limit_risk.block_enabled": True,
            "entry_risk_gate.high_return.followers_final_block": True,
        },
    },
    "relaxed_data_wait_for_leader": {
        "title": "Leader data-insufficient observe-ready proposal",
        "summary_ko": "데이터 부족으로 WAIT된 주도주를 실제 READY가 아닌 OBSERVE_READY 진단 상태로만 추적하는 제안입니다.",
        "category": "data_quality",
        "target_component": "data_quality_gate",
        "expected_effect_ko": "데이터 준비 지연 때문에 놓친 주도주를 진단 대상으로 보존합니다.",
        "expected_risk_ko": "데이터 품질이 낮은 후보의 노이즈가 늘 수 있습니다.",
        "patch": {
            "data_quality_gate.leader_observe_ready_enabled": True,
            "data_quality_gate.leader_observe_ready_order_enabled": False,
            "data_quality_gate.min_tick_quality_for_observe_ready": 0.7,
        },
    },
    "fast_theme_exit_shadow": {
        "title": "Fast exit giveback reduction proposal",
        "summary_ko": "HOLD 이후 수익 반납이 큰 케이스에 빠른 청산 조건을 observe-only 후보로 검토하는 제안입니다.",
        "category": "exit",
        "target_component": "exit_engine",
        "expected_effect_ko": "최고수익률 대비 반납폭을 줄입니다.",
        "expected_risk_ko": "추세 지속 구간에서 청산이 빨라질 수 있습니다.",
        "patch": {
            "exit_engine.theme_score_drop_exit_enabled": True,
            "exit_engine.giveback_exit_enabled": True,
            "exit_engine.max_giveback_pct": 2.0,
            "exit_engine.confirmation_cycles": 2,
        },
    },
    "order_guard_position_sizing": {
        "title": "Order guard position sizing proposal",
        "summary_ko": "반복 거절되는 READY 후보를 소액 관찰 후보로만 다루는 포지션 사이징 제안입니다.",
        "category": "position_sizing",
        "target_component": "position_sizing",
        "expected_effect_ko": "리스크를 늘리지 않고 거절 후보의 후속 흐름을 관찰합니다.",
        "expected_risk_ko": "실제 주문 금액 변경은 이번 PR 범위가 아니며 후속 검증이 필요합니다.",
        "patch": {
            "position_sizing.ready_small_multiplier": 0.3,
            "position_sizing.max_position_amount_for_observe_candidate": 300000,
        },
    },
    "generic_threshold": {
        "title": "Threshold A/B strategy proposal",
        "summary_ko": "DRY_RUN threshold A/B 추천을 proposal guardrail로 재검증한 제안입니다.",
        "category": "gate",
        "target_component": "hybrid_gate",
        "expected_effect_ko": "DRY_RUN false signal을 줄일 가능성이 있습니다.",
        "expected_risk_ko": "threshold A/B 결과를 그대로 적용하지 않고 추가 검토가 필요합니다.",
        "patch": {},
    },
}


def config_from_settings(settings: Any) -> StrategyChangeProposalConfig:
    return StrategyChangeProposalConfig(
        enabled=bool(getattr(settings, "change_proposal_enabled", True)),
        min_sample_count=int(getattr(settings, "change_proposal_min_sample_count", 20)),
        min_trade_days=int(getattr(settings, "change_proposal_min_trade_days", 2)),
        min_replay_count=int(getattr(settings, "change_proposal_min_replay_count", 1)),
        max_fp_increase=int(getattr(settings, "change_proposal_max_fp_increase", 1)),
        max_opportunity_loss_increase=int(getattr(settings, "change_proposal_max_opportunity_loss_increase", 1)),
        strong_min_confidence=float(getattr(settings, "change_proposal_strong_min_confidence", 0.7)),
        allow_auto_apply=False,
        default_expire_days=int(getattr(settings, "change_proposal_default_expire_days", 5)),
    )


def build_config_diff(proposal: dict[str, Any]) -> dict[str, Any]:
    builder = StrategyConfigPatchBuilder()
    preview = builder.build_patch(
        proposal.get("candidate_config_patch") or {},
        baseline=proposal.get("baseline_config_snapshot") or {},
    )
    return {
        "baseline_config_hash": preview.baseline_config_hash,
        "candidate_config_hash": preview.candidate_config_hash,
        "diff": preview.diff,
        "forbidden_keys": preview.forbidden_keys,
        "dangerous_keys": preview.dangerous_keys,
        "disclaimer_ko": "미리보기 전용입니다. 실제 runtime config는 변경하지 않습니다.",
    }


def _build_evidence_items(
    *,
    proposal_id: str,
    trade_date: str,
    source_type: str,
    source_ids: list[str],
    metrics: dict[str, Any],
    raw_source: dict[str, Any],
) -> list[StrategyChangeEvidence]:
    created_at = datetime.now().isoformat(timespec="seconds")
    items: list[StrategyChangeEvidence] = []
    source_id = ",".join(str(item) for item in source_ids if str(item))
    for metric_name, value in sorted(metrics.items()):
        if metric_name in {"source_grade", "replay_data_quality"}:
            continue
        numeric = _float_or_none(value)
        items.append(
            StrategyChangeEvidence(
                evidence_id=_stable_evidence_id(proposal_id, source_type, source_id, metric_name),
                proposal_id=proposal_id,
                source_type=source_type,
                source_id=source_id,
                trade_date=trade_date,
                metric_name=metric_name,
                metric_value=numeric,
                metric_unit="count" if metric_name.endswith("_count") or metric_name == "sample_count" else "",
                sample_count=int(metrics.get("sample_count") or 0),
                confidence=float(metrics.get("confidence") or 0) if metrics.get("confidence") is not None else None,
                evidence_payload={"metrics": metrics, "source": raw_source},
                created_at=created_at,
            )
        )
    return items


def _metrics_from_evidence(evidence: list[StrategyChangeEvidence]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for item in evidence:
        name = str(item.metric_name or "")
        if not name:
            continue
        if name in {"sample_count", "changed_decision_count", "replay_count", "labeled_trade_days"}:
            metrics[name] = max(int(metrics.get(name) or 0), int(item.metric_value or item.sample_count or 0))
        elif name.endswith("_count"):
            metrics[name] = int(metrics.get(name) or 0) + int(item.metric_value or 0)
        else:
            metrics[name] = item.metric_value
        payload_metrics = (item.evidence_payload or {}).get("metrics") or {}
        for key in ("source_grade", "replay_data_quality"):
            if payload_metrics.get(key):
                metrics[key] = payload_metrics.get(key)
    return metrics


def _stable_proposal_id(*, source_type: str, source_ids: list[str], target_component: str, patch: dict[str, Any]) -> str:
    payload = {
        "source_type": source_type,
        "source_ids": sorted(str(item) for item in source_ids if str(item)),
        "target_component": target_component,
        "patch": patch,
    }
    return f"scp_{hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:16]}"


def _stable_evidence_id(proposal_id: str, source_type: str, source_id: str, metric_name: str) -> str:
    raw = f"{proposal_id}:{source_type}:{source_id}:{metric_name}"
    return f"sce_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _metric_key_segment(value: Any) -> str:
    text = str(value or "").strip().lower()
    chars = [char if char.isalnum() else "_" for char in text]
    return "_".join(part for part in "".join(chars).split("_") if part)


def _dedupe_proposals(proposals: list[StrategyChangeProposal]) -> list[StrategyChangeProposal]:
    by_id: dict[str, StrategyChangeProposal] = {}
    for proposal in proposals:
        by_id.setdefault(proposal.proposal_id, proposal)
    return list(by_id.values())


def _grade_from_score(metrics: dict[str, Any]) -> str:
    sample_count = int(metrics.get("sample_count") or 0)
    confidence = float(metrics.get("confidence") or 0)
    score = float(metrics.get("net_benefit_score") or 0)
    if sample_count <= 0:
        return "DATA_INSUFFICIENT"
    if score < 0:
        return "RISKY_CANDIDATE"
    if score >= 3 and confidence >= 0.7:
        return "STRONG_CANDIDATE"
    return "WATCH_CANDIDATE"


def _cap_grade(current: str, cap: str) -> str:
    return cap if GRADE_ORDER.get(current, 99) < GRADE_ORDER.get(cap, 99) else current


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _get_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in str(path).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    current = payload
    parts = str(path).split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _patch_risk_level(path: str, before: Any, after: Any) -> str:
    lowered = path.lower()
    if _is_forbidden_path(path):
        return "forbidden"
    if any(token in lowered for token in ("order", "position", "risk", "vi", "upper_limit", "live")):
        return "high" if "live" in lowered else "medium"
    if before != after:
        return "medium"
    return "low"


def _is_forbidden_path(path: str) -> bool:
    lowered = path.lower()
    return any(part in lowered for part in FORBIDDEN_KEY_PARTS)


def _mask_if_sensitive(path: str, value: Any) -> Any:
    lowered = path.lower()
    if _is_forbidden_path(path) or any(part in lowered for part in ("token", "secret", "password", "account")):
        return "***"
    return value


def _patch_description(path: str, value: Any) -> str:
    if "risk_off" in path:
        return "RISK_OFF 조건의 주도주 관찰 후보 처리 방식을 바꿉니다."
    if "late_chase" in path:
        return "추격 진입 차단 기준을 조정합니다."
    if "entry_risk_gate" in path:
        return "진입 리스크 게이트 기준을 조정합니다."
    if "data_quality_gate" in path:
        return "데이터 부족 후보의 observe-only 진단 기준을 조정합니다."
    if "exit_engine" in path:
        return "청산/수익 반납 방지 조건을 조정합니다."
    if "position_sizing" in path:
        return "포지션 사이징 후보 기준을 조정합니다."
    return f"{path} 값을 {value}로 미리보기합니다."


def _rollout_plan(template: dict[str, Any], patch: StrategyChangePatch) -> dict[str, Any]:
    return {
        "phase": "observe_only",
        "steps": [
            "해당 patch를 observe-only shadow policy로 1거래일 추가 검증",
            "장중 outcome label에서 false_positive 증가 여부 확인",
            "replay 3거래일 이상에서 net benefit 양수 확인",
            "운영자 승인 후 DRY_RUN 후보로 승격",
        ],
        "success_criteria": [
            "false_positive_increase_count <= 1",
            "opportunity_loss_reduced_count >= 3",
            "data_quality_status != LOW",
        ],
        "stop_criteria": [
            "drawdown 관련 false positive 증가",
            "VI/상한가 근접 사고성 진입 후보 증가",
            "DATA_INSUFFICIENT 비율 증가",
        ],
        "auto_apply": False,
        "target_component": template.get("target_component", ""),
        "candidate_config_hash": patch.candidate_config_hash,
    }


def _rollback_plan(patch: StrategyChangePatch) -> dict[str, Any]:
    return {
        "rollback_type": "config_revert",
        "baseline_config_hash": patch.baseline_config_hash,
        "steps": [
            "candidate patch 비활성화",
            "baseline config hash로 복귀",
            "change proposal status를 REJECTED 또는 EXPIRED로 변경",
            "관련 replay/report 재생성",
        ],
    }


def _patch_has_observe_only_false(patch: dict[str, Any]) -> bool:
    return any("observe_only" in str(path).lower() and value is False for path, value in (patch or {}).items())


def _patch_promotes_data_insufficient_to_ready(patch: dict[str, Any]) -> bool:
    for path, value in (patch or {}).items():
        lowered = str(path).lower()
        if "data" in lowered and "ready" in lowered and "observe" not in lowered and bool(value):
            return True
        if "order_enabled" in lowered and bool(value):
            return True
    return False


def _patch_increases_order_amount(patch: dict[str, Any], baseline: dict[str, Any]) -> bool:
    for path, value in (patch or {}).items():
        lowered = str(path).lower()
        if "amount" not in lowered:
            continue
        before = _get_path(baseline or {}, path)
        try:
            if float(value) > float(before or 0):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _patch_relaxes_vi_or_limit(patch: dict[str, Any]) -> bool:
    for path, value in (patch or {}).items():
        lowered = str(path).lower()
        if any(token in lowered for token in ("vi", "upper_limit")) and value is False:
            return True
    return False


def _threshold_candidate_patch(candidate: dict[str, Any]) -> dict[str, Any]:
    parameter = str(candidate.get("parameter_name") or candidate.get("candidate_id") or "threshold").strip()
    value = candidate.get("candidate_value")
    target = _target_for_category(candidate.get("category"))
    normalized = parameter.replace(" ", "_").replace("/", "_")
    return {f"{target}.threshold_ab.{normalized}": value}


def _policy_key_for_threshold(candidate: dict[str, Any]) -> str:
    parameter = str(candidate.get("parameter_name") or candidate.get("candidate_id") or "").lower()
    if "late" in parameter or "chase" in parameter:
        return "strict_late_chase"
    if "risk" in parameter or "vi" in parameter or "upper" in parameter:
        return "strict_entry_risk"
    if "exit" in parameter:
        return "fast_theme_exit_shadow"
    return "generic_threshold"


def _normalize_category(value: Any) -> str:
    text = str(value or "gate").strip()
    if text == "safety":
        return "order_guard"
    return text if text in CHANGE_CATEGORIES else "gate"


def _target_for_category(value: Any) -> str:
    category = _normalize_category(value)
    return {
        "gate": "hybrid_gate",
        "risk": "entry_risk_gate",
        "theme": "theme_gate",
        "exit": "exit_engine",
        "session": "session_gate",
        "data_quality": "data_quality_gate",
        "order_guard": "order_guard",
        "position_sizing": "position_sizing",
    }.get(category, "hybrid_gate")


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
