from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Optional

from trading.strategy.candidates import normalize_code


@dataclass(frozen=True)
class StrategyTick:
    code: str
    price: int
    change_rate: float = 0.0
    cum_volume: int = 0
    best_ask: int = 0
    best_bid: int = 0
    trade_value: float = 0.0
    execution_strength: float = 0.0
    spread_ticks: int = 0
    timestamp: datetime = datetime.min
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_realtime(
        cls,
        code: str,
        price,
        change_rate=0.0,
        cum_volume=0,
        best_ask=0,
        best_bid=0,
        trade_value=0,
        execution_strength=0,
        spread_ticks=0,
        timestamp: Optional[datetime] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> "StrategyTick":
        return cls(
            code=normalize_code(code),
            price=_clean_abs_int(price),
            change_rate=_clean_float(change_rate),
            cum_volume=_clean_abs_int(cum_volume),
            best_ask=_clean_abs_int(best_ask),
            best_bid=_clean_abs_int(best_bid),
            trade_value=max(0.0, _clean_float(trade_value)),
            execution_strength=max(0.0, _clean_float(execution_strength)),
            spread_ticks=_clean_abs_int(spread_ticks),
            timestamp=(timestamp or datetime.now()).replace(microsecond=0),
            metadata=dict(metadata or {}),
        )


class MarketDataStore:
    def __init__(self) -> None:
        self._latest_ticks: dict[str, StrategyTick] = {}
        self._last_timestamps: dict[str, datetime] = {}
        self._day_highs: dict[str, int] = {}
        self._day_lows: dict[str, int] = {}
        self._tick_counts: dict[str, int] = {}

    def update_tick(self, tick: StrategyTick) -> bool:
        last_timestamp = self._last_timestamps.get(tick.code)
        if last_timestamp is not None and tick.timestamp < last_timestamp:
            return False
        previous = self._latest_ticks.get(tick.code)
        if previous is not None and tick.price <= 0 < previous.price:
            tick = _merge_missing_price_tick(previous, tick)
        self._latest_ticks[tick.code] = tick
        self._last_timestamps[tick.code] = tick.timestamp
        self._tick_counts[tick.code] = self._tick_counts.get(tick.code, 0) + 1
        if tick.price > 0:
            self._day_highs[tick.code] = max(self._day_highs.get(tick.code, tick.price), tick.price)
            existing_low = self._day_lows.get(tick.code)
            self._day_lows[tick.code] = tick.price if existing_low is None else min(existing_low, tick.price)
        return True

    def latest_tick(self, code: str) -> Optional[StrategyTick]:
        return self._latest_ticks.get(normalize_code(code))

    def has_recent_tick(self, code: str, now: datetime, max_age_sec: int) -> bool:
        tick = self.latest_tick(code)
        if tick is None:
            return False
        return (now - tick.timestamp).total_seconds() <= max_age_sec

    def day_high_low(self, code: str) -> tuple[int, int]:
        clean_code = normalize_code(code)
        return self._day_highs.get(clean_code, 0), self._day_lows.get(clean_code, 0)

    def tick_count(self, code: str) -> int:
        return self._tick_counts.get(normalize_code(code), 0)


def _merge_missing_price_tick(previous: StrategyTick, tick: StrategyTick) -> StrategyTick:
    metadata = dict(previous.metadata or {})
    metadata.update(dict(tick.metadata or {}))
    metadata["merged_from_previous_price_tick"] = True
    return replace(
        tick,
        price=previous.price,
        change_rate=tick.change_rate if tick.change_rate else previous.change_rate,
        cum_volume=tick.cum_volume if tick.cum_volume else previous.cum_volume,
        best_ask=tick.best_ask if tick.best_ask else previous.best_ask,
        best_bid=tick.best_bid if tick.best_bid else previous.best_bid,
        trade_value=tick.trade_value if tick.trade_value else previous.trade_value,
        execution_strength=tick.execution_strength if tick.execution_strength else previous.execution_strength,
        spread_ticks=tick.spread_ticks if tick.spread_ticks else previous.spread_ticks,
        metadata=metadata,
    )


def _clean_abs_int(value) -> int:
    if value is None:
        return 0
    raw = str(value).strip().replace(",", "").replace("+", "")
    if not raw:
        return 0
    try:
        return abs(int(float(raw)))
    except (TypeError, ValueError):
        return 0


def _clean_float(value) -> float:
    if value is None:
        return 0.0
    raw = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
