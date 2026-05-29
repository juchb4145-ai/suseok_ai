from __future__ import annotations


def format_money(value: int) -> str:
    return f"{int(value):,}" if value else ""


def format_metric(value) -> str:
    return "" if value is None else f"{float(value):.2f}"


def format_percent(value) -> str:
    return f"{float(value or 0.0):.2f}%"


def dedupe_text(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
