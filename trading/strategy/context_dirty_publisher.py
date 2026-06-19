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
        self._previous = current
        codes = []
        for side in _changed_sides(current):
            codes.extend(normalize_code(code) for code in list((codes_by_side or {}).get(side, ()) or []) if normalize_code(code))
        return _publish(dirty_queue, codes, DirtyReason.MARKET_REGIME_CHANGED.value, now=now)


class ThemeStateDirtyPublisher:
    def __init__(self) -> None:
        self._previous: dict[str, tuple[str, str, str]] = {}

    def publish(self, theme_states: Iterable[Any], *, dirty_queue: Any, code_by_theme: Mapping[str, Iterable[str]] | None = None, now: datetime | None = None) -> DirtyPublishResult:
        codes: list[str] = []
        for state in theme_states:
            theme_id = str(getattr(state, "theme_id", "") or "")
            current = (
                str(getattr(state, "theme_state", "") or ""),
                str(getattr(state, "leader_symbol", "") or ""),
                ",".join(str(item) for item in tuple(getattr(state, "co_leader_symbols", ()) or ())),
            )
            if self._previous.get(theme_id) == current:
                continue
            self._previous[theme_id] = current
            codes.extend(normalize_code(code) for code in list((code_by_theme or {}).get(theme_id, ()) or []) if normalize_code(code))
            leader = normalize_code(getattr(state, "leader_symbol", "") or "")
            if leader:
                codes.append(leader)
        return _publish(dirty_queue, codes, DirtyReason.THEME_STATE_CHANGED.value, now=now)


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


def _market_key(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "global": str(snapshot.get("global_status") or ""),
        "kospi": str(snapshot.get("kospi_status") or ""),
        "kosdaq": str(snapshot.get("kosdaq_status") or ""),
        "action": str(snapshot.get("market_action") or ""),
        "block": bool(snapshot.get("block_new_entry")),
    }


def _changed_sides(_current: Mapping[str, Any]) -> tuple[str, ...]:
    return ("KOSPI", "KOSDAQ", "UNKNOWN")


__all__ = [
    "DirtyPublishResult",
    "MarketRegimeDirtyPublisher",
    "StrategyContextDirtyPublisher",
    "ThemeStateDirtyPublisher",
]
