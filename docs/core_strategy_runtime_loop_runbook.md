# Core StrategyRuntime Loop Runbook

## Purpose

PR-4 runs `StrategyRuntime` inside the 64bit Core API process without importing Kiwoom ActiveX or PyQt. The runtime is OBSERVE-first: it evaluates candidates, gates, entry plans, virtual orders, positions, and reviews. It does not auto-send live orders.

## RuntimeSupervisor

`trading_app.runtime_supervisor.RuntimeSupervisor` owns the runtime lifecycle:

- builds the API-safe `StrategyRuntime`,
- starts and stops the runtime,
- runs periodic cycles,
- runs manual cycles,
- prevents overlapping cycles,
- captures warnings/errors,
- keeps the last runtime snapshot for API/dashboard,
- shuts down cleanly during FastAPI shutdown.

The runtime work runs through a single-worker executor so `StrategyRuntime.start()`, `cycle()`, and `stop()` use the same runtime-owned SQLite connection thread. API request DB connections remain separate.

## Startup

Runtime is disabled by default:

```powershell
$env:TRADING_RUNTIME_ENABLED = "0"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
```

Manual start:

```powershell
$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "0"
$env:TRADING_RUNTIME_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
```

Auto-start:

```powershell
$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "1"
$env:TRADING_RUNTIME_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
```

If auto-start fails, the API server stays alive and exposes the failure through `/api/runtime/status`.

## Order Policy

- `OBSERVE`: virtual order/review only. No `/api/orders/enqueue` call and no `send_order` GatewayCommand.
- `DRY_RUN`: when `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=1`, entry virtual order submissions create durable dry-run order intents through `OrderEnqueueService`. They do not create Gateway `send_order` commands.
- `LIVE`: runtime auto live order is blocked. `TRADING_RUNTIME_ALLOW_LIVE_ORDERS=1` records a warning; live enablement is a separate safety PR.

`StrategyRuntimeConfig.order_mode` remains forced to `OBSERVE` to avoid mixing legacy order modes with Core `TRADING_MODE`.

## Runtime Order Sink

PR-5 adds a runtime order sink:

- `NoopRuntimeOrderSink` for OBSERVE and disabled DRY_RUN order enqueue.
- `DryRunRuntimeOrderSink` for `TRADING_RUNTIME_MODE=DRY_RUN` and `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=1`.

The entry sink is called after a valid entry plan has a submitted or recovered virtual order. The exit sink is called after a filled exit decision is saved for an open virtual position. Both record `runtime_order_intents` with decision safety and live safety. The sink uses the same `OrderEnqueueService` as `POST /api/orders/enqueue`, but it calls the service directly in-process rather than calling the HTTP endpoint.

LIVE runtime orders remain disabled in PR-5.
PR-6 still keeps LIVE runtime orders disabled; exit decisions are recorded as DRY_RUN sell intents only.

## Gateway Event Flow

Gateway sends events to:

```text
POST /api/gateway/events
```

Core still persists candidate/execution/order events through the existing path. In addition, `RuntimeSupervisor.handle_gateway_event()` routes `price_tick` into `GatewayEventMarketDataBridge`, which updates:

- `MarketDataStore`
- `CandleBuilder`
- `MarketIndexStore`

Raw ticks are not bulk inserted into SQLite.

## Realtime and Condition Commands

Runtime does not call Kiwoom directly.

`GatewayCommandRealtimeClient` converts runtime subscription changes into:

- `register_realtime`
- `remove_realtime`
- `remove_all_realtime`

`GatewayCommandConditionAdapter` converts condition startup/shutdown into:

- `load_conditions`
- `send_condition`
- `stop_condition`

If Gateway heartbeat/login is unavailable, condition commands are deferred with warnings such as `GATEWAY_HEARTBEAT_REQUIRED_FOR_CONDITIONS`.

## Cycle Safety

- Periodic cycles run every `TRADING_RUNTIME_EVALUATION_INTERVAL_SEC`.
- `POST /api/runtime/cycle` runs one manual cycle.
- If a cycle is already running, the next cycle is skipped and `skipped_cycle_count` increases.
- Cycle exceptions are caught; `failed_cycle_count` and `last_error` are updated.
- A failed cycle does not kill FastAPI.

## APIs

All mutating runtime APIs require the local token:

```powershell
$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/start -Headers $headers
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/cycle -Headers $headers
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/runtime/stop -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/snapshot
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/readiness
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run?limit=20
```

Dashboard `/` shows runtime enabled/running state, last cycle, counts, warnings, errors, and DRY_RUN order intent summary.

## DRY_RUN Performance Report Procedure

After runtime cycles have produced entry/buy and exit/sell intents, generate an analysis report:

```powershell
$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/performance/dry-run
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/runtime/performance/dry-run/rebuild?persist=true&export=true&format=all" -Headers $headers
```

The report is analysis-only. It reads `runtime_order_intents`, `virtual_positions`, `exit_decisions`, and `trade_reviews`; it does not run a StrategyRuntime cycle and does not enqueue Gateway `send_order`.

## Persistence

Runtime events and cycle summaries are stored in:

- `runtime_events`
- `runtime_cycles`
- `runtime_order_intents`
- `runtime_order_intent_events`
- existing `logs`

These are intentionally lightweight summaries, not raw tick storage.

## Incident Checklist

1. Check `/api/runtime/status` for `running`, `last_error`, warnings, and failed/skipped counts.
2. Check `/api/gateway/status` for heartbeat/login/orderable state.
3. Check `/api/gateway/commands/status` for rejected condition/realtime commands.
4. Inspect `runtime_cycles` and logs for repeated cycle failures.
5. Keep runtime in OBSERVE until DRY_RUN/LIVE order adapter safety is implemented separately.
