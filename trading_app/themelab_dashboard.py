from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from storage.db import TradingDatabase
from trading.theme_engine.repository import ThemeEngineRepository


GATE_ORDER = {"READY": 0, "READY_SMALL": 1, "WAIT": 2, "OBSERVE": 3, "BLOCKED": 4}
ROLE_ORDER = {"LEADER": 0, "CO_LEADER": 1, "FOLLOWER": 2, "LATE_LAGGARD": 3, "WEAK_MEMBER": 4, "OVERHEATED": 5}
DISPLAY_WAIT_ORDER = {
    "LATE_CHASE_TEMP_WAIT": 0,
    "WAIT_MARKET_CONFIRMATION_PENDING": 1,
    "WAIT_MARKET_RECOVERY_PENDING": 1,
    "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK": 1,
    "WAIT_CANDIDATE_MARKET_RISK_OFF": 1,
    "WAIT_CANDIDATE_MARKET_WEAK": 1,
    "WAIT_DATA_SUPPORT_NOT_READY": 2,
    "WAIT_DATA_LATEST_TICK_STALE": 2,
}


def build_theme_lab_dashboard_snapshot(db: TradingDatabase) -> dict[str, Any]:
    raw = db.latest_theme_lab_flow_result()
    if not raw:
        return _empty_snapshot()

    themes = _as_list(raw.get("theme_rankings") or raw.get("theme_condition_snapshots"))
    watchset = _sorted_watchset(_as_list(raw.get("watchset_snapshots")))
    condition_counts = _condition_theme_counts(db, raw)
    data_quality = _data_quality(raw, watchset)
    entry_candidates = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    chart_universe = _chart_universe(themes, watchset, entry_candidates)
    selected = _select_chart(chart_universe, watchset)
    selected_watch = next((item for item in watchset if item.get("symbol") == selected.get("symbol")), {})

    ranked_themes = _ranked_theme_rows(themes, condition_counts)
    summary = _summary(ranked_themes, watchset, entry_candidates, data_quality)

    return {
        "available": True,
        "source": "theme_lab_flow_snapshots",
        "created_at": raw.get("created_at", ""),
        "calculated_at": raw.get("calculated_at", ""),
        "last_updated_at": _now_time(),
        "market": _market(raw.get("market_status") or {}),
        "condition_statuses": _condition_statuses(db),
        "data_quality": data_quality,
        "ranked_themes": ranked_themes[:30],
        "watchset": [_watch_row(item) for item in watchset],
        "entry_candidates": [_entry_row(item, index) for index, item in enumerate(entry_candidates, start=1)],
        "chart_universe": chart_universe,
        "selected_chart": selected,
        "gate_detail": _gate_detail(selected_watch),
        "summary": summary,
    }


def _empty_snapshot() -> dict[str, Any]:
    return {
        "available": False,
        "source": "theme_lab_flow_snapshots",
        "created_at": "",
        "calculated_at": "",
        "last_updated_at": _now_time(),
        "market": {
            "market_status": "WAITING",
            "kospi_return_pct": None,
            "kosdaq_return_pct": None,
            "market_strong_count": 0,
            "market_leader_count": 0,
            "sides": [
                _empty_market_side("KOSPI"),
                _empty_market_side("KOSDAQ"),
            ],
        },
        "condition_statuses": [],
        "data_quality": {
            "status": "BROKEN",
            "message": "ThemeLabFlow 결과가 아직 없습니다.",
            "vi_status_supported": False,
            "watchset_size": 0,
        },
        "ranked_themes": [],
        "watchset": [],
        "entry_candidates": [],
        "chart_universe": _index_chart_items(),
        "selected_chart": {"symbol": "KOSDAQ", "name": "KOSDAQ", "type": "index", "chart_data_status": "NO_CANDLE_DATA"},
        "gate_detail": {"gate_status": "OBSERVE", "summary_message": "선택된 WatchSet 종목이 없습니다."},
        "summary": _empty_summary(),
    }


def _market(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_status": _value(raw.get("market_status") or raw.get("status") or "UNKNOWN"),
        "kospi_return_pct": raw.get("kospi_return_pct"),
        "kosdaq_return_pct": raw.get("kosdaq_return_pct"),
        "market_strong_count": int(raw.get("market_strong_count") or 0),
        "market_leader_count": int(raw.get("market_leader_count") or 0),
        "advancers": int(raw.get("advancers") or 0),
        "decliners": int(raw.get("decliners") or 0),
        "data_quality_flags": list(raw.get("data_quality_flags") or []),
        "sides": [_market_side(raw, "KOSPI"), _market_side(raw, "KOSDAQ")],
    }


def _market_side(raw: dict[str, Any], side: str) -> dict[str, Any]:
    key = side.lower()
    side_statuses = raw.get("side_statuses") if isinstance(raw.get("side_statuses"), dict) else {}
    side_data = dict(side_statuses.get(side) or side_statuses.get(side.upper()) or side_statuses.get(key) or {})
    return {
        "side": side,
        "status": _value(
            side_data.get("status")
            or raw.get(f"{key}_confirmed_status")
            or raw.get(f"{key}_status")
            or raw.get("market_status")
            or raw.get("status")
            or "UNKNOWN"
        ),
        "index_return_pct": _first_not_none(
            side_data.get("index_return_pct"),
            raw.get(f"{key}_index_return_pct"),
            raw.get(f"{key}_return_pct"),
        ),
        "breadth_pct": _first_not_none(side_data.get("breadth_pct"), raw.get(f"{key}_breadth_pct")),
        "breadth_ready": bool(_first_not_none(side_data.get("breadth_ready"), raw.get(f"{key}_breadth_ready"), False)),
        "breadth_sample_count": int(_first_not_none(side_data.get("breadth_sample_count"), raw.get(f"{key}_breadth_sample_count"), 0) or 0),
        "breadth_source": _value(side_data.get("breadth_source") or raw.get(f"{key}_breadth_source") or ""),
        "breadth_trust_level": _value(side_data.get("breadth_trust_level") or raw.get(f"{key}_breadth_trust_level") or "UNKNOWN"),
        "breadth_gate_usable": bool(_first_not_none(side_data.get("breadth_gate_usable"), raw.get(f"{key}_breadth_gate_usable"), False)),
        "breadth_diagnostic_only": bool(_first_not_none(side_data.get("breadth_diagnostic_only"), raw.get(f"{key}_breadth_diagnostic_only"), False)),
        "valid_quote_ratio": _first_not_none(side_data.get("valid_quote_ratio"), raw.get(f"{key}_valid_quote_ratio")),
        "turnover_weighted_return_pct": _first_not_none(
            side_data.get("turnover_weighted_return_pct"),
            raw.get(f"{key}_turnover_weighted_return_pct"),
        ),
        "reason_codes": list(side_data.get("reason_codes") or []),
        "data_quality_flags": list(side_data.get("data_quality_flags") or []),
    }


def _empty_market_side(side: str) -> dict[str, Any]:
    return {
        "side": side,
        "status": "WAITING",
        "index_return_pct": None,
        "breadth_pct": None,
        "breadth_ready": False,
        "breadth_sample_count": 0,
        "breadth_source": "",
        "breadth_trust_level": "UNKNOWN",
        "breadth_gate_usable": False,
        "breadth_diagnostic_only": False,
        "valid_quote_ratio": None,
        "turnover_weighted_return_pct": None,
        "reason_codes": [],
        "data_quality_flags": [],
    }


def _summary(
    ranked_themes: list[dict[str, Any]],
    watchset: list[dict[str, Any]],
    entry_candidates: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    gates = Counter(str(item.get("gate_status") or "UNKNOWN") for item in watchset)
    displays = Counter(str(item.get("display_status") or item.get("gate_status") or "UNKNOWN") for item in watchset)
    theme_statuses = Counter(_theme_status_bucket(item.get("theme_status")) for item in ranked_themes)
    leader_count = sum(1 for item in watchset if item.get("stock_role") == "LEADER")
    co_leader_count = sum(1 for item in watchset if item.get("stock_role") == "CO_LEADER")
    late_laggard_count = sum(1 for item in watchset if item.get("stock_role") == "LATE_LAGGARD")
    live_guard_passed = sum(1 for item in watchset if item.get("live_order_guard_passed"))
    ready_like = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    live_guard_blocked = sum(1 for item in ready_like if not item.get("live_order_guard_passed"))
    market_pending_count = sum(1 for item in watchset if _is_market_pending(item))
    data_not_ready_count = sum(1 for item in watchset if _is_data_not_ready(item))
    top_theme = ranked_themes[0] if ranked_themes else {}
    status, message = _operation_status_message(
        ready_count=gates.get("READY", 0),
        ready_small_count=gates.get("READY_SMALL", 0),
        market_pending_count=market_pending_count,
        data_not_ready_count=data_not_ready_count,
        late_chase_wait_count=displays.get("LATE_CHASE_TEMP_WAIT", 0),
        chase_risk_blocked_count=displays.get("CHASE_RISK_BLOCKED", 0),
        live_guard_passed_count=live_guard_passed,
        live_guard_blocked_count=live_guard_blocked,
        order_candidate_count=len(entry_candidates),
        data_quality_status=str(data_quality.get("status") or "UNKNOWN"),
        watchset_size=len(watchset),
    )
    return {
        "theme_count": len(ranked_themes),
        "watchset_size": len(watchset),
        "ready_count": gates.get("READY", 0),
        "ready_small_count": gates.get("READY_SMALL", 0),
        "wait_count": gates.get("WAIT", 0),
        "observe_count": gates.get("OBSERVE", 0),
        "blocked_count": gates.get("BLOCKED", 0),
        "late_chase_wait_count": displays.get("LATE_CHASE_TEMP_WAIT", 0),
        "chase_risk_blocked_count": displays.get("CHASE_RISK_BLOCKED", 0),
        "market_pending_count": market_pending_count,
        "data_not_ready_count": data_not_ready_count,
        "diagnostic_only_count": sum(1 for item in watchset if item.get("diagnostic_only")),
        "submittable_count": sum(1 for item in watchset if item.get("submittable")),
        "runtime_order_intent_created_count": sum(1 for item in watchset if item.get("runtime_order_intent_created")),
        "virtual_order_created_count": sum(1 for item in watchset if item.get("virtual_order_created")),
        "live_order_enabled": any(item.get("live_order_enabled") for item in watchset),
        "live_guard_passed_count": live_guard_passed,
        "live_guard_blocked_count": live_guard_blocked,
        "leader_count": leader_count,
        "co_leader_count": co_leader_count,
        "late_laggard_count": late_laggard_count,
        "order_candidate_count": len(entry_candidates),
        "theme_status_counts": dict(theme_statuses),
        "display_status_counts": dict(displays),
        "top_theme_name": top_theme.get("theme_name", ""),
        "top_theme_status": top_theme.get("theme_status", ""),
        "top_theme_score": top_theme.get("condition_score", 0),
        "top_leader_name": top_theme.get("top_leader_name", ""),
        "top_leader_symbol": top_theme.get("top_leader_symbol", ""),
        "top_leader_turnover_krw": top_theme.get("top_leader_turnover_krw", 0),
        "operation_status": status,
        "operation_message_ko": message,
    }


def _empty_summary() -> dict[str, Any]:
    status, message = _operation_status_message(
        ready_count=0,
        ready_small_count=0,
        market_pending_count=0,
        data_not_ready_count=0,
        late_chase_wait_count=0,
        chase_risk_blocked_count=0,
        live_guard_passed_count=0,
        live_guard_blocked_count=0,
        order_candidate_count=0,
        data_quality_status="BROKEN",
        watchset_size=0,
    )
    return {
        "theme_count": 0,
        "watchset_size": 0,
        "ready_count": 0,
        "ready_small_count": 0,
        "wait_count": 0,
        "observe_count": 0,
        "blocked_count": 0,
        "late_chase_wait_count": 0,
        "chase_risk_blocked_count": 0,
        "market_pending_count": 0,
        "data_not_ready_count": 0,
        "diagnostic_only_count": 0,
        "submittable_count": 0,
        "runtime_order_intent_created_count": 0,
        "virtual_order_created_count": 0,
        "live_order_enabled": False,
        "live_guard_passed_count": 0,
        "live_guard_blocked_count": 0,
        "leader_count": 0,
        "co_leader_count": 0,
        "late_laggard_count": 0,
        "order_candidate_count": 0,
        "theme_status_counts": {},
        "display_status_counts": {},
        "top_theme_name": "",
        "top_theme_status": "",
        "top_theme_score": 0,
        "top_leader_name": "",
        "top_leader_symbol": "",
        "top_leader_turnover_krw": 0,
        "operation_status": status,
        "operation_message_ko": message,
    }


def _theme_status_bucket(value: Any) -> str:
    text = _value(value).upper()
    if "LEADING" in text:
        return "LEADING"
    if "ACTIVE" in text:
        return "ACTIVE"
    if "WATCH" in text:
        return "WATCH"
    if "WEAK" in text:
        return "WEAK"
    return text or "UNKNOWN"


def _is_market_pending(item: dict[str, Any]) -> bool:
    display = str(item.get("display_status") or "")
    return (
        display.startswith("WAIT_MARKET")
        or display.startswith("WAIT_CANDIDATE_MARKET")
        or bool(item.get("market_confirmation_pending"))
        or bool(item.get("market_recovery_pending"))
    )


def _is_data_not_ready(item: dict[str, Any]) -> bool:
    display = str(item.get("display_status") or "")
    flags = set(item.get("data_quality_flags") or []) | set(item.get("price_location_data_quality_flags") or [])
    return (
        display.startswith("WAIT_DATA")
        or bool(item.get("diagnostic_only"))
        or item.get("latest_tick_ready") is False
        or bool(item.get("support_ready_reason"))
        or any(str(flag).startswith("MISSING") or str(flag).startswith("STALE") for flag in flags)
    )


def _operation_status_message(
    *,
    ready_count: int,
    ready_small_count: int,
    market_pending_count: int,
    data_not_ready_count: int,
    late_chase_wait_count: int,
    chase_risk_blocked_count: int,
    live_guard_passed_count: int,
    live_guard_blocked_count: int,
    order_candidate_count: int,
    data_quality_status: str,
    watchset_size: int,
) -> tuple[str, str]:
    data_status = data_quality_status.upper()
    ready_like = ready_count + ready_small_count
    if watchset_size == 0:
        return "SNAPSHOT_UNAVAILABLE", "ThemeLabFlow 결과 대기 중입니다."
    if ready_count > 0 and live_guard_passed_count > 0 and data_status not in {"DEGRADED", "BROKEN"}:
        return "READY_TO_TRADE", "READY 후보가 있고 데이터 품질이 정상입니다."
    if ready_like > 0 and live_guard_passed_count == 0 and live_guard_blocked_count > 0:
        return "READY_BUT_LIVE_BLOCKED", "READY 후보는 있으나 LIVE Guard 통과 후보가 없습니다."
    if data_status in {"DEGRADED", "BROKEN"} or data_not_ready_count >= max(1, ready_like + market_pending_count):
        return "WAIT_DATA_QUALITY", "VWAP/지지선/틱 데이터 부족으로 진단 전용 후보가 많습니다."
    if market_pending_count > 0:
        return "WAIT_MARKET_CONFIRMATION", "시장 확인 대기 후보가 많아 관찰 우선입니다."
    if chase_risk_blocked_count > 0 or late_chase_wait_count >= max(1, ready_like):
        return "RISK_BLOCKED", "추격매수 차단 후보가 많아 신규 진입 대기입니다."
    if order_candidate_count == 0:
        if ready_like == 0 and watchset_size > 0:
            return "OBSERVE_ONLY", "현재 READY 후보가 없어 관찰 우선입니다."
        return "NO_SIGNAL", "현재 주문 후보가 없습니다."
    return "OBSERVE_ONLY", "장중 매수 가능 후보를 관찰 중입니다."


def _condition_statuses(db: TradingDatabase) -> list[dict[str, Any]]:
    defaults = {
        "theme_lab_alive": "테마랩_생존_-1",
        "theme_lab_strong": "테마랩_강세_3",
        "theme_lab_leader": "테마랩_주도_5",
    }
    rows = []
    try:
        profiles = db.list_condition_profiles(enabled=None)
    except Exception:
        profiles = []
    by_purpose = {profile.purpose: profile for profile in profiles}
    for purpose, default_name in defaults.items():
        profile = by_purpose.get(purpose)
        rows.append(
            {
                "condition_name": profile.condition_name if profile else default_name,
                "purpose": purpose,
                "resolved_index": profile.last_resolved_index if profile and profile.last_resolved_index is not None else "UNKNOWN",
                "registered": bool(profile and profile.last_resolved_index is not None),
                "screen_no": "",
                "include_count": 0,
                "remove_count": 0,
                "last_event_at": "",
                "warning": "" if profile else "CONDITION_PROFILE_UNRESOLVED",
            }
        )
    return rows


def _data_quality(raw: dict[str, Any], watchset: list[dict[str, Any]]) -> dict[str, Any]:
    data = dict(raw.get("data_quality") or {})
    price_flags = [flag for item in watchset for flag in item.get("data_quality_flags", []) + item.get("price_location_data_quality_flags", [])]
    missing_vwap = int(_first_not_none(data.get("vwap_missing_count"), price_flags.count("MISSING_VWAP")) or 0)
    missing_session_high = int(_first_not_none(data.get("session_high_missing_count"), price_flags.count("MISSING_SESSION_HIGH")) or 0)
    missing_prev_close = int(_first_not_none(data.get("prev_close_missing_count"), price_flags.count("MISSING_PREV_CLOSE")) or 0)
    candle_missing = int(_first_not_none(data.get("candle_missing_count"), len(watchset)) or 0)
    quote_stale = int(_first_not_none(data.get("quote_stale_count"), 0) or 0)
    status = str(data.get("status") or "OK")
    if candle_missing or quote_stale >= 5:
        status = "DEGRADED"
    elif any([missing_vwap, missing_session_high, missing_prev_close, quote_stale]):
        status = "WARNING"
    return {
        "status": status,
        "quote_stale_count": quote_stale,
        "prev_close_missing_count": missing_prev_close,
        "candle_missing_count": candle_missing,
        "vwap_missing_count": missing_vwap,
        "session_high_missing_count": missing_session_high,
        "vi_status_supported": bool(data.get("vi_status_supported", False)),
        "theme_mapping_missing_count": int(data.get("theme_mapping_missing_count") or 0),
        "watchset_size": len(watchset),
        "realtime_subscription_count": int(data.get("realtime_subscription_count") or 0),
        "realtime_subscription_limit": int(data.get("realtime_subscription_limit") or 0),
        "message": _data_quality_message(status, candle_missing, missing_vwap),
    }


def _theme_row(item: dict[str, Any], rank: int, condition_counts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    overlay = dict((condition_counts or {}).get(str(item.get("theme_id") or ""), {}) or {})
    eligible_total = int(item.get("eligible_total_members") or 0)
    price_alive_count = int(item.get("alive_count") or 0)
    price_strong_count = int(item.get("strong_count") or 0)
    price_leader_count = int(item.get("leader_count") or 0)
    condition_alive_count = int(overlay.get("alive") or 0)
    condition_strong_count = int(overlay.get("strong") or 0)
    condition_leader_count = int(overlay.get("leader") or 0)
    member_hits = [dict(hit) for hit in item.get("member_hits") or [] if isinstance(hit, dict)]
    leader = _leader_candidate(member_hits, overlay)
    quality = _theme_member_quality(member_hits, eligible_total)
    alive_count = max(price_alive_count, condition_alive_count)
    strong_count = max(price_strong_count, condition_strong_count)
    leader_count = max(price_leader_count, condition_leader_count)
    row = {
        "rank": rank,
        "theme_id": item.get("theme_id") or item.get("theme_name") or "",
        "theme_name": item.get("theme_name") or item.get("theme_id") or "-",
        "theme_status": _value(item.get("theme_status") or "UNKNOWN"),
        "eligible_total_members": eligible_total,
        "alive_count": alive_count,
        "alive_ratio": _ratio(alive_count, eligible_total),
        "strong_count": strong_count,
        "strong_ratio": _ratio(strong_count, eligible_total),
        "leader_count": leader_count,
        "leader_ratio": _ratio(leader_count, eligible_total),
        "price_alive_count": price_alive_count,
        "price_alive_ratio": float(item.get("alive_ratio") or 0),
        "price_strong_count": price_strong_count,
        "price_strong_ratio": float(item.get("strong_ratio") or 0),
        "price_leader_count": price_leader_count,
        "price_leader_ratio": float(item.get("leader_ratio") or 0),
        "condition_alive_count": condition_alive_count,
        "condition_strong_count": condition_strong_count,
        "condition_leader_count": condition_leader_count,
        "condition_signal_source": "condition_events" if any([condition_alive_count, condition_strong_count, condition_leader_count]) else "",
        "condition_score": float(item.get("condition_score") or 0),
        "theme_turnover_krw": float(item.get("theme_turnover_krw") or 0),
        "turnover_label": "수신대금",
        "priced_member_count": quality["priced_member_count"],
        "turnover_member_count": quality["turnover_member_count"],
        "member_data_coverage_label": quality["coverage_label"],
        "top_leader_symbol": leader["symbol"] or item.get("top_leader_symbol") or "",
        "top_leader_name": leader["name"] or item.get("top_leader_name") or "",
        "top_leader_turnover_krw": leader["turnover_krw"],
        "top_leader_return_pct": leader["return_pct"],
        "top_leader_source": leader["source"],
        "data_quality_flags": list(item.get("data_quality_flags") or []),
    }
    row["has_live_price_signal"] = _theme_has_live_price_signal(row)
    row["quality_label"] = _theme_quality_label(row)
    return row


def _ranked_theme_rows(themes: list[dict[str, Any]], condition_counts: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = [_theme_row(item, index, condition_counts) for index, item in enumerate(themes, start=1)]
    rows.sort(key=_theme_sort_key)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def _theme_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    no_live_signal = not row.get("has_live_price_signal")
    return (
        1 if no_live_signal else 0,
        -float(row.get("condition_score") or 0),
        -int(row.get("strong_count") or 0),
        -int(row.get("alive_count") or 0),
        -float(row.get("theme_turnover_krw") or 0),
        str(row.get("theme_name") or ""),
    )


def _theme_has_live_price_signal(row: dict[str, Any]) -> bool:
    return any(
        [
            int(row.get("alive_count") or 0) > 0,
            int(row.get("strong_count") or 0) > 0,
            int(row.get("leader_count") or 0) > 0,
            float(row.get("theme_turnover_krw") or 0) > 0,
        ]
    )


def _theme_quality_label(row: dict[str, Any]) -> str:
    flags = set(row.get("data_quality_flags") or [])
    labels = []
    if "MISSING_CURRENT_PRICE" in flags:
        labels.append("일부 구성종목 데이터 대기" if row.get("has_live_price_signal") else "현재가 미수신")
    if "MISSING_PREV_CLOSE" in flags:
        labels.append("전일종가 일부 누락")
    if "EXCLUSION_METADATA_FALLBACK" in flags:
        if not row.get("has_live_price_signal"):
            labels.append("제외 메타 보조값 사용")
    return ", ".join(labels)


def _theme_member_quality(member_hits: list[dict[str, Any]], eligible_total: int) -> dict[str, Any]:
    priced = 0
    turnover = 0
    for hit in member_hits:
        if hit.get("excluded"):
            continue
        flags = set(hit.get("data_quality_flags") or [])
        if "MISSING_CURRENT_PRICE" not in flags and hit.get("return_pct") is not None:
            priced += 1
        if float(hit.get("turnover_krw") or hit.get("turnover") or 0) > 0:
            turnover += 1
    total = eligible_total or len([hit for hit in member_hits if not hit.get("excluded")])
    return {
        "priced_member_count": priced,
        "turnover_member_count": turnover,
        "coverage_label": f"{priced}/{total} 종목 수신" if total else "0/0 종목 수신",
    }


def _leader_candidate(member_hits: list[dict[str, Any]], overlay: dict[str, Any]) -> dict[str, Any]:
    code_levels: dict[str, int] = {}
    for code in overlay.get("alive_codes") or []:
        code_levels[str(code)] = max(code_levels.get(str(code), 0), 1)
    for code in overlay.get("strong_codes") or []:
        code_levels[str(code)] = max(code_levels.get(str(code), 0), 2)
    for code in overlay.get("leader_codes") or []:
        code_levels[str(code)] = max(code_levels.get(str(code), 0), 3)

    candidates = []
    for hit in member_hits:
        if hit.get("excluded"):
            continue
        symbol = str(hit.get("symbol") or "").strip()
        if not symbol:
            continue
        price_level = 3 if hit.get("leader_hit") else 2 if hit.get("strong_hit") else 1 if hit.get("alive_hit") else 0
        condition_level = code_levels.get(symbol, 0)
        level = max(price_level, condition_level)
        if level <= 0:
            continue
        turnover = float(hit.get("turnover_krw") or hit.get("turnover") or 0)
        return_pct = float(hit.get("return_pct") or 0)
        candidates.append(
            {
                "symbol": symbol,
                "name": str(hit.get("name") or ""),
                "turnover_krw": turnover,
                "return_pct": return_pct,
                "level": level,
                "source": "조건식" if condition_level >= price_level and condition_level > 0 else "가격",
            }
        )
    if not candidates:
        return {"symbol": "", "name": "", "turnover_krw": 0.0, "return_pct": None, "source": ""}
    candidates.sort(key=lambda item: (item["level"], item["turnover_krw"], item["return_pct"]), reverse=True)
    return candidates[0]


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total > 0 else 0.0


def _condition_theme_counts(db: TradingDatabase, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    trade_date = _trade_date(raw)
    if not trade_date:
        return {}
    try:
        candidates = db.list_candidates(trade_date=trade_date)
        profiles = db.list_condition_profiles(enabled=None)
        memberships = ThemeEngineRepository(db).list_current_memberships(active=True)
    except Exception:
        return {}
    purpose_by_name = {profile.condition_name: profile.purpose for profile in profiles}
    themes_by_code: dict[str, set[str]] = defaultdict(set)
    for membership in memberships:
        code = str(getattr(membership, "stock_code", "") or "").strip()
        theme_id = str(getattr(membership, "theme_id", "") or "").strip()
        if code and theme_id:
            themes_by_code[code].add(theme_id)
    codes_by_theme_level: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"alive": set(), "strong": set(), "leader": set()})
    for candidate in candidates:
        if _state_value(getattr(candidate, "state", "")).upper() in {"EXPIRED", "REMOVED", "CANCELLED"}:
            continue
        code = str(getattr(candidate, "code", "") or "").strip()
        if not code:
            continue
        levels = _candidate_condition_levels(candidate, purpose_by_name)
        if not levels:
            continue
        for theme_id in themes_by_code.get(code, set()):
            for level in levels:
                codes_by_theme_level[theme_id][level].add(code)
    results: dict[str, dict[str, Any]] = {}
    for theme_id, levels in codes_by_theme_level.items():
        row: dict[str, Any] = {}
        for level, codes in levels.items():
            row[level] = len(codes)
            row[f"{level}_codes"] = sorted(codes)
        results[theme_id] = row
    return results


def _candidate_condition_levels(candidate: Any, purpose_by_name: dict[str, str]) -> set[str]:
    levels: set[str] = set()
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    purposes = {str(value or "") for value in dict(metadata.get("condition_purposes", {}) or {}).values()}
    names = {str(name or "") for name in getattr(candidate, "condition_names", []) or []}
    purposes.update(str(purpose_by_name.get(name, "") or "") for name in names)
    text = " ".join(sorted(names))
    if "theme_lab_alive" in purposes or "생존" in text:
        levels.add("alive")
    if "theme_lab_strong" in purposes or "강세" in text:
        levels.update({"alive", "strong"})
    if "theme_lab_leader" in purposes or "주도" in text:
        levels.update({"alive", "strong", "leader"})
    return levels


def _trade_date(raw: dict[str, Any]) -> str:
    for key in ("calculated_at", "created_at"):
        text = str(raw.get(key) or "").strip()
        if len(text) >= 10:
            return text[:10]
    return datetime.now().date().isoformat()


def _state_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _display_status(item: dict[str, Any], gate: str) -> str:
    existing = str(item.get("display_status") or item.get("normalized_status") or "").strip()
    if existing:
        return existing
    reasons = {str(reason or "") for reason in item.get("reason_codes") or item.get("risk_reason_codes") or []}
    market_reasons = {str(reason or "") for reason in item.get("market_side_reason_codes") or []}
    all_reasons = reasons | market_reasons
    market_status = str(item.get("candidate_market_confirmed_status") or item.get("candidate_market_status") or "")
    if bool(item.get("chase_risk")) or "CHASE_RISK" in all_reasons:
        return "CHASE_RISK_BLOCKED"
    if str(item.get("late_chase_level") or "") == "soft_block" or "LATE_CHASE_TEMP_WAIT" in all_reasons:
        return "LATE_CHASE_TEMP_WAIT"
    if "MARKET_CONFIRMATION_STATE_CONSERVATIVE_FALLBACK" in all_reasons:
        return "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK"
    if bool(item.get("candidate_market_recovery_pending")):
        return "WAIT_MARKET_RECOVERY_PENDING"
    if bool(item.get("candidate_market_confirmation_pending")):
        return "WAIT_MARKET_CONFIRMATION_PENDING"
    if market_status == "RISK_OFF":
        return "WAIT_CANDIDATE_MARKET_RISK_OFF"
    if market_status == "WEAK":
        return "WAIT_CANDIDATE_MARKET_WEAK"
    support_reason = str(item.get("support_ready_reason") or item.get("selected_support_ready_reason") or "")
    if support_reason:
        return "WAIT_DATA_SUPPORT_NOT_READY"
    if item.get("latest_tick_ready") is False:
        return "WAIT_DATA_LATEST_TICK_STALE"
    return gate


def _watch_row(item: dict[str, Any]) -> dict[str, Any]:
    gate = _value(item.get("final_gate_status") or item.get("gate_status") or "OBSERVE")
    display_status = _display_status(item, gate)
    candidate_market = item.get("candidate_market") or "UNKNOWN"
    return {
        "gate_status": gate,
        "final_status": gate,
        "display_status": display_status,
        "normalized_status": display_status,
        "symbol": item.get("symbol") or "",
        "code": item.get("code") or item.get("symbol") or "",
        "stock_name": item.get("name") or item.get("stock_name") or "",
        "name": item.get("name") or item.get("stock_name") or "",
        "candidate_instance_id": item.get("candidate_instance_id", ""),
        "candidate_market": candidate_market,
        "candidate_market_source": item.get("candidate_market_source", ""),
        "primary_theme": item.get("primary_theme") or "",
        "theme_name": item.get("theme_name") or item.get("primary_theme") or "",
        "theme_score": item.get("theme_score", item.get("condition_score")),
        "stock_role": _value(item.get("stock_role") or "UNKNOWN"),
        "strategy_eligible": gate in {"READY", "READY_SMALL"},
        "order_eligibility": item.get("order_eligibility", ""),
        "entry_profile": item.get("profile", ""),
        "ready_type": item.get("ready_type", ""),
        "return_pct": item.get("return_pct"),
        "turnover_krw": item.get("turnover_krw"),
        "condition_level": int(item.get("condition_level") or 0),
        "price_location_status": _value(item.get("price_location_status") or item.get("price_location") or "UNKNOWN"),
        "price_location": _value(item.get("price_location_status") or item.get("price_location") or "UNKNOWN"),
        "price_location_score": float(item.get("price_location_score") or 0),
        "price_location_block_reason": item.get("price_location_block_reason", ""),
        "risk_level": _value(item.get("risk_level") or "UNKNOWN"),
        "chase_risk": bool(item.get("chase_risk")),
        "chase_risk_reason": item.get("chase_risk_reason", ""),
        "late_chase_level": item.get("late_chase_level", ""),
        "late_chase_score": item.get("late_chase_score"),
        "late_chase_block_type": item.get("late_chase_block_type", ""),
        "late_chase_temp_wait": bool(item.get("late_chase_temp_wait") or display_status == "LATE_CHASE_TEMP_WAIT"),
        "late_chase_recoverable": bool(item.get("late_chase_recoverable")),
        "late_chase_recheck_after_sec": int(item.get("late_chase_recheck_after_sec") or 0),
        "support_source": item.get("selected_support_source") or item.get("nearest_support") or item.get("support_source") or "",
        "support_price": item.get("selected_support_price") or item.get("nearest_support_price") or item.get("support_price"),
        "support_ready": bool(item.get("support_ready", item.get("selected_support_ready", False))),
        "support_ready_reason": item.get("support_ready_reason") or item.get("selected_support_ready_reason") or "",
        "latest_tick_ready": bool(item.get("latest_tick_ready", True)),
        "latest_tick_age_sec": item.get("latest_tick_age_sec"),
        "base_line_120_ready": bool(item.get("base_line_120_ready", False)),
        "base_line_120_candle_count": int(item.get("base_line_120_candle_count") or 0),
        "vwap_ready": bool(item.get("vwap_ready", False)),
        "recent_support_ready": bool(item.get("recent_support_ready", False)),
        "market_raw_status": item.get("candidate_market_raw_status") or item.get("market_raw_status", ""),
        "market_confirmed_status": item.get("candidate_market_confirmed_status") or item.get("candidate_market_status") or item.get("market_confirmed_status", ""),
        "kospi_market_status": item.get("kospi_market_status", ""),
        "kosdaq_market_status": item.get("kosdaq_market_status", ""),
        "market_previous_confirmed_status": item.get("market_previous_confirmed_status", ""),
        "market_confirmation_pending": bool(item.get("candidate_market_confirmation_pending", item.get("market_confirmation_pending", False))),
        "market_recovery_pending": bool(item.get("candidate_market_recovery_pending", item.get("market_recovery_pending", False))),
        "market_weak_consecutive_cycles": int(item.get("market_side_weak_consecutive_cycles", item.get("market_weak_consecutive_cycles", 0)) or 0),
        "market_risk_off_consecutive_cycles": int(item.get("market_side_risk_off_consecutive_cycles", item.get("market_risk_off_consecutive_cycles", 0)) or 0),
        "market_healthy_consecutive_cycles": int(item.get("market_side_healthy_consecutive_cycles", item.get("market_healthy_consecutive_cycles", 0)) or 0),
        "market_wait_reason": item.get("market_wait_reason")
        or (display_status if display_status.startswith("WAIT_MARKET") or display_status.startswith("WAIT_CANDIDATE_MARKET") else ""),
        "market_wait_started_at": item.get("market_side_wait_started_at") or item.get("market_wait_started_at", ""),
        "market_wait_cycle_id": item.get("market_side_cycle_id") or item.get("market_wait_cycle_id", ""),
        "market_wait_recheck_after_sec": int(item.get("market_side_recheck_after_sec", item.get("market_wait_recheck_after_sec", 0)) or 0),
        "market_wait_recovered_at": item.get("market_side_recovered_at") or item.get("market_wait_recovered_at", ""),
        "market_wait_cycles_to_recover": int(item.get("market_side_cycles_to_recover", item.get("market_wait_cycles_to_recover", 0)) or 0),
        "market_confirmation_state_source": item.get("market_confirmation_state_source", ""),
        "market_confirmation_state_restored": bool(item.get("market_confirmation_state_restored")),
        "market_confirmation_state_persisted": bool(item.get("market_confirmation_state_persisted")),
        "market_confirmation_state_age_sec": item.get("market_confirmation_state_age_sec"),
        "market_confirmation_state_max_restore_age_sec": item.get("market_confirmation_state_max_restore_age_sec"),
        "market_confirmation_state_restore_reason": item.get("market_confirmation_state_restore_reason", ""),
        "market_confirmation_state_reset_reason": item.get("market_confirmation_state_reset_reason", ""),
        "market_session_id": item.get("market_session_id", ""),
        "market_session_type": item.get("market_session_type", ""),
        "market_trade_date": item.get("market_trade_date", ""),
        "market_restore_allowed": bool(item.get("market_restore_allowed", True)),
        "market_reset_required": bool(item.get("market_reset_required", False)),
        "market_side_breadth_pct": item.get("candidate_breadth_pct", item.get("market_side_breadth_pct")),
        "market_side_index_return_pct": item.get("candidate_index_return_pct", item.get("market_side_index_return_pct")),
        "market_side_turnover_weighted_return_pct": item.get("market_side_turnover_weighted_return_pct"),
        "market_side_breadth_source": item.get("candidate_breadth_source") or item.get("market_side_breadth_source", ""),
        "market_side_breadth_trust_level": item.get("candidate_breadth_trust_level") or item.get("market_side_breadth_trust_level", ""),
        "market_side_breadth_gate_usable": bool(item.get("candidate_breadth_gate_usable", item.get("market_side_breadth_gate_usable", False))),
        "market_side_source_conflict": bool(item.get("market_side_source_conflict"))
        or "SIDE_BREADTH_SOURCE_CONFLICT" in set(item.get("market_side_reason_codes") or item.get("blocked_reason_codes") or []),
        "market_side_source_conflict_delta": item.get("market_side_source_conflict_delta"),
        "market_side_valid_quote_ratio": item.get("candidate_valid_quote_ratio", item.get("market_side_valid_quote_ratio")),
        "market_side_sample_count": int(item.get("candidate_breadth_sample_count", item.get("market_side_sample_count", 0)) or 0),
        "entry_plan_created": bool(item.get("entry_plan_created")),
        "diagnostic_only": bool(item.get("diagnostic_only")),
        "submittable": bool(item.get("submittable", gate in {"READY", "READY_SMALL"})),
        "blocked_reason": item.get("blocked_reason", ""),
        "blocked_reason_codes": list(item.get("reason_codes") or item.get("blocked_reason_codes") or item.get("risk_reason_codes") or []),
        "runtime_order_intent_created": bool(item.get("runtime_order_intent_created")),
        "virtual_order_created": bool(item.get("virtual_order_created")),
        "live_order_enabled": bool(item.get("live_order_enabled")),
        "live_order_guard_passed": bool(item.get("live_order_guard_passed")),
        "position_size_multiplier": float(item.get("position_size_multiplier") or 1.0),
        "recheck_after_sec": int(item.get("recheck_after_sec") or 0),
        "summary_reason": _summary_message(item, gate, display_status),
        "risk_reason_codes": list(item.get("risk_reason_codes") or []),
        "price_location_reason_codes": list(item.get("price_location_reason_codes") or []),
        "data_quality_flags": list(item.get("data_quality_flags") or []),
        "price_location_data_quality_flags": list(item.get("price_location_data_quality_flags") or []),
        "metrics": {
            "pullback_from_high_pct": item.get("pullback_from_high_pct"),
            "distance_to_session_high_pct": item.get("distance_to_session_high_pct"),
            "vwap_gap_pct": item.get("vwap_gap_pct"),
            "upper_limit_gap_pct": item.get("upper_limit_gap_pct"),
            "breakout_level_gap_pct": item.get("breakout_level_gap_pct"),
            "support_gap_pct": item.get("support_gap_pct"),
            "vi_active": item.get("vi_active"),
            "seconds_since_vi_release": item.get("seconds_since_vi_release"),
        },
    }


def _entry_row(item: dict[str, Any], priority: int) -> dict[str, Any]:
    return {
        "priority": priority,
        "symbol": item.get("symbol") or "",
        "code": item.get("code") or item.get("symbol") or "",
        "stock_name": item.get("stock_name") or item.get("name") or "",
        "theme_name": item.get("primary_theme") or "",
        "stock_role": item.get("stock_role") or "",
        "gate_status": item.get("gate_status") or "",
        "display_status": item.get("display_status") or item.get("gate_status") or "",
        "position_size_multiplier": item.get("position_size_multiplier") or 1.0,
        "entry_reference": _metric_ref(item, "breakout_level_gap_pct", "돌파 기준"),
        "stop_reference": _metric_ref(item, "support_gap_pct", "지지선"),
        "live_order_enabled": bool(item.get("live_order_enabled")),
        "live_order_guard_passed": bool(item.get("live_order_guard_passed")),
        "runtime_order_intent_created": bool(item.get("runtime_order_intent_created")),
        "virtual_order_created": bool(item.get("virtual_order_created")),
        "candidate_instance_id": item.get("candidate_instance_id", ""),
        "diagnostic_only": bool(item.get("diagnostic_only")),
        "submittable": bool(item.get("submittable")),
        "reason": item.get("summary_reason") or "",
    }


def _gate_detail(item: dict[str, Any]) -> dict[str, Any]:
    if not item:
        return {"gate_status": "OBSERVE", "summary_message": "선택된 WatchSet 종목이 없습니다."}
    row = _watch_row(item)
    return {
        **row,
        "summary_message": row["summary_reason"],
        "missing_data": _missing_data(row),
    }


def _chart_universe(themes: list[dict[str, Any]], watchset: list[dict[str, Any]], entry_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = {item["symbol"]: item for item in _index_chart_items()}

    def add(symbol: str, name: str, item_type: str, reason: str, priority: int, status: str = "NO_CANDLE_DATA") -> None:
        if not symbol or len(items) >= 50:
            return
        current = items.get(symbol)
        if current is None or priority < current["priority"]:
            items[symbol] = {
                "symbol": symbol,
                "name": name or symbol,
                "type": item_type,
                "reason": reason,
                "priority": priority,
                "has_candle_data": status == "READY",
                "chart_data_status": status,
                "last_candle_at": "",
            }

    for item in entry_candidates:
        add(item.get("symbol", ""), item.get("stock_name") or item.get("name") or "", "stock", item.get("gate_status", ""), 20)
    watch_count = 0
    for item in watchset:
        if watch_count >= 20:
            break
        if item.get("condition_level") <= 1 and item.get("gate_status") == "OBSERVE":
            continue
        if item.get("gate_status") == "BLOCKED" and not item.get("recheck_after_sec"):
            continue
        if item.get("symbol") in items:
            continue
        add(item.get("symbol", ""), item.get("stock_name") or item.get("name") or "", "stock", "WATCHSET", 50 + watch_count)
        watch_count += 1
    for theme in themes[:3]:
        symbol = theme.get("top_leader_symbol") or ""
        add(symbol, theme.get("top_leader_name") or symbol, "stock", "THEME_LEADER", 60)
    return sorted(items.values(), key=lambda item: (item["priority"], item["symbol"]))


def _index_chart_items() -> list[dict[str, Any]]:
    return [
        {"symbol": "KOSPI", "name": "KOSPI", "type": "index", "reason": "INDEX", "priority": 10, "has_candle_data": False, "chart_data_status": "NO_CANDLE_DATA", "last_candle_at": ""},
        {"symbol": "KOSDAQ", "name": "KOSDAQ", "type": "index", "reason": "INDEX", "priority": 11, "has_candle_data": False, "chart_data_status": "NO_CANDLE_DATA", "last_candle_at": ""},
    ]


def _select_chart(chart_universe: list[dict[str, Any]], watchset: list[dict[str, Any]]) -> dict[str, Any]:
    for status in ("READY", "READY_SMALL", "THEME_LEADER"):
        for item in chart_universe:
            if item.get("reason") == status:
                return item
    for item in chart_universe:
        if item.get("symbol") == "KOSDAQ":
            return item
    return chart_universe[0] if chart_universe else {}


def _sorted_watchset(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [_watch_row(item) for item in items]

    def sort_key(row: dict[str, Any]):
        return (
            _operating_priority(row),
            _recheck_seconds(row),
            ROLE_ORDER.get(str(row.get("stock_role") or "UNKNOWN"), 9),
            -float(row.get("turnover_krw") or 0),
            -float(row.get("theme_score") or 0),
            -int(row.get("condition_level") or 0),
            -float(row.get("price_location_score") or 0),
            str(row.get("symbol") or ""),
        )

    return sorted(rows, key=sort_key)


def _operating_priority(row: dict[str, Any]) -> int:
    gate = str(row.get("gate_status") or "OBSERVE")
    display = str(row.get("display_status") or gate)
    if gate == "READY":
        return 0
    if gate == "READY_SMALL":
        return 1
    if gate == "WAIT":
        return 20 + DISPLAY_WAIT_ORDER.get(display, 0)
    if _is_market_pending(row):
        return 31
    if _is_data_not_ready(row):
        return 32
    if gate == "OBSERVE":
        return 40
    if gate == "BLOCKED":
        return 50
    return 60 + GATE_ORDER.get(gate, 9)


def _recheck_seconds(row: dict[str, Any]) -> int:
    candidates = [
        int(row.get("recheck_after_sec") or 0),
        int(row.get("late_chase_recheck_after_sec") or 0),
        int(row.get("market_wait_recheck_after_sec") or 0),
    ]
    positives = [value for value in candidates if value > 0]
    return min(positives) if positives else 999999


def _summary_message(item: dict[str, Any], gate: str, display_status: str = "") -> str:
    role = _value(item.get("stock_role") or "UNKNOWN")
    location = _value(item.get("price_location_status") or "UNKNOWN")
    multiplier = float(item.get("position_size_multiplier") or 1.0)
    reasons = list(item.get("risk_reason_codes") or item.get("price_location_reason_codes") or [])
    display_status = str(display_status or item.get("display_status") or "")
    if display_status == "LATE_CHASE_TEMP_WAIT":
        seconds = int(item.get("late_chase_recheck_after_sec") or item.get("recheck_after_sec") or 0)
        return f"추격매수 대기: {seconds}초 후 재확인" if seconds else "추격매수 대기"
    if display_status == "CHASE_RISK_BLOCKED":
        return "추격매수 리스크로 신규 진입 차단"
    if display_status.startswith("WAIT_MARKET") or display_status.startswith("WAIT_CANDIDATE_MARKET"):
        seconds = int(item.get("market_wait_recheck_after_sec") or item.get("recheck_after_sec") or 0)
        suffix = f", {seconds}초 후 재확인" if seconds else ""
        return f"시장 확인 대기{suffix}"
    if display_status.startswith("WAIT_DATA"):
        reason = item.get("support_ready_reason") or item.get("blocked_reason") or "보조 데이터 준비 필요"
        return f"데이터 보강 대기: {reason}"
    if gate == "READY":
        live_note = "" if item.get("live_order_guard_passed") else " / LIVE Guard 미통과"
        return f"{role} / {location} 조건으로 진입 가능, {multiplier:.2g}배 비중{live_note}"
    if gate == "READY_SMALL":
        live_note = "" if item.get("live_order_guard_passed") else " / LIVE Guard 미통과"
        return f"{role} 흐름은 유효하지만 {location} 기준으로 소액 관찰 진입, {multiplier:.2g}배 비중{live_note}"
    if gate == "WAIT":
        return f"{location} 또는 리스크 확인 필요로 WAIT"
    if gate == "BLOCKED":
        return "진입 차단: " + (", ".join(str(reason) for reason in reasons[:3]) if reasons else "리스크 필터 차단")
    return str(item.get("watch_reason") or "관찰 단계")


def _missing_data(row: dict[str, Any]) -> list[str]:
    flags = set(row.get("data_quality_flags") or []) | set(row.get("price_location_data_quality_flags") or [])
    missing = ["분봉 데이터 없음"]
    if row["metrics"].get("vwap_gap_pct") is None or "MISSING_VWAP" in flags:
        missing.append("VWAP 데이터 없음")
    if row["metrics"].get("distance_to_session_high_pct") is None or "MISSING_SESSION_HIGH" in flags:
        missing.append("session_high 데이터 없음")
    if not row["metrics"].get("vi_active") and not row["metrics"].get("seconds_since_vi_release"):
        missing.append("VI 미지원")
    return missing


def _metric_ref(item: dict[str, Any], key: str, label: str) -> str:
    value = (item.get("metrics") or {}).get(key) if "metrics" in item else item.get(key)
    if value is None:
        return "UNKNOWN"
    return f"{label} {float(value):+.2f}%"


def _data_quality_message(status: str, candle_missing: int, missing_vwap: int) -> str:
    if status == "DEGRADED":
        return f"분봉 {candle_missing}종목 누락, VWAP {missing_vwap}종목 누락"
    if status == "WARNING":
        return "일부 보조 데이터가 누락되어 보수적으로 표시합니다."
    return "Data OK"


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")
