from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Optional

from trading.broker.models import new_message_id, utc_timestamp


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "dry_run_threshold_ab"

GRADE_LABELS_KO = {
    "STRONG_CANDIDATE": "강한 후보",
    "WATCH_CANDIDATE": "관찰 후보",
    "RISKY_CANDIDATE": "위험 후보",
    "DATA_INSUFFICIENT": "데이터 부족",
    "DO_NOT_APPLY": "적용 비추천",
}

GRADE_LABELS_KO.update(
    {
        "OBSERVE_CANDIDATE": "관찰 후보",
        "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE": "기준 변경 표본 부족",
        "DATA_INSUFFICIENT_ATTRIBUTION_CONFIDENCE": "귀속 신뢰도 부족",
        "OPERATIONAL_REVIEW_ONLY": "운영 검토 전용",
    }
)

CATEGORY_LABELS_KO = {
    "gate": "게이트",
    "risk": "리스크",
    "theme": "테마",
    "session": "시간대",
    "safety": "안전장치",
}


@dataclass(frozen=True)
class ThresholdABConfig:
    min_sample_count: int = 10
    min_trade_days: int = 5
    min_completed_lifecycles: int = 30
    min_entry_intents: int = 30
    min_exit_decisions: int = 10
    min_signal_samples: int = 5
    strong_fp_reduction_min: int = 3
    max_fn_increase: int = 1
    max_opportunity_loss_increase: int = 1
    confidence_min: float = 0.5
    min_eligible_sample_ratio: float = 0.8
    export_root: Path = REPORT_ROOT
    enable_apply: bool = False


@dataclass
class ThresholdCandidate:
    candidate_id: str
    name: str
    label_ko: str
    category: str
    parameter_name: str
    baseline_value: Any = ""
    candidate_value: Any = ""
    operator: str = ""
    reason: str = ""
    reason_ko: str = ""
    expected_effect: str = ""
    expected_effect_ko: str = ""
    expected_risk: str = ""
    expected_risk_ko: str = ""
    source_metric: str = ""
    confidence: float = 0.0
    data_quality: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category_ko"] = CATEGORY_LABELS_KO.get(self.category, self.category)
        return payload


@dataclass
class ThresholdABScenario:
    scenario_id: str
    scenario_name: str
    label_ko: str
    candidates: list[ThresholdCandidate]
    created_at: str
    trade_date: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [candidate.to_dict() for candidate in self.candidates]
        return payload


@dataclass
class ThresholdABResult:
    scenario_id: str
    candidate_id: str
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    delta: dict[str, Any]
    recommendation: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    data_quality: list[str] = field(default_factory=list)
    affected_lifecycles: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DryRunThresholdABAnalyzer:
    def __init__(self, *, config: Optional[ThresholdABConfig] = None, report_root: Optional[Path] = None) -> None:
        self.config = config or ThresholdABConfig()
        if report_root is not None:
            self.config = ThresholdABConfig(**{**asdict(self.config), "export_root": report_root})
        self.report_root = self.config.export_root

    def build_report(
        self,
        performance_report: dict,
        *,
        trade_date: Optional[str] = None,
        filters: Optional[dict[str, Any]] = None,
        limit: int = 100,
        offset: int = 0,
        include_risky: bool = True,
    ) -> dict[str, Any]:
        full_items = list(performance_report.get("all_items") or performance_report.get("items") or [])
        attribution_quality = _attribution_quality(full_items)
        eligible_items = _eligible_attribution_items(full_items)
        eligible_report = {**performance_report, "items": eligible_items, "all_items": eligible_items}
        candidates = self.build_candidates(eligible_report)
        results = [self.simulate_candidate(eligible_items, candidate, total_items=full_items).to_dict() for candidate in candidates]
        result_by_id = {str(result.get("candidate_id") or ""): result for result in results}
        recommendations = self._build_recommendations(candidates, results, include_risky=include_risky)
        scenarios = [
            ThresholdABScenario(
                scenario_id=new_message_id("threshold_scenario"),
                scenario_name="single_candidate_review",
                label_ko="단일 후보별 사후 비교",
                candidates=candidates,
                created_at=utc_timestamp(),
                trade_date=trade_date or performance_report.get("trade_date", ""),
                filters=dict(filters or {}),
            ).to_dict()
        ]
        summary = {**self._summary(results, candidates), "attribution_quality": attribution_quality}
        start = max(0, int(offset or 0))
        end = start + max(1, int(limit or 100))
        visible_candidates = []
        for candidate in candidates:
            result = dict(result_by_id.get(candidate.candidate_id) or {})
            recommendation = dict(result.get("recommendation") or {})
            delta = dict(result.get("delta") or {})
            visible_candidates.append(
                {
                    **candidate.to_dict(),
                    "result": result,
                    "grade": recommendation.get("grade", ""),
                    "grade_ko": recommendation.get("grade_ko", ""),
                    "recommendation_grade": recommendation.get("grade", ""),
                    "expected_net_benefit_score": recommendation.get("expected_net_benefit_score", 0),
                    "sample_count": recommendation.get("sample_count", 0),
                    "total_samples": recommendation.get("total_samples", 0),
                    "eligible_samples": recommendation.get("eligible_samples", 0),
                    "excluded_legacy_samples": recommendation.get("excluded_legacy_samples", 0),
                    "excluded_low_confidence_samples": recommendation.get("excluded_low_confidence_samples", 0),
                    "excluded_ambiguous_samples": recommendation.get("excluded_ambiguous_samples", 0),
                    "eligible_sample_ratio": recommendation.get("eligible_sample_ratio", 0),
                    "attribution_confidence_distribution": recommendation.get("attribution_confidence_distribution", []),
                    "sample_trade_days": recommendation.get("sample_trade_days", 0),
                    "completed_lifecycle_count": recommendation.get("completed_lifecycle_count", 0),
                    "entry_intent_count": recommendation.get("entry_intent_count", 0),
                    "exit_decision_count": recommendation.get("exit_decision_count", 0),
                    "signal_sample_count": recommendation.get("signal_sample_count", 0),
                    "guardrail_passed": recommendation.get("guardrail_passed", False),
                    "blocked_by_guardrail_reason": recommendation.get("blocked_by_guardrail_reason", ""),
                    "candidate_trade_day_counts": recommendation.get("candidate_trade_day_counts", {}),
                    "confidence": recommendation.get("confidence", candidate.confidence),
                    "avoided_false_positive_count": delta.get("avoided_false_positive_count", 0),
                    "newly_created_false_negative_count": delta.get("newly_created_false_negative_count", 0),
                    "opportunity_loss_delta": delta.get("opportunity_loss_delta", 0),
                }
            )
        visible_candidates.sort(
            key=lambda item: (
                GRADE_SORT.get(str(item.get("recommendation_grade") or ""), 99),
                -(item.get("expected_net_benefit_score") or 0),
                -float(item.get("confidence") or 0),
            )
        )
        visible_candidates = visible_candidates[start:end]
        return {
            "report_id": new_message_id("threshold_ab"),
            "status": "READY",
            "generated_at": utc_timestamp(),
            "trade_date": trade_date or performance_report.get("trade_date", ""),
            "filters": dict(filters or {}),
            "summary": summary,
            "candidates": visible_candidates,
            "scenarios": scenarios,
            "results": result_by_id,
            "recommendations": recommendations,
            "total_candidates": len(candidates),
            "data_quality": self._data_quality(full_items, candidates),
            "disclaimer_ko": "이 리포트는 DRY_RUN 사후 분석 후보입니다. 실제 전략 설정에 자동 적용하지 않습니다.",
        }

    def build_candidates(self, report: dict) -> list[ThresholdCandidate]:
        items = list(report.get("all_items") or report.get("items") or [])
        candidates: list[ThresholdCandidate] = []
        candidates.extend(self._late_chase_candidates(items))
        candidates.extend(self._low_breadth_candidates(items))
        candidates.extend(self._score_threshold_candidates(items, "theme_score", "테마 점수 최소 기준 조정", "theme"))
        candidates.extend(self._score_threshold_candidates(items, "hybrid_score", "하이브리드 점수 최소 기준 조정", "gate"))
        candidates.extend(self._score_threshold_candidates(items, "gate_score", "게이트 점수 최소 기준 조정", "gate"))
        candidates.extend(self._session_candidates(items))
        candidates.extend(self._live_safety_candidates(items))
        unique: dict[str, ThresholdCandidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.candidate_id, candidate)
        return list(unique.values())

    def simulate_candidate(self, report_items: list[dict], candidate: ThresholdCandidate, *, total_items: Optional[list[dict]] = None) -> ThresholdABResult:
        total_items = list(total_items or report_items)
        attribution_quality = _attribution_quality(total_items, candidate=candidate)
        baseline_selected = [item for item in report_items if _baseline_selected(item)]
        baseline_metrics = _metrics(baseline_selected)
        affected = [item for item in report_items if _candidate_matches(candidate, item)]
        blocked_by_candidate = []
        newly_allowed = []
        candidate_selected = list(baseline_selected)

        if candidate.operator in {"block_if_contains", "min", "max", "penalty_if_contains"}:
            blocked_by_candidate = [item for item in baseline_selected if _candidate_matches(candidate, item)]
            blocked_ids = {item.get("lifecycle_id") for item in blocked_by_candidate}
            candidate_selected = [item for item in candidate_selected if item.get("lifecycle_id") not in blocked_ids]
        elif candidate.operator in {"allow_if_above", "allow_if_contains"}:
            newly_allowed = [
                item
                for item in report_items
                if item not in candidate_selected and _candidate_matches(candidate, item) and _looks_like_opportunity_loss(item)
            ]
            candidate_selected.extend(newly_allowed)

        candidate_metrics = _metrics(candidate_selected)
        avoided_fp = sum(1 for item in blocked_by_candidate if item.get("dry_run_false_positive_type"))
        newly_created_fn = sum(1 for item in blocked_by_candidate if _would_be_missed_good_trade(item))
        opportunity_loss_delta = (
            sum(1 for item in blocked_by_candidate if _would_be_missed_good_trade(item))
            - sum(1 for item in newly_allowed if _looks_like_opportunity_loss(item))
        )
        delta = {
            "candidate_entry_count_delta": candidate_metrics["entry_count"] - baseline_metrics["entry_count"],
            "blocked_by_candidate_count": len(blocked_by_candidate),
            "newly_allowed_count": len(newly_allowed),
            "avoided_false_positive_count": avoided_fp,
            "newly_created_false_negative_count": newly_created_fn,
            "opportunity_loss_delta": opportunity_loss_delta,
            "win_rate_delta": _delta(candidate_metrics.get("win_rate"), baseline_metrics.get("win_rate")),
            "avg_realized_return_delta": _delta(candidate_metrics.get("avg_realized_return_pct"), baseline_metrics.get("avg_realized_return_pct")),
            "avg_max_return_20m_delta": _delta(candidate_metrics.get("avg_max_return_20m"), baseline_metrics.get("avg_max_return_20m")),
            "avg_drawdown_delta": _delta(candidate_metrics.get("avg_max_drawdown_20m"), baseline_metrics.get("avg_max_drawdown_20m")),
            "false_positive_delta": candidate_metrics["false_positive_count"] - baseline_metrics["false_positive_count"],
            "false_negative_delta": newly_created_fn,
        }
        sample_count = len(affected)
        confidence = _confidence(sample_count, self.config.min_sample_count)
        net_benefit = round(
            avoided_fp * 2.0
            - newly_created_fn * 2.5
            - max(0, opportunity_loss_delta) * 2.0
            + (delta["win_rate_delta"] or 0.0) * 10.0
            + (delta["avg_realized_return_delta"] or 0.0) * 0.5,
            4,
        )
        raw_grade = _grade(
            sample_count=sample_count,
            confidence=confidence,
            avoided_fp=avoided_fp,
            newly_created_fn=newly_created_fn,
            opportunity_loss_delta=opportunity_loss_delta,
            net_benefit=net_benefit,
            config=self.config,
        )
        guardrail = _sample_guardrail(affected, candidate, self.config, attribution_quality=attribution_quality)
        grade = _guardrail_grade(raw_grade, guardrail, candidate)
        warnings = []
        if guardrail["blocked_by_guardrail_reason"]:
            warnings.append(f"THRESHOLD_AB_GUARDRAIL:{guardrail['blocked_by_guardrail_reason']}")
        if sample_count < self.config.min_sample_count:
            warnings.append("표본 수가 적어 관찰이 더 필요합니다.")
        if newly_created_fn > 0 or opportunity_loss_delta > 0:
            warnings.append("오탐 감소와 함께 미탐/기회손실이 늘 수 있습니다.")
        recommendation = {
            "grade": grade,
            "grade_ko": GRADE_LABELS_KO.get(grade, grade),
            "raw_grade": raw_grade,
            "expected_net_benefit_score": net_benefit,
            "confidence": confidence,
            "sample_count": sample_count,
            **attribution_quality,
            **guardrail,
            "reason_ko": candidate.reason_ko,
            "apply_enabled": False,
            "apply_note_ko": "이번 PR에서는 실제 설정 적용을 하지 않습니다.",
        }
        return ThresholdABResult(
            scenario_id=f"scenario:{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            baseline=baseline_metrics,
            candidate=candidate_metrics,
            delta=delta,
            recommendation=recommendation,
            warnings=warnings,
            data_quality=list(candidate.data_quality or []),
            affected_lifecycles=[_sample_item(item) for item in affected[:20]],
        )

    def export_json(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "candidate_id",
            "category",
            "parameter_name",
            "label_ko",
            "baseline_value",
            "candidate_value",
            "recommendation_grade",
            "sample_count",
            "total_samples",
            "eligible_samples",
            "excluded_legacy_samples",
            "excluded_low_confidence_samples",
            "excluded_ambiguous_samples",
            "eligible_sample_ratio",
            "sample_trade_days",
            "completed_lifecycle_count",
            "entry_intent_count",
            "exit_decision_count",
            "signal_sample_count",
            "guardrail_passed",
            "blocked_by_guardrail_reason",
            "confidence",
            "avoided_false_positive_count",
            "newly_created_false_negative_count",
            "opportunity_loss_delta",
            "win_rate_delta",
            "avg_return_delta",
            "avg_drawdown_delta",
            "reason_ko",
            "expected_effect_ko",
            "expected_risk_ko",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for candidate in report.get("candidates", []):
                result = (report.get("results") or {}).get(candidate.get("candidate_id"), {})
                recommendation = result.get("recommendation") or {}
                delta = result.get("delta") or {}
                writer.writerow(
                    {
                        "candidate_id": candidate.get("candidate_id", ""),
                        "category": candidate.get("category", ""),
                        "parameter_name": candidate.get("parameter_name", ""),
                        "label_ko": candidate.get("label_ko", ""),
                        "baseline_value": candidate.get("baseline_value", ""),
                        "candidate_value": candidate.get("candidate_value", ""),
                        "recommendation_grade": recommendation.get("grade", ""),
                        "sample_count": recommendation.get("sample_count", 0),
                        "total_samples": recommendation.get("total_samples", 0),
                        "eligible_samples": recommendation.get("eligible_samples", 0),
                        "excluded_legacy_samples": recommendation.get("excluded_legacy_samples", 0),
                        "excluded_low_confidence_samples": recommendation.get("excluded_low_confidence_samples", 0),
                        "excluded_ambiguous_samples": recommendation.get("excluded_ambiguous_samples", 0),
                        "eligible_sample_ratio": recommendation.get("eligible_sample_ratio", 0),
                        "sample_trade_days": recommendation.get("sample_trade_days", 0),
                        "completed_lifecycle_count": recommendation.get("completed_lifecycle_count", 0),
                        "entry_intent_count": recommendation.get("entry_intent_count", 0),
                        "exit_decision_count": recommendation.get("exit_decision_count", 0),
                        "signal_sample_count": recommendation.get("signal_sample_count", 0),
                        "guardrail_passed": recommendation.get("guardrail_passed", False),
                        "blocked_by_guardrail_reason": recommendation.get("blocked_by_guardrail_reason", ""),
                        "confidence": recommendation.get("confidence", 0),
                        "avoided_false_positive_count": delta.get("avoided_false_positive_count", 0),
                        "newly_created_false_negative_count": delta.get("newly_created_false_negative_count", 0),
                        "opportunity_loss_delta": delta.get("opportunity_loss_delta", 0),
                        "win_rate_delta": delta.get("win_rate_delta", ""),
                        "avg_return_delta": delta.get("avg_realized_return_delta", ""),
                        "avg_drawdown_delta": delta.get("avg_drawdown_delta", ""),
                        "reason_ko": candidate.get("reason_ko", ""),
                        "expected_effect_ko": candidate.get("expected_effect_ko", ""),
                        "expected_risk_ko": candidate.get("expected_risk_ko", ""),
                    }
                )
        return path

    def export_markdown(self, report: dict, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = report.get("summary") or {}
        lines = [
            "# DRY_RUN 기반 게이트/리스크 기준 A/B 제안 리포트",
            "",
            f"- 생성 시각: {report.get('generated_at', '')}",
            f"- 대상 거래일: {report.get('trade_date') or '전체'}",
            f"- 리포트 ID: {report.get('report_id', '')}",
            "- 주의: 이 리포트는 후보 제안이며 실제 전략 설정에 자동 적용하지 않습니다.",
            "",
            "## 요약",
            f"- 전체 후보: {summary.get('candidate_count', 0)}",
            f"- 강한 후보: {summary.get('strong_candidate_count', 0)}",
            f"- 관찰 후보: {summary.get('watch_candidate_count', 0)}",
            f"- 위험 후보: {summary.get('risky_candidate_count', 0)}",
            f"- 데이터 부족: {summary.get('data_insufficient_count', 0)}",
            f"- 예상 FP 감소: {summary.get('total_avoided_false_positive_count', 0)}",
            f"- 예상 FN 증가: {summary.get('total_new_false_negative_count', 0)}",
            f"- 예상 기회손실 변화: {summary.get('total_opportunity_loss_delta', 0)}",
            "",
            "## 추천 Top 10",
        ]
        lines.extend(
            [
                "",
                "## Guardrails",
                f"- sample_trade_days_min: {summary.get('guardrail_policy', {}).get('min_trade_days', 0)}",
                f"- completed_lifecycle_count_min: {summary.get('guardrail_policy', {}).get('min_completed_lifecycles', 0)}",
                f"- entry_intent_count_min: {summary.get('guardrail_policy', {}).get('min_entry_intents', 0)}",
                f"- exit_decision_count_min: {summary.get('guardrail_policy', {}).get('min_exit_decisions', 0)}",
                f"- signal_sample_count_min: {summary.get('guardrail_policy', {}).get('min_signal_samples', 0)}",
            f"- DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE: {summary.get('data_insufficient_for_threshold_change_count', 0)}",
            f"- DATA_INSUFFICIENT_ATTRIBUTION_CONFIDENCE: {summary.get('data_insufficient_attribution_confidence_count', 0)}",
            f"- OPERATIONAL_REVIEW_ONLY: {summary.get('operational_review_only_count', 0)}",
            "",
            "## Attribution Confidence Guardrail",
            "- legacy/low-confidence samples are reported for diagnostics only",
            "- they are not used for threshold recommendation",
            f"- total_samples: {(summary.get('attribution_quality') or {}).get('total_samples', 0)}",
            f"- eligible_samples: {(summary.get('attribution_quality') or {}).get('eligible_samples', 0)}",
            f"- excluded_legacy_samples: {(summary.get('attribution_quality') or {}).get('excluded_legacy_samples', 0)}",
            f"- excluded_low_confidence_samples: {(summary.get('attribution_quality') or {}).get('excluded_low_confidence_samples', 0)}",
            f"- excluded_ambiguous_samples: {(summary.get('attribution_quality') or {}).get('excluded_ambiguous_samples', 0)}",
            f"- eligible_sample_ratio: {(summary.get('attribution_quality') or {}).get('eligible_sample_ratio', 0)}",
        ]
    )
        for item in report.get("recommendations", [])[:10]:
            lines.extend(
                [
                    f"- **{item.get('label_ko', '')}** ({item.get('grade_ko', item.get('grade', ''))})",
                    f"  - 기준: `{item.get('parameter_name', '')}` {item.get('baseline_value', '')} -> {item.get('candidate_value', '')}",
                    f"  - 기대효과: {item.get('expected_effect_ko', '')}",
                    f"  - 예상리스크: {item.get('expected_risk_ko', '')}",
                ]
            )
        lines.extend(["", "## 후보 상세"])
        lines.extend(["", "## Guardrail Candidate Fields"])
        for candidate in report.get("candidates", [])[:10]:
            result = (report.get("results") or {}).get(candidate.get("candidate_id"), {})
            recommendation = result.get("recommendation") or {}
            lines.extend(
                [
                    f"- {candidate.get('candidate_id', '')}",
                    f"  - sample_trade_days: {recommendation.get('sample_trade_days', 0)}",
                    f"  - completed_lifecycle_count: {recommendation.get('completed_lifecycle_count', 0)}",
                    f"  - entry_intent_count: {recommendation.get('entry_intent_count', 0)}",
                    f"  - exit_decision_count: {recommendation.get('exit_decision_count', 0)}",
                    f"  - signal_sample_count: {recommendation.get('signal_sample_count', 0)}",
                    f"  - guardrail_passed: {recommendation.get('guardrail_passed', False)}",
                    f"  - blocked_by_guardrail_reason: {recommendation.get('blocked_by_guardrail_reason', '')}",
                    f"  - candidate_trade_day_counts: {json.dumps(recommendation.get('candidate_trade_day_counts', {}), ensure_ascii=False, sort_keys=True)}",
                ]
            )
        for candidate in report.get("candidates", [])[:50]:
            result = (report.get("results") or {}).get(candidate.get("candidate_id"), {})
            recommendation = result.get("recommendation") or {}
            delta = result.get("delta") or {}
            lines.extend(
                [
                    f"### {candidate.get('label_ko', candidate.get('candidate_id'))}",
                    f"- 등급: {recommendation.get('grade_ko', recommendation.get('grade', ''))}",
                    f"- 분류: {candidate.get('category_ko', candidate.get('category', ''))}",
                    f"- 표본 수: {recommendation.get('sample_count', 0)}",
                    f"- 기대 순효과 점수: {recommendation.get('expected_net_benefit_score', 0)}",
                    f"- FP 감소: {delta.get('avoided_false_positive_count', 0)}",
                    f"- FN 증가: {delta.get('newly_created_false_negative_count', 0)}",
                    f"- 기회손실 변화: {delta.get('opportunity_loss_delta', 0)}",
                    f"- 추천 이유: {candidate.get('reason_ko', '')}",
                    "",
                ]
            )
        lines.extend(
            [
                "## 실제 적용 전 확인사항",
                "- 최소 2~3거래일 이상 같은 방향의 결과가 반복되는지 확인한다.",
                "- FN/Opportunity Loss 증가가 감당 가능한 수준인지 확인한다.",
                "- 실제 적용은 별도 승인/적용 PR에서 strategy_runtime_settings를 변경한다.",
            ]
        )
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    def export_report(self, report: dict, *, fmt: str = "json") -> dict[str, str]:
        trade_date = str(report.get("trade_date") or datetime.now().date().isoformat())
        report_dir = self.report_root / trade_date
        stem = f"dry_run_threshold_ab_{trade_date}"
        exports: dict[str, str] = {}
        formats = ["json", "csv", "md"] if fmt == "all" else [fmt]
        for item in formats:
            if item == "json":
                exports["json"] = str(self.export_json(report, report_dir / f"{stem}.json"))
            elif item == "csv":
                exports["csv"] = str(self.export_csv(report, report_dir / f"{stem}.csv"))
            elif item in {"md", "markdown"}:
                exports["md"] = str(self.export_markdown(report, report_dir / f"{stem}.md"))
        return exports

    def _late_chase_candidates(self, items: list[dict]) -> list[ThresholdCandidate]:
        matched = [item for item in items if _contains_any(_reason_text(item), ["LATE_CHASE", "CHASE_RISK", "LATE_LAGGARD"])]
        if not matched:
            return []
        fp_rate = _ratio(sum(1 for item in matched if item.get("dry_run_false_positive_type")), len(matched))
        avg_return = _avg([item.get("realized_return_pct") for item in matched])
        if fp_rate < 0.25 and (avg_return is None or avg_return >= 0):
            return []
        return [
            ThresholdCandidate(
                candidate_id="risk:late_chase:block",
                name="late_chase_block_or_penalty",
                label_ko="추격매수 위험 차단 강화",
                category="risk",
                parameter_name="gate_reason_contains",
                baseline_value="allow_or_score",
                candidate_value="block:LATE_CHASE|CHASE_RISK|LATE_LAGGARD",
                operator="block_if_contains",
                reason="late chase related false positives",
                reason_ko="추격매수/후발주 사유의 오탐 비율이 높습니다.",
                expected_effect="reduce false positives",
                expected_effect_ko="늦은 추격 진입으로 인한 오탐을 줄일 수 있습니다.",
                expected_risk="miss late rallies",
                expected_risk_ko="늦게라도 이어지는 상승 종목을 놓칠 수 있습니다.",
                source_metric="gate_reason,false_positive_rate",
                confidence=_confidence(len(matched), self.config.min_sample_count),
                data_quality=[] if len(matched) >= self.config.min_sample_count else ["SAMPLE_COUNT_LOW"],
                metadata={"tokens": ["LATE_CHASE", "CHASE_RISK", "LATE_LAGGARD"], "sample_count": len(matched), "false_positive_rate": fp_rate},
            )
        ]

    def _low_breadth_candidates(self, items: list[dict]) -> list[ThresholdCandidate]:
        matched = [item for item in items if "LOW_BREADTH" in _reason_text(item).upper()]
        if not matched:
            return []
        opportunity = sum(1 for item in matched if item.get("opportunity_loss_type") or item.get("dry_run_false_negative_type"))
        if opportunity <= 0:
            return []
        return [
            ThresholdCandidate(
                candidate_id="gate:low_breadth:watch_allow",
                name="low_breadth_watch_allow_when_theme_strong",
                label_ko="시장 폭 약함 조건 완화 검토",
                category="gate",
                parameter_name="LOW_BREADTH_policy",
                baseline_value="block",
                candidate_value="allow_watch_if_theme_score>=70",
                operator="allow_if_contains",
                reason="LOW_BREADTH rejected but rallied",
                reason_ko="LOW_BREADTH로 막았지만 이후 상승한 사례가 있습니다.",
                expected_effect="reduce opportunity loss",
                expected_effect_ko="테마 점수가 높은 LOW_BREADTH 후보의 기회손실을 줄일 수 있습니다.",
                expected_risk="increase weak-market false positives",
                expected_risk_ko="장 약세 구간에서 손실 후보가 늘 수 있습니다.",
                source_metric="gate_reason,opportunity_loss",
                confidence=_confidence(len(matched), self.config.min_sample_count),
                data_quality=[] if len(matched) >= self.config.min_sample_count else ["SAMPLE_COUNT_LOW"],
                metadata={"tokens": ["LOW_BREADTH"], "sample_count": len(matched), "opportunity_count": opportunity, "theme_score_min": 70},
            )
        ]

    def _score_threshold_candidates(self, items: list[dict], field: str, label: str, category: str) -> list[ThresholdCandidate]:
        scored = [item for item in items if _float_or_none(item.get(field)) is not None]
        if not scored:
            return []
        candidates: list[ThresholdCandidate] = []
        for threshold in [60, 70, 80]:
            below = [item for item in scored if (_float_or_none(item.get(field)) or 0) < threshold]
            above = [item for item in scored if (_float_or_none(item.get(field)) or 0) >= threshold]
            if not below or not above:
                continue
            below_fp_rate = _ratio(sum(1 for item in below if item.get("dry_run_false_positive_type")), len(below))
            above_win_rate = _win_rate(above)
            if (below_fp_rate is not None and below_fp_rate >= 0.25) or (
                above_win_rate is not None and above_win_rate >= 0.5
            ):
                candidates.append(
                    ThresholdCandidate(
                        candidate_id=f"{category}:{field}:min_{threshold}",
                        name=f"{field}_min_{threshold}",
                        label_ko=f"{label} {threshold}",
                        category=category,
                        parameter_name=f"{field}_min",
                        baseline_value="current",
                        candidate_value=threshold,
                        operator="min",
                        reason=f"{field} low bucket underperformed",
                        reason_ko=f"{field}가 낮은 구간에서 오탐 또는 약한 성과가 관찰됐습니다.",
                        expected_effect="reduce weak score entries",
                        expected_effect_ko="점수가 낮은 진입을 줄여 오탐을 낮출 수 있습니다.",
                        expected_risk="miss low-score rebounds",
                        expected_risk_ko="낮은 점수에서도 반등한 종목을 놓칠 수 있습니다.",
                        source_metric=field,
                        confidence=_confidence(len(scored), self.config.min_sample_count),
                        data_quality=[] if len(scored) >= self.config.min_sample_count else ["SAMPLE_COUNT_LOW"],
                        metadata={
                            "field": field,
                            "threshold": threshold,
                            "below_count": len(below),
                            "above_count": len(above),
                            "below_false_positive_rate": below_fp_rate,
                            "above_win_rate": above_win_rate,
                        },
                    )
                )
        return candidates[:2]

    def _session_candidates(self, items: list[dict]) -> list[ThresholdCandidate]:
        result: list[ThresholdCandidate] = []
        buckets = sorted({str(item.get("session_bucket") or "") for item in items if item.get("session_bucket")})
        for bucket in buckets:
            matched = [item for item in items if str(item.get("session_bucket") or "") == bucket]
            if not matched:
                continue
            fp_rate = _ratio(sum(1 for item in matched if item.get("dry_run_false_positive_type")), len(matched))
            if fp_rate >= 0.3:
                result.append(
                    ThresholdCandidate(
                        candidate_id=f"session:{bucket}:tighten",
                        name=f"session_{bucket}_tighten",
                        label_ko=f"{bucket} 시간대 진입 기준 강화",
                        category="session",
                        parameter_name="session_bucket_policy",
                        baseline_value="same_threshold",
                        candidate_value=f"tighten:{bucket}",
                        operator="block_if_contains",
                        reason="session bucket false positive rate high",
                        reason_ko=f"{bucket} 시간대의 오탐 비율이 높습니다.",
                        expected_effect="reduce session-specific false positives",
                        expected_effect_ko="특정 시간대의 손실/오탐 진입을 줄일 수 있습니다.",
                        expected_risk="miss session-specific rallies",
                        expected_risk_ko="해당 시간대의 강한 반등을 놓칠 수 있습니다.",
                        source_metric="session_bucket",
                        confidence=_confidence(len(matched), self.config.min_sample_count),
                        data_quality=[] if len(matched) >= self.config.min_sample_count else ["SAMPLE_COUNT_LOW"],
                        metadata={"session_bucket": bucket, "sample_count": len(matched), "false_positive_rate": fp_rate},
                    )
                )
        return result

    def _live_safety_candidates(self, items: list[dict]) -> list[ThresholdCandidate]:
        reasons = sorted({str(item.get("entry_live_reject_reason") or "") for item in items if item.get("entry_live_reject_reason")})
        result: list[ThresholdCandidate] = []
        for reason in reasons:
            matched = [item for item in items if str(item.get("entry_live_reject_reason") or "") == reason]
            rallied = [item for item in matched if item.get("opportunity_loss_type") or item.get("dry_run_false_negative_type")]
            if not rallied:
                continue
            result.append(
                ThresholdCandidate(
                    candidate_id=f"safety:{_slug(reason)}:inspect",
                    name=f"safety_{_slug(reason)}_inspect",
                    label_ko="안전장치 차단 후 상승 사례 점검",
                    category="safety",
                    parameter_name="live_safety_reject_reason",
                    baseline_value=reason,
                    candidate_value="운영 상태 개선 검토",
                    operator="allow_if_contains",
                    reason="live safety rejected but rallied",
                    reason_ko=f"{reason} 때문에 LIVE였다면 막혔을 후보가 이후 상승했습니다.",
                    expected_effect="reduce operational opportunity loss",
                    expected_effect_ko="연결/계좌/장상태 문제로 인한 기회손실을 줄일 수 있습니다.",
                    expected_risk="do not relax safety automatically",
                    expected_risk_ko="안전장치는 자동 완화하면 안 됩니다. 운영 상태 개선 대상으로만 봅니다.",
                    source_metric="entry_live_reject_reason",
                    confidence=_confidence(len(matched), self.config.min_sample_count),
                    data_quality=["SAFETY_RELAXATION_NOT_RECOMMENDED"] + ([] if len(matched) >= self.config.min_sample_count else ["SAMPLE_COUNT_LOW"]),
                    metadata={"reject_reason": reason, "sample_count": len(matched), "rallied_count": len(rallied)},
                )
            )
        return result

    def _summary(self, results: list[dict], candidates: list[ThresholdCandidate]) -> dict[str, Any]:
        grades = [str((result.get("recommendation") or {}).get("grade") or "") for result in results]
        return {
            "candidate_count": len(candidates),
            "strong_candidate_count": grades.count("STRONG_CANDIDATE"),
            "watch_candidate_count": grades.count("WATCH_CANDIDATE"),
            "observe_candidate_count": grades.count("OBSERVE_CANDIDATE"),
            "risky_candidate_count": grades.count("RISKY_CANDIDATE"),
            "data_insufficient_count": grades.count("DATA_INSUFFICIENT"),
            "data_insufficient_for_threshold_change_count": grades.count("DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE"),
            "data_insufficient_attribution_confidence_count": grades.count("DATA_INSUFFICIENT_ATTRIBUTION_CONFIDENCE"),
            "operational_review_only_count": grades.count("OPERATIONAL_REVIEW_ONLY"),
            "do_not_apply_count": grades.count("DO_NOT_APPLY"),
            "total_avoided_false_positive_count": sum(int((result.get("delta") or {}).get("avoided_false_positive_count") or 0) for result in results),
            "total_new_false_negative_count": sum(int((result.get("delta") or {}).get("newly_created_false_negative_count") or 0) for result in results),
            "total_opportunity_loss_delta": sum(int((result.get("delta") or {}).get("opportunity_loss_delta") or 0) for result in results),
            "apply_enabled": False,
            "guardrail_policy": {
                "min_trade_days": self.config.min_trade_days,
                "min_completed_lifecycles": self.config.min_completed_lifecycles,
                "min_entry_intents": self.config.min_entry_intents,
                "min_exit_decisions": self.config.min_exit_decisions,
                "min_signal_samples": self.config.min_signal_samples,
                "min_sample_count": self.config.min_sample_count,
                "min_eligible_sample_ratio": self.config.min_eligible_sample_ratio,
            },
        }

    def _build_recommendations(self, candidates: list[ThresholdCandidate], results: list[dict], *, include_risky: bool) -> list[dict[str, Any]]:
        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        rows: list[dict[str, Any]] = []
        for result in results:
            recommendation = result.get("recommendation") or {}
            grade = str(recommendation.get("grade") or "")
            if not include_risky and grade in {"RISKY_CANDIDATE", "DO_NOT_APPLY"}:
                continue
            candidate = candidate_by_id.get(str(result.get("candidate_id") or ""))
            if candidate is None:
                continue
            rows.append(
                {
                    **candidate.to_dict(),
                    "grade": grade,
                    "grade_ko": GRADE_LABELS_KO.get(grade, grade),
                    "expected_net_benefit_score": recommendation.get("expected_net_benefit_score", 0),
                    "confidence": recommendation.get("confidence", candidate.confidence),
                    "sample_count": recommendation.get("sample_count", 0),
                    "total_samples": recommendation.get("total_samples", 0),
                    "eligible_samples": recommendation.get("eligible_samples", 0),
                    "excluded_legacy_samples": recommendation.get("excluded_legacy_samples", 0),
                    "excluded_low_confidence_samples": recommendation.get("excluded_low_confidence_samples", 0),
                    "excluded_ambiguous_samples": recommendation.get("excluded_ambiguous_samples", 0),
                    "eligible_sample_ratio": recommendation.get("eligible_sample_ratio", 0),
                    "sample_trade_days": recommendation.get("sample_trade_days", 0),
                    "completed_lifecycle_count": recommendation.get("completed_lifecycle_count", 0),
                    "entry_intent_count": recommendation.get("entry_intent_count", 0),
                    "exit_decision_count": recommendation.get("exit_decision_count", 0),
                    "signal_sample_count": recommendation.get("signal_sample_count", 0),
                    "guardrail_passed": recommendation.get("guardrail_passed", False),
                    "blocked_by_guardrail_reason": recommendation.get("blocked_by_guardrail_reason", ""),
                    "candidate_trade_day_counts": recommendation.get("candidate_trade_day_counts", {}),
                    "delta": result.get("delta", {}),
                    "warnings": result.get("warnings", []),
                }
            )
        rows.sort(key=lambda item: (GRADE_SORT.get(item.get("grade", ""), 99), -(item.get("expected_net_benefit_score") or 0), -float(item.get("confidence") or 0)))
        return rows

    def _data_quality(self, items: list[dict], candidates: list[ThresholdCandidate]) -> dict[str, Any]:
        missing_fields = []
        for field in ["theme_score", "hybrid_score", "gate_score", "session_bucket", "gate_reason"]:
            missing = sum(1 for item in items if item.get(field) in {None, ""})
            if missing:
                missing_fields.append({"field": field, "missing_count": missing})
        return {
            "item_count": len(items),
            "candidate_count": len(candidates),
            "missing_fields": missing_fields,
            "notes": ["표본 수가 적거나 필드가 비어 있으면 후보 등급을 낮춥니다."],
        }


GRADE_SORT = {
    "STRONG_CANDIDATE": 0,
    "OBSERVE_CANDIDATE": 1,
    "WATCH_CANDIDATE": 1,
    "RISKY_CANDIDATE": 2,
    "OPERATIONAL_REVIEW_ONLY": 3,
    "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE": 4,
    "DATA_INSUFFICIENT_ATTRIBUTION_CONFIDENCE": 4,
    "DATA_INSUFFICIENT": 5,
    "DO_NOT_APPLY": 6,
}


def config_from_settings(settings: Any) -> ThresholdABConfig:
    return ThresholdABConfig(
        min_sample_count=int(getattr(settings, "threshold_ab_min_sample_count", 10)),
        min_trade_days=int(getattr(settings, "threshold_ab_min_trade_days", 5)),
        min_completed_lifecycles=int(getattr(settings, "threshold_ab_min_completed_lifecycles", 30)),
        min_entry_intents=int(getattr(settings, "threshold_ab_min_entry_intents", 30)),
        min_exit_decisions=int(getattr(settings, "threshold_ab_min_exit_decisions", 10)),
        min_signal_samples=int(getattr(settings, "threshold_ab_min_signal_samples", 5)),
        strong_fp_reduction_min=int(getattr(settings, "threshold_ab_strong_fp_reduction_min", 3)),
        max_fn_increase=int(getattr(settings, "threshold_ab_max_fn_increase", 1)),
        max_opportunity_loss_increase=int(getattr(settings, "threshold_ab_max_opportunity_loss_increase", 1)),
        confidence_min=float(getattr(settings, "threshold_ab_confidence_min", 0.5)),
        min_eligible_sample_ratio=float(getattr(settings, "threshold_ab_min_eligible_sample_ratio", 0.8)),
        export_root=Path(getattr(settings, "threshold_ab_export_root", REPORT_ROOT)),
        enable_apply=bool(getattr(settings, "threshold_ab_enable_apply", False)),
    )


def _eligible_attribution_items(items: list[dict]) -> list[dict]:
    return [item for item in items if _attribution_eligible(item)]


def _attribution_eligible(item: dict) -> bool:
    if bool(item.get("legacy_low_confidence_sample")):
        return False
    confidence = _item_attribution_confidence(item)
    if confidence in {"LOW", "AMBIGUOUS"}:
        return False
    if str(item.get("matched_by") or "") == "weak_code_date_fallback":
        return False
    return confidence in {"HIGH", "MEDIUM"}


def _item_attribution_confidence(item: dict) -> str:
    confidence = str(item.get("attribution_confidence") or "").upper()
    if confidence:
        return confidence
    matched_by = str(item.get("matched_by") or "")
    link_confidence = str(item.get("link_confidence") or "").upper()
    if matched_by in {"candidate_instance_id", "virtual_position_id", "virtual_order_to_position"}:
        return "HIGH"
    if matched_by == "candidate_id":
        return "MEDIUM"
    if matched_by == "ambiguous_code_date_fallback" or "AMBIGUOUS_CANDIDATE_LINK" in (item.get("data_quality_issues") or []):
        return "AMBIGUOUS"
    if matched_by == "weak_code_date_fallback":
        return "LOW"
    return link_confidence or "LOW"


def _attribution_quality(items: list[dict], *, candidate: Optional[ThresholdCandidate] = None) -> dict[str, Any]:
    relevant = [item for item in items if candidate is None or _candidate_matches(candidate, item)]
    total = len(relevant)
    eligible = [item for item in relevant if _attribution_eligible(item)]
    legacy = [item for item in relevant if bool(item.get("legacy_low_confidence_sample"))]
    low = [item for item in relevant if _item_attribution_confidence(item) == "LOW" or str(item.get("matched_by") or "") == "weak_code_date_fallback"]
    ambiguous = [item for item in relevant if _item_attribution_confidence(item) == "AMBIGUOUS"]
    distribution: dict[str, int] = {}
    for item in relevant:
        confidence = _item_attribution_confidence(item)
        distribution[confidence] = distribution.get(confidence, 0) + 1
    return {
        "total_samples": total,
        "eligible_samples": len(eligible),
        "excluded_legacy_samples": len(legacy),
        "excluded_low_confidence_samples": len(low),
        "excluded_ambiguous_samples": len(ambiguous),
        "eligible_sample_ratio": _ratio(len(eligible), total) if total else 0.0,
        "attribution_confidence_distribution": [
            {"confidence": key, "count": count}
            for key, count in sorted(distribution.items(), key=lambda pair: pair[0])
        ],
    }


def _baseline_selected(item: dict) -> bool:
    return bool(item.get("entry_intent_id")) and str(item.get("entry_intent_status") or "") in {"DRY_RUN_ACCEPTED", "ACCEPTED"}


def _candidate_matches(candidate: ThresholdCandidate, item: dict) -> bool:
    metadata = candidate.metadata or {}
    if candidate.parameter_name.endswith("_min"):
        field = str(metadata.get("field") or candidate.parameter_name.replace("_min", ""))
        threshold = _float_or_none(candidate.candidate_value)
        value = _float_or_none(item.get(field))
        return value is not None and threshold is not None and value < threshold
    if candidate.parameter_name == "LOW_BREADTH_policy":
        return "LOW_BREADTH" in _reason_text(item).upper() and (_float_or_none(item.get("theme_score")) or 0.0) >= float(metadata.get("theme_score_min") or 70)
    if candidate.parameter_name == "gate_reason_contains":
        return _contains_any(_reason_text(item), list(metadata.get("tokens") or []))
    if candidate.parameter_name == "session_bucket_policy":
        return str(item.get("session_bucket") or "") == str(metadata.get("session_bucket") or "")
    if candidate.parameter_name == "live_safety_reject_reason":
        return str(item.get("entry_live_reject_reason") or "") == str(metadata.get("reject_reason") or candidate.baseline_value)
    return False


def _metrics(items: list[dict]) -> dict[str, Any]:
    completed = [item for item in items if item.get("realized_return_pct") is not None or item.get("final_status")]
    wins = [item for item in completed if (_float_or_none(item.get("realized_return_pct")) or 0.0) > 0.0]
    return {
        "entry_count": len(items),
        "completed_count": len(completed),
        "win_rate": _ratio(len(wins), len(completed)),
        "avg_realized_return_pct": _avg([item.get("realized_return_pct") for item in completed]),
        "avg_max_return_20m": _avg([item.get("max_return_20m") for item in items]),
        "avg_max_drawdown_20m": _avg([item.get("max_drawdown_20m") for item in items]),
        "false_positive_count": sum(1 for item in items if item.get("dry_run_false_positive_type")),
        "false_negative_count": sum(1 for item in items if item.get("dry_run_false_negative_type")),
        "opportunity_loss_count": sum(1 for item in items if item.get("opportunity_loss_type")),
    }


def _would_be_missed_good_trade(item: dict) -> bool:
    if str(item.get("signal_classification") or "") == "true_positive":
        return True
    realized = _float_or_none(item.get("realized_return_pct"))
    max_return = _float_or_none(item.get("max_return_20m"))
    return (realized is not None and realized > 0.0) or (max_return is not None and max_return >= 3.0)


def _looks_like_opportunity_loss(item: dict) -> bool:
    return bool(item.get("opportunity_loss_type") or item.get("dry_run_false_negative_type")) or (
        (_float_or_none(item.get("max_return_20m")) or 0.0) >= 3.0 and not _baseline_selected(item)
    )


def _grade(
    *,
    sample_count: int,
    confidence: float,
    avoided_fp: int,
    newly_created_fn: int,
    opportunity_loss_delta: int,
    net_benefit: float,
    config: ThresholdABConfig,
) -> str:
    if sample_count < config.min_sample_count:
        return "DATA_INSUFFICIENT"
    if net_benefit <= 0 and avoided_fp <= 0:
        return "DO_NOT_APPLY"
    if newly_created_fn > config.max_fn_increase or opportunity_loss_delta > config.max_opportunity_loss_increase:
        return "RISKY_CANDIDATE"
    if avoided_fp >= config.strong_fp_reduction_min and confidence >= config.confidence_min and net_benefit > 0:
        return "STRONG_CANDIDATE"
    if net_benefit > 0 or avoided_fp > 0 or opportunity_loss_delta < 0:
        return "WATCH_CANDIDATE"
    return "DO_NOT_APPLY"


def _sample_guardrail(
    items: list[dict],
    candidate: ThresholdCandidate,
    config: ThresholdABConfig,
    *,
    attribution_quality: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    attribution_quality = dict(attribution_quality or {})
    trade_day_counts: dict[str, int] = {}
    for item in items:
        trade_date = str(item.get("trade_date") or "")
        if trade_date:
            trade_day_counts[trade_date] = trade_day_counts.get(trade_date, 0) + 1
    completed = sum(1 for item in items if item.get("realized_return_pct") is not None or item.get("final_status"))
    entry_intents = sum(1 for item in items if item.get("entry_intent_id") or item.get("entry_intent_status"))
    exit_decisions = sum(
        _exit_decision_count(item)
        for item in items
    )
    signal_samples = sum(
        1
        for item in items
        if item.get("signal_classification")
        or item.get("dry_run_false_positive_type")
        or item.get("dry_run_false_negative_type")
        or item.get("opportunity_loss_type")
    )
    reasons: list[str] = []
    if len(trade_day_counts) < config.min_trade_days:
        reasons.append("MIN_TRADE_DAYS")
    if completed < config.min_completed_lifecycles:
        reasons.append("MIN_COMPLETED_LIFECYCLES")
    if entry_intents < config.min_entry_intents:
        reasons.append("MIN_ENTRY_INTENTS")
    if exit_decisions < config.min_exit_decisions:
        reasons.append("MIN_EXIT_DECISIONS")
    if signal_samples < config.min_signal_samples:
        reasons.append("MIN_SIGNAL_SAMPLES")
    if len(items) < config.min_sample_count:
        reasons.append("MIN_SAMPLE_COUNT")
    if float(attribution_quality.get("eligible_sample_ratio") or 0.0) < float(config.min_eligible_sample_ratio or 0.8):
        reasons.append("ATTRIBUTION_CONFIDENCE_RATIO")
    if len(items) >= config.min_sample_count and int(attribution_quality.get("eligible_samples") or 0) < config.min_sample_count:
        reasons.append("ATTRIBUTION_ELIGIBLE_SAMPLE_COUNT")
    if candidate.category == "safety" or _contains_any(f"{candidate.parameter_name} {candidate.reason} {candidate.expected_risk}", ["safety", "live_safety"]):
        reasons.append("OPERATIONAL_REVIEW_ONLY")
    return {
        "sample_trade_days": len(trade_day_counts),
        "completed_lifecycle_count": completed,
        "entry_intent_count": entry_intents,
        "exit_decision_count": exit_decisions,
        "signal_sample_count": signal_samples,
        "guardrail_passed": not reasons,
        "blocked_by_guardrail_reason": ",".join(reasons),
        "candidate_trade_day_counts": dict(sorted(trade_day_counts.items())),
    }


def _guardrail_grade(raw_grade: str, guardrail: dict[str, Any], candidate: ThresholdCandidate) -> str:
    blocked_reason = str(guardrail.get("blocked_by_guardrail_reason") or "")
    if "OPERATIONAL_REVIEW_ONLY" in blocked_reason or candidate.category == "safety":
        return "OPERATIONAL_REVIEW_ONLY"
    if "ATTRIBUTION_CONFIDENCE_RATIO" in blocked_reason or "ATTRIBUTION_ELIGIBLE_SAMPLE_COUNT" in blocked_reason:
        return "DATA_INSUFFICIENT_ATTRIBUTION_CONFIDENCE"
    if blocked_reason:
        return "DATA_INSUFFICIENT_FOR_THRESHOLD_CHANGE"
    if raw_grade == "WATCH_CANDIDATE":
        return "OBSERVE_CANDIDATE"
    return raw_grade


def _exit_decision_count(item: dict) -> int:
    for key in ("exit_decision_ids", "exit_decision_types", "exit_reasons"):
        value = item.get(key)
        if isinstance(value, list):
            return len([part for part in value if part])
    return 1 if item.get("exit_decision_id") or item.get("exit_decision_type") or item.get("exit_reason") else 0


def _confidence(sample_count: int, min_sample_count: int) -> float:
    if min_sample_count <= 0:
        return 1.0
    return round(min(1.0, max(0.0, sample_count / float(min_sample_count * 2))), 4)


def _reason_text(item: dict) -> str:
    parts = [
        item.get("gate_reason"),
        item.get("entry_decision_safety_reason"),
        item.get("entry_live_reject_reason"),
        " ".join(str(value) for value in item.get("exit_reasons") or []),
        json.dumps(item.get("details") or {}, ensure_ascii=False),
    ]
    return " ".join(str(part or "") for part in parts)


def _contains_any(text: str, tokens: list[str]) -> bool:
    upper = text.upper()
    return any(str(token or "").upper() in upper for token in tokens)


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _avg(values: list[Any]) -> Optional[float]:
    parsed = [_float_or_none(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(sum(parsed) / len(parsed), 4)


def _win_rate(items: list[dict]) -> Optional[float]:
    completed = [item for item in items if item.get("realized_return_pct") is not None]
    wins = [item for item in completed if (_float_or_none(item.get("realized_return_pct")) or 0.0) > 0.0]
    return _ratio(len(wins), len(completed))


def _delta(candidate: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if candidate is None or baseline is None:
        return None
    return round(candidate - baseline, 4)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample_item(item: dict) -> dict[str, Any]:
    keys = [
        "lifecycle_id",
        "trade_date",
        "code",
        "theme_name",
        "gate_reason",
        "session_bucket",
        "entry_intent_status",
        "entry_live_reject_reason",
        "realized_return_pct",
        "max_return_20m",
        "max_drawdown_20m",
        "dry_run_false_positive_type",
        "dry_run_false_negative_type",
        "opportunity_loss_type",
        "theme_score",
        "hybrid_score",
        "gate_score",
    ]
    return {key: item.get(key) for key in keys}


def _slug(value: str) -> str:
    result = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    return "_".join(part for part in result.split("_") if part)[:60] or "unknown"
