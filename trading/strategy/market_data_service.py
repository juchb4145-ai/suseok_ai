from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from trading.broker.models import GatewayEvent
from trading.runtime_ports import MarketDataSnapshot
from trading.strategy.bridge import StrategyMarketDataBridge
from trading.strategy.candles import CandleBuilder
from trading.strategy.market_data import MarketDataStore, StrategyTick
from trading.strategy.market_index import IndexCodeMapper, MarketIndexStore, zero_padded_index_logical_code


class DirtyReason(str, Enum):
    PRICE_TICK = "PRICE_TICK"
    CANDLE_BOUNDARY = "CANDLE_BOUNDARY"
    DATA_QUALITY_CHANGED = "DATA_QUALITY_CHANGED"
    SPREAD_CHANGED = "SPREAD_CHANGED"
    ORDER_EVENT = "ORDER_EVENT"
    POSITION_EVENT = "POSITION_EVENT"
    THEME_ROLE_CHANGED = "THEME_ROLE_CHANGED"
    MARKET_REGIME_CHANGED = "MARKET_REGIME_CHANGED"


@dataclass(frozen=True)
class MarketDataServiceConfig:
    enabled: bool = True
    dirty_queue_enabled: bool = True
    batch_flush_enabled: bool = False
    max_tick_age_sec: int = 10
    dirty_debounce_ms: int = 200

    @classmethod
    def from_env(cls) -> "MarketDataServiceConfig":
        return cls(
            enabled=_env_bool("TRADING_MARKET_DATA_SERVICE_ENABLED", True),
            dirty_queue_enabled=_env_bool("TRADING_MARKET_DATA_DIRTY_QUEUE_ENABLED", True),
            batch_flush_enabled=_env_bool("TRADING_MARKET_DATA_BATCH_FLUSH_ENABLED", False),
            max_tick_age_sec=max(1, _env_int("TRADING_MARKET_DATA_MAX_TICK_AGE_SEC", 10)),
            dirty_debounce_ms=max(0, _env_int("TRADING_MARKET_DATA_DIRTY_DEBOUNCE_MS", 200)),
        )


@dataclass(frozen=True)
class DirtyCodeEvent:
    code: str
    reason: str
    source_event_id: str = ""
    marked_at: str = ""


class DirtyCodeQueue:
    def __init__(self, *, clock: Callable[[], datetime] | None = None, debounce_ms: int = 200) -> None:
        self.clock = clock or datetime.now
        self.debounce_ms = max(0, int(debounce_ms))
        self._items: OrderedDict[str, DirtyCodeEvent] = OrderedDict()
        self._last_marked_at: dict[tuple[str, str], datetime] = {}

    def mark_dirty(
        self,
        code: str,
        reason: DirtyReason | str,
        source_event_id: str = "",
        marked_at: datetime | None = None,
    ) -> bool:
        clean_code = _clean_code(code)
        if not clean_code:
            return False
        reason_text = str(reason.value if isinstance(reason, DirtyReason) else reason or "")
        if not reason_text:
            return False
        now = _clean_time(marked_at or self.clock())
        key = (clean_code, reason_text)
        previous = self._last_marked_at.get(key)
        if previous is not None and self.debounce_ms > 0:
            if (now - previous).total_seconds() * 1000.0 < self.debounce_ms:
                return False
        self._last_marked_at[key] = now
        existing = self._items.get(clean_code)
        if existing is not None:
            combined = reason_text if existing.reason == reason_text else f"{existing.reason},{reason_text}"
            self._items[clean_code] = DirtyCodeEvent(
                code=clean_code,
                reason=combined,
                source_event_id=source_event_id or existing.source_event_id,
                marked_at=_format_time(now),
            )
            self._items.move_to_end(clean_code)
            return True
        self._items[clean_code] = DirtyCodeEvent(
            code=clean_code,
            reason=reason_text,
            source_event_id=source_event_id,
            marked_at=_format_time(now),
        )
        return True

    def pop_dirty(self, limit: int = 100) -> list[DirtyCodeEvent]:
        result: list[DirtyCodeEvent] = []
        for _ in range(max(0, int(limit))):
            if not self._items:
                break
            _, item = self._items.popitem(last=False)
            result.append(item)
        return result

    def peek_dirty_count(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()
        self._last_marked_at.clear()

    def snapshot(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for item in self._items.values():
            for reason in item.reason.split(","):
                reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "dirty_count": len(self._items),
            "dirty_codes": list(self._items.keys()),
            "reason_counts": reasons,
            "debounce_ms": self.debounce_ms,
        }


@dataclass
class MarketDataService:
    market_data: MarketDataStore
    candle_builder: CandleBuilder
    market_index_store: MarketIndexStore | None = None
    index_code_mapper: IndexCodeMapper | None = None
    config: MarketDataServiceConfig = field(default_factory=MarketDataServiceConfig.from_env)
    warning_sink: Callable[[str], None] | None = None
    clock: Callable[[], datetime] | None = None
    flush_store: Any = None
    dirty_queue: DirtyCodeQueue | None = None

    def __post_init__(self) -> None:
        self.index_code_mapper = self.index_code_mapper or IndexCodeMapper()
        self.clock = self.clock or datetime.now
        self.dirty_queue = self.dirty_queue or DirtyCodeQueue(clock=self.clock, debounce_ms=self.config.dirty_debounce_ms)
        self._bridge = StrategyMarketDataBridge(
            self.market_data,
            self.candle_builder,
            market_index_store=self.market_index_store,
            index_code_mapper=self.index_code_mapper,
            clock=self.clock,
        )
        self.latest_snapshot_by_code: dict[str, MarketDataSnapshot] = {}
        self._last_quality_by_code: dict[str, str] = {}
        self._last_spread_by_code: dict[str, int] = {}
        self._pending_flush: dict[str, MarketDataSnapshot] = {}
        self._flush_warning_count = 0

    def update_from_gateway_event(self, event: GatewayEvent) -> bool:
        if event.type != "price_tick":
            return False
        return self.handle_price_tick(dict(event.payload or {}), source_event_id=str(event.event_id or ""))

    def handle_price_tick(self, payload: Mapping[str, Any], *, source_event_id: str = "") -> bool:
        if not self.config.enabled:
            return False
        code = _clean_code(payload.get("code") or payload.get("stock_code") or "")
        if not code:
            self._warn("PRICE_TICK_CODE_MISSING")
            return False
        before_completed = self._completed_candle_counts(code)
        handled = self._bridge.on_realtime_tick(
            code=code,
            price=payload.get("price", 0),
            change_rate=payload.get("change_rate", 0.0),
            cum_volume=payload.get("cum_volume", payload.get("volume", 0)),
            best_ask=payload.get("best_ask", 0),
            best_bid=payload.get("best_bid", 0),
            trade_value=payload.get("trade_value", 0),
            execution_strength=payload.get("execution_strength", 0),
            spread_ticks=payload.get("spread_ticks", 0),
            instrument_type=self._instrument_type(code, payload.get("instrument_type"), payload.get("name")),
            name=str(payload.get("name") or ""),
            day_high=payload.get("day_high", 0),
            day_low=payload.get("day_low", 0),
            trade_time=str(payload.get("trade_time") or ""),
            open_price=payload.get("open_price", 0),
            metadata=_tick_metadata(dict(payload), source_event_id=source_event_id),
        )
        if not handled:
            return False
        if self._is_index_payload(code, payload):
            return True
        tick = self.market_data.latest_tick(code)
        if tick is None:
            return False
        snapshot = self._snapshot_from_tick(tick, payload, source_event_id=source_event_id, now=tick.timestamp)
        self.latest_snapshot_by_code[code] = snapshot
        self._pending_flush[code] = snapshot
        self._mark_dirty(code, DirtyReason.PRICE_TICK, source_event_id, marked_at=tick.timestamp)
        after_completed = self._completed_candle_counts(code)
        if any(after_completed[interval] > before_completed.get(interval, 0) for interval in after_completed):
            self._mark_dirty(code, DirtyReason.CANDLE_BOUNDARY, source_event_id, marked_at=tick.timestamp)
        previous_quality = self._last_quality_by_code.get(code)
        if previous_quality is not None and previous_quality != snapshot.data_quality_status:
            self._mark_dirty(code, DirtyReason.DATA_QUALITY_CHANGED, source_event_id, marked_at=tick.timestamp)
        self._last_quality_by_code[code] = snapshot.data_quality_status
        previous_spread = self._last_spread_by_code.get(code)
        if previous_spread is not None and previous_spread != snapshot.spread_ticks:
            self._mark_dirty(code, DirtyReason.SPREAD_CHANGED, source_event_id, marked_at=tick.timestamp)
        self._last_spread_by_code[code] = snapshot.spread_ticks
        return True

    def latest_snapshot(self, code: str) -> MarketDataSnapshot | None:
        return self.latest_snapshot_by_code.get(_clean_code(code))

    def dirty_codes(self, *, limit: int = 1000) -> list[str]:
        return [item.code for item in self.dirty_queue.pop_dirty(limit=limit)]

    def data_quality_snapshot(self) -> dict[str, Any]:
        return self._bridge.data_quality_snapshot()

    def flush_batch(self) -> dict[str, Any]:
        pending_count = len(self._pending_flush)
        if not self.config.batch_flush_enabled:
            return {"enabled": False, "status": "DISABLED", "pending_count": pending_count, "flushed_count": 0}
        if self.flush_store is None:
            self._pending_flush.clear()
            return {"enabled": True, "status": "NOOP", "pending_count": pending_count, "flushed_count": 0}
        try:
            writer = getattr(self.flush_store, "save_market_data_snapshots_batch", None)
            if not callable(writer):
                writer = getattr(self.flush_store, "save_market_data_snapshot_batch", None)
            if not callable(writer):
                self._pending_flush.clear()
                return {"enabled": True, "status": "NOOP", "pending_count": pending_count, "flushed_count": 0}
            rows = [snapshot.__dict__ for snapshot in self._pending_flush.values()]
            flushed = int(writer(rows) or 0)
            self._pending_flush.clear()
            return {"enabled": True, "status": "OK", "pending_count": pending_count, "flushed_count": flushed}
        except Exception as exc:
            self._flush_warning_count += 1
            self._warn(f"MARKET_DATA_BATCH_FLUSH_FAILED:{exc}")
            return {"enabled": True, "status": "ERROR", "pending_count": pending_count, "flushed_count": 0, "error": str(exc)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "snapshot_count": len(self.latest_snapshot_by_code),
            "pending_flush_count": len(self._pending_flush),
            "dirty_queue": self.dirty_queue.snapshot(),
            "batch_flush_enabled": self.config.batch_flush_enabled,
            "flush_warning_count": self._flush_warning_count,
        }

    def _snapshot_from_tick(
        self,
        tick: StrategyTick,
        payload: Mapping[str, Any],
        *,
        source_event_id: str,
        now: datetime | None = None,
    ) -> MarketDataSnapshot:
        now = _clean_time(now or self.clock())
        tick_time = _clean_time(tick.timestamp)
        age_sec = max(0.0, (now - tick_time).total_seconds())
        metadata = dict(tick.metadata or {})
        quality, reasons = self._data_quality(tick, age_sec)
        freshness = "FRESH" if age_sec <= self.config.max_tick_age_sec else "STALE_TICK"
        store_day_high, store_day_low = self.market_data.day_high_low(tick.code)
        metadata_day_high = _safe_int(metadata.get("day_high") or metadata.get("session_high"))
        metadata_day_low = _safe_int(metadata.get("day_low") or metadata.get("session_low"))
        day_high = max(_safe_int(store_day_high), metadata_day_high)
        low_values = [value for value in (_safe_int(store_day_low), metadata_day_low) if value > 0]
        day_low = min(low_values) if low_values else 0
        vwap = _safe_float(metadata.get("vwap"))
        if vwap <= 0:
            vwap = self._vwap_from_candles(tick.code)
        price_source = str(metadata.get("price_source") or metadata.get("backfill_source") or "REALTIME")
        metadata["data_quality_reason_codes"] = reasons
        metadata["freshness_status"] = freshness
        if source_event_id:
            metadata["source_event_id"] = source_event_id
        return MarketDataSnapshot(
            code=tick.code,
            name=str(payload.get("name") or metadata.get("stock_name") or ""),
            price=int(tick.price or 0),
            change_rate=float(tick.change_rate or 0.0),
            trade_value=float(tick.trade_value or 0.0),
            turnover=float(tick.trade_value or 0.0),
            cum_volume=int(tick.cum_volume or 0),
            execution_strength=float(tick.execution_strength or 0.0),
            best_ask=int(tick.best_ask or 0),
            best_bid=int(tick.best_bid or 0),
            spread_ticks=int(tick.spread_ticks or 0),
            day_high=int(day_high or 0),
            day_low=int(day_low or 0),
            open_price=_safe_int(payload.get("open_price") or metadata.get("open_price")),
            vwap=float(vwap or 0.0),
            tick_at=_format_time(tick_time),
            tick_timestamp=_format_time(tick_time),
            received_at=_format_time(now),
            tick_age_sec=round(age_sec, 3),
            tick_age_ms=int(round(age_sec * 1000)),
            freshness_status= freshness,
            is_fresh=freshness == "FRESH",
            data_quality=quality,
            data_quality_status=quality,
            reason_codes=tuple(reasons),
            source_event_id=source_event_id,
            price_source=price_source,
            updated_at=_format_time(now),
            metadata=metadata,
        )

    def _data_quality(self, tick: StrategyTick, age_sec: float) -> tuple[str, list[str]]:
        reasons: list[str] = []
        metadata = dict(tick.metadata or {})
        if age_sec > self.config.max_tick_age_sec:
            reasons.append("STALE_TICK")
        if int(tick.price or 0) <= 0:
            reasons.append("MISSING_PRICE")
        if float(tick.trade_value or 0.0) <= 0 and int(tick.cum_volume or 0) <= 0:
            reasons.append("TURNOVER_MISSING")
        if str(metadata.get("price_source") or "").upper() == "TR_BACKFILL":
            reasons.append("TR_BACKFILL_PRICE_ONLY")
        if reasons:
            if any(reason in reasons for reason in {"STALE_TICK", "MISSING_PRICE", "TR_BACKFILL_PRICE_ONLY"}):
                return "DATA_WAIT", reasons
            return "WARN", reasons
        return "OK", ["DATA_READY"]

    def _completed_candle_counts(self, code: str) -> dict[int, int]:
        return {interval: len(self.candle_builder.completed_candles(code, interval)) for interval in (1, 3, 5)}

    def _vwap_from_candles(self, code: str) -> float:
        candles = self.candle_builder.completed_candles(code, 1)
        total_volume = sum(max(0, candle.volume) for candle in candles)
        if total_volume <= 0:
            return 0.0
        return sum(candle.close * max(0, candle.volume) for candle in candles) / total_volume

    def _mark_dirty(
        self,
        code: str,
        reason: DirtyReason,
        source_event_id: str = "",
        *,
        marked_at: datetime | None = None,
    ) -> None:
        if self.config.dirty_queue_enabled:
            self.dirty_queue.mark_dirty(code, reason, source_event_id=source_event_id, marked_at=marked_at)

    def _instrument_type(self, code: str, instrument_type: Any, name: Any = "") -> Any:
        explicit = str(instrument_type or "").strip().lower()
        if explicit == "index":
            return "index"
        if self.index_code_mapper.is_index_code(code):
            return "index"
        if zero_padded_index_logical_code(code) is not None:
            return "index"
        return instrument_type or "stock"

    def _is_index_payload(self, code: str, payload: Mapping[str, Any]) -> bool:
        return self._instrument_type(code, payload.get("instrument_type"), payload.get("name")) == "index"

    def _warn(self, warning: str) -> None:
        if self.warning_sink is not None:
            self.warning_sink(warning)


def _tick_metadata(payload: dict[str, Any], *, source_event_id: str = "") -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
    if source_event_id:
        metadata.setdefault("source_event_id", source_event_id)
    if payload.get("transport_trace"):
        metadata.setdefault("transport_trace", dict(payload.get("transport_trace") or {}))
    if payload.get("trace"):
        metadata.setdefault("transport_trace", dict(payload.get("trace") or {}))
    if payload.get("timestamp"):
        metadata.setdefault("broker_tick_timestamp", str(payload.get("timestamp") or ""))
    if payload.get("gateway_realtime_reliability"):
        metadata.setdefault("gateway_realtime_reliability", dict(payload.get("gateway_realtime_reliability") or {}))
    reason_codes = set(str(value) for value in metadata.get("reason_codes") or [] if str(value or "").strip())
    reason_codes.update(str(value) for value in payload.get("reason_codes") or [] if str(value or "").strip())
    if reason_codes:
        metadata["reason_codes"] = sorted(reason_codes)
    if payload.get("day_high"):
        metadata.setdefault("session_high", payload.get("day_high"))
        metadata.setdefault("day_high", payload.get("day_high"))
    if payload.get("day_low"):
        metadata.setdefault("session_low", payload.get("day_low"))
        metadata.setdefault("day_low", payload.get("day_low"))
    if payload.get("trade_time"):
        metadata.setdefault("trade_time", str(payload.get("trade_time") or ""))
    if payload.get("spread_price"):
        metadata.setdefault("spread_price", payload.get("spread_price"))
    price_source = str(payload.get("price_source") or metadata.get("price_source") or "").strip()
    if price_source:
        metadata["price_source"] = price_source
    return metadata


def _index_payload_hint(instrument_type: Any, name: Any = "") -> bool:
    explicit = str(instrument_type or "").strip().lower()
    if explicit == "index":
        return True
    display_name = str(name or "").strip().upper()
    return display_name in {"KOSPI", "KOSDAQ", "코스피", "코스닥"}


def _clean_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("A") and len(text) == 7:
        text = text[1:]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits.zfill(6) if digits else text


def _clean_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    return _clean_time(value).isoformat(timespec="seconds")


def _safe_int(value: Any) -> int:
    try:
        return abs(int(float(str(value or "0").strip().replace(",", "").replace("+", ""))))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(str(value or "0").strip().replace(",", "").replace("+", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "DirtyCodeEvent",
    "DirtyCodeQueue",
    "DirtyReason",
    "MarketDataService",
    "MarketDataServiceConfig",
]
