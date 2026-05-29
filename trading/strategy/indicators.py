from __future__ import annotations

from datetime import datetime
from typing import Optional

from trading.strategy.candidates import normalize_code
from trading.strategy.candles import Candle, CandleBuilder
from trading.strategy.market_data import MarketDataStore
from trading.strategy.models import IndicatorSnapshot


class PreviousDayLevelProvider:
    def __init__(self, levels: Optional[dict[str, tuple[int, int]]] = None) -> None:
        self._levels: dict[str, tuple[int, int]] = {}
        for code, value in (levels or {}).items():
            self.set_level(code, value[0], value[1])

    def set_level(self, code: str, prev_high: int, prev_low: int) -> None:
        self._levels[normalize_code(code)] = (max(0, int(prev_high)), max(0, int(prev_low)))

    def get(self, code: str) -> tuple[int, int]:
        return self._levels.get(normalize_code(code), (0, 0))


class IndicatorCalculator:
    def __init__(
        self,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        previous_day_levels: Optional[PreviousDayLevelProvider] = None,
    ) -> None:
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.previous_day_levels = previous_day_levels or PreviousDayLevelProvider()

    def build_snapshot(self, candidate_id: int, code: str, now: Optional[datetime] = None) -> Optional[IndicatorSnapshot]:
        clean_code = normalize_code(code)
        latest_tick = self.market_data.latest_tick(clean_code)
        if latest_tick is None:
            return None

        created_at = (now or latest_tick.timestamp or datetime.now()).replace(microsecond=0).isoformat()
        day_high, day_low = self.market_data.day_high_low(clean_code)
        prev_high, prev_low = self.previous_day_levels.get(clean_code)
        metadata: dict[str, object] = {
            "insufficient_reason": [],
            "vwap_price_basis": "close",
            "pullback_pct_basis": "negative_below_day_high",
        }
        insufficient = metadata["insufficient_reason"]
        assert isinstance(insufficient, list)

        if day_high <= 0:
            insufficient.append("day_high_missing")
        if day_low <= 0:
            insufficient.append("day_low_missing")
        if prev_high <= 0:
            insufficient.append("prev_high_missing")
        if prev_low <= 0:
            insufficient.append("prev_low_missing")

        vwap = self._calculate_vwap(clean_code, metadata)
        ema20_5m = self._calculate_ema20_5m(clean_code, metadata)
        day_mid = ((day_high + day_low) / 2.0) if day_high > 0 and day_low > 0 else None
        pullback_pct = None
        if day_high > 0:
            pullback_pct = ((latest_tick.price - day_high) / day_high) * 100.0

        return IndicatorSnapshot(
            candidate_id=candidate_id,
            code=clean_code,
            created_at=created_at,
            price=latest_tick.price,
            vwap=vwap,
            ema20_5m=ema20_5m,
            base_line_120=None,
            envelope_mid=None,
            day_high=day_high,
            day_low=day_low,
            day_mid=day_mid,
            prev_high=prev_high,
            prev_low=prev_low,
            pullback_pct=pullback_pct,
            metadata=metadata,
        )

    def _calculate_vwap(self, code: str, metadata: dict[str, object]) -> Optional[float]:
        candles = self._vwap_candles(code)
        total_volume = sum(candle.volume for candle in candles if candle.volume > 0)
        active = self.candle_builder.active_candle(code, 1)
        includes_active = active is not None and active.volume > 0 and any(candle == active for candle in candles)
        metadata["includes_active_candle"] = includes_active
        if total_volume <= 0:
            metadata["vwap_ready"] = False
            _append_insufficient(metadata, "vwap_volume_missing")
            return None
        weighted_price = sum(candle.close * candle.volume for candle in candles if candle.volume > 0)
        metadata["vwap_ready"] = True
        return weighted_price / total_volume

    def _vwap_candles(self, code: str) -> list[Candle]:
        completed = self.candle_builder.completed_candles(code, 1)
        result = list(completed)
        completed_starts = {candle.start_at for candle in completed}
        active = self.candle_builder.active_candle(code, 1)
        if active is not None and active.start_at not in completed_starts:
            result.append(active)
        return result

    def _calculate_ema20_5m(self, code: str, metadata: dict[str, object]) -> Optional[float]:
        candles = self.candle_builder.completed_candles(code, 5)
        metadata["ema20_5m_candle_count"] = len(candles)
        metadata["ema20_5m_ready"] = len(candles) >= 20
        if not candles:
            _append_insufficient(metadata, "ema20_5m_missing")
            return None
        closes = [float(candle.close) for candle in candles]
        return _ema(closes, 20)


def _ema(values: list[float], period: int) -> float:
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value * alpha) + (ema * (1 - alpha))
    return ema


def _append_insufficient(metadata: dict[str, object], reason: str) -> None:
    reasons = metadata.setdefault("insufficient_reason", [])
    if isinstance(reasons, list) and reason not in reasons:
        reasons.append(reason)
