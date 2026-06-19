# Intraday Theme Rotation and Leadership Handover

This document defines the Reboot V2 / Theme Core V3 intraday theme rotation hardening path. It is an observe-only extension to the existing Opening Theme Burst and RT-TLS flow. It does not add SetupRouter, OpportunityRanker, EntryPlan creation, RuntimeOrderIntent creation, virtual buy orders, Gateway `send_order`, Gateway `cancel_order`, threshold tuning, or position sizing changes.

## Operating Mode

Required safety posture:

```text
TRADING_MODE=OBSERVE
TRADING_RUNTIME_MODE=OBSERVE
TRADING_ORDER_MANAGER_ENABLED=0
TRADING_ORDER_INTENT_ENABLED=false
TRADING_SEND_ORDER_ALLOWED=false
TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS=0
TRADING_ALLOW_LIVE_SIM_ORDERS=0
ready_allowed=false
order_intent_allowed=false
live_order_allowed=false
```

Theme rotation outputs are stored and displayed as observation products only. `DATA_WAIT` is never treated as a pass just because a module is missing a result.

## Runtime Order

The intraday path extends the V2 cycle after Opening Burst:

```text
Gateway tick drain
MarketDataService update
hydration/base subscriptions
MarketRegime
Opening Burst
Intraday Discovery
ActiveSeedRegistry
TurnoverFlow
ThemeCohort
ThemeStateMachine
LeadershipRanker and Handover
StockRole
FocusedExpansion and ExpansionLease
CandidateBridge reconcile
CandidateHydrator
BestThemeContextResolver
StrategyContextAssembler
Dirty publishers and DirtyEvaluator
EntryEngine observe fallback only
Dashboard V2
```

## P0 Runtime Wiring Completion

The P0 runtime wiring makes Theme Core V3 consume persisted discovery and live snapshot state instead of relying on transient in-memory rows.

Completed contracts:

- Gateway `command_ack`, `command_failed`, `command_timeout`, and `command_expired` events with `purpose=intraday_turnover_seed` are consumed by `IntradayDiscoveryRuntimePipeline` before the generic market-data bridge.
- Intraday discovery batches and rows are persisted idempotently by `command_id` or by `(trade_date, session_phase, bucket)`.
- Failed, timed-out, expired, or empty discovery responses are stored as batches but do not create seed rows.
- `ThemeCoreV3RuntimePipeline` restores active seed, turnover-flow, and candidate-bridge source state from DB at runtime start.
- Active seed sources include realtime theme-membership ticks, opening turnover seed, intraday discovery seed, and optional condition include booster sources.
- Replayed raw seed rows do not extend TTL or keep historical max turnover/change values alive. The latest observation wins, and stale sources expire.
- Turnover flow persists minute observations and detects duplicate timestamps, out-of-order timestamps, and cumulative reset events.
- CandidateBridge include/remove state is reconciled from the restored active source state, so a restart does not re-emit the same include.
- Reboot V2 runtime publishes dirty codes from market-regime, theme-state, theme-leader, theme-role, and strategy-context changes before the dirty evaluator runs.
- All outputs remain observe-only: no `READY`, `EntryPlan`, `RuntimeOrderIntent`, virtual order, Gateway `send_order`, or Gateway `cancel_order` is produced by this path.

## Intraday opt10032 Discovery

`trading/theme_engine/intraday_discovery.py` schedules rolling `opt10032` turnover seed requests after the opening burst window.

Schedule:

```text
09:20-11:00 every 5 minutes
11:00-13:20 every 10 minutes
13:20-14:30 every 5 minutes
14:30-15:00 every 10 minutes
after 15:00 stop
```

Command contract:

- `type=tr_request`
- `purpose=intraday_turnover_seed`
- `response_mode=capture`
- `top_n=100` by default
- idempotent by trade date, session phase, and time bucket
- skipped when a pending TR seed command or queue depth limit exists
- routed through the Gateway command queue and rate limiter
- never creates order commands

The request uses the same opt10032 input contract as Opening Burst:

```text
시장구분=000
관리종목포함=0
거래소구분=3
```

## Active Seed Registry

`trading/theme_engine/signal_registry.py` keeps seed sources active only while they are fresh.

The registry is the source of truth for Theme Core V3 source availability. Core reads opening seed rows, intraday discovery rows, realtime theme-membership ticks, and condition include events as source deltas, merges them into the registry, then ranks themes from the active registry snapshot.

Tracked fields:

- `observed_at`
- `last_seen_at`
- `tick_at`
- `tick_age_sec`
- `freshness_status`
- `source_confirmation_count`
- `active`
- `expiry_at`

Freshness rules:

- `FRESH` and `DEGRADED` may contribute to realtime breadth.
- `STALE`, `TR_BACKFILL_ONLY`, and `MISSING` do not count as strong, leader, breadth, or recent flow evidence.
- Source removal removes only that source. It does not delete the candidate when other active sources remain.

Condition-search include is an optional booster or discovery source. It can raise priority or add a source, but it cannot directly produce `READY`, `EntryPlan`, order intent, or Gateway order command.

## Turnover Flow

`trading/theme_engine/turnover_flow.py` separates cumulative turnover from recent turnover flow.

Stock metrics:

- cumulative turnover
- 1 minute turnover delta
- 3 minute turnover delta
- 5 minute turnover delta
- 1 minute speed
- 3 minute speed
- acceleration
- flow percentile

Theme metrics:

- theme 1 minute delta
- theme 3 minute delta
- theme 5 minute delta
- flow share
- flow share delta
- flow percentile
- fresh flow coverage

Negative cumulative resets are clamped to zero for recent delta calculations.

## Cohort Coverage

Theme cohorts track both full-universe and sampled coverage:

- `universe_member_count`
- `tradable_member_count`
- `observed_member_count`
- `target_sample_count`
- `fresh_sample_count`
- `full_universe_coverage_ratio`
- `planned_sample_coverage_ratio`
- `breadth_trust_level`

Large themes should not remain permanently `DATA_WAIT` only because full-universe coverage is incomplete. If planned sampled coverage is sufficient, the theme can remain `WATCH_THEME` or proceed through normal state classification with sampled trust marked.

## Leadership Handover

`trading/theme_engine/leadership_handover.py` prevents one-cycle spikes from taking over the theme board.

Statuses:

- `NEUTRAL`
- `INCUMBENT`
- `CHALLENGER`
- `TAKEOVER_PENDING`
- `TAKEOVER_CONFIRMED`
- `LOSING_LEADERSHIP`
- `ROTATED_OUT`

Rules:

- A challenger must beat the incumbent by score and flow-share advantage.
- Takeover requires persistence by seconds or cycles.
- `DATA_WAIT`, stale, or weak themes cannot take over.
- An incumbent can enter `LOSING_LEADERSHIP` when recent flow collapses.

## Expansion Lease

`trading/theme_engine/expansion_lease.py` keeps realtime expansion subscriptions stable under theme churn.

Lease behavior:

- selected targets receive TTL and minimum-hold windows
- protected codes are retained
- first fresh tick is tracked separately
- removed themes enter removal pending only after minimum hold
- expired leases are removable

This protects the realtime registration set from rapid add/remove churn while still allowing rotation out when a theme is no longer eligible.

## Candidate Bridge Reconcile

`trading/theme_engine/candidate_bridge_reconciler.py` emits include and remove source events for Theme Core V3 source state.

Allowed include roles:

- `LEADER_CONFIRMED`
- `CO_LEADER_CONFIRMED`
- `FOLLOWER_ALLOWED`

Allowed theme states:

- `LEADING_THEME`
- `SPREADING_THEME`
- `LEADER_ONLY_THEME`

Blocked:

- `LATE_LAGGARD`
- `OVERHEATED`
- `WEAK_MEMBER`
- followers inside `LEADER_ONLY_THEME`

Remove events mean the Theme Core V3 source is no longer active. They do not assert that the whole candidate must be deleted if another source is still active.

The reconciler persists active bridge source state. On restart, Core restores that state before reconciling the current role decisions. This prevents repeated include events for unchanged leader/co-leader sources while still emitting remove events when a previously active source disappears.

## Dirty Publisher Wiring

Dirty publishers connect observation products to `DirtyStrategyEvaluator` without invoking the legacy full-scan path:

- `MarketRegimeDirtyPublisher` marks only codes for the changed market side after the initial publish.
- `ThemeStateDirtyPublisher` separates `THEME_STATE_CHANGED`, `THEME_LEADER_CHANGED`, and `THEME_ROLE_CHANGED`.
- `StrategyContextDirtyPublisher` marks a code only when its context id changes.
- The dirty queue is consumed by `DirtyStrategyEvaluator` in shadow/observe mode only.
- Dirty publishing does not authorize order intents or live orders.

## Best Theme Context

`trading/theme_engine/context_resolver.py` resolves multi-theme stocks to the most actionable current theme by freshness, theme state, trade role, leadership score, theme score, persistence, and role score.

This prevents a stock that belongs to several themes from being evaluated with stale or weaker context just because that theme appeared first in the board.

## Dashboard Direction

Dashboard V2 should expose:

- current intraday discovery phase and last opt10032 seed bucket
- active seed count and expired seed count
- stale/backfill source counts
- top 5 leading themes
- incumbent and challenger handover status
- top leader and co-leader per theme
- excluded late laggard count
- excluded overheated count
- condition booster inflow count
- candidate bridge include/remove counts
- expansion lease active, holding, removal pending, and expired counts

Legacy diagnostics may remain under debug or legacy views, but the default V2 dashboard should not show hybrid score, final grade, threshold A/B, shadow/promotion, raw condition-hit tables, or legacy ThemeLab diagnostics as primary panels.

## Verification

Focused tests:

```powershell
python -m pytest tests/test_intraday_theme_discovery.py -q
python -m pytest tests/test_active_seed_registry.py -q
python -m pytest tests/test_theme_turnover_flow.py -q
python -m pytest tests/test_theme_cohort_engine.py -q
python -m pytest tests/test_theme_leadership_handover.py -q
python -m pytest tests/test_theme_expansion_lease.py -q
python -m pytest tests/test_candidate_bridge_reconciler.py -q
python -m pytest tests/test_context_dirty_publisher.py -q
python -m pytest tests/test_best_theme_context_resolver.py -q
python -m pytest tests/test_theme_core_v3_runtime.py -q
python -m pytest tests/test_runtime_factory.py -q
python -m pytest tests/test_runtime_supervisor.py -q
```

Safety checks:

- zero condition profiles still allow `opt10032 seed + realtime snapshot + theme membership` theme ranking
- a single +7 percent stock is not enough to confirm a theme
- condition include is stored only as booster/discovery source
- condition include alone cannot create `READY` or order intent
- `LEADER_ONLY_THEME` admits only leader/co-leader roles
- stale, `DATA_WAIT`, `OVERHEATED`, VI, and upper-limit-near stocks do not become ready
- Gateway command queue contains no `send_order` or `cancel_order` commands
