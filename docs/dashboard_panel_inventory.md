# ThemeLab Dashboard Panel Inventory

이 문서는 ThemeLab 대시보드의 패널을 운영자 의사결정 화면 기준으로 분류한다. 매수/매도 로직, gate threshold, risk threshold, LIVE_SIM/LIVE_REAL guard, DB schema는 이 분류와 무관하며 변경 대상이 아니다.

## Classification

| Panel / Section | Category | Default Surface | Notes |
| --- | --- | --- | --- |
| Main action summary | MAIN | `tab-main` | 오늘 한 줄 운영 메시지와 다음 확인 액션. |
| 오늘 운영 상태 | MAIN | `tab-main` | 장 상태, Kiwoom 연결, 데이터 상태, 주문 모드, 신규 매수 가능 여부. |
| 지금 주도테마 | MAIN | `tab-main` | 상위 5개만 표시하고 전체 순위는 테마 상세 탭. |
| 지금 매수 후보 | MAIN | `tab-main` | READY/READY_SMALL/READY_EARLY_SMALL/READY_SHADOW_SMALL_ENTRY 상위 10개만 표시. |
| 왜 안 사고 있나 | MAIN | `tab-main` | 상위 사유 3개만 표시하고 상세는 안 산 이유 탭. |
| 주문/리스크 안전 | MAIN | `tab-main` | LIVE_SIM, reconcile, 미체결, Shadow Small Entry, 당일 리스크 중 중요 5개. |
| 운용 Cockpit | DEBUG | `tab-developer` | 기존 운영/개발 혼합 cockpit. 핵심 요약은 메인 운영 상태로 대체. |
| 시장 상태 | MAIN | `tab-main` / `tab-themes` | 메인에는 상태 요약, 상세 지표는 기존 cockpit/테마 상세. |
| 테마 상태 | DETAIL | `tab-themes` | 테마 순위, 구성 종목, 대장/공동대장/후발. |
| 후보 상태 | DETAIL | `tab-candidates` | WatchSet 전체와 필터. |
| 주문 후보 | DETAIL | `tab-candidates` | Entry candidate 원장성 상세. |
| 데이터 품질 | DETAIL | `tab-no-buy` / `tab-developer` | 운영 요약은 메인, 원시 counter는 개발자 상세. |
| LIVE 준비상태 | MAIN | `tab-main` / `tab-orders-risk` | 메인에는 준비 여부, 상세는 주문/리스크. |
| Buy-Zero RCA | DETAIL | `tab-no-buy` | 메인 top-level 제거. 매수 0건 상세 원인 분석. |
| READY인데 주문 안 나간 종목 | DETAIL | `tab-no-buy` | Buy-Zero RCA 하위 상세. |
| OBSERVE/BLOCKED 이후 급등 후보 | DETAIL | `tab-no-buy` | 놓친 기회 상세. |
| Trace Drilldown | DEBUG | `tab-developer` | reason_code, stage, raw trace는 기본 숨김. |
| LIVE_SIM lifecycle audit | DETAIL | `tab-orders-risk` | 주문/리스크 탭으로 통합. |
| Reconcile issue | DETAIL | `tab-orders-risk` | 주문/잔고 재확인 상세. |
| 미체결/취소/주문번호 누락 | DETAIL | `tab-orders-risk` | LIVE_SIM audit와 함께 표시. |
| account/exit/kill switch/duplicate guard | DETAIL | `tab-orders-risk` | 주문 안전장치 상세. |
| Conservative Reason Outcome | REPORT | `tab-no-buy` / `tab-reports` | no-buy 요약은 안 산 이유, 장후 검증은 리포트. |
| DATA_INSUFFICIENT taxonomy | DETAIL | `tab-no-buy` | 데이터 부족 분류. |
| Shadow Small Entry Promotion | DETAIL | `tab-small-entry` | 소액 승격 후보와 차단 근거. |
| Shadow Small Entry Ops | DETAIL | `tab-small-entry` | Preflight/Arm/Confirm/Pause/Rollback 제어. |
| Shadow Small Entry Pilot | DETAIL / REPORT | `tab-small-entry` | 파일럿 상태와 안전 체크. export 산출물은 리포트 탭 문맥에서도 참조. |
| Shadow A/B | REPORT | `tab-reports` | Gate reason outcome replay. |
| Promotion Decision Cockpit | REPORT | `tab-reports` | 승격 판단 리포트/검토. |
| Post-market Review | REPORT | `tab-reports` | 장후 리뷰. |
| StrategyChangeProposal | REPORT | `tab-reports` | 전략 변경 제안과 export 링크 위치. |
| Operator Alert Inbox | DEBUG | `tab-developer` | 전체 timeline/ack 원장. |
| Event Journal | DEBUG | `tab-developer` | full timeline. |
| Operator Action Center | DEBUG | `tab-developer` | command/action id와 runbook 연결. |
| Runbook | DEBUG | `tab-developer` | 운영 조치 상세. |
| raw JSON/debug panel | DEBUG | `tab-developer` | 기본 숨김, 원문 확인 전용. |

## Main Screen Contract

메인 화면은 다음 5개 블록만 기본 노출한다.

1. 오늘 운영 상태
2. 지금 주도테마
3. 지금 매수 후보
4. 왜 안 사고 있나
5. 주문/리스크 안전 상태

메인 화면 제한은 상위 테마 5개, 매수 후보 10개, 안 산 이유 3개, 주문/리스크 알림 5개다. `reason_code`, raw JSON, trace timeline, command id는 기본 숨김이며 관련 상세 탭에서만 노출한다.
