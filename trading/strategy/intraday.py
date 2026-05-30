from __future__ import annotations

from trading.rules import tick_size
from trading.strategy.candles import Candle
from trading.strategy.market_data import StrategyTick
from trading.strategy.models import IndicatorSnapshot
from trading.strategy.runtime_settings import StrategyRuntimeSettings, legacy_strategy_runtime_settings


VOLUME_REACCEL_RATIO = 1.2
VOLUME_DECELERATION_RATIO = 0.8
CHASE_RISK_WITHIN_HIGH_PCT = 0.5
LOW_BREAK_LOOKBACK = 3
LARGE_CANDLE_BODY_PCT = 2.0
LARGE_CANDLE_CLOSE_POSITION = 0.75


class IntradayStateTracker:
    def __init__(self, settings: StrategyRuntimeSettings | None = None) -> None:
        self.settings = settings or legacy_strategy_runtime_settings()

    def apply(self, snapshot: IndicatorSnapshot, candles: list[Candle], latest_tick: StrategyTick) -> IndicatorSnapshot:
        metadata = dict(snapshot.metadata or {})
        metadata.setdefault("pullback_phase", "unknown")

        snapshot.volume_reaccel = self._volume_reaccel(candles, metadata)
        metadata["volume_deceleration"] = self._volume_deceleration(candles, metadata)
        snapshot.failed_low_break_rebound = self._failed_low_break_rebound(candles, latest_tick, metadata)
        snapshot.chase_risk = self._chase_risk(snapshot, latest_tick, metadata)
        self._large_candle_state(candles, metadata)
        self._recent_momentum(candles, metadata)
        snapshot.metadata = metadata
        return snapshot

    def _volume_reaccel(self, candles: list[Candle], metadata: dict) -> bool:
        if len(candles) < 3:
            metadata["volume_reaccel_ready"] = False
            _append_insufficient(metadata, "volume_reaccel_history_short")
            return False
        recent = candles[-1].volume
        prior_avg = (candles[-2].volume + candles[-3].volume) / 2.0
        threshold = self.settings.number("pullback_thresholds.volume_reaccel_ratio", VOLUME_REACCEL_RATIO)
        metadata["volume_reaccel_ready"] = True
        metadata["volume_reaccel_ratio_threshold"] = threshold
        metadata["volume_reaccel_prior_avg"] = prior_avg
        if prior_avg <= 0:
            return False
        return recent >= prior_avg * threshold

    def _volume_deceleration(self, candles: list[Candle], metadata: dict) -> bool:
        if len(candles) < 3:
            metadata["volume_deceleration_ready"] = False
            _append_insufficient(metadata, "volume_deceleration_history_short")
            return False
        recent = candles[-1].volume
        prior_avg = (candles[-2].volume + candles[-3].volume) / 2.0
        threshold = self.settings.number("pullback_thresholds.volume_deceleration_ratio", VOLUME_DECELERATION_RATIO)
        metadata["volume_deceleration_ready"] = True
        metadata["volume_deceleration_ratio_threshold"] = threshold
        metadata["volume_deceleration_recent"] = recent
        metadata["volume_deceleration_prior_avg"] = prior_avg
        if prior_avg <= 0:
            return False
        ratio = recent / prior_avg
        metadata["volume_deceleration_ratio"] = ratio
        return ratio <= threshold

    def _failed_low_break_rebound(self, candles: list[Candle], latest_tick: StrategyTick, metadata: dict) -> bool:
        lookback = self.settings.integer("pullback_thresholds.low_break_lookback", LOW_BREAK_LOOKBACK)
        if len(candles) < lookback + 1:
            metadata["failed_low_break_rebound_ready"] = False
            _append_insufficient(metadata, "failed_low_break_history_short")
            return False

        recent = candles[-1]
        prior = candles[-(lookback + 1) : -1]
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
        threshold = self.settings.number("pullback_thresholds.chase_risk_within_high_pct", CHASE_RISK_WITHIN_HIGH_PCT)
        metadata["chase_risk_within_high_pct"] = threshold
        if snapshot.day_high <= 0 or snapshot.pullback_pct is None:
            _append_insufficient(metadata, "chase_risk_day_high_missing")
            return False
        within_high_pct = ((snapshot.day_high - latest_tick.price) / snapshot.day_high) * 100.0
        metadata["chase_risk_current_within_high_pct"] = within_high_pct
        return 0 <= within_high_pct <= threshold and snapshot.pullback_pct > -threshold

    def _large_candle_state(self, candles: list[Candle], metadata: dict) -> None:
        body_pct = self.settings.number("pullback_thresholds.large_candle_body_pct", LARGE_CANDLE_BODY_PCT)
        close_position = self.settings.number("pullback_thresholds.large_candle_close_position", LARGE_CANDLE_CLOSE_POSITION)
        large_3m = _large_window_candle(candles, 3, body_pct, close_position)
        large_5m = _large_window_candle(candles, 5, body_pct, close_position)
        if large_3m is None:
            _append_insufficient(metadata, "large_3m_candle_history_short")
        if large_5m is None:
            _append_insufficient(metadata, "large_5m_candle_history_short")
        metadata["after_large_3m_candle"] = bool(large_3m and large_3m["after_large_candle"])
        metadata["after_large_5m_candle"] = bool(large_5m and large_5m["after_large_candle"])
        metadata["large_3m_candle_body_pct"] = large_3m["body_pct"] if large_3m else None
        metadata["large_5m_candle_body_pct"] = large_5m["body_pct"] if large_5m else None
        metadata["large_3m_candle_close_position"] = large_3m["close_position"] if large_3m else None
        metadata["large_5m_candle_close_position"] = large_5m["close_position"] if large_5m else None
        metadata["large_candle_body_pct"] = max(
            [value for value in [metadata["large_3m_candle_body_pct"], metadata["large_5m_candle_body_pct"]] if value is not None],
            default=None,
        )
        metadata["candle_close_position"] = max(
            [value for value in [metadata["large_3m_candle_close_position"], metadata["large_5m_candle_close_position"]] if value is not None],
            default=None,
        )

    def _recent_momentum(self, candles: list[Candle], metadata: dict) -> None:
        metadata["return_pct_5m"] = _window_return_pct(candles, 5)
        metadata["return_pct_20m"] = _window_return_pct(candles, 20)
        if not candles:
            _append_insufficient(metadata, "momentum_history_missing")
            metadata["first_breakout_at"] = ""
            metadata["last_high_update_at"] = ""
            return
        running_high = candles[0].high
        first_breakout_at = ""
        last_high_update_at = candles[0].start_at.isoformat()
        for candle in candles[1:]:
            if candle.high > running_high:
                running_high = candle.high
                last_high_update_at = candle.start_at.isoformat()
                if not first_breakout_at and candle.close >= candle.open:
                    first_breakout_at = candle.start_at.isoformat()
        if not first_breakout_at:
            rising = next((candle for candle in candles if candle.close > candle.open), None)
            first_breakout_at = rising.start_at.isoformat() if rising else ""
        metadata["first_breakout_at"] = first_breakout_at
        metadata["last_high_update_at"] = last_high_update_at


def _append_insufficient(metadata: dict, reason: str) -> None:
    reasons = metadata.setdefault("insufficient_reason", [])
    if isinstance(reasons, list) and reason not in reasons:
        reasons.append(reason)


def _window_return_pct(candles: list[Candle], minutes: int):
    if len(candles) < minutes:
        return None
    window = candles[-minutes:]
    first = window[0].open
    if first <= 0:
        return None
    return round(((window[-1].close - first) / first) * 100.0, 6)


def _large_window_candle(
    candles: list[Candle],
    minutes: int,
    body_threshold_pct: float = LARGE_CANDLE_BODY_PCT,
    close_position_threshold: float = LARGE_CANDLE_CLOSE_POSITION,
):
    if len(candles) < minutes:
        return None
    window = candles[-minutes:]
    open_price = window[0].open
    close_price = window[-1].close
    high = max(candle.high for candle in window)
    low = min(candle.low for candle in window)
    if open_price <= 0:
        return None
    body_pct = abs(close_price - open_price) / open_price * 100.0
    close_position = 1.0 if high <= low else (close_price - low) / (high - low)
    bullish = close_price > open_price
    return {
        "body_pct": round(body_pct, 6),
        "close_position": round(close_position, 6),
        "after_large_candle": bool(
            bullish
            and body_pct >= body_threshold_pct
            and close_position >= close_position_threshold
        ),
    }
