# Gateway Command Queue Runbook

## Command Flow

PR-2 keeps the REST long-poll topology. PR-3 persists the Core-side command queue and history in SQLite:

1. Core enqueues a `GatewayCommand`.
   - Core writes `gateway_commands`, registers a dedupe key, and appends an `enqueue` event in one transaction.
2. 32bit Gateway polls `GET /api/gateway/commands`.
3. Core marks selected commands `DISPATCHED`.
4. Gateway applies local rate limit before calling Kiwoom.
5. Gateway emits `command_started`.
6. Gateway calls Kiwoom and emits `command_ack` or `command_failed`.
7. Core updates command history and saves order results when present.

Polling a command is not success. Only `command_ack status=ACKED` marks success.

## Status Transitions

```text
QUEUED -> DISPATCHED -> ACKED
QUEUED -> DISPATCHED -> FAILED
QUEUED -> DISPATCHED -> REJECTED
QUEUED -> EXPIRED
QUEUED -> CANCELLED
DISPATCHED -> EXPIRED
```

`DISPATCHED` records `attempts` and `dispatched_at`. `ACKED`, `FAILED`, `REJECTED`, `EXPIRED`, and `CANCELLED` are finished states.

## Ack, Retry, Expire

- Gateway emits `command_started` before Kiwoom execution.
- Gateway emits `command_ack` after a concrete Kiwoom result.
- Gateway emits `command_failed` when the call raises before a result.
- Order commands default to `max_attempts=1`.
- TR/condition/realtime commands can retry within their max attempts.
- Commands past `expires_at` are marked `EXPIRED` and are not dispatched.

Finished command records are stored in SQLite for dashboard/history and can be pruned:

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/gateway/commands/prune?older_than_sec=3600"
```

## Idempotency and Dedupe Keys

If `idempotency_key` is supplied, it is the dedupe key. Otherwise Core derives deterministic keys:

- `send_order`: `order:{account}:{code}:{side}:{quantity}:{price}:{tag}:{strategy_order_id}`
- `cancel_order`: `cancel:{account}:{code}:{original_order_no}`
- `modify_order`: `modify:{account}:{code}:{original_order_no}:{quantity}:{price}`
- `tr_request`: `tr:{rq_name}:{tr_code}:{screen_no}:{request_id}`

The queue rejects duplicates when an equal key is already active or retained in `gateway_command_dedupe_keys`.
Order command dedupe rows survive Core restarts and remain even if command history is pruned until `TRADING_COMMAND_DEDUPE_RETENTION_SEC` expires.

## Rate Limit

Gateway applies rate limit immediately before Kiwoom calls. Conservative defaults:

- `send_order`, `cancel_order`, `modify_order`: `0.35s`
- `tr_request`: `0.8s`
- `send_condition`, realtime register/remove: `0.5s`
- `login`, `load_conditions`: `1.0s`

Overrides:

```powershell
$env:GATEWAY_RATE_LIMIT_SEND_ORDER_SEC = "0.5"
$env:GATEWAY_RATE_LIMIT_TR_REQUEST_SEC = "1.0"
$env:GATEWAY_RATE_LIMIT_DEFAULT_SEC = "0.2"
```

When limited, Gateway emits `rate_limited` and requeues the command locally until the timer allows execution.

## Order Enqueue API

`POST /api/orders/enqueue` is the only new Core path for real order commands. Core never calls Kiwoom directly.

Example:

```powershell
$body = @{
  account = "1234567890"
  code = "005930"
  side = "buy"
  quantity = 1
  price = 70000
  order_type = 1
  hoga = "00"
  tag = "STRAT_A_005930"
  strategy_name = "hybrid_gate"
  candidate_id = 123
  reason = "READY gate result"
  idempotency_key = "hybrid_gate:2026-05-30:005930:1"
} | ConvertTo-Json

Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/orders/enqueue -Body $body -ContentType "application/json"
```

## OBSERVE, DRY_RUN, LIVE

- `OBSERVE`: rejects real order enqueue with `OBSERVE_MODE`; no `send_order` command is queued.
- `DRY_RUN`: accepts as dry-run only; no Kiwoom `send_order` command is queued.
- `LIVE`: queues `send_order` only when `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`, Gateway is healthy, and safety checks pass.

## Safety Checks

Minimum Core-side checks:

- Mode/live enablement.
- Account/code/side/quantity/price validity.
- Order amount cap.
- Per-code daily command count cap.
- Duplicate idempotency/dedupe key.
- Gateway connected.
- Gateway heartbeat fresh.
- Kiwoom login true.
- Gateway orderable true.
- Gateway account match.

Environment:

```powershell
$env:TRADING_MODE = "LIVE"
$env:TRADING_ALLOW_LIVE = "1"
$env:TRADING_MAX_ORDER_AMOUNT = "3000000"
$env:TRADING_MAX_DAILY_ORDERS_PER_CODE = "5"
$env:TRADING_ORDER_COMMAND_TTL_SEC = "30"
$env:TRADING_ORDER_COMMAND_MAX_ATTEMPTS = "1"
$env:TRADING_COMMAND_DEDUPE_RETENTION_SEC = "86400"
$env:TRADING_COMMAND_HISTORY_RETENTION_SEC = "604800"
$env:TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED = "1"
```

## Status APIs

- Gateway polling: `GET /api/gateway/commands`
- Queue summary: `GET /api/gateway/commands/status`
- Queue history: `GET /api/gateway/commands/history?status=&command_type=&trade_date=&limit=&offset=&include_payload=false`
- Command detail: `GET /api/gateway/commands/{command_id}`
- Command timeline: `GET /api/gateway/commands/{command_id}/events`
- Cancel queued command: `POST /api/gateway/commands/{command_id}/cancel`
- Prune finished records: `POST /api/gateway/commands/prune`

The dashboard shows queued/dispatched/acked/failed/expired/duplicate/rate-limited/stale counts and recent DB-backed command history.

## Command Queue + Transport Latency

PR-8 adds transport timing around the same command lifecycle. Use these APIs with the queue status:

- `GET /api/gateway/transport/status`
- `GET /api/gateway/transport/latency?command_id=<id>`
- `GET /api/gateway/transport/latency/summary?group_by=message_type`
- `GET /api/gateway/transport/websocket-decision`

When a command stays `DISPATCHED` for too long:

1. Check `GET /api/gateway/commands/{command_id}` and its event timeline.
2. Check `/api/gateway/transport/latency?command_id=<id>`.
3. If `long_poll_wait_ms` is high, tune Gateway `--poll-wait-sec` or consider a mock WebSocket experiment.
4. If `rate_limit_wait_ms` is high, the Gateway is pacing Kiwoom calls and WebSocket will not help.
5. If `gateway_execute_ms` is high, inspect Kiwoom/COM execution.
6. If `ack_round_trip_ms` is high but execution is low, inspect Gateway -> Core REST POST.

The transport report is exported to `reports/gateway_transport_latency/<trade_date>/`.

PR-9 adds `/ws/gateway/transport` only for mock experiments. It does not bypass this command queue:

- WebSocket command delivery still calls `dispatch_commands()`.
- WebSocket delivery alone does not move a command to `ACKED`.
- `command_ack` and `command_failed` over WebSocket reuse the same ack/fail persistence path as REST.
- Reconnect experiments must verify that already `DISPATCHED` commands are not replayed as new commands.

## Reconnect Notes

Gateway reconnects can re-poll only commands that Core still considers dispatchable. Core restart recovery reloads valid `QUEUED` commands only. `DISPATCHED` order commands are not automatically requeued or resent. Order commands have `max_attempts=1` by default, and idempotency/dedupe keys prevent the same order from being queued twice after restart. If Gateway loses the ack after a real Kiwoom send, operators should inspect Kiwoom order/execution events and command history before manual retry.

See also: [Gateway Command Persistence Runbook](gateway_command_persistence_runbook.md).
