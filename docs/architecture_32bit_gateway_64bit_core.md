# 32bit Kiwoom Gateway / 64bit Core Architecture

## Goal

The application is split into two local processes:

- **32bit Kiwoom Gateway**: owns Kiwoom OpenAPI+ ActiveX/QAxWidget only.
- **64bit Core/API/Web Dashboard**: owns strategy runtime, theme engine, review, risk checks, persistence, API, and dashboard.

The Core process must not import `PyQt5.QAxContainer`, `QAxWidget`, or a concrete `KiwoomClient`.

## Process Boundaries

### 32bit Kiwoom Gateway

Entrypoint: `apps/kiwoom_gateway.py`

Responsibilities:

- Kiwoom login through OpenAPI+ ActiveX.
- Realtime quote register/remove.
- Condition load, query, and realtime condition events.
- TR request execution.
- Order/cancel/modify request execution.
- Chejan/order/execution event capture.
- Lightweight event queueing, tick coalescing, and delivery to Core.

Non-goals:

- Strategy scoring.
- Theme analysis.
- Review/backtest work.
- Heavy DB analysis.
- Web dashboard rendering.

The Gateway queues COM events immediately. A background network worker batches outbound events and polls Core commands, while the Qt thread drains commands and calls Kiwoom APIs. This keeps the COM event loop from doing strategy or HTTP-heavy work.

### 64bit Core/API Server

Entrypoint: `apps/core_api.py` or:

```powershell
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000 --reload
```

Responsibilities:

- Candidate lifecycle and state.
- Hybrid gate decisions.
- Theme engine snapshots.
- Strategy runtime orchestration and OBSERVE loop execution.
- Order decision and `OrderGuard`/risk checks before any real order command is queued.
- DB persistence.
- Review and performance validation.
- Web dashboard API and WebSocket snapshots.

The Core depends on `trading.broker.*` protocol models, not on `kiwoom.client`.

`StrategyRuntime` now runs inside the 64bit Core API under `RuntimeSupervisor`. It uses an API-only factory and adapters:

- Gateway `price_tick` events update Core market data and candles.
- Realtime register/remove requests are enqueued as `GatewayCommand`.
- Condition load/send/stop requests are enqueued as `GatewayCommand`.
- Runtime order behavior is OBSERVE-first. PR-5 records DRY_RUN entry/buy intents and PR-6 records DRY_RUN exit/sell intents through `OrderEnqueueService`, but the runtime still never emits Gateway `send_order` commands.

The 32bit Gateway remains Kiwoom communication only.

## Broker Domain Layer

Broker-neutral models live in `trading/broker/models.py`:

- `BrokerOrderRequest`
- `BrokerOrderResult`
- `BrokerExecutionEvent`
- `BrokerPriceTick`
- `BrokerConditionEvent`
- `BrokerTrRequest`
- `BrokerTrResponse`
- `GatewayCommand`
- `GatewayEvent`

`trading/broker/protocol.py` defines protocol interfaces. Existing Kiwoom types are re-exported for compatibility, but DB/Core code should import broker-neutral models.

## Communication Choice

Two options were considered:

| Option | Pros | Cons | Current decision |
| --- | --- | --- | --- |
| FastAPI WebSocket Gateway channel | Lower latency, bidirectional, natural command stream | More reconnection, heartbeat, and Qt-thread coordination to harden before live orders | Measure first, then test only if PR-8 metrics justify it |
| REST event ingest + command long-poll | Simple, debuggable, easier with QAx event loop, resilient to short disconnects | Slightly higher latency, more HTTP requests | Keep as default through PR-8 |

Current communication:

- Gateway -> Core: `POST /api/gateway/events`
- Core -> Gateway: `GET /api/gateway/commands?wait_sec=...`

Dashboard updates use:

- `GET /api/snapshot`
- `WS /ws/dashboard`

PR-8 adds transport latency metrics to the REST/long-poll path. Gateway transport WebSocket and Dashboard WebSocket are separate channels: the Dashboard WebSocket only pushes browser snapshots and does not move Kiwoom commands.

## Message Envelope

Every Gateway event includes:

- `type`
- `event_id`
- `request_id`
- `timestamp`
- `source`
- `payload`
- optional `command_id`
- optional `idempotency_key`

Every Core command includes:

- `type`
- `command_id`
- `request_id`
- `timestamp`
- `source`
- `payload`
- optional `idempotency_key`

Order-related commands must carry `command_id` or `idempotency_key` to prevent duplicate execution after reconnect.

## Command Queue

PR-2 replaces the simple in-memory list with a stateful command queue. PR-3 persists this queue to SQLite because StrategyRuntime must not be allowed to generate live order commands before command state and idempotency survive Core restarts:

- `QUEUED`
- `DISPATCHED`
- `ACKED`
- `REJECTED`
- `FAILED`
- `EXPIRED`
- `CANCELLED`

Gateway polling moves commands from `QUEUED` to `DISPATCHED`. Gateway execution then reports `command_started`, `command_ack`, or `command_failed`. A command is successful only after `command_ack status=ACKED`.

Order commands use deterministic dedupe keys when `idempotency_key` is absent. Duplicate keys already active or retained in SQLite are rejected, including after Core API restart.

Persistence tables:

- `gateway_commands`
- `gateway_command_events`
- `gateway_command_dedupe_keys`

Recovery policy:

- valid `QUEUED` commands are restored and can be dispatched,
- `DISPATCHED` commands are never restored to `QUEUED`,
- `DISPATCHED` order commands are never automatically resent,
- retained dedupe keys continue to block duplicate orders.

Status APIs:

- `GET /api/gateway/commands/status`
- `GET /api/gateway/commands/history?status=&command_type=&trade_date=&limit=&offset=`
- `GET /api/gateway/commands/{command_id}`
- `GET /api/gateway/commands/{command_id}/events`
- `POST /api/gateway/commands/{command_id}/cancel`
- `POST /api/gateway/commands/prune`

## Rate Limit

Gateway applies conservative local rate limits immediately before Kiwoom calls. Defaults can be overridden with `GATEWAY_RATE_LIMIT_*_SEC` environment variables. This keeps the Core queue simple while ensuring the 32bit process never bursts Kiwoom commands during reconnect or backlog drain.

WebSocket command transport is still deferred because PR-2/PR-3 priority is correctness: idempotency, ack, retry, expiration, rate-limit observability, and durable recovery. A WebSocket channel should be reconsidered when:

- command latency from long-poll becomes a measured bottleneck,
- command ack/event correlation is stable in production logs,
- reconnection semantics are tested against Kiwoom login/session loss,
- dashboard users need sub-second command status updates beyond current polling.

PR-8 records `gateway_transport_latency_samples` and `gateway_transport_latency_reports` so this decision is numeric. WebSocket is considered only when command p95 is high and `long_poll_wait_ms` is the dominant source. If `rate_limit_wait_ms`, `gateway_execute_ms`, or Core DB persistence dominates, WebSocket is not expected to fix the issue.

PR-9 adds a mock-only Gateway WebSocket experiment endpoint at `/ws/gateway/transport`. This endpoint is not the Dashboard WebSocket and is not the real Kiwoom Gateway default. The real 32bit Gateway still uses REST event ingest plus command long-poll by default.

The mock WebSocket path deliberately reuses the same Core safety path:

- Core sends commands only through `GatewayStateStore.dispatch_commands()`.
- WebSocket command delivery does not mark commands successful.
- `command_started`, `command_ack`, and `command_failed` are converted back into `GatewayEvent` records and handled by the same persistence/ack code as REST.
- Existing idempotency, dedupe, command history, recovery, and transport latency tables remain authoritative.

The WebSocket mock experiment is used to compare `transport_mode='rest_long_poll'` and `transport_mode='websocket_mock'` samples under the same scenario. A real Gateway WebSocket pilot requires a separate PR after reconnect, duplicate ack, session loss, and LIVE-disabled command behavior are proven.

## Safety Defaults

- Default mode is `OBSERVE`.
- `LIVE` is not order-enabled unless `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Gateway endpoints require a local token (`TRADING_CORE_TOKEN`, default `local-dev-token` for development).
- Core binding should stay at `127.0.0.1`.
- Real order command creation must pass `OrderGuard`/risk guard before it is enqueued.
- Gateway status exposes heartbeat age, stale state, Kiwoom login state, and orderable flag.
- `/api/orders/enqueue` is the only Core API path that can create a real `send_order` command.
- `OBSERVE` never queues real order commands. `DRY_RUN` accepts only dry-run records. `LIVE` requires `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Runtime LIVE auto orders are disabled in PR-5. StrategyRuntime remains OBSERVE internally even if Core trading mode is LIVE.
- Runtime DRY_RUN intent recording is enabled only with `TRADING_RUNTIME_MODE=DRY_RUN` and `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=1`; entry/buy and exit/sell records go to `runtime_order_intents`, not the Gateway command queue.

## Persistence and Performance

- SQLite is configured with `busy_timeout`, WAL, and `synchronous=NORMAL`.
- Raw ticks are not inserted one by one by the PR-1 Core API.
- UI receives snapshots instead of raw tick streams.
- Gateway queue can coalesce repeated `price_tick` events by code within each flush batch.
- Transport latency samples are stored with sampling. Command/ack/condition/execution/order events are retained, while noisy `price_tick` and `heartbeat` metrics use configurable sampling rates.
- Long logs and dashboard tables are capped to recent rows.

## Legacy

The old single-process PyQt app remains available as:

```powershell
python apps/legacy_pyqt_app.py --mock
```

It is deprecated for new development. `main.py` remains as a compatibility entrypoint while strategy/UI work moves to Core/API/Web.

`trading/engine.py` still has a direct `client.send_order` path for the legacy PyQt app. New 64bit Core order flow must use `/api/orders/enqueue`, which creates a `GatewayCommand` and lets the 32bit Gateway execute Kiwoom calls.

## PR Roadmap

PR-1:

- Broker-neutral models/protocols.
- Core FastAPI health/status/snapshot APIs.
- Dashboard MVP with WebSocket snapshot push and polling fallback.
- Gateway status and mock Gateway REST flow.
- Legacy PyQt entrypoint.
- 64bit import tests without PyQt.

PR-2:

- Full Kiwoom Gateway command queue and rate limits.
- Command ack/fail/expire history.
- Hardened `/api/orders/enqueue` safety gate.

PR-3:

- SQLite-backed command queue, event timeline, and dedupe ledger.
- Core restart recovery for valid `QUEUED` commands.
- No automatic resend for `DISPATCHED` order commands.
- DB-backed command history/detail/status APIs.

PR-4:

- StrategyRuntime loop inside 64bit Core API.
- RuntimeSupervisor start/stop/manual cycle/status APIs.
- Gateway event to market-data bridge.
- Realtime/condition requests through GatewayCommand queue.
- Runtime dashboard status.

PR-8:

- REST event ingest and command long-poll latency metrics.
- Event, command dispatch, Gateway execution, rate-limit wait, and ack round-trip samples.
- SQLite-backed transport latency reports and WebSocket decision advisor.
- Dashboard transport latency card.
- WebSocket remains off by default.

PR-9:

- Mock-only Gateway WebSocket transport endpoint.
- Mock WebSocket Gateway CLI and REST-vs-WebSocket comparison report.
- WebSocket path reuses command queue, dedupe, persistence, and ack handlers.
- Dashboard mock experiment summary.
- Real Kiwoom Gateway remains REST long-poll by default.

PR-5:

- `OrderEnqueueService` shared by API and runtime.
- Runtime DRY_RUN order intent sink.
- Persistent `runtime_order_intents` and event timeline.
- DRY_RUN decision safety vs LIVE safety recording.
- No runtime Gateway `send_order` creation.

PR-6:

- Runtime exit decisions generate DRY_RUN sell intents.
- `order_phase` and `side` distinguish entry/buy from exit/sell.
- Partial take-profit and full-close decisions use distinct idempotency keys.
- Dashboard/API summarize sell intents and exit decision types.

PR-7:

- DRY_RUN entry/buy and exit/sell intents are linked into performance lifecycles.
- The analyzer joins `runtime_order_intents`, `virtual_positions`, `exit_decisions`, and `trade_reviews`.
- False positive, false negative, opportunity-loss, and data-quality summaries are exposed through API, Dashboard, and JSON/CSV/Markdown exports.
- This layer is analysis-only and still does not create Gateway `send_order`.

Next:

- Dashboard transport/performance pagination hardening.
- Real Gateway WebSocket limited pilot only if mock comparison supports it.
- Dashboard screen hardening and richer order/position views.
- DRY_RUN report-driven gate/risk threshold A/B suggestions.
- LIVE order enablement as a separate safety PR.
