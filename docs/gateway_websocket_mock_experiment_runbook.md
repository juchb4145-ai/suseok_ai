# Gateway WebSocket Mock Experiment Runbook

## Purpose

PR-9 adds a mock-only Gateway WebSocket transport experiment. It does not switch the real 32bit Kiwoom Gateway to WebSocket.

The goal is to compare:

- REST event ingest + command long-poll
- mock Gateway WebSocket transport

Both paths use the same Core command queue, dedupe ledger, command persistence, and ack/fail handlers.

## Transport Boundaries

Dashboard WebSocket:

- Endpoint: `/ws/dashboard`
- Purpose: browser dashboard snapshots only

Gateway transport WebSocket:

- Endpoint: `/ws/gateway/transport`
- Purpose: mock Gateway event/command/ack transport experiment
- Production default: off
- Real Kiwoom Gateway default: REST long-poll

## WebSocket Message Envelope

`GatewayWsMessage` lives in `trading/broker/ws_messages.py`.

Fields:

- `type`
- `message_id`
- `trace_id`
- `timestamp`
- `source`
- `payload`
- `metadata`
- optional `command_id`
- optional `event_id`
- optional `sequence`

Message types:

- `hello`
- `hello_ack`
- `heartbeat`
- `ping`
- `pong`
- `gateway_event`
- `ready_for_commands`
- `core_command_batch`
- `command_started`
- `command_ack`
- `command_failed`
- `rate_limited`
- `transport_error`

The envelope is transport-level only. Payloads still use existing `GatewayEvent` and `GatewayCommand` schemas.

## Core Endpoint Flow

1. Mock Gateway connects to `/ws/gateway/transport?token=<TOKEN>`.
2. Core returns `hello_ack`.
3. Mock Gateway sends `gateway_event` or `heartbeat`.
4. Core converts it to `GatewayEvent` and calls the same ingest path used by REST.
5. Mock Gateway sends `ready_for_commands`.
6. Core calls `gateway_state.dispatch_commands()`.
7. Core sends `core_command_batch`.
8. Mock Gateway sends `command_started`.
9. Mock Gateway sends `command_ack` or `command_failed`.
10. Core uses the existing `_persist_gateway_event` and `_handle_command_ack` logic.

Sending a command over WebSocket does not mark it `ACKED`. Only `command_ack status=ACKED` does.

## Mock Gateway

Run:

```powershell
python apps/mock_websocket_gateway.py `
  --ws-url ws://127.0.0.1:8000/ws/gateway/transport `
  --token $env:TRADING_CORE_TOKEN `
  --scenario command-heavy `
  --duration-sec 60 `
  --command-delay-ms 20 `
  --experiment-id exp-001
```

Scenarios:

- `basic`
- `burst`
- `command-heavy`
- `event-heavy`
- `reconnect`
- `ack-failure`

The mock gateway does not import Kiwoom, PyQt, QAxWidget, or KiwoomClient.

## REST vs WebSocket Comparison

Experiment samples carry:

- `transport_mode`
- `experiment_id`
- `scenario`
- `connection_id`
- `websocket_session_id`
- `ws_send_ms`
- `ws_receive_ms`
- `ws_reconnect_count`
- `ws_message_sequence`

Build comparison:

```powershell
$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/gateway/transport/experiments/rebuild?experiment_id=exp-001&scenario=command-heavy&persist=true&export=true" `
  -Headers $headers
```

Or use:

```powershell
python apps/transport_experiment.py `
  --core-url http://127.0.0.1:8000 `
  --token $env:TRADING_CORE_TOKEN `
  --experiment-id exp-001 `
  --scenario command-heavy `
  --export
```

This script assumes the Core server is already running and uses persisted latency samples.

## APIs

- `GET /api/gateway/transport/experiments`
- `POST /api/gateway/transport/experiments/rebuild`
- `GET /api/gateway/transport/experiments/{experiment_id}`
- `GET /api/gateway/transport/websocket-decision`

The WebSocket decision endpoint now includes the latest mock comparison when available. `real_gateway_switch_ready` remains `false` in PR-9.

## 대시보드

PR-10은 `/` 화면에 페이지네이션이 적용된 **WebSocket Mock 실험 목록** 표를 추가한다.

이 표에서 다음을 확인한다.

- `experiment_id` 또는 `scenario`로 필터링
- REST와 WebSocket 샘플 수 비교
- REST 명령 p95와 WebSocket 명령 p95 비교
- 명령 p95 차이와 권고 결과 확인
- 행 클릭 후 상세 패널에서 원본 JSON 확인

대시보드에는 실제 Gateway 전송 방식을 전환하는 버튼이 없다. 이 표는 실험 결과 분석 전용이다.

## Recommendation Labels

## Mock 이후 Real Pilot

PR-11은 mock 비교 이후 실제 32bit Kiwoom Gateway에서 제한적 WebSocket pilot을 실행할 수 있는 `websocket-pilot` transport를 추가한다. 이 단계에서도 기본 운영 transport는 REST long-poll이며, `send_order`, `cancel_order`, `modify_order`는 기본 차단된다.

Real pilot 실행과 soak test 절차는 [Gateway WebSocket Real Pilot Runbook](gateway_websocket_real_pilot_runbook.md)을 따른다.

- `KEEP_REST_LONG_POLL`
- `TUNE_REST_LONG_POLL`
- `RUN_LONGER_WEBSOCKET_EXPERIMENT`
- `WEBSOCKET_PROMISING_BUT_NEEDS_REAL_GATEWAY_TEST`
- `WEBSOCKET_NOT_HELPFUL_RATE_LIMIT_BOUND`
- `WEBSOCKET_NOT_HELPFUL_KIWOOM_EXECUTION_BOUND`
- `PREPARE_REAL_GATEWAY_WEBSOCKET_PR`

## When WebSocket Helps

WebSocket is promising when:

- REST command p95 is high,
- `long_poll_wait_ms` is a major contributor,
- mock WebSocket materially lowers command p95,
- mock WebSocket error/reconnect rate is not worse than REST.

## When WebSocket Does Not Help

WebSocket is not useful if:

- `rate_limit_wait_ms` dominates,
- `gateway_execute_ms` dominates,
- Core DB/API persistence dominates,
- reconnect/error rate is worse,
- command ack/dedupe behavior is not stable.

## Before Real Gateway WebSocket

Required before any real Gateway pilot:

- mock comparison has enough samples,
- reconnection behavior is tested,
- duplicate command ack is idempotent,
- `DISPATCHED` commands are not replayed after reconnect,
- LIVE auto orders remain off,
- real Gateway pilot runs with order execution disabled first.
