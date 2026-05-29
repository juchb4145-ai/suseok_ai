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
