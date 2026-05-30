# Kiwoom Trading System

This repository is moving from a 32bit single-process PyQt/Kiwoom app to a split architecture:

- **32bit Kiwoom Gateway**: Kiwoom OpenAPI+ ActiveX/QAxWidget only.
- **64bit Core/API/Web Dashboard**: strategy runtime, candidates, themes, reviews, risk checks, DB, API, and web UI.

The old PyQt desktop app is still available as a deprecated legacy entrypoint.

## 64bit Core/API

Use a 64bit Python environment. The Core requirements intentionally do not include PyQt5.

```powershell
python -m pip install -r requirements-64.txt
$env:TRADING_CORE_TOKEN = "change-me-local-token"
$env:TRADING_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000 --reload
```

Common Core environment variables:

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

Dashboard:

```text
http://127.0.0.1:8000/
```

Core APIs:

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

Dashboard `/` shows entry/buy counts, exit/sell counts, recent sell intents, exit decision type summaries, and DRY_RUN performance false-positive/false-negative summaries.

Gateway transport latency checks:

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

`/ws/gateway/transport` is mock-only in PR-9. It is separate from Dashboard `/ws/dashboard`, and it still uses the same Gateway command queue, dedupe ledger, persistence, and ack/fail handlers. The real 32bit Kiwoom Gateway remains on REST + long-poll by default.

Dashboard drilldowns:

- `/` keeps WebSocket snapshot cards for Core/Gateway/runtime summary.
- Transport latency samples, WebSocket mock experiments, DRY_RUN order intents, DRY_RUN performance cases, false signals, and Gateway command history are loaded through paginated REST tables.
- Each table supports filters, page size, Prev/Next, reload, optional auto refresh, stale markers, and row detail drawers.
- Rebuild/export actions prompt for `TRADING_CORE_TOKEN`; no token is hardcoded in frontend code.

Operational order:

1. Check summary cards for mode, Gateway heartbeat, runtime, and command status.
2. Use Gateway Command History for failed or stale commands.
3. Use Transport Latency Samples for command/event/ack timing.
4. Use WebSocket Mock Experiments only for REST-vs-WebSocket evidence, not switching.
5. Use DRY_RUN Performance and False Signals for strategy diagnosis.

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
- `--transport websocket-experimental`: mock-only experiment path; real Kiwoom Gateway falls back to or stays on REST unless `--mock` is used.
- `--poll-wait-sec`: command long-poll wait duration.
- `--network-interval-sec`: network worker interval hint.
- `--metrics-enabled`: emit transport trace metadata.
- `--metrics-sample-price-tick-rate`: noisy tick metric sampling rate.
- `--metrics-sample-heartbeat-rate`: heartbeat metric sampling rate.

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
- [Runbook](docs/runbook_32bit_gateway_64bit_core.md)
- [Gateway Command Queue Runbook](docs/gateway_command_queue_runbook.md)
- [Gateway Command Persistence Runbook](docs/gateway_command_persistence_runbook.md)
- [Core StrategyRuntime Loop Runbook](docs/core_strategy_runtime_loop_runbook.md)
- [Runtime DRY_RUN Order Enqueue Runbook](docs/runtime_dry_run_order_enqueue_runbook.md)
- [Runtime DRY_RUN Exit/Sell Intent Runbook](docs/runtime_dry_run_exit_sell_intent_runbook.md)
- [DRY_RUN Performance Report Runbook](docs/dry_run_performance_report_runbook.md)
- [Gateway Transport Latency Runbook](docs/gateway_transport_latency_runbook.md)
- [Gateway WebSocket Mock Experiment Runbook](docs/gateway_websocket_mock_experiment_runbook.md)
- [Dashboard Pagination Runbook](docs/dashboard_pagination_runbook.md)
