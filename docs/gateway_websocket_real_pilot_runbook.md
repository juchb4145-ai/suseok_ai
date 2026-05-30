# Gateway WebSocket Real Pilot Runbook

## 목적

PR-11은 실제 32bit Kiwoom Gateway에서 WebSocket transport를 제한적으로 파일럿할 수 있게 한다. 기본 운영 transport는 계속 REST event ingest + command long-poll이다. WebSocket real pilot은 지연시간과 재연결 안정성을 검증하기 위한 단계이며, 실제 WebSocket 전면 전환이나 LIVE 자동주문 PR이 아니다.

## Mock 실험과 Real Pilot 차이

Mock WebSocket 실험:

- `apps/mock_websocket_gateway.py` 사용
- Kiwoom OpenAPI+/QAxWidget 사용 안 함
- REST와 WebSocket mock을 같은 시나리오로 비교
- 실제 Gateway 전환 전 latency 근거를 만드는 단계

Real Gateway WebSocket pilot:

- `apps/kiwoom_gateway.py --transport websocket-pilot` 사용
- 실제 32bit Kiwoom Gateway 프로세스에서 실행
- WebSocket으로 event/command/ack를 주고받지만 기존 command queue, dedupe, persistence, ack/fail handler를 그대로 사용
- 기본적으로 `send_order`, `cancel_order`, `modify_order`는 차단

## 실행 전 조건

- Core API가 먼저 실행 중이어야 한다.
- `/ws/gateway/transport` token이 `TRADING_CORE_TOKEN`과 맞아야 한다.
- 32bit Python 환경에 `websockets`가 설치되어 있어야 한다.
- 파일럿 중에도 LIVE 자동주문은 켜지 않는다.

## 환경변수

필수:

- `TRADING_GATEWAY_WEBSOCKET_REAL_PILOT=1`
- `TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL=1`
- `TRADING_GATEWAY_TRANSPORT=websocket-pilot`
- `TRADING_GATEWAY_WS_URL=ws://127.0.0.1:8000/ws/gateway/transport`

fallback:

- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST=1` 기본값
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_ERRORS=3`
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_RECONNECTS=5`
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_ON_AUTH_FAILURE=1`
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_ON_SESSION_LOSS=1`

command policy:

- `TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOWED_COMMANDS=login,load_conditions,send_condition,register_realtime,remove_realtime,tr_request`
- `TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS=1` 기본값
- `TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOW_ORDER_COMMANDS=0` 기본값

## 실행 예시

```powershell
$env:TRADING_GATEWAY_WEBSOCKET_REAL_PILOT = "1"
$env:TRADING_GATEWAY_WEBSOCKET_ALLOW_REAL = "1"
$env:TRADING_GATEWAY_TRANSPORT = "websocket-pilot"
$env:TRADING_GATEWAY_WS_URL = "ws://127.0.0.1:8000/ws/gateway/transport"
$env:TRADING_GATEWAY_WEBSOCKET_FALLBACK_TO_REST = "1"

py -3.9-32 apps/kiwoom_gateway.py `
  --core-url http://127.0.0.1:8000 `
  --token change-me-local-token `
  --transport websocket-pilot `
  --ws-url ws://127.0.0.1:8000/ws/gateway/transport
```

feature flag가 없으면 `websocket-pilot`은 REST로 fallback하거나, fallback이 꺼져 있으면 안전하게 실행을 거부한다.

## 허용/차단 command

기본 허용:

- `login`
- `load_conditions`
- `send_condition`
- `register_realtime`
- `remove_realtime`
- `tr_request`

기본 차단:

- `send_order`
- `cancel_order`
- `modify_order`

주문 계열 command가 WebSocket pilot으로 들어오면 Gateway는 Kiwoom 주문 메서드를 호출하지 않고 `command_ack`를 `REJECTED`로 보낸다.

```text
reason=WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED
```

이 정책은 LIVE order enablement PR 전까지 유지한다.

## Reconnect / Session Loss / Duplicate Ack

- WebSocket receive loop는 Gateway 내부 background thread에서 돌며 Kiwoom COM event loop를 막지 않는다.
- 연결마다 `ws_session_id`, `ws_connection_id`, sequence를 남긴다.
- reconnect count와 fallback reason은 heartbeat payload와 transport metrics에 포함된다.
- duplicate ack는 command id 기준으로 idempotent하게 처리하고 warning event로 남긴다.
- unknown ack는 Core/Gateway를 죽이지 않고 warning으로 기록한다.
- reconnect 후 기존 `DISPATCHED` order command를 임의로 재전송하지 않는다.

## Fallback 정책

다음 상황에서는 REST fallback 또는 degraded stop이 발생한다.

- 인증 실패
- 반복 연결 실패
- reconnect 한도 초과
- session loss
- command in-flight 중 send failure

fallback 이후 heartbeat에는 다음 값이 포함된다.

- `original_transport=websocket_real_pilot`
- `transport_mode=rest_long_poll_fallback`
- `ws_fallback_state`
- `ws_fallback_reason`

## Soak Test

Core 상태를 관찰하는 soak 도구:

```powershell
python tools/websocket_real_pilot_soak.py `
  --core-url http://127.0.0.1:8000 `
  --token change-me-local-token `
  --duration-sec 3600 `
  --interval-sec 30 `
  --transport-mode websocket_real_pilot `
  --fail-on-duplicate-ack `
  --fail-on-session-loss `
  --max-reconnect-count 3
```

이 도구는 KiwoomClient를 import하지 않는다. Core API에서 WebSocket pilot 상태와 latency summary를 주기적으로 읽고 PASS/FAIL을 출력한다.

## 확인 API

- `GET /api/gateway/transport/websocket-pilot/status`
- `GET /api/gateway/transport/status`
- `GET /api/gateway/transport/latency?transport_mode=websocket_real_pilot`
- `GET /api/gateway/transport/latency/summary?transport_mode=websocket_real_pilot`
- `GET /api/gateway/transport/websocket-decision`

Dashboard `/`의 게이트웨이 전송 상태 카드에서도 real pilot 연결 상태, session id, reconnect, session loss, duplicate ack, unknown ack, 차단 주문 수를 확인할 수 있다.

## 성공 기준

실제 WebSocket 전환 PR로 넘어가기 전 최소 조건:

- 장시간 soak test PASS
- duplicate ack 0
- unknown ack 0 또는 원인 설명 가능
- session loss 0 또는 fallback 정상 확인
- reconnect count가 기준 이하
- `send_order/cancel_order/modify_order`가 계속 차단됨
- REST 대비 WebSocket real pilot command/ack p95 개선이 확인됨
- rate limit 또는 Kiwoom execution 병목이 아닌 transport 병목임이 리포트로 확인됨

## 장애 시 확인 순서

1. `/api/gateway/transport/websocket-pilot/status`에서 state와 fallback reason 확인
2. `/api/gateway/transport/latency?transport_mode=websocket_real_pilot`에서 오류 sample 확인
3. 같은 `command_id`로 `/api/gateway/commands/{command_id}` 확인
4. session loss 또는 duplicate ack가 있으면 WebSocket 전환을 중단하고 REST 기본 경로로 복귀
5. Gateway 재시작 후에도 DISPATCHED order command를 수동으로 재전송하지 않음
