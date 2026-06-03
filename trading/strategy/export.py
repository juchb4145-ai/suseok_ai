from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile

from trading.strategy.hybrid_gate import summarize_hybrid_gate_reviews
from trading.strategy.models import ReviewFinalStatus, TradeReview


REVIEW_EXPORT_COLUMNS = [
    "created_at",
    "trade_date",
    "code",
    "name",
    "market",
    "theme_id",
    "theme_name",
    "strategy_profile",
    "final_grade",
    "final_status",
    "virtual_order_status",
    "exit_reason",
    "entry_price",
    "exit_price",
    "max_return_5m",
    "max_return_10m",
    "max_return_20m",
    "max_drawdown_20m",
    "false_negative_flag",
    "false_positive_flag",
    "missed_reason",
    "review_key",
]

REVIEW_SUMMARY_COLUMNS = [
    "section",
    "key",
    "candidate_count",
    "ready_count",
    "blocked_count",
    "soft_block_count",
    "virtual_filled_count",
    "max_return_20m_avg",
    "max_drawdown_20m_avg",
    "false_positive_count",
    "false_negative_count",
    "opportunity_missed_count",
    "loss_avoided_count",
]
FALSE_NEGATIVE_RETURN_THRESHOLD_PCT = 3.0
FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT = -3.0
MARKET_DIAGNOSTIC_CODES = ["MARKET_BREADTH_WEAK", "INDEX_SLOPE_WEAK", "OPEN_GAP_RISK"]
THEME_LEADERSHIP_CODES = ["THEME_SYNC_WEAK", "LEADER_FOLLOWER_GAP", "LEADER_REPLACED"]
FILL_DIAGNOSTIC_CODES = ["FILL_LIQUIDITY_WEAK", "SPREAD_TOO_WIDE", "FILL_INPUT_INSUFFICIENT"]
SESSION_BUCKETS = ["OPEN_0_10", "OPEN_10_90", "MIDDAY", "LATE"]
READY_STATUSES = {
    ReviewFinalStatus.VIRTUAL_SUBMITTED.value,
    ReviewFinalStatus.VIRTUAL_FILLED.value,
    ReviewFinalStatus.VIRTUAL_UNFILLED.value,
    ReviewFinalStatus.VIRTUAL_CANCELLED.value,
    ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
}
BLOCKED_STATUSES = {
    ReviewFinalStatus.BLOCKED_TEMP.value,
    ReviewFinalStatus.BLOCKED_FINAL.value,
    ReviewFinalStatus.PLAN_NOT_CREATED.value,
    ReviewFinalStatus.DATA_INSUFFICIENT.value,
    ReviewFinalStatus.EXPIRED.value,
}
VIRTUAL_FILLED_STATUSES = {
    ReviewFinalStatus.VIRTUAL_FILLED.value,
    ReviewFinalStatus.VIRTUAL_PARTIAL_TAKE_PROFIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TAKE_PROFIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_SUPPORT_LOSS.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TIME_EXIT.value,
    ReviewFinalStatus.VIRTUAL_CLOSED_TRAILING_STOP.value,
}


class ReviewExporter:
    def build_summary(self, reviews: list[TradeReview]) -> dict:
        return build_review_summary(reviews)

    def export_csv(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _temp_path(target, encoding="utf-8-sig", newline="") as temp:
            writer = csv.DictWriter(temp.handle, fieldnames=REVIEW_EXPORT_COLUMNS)
            writer.writeheader()
            for review in reviews:
                writer.writerow({column: _cell(getattr(review, column, "")) for column in REVIEW_EXPORT_COLUMNS})
        return target

    def export_summary_csv(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(reviews)
        rows = _summary_csv_rows(summary)
        with _temp_path(target, encoding="utf-8-sig", newline="") as temp:
            writer = csv.DictWriter(temp.handle, fieldnames=REVIEW_SUMMARY_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: _cell(row.get(column)) for column in REVIEW_SUMMARY_COLUMNS})
        return target

    def export_json(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": self.build_summary(reviews),
            "review_count": len(reviews),
        }
        with _temp_path(target, encoding="utf-8", newline="\n") as temp:
            json.dump(payload, temp.handle, ensure_ascii=False, indent=2)
            temp.handle.write("\n")
        return target

    def export_markdown(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(reviews)
        false_negative = [review for review in reviews if review.false_negative_flag]
        false_positive = [review for review in reviews if review.false_positive_flag]
        lines = [
            "# Strategy Review",
            "",
            "## Summary",
            "",
            f"- Total reviews: {len(reviews)}",
            f"- False negative: {len(false_negative)}",
            f"- False positive: {len(false_positive)}",
            "",
            "## Definitions",
            "",
            f"- false_positive: ready 또는 filled 이후 max_return이 낮고 20분 낙폭이 {FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT}% 이하인 손실 후보입니다.",
            f"- false_negative: blocked, unfilled, soft_block 이후 max_return_20m이 {FALSE_NEGATIVE_RETURN_THRESHOLD_PCT}% 이상 발생한 놓친 기회 후보입니다.",
            "- 이 리포트는 추천 후보와 검토 근거만 만들며 전략 설정은 자동 변경하지 않습니다.",
            "",
            "## False Negative",
            "",
        ]
        lines.extend(_summary_rows(false_negative))
        lines.extend(["", "## False Positive", ""])
        lines.extend(_summary_rows(false_positive))
        lines.extend(["", "## Missed Reason Code Performance", ""])
        lines.extend(_reason_code_rows(false_negative, "false_negative"))
        lines.extend(["", "## Loss Reason Code Performance", ""])
        lines.extend(_reason_code_rows(false_positive, "false_positive"))
        lines.extend(["", "## Reason Code Performance", ""])
        lines.extend(_performance_table(summary["reason_code_performance"], key_title="Reason Code"))
        lines.extend(["", "## Legacy vs New Matrix", ""])
        lines.extend(_legacy_matrix_rows(summary["legacy_new_matrix"]))
        lines.extend(["", "## Interpretation", ""])
        lines.extend(_interpretation_rows(summary["interpretation"]))
        lines.extend(["", "## Session Bucket Performance", ""])
        lines.extend(_session_rows(summary["session_bucket_performance"]))
        lines.extend(["", "## Market Diagnostics", ""])
        lines.extend(_performance_table(summary["market_diagnostics"]["reason_codes"], key_title="Reason Code"))
        lines.extend(["", "### Breadth Scope", ""])
        lines.extend(_performance_table(summary["market_diagnostics"]["breadth_scope"], key_title="Breadth Scope"))
        lines.extend(["", "## Theme / Leadership Diagnostics", ""])
        lines.extend(_performance_table(summary["theme_leadership_diagnostics"]["reason_codes"], key_title="Reason Code"))
        lines.extend(["", "### Leader Persistence Score", ""])
        lines.extend(_performance_table(summary["theme_leadership_diagnostics"]["leader_persistence_score_bins"], key_title="Score Bin"))
        lines.extend(["", "## Late Chase Diagnostics", ""])
        lines.extend(_performance_table(summary["late_chase_diagnostics"]["level_performance"], key_title="Level"))
        lines.extend(["", "### Late Chase Comparison", ""])
        lines.extend(_performance_table(summary["late_chase_diagnostics"]["breakout_vs_late_chase"], key_title="Case"))
        lines.extend(
            [
                "",
                f"- LATE_CHASE count: {summary['late_chase_diagnostics']['late_chase_count']}",
                f"- LATE_CHASE soft_block avg max_return_20m: {_cell(summary['late_chase_diagnostics']['soft_block_max_return_20m_avg'])}",
                f"- LATE_CHASE warning count: {summary['late_chase_diagnostics'].get('warning_count', 0)}",
                f"- LATE_CHASE warning avg max_drawdown_20m: {_cell(summary['late_chase_diagnostics'].get('warning_max_drawdown_20m_avg'))}",
            ]
        )
        lines.extend(["", "## Fill Diagnostics", ""])
        lines.extend(_performance_table(summary["fill_diagnostics"]["confidence_level_performance"], key_title="Confidence"))
        lines.extend(["", "### Fill Reason Codes", ""])
        lines.extend(_performance_table(summary["fill_diagnostics"]["reason_codes"], key_title="Reason Code"))
        lines.extend(
            [
                "",
                f"- legacy_fill_result=true and v2_would_fill=false: {summary['fill_diagnostics']['legacy_fill_true_v2_false_count']}",
            ]
        )
        lines.extend(["", "## Hybrid Gate Summary", ""])
        lines.extend(_hybrid_summary_rows(summary["hybrid_gate_summary"]))
        lines.extend(["", "## Details", ""])
        if reviews:
            lines.append("| Time | Code | Theme | Grade | Status | 20m High | 20m DD | Reason |")
            lines.append("|---|---|---|---|---|---:|---:|---|")
            for review in reviews:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            _cell(review.created_at),
                            _cell(review.code),
                            _cell(review.theme_name or review.theme_id),
                            _cell(review.final_grade),
                            _cell(review.final_status),
                            _cell(review.max_return_20m),
                            _cell(review.max_drawdown_20m),
                            _cell(review.missed_reason or review.exit_reason),
                        ]
                    )
                    + " |"
                )
        else:
            lines.append("_No reviews._")
        with _temp_path(target, encoding="utf-8", newline="\n") as temp:
            temp.handle.write("\n".join(lines) + "\n")
        return target


def build_review_summary(reviews: list[TradeReview]) -> dict:
    reason_performance = _performance_by_key(reviews, lambda review: _all_reason_codes(review))
    legacy_new_matrix = _legacy_new_matrix(reviews)
    return {
        "thresholds": {
            "false_negative_return_pct": FALSE_NEGATIVE_RETURN_THRESHOLD_PCT,
            "false_positive_drawdown_pct": FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT,
        },
        "reason_code_performance": reason_performance,
        "legacy_new_matrix": legacy_new_matrix,
        "interpretation": _interpret_legacy_matrix(legacy_new_matrix),
        "session_bucket_performance": _session_bucket_performance(reviews),
        "market_diagnostics": _market_diagnostics(reviews),
        "theme_leadership_diagnostics": _theme_leadership_diagnostics(reviews),
        "late_chase_diagnostics": _late_chase_diagnostics(reviews),
        "fill_diagnostics": _fill_diagnostics(reviews),
        "hybrid_gate_summary": summarize_hybrid_gate_reviews(reviews),
    }


def _summary_csv_rows(summary: dict) -> list[dict]:
    rows: list[dict] = []
    for section, values in [
        ("reason_code", summary.get("reason_code_performance") or []),
        ("session_bucket", summary.get("session_bucket_performance") or []),
        ("market_reason", (summary.get("market_diagnostics") or {}).get("reason_codes") or []),
        ("theme_leadership_reason", (summary.get("theme_leadership_diagnostics") or {}).get("reason_codes") or []),
        ("late_chase_level", (summary.get("late_chase_diagnostics") or {}).get("level_performance") or []),
        ("fill_confidence", (summary.get("fill_diagnostics") or {}).get("confidence_level_performance") or []),
    ]:
        for item in values:
            row = {column: item.get(column) for column in REVIEW_SUMMARY_COLUMNS}
            row["section"] = section
            row["key"] = item.get("key")
            rows.append(row)
    return rows


def _performance_by_key(reviews: list[TradeReview], key_fn) -> list[dict]:
    groups: dict[str, list[TradeReview]] = {}
    for review in reviews:
        for key in key_fn(review):
            groups.setdefault(key, []).append(review)
    return [
        _performance_row(key, items)
        for key, items in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def _performance_row(key: str, reviews: list[TradeReview]) -> dict:
    reason_counter = Counter()
    for review in reviews:
        reason_counter.update(_all_reason_codes(review))
    return {
        "key": key,
        "candidate_count": len(reviews),
        "ready_count": sum(1 for review in reviews if _is_ready(review)),
        "blocked_count": sum(1 for review in reviews if _is_blocked(review)),
        "soft_block_count": sum(1 for review in reviews if _is_soft_block(review)),
        "virtual_filled_count": sum(1 for review in reviews if _is_virtual_filled(review)),
        "max_return_5m_avg": _avg(_metric_values(reviews, "max_return_5m")),
        "max_return_10m_avg": _avg(_metric_values(reviews, "max_return_10m")),
        "max_return_20m_avg": _avg(_metric_values(reviews, "max_return_20m")),
        "max_drawdown_20m_avg": _avg(_metric_values(reviews, "max_drawdown_20m")),
        "false_positive_count": sum(1 for review in reviews if review.false_positive_flag),
        "false_negative_count": sum(1 for review in reviews if review.false_negative_flag),
        "opportunity_missed_count": sum(1 for review in reviews if _is_opportunity_missed(review)),
        "loss_avoided_count": sum(1 for review in reviews if _is_loss_avoided(review)),
        "top_reason_codes": [code for code, _ in reason_counter.most_common(5)],
        "samples": _sample_codes(reviews),
    }


def _legacy_new_matrix(reviews: list[TradeReview]) -> list[dict]:
    labels = [
        ("ready", "ready"),
        ("ready", "blocked"),
        ("blocked", "ready"),
        ("blocked", "blocked"),
    ]
    groups = {label: [] for label in labels}
    for review in reviews:
        details = dict(review.details or {})
        legacy = _decision_bucket(details.get("legacy_result", review.final_status))
        new = _decision_bucket(details.get("new_result", details.get("legacy_result", review.final_status)))
        if (legacy, new) in groups:
            groups[(legacy, new)].append(review)
    rows = []
    for legacy, new in labels:
        items = groups[(legacy, new)]
        reason_counter = Counter()
        for review in items:
            reason_counter.update(_all_reason_codes(review))
        metric_count = len([review for review in items if review.max_return_20m is not None or review.max_drawdown_20m is not None])
        denominator = metric_count or len(items) or 1
        rows.append(
            {
                "key": f"legacy_{legacy}_new_{new}",
                "legacy": legacy,
                "new": new,
                "count": len(items),
                "max_return_20m_avg": _avg(_metric_values(items, "max_return_20m")),
                "max_drawdown_20m_avg": _avg(_metric_values(items, "max_drawdown_20m")),
                "positive_rate": _rate(sum(1 for review in items if _positive_after(review)), denominator),
                "negative_rate": _rate(sum(1 for review in items if _negative_after(review)), denominator),
                "representative_reason_codes": [code for code, _ in reason_counter.most_common(5)],
                "samples": _sample_codes(items),
            }
        )
    return rows


def _interpret_legacy_matrix(matrix: list[dict]) -> list[str]:
    rows = {item["key"]: item for item in matrix}
    messages = []
    ready_blocked = rows.get("legacy_ready_new_blocked", {})
    if ready_blocked.get("count", 0):
        if float(ready_blocked.get("negative_rate") or 0) > float(ready_blocked.get("positive_rate") or 0):
            messages.append("legacy ready / new blocked: 이후 하락 비율이 높아 신규 룰이 오탐을 줄였을 가능성이 있습니다. 추천 후보로만 검토하세요.")
        else:
            messages.append("legacy ready / new blocked: 이후 급등 비율이 높아 신규 룰이 미탐을 만들었을 가능성이 있습니다. 완화 후보입니다.")
    blocked_ready = rows.get("legacy_blocked_new_ready", {})
    if blocked_ready.get("count", 0):
        if float(blocked_ready.get("positive_rate") or 0) >= float(blocked_ready.get("negative_rate") or 0):
            messages.append("legacy blocked / new ready: 이후 급등 비율이 높아 신규 룰이 기회를 살렸을 가능성이 있습니다. 완화 후보입니다.")
        else:
            messages.append("legacy blocked / new ready: 이후 하락 비율이 높아 신규 룰이 위험 진입을 늘렸을 가능성이 있습니다. 강화 검토가 필요합니다.")
    return messages or ["legacy/new 비교에서 뚜렷한 조정 후보가 아직 없습니다."]


def _session_bucket_performance(reviews: list[TradeReview]) -> list[dict]:
    groups: dict[str, list[TradeReview]] = {bucket: [] for bucket in SESSION_BUCKETS}
    for review in reviews:
        bucket = str((review.details or {}).get("session_bucket") or "UNKNOWN")
        groups.setdefault(bucket, []).append(review)
    rows = []
    for bucket, items in groups.items():
        if not items and bucket not in SESSION_BUCKETS:
            continue
        base = _performance_row(bucket, items)
        base["ready_rate"] = _rate(base["ready_count"], base["candidate_count"])
        base["virtual_fill_rate"] = _rate(base["virtual_filled_count"], base["candidate_count"])
        rows.append(base)
    return rows


def _market_diagnostics(reviews: list[TradeReview]) -> dict:
    return {
        "reason_codes": _performance_by_key(reviews, lambda review: [code for code in _all_reason_codes(review) if code in MARKET_DIAGNOSTIC_CODES]),
        "breadth_scope": _performance_by_key(reviews, lambda review: [_breadth_scope(review)] if _breadth_scope(review) else []),
    }


def _theme_leadership_diagnostics(reviews: list[TradeReview]) -> dict:
    return {
        "reason_codes": _performance_by_key(reviews, lambda review: [code for code in _all_reason_codes(review) if code in THEME_LEADERSHIP_CODES]),
        "leader_persistence_score_bins": _performance_by_key(reviews, lambda review: [_score_bin(_leader_persistence_score(review))]),
    }


def _late_chase_diagnostics(reviews: list[TradeReview]) -> dict:
    late_chase_items = [review for review in reviews if "LATE_CHASE" in _all_reason_codes(review)]
    soft_block_items = [review for review in reviews if _late_chase_level(review) == "soft_block"]
    warning_items = [review for review in reviews if _late_chase_level(review) == "warning" or "LATE_CHASE_WARNING" in _all_reason_codes(review)]
    breakout_items = [
        review
        for review in reviews
        if _late_chase_detail(review).get("near_session_high") is True
        and _late_chase_detail(review).get("volume_reacceleration_confirmed") is True
        and _late_chase_level(review) in {"none", "warning"}
    ]
    return {
        "late_chase_count": len(late_chase_items),
        "soft_block_max_return_20m_avg": _avg(_metric_values(soft_block_items, "max_return_20m")),
        "warning_count": len(warning_items),
        "warning_max_return_20m_avg": _avg(_metric_values(warning_items, "max_return_20m")),
        "warning_max_drawdown_20m_avg": _avg(_metric_values(warning_items, "max_drawdown_20m")),
        "level_performance": _performance_by_key(reviews, lambda review: [_late_chase_level(review)] if _late_chase_level(review) else []),
        "breakout_vs_late_chase": [
            _performance_row("breakout_guardrail_passed", breakout_items),
            _performance_row("late_chase_soft_block", soft_block_items),
            _performance_row("late_chase_warning_tag_only", warning_items),
        ],
    }


def _fill_diagnostics(reviews: list[TradeReview]) -> dict:
    legacy_true_v2_false = [
        review
        for review in reviews
        if _fill_detail(review).get("legacy_fill_result") is True and _fill_detail(review).get("v2_would_fill") is False
    ]
    return {
        "legacy_fill_true_v2_false_count": len(legacy_true_v2_false),
        "confidence_level_performance": _performance_by_key(reviews, lambda review: [_fill_confidence_level(review)] if _fill_confidence_level(review) else []),
        "reason_codes": _performance_by_key(reviews, lambda review: [code for code in _all_reason_codes(review) if code in FILL_DIAGNOSTIC_CODES]),
    }


def _performance_table(rows: list[dict], key_title: str = "Key") -> list[str]:
    if not rows:
        return ["_None._"]
    lines = [
        f"| {key_title} | Count | Ready | Blocked | Soft Block | Filled | Avg 20m High | Avg 20m DD | FP | FN | Missed | Loss Avoided | Top Reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("key")),
                    _cell(row.get("candidate_count")),
                    _cell(row.get("ready_count")),
                    _cell(row.get("blocked_count")),
                    _cell(row.get("soft_block_count")),
                    _cell(row.get("virtual_filled_count")),
                    _cell(row.get("max_return_20m_avg")),
                    _cell(row.get("max_drawdown_20m_avg")),
                    _cell(row.get("false_positive_count")),
                    _cell(row.get("false_negative_count")),
                    _cell(row.get("opportunity_missed_count")),
                    _cell(row.get("loss_avoided_count")),
                    _cell(", ".join(row.get("top_reason_codes") or [])),
                ]
            )
            + " |"
        )
    return lines


def _legacy_matrix_rows(rows: list[dict]) -> list[str]:
    lines = [
        "| Case | Count | Avg 20m High | Avg 20m DD | Positive Rate | Negative Rate | Representative Reasons |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("key")),
                    _cell(row.get("count")),
                    _cell(row.get("max_return_20m_avg")),
                    _cell(row.get("max_drawdown_20m_avg")),
                    _cell(row.get("positive_rate")),
                    _cell(row.get("negative_rate")),
                    _cell(", ".join(row.get("representative_reason_codes") or [])),
                ]
            )
            + " |"
        )
    return lines


def _interpretation_rows(messages: list[str]) -> list[str]:
    return [f"- {message}" for message in messages] if messages else ["_None._"]


def _session_rows(rows: list[dict]) -> list[str]:
    lines = [
        "| Session | Count | Ready Rate | Fill Rate | Avg 20m High | Avg 20m DD | FP | FN | Top Reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("key")),
                    _cell(row.get("candidate_count")),
                    _cell(row.get("ready_rate")),
                    _cell(row.get("virtual_fill_rate")),
                    _cell(row.get("max_return_20m_avg")),
                    _cell(row.get("max_drawdown_20m_avg")),
                    _cell(row.get("false_positive_count")),
                    _cell(row.get("false_negative_count")),
                    _cell(", ".join(row.get("top_reason_codes") or [])),
                ]
            )
            + " |"
        )
    return lines


def _hybrid_summary_rows(summary: dict) -> list[str]:
    if not summary or not summary.get("candidate_count"):
        return ["_None._"]
    lines = [
        f"- Hybrid candidates: {summary.get('candidate_count', 0)}",
        f"- READY but legacy not bought: {_cell(', '.join(summary.get('ready_but_legacy_not_bought') or []) or 0)}",
        f"- Legacy ready but Hybrid BLOCKED: {_cell(', '.join(summary.get('legacy_ready_but_hybrid_blocked') or []) or 0)}",
        f"- LEADER_ONLY_THEME blocked: {_cell(', '.join(summary.get('leader_only_blocked') or []) or 0)}",
        f"- WATCH small-entry candidates: {_cell(', '.join(summary.get('watch_small_entry_candidates') or []) or 0)}",
        "",
        "### Hybrid Status Counts",
        "",
    ]
    status_counts = summary.get("status_counts") or {}
    if status_counts:
        lines.extend(["| Status | Count |", "|---|---:|"])
        for status, count in sorted(status_counts.items()):
            lines.append(f"| {_cell(status)} | {_cell(count)} |")
    else:
        lines.append("_None._")
    lines.extend(["", "### Hybrid WAIT Reasons", ""])
    wait_reasons = summary.get("wait_reason_top") or []
    if wait_reasons:
        lines.extend(["| Reason | Count |", "|---|---:|"])
        for item in wait_reasons[:10]:
            lines.append(f"| {_cell(item.get('reason_code'))} | {_cell(item.get('count'))} |")
    else:
        lines.append("_None._")
    lines.extend(["", "### Hybrid Score Buckets", ""])
    lines.extend(_hybrid_bucket_rows("Theme Score", summary.get("theme_score_buckets") or {}))
    lines.extend(["", "### Membership Score Buckets", ""])
    lines.extend(_hybrid_bucket_rows("Membership Score", summary.get("membership_score_buckets") or {}))
    return lines


def _hybrid_bucket_rows(title: str, buckets: dict) -> list[str]:
    if not buckets:
        return ["_None._"]
    lines = [f"| {title} | Count |", "|---|---:|"]
    for key in ["0-39", "40-64", "65-74", "75+"]:
        lines.append(f"| {_cell(key)} | {_cell(buckets.get(key, 0))} |")
    return lines


def _all_reason_codes(review: TradeReview) -> list[str]:
    details = dict(review.details or {})
    values = []
    for key in [
        "blocking_reason_codes",
        "entry_condition_codes",
        "exit_reason_codes",
        "comparison_reason_codes",
        "secondary_reason_codes",
    ]:
        values.extend(details.get(key) or [])
    values.extend(_standard_reason_fields(details))
    diagnostics = details.get("fill_diagnostics_v2") or {}
    if isinstance(diagnostics, dict):
        values.extend(diagnostics.get("v2_non_fill_reason_codes") or [])
    if review.missed_reason:
        values.append(review.missed_reason)
    if review.exit_reason:
        values.append(review.exit_reason)
    values.extend(details.get("hybrid_reason_codes") or [])
    return _dedupe(values)


def _metric_values(reviews: list[TradeReview], field: str) -> list[float]:
    values = []
    for review in reviews:
        value = getattr(review, field, None)
        if value is None:
            continue
        values.append(float(value))
    return values


def _is_ready(review: TradeReview) -> bool:
    return str(review.final_status or "") in READY_STATUSES


def _is_blocked(review: TradeReview) -> bool:
    return str(review.final_status or "") in BLOCKED_STATUSES


def _is_virtual_filled(review: TradeReview) -> bool:
    return str(review.final_status or "") in VIRTUAL_FILLED_STATUSES or str(review.virtual_order_status or "") == "filled"


def _is_soft_block(review: TradeReview) -> bool:
    return "SOFT_BLOCK_ONLY" in _all_reason_codes(review)


def _is_opportunity_missed(review: TradeReview) -> bool:
    return bool(review.false_negative_flag) or ((_is_blocked(review) or _is_soft_block(review)) and _positive_after(review))


def _is_loss_avoided(review: TradeReview) -> bool:
    return (_is_blocked(review) or _is_soft_block(review)) and _negative_after(review)


def _positive_after(review: TradeReview) -> bool:
    return review.max_return_20m is not None and float(review.max_return_20m) >= FALSE_NEGATIVE_RETURN_THRESHOLD_PCT


def _negative_after(review: TradeReview) -> bool:
    return review.max_drawdown_20m is not None and float(review.max_drawdown_20m) <= FALSE_POSITIVE_DRAWDOWN_THRESHOLD_PCT


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def _decision_bucket(value) -> str:
    if isinstance(value, bool):
        return "ready" if value else "blocked"
    text = str(value or "").strip().upper()
    if not text:
        return "blocked"
    if text in {"TRUE", "READY", "PASS", "PASSED"} or text in READY_STATUSES:
        return "ready"
    if text.startswith("VIRTUAL_"):
        return "ready"
    return "blocked"


def _breadth_scope(review: TradeReview) -> str:
    details = dict(review.details or {})
    diagnostics = details.get("market_diagnostics_v2") or details.get("market_diagnostics") or {}
    if isinstance(diagnostics, dict):
        return str(diagnostics.get("breadth_scope") or diagnostics.get("scope") or "")
    return str(details.get("breadth_scope") or "")


def _leader_persistence_score(review: TradeReview):
    details = dict(review.details or {})
    diagnostics = details.get("leadership_diagnostics_v2") or {}
    if isinstance(diagnostics, dict):
        return diagnostics.get("leader_persistence_score")
    return None


def _score_bin(value) -> str:
    if value is None:
        return "missing"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "missing"
    if score >= 70:
        return "70+"
    if score >= 40:
        return "40-69"
    return "0-39"


def _late_chase_detail(review: TradeReview) -> dict:
    diagnostics = (review.details or {}).get("late_chase_diagnostics") or {}
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def _late_chase_level(review: TradeReview) -> str:
    diagnostics = _late_chase_detail(review)
    return str(diagnostics.get("late_chase_level") or (review.details or {}).get("late_chase_level") or "")


def _fill_detail(review: TradeReview) -> dict:
    diagnostics = (review.details or {}).get("fill_diagnostics_v2") or {}
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def _fill_confidence_level(review: TradeReview) -> str:
    return str(_fill_detail(review).get("fill_confidence_level") or "")


def _summary_rows(reviews: list[TradeReview]) -> list[str]:
    if not reviews:
        return ["_None._"]
    rows = ["| Code | Theme | Status | 20m High | 20m DD | Type |", "|---|---|---|---:|---:|---|"]
    for review in reviews:
        rows.append(
            "| "
            + " | ".join(
                [
                    _cell(review.code),
                    _cell(review.theme_name or review.theme_id),
                    _cell(review.final_status),
                    _cell(review.max_return_20m),
                    _cell(review.max_drawdown_20m),
                    _cell(review.details.get("false_negative_type") or review.missed_reason),
                ]
            )
            + " |"
        )
    return rows


def _reason_code_rows(reviews: list[TradeReview], mode: str) -> list[str]:
    groups: dict[str, list[TradeReview]] = {}
    for review in reviews:
        for code in _reason_codes(review, mode):
            groups.setdefault(code, []).append(review)
    if not groups:
        return ["_None._"]
    rows = [
        "| Reason Code | Count | Avg 20m High | Max 20m High | Avg 20m DD | Samples |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for code, items in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        returns = [float(review.max_return_20m) for review in items if review.max_return_20m is not None]
        drawdowns = [float(review.max_drawdown_20m) for review in items if review.max_drawdown_20m is not None]
        samples = ", ".join(_sample_codes(items))
        rows.append(
            "| "
            + " | ".join(
                [
                    _cell(code),
                    _cell(len(items)),
                    _cell(_avg(returns)),
                    _cell(max(returns) if returns else None),
                    _cell(_avg(drawdowns)),
                    _cell(samples),
                ]
            )
            + " |"
        )
    return rows


def _reason_codes(review: TradeReview, mode: str) -> list[str]:
    details = dict(review.details or {})
    if mode == "false_negative":
        values = list(details.get("blocking_reason_codes") or [])
        values.extend(details.get("comparison_reason_codes") or [])
        values.extend(_standard_reason_fields(details))
        if not values and review.missed_reason:
            values.append(review.missed_reason)
        return _dedupe(values)
    values = list(details.get("entry_condition_codes") or [])
    values.extend(details.get("exit_reason_codes") or [])
    values.extend(details.get("comparison_reason_codes") or [])
    values.extend(_standard_reason_fields(details))
    if not values and review.exit_reason:
        values.append(review.exit_reason)
    return _dedupe(values)


def _standard_reason_fields(details: dict) -> list[str]:
    values = []
    primary = details.get("primary_reason_code")
    if primary:
        values.append(primary)
    values.extend(details.get("secondary_reason_codes") or [])
    return values


def _sample_codes(reviews: list[TradeReview]) -> list[str]:
    result: list[str] = []
    for review in reviews:
        code = str(review.code or "")
        if code and code not in result:
            result.append(code)
        if len(result) >= 5:
            break
    return result


def _avg(values: list[float]):
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _dedupe(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result


class _temp_path:
    def __init__(self, target: Path, encoding: str, newline: str) -> None:
        self.target = target
        self.encoding = encoding
        self.newline = newline
        self.handle = None
        self.path = None

    def __enter__(self):
        temp = NamedTemporaryFile(
            "w",
            delete=False,
            dir=self.target.parent,
            prefix=f".{self.target.name}.",
            suffix=".tmp",
            encoding=self.encoding,
            newline=self.newline,
        )
        self.handle = temp
        self.path = Path(temp.name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.handle is not None
        assert self.path is not None
        self.handle.close()
        if exc_type is not None:
            self.path.unlink(missing_ok=True)
            return
        self.path.replace(self.target)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if isinstance(value, bool):
        return "Y" if value else ""
    return str(value)
