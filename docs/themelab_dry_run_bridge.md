# ThemeLab DRY_RUN Lifecycle Bridge

This bridge connects ThemeLab scanner decisions to the existing DRY_RUN trading experiment loop:

ThemeLab gate decision -> Candidate lifecycle -> EntryPlanBuilder -> virtual order -> runtime DRY_RUN order intent -> virtual position/exit/review.

It is not LIVE enablement.

## Safety Boundaries

- The bridge never creates Gateway `send_order` commands.
- LIVE runtime orders remain disabled by the existing runtime/order safety layers.
- `OrderGuard`, `SafetyGuard`, and `live_safety` are not relaxed.
- Strategy thresholds are not changed.
- `LOW_BREADTH`, `DATA_INSUFFICIENT`, and `THEME_WEAK` are not relaxed.
- OBSERVE mode uses lifecycle and diagnostics only; buy intents require the existing DRY_RUN runtime order sink.

## Eligibility Mapping

ThemeLab decisions are mapped conservatively:

- `READY` with `GOOD_PULLBACK`, `PULLBACK_RECLAIM`, or `VWAP_RECLAIM`, and risk `PASS` or `RISK_ADJUST`
  becomes `READY_PULLBACK` and can create a pullback EntryPlan.
- `READY_SMALL` with `GOOD_PULLBACK` or `PULLBACK_RECLAIM`, role `LEADER` or `CO_LEADER`, and risk `PASS` or `RISK_ADJUST`
  becomes `READY_SMALL_PULLBACK`. The position multiplier is carried into split-leg weights.
- `CHASE_HIGH`, `BREAKOUT_CONTINUATION`, and `VWAP_OVEREXTENDED`
  become observe-only statuses and cannot create DRY_RUN buy intents.
- `DATA_INSUFFICIENT` or `INDICATOR_DATA_INSUFFICIENT`
  becomes `WAIT_DATA` and cannot create DRY_RUN buy intents.
- `WEAK_THEME` or `THEME_WEAK`
  becomes `BLOCK_THEME` and cannot create DRY_RUN buy intents.
- Other `WAIT` decisions remain temporary wait states.
- Other `BLOCKED` decisions remain blocked states.

## Entry Plans

The bridge reuses `EntryPlanBuilder`.

If support data is missing, the plan is diagnostic-only with `reason=support_missing`.
If the limit price is too far behind the current price, the plan is diagnostic-only with `reason=max_chase_exceeded`.
Diagnostic-only plans do not create virtual orders or DRY_RUN buy intents.

Runtime order intent metadata includes:

- `source=themelab_flow`
- code/trade date/candidate/theme fields
- lab and final gate status
- order eligibility
- price location and risk fields
- reason codes
- support and limit price fields
- split leg and weight fields

## Idempotency

ThemeLab entry intents use a stable key:

```text
themelab_flow:{trade_date}:{code}:{candidate_id}:{order_phase}:{leg_index}
```

This keeps repeated cycles from creating duplicate order intents for the same candidate and leg.

## Rollback

Two safe rollback paths are available:

- Set `theme_engine_mode="legacy"` to use the legacy gate pipeline.
- Set `theme_lab_dry_run_bridge_enabled=false` to keep ThemeLab scanner/dashboard behavior while disabling the lifecycle bridge.
