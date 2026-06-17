from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from storage.db import TradingDatabase
from trading.broker.command_queue import ORDER_COMMAND_TYPES
from trading.broker.gateway_state import GatewayStateStore
from trading.broker.models import new_message_id, utc_timestamp
from trading.strategy.runtime_settings import StrategyRuntimeSettingsRepository
from trading.theme_engine.backfill import is_theme_backfill_record
from trading_app.dependencies import CoreSettings
from trading_app.dry_run_performance import DryRunPerformanceAnalyzer, config_from_settings


PREFLIGHT_STATUS_GO = "GO"
PREFLIGHT_STATUS_GO_WITH_WARNINGS = "GO_WITH_WARNINGS"
PREFLIGHT_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
PREFLIGHT_STATUS_NO_GO = "NO_GO"
PREFLIGHT_STATUS_FAIL_CLOSED = "FAIL_CLOSED"

_SIMULATION_ALIASES = {"1", "SIM", "SIMULATION", "MOCK", "PAPER", "PAPER_TRADING", "LIVE_SIM", "DEMO", "TEST", "모의", "모의투자"}
_REAL_ALIASES = {"0", "REAL", "LIVE", "PROD", "PRODUCTION", "LIVE_REAL", "실전", "실계좌"}
_RECENT_TR_FAILURE_ERRORS = {"RATE_LIMITED", "TR_TIMEOUT", "TR_REQUEST_FAILED"}


@dataclass(frozen=True)
class LiveSimPreflightConfig:
    gateway_queue_warn_threshold: int = 20
    gateway_queue_block_threshold: int = 100
    reconnect_warn_threshold: int = 3
    recent_error_warn_threshold: int = 1
    rate_limit_recent_window_sec: float = 60.0
    parser_miss_warn_ratio: float = 0.20
    parser_miss_block_ratio: float = 0.50


class LiveSimPreflightService:
    def __init__(
        self,
        db: TradingDatabase,
        gateway_state: GatewayStateStore,
        *,
        settings: CoreSettings,
        config: LiveSimPreflightConfig | None = None,
    ) -> None:
        self.db = db
        self.gateway_state = gateway_state
        self.settings = settings
        self.config = config or LiveSimPreflightConfig()

    def build_snapshot(
        self,
        *,
        runtime_status: dict[str, Any] | None = None,
        performance_report: dict[str, Any] | None = None,
        transport_status: dict[str, Any] | None = None,
        theme_lab_snapshot: dict[str, Any] | None = None,
        persist: bool = False,
        include_details: bool = True,
    ) -> dict[str, Any]:
        checked_at = utc_timestamp()
        runtime_settings = StrategyRuntimeSettingsRepository(self.db).load()
        execution = dict(runtime_settings.value("order_execution", {}) or {})
        exit_guard = dict(runtime_settings.value("live_sim_exit_guard", {}) or {})
        lifecycle = dict(runtime_settings.value("live_sim_order_lifecycle", {}) or {})
        reconcile = dict(runtime_settings.value("live_sim_reconcile", {}) or {})
        gateway_snapshot = self.gateway_state.snapshot().to_dict()
        command_summary = self.gateway_state.command_snapshot()
        transport_status = transport_status or {}
        theme_lab_snapshot = theme_lab_snapshot or {}
        performance_report = performance_report or self._latest_performance_report()

        blocking: list[str] = []
        warnings: list[str] = []
        fail_closed: list[str] = []
        insufficient: list[str] = []

        account_summary = _account_mode_summary(gateway_snapshot, execution)
        if account_summary["real_detected"]:
            fail_closed.append("BROKER_ACCOUNT_REAL_DETECTED")
        if account_summary["unknown_detected"] and bool(execution.get("fail_closed_on_account_unknown", True)):
            fail_closed.append("BROKER_ACCOUNT_UNKNOWN_FAIL_CLOSED")
        if not account_summary["simulation_confirmed"]:
            blocking.append("SIMULATION_ACCOUNT_NOT_CONFIRMED")
        if bool(account_summary.get("allowed_account_mismatch")):
            fail_closed.append("ALLOWED_ACCOUNT_MISMATCH")

        if not bool(gateway_snapshot.get("kiwoom_logged_in")):
            blocking.append("KIWOOM_LOGIN_FALSE")
        if not bool(gateway_snapshot.get("orderable")):
            blocking.append("KIWOOM_ORDERABLE_FALSE")
        if not bool(gateway_snapshot.get("heartbeat_ok")):
            blocking.append("GATEWAY_HEARTBEAT_NOT_FRESH")

        if _is_live_real_execution(execution):
            fail_closed.append("LIVE_REAL_EXECUTION_CONFIG_DETECTED")
        if self.settings.allow_live or self.settings.live_order_enabled or self.settings.runtime_allow_live_orders:
            fail_closed.append("CORE_LIVE_ORDER_FLAG_DETECTED")

        if str(execution.get("mode") or "").upper() != "LIVE_SIM":
            blocking.append("ORDER_EXECUTION_MODE_NOT_LIVE_SIM")
        if not bool(execution.get("live_sim_enabled")):
            blocking.append("LIVE_SIM_DISABLED")
        if bool(execution.get("live_real_enabled")):
            fail_closed.append("LIVE_REAL_ENABLED_TRUE")
        if bool(execution.get("kill_switch_active")):
            blocking.append("KILL_SWITCH_ACTIVE")
        for key in ("max_orders_per_day", "max_new_positions_per_day", "max_order_amount_krw", "max_total_exposure_krw"):
            if _positive_number(execution.get(key)) is None:
                blocking.append(f"RISK_LIMIT_MISSING:{key}")
        if not bool(exit_guard.get("enabled")):
            blocking.append("EXIT_GUARD_DISABLED")
        if not bool(lifecycle.get("enabled")):
            blocking.append("LIVE_SIM_ORDER_LIFECYCLE_DISABLED")
        if not bool(reconcile.get("enabled")):
            blocking.append("LIVE_SIM_RECONCILE_DISABLED")

        performance_summary, performance_status, performance_reasons = _performance_summary(performance_report)
        if performance_status == PREFLIGHT_STATUS_INSUFFICIENT_DATA:
            insufficient.extend(performance_reasons)
        elif performance_status == PREFLIGHT_STATUS_NO_GO:
            blocking.extend(performance_reasons)

        gateway_load_summary = self._gateway_load_summary(
            gateway_snapshot,
            command_summary,
            transport_status=transport_status,
        )
        if int(gateway_load_summary.get("gateway_queue_depth") or 0) >= self.config.gateway_queue_block_threshold:
            blocking.append("GATEWAY_COMMAND_QUEUE_DEPTH_BLOCK")
        elif int(gateway_load_summary.get("gateway_queue_depth") or 0) >= self.config.gateway_queue_warn_threshold:
            warnings.append("GATEWAY_COMMAND_QUEUE_DEPTH_HIGH")
        if int(gateway_load_summary.get("reconnect_count") or 0) >= self.config.reconnect_warn_threshold:
            warnings.append("GATEWAY_RECONNECT_COUNT_HIGH")
        if int(gateway_load_summary.get("recent_rate_limit_count") or 0) >= self.config.recent_error_warn_threshold:
            warnings.append("GATEWAY_RATE_LIMIT_RECENT")
        if int(gateway_load_summary.get("recent_tr_failure_count") or 0) >= self.config.recent_error_warn_threshold:
            warnings.append("GATEWAY_TR_FAILURE_RECENT")
        if _positive_number(gateway_load_summary.get("command_latency_p95_ms")) is not None:
            p95 = float(gateway_load_summary.get("command_latency_p95_ms") or 0.0)
            if p95 > float(getattr(self.settings, "transport_command_p95_warn_ms", 1000) or 1000):
                warnings.append("GATEWAY_COMMAND_LATENCY_P95_HIGH")

        backfill_summary = _backfill_summary(theme_lab_snapshot)
        parser_miss_ratio = _positive_number(backfill_summary.get("parser_miss_ratio"))
        if parser_miss_ratio is not None:
            if parser_miss_ratio >= self.config.parser_miss_block_ratio:
                blocking.append("THEME_BACKFILL_PARSER_MISS_RATIO_BLOCK")
            elif parser_miss_ratio >= self.config.parser_miss_warn_ratio:
                warnings.append("THEME_BACKFILL_PARSER_MISS_RATIO_HIGH")
        if int(backfill_summary.get("tr_backfill_caused_ready_count") or 0) > 0:
            fail_closed.append("THEME_BACKFILL_CAUSED_READY_DETECTED")

        blocking = _dedupe(blocking)
        warnings = _dedupe(warnings)
        fail_closed = _dedupe(fail_closed)
        insufficient = _dedupe(insufficient)
        status = _final_status(fail_closed=fail_closed, blocking=blocking, insufficient=insufficient, warnings=warnings)
        all_blocking = _dedupe(fail_closed + blocking + insufficient)
        snapshot = {
            "snapshot_id": new_message_id("live_sim_preflight"),
            "status": status,
            "blocking_reasons": all_blocking,
            "warning_reasons": warnings,
            "operator_message_ko": _operator_message(status, all_blocking, warnings),
            "recommended_action_ko": _recommended_action(status),
            "checked_at": checked_at,
            "source_metrics": {
                "gateway": _redact_gateway_snapshot(gateway_snapshot),
                "commands": command_summary,
                "runtime_status": runtime_status or {},
                "runtime_order_execution": _redact_account_config(execution),
                "performance_report_id": performance_report.get("report_id", ""),
                "performance_status": performance_report.get("status", ""),
            },
            "account_mode_summary": account_summary,
            "performance_summary": performance_summary,
            "gateway_load_summary": gateway_load_summary,
            "backfill_summary": backfill_summary,
            "safety_summary": {
                "core_mode": self.settings.mode,
                "runtime_mode": self.settings.runtime_mode,
                "runtime_allow_live_orders": bool(self.settings.runtime_allow_live_orders),
                "core_allow_live": bool(self.settings.allow_live),
                "live_real_enabled": bool(execution.get("live_real_enabled")),
                "live_sim_enabled": bool(execution.get("live_sim_enabled")),
                "order_execution_mode": str(execution.get("mode") or ""),
                "kill_switch_active": bool(execution.get("kill_switch_active")),
                "exit_guard_enabled": bool(exit_guard.get("enabled")),
                "live_sim_order_lifecycle_enabled": bool(lifecycle.get("enabled")),
                "live_sim_reconcile_enabled": bool(reconcile.get("enabled")),
                "risk_limits": {
                    key: execution.get(key)
                    for key in ("max_orders_per_day", "max_new_positions_per_day", "max_order_amount_krw", "max_total_exposure_krw")
                },
            },
        }
        if persist:
            self.db.save_live_sim_preflight_snapshot(snapshot)
        return snapshot if include_details else compact_preflight_snapshot(snapshot)

    def _latest_performance_report(self) -> dict[str, Any]:
        reports = self.db.list_dry_run_performance_reports(limit=1) if hasattr(self.db, "list_dry_run_performance_reports") else []
        if reports:
            report = dict(reports[0] or {})
            report["source"] = "persisted_latest"
            return report
        try:
            report = DryRunPerformanceAnalyzer(self.db, config=config_from_settings(self.settings)).build_report(limit=10000)
            report["source"] = "computed_current"
            return report
        except Exception as exc:
            return {"status": "ERROR", "source": "error", "error": str(exc), "summary": {}}

    def _gateway_load_summary(
        self,
        gateway_snapshot: dict[str, Any],
        command_summary: dict[str, Any],
        *,
        transport_status: dict[str, Any],
    ) -> dict[str, Any]:
        records = self.gateway_state.list_commands(limit=1000, include_finished=True)
        active_records = [record for record in records if str(record.get("status") or "") in {"QUEUED", "DISPATCHED"}]
        active_order_records = [record for record in active_records if str(record.get("command_type") or "") in ORDER_COMMAND_TYPES]
        active_backfill_records = [record for record in active_records if is_theme_backfill_record(record)]
        tr_failures = _recent_tr_failure_count(records)
        summary = dict((transport_status or {}).get("latest_summary") or {})
        return {
            "gateway_queue_depth": int(command_summary.get("queued_count") or 0) + int(command_summary.get("dispatched_count") or 0),
            "order_command_pending_count": len(active_order_records),
            "backfill_pending_count": len(active_backfill_records),
            "queued_count": int(command_summary.get("queued_count") or 0),
            "dispatched_count": int(command_summary.get("dispatched_count") or 0),
            "heartbeat_age_sec": gateway_snapshot.get("heartbeat_age_sec"),
            "heartbeat_ok": bool(gateway_snapshot.get("heartbeat_ok")),
            "reconnect_count": int(gateway_snapshot.get("reconnect_count") or 0),
            "recent_gateway_errors": _recent_gateway_errors(self.gateway_state),
            "recent_rate_limit_count": _recent_rate_limit_count(self.gateway_state, self.config.rate_limit_recent_window_sec),
            "total_rate_limited_count": int(command_summary.get("rate_limited_count") or 0),
            "recent_tr_failure_count": tr_failures,
            "command_latency_p95_ms": summary.get("command_latency_p95_ms"),
            "command_latency_p99_ms": summary.get("command_latency_p99_ms"),
            "latest_command_latency_ms": summary.get("latest_command_latency_ms"),
        }


def compact_preflight_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: snapshot.get(key)
        for key in (
            "snapshot_id",
            "status",
            "blocking_reasons",
            "warning_reasons",
            "operator_message_ko",
            "recommended_action_ko",
            "checked_at",
            "account_mode_summary",
            "performance_summary",
            "gateway_load_summary",
            "backfill_summary",
            "safety_summary",
        )
    }


def _account_mode_summary(gateway_snapshot: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    heartbeat = dict(gateway_snapshot.get("last_heartbeat_payload") or {})
    account = str(
        heartbeat.get("account")
        or heartbeat.get("account_no")
        or heartbeat.get("account_number")
        or gateway_snapshot.get("account")
        or ""
    )
    raw_modes = {
        "broker_env": _first_text(heartbeat.get("broker_env"), gateway_snapshot.get("mode")),
        "server_mode": _first_text(heartbeat.get("server_mode"), heartbeat.get("server_gubun")),
        "account_mode": _first_text(heartbeat.get("account_mode"), heartbeat.get("account_type")),
        "server_gubun": _first_text(heartbeat.get("server_gubun")),
    }
    normalized = {key: _normalize_broker_mode(value) for key, value in raw_modes.items()}
    modes = [normalized["broker_env"], normalized["server_mode"], normalized["account_mode"]]
    real_detected = "REAL" in modes or normalized["server_gubun"] == "REAL"
    simulation_confirmed = "SIMULATION" in modes or normalized["server_gubun"] == "SIMULATION"
    unknown_detected = not real_detected and not simulation_confirmed
    allowed = [str(item) for item in execution.get("allowed_account_numbers") or [] if str(item or "").strip()]
    mismatch = bool(allowed and account and account not in allowed)
    if allowed and not account:
        mismatch = True
    return {
        "account_masked": _mask_account(account),
        "account_present": bool(account),
        "allowed_account_numbers_configured": bool(allowed),
        "allowed_account_numbers_masked": [_mask_account(item) for item in allowed],
        "allowed_account_mismatch": mismatch,
        "raw_modes": raw_modes,
        "normalized_modes": normalized,
        "simulation_confirmed": simulation_confirmed,
        "real_detected": real_detected,
        "unknown_detected": unknown_detected,
    }


def _performance_summary(report: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    summary = dict(report.get("summary") or {})
    go_no_go = dict(summary.get("go_no_go") or {})
    total = int(summary.get("total_lifecycle_count") or report.get("total_items") or len(report.get("items") or []) or 0)
    source = str(report.get("source") or "")
    performance = {
        "available": bool(summary),
        "source": source,
        "report_id": report.get("report_id", ""),
        "status": report.get("status", ""),
        "generated_at": report.get("generated_at", ""),
        "trade_date": report.get("trade_date", ""),
        "total_lifecycle_count": total,
        "trade_day_count": summary.get("trade_day_count", 0),
        "accepted_completed_lifecycle_count": summary.get("accepted_completed_lifecycle_count", 0),
        "dry_run_accepted_count": summary.get("dry_run_accepted_count", 0),
        "net_expectancy": summary.get("net_expectancy"),
        "bad_ready_rate": summary.get("cost_adjusted_bad_ready_rate"),
        "opportunity_loss_rate": summary.get("cost_adjusted_opportunity_loss_rate"),
        "stale_tick_rate": dict(summary.get("execution_realism") or {}).get("stale_tick_rate"),
        "latency_distortion_rate": dict(summary.get("execution_realism") or {}).get("gateway_latency_high_rate"),
        "go_no_go": go_no_go,
    }
    if not summary or total <= 0:
        return performance, PREFLIGHT_STATUS_INSUFFICIENT_DATA, ["PERFORMANCE_REPORT_MISSING_OR_EMPTY"]
    readiness = str(go_no_go.get("readiness") or "").upper()
    blocked = [str(item) for item in go_no_go.get("blocked_by") or [] if str(item)]
    if readiness == "INSUFFICIENT_DATA":
        return performance, PREFLIGHT_STATUS_INSUFFICIENT_DATA, blocked or ["PERFORMANCE_INSUFFICIENT_DATA"]
    decision = str(go_no_go.get("decision") or "").upper()
    if decision != PREFLIGHT_STATUS_GO:
        return performance, PREFLIGHT_STATUS_NO_GO, blocked or ["PERFORMANCE_GO_NO_GO_NOT_GO"]
    return performance, PREFLIGHT_STATUS_GO, []


def _backfill_summary(theme_lab_snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = dict(theme_lab_snapshot.get("theme_backfill_runtime") or theme_lab_snapshot.get("backfill_summary") or {})
    return {
        "enabled": bool(raw.get("enabled")),
        "observe_only": bool(raw.get("observe_only")),
        "paused_reason": str(raw.get("paused_reason") or ""),
        "queued_count": int(raw.get("queued_count") or 0),
        "dispatched_count": int(raw.get("dispatched_count") or 0),
        "backfill_pending_count": int(raw.get("queued_count") or 0) + int(raw.get("dispatched_count") or 0),
        "parser_miss_ratio": raw.get("parser_miss_ratio"),
        "tr_backfill_caused_ready_count": int(raw.get("tr_backfill_caused_ready_count") or 0),
        "load_guard": dict(raw.get("load_guard") or {}),
    }


def _recent_gateway_errors(gateway_state: GatewayStateStore) -> list[str]:
    errors: list[str] = []
    for event in gateway_state.recent_events(limit=50):
        if event.type in {"gateway_error", "error", "rate_limited"}:
            payload = dict(event.payload or {})
            errors.append(str(payload.get("message") or payload.get("error") or event.type))
    return errors[-10:]


def _recent_rate_limit_count(gateway_state: GatewayStateStore, window_sec: float) -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(0.0, float(window_sec or 0.0)))
    count = 0
    for event in gateway_state.recent_events(limit=200):
        if event.type != "rate_limited":
            continue
        event_time = _parse_event_time(event.timestamp)
        if event_time is None or event_time >= cutoff:
            count += 1
    return count


def _recent_tr_failure_count(records: list[dict[str, Any]]) -> int:
    count = 0
    for record in records[:200]:
        if str(record.get("command_type") or "") != "tr_request":
            continue
        text = " ".join(
            [
                str(record.get("status") or ""),
                str(record.get("last_error") or ""),
                str((record.get("result_payload") or {}).get("error") if isinstance(record.get("result_payload"), dict) else ""),
                str((record.get("result_payload") or {}).get("reason") if isinstance(record.get("result_payload"), dict) else ""),
            ]
        ).upper()
        if any(token in text for token in _RECENT_TR_FAILURE_ERRORS) or str(record.get("status") or "") in {"FAILED", "REJECTED"}:
            count += 1
    return count


def _parse_event_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_broker_mode(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "UNKNOWN"
    if text in _SIMULATION_ALIASES or "SIM" in text or "모의" in text:
        return "SIMULATION"
    if text in _REAL_ALIASES or "REAL" in text or "실전" in text or "실계좌" in text:
        return "REAL"
    return "UNKNOWN"


def _final_status(*, fail_closed: list[str], blocking: list[str], insufficient: list[str], warnings: list[str]) -> str:
    if fail_closed:
        return PREFLIGHT_STATUS_FAIL_CLOSED
    if blocking:
        return PREFLIGHT_STATUS_NO_GO
    if insufficient:
        return PREFLIGHT_STATUS_INSUFFICIENT_DATA
    if warnings:
        return PREFLIGHT_STATUS_GO_WITH_WARNINGS
    return PREFLIGHT_STATUS_GO


def _operator_message(status: str, blocking: list[str], warnings: list[str]) -> str:
    if status == PREFLIGHT_STATUS_GO:
        return "LIVE_SIM 자동주문 전 점검을 통과했습니다. 모의투자 계좌와 주문 안전 조건이 확인되었습니다."
    if status == PREFLIGHT_STATUS_GO_WITH_WARNINGS:
        return "LIVE_SIM 필수 조건은 통과했지만 운영자가 확인해야 할 경고가 있습니다."
    if status == PREFLIGHT_STATUS_INSUFFICIENT_DATA:
        return "성과 검증 데이터가 부족해 오늘 LIVE_SIM 자동주문은 기본 차단됩니다."
    if status == PREFLIGHT_STATUS_FAIL_CLOSED:
        return "실계좌 또는 LIVE_REAL 위험이 감지되어 fail-closed로 차단했습니다."
    first = blocking[0] if blocking else warnings[0] if warnings else "NO_GO"
    return f"LIVE_SIM 자동주문 시작 조건을 통과하지 못했습니다. 주요 사유: {first}"


def _recommended_action(status: str) -> str:
    if status == PREFLIGHT_STATUS_GO:
        return "운영자가 계좌와 대시보드 상태를 최종 확인한 뒤 기존 절차대로 Runtime을 시작하세요."
    if status == PREFLIGHT_STATUS_GO_WITH_WARNINGS:
        return "경고 사유를 확인하고 명시적 승인 플래그가 있는 경우에만 진행하세요."
    if status == PREFLIGHT_STATUS_INSUFFICIENT_DATA:
        return "DRY_RUN 성과 리포트를 재생성하고 최소 거래일/라이프사이클 표본을 채운 뒤 다시 점검하세요."
    if status == PREFLIGHT_STATUS_FAIL_CLOSED:
        return "즉시 중단하고 Kiwoom 계좌 모드, LIVE_REAL 설정, 허용 계좌 목록을 점검하세요. 강제 플래그로 우회하지 마세요."
    return "차단 사유를 해결한 뒤 preflight rebuild를 다시 실행하세요."


def _is_live_real_execution(execution: dict[str, Any]) -> bool:
    return str(execution.get("mode") or "").upper() in {"LIVE", "LIVE_REAL"} or bool(execution.get("live_real_enabled"))


def _positive_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _redact_account_config(execution: dict[str, Any]) -> dict[str, Any]:
    payload = dict(execution)
    payload["allowed_account_numbers"] = [_mask_account(item) for item in payload.get("allowed_account_numbers") or []]
    return payload


def _redact_gateway_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = dict(snapshot or {})
    for key in ("account", "account_no", "account_number"):
        if key in payload:
            payload[key] = _mask_account(payload.get(key))
    heartbeat = dict(payload.get("last_heartbeat_payload") or {})
    for key in ("account", "account_no", "account_number"):
        if key in heartbeat:
            heartbeat[key] = _mask_account(heartbeat.get(key))
    if heartbeat:
        payload["last_heartbeat_payload"] = heartbeat
    return payload


def _mask_account(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "*" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result


def preflight_trade_date(snapshot: dict[str, Any]) -> str:
    checked_at = str(snapshot.get("checked_at") or "")
    if len(checked_at) >= 10:
        return checked_at[:10]
    return datetime.now(timezone.utc).date().isoformat()
