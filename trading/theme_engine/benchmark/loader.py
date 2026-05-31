from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from trading.theme_engine.benchmark.schemas import (
    ExternalThemeBenchmarkSnapshot,
    ExternalThemeBenchmarkStock,
    ExternalThemeBenchmarkTheme,
)
from trading.theme_engine.normalizer import normalize_stock_code


DEFAULT_RANKING_BASIS = "change_rate"


def load_external_theme_benchmark(path: str | Path) -> ExternalThemeBenchmarkSnapshot:
    file_path = _local_path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        return _load_json(file_path)
    if suffix == ".csv":
        return _load_csv(file_path)
    raise ValueError(f"unsupported benchmark file type: {file_path.suffix}")


def _load_json(path: Path) -> ExternalThemeBenchmarkSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("benchmark must be object")
    source = _required_text(payload.get("source"), "missing source")
    trade_date = _required_text(payload.get("trade_date"), "missing trade_date")
    themes = payload.get("themes")
    if not isinstance(themes, list):
        raise ValueError("themes must be list")
    result: ExternalThemeBenchmarkSnapshot = {
        "source": source,
        "captured_at": str(payload.get("captured_at") or ""),
        "trade_date": trade_date,
        "ranking_basis": str(payload.get("ranking_basis") or DEFAULT_RANKING_BASIS),
        "themes": [_json_theme_dict(theme, index) for index, theme in enumerate(themes, start=1)],
    }
    _validate_theme_ranks(result["themes"])
    return result


def _load_csv(path: Path) -> ExternalThemeBenchmarkSnapshot:
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        raise ValueError("themes must be list")
    source = _single_required_value(rows, "source", "missing source")
    trade_date = _single_required_value(rows, "trade_date", "missing trade_date")
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        theme_name = _required_text(row.get("theme_name"), "missing external_theme_name")
        group = grouped.setdefault(
            theme_name,
            {
                "external_theme_name": theme_name,
                "canonical_theme_hint": str(row.get("canonical_theme_hint") or theme_name),
                "rank": _int_value(row.get("theme_rank"), default=len(grouped) + 1),
                "score": 0.0,
                "top_stocks": [],
                "members": [],
            },
        )
        stock = _stock_dict(
            row,
            rank_key="stock_rank",
            require_rank=False,
            include_metrics=True,
        )
        group["members"].append(_member_dict(stock))
        if _is_top_stock(row):
            group["top_stocks"].append(stock)
    themes = list(grouped.values())
    for theme in themes:
        if not theme["top_stocks"] and not theme["members"]:
            raise ValueError("top_stocks or members required")
        theme["top_stocks"] = sorted(theme["top_stocks"], key=lambda item: int(item.get("rank") or 0))
    _validate_theme_ranks(themes)
    return {
        "source": source,
        "captured_at": "",
        "trade_date": trade_date,
        "ranking_basis": DEFAULT_RANKING_BASIS,
        "themes": themes,
    }


def _json_theme_dict(theme: Any, index: int) -> ExternalThemeBenchmarkTheme:
    if not isinstance(theme, dict):
        raise ValueError("theme must be object")
    theme_name = _required_text(theme.get("external_theme_name"), "missing external_theme_name")
    top_stocks = [_json_stock_dict(item, require_rank=True, include_metrics=True) for item in list(theme.get("top_stocks") or [])]
    members = [_json_stock_dict(item, require_rank=False, include_metrics=False) for item in list(theme.get("members") or [])]
    if not top_stocks and not members:
        raise ValueError("top_stocks or members required")
    return {
        "external_theme_name": theme_name,
        "canonical_theme_hint": str(theme.get("canonical_theme_hint") or theme_name),
        "rank": _int_value(theme.get("rank"), default=index),
        "score": _float_value(theme.get("score"), default=0.0),
        "top_stocks": sorted(top_stocks, key=lambda item: int(item.get("rank") or 0)),
        "members": members,
    }


def _json_stock_dict(
    item: Any,
    *,
    require_rank: bool,
    include_metrics: bool,
) -> ExternalThemeBenchmarkStock:
    if not isinstance(item, dict):
        raise ValueError("stock must be object")
    return _stock_dict(item, rank_key="rank", require_rank=require_rank, include_metrics=include_metrics)


def _stock_dict(
    item: dict[str, Any],
    *,
    rank_key: str,
    require_rank: bool,
    include_metrics: bool,
) -> ExternalThemeBenchmarkStock:
    raw_code = item.get("stock_code")
    stock_code = normalize_stock_code(str(raw_code or ""))
    if not stock_code:
        raise ValueError(f"invalid stock_code: {raw_code}")
    result: ExternalThemeBenchmarkStock = {
        "stock_code": stock_code,
        "stock_name": str(item.get("stock_name") or ""),
    }
    rank = _int_value(item.get(rank_key), default=0)
    if require_rank or rank:
        result["rank"] = rank
    if include_metrics:
        change_rate = _optional_float(item.get("change_rate"))
        turnover = _optional_float(item.get("turnover"))
        if change_rate is not None:
            result["change_rate"] = change_rate
        if turnover is not None:
            result["turnover"] = turnover
    return result


def _member_dict(stock: ExternalThemeBenchmarkStock) -> ExternalThemeBenchmarkStock:
    return {
        "stock_code": stock["stock_code"],
        "stock_name": stock.get("stock_name", ""),
    }


def _is_top_stock(row: dict[str, Any]) -> bool:
    raw = str(row.get("is_top_stock") or "").strip().lower()
    if raw:
        return raw in {"1", "true", "t", "yes", "y"}
    return _int_value(row.get("stock_rank"), default=0) <= 5


def _validate_theme_ranks(themes: list[ExternalThemeBenchmarkTheme]) -> None:
    seen: set[int] = set()
    for theme in themes:
        rank = int(theme["rank"])
        if rank in seen:
            raise ValueError("duplicate theme rank")
        seen.add(rank)


def _single_required_value(rows: list[dict[str, Any]], key: str, message: str) -> str:
    values = {str(row.get(key) or "").strip() for row in rows if str(row.get(key) or "").strip()}
    if not values:
        raise ValueError(message)
    if len(values) > 1:
        raise ValueError(f"multiple {key} values")
    return values.pop()


def _required_text(value: Any, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(message)
    return text


def _int_value(value: Any, *, default: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        if default is not None:
            return int(default)
        raise ValueError("invalid rank")
    try:
        return int(float(str(value).strip()))
    except ValueError as exc:
        raise ValueError("invalid rank") from exc


def _float_value(value: Any, *, default: float | None = None) -> float:
    if value is None or str(value).strip() == "":
        if default is not None:
            return float(default)
        raise ValueError("invalid numeric value")
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise ValueError(f"invalid numeric value: {value}") from exc


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return _float_value(value)


def _local_path(path: str | Path) -> Path:
    text = str(path)
    if "://" in text:
        raise ValueError("local file path required")
    return Path(path)
