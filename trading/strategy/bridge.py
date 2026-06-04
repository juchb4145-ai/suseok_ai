from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from trading.broker.data_quality import RealtimeDataQualityTracker
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_index import IndexCodeMapper, IndexTick, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.realtime_features import RealtimeFeatureCalculator


class StrategyMarketDataBridge:
    def __init__(
        self,
        market_data: MarketDataStore,
        candle_builder: CandleBuilder,
        market_index_store: Optional[MarketIndexStore] = None,
        index_code_mapper: Optional[IndexCodeMapper] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.market_data = market_data
        self.candle_builder = candle_builder
        self.market_index_store = market_index_store
        self.index_code_mapper = index_code_mapper or IndexCodeMapper()
        self.clock = clock or datetime.now
        self.client = None
        self.realtime_features = RealtimeFeatureCalculator()
        self.data_quality = RealtimeDataQualityTracker()

    def attach(self, client) -> None:
        self.client = client
        client.price_received.connect(self.on_realtime_tick)

    def on_realtime_tick(
        self,
        code: str,
        price,
        change_rate=0.0,
        cum_volume=0,
        best_ask=0,
        best_bid=0,
        trade_value=0,
        execution_strength=0,
        spread_ticks=0,
        instrument_type: Optional[str] = None,
        name: str = "",
        day_high=0,
        day_low=0,
        trade_time: str = "",
        open_price=0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        resolved_type = self._instrument_type(code, instrument_type)
        if resolved_type == "index":
            return self._route_index_tick(
                code=code,
                name=name,
                price=price,
                change_rate=change_rate,
                cum_volume=cum_volume,
                day_high=day_high,
                day_low=day_low,
            )
        if resolved_type != "stock":
            return False
        return self._route_stock_tick(
            code=code,
            price=price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            best_ask=best_ask,
            best_bid=best_bid,
            trade_value=trade_value,
            execution_strength=execution_strength,
            spread_ticks=spread_ticks,
            day_high=day_high,
            day_low=day_low,
            trade_time=trade_time,
            open_price=open_price,
            metadata=metadata,
        )

    def _route_stock_tick(
        self,
        *,
        code: str,
        price,
        change_rate=0.0,
        cum_volume=0,
        best_ask=0,
        best_bid=0,
        trade_value=0,
        execution_strength=0,
        spread_ticks=0,
        day_high=0,
        day_low=0,
        trade_time: str = "",
        open_price=0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        timestamp = self.clock()
        tick_metadata = self._stock_metadata(
            metadata=metadata,
            day_high=day_high,
            day_low=day_low,
            trade_time=trade_time,
            open_price=open_price,
            best_ask=best_ask,
            best_bid=best_bid,
        )
        clean_price = _safe_int(price)
        previous_tick = self.market_data.latest_tick(code)
        if clean_price <= 0 and previous_tick is not None and previous_tick.price > 0:
            clean_price = previous_tick.price
            tick_metadata["merged_from_previous_price_tick"] = True
        feature_result = self.realtime_features.enrich(
            code=code,
            price=clean_price,
            cum_volume=_safe_int(cum_volume),
            trade_value=_safe_float(trade_value),
            timestamp=timestamp,
            candle_builder=self.candle_builder,
            metadata=tick_metadata,
            change_rate=_safe_float(change_rate),
        )
        tick = StrategyTick.from_realtime(
            code=code,
            price=clean_price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            best_ask=best_ask,
            best_bid=best_bid,
            trade_value=feature_result.trade_value,
            execution_strength=execution_strength,
            spread_ticks=spread_ticks,
            timestamp=timestamp,
            metadata=feature_result.metadata,
        )
        if not self.market_data.update_tick(tick):
            return False
        self.data_quality.observe_price_tick(_quality_payload(tick))
        return self.candle_builder.update(tick)

    def _route_index_tick(
        self,
        *,
        code: str,
        name: str = "",
        price,
        change_rate=0.0,
        cum_volume=0,
        day_high=0,
        day_low=0,
    ) -> bool:
        if self.market_index_store is None:
            return False
        logical_code = self.index_code_mapper.logical_code(code)
        if logical_code is None:
            return False
        tick = IndexTick.from_realtime(
            index_code=logical_code,
            name=name or self.index_code_mapper.display_name(code),
            price=price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            day_high=day_high,
            day_low=day_low,
            timestamp=self.clock(),
        )
        self.market_index_store.update_index_tick(tick)
        return True

    def _instrument_type(self, code: str, instrument_type: Optional[str]) -> str:
        if instrument_type:
            return str(instrument_type).strip().lower()
        return "index" if self.index_code_mapper.is_index_code(code) else "stock"

    def data_quality_snapshot(self) -> dict[str, Any]:
        return self.data_quality.snapshot()

    @staticmethod
    def _stock_metadata(
        *,
        metadata: Optional[dict[str, Any]],
        day_high,
        day_low,
        trade_time: str,
        open_price,
        best_ask,
        best_bid,
    ) -> dict[str, Any]:
        result = dict(metadata or {})
        if day_high:
            result["session_high"] = day_high
            result["day_high"] = day_high
        if day_low:
            result["session_low"] = day_low
            result["day_low"] = day_low
        if trade_time:
            result["trade_time"] = str(trade_time)
        if open_price:
            result["open_price"] = open_price
        if best_ask and best_bid:
            result.setdefault("spread_price", max(0, _safe_int(best_ask) - _safe_int(best_bid)))
        return result


def _quality_payload(tick: StrategyTick) -> dict[str, Any]:
    return {
        "code": tick.code,
        "price": tick.price,
        "change_rate": tick.change_rate,
        "cum_volume": tick.cum_volume,
        "volume": tick.cum_volume,
        "trade_value": tick.trade_value,
        "execution_strength": tick.execution_strength,
        "best_ask": tick.best_ask,
        "best_bid": tick.best_bid,
        "day_high": tick.metadata.get("day_high") or tick.metadata.get("session_high") or 0,
        "day_low": tick.metadata.get("day_low") or tick.metadata.get("session_low") or 0,
        "metadata": dict(tick.metadata or {}),
    }


def _safe_int(value) -> int:
    text = str(value or "").strip().replace(",", "").replace("+", "")
    if not text:
        return 0
    try:
        return abs(int(float(text)))
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float:
    text = str(value or "").strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0
