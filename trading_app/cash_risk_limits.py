from __future__ import annotations

from typing import Any


CASH_CANDIDATE_KEYS = (
    "available_cash_krw",
    "orderable_cash_krw",
    "buying_power_krw",
    "cash_balance_krw",
    "deposit_krw",
    "d2_deposit_krw",
    "d_plus_2_deposit_krw",
)


def resolve_live_sim_cash_limits(execution: dict[str, Any], *payloads: dict[str, Any]) -> dict[str, Any]:
    """Resolve optional cash-based LIVE_SIM limits.

    The project currently does not have a single canonical deposit field from
    Kiwoom, so this accepts both persisted config and gateway heartbeat/status
    payloads. If no positive cash value is found, callers should keep their
    legacy fixed-amount limits.
    """

    execution = dict(execution or {})
    enabled = _bool(execution.get("cash_based_limits_enabled"), True)
    cash, cash_source = _resolve_available_cash(execution, *payloads)
    if not enabled or cash <= 0:
        return {
            "enabled": False,
            "available_cash_krw": 0,
            "cash_source": cash_source,
            "reason": "CASH_BASIS_UNAVAILABLE" if enabled else "CASH_BASED_LIMITS_DISABLED",
        }

    daily_turnover_pct = _ratio(execution.get("daily_turnover_limit_pct"), 0.50)
    per_order_pct = _ratio(execution.get("per_order_limit_pct"), 0.05)
    total_exposure_pct = _ratio(execution.get("total_exposure_limit_pct"), 0.25)
    per_symbol_pct = _ratio(execution.get("per_symbol_exposure_limit_pct"), 0.07)
    min_lot_exception_pct = _ratio(execution.get("min_lot_exception_pct"), 0.06)
    per_trade_risk_pct = _ratio(execution.get("per_trade_risk_limit_pct"), 0.0025)

    return {
        "enabled": True,
        "available_cash_krw": cash,
        "cash_source": cash_source,
        "daily_turnover_limit_pct": daily_turnover_pct,
        "daily_turnover_amount_krw": _limit_from_ratio(
            cash,
            daily_turnover_pct,
            absolute_cap=_int(execution.get("daily_turnover_absolute_cap_krw"), 0),
        ),
        "per_order_limit_pct": per_order_pct,
        "per_order_amount_krw": _limit_from_ratio(
            cash,
            per_order_pct,
            absolute_cap=_int(execution.get("per_order_absolute_cap_krw"), 0),
        ),
        "total_exposure_limit_pct": total_exposure_pct,
        "total_exposure_amount_krw": _limit_from_ratio(
            cash,
            total_exposure_pct,
            absolute_cap=_int(execution.get("total_exposure_absolute_cap_krw"), 0),
        ),
        "per_symbol_exposure_limit_pct": per_symbol_pct,
        "per_symbol_exposure_amount_krw": _limit_from_ratio(
            cash,
            per_symbol_pct,
            absolute_cap=_int(execution.get("per_symbol_exposure_absolute_cap_krw"), 0),
        ),
        "min_lot_exception_enabled": _bool(execution.get("min_lot_exception_enabled"), True),
        "min_lot_exception_pct": min_lot_exception_pct,
        "min_lot_exception_amount_krw": _limit_from_ratio(
            cash,
            min_lot_exception_pct,
            absolute_cap=_int(execution.get("min_lot_exception_absolute_cap_krw"), 0),
        ),
        "per_trade_risk_limit_pct": per_trade_risk_pct,
        "per_trade_risk_amount_krw": _limit_from_ratio(cash, per_trade_risk_pct),
    }


def _resolve_available_cash(execution: dict[str, Any], *payloads: dict[str, Any]) -> tuple[int, str]:
    for key in CASH_CANDIDATE_KEYS:
        value = _int(execution.get(key), 0)
        if value > 0:
            return value, f"order_execution.{key}"

    for index, payload in enumerate(payloads):
        for key in CASH_CANDIDATE_KEYS:
            value = _int((payload or {}).get(key), 0)
            if value > 0:
                return value, f"payload[{index}].{key}"
        heartbeat = dict((payload or {}).get("last_heartbeat_payload") or {})
        for key in CASH_CANDIDATE_KEYS:
            value = _int(heartbeat.get(key), 0)
            if value > 0:
                return value, f"payload[{index}].last_heartbeat_payload.{key}"
    return 0, ""


def _limit_from_ratio(cash: int, ratio: float, *, absolute_cap: int = 0) -> int:
    amount = max(0, int(cash * max(0.0, ratio)))
    if absolute_cap > 0:
        amount = min(amount, absolute_cap)
    return amount


def _ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed > 1.0:
        parsed = parsed / 100.0
    return max(0.0, parsed)


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return int(default)
