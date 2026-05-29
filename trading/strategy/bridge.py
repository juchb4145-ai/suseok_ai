from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_index import IndexCodeMapper, IndexTick, MarketIndexStore
from trading.strategy.market_data import MarketDataStore, StrategyTick


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
        instrument_type: Optional[str] = None,
        name: str = "",
        day_high=0,
        day_low=0,
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
    ) -> bool:
        tick = StrategyTick.from_realtime(
            code=code,
            price=price,
            change_rate=change_rate,
            cum_volume=cum_volume,
            best_ask=best_ask,
            best_bid=best_bid,
            timestamp=self.clock(),
        )
        if not self.market_data.update_tick(tick):
            return False
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
