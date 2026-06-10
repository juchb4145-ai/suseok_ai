from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from storage.db import TradingDatabase
from trading.broker.models import BrokerPriceTick, GatewayEvent, utc_timestamp

KST = timezone(timedelta(hours=9), "KST")


@dataclass(frozen=True)
class ReplayTickWriterConfig:
    enabled: bool = True
    queue_max_size: int = 5000
    batch_size: int = 200
    flush_interval_sec: float = 1.0
    min_interval_ms: float = 500.0


def replay_tick_writer_config_from_settings(settings: Any) -> ReplayTickWriterConfig:
    return ReplayTickWriterConfig(
        enabled=bool(getattr(settings, "replay_tick_history_enabled", True)),
        queue_max_size=max(1, int(getattr(settings, "replay_tick_history_queue_max_size", 5000) or 5000)),
        batch_size=max(1, int(getattr(settings, "replay_tick_history_batch_size", 200) or 200)),
        flush_interval_sec=max(0.05, float(getattr(settings, "replay_tick_history_flush_interval_sec", 1.0) or 1.0)),
        min_interval_ms=max(0.0, float(getattr(settings, "replay_tick_history_min_interval_ms", 500.0) or 500.0)),
    )


class ReplayGradeTickBuffer:
    def __init__(self, db_path: str | Path, config: ReplayTickWriterConfig | None = None) -> None:
        self.db_path = Path(db_path).expanduser()
        self.config = config or ReplayTickWriterConfig()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.config.queue_max_size)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._last_accepted_monotonic_by_code: dict[str, float] = {}
        self._received_count = 0
        self._queued_count = 0
        self._persisted_count = 0
        self._dropped_count = 0
        self._throttled_count = 0
        self._failed_count = 0
        self._last_error = ""
        self._last_queued_at = ""
        self._last_flushed_at = ""
        self._last_code = ""
        self._last_batch_size = 0

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="replay-tick-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout or 0.1)))

    def enqueue_event(self, event: GatewayEvent) -> bool:
        if not self.config.enabled or event.type != "price_tick":
            return False
        with self._state_lock:
            self._received_count += 1
        try:
            row = self._row_from_event(event)
        except Exception as exc:
            self._record_failure(exc)
            return False
        code = str(row.get("code") or "")
        if not code or not row.get("price"):
            with self._state_lock:
                self._dropped_count += 1
                self._last_error = "PRICE_TICK_INSUFFICIENT"
            return False
        if self._is_throttled(code):
            with self._state_lock:
                self._throttled_count += 1
            return False
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            with self._state_lock:
                self._dropped_count += 1
                self._last_error = "REPLAY_TICK_QUEUE_FULL"
            return False
        with self._state_lock:
            self._queued_count += 1
            self._last_queued_at = row["received_at"]
            self._last_code = code
        return True

    def snapshot(self) -> dict[str, Any]:
        thread = self._thread
        with self._state_lock:
            return {
                "enabled": self.config.enabled,
                "running": bool(thread and thread.is_alive()),
                "queue_size": self._queue.qsize(),
                "queue_max_size": self.config.queue_max_size,
                "batch_size": self.config.batch_size,
                "flush_interval_sec": self.config.flush_interval_sec,
                "min_interval_ms": self.config.min_interval_ms,
                "received_count": self._received_count,
                "queued_count": self._queued_count,
                "persisted_count": self._persisted_count,
                "dropped_count": self._dropped_count,
                "throttled_count": self._throttled_count,
                "failed_count": self._failed_count,
                "last_error": self._last_error,
                "last_queued_at": self._last_queued_at,
                "last_flushed_at": self._last_flushed_at,
                "last_code": self._last_code,
                "last_batch_size": self._last_batch_size,
            }

    def _run(self) -> None:
        db = TradingDatabase(str(self.db_path))
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                batch = self._drain_batch()
                if not batch:
                    continue
                try:
                    persisted = db.save_gateway_price_ticks_batch(batch)
                    with self._state_lock:
                        self._persisted_count += int(persisted or 0)
                        self._last_batch_size = len(batch)
                        self._last_flushed_at = utc_timestamp()
                        self._last_error = ""
                except Exception as exc:
                    self._record_failure(exc)
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            db.close()

    def _drain_batch(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        timeout = max(0.05, float(self.config.flush_interval_sec or 1.0))
        try:
            first = self._queue.get(timeout=timeout)
        except queue.Empty:
            return batch
        batch.append(first)
        limit = max(1, int(self.config.batch_size or 1))
        while len(batch) < limit:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _is_throttled(self, code: str) -> bool:
        min_interval_ms = max(0.0, float(self.config.min_interval_ms or 0.0))
        if min_interval_ms <= 0:
            return False
        now = time.monotonic() * 1000.0
        with self._state_lock:
            previous = self._last_accepted_monotonic_by_code.get(code)
            if previous is not None and now - previous < min_interval_ms:
                return True
            self._last_accepted_monotonic_by_code[code] = now
        return False

    def _row_from_event(self, event: GatewayEvent) -> dict[str, Any]:
        payload = dict(event.payload or {})
        tick = BrokerPriceTick.from_dict(payload)
        received_at = utc_timestamp()
        trace = payload.get("_transport_trace") if isinstance(payload.get("_transport_trace"), dict) else {}
        transport_mode = str(payload.get("transport_mode") or trace.get("transport_mode") or "")
        raw_timestamp = str(payload.get("timestamp") or event.timestamp or tick.timestamp)
        return {
            "event_id": event.event_id,
            "timestamp": _replay_session_timestamp(raw_timestamp, trade_time=tick.trade_time),
            "received_at": received_at,
            "code": tick.code,
            "name": tick.name,
            "price": tick.price,
            "change_rate": tick.change_rate,
            "cum_volume": tick.volume,
            "trade_value": tick.trade_value,
            "execution_strength": tick.execution_strength,
            "best_bid": tick.best_bid,
            "best_ask": tick.best_ask,
            "spread_ticks": tick.spread_ticks,
            "source": event.source,
            "transport_mode": transport_mode,
            "instrument_type": tick.instrument_type,
            "trade_time": tick.trade_time,
            "day_high": tick.day_high,
            "day_low": tick.day_low,
            "raw_payload": payload,
            "metadata": tick.metadata,
            "created_at": received_at,
        }

    def _record_failure(self, exc: Exception) -> None:
        with self._state_lock:
            self._failed_count += 1
            self._last_error = str(exc) or repr(exc)


def _replay_session_timestamp(value: str, *, trade_time: str = "") -> str:
    base = _parse_timestamp(value)
    if trade_time and len(str(trade_time).strip()) >= 6:
        hhmmss = "".join(ch for ch in str(trade_time).strip() if ch.isdigit())[:6]
        if len(hhmmss) == 6:
            base = base.astimezone(KST)
            return (
                f"{base.date().isoformat()}T"
                f"{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
            )
    if base.tzinfo is not None:
        return base.astimezone(KST).replace(tzinfo=None).isoformat(timespec="seconds")
    return base.isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
