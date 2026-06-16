# Exit Policy Validation Report

## Purpose

Exit Policy Validation is a review-only Shadow Exit Policy A/B report for LIVE_SIM Hybrid READY Canary lifecycles. It asks one question after entry fill: which exit policy would have preserved or improved expectancy compared with the actual LIVE_SIM exit?

It does not enable LIVE_REAL, create sell/cancel/modify orders, relax Hybrid thresholds, or write `strategy_runtime_settings`.

## Data Sources

The analyzer reuses the LIVE_SIM Canary lifecycle linker and then adds post-entry price/context paths:

- `live_sim_canary_decisions`
- `runtime_order_intents`
- `live_sim_orders`
- `live_sim_fill_events`
- `live_sim_positions`
- `live_sim_cancel_orders`
- `exit_decisions`
- `trade_reviews`
- `gateway_price_ticks`
- `position_context_history`
- `hybrid_gate_validation_events`
- DRY_RUN performance lifecycle data

If post-entry tick/minute data is missing, the case is classified as `INSUFFICIENT_DATA`. Missing data is never converted to zero return.

## Shadow Scenarios

Default scenarios are analysis-only:

- `baseline_current`
- `tight_stop_fast_profit`
- `balanced_intraday`
- `wide_stop_theme_runner`
- `trailing_after_mfe`
- `partial_take_profit`
- `context_risk_fast_exit_aggressive`
- `context_risk_fast_exit_balanced`
- `context_risk_fast_exit_conservative`

The baseline reads `live_sim_exit_guard` when available and otherwise uses `stop_loss_pct=-2.0`, `take_profit_pct=5.0`, `max_hold_minutes=60`.

## APIs

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/exit-policy/validation?trade_date=2026-06-17"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/exit-policy/validation/scenarios?trade_date=2026-06-17"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/exit-policy/validation/cases?trade_date=2026-06-17&scenario_id=balanced_intraday"
```

Rebuild/export:

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

## Dashboard

The dashboard `Exit 정책 검증` panel shows:

- lifecycle sample count
- actual LIVE_SIM average net return
- best shadow scenario and expectancy
- stop-loss, take-profit, time-exit, trailing, giveback, and insufficient-data counts
- scenario summary table
- case comparison table with raw detail JSON

The panel must not include sell execution, cancel/modify, or settings-apply buttons. It displays analysis-only wording in Korean.

## Review Guidance

Recommendations and change proposals are evidence notes only:

- `auto_apply=false`
- `requires_operator_approval=true`
- no runtime config write
- no Gateway command creation

Use the output as the next PR candidate input only after enough comparable LIVE_SIM samples are available.
