from __future__ import annotations

from typing import Any


REASON_LABELS_KO: dict[str, str] = {
    "DATA_WAIT_REALTIME_TICK_MISSING": "실시간 tick 대기",
    "LATEST_TICK_MISSING": "실시간 tick 대기",
    "TR_PRICE_ONLY_NOT_READY": "TR 가격만 있어 실시간 확인 대기",
    "MARKET_RISK_OFF_BLOCK": "시장 RISK_OFF로 신규진입 차단",
    "MARKET_RISK_OFF_NEW_BUY_BLOCK": "시장 RISK_OFF로 신규 매수 차단",
    "MARKET_WEAK_WAIT": "약한 시장으로 진입 대기",
    "MARKET_DATA_WAIT": "시장 데이터 대기",
    "THEME_LEADER_ONLY_FOLLOWER_BLOCK": "대장주 단독 테마에서 후발주 차단",
    "PRICE_CHASE_HIGH_WAIT": "고가 추격 위험으로 가격 대기",
    "CHASE_HIGH": "고가 추격 위험",
    "VWAP_OVEREXTENDED": "VWAP 과열 구간",
    "OVERHEATED": "과열 구간 차단",
    "REAL_BROKER_BLOCKED": "실계좌 환경 감지로 주문 차단",
    "BROKER_ENV_UNKNOWN": "브로커 환경 확인 대기",
    "BROKER_NOT_LOGGED_IN": "Kiwoom 로그인 대기",
    "BROKER_NOT_ORDERABLE": "주문 가능 상태 아님",
    "ACCOUNT_NOT_CONFIGURED": "계좌 미설정",
    "ACCOUNT_NOT_WHITELISTED": "모의계좌 whitelist 미설정",
    "GATEWAY_HEARTBEAT_STALE": "Gateway heartbeat 지연",
    "COMMAND_QUEUE_UNHEALTHY": "명령 큐 상태 점검 필요",
    "LIVE_SIM_FLAG_DISABLED": "모의주문 비활성",
    "ORDER_MANAGER_DISABLED": "OrderManager 비활성",
    "ORDER_MANAGER_OBSERVE_ONLY": "관찰 전용 모드",
    "KILL_SWITCH_BLOCKS_BUY": "킬스위치로 신규 매수 차단",
    "POSITION_RISK_STOP_NEW_ENTRY": "포지션 리스크로 신규진입 중지 권고",
    "POSITION_RISK_KILL_SWITCH_RECOMMENDED": "포지션 리스크가 킬스위치 권고",
    "DAILY_BUY_ORDER_LIMIT": "일일 매수 주문 한도 도달",
    "DAILY_CODE_ORDER_LIMIT": "종목별 일일 주문 한도 도달",
    "MAX_OPEN_POSITIONS": "최대 보유 포지션 한도 도달",
    "MAX_ORDER_AMOUNT": "주문 금액 한도 초과",
    "MAX_ORDER_QUANTITY": "주문 수량 한도 초과",
    "MAX_THEME_EXPOSURE": "테마 노출 한도 초과",
    "DUPLICATE_OPEN_POSITION": "동일 종목 보유 중",
    "DUPLICATE_PENDING_ORDER": "동일 종목 주문 대기 중",
    "SPREAD_TOO_WIDE": "호가 스프레드 과다",
    "VI_ACTIVE_BUY_BLOCK": "VI 활성으로 신규 매수 차단",
    "UPPER_LIMIT_NEAR_BUY_BLOCK": "상한가 근접으로 신규 매수 차단",
    "STALE_ENTRY_DECISION": "진입 판단이 오래됨",
    "STALE_EXIT_DECISION": "청산 판단이 오래됨",
    "STALE_QUOTE": "시세가 오래됨",
    "UNMATCHED_EXECUTION": "체결 이벤트 수동 대조 필요",
}

THEME_STATUS_LABELS_KO = {
    "LEADING_THEME": "주도테마",
    "SPREADING_THEME": "확산테마",
    "LEADER_ONLY_THEME": "대장주 단독",
    "WATCH_THEME": "관찰",
    "WEAK_THEME": "약함",
    "DATA_WAIT": "데이터대기",
}

ENTRY_BUCKET_LABELS_KO = {
    "TIMING_READY": "진입 준비 관찰",
    "SETUP_READY": "가격 대기",
    "ORDER_PENDING": "주문 관리 중",
    "WAIT": "대기",
    "BLOCK": "차단",
}

SEVERITY_BY_REASON_PREFIX = (
    ("REAL_BROKER", "critical"),
    ("KILL_SWITCH", "critical"),
    ("MARKET_RISK_OFF", "critical"),
    ("BROKER_", "warning"),
    ("ACCOUNT_", "warning"),
    ("ORDER_", "warning"),
    ("MAX_", "warning"),
    ("STALE_", "warning"),
    ("DATA_WAIT", "info"),
    ("WAIT", "info"),
)


def reason_label_ko(reason_code: Any) -> str:
    code = str(reason_code or "").strip()
    return REASON_LABELS_KO.get(code, code or "-")


def reason_severity(reason_code: Any) -> str:
    code = str(reason_code or "").strip().upper()
    for prefix, severity in SEVERITY_BY_REASON_PREFIX:
        if code.startswith(prefix):
            return severity
    if "BLOCK" in code or "REJECT" in code:
        return "warning"
    if "WAIT" in code or "MISSING" in code:
        return "info"
    return "normal"


def suggested_action_ko(reason_code: Any) -> str:
    code = str(reason_code or "").strip().upper()
    if code in {"REAL_BROKER_BLOCKED", "BROKER_ENV_UNKNOWN"}:
        return "브로커 환경과 모의투자 접속 여부를 확인한다."
    if code in {"ACCOUNT_NOT_WHITELISTED", "ACCOUNT_NOT_CONFIGURED"}:
        return "모의계좌 whitelist와 계좌 설정을 확인한다."
    if "HEARTBEAT" in code:
        return "Gateway 연결과 heartbeat를 확인한다."
    if code.startswith("MARKET_RISK_OFF"):
        return "신규진입은 중단하고 보유 리스크 축소 판단을 우선 확인한다."
    if code.startswith("DATA_WAIT") or "MISSING" in code or "STALE" in code:
        return "실시간 tick, candle, TR 보강 상태를 기다리거나 데이터 연결을 점검한다."
    if "CHASE" in code or "OVEREXTENDED" in code:
        return "추격 구간을 피하고 VWAP/눌림 재확인을 기다린다."
    if "KILL_SWITCH" in code:
        return "신규 매수는 중단하고 포지션 축소와 runbook 기준을 확인한다."
    if code.startswith("MAX_") or "LIMIT" in code:
        return "일일 한도와 테마/종목 노출을 확인한다."
    return "관련 섹션의 상세 사유와 최신 데이터를 확인한다."


def theme_status_label_ko(status: Any) -> str:
    value = str(status or "").strip()
    return THEME_STATUS_LABELS_KO.get(value, value or "-")


def entry_bucket_label_ko(bucket: Any) -> str:
    value = str(bucket or "").strip()
    return ENTRY_BUCKET_LABELS_KO.get(value, value or "-")
