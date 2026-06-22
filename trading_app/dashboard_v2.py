from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from trading_app.dashboard_labels import (
    entry_bucket_label_ko,
    reason_label_ko,
    reason_severity,
    suggested_action_ko,
    theme_status_label_ko,
)
from trading_app.pre_market_check import pre_market_report_empty


DASHBOARD_V2_SCHEMA_VERSION = "dashboard_v2.reboot_ops.v1"
DASHBOARD_V2_NAMESPACE = "reboot_v2.main"
DASHBOARD_V2_VIEW_NAME = "reboot_v2.main"
DASHBOARD_V2_PAYLOAD_TYPE = "dashboard_v2_snapshot"


def dashboard_v2_enabled() -> bool:
    return _env_bool("TRADING_DASHBOARD_V2_ENABLED", _env_bool("STRATEGY_REBOOT_V2_DASHBOARD", True))


def dashboard_v2_auto_route_enabled() -> bool:
    return _env_bool("TRADING_DASHBOARD_V2_AUTO_ROUTE", True)


def build_dashboard_v2_snapshot(snapshot: dict[str, Any] | None, *, detail: str = "slim") -> dict[str, Any]:
    base = dict(snapshot or {})
    full = str(detail or "slim").lower() in {"full", "debug", "verbose"}
    runtime = dict(base.get("runtime") or {})
    gateway = dict(base.get("gateway") or {})
    commands = dict(base.get("commands") or {})
    market = _prefer_runtime_section(base.get("market_regime"), runtime.get("market_regime"))
    themes = _prefer_runtime_section(base.get("theme_board"), runtime.get("theme_board"))
    entry = _prefer_runtime_section(base.get("entry_engine"), runtime.get("entry_engine"))
    setup_router = _prefer_runtime_section(base.get("setup_router_v3"), runtime.get("setup_router_v3"))
    exit_engine = _prefer_runtime_section(base.get("exit_engine"), runtime.get("exit_engine"))
    pos_risk = _prefer_runtime_section(base.get("position_risk"), runtime.get("position_risk"))
    order_manager = _prefer_runtime_section(base.get("order_manager"), runtime.get("order_manager"))
    market_rs_shadow = _prefer_runtime_section(base.get("market_relative_strength_shadow"), runtime.get("market_relative_strength_shadow"))
    market_rs_outcomes = _prefer_runtime_section(base.get("market_relative_strength_outcomes"), runtime.get("market_relative_strength_outcomes"))
    candidates = dict(base.get("candidates") or {})

    payload = {
        "schema_version": DASHBOARD_V2_SCHEMA_VERSION,
        "payload_type": DASHBOARD_V2_PAYLOAD_TYPE,
        "snapshot_namespace": DASHBOARD_V2_NAMESPACE,
        "view_name": DASHBOARD_V2_VIEW_NAME,
        "source_kind": str(base.get("source_kind") or "CANONICAL_V2_BUILDER"),
        "generated_at": base.get("timestamp") or _now(),
        "enabled": dashboard_v2_enabled(),
        "v2_status": _v2_status(base, runtime, gateway, market, order_manager, pos_risk),
        "market_overview": _market_overview(market),
        "leading_themes": _leading_themes(themes),
        "theme_rotation": _theme_rotation_summary(themes, runtime),
        "entry_candidates": _entry_candidates(entry, candidates, order_manager),
        "setup_router_v3": _setup_router_v3_summary(setup_router),
        "position_risk": _position_risk(pos_risk, exit_engine),
        "exit_watch": _exit_watch(exit_engine),
        "order_manager": _order_manager(order_manager, gateway, commands),
        "market_relative_strength_shadow": _market_relative_strength_shadow(market_rs_shadow, market_rs_outcomes),
        "pre_market_check": _pre_market_check(base),
        "wait_block_reasons": _wait_block_reasons(base, runtime),
        "system_health": _system_health(base, runtime, gateway, commands),
        "legacy_debug_link": {
            "label": "개발자 상세",
            "href": "/legacy",
            "debug_href": "/api/snapshot?detail=full",
        },
        "ui_policy": {
            "main_view_raw_json": False,
            "main_view_legacy_panels": False,
            "live_real_enable_ui_allowed": False,
            "order_enable_ui_allowed": False,
            "kill_switch_reset_ui_allowed": False,
        },
    }
    payload["safety_banners"] = _safety_banners(payload)
    if full:
        payload["debug"] = {
            "market_regime": market,
            "theme_board": themes,
            "entry_engine": entry,
            "setup_router_v3": setup_router,
            "exit_engine": exit_engine,
            "position_risk": pos_risk,
            "order_manager": order_manager,
            "market_relative_strength_shadow": market_rs_shadow,
            "market_relative_strength_outcomes": market_rs_outcomes,
        }
    return payload


def _prefer_runtime_section(base_section: Any, runtime_section: Any) -> dict[str, Any]:
    base_data = dict(base_section or {})
    runtime_data = dict(runtime_section or {})
    if _section_is_active(runtime_data):
        return runtime_data
    return base_data or runtime_data


def _section_is_active(section: dict[str, Any]) -> bool:
    if not section:
        return False
    if bool(section.get("enabled")):
        return True
    status = str(section.get("status") or "").strip().upper()
    if status in {"DISABLED", "EMPTY", "UNAVAILABLE"}:
        return False
    return bool(status or section.get("calculated_at"))


def _v2_status(
    base: dict[str, Any],
    runtime: dict[str, Any],
    gateway: dict[str, Any],
    market: dict[str, Any],
    order_manager: dict[str, Any],
    position_risk: dict[str, Any],
) -> dict[str, Any]:
    core = dict(base.get("core") or {})
    broker_env = str(order_manager.get("broker_env") or _broker_env_from_gateway(gateway))
    kill = str(order_manager.get("kill_switch_state") or "NORMAL")
    market_status = str(market.get("global_status") or "")
    systemic_risk_off = _systemic_risk_off(market)
    runtime_profile = str(
        runtime.get("runtime_profile")
        or base.get("runtime_profile")
        or os.getenv("STRATEGY_RUNTIME_PROFILE")
        or os.getenv("STRATEGY_REBOOT_V2_PROFILE")
        or "V2_OBSERVE"
    ).upper()
    reboot_v2_enabled = bool(runtime.get("reboot_v2_enabled")) or runtime_profile != "LEGACY"
    pipeline_status = dict(runtime.get("pipeline_status") or {})
    data_freshness = _data_freshness_status(gateway, market, runtime, reboot_v2_enabled=reboot_v2_enabled)
    live_sim_allowed = bool(order_manager.get("live_sim_orders_allowed"))
    observe_only = bool("ORDER_MANAGER_OBSERVE_ONLY" in set(order_manager.get("warnings") or [])) or not live_sim_allowed
    label = "정상"
    if kill in {"KILL_SWITCH_ACTIVE", "REDUCE_ONLY", "STOP_NEW_BUY"}:
        label = "위험"
    elif broker_env == "REAL":
        label = "주문차단"
    elif systemic_risk_off:
        label = "위험"
    elif not live_sim_allowed:
        label = "관찰전용"
    if data_freshness == "WAIT_DATA":
        label = "데이터대기"
    return {
        "reboot_v2_enabled": reboot_v2_enabled,
        "runtime_profile": runtime_profile,
        "dashboard_v2_enabled": dashboard_v2_enabled(),
        "trading_mode": core.get("mode") or runtime.get("mode") or "OBSERVE",
        "order_manager_mode": order_manager.get("mode", "OBSERVE"),
        "observe_only": observe_only,
        "dry_run_enabled": bool(runtime.get("dry_run_order_sink_enabled") or runtime.get("mode") == "DRY_RUN"),
        "live_sim_enabled": live_sim_allowed,
        "real_broker_blocked": broker_env == "REAL" or "REAL_BROKER_BLOCKED" in set(order_manager.get("warnings") or []),
        "broker_env": broker_env,
        "account_mode": _account_mode(gateway, order_manager),
        "account": order_manager.get("account") or gateway.get("account") or "",
        "account_whitelisted": bool(order_manager.get("account_whitelisted")),
        "market_session_status": market.get("market_session_status") or runtime.get("market_session_status") or "",
        "last_runtime_cycle_at": runtime.get("last_cycle_at") or runtime.get("cycle_at") or "",
        "last_gateway_heartbeat_at": gateway.get("last_heartbeat_at") or "",
        "data_freshness_status": data_freshness,
        "pipeline_status": pipeline_status,
        "stages": {
            "candidate_ingestion": _stage_status("candidate_ingestion", runtime.get("candidate_ingestion"), pipeline_status),
            "candidate_hydrator": _stage_status("candidate_hydrator", runtime.get("candidate_hydration"), pipeline_status),
            "opening_burst": _stage_status("opening_burst", runtime.get("opening_burst"), pipeline_status),
            "theme_board": _stage_status("theme_board", runtime.get("theme_board"), pipeline_status),
            "market_regime": _stage_status("market_regime", runtime.get("market_regime"), pipeline_status),
            "entry_engine": _stage_status("entry_engine", runtime.get("entry_engine"), pipeline_status),
            "setup_router_v3": _stage_status("setup_router_v3", runtime.get("setup_router_v3"), pipeline_status),
            "market_relative_strength_shadow": _stage_status(
                "market_relative_strength_shadow",
                runtime.get("market_relative_strength_shadow"),
                pipeline_status,
            ),
            "exit_engine": _stage_status("exit_engine", runtime.get("exit_engine_reboot") or runtime.get("exit_engine"), pipeline_status),
            "position_risk": _stage_status("position_risk", runtime.get("position_risk"), pipeline_status),
            "order_manager": _stage_status("order_manager", runtime.get("order_manager"), pipeline_status),
        },
        "kill_switch_state": kill,
        "status_label": label,
        "operator_message_ko": _status_message(label, market_status, broker_env, kill, live_sim_allowed, systemic_risk_off),
    }


def _market_overview(market: dict[str, Any]) -> dict[str, Any]:
    global_status = str(market.get("global_status") or market.get("status") or "UNKNOWN")
    systemic_risk_off = _systemic_risk_off(market)
    composite_mode = str(market.get("composite_market_mode") or "")
    return {
        "status": market.get("status", "EMPTY"),
        "global_status": global_status,
        "composite_market_mode": composite_mode,
        "composite_market_mode_label_ko": market.get("composite_market_mode_label_ko") or _composite_mode_label_ko(composite_mode),
        "systemic_risk_off": systemic_risk_off,
        "kospi_status": market.get("kospi_status", "UNKNOWN"),
        "kosdaq_status": market.get("kosdaq_status", "UNKNOWN"),
        "kospi_return_pct": _num(market.get("kospi_return_pct")),
        "kosdaq_return_pct": _num(market.get("kosdaq_return_pct")),
        "kospi_breadth_pct": _num(market.get("kospi_breadth_pct")),
        "kosdaq_breadth_pct": _num(market.get("kosdaq_breadth_pct")),
        "risk_off_detected": bool(market.get("risk_off_detected") or global_status == "RISK_OFF"),
        "weak_market_detected": bool(market.get("weak_market_detected") or global_status == "WEAK"),
        "candidate_policy_summary_by_side": dict(market.get("candidate_policy_summary_by_side") or {}),
        "market_side_unresolved_count": int(market.get("market_side_unresolved_count") or 0),
        "split_market_reduced_count": int(market.get("split_market_reduced_count") or 0),
        "block_new_entry_count": int(market.get("block_new_entry_count") or 0),
        "wait_market_count": int(market.get("wait_market_count") or 0),
        "market_operator_message_ko": market.get("market_operator_message_ko") or _market_message(global_status, market, systemic_risk_off),
        "top_reasons": [_reason_item(item) for item in list(market.get("top_reasons") or [])[:5]],
    }


def _leading_themes(theme_board: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for index, theme in enumerate(list(theme_board.get("top_themes") or [])[:5], start=1):
        item = dict(theme or {})
        status = str(item.get("theme_status") or "")
        reasons = list(item.get("reason_codes") or [])
        rows.append(
            {
                "rank": int(item.get("theme_rank") or index),
                "theme_name": item.get("theme_name") or item.get("name") or "-",
                "theme_status": status,
                "theme_status_label": theme_status_label_ko(status),
                "theme_score": _num(item.get("theme_score")),
                "leader_symbol": item.get("leader_symbol", ""),
                "leader_name": item.get("leader_name", ""),
                "co_leader_symbols": list(item.get("co_leader_symbols") or [])[:5],
                "strong_count": int(item.get("strong_count") or 0),
                "leader_count": int(item.get("leader_count") or 0),
                "theme_turnover_krw": _num(item.get("theme_turnover_krw")),
                "previous_rank": int(item.get("previous_rank") or 0),
                "rank_delta": int(item.get("leadership_rank_delta") or item.get("rank_delta") or 0),
                "state_age_sec": int(item.get("state_age_sec") or 0),
                "temporal_persistence_sec": int(item.get("temporal_persistence_sec") or 0),
                "state_cycle_count": int(item.get("state_cycle_count") or item.get("persistence_count") or 0),
                "leadership_status": item.get("leadership_status", ""),
                "leadership_score": _num(item.get("leadership_score")),
                "recent_flow_score": _num(item.get("recent_flow_score")),
                "flow_share": _num(item.get("theme_flow_share") or item.get("flow_share")),
                "flow_share_delta": _num(item.get("theme_flow_share_delta") or item.get("flow_share_delta")),
                "state_leadership_consistent": bool(item.get("state_leadership_consistent", True)),
                "state_leadership_mismatch_code": item.get("state_leadership_mismatch_code", ""),
                "last_fresh_signal_at": item.get("last_fresh_signal_at", ""),
                "market_overlay": {
                    "dominant_market_side": item.get("dominant_market_side", ""),
                    "market_risk_flag": bool(item.get("market_risk_flag")),
                },
                "data_quality_status": _data_quality_status(item),
                "reason_summary_ko": _reason_summary_ko(reasons) or theme_status_label_ko(status),
                "reason_codes": reasons,
            }
        )
    return {
        "status": theme_board.get("status", "EMPTY"),
        "calculated_at": theme_board.get("calculated_at", ""),
        "items": rows,
        "top5_count": len(rows),
        "hidden_total_hint": "전체 테마 표는 개발자 상세에서 확인",
    }


def _theme_rotation_summary(theme_board: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    handover = dict(theme_board.get("leadership_handover") or {})
    expansion = dict(runtime.get("theme_expansion_subscription") or theme_board.get("theme_expansion_subscription") or {})
    lease = dict(expansion.get("lease_snapshot") or {})
    top_themes = [dict(item or {}) for item in list(theme_board.get("top_themes") or [])]
    incumbent = next((item for item in top_themes if str(item.get("leadership_status") or "").upper() in {"INCUMBENT", "TAKEOVER_CONFIRMED"}), {})
    challengers = [item for item in top_themes if str(item.get("leadership_status") or "").upper() == "CHALLENGER"]
    pending = [item for item in top_themes if str(item.get("leadership_status") or "").upper() == "TAKEOVER_PENDING"]
    losing = [item for item in top_themes if str(item.get("leadership_status") or "").upper() == "LOSING_LEADERSHIP"]
    rotated = [item for item in top_themes if str(item.get("leadership_status") or "").upper() == "ROTATED_OUT"]
    mismatches = [item for item in top_themes if item.get("state_leadership_consistent") is False]
    return {
        "status": theme_board.get("status", "EMPTY"),
        "calculated_at": theme_board.get("calculated_at", ""),
        "current_incumbent_theme_id": incumbent.get("theme_id", ""),
        "current_incumbent_name": incumbent.get("theme_name", ""),
        "challenger_count": len(challengers),
        "takeover_pending_count": len(pending),
        "losing_leadership_count": len(losing),
        "rotated_out_count": len(rotated),
        "state_mismatch_count": len(mismatches),
        "transition_count": int(handover.get("transition_count") or 0),
        "active_expansion_lease_count": int(lease.get("active_lease_count") or 0),
        "holding_lease_count": int(lease.get("holding_count") or 0),
        "pending_removal_lease_count": int(lease.get("pending_removal_count") or 0),
        "subscription_churn_count": int(lease.get("churn_count") or 0),
        "first_fresh_tick_wait_count": int(lease.get("first_tick_wait_count") or 0),
        "mismatch_codes": [item.get("state_leadership_mismatch_code", "") for item in mismatches if item.get("state_leadership_mismatch_code")],
    }


def _entry_candidates(entry: dict[str, Any], candidates: dict[str, Any], order_manager: dict[str, Any]) -> dict[str, Any]:
    source_rows = []
    source_rows.extend(list(entry.get("top_ready_candidates") or []))
    source_rows.extend(list(entry.get("decisions") or []))
    if not source_rows:
        source_rows.extend(list(candidates.get("items") or []))
    seen: set[str] = set()
    rows = []
    pending_codes = {
        str(item.get("code") or "")
        for item in list(order_manager.get("managed_orders") or [])
        if str(item.get("status") or "") in {"QUEUED_TO_GATEWAY", "ACKED_BY_GATEWAY", "PARTIALLY_FILLED", "CANCEL_PENDING"}
    }
    for raw in source_rows:
        item = dict(raw or {})
        code = str(item.get("code") or "")
        key = f"{code}:{item.get('entry_status') or item.get('display_state') or item.get('id') or len(rows)}"
        if key in seen:
            continue
        seen.add(key)
        entry_status = str(item.get("entry_status") or item.get("display_state") or "")
        bucket = _entry_bucket(item, pending_codes)
        reasons = _entry_reasons(item)
        context = _row_strategy_context(item)
        context_theme = dict(context.get("theme") or {})
        context_stock = dict(context.get("stock") or {})
        context_sources = dict(context.get("source_timestamps") or {})
        rows.append(
            {
                "code": code,
                "name": item.get("name", ""),
                "theme_name": item.get("theme_name") or item.get("theme_board_theme_name") or "",
                "theme_status": item.get("theme_status") or item.get("theme_board_theme_status") or "",
                "stock_role": item.get("stock_role") or item.get("theme_board_stock_role") or "",
                "market_status": item.get("market_status") or item.get("market_regime_status") or "",
                "market_action": item.get("market_action") or "",
                "entry_status": entry_status or "UNKNOWN",
                "display_bucket": bucket,
                "display_bucket_label": entry_bucket_label_ko(bucket),
                "setup_status": "SETUP_READY" if bucket in {"SETUP_READY", "TIMING_READY", "ORDER_PENDING"} else bucket,
                "price_location": item.get("price_location") or item.get("entry_price_location") or "",
                "current_price": int(_num(item.get("current_price"))),
                "change_rate_pct": _num(item.get("change_rate_pct") or item.get("change_rate")),
                "vwap_gap_pct": _num(item.get("vwap_gap_pct")),
                "pullback_from_high_pct": _num(item.get("pullback_from_high_pct")),
                "limit_price_hint": int(_num(item.get("limit_price_hint"))),
                "position_size_multiplier_hint": _num(item.get("position_size_multiplier_hint")),
                "dry_run_intent_allowed": bool(item.get("dry_run_intent_allowed") or item.get("entry_dry_run_intent_allowed")),
                "live_order_allowed": bool(item.get("live_order_allowed") or item.get("entry_live_order_allowed")),
                "strategy_context_version": context.get("schema_version") or item.get("strategy_context_version") or "",
                "strategy_context_id": context.get("context_id") or item.get("strategy_context_id") or "",
                "session_phase": context.get("session_phase") or item.get("session_phase") or "",
                "market_context_at": context_sources.get("market_context_at") or "",
                "theme_context_at": context_sources.get("theme_context_at") or "",
                "context_fresh": bool(context.get("context_fresh") or item.get("context_fresh")),
                "theme_transition": context_theme.get("theme_transition") or "",
                "theme_persistence_count": int(_num(context_theme.get("persistence_count"))),
                "raw_stock_role": context_stock.get("raw_stock_role") or "",
                "trade_stock_role": context_stock.get("trade_stock_role") or "",
                "selected_theme_id": context.get("selected_theme_id") or "",
                "previous_selected_theme_id": context.get("previous_selected_theme_id") or "",
                "theme_selection_changed": bool(context.get("theme_selection_changed")),
                "alternative_theme_count": len(list(context.get("alternative_theme_ids") or [])),
                "leadership_status": context_theme.get("leadership_status") or "",
                "leadership_entry_policy": context_theme.get("leadership_entry_policy") or "",
                "focused_expansion_source": item.get("focused_expansion_source") or "",
                "blocking_stage": context.get("blocking_stage") or item.get("blocking_stage") or "",
                "next_required_action": item.get("next_required_action") or "",
                "reason_summary_ko": _reason_summary_ko(reasons),
                "operator_message_ko": item.get("operator_message_ko") or item.get("entry_operator_message_ko") or _entry_message(bucket),
                "reason_codes": reasons,
            }
        )
    priority = {"ORDER_PENDING": 0, "TIMING_READY": 1, "SETUP_READY": 2, "WAIT": 3, "BLOCK": 4}
    rows = sorted(rows, key=lambda row: (priority.get(row["display_bucket"], 9), row["code"]))[:15]
    bucket_counts = Counter(row["display_bucket"] for row in rows)
    observe_ready_count = int(
        entry.get("observe_ready_count")
        or sum(1 for row in rows if row["display_bucket"] in {"TIMING_READY", "ORDER_PENDING"})
    )
    evaluated_count = int(entry.get("evaluated_count") or len(rows))
    evaluation_eligible_count = int(entry.get("evaluation_eligible_count") or evaluated_count)
    return {
        "status": entry.get("status", "EMPTY"),
        "calculated_at": entry.get("calculated_at", ""),
        "evaluated_count": evaluated_count,
        "evaluation_eligible_count": evaluation_eligible_count,
        "observe_ready_count": observe_ready_count,
        "wait_count": int(entry.get("wait_count") or bucket_counts.get("WAIT", 0)),
        "data_wait_count": int(entry.get("data_wait_count") or 0),
        "theme_wait_count": int(entry.get("theme_wait_count") or 0),
        "market_wait_count": int(entry.get("market_wait_count") or 0),
        "price_wait_count": int(entry.get("price_wait_count") or bucket_counts.get("SETUP_READY", 0)),
        "hard_block_count": int(entry.get("hard_block_count") or bucket_counts.get("BLOCK", 0)),
        "items": rows,
        "ready_items": [row for row in rows if row["display_bucket"] in {"TIMING_READY", "ORDER_PENDING"}],
        "bucket_counts": dict(bucket_counts),
        "top_wait_reasons": list(entry.get("top_wait_reasons") or []),
        "top_block_reasons": list(entry.get("top_block_reasons") or []),
    }


def _setup_router_v3_summary(section: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for raw in list(section.get("observations") or [])[:20]:
        item = dict(raw or {})
        reasons = list(item.get("reason_codes") or [])
        rows.append(
            {
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "theme_name": item.get("theme_name", ""),
                "setup_type": item.get("setup_type", ""),
                "shape_status": item.get("shape_status", ""),
                "context_status": item.get("context_status", ""),
                "router_status": item.get("router_status", ""),
                "entry_alignment_status": item.get("entry_alignment_status", ""),
                "setup_quality_score": _num(item.get("setup_quality_score")),
                "current_price": _num(item.get("current_price")),
                "price_structure": dict(item.get("price_structure") or {}),
                "setup_generation": int(item.get("setup_generation") or 1),
                "setup_instance_id": item.get("setup_instance_id", ""),
                "lifecycle_state": item.get("lifecycle_state", ""),
                "post_subscription_tick_verified": bool(item.get("post_subscription_tick_verified", True)),
                "entry_decision_age_sec": _num(item.get("entry_decision_age_sec")),
                "router_version": item.get("router_version", ""),
                "last_material_change_at": item.get("last_material_change_at", ""),
                "reason_codes": reasons,
                "reason_summary_ko": _reason_summary_ko(reasons),
                "updated_at": item.get("updated_at") or item.get("calculated_at") or "",
                "primary_setup": bool(item.get("primary_setup")),
                "observe_only": True,
            }
        )
    return {
        "enabled": bool(section.get("enabled")),
        "status": section.get("status", "DISABLED" if not section.get("enabled") else "EMPTY"),
        "schema_version": section.get("schema_version", "setup_router_v3.observe.v3"),
        "feature_schema_version": section.get("feature_schema_version", "setup_router_v3.features.v3"),
        "router_version": section.get("router_version", "setup_router_v3.3"),
        "state_version": section.get("state_version", "setup_router_v3.state.v2"),
        "output_mode": section.get("output_mode", "OBSERVE"),
        "observe_only": True,
        "calculated_at": section.get("calculated_at", ""),
        "candidate_count": int(section.get("candidate_count") or 0),
        "evaluated_count": int(section.get("evaluated_count") or 0),
        "observation_count": int(section.get("observation_count") or 0),
        "valid_observe_count": int(section.get("valid_observe_count") or 0),
        "pending_count": int(section.get("pending_count") or 0),
        "data_wait_count": int(section.get("data_wait_count") or 0),
        "context_blocked_count": int(section.get("context_blocked_count") or 0),
        "avoid_count": int(section.get("avoid_count") or 0),
        "unknown_count": int(section.get("unknown_count") or 0),
        "invalidated_count": int(section.get("invalidated_count") or 0),
        "setup_type_counts": dict(section.get("setup_type_counts") or {}),
        "status_counts": dict(section.get("status_counts") or {}),
        "shape_counts": dict(section.get("shape_counts") or {}),
        "context_counts": dict(section.get("context_counts") or {}),
        "top_reasons": list(section.get("top_reasons") or [])[:10],
        "items": rows,
        "primary_message_ko": "주요 Setup 없음" if not any(row.get("primary_setup") for row in rows) else "",
        "safety": {
            "ready_allowed": False,
            "candidate_promotion_allowed": False,
            "opportunity_rank_allowed": False,
            "order_intent_allowed": False,
            "live_order_allowed": False,
            "recommended_position_size_multiplier": 0,
        },
        "operator_message_ko": "SetupRouter V3는 setup 유형만 관측하며 READY/주문으로 승격하지 않습니다.",
    }


def _row_strategy_context(item: dict[str, Any]) -> dict[str, Any]:
    direct = item.get("strategy_context_v3")
    if isinstance(direct, dict):
        return dict(direct)
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("strategy_context_v3"), dict):
        return dict(metadata.get("strategy_context_v3") or {})
    details = item.get("details")
    if isinstance(details, dict):
        context = details.get("strategy_context_v3")
        if isinstance(context, dict):
            return dict(context)
    return {}


def _position_risk(position_risk: dict[str, Any], exit_engine: dict[str, Any]) -> dict[str, Any]:
    exit_by_position = {str(item.get("position_id") or ""): dict(item or {}) for item in list(exit_engine.get("ready_exit_decisions") or [])}
    rows = []
    for raw in list(position_risk.get("positions") or [])[:20]:
        item = dict(raw or {})
        exit_decision = exit_by_position.get(str(item.get("position_id") or ""), {})
        reasons = list(item.get("reason_codes") or []) + list(exit_decision.get("reason_codes") or [])
        rows.append(
            {
                "code": item.get("code", ""),
                "name": item.get("name", ""),
                "entry_price": int(_num(item.get("entry_price") or item.get("avg_entry_price"))),
                "current_price": int(_num(item.get("current_price"))),
                "current_return_pct": _num(item.get("current_return_pct")),
                "max_return_pct": _num(item.get("max_return_pct")),
                "max_drawdown_pct": _num(item.get("max_drawdown_pct")),
                "holding_minutes": int(_num(item.get("holding_minutes"))),
                "theme_status": dict(item.get("details") or {}).get("theme_status", ""),
                "market_side": item.get("market_side") or dict(item.get("details") or {}).get("market_side", ""),
                "market_status": item.get("side_market_regime") or dict(item.get("details") or {}).get("market_status", ""),
                "position_market_action": item.get("position_market_action") or dict(item.get("details") or {}).get("position_market_action", ""),
                "recommended_exit_ratio": _num(item.get("recommended_exit_ratio")),
                "structure_intact": bool(item.get("structure_intact", True)),
                "actual_order_status": "OBSERVE_ONLY",
                "exit_status": exit_decision.get("exit_status", ""),
                "exit_reason": exit_decision.get("exit_reason", ""),
                "stop_loss_price": int(_num(item.get("stop_loss_price"))),
                "trailing_stop_price": int(_num(item.get("trailing_stop_price"))),
                "take_profit_price": int(_num(item.get("take_profit_price"))),
                "risk_message_ko": _reason_summary_ko(reasons) or "보유 리스크 관찰",
                "reason_codes": reasons,
            }
        )
    return {
        "status": position_risk.get("status", "EMPTY"),
        "portfolio_risk_level": position_risk.get("portfolio_risk_level", "NORMAL"),
        "open_position_count": int(position_risk.get("open_position_count") or 0),
        "total_exposure": int(_num(position_risk.get("total_exposure"))),
        "gross_exposure_limit_krw": int(_num(position_risk.get("gross_exposure_limit_krw"))),
        "gross_pending_buy_exposure_krw": int(_num(position_risk.get("gross_pending_buy_exposure_krw"))),
        "gross_reserved_exposure_krw": int(_num(position_risk.get("gross_reserved_exposure_krw"))),
        "gross_available_exposure_krw": int(_num(position_risk.get("gross_available_exposure_krw"))),
        "gross_utilization_pct": _num(position_risk.get("gross_utilization_pct")),
        "composite_market_mode": position_risk.get("composite_market_mode", ""),
        "systemic_risk_off": bool(position_risk.get("systemic_risk_off")),
        "market_context_fresh": bool(position_risk.get("market_context_fresh")),
        "market_side_budgets": dict(position_risk.get("market_side_budgets") or {}),
        "stop_new_entry_by_side": dict(position_risk.get("stop_new_entry_by_side") or {}),
        "reduce_only_by_side": dict(position_risk.get("reduce_only_by_side") or {}),
        "position_market_action_counts": dict(position_risk.get("position_market_action_counts") or {}),
        "theme_exposure_top": _top_mapping(position_risk.get("theme_exposure") or {}, limit=5),
        "market_side_exposure": dict(position_risk.get("market_side_exposure") or {}),
        "unrealized_pnl_pct": _num(position_risk.get("unrealized_pnl_pct")),
        "daily_realized_pnl_pct": _num(position_risk.get("daily_realized_pnl_pct")),
        "stop_new_entry_recommended": bool(position_risk.get("stop_new_entry_recommended")),
        "kill_switch_recommended": bool(position_risk.get("kill_switch_recommended")),
        "top_position_risks": [_reason_item(item) for item in list(position_risk.get("top_position_risks") or [])[:10]],
        "exit_now_count": int(exit_engine.get("exit_now_count") or 0),
        "scale_out_count": int(exit_engine.get("scale_out_count") or 0),
        "wait_confirmation_count": int(exit_engine.get("wait_confirmation_count") or 0),
        "positions": rows,
    }


def _exit_watch(exit_engine: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": exit_engine.get("status", "EMPTY"),
        "exit_now_count": int(exit_engine.get("exit_now_count") or 0),
        "scale_out_count": int(exit_engine.get("scale_out_count") or 0),
        "wait_confirmation_count": int(exit_engine.get("wait_confirmation_count") or 0),
        "data_wait_count": int(exit_engine.get("data_wait_count") or 0),
        "top_exit_reasons": [_reason_item(item) for item in list(exit_engine.get("top_exit_reasons") or [])[:10]],
        "ready_exit_decisions": list(exit_engine.get("ready_exit_decisions") or [])[:10],
    }


def _order_manager(order_manager: dict[str, Any], gateway: dict[str, Any], commands: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for raw in list(order_manager.get("managed_orders") or [])[:12]:
        item = dict(raw or {})
        sent_at = str(item.get("sent_at") or item.get("created_at") or "")
        cancel_sec = int(item.get("cancel_after_sec") or 0)
        rows.append(
            {
                "code": item.get("code", ""),
                "side": item.get("side", ""),
                "quantity": int(item.get("quantity") or 0),
                "price": int(item.get("price") or 0),
                "filled_quantity": int(item.get("filled_quantity") or 0),
                "remaining_quantity": int(item.get("remaining_quantity") or 0),
                "status": item.get("status", ""),
                "order_no": item.get("order_no", ""),
                "sent_at": sent_at,
                "cancel_after_sec": cancel_sec,
                "cancel_due_at": _plus_seconds(sent_at, cancel_sec) if sent_at and cancel_sec else "",
                "reason_summary_ko": _order_reason_summary(item),
            }
        )
    return {
        "status": order_manager.get("status", "DISABLED"),
        "enabled": bool(order_manager.get("enabled")),
        "order_manager_enabled": bool(order_manager.get("enabled")),
        "observe_only": bool(order_manager.get("observe_only")),
        "intent_enabled": bool(order_manager.get("intent_enabled")),
        "local_order_enabled": bool(order_manager.get("local_order_enabled")),
        "gateway_command_enqueue_enabled": bool(order_manager.get("gateway_command_enqueue_enabled")),
        "send_order_allowed": bool(order_manager.get("send_order_allowed")),
        "mode": order_manager.get("mode", "OBSERVE"),
        "live_sim_orders_allowed": bool(order_manager.get("live_sim_orders_allowed")),
        "broker_env": order_manager.get("broker_env") or _broker_env_from_gateway(gateway),
        "account_mode": _account_mode(gateway, order_manager),
        "account": order_manager.get("account") or gateway.get("account") or "",
        "account_whitelisted": bool(order_manager.get("account_whitelisted")),
        "real_broker_blocked": (order_manager.get("broker_env") or _broker_env_from_gateway(gateway)) == "REAL",
        "kill_switch_state": order_manager.get("kill_switch_state", "NORMAL"),
        "today_buy_order_count": int(order_manager.get("today_buy_order_count") or 0),
        "today_sell_order_count": int(order_manager.get("today_sell_order_count") or 0),
        "created_intent_count": int(order_manager.get("created_intent_count") or 0),
        "risk_approved_count": int(order_manager.get("risk_approved_count") or 0),
        "risk_rejected_count": int(order_manager.get("risk_rejected_count") or 0),
        "local_order_created_count": int(order_manager.get("local_order_created_count") or 0),
        "command_blocked_observe_only_count": int(order_manager.get("command_blocked_observe_only_count") or 0),
        "queued_command_count": int(order_manager.get("queued_command_count") or 0),
        "reconcile_required_count": int(order_manager.get("reconcile_required_count") or 0),
        "stop_new_buy": bool(order_manager.get("stop_new_buy")),
        "reduce_only": bool(order_manager.get("reduce_only")),
        "risk_state": order_manager.get("risk_state", ""),
        "open_order_count": int(order_manager.get("open_order_count") or 0),
        "pending_cancel_count": int(order_manager.get("pending_cancel_count") or 0),
        "rejected_order_count": int(order_manager.get("rejected_order_count") or 0),
        "last_order_at": order_manager.get("last_order_at", ""),
        "last_reject_reason": order_manager.get("last_reject_reason", ""),
        "next_cancel_due_count": sum(1 for row in rows if row["remaining_quantity"] > 0 and row["cancel_due_at"]),
        "command_queue_health": _command_queue_health(commands, gateway),
        "orders": rows,
        "warnings": list(order_manager.get("warnings") or []),
    }


def _market_relative_strength_shadow(shadow: dict[str, Any], outcomes: dict[str, Any]) -> dict[str, Any]:
    summary = dict(outcomes.get("summary") or {})
    recommendations = dict(outcomes.get("recommendations") or {})
    scenario_counts = dict(shadow.get("scenario_counts") or {})
    status_counts = dict(shadow.get("shadow_status_counts") or {})
    recent_rows = []
    for raw in list(shadow.get("recent_candidates") or [])[:8]:
        item = dict(raw or {})
        scenario = str(item.get("shadow_scenario") or "")
        variant = str(item.get("shadow_variant") or "")
        promotion_eligible = False
        recent_rows.append(
            {
                "code": item.get("code") or "",
                "name": item.get("name") or "",
                "market_side": item.get("market_side") or "",
                "side_market_regime": item.get("side_market_regime") or "",
                "shadow_scenario": scenario,
                "shadow_variant": variant,
                "shadow_status": item.get("shadow_status") or "",
                "actual_market_action": item.get("actual_market_action") or "",
                "actual_entry_status": item.get("actual_entry_status") or "",
                "counterfactual_action": item.get("counterfactual_action") or "",
                "relative_strength_vs_index_pct": _num(item.get("relative_strength_vs_index_pct")),
                "price_location": item.get("price_location") or "",
                "promotion_eligible": promotion_eligible,
                "shadow_filter_passed": bool(item.get("shadow_filter_passed")),
                "review_candidate": bool(item.get("review_candidate")) and scenario == "WEAK_SIDE_STRICT_SHADOW" and variant == "STRICT",
                "actual_order_mode_label": "분석/관측전용",
            }
        )
    candidate_count = int(summary.get("shadow_candidate_count") or status_counts.get("SHADOW_CANDIDATE") or 0)
    labeled_count = int(summary.get("labeled_count") or 0)
    status = str(shadow.get("status") or outcomes.get("status") or "NO_DATA")
    recommendation = str(recommendations.get("current_recommendation") or "NO_DATA")
    return {
        "status": status,
        "enabled": bool(shadow.get("enabled")),
        "outcome_status": str(outcomes.get("status") or ""),
        "calculated_at": shadow.get("calculated_at") or outcomes.get("generated_at") or "",
        "title_ko": "분리장세 상대강도 관찰",
        "operator_message_ko": "실제 주문과 분리된 분석/관측전용 shadow 검증입니다.",
        "actual_order_mode_label": "분석/관측전용",
        "analysis_only": True,
        "creates_orders": False,
        "order_intent_allowed": False,
        "dry_run_order_allowed": False,
        "live_order_allowed": False,
        "shadow_candidate_count": candidate_count,
        "tracked_event_count": int(outcomes.get("tracked_event_count") or 0),
        "matured_pending_count": int(outcomes.get("matured_pending_count") or 0),
        "persisted_outcome_count": int(outcomes.get("persisted_outcome_count") or 0),
        "healthy_side_reduced_count": int(summary.get("healthy_side_reduced_count") or scenario_counts.get("HEALTHY_SIDE_REDUCED") or 0),
        "counterpart_data_degraded_count": int(scenario_counts.get("COUNTERPART_DATA_DEGRADED_REDUCED") or 0),
        "weak_side_shadow_candidate_count": int(summary.get("weak_side_shadow_candidate_count") or scenario_counts.get("WEAK_SIDE_STRICT_SHADOW") or 0),
        "risk_off_side_diagnostic_count": int(summary.get("risk_off_side_diagnostic_count") or scenario_counts.get("RISK_OFF_SIDE_DIAGNOSTIC") or 0),
        "systemic_excluded_count": int(summary.get("systemic_excluded_count") or scenario_counts.get("SYSTEMIC_RISK_EXCLUDED") or 0),
        "data_wait_excluded_count": int(summary.get("market_side_unresolved_count") or scenario_counts.get("DATA_WAIT_EXCLUDED") or 0),
        "labeled_count": labeled_count,
        "avg_mfe_10m": _num(summary.get("avg_mfe_10m")),
        "avg_mae_10m": _num(summary.get("avg_mae_10m")),
        "avg_return_10m": _num(summary.get("avg_return_10m")),
        "shadow_edge_rate_10m": _num(summary.get("shadow_edge_rate_10m")),
        "shadow_risk_case_rate_10m": _num(summary.get("shadow_risk_case_rate_10m")),
        "current_recommendation": recommendation,
        "risk_off_promotion_allowed": False,
        "recent_candidates": recent_rows,
    }


def _pre_market_check(base: dict[str, Any]) -> dict[str, Any]:
    report = dict(base.get("pre_market_check") or {})
    if not report:
        report = pre_market_report_empty()
    items = list(report.get("items") or [])
    item_by_key = {str(item.get("key") or ""): dict(item or {}) for item in items if isinstance(item, dict)}
    return {
        "schema_version": report.get("schema_version", "pre_market_check.v1"),
        "trade_date": report.get("trade_date", ""),
        "checked_at": report.get("checked_at", ""),
        "requested_mode": report.get("requested_mode", "OBSERVE"),
        "go_no_go": report.get("go_no_go", "MANUAL_REVIEW_REQUIRED"),
        "summary_status": report.get("summary_status", "UNKNOWN"),
        "pass_count": int(report.get("pass_count") or 0),
        "warn_count": int(report.get("warn_count") or 0),
        "fail_count": int(report.get("fail_count") or 0),
        "unknown_count": int(report.get("unknown_count") or 0),
        "blocking_reasons": list(report.get("blocking_reasons") or [])[:10],
        "warning_reasons": list(report.get("warning_reasons") or [])[:10],
        "operator_message_ko": report.get("operator_message_ko", "수동 확인 필요"),
        "recommended_action_ko": report.get("recommended_action_ko", ""),
        "broker_env": _item_detail(item_by_key, "broker_environment", "broker_env", ""),
        "account_whitelist": _item_detail(item_by_key, "account_whitelist", "account_whitelisted", False),
        "gateway_heartbeat": item_by_key.get("gateway_heartbeat", {}),
        "sqlite_health": item_by_key.get("sqlite_operational_store", {}),
        "kill_switch": item_by_key.get("kill_switch", {}),
        "pending_reconcile": item_by_key.get("pending_reconcile", {}),
        "data_preload": {
            "warehouse_preload": item_by_key.get("warehouse_preload", {}),
            "theme_board_latest": item_by_key.get("theme_board_latest", {}),
            "market_regime_index_watch": item_by_key.get("market_regime_index_watch", {}),
        },
        "recommended_items": [
            dict(item or {})
            for item in items
            if str(dict(item or {}).get("status") or "") in {"FAIL", "WARN", "UNKNOWN"}
        ][:8],
        "items": items,
    }


def _wait_block_reasons(base: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    market = _prefer_runtime_section(base.get("market_regime"), runtime.get("market_regime"))
    themes = _prefer_runtime_section(base.get("theme_board"), runtime.get("theme_board"))
    entry = _prefer_runtime_section(base.get("entry_engine"), runtime.get("entry_engine"))
    exit_engine = _prefer_runtime_section(base.get("exit_engine"), runtime.get("exit_engine"))
    pos_risk = _prefer_runtime_section(base.get("position_risk"), runtime.get("position_risk"))
    order_manager = _prefer_runtime_section(base.get("order_manager"), runtime.get("order_manager"))
    market_rs_shadow = _prefer_runtime_section(base.get("market_relative_strength_shadow"), runtime.get("market_relative_strength_shadow"))
    reason_sources = [
        base.get("candidate_ingestion"),
        themes,
        market,
        entry,
        market_rs_shadow,
        exit_engine,
        pos_risk,
        order_manager,
        base.get("pre_market_check"),
    ]
    counts: Counter[str] = Counter()
    samples: defaultdict[str, list[str]] = defaultdict(list)
    for source in reason_sources:
        _collect_reasons(source, counts, samples)
    rows = []
    for reason, count in counts.most_common(12):
        rows.append(
            {
                "reason_code": reason,
                "reason_ko": reason_label_ko(reason),
                "count": int(count),
                "severity": reason_severity(reason),
                "affected_codes_sample": samples.get(reason, [])[:5],
                "suggested_action_ko": suggested_action_ko(reason),
            }
        )
    return {"items": rows, "total_reason_count": sum(counts.values())}


def _system_health(base: dict[str, Any], runtime: dict[str, Any], gateway: dict[str, Any], commands: dict[str, Any]) -> dict[str, Any]:
    transport = dict(base.get("transport") or {})
    market = _prefer_runtime_section(base.get("market_regime"), runtime.get("market_regime"))
    themes = _prefer_runtime_section(base.get("theme_board"), runtime.get("theme_board"))
    entry = _prefer_runtime_section(base.get("entry_engine"), runtime.get("entry_engine"))
    exit_engine = _prefer_runtime_section(base.get("exit_engine"), runtime.get("exit_engine"))
    return {
        "summary_status": "정상" if bool(gateway.get("heartbeat_ok")) and not runtime.get("last_error") else "점검",
        "gateway_heartbeat": {
            "ok": bool(gateway.get("heartbeat_ok")),
            "age_sec": gateway.get("heartbeat_age_sec"),
            "last_heartbeat_at": gateway.get("last_heartbeat_at", ""),
        },
        "kiwoom_logged_in": bool(gateway.get("kiwoom_logged_in")),
        "command_queue_depth": int(commands.get("queued_count") or gateway.get("pending_command_count") or 0),
        "command_ack_latency": transport.get("ack_latency_p95_ms"),
        "event_latency": transport.get("event_latency_p95_ms"),
        "transport_status": transport.get("mode") or transport.get("transport_mode") or "",
        "sqlite_health": "OK",
        "runtime_cycle_duration": runtime.get("last_cycle_duration_ms"),
        "last_exception": runtime.get("last_error", ""),
        "data_freshness": _data_freshness_status(
            gateway,
            market,
            runtime,
            reboot_v2_enabled=bool(runtime.get("reboot_v2_enabled")) or str(runtime.get("runtime_profile") or "").upper() != "LEGACY",
        ),
        "latest_tick_age_percentile": transport.get("price_tick_age_p95_sec"),
        "latest_theme_board_at": themes.get("calculated_at", ""),
        "latest_market_regime_at": market.get("calculated_at", ""),
        "latest_entry_engine_at": entry.get("calculated_at", ""),
        "latest_exit_engine_at": exit_engine.get("calculated_at", ""),
        "collapsed_by_default": True,
    }


def _safety_banners(payload: dict[str, Any]) -> list[dict[str, Any]]:
    status = dict(payload.get("v2_status") or {})
    market = dict(payload.get("market_overview") or {})
    order = dict(payload.get("order_manager") or {})
    pre_market = dict(payload.get("pre_market_check") or {})
    banners: list[dict[str, Any]] = []
    decision = str(pre_market.get("go_no_go") or "")
    if decision == "NO_GO":
        banners.append(
            {
                "severity": "critical",
                "message_ko": f"장전 점검 NO-GO: {pre_market.get('operator_message_ko') or '운영 금지'}",
                "reason_code": "PRE_MARKET_NO_GO",
            }
        )
    elif decision == "MANUAL_REVIEW_REQUIRED":
        banners.append(
            {
                "severity": "warning",
                "message_ko": "장전 점검: 수동 확인 필요",
                "reason_code": "PRE_MARKET_MANUAL_REVIEW_REQUIRED",
            }
        )
    elif decision == "GO_OBSERVE":
        banners.append(
            {
                "severity": "info",
                "message_ko": "장전 점검: 관찰 전용 가능",
                "reason_code": "PRE_MARKET_GO_OBSERVE",
            }
        )
    elif decision == "GO_LIVE_SIM_LIMITED":
        banners.append(
            {
                "severity": "info",
                "message_ko": "장전 점검: 모의주문 제한 가능, 실계좌 아님",
                "reason_code": "PRE_MARKET_GO_LIVE_SIM_LIMITED",
            }
        )
    if status.get("broker_env") == "REAL" or order.get("real_broker_blocked"):
        banners.append({"severity": "critical", "message_ko": "실계좌 환경 감지: 모든 자동주문 차단", "reason_code": "REAL_BROKER_BLOCKED"})
    if not order.get("live_sim_orders_allowed"):
        banners.append({"severity": "info", "message_ko": "모의주문 비활성: 관찰 전용", "reason_code": "LIVE_SIM_FLAG_DISABLED"})
    if order.get("account") and not order.get("account_whitelisted"):
        banners.append({"severity": "warning", "message_ko": "계좌 whitelist 대기", "reason_code": "ACCOUNT_NOT_WHITELISTED"})
    if status.get("kill_switch_state") in {"KILL_SWITCH_ACTIVE", "STOP_NEW_BUY", "REDUCE_ONLY"}:
        banners.append({"severity": "critical", "message_ko": "킬스위치 활성: 신규 매수 차단", "reason_code": "KILL_SWITCH_BLOCKS_BUY"})
    if market.get("systemic_risk_off"):
        banners.append({"severity": "critical", "message_ko": "SYSTEMIC RISK_OFF: 전체 신규진입 차단", "reason_code": "SYSTEMIC_RISK_OFF_BLOCK"})
    elif market.get("risk_off_detected"):
        banners.append({"severity": "warning", "message_ko": "분리장세 주의: 위험 시장만 차단하고 정상 시장은 축소 관찰", "reason_code": "SPLIT_MARKET_HEALTHY_SIDE_REDUCED"})
    if status.get("data_freshness_status") == "STALE":
        banners.append({"severity": "warning", "message_ko": "Gateway heartbeat 지연: 데이터 신선도 점검", "reason_code": "GATEWAY_HEARTBEAT_STALE"})
    if not order.get("order_manager_enabled"):
        banners.append({"severity": "info", "message_ko": "OrderManager 비활성: 관찰 전용", "reason_code": "ORDER_MANAGER_DISABLED"})
    return banners


def _collect_reasons(source: Any, counts: Counter[str], samples: defaultdict[str, list[str]]) -> None:
    if isinstance(source, list):
        for item in source:
            _collect_reasons(item, counts, samples)
        return
    if not isinstance(source, dict):
        return
    if source.get("reason_code"):
        _add_reason(counts, samples, source.get("reason_code"), source.get("code"))
    for key in ("reason_codes", "warnings"):
        for reason in list(source.get(key) or []):
            _add_reason(counts, samples, reason, source.get("code"))
    for key in (
        "top_reasons",
        "top_wait_reasons",
        "top_block_reasons",
        "top_position_risks",
        "top_exit_reasons",
        "top_wait_or_block_reasons",
    ):
        for item in list(source.get(key) or []):
            if isinstance(item, dict):
                reason = item.get("reason") or item.get("reason_code")
                count = int(item.get("count") or 1)
                if reason:
                    counts[str(reason)] += count
                    code = item.get("code")
                    if code:
                        samples[str(reason)].append(str(code))
    for key in ("items", "top_ready_candidates", "positions", "managed_orders", "orders", "ready_exit_decisions", "recent_candidates"):
        for item in list(source.get(key) or []):
            _collect_reasons(item, counts, samples)


def _add_reason(counts: Counter[str], samples: defaultdict[str, list[str]], reason: Any, code: Any = None) -> None:
    text = str(reason or "").strip()
    if not text:
        return
    counts[text] += 1
    if code:
        sample = str(code)
        if sample not in samples[text]:
            samples[text].append(sample)


def _reason_item(item: Any) -> dict[str, Any]:
    data = dict(item or {}) if isinstance(item, dict) else {"reason": item, "count": 1}
    reason = str(data.get("reason") or data.get("reason_code") or "")
    return {
        "reason_code": reason,
        "reason_ko": reason_label_ko(reason),
        "count": int(data.get("count") or 1),
        "severity": reason_severity(reason),
        "suggested_action_ko": suggested_action_ko(reason),
    }


def _item_detail(item_by_key: dict[str, dict[str, Any]], key: str, detail_key: str, default: Any) -> Any:
    item = dict(item_by_key.get(key) or {})
    details = dict(item.get("details") or {})
    return details.get(detail_key, default)


def _entry_bucket(item: dict[str, Any], pending_codes: set[str]) -> str:
    code = str(item.get("code") or "")
    if code and code in pending_codes:
        return "ORDER_PENDING"
    status = str(item.get("entry_status") or item.get("display_state") or "").upper()
    if status == "OBSERVE_READY":
        return "TIMING_READY"
    if status in {"PRICE_WAIT"}:
        return "SETUP_READY"
    if status in {"WAIT", "DATA_WAIT", "MARKET_WAIT", "THEME_WAIT"}:
        return "WAIT"
    if status in {"HARD_BLOCK", "BLOCKED"}:
        return "BLOCK"
    if bool(item.get("entry_ready_allowed")):
        return "TIMING_READY"
    return "WAIT"


def _entry_reasons(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("reason_codes", "entry_reason_codes", "market_reason_codes"):
        values.extend(str(reason) for reason in list(item.get(key) or []) if reason)
    return _dedupe(values)


def _entry_message(bucket: str) -> str:
    if bucket == "TIMING_READY":
        return "진입 준비 관찰 상태입니다."
    if bucket == "SETUP_READY":
        return "기본 조건은 통과했지만 가격 타이밍을 기다립니다."
    if bucket == "ORDER_PENDING":
        return "OrderManager가 LIVE_SIM 주문 상태를 관리 중입니다."
    if bucket == "BLOCK":
        return "리스크 또는 정책 조건으로 차단 중입니다."
    return "시장, 테마, 가격 또는 데이터 조건을 기다립니다."


def _reason_summary_ko(reasons: Iterable[Any]) -> str:
    labels = [reason_label_ko(reason) for reason in _dedupe([str(item) for item in reasons if item])]
    return ", ".join(labels[:3])


def _order_reason_summary(item: dict[str, Any]) -> str:
    details = dict(item.get("details") or {})
    risk = dict(details.get("risk") or {})
    reasons = list(risk.get("reason_codes") or item.get("reason_codes") or [])
    if reasons:
        return _reason_summary_ko(reasons)
    status = str(item.get("status") or "")
    if status == "FILLED":
        return "체결 완료"
    if status == "PARTIALLY_FILLED":
        return "부분 체결, 잔량 관리"
    if status == "CANCEL_PENDING":
        return "미체결 취소 요청 중"
    if "REJECT" in status:
        return "주문 거부 확인 필요"
    return status or "-"


def _market_message(status: str, market: dict[str, Any], systemic_risk_off: bool = False) -> str:
    if systemic_risk_off:
        return "시장국면: SYSTEMIC_RISK_OFF - 전체 신규진입 차단"
    composite = str(market.get("composite_market_mode") or "")
    if composite in {"SPLIT_KOSPI_ON", "SPLIT_KOSDAQ_ON"}:
        return market.get("market_operator_message_ko") or f"시장국면: {_composite_mode_label_ko(composite)}"
    if status == "RISK_OFF":
        return "시장국면: 분리장세 RISK_OFF - 해당 시장만 신규진입 차단"
    if status == "SELECTIVE":
        return "시장국면: SELECTIVE - 대장주 중심 축소 진입만 관찰"
    if status == "EXPANSION":
        return "시장국면: EXPANSION - 주도테마 확산 여부 관찰"
    if status == "WEAK":
        return "시장국면: WEAK - 신규진입 대기, 보유 리스크 우선"
    if status == "DATA_WAIT" or int(market.get("data_wait_count") or 0) > 0:
        return "시장 데이터 대기 - 지수 tick 또는 breadth 부족"
    return f"시장국면: {status or 'UNKNOWN'}"


def _status_message(label: str, market_status: str, broker_env: str, kill: str, live_sim_allowed: bool, systemic_risk_off: bool = False) -> str:
    if broker_env == "REAL":
        return "실계좌 환경 감지: 모든 자동주문 차단"
    if kill in {"KILL_SWITCH_ACTIVE", "REDUCE_ONLY", "STOP_NEW_BUY"}:
        return "킬스위치 활성: 신규 매수 차단"
    if systemic_risk_off:
        return "SYSTEMIC RISK_OFF: 전체 신규진입 차단"
    if not live_sim_allowed:
        return "관찰 전용 또는 모의주문 비활성 상태입니다."
    return f"Dashboard V2 상태: {label}"


def _data_freshness_status(
    gateway: dict[str, Any],
    market: dict[str, Any],
    runtime: dict[str, Any],
    *,
    reboot_v2_enabled: bool = False,
) -> str:
    if not reboot_v2_enabled:
        return "DISABLED"
    if not bool(gateway.get("heartbeat_ok")):
        return "STALE"
    if any(str(dict(runtime.get(name) or {}).get("status") or "").upper() == "ERROR" for name in ("theme_board", "market_regime", "entry_engine")):
        return "ERROR"
    if not reboot_v2_enabled and runtime.get("data_warmup_status") not in {"", None, "ready"}:
        return "WAIT_DATA"
    if bool(market.get("enabled")) and market.get("global_status") == "DATA_WAIT":
        return "WAIT_DATA"
    return "FRESH"


def _systemic_risk_off(market: dict[str, Any]) -> bool:
    if "systemic_risk_off" in market:
        return bool(market.get("systemic_risk_off"))
    kospi = str(market.get("kospi_status") or "").upper()
    kosdaq = str(market.get("kosdaq_status") or "").upper()
    return (kospi == "RISK_OFF" and kosdaq in {"RISK_OFF", "WEAK"}) or (
        kosdaq == "RISK_OFF" and kospi in {"RISK_OFF", "WEAK"}
    )


def _composite_mode_label_ko(mode: str) -> str:
    return {
        "BROAD_RISK_ON": "전반적 위험 선호",
        "SPLIT_KOSPI_ON": "분리장세 - KOSPI 우위",
        "SPLIT_KOSDAQ_ON": "분리장세 - KOSDAQ 우위",
        "MIXED_CAUTION": "혼조 주의",
        "DATA_DEGRADED": "시장 데이터 일부 대기",
        "SYSTEMIC_RISK_OFF": "시스템 전체 위험",
        "MARKET_CLOSED": "장 종료",
    }.get(str(mode or ""), str(mode or "UNKNOWN"))


def _stage_status(name: str, section: Any, pipeline_status: dict[str, Any]) -> dict[str, Any]:
    data = dict(section or {}) if isinstance(section, dict) else {}
    enabled = bool(data.get("enabled")) if "enabled" in data else bool(pipeline_status.get(name))
    status = str(data.get("status") or ("WARMUP" if enabled else "DISABLED")).upper()
    if not enabled and status in {"", "WARMUP", "DATA_WAIT"}:
        status = "DISABLED"
    return {
        "enabled": enabled,
        "status": status,
        "last_run_at": data.get("calculated_at") or data.get("last_run_at") or "",
        "last_input_at": data.get("last_input_at") or data.get("last_seed_batch_at") or "",
        "input_count": int(data.get("input_count") or data.get("candidate_count") or data.get("seed_symbol_count") or data.get("open_position_count") or 0),
        "output_count": int(data.get("output_count") or data.get("selected_count") or data.get("observe_ready_count") or data.get("active_theme_count") or 0),
        "blocking_reason": data.get("blocking_reason") or data.get("paused_reason") or data.get("reason") or "",
        "next_required_action": data.get("next_required_action") or ("ENABLE_CONFIG" if not enabled else ""),
    }


def _broker_env_from_gateway(gateway: dict[str, Any]) -> str:
    payload = dict(gateway.get("last_heartbeat_payload") or {})
    values = [
        gateway.get("broker_env"),
        gateway.get("account_mode"),
        gateway.get("server_mode"),
        gateway.get("server_gubun"),
        gateway.get("mode"),
        payload.get("broker_env"),
        payload.get("account_mode"),
        payload.get("server_mode"),
        payload.get("server_gubun"),
        payload.get("mode"),
    ]
    normalized = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
    if normalized & {"REAL", "PROD", "PRODUCTION", "LIVE", "LIVE_REAL", "0"}:
        return "REAL"
    if normalized & {"SIM", "SIMULATION", "MOCK", "PAPER", "DEMO", "LIVE_SIM", "1"}:
        return "SIMULATION"
    return "UNKNOWN"


def _account_mode(gateway: dict[str, Any], order_manager: dict[str, Any]) -> str:
    payload = dict(gateway.get("last_heartbeat_payload") or {})
    return str(
        order_manager.get("account_mode")
        or gateway.get("account_mode")
        or payload.get("account_mode")
        or payload.get("server_mode")
        or payload.get("server_gubun")
        or ""
    )


def _command_queue_health(commands: dict[str, Any], gateway: dict[str, Any]) -> str:
    queued = int(commands.get("queued_count") or gateway.get("pending_command_count") or 0)
    if queued >= 1000:
        return "위험"
    if queued >= 50:
        return "주의"
    return "정상"


def _top_mapping(value: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    rows = [{"key": str(key), "value": item} for key, item in dict(value or {}).items()]
    return rows[:limit]


def _data_quality_status(item: dict[str, Any]) -> str:
    flags = list(item.get("data_quality_flags") or [])
    if flags:
        return "데이터대기"
    ratio = _num(item.get("realtime_valid_ratio"))
    if ratio and ratio < 0.7:
        return "데이터대기"
    return "정상"


def _plus_seconds(value: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (base + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _num(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
