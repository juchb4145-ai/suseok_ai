from __future__ import annotations

from trading.rules import tick_size
from trading.strategy.candles import Candle
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import IndicatorSnapshot


VOLUME_REACCEL_RATIO = 1.2
CHASE_RISK_WITHIN_HIGH_PCT = 0.5
LOW_BREAK_LOOKBACK = 3


class IntradayStateTracker:
    def apply(self, snapshot: IndicatorSnapshot, candles: list[Candle], latest_tick: StrategyTick) -> IndicatorSnapshot:
        metadata = dict(snapshot.metadata or {})
        metadata.setdefault("pullback_phase", "unknown")

        snapshot.volume_reaccel = self._volume_reaccel(candles, metadata)
        snapshot.failed_low_break_rebound = self._failed_low_break_rebound(candles, latest_tick, metadata)
        snapshot.chase_risk = self._chase_risk(snapshot, latest_tick, metadata)
        snapshot.metadata = metadata
        return snapshot

    def _volume_reaccel(self, candles: list[Candle], metadata: dict) -> bool:
        if len(candles) < 3:
            metadata["volume_reaccel_ready"] = False
            _append_insufficient(metadata, "volume_reaccel_history_short")
            return False
        recent = candles[-1].volume
        prior_avg = (candles[-2].volume + candles[-3].volume) / 2.0
        metadata["volume_reaccel_ready"] = True
        metadata["volume_reaccel_ratio_threshold"] = VOLUME_REACCEL_RATIO
        metadata["volume_reaccel_prior_avg"] = prior_avg
        if prior_avg <= 0:
            return False
        return recent >= prior_avg * VOLUME_REACCEL_RATIO

    def _failed_low_break_rebound(self, candles: list[Candle], latest_tick: StrategyTick, metadata: dict) -> bool:
        if len(candles) < LOW_BREAK_LOOKBACK + 1:
            metadata["failed_low_break_rebound_ready"] = False
            _append_insufficient(metadata, "failed_low_break_history_short")
            return False

        recent = candles[-1]
        prior = candles[-(LOW_BREAK_LOOKBACK + 1) : -1]
        prior_low = min(candle.low for candle in prior)
        break_price = prior_low - tick_size(prior_low)
        broke_low = recent.low <= break_price
        recovered = latest_tick.price > prior_low or recent.close > prior_low

        metadata["failed_low_break_rebound_ready"] = True
        metadata["failed_low_break_prior_low"] = prior_low
        metadata["failed_low_break_break_price"] = break_price
        metadata["failed_low_break_broke_low"] = broke_low
        metadata["failed_low_break_recovered"] = recovered
        return bool(broke_low and recovered)

    def _chase_risk(self, snapshot: IndicatorSnapshot, latest_tick: StrategyTick, metadata: dict) -> bool:
        metadata["chase_risk_within_high_pct"] = CHASE_RISK_WITHIN_HIGH_PCT
        if snapshot.day_high <= 0 or snapshot.pullback_pct is None:
            _append_insufficient(metadata, "chase_risk_day_high_missing")
            return False
        within_high_pct = ((snapshot.day_high - latest_tick.price) / snapshot.day_high) * 100.0
        metadata["chase_risk_current_within_high_pct"] = within_high_pct
        return 0 <= within_high_pct <= CHASE_RISK_WITHIN_HIGH_PCT and snapshot.pullback_pct > -CHASE_RISK_WITHIN_HIGH_PCT


def _append_insufficient(metadata: dict, reason: str) -> None:
    reasons = metadata.setdefault("insufficient_reason", [])
    if isinstance(reasons, list) and reason not in reasons:
        reasons.append(reason)
