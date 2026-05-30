# Gateway Transport Latency Runbook

## Purpose

PR-8 does not switch the Gateway transport to WebSocket. It instruments the current REST event ingest and command long-poll path so the team can decide from numbers.

Current transport:

- Gateway -> Core events: `POST /api/gateway/events`
- Core -> Gateway commands: `GET /api/gateway/commands?wait_sec=...`
- Gateway command result: `command_started`, `command_ack`, `command_failed`, `rate_limited` events back to Core

Dashboard WebSocket (`/ws/dashboard`) is separate. It is only for browser snapshots and is not the Gateway command transport.

## Measured Segments

Gateway -> Core event path:

- Gateway event create/enqueue time
- Gateway REST POST start time
- Core receive time
- Core persist time
- Optional runtime/dashboard forwarding time

Core -> Gateway command path:

- Core command created/queued time
- Core dispatch time
- Long-poll response time
- Gateway poll receive time
- Gateway local queue wait
- Gateway command start time

Execution and ack path:

- Gateway Kiwoom call start/finish time
- Gateway execute duration
- Rate-limit wait duration
- Gateway ack create/post time
- Core ack receive/persist time
- Ack round-trip from long-poll response to Core ack receive

Monotonic timestamps are only compared inside the same process. End-to-end cross-process timing uses UTC wall time and sets `clock_skew_warning` if timing looks inconsistent.

## SQLite Tables

Samples:

- `gateway_transport_latency_samples`

Reports:

- `gateway_transport_latency_reports`

Samples are intentionally not raw tick storage. Sampling defaults:

- command/ack/order/execution/condition events: 100%
- `price_tick`: `TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE`, default `0.01`
- `heartbeat`: `TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE`, default `0.1`

## Environment

Core:

```powershell
$env:TRADING_TRANSPORT_METRICS_ENABLED = "1"
$env:TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE = "0.01"
$env:TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE = "0.1"
$env:TRADING_TRANSPORT_METRICS_RETENTION_SEC = "604800"
$env:TRADING_TRANSPORT_EVENT_P95_WARN_MS = "500"
$env:TRADING_TRANSPORT_COMMAND_P95_WARN_MS = "1000"
$env:TRADING_TRANSPORT_ACK_P95_WARN_MS = "1000"
$env:TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_P95_MS = "1000"
$env:TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_EMPTY_POLL_RATE = "0.8"
```

Gateway:

```powershell
py -3.9-32 apps/kiwoom_gateway.py `
  --core-url http://127.0.0.1:8000 `
  --token $env:TRADING_CORE_TOKEN `
  --transport rest `
  --poll-wait-sec 1.0 `
  --network-interval-sec 0.5
```

`--transport websocket-experimental` is mock-only in PR-9 and is not the production default.

## APIs

Status:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/status
```

Samples:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/transport/latency?direction=core_to_gateway&limit=100"
```

Summary:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/transport/latency/summary?group_by=message_type"
```

Rebuild and persist report:

```powershell
$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/gateway/transport/latency/rebuild?persist=true&export=true" -Headers $headers
```

WebSocket decision:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/websocket-decision
```

REST vs WebSocket mock experiment comparison:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/experiments

$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/gateway/transport/experiments/rebuild?experiment_id=exp-001&scenario=command-heavy&persist=true&export=true" `
  -Headers $headers

Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/experiments/exp-001
```

대시보드:

- `/`를 연다.
- **전송 지연 샘플** 표에서 샘플을 페이지 단위로 탐색한다.
- `command_id`, `event_id`, `direction`, `message_type`, `transport_mode`, `experiment_id`, `scenario`로 필터링한다.
- 행을 클릭하면 지연 샘플 상세 패널이 열린다.
- 명령 상태 전이가 필요하면 같은 `command_id`로 **게이트웨이 명령 이력**도 함께 확인한다.

Exports are written to:

```text
reports/gateway_transport_latency/<trade_date>/
```

## How to Read the Numbers

Gateway -> Core event p95 high:

- If `gateway_post_ms` is high, inspect HTTP/network path.
- If `core_persist_ms` is high, inspect SQLite contention or Core event handlers.
- If only `price_tick` is noisy, reduce tick volume or sampling before changing transport.

Core -> Gateway command p95 high:

- If `long_poll_wait_ms` dominates, reduce `--poll-wait-sec` or test a push channel.
- If `core_dispatch_wait_ms` dominates, inspect command queue/DB/Core load.
- If empty poll rate is high and command latency is fine, increase wait time to reduce HTTP request churn.

Ack p95 high:

- If `gateway_execute_ms` dominates, WebSocket will not fix it; inspect Kiwoom/COM calls.
- If `rate_limit_wait_ms` dominates, tune command pacing/rate limits.
- If `ack_round_trip_ms` dominates while execute/rate-limit are low, inspect REST POST/HTTP path.

## WebSocket Decision Criteria

WebSocket is recommended for experiment only when:

- command p95 exceeds `TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_P95_MS`,
- `long_poll_wait_ms` is a major contributor,
- rate-limit wait and Gateway execution are not the dominant bottlenecks.

WebSocket is not recommended when:

- p95 latency is under thresholds,
- `rate_limit_wait_ms` dominates,
- `gateway_execute_ms` dominates,
- Core DB/persist latency dominates,
- duplicate/ack/dedupe correctness is not stable.

Decision labels:

- `KEEP_REST_LONG_POLL`
- `TUNE_LONG_POLL_WAIT_SEC`
- `INVESTIGATE_RATE_LIMIT`
- `INVESTIGATE_KIWOOM_EXECUTION`
- `TRY_WEBSOCKET_EXPERIMENT`
- `SWITCH_TO_WEBSOCKET_AFTER_TESTS`

PR-8 can produce `TRY_WEBSOCKET_EXPERIMENT`; it does not switch production transport.

PR-9 adds a mock-only WebSocket comparison path. It can produce labels such as:

- `TUNE_REST_LONG_POLL`
- `RUN_LONGER_WEBSOCKET_EXPERIMENT`
- `WEBSOCKET_PROMISING_BUT_NEEDS_REAL_GATEWAY_TEST`
- `WEBSOCKET_NOT_HELPFUL_RATE_LIMIT_BOUND`
- `WEBSOCKET_NOT_HELPFUL_KIWOOM_EXECUTION_BOUND`

`real_gateway_switch_ready` stays `false` in PR-9. Mock WebSocket results are evidence for the next PR, not a production transport switch.

## REST vs WebSocket Mock Comparison

The mock experiment uses `/ws/gateway/transport`, which is separate from Dashboard `/ws/dashboard`.

The WebSocket path still uses:

- `gateway_state.dispatch_commands()` for command dispatch,
- the same command persistence and dedupe tables,
- the same `command_started`, `command_ack`, and `command_failed` event handlers,
- the same latency sample table with `transport_mode='websocket_mock'`.

Compare these values first:

- REST `command_latency_p95_ms` vs WebSocket `command_latency_p95_ms`
- REST `ack_latency_p95_ms` vs WebSocket `ack_latency_p95_ms`
- REST `event_latency_p95_ms` vs WebSocket `event_latency_p95_ms`
- REST `long_poll_wait_p95_ms`
- both transports' error/reconnect counts

If WebSocket improves command p95 but REST was not long-poll bound, the result is not enough to justify a real Gateway switch. If `rate_limit_wait_ms` or `gateway_execute_ms` dominates, WebSocket is the wrong fix.

## Operational Tuning Order

1. Check `/api/gateway/transport/status`.
2. If p95 is high, inspect direction/message-type groups.
3. Compare `long_poll_wait_ms`, `rate_limit_wait_ms`, and `gateway_execute_ms`.
4. Tune long-poll settings only when long-poll wait is the bottleneck.
5. Tune rate limits only when rate-limit wait dominates.
6. Investigate Kiwoom calls when execute time dominates.
7. Run mock-only WebSocket experiment only after REST bottleneck is confirmed.
