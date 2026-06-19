from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code


@dataclass(frozen=True)
class BestThemeContext:
    code: str
    selected_theme_id: str = ""
    selected_reason: str = ""
    alternative_theme_ids: tuple[str, ...] = ()
    theme_selection_changed: bool = False
    previous_selected_theme_id: str = ""
    resolver_version: str = "best_theme_context_v1"
    theme: dict[str, Any] | None = None
    stock: dict[str, Any] | None = None


class BestThemeContextResolver:
    def resolve(
        self,
        code: str,
        *,
        theme_board: Mapping[str, Any],
        previous_selected_theme_id: str = "",
    ) -> BestThemeContext:
        clean_code = normalize_code(code)
        stocks = [dict(item or {}) for item in list(theme_board.get("stocks") or []) if normalize_code(dict(item or {}).get("code") or "") == clean_code]
        themes_by_id = _themes_by_id(theme_board)
        candidates = []
        for stock in stocks:
            theme_id = str(stock.get("theme_id") or "")
            theme = themes_by_id.get(theme_id, {})
            if not theme:
                continue
            candidates.append((stock, theme, _score(stock, theme)))
        if not candidates:
            return BestThemeContext(code=clean_code, selected_reason="NO_THEME_CONTEXT")
        stock, theme, _value = sorted(candidates, key=lambda item: item[2], reverse=True)[0]
        selected = str(theme.get("theme_id") or stock.get("theme_id") or "")
        alternatives = tuple(
            dict.fromkeys(
                str(item_theme.get("theme_id") or item_stock.get("theme_id") or "")
                for item_stock, item_theme, _score_value in sorted(candidates, key=lambda item: item[2], reverse=True)
                if str(item_theme.get("theme_id") or item_stock.get("theme_id") or "") and str(item_theme.get("theme_id") or item_stock.get("theme_id") or "") != selected
            )
        )
        return BestThemeContext(
            code=clean_code,
            selected_theme_id=selected,
            selected_reason=_reason(stock, theme),
            alternative_theme_ids=alternatives,
            theme_selection_changed=bool(previous_selected_theme_id and previous_selected_theme_id != selected),
            previous_selected_theme_id=previous_selected_theme_id,
            theme=theme,
            stock=stock,
        )

    def resolve_many(self, codes: Iterable[str], *, theme_board: Mapping[str, Any], previous_by_code: Mapping[str, str] | None = None) -> dict[str, BestThemeContext]:
        previous_by_code = previous_by_code or {}
        return {
            normalize_code(code): self.resolve(
                code,
                theme_board=theme_board,
                previous_selected_theme_id=str(previous_by_code.get(normalize_code(code)) or ""),
            )
            for code in codes
            if normalize_code(code)
        }


def _themes_by_id(board: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    themes: dict[str, dict[str, Any]] = {}
    for source in ("themes", "top_themes", "themes_by_id"):
        raw = board.get(source)
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                theme = dict(value or {})
                theme.setdefault("theme_id", key)
                themes[str(theme.get("theme_id") or key)] = theme
        else:
            for item in list(raw or []):
                theme = dict(item or {})
                theme_id = str(theme.get("theme_id") or "")
                if theme_id:
                    themes[theme_id] = theme
    return themes


def _score(stock: Mapping[str, Any], theme: Mapping[str, Any]) -> tuple[int, int, int, float, float, int, float]:
    return (
        _freshness_priority(theme),
        _theme_state_priority(theme),
        _trade_role_priority(stock),
        _float(theme.get("leadership_score")),
        _float(theme.get("theme_score")),
        _int(theme.get("persistence_count")),
        _float(stock.get("role_score") or stock.get("stock_score")),
    )


def _freshness_priority(theme: Mapping[str, Any]) -> int:
    status = str(theme.get("freshness_status") or theme.get("data_quality_status") or "FRESH").upper()
    return {"FRESH": 5, "OK": 5, "DEGRADED": 3, "SAMPLED": 3, "DATA_WAIT": 1, "STALE": 0}.get(status, 2)


def _theme_state_priority(theme: Mapping[str, Any]) -> int:
    state = str(theme.get("theme_state") or theme.get("theme_status") or "").upper()
    order = {
        "LEADING_THEME": 8,
        "SPREADING_THEME": 7,
        "LEADER_ONLY_THEME": 6,
        "EMERGING_THEME": 5,
        "WATCH_THEME": 4,
        "FADING_THEME": 3,
        "WEAK_THEME": 1,
        "DATA_WAIT": 0,
    }
    return order.get(state, 0)


def _trade_role_priority(stock: Mapping[str, Any]) -> int:
    role = str(stock.get("trade_role") or stock.get("stock_role") or "").upper()
    return {"LEADER_CONFIRMED": 5, "CO_LEADER_CONFIRMED": 4, "FOLLOWER_ALLOWED": 3, "LEADER": 2, "CO_LEADER": 2}.get(role, 0)


def _reason(stock: Mapping[str, Any], theme: Mapping[str, Any]) -> str:
    state = str(theme.get("theme_state") or theme.get("theme_status") or "")
    role = str(stock.get("trade_role") or stock.get("stock_role") or "")
    return f"SELECTED_BY_{state or 'THEME'}_{role or 'ROLE'}"


def _float(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


__all__ = ["BestThemeContext", "BestThemeContextResolver"]
