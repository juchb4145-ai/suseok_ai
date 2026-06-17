from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional

from trading.strategy.candidates import normalize_code
from trading_app.theme_lab_gate_reason_outcomes import (
    ThemeLabGateReasonOutcomeAnalyzer,
    ThemeLabGateReasonOutcomeConfig,
)


REPORT_ROOT = Path(__file__).resolve().parents[1] / "reports"

SETUP_TYPES = (
    "CORE_PULLBACK",
    "LEADER_PROBE",
    "RELATIVE_STRENGTH",
    "MOMENTUM_CONTINUATION",
    "ROTATION_FOLLOWER",
    "AVOID",
    "UNKNOWN",
)

CSV_COLUMNS = [
    "trade_date",
    "observed_at",
    "code",
    "name",
    "theme_name",
    "trade_setup_type",
    "trade_setup_action",
    "trade_setup_confidence_score",
    "final_gate_status",
    "gate_status",
    "price_location_status",
    "price_location_readiness",
    "stock_role",
    "risk_level",
    "reason_codes",
    "base_price",
    "mfe_5m",
    "mfe_15m",
    "mfe_25m",
    "mfe_60m",
    "mae_5m",
    "mae_15m",
    "mae_25m",
    "mae_60m",
    "return_5m",
    "return_15m",
    "return_25m",
    "return_60m",
    "good_candidate_15m",
    "risk_case_15m",
    "missed_opportunity_15m",
    "bad_candidate_15m",
    "good_block_15m",
    "good_block_miss_15m",
]


@dataclass(frozen=True)
class TradeSetupOutcomeConfig:
    windows_min: tuple[int, ...] = (5, 15, 25, 60)
    label_horizon_min: int = 15
    good_candidate_mfe_15m_pct: float = 1.8
    risk_case_mae_15m_pct: float = -1.2
    bad_candidate_return_15m_pct: float = -1.0
    avoid_good_block_miss_mfe_15m_pct: float = 3.0
    leader_probe_min_labeled_count: int = 20
    leader_probe_min_win_rate_15m: float = 0.55
    leader_probe_max_risk_case_rate_15m: float = 0.20
    leader_probe_min_avg_mfe_15m_pct: float = 1.8
    leader_probe_min_avg_mae_15m_pct: float = -1.2
    relative_strength_min_labeled_count: int = 20
    relative_strength_min_win_rate_15m: float = 0.55
    relative_strength_max_risk_case_rate_15m: float = 0.15
    relative_strength_min_avg_mae_15m_pct: float = -1.0
    momentum_min_labeled_count: int = 30
    momentum_max_risk_case_rate_15m: float = 0.25


class TradeSetupOutcomeAnalyzer:
    def __init__(
        self,
        db,
        *,
        config: Optional[TradeSetupOutcomeConfig] = None,
        report_root: Optional[Path] = None,
    ) -> None:
        self.db = db
        self.config = config or TradeSetupOutcomeConfig()
        self.report_root = Path(report_root) if report_root is not None else REPORT_ROOT

    def build_report(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 10000,
        source_report: Optional[dict[str, Any]] = None,
        source_items: Optional[Iterable[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if source_items is not None:
            raw_items = [dict(item or {}) for item in source_items]
            source_report_id = "inline"
            source_status = "READY"
        else:
            if source_report is None:
                source_report = self._build_source_report(trade_date=trade_date, limit=limit)
            raw_items = [dict(item or {}) for item in source_report.get("items") or []]
            source_report_id = str(source_report.get("report_id") or "")
            source_status = str(source_report.get("status") or "READY")
            if trade_date is None:
                trade_date = str(source_report.get("trade_date") or "") or None

        trade_date = str(trade_date or _latest_item_trade_date(raw_items) or "")
        rows = [self._outcome_row(item, trade_date=trade_date) for item in raw_items[: max(1, int(limit or 10000))]]
        rows = [row for row in rows if row.get("code")]
        summary_by_type = self._summary_by_type(rows)
        recommendations = {
            setup_type: dict(summary_by_type.get(setup_type, {}).get("recommendation_detail") or {})
            for setup_type in SETUP_TYPES
        }
        top_missed = _top_rows([row for row in rows if row.get("missed_opportunity_15m")], "mfe_15m", reverse=True)
        top_bad = _top_rows([row for row in rows if row.get("bad_candidate_15m")], "mae_15m", reverse=False)
        top_good_block_misses = _top_rows([row for row in rows if row.get("good_block_miss_15m")], "mfe_15m", reverse=True)
        report = {
            "available": bool(rows),
            "status": "READY" if rows else ("ERROR" if source_status.upper() == "ERROR" else "NO_DATA"),
            "report_id": f"trade_setup_outcomes:{trade_date or 'all'}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "config": asdict(self.config),
            "source_report_id": source_report_id,
            "summary": self._summary(rows),
            "summary_by_type": summary_by_type,
            "recommendations": recommendations,
            "leader_probe_recommendation": recommendations.get("LEADER_PROBE") or {},
            "relative_strength_recommendation": recommendations.get("RELATIVE_STRENGTH") or {},
            "top_missed_opportunities": top_missed,
            "top_bad_candidates": top_bad,
            "top_good_block_misses": top_good_block_misses,
            "rows": rows,
            "notes": [
                "read_only_trade_setup_outcome_report",
                "does_not_change_order_generation_or_order_enabled_defaults",
                "position_size_multiplier_is_not_applied_to_order_quantity",
            ],
        }
        return report

    def export_json(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return path

    def export_csv(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for row in report.get("rows") or []:
                writer.writerow({column: _csv_value(row.get(column)) for column in CSV_COLUMNS})
        return path

    def export_markdown(self, report: dict[str, Any], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        trade_date = str(report.get("trade_date") or "all")
        summary_by_type = dict(report.get("summary_by_type") or {})
        recommendations = dict(report.get("recommendations") or {})
        leader = summary_by_type.get("LEADER_PROBE") or {}
        relative = summary_by_type.get("RELATIVE_STRENGTH") or {}
        momentum = summary_by_type.get("MOMENTUM_CONTINUATION") or {}
        avoid = summary_by_type.get("AVOID") or {}
        lines = [
            f"# Trade Setup Outcomes ({trade_date})",
            "",
            "이 리포트는 전략 유형별 성과 검증 전용입니다. 주문 생성, READY threshold, order_enabled 기본값은 변경하지 않습니다.",
            "",
            "## 운영자 요약",
            (
                f"- 오늘 LEADER_PROBE 후보 {leader.get('candidate_count', 0)}개 중 "
                f"15분 기준 상승 후보 {leader.get('good_candidate_count_15m', 0)}개, "
                f"추천은 {(recommendations.get('LEADER_PROBE') or {}).get('recommendation', 'NO_CANDIDATES')}입니다."
            ),
            (
                f"- RELATIVE_STRENGTH 후보는 {relative.get('labeled_count', 0)}개가 라벨링됐고 "
                f"추천은 {(recommendations.get('RELATIVE_STRENGTH') or {}).get('recommendation', 'NO_CANDIDATES')}입니다."
            ),
            (
                f"- MOMENTUM_CONTINUATION은 평균 MFE {momentum.get('avg_mfe_15m')}%, "
                f"평균 MAE {momentum.get('avg_mae_15m')}%로 승격 여부를 관찰합니다."
            ),
            (
                f"- AVOID 후보 중 15분 급등 재검토 케이스는 "
                f"{avoid.get('good_block_miss_count', 0)}개입니다."
            ),
            "",
            "## Setup Type Summary",
            "| setup_type | candidates | labeled | win_rate_15m | risk_case_rate_15m | avg_mfe_15m | avg_mae_15m | recommendation |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for setup_type in SETUP_TYPES:
            row = summary_by_type.get(setup_type) or {}
            rec = (recommendations.get(setup_type) or {}).get("recommendation") or row.get("recommendation") or "NO_CANDIDATES"
            lines.append(
                "| {setup} | {candidate} | {labeled} | {win} | {risk} | {mfe} | {mae} | {rec} |".format(
                    setup=setup_type,
                    candidate=row.get("candidate_count", 0),
                    labeled=row.get("labeled_count", 0),
                    win=row.get("win_rate_15m", 0.0),
                    risk=row.get("risk_case_rate_15m", 0.0),
                    mfe=row.get("avg_mfe_15m"),
                    mae=row.get("avg_mae_15m"),
                    rec=rec,
                )
            )
        lines.extend(["", "## Top Missed Opportunities"])
        for row in report.get("top_missed_opportunities") or []:
            lines.append(
                f"- {row.get('code')} {row.get('name')}: {row.get('trade_setup_type')} "
                f"MFE15={row.get('mfe_15m')}%, return15={row.get('return_15m')}%, action={row.get('trade_setup_action')}"
            )
        if not report.get("top_missed_opportunities"):
            lines.append("- 없음")
        lines.extend(["", "## Top Bad Candidates"])
        for row in report.get("top_bad_candidates") or []:
            lines.append(
                f"- {row.get('code')} {row.get('name')}: {row.get('trade_setup_type')} "
                f"MAE15={row.get('mae_15m')}%, return15={row.get('return_15m')}%, reasons={','.join(row.get('reason_codes') or [])}"
            )
        if not report.get("top_bad_candidates"):
            lines.append("- 없음")
        lines.extend(
            [
                "",
                "## Notes",
                "- LEADER_PROBE/RELATIVE_STRENGTH 승격은 표본, 승률, MAE 위험률 기준을 모두 만족할 때만 검토 대상입니다.",
                "- AVOID 급등 케이스는 차단 완화가 아니라 별도 리뷰 대상입니다.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def export_all(self, report: dict[str, Any], *, report_dir: Path | None = None, stem: str | None = None) -> dict[str, str]:
        target = Path(report_dir) if report_dir is not None else self.report_root
        trade_date = str(report.get("trade_date") or "all").replace("-", "")
        stem = stem or f"trade_setup_outcomes_{trade_date}"
        return {
            "json": str(self.export_json(report, target / f"{stem}.json")),
            "csv": str(self.export_csv(report, target / f"{stem}.csv")),
            "md": str(self.export_markdown(report, target / f"{stem}.md")),
        }

    def _build_source_report(self, *, trade_date: Optional[str], limit: int) -> dict[str, Any]:
        if self.db is None:
            return {"status": "NO_DATA", "trade_date": trade_date or "", "items": []}
        source_config = ThemeLabGateReasonOutcomeConfig(
            windows_min=tuple(sorted({int(value) for value in self.config.windows_min if int(value) > 0})),
            label_horizon_min=int(self.config.label_horizon_min),
        )
        return ThemeLabGateReasonOutcomeAnalyzer(self.db, config=source_config).build_report(
            trade_date=trade_date,
            limit=limit,
        )

    def _outcome_row(self, item: dict[str, Any], *, trade_date: str) -> dict[str, Any]:
        setup_type = _setup_type(item.get("trade_setup_type"))
        reason_codes = _reason_codes(item)
        action = str(item.get("trade_setup_action") or item.get("recommended_action") or "").strip().upper()
        final_status = _status(item, "final_gate_status", "status", "gate_status")
        gate_status = _status(item, "gate_status", "status", "final_gate_status")
        row: dict[str, Any] = {
            "trade_date": str(item.get("trade_date") or trade_date or ""),
            "observed_at": str(item.get("observed_at") or item.get("created_at") or ""),
            "code": normalize_code(item.get("code") or item.get("symbol") or item.get("stock_code") or ""),
            "name": str(item.get("name") or item.get("stock_name") or ""),
            "theme_name": str(item.get("theme_name") or item.get("primary_theme") or item.get("theme_id") or ""),
            "trade_setup_type": setup_type,
            "trade_setup_action": action,
            "trade_setup_confidence_score": _number(item.get("trade_setup_confidence_score")),
            "final_gate_status": final_status,
            "gate_status": gate_status,
            "price_location_status": str(item.get("price_location_status") or ""),
            "price_location_readiness": str(item.get("price_location_readiness") or ""),
            "stock_role": str(item.get("stock_role") or item.get("leader_type") or ""),
            "risk_level": str(item.get("risk_level") or item.get("entry_risk_level") or ""),
            "reason_codes": reason_codes,
            "base_price": _number(item.get("base_price") or item.get("current_price") or item.get("price")),
        }
        for window in self.config.windows_min:
            window = int(window)
            row[f"mfe_{window}m"] = _metric(item, "mfe", window)
            row[f"mae_{window}m"] = _metric(item, "mae", window)
            row[f"return_{window}m"] = _metric(item, "return", window)
        row["labeled"] = _labeled(row, item)
        mfe_15m = _number(row.get("mfe_15m"))
        mae_15m = _number(row.get("mae_15m"))
        return_15m = _number(row.get("return_15m"))
        good_candidate = bool(row["labeled"] and mfe_15m is not None and mfe_15m >= self.config.good_candidate_mfe_15m_pct)
        risk_case = bool(row["labeled"] and mae_15m is not None and mae_15m <= self.config.risk_case_mae_15m_pct)
        bad_candidate = bool(
            row["labeled"]
            and not good_candidate
            and (
                risk_case
                or (return_15m is not None and return_15m <= self.config.bad_candidate_return_15m_pct)
            )
        )
        buy_ready = action == "NORMAL_READY" or final_status in {"READY", "READY_SMALL"}
        missed = bool(setup_type not in {"CORE_PULLBACK", "AVOID"} and good_candidate and not buy_ready)
        good_block = bool(setup_type == "AVOID" and row["labeled"] and mfe_15m is not None and mfe_15m < self.config.good_candidate_mfe_15m_pct)
        good_block_miss = bool(
            setup_type == "AVOID"
            and row["labeled"]
            and mfe_15m is not None
            and mfe_15m >= self.config.avoid_good_block_miss_mfe_15m_pct
        )
        row.update(
            {
                "good_candidate_15m": good_candidate,
                "risk_case_15m": risk_case,
                "missed_opportunity_15m": missed,
                "bad_candidate_15m": bad_candidate,
                "good_block_15m": good_block,
                "good_block_miss_15m": good_block_miss,
            }
        )
        return row

    def _summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [row for row in rows if row.get("labeled")]
        return {
            "candidate_count": len(rows),
            "labeled_count": len(labeled),
            "unlabeled_count": len(rows) - len(labeled),
            "good_candidate_count_15m": _count(rows, "good_candidate_15m"),
            "risk_case_count_15m": _count(rows, "risk_case_15m"),
            "missed_opportunity_count": _count(rows, "missed_opportunity_15m"),
            "bad_candidate_count": _count(rows, "bad_candidate_15m"),
            "good_block_miss_count": _count(rows, "good_block_miss_15m"),
            "setup_type_counts": {setup: sum(1 for row in rows if row.get("trade_setup_type") == setup) for setup in SETUP_TYPES},
        }

    def _summary_by_type(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("trade_setup_type") or "UNKNOWN")].append(row)
        return {setup_type: self._summary_row(setup_type, grouped.get(setup_type, [])) for setup_type in SETUP_TYPES}

    def _summary_row(self, setup_type: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        labeled = [row for row in rows if row.get("labeled")]
        wins = [row for row in labeled if _number(row.get("return_15m")) is not None and float(row.get("return_15m") or 0.0) > 0.0]
        risk_cases = [row for row in labeled if row.get("risk_case_15m")]
        missed = [row for row in rows if row.get("missed_opportunity_15m")]
        bad = [row for row in rows if row.get("bad_candidate_15m")]
        good_block_misses = [row for row in rows if row.get("good_block_miss_15m")]
        row = {
            "trade_setup_type": setup_type,
            "candidate_count": len(rows),
            "labeled_count": len(labeled),
            "avg_mfe_15m": _avg(row.get("mfe_15m") for row in labeled),
            "avg_mae_15m": _avg(row.get("mae_15m") for row in labeled),
            "avg_return_15m": _avg(row.get("return_15m") for row in labeled),
            "win_count_15m": len(wins),
            "win_rate_15m": _ratio(len(wins), len(labeled)),
            "risk_case_count_15m": len(risk_cases),
            "risk_case_rate_15m": _ratio(len(risk_cases), len(labeled)),
            "good_candidate_count_15m": _count(rows, "good_candidate_15m"),
            "missed_opportunity_count": len(missed),
            "missed_opportunity_rate": _ratio(len(missed), len(labeled)),
            "bad_candidate_count": len(bad),
            "bad_candidate_rate": _ratio(len(bad), len(labeled)),
            "good_block_count": _count(rows, "good_block_15m"),
            "good_block_miss_count": len(good_block_misses),
            "top_positive_candidates": _top_rows(rows, "mfe_15m", reverse=True),
            "top_negative_candidates": _top_rows(rows, "mae_15m", reverse=False),
            "top_block_reasons": _top_reasons(rows),
        }
        recommendation = self._recommendation(setup_type, row, rows)
        row["recommendation"] = recommendation["recommendation"]
        row["recommendation_detail"] = recommendation
        return row

    def _recommendation(self, setup_type: str, summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        candidate_count = int(summary.get("candidate_count") or 0)
        labeled_count = int(summary.get("labeled_count") or 0)
        win_rate = float(summary.get("win_rate_15m") or 0.0)
        risk_rate = float(summary.get("risk_case_rate_15m") or 0.0)
        avg_mfe = _number(summary.get("avg_mfe_15m"))
        avg_mae = _number(summary.get("avg_mae_15m"))
        if candidate_count <= 0:
            return _recommendation(
                "NO_CANDIDATES",
                ["NO_CANDIDATES"],
                f"{setup_type} 후보가 아직 없습니다. 관측 데이터가 쌓이면 자동으로 평가됩니다.",
                summary,
            )
        if setup_type == "LEADER_PROBE":
            if labeled_count < self.config.leader_probe_min_labeled_count:
                return _insufficient_sample(setup_type, labeled_count, self.config.leader_probe_min_labeled_count, summary)
            if risk_rate > self.config.leader_probe_max_risk_case_rate_15m or (
                avg_mae is not None and avg_mae < self.config.leader_probe_min_avg_mae_15m_pct
            ):
                return _recommendation(
                    "DO_NOT_PROMOTE",
                    ["RISK_TOO_HIGH"],
                    "LEADER_PROBE의 15분 손실/위험 비율이 높아 LIVE_SIM 소액 승격은 보류하세요.",
                    summary,
                )
            passed = (
                win_rate >= self.config.leader_probe_min_win_rate_15m
                and risk_rate <= self.config.leader_probe_max_risk_case_rate_15m
                and avg_mfe is not None
                and avg_mfe >= self.config.leader_probe_min_avg_mfe_15m_pct
                and avg_mae is not None
                and avg_mae >= self.config.leader_probe_min_avg_mae_15m_pct
            )
            if passed:
                return _recommendation(
                    "REVIEW_FOR_LIVE_SIM_SMALL",
                    ["LEADER_PROBE_CRITERIA_PASS"],
                    "LEADER_PROBE가 표본/승률/위험 기준을 충족했습니다. 다음 PR에서 LIVE_SIM 소액 승격을 리뷰할 수 있습니다.",
                    summary,
                )
            return _recommendation(
                "WATCH_MORE",
                ["CRITERIA_PARTIAL_PASS"],
                "LEADER_PROBE는 일부 기준만 충족했습니다. order_enabled는 유지하고 표본을 더 모으세요.",
                summary,
            )
        if setup_type == "RELATIVE_STRENGTH":
            if labeled_count < self.config.relative_strength_min_labeled_count:
                return _insufficient_sample(setup_type, labeled_count, self.config.relative_strength_min_labeled_count, summary)
            if risk_rate > self.config.relative_strength_max_risk_case_rate_15m or (
                avg_mae is not None and avg_mae < self.config.relative_strength_min_avg_mae_15m_pct
            ):
                return _recommendation(
                    "DO_NOT_PROMOTE",
                    ["RELATIVE_STRENGTH_RISK_TOO_HIGH"],
                    "RELATIVE_STRENGTH의 하방 위험이 기준보다 큽니다. 소액 승격을 보류하세요.",
                    summary,
                )
            if win_rate >= self.config.relative_strength_min_win_rate_15m:
                return _recommendation(
                    "REVIEW_FOR_LIVE_SIM_SMALL",
                    ["RELATIVE_STRENGTH_CRITERIA_PASS"],
                    "RELATIVE_STRENGTH가 별도 기준을 충족했습니다. LIVE_SIM 소액 후보로 리뷰할 수 있습니다.",
                    summary,
                )
            return _recommendation(
                "WATCH_MORE",
                ["WIN_RATE_BELOW_THRESHOLD"],
                "RELATIVE_STRENGTH 승률이 아직 기준 아래입니다. 관측을 이어가세요.",
                summary,
            )
        if setup_type == "MOMENTUM_CONTINUATION":
            if labeled_count < self.config.momentum_min_labeled_count:
                return _insufficient_sample(setup_type, labeled_count, self.config.momentum_min_labeled_count, summary)
            if risk_rate > self.config.momentum_max_risk_case_rate_15m:
                return _recommendation(
                    "DO_NOT_PROMOTE",
                    ["MOMENTUM_RISK_TOO_HIGH"],
                    "MOMENTUM_CONTINUATION은 MFE가 높아도 MAE 위험률이 높아 승격하지 않습니다.",
                    summary,
                )
            return _recommendation(
                "WATCH_MORE",
                ["MOMENTUM_DEFAULT_WATCH_MORE"],
                "MOMENTUM_CONTINUATION은 기본적으로 추가 표본 관찰 대상입니다.",
                summary,
            )
        if setup_type == "ROTATION_FOLLOWER":
            contamination = any(
                row.get("stock_role") == "LATE_LAGGARD"
                or "LATE_LAGGARD" in row.get("reason_codes", [])
                or "LEADER_ONLY_THEME" in row.get("reason_codes", [])
                or "LEADER_ONLY_THEME_LAGGARD_BLOCK" in row.get("reason_codes", [])
                for row in rows
            )
            if contamination:
                return _recommendation(
                    "DO_NOT_PROMOTE",
                    ["ROTATION_CONTAINS_LEADER_ONLY_OR_LATE_LAGGARD"],
                    "ROTATION_FOLLOWER 표본에 LEADER_ONLY_THEME 또는 LATE_LAGGARD 성격이 섞여 승격하지 않습니다.",
                    summary,
                )
            return _recommendation(
                "WATCH_MORE",
                ["ROTATION_DEFAULT_WATCH_MORE"],
                "ROTATION_FOLLOWER는 현재 관찰 전용입니다. 확산 테마 표본을 더 모으세요.",
                summary,
            )
        if setup_type == "CORE_PULLBACK":
            return _recommendation(
                "QUALITY_BASELINE_ONLY",
                ["CORE_PULLBACK_POLICY_UNCHANGED"],
                "CORE_PULLBACK은 기존 READY 품질 점검용입니다. 주문 정책은 변경하지 않습니다.",
                summary,
            )
        if setup_type == "AVOID":
            if int(summary.get("good_block_miss_count") or 0) > 0:
                return _recommendation(
                    "REVIEW_AVOID_MISSES",
                    ["AVOID_GOOD_BLOCK_MISS_FOUND"],
                    "AVOID 차단 후 급등한 케이스가 있습니다. 차단 완화가 아니라 사후 리뷰 대상으로만 보세요.",
                    summary,
                )
            return _recommendation(
                "KEEP_BLOCKING",
                ["AVOID_BLOCKS_HELD"],
                "AVOID 차단은 유지합니다. 급등 누락 케이스가 충분히 확인되지 않았습니다.",
                summary,
            )
        return _recommendation(
            "WATCH_MORE",
            ["UNKNOWN_SETUP_TYPE"],
            "UNKNOWN setup은 분류 품질을 먼저 확인하세요.",
            summary,
        )


def snapshot_payload(report: dict[str, Any]) -> dict[str, Any]:
    if str(report.get("status") or "").upper() == "ERROR":
        return empty_payload(str(report.get("error") or ""))
    recommendations = dict(report.get("recommendations") or {})
    return {
        "available": bool(report.get("available")),
        "status": report.get("status") or "NO_DATA",
        "trade_date": report.get("trade_date") or "",
        "generated_at": report.get("generated_at") or "",
        "summary": report.get("summary") or {},
        "summary_by_type": report.get("summary_by_type") or {},
        "recommendations": recommendations,
        "leader_probe_recommendation": recommendations.get("LEADER_PROBE") or report.get("leader_probe_recommendation") or {},
        "relative_strength_recommendation": recommendations.get("RELATIVE_STRENGTH") or report.get("relative_strength_recommendation") or {},
        "top_missed_opportunities": list(report.get("top_missed_opportunities") or [])[:10],
        "top_bad_candidates": list(report.get("top_bad_candidates") or [])[:10],
        "top_good_block_misses": list(report.get("top_good_block_misses") or [])[:10],
    }


def empty_payload(error: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "status": "ERROR" if error else "NO_DATA",
        "trade_date": "",
        "generated_at": "",
        "summary": {},
        "summary_by_type": {},
        "recommendations": {},
        "leader_probe_recommendation": {},
        "relative_strength_recommendation": {},
        "top_missed_opportunities": [],
        "top_bad_candidates": [],
        "top_good_block_misses": [],
        **({"error": error} if error else {}),
    }


def _recommendation(recommendation: str, reason_codes: list[str], operator_message_ko: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommendation": recommendation,
        "reason_codes": reason_codes,
        "operator_message_ko": operator_message_ko,
        "criteria_snapshot": {
            "candidate_count": int(summary.get("candidate_count") or 0),
            "labeled_count": int(summary.get("labeled_count") or 0),
            "win_rate_15m": summary.get("win_rate_15m", 0.0),
            "risk_case_rate_15m": summary.get("risk_case_rate_15m", 0.0),
            "avg_mfe_15m": summary.get("avg_mfe_15m"),
            "avg_mae_15m": summary.get("avg_mae_15m"),
        },
    }


def _insufficient_sample(setup_type: str, labeled_count: int, required_count: int, summary: dict[str, Any]) -> dict[str, Any]:
    return _recommendation(
        "INSUFFICIENT_SAMPLE",
        ["INSUFFICIENT_SAMPLE"],
        f"{setup_type} 표본이 {labeled_count}/{required_count}개로 부족합니다. LIVE_SIM 승격 판단은 보류하고 관측을 계속하세요.",
        summary,
    )


def _setup_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in SETUP_TYPES else "UNKNOWN"


def _status(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(item.get(key) or "").strip().upper()
        if text:
            return text
    return "UNKNOWN"


def _reason_codes(item: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("reason_codes", "secondary_reason_codes", "trade_setup_reason_codes", "primary_reason"):
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(","))
        elif isinstance(raw, Iterable):
            values.extend(raw)
        else:
            values.append(raw)
    result: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text and text not in result:
            result.append(text)
    return result


def _metric(item: dict[str, Any], name: str, window: int) -> float | None:
    for key in (f"{name}_{window}m", f"{name}_{window}m_pct"):
        number = _number(item.get(key))
        if number is not None:
            return number
    return None


def _labeled(row: dict[str, Any], source: dict[str, Any]) -> bool:
    if str(source.get("outcome_data_quality") or "").lower() == "ready":
        return True
    return any(row.get(key) is not None for key in ("mfe_15m", "mae_15m", "return_15m"))


def _top_rows(rows: list[dict[str, Any]], sort_key: str, *, reverse: bool, limit: int = 5) -> list[dict[str, Any]]:
    def _sort_value(row: dict[str, Any]) -> float:
        value = _number(row.get(sort_key))
        if value is None:
            return float("-inf") if reverse else float("inf")
        return value

    return [_candidate_view(row) for row in sorted(rows, key=_sort_value, reverse=reverse)[:limit]]


def _candidate_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": row.get("trade_date") or "",
        "observed_at": row.get("observed_at") or "",
        "code": row.get("code") or "",
        "name": row.get("name") or "",
        "theme_name": row.get("theme_name") or "",
        "trade_setup_type": row.get("trade_setup_type") or "UNKNOWN",
        "trade_setup_action": row.get("trade_setup_action") or "",
        "trade_setup_confidence_score": row.get("trade_setup_confidence_score"),
        "final_gate_status": row.get("final_gate_status") or "",
        "price_location_status": row.get("price_location_status") or "",
        "stock_role": row.get("stock_role") or "",
        "risk_level": row.get("risk_level") or "",
        "reason_codes": list(row.get("reason_codes") or []),
        "mfe_15m": row.get("mfe_15m"),
        "mae_15m": row.get("mae_15m"),
        "return_15m": row.get("return_15m"),
        "good_candidate_15m": bool(row.get("good_candidate_15m")),
        "risk_case_15m": bool(row.get("risk_case_15m")),
        "missed_opportunity_15m": bool(row.get("missed_opportunity_15m")),
        "bad_candidate_15m": bool(row.get("bad_candidate_15m")),
        "good_block_miss_15m": bool(row.get("good_block_miss_15m")),
    }


def _top_reasons(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        status = str(row.get("final_gate_status") or "").upper()
        should_count = status not in {"READY", "READY_SMALL"} or row.get("bad_candidate_15m") or row.get("trade_setup_type") == "AVOID"
        if not should_count:
            continue
        for reason in row.get("reason_codes") or ["UNKNOWN"]:
            counter[str(reason or "UNKNOWN").upper()] += 1
    return [{"reason_code": reason, "count": int(count)} for reason, count in counter.most_common(limit)]


def _latest_item_trade_date(items: list[dict[str, Any]]) -> str:
    dates = sorted({str(item.get("trade_date") or item.get("observed_at") or "")[:10] for item in items if str(item.get("trade_date") or item.get("observed_at") or "")[:10]})
    return dates[-1] if dates else ""


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).strip().replace(",", "")), 4)
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return round(mean(numbers), 4) if numbers else None


def _ratio(part: int, total: int) -> float:
    return round(float(part) / float(total), 4) if total else 0.0


def _count(rows: Iterable[dict[str, Any]], key: str) -> int:
    return sum(1 for row in rows if row.get(key))


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value
