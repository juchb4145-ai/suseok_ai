from __future__ import annotations

from trading.theme_engine.models import StockSnapshot
from trading.theme_engine.normalizer import normalize_stock_code


def snapshot_from_strategy_tick(tick, stock_name: str = "") -> StockSnapshot:
    metadata = dict(getattr(tick, "metadata", {}) or {})
    return StockSnapshot(
        stock_code=normalize_stock_code(getattr(tick, "code", "")),
        stock_name=stock_name or str(metadata.get("stock_name") or ""),
        current_price=float(getattr(tick, "price", 0) or 0),
        change_rate=float(getattr(tick, "change_rate", 0.0) or 0.0),
        volume=int(getattr(tick, "cum_volume", 0) or 0),
        turnover=float(getattr(tick, "trade_value", 0.0) or 0.0),
        execution_strength=float(getattr(tick, "execution_strength", 0.0) or 0.0),
        best_bid=float(getattr(tick, "best_bid", 0) or 0),
        best_ask=float(getattr(tick, "best_ask", 0) or 0),
        session_high=float(metadata.get("session_high") or metadata.get("day_high") or 0),
        session_low=float(metadata.get("session_low") or metadata.get("day_low") or 0),
        momentum_1m=float(metadata.get("momentum_1m") or 0.0),
        momentum_3m=float(metadata.get("momentum_3m") or 0.0),
        momentum_5m=float(metadata.get("momentum_5m") or 0.0),
        turnover_strength=float(metadata.get("turnover_strength") or 1.0),
        ts=str(getattr(tick, "timestamp", "") or ""),
        metadata=metadata,
    )


def snapshot_from_dict(data: dict) -> StockSnapshot:
    return StockSnapshot(
        stock_code=normalize_stock_code(str(data.get("stock_code") or data.get("code") or "")),
        stock_name=str(data.get("stock_name") or data.get("name") or ""),
        current_price=float(data.get("current_price", data.get("price", 0)) or 0),
        change_rate=float(data.get("change_rate", 0.0) or 0.0),
        volume=int(data.get("volume", data.get("cum_volume", 0)) or 0),
        turnover=float(data.get("turnover", data.get("trade_value", 0.0)) or 0.0),
        execution_strength=float(data.get("execution_strength", 0.0) or 0.0),
        best_bid=float(data.get("best_bid", 0.0) or 0.0),
        best_ask=float(data.get("best_ask", 0.0) or 0.0),
        session_high=float(data.get("session_high", 0.0) or 0.0),
        session_low=float(data.get("session_low", 0.0) or 0.0),
        momentum_1m=float(data.get("momentum_1m", 0.0) or 0.0),
        momentum_3m=float(data.get("momentum_3m", 0.0) or 0.0),
        momentum_5m=float(data.get("momentum_5m", 0.0) or 0.0),
        turnover_strength=float(data.get("turnover_strength", 1.0) or 1.0),
        ts=str(data.get("ts") or ""),
        metadata=dict(data.get("metadata") or {}),
    )
