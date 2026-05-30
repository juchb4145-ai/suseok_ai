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

대시보드:

```text
http://127.0.0.1:8000/
```

Core API:

- `GET /health`
- `GET /api/status`
- `GET /api/gateway/status`
- `GET /api/candidates`
- `GET /api/themes`
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
- `POST /api/runtime/restart`
- `POST /api/runtime/cycle`
- `GET /api/runtime/snapshot`
- `GET /api/runtime/readiness`
- `GET /api/runtime/orders/dry-run`
- `GET /api/runtime/orders/dry-run/summary`
- `GET /api/runtime/orders/dry-run/{intent_id}`
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

Dashboard `/`는 entry/buy 수, exit/sell 수, 최근 sell intent, 청산 판단 유형 요약, DRY_RUN 성과 오탐/미탐 요약을 보여준다.

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
- 전송 지연 샘플, WebSocket mock 실험, DRY_RUN 주문 의도, DRY_RUN 성과 사례, 오탐/미탐 신호, Gateway 명령 이력은 페이지네이션 REST 표로 조회한다.
- 각 표는 필터, 페이지 크기, 이전/다음, 새로고침, 선택적 자동 새로고침, 오래된 데이터 표시, 행 상세 패널을 지원한다.
- 리포트 재생성/export 작업은 `TRADING_CORE_TOKEN`을 입력받아 호출한다. 토큰은 프론트엔드 코드에 하드코딩하지 않는다.

운영자가 보는 순서:

1. 요약 카드에서 모드, Gateway heartbeat, runtime, 명령 상태를 확인한다.
2. 실패하거나 오래된 명령은 Gateway 명령 이력에서 확인한다.
3. 명령/event/ack 지연은 전송 지연 샘플에서 확인한다.
4. WebSocket Mock 실험은 REST 대비 근거 확인용으로만 사용하고, 실제 전환 버튼으로 쓰지 않는다.
5. 전략 진단은 DRY_RUN 성과 분석과 오탐/미탐 신호에서 확인한다.

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
- [Gateway 전송 지연 Runbook](docs/gateway_transport_latency_runbook.md)
- [Gateway WebSocket Mock 실험 Runbook](docs/gateway_websocket_mock_experiment_runbook.md)
- [Gateway WebSocket Real Pilot Runbook](docs/gateway_websocket_real_pilot_runbook.md)
- [대시보드 페이지네이션 Runbook](docs/dashboard_pagination_runbook.md)
