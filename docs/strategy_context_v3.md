# Strategy Context V3

Strategy Context V3 is the Reboot V2 contract that unifies market, theme, stock-role, data, and risk inputs before EntryEngine evaluation. It is not a new trading strategy and does not enable orders.

## Runtime Order

The V2 observe cycle uses this order:

```text
Gateway price tick drain
MarketDataService update
hydration ACK/failure merge
base realtime subscriptions
MarketRegime
Opening Burst
Theme Core V3
FocusedExpansion realtime subscription reconcile
CandidateBridge confirmed event ingest
CandidateHydrator enqueue/retry
Candidate realtime subscription reconcile
StrategyContextAssembler
DirtyStrategyEvaluator / EntryEngine
ExitEngine / PositionRisk when open position exists
Dashboard snapshot
```

Theme Core V3 receives the current MarketRegime snapshot in the same cycle. In the V2 runtime path, missing market context is `DATA_WAIT` with `MARKET_CONTEXT_NOT_READY`; it must not fall back to a static `SELECTIVE` phase.

## Snapshot Contract

`StrategyContextSnapshot` is stored under `candidate.metadata["strategy_context_v3"]` and persisted to:

- `strategy_context_latest`
- `strategy_context_snapshots`

Main sections:

- `market`: side/global regime, market action, risk score, breadth, index slope
- `theme`: theme state, transition, score delta, persistence, leader stability
- `stock`: raw role, trade role, turnover, momentum, overheat/VI flags
- `data`: realtime freshness, candle readiness, VWAP/high-low readiness
- `risk`: market/theme/role/overheat/stale-data blocks

The safety flags remain fixed:

```text
ready_allowed=false
order_intent_allowed=false
live_order_allowed=false
```

## Candidate Bridge

`TRADING_THEME_CORE_V3_INGEST_CANDIDATES=true` is allowed in V2 observe because it only inserts or updates Candidate FSM discovery/watch context. CandidateBridge must not create:

- `SETUP_READY`
- `TIMING_READY`
- `READY`
- `EntryPlan`
- `OrderIntent`
- `GatewayCommand`

Allowed bridge inputs are confirmed theme/role combinations such as leading/spreading leader and co-leader. Blocked roles and weak/data-wait themes remain excluded.

## EntryEngine Cutover

V2 runtime uses:

```text
TRADING_ENTRY_USE_STRATEGY_CONTEXT_V3=true
TRADING_ENTRY_ALLOW_LEGACY_THEME_CONTEXT_FALLBACK=false
```

When StrategyContext V3 is missing, EntryEngine returns `DATA_WAIT` with `STRATEGY_CONTEXT_V3_MISSING` instead of reading legacy `theme_board_*` metadata.

## Persistence

Theme state persistence is same-trade-date only:

- `theme_state_runtime_latest`
- `theme_state_transitions`

Core restart restores same-day `persistence_count` and `leader_stability_count`; previous trading-day state is not carried into today.

## Observe Safety

Still disabled in this PR:

- DRY_RUN order intents
- LIVE_SIM orders
- REAL orders
- Gateway `send_order` / `cancel_order`
- EntryPlanBuilder and legacy VirtualOrder path
- threshold or theme-score tuning

## Verification

```powershell
python -m pytest tests/test_strategy_context_v3.py -q
python -m pytest tests/test_market_regime.py -q
python -m pytest tests/test_theme_core_v3_runtime.py -q
python -m pytest tests/test_theme_expansion_planner.py -q
python -m pytest tests/test_candidate_fsm.py -q
python -m pytest tests/test_entry_engine.py -q
python -m pytest tests/test_dirty_strategy_evaluator.py -q
python -m pytest tests/test_reboot_v2_runtime_cutover.py -q
python -m pytest tests/test_dashboard_v2_snapshot.py -q
python -m pytest -q
```
