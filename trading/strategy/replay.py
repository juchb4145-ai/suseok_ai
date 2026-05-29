from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick


REPLAY_COLUMNS = [
    "timestamp",
    "code",
    "price",
    "change_rate",
    "cum_volume",
    "best_ask",
    "best_bid",
    "source",
    "row_type",
]


@dataclass
class TickReplayResult:
    processed_ticks: int = 0
    ignored_rows: int = 0
    completed_1m_count: int = 0
    details: dict = field(default_factory=dict)


class TickReplayRunner:
    def __init__(self, market_data: MarketDataStore | None = None, candle_builder: CandleBuilder | None = None) -> None:
        self.market_data = market_data or MarketDataStore()
        self.candle_builder = candle_builder or CandleBuilder()

    def replay_rows(self, rows: Iterable[dict]) -> TickReplayResult:
        result = TickReplayResult()
        sorted_rows = sorted(enumerate(rows), key=lambda item: (_parse_timestamp(item[1].get("timestamp")), item[0]))
        seen_codes: set[str] = set()
        for _, row in sorted_rows:
            row_type = str(row.get("row_type") or "tick")
            if row_type not in {"tick", "price"}:
                result.ignored_rows += 1
                continue
            tick = StrategyTick.from_realtime(
                row.get("code", ""),
                row.get("price", 0),
                change_rate=row.get("change_rate", 0.0),
                cum_volume=row.get("cum_volume", 0),
                best_ask=row.get("best_ask", 0),
                best_bid=row.get("best_bid", 0),
                timestamp=_parse_timestamp(row.get("timestamp")),
            )
            if not tick.code or tick.price <= 0:
                result.ignored_rows += 1
                continue
            seen_codes.add(tick.code)
            if self.market_data.update_tick(tick):
                self.candle_builder.update(tick)
                result.processed_ticks += 1
            else:
                result.ignored_rows += 1
        for code in seen_codes:
            latest = self.market_data.latest_tick(code)
            if latest is not None:
                self.candle_builder.flush(code, latest.timestamp + timedelta(minutes=1))
            result.completed_1m_count += len(self.candle_builder.completed_candles(code, 1))
        result.details["codes"] = sorted(seen_codes)
        return result

    def replay_csv(self, path: str | Path) -> TickReplayResult:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            return self.replay_rows(csv.DictReader(handle))


def _parse_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if value is None or str(value).strip() == "":
        return datetime.min
    return datetime.fromisoformat(str(value).strip()).replace(microsecond=0)
