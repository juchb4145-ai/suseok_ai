# Kiwoom 자동매매 시스템

이 저장소는 32bit 단일 프로세스 PyQt/Kiwoom 앱에서 다음 분리 구조로 이전 중이다.

- **32bit Kiwoom Gateway**: Kiwoom OpenAPI+ ActiveX/QAxWidget 전용.
- **64bit Core/API/Web Dashboard**: 전략 runtime, 후보, 테마, 리뷰, 리스크 검사, DB, API, 웹 UI 담당.

기존 PyQt 데스크톱 앱은 deprecated legacy entrypoint로만 유지한다.

## 64bit Core/API

64bit Python 환경을 사용한다. Core requirements에는 의도적으로 PyQt5를 넣지 않는다.

```powershell
python -m pip install -r requirements-64.txt
$env:TRADING_CORE_TOKEN = "change-me-local-token"
$env:TRADING_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000 --reload
```

주요 Core 환경변수:

- `TRADING_CORE_TOKEN`: local Gateway/Core token.
- `TRADING_DB_PATH`: SQLite path.
- `TRADING_MODE`: `OBSERVE`, `DRY_RUN`, or `LIVE`.
- `TRADING_ALLOW_LIVE`: must be `1` before LIVE orders can be queued.
- `TRADING_MAX_ORDER_AMOUNT`: per-order amount cap.
- `TRADING_MAX_DAILY_ORDERS_PER_CODE`: per-code command cap.
- `TRADING_ORDER_COMMAND_TTL_SEC`: order command expiry.
- `TRADING_ORDER_COMMAND_MAX_ATTEMPTS`: order command max attempts.
- `TRADING_COMMAND_DEDUPE_RETENTION_SEC`: order command dedupe retention, default `86400`.
- `TRADING_COMMAND_HISTORY_RETENTION_SEC`: finished command history retention target, default `604800`.
- `TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED`: mark stale dispatched commands expired on recovery when `1`.
- `TRADING_RUNTIME_ENABLED`: enable Core StrategyRuntime supervisor, default `0`.
- `TRADING_RUNTIME_AUTO_START`: start runtime on API startup, default `0`.
- `TRADING_RUNTIME_MODE`: runtime policy, `OBSERVE` or `DRY_RUN`; runtime internals stay OBSERVE.
- `TRADING_RUNTIME_EVALUATION_INTERVAL_SEC`: runtime loop interval.
- `TRADING_RUNTIME_CYCLE_TIMEOUT_SEC`: cycle timeout.
- `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS`: enables runtime DRY_RUN order intent records when runtime mode is `DRY_RUN`, default `0`.
- `TRADING_RUNTIME_ALLOW_LIVE_ORDERS`: live auto order flag, default `0`; PR-5 still blocks runtime live orders.
- `TRADING_RUNTIME_DRY_RUN_ACCOUNT`: optional account label for runtime dry-run intents.
- `TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT`: base notional amount for runtime dry-run quantity, default `1000000`.
- `TRADING_RUNTIME_DRY_RUN_MIN_QUANTITY`: minimum runtime dry-run quantity, default `1`.
- `TRADING_RUNTIME_DRY_RUN_HOGA`: runtime dry-run hoga code, default `00`.
- `TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_BUY`: runtime dry-run buy order type, default `1`.
- `TRADING_RUNTIME_DRY_RUN_ORDER_TYPE_SELL`: runtime dry-run sell order type, default `2`.
- `TRADING_RUNTIME_DRY_RUN_REQUIRE_ACCOUNT`: require explicit account for runtime dry-run intents, default `0`.
- `TRADING_RUNTIME_DRY_RUN_RESPECT_WEIGHT_PCT`: apply split-leg weight to dry-run quantity, default `1`.
- `TRADING_TRANSPORT_METRICS_ENABLED`: Gateway/Core transport latency metrics, default `1`.
- `TRADING_TRANSPORT_METRICS_SAMPLE_PRICE_TICK_RATE`: sampled `price_tick` metric rate, default `0.01`.
- `TRADING_TRANSPORT_METRICS_SAMPLE_HEARTBEAT_RATE`: sampled heartbeat metric rate, default `0.1`.
- `TRADING_TRANSPORT_METRICS_RETENTION_SEC`: transport sample retention target, default `604800`.
- `TRADING_TRANSPORT_EVENT_P95_WARN_MS`: event latency warning threshold, default `500`.
- `TRADING_TRANSPORT_COMMAND_P95_WARN_MS`: command latency warning threshold, default `1000`.
- `TRADING_TRANSPORT_ACK_P95_WARN_MS`: ack latency warning threshold, default `1000`.
- `TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_P95_MS`: WebSocket experiment threshold, default `1000`.
- `TRADING_TRANSPORT_WEBSOCKET_RECOMMEND_EMPTY_POLL_RATE`: empty-poll tuning threshold, default `0.8`.
- `TRADING_TRANSPORT_WEBSOCKET_EXPERIMENT_ENABLED`: reserved experimental flag, default `0`.
- `TRADING_THRESHOLD_AB_MIN_SAMPLE_COUNT`: DRY_RUN 기준 A/B 후보 최소 표본 수, default `10`.
- `TRADING_THRESHOLD_AB_STRONG_FP_REDUCTION_MIN`: 강한 후보로 보기 위한 FP 감소 수, default `3`.
- `TRADING_THRESHOLD_AB_MAX_FN_INCREASE`: 허용할 FN 증가 수, default `1`.
- `TRADING_THRESHOLD_AB_MAX_OPPORTUNITY_LOSS_INCREASE`: 허용할 기회손실 증가 수, default `1`.
- `TRADING_THRESHOLD_AB_CONFIDENCE_MIN`: 강한 후보 최소 신뢰도, default `0.5`.
- `TRADING_THRESHOLD_AB_EXPORT_ROOT`: A/B 제안 리포트 export root, default `reports/dry_run_threshold_ab`.
- `TRADING_THRESHOLD_AB_ENABLE_APPLY`: 후보 적용 기능 플래그, default `0`; 이번 구조에서는 적용하지 않는다.

대시보드:

```text
http://127.0.0.1:8000/
```

빠른 운영 점검:

```powershell
python tools/ops_check.py --core-url http://127.0.0.1:8000 --token change-me-local-token
Invoke-RestMethod "http://127.0.0.1:8000/api/ops/alerts"
```

대시보드 상단의 `운영 점검 알림`은 Gateway heartbeat, Kiwoom 로그인, command 실패/거부, WebSocket pilot fallback, Runtime 오류, DRY_RUN 오탐/미탐 신호를 한곳에 모아 보여준다. `LIVE` 자동주문은 별도 안전 PR 전까지 항상 차단으로 표시된다.

Core API:

- `GET /health`
- `GET /api/status`
- `GET /api/ops/alerts`
- `GET /api/gateway/status`
- `GET /api/candidates`
- `GET /api/themes`
- `POST /api/themes/sync/naver?replace=true&max_pages=20`
- `GET /api/orders`
- `GET /api/reviews`
- `GET /api/snapshot`
- `WS /ws/dashboard`
- `POST /api/gateway/events`
- `GET /api/gateway/commands` Gateway polling only
- `GET /api/gateway/commands/status`
- `GET /api/gateway/commands/history?status=&command_type=&trade_date=&limit=&offset=&include_payload=false`
- `GET /api/gateway/commands/{command_id}`
- `GET /api/gateway/commands/{command_id}/events`
- `POST /api/gateway/commands/{command_id}/cancel`
- `POST /api/gateway/commands/prune`
- `POST /api/orders/enqueue`
- `GET /api/runtime/status`
- `POST /api/runtime/start`
- `POST /api/runtime/stop`

Dashboard performance flags:

- `TRADING_DASHBOARD_SNAPSHOT_CACHE_TTL_SEC`: shared `/api/snapshot` and `/ws/dashboard` snapshot cache TTL; default `5`.
- `TRADING_DASHBOARD_HEAVY_SECTION_CACHE_TTL_SEC`: cache TTL for heavy dashboard sections such as themes, reviews, and dry-run performance; default `30`.
- `TRADING_DASHBOARD_WS_PUSH_INTERVAL_SEC`: per-client dashboard WebSocket push interval; default `5`. The browser uses `/api/snapshot` polling only as a WebSocket fallback.
- `POST /api/runtime/restart`
- `POST /api/runtime/cycle`
- `GET /api/runtime/snapshot`
- `GET /api/runtime/readiness`
- `GET /api/runtime/orders/dry-run`
- `GET /api/runtime/orders/dry-run/summary`
- `GET /api/runtime/orders/dry-run/{intent_id}`
- `GET /api/runtime/threshold-ab/dry-run`
- `POST /api/runtime/threshold-ab/dry-run/rebuild`
- `GET /api/runtime/threshold-ab/dry-run/reports`
- `GET /api/runtime/threshold-ab/dry-run/reports/{report_id}`
- `GET /api/runtime/threshold-ab/dry-run/candidates/{candidate_id}`
- `GET /api/gateway/transport/status`
- `GET /api/gateway/transport/latency`
- `GET /api/gateway/transport/latency/summary`
- `POST /api/gateway/transport/latency/rebuild`
- `GET /api/gateway/transport/latency/reports`
- `GET /api/gateway/transport/latency/reports/{report_id}`
- `GET /api/gateway/transport/latency/export`
- `GET /api/gateway/transport/websocket-decision`

Order enqueue example:

```powershell
$body = @{
  account = "1234567890"
  code = "005930"
  side = "buy"
  quantity = 1
  price = 70000
  order_type = 1
  hoga = "00"
  tag = "HYBRID_005930"
  strategy_name = "hybrid_gate"
  candidate_id = 123
  reason = "READY gate result"
  idempotency_key = "hybrid_gate:2026-05-30:005930:1"
} | ConvertTo-Json

Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/orders/enqueue -Body $body -ContentType "application/json"
```

Runtime examples:

```powershell
$env:TRADING_RUNTIME_ENABLED = "0"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000

$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "0"
$env:TRADING_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000

$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "1"
$env:TRADING_RUNTIME_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000

$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_MODE = "DRY_RUN"
$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "1"
$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT = "1000000"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
```

The DRY_RUN runtime setting records order intents only. It does not create Gateway `send_order` commands and does not send Kiwoom orders.

Dry-run sell intent checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/orders/dry-run?side=sell&order_phase=exit"
```

DRY_RUN performance report checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/performance/dry-run
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/performance/dry-run/false-signals?type=all"

$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/runtime/performance/dry-run/rebuild?persist=true&export=true&format=all" -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/performance/dry-run/reports
```

Exports are written to `reports/dry_run_performance/<trade_date>/` as JSON, CSV, and Markdown.

False signal thresholds:

- `TRADING_DRY_RUN_FP_LOSS_THRESHOLD_PCT` default `-1.0`
- `TRADING_DRY_RUN_FP_DRAWDOWN_THRESHOLD_PCT` default `-3.0`
- `TRADING_DRY_RUN_FN_RALLY_THRESHOLD_PCT` default `3.0`
- `TRADING_DRY_RUN_GOOD_TRADE_THRESHOLD_PCT` default `2.0`
- `TRADING_DRY_RUN_MIN_HOLD_MINUTES_FOR_FINAL` default `20`
- `TRADING_DRY_RUN_PENDING_GRACE_MINUTES` default `30`

DRY_RUN 기준 A/B 제안:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/threshold-ab/dry-run?trade_date=2026-05-30"

$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/runtime/threshold-ab/dry-run/rebuild?trade_date=2026-05-30&persist=true&export=true&format=all" `
  -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/threshold-ab/dry-run/reports
```

A/B 제안 리포트는 `reports/dry_run_threshold_ab/<trade_date>/`에 JSON, CSV, Markdown으로 저장된다. 이 리포트는 `theme_score`, `hybrid_score`, `LATE_CHASE`, `LOW_BREADTH` 같은 기준 후보를 제안만 하며, `strategy_runtime_settings`를 자동 수정하지 않는다.

Dashboard `/`는 entry/buy 수, exit/sell 수, 최근 sell intent, 청산 판단 유형 요약, DRY_RUN 성과 오탐/미탐 요약, 게이트/리스크 A/B 후보를 보여준다.

Gateway 전송 지연 확인:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/status
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/transport/latency/summary?group_by=message_type"
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/websocket-decision

$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/gateway/transport/latency/rebuild?persist=true&export=true" -Headers $headers
```

Transport reports are written to `reports/gateway_transport_latency/<trade_date>/` as JSON, CSV, and Markdown. PR-8 keeps Gateway transport on REST + long-poll by default; WebSocket is only a later experiment if latency reports show long-poll wait is the bottleneck.

Gateway WebSocket mock experiment checks:

```powershell
python apps/mock_websocket_gateway.py `
  --ws-url ws://127.0.0.1:8000/ws/gateway/transport `
  --token $env:TRADING_CORE_TOKEN `
  --scenario command-heavy `
  --duration-sec 60 `
  --command-delay-ms 20 `
  --experiment-id exp-001

$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/gateway/transport/experiments/rebuild?experiment_id=exp-001&scenario=command-heavy&persist=true&export=true" `
  -Headers $headers

Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/experiments/exp-001
```

`/ws/gateway/transport`는 PR-9의 mock 전용 실험 채널이다. Dashboard `/ws/dashboard`와 별도이며, WebSocket 실험도 기존 Gateway 명령 큐, dedupe ledger, 영속화, ack/fail handler를 그대로 사용한다. 실제 32bit Kiwoom Gateway 기본값은 계속 REST + long-poll이다.

Dashboard 상세 탐색:

- `/` 화면의 요약 카드는 Core/Gateway/runtime 상태를 WebSocket snapshot으로 갱신한다.
- 전송 지연 샘플, WebSocket mock 실험, DRY_RUN 주문 의도, DRY_RUN 성과 사례, 오탐/미탐 신호, 게이트/리스크 A/B 후보, Gateway 명령 이력은 페이지네이션 REST 표로 조회한다.
- 각 표는 필터, 페이지 크기, 이전/다음, 새로고침, 선택적 자동 새로고침, 오래된 데이터 표시, 행 상세 패널을 지원한다.
- 리포트 재생성/export 작업은 `TRADING_CORE_TOKEN`을 입력받아 호출한다. 토큰은 프론트엔드 코드에 하드코딩하지 않는다.

운영자가 보는 순서:

1. 요약 카드에서 모드, Gateway heartbeat, runtime, 명령 상태를 확인한다.
2. 실패하거나 오래된 명령은 Gateway 명령 이력에서 확인한다.
3. 명령/event/ack 지연은 전송 지연 샘플에서 확인한다.
4. WebSocket Mock 실험은 REST 대비 근거 확인용으로만 사용하고, 실제 전환 버튼으로 쓰지 않는다.
5. 전략 진단은 DRY_RUN 성과 분석과 오탐/미탐 신호에서 확인한다.
6. 기준 변경 후보는 `DRY_RUN 기준 제안`에서 보되, 실제 적용은 별도 승인 PR 전까지 하지 않는다.

## 32bit Kiwoom Gateway

Use 32bit Python 3.9.13 with Kiwoom OpenAPI+ installed and OCX registered.

```powershell
py -3.9-32 -m pip install -r requirements-32.txt
py -3.9-32 apps/kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token change-me-local-token --transport rest --poll-wait-sec 1.0 --network-interval-sec 0.5
```

Gateway rate-limit overrides:

- `GATEWAY_RATE_LIMIT_SEND_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_CANCEL_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_MODIFY_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_TR_REQUEST_SEC`
- `GATEWAY_RATE_LIMIT_SEND_CONDITION_SEC`
- `GATEWAY_RATE_LIMIT_REGISTER_REALTIME_SEC`
- `GATEWAY_RATE_LIMIT_REMOVE_REALTIME_SEC`
- `GATEWAY_RATE_LIMIT_DEFAULT_SEC`

Mock Gateway smoke test:

```powershell
py -3.9-32 apps/kiwoom_gateway.py --mock --once --core-url http://127.0.0.1:8000 --token change-me-local-token
```

Gateway transport options:

- `--transport rest`: default production path.
- `--transport websocket-pilot`: real 32bit Gateway limited pilot path; requires explicit feature flags and still blocks order commands by default.
- `--transport websocket-experimental`: deprecated mock-only alias; real Gateway falls back to REST.
- `--poll-wait-sec`: command long-poll wait duration.
- `--network-interval-sec`: network worker interval hint.
- `--metrics-enabled`: emit transport trace metadata.
- `--metrics-sample-price-tick-rate`: noisy tick metric sampling rate.
- `--metrics-sample-heartbeat-rate`: heartbeat metric sampling rate.

WebSocket real Gateway pilot:

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

Pilot safety flags:

- `TRADING_GATEWAY_WEBSOCKET_PILOT_ALLOWED_COMMANDS`: default allows login/condition/realtime/TR commands.
- `TRADING_GATEWAY_WEBSOCKET_PILOT_BLOCK_ORDER_COMMANDS=1`: default blocks `send_order`, `cancel_order`, `modify_order`.
- `TRADING_GATEWAY_WEBSOCKET_PRICE_TICK_SAMPLE_RATE`: broad real-time tick WebSocket sample rate; default `0`.
- `TRADING_GATEWAY_WEBSOCKET_PRIORITY_TICK_SOURCES`: sources that bypass broad tick sampling; default `holding,theme_lab_watchset`.
- `TRADING_GATEWAY_WEBSOCKET_PRIORITY_TICK_CODES`: optional comma-separated codes that always use the WebSocket pilot tick path.
- `TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_ENABLED`: coalesce condition-event bursts into WebSocket batches; default `1`.
- `TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_MAX_SIZE`: max `condition_event` items per batch; default `100`.
- `TRADING_GATEWAY_WEBSOCKET_CONDITION_EVENT_BATCH_MAX_WAIT_MS`: max micro-batch wait before flush; default `200`.
- `TRADING_CORE_WS_CONDITION_EVENT_ASYNC_ENABLED`: process WS condition-event batches on a Core worker instead of the receive loop; default `1`.
- `TRADING_CORE_WS_CONDITION_EVENT_QUEUE_SIZE`: Core bounded queued event count for async condition-event processing; default `5000`. Status exposes pending events as `core_condition_event_queue_size`, pending batches as `core_condition_event_queue_batch_count`, and in-flight work as `core_condition_event_active_count`.
- `TRADING_CORE_WS_CONDITION_EVENT_WORKERS`: shard Core WS `condition_event` batches by stock code across worker queues while preserving per-code order; default `4`, allowed range `1..8`. Status exposes `core_condition_event_worker_count`, `core_condition_event_active_worker_count`, `core_condition_event_last_worker_index`, and `core_condition_event_last_shard_key`.
- `TRADING_CORE_WS_CONDITION_EVENT_BATCH_CHUNK_SIZE`: split and adaptively drain per-shard Core WS `condition_event` batches up to this event count per worker execution; default `64`, allowed range `1..500`. Status exposes `core_condition_event_batch_chunk_size`, `core_condition_event_queue_sizes_by_worker`, `core_condition_event_queue_batch_counts_by_worker`, `core_condition_event_last_drained_batch_count`, and `core_condition_event_last_queued_batch_count`.
- `TRADING_CORE_WS_CONDITION_EVENT_STALE_INCLUDE_SKIP_MS`: skip stale WS `condition_event` include items that waited in the Core condition queue longer than this threshold before candidate creation; default `15000`, set `0` to disable. Remove events are still processed. Status exposes `core_condition_event_stale_queue_wait_skipped_count`, `core_condition_event_last_stale_queue_wait_ms`, and `core_condition_event_stale_include_skip_ms`.
- Core coalesces duplicate WS `condition_event` bursts by `condition_index/name + code` before enqueueing and skips stale queued events when a newer state for the same key arrived. Status exposes `core_condition_event_received_count`, `core_condition_event_queued_count`, `core_condition_event_coalesced_count`, and `core_condition_event_stale_skipped_count`.
- `TRADING_CORE_WS_EVENT_ASYNC_ENABLED`: queue non-batch WS Gateway events and send-completed diagnostics on a Core worker instead of blocking the receive loop; default `1`. `TRADING_CORE_WS_EVENT_QUEUE_SIZE` defaults to `10000`, and status exposes `core_ws_event_queue_size`, `core_ws_event_last_queue_wait_ms`, `core_ws_event_processed_count`, `core_ws_event_failed_count`, and `core_ws_event_dropped_count`.
- `TRADING_CORE_WS_EVENT_PRIORITY_ENABLED`: process control WS events (`command_ack`, `command_started`, `command_failed`, `rate_limited`, heartbeats, and order/execution events) ahead of data events such as `price_tick` and send-completed diagnostics; default `1`. Status exposes `core_ws_event_priority_enabled`, `core_ws_event_control_queued_count`, `core_ws_event_data_queued_count`, and `core_ws_event_last_priority`.
- `TRADING_CORE_WS_EVENT_WORKER_SPLIT_ENABLED`: run separate Core WS workers for control and data events so price tick/send-completed bursts do not block command/heartbeat processing; default `1`. Status exposes `core_ws_event_split_enabled`, `core_ws_event_control_queue_size`, `core_ws_event_data_queue_size`, `core_ws_event_control_active_count`, `core_ws_event_data_active_count`, and `core_ws_event_last_worker_kind`.
- `TRADING_CORE_WS_EVENT_CONTROL_WORKERS`: shard Core WS control events across workers while keeping the same `command_id`/order key on one worker; default `2`, allowed range `1..8`. Status exposes `core_ws_event_control_worker_count`, `core_ws_event_control_queue_sizes`, and `core_ws_event_last_control_worker_index`.
- `TRADING_CORE_WS_PRICE_TICK_COALESCE_ENABLED`: keep only the latest pending Core WS `price_tick` per `instrument_type + code` so watchset/holding 100% tick bursts do not delay heartbeat/command processing; default `1`. Status exposes `core_ws_price_tick_received_count`, `core_ws_price_tick_queued_count`, `core_ws_price_tick_coalesced_count`, `core_ws_price_tick_processed_count`, `core_ws_price_tick_pending_key_count`, and `core_ws_price_tick_dropped_count`.
- WebSocket send diagnostics split Gateway-to-Core latency into `gateway_ws_queue_to_send_start_ms`, `gateway_ws_send_start_to_core_receive_ms`, and the legacy combined `gateway_ws_to_core_receive_ms`.
- `TRADING_GATEWAY_WEBSOCKET_SEND_COMPLETED_DIAGNOSTICS`: emit Gateway send-completed diagnostics for control messages; default `1`. Samples can then expose `gateway_ws_send_start_to_send_complete_ms` and `gateway_ws_send_complete_to_core_receive_ms`.
- WebSocket pilot compacts Kiwoom status heartbeats before sending them to Core: essential login/order/queue/error counters are kept, while nested `rate_limit`, fallback snapshots, and long priority-code lists are summarized or omitted to reduce heartbeat tail latency.
- Gateway WebSocket pilot drains Core responses on a dedicated receiver task so event acks cannot build up behind outbound bursts.
- Core WebSocket replies use a bounded outbound writer queue (`TRADING_CORE_WS_OUTBOUND_QUEUE_SIZE`, default `1000`) so the receive loop can return to `receive_text()` quickly. Status exposes `core_ws_outbound_queue_size`, `core_ws_last_send_json_ms`, and `core_ws_receive_loop_gap_ms`.
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_ERRORS=3`
- `TRADING_GATEWAY_WEBSOCKET_FALLBACK_AFTER_RECONNECTS=5`

Soak check:

```powershell
python tools/websocket_real_pilot_soak.py `
  --core-url http://127.0.0.1:8000 `
  --token change-me-local-token `
  --duration-sec 3600 `
  --interval-sec 30 `
  --fail-on-duplicate-ack `
  --fail-on-session-loss `
  --max-reconnect-count 3
```

Even in `websocket-pilot`, LIVE auto orders are not enabled. Order commands are rejected by default with `WEBSOCKET_PILOT_ORDER_COMMAND_BLOCKED`.

Mock WebSocket Gateway:

```powershell
python apps/mock_websocket_gateway.py `
  --core-url http://127.0.0.1:8000 `
  --ws-url ws://127.0.0.1:8000/ws/gateway/transport `
  --token change-me-local-token `
  --scenario basic `
  --duration-sec 30
```

Transport experiment report helper:

```powershell
python apps/transport_experiment.py `
  --core-url http://127.0.0.1:8000 `
  --token change-me-local-token `
  --experiment-id exp-001 `
  --scenario command-heavy `
  --export
```

## Legacy PyQt App

Deprecated compatibility path:

```powershell
py -3.9-32 apps/legacy_pyqt_app.py --mock
```

`main.py` remains for compatibility, but new strategy/API/dashboard development should target `trading_app` and `apps/kiwoom_gateway.py`.

`trading/engine.py` direct `client.send_order` behavior is legacy-only. The 64bit Core must queue real orders through `/api/orders/enqueue`.

## Safety Defaults

- Default mode is `OBSERVE`.
- `LIVE` order enablement requires both `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Gateway/Core traffic requires a local token.
- Bind Core to `127.0.0.1` unless there is a reviewed deployment plan.
- Core must pass order/risk guards before queueing any real order command.
- Gateway polling does not mean success. Only `command_ack status=ACKED` marks a command successful.
- Duplicate `idempotency_key` or deterministic order dedupe keys are rejected while active or retained in SQLite.
- Core restart restores valid `QUEUED` commands only. `DISPATCHED` order commands are not automatically resent.
- StrategyRuntime auto LIVE orders are not enabled in PR-6. Runtime uses GatewayCommand for realtime/condition requests, virtual order/review for strategy flow, and optional DRY_RUN buy/sell order intent records for analysis.

More detail:

- [Architecture](docs/architecture_32bit_gateway_64bit_core.md)
- [32bit Gateway / 64bit Core Runbook](docs/runbook_32bit_gateway_64bit_core.md)
- [Gateway 명령 큐 Runbook](docs/gateway_command_queue_runbook.md)
- [Gateway 명령 영속화 Runbook](docs/gateway_command_persistence_runbook.md)
- [Core StrategyRuntime Loop Runbook](docs/core_strategy_runtime_loop_runbook.md)
- [Runtime DRY_RUN 주문 의도 Runbook](docs/runtime_dry_run_order_enqueue_runbook.md)
- [Runtime DRY_RUN Exit/Sell Intent Runbook](docs/runtime_dry_run_exit_sell_intent_runbook.md)
- [DRY_RUN 성과 리포트 Runbook](docs/dry_run_performance_report_runbook.md)
- [DRY_RUN 기준 A/B 제안 Runbook](docs/dry_run_threshold_ab_runbook.md)
- [Gateway 전송 지연 Runbook](docs/gateway_transport_latency_runbook.md)
- [Gateway WebSocket Mock 실험 Runbook](docs/gateway_websocket_mock_experiment_runbook.md)
- [Gateway WebSocket Real Pilot Runbook](docs/gateway_websocket_real_pilot_runbook.md)
- [대시보드 페이지네이션 Runbook](docs/dashboard_pagination_runbook.md)
