from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.market_data import StrategyTick


@dataclass(frozen=True)
class Candle:
    code: str
    interval_min: int
    start_at: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int = 0

    def with_tick(self, tick: StrategyTick, volume_delta: int) -> "Candle":
        return Candle(
            code=self.code,
            interval_min=self.interval_min,
            start_at=self.start_at,
            open=self.open,
            high=max(self.high, tick.price),
            low=min(self.low, tick.price),
            close=tick.price,
            volume=self.volume + max(0, volume_delta),
        )


class CandleBuilder:
    def __init__(self) -> None:
        self._active_1m: dict[str, Candle] = {}
        self._completed: dict[tuple[str, int], list[Candle]] = {}
        self._last_tick_timestamp: dict[str, datetime] = {}
        self._last_cum_volume: dict[str, int] = {}
        self._completed_1m_keys: set[tuple[str, datetime]] = set()

    def update(self, tick: StrategyTick) -> bool:
        last_timestamp = self._last_tick_timestamp.get(tick.code)
        if last_timestamp is not None and tick.timestamp < last_timestamp:
            return False

        volume_delta = self._volume_delta(tick)
        start_at = minute_start(tick.timestamp)
        active = self._active_1m.get(tick.code)

        if active is None:
            self._active_1m[tick.code] = self._new_candle(tick, start_at, 0)
        elif active.start_at == start_at:
            self._active_1m[tick.code] = active.with_tick(tick, volume_delta)
        elif active.start_at < start_at:
            self._complete_1m(active)
            self._active_1m[tick.code] = self._new_candle(tick, start_at, volume_delta)
        else:
            return False

        self._last_tick_timestamp[tick.code] = tick.timestamp
        return True

    def flush(self, code: str, now: datetime) -> Optional[Candle]:
        clean_code = normalize_code(code)
        active = self._active_1m.get(clean_code)
        if active is None:
            return None
        if minute_start(now) <= active.start_at:
            return None
        self._active_1m.pop(clean_code, None)
        self._complete_1m(active)
        return active

    def active_candle(self, code: str, interval_min: int = 1) -> Optional[Candle]:
        if interval_min != 1:
            return None
        return self._active_1m.get(normalize_code(code))

    def completed_candles(self, code: str, interval_min: int) -> list[Candle]:
        return list(self._completed.get((normalize_code(code), interval_min), []))

    def _volume_delta(self, tick: StrategyTick) -> int:
        previous = self._last_cum_volume.get(tick.code)
        self._last_cum_volume[tick.code] = tick.cum_volume
        if previous is None:
            return 0
        if tick.cum_volume < previous:
            return 0
        return tick.cum_volume - previous

    @staticmethod
    def _new_candle(tick: StrategyTick, start_at: datetime, volume_delta: int) -> Candle:
        return Candle(
            code=tick.code,
            interval_min=1,
            start_at=start_at,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=max(0, volume_delta),
        )

    def _complete_1m(self, candle: Candle) -> None:
        key = (candle.code, candle.start_at)
        if key in self._completed_1m_keys:
            return
        self._completed_1m_keys.add(key)
        self._append_completed(candle)
        self._aggregate_completed(candle.code, 3)
        self._aggregate_completed(candle.code, 5)

    def _append_completed(self, candle: Candle) -> None:
        self._completed.setdefault((candle.code, candle.interval_min), []).append(candle)

    def _aggregate_completed(self, code: str, interval_min: int) -> None:
        one_minute = self._completed.get((code, 1), [])
        if not one_minute:
            return
        bucket_start = interval_start(one_minute[-1].start_at, interval_min)
        bucket = [candle for candle in one_minute if bucket_start <= candle.start_at and candle.start_at < add_minutes(bucket_start, interval_min)]
        if len(bucket) != interval_min:
            return
        completed_key = (code, interval_min)
        if self._completed.get(completed_key) and self._completed[completed_key][-1].start_at == bucket_start:
            return
        self._append_completed(
            Candle(
                code=code,
                interval_min=interval_min,
                start_at=bucket_start,
                open=bucket[0].open,
                high=max(candle.high for candle in bucket),
                low=min(candle.low for candle in bucket),
                close=bucket[-1].close,
                volume=sum(candle.volume for candle in bucket),
            )
        )


def minute_start(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def interval_start(value: datetime, interval_min: int) -> datetime:
    start = minute_start(value)
    return start.replace(minute=start.minute - (start.minute % interval_min))


def add_minutes(value: datetime, minutes: int) -> datetime:
    return value + timedelta(minutes=minutes)
