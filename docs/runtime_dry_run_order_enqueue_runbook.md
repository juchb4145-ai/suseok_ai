# Runtime DRY_RUN Order Enqueue Runbook

## Purpose

PR-5 connected StrategyRuntime entry decisions to DRY_RUN buy intents. PR-6 adds exit decisions as DRY_RUN sell intents. These intents are not real orders. They are durable records of what the runtime would have wanted to order, with idempotency, dedupe, quantity calculation, and safety results attached.

The runtime still never calls Kiwoom, `QAxWidget`, or Gateway `send_order` directly.

## Policy

- `TRADING_RUNTIME_MODE=OBSERVE`: runtime creates virtual orders/reviews only. No dry-run order intent is created.
- `TRADING_RUNTIME_MODE=DRY_RUN` and `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=0`: runtime still creates virtual orders/reviews only and records `DRY_RUN_ORDER_ENQUEUE_DISABLED`.
- `TRADING_RUNTIME_MODE=DRY_RUN` and `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=1`: entry virtual order submissions create buy intents and saved exit decisions create sell intents in `runtime_order_intents`.
- `TRADING_RUNTIME_ALLOW_LIVE_ORDERS=1`: still blocked in PR-5. Runtime LIVE automation remains a separate safety PR.

`StrategyRuntimeConfig.order_mode` remains forced to `OBSERVE`. DRY_RUN order intent creation is handled by the runtime order sink, not by changing the legacy runtime order mode.

## Flow

```text
StrategyRuntime cycle
  -> gate result READY
  -> EntryPlan created or reused
  -> VirtualOrder submitted or recovered
  -> RuntimeOrderSink.on_entry_order_decision
  -> OrderEnqueueService.enqueue_dry_run_order
  -> runtime_order_intents(order_phase=entry, side=buy) + runtime_order_intent_events
```

Exit flow:

```text
StrategyRuntime cycle
  -> virtual position evaluated
  -> ExitDecision saved
  -> RuntimeOrderSink.on_exit_order_decision
  -> OrderEnqueueService.enqueue_dry_run_order
  -> runtime_order_intents(order_phase=exit, side=sell) + runtime_order_intent_events
```

API callers also use the same service:

```text
POST /api/orders/enqueue dry_run=true
  -> OrderEnqueueService.enqueue_order
  -> runtime_order_intents
```

The runtime does not call its own HTTP endpoint.

## Dedupe

Runtime entry idempotency key:

```text
runtime:dryrun:entry:{trade_date}:{candidate_id}:{entry_plan_id}:{virtual_order_id}:{leg_index}:{code}:{side}:{price}
```

The deterministic broker dedupe key still includes order payload fields and runtime metadata such as `candidate_id`, `virtual_order_id`, and `leg_index`. This allows split entries to coexist while blocking the same virtual order leg from creating repeated intents after cycles or Core restarts.

Runtime exit idempotency key includes `virtual_position_id`, `exit_decision_id` when available, `exit_decision_type`, price, exit percent, and exit quantity. This keeps partial take-profit and later full-close sell intents distinct.

Duplicates do not create another `runtime_order_intents` row. They append a `duplicate_rejected` event to the original intent and return `duplicate_of`.

## Quantity

Runtime DRY_RUN quantity uses:

- `TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT`, default `1000000`
- virtual order `weight_pct` when `TRADING_RUNTIME_DRY_RUN_RESPECT_WEIGHT_PCT=1`
- `floor(order_amount / price)`
- `TRADING_RUNTIME_DRY_RUN_MIN_QUANTITY`, default `1`

If price is invalid or quantity falls below the minimum, the intent is stored as `DRY_RUN_REJECTED`.

## Safety

Two safety results are stored.

`safety_json` is decision safety. It validates whether the DRY_RUN intent is structurally valid:

- account placeholder if allowed
- code and side
- quantity and price
- order amount cap
- daily duplicate limits
- dry-run dedupe

Gateway offline state does not reject the DRY_RUN intent.

`live_safety_json` answers whether the same request would pass LIVE requirements:

- `TRADING_MODE=LIVE`
- `TRADING_ALLOW_LIVE=1`
- Gateway connected and heartbeat healthy
- Kiwoom logged in
- orderable
- account match

This separation lets reviews show both the strategy decision and why LIVE would have been blocked.

## Tables

- `runtime_order_intents`
- `runtime_order_intent_events`

Important fields:

- `intent_id`
- `candidate_id`
- `entry_plan_id`
- `virtual_order_id`
- `virtual_position_id`
- `exit_decision_id`
- `exit_decision_type`
- `order_phase`
- `side`
- `trade_review_id`
- `idempotency_key`
- `dedupe_key`
- `safety_json`
- `live_safety_json`
- `request_json`
- `response_json`
- `metadata_json`

## APIs

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run?limit=20
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/orders/dry-run?side=sell&order_phase=exit"
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/<intent_id>
```

DRY_RUN API enqueue:

```powershell
$body = @{
  account = "1234567890"
  code = "005930"
  side = "buy"
  quantity = 1
  price = 70000
  order_type = 1
  hoga = "00"
  tag = "manual-dry-run"
  strategy_name = "manual"
  reason = "dry-run validation"
  idempotency_key = "manual:dryrun:2026-05-30:005930:1"
  dry_run = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/orders/enqueue -Body $body -ContentType "application/json"
```

## Dashboard

The dashboard shows:

- dry-run sink enabled/policy
- total/accepted/rejected/duplicate intents
- entry/buy and exit/sell counts
- recent sell intents
- exit decision type summary
- live would pass/reject counts
- recent dry-run intents
- top live reject reasons

Raw ticks are not rendered.

## Incident Checklist

1. Confirm `/api/runtime/status` has `mode=DRY_RUN` and no LIVE order warning.
2. Confirm `/api/runtime/orders/dry-run/summary` has expected counts.
3. Inspect the intent detail endpoint for `safety` and `live_safety`.
4. Verify `/api/gateway/commands/status` did not gain `send_order` commands from runtime.
5. Check `trade_reviews.details` for `dry_run_order_intent_id` linkage.
