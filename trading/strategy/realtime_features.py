from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import CandleBuilder, minute_start


@dataclass(frozen=True)
class RealtimeFeatureResult:
    trade_value: float
    metadata: dict[str, Any]


@dataclass
class _MinuteTurnover:
    minute: datetime
    delta: float = 0.0


class RealtimeFeatureCalculator:
    def __init__(self, *, turnover_average_window: int = 5) -> None:
        self.turnover_average_window = max(1, int(turnover_average_window))
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
    ) -> RealtimeFeatureResult:
        clean_code = normalize_code(code)
        enriched = dict(metadata or {})
        reason_codes = set(str(value) for value in enriched.get("reason_codes") or [] if str(value or "").strip())

        effective_trade_value = max(0.0, _float(trade_value))
        if effective_trade_value <= 0 and price > 0 and cum_volume > 0:
            effective_trade_value = float(price * cum_volume)
            reason_codes.add("TURNOVER_ESTIMATED")

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
