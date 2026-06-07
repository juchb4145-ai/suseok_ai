from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


READY_EVENT_TYPES = {"BUY_READY_NEW", "BUY_READY_SMALL_NEW", "READY_BUT_LIVE_BLOCKED"}
ORDER_EVENT_TYPES = {"ORDER_INTENT_CREATED", "VIRTUAL_ORDER_CREATED"}
WAIT_BLOCK_EVENT_TYPES = {
    "READY_TO_WAIT",
    "MARKET_WAIT_STARTED",
    "DATA_QUALITY_DEGRADED",
    "SNAPSHOT_STALE",
    "GATEWAY_DISCONNECTED",
    "CHASE_RISK_BLOCKED",
    "LATE_CHASE_TEMP_WAIT",
}
CHASE_EVENT_TYPES = {"CHASE_RISK_BLOCKED", "LATE_CHASE_TEMP_WAIT"}
REVIEW_EVENT_TYPES = READY_EVENT_TYPES | WAIT_BLOCK_EVENT_TYPES | ORDER_EVENT_TYPES
RETURN_WINDOWS = (1, 3, 5, 10)


def build_postmarket_review(db, trade_date: str, review_scope: str = "postmarket") -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = collect_candidate_events(db, trade_date)
    ordered_keys = _ordered_candidate_keys(events)
    items: list[dict[str, Any]] = []

    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "").upper()
        if event_type not in REVIEW_EVENT_TYPES:
            continue
        enriched = {**event, "has_order": _event_has_order(event, ordered_keys)}
        base = resolve_base_price(enriched)
        future = resolve_future_prices(enriched)
        returns = _calculate_returns(base.get("base_price"), future)
        classification = classify_event_outcome(enriched, returns, {"base": base, "future": future})
        item = {
            "review_id": _review_id(trade_date, review_scope, enriched),
            "trade_date": trade_date,
            "generated_at": generated_at,
            "review_scope": review_scope,
            "symbol": _event_value(enriched, "symbol"),
            "stock_name": _event_value(enriched, "stock_name", "name"),
            "primary_theme": _event_value(enriched, "primary_theme", "theme_name"),
            "stock_role": _event_value(enriched, "stock_role", "role"),
            "candidate_instance_id": _event_value(enriched, "candidate_instance_id"),
            "event_id": _event_value(enriched, "event_id", "id"),
            "event_type": event_type,
            "source_status": _source_status(enriched),
            "block_reason": _block_reason(enriched),
            "block_reason_codes": _block_reason_codes(enriched),
            "base_time": base.get("base_time"),
            "base_price": base.get("base_price"),
            "price_1m": future.get("price_1m"),
            "price_3m": future.get("price_3m"),
            "price_5m": future.get("price_5m"),
            "price_10m": future.get("price_10m"),
            "price_close_or_last": future.get("price_close_or_last"),
            "return_1m_pct": returns.get("return_1m_pct"),
            "return_3m_pct": returns.get("return_3m_pct"),
            "return_5m_pct": returns.get("return_5m_pct"),
            "return_10m_pct": returns.get("return_10m_pct"),
            "return_close_or_last_pct": returns.get("return_close_or_last_pct"),
            **classification,
            "payload": {
                "event": enriched,
                "price_sources": {
                    "base": base.get("source") or "",
                    "future": future.get("source") or "",
                },
                "has_order": bool(enriched.get("has_order")),
            },
        }
        items.append(item)

    summary = summarize_review_items(items)
    return {
        "trade_date": trade_date,
        "review_scope": review_scope,
        "generated_at": generated_at,
        "generated_count": len(items),
        "items": items,
        "summary": summary,
        "block_reason_summary": build_block_reason_summary(items),
        "symbol_summary": build_symbol_review_summary(items),
        "theme_summary": build_theme_review_summary(items),
    }


def collect_candidate_events(db, trade_date: str) -> list[dict[str, Any]]:
    events = db.list_operator_events(
        trade_date,
        include_acknowledged=True,
        include_hidden=True,
        limit=1000,
    )
    return sorted(events, key=lambda item: str(item.get("occurred_at") or item.get("created_at") or ""))


def resolve_base_price(event: dict[str, Any], price_sources: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _payload(event)
    sources = [payload, event, price_sources or {}]
    for source in sources:
        if not isinstance(source, dict):
            continue
        price = _first_number(
            source,
            (
                "base_price",
                "current_price",
                "last_price",
                "price",
                "reference_price",
                "entry_price",
                "vwap",
            ),
        )
        if price is not None:
            return {
                "base_time": _first_text(source, ("base_time", "occurred_at", "calculated_at")) or event.get("occurred_at"),
                "base_price": price,
                "source": "payload_direct" if source is payload else "event_direct",
            }
    candles = _extract_candles(event)
    if candles:
        close = _candle_close(candles[0])
        if close is not None:
            return {
                "base_time": _candle_time(candles[0]) or event.get("occurred_at"),
                "base_price": close,
                "source": "candle_first_close",
            }
    return {"base_time": event.get("occurred_at"), "base_price": None, "source": "missing"}


def resolve_future_prices(
    event: dict[str, Any],
    price_sources: dict[str, Any] | None = None,
    windows: list[int] | tuple[int, ...] = RETURN_WINDOWS,
) -> dict[str, Any]:
    payload = _payload(event)
    sources = [payload, event, price_sources or {}]
    result: dict[str, Any] = {f"price_{window}m": None for window in windows}
    result["price_close_or_last"] = None
    source_name = ""

    for source in sources:
        if not isinstance(source, dict):
            continue
        future_prices = source.get("future_prices")
        if isinstance(future_prices, dict):
            source = {**source, **future_prices}
        found = False
        for window in windows:
            price = _first_number(source, (f"price_{window}m", f"future_price_{window}m", f"after_{window}m_price"))
            if price is not None:
                result[f"price_{window}m"] = price
                found = True
        close = _first_number(
            source,
            (
                "price_close_or_last",
                "close_or_last_price",
                "last_observed_price",
                "session_close_price",
                "close_price",
            ),
        )
        if close is not None:
            result["price_close_or_last"] = close
            found = True
        if found:
            source_name = "payload_direct" if source is payload else "event_direct"
            break

    if any(result.get(f"price_{window}m") is not None for window in windows) or result.get("price_close_or_last") is not None:
        result["source"] = source_name or "direct"
        return result

    candles = _extract_candles(event)
    if candles:
        for window in windows:
            index = min(max(window, 1), len(candles) - 1)
            result[f"price_{window}m"] = _candle_close(candles[index])
        result["price_close_or_last"] = _candle_close(candles[-1])
        result["source"] = "candle_close"
        return result

    result["source"] = "missing"
    return result


def classify_event_outcome(event: dict[str, Any], returns: dict[str, float | None], data_quality: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or event.get("type") or "").upper()
    has_order = bool(event.get("has_order")) or _payload_flag(event, "runtime_order_intent_created") or _payload_flag(event, "virtual_order_created")
    short_return = _max_return(returns, "return_3m_pct", "return_5m_pct")
    downside = _min_return(returns, "return_3m_pct", "return_5m_pct", "return_close_or_last_pct")
    has_return = any(returns.get(key) is not None for key in ("return_3m_pct", "return_5m_pct", "return_close_or_last_pct"))
    confidence = _confidence(event, data_quality, has_return)

    if not has_return:
        return _classification(
            "DATA_INSUFFICIENT",
            confidence="LOW",
            reason="기준가 또는 후속 가격 데이터가 부족합니다.",
            recommendation="가격 payload, minute candle, chart_universe 저장 경로를 점검하세요.",
        )
    if event_type in CHASE_EVENT_TYPES and downside is not None and downside <= -0.5:
        return _classification(
            "PROTECTED_FROM_CHASE",
            confidence=confidence,
            reason="추격 차단 뒤 후속 가격이 약했습니다.",
            recommendation="late chase block은 유효했습니다. 같은 조건의 반복 빈도만 리뷰하세요.",
        )
    if event_type in READY_EVENT_TYPES and not has_order and short_return is not None and short_return >= 1.5:
        return _classification(
            "MISSED_OPPORTUNITY",
            confidence=confidence,
            reason="READY 계열 이벤트 뒤 주문 연결 없이 의미 있는 상승이 발생했습니다.",
            recommendation="Guard 우회가 아니라 차단 사유와 주문 시도 연결 로그를 사후 리뷰하세요.",
        )
    if event_type in WAIT_BLOCK_EVENT_TYPES and downside is not None and downside <= -1.0:
        return _classification(
            "GOOD_BLOCK",
            confidence=confidence,
            reason="차단/대기 뒤 가격이 하락해 방어 판단이 유효했습니다.",
            recommendation="같은 차단 사유는 유지 후보로 보고 반복 샘플만 더 확인하세요.",
        )
    if event_type in WAIT_BLOCK_EVENT_TYPES and short_return is not None and short_return >= 1.5:
        return _classification(
            "REVIEW_NEEDED",
            confidence=confidence,
            reason="차단/대기 뒤 가격이 강하게 상승했습니다.",
            recommendation="자동 변경 없이 데이터 지연, 시장 대기, 추격 차단 사유를 사람이 검토하세요.",
        )
    return _classification(
        "NEUTRAL",
        confidence=confidence,
        reason="후속 가격 움직임이 분류 기준에 닿지 않았습니다.",
        recommendation="누적 샘플에서만 판단하세요.",
    )


def summarize_review_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    outcome_counts = Counter(str(item.get("outcome_label") or "").upper() for item in items)
    event_counts = Counter(str(item.get("event_type") or "").upper() for item in items)
    return {
        "total_count": len(items),
        "ready_count": event_counts.get("BUY_READY_NEW", 0),
        "ready_small_count": event_counts.get("BUY_READY_SMALL_NEW", 0),
        "ready_without_order_count": sum(
            1
            for item in items
            if str(item.get("event_type") or "").upper() in READY_EVENT_TYPES and not bool((item.get("payload") or {}).get("has_order"))
        ),
        "ready_but_live_blocked_count": event_counts.get("READY_BUT_LIVE_BLOCKED", 0),
        "order_intent_created_count": event_counts.get("ORDER_INTENT_CREATED", 0),
        "virtual_order_created_count": event_counts.get("VIRTUAL_ORDER_CREATED", 0),
        "data_wait_count": event_counts.get("DATA_QUALITY_DEGRADED", 0) + event_counts.get("SNAPSHOT_STALE", 0),
        "market_wait_count": event_counts.get("MARKET_WAIT_STARTED", 0),
        "chase_blocked_count": event_counts.get("CHASE_RISK_BLOCKED", 0),
        "late_chase_temp_wait_count": event_counts.get("LATE_CHASE_TEMP_WAIT", 0),
        "observe_count": event_counts.get("READY_TO_WAIT", 0),
        "blocked_count": sum(event_counts.get(event_type, 0) for event_type in WAIT_BLOCK_EVENT_TYPES),
        "missed_opportunity_count": outcome_counts.get("MISSED_OPPORTUNITY", 0),
        "good_block_count": outcome_counts.get("GOOD_BLOCK", 0),
        "review_needed_count": outcome_counts.get("REVIEW_NEEDED", 0),
        "protected_from_chase_count": outcome_counts.get("PROTECTED_FROM_CHASE", 0),
        "protected_from_loss_count": outcome_counts.get("PROTECTED_FROM_CHASE", 0) + outcome_counts.get("GOOD_BLOCK", 0),
        "data_insufficient_count": outcome_counts.get("DATA_INSUFFICIENT", 0),
        "uncertain_block_count": outcome_counts.get("REVIEW_NEEDED", 0) + outcome_counts.get("DATA_INSUFFICIENT", 0),
        "neutral_count": outcome_counts.get("NEUTRAL", 0),
        "by_outcome_label": dict(outcome_counts),
        "by_event_type": dict(event_counts),
        "by_block_reason": build_block_reason_summary(items),
        "by_symbol": build_symbol_review_summary(items),
        "by_theme": build_theme_review_summary(items),
    }


def build_block_reason_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(str(item.get("block_reason") or "UNKNOWN") for item in items if item.get("block_reason") or item.get("outcome_label") != "NEUTRAL")
    return [{"block_reason": reason, "count": count} for reason, count in counts.most_common(20)]


def build_symbol_review_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "missed_opportunity_count": 0, "review_needed_count": 0})
    for item in items:
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        row = grouped[symbol]
        row["symbol"] = symbol
        row["stock_name"] = item.get("stock_name") or row.get("stock_name") or ""
        row["count"] += 1
        if item.get("outcome_label") == "MISSED_OPPORTUNITY":
            row["missed_opportunity_count"] += 1
        if item.get("outcome_label") == "REVIEW_NEEDED":
            row["review_needed_count"] += 1
    return sorted(grouped.values(), key=lambda row: (-row["count"], row["symbol"]))[:20]


def build_theme_review_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "missed_opportunity_count": 0, "review_needed_count": 0})
    for item in items:
        theme = str(item.get("primary_theme") or "")
        if not theme:
            continue
        row = grouped[theme]
        row["primary_theme"] = theme
        row["count"] += 1
        if item.get("outcome_label") == "MISSED_OPPORTUNITY":
            row["missed_opportunity_count"] += 1
        if item.get("outcome_label") == "REVIEW_NEEDED":
            row["review_needed_count"] += 1
    return sorted(grouped.values(), key=lambda row: (-row["count"], row["primary_theme"]))[:20]


def _ordered_candidate_keys(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "").upper()
        if event_type not in ORDER_EVENT_TYPES:
            continue
        candidate_id = _event_value(event, "candidate_instance_id")
        symbol = _event_value(event, "symbol")
        if candidate_id or symbol:
            keys.add((candidate_id, symbol))
    return keys


def _event_has_order(event: dict[str, Any], ordered_keys: set[tuple[str, str]]) -> bool:
    if str(event.get("event_type") or "").upper() in ORDER_EVENT_TYPES:
        return True
    if _payload_flag(event, "runtime_order_intent_created") or _payload_flag(event, "virtual_order_created"):
        return True
    candidate_id = _event_value(event, "candidate_instance_id")
    symbol = _event_value(event, "symbol")
    return (candidate_id, symbol) in ordered_keys or (candidate_id, "") in ordered_keys or ("", symbol) in ordered_keys


def _review_id(trade_date: str, review_scope: str, event: dict[str, Any]) -> str:
    event_id = _event_value(event, "event_id", "id")
    candidate_id = _event_value(event, "candidate_instance_id")
    symbol = _event_value(event, "symbol")
    event_type = str(event.get("event_type") or event.get("type") or "UNKNOWN").upper()
    key = event_id or candidate_id or f"{symbol}:{event.get('occurred_at') or ''}"
    return f"postmarket:{trade_date}:{review_scope}:{event_type}:{key}"


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def _event_value(event: dict[str, Any], *keys: str) -> str:
    payload = _payload(event)
    for source in (event, payload):
        for key in keys:
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _source_status(event: dict[str, Any]) -> str:
    return _event_value(event, "display_status", "gate_status", "to_status", "from_status", "source_status")


def _block_reason(event: dict[str, Any]) -> str:
    payload = _payload(event)
    for source in (event, payload):
        reason = _first_text(
            source,
            (
                "block_reason",
                "summary_reason",
                "reason",
                "support_ready_reason",
                "market_wait_reason",
                "live_guard_reject_reason",
            ),
        )
        if reason:
            return reason
    codes = _block_reason_codes(event)
    return codes[0] if codes else str(event.get("event_type") or "")


def _block_reason_codes(event: dict[str, Any]) -> list[str]:
    payload = _payload(event)
    codes: list[str] = []
    for source in (event, payload):
        for key in (
            "block_reason_codes",
            "reason_codes",
            "risk_reason_codes",
            "data_quality_flags",
            "price_location_reason_codes",
        ):
            raw = source.get(key)
            if isinstance(raw, str) and raw:
                codes.append(raw)
            elif isinstance(raw, (list, tuple, set)):
                codes.extend(str(value) for value in raw if value)
    return list(dict.fromkeys(codes))


def _extract_candles(event: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _payload(event)
    candidates = [
        payload.get("recent_candles_1m"),
        payload.get("candles"),
        payload.get("minute_candles"),
        (payload.get("selected_chart") or {}).get("candles") if isinstance(payload.get("selected_chart"), dict) else None,
        (payload.get("chart") or {}).get("candles") if isinstance(payload.get("chart"), dict) else None,
        event.get("recent_candles_1m"),
        event.get("candles"),
    ]
    chart_universe = payload.get("chart_universe")
    if isinstance(chart_universe, list):
        symbol = _event_value(event, "symbol")
        for chart in chart_universe:
            if isinstance(chart, dict) and str(chart.get("symbol") or "") == symbol:
                candidates.append(chart.get("candles"))
    for raw in candidates:
        if isinstance(raw, list):
            candles = [item for item in raw if isinstance(item, dict)]
            if candles:
                return candles
    return []


def _candle_close(candle: dict[str, Any]) -> float | None:
    return _first_number(candle, ("close", "price", "last_price"))


def _candle_time(candle: dict[str, Any]) -> str:
    return _first_text(candle, ("start_at", "time", "timestamp", "occurred_at"))


def _calculate_returns(base_price: float | None, future: dict[str, Any]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for window in RETURN_WINDOWS:
        result[f"return_{window}m_pct"] = _return_pct(base_price, future.get(f"price_{window}m"))
    result["return_close_or_last_pct"] = _return_pct(base_price, future.get("price_close_or_last"))
    return result


def _return_pct(base_price: float | None, future_price: float | None) -> float | None:
    if base_price is None or future_price is None or base_price <= 0:
        return None
    return round((future_price - base_price) / base_price * 100, 4)


def _max_return(returns: dict[str, float | None], *keys: str) -> float | None:
    values = [returns.get(key) for key in keys if returns.get(key) is not None]
    return max(values) if values else None


def _min_return(returns: dict[str, float | None], *keys: str) -> float | None:
    values = [returns.get(key) for key in keys if returns.get(key) is not None]
    return min(values) if values else None


def _confidence(event: dict[str, Any], data_quality: dict[str, Any], has_return: bool) -> str:
    if not has_return:
        return "LOW"
    base_source = str((data_quality.get("base") or {}).get("source") or "")
    future_source = str((data_quality.get("future") or {}).get("source") or "")
    if _event_value(event, "candidate_instance_id") and "direct" in base_source and "direct" in future_source:
        return "HIGH"
    if "missing" not in base_source and "missing" not in future_source:
        return "MEDIUM"
    return "LOW"


def _classification(outcome: str, *, confidence: str, reason: str, recommendation: str) -> dict[str, str]:
    return {
        "outcome_label": outcome,
        "confidence": confidence,
        "confidence_reason": reason,
        "recommendation_ko": recommendation,
    }


def _payload_flag(event: dict[str, Any], key: str) -> bool:
    value = _payload(event).get(key)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _first_number(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = source.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""
