from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import StrategyTick, _clean_abs_int, _clean_float


DEFAULT_INDEX_CODE_MAP = {
    "001": ("KOSPI", "KOSPI"),
    "101": ("KOSDAQ", "KOSDAQ"),
    "KOSPI": ("KOSPI", "KOSPI"),
    "KOSDAQ": ("KOSDAQ", "KOSDAQ"),
}


class IndexCodeMapper:
    """Maps broker-specific index codes to internal logical index codes."""

    def __init__(self, mapping: Optional[dict[str, tuple[str, str] | str]] = None) -> None:
        self._mapping: dict[str, tuple[str, str]] = {}
        for raw_code, value in (mapping or DEFAULT_INDEX_CODE_MAP).items():
            if isinstance(value, tuple):
                logical_code, name = value
            else:
                logical_code, name = value, value
            self._mapping[self._clean(raw_code)] = (self._clean(logical_code), str(name or logical_code))

    def logical_code(self, raw_code: str) -> Optional[str]:
        resolved = self._mapping.get(self._clean(raw_code))
        return resolved[0] if resolved else None

    def display_name(self, raw_code: str) -> str:
        resolved = self._mapping.get(self._clean(raw_code))
        return resolved[1] if resolved else str(raw_code or "").strip().upper()

    def is_index_code(self, raw_code: str) -> bool:
        return self.logical_code(raw_code) is not None

    @staticmethod
    def _clean(value: str) -> str:
        return str(value or "").strip().upper()


@dataclass(frozen=True)
class IndexTick:
    index_code: str
    name: str
    price: int
    change_rate: float = 0.0
    cum_volume: int = 0
    day_high: int = 0
    day_low: int = 0
    timestamp: datetime = datetime.min

    @classmethod
    def from_realtime(
        cls,
        index_code: str,
        name: str,
        price,
        change_rate=0.0,
        cum_volume=0,
        day_high=0,
        day_low=0,
        timestamp: Optional[datetime] = None,
    ) -> "IndexTick":
        return cls(
            index_code=str(index_code or "").strip().upper(),
            name=str(name or ""),
            price=_clean_abs_int(price),
            change_rate=_clean_float(change_rate),
            cum_volume=_clean_abs_int(cum_volume),
            day_high=_clean_abs_int(day_high),
            day_low=_clean_abs_int(day_low),
            timestamp=(timestamp or datetime.now()).replace(microsecond=0),
        )

    def to_strategy_tick(self) -> StrategyTick:
        return StrategyTick(
            code=_index_storage_code(self.index_code),
            price=self.price,
            change_rate=self.change_rate,
            cum_volume=self.cum_volume,
            timestamp=self.timestamp,
        )


@dataclass
class MarketIndexState:
    index_code: str
    price: int = 0
    change_rate: float = 0.0
    day_high: int = 0
    day_low: int = 0
    day_mid: Optional[float] = None
    direction_5m: str = "UNKNOWN"
    mid_position: str = "UNKNOWN"
    low_break_recent: bool = False
    metadata: dict = field(default_factory=dict)


class MarketIndexStore:
    def __init__(self, candle_builder: Optional[CandleBuilder] = None) -> None:
        from trading.strategy.market_data import MarketDataStore

        self.market_data = MarketDataStore()
        self.candle_builder = candle_builder or CandleBuilder()
        self._names: dict[str, str] = {}
        self._display_codes: dict[str, str] = {}
        self._metadata: dict[str, dict] = {}
        self._low_break_recent: dict[str, bool] = {}

    def update_index_tick(self, tick: IndexTick) -> MarketIndexState:
        code = self._storage_code(tick.index_code)
        strategy_tick = StrategyTick(
            code=code,
            price=tick.price,
            change_rate=tick.change_rate,
            cum_volume=tick.cum_volume,
            timestamp=tick.timestamp,
        )
        self._names[code] = tick.name
        self._display_codes[code] = tick.index_code
        metadata = self._metadata.setdefault(code, {})
        _, previous_low = self.market_data.day_high_low(code)

        updated = self.market_data.update_tick(strategy_tick)
        if not updated:
            return self.state(tick.index_code)
        if tick.day_high > 0:
            self.market_data._day_highs[code] = max(self.market_data._day_highs.get(code, tick.day_high), tick.day_high)
        if tick.day_low > 0:
            existing_low = self.market_data._day_lows.get(code)
            self.market_data._day_lows[code] = tick.day_low if existing_low is None else min(existing_low, tick.day_low)

        self.candle_builder.update(strategy_tick)
        _, current_low = self.market_data.day_high_low(code)
        low_break = previous_low > 0 and current_low > 0 and current_low < previous_low
        self._low_break_recent[code] = low_break
        if low_break:
            metadata["low_break_at"] = strategy_tick.timestamp.isoformat()
            metadata["low_break_count"] = int(metadata.get("low_break_count", 0)) + 1
        else:
            metadata.setdefault("low_break_count", int(metadata.get("low_break_count", 0)))
        return self.state(tick.index_code)

    def state(self, index_code: str) -> MarketIndexState:
        clean_code = self._storage_code(index_code)
        tick = self.market_data.latest_tick(clean_code)
        day_high, day_low = self.market_data.day_high_low(clean_code)
        day_mid = ((day_high + day_low) / 2.0) if day_high > 0 and day_low > 0 else None
        return MarketIndexState(
            index_code=self._display_codes.get(clean_code, str(index_code or "").strip().upper()),
            price=tick.price if tick else 0,
            change_rate=tick.change_rate if tick else 0.0,
            day_high=day_high,
            day_low=day_low,
            day_mid=day_mid,
            direction_5m=self._direction_5m(clean_code),
            mid_position=self._mid_position(tick.price if tick else 0, day_mid),
            low_break_recent=self._low_break_recent.get(clean_code, False),
            metadata=dict(self._metadata.get(clean_code, {})),
        )

    def _direction_5m(self, code: str) -> str:
        candles = self.candle_builder.completed_candles(code, 5)
        if len(candles) < 2:
            return "UNKNOWN"
        previous = candles[-2].close
        current = candles[-1].close
        if current > previous:
            return "UP"
        if current < previous:
            return "DOWN"
        return "FLAT"

    @staticmethod
    def _mid_position(price: int, day_mid: Optional[float]) -> str:
        if price <= 0 or day_mid is None:
            return "UNKNOWN"
        if price > day_mid:
            return "ABOVE_MID"
        if price < day_mid:
            return "BELOW_MID"
        return "AT_MID"

    @staticmethod
    def _storage_code(index_code: str) -> str:
        return _index_storage_code(index_code)


def _index_storage_code(index_code: str) -> str:
    raw = str(index_code or "").strip().upper()
    if raw.isdigit():
        return raw
    if raw.startswith("A") and raw[1:].isdigit():
        raw = raw[1:]
    cleaned = "".join(ch for ch in raw if ch.isalnum())
    return f"IDX{cleaned}"
