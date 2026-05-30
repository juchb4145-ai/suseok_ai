from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading.broker.models import utc_timestamp


SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1, "ok": 0}


@dataclass(frozen=True)
class OpsAlert:
    id: str
    severity: str
    area: str
    title: str
    message: str
    action: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "area": self.area,
            "title": self.title,
            "message": self.message,
            "action": self.action,
            "details": dict(self.details or {}),
        }


def build_ops_alerts(
    *,
    core: dict[str, Any] | None = None,
    gateway: dict[str, Any] | None = None,
    commands: dict[str, Any] | None = None,
    transport: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    dry_run_performance: dict[str, Any] | None = None,
    logs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    core = dict(core or {})
    gateway = dict(gateway or {})
    commands = dict(commands or {})
    transport = dict(transport or {})
    runtime = dict(runtime or {})
    dry_run_performance = dict(dry_run_performance or {})
    logs = dict(logs or {})
    alerts: list[OpsAlert] = []

    heartbeat_ok = bool(gateway.get("heartbeat_ok"))
    heartbeat_age = _float(gateway.get("heartbeat_age_sec"))
    connected = bool(gateway.get("connected"))
    logged_in = bool(gateway.get("kiwoom_logged_in"))
    orderable = bool(gateway.get("orderable"))

    if not connected:
        alerts.append(
            OpsAlert(
                "GATEWAY_DISCONNECTED",
                "critical",
                "Gateway",
                "Gateway 연결 없음",
                "Core가 32bit Kiwoom Gateway heartbeat를 받지 못하고 있습니다.",
                "32bit Gateway 프로세스와 token/core-url 설정을 확인하세요.",
            )
        )
    elif not heartbeat_ok:
        alerts.append(
            OpsAlert(
                "GATEWAY_HEARTBEAT_STALE",
                "critical",
                "Gateway",
                "Gateway heartbeat 지연",
                f"마지막 heartbeat가 {heartbeat_age:.0f}초 전입니다.",
                "Gateway 터미널 오류, WebSocket fallback, 네트워크 루프 정지를 확인하세요.",
                {"heartbeat_age_sec": heartbeat_age},
            )
        )
    elif heartbeat_age > 10:
        alerts.append(
            OpsAlert(
                "GATEWAY_HEARTBEAT_SLOW",
                "warning",
                "Gateway",
                "Gateway heartbeat 느림",
                f"heartbeat age가 {heartbeat_age:.0f}초입니다.",
                "일시 지연인지 지속 지연인지 대시보드 전송 지표를 같이 확인하세요.",
                {"heartbeat_age_sec": heartbeat_age},
            )
        )

    if connected and not logged_in:
        alerts.append(
            OpsAlert(
                "KIWOOM_NOT_LOGGED_IN",
                "critical",
                "Kiwoom",
                "Kiwoom 로그인 안 됨",
                "Gateway는 연결됐지만 Kiwoom OpenAPI 로그인 상태가 아닙니다.",
                "32bit Gateway 화면의 Kiwoom 로그인창에서 로그인을 완료하세요.",
            )
        )
    if logged_in and not orderable:
        alerts.append(
            OpsAlert(
                "GATEWAY_NOT_ORDERABLE",
                "warning",
                "Safety",
                "주문 가능 플래그 꺼짐",
                "Kiwoom 로그인은 됐지만 orderable=false입니다.",
                "계좌 선택, 장 시간, OBSERVE/DRY_RUN/LIVE 정책을 확인하세요.",
            )
        )

    _append_command_alerts(alerts, commands)
    _append_transport_alerts(alerts, transport)
    _append_runtime_alerts(alerts, runtime)
    _append_performance_alerts(alerts, dry_run_performance)
    _append_log_alerts(alerts, logs)

    alerts.sort(key=lambda item: (-SEVERITY_ORDER.get(item.severity, 0), item.area, item.id))
    counts = {
        "critical": sum(1 for item in alerts if item.severity == "critical"),
        "warning": sum(1 for item in alerts if item.severity == "warning"),
        "info": sum(1 for item in alerts if item.severity == "info"),
    }
    highest = "ok"
    for severity in ("critical", "warning", "info"):
        if counts[severity]:
            highest = severity
            break
    return {
        "generated_at": utc_timestamp(),
        "summary": {
            **counts,
            "total": len(alerts),
            "highest_severity": highest,
            "safe_to_collect_data": counts["critical"] == 0 and heartbeat_ok and logged_in,
            "safe_to_run_ws_pilot": counts["critical"] == 0 and heartbeat_ok,
            "safe_to_live_order": False,
            "live_order_note": "LIVE 자동주문은 별도 안전 PR 전까지 허용하지 않습니다.",
        },
        "alerts": [alert.to_dict() for alert in alerts],
    }


def _append_command_alerts(alerts: list[OpsAlert], commands: dict[str, Any]) -> None:
    failed = _int(commands.get("failed_count"))
    rejected = _int(commands.get("rejected_count"))
    expired = _int(commands.get("expired_count"))
    stale = _int(commands.get("stale_dispatched_count"))
    rate_limited = _int(commands.get("rate_limited_count"))
    duplicate_rejected = _int(commands.get("duplicate_rejected_count"))

    if stale:
        alerts.append(
            OpsAlert(
                "COMMAND_STALE_DISPATCHED",
                "critical",
                "Command",
                "DISPATCHED 명령이 오래 남음",
                f"stale dispatched command가 {stale}건 있습니다.",
                "명령 상세와 이벤트 타임라인에서 Gateway 실행/ACK 누락 여부를 확인하세요.",
                {"stale_dispatched_count": stale},
            )
        )
    if failed:
        alerts.append(
            OpsAlert(
                "COMMAND_FAILED",
                "warning",
                "Command",
                "실패한 Gateway 명령 있음",
                f"FAILED command가 {failed}건 있습니다.",
                "/api/gateway/commands/history?status=FAILED 로 원인을 확인하세요.",
                {"failed_count": failed},
            )
        )
    if rejected:
        alerts.append(
            OpsAlert(
                "COMMAND_REJECTED",
                "warning",
                "Command",
                "거부된 Gateway 명령 있음",
                f"REJECTED command가 {rejected}건 있습니다.",
                "WebSocket pilot order 차단, allowlist, safety policy를 확인하세요.",
                {"rejected_count": rejected},
            )
        )
    if expired:
        alerts.append(
            OpsAlert(
                "COMMAND_EXPIRED",
                "info",
                "Command",
                "만료된 Gateway 명령 있음",
                f"EXPIRED command가 {expired}건 있습니다.",
                "재실행이 필요한 명령인지 history에서 확인하세요.",
                {"expired_count": expired},
            )
        )
    if rate_limited:
        alerts.append(
            OpsAlert(
                "COMMAND_RATE_LIMITED",
                "info",
                "Command",
                "rate limit 대기 발생",
                f"rate limited event가 {rate_limited}건 있습니다.",
                "Kiwoom pacing 문제인지 transport 지연인지 분리해서 보세요.",
                {"rate_limited_count": rate_limited},
            )
        )
    if duplicate_rejected:
        alerts.append(
            OpsAlert(
                "COMMAND_DUPLICATE_REJECTED",
                "info",
                "Command",
                "중복 명령 차단됨",
                f"중복 command {duplicate_rejected}건이 차단됐습니다.",
                "idempotency/dedupe가 동작한 정상 방어일 수 있습니다.",
                {"duplicate_rejected_count": duplicate_rejected},
            )
        )


def _append_transport_alerts(alerts: list[OpsAlert], transport: dict[str, Any]) -> None:
    warning_flags = list(transport.get("warning_flags") or [])
    real_pilot = dict(transport.get("real_gateway_websocket_pilot") or {})
    error_count = _int(transport.get("transport_error_count"))
    reconnect_count = _int(transport.get("reconnect_count"))
    event_p95 = _float(transport.get("event_latency_p95_ms"))
    command_p95 = _float(transport.get("command_latency_p95_ms"))
    ack_p95 = _float(transport.get("ack_latency_p95_ms"))

    if error_count:
        alerts.append(
            OpsAlert(
                "TRANSPORT_ERRORS",
                "critical",
                "Transport",
                "전송 오류 발생",
                f"최근 transport error가 {error_count}건 있습니다.",
                "Core/Gateway 로그와 recent transport errors를 확인하세요.",
                {"transport_error_count": error_count},
            )
        )
    if "EVENT_P95_HIGH" in warning_flags:
        alerts.append(
            OpsAlert(
                "TRANSPORT_EVENT_P95_HIGH",
                "warning",
                "Transport",
                "이벤트 지연 P95 높음",
                f"event latency p95가 {event_p95:.1f}ms입니다.",
                "heartbeat 위주 샘플인지, 실제 price/condition 이벤트 지연인지 latency table에서 확인하세요.",
                {"event_latency_p95_ms": event_p95},
            )
        )
    if "COMMAND_P95_HIGH" in warning_flags:
        alerts.append(
            OpsAlert(
                "TRANSPORT_COMMAND_P95_HIGH",
                "warning",
                "Transport",
                "명령 전달 P95 높음",
                f"command latency p95가 {command_p95:.1f}ms입니다.",
                "long-poll wait, WS queue wait, command backlog를 확인하세요.",
                {"command_latency_p95_ms": command_p95},
            )
        )
    if "ACK_P95_HIGH" in warning_flags:
        alerts.append(
            OpsAlert(
                "TRANSPORT_ACK_P95_HIGH",
                "warning",
                "Transport",
                "ACK 왕복 P95 높음",
                f"ACK latency p95가 {ack_p95:.1f}ms입니다.",
                "Gateway 실행 시간과 rate-limit wait를 분리해서 확인하세요.",
                {"ack_latency_p95_ms": ack_p95},
            )
        )
    if reconnect_count:
        alerts.append(
            OpsAlert(
                "TRANSPORT_RECONNECT",
                "warning",
                "Transport",
                "Gateway 재연결 발생",
                f"Gateway reconnect count가 {reconnect_count}입니다.",
                "장중 반복 재연결이면 WebSocket/REST 안정성 리포트를 남기세요.",
                {"reconnect_count": reconnect_count},
            )
        )

    if real_pilot.get("enabled"):
        if not real_pilot.get("connected"):
            alerts.append(
                OpsAlert(
                    "WS_PILOT_NOT_CONNECTED",
                    "warning",
                    "WebSocket",
                    "Real WS Pilot 미연결",
                    "WebSocket pilot이 enabled지만 connected=false입니다.",
                    "fallback 여부와 Gateway 터미널의 WebSocket 오류를 확인하세요.",
                )
            )
        fallback_reason = str(real_pilot.get("fallback_reason") or real_pilot.get("fallback_state") or "")
        if fallback_reason:
            alerts.append(
                OpsAlert(
                    "WS_PILOT_FALLBACK",
                    "warning",
                    "WebSocket",
                    "WebSocket pilot fallback 발생",
                    f"fallback={fallback_reason}",
                    "REST fallback 상태에서 중복 명령이 없는지 command history를 확인하세요.",
                    {"fallback_reason": fallback_reason},
                )
            )
        for key, title in [
            ("session_loss_count", "WebSocket 세션 손실"),
            ("duplicate_ack_count", "중복 ACK 감지"),
            ("unknown_ack_count", "Unknown ACK 감지"),
        ]:
            value = _int(real_pilot.get(key))
            if value:
                alerts.append(
                    OpsAlert(
                        f"WS_PILOT_{key.upper()}",
                        "warning",
                        "WebSocket",
                        title,
                        f"{title} 카운트가 {value}입니다.",
                        "pilot 상태에서는 즉시 원인을 확인하고 soak 결과에 기록하세요.",
                        {key: value},
                    )
                )
        blocked_orders = _int(real_pilot.get("blocked_order_command_count"))
        if blocked_orders:
            alerts.append(
                OpsAlert(
                    "WS_PILOT_ORDER_COMMAND_BLOCKED",
                    "info",
                    "Safety",
                    "WebSocket pilot 주문 명령 차단",
                    f"pilot 안전정책으로 order command {blocked_orders}건이 차단됐습니다.",
                    "LIVE enablement PR 전에는 정상 방어 동작입니다.",
                    {"blocked_order_command_count": blocked_orders},
                )
            )


def _append_runtime_alerts(alerts: list[OpsAlert], runtime: dict[str, Any]) -> None:
    if not runtime:
        return
    if runtime.get("enabled") and not runtime.get("running"):
        alerts.append(
            OpsAlert(
                "RUNTIME_ENABLED_NOT_RUNNING",
                "info",
                "Runtime",
                "Runtime enabled지만 정지 상태",
                "TRADING_RUNTIME_ENABLED=1 이지만 runtime loop가 running=false입니다.",
                "수동 운용이면 정상이고, 자동 수집이면 /api/runtime/start를 확인하세요.",
            )
        )
    last_error = str(runtime.get("last_error") or "")
    if last_error:
        alerts.append(
            OpsAlert(
                "RUNTIME_LAST_ERROR",
                "critical",
                "Runtime",
                "Runtime 마지막 오류 있음",
                last_error,
                "cycle 로그와 runtime_cycles를 확인하세요.",
            )
        )
    failed_cycles = _int(runtime.get("failed_cycle_count"))
    if failed_cycles:
        alerts.append(
            OpsAlert(
                "RUNTIME_FAILED_CYCLES",
                "warning",
                "Runtime",
                "Runtime cycle 실패 발생",
                f"failed cycle count가 {failed_cycles}입니다.",
                "반복 실패면 runtime을 stop 후 로그와 DB 상태를 확인하세요.",
                {"failed_cycle_count": failed_cycles},
            )
        )
    for warning in list(runtime.get("warnings") or [])[:5]:
        alerts.append(
            OpsAlert(
                "RUNTIME_WARNING",
                "info",
                "Runtime",
                "Runtime 경고",
                str(warning),
                "경고가 반복되는지 runtime status에서 확인하세요.",
            )
        )


def _append_performance_alerts(alerts: list[OpsAlert], performance: dict[str, Any]) -> None:
    false_positive = _int(performance.get("false_positive_count"))
    false_negative = _int(performance.get("false_negative_count"))
    opportunity_loss = _int(performance.get("opportunity_loss_count"))
    if false_positive:
        alerts.append(
            OpsAlert(
                "DRY_RUN_FALSE_POSITIVE",
                "info",
                "Performance",
                "DRY_RUN 오탐 사례 있음",
                f"false positive 후보가 {false_positive}건 집계됐습니다.",
                "데이터가 더 쌓이면 gate/risk threshold A/B 제안에 사용하세요.",
                {"false_positive_count": false_positive},
            )
        )
    if false_negative or opportunity_loss:
        alerts.append(
            OpsAlert(
                "DRY_RUN_OPPORTUNITY_LOSS",
                "info",
                "Performance",
                "미탐/기회손실 사례 있음",
                f"false negative {false_negative}건, opportunity loss {opportunity_loss}건입니다.",
                "거부 사유별 상승 사례를 장마감 리포트에서 확인하세요.",
                {"false_negative_count": false_negative, "opportunity_loss_count": opportunity_loss},
            )
        )


def _append_log_alerts(alerts: list[OpsAlert], logs: dict[str, Any]) -> None:
    warnings = list(logs.get("warnings") or [])
    if warnings:
        alerts.append(
            OpsAlert(
                "RECENT_WARNING_LOGS",
                "info",
                "Logs",
                "최근 경고 로그 있음",
                f"최근 warning log가 {len(warnings)}건 있습니다.",
                "로그 테이블에서 gateway/core 경고를 확인하세요.",
                {"sample": warnings[:3]},
            )
        )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
