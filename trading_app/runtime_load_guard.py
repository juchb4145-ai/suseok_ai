from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from trading.broker.command_queue import ORDER_COMMAND_TYPES
from trading.broker.gateway_state import GatewayStateStore
from trading.theme_engine.backfill import is_theme_backfill_record


LOAD_GUARD_OK = "OK"
LOAD_GUARD_DEGRADED = "DEGRADED"
LOAD_GUARD_PAUSED = "PAUSED"
LOAD_GUARD_FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True)
class RuntimeLoadGuardConfig:
    heartbeat_stale_sec: float = 15.0
    command_latency_pause_ms: float = 1500.0
    rate_limit_pause_window_sec: float = 60.0
    parser_miss_degrade_ratio: float = 0.20
    parser_miss_pause_ratio: float = 0.50


def build_runtime_load_guard_snapshot(
    gateway_state: GatewayStateStore,
    *,
    raw_theme_lab: dict[str, Any] | None = None,
    theme_lab_result: Any | None = None,
    transport_status: dict[str, Any] | None = None,
    backfill_summary: dict[str, Any] | None = None,
    config: RuntimeLoadGuardConfig | None = None,
) -> dict[str, Any]:
    cfg = config or RuntimeLoadGuardConfig()
    gateway = gateway_state.snapshot().to_dict()
    command_summary = gateway_state.command_snapshot()
    records = gateway_state.list_commands(limit=1000, include_finished=True)
    active_records = [record for record in records if str(record.get("status") or "") in {"QUEUED", "DISPATCHED"}]
    order_pending = [record for record in active_records if str(record.get("command_type") or "") in ORDER_COMMAND_TYPES]
    backfill_pending = [record for record in active_records if is_theme_backfill_record(record)]
    raw_theme_lab = raw_theme_lab or {}
    backfill_summary = backfill_summary or dict(raw_theme_lab.get("theme_backfill_runtime") or {})
    pause_codes: list[str] = []
    degrade_codes: list[str] = []
    fail_codes: list[str] = []

    if _theme_lab_has_ready_like(raw_theme_lab) or _result_has_ready_like(theme_lab_result):
        degrade_codes.append("READY_OR_READY_SMALL_PRESENT")
    if order_pending:
        pause_codes.append("ORDER_COMMAND_PENDING")
    heartbeat_age = _float_or_none(gateway.get("heartbeat_age_sec"))
    heartbeat_limit = _float_or_none(gateway.get("heartbeat_timeout_sec")) or cfg.heartbeat_stale_sec
    if not bool(gateway.get("heartbeat_ok")) or (heartbeat_age is not None and heartbeat_age > max(heartbeat_limit, cfg.heartbeat_stale_sec)):
        pause_codes.append("GATEWAY_HEARTBEAT_STALE")
    command_p95 = _command_latency_p95(transport_status or {})
    if command_p95 is not None and command_p95 > cfg.command_latency_pause_ms:
        pause_codes.append("COMMAND_LATENCY_HIGH")
    total_rate_limited_count = int(command_summary.get("rate_limited_count") or 0)
    recent_rate_limit_count = _recent_rate_limit_count(gateway_state, cfg.rate_limit_pause_window_sec)
    recent_tr_failure_count = _recent_tr_failure_count(records)
    if recent_rate_limit_count > 0:
        pause_codes.append("RATE_LIMITED_RECENT")
    if recent_tr_failure_count > 0:
        pause_codes.append("TR_FAILURE_RECENT")
    tr_backfill_caused_ready_count = int(backfill_summary.get("tr_backfill_caused_ready_count") or 0)
    if tr_backfill_caused_ready_count > 0:
        fail_codes.append("TR_BACKFILL_CAUSED_READY")
    parser_miss_ratio = _float_or_none(backfill_summary.get("parser_miss_ratio"))
    if parser_miss_ratio is not None:
        if parser_miss_ratio >= cfg.parser_miss_pause_ratio:
            pause_codes.append("PARSER_MISS_RATIO_HIGH")
        elif parser_miss_ratio >= cfg.parser_miss_degrade_ratio:
            degrade_codes.append("PARSER_MISS_RATIO_DEGRADED")

    if fail_codes:
        status = LOAD_GUARD_FAIL_CLOSED
    elif pause_codes:
        status = LOAD_GUARD_PAUSED
    elif degrade_codes:
        status = LOAD_GUARD_DEGRADED
    else:
        status = LOAD_GUARD_OK

    reason_codes = _dedupe(fail_codes + pause_codes + degrade_codes)
    return {
        "load_guard_status": status,
        "paused_backfill": status in {LOAD_GUARD_PAUSED, LOAD_GUARD_FAIL_CLOSED},
        "pause_reason_codes": reason_codes,
        "operator_message_ko": _operator_message(status, reason_codes),
        "last_changed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "affected_services": _affected_services(status, reason_codes),
        "gateway_queue_depth": int(command_summary.get("queued_count") or 0) + int(command_summary.get("dispatched_count") or 0),
        "order_command_pending_count": len(order_pending),
        "backfill_pending_count": len(backfill_pending),
        "recent_rate_limit_count": recent_rate_limit_count,
        "total_rate_limited_count": total_rate_limited_count,
        "recent_tr_failure_count": recent_tr_failure_count,
        "heartbeat_age_sec": heartbeat_age,
        "command_latency_p95_ms": command_p95,
        "parser_miss_ratio": parser_miss_ratio,
        "tr_backfill_caused_ready_count": tr_backfill_caused_ready_count,
    }


def runtime_load_guard_from_theme_result(
    gateway_state: GatewayStateStore,
    result: Any,
    *,
    backfill_summary: dict[str, Any] | None = None,
    config: RuntimeLoadGuardConfig | None = None,
) -> dict[str, Any]:
    return build_runtime_load_guard_snapshot(
        gateway_state,
        theme_lab_result=result,
        backfill_summary=backfill_summary,
        config=config,
    )


def _theme_lab_has_ready_like(raw: dict[str, Any]) -> bool:
    for item in list(raw.get("watchset_snapshots") or []):
        status = str(item.get("final_gate_status") or item.get("gate_status") or "")
        if status in {"READY", "READY_SMALL"}:
            return True
    return False


def _result_has_ready_like(result: Any | None) -> bool:
    if result is None:
        return False
    for watch in getattr(result, "watchset", ()) or ():
        status = str(getattr(watch, "final_gate_status", "") or getattr(watch, "gate_status", "") or "")
        if status in {"READY", "READY_SMALL"}:
            return True
    return False


def _command_latency_p95(transport_status: dict[str, Any]) -> float | None:
    summary = dict(transport_status.get("latest_summary") or transport_status.get("summary") or {})
    return _float_or_none(summary.get("command_latency_p95_ms") or summary.get("rest_command_p95_ms"))


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
        if any(token in text for token in ("RATE_LIMITED", "TR_TIMEOUT", "TR_REQUEST_FAILED")) or str(record.get("status") or "") in {"FAILED", "REJECTED"}:
            count += 1
    return count


def _recent_rate_limit_count(gateway_state: GatewayStateStore, window_sec: float) -> int:
    try:
        events = gateway_state.recent_events(limit=200)
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(0.0, float(window_sec or 0.0)))
    count = 0
    for event in events:
        if str(getattr(event, "type", "") or "") != "rate_limited":
            continue
        event_time = _parse_event_time(getattr(event, "timestamp", ""))
        if event_time is None or event_time >= cutoff:
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


def _affected_services(status: str, reason_codes: list[str]) -> list[str]:
    if status == LOAD_GUARD_OK:
        return []
    services = ["theme_backfill", "non_order_tr"]
    if "TR_BACKFILL_CAUSED_READY" in reason_codes:
        services.append("theme_backfill_dispatch")
    return services


def _operator_message(status: str, reason_codes: list[str]) -> str:
    if status == LOAD_GUARD_OK:
        return "Gateway/TR 부하가 안정적입니다."
    if status == LOAD_GUARD_DEGRADED:
        return "보조 TR 품질이 낮아져 backfill을 보수적으로 낮춥니다."
    if status == LOAD_GUARD_FAIL_CLOSED:
        return "TR backfill이 READY 근거에 섞인 정황이 있어 backfill을 fail-closed로 중단합니다."
    reason = reason_codes[0] if reason_codes else "LOAD_GUARD"
    return f"주문 안정성을 위해 backfill/비주문 TR을 일시 중지합니다. 사유: {reason}"


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "")
        if text and text not in result:
            result.append(text)
    return result
