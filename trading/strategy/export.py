from __future__ import annotations

import csv
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

from trading.strategy.models import TradeReview


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


class ReviewExporter:
    def export_csv(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with _temp_path(target, encoding="utf-8-sig", newline="") as temp:
            writer = csv.DictWriter(temp.handle, fieldnames=REVIEW_EXPORT_COLUMNS)
            writer.writeheader()
            for review in reviews:
                writer.writerow({column: _cell(getattr(review, column, "")) for column in REVIEW_EXPORT_COLUMNS})
        return target

    def export_markdown(self, reviews: list[TradeReview], path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
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
        if not values and review.missed_reason:
            values.append(review.missed_reason)
        return _dedupe(values)
    values = list(details.get("entry_condition_codes") or [])
    values.extend(details.get("exit_reason_codes") or [])
    if not values and review.exit_reason:
        values.append(review.exit_reason)
    return _dedupe(values)


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
