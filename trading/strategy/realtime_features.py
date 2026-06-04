from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder, minute_start
from trading.rules import tick_size


@dataclass(frozen=True)
class RealtimeFeatureResult:
    trade_value: float
    metadata: dict[str, Any]


@dataclass
class _MinuteTurnover:
    minute: datetime
    delta: float = 0.0


class RealtimeFeatureCalculator:
    def __init__(self, *, turnover_average_window: int = 5, recent_candle_window: int = 5, support_window: int = 3) -> None:
        self.turnover_average_window = max(1, int(turnover_average_window))
        self.recent_candle_window = max(1, int(recent_candle_window))
        self.support_window = max(1, int(support_window))
        self._last_trade_value: dict[str, float] = {}
        self._minute_turnover: dict[str, _MinuteTurnover] = {}
        self._recent_minute_turnovers: dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.turnover_average_window)
        )

    def enrich(
        self,
        *,
        code: str,
        price: int,
        cum_volume: int,
        trade_value: float,
        timestamp: datetime,
        candle_builder: CandleBuilder,
        metadata: dict[str, Any] | None = None,
        change_rate: float = 0.0,
    ) -> RealtimeFeatureResult:
        clean_code = normalize_code(code)
        enriched = dict(metadata or {})
        reason_codes = set(str(value) for value in enriched.get("reason_codes") or [] if str(value or "").strip())

        raw_trade_value = max(0.0, _float(trade_value))
        effective_trade_value = raw_trade_value
        if effective_trade_value <= 0 and price > 0 and cum_volume > 0:
            effective_trade_value = float(price * cum_volume)
            reason_codes.add("TURNOVER_ESTIMATED")

        vwap = self._vwap(raw_trade_value, cum_volume)
        if vwap is not None:
            enriched["vwap"] = vwap
            enriched["vwap_ready"] = True
        price_context = self._recent_price_context(
            clean_code,
            candle_builder,
            current_price=price,
            timestamp=timestamp,
        )
        enriched.update(price_context)
        prev_close = self._prev_close(enriched, price=price, change_rate=change_rate)
        if prev_close is not None:
            enriched.setdefault("prev_close", prev_close)
            upper_limit = self._upper_limit_price(prev_close)
            if upper_limit is not None:
                enriched["upper_limit_price"] = upper_limit
                enriched["upper_limit_gap_pct"] = round(((upper_limit - price) / max(1.0, float(price))) * 100.0, 4)
        breakout_level = self._breakout_level(price_context)
        if breakout_level is not None:
            enriched["breakout_level"] = breakout_level

        momentums, warmup = self._momentums(clean_code, candle_builder)
        enriched.update(momentums)
        if warmup:
            reason_codes.add("MOMENTUM_WARMUP")

        enriched["turnover_strength"] = self._turnover_strength(
            clean_code,
            effective_trade_value,
            timestamp,
        )
        enriched["reason_codes"] = sorted(reason_codes)
        return RealtimeFeatureResult(trade_value=effective_trade_value, metadata=enriched)

    def _momentums(self, code: str, candle_builder: CandleBuilder) -> tuple[dict[str, float], bool]:
        values = {
            "momentum_1m": self._interval_momentum(code, candle_builder, 1),
            "momentum_3m": self._interval_momentum(code, candle_builder, 3),
            "momentum_5m": self._interval_momentum(code, candle_builder, 5),
        }
        warmup = any(value is None for value in values.values())
        return {key: round(float(value or 0.0), 4) for key, value in values.items()}, warmup

    @staticmethod
    def _interval_momentum(code: str, candle_builder: CandleBuilder, interval_min: int) -> float | None:
        candles = candle_builder.completed_candles(code, interval_min)
        if not candles:
            return None
        candle = candles[-1]
        if candle.open <= 0:
            return 0.0
        return ((candle.close - candle.open) / candle.open) * 100.0

    @staticmethod
    def _vwap(trade_value: float, cum_volume: int) -> float | None:
        volume = max(0, int(cum_volume or 0))
        if trade_value <= 0 or volume <= 0:
            return None
        return round(float(trade_value) / volume, 4)

    def _recent_price_context(
        self,
        code: str,
        candle_builder: CandleBuilder,
        *,
        current_price: int,
        timestamp: datetime,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        completed_1m = candle_builder.completed_candles(code, 1)
        candles_1m = completed_1m[-self.recent_candle_window :]
        candles_3m = candle_builder.completed_candles(code, 3)[-self.recent_candle_window :]
        active_1m = candle_builder.active_candle(code, 1)
        current_start = minute_start(timestamp)
        logical_completed_1m = list(candles_1m)
        display_payloads = [_candle_payload(candle, completed=True) for candle in candles_1m]
        active_payload: dict[str, Any] | None = None
        if active_1m is not None and active_1m.start_at < current_start:
            if not logical_completed_1m or logical_completed_1m[-1].start_at != active_1m.start_at:
                logical_completed_1m.append(active_1m)
                display_payloads.append(_candle_payload(active_1m, completed=True))
            active_payload = _tick_candle_payload(current_start, current_price)
        elif active_1m is not None:
            active_payload = _active_candle_payload(active_1m, current_price=current_price)
        elif current_price > 0:
            active_payload = _tick_candle_payload(current_start, current_price)
        if active_payload:
            if not display_payloads or display_payloads[-1].get("start_at") != active_payload.get("start_at"):
                display_payloads.append(active_payload)
            else:
                display_payloads[-1] = active_payload
        if display_payloads:
            metadata["recent_candles_1m"] = display_payloads[-self.recent_candle_window :]
        valid_support_candles = [candle for candle in logical_completed_1m if candle.low > 0]
        if valid_support_candles:
            support_candles = valid_support_candles[-self.support_window :]
            metadata["recent_support_price"] = min(float(candle.low) for candle in support_candles)
            metadata["recent_support_candle_count"] = len(support_candles)
            metadata["recent_support_ready"] = len(support_candles) >= self.support_window
            metadata["recent_support_source"] = "completed_1m_low"
        elif active_payload:
            metadata["recent_support_price"] = float(active_payload["low"])
            metadata["recent_support_candle_count"] = 0
            metadata["recent_support_ready"] = False
            metadata["recent_support_source"] = "active_1m_low_provisional"
        if candles_3m:
            metadata["recent_candles_3m"] = [_candle_payload(candle, completed=True) for candle in candles_3m]
            metadata["recent_3m_bar_count"] = len(candles_3m)
        metadata["completed_minute_bar_count"] = len(logical_completed_1m)
        metadata["minute_bar_present"] = bool(display_payloads)
        return metadata

    @staticmethod
    def _prev_close(metadata: dict[str, Any], *, price: int, change_rate: float) -> float | None:
        for key in ("prev_close", "previous_close", "yesterday_close"):
            value = _float(metadata.get(key))
            if value > 0:
                return value
        rate = _float(change_rate)
        if price > 0 and rate != -100.0:
            inferred = float(price) / (1.0 + (rate / 100.0))
            if inferred > 0:
                metadata["prev_close_inferred_from_change_rate"] = True
                return round(inferred, 4)
        return None

    @staticmethod
    def _upper_limit_price(prev_close: float) -> int | None:
        if prev_close <= 0:
            return None
        raw = int(prev_close * 1.3)
        unit = max(1, tick_size(raw))
        return (raw // unit) * unit

    @staticmethod
    def _breakout_level(metadata: dict[str, Any]) -> float | None:
        completed = [
            candle
            for candle in metadata.get("recent_candles_1m") or []
            if candle.get("completed", True)
        ]
        if not completed:
            return None
        highs = [_float(candle.get("high")) for candle in completed]
        highs = [value for value in highs if value > 0]
        return max(highs) if highs else None

    def _turnover_strength(self, code: str, trade_value: float, timestamp: datetime) -> float:
        if trade_value <= 0:
            return 1.0

        current_minute = minute_start(timestamp)
        state = self._minute_turnover.get(code)
        if state is None:
            state = _MinuteTurnover(minute=current_minute)
            self._minute_turnover[code] = state
        elif state.minute < current_minute:
            if state.delta > 0:
                self._recent_minute_turnovers[code].append(state.delta)
            state = _MinuteTurnover(minute=current_minute)
            self._minute_turnover[code] = state

        previous_trade_value = self._last_trade_value.get(code)
        self._last_trade_value[code] = trade_value
        if previous_trade_value is not None and trade_value >= previous_trade_value:
            state.delta += trade_value - previous_trade_value

        recent = [value for value in self._recent_minute_turnovers.get(code, []) if value > 0]
        if not recent:
            return 1.0
        average = sum(recent) / len(recent)
        if average <= 0:
            return 1.0
        return round(max(0.0, state.delta / average), 4)


def _candle_payload(candle, *, completed: bool) -> dict[str, Any]:
    return {
        "start_at": candle.start_at.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "completed": completed,
    }


def _active_candle_payload(candle, *, current_price: int) -> dict[str, Any]:
    price = current_price if current_price > 0 else candle.close
    return {
        "start_at": candle.start_at.isoformat(),
        "open": candle.open,
        "high": max(candle.high, price),
        "low": min(candle.low, price),
        "close": price,
        "volume": candle.volume,
        "completed": False,
    }


def _tick_candle_payload(start_at: datetime, current_price: int) -> dict[str, Any] | None:
    if current_price <= 0:
        return None
    return {
        "start_at": start_at.isoformat(),
        "open": current_price,
        "high": current_price,
        "low": current_price,
        "close": current_price,
        "volume": 0,
        "completed": False,
    }


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0
