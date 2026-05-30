# 운영 일일 점검 Runbook

이 문서는 데이터가 충분히 쌓이기 전에도 매일 바로 확인할 수 있는 항목을 정리한다.
목표는 `LIVE` 자동주문을 켜는 것이 아니라, `OBSERVE/DRY_RUN` 수집과 WebSocket pilot이 안정적으로 돌고 있는지 빠르게 확인하는 것이다.

## 핵심 원칙

- 기본 운용은 `OBSERVE` 또는 `DRY_RUN`이다.
- `LIVE` 자동주문은 별도 안전 PR 전까지 차단한다.
- Kiwoom 로그인, heartbeat, command 실패, WebSocket fallback을 먼저 본다.
- 성과 판단 PR은 데이터가 쌓인 뒤 진행한다.

## 빠른 점검 명령

```powershell
python tools/ops_check.py `
  --core-url http://127.0.0.1:8000 `
  --token local-dev-token
```

긴급 알림이 있으면 실패 코드로 종료하려면:

```powershell
python tools/ops_check.py `
  --core-url http://127.0.0.1:8000 `
  --token local-dev-token `
  --fail-on-critical
```

원본 JSON이 필요하면:

```powershell
python tools/ops_check.py --core-url http://127.0.0.1:8000 --json
```

## Dashboard에서 볼 곳

대시보드 상단의 `운영 점검 알림` 카드를 먼저 본다.

- `긴급`: 지금 바로 확인해야 하는 상태
- `주의`: 운용은 가능하지만 원인 확인이 필요한 상태
- `정보`: 정상 방어 동작 또는 나중에 리포트에 반영할 상태
- `데이터 수집`: `OBSERVE/DRY_RUN` 수집을 계속해도 되는지
- `WS Pilot`: WebSocket pilot 관찰을 계속해도 되는지
- `LIVE 자동주문`: 현재 구조에서는 항상 차단

## API

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/ops/alerts"
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/status"
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/transport/status"
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/transport/websocket-pilot/status"
```

## 알림 해석

### Gateway heartbeat 지연

`GATEWAY_HEARTBEAT_STALE` 또는 `GATEWAY_HEARTBEAT_SLOW`가 뜨면:

1. 32bit Gateway 터미널이 살아 있는지 확인한다.
2. 대시보드의 WebSocket fallback/reconnect/session loss를 본다.
3. `/api/gateway/status`의 `heartbeat_age_sec`가 계속 증가하는지 본다.

### Kiwoom 로그인 안 됨

`KIWOOM_NOT_LOGGED_IN`이 뜨면:

1. 32bit Gateway 화면에 Kiwoom 로그인창이 떠 있는지 확인한다.
2. 로그인 완료 후 `/api/gateway/status`에서 `kiwoom_logged_in=true`를 확인한다.
3. 계좌가 비어 있으면 `TRADING_ACCOUNT` 또는 Kiwoom 계좌 조회 상태를 확인한다.

### Command 실패/거부

`COMMAND_FAILED`, `COMMAND_REJECTED`, `COMMAND_STALE_DISPATCHED`가 뜨면:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/commands/history?limit=20"
```

특정 command는:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/commands/<COMMAND_ID>"
```

WebSocket real pilot에서는 `send_order`, `cancel_order`, `modify_order`가 기본 차단된다. 이 차단은 `LIVE enablement` 전까지 정상 방어 동작이다.

### WebSocket pilot fallback/session 문제

`WS_PILOT_FALLBACK`, `WS_PILOT_SESSION_LOSS_COUNT`, `WS_PILOT_DUPLICATE_ACK_COUNT`, `WS_PILOT_UNKNOWN_ACK_COUNT`가 뜨면:

1. `/api/gateway/transport/websocket-pilot/status`를 본다.
2. fallback reason과 reconnect count를 기록한다.
3. soak 결과에 포함해서 WebSocket 전면 전환 판단에 사용한다.

### DRY_RUN 오탐/미탐

`DRY_RUN_FALSE_POSITIVE`, `DRY_RUN_OPPORTUNITY_LOSS`는 즉시 장애가 아니다.
데이터가 충분히 쌓인 뒤 gate/risk threshold A/B 제안 PR에서 사용한다.

## 장중 권장 순서

1. Core/API 실행 확인
2. 32bit Gateway 실행 및 Kiwoom 로그인 확인
3. `python tools/ops_check.py` 실행
4. 대시보드 `운영 점검 알림` 확인
5. WebSocket pilot 상태와 heartbeat 확인
6. 장중에는 `OBSERVE/DRY_RUN` 데이터만 수집
7. 장마감 후 transport/performance report export

## 다음 PR로 넘길 것

- WebSocket 전면 전환 여부 결정
- gate/risk threshold A/B 제안
- Dashboard alerting 고도화
- LIVE order enablement
