from __future__ import annotations

from datetime import datetime
from typing import Optional

from trading.theme_engine.models import StockSnapshot
from trading.theme_engine.normalizer import normalize_stock_code
from trading.theme_engine.stock_snapshot import snapshot_from_strategy_tick


class KiwoomRealtimeThemeAdapter:
    def __init__(self) -> None:
        self._snapshots: dict[str, StockSnapshot] = {}

    def from_strategy_tick(self, tick) -> StockSnapshot:
        snapshot = snapshot_from_strategy_tick(tick)
        snapshot.updated_at = _now_text()
        if snapshot.turnover <= 0 and snapshot.current_price > 0 and snapshot.volume > 0:
            snapshot.turnover = snapshot.current_price * snapshot.volume
            snapshot.metadata["reason_codes"] = _append_reason(snapshot.metadata, "TURNOVER_ESTIMATED")
        return snapshot

    def from_kiwoom_real_data(self, stock_code: str, real_data: dict) -> StockSnapshot:
        code = normalize_stock_code(stock_code or str(_first(real_data, "stock_code", "code", "종목코드") or ""))
        price = abs(_float(_first(real_data, "current_price", "price", "현재가")))
        volume = abs(int(_float(_first(real_data, "volume", "cum_volume", "누적거래량"))))
        turnover = abs(_float(_first(real_data, "turnover", "trade_value", "거래대금")))
        metadata = dict(real_data.get("metadata") or {})
        reason_codes = list(metadata.get("reason_codes") or [])
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = price * volume
            reason_codes.append("TURNOVER_ESTIMATED")
        if reason_codes:
            metadata["reason_codes"] = sorted(set(reason_codes))
        return StockSnapshot(
            stock_code=code,
            stock_name=str(_first(real_data, "stock_name", "name", "종목명") or ""),
            current_price=price,
            change_rate=_float(_first(real_data, "change_rate", "등락률", "등락율")),
            volume=volume,
            turnover=turnover,
            execution_strength=max(0.0, _float(_first(real_data, "execution_strength", "체결강도"))),
            best_bid=abs(_float(_first(real_data, "best_bid", "매수호가"))),
            best_ask=abs(_float(_first(real_data, "best_ask", "매도호가"))),
            session_high=abs(_float(_first(real_data, "session_high", "high", "고가"))),
            session_low=abs(_float(_first(real_data, "session_low", "low", "저가"))),
            momentum_1m=_float(_first(real_data, "momentum_1m")),
            momentum_3m=_float(_first(real_data, "momentum_3m")),
            momentum_5m=_float(_first(real_data, "momentum_5m")),
            turnover_strength=max(0.0, _float(_first(real_data, "turnover_strength")) or 1.0),
            ts=str(_first(real_data, "ts", "timestamp") or ""),
            updated_at=_now_text(),
            metadata=metadata,
        )

    def update_snapshot(self, snapshot: StockSnapshot) -> None:
        snapshot.stock_code = normalize_stock_code(snapshot.stock_code)
        snapshot.updated_at = snapshot.updated_at or _now_text()
        self._snapshots[snapshot.stock_code] = snapshot

    def latest_snapshot(self, stock_code: str) -> Optional[StockSnapshot]:
        return self._snapshots.get(normalize_stock_code(stock_code))

    def latest_snapshots(self, stock_codes: list[str]) -> dict[str, StockSnapshot]:
        result = {}
        for code in stock_codes:
            snapshot = self.latest_snapshot(code)
            if snapshot is not None:
                result[normalize_stock_code(code)] = snapshot
        return result

    def all_snapshots(self) -> dict[str, StockSnapshot]:
        return dict(self._snapshots)


def _first(data: dict, *keys: str):
    for key in keys:
        if key in data and data[key] not in {None, ""}:
            return data[key]
    return None


def _float(value) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _append_reason(metadata: dict, reason: str) -> list[str]:
    reasons = list(metadata.get("reason_codes") or [])
    reasons.append(reason)
    return sorted(set(reasons))


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
