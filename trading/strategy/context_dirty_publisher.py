from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data_service import DirtyReason


@dataclass(frozen=True)
class DirtyPublishResult:
    published_count: int = 0
    skipped_count: int = 0
    reason: str = ""
    codes: tuple[str, ...] = ()


class MarketRegimeDirtyPublisher:
    def __init__(self) -> None:
        self._previous: dict[str, Any] = {}

    def publish(self, snapshot: Mapping[str, Any], *, dirty_queue: Any, codes_by_side: Mapping[str, Iterable[str]] | None = None, now: datetime | None = None) -> DirtyPublishResult:
        current = _market_key(snapshot)
        if current == self._previous:
            return DirtyPublishResult(skipped_count=1, reason="MARKET_REGIME_UNCHANGED")
        previous = dict(self._previous)
        self._previous = current
        codes = []
        for side in _changed_sides(previous, current):
            codes.extend(normalize_code(code) for code in list((codes_by_side or {}).get(side, ()) or []) if normalize_code(code))
        return _publish(dirty_queue, codes, DirtyReason.MARKET_REGIME_CHANGED.value, now=now)


class ThemeStateDirtyPublisher:
    def __init__(self) -> None:
        self._previous_theme_state: dict[str, str] = {}
        self._previous_leaders: dict[str, tuple[str, str]] = {}
        self._previous_roles: dict[tuple[str, str], str] = {}

    def publish(
        self,
        theme_states: Iterable[Any],
        *,
        dirty_queue: Any,
        code_by_theme: Mapping[str, Iterable[str]] | None = None,
        stock_roles: Iterable[Any] = (),
        now: datetime | None = None,
    ) -> DirtyPublishResult:
        state_codes: list[str] = []
        leader_codes: list[str] = []
        for state in theme_states:
            theme_id = str(_get(state, "theme_id") or "")
            if not theme_id:
                continue
            state_value = str(_get(state, "theme_state") or _get(state, "theme_status") or "")
            leader_value = normalize_code(_get(state, "leader_symbol") or "")
            co_leaders = ",".join(normalize_code(str(item)) for item in tuple(_get(state, "co_leader_symbols") or ()) if normalize_code(str(item)))
            if self._previous_theme_state.get(theme_id) != state_value:
                state_codes.extend(_theme_codes(theme_id, code_by_theme))
            previous_leaders = self._previous_leaders.get(theme_id)
            current_leaders = (leader_value, co_leaders)
            if previous_leaders is not None and previous_leaders != current_leaders:
                leader_codes.extend(_theme_codes(theme_id, code_by_theme))
                if leader_value:
                    leader_codes.append(leader_value)
                leader_codes.extend(code for code in co_leaders.split(",") if code)
            self._previous_theme_state[theme_id] = state_value
            self._previous_leaders[theme_id] = current_leaders

        role_codes: list[str] = []
        for stock in stock_roles:
            theme_id = str(_get(stock, "theme_id") or "")
            code = normalize_code(_get(stock, "code") or _get(stock, "stock_code") or "")
            role = str(_get(stock, "trade_role") or _get(stock, "stock_role") or _get(stock, "raw_role") or "")
            if not theme_id or not code or not role:
                continue
            key = (theme_id, code)
            previous_role = self._previous_roles.get(key)
            if previous_role is not None and previous_role != role:
                role_codes.append(code)
            self._previous_roles[key] = role

        return _combine_results(
            (
                _publish(dirty_queue, state_codes, DirtyReason.THEME_STATE_CHANGED.value, now=now),
                _publish(dirty_queue, leader_codes, DirtyReason.THEME_LEADER_CHANGED.value, now=now),
                _publish(dirty_queue, role_codes, DirtyReason.THEME_ROLE_CHANGED.value, now=now),
            )
        )


class StrategyContextDirtyPublisher:
    def __init__(self) -> None:
        self._previous_context_by_code: dict[str, str] = {}

    def publish(self, contexts: Iterable[Mapping[str, Any]], *, dirty_queue: Any, now: datetime | None = None) -> DirtyPublishResult:
        codes: list[str] = []
        for context in contexts:
            code = normalize_code(context.get("code") or "")
            context_id = str(context.get("context_id") or "")
            if not code or not context_id:
                continue
            if self._previous_context_by_code.get(code) == context_id:
                continue
            self._previous_context_by_code[code] = context_id
            codes.append(code)
        return _publish(dirty_queue, codes, DirtyReason.STRATEGY_CONTEXT_CHANGED.value, now=now)


def _publish(dirty_queue: Any, codes: Iterable[str], reason: str, *, now: datetime | None) -> DirtyPublishResult:
    mark = getattr(dirty_queue, "mark_dirty", None)
    unique = tuple(dict.fromkeys(normalize_code(code) for code in codes if normalize_code(code)))
    if not callable(mark):
        return DirtyPublishResult(skipped_count=len(unique), reason="DIRTY_QUEUE_UNAVAILABLE", codes=unique)
    published = 0
    for code in unique:
        if mark(code, reason, marked_at=now):
            published += 1
    return DirtyPublishResult(published_count=published, skipped_count=len(unique) - published, reason=reason, codes=unique)


def _combine_results(results: Iterable[DirtyPublishResult]) -> DirtyPublishResult:
    items = list(results)
    reasons: list[str] = []
    codes: list[str] = []
    for item in items:
        if (item.codes or item.published_count or item.skipped_count) and item.reason and item.reason not in reasons:
            reasons.append(item.reason)
        codes.extend(item.codes)
    if not reasons:
        return DirtyPublishResult(skipped_count=1, reason="THEME_UNCHANGED")
    return DirtyPublishResult(
        published_count=sum(item.published_count for item in items),
        skipped_count=sum(item.skipped_count for item in items),
        reason=",".join(reasons),
        codes=tuple(dict.fromkeys(normalize_code(code) for code in codes if normalize_code(code))),
    )


def _get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _market_key(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "global": str(snapshot.get("global_status") or ""),
        "kospi": str(snapshot.get("kospi_status") or ""),
        "kosdaq": str(snapshot.get("kosdaq_status") or ""),
        "action": str(snapshot.get("market_action") or ""),
        "block": bool(snapshot.get("block_new_entry")),
    }


def _changed_sides(previous: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, ...]:
    if not previous:
        return ("KOSPI", "KOSDAQ", "UNKNOWN")
    sides: list[str] = []
    shared_changed = previous.get("action") != current.get("action") or previous.get("block") != current.get("block")
    if shared_changed or previous.get("kospi") != current.get("kospi"):
        sides.append("KOSPI")
    if shared_changed or previous.get("kosdaq") != current.get("kosdaq"):
        sides.append("KOSDAQ")
    if shared_changed or previous.get("global") != current.get("global"):
        sides.append("UNKNOWN")
    return tuple(sides)


def _theme_codes(theme_id: str, code_by_theme: Mapping[str, Iterable[str]] | None) -> list[str]:
    return [normalize_code(code) for code in list((code_by_theme or {}).get(theme_id, ()) or []) if normalize_code(code)]


__all__ = [
    "DirtyPublishResult",
    "MarketRegimeDirtyPublisher",
    "StrategyContextDirtyPublisher",
    "ThemeStateDirtyPublisher",
]
