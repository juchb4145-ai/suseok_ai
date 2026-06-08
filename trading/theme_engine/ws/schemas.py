from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_subscribe_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("subscribe payload must be a JSON object")
    channels = payload.get("channels") or []
    if isinstance(channels, str):
        channels = [channels]
    top_n = _bounded_int(payload.get("top_n"), default=20, minimum=1, maximum=100)
    return {
        "action": str(payload.get("action") or ""),
        "channels": [str(item) for item in channels],
        "top_n": top_n,
        "theme_ids": _string_list(payload.get("theme_ids")),
        "stock_codes": _string_list(payload.get("stock_codes")),
    }


def build_theme_rank_payload(rank_items, *, top_n: int = 20, ts: str | None = None) -> dict[str, Any]:
    return {
        "type": "theme_rank",
        "ts": ts or _now_ts(),
        "top_n": int(top_n),
        "themes": [_rank_item_dict(item) for item in list(rank_items)[: int(top_n)]],
    }


def build_theme_detail_payload(theme_id: str, theme, members, activity=None, *, ts: str | None = None) -> dict[str, Any]:
    return {
        "type": "theme_detail",
        "ts": ts or _now_ts(),
        "theme_id": theme_id,
        "theme": _obj_dict(theme),
        "activity": _obj_dict(activity) if activity else None,
        "members": [_obj_dict(member) for member in members],
    }


def build_stock_theme_state_payload(state, *, ts: str | None = None) -> dict[str, Any]:
    return {
        "type": "stock_theme_state",
        "ts": ts or _now_ts(),
        "stock_code": state.stock_code,
        "stock_name": state.stock_name,
        "ready": bool(state.ready),
        "reason_code": state.reason_code,
        "primary_theme_id": state.primary_theme_id,
        "primary_theme_name": state.primary_theme_name,
        "primary_rank": state.primary_rank,
        "membership_score": state.membership_score,
        "leadership_role": state.leadership_role,
        "themes": [_obj_dict(theme) for theme in state.themes],
    }


def build_heartbeat_payload(*, ts: str | None = None) -> dict[str, Any]:
    return {"type": "heartbeat", "ts": ts or _now_ts()}


def build_runtime_health_payload(health: dict[str, Any], *, ts: str | None = None) -> dict[str, Any]:
    payload = {"type": "runtime_health", "ts": ts or _now_ts()}
    payload.update(dict(health or {}))
    return payload


def build_error_payload(message: str, *, code: str = "ERROR", ts: str | None = None) -> dict[str, Any]:
    return {"type": "error", "ts": ts or _now_ts(), "code": code, "message": message}


def _rank_item_dict(item) -> dict[str, Any]:
    details = dict(getattr(item, "details", {}) or {})
    return {
        "rank": item.rank,
        "theme_id": item.theme_id,
        "theme_name": item.theme_name,
        "theme_score": item.theme_score,
        "status": _value(getattr(item, "status", "")),
        "trade_eligible": bool(getattr(item, "trade_eligible", False)),
        "rank_delta_1m": item.rank_delta_1m,
        "rank_delta_5m": item.rank_delta_5m,
        "weighted_return_pct": item.weighted_return_pct,
        "turnover": item.turnover,
        "turnover_strength": item.turnover_strength,
        "breadth": item.breadth,
        "rising_count": item.rising_count,
        "total_count": item.total_count,
        "leader_code": item.leader_code,
        "leader_name": item.leader_name,
        "leader_return_pct": item.leader_return_pct,
        "leader_gap": item.leader_gap,
        "top3_concentration": item.top3_concentration,
        "reason_codes": list(details.get("reason_codes") or []),
        "top_stocks": list(details.get("top_stocks") or [])[:5],
        "snapshot_quality": dict(details.get("snapshot_quality") or {}),
    }


def _obj_dict(obj) -> dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "__dict__"):
        result = {}
        for key, value in obj.__dict__.items():
            if key.startswith("_"):
                continue
            if hasattr(value, "value"):
                value = value.value
            elif isinstance(value, list):
                value = [_obj_dict(item) if hasattr(item, "__dict__") else item for item in value]
            elif hasattr(value, "__dict__"):
                value = _obj_dict(value)
            result[key] = value
        return result
    return dict(obj)


def _value(value) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _now_ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
