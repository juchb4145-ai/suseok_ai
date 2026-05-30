# 대시보드 페이지네이션 Runbook

## 목적

PR-10 대시보드는 기존 FastAPI + HTML + vanilla JS 구조를 유지하면서 운영자가 데이터를 탐색하기 쉽게 페이지네이션, 필터, 행 상세보기를 추가한다.

요약 카드는 계속 `/api/snapshot`과 `/ws/dashboard`에서 받는다. 반면 명령 이력, 전송 지연 샘플, DRY_RUN 주문 의도, 성과 사례처럼 데이터가 계속 쌓이는 표는 각 REST API에서 `limit`과 `offset`으로 필요한 페이지만 가져온다. 그래서 스냅샷이 너무 커지지 않고, 장중에도 대시보드가 가볍게 유지된다.

## 데이터 흐름

요약 경로:

- `/ws/dashboard`
- WebSocket 실패 시 `/api/snapshot` 폴링 fallback
- 상태 카드, 카운터, 경고, 짧은 요약만 갱신

표 조회 경로:

- 각 표가 REST API를 직접 호출
- 표마다 독립적인 필터, 페이지, 자동 새로고침 상태를 유지
- 한 표의 필터 변경이 다른 표를 초기화하지 않음
- 늦게 도착한 응답이 최신 화면을 덮지 않도록 표별 요청 순번과 `AbortController` 사용

## 주요 표

운영 점검 알림:

- API: `GET /api/ops/alerts`
- 위치: 대시보드 상단
- 보는 법: 긴급/주의/정보 알림을 먼저 확인한다. heartbeat 지연, Kiwoom 로그인 실패, command 실패/거부, WebSocket fallback, Runtime 오류를 한곳에서 본다.
- 주의: `LIVE 자동주문`은 별도 안전 PR 전까지 항상 차단으로 표시된다.

전송 지연 샘플:

- API: `GET /api/gateway/transport/latency`
- 필터: `trade_date`, `direction`, `message_type`, `transport_mode`, `experiment_id`, `scenario`, `command_id`, `event_id`
- 상세: `GET /api/gateway/transport/latency/{sample_id}`
- 보는 법: `long_poll_wait_ms`, `gateway_execute_ms`, `rate_limit_wait_ms`, `ack_round_trip_ms`를 비교해서 병목이 네트워크인지, Kiwoom 실행인지, rate limit인지 분리한다.

WebSocket Mock 실험:

- API: `GET /api/gateway/transport/experiments`
- 필터: `experiment_id`, `scenario`
- 상세: `GET /api/gateway/transport/experiments/{experiment_id}`
- 주의: 이 표는 실험 결과를 보는 용도다. 실제 32bit Gateway 전송 방식을 WebSocket으로 켜는 기능은 없다.

DRY_RUN 주문 의도:

- API: `GET /api/runtime/orders/dry-run`
- 필터: `trade_date`, `status`, `code`, `side`, `order_phase`, `candidate_id`, `virtual_position_id`, `exit_decision_id`
- 상세: `GET /api/runtime/orders/dry-run/{intent_id}`
- 보는 법: entry/buy와 exit/sell을 나눠서 보고, `live_would_pass`와 `live_reject_reason`으로 실제 LIVE였다면 막혔을 이유를 확인한다.

DRY_RUN 성과 사례:

- API: `GET /api/runtime/performance/dry-run`
- 필터: `trade_date`, `strategy_name`, `code`, `theme_name`, `side`, `order_phase`, `include_rejected`, `include_duplicates`
- 상세: `GET /api/runtime/performance/dry-run/lifecycles/{lifecycle_id}`
- 보는 법: 종목, 테마, 게이트 사유별로 수익률, 낙폭, 오탐, 미탐, 기회손실을 추적한다.

오탐/미탐 신호:

- API: `GET /api/runtime/performance/dry-run/false-signals`
- 필터: `trade_date`, `type`
- 상세: `lifecycle_id`가 있으면 라이프사이클 상세 API 사용
- 보는 법: `false_positive`, `false_negative`, `opportunity_loss`를 나눠서 게이트 사유와 safety reject 사유를 함께 본다.

게이트웨이 명령 이력:

- API: `GET /api/gateway/commands/history`
- 필터: `status`, `command_type`, `trade_date`, `command_id`, `include_finished`
- 상세: `GET /api/gateway/commands/{command_id}`
- 보는 법: `FAILED`, `EXPIRED`, 오래된 `DISPATCHED` 명령부터 확인하고, 같은 `command_id`로 전송 지연 샘플을 다시 조회한다.

## 표 조작

각 표는 다음 기능을 가진다.

- 필터 적용
- 초기화
- 현재 페이지 새로고침
- 이전 / 다음 페이지
- 페이지 크기: 25, 50, 100, 200
- 자동 새로고침
- 마지막 조회 시각 표시
- 약 30초 이상 지나면 오래된 데이터 표시
- 로딩, 빈 결과, 오류 행 표시

행을 클릭하면 오른쪽 상세 패널이 열린다. 상세 패널에는 핵심 식별자와 상태를 먼저 보여주고, 아래에는 원본 JSON을 접고 펼칠 수 있게 둔다. 문제가 애매할 때는 이 원본 JSON을 기준으로 API 응답을 그대로 확인한다.

## 보호된 작업

일부 표에는 리포트 재생성이나 export 작업이 있다.

- 전송 지연 리포트 재생성/export
- WebSocket Mock 비교 리포트 재생성/export
- DRY_RUN 성과 리포트 재생성/export

이 작업들은 token 보호 API를 호출한다. 대시보드는 `TRADING_CORE_TOKEN`을 입력받아 `localStorage`에 저장하지만, 토큰을 프론트엔드 코드에 하드코딩하지 않는다. 공용 PC나 원격 접속 환경에서는 브라우저 저장소를 정리해야 한다.

## 운영 확인 순서

게이트웨이 지연:

1. 게이트웨이 전송 상태 카드를 본다.
2. 전송 지연 샘플 표를 연다.
3. `command_id` 또는 `direction=core_to_gateway`로 필터링한다.
4. `long_poll_wait_ms`, `gateway_execute_ms`, `rate_limit_wait_ms`, `ack_round_trip_ms`를 비교한다.
5. 롱폴 대기가 병목이면 실제 WebSocket 전환 전에 WebSocket Mock 실험 결과를 먼저 확인한다.

DRY_RUN 성과:

1. DRY_RUN 성과 분석 요약을 본다.
2. DRY_RUN 성과 사례 표를 연다.
3. `code`, `theme_name`, `strategy_name`으로 필터링한다.
4. 라이프사이클 상세를 열어 연결된 주문 의도, 가상 포지션, 리뷰 정보를 확인한다.

오탐/미탐:

1. 오탐/미탐 신호 표를 연다.
2. `false_positive`, `false_negative`, `opportunity_loss` 중 하나로 필터링한다.
3. 라이프사이클 상세를 연다.
4. 게이트 사유, LIVE safety 거부 사유, 수익률/낙폭 지표를 같이 본다.

명령 실패:

1. 게이트웨이 명령 이력 표를 연다.
2. `status=FAILED` 또는 `command_type`으로 필터링한다.
3. 명령 상세 패널을 연다.
4. 명령 이벤트 타임라인과 같은 `command_id`의 전송 지연 샘플을 함께 확인한다.

## 경계

PR-10 대시보드는 다음을 하지 않는다.

- LIVE 자동주문 활성화
- 주문 실행 버튼 추가
- Gateway 전송 방식을 WebSocket으로 전환
- raw tick 전체 렌더링
- React/Vite 같은 대형 프론트엔드 의존성 도입
