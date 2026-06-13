from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from storage.db import TradingDatabase
from trading.theme_engine.backfill import THEME_BACKFILL_PURPOSE
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.conservative_reason_outcomes import (
    ConservativeReasonOutcomeAnalyzer,
    empty_payload as conservative_reason_empty_payload,
    snapshot_payload as conservative_reason_snapshot_payload,
)
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.shadow_small_entry_promotion import (
    ShadowSmallEntryPromotionAnalyzer,
    empty_payload as shadow_small_entry_empty_payload,
    snapshot_payload as shadow_small_entry_snapshot_payload,
)
from trading_app.shadow_small_entry_ops import (
    ShadowSmallEntryOpsService,
    snapshot_payload as shadow_small_entry_ops_snapshot_payload,
)
from trading_app.shadow_small_entry_pilot import (
    ShadowSmallEntryPilotService,
    empty_payload as shadow_small_entry_pilot_empty_payload,
    snapshot_payload as shadow_small_entry_pilot_snapshot_payload,
)
from trading_app.theme_lab_gate_reason_outcomes import ThemeLabGateReasonOutcomeAnalyzer


GATE_ORDER = {"READY": 0, "READY_SMALL": 1, "WAIT": 2, "OBSERVE": 3, "BLOCKED": 4}
ROLE_ORDER = {"LEADER": 0, "CO_LEADER": 1, "FOLLOWER": 2, "LATE_LAGGARD": 3, "WEAK_MEMBER": 4, "OVERHEATED": 5}
SNAPSHOT_STALE_THRESHOLD_SEC = 60
DISPLAY_WAIT_ORDER = {
    "LATE_CHASE_TEMP_WAIT": 0,
    "WAIT_MARKET_CONFIRMATION_PENDING": 1,
    "WAIT_MARKET_RECOVERY_PENDING": 1,
    "WAIT_MARKET_STATE_CONSERVATIVE_FALLBACK": 1,
    "WAIT_CANDIDATE_MARKET_RISK_OFF": 1,
    "WAIT_CANDIDATE_MARKET_WEAK": 1,
    "WAIT_FAILED_BREAKOUT": 2,
    "WAIT_DEEP_PULLBACK": 2,
    "WAIT_PRICE_LOCATION_DATA": 2,
    "WAIT_PRICE_LOCATION_WARMUP": 2,
    "WAIT_PRICE_LOCATION_PROVISIONAL": 2,
    "WAIT_PRICE_LOCATION_UNKNOWN": 2,
    "WAIT_DATA_SUPPORT_NOT_READY": 2,
    "WAIT_DATA_LATEST_TICK_STALE": 2,
}
NAVER_THEME_SYNC_SOURCE = "naver_theme_universe"


def build_theme_lab_dashboard_snapshot(
    db: TradingDatabase,
    *,
    runtime_status: dict[str, Any] | None = None,
    gateway_state: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    raw = db.latest_theme_lab_flow_result()
    theme_source_sync = _theme_source_sync_status(db)
    if not raw:
        return _empty_snapshot(runtime_status=runtime_status, theme_source_sync=theme_source_sync, db=db, gateway_state=gateway_state)

    themes = _as_list(raw.get("theme_rankings") or raw.get("theme_condition_snapshots"))
    gate_decisions = _as_list(raw.get("gate_decisions"))
    watchset = _sorted_watchset(_merge_watchset_gate_decisions(_as_list(raw.get("watchset_snapshots")), gate_decisions))
    condition_counts = _condition_theme_counts(db, raw)
    data_quality = _data_quality(raw, watchset)
    runtime = _runtime_context(runtime_status)
    freshness = _snapshot_freshness(raw, now=now)
    data_quality = {**data_quality, **_freshness_quality_fields(freshness)}
    entry_candidates = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    chart_universe = _chart_universe(themes, watchset, entry_candidates)
    selected = _select_chart(chart_universe, watchset)
    selected_watch = next((item for item in watchset if item.get("symbol") == selected.get("symbol")), {})

    backfill_runtime = _theme_backfill_runtime(raw, gateway_state)
    gateway = _gateway_context(gateway_state)
    backfill_status_by_theme = _theme_backfill_status_by_theme(gateway_state)
    ranked_themes = _ranked_theme_rows(themes, condition_counts, backfill_status_by_theme=backfill_status_by_theme)
    summary = _summary(ranked_themes, watchset, entry_candidates, data_quality, runtime=runtime, freshness=freshness)
    trade_date = _snapshot_trade_date(raw)
    gate_reason_outcomes = _theme_lab_gate_reason_outcomes(db, trade_date=trade_date)
    live_sim_audit = _live_sim_audit(db, gateway_state=gateway_state, trade_date=trade_date)
    conservative_reason_outcomes = _conservative_reason_outcomes(db, trade_date=trade_date)
    shadow_small_entry_promotion = _shadow_small_entry_promotion(db, trade_date=trade_date)
    shadow_small_entry_ops = _shadow_small_entry_ops(db, gateway_state=gateway_state, trade_date=trade_date)
    shadow_small_entry_pilot = _shadow_small_entry_pilot(db, gateway_state=gateway_state, trade_date=trade_date)

    return {
        "available": True,
        "source": "theme_lab_flow_snapshots",
        "created_at": raw.get("created_at", ""),
        "calculated_at": raw.get("calculated_at", ""),
        "last_updated_at": _now_time(),
        "runtime": runtime,
        "gateway": gateway,
        "theme_backfill_runtime": backfill_runtime,
        "theme_source_sync": theme_source_sync,
        **_freshness_quality_fields(freshness),
        "market": _market(raw.get("market_status") or {}),
        "condition_statuses": _condition_statuses(db, gateway_state),
        "data_quality": data_quality,
        "ranked_themes": ranked_themes[:30],
        "watchset": [_watch_row(item) for item in watchset],
        "entry_candidates": [_entry_row(item, index) for index, item in enumerate(entry_candidates, start=1)],
        "chart_universe": chart_universe,
        "selected_chart": selected,
        "gate_detail": _gate_detail(selected_watch),
        "gate_reason_outcomes": gate_reason_outcomes,
        "live_sim_audit": live_sim_audit,
        "conservative_reason_outcomes": conservative_reason_outcomes,
        "shadow_small_entry_promotion": shadow_small_entry_promotion,
        "shadow_small_entry_ops": shadow_small_entry_ops,
        "shadow_small_entry_pilot": shadow_small_entry_pilot,
        "shadow_small_entry": gate_reason_outcomes.get("shadow_small_entry") or _empty_shadow_small_entry(),
        "shadow_small_entry_ab": gate_reason_outcomes.get("shadow_small_entry_ab") or _empty_shadow_small_entry_ab(),
        "summary": summary,
    }


def _empty_snapshot(
    *,
    runtime_status: dict[str, Any] | None = None,
    theme_source_sync: dict[str, Any] | None = None,
    db: TradingDatabase | None = None,
    gateway_state: Any | None = None,
) -> dict[str, Any]:
    runtime = _runtime_context(runtime_status)
    freshness = _empty_freshness()
    return {
        "available": False,
        "source": "theme_lab_flow_snapshots",
        "created_at": "",
        "calculated_at": "",
        "last_updated_at": _now_time(),
        "runtime": runtime,
        "gateway": _gateway_context(None),
        "theme_source_sync": theme_source_sync or _empty_theme_source_sync_status(),
        "theme_backfill_runtime": {
            "enabled": False,
            "paused_reason": "SNAPSHOT_UNAVAILABLE",
            "queued_count": 0,
            "dispatched_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "skipped_count": 0,
            "expired_count": 0,
            "observe_pilot_active": False,
            "history_window": "recent_500_commands",
            "parser_miss_count": 0,
            "parser_miss_ratio": None,
            "backfill_expired_before_dispatch_count": 0,
            "gateway_command_queue_depth": 0,
            "tr_backfill_caused_ready_count": 0,
        },
        **_freshness_quality_fields(freshness),
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
        "gate_reason_outcomes": _empty_gate_reason_outcomes(),
        "live_sim_audit": _live_sim_audit(db, gateway_state=gateway_state, trade_date=datetime.now().date().isoformat()) if db is not None else _live_sim_audit_empty(),
        "conservative_reason_outcomes": _conservative_reason_outcomes(db, trade_date=datetime.now().date().isoformat()) if db is not None else conservative_reason_empty_payload(),
        "shadow_small_entry_promotion": _shadow_small_entry_promotion(db, trade_date=datetime.now().date().isoformat()) if db is not None else shadow_small_entry_empty_payload(),
        "shadow_small_entry_ops": _shadow_small_entry_ops(db, gateway_state=gateway_state, trade_date=datetime.now().date().isoformat()) if db is not None else _shadow_small_entry_ops_empty(),
        "shadow_small_entry_pilot": _shadow_small_entry_pilot(db, gateway_state=gateway_state, trade_date=datetime.now().date().isoformat()) if db is not None else shadow_small_entry_pilot_empty_payload(datetime.now().date().isoformat()),
        "shadow_small_entry": _empty_shadow_small_entry(),
        "shadow_small_entry_ab": _empty_shadow_small_entry_ab(),
        "summary": _empty_summary(runtime=runtime, freshness=freshness),
    }


def _live_sim_audit(db: TradingDatabase, *, gateway_state: Any | None, trade_date: str = "") -> dict[str, Any]:
    try:
        return LiveSimLifecycleAuditor(db, gateway_state=gateway_state).build_report(trade_date=trade_date or None, limit=1000)
    except Exception as exc:
        payload = _live_sim_audit_empty()
        payload.update({"status": "ERROR", "error": str(exc)})
        return payload


def _live_sim_audit_empty() -> dict[str, Any]:
    return {
        "available": False,
        "status": "NO_DATA",
        "summary": {},
        "order_funnel": [],
        "open_orders": [],
        "reconcile_issues": [],
        "position_issues": [],
        "cancel_issues": [],
        "last_updated_at": "",
        "operator": {"status_message_ko": "LIVE_SIM audit 데이터가 아직 없습니다.", "top_actions": []},
    }


def _conservative_reason_outcomes(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    try:
        report = ConservativeReasonOutcomeAnalyzer(db).build_report(trade_date=trade_date or None, limit=10000)
        return conservative_reason_snapshot_payload(report)
    except Exception as exc:
        return conservative_reason_empty_payload(str(exc))


def _shadow_small_entry_promotion(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    try:
        report = ShadowSmallEntryPromotionAnalyzer(db).build_report(
            trade_date=trade_date or None,
            limit=10000,
            include_traces=False,
        )
        return shadow_small_entry_snapshot_payload(report)
    except Exception as exc:
        return shadow_small_entry_empty_payload(str(exc))


def _shadow_small_entry_ops(db: TradingDatabase, *, gateway_state: Any | None, trade_date: str = "") -> dict[str, Any]:
    try:
        return shadow_small_entry_ops_snapshot_payload(
            ShadowSmallEntryOpsService(db, gateway_state=gateway_state).status(trade_date=trade_date or None)
        )
    except Exception as exc:
        empty = _shadow_small_entry_ops_empty()
        empty.update({"status": "ERROR", "preflight_status": "ERROR", "preflight_blocking_reasons": [str(exc)]})
        return empty


def _shadow_small_entry_ops_empty() -> dict[str, Any]:
    return {
        "available": False,
        "status": "NO_DATA",
        "mode": "observe_only",
        "order_enabled": False,
        "preflight_status": "NO_DATA",
        "preflight_blocking_reasons": [],
        "activation_armed": False,
        "activation_expires_at": "",
        "last_status_change_at": "",
        "last_status_change_reason": "",
        "today": {
            "promotion_count": 0,
            "submitted_count": 0,
            "filled_count": 0,
            "open_position_count": 0,
            "total_notional_krw": 0,
            "realized_pnl_krw": 0,
            "unrealized_pnl_krw": 0,
            "order_reject_count": 0,
            "unknown_submit_count": 0,
            "reconcile_required_count": 0,
        },
        "limits": {},
        "audit": {},
        "warnings": [],
        "operator_message_ko": "Shadow Small Entry 운영 trace가 아직 없습니다.",
        "last_updated_at": "",
    }


def _shadow_small_entry_pilot(db: TradingDatabase, *, gateway_state: Any | None, trade_date: str = "") -> dict[str, Any]:
    try:
        return shadow_small_entry_pilot_snapshot_payload(
            ShadowSmallEntryPilotService(db, gateway_state=gateway_state).status(trade_date=trade_date or None)
        )
    except Exception as exc:
        return shadow_small_entry_pilot_empty_payload(trade_date or datetime.now().date().isoformat(), str(exc))


def _theme_lab_gate_reason_outcomes(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    try:
        report = ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date=trade_date or None, limit=10000)
    except Exception as exc:
        empty = _empty_gate_reason_outcomes()
        empty.update({"status": "ERROR", "error": str(exc)})
        return empty
    return {
        "status": report.get("status") or "READY",
        "report_id": report.get("report_id") or "",
        "trade_date": report.get("trade_date") or "",
        "generated_at": report.get("generated_at") or "",
        "summary": report.get("summary") or {},
        "top_missed_opportunity_reasons": list(report.get("top_missed_opportunity_reasons") or [])[:10],
        "shadow_small_entry": report.get("shadow_small_entry") or _empty_shadow_small_entry(),
        "shadow_small_entry_ab": report.get("shadow_small_entry_ab") or _empty_shadow_small_entry_ab(),
    }


def _snapshot_trade_date(raw: dict[str, Any]) -> str:
    for key in ("calculated_at", "created_at"):
        value = str(raw.get(key) or "")
        if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
            return value[:10]
    return ""


def _empty_gate_reason_outcomes() -> dict[str, Any]:
    return {
        "status": "NO_DATA",
        "report_id": "",
        "trade_date": "",
        "generated_at": "",
        "summary": {},
        "top_missed_opportunity_reasons": [],
        "shadow_small_entry": _empty_shadow_small_entry(),
        "shadow_small_entry_ab": _empty_shadow_small_entry_ab(),
    }


def _empty_shadow_small_entry() -> dict[str, Any]:
    return {
        "summary": {
            "candidate_count": 0,
            "labeled_count": 0,
            "win_count_15m": 0,
            "win_rate_15m": 0.0,
            "risk_case_count_15m": 0,
            "risk_case_rate_15m": 0.0,
            "avg_mfe_15m_pct": None,
            "avg_mae_15m_pct": None,
            "missed_opportunity_capture_count": 0,
            "missed_opportunity_reduction_estimate": 0.0,
            "position_size_multiplier": 0.25,
        },
        "by_reason": [],
        "by_role": [],
        "by_market_status": [],
        "top_candidates": [],
        "rejected_reason_counts": {},
    }


def _empty_shadow_small_entry_ab() -> dict[str, Any]:
    return {
        "scenario_count": 0,
        "scenarios": [],
        "best_scenarios": [],
        "matrix": {
            "by_multiplier": [],
            "by_min_condition_level": [],
            "by_roles": [],
            "by_risk_set": [],
        },
        "notes": [],
    }


def _theme_source_sync_status(db: TradingDatabase) -> dict[str, Any]:
    run = ThemeEngineRepository(db).latest_source_sync_run(NAVER_THEME_SYNC_SOURCE)
    if run is None:
        return _empty_theme_source_sync_status()
    return {
        "id": run.id,
        "source": run.source,
        "status": run.status,
        "theme_count": run.theme_count,
        "member_count": run.member_count,
        "error_count": run.error_count,
        "message": run.message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "details": dict(run.details or {}),
    }


def _empty_theme_source_sync_status() -> dict[str, Any]:
    return {
        "id": None,
        "source": NAVER_THEME_SYNC_SOURCE,
        "status": "NOT_SYNCED",
        "theme_count": 0,
        "member_count": 0,
        "error_count": 0,
        "message": "",
        "started_at": "",
        "finished_at": "",
        "details": {},
    }


def _merge_watchset_gate_decisions(watchset: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions_by_symbol = {str(item.get("symbol") or ""): item for item in decisions if item.get("symbol")}
    decision_fields = (
        "status",
        "reason_codes",
        "blocked_reason",
        "risk_level",
        "risk_reason_codes",
        "position_size_multiplier",
        "recheck_after_sec",
        "price_location_status",
        "price_location_score",
        "price_location_reason_codes",
        "candidate_market",
        "candidate_market_source",
        "candidate_market_status",
        "candidate_market_action",
        "candidate_index_return_pct",
        "global_market_status",
        "kospi_market_status",
        "kosdaq_market_status",
        "kospi_return_pct",
        "kosdaq_return_pct",
        "candidate_breadth_pct",
        "candidate_breadth_ready",
        "candidate_breadth_sample_count",
        "candidate_breadth_source",
        "candidate_valid_quote_ratio",
        "candidate_breadth_trust_level",
        "candidate_breadth_gate_usable",
        "candidate_breadth_diagnostic_only",
        "candidate_market_raw_status",
        "candidate_market_confirmed_status",
        "candidate_market_confirmation_pending",
        "candidate_market_recovery_pending",
        "market_side_reason_codes",
        "market_side_data_quality_flags",
    )
    merged: list[dict[str, Any]] = []
    for item in watchset:
        row = dict(item)
        decision = decisions_by_symbol.get(str(row.get("symbol") or ""))
        if decision:
            if decision.get("status") and row.get("gate_status") in (None, ""):
                row["gate_status"] = decision.get("status")
            for field in decision_fields:
                value = decision.get(field)
                if value not in (None, "", [], {}):
                    row[field] = value
            risk_off = dict(decision.get("risk_off_entry_details") or {})
            if risk_off:
                row["risk_off_entry"] = risk_off
                for key in (
                    "risk_off_entry_enabled",
                    "risk_off_entry_observe_only",
                    "risk_off_entry_allowed",
                    "risk_off_entry_rejected_reason",
                    "risk_off_entry_failed_checks",
                    "risk_off_entry_passed_checks",
                    "risk_off_entry_blocking_data_flags",
                    "risk_off_shadow_entry",
                    "risk_off_relative_strength_pct",
                    "risk_off_candidate_breadth_pct",
                    "risk_off_candidate_index_return_pct",
                    "risk_off_max_position_size_multiplier",
                    "risk_off_exit_hint",
                ):
                    row[key] = risk_off.get(key)
        merged.append(row)
    return merged


def _gateway_context(gateway_state: Any | None) -> dict[str, Any]:
    if gateway_state is None:
        return {
            "connected": False,
            "heartbeat_ok": False,
            "kiwoom_logged_in": False,
            "orderable": False,
            "connection_state": "UNKNOWN",
        }
    try:
        snapshot = gateway_state.snapshot().to_dict()
    except Exception:
        return {
            "connected": False,
            "heartbeat_ok": False,
            "kiwoom_logged_in": False,
            "orderable": False,
            "connection_state": "UNKNOWN",
        }
    return {
        "connected": bool(snapshot.get("connected")),
        "heartbeat_ok": bool(snapshot.get("heartbeat_ok")),
        "kiwoom_logged_in": bool(snapshot.get("kiwoom_logged_in")),
        "orderable": bool(snapshot.get("orderable")),
        "connection_state": str(snapshot.get("connection_state") or "UNKNOWN"),
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
    *,
    runtime: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime or _runtime_context(None)
    freshness = freshness or _empty_freshness()
    gates = Counter(str(item.get("gate_status") or "UNKNOWN") for item in watchset)
    displays = Counter(str(item.get("display_status") or item.get("gate_status") or "UNKNOWN") for item in watchset)
    theme_statuses = Counter(_theme_status_bucket(item.get("theme_status")) for item in ranked_themes)
    leader_count = sum(1 for item in watchset if item.get("stock_role") == "LEADER")
    co_leader_count = sum(1 for item in watchset if item.get("stock_role") == "CO_LEADER")
    late_laggard_count = sum(1 for item in watchset if item.get("stock_role") == "LATE_LAGGARD")
    live_guard_passed = sum(1 for item in watchset if item.get("live_order_guard_passed"))
    ready_like = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    live_guard_blocked = sum(1 for item in ready_like if not item.get("live_order_guard_passed"))
    risk_off_small_entries = [item for item in watchset if _is_risk_off_small_entry(item)]
    risk_off_reject_reasons = Counter(
        str(item.get("risk_off_entry_rejected_reason") or "UNKNOWN")
        for item in watchset
        if item.get("risk_off_entry_enabled") and not item.get("risk_off_entry_allowed")
    )
    market_pending_count = sum(1 for item in watchset if _is_market_pending(item))
    market_confirmation_pending_count = sum(
        1
        for item in watchset
        if item.get("candidate_market_confirmation_pending") or item.get("market_confirmation_pending")
    )
    market_recovery_pending_count = sum(
        1
        for item in watchset
        if item.get("candidate_market_recovery_pending") or item.get("market_recovery_pending")
    )
    market_risk_off_wait_count = sum(
        1
        for item in watchset
        if str(item.get("display_status") or "") == "WAIT_CANDIDATE_MARKET_RISK_OFF"
        or str(item.get("candidate_market_confirmed_status") or item.get("market_confirmed_status") or "") == "RISK_OFF"
    )
    data_not_ready_count = sum(1 for item in watchset if _is_data_not_ready(item))
    theme_data_not_ready_count = sum(1 for item in ranked_themes if _is_theme_data_not_ready(item))
    top_theme = ranked_themes[0] if ranked_themes else {}
    status, message = _operation_status_message(
        theme_count=len(ranked_themes),
        ready_count=gates.get("READY", 0),
        ready_small_count=gates.get("READY_SMALL", 0),
        market_pending_count=market_pending_count,
        market_confirmation_pending_count=market_confirmation_pending_count,
        market_recovery_pending_count=market_recovery_pending_count,
        market_risk_off_wait_count=market_risk_off_wait_count,
        data_not_ready_count=data_not_ready_count,
        theme_data_not_ready_count=theme_data_not_ready_count,
        late_chase_wait_count=displays.get("LATE_CHASE_TEMP_WAIT", 0),
        chase_risk_blocked_count=displays.get("CHASE_RISK_BLOCKED", 0),
        live_guard_passed_count=live_guard_passed,
        live_guard_blocked_count=live_guard_blocked,
        order_candidate_count=len(entry_candidates),
        data_quality_status=str(data_quality.get("status") or "UNKNOWN"),
        watchset_size=len(watchset),
        runtime=runtime,
        freshness=freshness,
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
        "market_confirmation_pending_count": market_confirmation_pending_count,
        "market_recovery_pending_count": market_recovery_pending_count,
        "market_risk_off_wait_count": market_risk_off_wait_count,
        "data_not_ready_count": data_not_ready_count,
        "diagnostic_only_count": sum(1 for item in watchset if item.get("diagnostic_only")),
        "submittable_count": sum(1 for item in watchset if item.get("submittable")),
        "runtime_order_intent_created_count": sum(1 for item in watchset if item.get("runtime_order_intent_created")),
        "virtual_order_created_count": sum(1 for item in watchset if item.get("virtual_order_created")),
        "risk_off_small_entry_candidate_count": sum(1 for item in watchset if item.get("risk_off_entry_enabled")),
        "risk_off_small_entry_allowed_count": sum(1 for item in risk_off_small_entries if item.get("risk_off_entry_allowed")),
        "risk_off_small_entry_observe_only_count": sum(
            1 for item in risk_off_small_entries if item.get("risk_off_entry_observe_only")
        ),
        "risk_off_small_entry_rejected_count": sum(
            1 for item in watchset if item.get("risk_off_entry_enabled") and not item.get("risk_off_entry_allowed")
        ),
        "risk_off_small_entry_reject_reason_counts": dict(risk_off_reject_reasons),
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
        **_summary_runtime_fields(runtime, freshness),
        "operation_status": status,
        "operation_message_ko": message,
    }


def _empty_summary(
    *,
    runtime: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime or _runtime_context(None)
    freshness = freshness or _empty_freshness()
    status, message = _operation_status_message(
        theme_count=0,
        ready_count=0,
        ready_small_count=0,
        market_pending_count=0,
        market_confirmation_pending_count=0,
        market_recovery_pending_count=0,
        market_risk_off_wait_count=0,
        data_not_ready_count=0,
        theme_data_not_ready_count=0,
        late_chase_wait_count=0,
        chase_risk_blocked_count=0,
        live_guard_passed_count=0,
        live_guard_blocked_count=0,
        order_candidate_count=0,
        data_quality_status="BROKEN",
        watchset_size=0,
        runtime=runtime,
        freshness=freshness,
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
        "market_confirmation_pending_count": 0,
        "market_recovery_pending_count": 0,
        "market_risk_off_wait_count": 0,
        "data_not_ready_count": 0,
        "diagnostic_only_count": 0,
        "submittable_count": 0,
        "runtime_order_intent_created_count": 0,
        "virtual_order_created_count": 0,
        "risk_off_small_entry_candidate_count": 0,
        "risk_off_small_entry_allowed_count": 0,
        "risk_off_small_entry_observe_only_count": 0,
        "risk_off_small_entry_rejected_count": 0,
        "risk_off_small_entry_reject_reason_counts": {},
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
        **_summary_runtime_fields(runtime, freshness),
        "operation_status": status,
        "operation_message_ko": message,
    }


def _runtime_context(runtime_status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_status, dict):
        return {
            "known": False,
            "enabled": None,
            "auto_start": None,
            "running": None,
            "mode": "",
            "last_cycle_at": "",
            "cycle_count": 0,
            "worker_stage": "",
            "status": "UNKNOWN",
            "realtime_data_quality": {},
            "realtime_reliability_score": 0.0,
            "realtime_reliability_bucket": "NO_DATA",
        }
    running = bool(runtime_status.get("running"))
    realtime_quality = dict(runtime_status.get("realtime_data_quality") or {})
    return {
        "known": True,
        "enabled": bool(runtime_status.get("enabled")),
        "auto_start": bool(runtime_status.get("auto_start")),
        "running": running,
        "mode": str(runtime_status.get("mode") or ""),
        "last_cycle_at": str(runtime_status.get("last_cycle_at") or ""),
        "cycle_count": int(runtime_status.get("cycle_count") or 0),
        "worker_stage": str(runtime_status.get("worker_stage") or ""),
        "status": "ACTIVE" if running else "RUNTIME_INACTIVE",
        "realtime_data_quality": realtime_quality,
        "realtime_reliability_score": float(realtime_quality.get("realtime_reliability_score") or 0.0),
        "realtime_reliability_bucket": str(realtime_quality.get("realtime_reliability_bucket") or "NO_DATA"),
    }


def _snapshot_freshness(raw: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    parsed = _parse_snapshot_time(str(raw.get("calculated_at") or "")) or _parse_snapshot_time(str(raw.get("created_at") or ""))
    if parsed is None:
        return _empty_freshness()
    if now is None:
        current = datetime.now(tz=parsed.tzinfo) if parsed.tzinfo else datetime.now()
    else:
        current = now
    if parsed.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=parsed.tzinfo)
    elif parsed.tzinfo is None and current.tzinfo is not None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    age_sec = max(0, int((current - parsed).total_seconds()))
    return {
        "snapshot_age_sec": age_sec,
        "snapshot_age_label": _age_label(age_sec),
        "snapshot_stale": age_sec > SNAPSHOT_STALE_THRESHOLD_SEC,
        "snapshot_stale_threshold_sec": SNAPSHOT_STALE_THRESHOLD_SEC,
    }


def _empty_freshness() -> dict[str, Any]:
    return {
        "snapshot_age_sec": None,
        "snapshot_age_label": "",
        "snapshot_stale": False,
        "snapshot_stale_threshold_sec": SNAPSHOT_STALE_THRESHOLD_SEC,
    }


def _freshness_quality_fields(freshness: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_age_sec": freshness.get("snapshot_age_sec"),
        "snapshot_age_label": freshness.get("snapshot_age_label", ""),
        "snapshot_stale": bool(freshness.get("snapshot_stale")),
        "snapshot_stale_threshold_sec": freshness.get("snapshot_stale_threshold_sec", SNAPSHOT_STALE_THRESHOLD_SEC),
    }


def _summary_runtime_fields(runtime: dict[str, Any], freshness: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_known": bool(runtime.get("known")),
        "runtime_status": runtime.get("status", "UNKNOWN"),
        "runtime_enabled": runtime.get("enabled"),
        "runtime_auto_start": runtime.get("auto_start"),
        "runtime_running": runtime.get("running"),
        "runtime_mode": runtime.get("mode", ""),
        "runtime_last_cycle_at": runtime.get("last_cycle_at", ""),
        "runtime_cycle_count": runtime.get("cycle_count", 0),
        **_freshness_quality_fields(freshness),
    }


def _parse_snapshot_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _age_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


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


def _is_theme_data_not_ready(item: dict[str, Any]) -> bool:
    flags = set(item.get("data_quality_flags") or [])
    return any(str(flag).startswith("MISSING") or str(flag).startswith("STALE") for flag in flags)


def _operation_status_message(
    *,
    theme_count: int,
    ready_count: int,
    ready_small_count: int,
    market_pending_count: int,
    market_confirmation_pending_count: int,
    market_recovery_pending_count: int,
    market_risk_off_wait_count: int,
    data_not_ready_count: int,
    theme_data_not_ready_count: int,
    late_chase_wait_count: int,
    chase_risk_blocked_count: int,
    live_guard_passed_count: int,
    live_guard_blocked_count: int,
    order_candidate_count: int,
    data_quality_status: str,
    watchset_size: int,
    runtime: dict[str, Any] | None = None,
    freshness: dict[str, Any] | None = None,
) -> tuple[str, str]:
    runtime = runtime or _runtime_context(None)
    freshness = freshness or _empty_freshness()
    data_status = data_quality_status.upper()
    ready_like = ready_count + ready_small_count
    if runtime.get("known") and not runtime.get("running"):
        return "RUNTIME_INACTIVE", "전략 Runtime loop가 꺼져 있어 마지막 ThemeLab 스냅샷만 표시 중입니다."
    if runtime.get("known") and freshness.get("snapshot_stale"):
        age_label = str(freshness.get("snapshot_age_label") or "오래")
        return "SNAPSHOT_STALE", f"ThemeLab 스냅샷이 {age_label} 전 계산되어 최신 운용 상태가 아닙니다."
    if watchset_size == 0:
        if theme_count <= 0 or data_status == "BROKEN":
            return "SNAPSHOT_UNAVAILABLE", "ThemeLabFlow 결과 대기 중입니다."
        if data_status == "DEGRADED" or theme_data_not_ready_count > 0:
            return "WAIT_DATA_QUALITY", "테마 결과는 있으나 지수/현재가 데이터 워밍업 중입니다."
        return "OBSERVE_ONLY", "ThemeLabFlow 결과는 있으나 WatchSet 조건을 통과한 종목이 없습니다."
    if ready_count > 0 and live_guard_passed_count > 0 and data_status not in {"DEGRADED", "BROKEN"}:
        return "READY_TO_TRADE", "READY 후보가 있고 데이터 품질이 정상입니다."
    if ready_like > 0 and live_guard_passed_count == 0 and live_guard_blocked_count > 0:
        return "READY_BUT_LIVE_BLOCKED", "READY 후보는 있으나 LIVE Guard 통과 후보가 없습니다."
    if data_status in {"DEGRADED", "BROKEN"} or data_not_ready_count >= max(1, ready_like + market_pending_count):
        return "WAIT_DATA_QUALITY", "VWAP/지지선/틱 데이터 부족으로 진단 전용 후보가 많습니다."
    if market_confirmation_pending_count > 0:
        return "WAIT_MARKET_CONFIRMATION", "시장 확인 대기 후보가 많아 관찰 우선입니다."
    if market_recovery_pending_count > 0 or market_risk_off_wait_count > 0:
        return "WAIT_MARKET_RISK_OFF", "시장 RISK_OFF가 확인되어 회복 또는 RISK_OFF 소액진입 조건 충족을 대기 중입니다."
    if market_pending_count > 0:
        return "WAIT_MARKET_CONDITION", "시장 조건 대기 후보가 많아 관찰 우선입니다."
    if chase_risk_blocked_count > 0 or late_chase_wait_count >= max(1, ready_like):
        return "RISK_BLOCKED", "추격매수 차단 후보가 많아 신규 진입 대기입니다."
    if order_candidate_count == 0:
        if ready_like == 0 and watchset_size > 0:
            return "OBSERVE_ONLY", "현재 READY 후보가 없어 관찰 우선입니다."
        return "NO_SIGNAL", "현재 주문 후보가 없습니다."
    return "OBSERVE_ONLY", "장중 매수 가능 후보를 관찰 중입니다."


_CONDITION_PURPOSE_LABELS = {
    "theme_lab_alive": "생존 조건식",
    "theme_lab_strong": "강세 조건식",
    "theme_lab_leader": "주도 조건식",
}


def _condition_statuses(db: TradingDatabase, gateway_state: Any | None = None) -> list[dict[str, Any]]:
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
    latest_commands = _latest_condition_commands(gateway_state)
    gateway_snapshot = _gateway_status_dict(gateway_state)
    heartbeat_ok = gateway_snapshot.get("heartbeat_ok")
    gateway_connected = gateway_snapshot.get("connected")
    for purpose, default_name in defaults.items():
        profile = by_purpose.get(purpose)
        command = latest_commands.get(profile.condition_name if profile else default_name, {})
        command_status = str(command.get("status") or "")
        command_error = str(command.get("last_error") or "")
        command_payload = dict(command.get("payload") or {})
        registered = command_status == "ACKED"
        warning = ""
        if not profile:
            warning = "CONDITION_PROFILE_UNRESOLVED"
        elif command_status in {"FAILED", "EXPIRED", "EXPIRED_BEFORE_DISPATCH"}:
            warning = _condition_command_warning(command_status, command_error)
        elif command_status and command_status != "ACKED":
            warning = f"CONDITION_SEND_{command_status}"
        elif profile.last_resolved_index is not None and not command_status:
            warning = "CONDITION_SEND_NOT_CONFIRMED"
        warning_label = _condition_warning_label(warning)
        rows.append(
            {
                "condition_name": profile.condition_name if profile else default_name,
                "purpose": purpose,
                "purpose_label": _CONDITION_PURPOSE_LABELS.get(purpose, purpose),
                "resolved_index": profile.last_resolved_index if profile and profile.last_resolved_index is not None else "UNKNOWN",
                "registered": registered,
                "registered_label": "정상" if registered else "확인 필요",
                "command_status": command_status or "UNKNOWN",
                "command_status_label": _condition_command_status_label(command_status, registered=registered),
                "screen_no": str(command_payload.get("screen_no") or ""),
                "include_count": 0,
                "remove_count": 0,
                "last_event_at": "",
                "warning": warning,
                "warning_label": warning_label,
                "warning_detail": _condition_warning_detail(
                    warning,
                    command_status=command_status,
                    heartbeat_ok=heartbeat_ok,
                    gateway_connected=gateway_connected,
                ),
                "action_hint": _condition_action_hint(
                    warning,
                    heartbeat_ok=heartbeat_ok,
                    gateway_connected=gateway_connected,
                ),
                "gateway_heartbeat_ok": heartbeat_ok,
                "gateway_connected": gateway_connected,
            }
        )
    return rows


def _gateway_status_dict(gateway_state: Any | None) -> dict[str, Any]:
    if gateway_state is None:
        return {}
    try:
        return dict(gateway_state.snapshot().to_dict())
    except Exception:
        return {}


def _latest_condition_commands(gateway_state: Any | None) -> dict[str, dict[str, Any]]:
    if gateway_state is None:
        return {}
    try:
        current_session = _gateway_session_tokens(gateway_state.snapshot().to_dict().get("last_heartbeat_payload") or {})
        records = gateway_state.list_commands(limit=500, include_finished=True, command_type="send_condition")
    except Exception:
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = _record_payload(record)
        name = str(payload.get("condition_name") or "")
        if not name:
            continue
        current = latest.get(name)
        candidate = {
            "status": str(record.get("status") or ""),
            "last_error": str(record.get("last_error") or ""),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "payload": payload,
            "result_payload": dict(record.get("result_payload") or {}),
        }
        if current is not None and not _prefer_condition_command_record(candidate, current, current_session):
            continue
        latest[name] = candidate
    return latest


def _prefer_condition_command_record(
    candidate: dict[str, Any],
    current: dict[str, Any],
    current_session: set[str],
) -> bool:
    candidate_current_ack = str(candidate.get("status") or "").upper() == "ACKED" and _record_matches_session(
        candidate,
        current_session,
    )
    current_current_ack = str(current.get("status") or "").upper() == "ACKED" and _record_matches_session(
        current,
        current_session,
    )
    if candidate_current_ack != current_current_ack:
        return candidate_current_ack
    candidate_created = str(candidate.get("created_at") or "")
    current_created = str(current.get("created_at") or "")
    if candidate_created != current_created:
        return candidate_created > current_created
    return str(candidate.get("updated_at") or "") > str(current.get("updated_at") or "")


def _record_matches_session(record: dict[str, Any], current_session: set[str]) -> bool:
    if not current_session:
        return True
    record_session = _gateway_session_tokens(record.get("result_payload") or {})
    if not record_session:
        return True
    return not record_session.isdisjoint(current_session)


def _gateway_session_tokens(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    tokens: set[str] = set()
    for key in ("ws_session_id", "websocket_session_id", "ws_connection_id", "connection_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            tokens.add(value)
    trace = payload.get("transport_trace")
    if isinstance(trace, dict):
        tokens.update(_gateway_session_tokens(trace))
    return tokens


def _condition_command_warning(status: str, error: str) -> str:
    clean_error = str(error or "").strip()
    if status == "FAILED" and clean_error in {"", "condition sent"}:
        return "CONDITION_SEND_FAILED"
    if status in {"EXPIRED", "EXPIRED_BEFORE_DISPATCH"} and clean_error in {"", status}:
        return "COMMAND_TTL_EXPIRED"
    return clean_error or status


def _condition_command_status_label(status: str, *, registered: bool) -> str:
    normalized = str(status or "").upper()
    if registered or normalized == "ACKED":
        return "등록 확인 완료"
    if normalized == "FAILED":
        return "등록 확인 실패"
    if normalized in {"EXPIRED", "EXPIRED_BEFORE_DISPATCH"}:
        return "등록 명령 만료"
    if normalized in {"QUEUED", "DISPATCHED"}:
        return "등록 처리 중"
    if normalized:
        return "등록 확인 필요"
    return "등록 확인 대기"


def _condition_warning_label(warning: str) -> str:
    normalized = str(warning or "").strip().upper()
    if not normalized:
        return "정상"
    labels = {
        "CONDITION_PROFILE_UNRESOLVED": "조건식 설정 없음",
        "CONDITION_SEND_FAILED": "등록 확인 실패",
        "COMMAND_TTL_EXPIRED": "등록 명령 만료",
        "CONDITION_SEND_NOT_CONFIRMED": "등록 확인 없음",
    }
    if normalized in labels:
        return labels[normalized]
    if normalized.startswith("CONDITION_SEND_"):
        return "등록 확인 필요"
    return "확인 필요"


def _condition_warning_detail(
    warning: str,
    *,
    command_status: str = "",
    heartbeat_ok: Any = None,
    gateway_connected: Any = None,
) -> str:
    normalized = str(warning or "").strip().upper()
    if not normalized:
        return "현재 세션에서 조건식 등록 ACK가 확인됐습니다."
    if normalized == "CONDITION_PROFILE_UNRESOLVED":
        return "조건식 목적에 매핑된 프로필을 찾지 못했습니다. 조건식 이름과 목적 설정을 확인해야 합니다."
    if normalized == "CONDITION_SEND_FAILED":
        detail = "키움 조건검색 등록 명령을 보낸 뒤 성공 ACK가 확정되지 않았습니다."
    elif normalized == "COMMAND_TTL_EXPIRED":
        detail = "조건식 등록 명령이 게이트웨이에서 처리되기 전에 유효 시간이 지났습니다."
    elif normalized == "CONDITION_SEND_NOT_CONFIRMED":
        detail = "조건식 인덱스는 해석됐지만 현재 세션의 등록 명령 이력이 확인되지 않았습니다."
    elif normalized.startswith("CONDITION_SEND_"):
        detail = f"조건식 등록 명령 상태가 {str(command_status or 'UNKNOWN').upper()}입니다."
    else:
        detail = str(warning or "조건식 등록 상태 확인이 필요합니다.")
    if gateway_connected is False:
        return f"{detail} 키움 게이트웨이 연결 상태부터 확인하세요."
    if heartbeat_ok is False:
        return f"{detail} 현재 게이트웨이 heartbeat가 정상으로 확인되지 않아 재등록 ACK가 지연될 수 있습니다."
    return detail


def _condition_action_hint(warning: str, *, heartbeat_ok: Any = None, gateway_connected: Any = None) -> str:
    if not str(warning or "").strip():
        return ""
    if gateway_connected is False:
        return "키움 Open API 게이트웨이 연결과 로그인을 먼저 정상화한 뒤 조건식 등록 상태를 다시 확인하세요."
    if heartbeat_ok is False:
        return "게이트웨이 heartbeat가 정상화되는지 확인한 뒤 런타임의 조건식 재등록 ACK를 다시 확인하세요."
    return "조건식 재등록 명령이 ACK로 돌아오는지 런타임/게이트웨이 상태를 확인하세요."


def _data_quality(raw: dict[str, Any], watchset: list[dict[str, Any]]) -> dict[str, Any]:
    data = dict(raw.get("data_quality") or {})
    price_flags = [flag for item in watchset for flag in item.get("data_quality_flags", []) + item.get("price_location_data_quality_flags", [])]
    missing_current_price = int(_first_not_none(data.get("current_price_missing_count"), data.get("missing_current_price_count"), price_flags.count("MISSING_CURRENT_PRICE")) or 0)
    missing_vwap = int(_first_not_none(data.get("vwap_missing_count"), price_flags.count("MISSING_VWAP")) or 0)
    missing_session_high = int(_first_not_none(data.get("session_high_missing_count"), price_flags.count("MISSING_SESSION_HIGH")) or 0)
    missing_prev_close = int(_first_not_none(data.get("prev_close_missing_count"), data.get("missing_prev_close_count"), price_flags.count("MISSING_PREV_CLOSE")) or 0)
    candle_missing = int(_first_not_none(data.get("candle_missing_count"), _watchset_candle_missing_count(watchset)) or 0)
    quote_stale = int(_first_not_none(data.get("quote_stale_count"), 0) or 0)
    status = str(data.get("status") or "OK")
    if status == "BROKEN":
        pass
    elif candle_missing or quote_stale >= 5:
        status = "DEGRADED"
    elif any([missing_current_price, missing_vwap, missing_session_high, missing_prev_close, quote_stale]):
        status = "WARNING"
    reasons = _data_quality_reasons(
        candle_missing=candle_missing,
        quote_stale=quote_stale,
        missing_current_price=missing_current_price,
        missing_prev_close=missing_prev_close,
        missing_vwap=missing_vwap,
        missing_session_high=missing_session_high,
    )
    return {
        "status": status,
        "quote_stale_count": quote_stale,
        "current_price_missing_count": missing_current_price,
        "prev_close_missing_count": missing_prev_close,
        "candle_missing_count": candle_missing,
        "vwap_missing_count": missing_vwap,
        "session_high_missing_count": missing_session_high,
        "vi_status_supported": bool(data.get("vi_status_supported", False)),
        "theme_mapping_missing_count": int(data.get("theme_mapping_missing_count") or 0),
        "watchset_size": len(watchset),
        "realtime_subscription_count": int(data.get("realtime_subscription_count") or 0),
        "realtime_subscription_limit": int(data.get("realtime_subscription_limit") or 0),
        "reasons": reasons,
        "message": _data_quality_message(status, reasons),
    }


def _watchset_candle_missing_count(watchset: list[dict[str, Any]]) -> int:
    missing = 0
    for item in watchset:
        if int(item.get("completed_minute_bar_count") or 0) > 0:
            continue
        if _as_list(item.get("recent_candles_1m")):
            continue
        if bool(item.get("minute_bar_present")):
            continue
        missing += 1
    return missing


def _data_quality_reasons(
    *,
    candle_missing: int,
    quote_stale: int,
    missing_current_price: int,
    missing_prev_close: int,
    missing_vwap: int,
    missing_session_high: int,
) -> list[str]:
    reasons = []
    if candle_missing:
        reasons.append(f"WatchSet 분봉 {candle_missing}종목 누락")
    if quote_stale:
        reasons.append(f"stale quote {quote_stale}종목")
    if missing_vwap:
        reasons.append(f"VWAP {missing_vwap}종목 누락")
    if missing_session_high:
        reasons.append(f"당일 고점 {missing_session_high}종목 누락")
    if missing_current_price:
        reasons.append(f"테마 universe 현재가 {missing_current_price}종목 누락")
    if missing_prev_close:
        reasons.append(f"테마 universe 전일종가 {missing_prev_close}종목 누락")
    return reasons


def _theme_row(
    item: dict[str, Any],
    rank: int,
    condition_counts: dict[str, dict[str, Any]] | None = None,
    backfill_status_by_theme: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "prev_close_member_count": quality["prev_close_member_count"],
        "member_price_coverage_ratio": quality["price_coverage_ratio"],
        "member_turnover_coverage_ratio": quality["turnover_coverage_ratio"],
        "missing_current_price_member_count": quality["missing_current_price_member_count"],
        "missing_prev_close_member_count": quality["missing_prev_close_member_count"],
        "missing_current_price_members": quality["missing_current_price_members"],
        "missing_prev_close_members": quality["missing_prev_close_members"],
        "member_data_coverage_label": quality["coverage_label"],
        "top_leader_symbol": leader["symbol"] or item.get("top_leader_symbol") or "",
        "top_leader_name": leader["name"] or item.get("top_leader_name") or "",
        "top_leader_turnover_krw": leader["turnover_krw"],
        "top_leader_return_pct": leader["return_pct"],
        "top_leader_source": leader["source"],
        "data_quality_flags": list(item.get("data_quality_flags") or []),
    }
    row["has_live_price_signal"] = _theme_has_live_price_signal(row)
    row.update(_theme_quality_profile(row))
    row.update((backfill_status_by_theme or {}).get(str(row["theme_id"]), {}))
    return row


def _ranked_theme_rows(
    themes: list[dict[str, Any]],
    condition_counts: dict[str, dict[str, Any]] | None = None,
    *,
    backfill_status_by_theme: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = [
        _theme_row(item, index, condition_counts, backfill_status_by_theme)
        for index, item in enumerate(themes, start=1)
    ]
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


def _theme_backfill_runtime(raw: dict[str, Any], gateway_state: Any | None) -> dict[str, Any]:
    base = dict(raw.get("theme_backfill_runtime") or {})
    base.setdefault("enabled", False)
    base.setdefault("paused_reason", "")
    base.setdefault("queued_count", 0)
    base.setdefault("dispatched_count", 0)
    base.setdefault("success_count", 0)
    base.setdefault("failure_count", 0)
    base.setdefault("skipped_count", 0)
    base.setdefault("expired_count", 0)
    base.setdefault("observe_pilot_active", bool(base.get("enabled") and str(base.get("trading_mode") or "OBSERVE") == "OBSERVE"))
    base.setdefault("history_window", "recent_500_commands")
    base.setdefault("parser_miss_count", 0)
    base.setdefault("parser_miss_ratio", None)
    base.setdefault("backfill_expired_before_dispatch_count", 0)
    base.setdefault("theme_backfill_dispatched_count", 0)
    base.setdefault("theme_backfill_success_count", 0)
    base.setdefault("theme_backfill_failure_count", 0)
    base.setdefault("theme_backfill_skip_count", 0)
    base.setdefault("gateway_command_queue_depth", 0)
    base.setdefault("backfill_paused_by_ready_count", 0)
    base.setdefault("backfill_paused_by_order_count", 0)
    base.setdefault("backfill_paused_by_gateway_unhealthy_count", 0)
    base.setdefault("last_success_at", "")
    base.setdefault("last_failure_at", "")
    base.setdefault("last_failure_reason", "")
    base.setdefault("tr_backfill_caused_ready_count", 0)
    base.setdefault("gateway_unhealthy_detail", "")
    base.setdefault("gateway_unhealthy_display", "")
    if gateway_state is None:
        _annotate_backfill_gateway_detail(base, None)
        return base
    for key in (
        "queued_count",
        "dispatched_count",
        "success_count",
        "failure_count",
        "skipped_count",
        "expired_count",
        "parser_miss_count",
        "backfill_expired_before_dispatch_count",
        "theme_backfill_dispatched_count",
        "theme_backfill_success_count",
        "theme_backfill_failure_count",
        "theme_backfill_skip_count",
        "backfill_paused_by_ready_count",
        "backfill_paused_by_order_count",
        "backfill_paused_by_gateway_unhealthy_count",
    ):
        base[key] = 0
    records = _theme_backfill_records(gateway_state)
    _annotate_backfill_gateway_detail(base, gateway_state)
    base["history_window"] = "recent_500_commands"
    base["gateway_command_queue_depth"] = len([record for record in records if str(record.get("status") or "") in {"QUEUED", "DISPATCHED"}])
    parsed_records = 0
    for record in records:
        status = str(record.get("status") or "")
        if status == "QUEUED":
            base["queued_count"] = int(base.get("queued_count") or 0) + 1
        elif status == "DISPATCHED":
            base["dispatched_count"] = int(base.get("dispatched_count") or 0) + 1
            base["theme_backfill_dispatched_count"] = int(base.get("theme_backfill_dispatched_count") or 0) + 1
        elif status == "ACKED":
            base["success_count"] = int(base.get("success_count") or 0) + 1
            base["theme_backfill_success_count"] = int(base.get("theme_backfill_success_count") or 0) + 1
            base["last_success_at"] = max(str(base.get("last_success_at") or ""), str(record.get("finished_at") or record.get("acked_at") or ""))
        elif status == "FAILED":
            base["failure_count"] = int(base.get("failure_count") or 0) + 1
            base["theme_backfill_failure_count"] = int(base.get("theme_backfill_failure_count") or 0) + 1
            failed_at = str(record.get("finished_at") or "")
            if failed_at >= str(base.get("last_failure_at") or ""):
                base["last_failure_at"] = failed_at
                base["last_failure_reason"] = str(record.get("last_error") or "")
        elif status.startswith("SKIPPED"):
            base["skipped_count"] = int(base.get("skipped_count") or 0) + 1
            base["theme_backfill_skip_count"] = int(base.get("theme_backfill_skip_count") or 0) + 1
            if status == "SKIPPED_READY":
                base["backfill_paused_by_ready_count"] = int(base.get("backfill_paused_by_ready_count") or 0) + 1
            elif status == "SKIPPED_ORDER_PENDING":
                base["backfill_paused_by_order_count"] = int(base.get("backfill_paused_by_order_count") or 0) + 1
            elif status == "SKIPPED_GATEWAY_UNHEALTHY":
                base["backfill_paused_by_gateway_unhealthy_count"] = int(base.get("backfill_paused_by_gateway_unhealthy_count") or 0) + 1
        elif status.startswith("EXPIRED"):
            base["expired_count"] = int(base.get("expired_count") or 0) + 1
            if status == "EXPIRED_BEFORE_DISPATCH":
                base["backfill_expired_before_dispatch_count"] = int(base.get("backfill_expired_before_dispatch_count") or 0) + 1
        result_payload = _record_result_payload(record)
        parser_status = str(result_payload.get("parser_status") or dict(result_payload.get("raw") or {}).get("parser_status") or "")
        if parser_status:
            parsed_records += 1
            if parser_status != "OK":
                base["parser_miss_count"] = int(base.get("parser_miss_count") or 0) + 1
    base["parser_miss_ratio"] = None if parsed_records <= 0 else round(int(base.get("parser_miss_count") or 0) / parsed_records, 4)
    return base


def _annotate_backfill_gateway_detail(base: dict[str, Any], gateway_state: Any | None) -> None:
    paused_reason = str(base.get("paused_reason") or "")
    if paused_reason not in {"GATEWAY_UNHEALTHY", "SKIPPED_GATEWAY_UNHEALTHY"}:
        base["gateway_unhealthy_detail"] = ""
        base["gateway_unhealthy_display"] = ""
        return
    if gateway_state is None:
        base["gateway_unhealthy_detail"] = "GATEWAY_STATE_UNAVAILABLE"
        base["gateway_unhealthy_display"] = "\uac8c\uc774\ud2b8\uc6e8\uc774 \uc0c1\ud0dc \ud655\uc778 \ubd88\uac00"
        return
    try:
        snapshot = gateway_state.snapshot().to_dict()
    except Exception:
        snapshot = {}
    if not bool(snapshot.get("connected")):
        base["gateway_unhealthy_detail"] = "GATEWAY_DISCONNECTED"
        base["gateway_unhealthy_display"] = "\uac8c\uc774\ud2b8\uc6e8\uc774 \ubbf8\uc5f0\uacb0"
    elif not bool(snapshot.get("heartbeat_ok")):
        base["gateway_unhealthy_detail"] = "HEARTBEAT_STALE"
        base["gateway_unhealthy_display"] = "\uac8c\uc774\ud2b8\uc6e8\uc774 heartbeat \uc9c0\uc5f0"
    elif not bool(snapshot.get("kiwoom_logged_in")):
        base["gateway_unhealthy_detail"] = "KIWOOM_NOT_LOGGED_IN"
        base["gateway_unhealthy_display"] = "\ud0a4\uc6c0 \ubbf8\ub85c\uadf8\uc778"
    else:
        base["gateway_unhealthy_detail"] = "UNKNOWN_GATEWAY_UNHEALTHY"
        base["gateway_unhealthy_display"] = "\uac8c\uc774\ud2b8\uc6e8\uc774 \uc0c1\ud0dc \ud655\uc778 \ud544\uc694"


def _theme_backfill_status_by_theme(gateway_state: Any | None) -> dict[str, dict[str, Any]]:
    if gateway_state is None:
        return {}
    status_by_theme: dict[str, dict[str, Any]] = {}
    priority = {
        "DISPATCHED": 0,
        "QUEUED": 1,
        "FAILED": 2,
        "SKIPPED_READY": 3,
        "SKIPPED_ORDER_PENDING": 3,
        "SKIPPED_GATEWAY_UNHEALTHY": 3,
        "SKIPPED_NON_BACKFILL_PENDING": 3,
        "SKIPPED_NOT_OBSERVE_MODE": 3,
        "EXPIRED_BEFORE_DISPATCH": 4,
        "EXPIRED": 4,
        "ACKED": 5,
    }
    for record in _theme_backfill_records(gateway_state):
        payload = _record_payload(record)
        themes = [str(payload.get("primary_theme_id") or "")]
        themes.extend(str(item) for item in payload.get("related_theme_ids") or [])
        status = str(record.get("status") or "")
        item = {
            "theme_backfill_status": _display_backfill_status(status),
            "theme_backfill_raw_status": status,
            "theme_backfill_failure_reason": str(record.get("last_error") or ""),
        }
        for theme_id in {theme for theme in themes if theme}:
            current = status_by_theme.get(theme_id)
            if current is None or priority.get(status, 99) < priority.get(str(current.get("theme_backfill_raw_status") or ""), 99):
                status_by_theme[theme_id] = item
    return status_by_theme


def _theme_backfill_records(gateway_state: Any) -> list[dict[str, Any]]:
    try:
        records = gateway_state.list_commands(limit=500, include_finished=True, command_type="tr_request")
    except Exception:
        return []
    return [record for record in records if _record_payload(record).get("purpose") == THEME_BACKFILL_PURPOSE]


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    command = dict(record.get("command") or {})
    payload = command.get("payload")
    if isinstance(payload, dict):
        return payload
    payload = record.get("payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _record_result_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("result_payload")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _display_backfill_status(status: str) -> str:
    if status == "QUEUED":
        return "대기"
    if status == "DISPATCHED":
        return "진행"
    if status == "ACKED":
        return "완료"
    if status == "FAILED":
        return "실패"
    if status.startswith("SKIPPED"):
        return "스킵"
    if status.startswith("EXPIRED"):
        return "만료"
    return ""


def _theme_has_live_price_signal(row: dict[str, Any]) -> bool:
    return any(
        [
            int(row.get("alive_count") or 0) > 0,
            int(row.get("strong_count") or 0) > 0,
            int(row.get("leader_count") or 0) > 0,
            float(row.get("theme_turnover_krw") or 0) > 0,
        ]
    )


def _theme_quality_profile(row: dict[str, Any]) -> dict[str, Any]:
    flags = set(row.get("data_quality_flags") or [])
    total = int(row.get("eligible_total_members") or 0)
    priced = int(row.get("priced_member_count") or 0)
    turnover = int(row.get("turnover_member_count") or 0)
    price_ratio = float(row.get("member_price_coverage_ratio") or _ratio(priced, total))
    turnover_ratio = float(row.get("member_turnover_coverage_ratio") or _ratio(turnover, total))
    missing_current = int(row.get("missing_current_price_member_count") or 0)
    missing_prev = int(row.get("missing_prev_close_member_count") or 0)
    if "MISSING_CURRENT_PRICE" in flags and missing_current <= 0:
        missing_current = max(0, total - priced)
    if "MISSING_PREV_CLOSE" in flags and missing_prev <= 0:
        missing_prev = max(0, total - priced)

    has_live_signal = bool(row.get("has_live_price_signal"))
    price_text = f"가격 {priced}/{total}({_pct_label(price_ratio)})" if total else "가격 universe 없음"
    turnover_text = f"대금 {turnover}/{total}({_pct_label(turnover_ratio)})" if total else "대금 universe 없음"
    reasons: list[str] = []
    if total:
        reasons.append(f"가격 커버리지 {priced}/{total}({_pct_label(price_ratio)})")
    if missing_current:
        reasons.append(f"현재가 누락 {missing_current}종목")
    if missing_prev:
        reasons.append(f"전일종가 누락 {missing_prev}종목")
    if "EXCLUSION_METADATA_FALLBACK" in flags:
        reasons.append("제외/거래가능 메타데이터 fallback")
    if turnover_ratio < 0.3 and total:
        reasons.append(f"대금 커버리지 {turnover}/{total}({_pct_label(turnover_ratio)})")

    status = "OK"
    tone = "ready"
    label = ""
    action = "정상: 테마 폭과 WatchSet/Gate 판단을 함께 사용"
    backfill_priority = "NONE"
    backfill_trs: list[str] = []

    if not total:
        status = "WARNING"
        tone = "warning"
        label = "테마 universe 비어 있음: 구성종목 매핑 확인 필요"
        action = "테마 순위는 참고용; canonical membership 적재 확인"
    elif not has_live_signal and ("MISSING_CURRENT_PRICE" in flags or price_ratio == 0):
        status = "BROKEN"
        tone = "blocked"
        label = f"테마 폭 산출 불가: {price_text}, 실시간 현재가 보강 필요"
        action = "매매 판단 제외; Kiwoom 현재가/TR 보강 후 재평가"
        backfill_priority = "HIGH"
    elif price_ratio < 0.3 or ("MISSING_CURRENT_PRICE" in flags and missing_current >= max(2, total * 0.7)):
        status = "DEGRADED"
        tone = "blocked"
        label = f"테마 폭 신뢰 낮음: {price_text}, 대장/조건식만 보수 해석"
        action = "실매수 판단은 WatchSet/Gate 품질 통과 종목만 사용"
        backfill_priority = "HIGH"
    elif flags or price_ratio < 0.6 or missing_current or missing_prev:
        status = "WARNING"
        tone = "warning"
        details = [price_text]
        if missing_current:
            details.append(f"현재가 {missing_current}종목 대기")
        if missing_prev:
            details.append(f"전일종가 {missing_prev}종목 보강 필요")
        if "EXCLUSION_METADATA_FALLBACK" in flags:
            details.append("메타 fallback")
        if has_live_signal:
            details.append("대장/조건식 확인")
        label = f"테마 폭 신뢰 보통: {', '.join(details)}"
        action = "테마 폭은 보수 해석; 대장 후보와 WatchSet 품질 우선"
        backfill_priority = "MEDIUM" if missing_current or missing_prev else "LOW"
    else:
        label = ""

    if backfill_priority != "NONE":
        if missing_current or "MISSING_CURRENT_PRICE" in flags:
            backfill_trs.append("opt10001 주식기본정보요청: 현재가/기준가 보강")
        if missing_prev or "MISSING_PREV_CLOSE" in flags:
            backfill_trs.append("opt10081 주식일봉차트조회요청: 전일종가 검증")

    backfill_symbols = _theme_backfill_symbols(row)
    if backfill_priority == "NONE" and backfill_symbols:
        backfill_priority = "LOW"
    return {
        "quality_label": label,
        "theme_quality_status": status,
        "theme_quality_tone": tone,
        "theme_quality_reasons": reasons[:5],
        "theme_quality_action": action,
        "theme_backfill_priority": backfill_priority,
        "theme_backfill_trs": backfill_trs,
        "theme_backfill_symbols": backfill_symbols,
        "theme_quality_coverage_summary": f"{price_text} · {turnover_text}",
    }


def _theme_member_quality(member_hits: list[dict[str, Any]], eligible_total: int) -> dict[str, Any]:
    priced = 0
    turnover = 0
    prev_close = 0
    missing_current = 0
    missing_prev = 0
    missing_current_members: list[str] = []
    missing_prev_members: list[str] = []
    active_total = 0
    for hit in member_hits:
        if hit.get("excluded"):
            continue
        active_total += 1
        flags = {str(flag) for flag in hit.get("data_quality_flags") or []}
        member_label = _member_label(hit)
        has_price_signal = any(
            [
                hit.get("return_pct") is not None,
                hit.get("current_price") is not None,
                hit.get("last_price") is not None,
                hit.get("price") is not None,
                float(hit.get("turnover_krw") or hit.get("turnover") or 0) > 0,
                bool(hit.get("alive_hit") or hit.get("strong_hit") or hit.get("leader_hit")),
            ]
        )
        if "MISSING_CURRENT_PRICE" in flags:
            missing_current += 1
            if member_label:
                missing_current_members.append(member_label)
        elif has_price_signal:
            priced += 1
        has_prev_close_signal = any(
            [
                hit.get("return_pct") is not None,
                hit.get("prev_close") is not None,
                hit.get("previous_close") is not None,
                hit.get("base_price") is not None,
            ]
        )
        if "MISSING_PREV_CLOSE" in flags:
            missing_prev += 1
            if member_label:
                missing_prev_members.append(member_label)
        elif has_prev_close_signal:
            prev_close += 1
        if float(hit.get("turnover_krw") or hit.get("turnover") or 0) > 0:
            turnover += 1
    total = eligible_total or active_total
    return {
        "priced_member_count": priced,
        "turnover_member_count": turnover,
        "prev_close_member_count": prev_close,
        "price_coverage_ratio": _ratio(priced, total),
        "turnover_coverage_ratio": _ratio(turnover, total),
        "missing_current_price_member_count": missing_current,
        "missing_prev_close_member_count": missing_prev,
        "missing_current_price_members": missing_current_members[:8],
        "missing_prev_close_members": missing_prev_members[:8],
        "coverage_label": f"{priced}/{total} 종목 수신" if total else "0/0 종목 수신",
    }


def _theme_backfill_symbols(row: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for key in ("missing_current_price_members", "missing_prev_close_members"):
        for label in row.get(key) or []:
            text = str(label or "").strip()
            if text and text not in symbols:
                symbols.append(text)
    return symbols[:5]


def _member_label(hit: dict[str, Any]) -> str:
    symbol = str(hit.get("symbol") or hit.get("code") or "").strip()
    name = str(hit.get("name") or hit.get("stock_name") or "").strip()
    if symbol and name:
        return f"{name}[{symbol}]"
    return name or symbol


def _pct_label(value: float) -> str:
    return f"{round(float(value or 0) * 100):.0f}%"


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
    if _is_risk_off_small_entry(item):
        ready_type = str(item.get("ready_type") or "")
        if ready_type == "READY_RISK_OFF_SMALL" or str(item.get("order_eligibility") or "") == "BUY_ELIGIBLE_RISK_OFF_SMALL":
            return "READY_RISK_OFF_SMALL"
        return "OBSERVE_RISK_OFF_SMALL_ENTRY"
    reason_values: list[Any] = []
    for key in ("reason_codes", "risk_reason_codes", "price_location_reason_codes", "price_location_readiness_reason_codes"):
        reason_values.extend(item.get(key) or [])
    reasons = {str(reason or "") for reason in reason_values}
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
    if (
        gate == "WAIT"
        and (
            str(item.get("price_location_status") or "") == "FAILED_BREAKOUT"
            or "FAILED_BREAKOUT" in all_reasons
        )
    ):
        return "WAIT_FAILED_BREAKOUT"
    if (
        gate == "WAIT"
        and (
            str(item.get("price_location_status") or "") == "DEEP_PULLBACK"
            or "DEEP_PULLBACK" in all_reasons
        )
    ):
        return "WAIT_DEEP_PULLBACK"
    if (
        gate == "WAIT"
        and (
            "PRICE_LOCATION_CORE_DATA_MISSING" in all_reasons
            or str(item.get("price_location_readiness") or "") == "MISSING_CORE"
        )
    ):
        return "WAIT_PRICE_LOCATION_DATA"
    if (
        gate == "WAIT"
        and (
            "PRICE_LOCATION_PROVISIONAL" in all_reasons
            or bool(item.get("price_location_provisional"))
            or str(item.get("price_location_readiness") or "") == "PROVISIONAL"
        )
    ):
        return "WAIT_PRICE_LOCATION_PROVISIONAL"
    if (
        gate == "WAIT"
        and (
            "PRICE_LOCATION_WARMUP" in all_reasons
            or str(item.get("price_location_readiness") or "") == "WARMUP"
        )
    ):
        return "WAIT_PRICE_LOCATION_WARMUP"
    if (
        gate == "WAIT"
        and (
            str(item.get("price_location_status") or "") == "UNKNOWN"
            or "PRICE_LOCATION_UNKNOWN" in all_reasons
            or "PRICE_LOCATION_DATA_MISSING" in all_reasons
        )
    ):
        return "WAIT_PRICE_LOCATION_UNKNOWN"
    return gate


def _is_risk_off_small_entry(item: dict[str, Any]) -> bool:
    reason_values: list[Any] = []
    for key in ("reason_codes", "risk_reason_codes", "price_location_reason_codes"):
        reason_values.extend(item.get(key) or [])
    reasons = {str(reason).upper() for reason in reason_values}
    return bool(
        item.get("risk_off_entry_allowed")
        or str(item.get("ready_type") or "") == "READY_RISK_OFF_SMALL"
        or str(item.get("final_gate_status") or "") in {"READY_RISK_OFF_SMALL", "OBSERVE_RISK_OFF_SMALL_ENTRY"}
        or bool(reasons & {"RISK_OFF_SMALL_ENTRY", "READY_RISK_OFF_SMALL", "OBSERVE_RISK_OFF_SMALL_ENTRY"})
    )


def _watch_row(item: dict[str, Any]) -> dict[str, Any]:
    gate = _value(item.get("final_gate_status") or item.get("gate_status") or "OBSERVE")
    display_status = _display_status(item, gate)
    candidate_market = item.get("candidate_market") or "UNKNOWN"
    recent_candles_1m = _as_list(item.get("recent_candles_1m"))
    recent_candles_3m = _as_list(item.get("recent_candles_3m"))
    momentum_1m = _first_not_none(item.get("momentum_1m"), _latest_candle_momentum(recent_candles_1m))
    momentum_3m = _first_not_none(item.get("momentum_3m"), _latest_candle_momentum(recent_candles_3m))
    momentum_5m = item.get("momentum_5m")
    momentum_1m_missing_reason = "" if momentum_1m is not None else _momentum_missing_reason(recent_candles_1m, "1분")
    momentum_3m_missing_reason = "" if momentum_3m is not None else _momentum_missing_reason(recent_candles_3m, "3분")
    summary_item = {
        **item,
        "momentum_1m": momentum_1m,
        "momentum_3m": momentum_3m,
        "momentum_5m": momentum_5m,
        "momentum_1m_missing_reason": momentum_1m_missing_reason,
        "momentum_3m_missing_reason": momentum_3m_missing_reason,
    }
    row = {
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
        "candidate_market_status": item.get("candidate_market_status", ""),
        "candidate_market_raw_status": item.get("candidate_market_raw_status") or item.get("market_raw_status", ""),
        "candidate_market_confirmed_status": item.get("candidate_market_confirmed_status")
        or item.get("candidate_market_status")
        or item.get("market_confirmed_status", ""),
        "candidate_market_confirmation_pending": bool(
            item.get("candidate_market_confirmation_pending", item.get("market_confirmation_pending", False))
        ),
        "candidate_market_recovery_pending": bool(
            item.get("candidate_market_recovery_pending", item.get("market_recovery_pending", False))
        ),
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
        "watch_reason": item.get("watch_reason", ""),
        "watchset_retained": bool(item.get("watchset_retained")),
        "watchset_retention_cycles": int(item.get("watchset_retention_cycles") or 0),
        "watchset_retention_reason": item.get("watchset_retention_reason", ""),
        "price_location_status": _value(item.get("price_location_status") or item.get("price_location") or "UNKNOWN"),
        "price_location": _value(item.get("price_location_status") or item.get("price_location") or "UNKNOWN"),
        "price_location_score": float(item.get("price_location_score") or 0),
        "price_location_readiness": _value(item.get("price_location_readiness") or "UNKNOWN"),
        "price_location_readiness_reason_codes": list(item.get("price_location_readiness_reason_codes") or []),
        "price_location_provisional": bool(item.get("price_location_provisional")),
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
        "current_price": item.get("current_price"),
        "vwap": item.get("vwap"),
        "recent_support_price": item.get("recent_support_price"),
        "upper_limit_price": item.get("upper_limit_price"),
        "breakout_level": item.get("breakout_level"),
        "momentum_1m": momentum_1m,
        "momentum_3m": momentum_3m,
        "momentum_5m": momentum_5m,
        "momentum_1m_missing_reason": momentum_1m_missing_reason,
        "momentum_3m_missing_reason": momentum_3m_missing_reason,
        "upper_wick_risk": item.get("upper_wick_risk"),
        "failed_breakout": item.get("failed_breakout"),
        "recent_candles_1m": recent_candles_1m,
        "recent_candles_3m": recent_candles_3m,
        "completed_minute_bar_count": int(item.get("completed_minute_bar_count") or 0),
        "recent_3m_bar_count": int(item.get("recent_3m_bar_count") or 0),
        "minute_bar_present": bool(item.get("minute_bar_present")),
        "recent_support_source": item.get("recent_support_source", ""),
        "recent_support_candle_count": int(item.get("recent_support_candle_count") or 0),
        "prev_close_inferred_from_change_rate": bool(item.get("prev_close_inferred_from_change_rate")),
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
        "risk_off_entry": dict(item.get("risk_off_entry") or {}),
        "risk_off_entry_enabled": bool(item.get("risk_off_entry_enabled")),
        "risk_off_entry_observe_only": bool(item.get("risk_off_entry_observe_only")),
        "risk_off_entry_allowed": bool(item.get("risk_off_entry_allowed")),
        "risk_off_entry_rejected_reason": item.get("risk_off_entry_rejected_reason", ""),
        "risk_off_entry_failed_checks": list(item.get("risk_off_entry_failed_checks") or []),
        "risk_off_entry_passed_checks": list(item.get("risk_off_entry_passed_checks") or []),
        "risk_off_entry_blocking_data_flags": list(item.get("risk_off_entry_blocking_data_flags") or []),
        "risk_off_shadow_entry": dict(item.get("risk_off_shadow_entry") or {}),
        "risk_off_relative_strength_pct": item.get("risk_off_relative_strength_pct"),
        "risk_off_candidate_breadth_pct": item.get("risk_off_candidate_breadth_pct"),
        "risk_off_candidate_index_return_pct": item.get("risk_off_candidate_index_return_pct"),
        "risk_off_max_position_size_multiplier": item.get("risk_off_max_position_size_multiplier"),
        "risk_off_exit_hint": dict(item.get("risk_off_exit_hint") or {}),
        "live_order_enabled": bool(item.get("live_order_enabled")),
        "live_order_guard_passed": bool(item.get("live_order_guard_passed")),
        "position_size_multiplier": float(item.get("position_size_multiplier") or 1.0),
        "recheck_after_sec": int(item.get("recheck_after_sec") or 0),
        "summary_reason": _summary_message(summary_item, gate, display_status),
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
            "momentum_1m": momentum_1m,
            "momentum_3m": momentum_3m,
            "vi_active": item.get("vi_active"),
            "seconds_since_vi_release": item.get("seconds_since_vi_release"),
        },
    }
    next_recheck_after_sec = _recheck_seconds(row)
    row["operator_action"] = _operator_action(row)
    row["next_recheck_after_sec"] = next_recheck_after_sec if next_recheck_after_sec != 999999 else None
    row["decision_checklist"] = _decision_checklist(row)
    row["price_map"] = _price_map(row)
    return row


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
    watch_by_symbol = {str(item.get("symbol") or ""): item for item in watchset}

    def add(symbol: str, name: str, item_type: str, reason: str, priority: int, status: str = "NO_CANDLE_DATA") -> None:
        if not symbol or len(items) >= 50:
            return
        current = items.get(symbol)
        watch = watch_by_symbol.get(symbol, {})
        chart_context = _chart_context(watch)
        resolved_status = chart_context.pop("chart_data_status")
        if current is None or priority < current["priority"]:
            items[symbol] = {
                "symbol": symbol,
                "name": name or symbol,
                "type": item_type,
                "reason": reason,
                "priority": priority,
                "has_candle_data": resolved_status == "READY",
                "chart_data_status": resolved_status if resolved_status != "NO_DATA" else status,
                **chart_context,
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


def _chart_context(item: dict[str, Any]) -> dict[str, Any]:
    candles_1m = _as_list(item.get("recent_candles_1m"))
    candles_3m = _as_list(item.get("recent_candles_3m"))
    candles = candles_1m or candles_3m
    quote_values = {
        "current_price": _number_or_none(item.get("current_price")),
        "vwap": _number_or_none(item.get("vwap")),
        "recent_support_price": _number_or_none(item.get("recent_support_price")),
        "upper_limit_price": _number_or_none(item.get("upper_limit_price")),
        "breakout_level": _number_or_none(item.get("breakout_level")),
    }
    status = "READY" if candles else "QUOTE_ONLY" if any(value is not None for value in quote_values.values()) else "NO_CANDLE_DATA"
    last = candles[-1] if candles else {}
    return {
        "chart_data_status": status,
        "candles": candles,
        "recent_candles_1m": candles_1m,
        "recent_candles_3m": candles_3m,
        "last_candle_at": str(last.get("start_at") or ""),
        "completed_minute_bar_count": int(item.get("completed_minute_bar_count") or 0),
        "recent_3m_bar_count": int(item.get("recent_3m_bar_count") or 0),
        "minute_bar_present": bool(item.get("minute_bar_present") or candles),
        "recent_support_source": item.get("recent_support_source", ""),
        "recent_support_ready": bool(item.get("recent_support_ready")),
        "recent_support_candle_count": int(item.get("recent_support_candle_count") or 0),
        "prev_close_inferred_from_change_rate": bool(item.get("prev_close_inferred_from_change_rate")),
        **quote_values,
    }


def _index_chart_items() -> list[dict[str, Any]]:
    return [
        {"symbol": "KOSPI", "name": "KOSPI", "type": "index", "reason": "INDEX", "priority": 10, "has_candle_data": False, "chart_data_status": "NO_CANDLE_DATA", "last_candle_at": ""},
        {"symbol": "KOSDAQ", "name": "KOSDAQ", "type": "index", "reason": "INDEX", "priority": 11, "has_candle_data": False, "chart_data_status": "NO_CANDLE_DATA", "last_candle_at": ""},
    ]


def _select_chart(chart_universe: list[dict[str, Any]], watchset: list[dict[str, Any]]) -> dict[str, Any]:
    for status in ("READY", "READY_SMALL", "THEME_LEADER"):
        for item in chart_universe:
            if item.get("reason") == status and item.get("has_candle_data"):
                return item
    for item in chart_universe:
        if item.get("has_candle_data"):
            return item
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


def _operator_action(row: dict[str, Any]) -> str:
    gate = str(row.get("gate_status") or "")
    display = str(row.get("display_status") or gate)
    ready_like = gate in {"READY", "READY_SMALL"}
    if ready_like and row.get("submittable") and row.get("live_order_guard_passed"):
        return "BUY_READY"
    if ready_like and not row.get("live_order_guard_passed"):
        return "LIVE_GUARD_BLOCKED"
    if row.get("diagnostic_only") or display.startswith("WAIT_DATA"):
        return "DATA_WAIT"
    if _is_market_pending(row) or display.startswith("WAIT_CANDIDATE_MARKET") or str(row.get("market_confirmed_status") or "") == "RISK_OFF":
        return "MARKET_WAIT"
    if row.get("chase_risk") or display == "CHASE_RISK_BLOCKED":
        return "CHASE_BLOCKED"
    return "OBSERVE"


def _decision_checklist(row: dict[str, Any]) -> dict[str, str]:
    return {
        "market": _market_decision(row),
        "theme": _theme_decision(row),
        "role": _role_decision(row),
        "price_location": _price_location_decision(row),
        "data": _data_decision(row),
        "chase_risk": _chase_decision(row),
        "order_link": _order_decision(row),
    }


def _market_decision(row: dict[str, Any]) -> str:
    display = str(row.get("display_status") or "")
    status = str(row.get("market_confirmed_status") or row.get("candidate_market_status") or "")
    if status == "RISK_OFF" or "RISK_OFF" in display:
        return "BLOCK"
    if _is_market_pending(row) or status in {"WEAK", "CHOPPY"}:
        return "WAIT"
    return "PASS"


def _theme_decision(row: dict[str, Any]) -> str:
    status = str(row.get("theme_status") or "").upper()
    try:
        theme_score = float(row.get("theme_score") or 0)
    except (TypeError, ValueError):
        theme_score = 0.0
    if "WEAK" in status or theme_score < 40:
        return "WEAK"
    if "WATCH" in status or theme_score < 65:
        return "WATCH"
    return "PASS"


def _role_decision(row: dict[str, Any]) -> str:
    role = str(row.get("stock_role") or "WEAK_MEMBER")
    return role if role in {"LEADER", "CO_LEADER", "FOLLOWER", "LATE_LAGGARD", "WEAK_MEMBER"} else "WEAK_MEMBER"


def _price_location_decision(row: dict[str, Any]) -> str:
    display = str(row.get("display_status") or "")
    status = str(row.get("price_location_status") or "")
    if display.startswith("WAIT_DATA") or status == "UNKNOWN":
        return "DATA_WAIT"
    if status in {"FAILED_BREAKOUT", "DEEP_PULLBACK"}:
        return "WAIT"
    if row.get("chase_risk") or display == "CHASE_RISK_BLOCKED":
        return "BLOCK"
    return "PASS"


def _data_decision(row: dict[str, Any]) -> str:
    flags = set(row.get("data_quality_flags") or []) | set(row.get("price_location_data_quality_flags") or [])
    if _is_data_not_ready(row):
        return "DEGRADED"
    return "WARNING" if flags else "OK"


def _chase_decision(row: dict[str, Any]) -> str:
    display = str(row.get("display_status") or "")
    if row.get("chase_risk") or display == "CHASE_RISK_BLOCKED":
        return "BLOCK"
    if display == "LATE_CHASE_TEMP_WAIT" or row.get("late_chase_level"):
        return "WAIT"
    return "PASS"


def _order_decision(row: dict[str, Any]) -> str:
    if row.get("runtime_order_intent_created"):
        return "INTENT_CREATED"
    if str(row.get("gate_status") or "") in {"READY", "READY_SMALL"} and not row.get("live_order_guard_passed"):
        return "LIVE_BLOCKED"
    if row.get("submittable") and row.get("live_order_guard_passed"):
        return "READY"
    return "OBSERVE"


def _price_map(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_price": row.get("current_price"),
        "vwap": row.get("vwap"),
        "recent_support_price": row.get("recent_support_price"),
        "support_price": row.get("support_price"),
        "breakout_level": row.get("breakout_level"),
        "upper_limit_price": row.get("upper_limit_price"),
    }


PRICE_LOCATION_MISSING_LABELS = {
    "MISSING_CURRENT_PRICE": "현재가",
    "MISSING_SESSION_HIGH": "당일 고점",
    "MISSING_VWAP": "VWAP",
    "MISSING_BREAKOUT_LEVEL": "돌파 기준",
    "MISSING_RECENT_SUPPORT_PRICE": "최근 지지선",
    "MISSING_RETURN_PCT": "등락률",
    "MISSING_STOCK_ROLE": "종목 역할",
    "MISSING_THEME_STATUS": "테마 상태",
    "MISSING_MARKET_STATUS": "시장 상태",
    "INVALID_RECENT_CANDLE": "최근 캔들",
}


def _price_location_wait_summary(item: dict[str, Any]) -> str:
    reasons = [
        str(reason)
        for reason in list(item.get("price_location_reason_codes") or []) + list(item.get("price_location_readiness_reason_codes") or [])
        if reason
    ]
    flags = {
        str(flag)
        for flag in list(item.get("data_quality_flags") or []) + list(item.get("price_location_data_quality_flags") or [])
        if flag
    }
    missing = [label for code, label in PRICE_LOCATION_MISSING_LABELS.items() if code in flags]
    if "PRICE_LOCATION_DATA_MISSING" in reasons or missing:
        detail = ", ".join(missing[:4]) + " 부족" if missing else "핵심 가격 데이터 부족"
        return f"가격 위치 데이터 대기: {detail}{_wait_recheck_suffix(item)}"

    metrics = _price_location_metric_summary(item)
    if metrics:
        return f"가격 위치 미확정: 분류 기준 밖({metrics}){_wait_recheck_suffix(item)}"

    reason_text = ", ".join(reasons[:3]) if reasons else "세부 가격 지표 확인 필요"
    return f"가격 위치 미확정: {reason_text}{_wait_recheck_suffix(item)}"


def _deep_pullback_wait_summary(item: dict[str, Any]) -> str:
    parts = []
    pullback = _metric_number(item, "pullback_from_high_pct")
    vwap_gap = _metric_number(item, "vwap_gap_pct")
    support_gap = _metric_number(item, "support_gap_pct")
    if pullback is not None:
        parts.append(f"고점대비 {pullback:.2f}% 눌림")
    if vwap_gap is not None:
        parts.append(f"VWAP {vwap_gap:+.2f}%")
    else:
        parts.append("VWAP 미확인")
    parts.append(_momentum_summary_text(item, "momentum_3m", "3분"))
    if support_gap is not None:
        parts.append(f"지지선 {support_gap:+.2f}%")
    detail = ", ".join(parts) if parts else "고점대비 눌림 과다 또는 VWAP/모멘텀 회복 미확인"
    return f"과도한 눌림 대기: {detail}{_wait_recheck_suffix(item)}"


def _failed_breakout_wait_summary(item: dict[str, Any]) -> str:
    parts = []
    breakout_gap = _metric_number(item, "breakout_level_gap_pct")
    vwap_gap = _metric_number(item, "vwap_gap_pct")
    upper_wick = item.get("upper_wick_risk")
    if breakout_gap is not None:
        parts.append(f"돌파선 {breakout_gap:+.2f}% 이탈")
    else:
        parts.append("돌파선 이탈폭 미확인")
    if upper_wick is not None:
        parts.append(f"윗꼬리 리스크 {_risk_flag_label(upper_wick)}")
    else:
        parts.append("윗꼬리 리스크 미확인")
    parts.append(_momentum_summary_text(item, "momentum_1m", "1분"))
    if vwap_gap is not None:
        parts.append(f"VWAP {vwap_gap:+.2f}%")
    detail = ", ".join(parts)
    return f"돌파 실패 대기: {detail}{_wait_recheck_suffix(item)}"


def _price_location_metric_summary(item: dict[str, Any]) -> str:
    fields = [
        ("pullback_from_high_pct", "고점대비", False),
        ("vwap_gap_pct", "VWAP", True),
        ("breakout_level_gap_pct", "돌파선", True),
        ("support_gap_pct", "지지선", True),
    ]
    parts = []
    for key, label, signed in fields:
        value = _metric_number(item, key)
        if value is None:
            continue
        formatted = f"{value:+.2f}%" if signed else f"{value:.2f}%"
        parts.append(f"{label} {formatted}")
    return ", ".join(parts)


def _metric_number(item: dict[str, Any], key: str) -> float | None:
    value = item.get(key)
    if value is None and isinstance(item.get("metrics"), dict):
        value = item["metrics"].get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _momentum_summary_text(item: dict[str, Any], key: str, label: str) -> str:
    value = _metric_number(item, key)
    if value is not None:
        return f"{label} 모멘텀 {value:+.2f}%"
    reason = str(item.get(f"{key}_missing_reason") or "").strip()
    suffix = f"({reason})" if reason else ""
    return f"{label} 모멘텀 미확인{suffix}"


def _latest_candle_momentum(candles: list[dict[str, Any]]) -> float | None:
    if not candles:
        return None
    candle = candles[-1]
    open_price = _metric_number(candle, "open")
    close_price = _metric_number(candle, "close")
    if open_price is None or close_price is None or open_price <= 0:
        return None
    return round(((close_price - open_price) / open_price) * 100.0, 4)


def _momentum_missing_reason(candles: list[dict[str, Any]], label: str) -> str:
    if not candles:
        return f"완성 {label}봉 없음"
    candle = candles[-1]
    open_price = _metric_number(candle, "open")
    close_price = _metric_number(candle, "close")
    if open_price is None or close_price is None:
        return f"최근 {label}봉 open/close 누락"
    if open_price <= 0:
        return f"최근 {label}봉 시가 0"
    return "계산값 미저장"


def _risk_flag_label(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return "있음"
        if normalized in {"0", "false", "no", "n", "off"}:
            return "없음"
    return "있음" if bool(value) else "없음"


def _wait_recheck_suffix(item: dict[str, Any]) -> str:
    candidates = [
        _int_or_zero(item.get("recheck_after_sec")),
        _int_or_zero(item.get("late_chase_recheck_after_sec")),
        _int_or_zero(item.get("market_wait_recheck_after_sec")),
    ]
    positives = [value for value in candidates if value > 0]
    return f", {min(positives)}초 후 재확인" if positives else ""


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _summary_message(item: dict[str, Any], gate: str, display_status: str = "") -> str:
    role = _value(item.get("stock_role") or "UNKNOWN")
    location = _value(item.get("price_location_status") or "UNKNOWN")
    multiplier = float(item.get("position_size_multiplier") or 1.0)
    reasons = (
        list(item.get("risk_reason_codes") or [])
        + list(item.get("price_location_reason_codes") or [])
        + list(item.get("price_location_readiness_reason_codes") or [])
    )
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
    if display_status == "WAIT_FAILED_BREAKOUT":
        return _failed_breakout_wait_summary(item)
    if display_status == "WAIT_DEEP_PULLBACK":
        return _deep_pullback_wait_summary(item)
    if display_status in {
        "WAIT_PRICE_LOCATION_UNKNOWN",
        "WAIT_PRICE_LOCATION_DATA",
        "WAIT_PRICE_LOCATION_WARMUP",
        "WAIT_PRICE_LOCATION_PROVISIONAL",
    }:
        return _price_location_wait_summary(item)
    if gate == "READY":
        live_note = "" if item.get("live_order_guard_passed") else " / LIVE Guard 미통과"
        return f"{role} / {location} 조건으로 진입 가능, {multiplier:.2g}배 비중{live_note}"
    if gate == "READY_SMALL":
        live_note = "" if item.get("live_order_guard_passed") else " / LIVE Guard 미통과"
        return f"{role} 흐름은 유효하지만 {location} 기준으로 소액 관찰 진입, {multiplier:.2g}배 비중{live_note}"
    if gate == "WAIT":
        if location == "FAILED_BREAKOUT" or "FAILED_BREAKOUT" in reasons:
            return _failed_breakout_wait_summary(item)
        if location == "DEEP_PULLBACK" or "DEEP_PULLBACK" in reasons:
            return _deep_pullback_wait_summary(item)
        if (
            location == "UNKNOWN"
            or "PRICE_LOCATION_UNKNOWN" in reasons
            or "PRICE_LOCATION_DATA_MISSING" in reasons
            or "PRICE_LOCATION_CORE_DATA_MISSING" in reasons
            or "PRICE_LOCATION_WARMUP" in reasons
            or "PRICE_LOCATION_PROVISIONAL" in reasons
        ):
            return _price_location_wait_summary(item)
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


def _data_quality_message(status: str, reasons: list[str]) -> str:
    if status == "DEGRADED":
        return ", ".join(reasons[:2]) if reasons else "핵심 실시간 데이터 부족"
    if status == "WARNING":
        return ", ".join(reasons[:2]) if reasons else "일부 보조 데이터가 누락되어 보수적으로 표시합니다."
    return "WatchSet 분봉/VWAP OK"


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")
