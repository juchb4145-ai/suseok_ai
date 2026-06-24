from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class FillSimulationConfig:
    stale_tick_age_sec: float = 3.0
    default_limit_latency_ms: int = 150
    default_market_latency_ms: int = 250
    conservative_missing_liquidity_fill_ratio: float = 0.5


@dataclass(frozen=True)
class FillSimulationResult:
    side: str
    order_style: str
    requested_price: Optional[float] = None
    request_price_raw: Optional[float] = None
    rounded_requested_price: Optional[float] = None
    fill_price: Optional[float] = None
    slippage_bps: Optional[float] = None
    fill_ratio: float = 0.0
    partial_fill: bool = False
    simulated_latency_ms: int = 0
    reject_or_skip_reason: str = ""
    stale_tick: bool = False
    tick_size: Optional[int] = None
    limit_price_hit: Optional[bool] = None
    data_status: str = "OK"
    observed_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread_ticks: Optional[int] = None
    trade_value: Optional[float] = None
    execution_strength: Optional[float] = None
    order_amount: Optional[float] = None
    tick_timestamp: str = ""
    tick_age_sec: Optional[float] = None
    observed_tick_id: str = ""
    fallback_reasons: list[str] = field(default_factory=list)
    optional_data_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def simulate_fill(
    order: dict[str, Any] | None,
    ticks: Iterable[dict[str, Any]],
    *,
    config: FillSimulationConfig | None = None,
    orderbook_depth: Optional[dict[str, Any]] = None,
    vi_event: Optional[dict[str, Any]] = None,
) -> FillSimulationResult:
    config = config or FillSimulationConfig()
    order_payload = dict(order or {})
    side = _side(order_payload)
    order_style = _order_style(order_payload)
    fallback_reasons: list[str] = []
    optional_data_missing: list[str] = []
    if orderbook_depth is None:
        optional_data_missing.append("ORDERBOOK_DEPTH_MISSING")
    if vi_event is None:
        optional_data_missing.append("VI_EVENT_MISSING")
    if order_style == "LIMIT_CONSERVATIVE":
        fallback_reasons.append("HOGA_UNSUPPORTED_LIMIT_CONSERVATIVE")

    latency_ms = _simulated_latency_ms(order_payload, order_style, config)
    created_at = _first_text(order_payload.get("created_at"), order_payload.get("updated_at"), order_payload.get("decision_at"))
    target_time = _parse_time(created_at)
    if target_time is not None:
        target_time = target_time + timedelta(milliseconds=latency_ms)
    sorted_ticks = sorted((dict(tick) for tick in ticks), key=lambda tick: _tick_time(tick) or datetime.min)
    observed_tick = _first_tick_at_or_after(sorted_ticks, target_time) if target_time is not None else {}
    if not observed_tick and target_time is not None:
        observed_tick = _latest_tick_at_or_before(sorted_ticks, target_time)
    observed_price = _float(observed_tick.get("price")) if observed_tick else None
    best_bid = _float(observed_tick.get("best_bid")) if observed_tick else None
    best_ask = _float(observed_tick.get("best_ask")) if observed_tick else None
    spread_ticks = _int(observed_tick.get("spread_ticks")) if observed_tick else None
    trade_value = _float(observed_tick.get("trade_value")) if observed_tick else None
    execution_strength = _float(observed_tick.get("execution_strength")) if observed_tick else None
    tick_time = _tick_time(observed_tick) if observed_tick else None
    tick_age_sec = None
    if target_time is not None and tick_time is not None:
        tick_age_sec = max(0.0, (target_time - tick_time).total_seconds()) if tick_time <= target_time else 0.0

    request_price_raw = _float(order_payload.get("price"))
    benchmark_price = _first_float(
        request_price_raw if request_price_raw and request_price_raw > 0 else None,
        best_ask if side == "buy" else best_bid,
        observed_price,
    )
    tick_size = krx_stock_tick_size(benchmark_price) if benchmark_price else None
    rounded_requested_price = (
        round_price_to_tick(request_price_raw, side=side, direction="passive")
        if request_price_raw and request_price_raw > 0
        else None
    )
    requested_price = rounded_requested_price or benchmark_price
    order_amount = _order_amount(order_payload, requested_price)

    base_result = {
        "side": side,
        "order_style": order_style,
        "requested_price": requested_price,
        "request_price_raw": request_price_raw,
        "rounded_requested_price": rounded_requested_price,
        "simulated_latency_ms": latency_ms,
        "tick_size": tick_size,
        "observed_price": observed_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_ticks": spread_ticks,
        "trade_value": trade_value,
        "execution_strength": execution_strength,
        "order_amount": order_amount,
        "tick_timestamp": _tick_timestamp(observed_tick),
        "tick_age_sec": tick_age_sec,
        "observed_tick_id": str(observed_tick.get("event_id") or observed_tick.get("id") or ""),
        "fallback_reasons": fallback_reasons,
        "optional_data_missing": optional_data_missing,
    }
    if not observed_tick:
        return FillSimulationResult(**base_result, reject_or_skip_reason="TICK_MISSING", data_status="SKIPPED")
    stale_tick = tick_age_sec is not None and tick_age_sec > float(config.stale_tick_age_sec)
    if stale_tick:
        return FillSimulationResult(
            **base_result,
            reject_or_skip_reason="STALE_TICK",
            stale_tick=True,
            data_status="SKIPPED",
        )
    if order_style in {"LIMIT", "LIMIT_CONSERVATIVE"} and not requested_price:
        return FillSimulationResult(**base_result, reject_or_skip_reason="PRICE_MISSING_FOR_LIMIT", data_status="SKIPPED")

    limit_price_hit = _limit_price_hit(side, requested_price, observed_price, best_bid, best_ask)
    if order_style in {"LIMIT", "LIMIT_CONSERVATIVE"} and limit_price_hit is False:
        return FillSimulationResult(
            **base_result,
            limit_price_hit=limit_price_hit,
            reject_or_skip_reason="LIMIT_PRICE_NOT_MARKETABLE",
            data_status="SKIPPED",
        )

    fill_price, price_fallback = _fill_price(
        side=side,
        order_style=order_style,
        requested_price=requested_price,
        observed_price=observed_price,
        best_bid=best_bid,
        best_ask=best_ask,
    )
    if price_fallback:
        fallback_reasons.append(price_fallback)
    if not fill_price or fill_price <= 0:
        return FillSimulationResult(
            **{**base_result, "fallback_reasons": fallback_reasons},
            limit_price_hit=limit_price_hit,
            reject_or_skip_reason="FILL_PRICE_UNAVAILABLE",
            data_status="SKIPPED",
        )
    fill_ratio, liquidity_reason = _fill_ratio(
        order_amount=order_amount,
        trade_value=trade_value,
        execution_strength=execution_strength,
        spread_ticks=spread_ticks,
        order_style=order_style,
        config=config,
    )
    if liquidity_reason:
        fallback_reasons.append(liquidity_reason)
    partial_fill = 0.0 < fill_ratio < 0.9999
    reason = "PARTIAL_FILL_CONSERVATIVE" if partial_fill else "OK"
    slippage_bps = _slippage_bps(side, requested_price, fill_price)
    return FillSimulationResult(
        **{**base_result, "fallback_reasons": fallback_reasons},
        fill_price=round(float(fill_price), 4),
        slippage_bps=slippage_bps,
        fill_ratio=fill_ratio,
        partial_fill=partial_fill,
        limit_price_hit=limit_price_hit,
        reject_or_skip_reason=reason,
        stale_tick=False,
        data_status="OK",
    )


def krx_stock_tick_size(price: Any) -> Optional[int]:
    value = _float(price)
    if value is None or value <= 0:
        return None
    if value < 2_000:
        return 1
    if value < 5_000:
        return 5
    if value < 20_000:
        return 10
    if value < 50_000:
        return 50
    if value < 200_000:
        return 100
    if value < 500_000:
        return 500
    return 1_000


def round_price_to_tick(price: Any, *, side: str, direction: str = "passive") -> Optional[float]:
    value = _float(price)
    tick = krx_stock_tick_size(value)
    if value is None or tick is None:
        return None
    normalized_side = str(side or "").lower()
    if direction == "aggressive":
        rounded = math.ceil(value / tick) * tick if normalized_side == "buy" else math.floor(value / tick) * tick
    else:
        rounded = math.floor(value / tick) * tick if normalized_side == "buy" else math.ceil(value / tick) * tick
    return float(max(tick, rounded))


def summarize_fill_simulations(results: Iterable[dict[str, Any] | FillSimulationResult]) -> dict[str, Any]:
    rows = [row.to_dict() if isinstance(row, FillSimulationResult) else dict(row) for row in results]
    samples = [row for row in rows if row]
    filled = [row for row in samples if _float(row.get("fill_ratio")) and (_float(row.get("fill_ratio")) or 0.0) > 0.0]
    slippage_values = [_float(row.get("slippage_bps")) for row in filled]
    slippage_values = [value for value in slippage_values if value is not None]
    fill_ratios = [_float(row.get("fill_ratio")) for row in samples]
    fill_ratios = [value for value in fill_ratios if value is not None]
    partial = [row for row in samples if bool(row.get("partial_fill"))]
    stale = [row for row in samples if bool(row.get("stale_tick"))]
    skipped = [row for row in samples if str(row.get("data_status") or "") == "SKIPPED" or not _float(row.get("fill_ratio"))]
    return {
        "sample_count": len(samples),
        "filled_count": len(filled),
        "filled_rate": _ratio(len(filled), len(samples)),
        "skipped_count": len(skipped),
        "skipped_rate": _ratio(len(skipped), len(samples)),
        "partial_fill_count": len(partial),
        "partial_fill_rate": _ratio(len(partial), len(samples)),
        "stale_tick_count": len(stale),
        "stale_tick_rate": _ratio(len(stale), len(samples)),
        "avg_slippage_bps": _avg(slippage_values),
        "median_slippage_bps": round(median(slippage_values), 4) if slippage_values else None,
        "avg_fill_ratio": _avg(fill_ratios),
        "median_fill_ratio": round(median(fill_ratios), 4) if fill_ratios else None,
        "avg_simulated_latency_ms": _avg([row.get("simulated_latency_ms") for row in samples]),
        "by_reject_or_skip_reason": _top_counts((row.get("reject_or_skip_reason") or "UNKNOWN" for row in samples), key="reason"),
        "by_order_style": _top_counts((row.get("order_style") or "UNKNOWN" for row in samples), key="style"),
        "optional_missing_data": _top_counts(
            (
                missing
                for row in samples
                for missing in list(row.get("optional_data_missing") or [])
            ),
            key="missing",
        ),
    }


def _fill_price(
    *,
    side: str,
    order_style: str,
    requested_price: Optional[float],
    observed_price: Optional[float],
    best_bid: Optional[float],
    best_ask: Optional[float],
) -> tuple[Optional[float], str]:
    normalized = str(side or "").lower()
    reference = best_ask if normalized == "buy" else best_bid
    fallback = ""
    if reference is None:
        tick = krx_stock_tick_size(observed_price) or krx_stock_tick_size(requested_price) or 1
        if observed_price is None:
            return None, "QUOTE_AND_PRICE_MISSING"
        reference = observed_price + tick if normalized == "buy" else observed_price - tick
        fallback = "BEST_QUOTE_MISSING_USED_CONSERVATIVE_LAST_PLUS_TICK"
    rounded = round_price_to_tick(reference, side=normalized, direction="aggressive")
    if rounded is None:
        return None, fallback
    if order_style in {"LIMIT", "LIMIT_CONSERVATIVE"} and requested_price:
        if normalized == "buy":
            rounded = min(float(rounded), float(requested_price))
        else:
            rounded = max(float(rounded), float(requested_price))
    return float(rounded), fallback


def _fill_ratio(
    *,
    order_amount: Optional[float],
    trade_value: Optional[float],
    execution_strength: Optional[float],
    spread_ticks: Optional[int],
    order_style: str,
    config: FillSimulationConfig,
) -> tuple[float, str]:
    reason = ""
    if not order_amount or order_amount <= 0:
        return 0.0, "ORDER_AMOUNT_MISSING"
    if not trade_value or trade_value <= 0:
        ratio = float(config.conservative_missing_liquidity_fill_ratio)
        reason = "TRADE_VALUE_MISSING_CONSERVATIVE_FILL"
    else:
        pressure = float(order_amount) / float(trade_value)
        if pressure <= 0.005:
            ratio = 1.0
        elif pressure <= 0.01:
            ratio = 0.95
        elif pressure <= 0.03:
            ratio = 0.75
        elif pressure <= 0.10:
            ratio = 0.35
        else:
            ratio = 0.10
    if execution_strength is None:
        reason = reason or "EXECUTION_STRENGTH_MISSING"
    elif execution_strength >= 150:
        ratio += 0.05
    elif execution_strength < 80:
        ratio -= 0.15
    if spread_ticks is None:
        reason = reason or "SPREAD_TICKS_MISSING"
    elif spread_ticks >= 6:
        ratio -= 0.25
    elif spread_ticks >= 4:
        ratio -= 0.15
    if order_style == "MARKET":
        ratio += 0.05
    return round(max(0.05, min(1.0, ratio)), 4), reason


def _limit_price_hit(
    side: str,
    requested_price: Optional[float],
    observed_price: Optional[float],
    best_bid: Optional[float],
    best_ask: Optional[float],
) -> Optional[bool]:
    if not requested_price or requested_price <= 0:
        return None
    normalized = str(side or "").lower()
    if normalized == "sell":
        reference = _first_float(best_bid, observed_price)
        return None if reference is None else reference >= requested_price
    reference = _first_float(best_ask, observed_price)
    return None if reference is None else reference <= requested_price


def _slippage_bps(side: str, requested_price: Optional[float], fill_price: Optional[float]) -> Optional[float]:
    if not requested_price or not fill_price or requested_price <= 0:
        return None
    if str(side or "").lower() == "sell":
        return round(((float(requested_price) - float(fill_price)) / float(requested_price)) * 10_000.0, 4)
    return round(((float(fill_price) - float(requested_price)) / float(requested_price)) * 10_000.0, 4)


def _order_style(order: dict[str, Any]) -> str:
    hoga = str(order.get("hoga") or "").strip()
    if hoga in {"03", "3"}:
        return "MARKET"
    if hoga in {"", "0", "00"}:
        return "LIMIT"
    return "LIMIT_CONSERVATIVE"


def _side(order: dict[str, Any]) -> str:
    text = str(order.get("side") or "").strip().lower()
    return "sell" if text == "sell" else "buy"


def _simulated_latency_ms(order: dict[str, Any], order_style: str, config: FillSimulationConfig) -> int:
    sources = [order, dict(order.get("metadata") or {}), dict(order.get("request") or {}), dict(order.get("response") or {})]
    value = _first_float(
        *(_recursive_first(source, "simulated_latency_ms") for source in sources),
        *(_recursive_first(source, "gateway_command_latency_ms") for source in sources),
        *(_recursive_first(source, "command_latency_ms") for source in sources),
    )
    if value is not None:
        return max(0, int(round(value)))
    return int(config.default_market_latency_ms if order_style == "MARKET" else config.default_limit_latency_ms)


def _order_amount(order: dict[str, Any], requested_price: Optional[float]) -> Optional[float]:
    amount = _float(order.get("order_amount"))
    if amount and amount > 0:
        return amount
    quantity = _float(order.get("quantity"))
    if quantity and quantity > 0 and requested_price and requested_price > 0:
        return float(quantity) * float(requested_price)
    return None


def _first_tick_at_or_after(ticks: list[dict[str, Any]], target_time: datetime | None) -> dict[str, Any]:
    if target_time is None:
        return {}
    for tick in ticks:
        tick_time = _tick_time(tick)
        if tick_time is not None and tick_time >= target_time:
            return tick
    return {}


def _latest_tick_at_or_before(ticks: list[dict[str, Any]], target_time: datetime | None) -> dict[str, Any]:
    if target_time is None:
        return {}
    selected: dict[str, Any] = {}
    for tick in ticks:
        tick_time = _tick_time(tick)
        if tick_time is not None and tick_time <= target_time:
            selected = tick
    return selected


def _tick_time(tick: dict[str, Any]) -> datetime | None:
    return _parse_time(_tick_timestamp(tick))


def _tick_timestamp(tick: dict[str, Any]) -> str:
    return str(tick.get("timestamp") or tick.get("received_at") or tick.get("created_at") or "")


def _recursive_first(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
        for value in payload.values():
            found = _recursive_first(value, key)
            if found not in (None, ""):
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _recursive_first(value, key)
            if found not in (None, ""):
                return found
    return None


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable[Any]) -> Optional[float]:
    parsed = [_float(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return round(sum(parsed) / len(parsed), 4)


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _top_counts(values: Iterable[Any], *, key: str = "type") -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value or "")
        if not text:
            continue
        counts[text] = counts.get(text, 0) + 1
    return [{key: value, "count": count} for value, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)]
