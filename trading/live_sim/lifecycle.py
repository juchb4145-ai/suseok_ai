from __future__ import annotations

from typing import Iterable


LIVE_SIM_ORDER_STATUSES: tuple[str, ...] = (
    "CREATED",
    "BLOCKED",
    "DUPLICATE",
    "SUBMITTED",
    "UNKNOWN_SUBMIT",
    "ACCEPTED",
    "PARTIAL_FILLED",
    "FILLED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "REJECTED",
    "FAILED",
    "EXPIRED",
    "RECONCILE_REQUIRED",
)

LIVE_SIM_TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    {
        "BLOCKED",
        "DUPLICATE",
        "FILLED",
        "CANCELLED",
        "REJECTED",
        "FAILED",
        "EXPIRED",
    }
)

LIVE_SIM_NON_TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    set(LIVE_SIM_ORDER_STATUSES) - LIVE_SIM_TERMINAL_ORDER_STATUSES
)

LIVE_SIM_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("CREATED", "BLOCKED"),
        ("CREATED", "DUPLICATE"),
        ("CREATED", "SUBMITTED"),
        ("SUBMITTED", "UNKNOWN_SUBMIT"),
        ("SUBMITTED", "ACCEPTED"),
        ("SUBMITTED", "REJECTED"),
        ("SUBMITTED", "FAILED"),
        ("SUBMITTED", "EXPIRED"),
        ("ACCEPTED", "PARTIAL_FILLED"),
        ("ACCEPTED", "FILLED"),
        ("ACCEPTED", "CANCEL_REQUESTED"),
        ("PARTIAL_FILLED", "FILLED"),
        ("PARTIAL_FILLED", "CANCEL_REQUESTED"),
        ("UNKNOWN_SUBMIT", "ACCEPTED"),
        ("UNKNOWN_SUBMIT", "PARTIAL_FILLED"),
        ("UNKNOWN_SUBMIT", "FILLED"),
        ("UNKNOWN_SUBMIT", "RECONCILE_REQUIRED"),
        ("CANCEL_REQUESTED", "CANCELLED"),
        ("CANCEL_REQUESTED", "PARTIAL_FILLED"),
        ("CANCEL_REQUESTED", "FILLED"),
        ("CANCEL_REQUESTED", "RECONCILE_REQUIRED"),
        ("RECONCILE_REQUIRED", "ACCEPTED"),
        ("RECONCILE_REQUIRED", "PARTIAL_FILLED"),
        ("RECONCILE_REQUIRED", "FILLED"),
        ("RECONCILE_REQUIRED", "CANCELLED"),
    }
)


def normalize_live_sim_status(value: object) -> str:
    return str(value or "").strip().upper()


def is_live_sim_terminal_status(value: object) -> bool:
    return normalize_live_sim_status(value) in LIVE_SIM_TERMINAL_ORDER_STATUSES


def is_live_sim_non_terminal_status(value: object) -> bool:
    status = normalize_live_sim_status(value)
    return status in LIVE_SIM_NON_TERMINAL_ORDER_STATUSES


def validate_live_sim_transition(status_from: object, status_to: object) -> dict[str, object]:
    from_status = normalize_live_sim_status(status_from)
    to_status = normalize_live_sim_status(status_to)
    if not to_status:
        return {
            "ok": True,
            "status_from": from_status,
            "status_to": to_status,
            "warning_code": "",
            "operator_message_ko": "",
        }
    if from_status == to_status or not from_status:
        return _transition_ok(from_status, to_status)
    if from_status not in LIVE_SIM_ORDER_STATUSES:
        return _transition_warning(
            from_status,
            to_status,
            "LIVE_SIM_UNKNOWN_STATUS_FROM",
            "이전 주문 상태를 알 수 없어 상태 전이 확인이 필요합니다.",
        )
    if to_status not in LIVE_SIM_ORDER_STATUSES:
        return _transition_warning(
            from_status,
            to_status,
            "LIVE_SIM_UNKNOWN_STATUS_TO",
            "새 주문 상태를 알 수 없어 상태 전이 확인이 필요합니다.",
        )
    if to_status == "RECONCILE_REQUIRED" and from_status not in LIVE_SIM_TERMINAL_ORDER_STATUSES:
        return _transition_ok(from_status, to_status)
    if (from_status, to_status) in LIVE_SIM_ALLOWED_TRANSITIONS:
        return _transition_ok(from_status, to_status)
    return _transition_warning(
        from_status,
        to_status,
        "LIVE_SIM_INVALID_STATUS_TRANSITION",
        _invalid_transition_message(from_status, to_status),
    )


def transition_warning_codes(events: Iterable[dict]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for event in events:
        result = validate_live_sim_transition(event.get("status_from"), event.get("status_to"))
        if result.get("ok"):
            continue
        warnings.append({**result, "event": event})
    return warnings


def _transition_ok(status_from: str, status_to: str) -> dict[str, object]:
    return {
        "ok": True,
        "status_from": status_from,
        "status_to": status_to,
        "warning_code": "",
        "operator_message_ko": "",
    }


def _transition_warning(status_from: str, status_to: str, code: str, message_ko: str) -> dict[str, object]:
    return {
        "ok": False,
        "status_from": status_from,
        "status_to": status_to,
        "warning_code": code,
        "operator_message_ko": message_ko,
    }


def _invalid_transition_message(status_from: str, status_to: str) -> str:
    if status_from == "FILLED" and status_to == "PARTIAL_FILLED":
        return "완전체결 이후 부분체결로 되돌아간 상태 전이입니다."
    if status_from == "CANCELLED" and status_to == "FILLED":
        return "취소 완료 이후 체결로 바뀐 상태입니다. broker 보정 여부를 확인하세요."
    if status_from in {"BLOCKED", "DUPLICATE"} and status_to == "SUBMITTED":
        return "차단 또는 중복 처리된 주문이 제출 상태로 바뀌었습니다."
    if status_from == "REJECTED" and status_to == "FILLED":
        return "거절된 주문에 체결이 연결되었습니다. 주문번호/체결 연결을 재확인하세요."
    return "허용되지 않은 LIVE_SIM 주문 상태 전이입니다."
