# LIVE_SIM activation checklist

`LIVE_SIM` is a Kiwoom simulation-account path only. It is not a real LIVE order switch, and repository defaults must remain `DRY_RUN` with `live_sim_enabled: false`.

## Required checks before enabling

- Confirm the 32-bit Kiwoom gateway is connected to the simulation server.
- Confirm heartbeat payload is normalized to simulation mode:
  - `broker_env: SIMULATION`
  - `server_mode: SIMULATION` or `MOCK`
  - `account_mode: SIMULATION`
- Confirm `live_real_enabled: false`.
- Configure `allowed_account_numbers` with real account numbers only in local/private config. Do not commit full account numbers.
- Confirm `live_sim_exit_guard.enabled: true`.
- Confirm `live_sim_order_lifecycle.enabled: true`.
- Confirm `live_sim_reconcile.enabled: true`.
- Confirm exposure limits:
  - `max_order_amount_krw`
  - `max_position_amount_krw`
  - `max_total_exposure_krw`
- Confirm kill switch is configured and not active.
- Confirm DRY_RUN reports are still collected. Simulation results can differ from real market execution.

## Example local settings

```yaml
order_execution:
  mode: "LIVE_SIM"
  live_sim_enabled: true
  live_real_enabled: false
  require_simulated_account: true
  allowed_account_mode: "SIMULATION"
  allowed_account_numbers:
    - "****1234"
  block_real_account: true
  submit_first_leg_only: true
  max_orders_per_day: 5
  max_new_positions_per_day: 3
  max_order_amount_krw: 300000
  max_position_amount_krw: 300000
  max_total_exposure_krw: 1000000

live_sim_order_lifecycle:
  enabled: true
  cancel_unfilled_buy_after_sec: 60
  cancel_unfilled_sell_after_sec: 60
  cancel_partial_remainder_after_sec: 90
  cancel_check_interval_sec: 5
  max_cancel_attempts: 2
  block_new_order_when_cancel_pending: true

live_sim_exit_guard:
  enabled: true
  stop_loss_pct: -2.0
  take_profit_pct: 5.0
  max_hold_minutes: 60
  exit_check_interval_sec: 3
  require_latest_tick_ready_for_exit: true
  market_close_liquidation_enabled: true

live_sim_reconcile:
  enabled: true
  reconcile_on_startup: true
  reconcile_on_reconnect: true
  reconcile_interval_sec: 30
  block_new_buy_on_reconcile_failure: true
```

## Fail-closed rules

New `LIVE_SIM` buys are blocked when any of these are true:

- exit monitor is unhealthy
- cancel scheduler is unhealthy
- pending cancel exists for the same code/account
- pending buy exists for the same code/account/candidate
- `UNKNOWN_SUBMIT` exists
- order or position is `RECONCILE_REQUIRED`
- external position is detected by reconcile
- reconcile failure limit is reached
- kill switch is active
- broker is disconnected or heartbeat is stale
- broker environment is `REAL` or `UNKNOWN`
- support/tick/late-chase/market/CHASE_RISK gates are not ready
- `LIVE_REAL` mode is requested

## Broker environment mapping

The order guard consumes normalized heartbeat fields only:

- `broker_name`
- `broker_env`: `SIMULATION`, `REAL`, `UNKNOWN`
- `server_mode`: `MOCK`, `SIMULATION`, `REAL`, `UNKNOWN`
- `account_mode`: `SIMULATION`, `REAL`, `UNKNOWN`
- `heartbeat_ok`
- `heartbeat_at`
- `raw_payload`

If the Kiwoom gateway uses different raw field names, map them in the gateway/broker adapter before they reach the runtime. Unknown values must stay `UNKNOWN` and fail closed.
