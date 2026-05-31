from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


PRICE_TICK_FIELDS = (
    "price",
    "change_rate",
    "volume",
    "trade_value",
    "execution_strength",
    "best_bid_ask",
    "day_high_low",
    "momentum",
)


@dataclass
class RealtimeDataQualityTracker:
    total_price_ticks: int = 0
    _field_counts: Counter[str] = field(default_factory=Counter)
    _reason_code_counts: Counter[str] = field(default_factory=Counter)
    estimated_turnover_count: int = 0
    parse_fallback_count: int = 0

    def observe_price_tick(self, payload: dict[str, Any]) -> None:
        payload = dict(payload or {})
        metadata = dict(payload.get("metadata") or {})
        self.total_price_ticks += 1

        for field in PRICE_TICK_FIELDS:
            if _field_present(field, payload, metadata):
                self._field_counts[field] += 1

        reason_codes = _reason_codes(payload, metadata)
        self._reason_code_counts.update(reason_codes)
        if "TURNOVER_ESTIMATED" in reason_codes:
            self.estimated_turnover_count += 1
        if "REAL_PARSE_FALLBACK" in reason_codes:
            self.parse_fallback_count += 1

    def snapshot(self) -> dict[str, Any]:
        total = max(1, self.total_price_ticks)
        return {
            "total_price_ticks": self.total_price_ticks,
            "field_coverage": {
                field: round(self._field_counts.get(field, 0) / total, 4) if self.total_price_ticks else 0.0
                for field in PRICE_TICK_FIELDS
            },
            "reason_code_counts": dict(sorted(self._reason_code_counts.items())),
            "estimated_turnover_count": self.estimated_turnover_count,
            "parse_fallback_count": self.parse_fallback_count,
            "summary": self.summary_line(),
        }

    def summary_line(self) -> str:
        snapshot = {
            field: round(
                self._field_counts.get(field, 0) / max(1, self.total_price_ticks),
                4,
            )
            if self.total_price_ticks
            else 0.0
            for field in PRICE_TICK_FIELDS
        }
        return (
            "REALTIME_DATA_QUALITY "
            f"total_ticks={self.total_price_ticks} "
            f"coverage_trade_value={snapshot['trade_value']:.2f} "
            f"coverage_execution_strength={snapshot['execution_strength']:.2f} "
            f"coverage_momentum={snapshot['momentum']:.2f} "
            f"estimated_turnover={self.estimated_turnover_count} "
            f"parse_fallback={self.parse_fallback_count}"
        )


def _field_present(field: str, payload: dict[str, Any], metadata: dict[str, Any]) -> bool:
    if field == "price":
        return _number(payload.get("price")) > 0
    if field == "change_rate":
        return "change_rate" in payload and payload.get("change_rate") is not None
    if field == "volume":
        return _number(payload.get("volume", payload.get("cum_volume"))) > 0
    if field == "trade_value":
        return _number(payload.get("trade_value")) > 0
    if field == "execution_strength":
        return _number(payload.get("execution_strength")) > 0
    if field == "best_bid_ask":
        return _number(payload.get("best_bid")) > 0 and _number(payload.get("best_ask")) > 0
    if field == "day_high_low":
        high = payload.get("day_high", payload.get("session_high", metadata.get("session_high")))
        low = payload.get("day_low", payload.get("session_low", metadata.get("session_low")))
        return _number(high) > 0 and _number(low) > 0
    if field == "momentum":
        return all(key in metadata or key in payload for key in ("momentum_1m", "momentum_3m", "momentum_5m"))
    return False


def _reason_codes(payload: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    values.extend(metadata.get("reason_codes") or [])
    values.extend(payload.get("reason_codes") or [])
    return sorted({str(value) for value in values if str(value or "").strip()})


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return abs(float(text))
    except (TypeError, ValueError):
        return 0.0
