from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from datetime import datetime
from threading import RLock
from typing import Any, Callable

from storage.db import TradingDatabase
from trading.theme_engine.backfill import THEME_BACKFILL_PURPOSE
from trading.theme_engine.repository import ThemeEngineRepository
from trading_app.conservative_reason_outcomes import (
    ConservativeReasonOutcomeAnalyzer,
    empty_payload as conservative_reason_empty_payload,
    snapshot_payload as conservative_reason_snapshot_payload,
)
from trading_app.live_sim_audit import LiveSimLifecycleAuditor
from trading_app.runtime_load_guard import build_runtime_load_guard_snapshot
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
OPERATOR_TERM_DICTIONARY: dict[str, dict[str, str]] = {
    "READY": {
        "label_ko": "매수 가능",
        "short_label_ko": "가능",
        "description_ko": "매수 게이트를 통과한 후보입니다.",
        "severity": "positive",
        "operator_action_ko": "후보 목록과 주문 안전 상태를 확인하세요.",
    },
    "READY_SMALL": {
        "label_ko": "소액 매수 가능",
        "short_label_ko": "소액",
        "description_ko": "정상 비중보다 작은 관찰성 진입 후보입니다.",
        "severity": "positive",
        "operator_action_ko": "소액 진입 조건과 당일 한도를 확인하세요.",
    },
    "EARLY_SMALL": {
        "label_ko": "장초반 소액 후보",
        "short_label_ko": "초반소액",
        "description_ko": "장초반 데이터가 완성되기 전 제한적으로 관찰하는 소액 후보입니다.",
        "severity": "neutral",
        "operator_action_ko": "분봉/VWAP/지지선 준비 상태를 확인하세요.",
    },
    "READY_EARLY_SMALL": {
        "label_ko": "장초반 소액 후보",
        "short_label_ko": "초반소액",
        "description_ko": "장초반 데이터가 완성되기 전 제한적으로 관찰하는 소액 후보입니다.",
        "severity": "neutral",
        "operator_action_ko": "분봉/VWAP/지지선 준비 상태를 확인하세요.",
    },
    "READY_SHADOW_SMALL_ENTRY": {
        "label_ko": "검증 기반 소액 후보",
        "short_label_ko": "검증소액",
        "description_ko": "검증 리포트 근거가 있는 소액 진입 후보입니다.",
        "severity": "positive",
        "operator_action_ko": "소액 진입 실험 탭에서 한도와 가드 상태를 확인하세요.",
    },
    "WAIT": {
        "label_ko": "대기",
        "short_label_ko": "대기",
        "description_ko": "조건 확인이 더 필요해 즉시 매수하지 않습니다.",
        "severity": "neutral",
        "operator_action_ko": "재확인 조건과 대기 사유를 확인하세요.",
    },
    "OBSERVE": {
        "label_ko": "관측",
        "short_label_ko": "관측",
        "description_ko": "주문보다 관찰이 우선인 후보입니다.",
        "severity": "neutral",
        "operator_action_ko": "테마와 가격 위치 변화를 지켜보세요.",
    },
    "BLOCKED": {
        "label_ko": "차단",
        "short_label_ko": "차단",
        "description_ko": "리스크 또는 안전 조건 때문에 신규 진입을 막고 있습니다.",
        "severity": "danger",
        "operator_action_ko": "차단 사유와 주문/리스크 상태를 확인하세요.",
    },
    "DATA_INSUFFICIENT": {
        "label_ko": "데이터 부족",
        "short_label_ko": "데이터",
        "description_ko": "현재 판단에 필요한 실시간 가격/지표가 충분하지 않습니다.",
        "severity": "warning",
        "operator_action_ko": "실시간 수신, 분봉 캐시, VWAP/지지선 상태를 확인하세요.",
    },
    "CORE_BLOCKING": {
        "label_ko": "핵심 데이터 부족",
        "short_label_ko": "핵심부족",
        "description_ko": "진입 판단의 핵심 데이터가 없어 보수적으로 막았습니다.",
        "severity": "warning",
        "operator_action_ko": "현재가, 분봉, VWAP, 지지선 수집 상태를 먼저 확인하세요.",
    },
    "ENTRY_BLOCKING": {
        "label_ko": "진입 판단 데이터 부족",
        "short_label_ko": "진입부족",
        "description_ko": "진입 위치 판단에 필요한 데이터가 부족합니다.",
        "severity": "warning",
        "operator_action_ko": "가격 위치와 지지선/VWAP 준비 상태를 확인하세요.",
    },
    "WARMUP_OPTIONAL": {
        "label_ko": "보조지표 준비중",
        "short_label_ko": "준비중",
        "description_ko": "보조지표가 장초반 또는 데이터 수집 과정에서 준비 중입니다.",
        "severity": "neutral",
        "operator_action_ko": "추가 분봉 수집을 기다리세요.",
    },
    "BACKFILL_ONLY_OBSERVE": {
        "label_ko": "실시간 미확인",
        "short_label_ko": "미확인",
        "description_ko": "TR 보강 데이터만 있고 실시간 흐름 확인이 부족합니다.",
        "severity": "neutral",
        "operator_action_ko": "실시간 조건식/틱 수신 여부를 확인하세요.",
    },
    "LATEST_TICK_STALE": {
        "label_ko": "실시간 틱 지연",
        "short_label_ko": "틱지연",
        "description_ko": "최신 틱 수신이 지연되어 판단을 보수적으로 봅니다.",
        "severity": "warning",
        "operator_action_ko": "실시간 수신과 게이트웨이 heartbeat를 확인하세요.",
    },
    "VWAP_MISSING": {
        "label_ko": "VWAP 미확인",
        "short_label_ko": "VWAP없음",
        "description_ko": "VWAP 계산 또는 수신이 아직 확인되지 않았습니다.",
        "severity": "warning",
        "operator_action_ko": "분봉 수집과 VWAP 계산 상태를 확인하세요.",
    },
    "LATE_CHASE": {
        "label_ko": "뒤늦은 추격 위험",
        "short_label_ko": "추격위험",
        "description_ko": "이미 오른 뒤 따라붙는 구간이라 진입을 기다립니다.",
        "severity": "warning",
        "operator_action_ko": "눌림 또는 VWAP/지지선 회복을 기다리세요.",
    },
    "LATE_CHASE_TEMP_WAIT": {
        "label_ko": "뒤늦은 추격 위험",
        "short_label_ko": "추격대기",
        "description_ko": "늦은 추격 위험이 있어 잠시 뒤 재확인합니다.",
        "severity": "warning",
        "operator_action_ko": "재확인 시간 이후 가격 위치를 다시 확인하세요.",
    },
    "CHASE_HIGH": {
        "label_ko": "고점 추격 위험",
        "short_label_ko": "고점추격",
        "description_ko": "고점 부근 추격 매수 위험이 큽니다.",
        "severity": "warning",
        "operator_action_ko": "고점 이탈 여부와 눌림 전환을 확인하세요.",
    },
    "CHASE_RISK": {
        "label_ko": "추격 위험",
        "short_label_ko": "추격",
        "description_ko": "가격 위치상 추격 매수 위험이 큽니다.",
        "severity": "warning",
        "operator_action_ko": "즉시 진입보다 눌림 확인을 우선하세요.",
    },
    "CHASE_RISK_BLOCKED": {
        "label_ko": "추격 위험 차단",
        "short_label_ko": "추격차단",
        "description_ko": "추격 매수 위험으로 신규 진입을 차단했습니다.",
        "severity": "danger",
        "operator_action_ko": "가격 위치가 안정될 때까지 기다리세요.",
    },
    "VWAP_OVEREXTENDED": {
        "label_ko": "VWAP 대비 과열",
        "short_label_ko": "VWAP과열",
        "description_ko": "현재가가 VWAP 대비 과도하게 벌어져 있습니다.",
        "severity": "warning",
        "operator_action_ko": "VWAP 근처 재접근 또는 눌림을 기다리세요.",
    },
    "BREAKOUT_CONTINUATION": {
        "label_ko": "돌파 연속 구간",
        "short_label_ko": "돌파연속",
        "description_ko": "돌파 이후 연속 상승 구간이라 추격 위험을 확인해야 합니다.",
        "severity": "warning",
        "operator_action_ko": "눌림 또는 지지선 회복 확인 후 접근하세요.",
    },
    "LOW_BREADTH": {
        "label_ko": "테마 확산 부족",
        "short_label_ko": "확산부족",
        "description_ko": "테마 내 동반 강세 폭이 충분하지 않습니다.",
        "severity": "neutral",
        "operator_action_ko": "동반 상승 종목 수와 거래대금 확산을 확인하세요.",
    },
    "LEADER_ONLY_THEME": {
        "label_ko": "대장주 단독 흐름",
        "short_label_ko": "단독흐름",
        "description_ko": "대장주는 강하지만 테마 전체 확산이 약합니다.",
        "severity": "neutral",
        "operator_action_ko": "공동대장과 후발주 확산 여부를 확인하세요.",
    },
    "RISK_OFF": {
        "label_ko": "시장 위험",
        "short_label_ko": "시장위험",
        "description_ko": "시장 상태가 약해 신규 진입을 보수적으로 봅니다.",
        "severity": "danger",
        "operator_action_ko": "KOSPI/KOSDAQ 회복 확인을 기다리세요.",
    },
    "WAIT_MARKET_CONFIRMATION_PENDING": {
        "label_ko": "시장 회복 확인 대기",
        "short_label_ko": "시장대기",
        "description_ko": "시장 회복이 확인될 때까지 대기합니다.",
        "severity": "neutral",
        "operator_action_ko": "시장 폭과 지수 회복 지속 여부를 확인하세요.",
    },
    "WAIT_MARKET_RECOVERY_PENDING": {
        "label_ko": "시장 회복 대기",
        "short_label_ko": "회복대기",
        "description_ko": "시장 약세 이후 회복 확인을 기다립니다.",
        "severity": "neutral",
        "operator_action_ko": "회복 연속 사이클을 확인하세요.",
    },
    "SUPPORT_NOT_READY": {
        "label_ko": "지지선 확인 전",
        "short_label_ko": "지지전",
        "description_ko": "최근 지지선이 아직 충분히 확인되지 않았습니다.",
        "severity": "neutral",
        "operator_action_ko": "지지선 수집과 가격 재확인을 기다리세요.",
    },
    "VWAP_RECLAIM": {
        "label_ko": "VWAP 회복",
        "short_label_ko": "VWAP회복",
        "description_ko": "가격이 VWAP 위로 회복했습니다.",
        "severity": "positive",
        "operator_action_ko": "다른 리스크 조건과 주문 가능 여부를 함께 확인하세요.",
    },
    "GOOD_PULLBACK": {
        "label_ko": "좋은 눌림",
        "short_label_ko": "눌림",
        "description_ko": "과열을 피한 눌림 구간으로 판단됩니다.",
        "severity": "positive",
        "operator_action_ko": "테마 강도와 주문 안전 상태를 확인하세요.",
    },
    "PULLBACK_RECLAIM": {
        "label_ko": "눌림 후 회복",
        "short_label_ko": "회복",
        "description_ko": "눌림 이후 가격이 다시 회복하는 흐름입니다.",
        "severity": "positive",
        "operator_action_ko": "회복이 유지되는지 확인하세요.",
    },
    "LIVE_SIM_BLOCKED": {
        "label_ko": "모의투자 주문 차단",
        "short_label_ko": "모의차단",
        "description_ko": "LIVE_SIM 주문 안전장치가 신규 주문을 막고 있습니다.",
        "severity": "danger",
        "operator_action_ko": "LIVE_SIM audit, reconcile, 주문번호 상태를 확인하세요.",
    },
    "RECONCILE_REQUIRED": {
        "label_ko": "주문/잔고 재확인 필요",
        "short_label_ko": "재확인",
        "description_ko": "주문 또는 잔고 상태를 다시 맞춰야 합니다.",
        "severity": "danger",
        "operator_action_ko": "미체결, 체결, 포지션 원장을 대조하세요.",
    },
    "UNKNOWN_SUBMIT": {
        "label_ko": "주문 결과 확인 필요",
        "short_label_ko": "결과확인",
        "description_ko": "주문 제출 결과가 명확하지 않습니다.",
        "severity": "danger",
        "operator_action_ko": "주문번호와 broker 응답을 확인하세요.",
    },
    "ORDER_SINK_NOOP": {
        "label_ko": "주문 경로 비활성",
        "short_label_ko": "경로비활성",
        "description_ko": "주문 sink가 실제 제출하지 않는 모드입니다.",
        "severity": "neutral",
        "operator_action_ko": "운영 모드가 관측 전용인지 확인하세요.",
    },
    "ENTRY_PLAN_DIAGNOSTIC_ONLY": {
        "label_ko": "진단 전용 후보",
        "short_label_ko": "진단전용",
        "description_ko": "주문 후보가 아니라 진단과 관찰을 위한 후보입니다.",
        "severity": "neutral",
        "operator_action_ko": "주문이 나가지 않는 것이 정상인지 확인하세요.",
    },
    "GATEWAY_DISCONNECTED": {
        "label_ko": "게이트웨이 미연결",
        "short_label_ko": "미연결",
        "description_ko": "32bit Kiwoom Gateway가 Core와 연결되어 있지 않습니다.",
        "severity": "danger",
        "operator_action_ko": "게이트웨이 실행 상태와 네트워크 연결을 확인하세요.",
    },
    "GATEWAY_HEARTBEAT_BAD": {
        "label_ko": "게이트웨이 응답 지연",
        "short_label_ko": "응답지연",
        "description_ko": "게이트웨이 heartbeat가 정상으로 들어오지 않습니다.",
        "severity": "danger",
        "operator_action_ko": "게이트웨이 프로세스가 멈췄는지 확인하고 재연결 상태를 보세요.",
    },
    "KIWOOM_NOT_LOGGED_IN": {
        "label_ko": "키움 미로그인",
        "short_label_ko": "미로그인",
        "description_ko": "Kiwoom OpenAPI 로그인이 확인되지 않았습니다.",
        "severity": "danger",
        "operator_action_ko": "키움 로그인 창과 계정 접속 상태를 확인하세요.",
    },
    "KIWOOM_NOT_ORDERABLE": {
        "label_ko": "키움 주문 불가",
        "short_label_ko": "주문불가",
        "description_ko": "키움 연결은 있어도 주문 가능 상태가 아닙니다.",
        "severity": "danger",
        "operator_action_ko": "계좌/모의투자 접속, 주문 가능 플래그, 장 상태를 확인하세요.",
    },
    "SHADOW_PROMOTION_EVIDENCE_NOT_READY": {
        "label_ko": "승격 근거 부족",
        "short_label_ko": "근거부족",
        "description_ko": "Shadow Small Entry를 LIVE_SIM으로 올릴 충분한 승격 근거가 아직 없습니다.",
        "severity": "warning",
        "operator_action_ko": "리포트 탭에서 승격 후보와 outcome 근거가 쌓였는지 확인하세요.",
    },
    "SNAPSHOT_UNAVAILABLE": {
        "label_ko": "스냅샷 대기",
        "short_label_ko": "대기",
        "description_ko": "ThemeLabFlow 결과를 아직 받지 못했습니다.",
        "severity": "neutral",
        "operator_action_ko": "런타임과 데이터 수신 상태를 확인하세요.",
    },
    "RUNTIME_INACTIVE": {
        "label_ko": "런타임 중지",
        "short_label_ko": "중지",
        "description_ko": "전략 런타임이 현재 동작하지 않습니다.",
        "severity": "warning",
        "operator_action_ko": "Runtime 상태를 확인하세요.",
    },
    "SNAPSHOT_STALE": {
        "label_ko": "스냅샷 지연",
        "short_label_ko": "지연",
        "description_ko": "ThemeLab 스냅샷이 오래되어 최신 상태가 아닙니다.",
        "severity": "warning",
        "operator_action_ko": "스냅샷 갱신과 런타임 상태를 확인하세요.",
    },
    "READY_TO_TRADE": {
        "label_ko": "매수 가능",
        "short_label_ko": "가능",
        "description_ko": "매수 가능 후보와 주문 안전 상태가 확인되었습니다.",
        "severity": "positive",
        "operator_action_ko": "후보 목록과 주문 상태를 확인하세요.",
    },
    "READY_BUT_LIVE_BLOCKED": {
        "label_ko": "후보 있음, 주문 차단",
        "short_label_ko": "주문차단",
        "description_ko": "READY 후보는 있으나 주문 안전장치가 막고 있습니다.",
        "severity": "warning",
        "operator_action_ko": "LIVE_SIM guard와 audit 상태를 확인하세요.",
    },
    "WAIT_DATA_QUALITY": {
        "label_ko": "데이터 준비중",
        "short_label_ko": "데이터",
        "description_ko": "분봉/VWAP/지지선 등 판단 데이터가 준비 중입니다.",
        "severity": "warning",
        "operator_action_ko": "실시간 데이터 수집 상태를 확인하세요.",
    },
    "WAIT_MARKET_CONFIRMATION": {
        "label_ko": "시장 확인 대기",
        "short_label_ko": "시장대기",
        "description_ko": "시장 회복 또는 강도 확인이 필요합니다.",
        "severity": "neutral",
        "operator_action_ko": "지수와 시장 폭을 확인하세요.",
    },
    "WAIT_MARKET_RISK_OFF": {
        "label_ko": "시장 위험 대기",
        "short_label_ko": "위험대기",
        "description_ko": "시장 위험 상태가 풀리기를 기다립니다.",
        "severity": "warning",
        "operator_action_ko": "시장 회복 연속성과 리스크 상태를 확인하세요.",
    },
    "WAIT_MARKET_CONDITION": {
        "label_ko": "시장 조건 대기",
        "short_label_ko": "시장조건",
        "description_ko": "시장 조건이 충분히 우호적이지 않습니다.",
        "severity": "neutral",
        "operator_action_ko": "시장 조건이 개선되는지 확인하세요.",
    },
    "RISK_BLOCKED": {
        "label_ko": "위험 차단",
        "short_label_ko": "차단",
        "description_ko": "리스크 조건 때문에 신규 진입을 막고 있습니다.",
        "severity": "danger",
        "operator_action_ko": "차단 사유와 재확인 시간을 확인하세요.",
    },
    "OBSERVE_ONLY": {
        "label_ko": "관측전용",
        "short_label_ko": "관측",
        "description_ko": "현재는 주문보다 관측이 우선입니다.",
        "severity": "neutral",
        "operator_action_ko": "테마와 후보 변화를 지켜보세요.",
    },
    "NO_SIGNAL": {
        "label_ko": "매수 후보 없음",
        "short_label_ko": "없음",
        "description_ko": "현재 주문 후보가 없습니다.",
        "severity": "neutral",
        "operator_action_ko": "안 산 이유 탭에서 큰 사유를 확인하세요.",
    },
    "OK": {
        "label_ko": "정상",
        "short_label_ko": "정상",
        "description_ko": "확인된 문제가 없습니다.",
        "severity": "positive",
        "operator_action_ko": "특별 조치가 필요하지 않습니다.",
    },
    "WARNING": {
        "label_ko": "확인 필요",
        "short_label_ko": "확인",
        "description_ko": "일부 상태 확인이 필요합니다.",
        "severity": "warning",
        "operator_action_ko": "상세 탭에서 원인을 확인하세요.",
    },
    "DIAGNOSTIC": {
        "label_ko": "진단 전용",
        "short_label_ko": "진단",
        "description_ko": "운영 장애가 아니라 진단/리포트용으로 확인할 상태입니다.",
        "severity": "neutral",
        "operator_action_ko": "장중 재개 전까지 참고 지표로만 확인하세요.",
    },
    "ERROR": {
        "label_ko": "문제 있음",
        "short_label_ko": "문제",
        "description_ko": "운영자가 확인해야 할 문제가 있습니다.",
        "severity": "danger",
        "operator_action_ko": "상세 로그와 audit 상태를 확인하세요.",
    },
    "NO_DATA": {
        "label_ko": "데이터 없음",
        "short_label_ko": "없음",
        "description_ko": "아직 표시할 데이터가 없습니다.",
        "severity": "muted",
        "operator_action_ko": "데이터가 쌓일 때까지 기다리세요.",
    },
}
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
THEME_LAB_DASHBOARD_REPORT_CACHE_TTL_SEC = 120.0
THEME_LAB_DASHBOARD_REPORT_STALE_TTL_SEC = 600.0
THEME_LAB_DASHBOARD_REPORT_CACHE_MAX_ITEMS = 64
_theme_lab_dashboard_report_cache: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}
_theme_lab_dashboard_report_cache_lock = RLock()


def _theme_lab_dashboard_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def _theme_lab_dashboard_report_cache_ttl_sec() -> float:
    return _theme_lab_dashboard_float_env(
        "TRADING_THEMELAB_DASHBOARD_REPORT_CACHE_TTL_SEC",
        THEME_LAB_DASHBOARD_REPORT_CACHE_TTL_SEC,
    )


def _theme_lab_dashboard_report_stale_ttl_sec() -> float:
    return _theme_lab_dashboard_float_env(
        "TRADING_THEMELAB_DASHBOARD_REPORT_STALE_TTL_SEC",
        THEME_LAB_DASHBOARD_REPORT_STALE_TTL_SEC,
    )


def _theme_lab_dashboard_database_cache_key(db: TradingDatabase) -> str:
    try:
        return str(db.path.resolve())
    except Exception:
        return str(getattr(db, "path", "") or "")


def _prune_theme_lab_dashboard_report_cache(now: float, ttl: float) -> None:
    expired_keys = [
        key for key, (cached_at, _) in _theme_lab_dashboard_report_cache.items() if now - cached_at > ttl
    ]
    for key in expired_keys:
        _theme_lab_dashboard_report_cache.pop(key, None)
    overflow = len(_theme_lab_dashboard_report_cache) - THEME_LAB_DASHBOARD_REPORT_CACHE_MAX_ITEMS
    if overflow > 0:
        oldest_keys = sorted(
            _theme_lab_dashboard_report_cache,
            key=lambda key: _theme_lab_dashboard_report_cache[key][0],
        )[:overflow]
        for key in oldest_keys:
            _theme_lab_dashboard_report_cache.pop(key, None)


def _cached_theme_lab_dashboard_report(
    db: TradingDatabase,
    report_name: str,
    *,
    trade_date: str = "",
    extra_key: str = "",
    builder: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    ttl = _theme_lab_dashboard_report_cache_ttl_sec()
    if ttl <= 0.0:
        return builder()
    stale_ttl = max(ttl, _theme_lab_dashboard_report_stale_ttl_sec())
    cache_key = (
        _theme_lab_dashboard_database_cache_key(db),
        report_name,
        str(trade_date or ""),
        str(extra_key or ""),
    )
    now = time.monotonic()
    with _theme_lab_dashboard_report_cache_lock:
        cached = _theme_lab_dashboard_report_cache.get(cache_key)
        if cached is not None and now - cached[0] <= ttl:
            return cached[1]
        if cached is not None and now - cached[0] <= stale_ttl:
            return cached[1]
    value = builder()
    stored_at = time.monotonic()
    with _theme_lab_dashboard_report_cache_lock:
        _prune_theme_lab_dashboard_report_cache(stored_at, ttl)
        _theme_lab_dashboard_report_cache[cache_key] = (stored_at, value)
    return value


def _gateway_report_cache_key(gateway_state: Any | None) -> str:
    if gateway_state is None:
        return "gateway:none"
    try:
        gateway = gateway_state.snapshot().to_dict()
    except Exception:
        return "gateway:unavailable"
    heartbeat = dict(gateway.get("last_heartbeat_payload") or {})
    fields = (
        gateway.get("connected"),
        gateway.get("heartbeat_ok"),
        gateway.get("kiwoom_logged_in"),
        gateway.get("orderable"),
        gateway.get("connection_state"),
        gateway.get("mode"),
        gateway.get("account"),
        gateway.get("broker_env") or heartbeat.get("broker_env"),
        heartbeat.get("server_mode"),
        heartbeat.get("kiwoom_logged_in"),
        heartbeat.get("orderable"),
        heartbeat.get("kill_switch_active"),
    )
    return "|".join(str(value) for value in fields)


def build_theme_lab_dashboard_snapshot(
    db: TradingDatabase,
    *,
    runtime_status: dict[str, Any] | None = None,
    gateway_state: Any | None = None,
    now: datetime | None = None,
    include_extended: bool = True,
) -> dict[str, Any]:
    raw = db.latest_theme_lab_flow_result()
    theme_source_sync = _theme_source_sync_status(db)
    if not raw:
        return _empty_snapshot(
            runtime_status=runtime_status,
            theme_source_sync=theme_source_sync,
            db=db,
            gateway_state=gateway_state,
            include_extended=include_extended,
        )

    themes = _as_list(raw.get("theme_rankings") or raw.get("theme_condition_snapshots"))
    gate_decisions = _as_list(raw.get("gate_decisions"))
    watchset = _sorted_watchset(_merge_watchset_gate_decisions(_as_list(raw.get("watchset_snapshots")), gate_decisions))
    condition_counts = _condition_theme_counts(db, raw)
    runtime = _runtime_context(runtime_status)
    freshness = _snapshot_freshness(raw, now=now)
    backfill_runtime = _theme_backfill_runtime(raw, gateway_state)
    data_quality = _data_quality(raw, watchset)
    data_quality = _apply_backfill_data_quality(data_quality, backfill_runtime)
    data_quality = {**data_quality, **_freshness_quality_fields(freshness)}
    entry_candidates = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    chart_universe = _chart_universe(themes, watchset, entry_candidates)
    selected = _select_chart(chart_universe, watchset)
    selected_watch = next((item for item in watchset if item.get("symbol") == selected.get("symbol")), {})

    gateway = _gateway_context(gateway_state)
    backfill_status_by_theme = _theme_backfill_status_by_theme(gateway_state)
    ranked_themes = _ranked_theme_rows(themes, condition_counts, backfill_status_by_theme=backfill_status_by_theme)
    data_quality = _apply_ranked_theme_data_quality(data_quality, ranked_themes)
    market = _market(raw.get("market_status") or {}, runtime=runtime)
    data_quality = _apply_market_session_data_quality_display(data_quality, market, runtime)
    summary = _summary(ranked_themes, watchset, entry_candidates, data_quality, runtime=runtime, freshness=freshness)
    trade_date = _snapshot_trade_date(raw)
    if include_extended:
        gate_reason_report = _theme_lab_gate_reason_source_report(db, trade_date=trade_date)
        gate_reason_outcomes = _theme_lab_gate_reason_outcomes_payload(gate_reason_report)
        live_sim_audit = _live_sim_audit(db, gateway_state=gateway_state, trade_date=trade_date)
        conservative_reason_report = _conservative_reason_report(
            db,
            trade_date=trade_date,
            source_report=gate_reason_report,
        )
        conservative_reason_outcomes = _conservative_reason_outcomes_payload(conservative_reason_report)
        shadow_small_entry_promotion_report = _shadow_small_entry_promotion_report(
            db,
            trade_date=trade_date,
            conservative_report=conservative_reason_report,
            themelab_report=gate_reason_report,
        )
        shadow_small_entry_promotion = _shadow_small_entry_promotion_payload(shadow_small_entry_promotion_report)
        shadow_small_entry_ops = _shadow_small_entry_ops(
            db,
            gateway_state=gateway_state,
            trade_date=trade_date,
            promotion_evidence=dict(shadow_small_entry_promotion_report.get("evidence") or {}),
            live_audit_report=live_sim_audit,
        )
        shadow_small_entry_pilot = _shadow_small_entry_pilot(db, gateway_state=gateway_state, trade_date=trade_date)
    else:
        gate_reason_outcomes = _empty_gate_reason_outcomes()
        live_sim_audit = _live_sim_audit_empty()
        conservative_reason_outcomes = conservative_reason_empty_payload()
        shadow_small_entry_promotion = shadow_small_entry_empty_payload()
        shadow_small_entry_ops = _shadow_small_entry_ops_empty()
        shadow_small_entry_pilot = shadow_small_entry_pilot_empty_payload(trade_date)

    payload = {
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
        "market": market,
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
    payload["operator_view"] = _operator_view(payload)
    return payload


def _empty_snapshot(
    *,
    runtime_status: dict[str, Any] | None = None,
    theme_source_sync: dict[str, Any] | None = None,
    db: TradingDatabase | None = None,
    gateway_state: Any | None = None,
    include_extended: bool = True,
) -> dict[str, Any]:
    runtime = _runtime_context(runtime_status)
    freshness = _empty_freshness()
    trade_date = datetime.now().date().isoformat()
    payload = {
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
        "live_sim_audit": _live_sim_audit(db, gateway_state=gateway_state, trade_date=trade_date) if db is not None and include_extended else _live_sim_audit_empty(),
        "conservative_reason_outcomes": _conservative_reason_outcomes(db, trade_date=trade_date) if db is not None and include_extended else conservative_reason_empty_payload(),
        "shadow_small_entry_promotion": _shadow_small_entry_promotion(db, trade_date=trade_date) if db is not None and include_extended else shadow_small_entry_empty_payload(),
        "shadow_small_entry_ops": _shadow_small_entry_ops(db, gateway_state=gateway_state, trade_date=trade_date) if db is not None and include_extended else _shadow_small_entry_ops_empty(),
        "shadow_small_entry_pilot": _shadow_small_entry_pilot(db, gateway_state=gateway_state, trade_date=trade_date) if db is not None and include_extended else shadow_small_entry_pilot_empty_payload(trade_date),
        "shadow_small_entry": _empty_shadow_small_entry(),
        "shadow_small_entry_ab": _empty_shadow_small_entry_ab(),
        "summary": _empty_summary(runtime=runtime, freshness=freshness),
    }
    payload["operator_view"] = _operator_view(payload)
    return payload


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


def _report_cache_extra_key(*reports: dict[str, Any] | None) -> str:
    values = []
    for report in reports:
        if not report:
            values.append("")
            continue
        values.append(str(report.get("report_id") or report.get("generated_at") or ""))
    return "|".join(values)


def _conservative_reason_report(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    source_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _cached_theme_lab_dashboard_report(
        db,
        "conservative_reason_report",
        trade_date=trade_date,
        extra_key=_report_cache_extra_key(source_report),
        builder=lambda: _build_conservative_reason_report(db, trade_date=trade_date, source_report=source_report),
    )


def _build_conservative_reason_report(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    source_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ConservativeReasonOutcomeAnalyzer(db).build_report(
        trade_date=trade_date or None,
        limit=10000,
        source_report=source_report,
    )


def _conservative_reason_outcomes(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    source_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _conservative_reason_outcomes_payload(
            _conservative_reason_report(db, trade_date=trade_date, source_report=source_report)
        )
    except Exception as exc:
        return conservative_reason_empty_payload(str(exc))


def _conservative_reason_outcomes_payload(report: dict[str, Any]) -> dict[str, Any]:
    return conservative_reason_snapshot_payload(report)


def _shadow_small_entry_promotion(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    conservative_report: dict[str, Any] | None = None,
    themelab_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _shadow_small_entry_promotion_payload(
            _shadow_small_entry_promotion_report(
                db,
                trade_date=trade_date,
                conservative_report=conservative_report,
                themelab_report=themelab_report,
            )
        )
    except Exception as exc:
        return shadow_small_entry_empty_payload(str(exc))


def _shadow_small_entry_promotion_report(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    conservative_report: dict[str, Any] | None = None,
    themelab_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _cached_theme_lab_dashboard_report(
        db,
        "shadow_small_entry_promotion_report",
        trade_date=trade_date,
        extra_key=_report_cache_extra_key(conservative_report, themelab_report),
        builder=lambda: _build_shadow_small_entry_promotion_report(
            db,
            trade_date=trade_date,
            conservative_report=conservative_report,
            themelab_report=themelab_report,
        ),
    )


def _build_shadow_small_entry_promotion_report(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    conservative_report: dict[str, Any] | None = None,
    themelab_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ShadowSmallEntryPromotionAnalyzer(db).build_report(
        trade_date=trade_date or None,
        limit=10000,
        include_traces=False,
        conservative_report=conservative_report,
        themelab_report=themelab_report,
    )


def _build_shadow_small_entry_promotion(
    db: TradingDatabase,
    *,
    trade_date: str = "",
    conservative_report: dict[str, Any] | None = None,
    themelab_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _shadow_small_entry_promotion_payload(
            _build_shadow_small_entry_promotion_report(
                db,
                trade_date=trade_date,
                conservative_report=conservative_report,
                themelab_report=themelab_report,
            )
        )
    except Exception as exc:
        return shadow_small_entry_empty_payload(str(exc))


def _shadow_small_entry_promotion_payload(report: dict[str, Any]) -> dict[str, Any]:
    return shadow_small_entry_snapshot_payload(report)


def _ops_report_cache_extra_key(
    gateway_state: Any | None,
    promotion_evidence: dict[str, Any] | None,
    live_audit_report: dict[str, Any] | None,
) -> str:
    return "|".join(
        (
            _gateway_report_cache_key(gateway_state),
            str((promotion_evidence or {}).get("report_id") or (promotion_evidence or {}).get("source_report_trade_date") or ""),
            str((live_audit_report or {}).get("last_updated_at") or (live_audit_report or {}).get("generated_at") or ""),
        )
    )


def _shadow_small_entry_ops(
    db: TradingDatabase,
    *,
    gateway_state: Any | None,
    trade_date: str = "",
    promotion_evidence: dict[str, Any] | None = None,
    live_audit_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _cached_theme_lab_dashboard_report(
        db,
        "shadow_small_entry_ops",
        trade_date=trade_date,
        extra_key=_ops_report_cache_extra_key(gateway_state, promotion_evidence, live_audit_report),
        builder=lambda: _build_shadow_small_entry_ops(
            db,
            gateway_state=gateway_state,
            trade_date=trade_date,
            promotion_evidence=promotion_evidence,
            live_audit_report=live_audit_report,
        ),
    )


def _build_shadow_small_entry_ops(
    db: TradingDatabase,
    *,
    gateway_state: Any | None,
    trade_date: str = "",
    promotion_evidence: dict[str, Any] | None = None,
    live_audit_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return shadow_small_entry_ops_snapshot_payload(
            ShadowSmallEntryOpsService(
                db,
                gateway_state=gateway_state,
                promotion_evidence=promotion_evidence,
                live_audit_report=live_audit_report,
            ).status(trade_date=trade_date or None)
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
    return _cached_theme_lab_dashboard_report(
        db,
        "theme_lab_gate_reason_outcomes",
        trade_date=trade_date,
        builder=lambda: _build_theme_lab_gate_reason_outcomes(db, trade_date=trade_date),
    )


def _build_theme_lab_gate_reason_outcomes(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    return _theme_lab_gate_reason_outcomes_payload(_theme_lab_gate_reason_source_report(db, trade_date=trade_date))


def _theme_lab_gate_reason_source_report(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    return _cached_theme_lab_dashboard_report(
        db,
        "theme_lab_gate_reason_source_report",
        trade_date=trade_date,
        builder=lambda: _build_theme_lab_gate_reason_source_report(db, trade_date=trade_date),
    )


def _build_theme_lab_gate_reason_source_report(db: TradingDatabase, *, trade_date: str = "") -> dict[str, Any]:
    try:
        return ThemeLabGateReasonOutcomeAnalyzer(db).build_report(trade_date=trade_date or None, limit=10000)
    except Exception as exc:
        return {"status": "ERROR", "error": str(exc)}


def _theme_lab_gate_reason_outcomes_payload(report: dict[str, Any]) -> dict[str, Any]:
    if str(report.get("status") or "").upper() == "ERROR":
        empty = _empty_gate_reason_outcomes()
        empty.update({"status": "ERROR", "error": str(report.get("error") or "")})
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


def _market(raw: dict[str, Any], *, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    market = {
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
    return _apply_runtime_market_session(market, runtime)


def _apply_runtime_market_session(market: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
    runtime = runtime or {}
    session = str(runtime.get("market_session_status") or "").strip().lower()
    gate_skip = str(runtime.get("gate_skip_reason") or "").strip().upper()
    if session != "closed" and gate_skip != "MARKET_SESSION_CLOSED":
        return market
    result = dict(market)
    result["market_status"] = "CLOSED"
    result["market_session_status"] = "closed"
    result["gate_skip_reason"] = "MARKET_SESSION_CLOSED"
    flags = list(dict.fromkeys(list(result.get("data_quality_flags") or []) + ["MARKET_SESSION_CLOSED"]))
    result["data_quality_flags"] = flags
    sides = []
    for side in _as_list(result.get("sides")):
        row = dict(side)
        row["status"] = "CLOSED"
        row["breadth_ready"] = False
        row["breadth_gate_usable"] = False
        row["reason_codes"] = list(dict.fromkeys(list(row.get("reason_codes") or []) + ["MARKET_SESSION_CLOSED"]))
        sides.append(row)
    result["sides"] = sides
    return result


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
    ready_like = [item for item in watchset if item.get("gate_status") in {"READY", "READY_SMALL"}]
    live_guard_passed = sum(1 for item in ready_like if _candidate_submittable(item) and item.get("live_order_guard_passed"))
    submittable_ready_count = sum(1 for item in ready_like if _candidate_submittable(item))
    live_guard_blocked = sum(1 for item in ready_like if _candidate_submittable(item) and not item.get("live_order_guard_passed"))
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
        submittable_ready_count=submittable_ready_count,
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
        submittable_ready_count=0,
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
            "market_session_status": "",
            "data_warmup_status": "",
            "gate_skip_reason": "",
        }
    running = bool(runtime_status.get("running"))
    realtime_quality = dict(runtime_status.get("realtime_data_quality") or {})
    latest_snapshot = dict(runtime_status.get("latest_snapshot") or {})
    readiness = dict(runtime_status.get("readiness") or {})
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
        "market_session_status": str(
            latest_snapshot.get("market_session_status") or readiness.get("market_session_status") or ""
        ),
        "data_warmup_status": str(latest_snapshot.get("data_warmup_status") or readiness.get("data_warmup_status") or ""),
        "gate_skip_reason": str(latest_snapshot.get("gate_skip_reason") or readiness.get("gate_skip_reason") or ""),
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
    submittable_ready_count: int = 0,
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
    if str(runtime.get("market_session_status") or "").lower() == "closed" or str(runtime.get("gate_skip_reason") or "").upper() == "MARKET_SESSION_CLOSED":
        return "OBSERVE_ONLY", "장 마감 상태입니다. 실시간 데이터 품질은 진단/리포트용으로만 확인합니다."
    if watchset_size == 0:
        if theme_count <= 0 or data_status == "BROKEN":
            return "SNAPSHOT_UNAVAILABLE", "ThemeLabFlow 결과 대기 중입니다."
        if data_status == "DEGRADED" or theme_data_not_ready_count > 0:
            return "WAIT_DATA_QUALITY", "테마 결과는 있으나 지수/현재가 데이터 워밍업 중입니다."
        return "OBSERVE_ONLY", "ThemeLabFlow 결과는 있으나 WatchSet 조건을 통과한 종목이 없습니다."
    if ready_count > 0 and live_guard_passed_count > 0 and data_status not in {"DEGRADED", "BROKEN"}:
        return "READY_TO_TRADE", "READY 후보가 있고 데이터 품질이 정상입니다."
    if ready_like > 0 and submittable_ready_count <= 0:
        return "ENTRY_PLAN_DIAGNOSTIC_ONLY", "READY 후보는 있으나 진입 계획이 관측/진단 전용입니다."
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


def _operator_term(code: Any) -> dict[str, str]:
    key = _value(code).upper()
    if key in OPERATOR_TERM_DICTIONARY:
        return dict(OPERATOR_TERM_DICTIONARY[key])
    label = key.replace("_", " ") if key else "UNKNOWN"
    return {
        "label_ko": label,
        "short_label_ko": label,
        "description_ko": "상세 탭에서 원문 상태를 확인하세요.",
        "severity": "muted",
        "operator_action_ko": "필요하면 개발자 상세 탭에서 원문 reason_code를 확인하세요.",
    }


def _operator_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary = dict(snapshot.get("summary") or {})
    market = dict(snapshot.get("market") or {})
    data_quality = dict(snapshot.get("data_quality") or {})
    gateway = dict(snapshot.get("gateway") or {})
    ranked_themes = _as_list(snapshot.get("ranked_themes"))
    watchset = _as_list(snapshot.get("watchset"))
    entry_statuses = {"READY", "READY_SMALL", "EARLY_SMALL", "READY_EARLY_SMALL", "READY_SHADOW_SMALL_ENTRY"}
    entry_candidates = [
        item
        for item in watchset
        if str(item.get("gate_status") or item.get("display_status") or "") in entry_statuses
        or str(item.get("display_status") or "") in entry_statuses
    ]
    no_buy_reasons = _operator_no_buy_reasons(summary, data_quality, watchset, snapshot)
    risk_status = _operator_risk_status(summary, data_quality, gateway, snapshot)
    main_action = _operator_main_action(summary, market, data_quality, entry_candidates, no_buy_reasons, risk_status)
    return {
        "main_action": main_action,
        "market": _operator_market_view(market),
        "operating_status": _operator_operating_status(summary, market, data_quality, gateway, snapshot),
        "top_themes": [_operator_theme_row(item) for item in ranked_themes[:5]],
        "buy_candidates": [_operator_candidate_row(item, index) for index, item in enumerate(entry_candidates[:10], start=1)],
        "no_buy_reasons": no_buy_reasons[:3],
        "risk_status": risk_status[:5],
        "panels": _operator_panels(snapshot),
        "tabs": _operator_tabs(snapshot, no_buy_reasons=no_buy_reasons, risk_status=risk_status),
        "developer": {"raw_available": True},
        "limits": {
            "top_themes": 5,
            "buy_candidates": 10,
            "no_buy_reasons": 3,
            "risk_status": 5,
        },
    }


def _operator_main_action(
    summary: dict[str, Any],
    market: dict[str, Any],
    data_quality: dict[str, Any],
    entry_candidates: list[dict[str, Any]],
    no_buy_reasons: list[dict[str, Any]],
    risk_status: list[dict[str, Any]],
) -> dict[str, Any]:
    market_status = _value(market.get("market_status")).upper()
    critical_risk = next((item for item in risk_status if item.get("severity") == "danger"), None)
    actionable_candidates = [item for item in entry_candidates if _candidate_order_permission_code(item) == "READY"]
    blocked_candidates = [item for item in entry_candidates if _candidate_order_permission_code(item) == "LIVE_SIM_BLOCKED"]
    diagnostic_candidates = [
        item for item in entry_candidates if _candidate_order_permission_code(item) == "ENTRY_PLAN_DIAGNOSTIC_ONLY"
    ]
    if market_status in {"CLOSED", "MARKET_CLOSED", "AFTER_MARKET", "POST_MARKET", "DONE"}:
        status = "OBSERVE_ONLY"
        message = "정규장이 종료되었습니다. 리포트 탭에서 장후 리뷰를 확인하세요."
    elif critical_risk and critical_risk.get("code") in {"LIVE_SIM_BLOCKED", "RECONCILE_REQUIRED", "UNKNOWN_SUBMIT"}:
        status = critical_risk["code"]
        message = "주문/체결 상태 확인이 필요해 신규 진입을 막고 있습니다."
    elif str(data_quality.get("status") or "").upper() in {"BROKEN", "DEGRADED"}:
        status = "WAIT_DATA_QUALITY"
        message = "실시간 데이터가 준비 중입니다. 분봉/VWAP/지지선 수집을 기다립니다."
    elif actionable_candidates:
        status = "READY_TO_TRADE"
        message = "지금 매수 가능 후보가 있습니다. 후보 목록을 확인하세요."
    elif blocked_candidates:
        status = "READY_BUT_LIVE_BLOCKED"
        message = "매수 후보는 있지만 주문 안전장치가 차단 중입니다. 주문/리스크 상태를 확인하세요."
    elif diagnostic_candidates:
        status = "ENTRY_PLAN_DIAGNOSTIC_ONLY"
        message = "매수 후보는 있지만 진입 계획이 관측/진단 전용이라 주문은 나가지 않았습니다."
    elif no_buy_reasons:
        status = "NO_SIGNAL"
        message = f"현재 매수 가능 후보가 없습니다. 가장 큰 이유는 {no_buy_reasons[0]['label_ko']}입니다."
    else:
        status = str(summary.get("operation_status") or "OBSERVE_ONLY")
        message = str(summary.get("operation_message_ko") or "현재는 관측 전용입니다. 주문은 나가지 않습니다.")
    term = _operator_term(status)
    return {
        "status": status,
        "label_ko": term["label_ko"],
        "message_ko": message,
        "severity": term["severity"],
        "operator_action_ko": term["operator_action_ko"],
    }


def _operator_market_view(market: dict[str, Any]) -> dict[str, Any]:
    status = str(market.get("market_status") or "UNKNOWN")
    sides = _as_list(market.get("sides"))
    side_labels = []
    for side in sides:
        side_labels.append(f"{side.get('side') or '-'} {side.get('status') or 'UNKNOWN'}")
    return {
        "status": status,
        "label_ko": _market_status_label(status),
        "message_ko": " / ".join(side_labels) if side_labels else "시장 상태 확인 중입니다.",
        "kospi": _operator_market_side(sides, "KOSPI", market.get("kospi_return_pct")),
        "kosdaq": _operator_market_side(sides, "KOSDAQ", market.get("kosdaq_return_pct")),
    }


def _operator_market_side(sides: list[dict[str, Any]], side: str, fallback_return: Any) -> dict[str, Any]:
    item = next((row for row in sides if str(row.get("side") or "").upper() == side), {})
    return {
        "side": side,
        "status": item.get("status") or "UNKNOWN",
        "return_pct": _first_not_none(item.get("index_return_pct"), fallback_return),
        "breadth_pct": item.get("breadth_pct"),
        "breadth_ready": bool(item.get("breadth_ready")),
        "breadth_trust_level": item.get("breadth_trust_level") or "UNKNOWN",
    }


def _operator_operating_status(
    summary: dict[str, Any],
    market: dict[str, Any],
    data_quality: dict[str, Any],
    gateway: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    live_enabled = bool(summary.get("live_order_enabled"))
    live_guard_blocked = int(summary.get("live_guard_blocked_count") or 0)
    operation_status = str(summary.get("operation_status") or "OBSERVE_ONLY")
    return [
        _operator_status_item(
            "market_session",
            "장 상태",
            _market_session_code(str(market.get("market_status") or "")),
            _market_session_label(str(market.get("market_status") or "")),
            _market_status_message(str(market.get("market_status") or "")),
        ),
        _operator_status_item(
            "kiwoom",
            "Kiwoom 연결",
            "OK" if gateway.get("connected") and gateway.get("heartbeat_ok") and gateway.get("kiwoom_logged_in") else "WARNING",
            "정상" if gateway.get("connected") and gateway.get("heartbeat_ok") and gateway.get("kiwoom_logged_in") else "확인 필요",
            "키움 연결과 heartbeat가 정상입니다." if gateway.get("connected") and gateway.get("heartbeat_ok") and gateway.get("kiwoom_logged_in") else "키움 연결, 로그인, heartbeat를 확인하세요.",
        ),
        _operator_status_item(
            "data",
            "데이터 상태",
            str(data_quality.get("status") or "UNKNOWN"),
            _data_status_label(str(data_quality.get("status") or "UNKNOWN")),
            str(data_quality.get("message") or "데이터 상태 확인 중입니다."),
        ),
        _operator_status_item(
            "orders",
            "주문 상태",
            "LIVE_SIM_BLOCKED" if live_guard_blocked else "READY" if live_enabled else "ORDER_SINK_NOOP",
            "중단됨" if live_guard_blocked else "LIVE_SIM 가능" if live_enabled else "관측전용",
            "LIVE Guard 차단 후보가 있습니다." if live_guard_blocked else "LIVE_SIM 주문 경로를 사용할 수 있습니다." if live_enabled else "지금은 관측 전용입니다. 주문은 나가지 않습니다.",
        ),
        _operator_status_item(
            "auto_trading",
            "오늘 자동매매 상태",
            operation_status,
            _operator_term(operation_status)["label_ko"],
            str(summary.get("operation_message_ko") or "운영 상태 확인 중입니다."),
        ),
    ]


def _operator_status_item(key: str, title: str, code: str, label: str, message: str) -> dict[str, str]:
    term = _operator_term(code)
    return {
        "key": key,
        "title_ko": title,
        "status": code,
        "label_ko": label,
        "severity": term["severity"],
        "message_ko": message,
    }


def _operator_theme_row(item: dict[str, Any]) -> dict[str, Any]:
    status = _theme_status_bucket(item.get("theme_status"))
    leader = item.get("top_leader_name") or item.get("top_leader_symbol") or "-"
    breadth = _theme_breadth_pct(item)
    co_leaders = _theme_member_names(item, role_key="CO_LEADER")[:3]
    return {
        "rank": int(item.get("rank") or 0),
        "theme_id": item.get("theme_id") or "",
        "theme_name": item.get("theme_name") or "-",
        "score": item.get("condition_score") or item.get("theme_score") or 0,
        "breadth_pct": breadth,
        "leader": leader,
        "co_leaders": co_leaders,
        "status": status,
        "label_ko": _theme_status_label(status),
        "severity": _theme_status_severity(status),
        "operator_message_ko": _theme_operator_message(item, status, breadth, leader),
    }


def _operator_candidate_row(item: dict[str, Any], priority: int) -> dict[str, Any]:
    gate = str(item.get("gate_status") or item.get("display_status") or "OBSERVE")
    display = str(item.get("display_status") or gate)
    location = str(item.get("price_location_status") or item.get("price_location") or "UNKNOWN")
    order_code = _candidate_order_permission_code(item)
    return {
        "priority": priority,
        "symbol": item.get("symbol") or "",
        "code": item.get("code") or item.get("symbol") or "",
        "stock_name": item.get("stock_name") or item.get("name") or "-",
        "theme_name": item.get("theme_name") or item.get("primary_theme") or "-",
        "gate_status": gate,
        "status_label_ko": _operator_term(gate)["label_ko"],
        "display_status": display,
        "display_label_ko": _operator_term(display)["label_ko"],
        "role": item.get("stock_role") or "UNKNOWN",
        "role_label_ko": _role_label(item.get("stock_role")),
        "entry_position": location,
        "entry_position_label_ko": _entry_position_label(location, display),
        "order_permission": order_code,
        "order_permission_label_ko": _order_permission_label(order_code),
        "severity": _operator_term(order_code if order_code != "READY" else gate)["severity"],
        "operator_message_ko": item.get("operator_message_ko") or _candidate_operator_message(item, location, order_code),
    }


def _operator_no_buy_reasons(
    summary: dict[str, Any],
    data_quality: dict[str, Any],
    watchset: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    reasons: Counter[str] = Counter()
    data_status = str(data_quality.get("status") or "").upper()
    if data_status in {"BROKEN", "DEGRADED"}:
        reasons["DATA_INSUFFICIENT"] += max(1, int(data_quality.get("candle_missing_count") or 0))
    if int(summary.get("market_pending_count") or 0) or int(summary.get("market_confirmation_pending_count") or 0):
        reasons["WAIT_MARKET_CONFIRMATION_PENDING"] += int(summary.get("market_pending_count") or 1)
    if int(summary.get("live_guard_blocked_count") or 0):
        reasons["LIVE_SIM_BLOCKED"] += int(summary.get("live_guard_blocked_count") or 0)
    if int(summary.get("late_chase_wait_count") or 0) or int(summary.get("chase_risk_blocked_count") or 0):
        reasons["LATE_CHASE"] += int(summary.get("late_chase_wait_count") or 0) + int(summary.get("chase_risk_blocked_count") or 0)
    for item in watchset:
        display = str(item.get("display_status") or "")
        if display in OPERATOR_TERM_DICTIONARY:
            reasons[display] += 1
        for key in ("blocked_reason_codes", "risk_reason_codes", "price_location_reason_codes", "price_location_readiness_reason_codes", "data_quality_flags", "price_location_data_quality_flags"):
            for reason in item.get(key) or []:
                code = _normalize_operator_reason_code(reason)
                if code:
                    reasons[code] += 1
        if item.get("diagnostic_only"):
            reasons["ENTRY_PLAN_DIAGNOSTIC_ONLY"] += 1
        if item.get("support_ready_reason"):
            reasons["SUPPORT_NOT_READY"] += 1
    live_sim = dict(snapshot.get("live_sim_audit") or {})
    live_summary = dict(live_sim.get("summary") or {})
    if int(live_summary.get("unknown_submit_count") or 0):
        reasons["UNKNOWN_SUBMIT"] += int(live_summary.get("unknown_submit_count") or 0)
    if int(live_summary.get("reconcile_required_order_count") or 0):
        reasons["RECONCILE_REQUIRED"] += int(live_summary.get("reconcile_required_order_count") or 0)
    if not reasons and int(summary.get("order_candidate_count") or 0) <= 0:
        reasons["NO_SIGNAL"] += 1
    rows = []
    for code, count in reasons.most_common():
        term = _operator_term(code)
        rows.append(
            {
                "code": code,
                "label_ko": term["label_ko"],
                "short_label_ko": term["short_label_ko"],
                "count": count,
                "severity": term["severity"],
                "description_ko": term["description_ko"],
                "operator_action_ko": term["operator_action_ko"],
                "operator_message_ko": _no_buy_message(code),
            }
        )
    return rows


def _operator_risk_status(
    summary: dict[str, Any],
    data_quality: dict[str, Any],
    gateway: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    live_sim = dict(snapshot.get("live_sim_audit") or {})
    live_summary = dict(live_sim.get("summary") or {})
    shadow_ops = dict(snapshot.get("shadow_small_entry_ops") or {})
    unknown_submit = int(live_summary.get("unknown_submit_count") or 0)
    reconcile_count = int(live_summary.get("reconcile_required_order_count") or 0)
    open_orders = int(live_summary.get("open_live_sim_order_count") or 0)
    cancel_wait = int(live_summary.get("cancel_requested_stale_count") or 0)
    risk_rows = [
        _risk_item(
            "live_sim_audit",
            "LIVE_SIM audit",
            "UNKNOWN_SUBMIT" if unknown_submit else "RECONCILE_REQUIRED" if reconcile_count else str(live_sim.get("status") or "NO_DATA"),
            "문제 있음" if unknown_submit else "확인 필요" if reconcile_count else "정상" if live_sim.get("status") == "OK" else "확인 필요",
            ((live_sim.get("operator") or {}).get("status_message_ko") or "LIVE_SIM 주문 audit 상태를 확인하세요."),
            count=unknown_submit or reconcile_count,
        ),
        _risk_item(
            "reconcile",
            "Reconcile",
            "RECONCILE_REQUIRED" if reconcile_count else "OK",
            "확인 필요" if reconcile_count else "정상",
            "주문/잔고 재확인이 필요합니다." if reconcile_count else "주문/잔고 재확인 이슈가 없습니다.",
            count=reconcile_count,
        ),
        _risk_item(
            "open_orders",
            "미체결",
            "WARNING" if open_orders or cancel_wait else "OK",
            "취소 대기" if cancel_wait else "있음" if open_orders else "없음",
            "미체결 또는 취소 대기 주문을 확인하세요." if open_orders or cancel_wait else "미체결 주문이 없습니다.",
            count=open_orders or cancel_wait,
        ),
        _risk_item(
            "shadow_small_entry",
            "Shadow Small Entry",
            "READY" if shadow_ops.get("order_enabled") else "ORDER_SINK_NOOP",
            "활성" if shadow_ops.get("order_enabled") else "관측전용",
            shadow_ops.get("operator_message_ko") or "현재는 관측 전용입니다. 주문은 나가지 않습니다.",
        ),
        _risk_item(
            "daily_risk",
            "당일 리스크",
            "WARNING" if int(summary.get("live_guard_blocked_count") or 0) or str(data_quality.get("status") or "").upper() in {"BROKEN", "DEGRADED"} else "OK",
            "확인 필요" if int(summary.get("live_guard_blocked_count") or 0) or str(data_quality.get("status") or "").upper() in {"BROKEN", "DEGRADED"} else "정상",
            "데이터 품질 또는 주문 Guard 차단 상태를 확인하세요." if int(summary.get("live_guard_blocked_count") or 0) or str(data_quality.get("status") or "").upper() in {"BROKEN", "DEGRADED"} else "당일 리스크 상태가 정상입니다.",
        ),
    ]
    if not gateway.get("connected") or not gateway.get("heartbeat_ok"):
        risk_rows.insert(
            0,
            _risk_item(
                "kiwoom_gateway",
                "Kiwoom 연결",
                "WARNING",
                "확인 필요",
                "키움 게이트웨이 연결과 heartbeat를 확인하세요.",
            ),
        )
    return risk_rows


def _risk_item(key: str, title: str, code: str, label: str, message: str, *, count: int = 0) -> dict[str, Any]:
    term = _operator_term(code)
    return {
        "key": key,
        "title_ko": title,
        "code": code,
        "label_ko": label,
        "severity": term["severity"],
        "message_ko": message,
        "count": count,
    }


def _operator_panels(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    live_sim = dict(snapshot.get("live_sim_audit") or {})
    conservative = dict(snapshot.get("conservative_reason_outcomes") or {})
    shadow_ops = dict(snapshot.get("shadow_small_entry_ops") or {})
    shadow_pilot = dict(snapshot.get("shadow_small_entry_pilot") or {})
    shadow_promotion = dict(snapshot.get("shadow_small_entry_promotion") or {})
    gate_outcomes = dict(snapshot.get("gate_reason_outcomes") or {})
    return {
        "buy_zero_rca": {
            "summary_ko": "매수 0건 원인은 안 산 이유 탭에서 요약하고, trace는 개발자 상세에서 확인합니다.",
            "visible_in_main": False,
            "tab": "no-buy",
            "status": gate_outcomes.get("status") or "NO_DATA",
        },
        "live_sim_audit": {
            "summary_ko": ((live_sim.get("operator") or {}).get("status_message_ko") or "LIVE_SIM audit는 주문/리스크 탭에서 확인합니다."),
            "visible_in_main": False,
            "tab": "orders-risk",
            "status": live_sim.get("status") or "NO_DATA",
        },
        "shadow_small_entry": {
            "summary_ko": shadow_ops.get("operator_message_ko") or "소액 진입 실험은 관측/승격/파일럿 상태를 한 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "small-entry",
            "status": shadow_ops.get("status") or "NO_DATA",
        },
        "shadow_small_entry_promotion": {
            "summary_ko": shadow_promotion.get("operator_message_ko") or "소액 승격 후보와 차단 근거는 소액 진입 실험 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "small-entry",
            "status": shadow_promotion.get("status") or "NO_DATA",
        },
        "shadow_small_entry_pilot": {
            "summary_ko": shadow_pilot.get("operator_message_ko") or "파일럿 실행 상태와 안전 체크는 소액 진입 실험 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "small-entry",
            "status": shadow_pilot.get("status") or "NO_DATA",
        },
        "conservative_reason": {
            "summary_ko": "보수적 차단 사유 outcome은 안 산 이유 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "no-buy",
            "status": conservative.get("status") or "NO_DATA",
        },
        "pilot_report": {
            "summary_ko": shadow_pilot.get("operator_message_ko") or "파일럿 리포트 파일은 소액 진입 실험과 리포트 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "small-entry",
            "status": shadow_pilot.get("status") or "NO_DATA",
        },
        "reports": {
            "summary_ko": "장후 outcome report, StrategyChangeProposal, CSV/Markdown export는 리포트 탭에서 확인합니다.",
            "visible_in_main": False,
            "tab": "reports",
            "status": conservative.get("status") or shadow_pilot.get("status") or "NO_DATA",
        },
        "developer_details": {
            "summary_ko": "raw JSON, trace, reason_code 원문은 개발자 상세 탭에서만 표시합니다.",
            "visible_in_main": False,
            "tab": "developer",
            "status": "HIDDEN",
        },
    }


def _operator_tabs(
    snapshot: dict[str, Any],
    *,
    no_buy_reasons: list[dict[str, Any]],
    risk_status: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    shadow_ops = dict(snapshot.get("shadow_small_entry_ops") or {})
    shadow_pilot = dict(snapshot.get("shadow_small_entry_pilot") or {})
    shadow_promotion = dict(snapshot.get("shadow_small_entry_promotion") or {})
    live_sim = dict(snapshot.get("live_sim_audit") or {})
    conservative = dict(snapshot.get("conservative_reason_outcomes") or {})
    buy_zero = dict(snapshot.get("gate_reason_outcomes") or {})
    return {
        "no_buy": {
            "summary_ko": f"안 산 이유 상위 {min(3, len(no_buy_reasons))}개를 요약합니다.",
            "count": len(no_buy_reasons),
        },
        "orders_risk": {
            "summary_ko": "LIVE_SIM audit, reconcile, 미체결/취소, 주문 안전장치를 확인합니다.",
            "count": len(risk_status),
        },
        "small_entry": {
            "summary_ko": shadow_ops.get("operator_message_ko") or "소액 진입 실험은 현재 관측/승격/파일럿 상태를 확인합니다.",
            "count": sum(
                1
                for item in (shadow_ops, shadow_pilot, shadow_promotion)
                if item.get("available") or item.get("status") or item.get("candidate_count")
            ),
        },
        "reports": {
            "summary_ko": "장후 outcome report, 파일럿 export, 전략 변경 제안을 확인합니다.",
            "count": sum(1 for item in (conservative, shadow_pilot, buy_zero) if item.get("available") or item.get("status")),
        },
        "system": {
            "summary_ko": "Core 상태, Gateway 연결, Kiwoom 로그인, Heartbeat(연결 생존 신호), Runtime(전략 실행 루프), DRY_RUN(모의 판단/가상 주문)을 확인합니다.",
            "count": len(risk_status),
        },
        "developer": {
            "summary_ko": "원본 JSON, command id, trace timeline, transport latency 원본, replay/debug 표, 상세 로그는 기본 숨김입니다.",
            "count": 0,
        },
    }


def _normalize_operator_reason_code(reason: Any) -> str:
    text = _value(reason).upper()
    if not text:
        return ""
    aliases = {
        "PRICE_LOCATION_DATA_MISSING": "DATA_INSUFFICIENT",
        "PRICE_LOCATION_CORE_DATA_MISSING": "CORE_BLOCKING",
        "PRICE_LOCATION_WARMUP": "WARMUP_OPTIONAL",
        "PRICE_LOCATION_UNKNOWN": "ENTRY_BLOCKING",
        "WAIT_DATA_SUPPORT_NOT_READY": "SUPPORT_NOT_READY",
        "MISSING_VWAP": "VWAP_MISSING",
        "MISSING_CURRENT_PRICE": "DATA_INSUFFICIENT",
        "MISSING_SESSION_HIGH": "DATA_INSUFFICIENT",
        "MISSING_PREV_CLOSE": "DATA_INSUFFICIENT",
        "STALE_TICK": "LATEST_TICK_STALE",
        "CHASE_RISK_BLOCKED": "LATE_CHASE",
        "LATE_CHASE_TEMP_WAIT": "LATE_CHASE",
    }
    return aliases.get(text, text)


def _theme_breadth_pct(item: dict[str, Any]) -> float | None:
    total = int(item.get("eligible_total_members") or 0)
    alive = int(item.get("alive_count") or item.get("strong_count") or 0)
    if total <= 0:
        ratio_value = item.get("alive_ratio") or item.get("strong_ratio")
        try:
            return round(float(ratio_value) * 100.0, 1)
        except (TypeError, ValueError):
            return None
    return round(alive / total * 100.0, 1)


def _theme_member_names(item: dict[str, Any], *, role_key: str) -> list[str]:
    names = []
    for member in item.get("member_hits") or []:
        if not isinstance(member, dict):
            continue
        role = str(member.get("stock_role") or member.get("role") or "").upper()
        if role == role_key:
            names.append(str(member.get("name") or member.get("symbol") or ""))
    return [name for name in names if name]


def _theme_operator_message(item: dict[str, Any], status: str, breadth: float | None, leader: str) -> str:
    score_value = item.get("condition_score") or item.get("theme_score") or 0
    if status == "LEADING":
        return f"{leader} 중심으로 점수 {float(score_value or 0):.0f}점, 확산도 {breadth if breadth is not None else '-'}%라 주도테마로 봅니다."
    if status == "ACTIVE":
        return f"테마 흐름은 살아 있고 확산도 {breadth if breadth is not None else '-'}%를 확인 중입니다."
    if status == "WATCH":
        return "관심 테마지만 확산 또는 대장주 지속성을 더 확인해야 합니다."
    if status == "WEAK":
        return "테마 강도가 약해 메인 매수 후보로 보기 어렵습니다."
    return "테마 상태를 확인 중입니다."


def _candidate_operator_message(item: dict[str, Any], location: str, order_code: str) -> str:
    if order_code == "LIVE_SIM_BLOCKED":
        return "후보는 있지만 LIVE_SIM 주문 안전장치가 차단 중입니다."
    if str(item.get("gate_status") or "") == "READY_SMALL":
        return "조건은 유효하지만 소액으로만 접근하는 후보입니다."
    if location in {"GOOD_PULLBACK", "PULLBACK_RECLAIM"}:
        return "테마와 가격 위치가 맞아 매수 후보로 볼 수 있습니다."
    if location == "VWAP_RECLAIM":
        return "VWAP 회복이 확인되어 진입 후보로 볼 수 있습니다."
    if "CHASE" in str(item.get("display_status") or "") or item.get("chase_risk"):
        return "테마는 강하지만 종목이 고점 추격 구간이라 매수하지 않습니다."
    return item.get("summary_reason") or "후보 조건을 확인 중입니다."


def _candidate_order_permission_code(item: dict[str, Any]) -> str:
    if item.get("submittable") and item.get("live_order_guard_passed"):
        return "READY"
    if item.get("diagnostic_only") or (str(item.get("gate_status") or "") in {"READY", "READY_SMALL"} and not _candidate_submittable(item)):
        return "ENTRY_PLAN_DIAGNOSTIC_ONLY"
    if str(item.get("gate_status") or "") in {"READY", "READY_SMALL"} and not item.get("live_order_guard_passed"):
        return "LIVE_SIM_BLOCKED"
    return "OBSERVE"


def _candidate_submittable(item: dict[str, Any]) -> bool:
    return bool(item.get("submittable", str(item.get("gate_status") or "") in {"READY", "READY_SMALL"}))


def _no_buy_message(code: str) -> str:
    return {
        "DATA_INSUFFICIENT": "지지선/VWAP 데이터가 아직 부족합니다.",
        "CORE_BLOCKING": "핵심 가격 데이터가 부족해 매수하지 않습니다.",
        "ENTRY_BLOCKING": "진입 판단 데이터가 부족해 기다립니다.",
        "SUPPORT_NOT_READY": "지지선 확인 전이라 매수하지 않습니다.",
        "WAIT_MARKET_CONFIRMATION_PENDING": "시장 상태가 약해 대기 중입니다.",
        "LATE_CHASE": "후보는 강하지만 추격 위험이라 기다립니다.",
        "LIVE_SIM_BLOCKED": "LIVE_SIM 주문 안전장치가 차단 중입니다.",
        "RECONCILE_REQUIRED": "주문/잔고 재확인이 필요해 신규 매수를 중단합니다.",
        "UNKNOWN_SUBMIT": "주문번호 확인이 필요해 신규 매수를 중단합니다.",
        "ENTRY_PLAN_DIAGNOSTIC_ONLY": "지금은 관측 전용입니다. 주문은 나가지 않습니다.",
        "NO_SIGNAL": "현재 매수 가능 후보가 없습니다.",
    }.get(code, _operator_term(code)["description_ko"])


def _role_label(role: Any) -> str:
    return {
        "LEADER": "대장",
        "CO_LEADER": "공동대장",
        "FOLLOWER": "후발",
        "LATE_LAGGARD": "후발",
        "WEAK_MEMBER": "제외",
        "OVERHEATED": "제외",
    }.get(_value(role).upper(), "확인중")


def _entry_position_label(location: str, display: str = "") -> str:
    if "CHASE" in display:
        return "추격 위험"
    return {
        "GOOD_PULLBACK": "좋은 눌림",
        "VWAP_RECLAIM": "VWAP 회복",
        "PULLBACK_RECLAIM": "눌림 후 회복",
        "SUPPORT_RECLAIM": "지지선 회복",
        "FAILED_BREAKOUT": "판단 대기",
        "DEEP_PULLBACK": "판단 대기",
        "UNKNOWN": "판단 대기",
    }.get(location, _operator_term(location)["label_ko"])


def _order_permission_label(code: str) -> str:
    if code == "READY":
        return "가능"
    if code == "LIVE_SIM_BLOCKED":
        return "차단"
    if code == "ENTRY_PLAN_DIAGNOSTIC_ONLY":
        return "관측만"
    return "관측만"


def _theme_status_label(status: str) -> str:
    return {
        "LEADING": "주도",
        "ACTIVE": "확산",
        "WATCH": "관심",
        "WEAK": "약함",
    }.get(status, status or "확인중")


def _theme_status_severity(status: str) -> str:
    return {
        "LEADING": "positive",
        "ACTIVE": "positive",
        "WATCH": "neutral",
        "WEAK": "warning",
    }.get(status, "muted")


def _market_status_label(status: str) -> str:
    text = status.upper()
    if text in {"EXPANSION", "SELECTIVE", "HEALTHY"}:
        return "정상"
    if text in {"CHOPPY", "WEAK", "WAITING"}:
        return "확인 필요"
    if text in {"RISK_OFF", "BROKEN"}:
        return "문제 있음"
    return "확인중"


def _market_session_code(status: str) -> str:
    text = status.upper()
    if text in {"CLOSED", "MARKET_CLOSED", "AFTER_MARKET", "POST_MARKET", "DONE"}:
        return "NO_DATA"
    if text in {"PREOPEN", "BEFORE_MARKET", "WAITING"}:
        return "WAIT"
    return "OK"


def _market_session_label(status: str) -> str:
    text = status.upper()
    if text in {"CLOSED", "MARKET_CLOSED", "AFTER_MARKET", "POST_MARKET", "DONE"}:
        return "장마감"
    if text in {"PREOPEN", "BEFORE_MARKET", "WAITING"}:
        return "장전"
    return "장중"


def _market_status_message(status: str) -> str:
    label = _market_status_label(status)
    return f"시장 상태는 {label}입니다."


def _data_status_label(status: str) -> str:
    text = status.upper()
    if text == "OK":
        return "정상"
    if text == "DIAGNOSTIC":
        return "진단 전용"
    if text in {"WARNING", "DEGRADED"}:
        return "준비중"
    if text == "BROKEN":
        return "문제 있음"
    return "확인중"


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
        current_session_confirmed = bool(command.get("current_session_confirmed", True))
        registered = command_status == "ACKED" and current_session_confirmed
        warning = ""
        if not profile:
            warning = "CONDITION_PROFILE_UNRESOLVED"
        elif command_status == "ACKED" and not current_session_confirmed:
            warning = "CONDITION_SEND_STALE_SESSION"
        elif command_status in {"FAILED", "EXPIRED", "EXPIRED_BEFORE_DISPATCH"}:
            warning = _condition_command_warning(command_status, command_error)
        elif command_status and command_status != "ACKED":
            warning = f"CONDITION_SEND_{command_status}"
        elif profile.last_resolved_index is not None and not command_status:
            warning = "CONDITION_SEND_NOT_CONFIRMED"
        warning_label = _condition_warning_label(warning)
        command_status_label = _condition_command_status_label(command_status, registered=registered)
        if warning == "CONDITION_SEND_STALE_SESSION":
            command_status_label = "등록 확인 필요"
        rows.append(
            {
                "condition_name": profile.condition_name if profile else default_name,
                "purpose": purpose,
                "purpose_label": _CONDITION_PURPOSE_LABELS.get(purpose, purpose),
                "resolved_index": profile.last_resolved_index if profile and profile.last_resolved_index is not None else "UNKNOWN",
                "registered": registered,
                "registered_label": "정상" if registered else "확인 필요",
                "command_status": command_status or "UNKNOWN",
                "command_status_label": command_status_label,
                "screen_no": str(command_payload.get("screen_no") or ""),
                "current_session_confirmed": current_session_confirmed,
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
        result_payload = dict(record.get("result_payload") or {})
        current = latest.get(name)
        candidate = {
            "status": str(record.get("status") or ""),
            "last_error": str(record.get("last_error") or ""),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "payload": payload,
            "result_payload": result_payload,
        }
        candidate["current_session_confirmed"] = _record_matches_session(candidate, current_session)
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
        return False
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
        "CONDITION_SEND_STALE_SESSION": "등록 확인 필요",
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
    elif normalized == "CONDITION_SEND_STALE_SESSION":
        detail = "이전 세션의 ACK는 있지만 현재 게이트웨이 세션에서 다시 등록된 ACK가 확인되지 않았습니다."
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


def _apply_backfill_data_quality(data_quality: dict[str, Any], backfill_runtime: dict[str, Any]) -> dict[str, Any]:
    result = dict(data_quality)
    if not str(backfill_runtime.get("last_success_at") or "").strip():
        return result

    current_after = _optional_int(backfill_runtime.get("missing_price_count_after"))
    prev_after = _optional_int(backfill_runtime.get("missing_prev_close_count_after"))
    adjusted = False
    if current_after is not None:
        current_missing = int(result.get("current_price_missing_count") or 0)
        result["current_price_missing_count"] = min(current_missing, current_after)
        adjusted = adjusted or result["current_price_missing_count"] != current_missing
    if prev_after is not None:
        prev_missing = int(result.get("prev_close_missing_count") or 0)
        result["prev_close_missing_count"] = min(prev_missing, prev_after)
        adjusted = adjusted or result["prev_close_missing_count"] != prev_missing
    if not adjusted:
        return result

    result["backfill_adjusted"] = True
    status = str(result.get("status") or "OK")
    if status != "BROKEN":
        candle_missing = int(result.get("candle_missing_count") or 0)
        quote_stale = int(result.get("quote_stale_count") or 0)
        missing_current_price = int(result.get("current_price_missing_count") or 0)
        missing_prev_close = int(result.get("prev_close_missing_count") or 0)
        missing_vwap = int(result.get("vwap_missing_count") or 0)
        missing_session_high = int(result.get("session_high_missing_count") or 0)
        if candle_missing or quote_stale >= 5:
            status = "DEGRADED"
        elif any([missing_current_price, missing_vwap, missing_session_high, missing_prev_close, quote_stale]):
            status = "WARNING"
        else:
            status = "OK"
        result["status"] = status
        reasons = _data_quality_reasons(
            candle_missing=candle_missing,
            quote_stale=quote_stale,
            missing_current_price=missing_current_price,
            missing_prev_close=missing_prev_close,
            missing_vwap=missing_vwap,
            missing_session_high=missing_session_high,
        )
        result["reasons"] = reasons
        result["message"] = _data_quality_message(status, reasons)
    return result


def _apply_ranked_theme_data_quality(data_quality: dict[str, Any], ranked_themes: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(data_quality)
    if not ranked_themes:
        return result

    status_counts = Counter(str(row.get("theme_quality_status") or "UNKNOWN").upper() for row in ranked_themes)
    blocking_count = int(status_counts.get("BROKEN", 0) + status_counts.get("DEGRADED", 0))
    warning_count = int(status_counts.get("WARNING", 0))
    zero_price_count = sum(
        1
        for row in ranked_themes
        if int(row.get("eligible_total_members") or 0) > 0
        and float(row.get("member_price_coverage_ratio") or 0.0) <= 0.0
    )
    coverage_values = [
        float(row.get("member_price_coverage_ratio") or 0.0)
        for row in ranked_themes
        if int(row.get("eligible_total_members") or 0) > 0
    ]
    min_price_coverage = min(coverage_values) if coverage_values else None

    result["theme_rank_quality_status_counts"] = dict(status_counts)
    result["theme_rank_degraded_count"] = blocking_count
    result["theme_rank_warning_count"] = warning_count
    result["theme_rank_zero_price_coverage_count"] = zero_price_count
    result["theme_rank_min_price_coverage_ratio"] = min_price_coverage

    reasons = list(result.get("reasons") or [])
    if blocking_count:
        reasons.append(
            f"Theme Rank 가격 품질 낮음: DEGRADED/BROKEN {blocking_count}개, 가격 0% {zero_price_count}개"
        )
        if str(result.get("status") or "OK").upper() != "BROKEN":
            result["status"] = "DEGRADED"
    elif warning_count and str(result.get("status") or "OK").upper() == "OK":
        reasons.append(f"Theme Rank 가격 품질 확인 필요: WARNING {warning_count}개")
        result["status"] = "WARNING"
    result["reasons"] = list(dict.fromkeys(reasons))
    result["message"] = _data_quality_message(str(result.get("status") or "OK"), result["reasons"])
    return result


def _apply_market_session_data_quality_display(
    data_quality: dict[str, Any],
    market: dict[str, Any],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    result = dict(data_quality)
    raw_status = str(result.get("status") or "OK").upper()
    result["raw_status"] = raw_status
    result["raw_message"] = result.get("message") or ""
    result.setdefault("display_status", raw_status)
    result.setdefault("display_message", result.get("message") or "")
    result.setdefault("operator_severity", _operator_term(raw_status)["severity"])
    if raw_status == "BROKEN" or not _is_market_session_closed(market, runtime):
        return result

    display_message = "장 마감: 실시간 현재가/틱 공백은 운영 장애가 아니라 진단용으로 표시합니다."
    result["status"] = "DIAGNOSTIC"
    result["message"] = display_message
    result["display_status"] = "DIAGNOSTIC"
    result["display_message"] = display_message
    result["operator_severity"] = "neutral"
    result["session_adjusted"] = True
    result["diagnostic_only"] = True
    result["display_reasons"] = list(
        dict.fromkeys(["MARKET_SESSION_CLOSED", *[str(reason) for reason in result.get("reasons") or []]])
    )
    return result


def _is_market_session_closed(market: dict[str, Any], runtime: dict[str, Any]) -> bool:
    market_status = str(market.get("market_status") or "").upper()
    runtime_session = str(runtime.get("market_session_status") or "").lower()
    gate_skip = str(runtime.get("gate_skip_reason") or "").upper()
    return (
        market_status in {"CLOSED", "MARKET_CLOSED", "AFTER_MARKET", "POST_MARKET", "DONE"}
        or runtime_session == "closed"
        or gate_skip == "MARKET_SESSION_CLOSED"
    )


def _data_quality_display_status(data_quality: dict[str, Any]) -> str:
    return str(data_quality.get("display_status") or data_quality.get("status") or "UNKNOWN")


def _data_quality_display_message(data_quality: dict[str, Any]) -> str:
    return str(data_quality.get("display_message") or data_quality.get("message") or "?곗씠???곹깭 ?뺤씤 以묒엯?덈떎.")


def _data_quality_blocks_operator_attention(data_quality: dict[str, Any]) -> bool:
    display_status = _data_quality_display_status(data_quality).upper()
    if display_status == "DIAGNOSTIC":
        return False
    return str(data_quality.get("status") or "").upper() in {"BROKEN", "DEGRADED"}


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    base.setdefault("backfill_paused_by_regular_session_count", 0)
    base.setdefault("last_success_at", "")
    base.setdefault("last_failure_at", "")
    base.setdefault("last_failure_reason", "")
    base.setdefault("tr_backfill_caused_ready_count", 0)
    base.setdefault("gateway_unhealthy_detail", "")
    base.setdefault("gateway_unhealthy_display", "")
    base.setdefault(
        "load_guard",
        {
            "load_guard_status": "FAIL_CLOSED",
            "paused_backfill": True,
            "pause_reason_codes": ["GATEWAY_STATE_UNAVAILABLE"],
            "operator_message_ko": "Gateway 상태를 확인할 수 없어 backfill을 중단합니다.",
            "affected_services": ["theme_backfill", "non_order_tr"],
        },
    )
    base.setdefault("load_guard_status", dict(base.get("load_guard") or {}).get("load_guard_status", "FAIL_CLOSED"))
    base.setdefault("paused_backfill", bool(dict(base.get("load_guard") or {}).get("paused_backfill", True)))
    base.setdefault("pause_reason_codes", list(dict(base.get("load_guard") or {}).get("pause_reason_codes") or []))
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
        "backfill_paused_by_regular_session_count",
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
            elif status == "SKIPPED_REGULAR_SESSION":
                base["backfill_paused_by_regular_session_count"] = int(base.get("backfill_paused_by_regular_session_count") or 0) + 1
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
    try:
        load_guard = build_runtime_load_guard_snapshot(
            gateway_state,
            raw_theme_lab=raw,
            backfill_summary=base,
        )
    except Exception as exc:
        load_guard = {
            "load_guard_status": "FAIL_CLOSED",
            "paused_backfill": True,
            "pause_reason_codes": ["LOAD_GUARD_EXCEPTION"],
            "operator_message_ko": f"Runtime Load Guard 확인 중 오류가 발생했습니다: {exc}",
            "affected_services": ["theme_backfill", "non_order_tr"],
        }
    base["load_guard"] = load_guard
    base["load_guard_status"] = str(load_guard.get("load_guard_status") or "")
    base["paused_backfill"] = bool(load_guard.get("paused_backfill"))
    base["pause_reason_codes"] = list(load_guard.get("pause_reason_codes") or [])
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
