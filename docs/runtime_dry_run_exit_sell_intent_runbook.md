# Runtime DRY_RUN Exit/Sell Intent Runbook

## Purpose

PR-6 records StrategyRuntime exit decisions as DRY_RUN sell intents. These are not real sell orders. They are durable records that let us inspect how the runtime would have exited a virtual position before any LIVE automation is enabled.

The runtime still does not create Gateway `send_order` commands, does not call Kiwoom, and does not call its own HTTP API.

## Entry vs Exit

- Entry intent: `side=buy`, `order_phase=entry`, created after a virtual order is submitted or recovered.
- Exit intent: `side=sell`, `order_phase=exit`, created after a filled `ExitDecision` is saved.

Both use `OrderEnqueueService.enqueue_dry_run_order()` and both persist:

- `decision_safety`
- `live_safety`
- idempotency key
- dedupe key
- request/response JSON
- runtime metadata

## Exit Decision Types

Handled decision types:

- `TAKE_PROFIT`: usually partial exit when `partial_exit=true`.
- `SUPPORT_LOSS`: full exit.
- `TIME_EXIT`: full exit.
- `TRAILING_STOP`: full exit.

Trailing floor updates or performance updates without a saved exit decision do not create sell intents.

## Quantity

Sell quantity is based on the virtual position:

- `position.quantity` is the base quantity.
- `partial_exit=true`: `floor(position.quantity * exit_percent / 100)`.
- `exit_percent` falls back to `take_profit_exit_percent`, then `70`.
- `full_exit=true` or final exit types: full `position.quantity`.
- Invalid or zero quantity creates a `DRY_RUN_REJECTED` intent with `QUANTITY_ZERO`.
- Invalid price creates a `DRY_RUN_REJECTED` intent with `PRICE_INVALID`.

Metadata preserves `remaining_weight_pct`, `filled_weight_pct`, exit percent source, price source, and calculation reason. The current virtual position model does not yet maintain precise remaining share quantity after partial exits; that remains a later reconciliation improvement.

## Idempotency

If `exit_decision_id` exists:

```text
runtime:dryrun:exit:{trade_date}:{virtual_position_id}:{exit_decision_id}:{exit_decision_type}:{code}:sell:{price}:{exit_percent}:{exit_quantity}
```

Without `exit_decision_id`:

```text
runtime:dryrun:exit:{trade_date}:{virtual_position_id}:{exit_decision_type}:{reason}:{code}:sell:{price}:{exit_percent}:{exit_quantity}
```

This keeps partial take-profit and later full close intents separate while preventing repeated cycles or Core restarts from recreating the same sell intent.

## Review Link

`TradeReview.details` now separates:

- `dry_run_entry_*`
- `dry_run_exit_*`
- legacy `dry_run_*` aliases for entry compatibility

All entry and exit intent rows are linked back through `runtime_order_intents.trade_review_id` when a review is saved.

## APIs

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/orders/dry-run?side=sell&order_phase=exit"
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/<intent_id>
```

Summary fields include:

- `by_side`
- `by_order_phase`
- `entry_total`
- `exit_total`
- `buy_total`
- `sell_total`
- `exit_by_decision_type`
- `exit_by_reason`
- `exit_live_would_pass`
- `exit_live_would_reject`

## Dashboard

Dashboard `/` shows:

- entry/buy intent counts
- exit/sell intent counts
- sell accepted/rejected/duplicate counts
- recent sell intents
- exit decision type summary

## Performance Lifecycle Link

PR-7 links exit/sell intents into the DRY_RUN performance lifecycle. A sell intent is connected by `virtual_position_id` first, then `virtual_order_id`, `trade_review_id`, and finally `candidate_id + code + trade_date`.

This lets the report distinguish:

- entry/buy accepted but later SUPPORT_LOSS
- partial TAKE_PROFIT followed by a later full exit
- exit intent without an entry intent (`orphan_exit`)
- review exists but no entry intent (`NO_ENTRY_INTENT_BUT_RALLIED`)

Check:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/performance/dry-run?order_phase=exit"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/performance/dry-run/false-signals?type=all"
```

## Incident Checklist

1. Confirm `/api/runtime/status` shows DRY_RUN policy and no LIVE runtime order enablement.
2. Check `/api/runtime/orders/dry-run?side=sell&order_phase=exit`.
3. Inspect `safety` and `live_safety` on a sell intent detail.
4. Check `/api/gateway/commands/status` to confirm runtime did not create `send_order`.
5. Inspect `TradeReview.details.dry_run_exit_summary` for review linkage.
