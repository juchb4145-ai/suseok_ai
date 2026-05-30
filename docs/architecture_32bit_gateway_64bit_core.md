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
- Strategy runtime orchestration.
- Order decision and `OrderGuard`/risk checks before any real order command is queued.
- DB persistence.
- Review and performance validation.
- Web dashboard API and WebSocket snapshots.

The Core depends on `trading.broker.*` protocol models, not on `kiwoom.client`.

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

| Option | Pros | Cons | PR-1 decision |
| --- | --- | --- | --- |
| FastAPI WebSocket Gateway channel | Lower latency, bidirectional, natural command stream | More reconnection and Qt-thread coordination to finish safely | Defer to PR-2 |
| REST event ingest + command long-poll | Simple, debuggable, easier with QAx event loop, resilient to short disconnects | Slightly higher latency, more HTTP requests | Use in PR-1 |

PR-1 uses:

- Gateway -> Core: `POST /api/gateway/events`
- Core -> Gateway: `GET /api/gateway/commands?wait_sec=...`

Dashboard updates use:

- `GET /api/snapshot`
- `WS /ws/dashboard`

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

## Safety Defaults

- Default mode is `OBSERVE`.
- `LIVE` is not order-enabled unless `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Gateway endpoints require a local token (`TRADING_CORE_TOKEN`, default `local-dev-token` for development).
- Core binding should stay at `127.0.0.1`.
- Real order command creation must pass `OrderGuard`/risk guard before it is enqueued.
- Gateway status exposes heartbeat age, stale state, Kiwoom login state, and orderable flag.

## Persistence and Performance

- SQLite is configured with `busy_timeout`, WAL, and `synchronous=NORMAL`.
- Raw ticks are not inserted one by one by the PR-1 Core API.
- UI receives snapshots instead of raw tick streams.
- Gateway queue can coalesce repeated `price_tick` events by code within each flush batch.
- Long logs and dashboard tables are capped to recent rows.

## Legacy

The old single-process PyQt app remains available as:

```powershell
python apps/legacy_pyqt_app.py --mock
```

It is deprecated for new development. `main.py` remains as a compatibility entrypoint while strategy/UI work moves to Core/API/Web.

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
- Persistent Gateway WebSocket if latency needs it.
- Real TR response row extraction and request correlation.
- StrategyRuntime process loop inside Core.
- RiskGuard-backed real order command API.
- Dashboard screen hardening and richer order/position views.
