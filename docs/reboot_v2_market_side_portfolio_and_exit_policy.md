# Reboot V2 Market-Side Portfolio Budget And Position Policy

## Scope

P2 keeps the Reboot V2 path in OBSERVE mode. It adds market-side portfolio budgets and position market actions, but does not enable live, dry-run, or gateway orders.

## Safety Contract

- P1 market relative strength shadow output is analysis-only.
- `promotion_eligible` is deprecated and always `false`.
- `shadow_filter_passed` and `review_candidate` may be used for review queues only.
- `BLOCK_NEW_ENTRY` blocks new entries only. It is not a sell signal.
- ExitEngine reads `PositionMarketAction`, not candidate market entry action, for market-driven position handling.

## Runtime Order

Recommended Reboot V2 order:

1. MarketRegime
2. ThemeBoard
3. StrategyContext
4. Dirty/Entry
5. P1 shadow
6. PositionRuntime/PositionRisk
7. ExitEngine
8. OrderManager

PositionRisk runs before ExitEngine so ExitEngine can consume the latest position action snapshot.

## Market-Side Portfolio Budget

`PortfolioRiskSnapshot` now includes gross exposure, pending buy reservation, market-side budgets, side stop-new-entry flags, composite mode, systemic risk, and market context freshness.

Each `MarketSidePortfolioBudget` tracks:

- side and counterpart regimes
- base/effective exposure limit
- open, pending, reserved, and available exposure
- utilization
- open and pending position counts
- available slots
- `ALLOW_BUDGET`, `REDUCED_BUDGET`, `STOP_NEW_ENTRY`, `DATA_WAIT`, or `MARKET_CLOSED`

Pending BUY reservation includes managed order intents/orders in approved, local-created, observe-blocked, queued, gateway-acked, and partially-filled states. SELL is not included.

## Position Market Action

`PositionRiskSnapshot` now emits `PositionMarketAction`:

- `HOLD`
- `TIGHTEN_STOP`
- `SCALE_OUT`
- `EXIT_IF_LOSER`
- `EXIT_NOW`
- `DATA_WAIT`

Healthy-side positions hold even when the counterpart market is weak. Stale, missing, or unresolved market context becomes `DATA_WAIT` and does not create a sell intent.

## OrderRisk

BUY checks the latest market-side budget when `TRADING_MARKET_SIDE_PORTFOLIO_ENABLED=1`.

With `TRADING_MARKET_SIDE_PORTFOLIO_ENFORCE_BUY_LIMITS=0`, market-side budget violations are recorded as diagnostics only.

With `TRADING_MARKET_SIDE_PORTFOLIO_ENFORCE_BUY_LIMITS=1`, BUY can be rejected for side/gross exposure limits, side position limits, stop-new-entry, data wait, or unknown side. SELL is not blocked by new-entry budget limits.

## Observe Script Defaults

`tools/start_reboot_v2_observe.ps1` enables:

- `TRADING_POSITION_RISK_ENABLED=1`
- `TRADING_EXIT_ENGINE_ENABLED=1`
- `TRADING_MARKET_SIDE_PORTFOLIO_ENABLED=1`
- `TRADING_MARKET_SIDE_PORTFOLIO_OBSERVE_ONLY=1`
- `TRADING_MARKET_SIDE_PORTFOLIO_ENFORCE_BUY_LIMITS=0`

It preserves the disabled order path:

- `TRADING_MODE=OBSERVE`
- `TRADING_ALLOW_LIVE=0`
- `TRADING_SEND_ORDER_ALLOWED=false`
- `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=0`
- `TRADING_RUNTIME_ALLOW_LIVE_ORDERS=0`
- `TRADING_ORDER_MANAGER_ENABLED=0`
- `TRADING_ORDER_INTENT_ENABLED=false`
- `TRADING_ALLOW_LIVE_SIM_ORDERS=0`
- `TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS=0`
- `TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS=0`
- `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND=false`
