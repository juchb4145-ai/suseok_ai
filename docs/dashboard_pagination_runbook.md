# Dashboard Pagination Runbook

## Purpose

PR-10 keeps the FastAPI + HTML + vanilla JS dashboard and adds operational pagination, filters, and row drilldowns. Summary cards still come from `/api/snapshot` and `/ws/dashboard`. Large tables now fetch their own REST API pages so accumulated command, transport, dry-run, and performance data do not bloat dashboard snapshots.

## Data Flow

Summary path:

- `/ws/dashboard`
- `/api/snapshot` polling fallback
- Updates status cards, counters, warnings, and compact summaries

Table path:

- REST API calls with `limit` and `offset`
- Independent table state per section
- Filter changes do not reset other tables
- Late responses are ignored with per-table request sequencing and `AbortController`

## Tables

Transport latency samples:

- API: `GET /api/gateway/transport/latency`
- Filters: `trade_date`, `direction`, `message_type`, `transport_mode`, `experiment_id`, `scenario`, `command_id`, `event_id`
- Drilldown: `GET /api/gateway/transport/latency/{sample_id}`

WebSocket mock experiments:

- API: `GET /api/gateway/transport/experiments`
- Filters: `experiment_id`, `scenario`
- Drilldown: `GET /api/gateway/transport/experiments/{experiment_id}`
- The dashboard never enables real Gateway WebSocket transport.

Runtime DRY_RUN orders:

- API: `GET /api/runtime/orders/dry-run`
- Filters: `trade_date`, `status`, `code`, `side`, `order_phase`, `candidate_id`, `virtual_position_id`, `exit_decision_id`
- Drilldown: `GET /api/runtime/orders/dry-run/{intent_id}`

DRY_RUN performance cases:

- API: `GET /api/runtime/performance/dry-run`
- Filters: `trade_date`, `strategy_name`, `code`, `theme_name`, `side`, `order_phase`, `include_rejected`, `include_duplicates`
- Drilldown: `GET /api/runtime/performance/dry-run/lifecycles/{lifecycle_id}`

False signals:

- API: `GET /api/runtime/performance/dry-run/false-signals`
- Filters: `trade_date`, `type`
- Drilldown uses the lifecycle detail endpoint when a lifecycle id exists.

Gateway commands:

- API: `GET /api/gateway/commands/history`
- Filters: `status`, `command_type`, `trade_date`, `command_id`, `include_finished`
- Drilldown: `GET /api/gateway/commands/{command_id}`

## Table Controls

Each table supports:

- Apply filters
- Reset filters
- Reload current page
- Prev / Next
- Page size: 25, 50, 100, 200
- Optional auto refresh at a conservative interval
- Last fetched timestamp
- Stale indicator after roughly 30 seconds
- Loading, empty, and error rows

Rows open the detail drawer. The drawer shows compact key fields and a raw JSON block for exact inspection.

## Protected Actions

Some tables expose rebuild/export actions:

- Transport latency report rebuild/export
- WebSocket mock comparison rebuild/export
- DRY_RUN performance report rebuild/export

These actions call token-protected endpoints. The dashboard prompts for `TRADING_CORE_TOKEN` and stores it in `localStorage` only after the operator enters it. The token is not hardcoded in frontend code.

## Operating Workflows

Gateway delay:

1. Check Gateway Transport cards.
2. Open Transport Latency Samples.
3. Filter by `command_id` or `direction=core_to_gateway`.
4. Compare `long_poll_wait_ms`, `gateway_execute_ms`, `rate_limit_wait_ms`, and `ack_round_trip_ms`.
5. If long-poll wait dominates, inspect WebSocket mock experiment results before planning any real pilot.

DRY_RUN outcome:

1. Check DRY_RUN Performance summary.
2. Open DRY_RUN Performance Cases.
3. Filter by `code`, `theme_name`, or `strategy_name`.
4. Open a lifecycle detail and inspect linked intent/review fields.

False signal:

1. Open False Signals.
2. Select `false_positive`, `false_negative`, or `opportunity_loss`.
3. Drill into lifecycle detail.
4. Compare gate reason, live safety reason, and return/drawdown metrics.

Command failure:

1. Open Gateway Command History.
2. Filter `status=FAILED` or `command_type`.
3. Open the command detail drawer.
4. Inspect the command event timeline and transport latency samples for the same command id.

## Boundaries

PR-10 does not:

- enable LIVE auto orders,
- add order execution buttons,
- switch Gateway transport to WebSocket,
- render raw ticks,
- introduce React/Vite or heavy frontend dependencies.
