from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class MarketAction(str, Enum):
    ALLOW_NORMAL = "ALLOW_NORMAL"
    ALLOW_REDUCED = "ALLOW_REDUCED"
    WAIT_MARKET = "WAIT_MARKET"
    BLOCK_NEW_ENTRY = "BLOCK_NEW_ENTRY"
    MARKET_CLOSED = "MARKET_CLOSED"
    DATA_WAIT = "DATA_WAIT"


MARKET_ACTION_UNMAPPED = "MARKET_ACTION_UNMAPPED"
MARKET_ACTION_DERIVED_FROM_SIDE_REGIME = "MARKET_ACTION_DERIVED_FROM_SIDE_REGIME"
MARKET_ACTION_NORMALIZED = "MARKET_ACTION_NORMALIZED"


_RAW_ACTION_MAP = {
    "ALLOW_NORMAL": MarketAction.ALLOW_NORMAL.value,
    "ALLOW_REDUCED": MarketAction.ALLOW_REDUCED.value,
    "WAIT": MarketAction.WAIT_MARKET.value,
    "WAIT_MARKET": MarketAction.WAIT_MARKET.value,
    "CHOPPY": MarketAction.WAIT_MARKET.value,
    "MIDDAY_CHOP": MarketAction.WAIT_MARKET.value,
    "BLOCK": MarketAction.BLOCK_NEW_ENTRY.value,
    "BLOCK_NEW_ENTRY": MarketAction.BLOCK_NEW_ENTRY.value,
    "RISK_OFF": MarketAction.BLOCK_NEW_ENTRY.value,
    "MARKET_CLOSED": MarketAction.MARKET_CLOSED.value,
    "CLOSED": MarketAction.MARKET_CLOSED.value,
    "DATA_WAIT": MarketAction.DATA_WAIT.value,
}

_SIDE_REGIME_MAP = {
    "EXPANSION": MarketAction.ALLOW_NORMAL.value,
    "SELECTIVE": MarketAction.ALLOW_REDUCED.value,
    "CHOPPY": MarketAction.WAIT_MARKET.value,
    "MIDDAY_CHOP": MarketAction.WAIT_MARKET.value,
    "WEAK": MarketAction.WAIT_MARKET.value,
    "RISK_OFF": MarketAction.BLOCK_NEW_ENTRY.value,
    "MARKET_CLOSED": MarketAction.MARKET_CLOSED.value,
    "CLOSED": MarketAction.MARKET_CLOSED.value,
    "DATA_WAIT": MarketAction.DATA_WAIT.value,
    "UNKNOWN": MarketAction.DATA_WAIT.value,
    "": MarketAction.DATA_WAIT.value,
}


@dataclass(frozen=True)
class NormalizedMarketAction:
    action: str
    normalized: bool = False
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_market_action(
    raw_action: Any,
    side_market_regime: Any = "",
    global_market_regime: Any = "",
    market_session_status: Any = "",
) -> NormalizedMarketAction:
    raw = _clean(raw_action)
    side = _clean(side_market_regime)
    global_regime = _clean(global_market_regime)
    session = _clean(market_session_status)
    reasons: list[str] = []

    if session in {"MARKET_CLOSED", "CLOSED"} or str(market_session_status or "").lower() == "closed":
        action = MarketAction.MARKET_CLOSED.value
        if raw and raw != action:
            reasons.append(MARKET_ACTION_NORMALIZED)
        return NormalizedMarketAction(action=action, normalized=bool(reasons), reason_codes=tuple(reasons))

    if raw in _RAW_ACTION_MAP and raw not in {"", "UNKNOWN", "UNMAPPED"}:
        action = _RAW_ACTION_MAP[raw]
        if action != raw:
            reasons.append(MARKET_ACTION_NORMALIZED)
        return NormalizedMarketAction(action=action, normalized=bool(reasons), reason_codes=tuple(reasons))

    derived_key = side if side not in {"", "UNKNOWN", "UNMAPPED"} else global_regime
    action = _SIDE_REGIME_MAP.get(derived_key, MarketAction.DATA_WAIT.value)
    if action == MarketAction.DATA_WAIT.value and derived_key not in _SIDE_REGIME_MAP:
        reasons.append(MARKET_ACTION_UNMAPPED)
    elif raw in {"", "UNKNOWN", "UNMAPPED"}:
        reasons.append(MARKET_ACTION_DERIVED_FROM_SIDE_REGIME)
        if action == MarketAction.DATA_WAIT.value:
            reasons.append(MARKET_ACTION_UNMAPPED)
    else:
        reasons.append(MARKET_ACTION_NORMALIZED)
        if action == MarketAction.DATA_WAIT.value:
            reasons.append(MARKET_ACTION_UNMAPPED)
    return NormalizedMarketAction(action=action, normalized=bool(reasons), reason_codes=tuple(_dedupe(reasons)))


def _clean(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip().upper()
    if "." in text:
        prefix, suffix = text.rsplit(".", 1)
        if prefix in {
            "CANDIDATEMARKETACTION",
            "MARKETACTION",
            "MARKETREGIMESTATUS",
            "MARKETSIDE",
            "COMPOSITEMARKETMODE",
        }:
            return suffix
    return text


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
