# Gateway Command Persistence Runbook

## Why Persistence Exists

Gateway commands are part of the live-order safety boundary. A Core restart must not erase:

- queued commands that have not reached Gateway yet,
- dispatched/acked/failed/cancelled history,
- `idempotency_key` and deterministic `dedupe_key` records,
- command event timelines such as `command_started`, `command_ack`, `command_failed`, and `rate_limited`.

PR-3 stores command state in SQLite before StrategyRuntime is connected to the Core process.

## Tables

`gateway_commands`

- One row per `GatewayCommand`.
- Stores command envelope, payload, status, timestamps, attempts, result payload, error text, and trade date.
- Indexed by status, command type, dedupe key, idempotency key, trade date, and update time.

`gateway_command_events`

- Append-only event timeline for command lifecycle and operational signals.
- Stores status transitions, duplicate rejections, rate-limit events, and gateway errors tied to `command_id`.

`gateway_command_dedupe_keys`

- Retained dedupe ledger used after Core restarts.
- Order command keys are retained for `TRADING_COMMAND_DEDUPE_RETENTION_SEC`.
- Failed order commands keep their dedupe key because a local failure may not prove Kiwoom did not receive the request.

## State Flow

```text
enqueue:
  gateway_commands INSERT
  gateway_command_dedupe_keys INSERT
  gateway_command_events enqueue

dispatch:
  QUEUED -> DISPATCHED
  attempts += 1
  gateway_command_events dispatch

command_started:
  status is confirmed as DISPATCHED
  gateway_command_events command_started

command_ack:
  DISPATCHED -> ACKED / FAILED / REJECTED
  result_payload_json updated
  order_results saved when send_order includes order_result

command_failed:
  order commands -> FAILED by default
  non-order commands may requeue only when retryable and attempts remain

expire/cancel:
  QUEUED/DISPATCHED -> EXPIRED
  QUEUED -> CANCELLED
```

Gateway polling is not success. Only `command_ack status=ACKED` means the Gateway actually called Kiwoom and received a successful result.

## Core Restart Recovery

On Core startup:

- `QUEUED` commands with valid TTL are loaded back into the in-memory dispatch queue.
- `DISPATCHED` commands are not requeued.
- `DISPATCHED` order commands are never automatically resent after restart.
- stale dispatched commands are visible through `stale_dispatched_count`.
- expired active commands can be marked `EXPIRED` when `TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED=1`.

Operators should inspect command detail, Gateway logs, Kiwoom order state, and execution events before retrying a stale dispatched order.

## Dedupe Retention

`idempotency_key` wins when supplied. Otherwise Core uses deterministic command keys:

- `send_order`: `order:{account}:{code}:{side}:{quantity}:{price}:{tag}:{strategy_order_id}`
- `cancel_order`: `cancel:{account}:{code}:{original_order_no}`
- `modify_order`: `modify:{account}:{code}:{original_order_no}:{quantity}:{price}`
- `tr_request`: `tr:{rq_name}:{tr_code}:{screen_no}:{request_id}`

For order commands, include a meaningful `tag`, `candidate_id`, or `strategy_order_id` so intentional additional orders can use distinct keys.

Default retention:

```powershell
$env:TRADING_COMMAND_DEDUPE_RETENTION_SEC = "86400"
$env:TRADING_COMMAND_HISTORY_RETENTION_SEC = "604800"
$env:TRADING_COMMAND_RECOVERY_EXPIRE_STALE_DISPATCHED = "1"
```

History pruning can remove old finished command rows, but order dedupe rows remain until their own retention expires.

## Operating APIs

- `GET /api/gateway/commands/status`
- `GET /api/gateway/commands/history?status=&command_type=&trade_date=&limit=&offset=&include_payload=false`
- `GET /api/gateway/commands/{command_id}`
- `GET /api/gateway/commands/{command_id}/events`
- `POST /api/gateway/commands/{command_id}/cancel`
- `POST /api/gateway/commands/prune?older_than_sec=3600`

Gateway polling remains separate:

- `GET /api/gateway/commands`

Do not use the polling endpoint as a dashboard/history endpoint.

## Incident Checklist

1. Check `/api/gateway/status` for heartbeat, Kiwoom login, orderable, and account.
2. Check `/api/gateway/commands/status` for stale dispatched, failed, expired, duplicate, and rate-limited counts.
3. Open `/api/gateway/commands/{command_id}` for the command record and timeline.
4. If an order is stale dispatched, verify Kiwoom order/execution state before manual retry.
5. If duplicate rejection occurs, inspect the dedupe key and `duplicate_of` command id.
6. Prune only after reviewing retention requirements for live trading.
