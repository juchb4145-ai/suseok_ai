# LIVE_SIM Canary Post-Trade Report

## Purpose

This report is a review-only feedback loop for LIVE_SIM Hybrid READY Canary orders. It checks how a simulated Kiwoom order moved from Canary decision to order intent, Gateway command, broker acceptance, fill, cancel, position, exit, and final result.

It does not enable LIVE_REAL, create orders, relax guards, change Hybrid thresholds, or tune `strategy_runtime_settings`.

## Linked Data

The linker reads these sources when present:

- `live_sim_canary_decisions`
- `runtime_order_intents`
- `gateway_commands`
- `gateway_command_events`
- `live_sim_orders`
- `live_sim_fill_events`
- `live_sim_positions`
- `live_sim_cancel_orders`
- `exit_decisions`
- `trade_reviews`
- `gateway_price_ticks`
- `hybrid_gate_validation_events`

Link priority:

1. `gateway_command_id`
2. `order_intent_id`
3. `idempotency_key`
4. `broker_order_no` / `order_no`
5. `candidate_instance_id + code + trade_date`
6. `candidate_id + code + trade_date`

Unlinked executions and orders are kept as data-quality issues instead of being hidden.

## DRY_RUN vs LIVE_SIM

The analyzer compares LIVE_SIM cases with DRY_RUN lifecycles by candidate instance, order intent, or candidate/code/date. Important fields:

- `dry_run_expected_entry_price` vs `live_sim_avg_entry_price`
- `dry_run_net_return_pct` vs `live_sim_net_return_pct`
- `net_return_diff_pct`
- `dry_run_exit_reason` vs `live_sim_exit_reason`
- `outcome_match`: `MATCH`, `LIVE_BETTER`, `LIVE_WORSE`, or `INCOMPARABLE`

`INCOMPARABLE` is used for no-fill, missing DRY_RUN data, or missing realized result. Missing data is never treated as zero return.

## Quality Grades

Entry fill quality:

- `GOOD`: full fill and entry slippage within the good threshold.
- `ACCEPTABLE`: filled with tolerable slippage or limited missing context.
- `BAD`: high slippage, stale tick, or weak partial fill quality.
- `NO_FILL`: submitted/accepted but no entry fill.
- `UNKNOWN`: not enough order data to grade.

Exit fill quality:

- `GOOD`: complete exit with favorable or tolerable sell slippage.
- `ACCEPTABLE`: exit filled but context is limited.
- `BAD`: missing exit for an open position, partial exit, or poor exit slippage.
- `NO_FILL`: exit order submitted but no exit fill.
- `UNKNOWN`: no exit expected or insufficient data.

## Issue Types

The standard issue taxonomy includes:

- `NO_BROKER_ACK`
- `ACK_BUT_NO_FILL`
- `PARTIAL_FILL_TIMEOUT`
- `CANCEL_FAILED`
- `CANCELLED_BEFORE_FILL`
- `EXIT_NOT_SUBMITTED`
- `EXIT_NO_FILL`
- `STOP_LOSS_DELAYED`
- `TAKE_PROFIT_NOT_CAPTURED`
- `TIME_EXIT_WEAK`
- `LIVE_WORSE_THAN_DRY_RUN`
- `SLIPPAGE_HIGH`
- `LATENCY_HIGH`
- `STALE_TICK_ENTRY`
- `RECONCILE_REQUIRED`
- `ORPHAN_EXECUTION`
- `ORPHAN_ORDER_RESULT`
- `POSITION_QTY_MISMATCH`
- `UNKNOWN_FINAL_STATUS`

Each issue carries `severity`, `operator_message_ko`, `recommended_action_ko`, and linked evidence fields.

## APIs

Read current summary and cases:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/canary/performance?trade_date=2026-06-17"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/canary/performance/cases?trade_date=2026-06-17&limit=50"
```

Rebuild and export reports:

```powershell
$headers = @{ "X-Local-Token" = "local-dev-token" }
Invoke-RestMethod `
  -Method Post `
  "http://127.0.0.1:8000/api/runtime/live-sim/canary/performance/rebuild" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"trade_date":"2026-06-17","persist":true,"export":"all"}'
```

Reports are written under:

```text
reports/live_sim_canary/<trade_date>/
```

## Exit Policy Validation Flow

After LIVE_SIM Canary post-trade linkage is clean enough, run the review-only Exit Policy Validation report to compare actual exits with shadow stop-loss, take-profit, trailing, time-exit, and context-risk exits.

```powershell
$headers = @{ "X-Local-Token" = "local-dev-token" }
Invoke-RestMethod `
  -Method Post `
  "http://127.0.0.1:8000/api/runtime/exit-policy/validation/rebuild" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"trade_date":"2026-06-17","persist":true,"export":"all"}'
```

Reports are written under:

```text
reports/exit_policy_validation/<trade_date>/
```

This follow-up report is analysis-only. It must not create sell/cancel/modify commands or write `strategy_runtime_settings`.

## Operator Review Guidance

Recommendations are review prompts only. Examples:

- If entry slippage is high, review `max_entry_slippage_bp` and limit-price policy in a later PR.
- If no-fill rate is high, review spread/liquidity at submission time.
- If time exits are weak, review `max_hold_minutes` only after enough samples.
- If reconcile or orphan cases exist, fix data linkage before changing strategy behavior.

## Minimum Sample Guidance

Do not use a single day or a small handful of cases to change settings. For a later settings PR, collect at least:

- 5 or more trading days.
- 30 or more completed/closed LIVE_SIM Canary lifecycles.
- Separate counts for `NO_FILL`, `PARTIAL_FILL`, and `RECONCILE_REQUIRED`.
- A stable explanation for `LIVE_WORSE_THAN_DRY_RUN`.

## Safety Notes

This feature is unrelated to LIVE_REAL activation. The rebuild/export API is analysis-only and must not call Gateway `send_order`, alter OrderGuard/SafetyGuard behavior, or write runtime strategy settings.
