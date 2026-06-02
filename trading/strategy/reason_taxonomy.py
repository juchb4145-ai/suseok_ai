from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from trading.strategy.reason_codes import normalize_reason_codes


WAIT_DATA = "WAIT_DATA"
WAIT_MARKET = "WAIT_MARKET"
WAIT_PULLBACK = "WAIT_PULLBACK"
WAIT_BREADTH = "WAIT_BREADTH"
WAIT_THEME_CONFIRMATION = "WAIT_THEME_CONFIRMATION"
WAIT_LEADER_CONFIRMATION = "WAIT_LEADER_CONFIRMATION"

BLOCK_THEME = "BLOCK_THEME"
BLOCK_RISK = "BLOCK_RISK"
BLOCK_CHASE = "BLOCK_CHASE"
BLOCK_LATE_LAGGARD = "BLOCK_LATE_LAGGARD"
BLOCK_DATA = "BLOCK_DATA"
BLOCK_LIQUIDITY = "BLOCK_LIQUIDITY"

OBSERVE_CHASE = "OBSERVE_CHASE"
OBSERVE_BREAKOUT = "OBSERVE_BREAKOUT"
OBSERVE_READY_SMALL = "OBSERVE_READY_SMALL"
OBSERVE_LEADER_ONLY = "OBSERVE_LEADER_ONLY"

READY = "READY"
WAIT = "WAIT"
BLOCKED = "BLOCKED"
OBSERVE = "OBSERVE"

WAIT_STATUSES = {
    WAIT_DATA,
    WAIT_MARKET,
    WAIT_PULLBACK,
    WAIT_BREADTH,
    WAIT_THEME_CONFIRMATION,
    WAIT_LEADER_CONFIRMATION,
}
BLOCK_STATUSES = {BLOCK_THEME, BLOCK_RISK, BLOCK_CHASE, BLOCK_LATE_LAGGARD, BLOCK_DATA, BLOCK_LIQUIDITY}
OBSERVE_STATUSES = {OBSERVE_CHASE, OBSERVE_BREAKOUT, OBSERVE_READY_SMALL, OBSERVE_LEADER_ONLY}
STANDARD_REASON_STATUSES = WAIT_STATUSES | BLOCK_STATUSES | OBSERVE_STATUSES | {READY, WAIT, BLOCKED, OBSERVE}


def normalize_reason_status(
    *,
    reason_codes: Iterable[Any] | None = None,
    display_state: str = "",
    existing_status: Any = "",
    block_type: str = "",
    can_recover: bool = False,
) -> str:
    existing = str(existing_status or "").strip().upper()
    if existing in STANDARD_REASON_STATUSES:
        return OBSERVE_BREAKOUT if existing in {"OBSERVE_BREAKOUT_CONTINUATION", "OBSERVE_VWAP_OVEREXTENDED"} else existing
    codes = normalize_reason_codes(reason_codes)
    text = " ".join(codes).upper()
    state = str(display_state or "").strip().upper()
    blocked = state == BLOCKED or (str(block_type or "").upper() == "FINAL" and not can_recover)
    observe = state == OBSERVE

    if _has(text, "THEME_WEAK", "WEAK_THEME", "THEME_SYNC_WEAK"):
        return BLOCK_THEME
    if _has(text, "LATE_LAGGARD"):
        return BLOCK_LATE_LAGGARD
    if _has(text, "CHASE_HIGH", "HIGH_CHASE", "CHASE_RISK", "LATE_CHASE", "VWAP_OVEREXTENDED"):
        return BLOCK_CHASE if blocked else OBSERVE_CHASE
    if _has(text, "BREAKOUT_CONTINUATION"):
        return OBSERVE_BREAKOUT
    if _has(text, "DATA_INSUFFICIENT", "INPUT_MISSING", "DATA_QUALITY_BLOCK", "FILL_INPUT_INSUFFICIENT"):
        return BLOCK_DATA if blocked else WAIT_DATA
    if _has(text, "LOW_BREADTH", "BREADTH_SCOPE_LIMITED", "MARKET_BREADTH_WEAK"):
        return WAIT_BREADTH if not blocked else BLOCK_RISK
    if _has(text, "INDEX_WEAK", "MARKET_WAIT", "MARKET_INDEX_TEMPORARY_CAP", "INDEX_SLOPE_WEAK", "RISK_OFF"):
        return WAIT_MARKET if not blocked else BLOCK_RISK
    if _has(text, "WAIT_PULLBACK", "PULLBACK", "SUPPORT_TOUCHED", "PRICE_LOCATION_UNKNOWN", "DEEP_PULLBACK"):
        return WAIT_PULLBACK
    if _has(text, "LEADERSHIP_WEAK", "LEADER_CONFIRM", "THEME_LEADER_COLLAPSE", "LEADER_ONLY_THEME_LAGGARD_BLOCK"):
        return WAIT_LEADER_CONFIRMATION if not blocked else BLOCK_RISK
    if _has(text, "THEME_STRENGTH_C", "THEME_CONFIRM", "WATCH_THEME", "LEADER_ONLY_THEME"):
        return WAIT_THEME_CONFIRMATION if not observe else OBSERVE_LEADER_ONLY
    if _has(text, "READY_SMALL"):
        return OBSERVE_READY_SMALL
    if _has(text, "FILL_LIQUIDITY_WEAK", "SPREAD_TOO_WIDE", "LOW_TURNOVER", "LIQUIDITY"):
        return BLOCK_LIQUIDITY if blocked else WAIT_DATA
    if blocked:
        return BLOCK_RISK
    if state == WAIT:
        return WAIT
    if observe:
        return OBSERVE
    if state == READY:
        return READY
    return state or OBSERVE


def reason_status_family(status: str) -> str:
    status = str(status or "").upper()
    if status in WAIT_STATUSES or status == WAIT:
        return WAIT
    if status in BLOCK_STATUSES or status == BLOCKED:
        return BLOCKED
    if status in OBSERVE_STATUSES or status == OBSERVE:
        return OBSERVE
    if status == READY:
        return READY
    return status or OBSERVE


def reason_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter()
    by_family = Counter()
    by_reason_code = Counter()
    for row in rows:
        status = str(row.get("reason_status") or row.get("sub_status") or "")
        if not status:
            status = normalize_reason_status(
                reason_codes=row.get("reason_codes") or [],
                display_state=str(row.get("display_state") or row.get("state") or ""),
                existing_status=row.get("sub_status") or "",
                block_type=str(row.get("block_type") or ""),
                can_recover=bool(row.get("can_recover")),
            )
        by_status[status] += 1
        by_family[reason_status_family(status)] += 1
        by_reason_code.update(normalize_reason_codes(row.get("reason_codes") or []))
    return {
        "by_status": [{"status": key, "count": count} for key, count in by_status.most_common()],
        "by_family": [{"family": key, "count": count} for key, count in by_family.most_common()],
        "by_reason_code": [{"reason": key, "count": count} for key, count in by_reason_code.most_common(20)],
    }


def _has(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)
