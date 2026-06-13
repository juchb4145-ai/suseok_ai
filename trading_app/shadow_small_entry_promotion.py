from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository, legacy_strategy_runtime_settings
from trading.strategy.shadow_small_entry_promotion import (
    MODE_LIVE_SIM_GUARDED,
    MODE_OBSERVE_ONLY,
    READY_SHADOW_SMALL_ENTRY,
    STATUS_BLOCKED,
    STATUS_NO_EVIDENCE,
    STATUS_OBSERVE_ONLY,
    STATUS_PROMOTED,
    ShadowSmallEntryPromotionConfig,
    evaluate_shadow_small_entry_promotion,
    config_from_settings,
    trace_payload_from_evaluation,
)
from trading_app.conservative_reason_outcomes import ConservativeReasonOutcomeAnalyzer
from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports" / "shadow_small_entry_promotion"
TRACE_STAGES = (
    "SHADOW_SMALL_ENTRY_EVIDENCE_LOADED",
    "SHADOW_SMALL_ENTRY_CANDIDATE_EVALUATED",
    "SHADOW_SMALL_ENTRY_OBSERVE_ONLY",
    "SHADOW_SMALL_ENTRY_PROMOTED",
    "SHADOW_SMALL_ENTRY_BLOCKED",
    "SHADOW_SMALL_ENTRY_ORDER_SUBMITTED",
    "SHADOW_SMALL_ENTRY_ORDER_BLOCKED",
)
CSV_COLUMNS = [
    "trade_date",
    "code",
    "name",
    "candidate_instance_id",
    "promotion_status",
    "final_status",
    "strategy_eligible",
    "rejected_reason",
    "reason_codes",
    "reason_group",
    "reason_code",
    "sample_count",
    "missed_opportunity_rate",
    "risk_avoided_rate",
    "good_block_rate",
    "avg_mfe_15m_pct",
    "avg_mae_15m_pct",
    "position_size_multiplier",
    "operator_message_ko",
]


@dataclass(frozen=True)
class ShadowSmallEntryPromotionEvidence:
    available: bool
    status: str
    source_reports: list[str]
    report_id: str = ""
    source_report_trade_date: str = ""
    report_age_days: Optional[int] = None
    sample_quality: str = "NO_DATA"
    eligible_reason_codes: list[str] = None  # type: ignore[assignment]
    eligible_reason_groups: list[str] = None  # type: ignore[assignment]
    blocked_reason_codes: list[str] = None  # type: ignore[assignment]
    reason_code_rows: list[dict[str, Any]] = None  # type: ignore[assignment]
    group_rows: list[dict[str, Any]] = None  # type: ignore[assignment]
    scenario_scores: list[dict[str, Any]] = None  # type: ignore[assignment]
    best_scenario: dict[str, Any] = None  # type: ignore[assignment]
    operator_message_ko: str = ""
    warnings: list[str] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("eligible_reason_codes", "eligible_reason_groups", "blocked_reason_codes", "reason_code_rows", "group_rows", "scenario_scores", "warnings"):
            payload[key] = list(payload.get(key) or [])
        payload["best_scenario"] = dict(payload.get("best_scenario") or {})
        return payload


class ShadowSmallEntryPromotionAnalyzer:
    def __init__(
        self,
        db,
        *,
        settings: Any | None = None,
        config: Optional[ShadowSmallEntryPromotionConfig] = None,
        report_root: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.settings = settings if settings is not None else self._load_settings()
        self.config = config or config_from_settings(self.settings)
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def load_evidence(self, *, trade_date: Optional[str] = None, limit: int = 50000) -> dict[str, Any]:
        cfg = self.config
        if not cfg.enabled:
            return ShadowSmallEntryPromotionEvidence(
                available=False,
                status="DISABLED",
                source_reports=list(cfg.source_reports),
                eligible_reason_codes=[],
                eligible_reason_groups=[],
                blocked_reason_codes=list(cfg.blocked_reason_codes),
                reason_code_rows=[],
                group_rows=[],
                scenario_scores=[],
                best_scenario={},
                operator_message_ko="소액 승격 정책이 비활성화되어 있습니다.",
                warnings=[],
            ).to_dict()
        warnings: list[str] = []
        try:
            conservative = ConservativeReasonOutcomeAnalyzer(self.db).build_report(trade_date=trade_date, limit=limit)
        except Exception as exc:
            conservative = {}
            warnings.append(f"CONSERVATIVE_REASON_REPORT_ERROR:{exc}")
        try:
            themelab = ThemeLabGateReasonOutcomeAnalyzer(self.db).build_report(trade_date=trade_date, limit=limit)
        except Exception as exc:
            themelab = {}
            warnings.append(f"THEMELAB_OUTCOME_REPORT_ERROR:{exc}")

        source_trade_date = str(conservative.get("trade_date") or themelab.get("trade_date") or trade_date or "")
        age_days = _report_age_days(source_trade_date)
        stale = age_days is not None and age_days > cfg.max_report_age_days
        reason_rows, group_rows = _eligible_rows(conservative, cfg)
        scenario_scores = _scenario_scores(themelab, cfg)
        eligible_reason_codes = sorted({str(row.get("reason_code") or "") for row in reason_rows if row.get("eligible")})
        eligible_groups = sorted({str(row.get("group") or "") for row in group_rows if row.get("eligible")})
        blocked_reasons = sorted(
            set(cfg.blocked_reason_codes)
            | {str(row.get("reason_code") or "") for row in reason_rows if row.get("blocked")}
        )
        sample_quality = _sample_quality(reason_rows + group_rows, cfg)
        available = bool(conservative.get("available")) and not stale and bool(reason_rows or group_rows or scenario_scores)
        status = "READY" if available else ("STALE_REPORT" if stale else "NO_DATA")
        if not bool(conservative.get("available")):
            warnings.append("NO_CONSERVATIVE_REASON_REPORT")
        if stale:
            warnings.append("SHADOW_SMALL_ENTRY_REPORT_STALE")
        best_scenario = scenario_scores[0] if scenario_scores else {}
        message = "소액 승격 근거를 불러왔습니다." if available else "소액 승격에 사용할 최신 outcome 근거가 없습니다."
        return ShadowSmallEntryPromotionEvidence(
            available=available,
            status=status,
            source_reports=list(cfg.source_reports),
            report_id=str(conservative.get("report_id") or themelab.get("report_id") or ""),
            source_report_trade_date=source_trade_date,
            report_age_days=age_days,
            sample_quality=sample_quality,
            eligible_reason_codes=eligible_reason_codes,
            eligible_reason_groups=eligible_groups,
            blocked_reason_codes=blocked_reasons,
            reason_code_rows=reason_rows,
            group_rows=group_rows,
            scenario_scores=scenario_scores,
            best_scenario=best_scenario,
            operator_message_ko=message,
            warnings=warnings,
        ).to_dict()

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 50000,
        include_traces: bool = True,
    ) -> dict[str, Any]:
        evidence = self.load_evidence(trade_date=trade_date, limit=limit)
        candidates = self._candidate_evaluations(trade_date=trade_date, evidence=evidence, limit=limit)
        traces = self.traces(trade_date=trade_date, limit=500, include_existing=include_traces, generated_candidates=candidates)
        summary = _summary(candidates, evidence, self.config, traces)
        generated_at = datetime.now().isoformat(timespec="seconds")
        return {
            "available": bool(evidence.get("available")),
            "status": "READY" if bool(evidence.get("available")) else str(evidence.get("status") or "NO_DATA"),
            "trade_date": trade_date or evidence.get("source_report_trade_date") or "",
            "generated_at": generated_at,
            "last_updated_at": _last_updated_at(traces) or generated_at,
            "config": self.config.to_dict(),
            "evidence": evidence,
            "summary": summary,
            "candidates": candidates[: max(1, int(limit or 50000))],
            "traces": traces[:500],
            "warnings": list(evidence.get("warnings") or []),
            "disclaimer_ko": "기본값은 observe_only/order_enabled=false입니다. 이 리포트는 LIVE_REAL 또는 guard threshold를 변경하지 않습니다.",
        }

    def candidates(self, *, trade_date: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        report = self.build_report(trade_date=trade_date, limit=max(limit, 500), include_traces=False)
        return list(report.get("candidates") or [])[: max(1, int(limit or 200))]

    def traces(
        self,
        *,
        trade_date: Optional[str] = None,
        code: str = "",
        candidate_instance_id: str = "",
        limit: int = 500,
        include_existing: bool = True,
        generated_candidates: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if include_existing and hasattr(self.db, "list_buy_zero_trace_events"):
            try:
                rows.extend(
                    self.db.list_buy_zero_trace_events(
                        trade_date=trade_date,
                        code=code or None,
                        candidate_instance_id=candidate_instance_id or None,
                        limit=max(1, int(limit or 500)),
                    )
                )
            except Exception:
                rows.extend([])
        rows = [row for row in rows if str(row.get("stage") or "") in TRACE_STAGES]
        if generated_candidates is not None and not code and not candidate_instance_id:
            rows.extend(_trace_rows_from_candidates(generated_candidates, trade_date=trade_date or ""))
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)), reverse=True)
        return rows[: max(1, int(limit or 500))]

    def export_json(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in report.get("candidates") or []:
                writer.writerow({column: _csv_value(row.get(column)) for column in CSV_COLUMNS})
        return path

    def export_markdown(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        summary = dict(report.get("summary") or {})
        lines = [
            f"# Shadow Small Entry Promotion ({report.get('trade_date') or 'all'})",
            "",
            "Read-only/guarded promotion report. Defaults remain observe_only and order_enabled=false.",
            "",
            "## Summary",
            f"- available: {report.get('available')}",
            f"- mode: {summary.get('mode')}",
            f"- order_enabled: {summary.get('order_enabled')}",
            f"- candidate_count: {summary.get('candidate_count', 0)}",
            f"- observe_only_count: {summary.get('observe_only_count', 0)}",
            f"- promoted_count: {summary.get('promoted_count', 0)}",
            f"- blocked_count: {summary.get('blocked_count', 0)}",
            "",
            "## Top Reason Groups",
        ]
        for row in summary.get("top_reason_groups") or []:
            lines.append(f"- {row.get('key')}: {row.get('count')}")
        lines.extend(["", "## Candidate Examples"])
        for row in (report.get("candidates") or [])[:10]:
            lines.append(
                f"- {row.get('code')} {row.get('name')}: {row.get('promotion_status')} "
                f"{row.get('rejected_reason') or ''} x{row.get('position_size_multiplier')}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict[str, Any], *, report_dir: Optional[Path] = None, stem: Optional[str] = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root / str(report.get("trade_date") or "all")
        stem = stem or f"shadow_small_entry_promotion_{report.get('trade_date') or 'all'}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def _candidate_evaluations(self, *, trade_date: Optional[str], evidence: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        items = self._source_candidate_items(trade_date=trade_date, limit=limit)
        rows: list[dict[str, Any]] = []
        for item in items:
            evaluation = evaluate_shadow_small_entry_promotion(
                trace=_candidate_trace(item),
                evidence=evidence,
                config=self.config,
            ).to_dict()
            evidence_row = dict(evaluation.get("evidence") or {})
            candidate = dict(evaluation.get("candidate") or {})
            row = {
                **candidate,
                "trade_date": trade_date or evidence.get("source_report_trade_date") or item.get("trade_date") or "",
                "promotion_status": evaluation.get("promotion_status") or "",
                "promoted": bool(evaluation.get("promoted")),
                "ready_type": evaluation.get("ready_type") or "",
                "final_status": evaluation.get("final_status") or "",
                "strategy_eligible": bool(evaluation.get("strategy_eligible")),
                "order_eligibility": evaluation.get("order_eligibility") or "",
                "final_grade": evaluation.get("final_grade") or "",
                "position_size_multiplier": evaluation.get("position_size_multiplier"),
                "rejected_reason": evaluation.get("rejected_reason") or "",
                "reason_codes": list(evaluation.get("reason_codes") or []),
                "operator_message_ko": evaluation.get("operator_message_ko") or "",
                "reason_group": evidence_row.get("reason_group") or item.get("primary_group") or "",
                "reason_code": evidence_row.get("reason_code") or item.get("primary_reason") or "",
                "sample_count": evidence_row.get("sample_count"),
                "missed_opportunity_rate": evidence_row.get("missed_opportunity_rate"),
                "risk_avoided_rate": evidence_row.get("risk_avoided_rate"),
                "good_block_rate": evidence_row.get("good_block_rate"),
                "avg_mfe_15m_pct": evidence_row.get("avg_mfe_15m_pct") or item.get("mfe_15m_pct"),
                "avg_mae_15m_pct": evidence_row.get("avg_mae_15m_pct") or item.get("mae_15m_pct"),
                "evidence": evidence_row,
                "evaluation": evaluation,
            }
            rows.append(row)
        rows.sort(key=lambda row: _status_sort(row.get("promotion_status")), reverse=True)
        return rows

    def _source_candidate_items(self, *, trade_date: Optional[str], limit: int) -> list[dict[str, Any]]:
        try:
            report = ConservativeReasonOutcomeAnalyzer(self.db).build_report(trade_date=trade_date, limit=limit)
        except Exception:
            return []
        review = report.get("review_for_small_entry") or {}
        candidates = list(review.get("candidates") or [])
        if candidates:
            return candidates[: max(1, int(limit or 50000))]
        return [
            item
            for item in report.get("items") or []
            if str(item.get("recommendation") or "").upper() == "REVIEW_FOR_SMALL_ENTRY"
        ][: max(1, int(limit or 50000))]

    def _load_settings(self) -> Any:
        try:
            return StrategyRuntimeSettingsRepository(self.db).load()
        except Exception:
            return legacy_strategy_runtime_settings()


def snapshot_payload(report: dict[str, Any]) -> dict[str, Any]:
    summary = dict(report.get("summary") or {})
    evidence = dict(report.get("evidence") or {})
    return {
        "available": bool(report.get("available")),
        "enabled": bool(summary.get("enabled")),
        "mode": summary.get("mode") or MODE_OBSERVE_ONLY,
        "order_enabled": bool(summary.get("order_enabled")),
        "status": report.get("status") or "NO_DATA",
        "source_report_trade_date": evidence.get("source_report_trade_date") or report.get("trade_date") or "",
        "candidate_count": int(summary.get("candidate_count") or 0),
        "observe_only_count": int(summary.get("observe_only_count") or 0),
        "promoted_count": int(summary.get("promoted_count") or 0),
        "blocked_count": int(summary.get("blocked_count") or 0),
        "submitted_count": int(summary.get("submitted_count") or 0),
        "max_promotions_per_cycle": int(summary.get("max_promotions_per_cycle") or 0),
        "max_promotions_per_day": int(summary.get("max_promotions_per_day") or 0),
        "used_promotions_today": int(summary.get("used_promotions_today") or 0),
        "top_reason_groups": list(summary.get("top_reason_groups") or [])[:5],
        "top_reason_codes": list(summary.get("top_reason_codes") or [])[:5],
        "last_updated_at": report.get("last_updated_at") or report.get("generated_at") or "",
        "warnings": list(report.get("warnings") or []),
        "summary": summary,
        "evidence": evidence,
        "candidates": list(report.get("candidates") or [])[:20],
        "disclaimer_ko": report.get("disclaimer_ko") or "",
    }


def empty_payload(error: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "enabled": True,
        "mode": MODE_OBSERVE_ONLY,
        "order_enabled": False,
        "status": "ERROR" if error else "NO_DATA",
        "source_report_trade_date": "",
        "candidate_count": 0,
        "observe_only_count": 0,
        "promoted_count": 0,
        "blocked_count": 0,
        "submitted_count": 0,
        "max_promotions_per_cycle": 1,
        "max_promotions_per_day": 3,
        "used_promotions_today": 0,
        "top_reason_groups": [],
        "top_reason_codes": [],
        "last_updated_at": "",
        "warnings": [error] if error else [],
        "summary": {},
        "evidence": {},
        "candidates": [],
        "disclaimer_ko": "기본값은 observe_only/order_enabled=false입니다.",
        **({"error": error} if error else {}),
    }


def record_promotion_trace_events(
    db,
    evaluation: Mapping[str, Any],
    *,
    trade_date: str,
    runtime_cycle_id: str = "",
    decision_cycle_id: str = "",
    decision_id: str = "",
    created_at: str = "",
) -> int:
    status = str(evaluation.get("promotion_status") or "")
    stages = ["SHADOW_SMALL_ENTRY_EVIDENCE_LOADED", "SHADOW_SMALL_ENTRY_CANDIDATE_EVALUATED"]
    if status == STATUS_PROMOTED:
        stages.append("SHADOW_SMALL_ENTRY_PROMOTED")
    elif status == STATUS_OBSERVE_ONLY:
        stages.append("SHADOW_SMALL_ENTRY_OBSERVE_ONLY")
    elif status == STATUS_BLOCKED:
        stages.append("SHADOW_SMALL_ENTRY_BLOCKED")
    rows = [
        trace_payload_from_evaluation(
            evaluation,
            stage=stage,
            trade_date=trade_date,
            runtime_cycle_id=runtime_cycle_id,
            decision_cycle_id=decision_cycle_id,
            decision_id=decision_id,
            created_at=created_at,
        )
        for stage in stages
    ]
    return db.save_buy_zero_trace_events(rows) if hasattr(db, "save_buy_zero_trace_events") else 0


def _eligible_rows(report: dict[str, Any], cfg: ShadowSmallEntryPromotionConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reason_rows = [_policy_row(row, cfg, key="reason_code") for row in report.get("by_reason_code") or []]
    group_rows = [_policy_row(row, cfg, key="group") for row in report.get("by_group") or []]
    return reason_rows, group_rows


def _policy_row(row: Mapping[str, Any], cfg: ShadowSmallEntryPromotionConfig, *, key: str) -> dict[str, Any]:
    payload = dict(row or {})
    name = str(payload.get(key) or "").upper()
    recommendation = str(payload.get("recommendation") or "").upper()
    sample = int(payload.get("labeled_count") or payload.get("sample_count") or payload.get("event_count") or 0)
    missed = float(payload.get("missed_opportunity_rate") or 0.0)
    risk = float(payload.get("risk_avoided_rate") or 0.0)
    good = float(payload.get("good_block_rate") or 0.0)
    mfe = payload.get("avg_mfe_15m_pct")
    mae = payload.get("avg_mae_15m_pct")
    confidence = min(0.95, sample / max(1.0, float(cfg.strong_sample_count or 30)))
    high_risk = name in set(cfg.blocked_reason_codes) or risk > cfg.max_risk_avoided_rate or good > cfg.max_good_block_rate
    eligible = (
        recommendation in set(cfg.allowed_recommendations)
        and sample >= max(1, min(cfg.min_sample_count, int(payload.get("event_count") or sample or 0)))
        and missed >= cfg.min_missed_opportunity_rate
        and risk <= cfg.max_risk_avoided_rate
        and good <= cfg.max_good_block_rate
        and (mfe is None or float(mfe or 0.0) >= cfg.min_avg_mfe_15m_pct)
        and (mae is None or float(mae or 0.0) >= cfg.max_avg_mae_15m_pct)
        and not high_risk
    )
    if key == "group" and name not in set(cfg.allowed_reason_groups):
        eligible = False
    if key == "reason_code" and cfg.allowed_reason_codes and name not in set(cfg.allowed_reason_codes) and recommendation not in set(cfg.allowed_recommendations):
        eligible = False
    return {
        **payload,
        key: name,
        "sample_count": sample,
        "confidence": round(confidence, 4),
        "sample_quality": _quality(sample, cfg),
        "eligible": bool(eligible),
        "blocked": bool(high_risk),
        "policy_reason": "ELIGIBLE" if eligible else ("HIGH_RISK_OR_GOOD_BLOCK" if high_risk else "THRESHOLD_NOT_MET"),
    }


def _scenario_scores(report: dict[str, Any], cfg: ShadowSmallEntryPromotionConfig) -> list[dict[str, Any]]:
    ab = dict(report.get("shadow_small_entry_ab") or {})
    rows = [dict(row) for row in list(ab.get("best_scenarios") or []) + list(ab.get("scenarios") or []) if isinstance(row, Mapping)]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        scenario_id = str(row.get("scenario_id") or "")
        if scenario_id in seen:
            continue
        seen.add(scenario_id)
        labeled = int(row.get("labeled_count") or row.get("candidate_count") or 0)
        row["sample_count"] = labeled
        row["confidence"] = min(0.95, labeled / max(1.0, float(cfg.strong_sample_count or 30)))
        row["sample_quality"] = _quality(labeled, cfg)
        row["eligible"] = str(row.get("recommendation") or "").upper() in set(cfg.allowed_recommendations) and row["confidence"] >= cfg.min_confidence
        result.append(row)
    result.sort(key=lambda item: (bool(item.get("eligible")), float(item.get("net_shadow_score") or 0.0), int(item.get("sample_count") or 0)), reverse=True)
    return result


def _candidate_trace(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": item.get("trade_date") or "",
        "code": item.get("code") or "",
        "name": item.get("name") or "",
        "candidate_instance_id": item.get("candidate_instance_id") or "",
        "status": item.get("status") or "WAIT",
        "reason_codes": list(item.get("reason_codes") or [item.get("primary_reason") or ""]),
        "primary_reason": item.get("primary_reason") or "",
        "primary_group": item.get("primary_group") or "",
        "stock_role": item.get("stock_role") or "",
        "price_location_status": item.get("price_location_status") or "",
        "price_location_readiness": item.get("price_location_readiness") or "PROVISIONAL",
        "risk_level": item.get("risk_level") or "PASS",
        "current_price": item.get("current_price") or item.get("base_price") or 0,
        "support_ready": item.get("support_ready", True),
        "vwap_ready": item.get("vwap_ready", item.get("price_location_status") == "VWAP_RECLAIM"),
        "latest_tick_ready": item.get("latest_tick_ready", True),
        "trade_value": item.get("trade_value") or 0,
    }


def _summary(candidates: list[dict[str, Any]], evidence: dict[str, Any], cfg: ShadowSmallEntryPromotionConfig, traces: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("promotion_status") or "") for row in candidates)
    groups = Counter(str(row.get("reason_group") or "UNKNOWN") for row in candidates if row.get("reason_group"))
    reasons = Counter(str(row.get("reason_code") or "UNKNOWN") for row in candidates if row.get("reason_code"))
    submitted = sum(1 for row in traces if str(row.get("stage") or "") == "SHADOW_SMALL_ENTRY_ORDER_SUBMITTED")
    return {
        "enabled": bool(cfg.enabled),
        "mode": cfg.mode,
        "order_enabled": bool(cfg.order_enabled),
        "source_report_trade_date": evidence.get("source_report_trade_date") or "",
        "candidate_count": len(candidates),
        "observe_only_count": int(statuses.get(STATUS_OBSERVE_ONLY, 0)),
        "promoted_count": int(statuses.get(STATUS_PROMOTED, 0)),
        "blocked_count": int(statuses.get(STATUS_BLOCKED, 0)),
        "no_evidence_count": int(statuses.get(STATUS_NO_EVIDENCE, 0)),
        "submitted_count": submitted,
        "max_promotions_per_cycle": cfg.max_promotions_per_cycle,
        "max_promotions_per_day": cfg.max_promotions_per_day,
        "used_promotions_today": int(statuses.get(STATUS_PROMOTED, 0)),
        "top_reason_groups": _counter_rows(groups),
        "top_reason_codes": _counter_rows(reasons),
        "sample_quality": evidence.get("sample_quality") or "NO_DATA",
        "operator_messages_ko": _top_messages(candidates, evidence),
    }


def _trace_rows_from_candidates(candidates: list[dict[str, Any]], *, trade_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in candidates[:200]:
        evaluation = row.get("evaluation") or {}
        status = str(evaluation.get("promotion_status") or "")
        stage = "SHADOW_SMALL_ENTRY_PROMOTED" if status == STATUS_PROMOTED else ("SHADOW_SMALL_ENTRY_OBSERVE_ONLY" if status == STATUS_OBSERVE_ONLY else "SHADOW_SMALL_ENTRY_BLOCKED")
        rows.append(trace_payload_from_evaluation(evaluation, stage=stage, trade_date=trade_date or row.get("trade_date") or ""))
    return rows


def _counter_rows(counter: Counter[str], limit: int = 10) -> list[dict[str, Any]]:
    return [{"key": key, "count": int(count)} for key, count in counter.most_common(limit)]


def _top_messages(candidates: list[dict[str, Any]], evidence: dict[str, Any]) -> list[str]:
    messages = [str(evidence.get("operator_message_ko") or "")]
    messages.extend(str(row.get("operator_message_ko") or "") for row in candidates[:5])
    return [message for message in dict.fromkeys(messages) if message]


def _sample_quality(rows: list[dict[str, Any]], cfg: ShadowSmallEntryPromotionConfig) -> str:
    if not rows:
        return "NO_DATA"
    best = max(int(row.get("sample_count") or 0) for row in rows)
    return _quality(best, cfg)


def _quality(sample: int, cfg: ShadowSmallEntryPromotionConfig) -> str:
    if sample <= 0:
        return "NO_DATA"
    if sample < cfg.min_sample_count:
        return "LOW"
    if sample < cfg.strong_sample_count:
        return "MEDIUM"
    return "HIGH"


def _report_age_days(trade_date: str) -> Optional[int]:
    if not trade_date:
        return None
    try:
        day = datetime.fromisoformat(str(trade_date)[:10]).date()
    except ValueError:
        return None
    return max(0, (datetime.now().date() - day).days)


def _last_updated_at(rows: Iterable[Mapping[str, Any]]) -> str:
    return max((str(row.get("created_at") or "") for row in rows), default="")


def _status_sort(status: Any) -> tuple[int, str]:
    order = {STATUS_PROMOTED: 4, STATUS_OBSERVE_ONLY: 3, STATUS_BLOCKED: 2, STATUS_NO_EVIDENCE: 1}
    text = str(status or "")
    return order.get(text, 0), text


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value if value is not None else ""


def _load_db(path: str):
    from storage.db import TradingDatabase

    return TradingDatabase(path)
