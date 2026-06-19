# Reboot V2 Split-Market Relative Strength Shadow

This module records split-market relative-strength evidence without changing entry policy, candidate FSM state, position sizing, or order paths.

## Safety Boundary

- `WAIT_MARKET` remains `WAIT_MARKET`.
- `BLOCK_NEW_ENTRY` remains blocked.
- `EntryDecision.entry_status` is read only.
- No `OrderIntent`, DRY_RUN order, LIVE_SIM order, or LIVE order is created.
- Counterfactual multipliers are analysis-only fields.
- RISK_OFF-side diagnostics are never promotion eligible.

## Runtime

`MarketRelativeStrengthShadowRuntimePipeline` runs after `strategy_context` plus entry/dirty evaluation and before exit, risk, and order-manager stages. It persists only `strategy_decision_events` with:

- `action_type = MARKET_RELATIVE_STRENGTH_SHADOW`
- `gate_status = OBSERVE`
- `block_type = OBSERVE_ONLY`

Duplicate suppression uses a material state key over scenario, variant, status, market side, side regime, composite mode, action, role, theme state, price location, and relative-strength band.

## Scenarios

- `HEALTHY_SIDE_REDUCED`
- `COUNTERPART_DATA_DEGRADED_REDUCED`
- `WEAK_SIDE_STRICT_SHADOW`
- `RISK_OFF_SIDE_DIAGNOSTIC`
- `SYSTEMIC_RISK_EXCLUDED`
- `DATA_WAIT_EXCLUDED`

## Outcomes And Report

Use:

```powershell
python tools/build_market_relative_strength_report.py --trade-date YYYY-MM-DD --horizons 300,600,1200 --export-all
```

The report name is `split_market_relative_strength_outcomes`. It groups 5/10/20 minute MFE, MAE, and end return by scenario, variant, market side, side regime, composite mode, action, role, theme, price location, session phase, relative-strength band, theme score band, and data quality.

Recommendations are report-only:

- `NO_DATA`
- `INSUFFICIENT_SAMPLE`
- `WATCH_MORE`
- `DO_NOT_PROMOTE`
- `REVIEW_WEAK_SIDE_SMALL_CANARY`
- `REVIEW_HEALTHY_SIDE_MULTIPLIER_LATER`
- `RISK_OFF_OBSERVE_ONLY_NO_PROMOTION`

## Observe Startup

`tools/start_reboot_v2_observe.ps1` enables the shadow evaluator while keeping live/order flags disabled:

- `TRADING_MODE=OBSERVE`
- `TRADING_ALLOW_LIVE=0`
- `TRADING_SEND_ORDER_ALLOWED=false`
- `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=0`
- `TRADING_RUNTIME_ALLOW_LIVE_ORDERS=0`
- `TRADING_ORDER_MANAGER_ENABLED=0`
- `TRADING_ORDER_INTENT_ENABLED=false`
