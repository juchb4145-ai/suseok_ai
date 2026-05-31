from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trading.theme_engine.benchmark.schemas import InternalThemeBenchmarkSnapshot, InternalThemeBenchmarkTheme
from trading.theme_engine.models import ThemeActivitySnapshot


DEFAULT_INTERNAL_BENCHMARK_SOURCE = "internal_dynamic_theme_engine"


def export_internal_theme_benchmark(
    ranked: list[ThemeActivitySnapshot],
    trade_date: str,
    source: str = DEFAULT_INTERNAL_BENCHMARK_SOURCE,
) -> InternalThemeBenchmarkSnapshot:
    return {
        "source": source,
        "generated_at": _now_ts(),
        "trade_date": str(trade_date),
        "ranking_basis": "theme_score",
        "themes": [_theme_dict(item) for item in ranked],
    }


def _theme_dict(item: ThemeActivitySnapshot) -> InternalThemeBenchmarkTheme:
    details = dict(item.details or {})
    return {
        "theme_id": item.theme_id,
        "theme_name": item.theme_name,
        "rank": int(item.rank),
        "theme_score": float(item.theme_score),
        "weighted_return_pct": float(item.weighted_return_pct),
        "turnover": float(item.turnover),
        "breadth": float(item.breadth),
        "leader_code": item.leader_code,
        "top_stocks": _list_of_dicts(details.get("top_stocks")),
        "members": [
            member
            for member in _list_of_dicts(details.get("scored_members"))
            if bool(member.get("active", True))
        ],
        "reason_codes": [str(code) for code in list(details.get("reason_codes") or [])],
        "snapshot_quality": dict(details.get("snapshot_quality") or {}),
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
