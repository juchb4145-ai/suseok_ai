from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from trading.broker.models import new_message_id


LIVE_SIM_CANARY_SOURCE = "live_sim_hybrid_ready_canary"
CANARY_SETTINGS_KEY = "live_sim_hybrid_ready_canary"


@dataclass(frozen=True)
class LiveSimCanaryDecision:
    decision_id: str
    trade_date: str = ""
    code: str = ""
    candidate_id: int | None = None
    candidate_instance_id: str = ""
    candidate_generation_seq: int = 0
    hybrid_status: str = ""
    hybrid_position_tier: str = ""
    hybrid_score: float | None = None
    theme_name: str = ""
    theme_score: float | None = None
    stock_role: str = ""
    price_location_status: str = ""
    price_location_readiness: str = ""
    eligible: bool = False
    status: str = "CONFIG_DISABLED"
    reason_codes: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    warning_reasons: list[str] = field(default_factory=list)
    operator_message_ko: str = ""
    preflight_status: str = ""
    dry_run_go_no_go_status: str = ""
    load_guard_status: str = ""
    limit_price: int = 0
    quantity: int = 0
    max_position_amount_krw: int = 0
    position_size_multiplier: float = 0.0
    order_ttl_sec: int = 0
    order_intent_id: str = ""
    gateway_command_id: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canary_config_from_settings(runtime_settings: Any) -> dict[str, Any]:
    if hasattr(runtime_settings, "value"):
        value = runtime_settings.value(CANARY_SETTINGS_KEY, {})
    elif isinstance(runtime_settings, dict):
        value = runtime_settings.get(CANARY_SETTINGS_KEY, runtime_settings)
    else:
        value = {}
    return dict(value or {})


def evaluate_live_sim_canary(
    *,
    candidate: Any = None,
    gate_result: Any = None,
    runtime_settings: Any = None,
    canary_config: dict[str, Any] | None = None,
    preflight_snapshot: dict[str, Any] | None = None,
    dry_run_performance: dict[str, Any] | None = None,
    load_guard_snapshot: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    counters: dict[str, Any] | None = None,
    limit_price: int | None = None,
    current_price: int | None = None,
    created_at: str = "",
) -> LiveSimCanaryDecision:
    config = dict(canary_config if canary_config is not None else canary_config_from_settings(runtime_settings))
    created = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = _merged_metadata(candidate=candidate, gate_result=gate_result, metadata=metadata)
    preflight = dict(preflight_snapshot or {})
    performance = _performance_payload(preflight, dry_run_performance)
    load_guard = dict(load_guard_snapshot or _load_guard_from_preflight(preflight) or {})
    counters = dict(counters or {})

    trade_date = _first_text(_get(candidate, "trade_date"), merged.get("trade_date"), created[:10])
    code = _first_text(_get(candidate, "code"), _get(gate_result, "code"), merged.get("code"))
    candidate_id = _optional_int(_first_non_empty(_get(candidate, "id"), _get(gate_result, "candidate_id"), merged.get("candidate_id")))
    candidate_instance_id = _first_text(merged.get("candidate_instance_id"))
    candidate_generation_seq = _optional_int(merged.get("candidate_generation_seq")) or 0
    hybrid_status = _upper(_first_text(merged.get("hybrid_status"), merged.get("final_gate_status"), merged.get("lab_gate_status"), _get(gate_result, "final_grade")))
    hybrid_position_tier = _first_text(merged.get("hybrid_position_tier"), merged.get("position_tier"))
    dynamic_theme_status = _upper(_first_text(merged.get("dynamic_theme_status"), merged.get("theme_status"), merged.get("theme_lab_theme_status")))
    stock_role = _upper(_first_text(merged.get("stock_role"), merged.get("leader_type"), merged.get("leadership_role"), merged.get("role")))
    price_location_readiness = _upper(_first_text(merged.get("price_location_readiness"), merged.get("price_readiness")))
    price_location_status = _first_text(merged.get("price_location_status"), merged.get("price_location"))
    risk_level = _upper(_first_text(merged.get("risk_level"), merged.get("tradeability_risk_level")))
    reason_universe = _reason_universe(merged)

    max_position_amount = max(0, _optional_int(config.get("max_position_amount_krw")) or 0)
    multiplier = max(0.0, _optional_float(config.get("position_size_multiplier")) or 0.0)
    ttl = max(0, _optional_int(config.get("order_ttl_sec")) or 0)
    resolved_limit = _resolve_limit_price(limit_price, current_price, merged)
    resolved_current = _resolve_current_price(current_price, merged)
    quantity = _quantity(max_position_amount, multiplier, resolved_limit)

    blocking: list[str] = []
    warnings: list[str] = []

    enabled = _bool(config.get("enabled"), False)
    order_enabled = _bool(config.get("order_enabled"), False)
    if not enabled:
        return LiveSimCanaryDecision(
            decision_id=new_message_id("canary_decision"),
            trade_date=trade_date,
            code=code,
            candidate_id=candidate_id,
            candidate_instance_id=candidate_instance_id,
            candidate_generation_seq=candidate_generation_seq,
            hybrid_status=hybrid_status,
            hybrid_position_tier=hybrid_position_tier,
            hybrid_score=_optional_float(_first_non_empty(merged.get("hybrid_score"), _get(gate_result, "final_score"))),
            theme_name=_first_text(merged.get("theme_name")),
            theme_score=_optional_float(merged.get("theme_score")),
            stock_role=stock_role,
            price_location_status=price_location_status,
            price_location_readiness=price_location_readiness,
            eligible=False,
            status="CONFIG_DISABLED",
            reason_codes=["CANARY_CONFIG_DISABLED"],
            blocking_reasons=[],
            warning_reasons=[],
            operator_message_ko="LIVE_SIM Canary 설정이 꺼져 있어 판단만 비활성 상태입니다.",
            preflight_status=_upper(preflight.get("status")),
            dry_run_go_no_go_status=_dry_run_status(performance),
            load_guard_status=_upper(load_guard.get("load_guard_status") or load_guard.get("status")),
            limit_price=resolved_limit,
            quantity=quantity,
            max_position_amount_krw=max_position_amount,
            position_size_multiplier=multiplier,
            order_ttl_sec=ttl,
            created_at=created,
            metadata=_decision_metadata(config, merged, counters, preflight, performance, load_guard),
        )

    preflight_status = _upper(preflight.get("status"))
    if _bool(config.get("require_preflight_go"), True):
        if preflight_status == "FAIL_CLOSED":
            blocking.append("PREFLIGHT_FAIL_CLOSED")
        elif preflight_status == "GO_WITH_WARNINGS":
            if not _bool(config.get("allow_go_with_warnings"), False):
                blocking.append("PREFLIGHT_GO_WITH_WARNINGS_BLOCKED")
            else:
                warnings.append("PREFLIGHT_GO_WITH_WARNINGS_ALLOWED")
        elif preflight_status != "GO":
            blocking.append("PREFLIGHT_NOT_GO" if preflight_status else "PREFLIGHT_MISSING")

    dry_run_status = _dry_run_status(performance)
    if _bool(config.get("require_dry_run_go_no_go"), True) and dry_run_status != "GO":
        blocking.append("DRY_RUN_GO_NO_GO_NOT_GO" if dry_run_status else "DRY_RUN_GO_NO_GO_MISSING")
    _append_performance_blocks(blocking, performance, config)

    if hybrid_status not in _upper_set(config.get("allowed_hybrid_statuses") or ["READY"]):
        blocking.append("HYBRID_STATUS_NOT_READY")
    if hybrid_position_tier not in set(config.get("allowed_position_tiers") or ["normal_first_entry"]):
        blocking.append("HYBRID_POSITION_TIER_NOT_ALLOWED")
    if dynamic_theme_status not in _upper_set(config.get("allowed_theme_statuses") or ["ACTIVE", "LEADING_THEME", "SPREADING_THEME"]):
        blocking.append("THEME_STATUS_NOT_ALLOWED")
    if stock_role not in _upper_set(config.get("allowed_stock_roles") or ["LEADER", "CO_LEADER"]):
        blocking.append("STOCK_ROLE_NOT_ALLOWED")
    if price_location_readiness not in _upper_set(config.get("allowed_price_location_readiness") or ["READY"]):
        blocking.append("PRICE_LOCATION_NOT_READY")
    if risk_level and risk_level not in _upper_set(config.get("allowed_risk_levels") or ["PASS"]):
        blocking.append("RISK_LEVEL_NOT_ALLOWED")

    if _bool(config.get("require_latest_tick_ready"), True) and not _bool(merged.get("latest_tick_ready"), False):
        blocking.append("LATEST_TICK_NOT_READY")
    if _bool(config.get("require_support_ready"), True) and not _bool(merged.get("support_ready"), False):
        blocking.append("SUPPORT_NOT_READY")
    if _bool(config.get("require_vwap_or_recent_support_ready"), True) and not _bool(
        _first_non_empty(merged.get("vwap_or_recent_support_ready"), merged.get("vwap_ready"), merged.get("recent_support_ready")),
        False,
    ):
        blocking.append("VWAP_OR_RECENT_SUPPORT_NOT_READY")
    if _bool(config.get("require_gate_usable_true"), True) and not _bool(merged.get("gate_usable"), False):
        blocking.append("GATE_USABLE_FALSE")
    if _bool(config.get("block_if_backfill_source_only"), True) and _has_any(reason_universe, {"TR_BACKFILL_ONLY", "BACKFILL_ONLY_OBSERVE"}):
        blocking.append("TR_BACKFILL_ONLY_BLOCKED")

    if _bool(config.get("block_if_market_risk_off"), True) and _has_any(
        reason_universe,
        {
            "MARKET_RISK_OFF",
            "EXTREME_RISK_OFF",
            "WAIT_MARKET_CONFIRMATION_PENDING",
            "CANDIDATE_MARKET_RISK_OFF",
            "KOSPI_MARKET_RISK_OFF",
            "KOSDAQ_MARKET_RISK_OFF",
            "GLOBAL_MARKET_RISK_OFF",
        },
    ):
        blocking.append("MARKET_RISK_OFF_BLOCKED")
    if _bool(config.get("block_if_chase_risk"), True) and (
        _bool(merged.get("chase_risk"), False)
        or _has_any(reason_universe, {"CHASE_RISK", "LATE_CHASE", "HIGH_CHASE_RISK", "VWAP_OVEREXTENDED", "BREAKOUT_CONTINUATION", "LATE_CHASE_TEMP_WAIT"})
    ):
        blocking.append("CHASE_RISK_BLOCKED")
    if _bool(config.get("block_if_late_laggard"), True) and _has_any(reason_universe, {"LATE_LAGGARD", "HIGH_RETURN_LATE_LAGGARD"}):
        blocking.append("LATE_LAGGARD_BLOCKED")
    if _bool(config.get("block_if_low_breadth"), True) and _has_any(reason_universe, {"LOW_BREADTH", "SIDE_BREADTH_LOW"}):
        blocking.append("LOW_BREADTH_BLOCKED")
    if _bool(config.get("block_if_leader_only_laggard"), True) and _has_any(reason_universe, {"LEADER_ONLY_THEME_LAGGARD_BLOCK"}):
        blocking.append("LEADER_ONLY_LAGGARD_BLOCKED")
    if _bool(config.get("block_if_entry_risk_temp_wait"), True) and _has_any(reason_universe, {"ENTRY_RISK_TEMP_WAIT"}):
        blocking.append("ENTRY_RISK_TEMP_WAIT_BLOCKED")
    if _bool(config.get("block_if_entry_risk_final_block"), True) and _has_any(
        reason_universe,
        {"ENTRY_RISK_FINAL_BLOCK", "VI_ACTIVE", "UPPER_LIMIT_HARD_NEAR"},
    ):
        blocking.append("ENTRY_RISK_FINAL_BLOCKED")

    load_guard_status = _upper(load_guard.get("load_guard_status") or load_guard.get("status"))
    if _bool(config.get("block_if_load_guard_not_ok"), True) and load_guard_status != "OK":
        blocking.append("LOAD_GUARD_NOT_OK" if load_guard_status else "LOAD_GUARD_MISSING")

    if _optional_int(counters.get("orders_per_day")) is not None and int(counters.get("orders_per_day") or 0) >= int(config.get("max_orders_per_day") or 0):
        blocking.append("MAX_ORDERS_PER_DAY_EXCEEDED")
    if _optional_int(counters.get("orders_per_cycle")) is not None and int(counters.get("orders_per_cycle") or 0) >= int(config.get("max_orders_per_cycle") or 0):
        blocking.append("MAX_ORDERS_PER_CYCLE_EXCEEDED")
    if _optional_int(counters.get("new_positions_per_day")) is not None and int(counters.get("new_positions_per_day") or 0) >= int(config.get("max_new_positions_per_day") or 0):
        blocking.append("MAX_NEW_POSITIONS_PER_DAY_EXCEEDED")
    if _bool(counters.get("has_open_order_for_code"), False):
        blocking.append("SAME_CODE_OPEN_ORDER_EXISTS")
    if _bool(counters.get("has_position_for_code"), False):
        blocking.append("SAME_CODE_POSITION_EXISTS")

    if _bool(config.get("submit_first_leg_only"), True) and (_optional_int(merged.get("leg_index")) or 1) != 1:
        blocking.append("ONLY_FIRST_LEG_ALLOWED")
    if resolved_limit <= 0:
        blocking.append("LIMIT_PRICE_INVALID")
    if resolved_current > 0 and resolved_limit > 0:
        max_bp = max(0, _optional_float(config.get("max_entry_slippage_bp")) or 0.0)
        if resolved_limit > int(math.floor(resolved_current * (1.0 + max_bp / 10000.0))):
            blocking.append("LIMIT_PRICE_SLIPPAGE_EXCEEDED")
    if quantity <= 0:
        blocking.append("BLOCKED_QUANTITY_BELOW_MIN")

    blocking = _dedupe(blocking)
    warnings = _dedupe(warnings)
    status = "ELIGIBLE"
    eligible = True
    if blocking:
        status = "BLOCKED"
        eligible = False
    elif not order_enabled:
        status = "OBSERVE_ONLY"
        eligible = False
        warnings.append("CANARY_ORDER_DISABLED_OBSERVE_ONLY")

    reason_codes = _dedupe(
        [str(config.get("reason_code") or "LIVE_SIM_HYBRID_READY_CANARY")]
        + (["CANARY_ELIGIBLE"] if eligible else [])
        + blocking
        + warnings
    )
    return LiveSimCanaryDecision(
        decision_id=new_message_id("canary_decision"),
        trade_date=trade_date,
        code=code,
        candidate_id=candidate_id,
        candidate_instance_id=candidate_instance_id,
        candidate_generation_seq=candidate_generation_seq,
        hybrid_status=hybrid_status,
        hybrid_position_tier=hybrid_position_tier,
        hybrid_score=_optional_float(_first_non_empty(merged.get("hybrid_score"), _get(gate_result, "final_score"))),
        theme_name=_first_text(merged.get("theme_name")),
        theme_score=_optional_float(merged.get("theme_score")),
        stock_role=stock_role,
        price_location_status=price_location_status,
        price_location_readiness=price_location_readiness,
        eligible=eligible,
        status=status,
        reason_codes=reason_codes,
        blocking_reasons=blocking,
        warning_reasons=warnings,
        operator_message_ko=_operator_message(status, blocking, warnings),
        preflight_status=preflight_status,
        dry_run_go_no_go_status=dry_run_status,
        load_guard_status=load_guard_status,
        limit_price=resolved_limit,
        quantity=quantity,
        max_position_amount_krw=max_position_amount,
        position_size_multiplier=multiplier,
        order_ttl_sec=ttl,
        created_at=created,
        metadata=_decision_metadata(config, merged, counters, preflight, performance, load_guard),
    )


def decision_record_from_result(
    decision: LiveSimCanaryDecision,
    *,
    order_result: Any = None,
) -> dict[str, Any]:
    payload = decision.to_dict()
    if order_result is not None:
        result_dict = order_result.to_dict() if hasattr(order_result, "to_dict") else dict(order_result or {})
        payload["order_intent_id"] = str(result_dict.get("intent_id") or payload.get("order_intent_id") or "")
        payload["gateway_command_id"] = str(result_dict.get("command_id") or payload.get("gateway_command_id") or "")
        metadata = dict(payload.get("metadata") or {})
        metadata["live_sim_order_result"] = result_dict
        payload["metadata"] = metadata
    return payload


def _merged_metadata(*, candidate: Any, gate_result: Any, metadata: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(_dict_from(_get(candidate, "metadata")))
    result.update(_dict_from(_get(gate_result, "details")))
    bridge = _dict_from(result.get("theme_lab_bridge"))
    result.update({k: v for k, v in bridge.items() if k not in result or result.get(k) in (None, "")})
    result.update(dict(metadata or {}))
    return result


def _decision_metadata(
    config: dict[str, Any],
    merged: dict[str, Any],
    counters: dict[str, Any],
    preflight: dict[str, Any],
    performance: dict[str, Any],
    load_guard: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": LIVE_SIM_CANARY_SOURCE,
        "config": _public_config(config),
        "inputs": merged,
        "counters": counters,
        "preflight_snapshot_id": preflight.get("snapshot_id", ""),
        "dry_run_performance": performance,
        "load_guard": load_guard,
    }


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(config or {}).items() if "account" not in str(key).lower()}


def _performance_payload(preflight: dict[str, Any], explicit: dict[str, Any] | None) -> dict[str, Any]:
    if explicit:
        return dict(explicit)
    return dict(preflight.get("performance_summary") or preflight.get("dry_run_performance") or {})


def _dry_run_status(performance: dict[str, Any]) -> str:
    direct = _upper(_first_text(performance.get("go_no_go_status"), performance.get("decision"), performance.get("status")))
    go_no_go = dict(performance.get("go_no_go") or {})
    if go_no_go:
        direct = _upper(_first_text(go_no_go.get("decision"), go_no_go.get("readiness"), direct))
    return direct


def _append_performance_blocks(blocking: list[str], performance: dict[str, Any], config: dict[str, Any]) -> None:
    trade_days = _optional_int(performance.get("trade_day_count"))
    if trade_days is not None and trade_days < int(config.get("min_trade_days") or 0):
        blocking.append("DRY_RUN_TRADE_DAYS_BELOW_MIN")
    accepted = _optional_int(_first_non_empty(performance.get("accepted_completed_lifecycle_count"), performance.get("dry_run_accepted_count")))
    if accepted is not None and accepted < int(config.get("min_accepted_entry_lifecycles") or 0):
        blocking.append("DRY_RUN_ACCEPTED_LIFECYCLES_BELOW_MIN")
    expectancy = _optional_float(_first_non_empty(performance.get("net_expectancy_pct"), performance.get("net_expectancy")))
    if expectancy is not None and expectancy < float(config.get("min_net_expectancy_pct") or 0.0):
        blocking.append("DRY_RUN_NET_EXPECTANCY_BELOW_MIN")
    bad_ready = _optional_float(performance.get("bad_ready_rate"))
    if bad_ready is not None and bad_ready > float(config.get("max_bad_ready_rate") or 0.0):
        blocking.append("DRY_RUN_BAD_READY_RATE_TOO_HIGH")
    stale_tick = _optional_float(performance.get("stale_tick_rate"))
    if stale_tick is not None and stale_tick > float(config.get("max_stale_tick_rate") or 0.0):
        blocking.append("DRY_RUN_STALE_TICK_RATE_TOO_HIGH")
    latency = _optional_float(_first_non_empty(performance.get("latency_risk_rate"), performance.get("latency_distortion_rate")))
    if latency is not None and latency > float(config.get("max_latency_risk_rate") or 0.0):
        blocking.append("DRY_RUN_LATENCY_RISK_RATE_TOO_HIGH")


def _load_guard_from_preflight(preflight: dict[str, Any]) -> dict[str, Any]:
    source_metrics = dict(preflight.get("source_metrics") or {})
    runtime_status = dict(source_metrics.get("runtime_status") or {})
    for value in (
        runtime_status.get("load_guard"),
        runtime_status.get("runtime_load_guard"),
        dict(preflight.get("backfill_summary") or {}).get("load_guard"),
        preflight.get("load_guard"),
    ):
        if isinstance(value, dict) and value:
            return dict(value)
    return {}


def _reason_universe(metadata: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    keys = [
        "reason_codes",
        "risk_reason_codes",
        "market_reason_codes",
        "market_side_reason_codes",
        "entry_risk_reason_codes",
        "data_quality_flags",
        "market_side_data_quality_flags",
        "blocking_reasons",
    ]
    for key in keys:
        raw = metadata.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.update(_upper(item) for item in raw if str(item or ""))
        elif raw not in (None, ""):
            values.add(_upper(raw))
    for key in ("sub_status", "final_gate_status", "candidate_market_status", "market_status", "source", "tick_source"):
        value = metadata.get(key)
        if value not in (None, ""):
            values.add(_upper(value))
    return values


def _resolve_limit_price(limit_price: int | None, current_price: int | None, metadata: dict[str, Any]) -> int:
    for value in (limit_price, metadata.get("limit_price"), metadata.get("entry_limit_price"), metadata.get("planned_limit_price")):
        number = _optional_int(value)
        if number and number > 0:
            return int(number)
    return _resolve_current_price(current_price, metadata)


def _resolve_current_price(current_price: int | None, metadata: dict[str, Any]) -> int:
    for value in (current_price, metadata.get("current_price"), metadata.get("price"), metadata.get("last_price")):
        number = _optional_int(value)
        if number and number > 0:
            return int(number)
    return 0


def _quantity(max_position_amount: int, multiplier: float, limit_price: int) -> int:
    if max_position_amount <= 0 or multiplier <= 0 or limit_price <= 0:
        return 0
    return int(math.floor((max_position_amount * multiplier) / limit_price))


def _operator_message(status: str, blocking: list[str], warnings: list[str]) -> str:
    if status == "ELIGIBLE":
        return "LIVE_SIM Canary 조건을 모두 통과했습니다. 기존 LIVE_SIM 안전 큐를 통해 1차 매수 후보만 제출 가능합니다."
    if status == "OBSERVE_ONLY":
        return "LIVE_SIM Canary 조건은 관찰 모드입니다. order_enabled=false라 주문은 생성하지 않고 판단만 기록합니다."
    first = blocking[0] if blocking else warnings[0] if warnings else "CANARY_BLOCKED"
    return f"LIVE_SIM Canary 주문 후보에서 차단했습니다. 주요 사유: {first}"


def _dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _first_text(*values: Any) -> str:
    value = _first_non_empty(*values)
    return str(value or "")


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _upper_set(values: Any) -> set[str]:
    return {_upper(item) for item in list(values or []) if str(item or "")}


def _has_any(values: set[str], needles: set[str]) -> bool:
    return bool(values & {_upper(item) for item in needles})


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
