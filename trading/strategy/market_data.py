from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from trading.strategy.candidates import normalize_code


@dataclass(frozen=True)
class StrategyTick:
    code: str
    price: int
    change_rate: float = 0.0
    cum_volume: int = 0
    best_ask: int = 0
    best_bid: int = 0
    timestamp: datetime = datetime.min

    @classmethod
    def from_realtime(
        cls,
        code: str,
        price,
        change_rate=0.0,
        cum_volume=0,
        best_ask=0,
        best_bid=0,
        timestamp: Optional[datetime] = None,
    ) -> "StrategyTick":
        return cls(
            code=normalize_code(code),
            price=_clean_abs_int(price),
            change_rate=_clean_float(change_rate),
            cum_volume=_clean_abs_int(cum_volume),
            best_ask=_clean_abs_int(best_ask),
            best_bid=_clean_abs_int(best_bid),
            timestamp=(timestamp or datetime.now()).replace(microsecond=0),
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
