# SetupRouter V3 OBSERVE

SetupRouter V3 is a read-only strategy layer for Theme Core V3. It classifies temporal setup shapes from Strategy Context V3, the latest EntryDecision, realtime ticks, and completed 1m candles.

It does not create READY, EntryPlan, OrderIntent, VirtualOrder, opportunity rank, position size, or gateway order commands.

## Scope

- Runtime profile: THEME_CORE_V3 / Reboot V2 only
- Schema: `setup_router_v3.observe.v1`
- Output mode: `OBSERVE`
- Default flag: `TRADING_SETUP_ROUTER_V3_ENABLED=false`
- Observe script: `tools/start_reboot_v2_observe.ps1` enables it explicitly

All observations must keep:

- `ready_allowed=false`
- `candidate_promotion_allowed=false`
- `opportunity_rank_allowed=false`
- `order_intent_allowed=false`
- `live_order_allowed=false`
- `recommended_position_size_multiplier=0`

## Inputs

- `CandidateStateContractService.snapshot(candidate)`
- Strategy Context V3 latest payload
- latest EntryDecision payload
- `MarketDataStore.latest_tick(code)`
- `CandleBuilder.completed_candles(code, 1)`
- `CandleBuilder.active_candle(code, 1)`
- previous SetupRouter observation
- optional Expansion Lease read model

The historical sequence uses completed candles only. The active candle is auxiliary current-trigger evidence and must not rewrite past setup structure.

## Status Separation

`shape_status` describes the price setup:

- `NOT_SEEN`
- `FORMING`
- `MATCHED`
- `INVALIDATED`
- `EXPIRED`
- `DATA_WAIT`

`context_status` describes market/theme/role/data eligibility:

- `ELIGIBLE`
- `WAIT`
- `BLOCKED`
- `DATA_WAIT`

`router_status` is the read-only combination:

- shape `MATCHED` + context `ELIGIBLE` -> `VALID_OBSERVE`
- shape `FORMING` + context `ELIGIBLE/WAIT` -> `PENDING`
- shape `MATCHED` + context `WAIT` -> `PENDING`
- shape `MATCHED` + context `BLOCKED` -> `CONTEXT_BLOCKED`
- missing/stale data -> `DATA_WAIT`
- invalidated setup -> `INVALIDATED`

## Setup Types

### LEADER_FIRST_PULLBACK

Requires a local peak followed by the first controlled pullback:

- min completed 1m candles: 3
- pullback range: 0.7% to 3.5%
- deep invalidation: 5.5%
- max below VWAP: 0.7%
- TTL target: 900 seconds

### VWAP_RECLAIM

Requires prior-below-VWAP evidence and then reclaim:

- prior below VWAP: at least 0.15%
- reclaim above VWAP: at least 0.05%
- max extension over VWAP: 1.5%
- invalidation below VWAP: 0.5%
- lookback: 5 completed 1m candles

Current price above VWAP alone is not enough.

### BREAKOUT_RETEST

Requires breakout first, then later retest:

- breakout buffer: 0.25%
- retest lower tolerance: 0.30%
- retest upper tolerance: 0.80%
- hold tolerance: 0.20%
- invalidation below breakout: 0.70%
- lookback: 5 completed 1m candles

`ALLOW_REDUCED` market action keeps this setup at `PENDING`; valid breakout retest observation is reserved for `ALLOW_NORMAL`.

## Data Wait Guard

SetupRouter returns `DATA_WAIT` when any of the following are missing or stale:

- Strategy Context V3
- fresh realtime tick
- fresh theme or market context
- source timestamps
- enough completed 1m candles
- non-backfill realtime price

TR backfill and realtime coverage gaps are evidence for data readiness, not setup validity.

## Runtime Placement

In `RebootV2Runtime.cycle()`:

1. Strategy Context V3
2. DirtyEvaluator / EntryEngine OBSERVE
3. SetupRouter V3 OBSERVE
4. Market Relative Strength Shadow
5. Position/Exit/OrderManager disabled observe path

SetupRouter is downstream of EntryDecision and upstream of dashboard/audit. It does not mutate Candidate FSM or EntryDecision.

## Storage

- `setup_router_runs`
- `setup_observations_latest`
- `setup_observation_transitions`
- `setup_router_primary_latest`

Latest observations are upserted by `(trade_date, candidate_instance_id, setup_type)`. Transitions are written only when status/shape/context changes and the fingerprint changes.

## API

- `/api/setup-router-v3/summary`
- `/api/setup-router-v3/latest`
- `/api/setup-router-v3/observations`
- `/api/setup-router-v3/transitions`
- `/api/setup-router-v3/candidates/{code}`

All endpoints are read-only and include safety flags.

## Dashboard

Dashboard V2 shows SetupRouter in a separate “Setup 유형 관찰” panel. It must not replace “진입 준비 관찰 후보”.

Columns:

- stock
- theme
- setup type
- shape/context
- entry alignment
- reason/price structure

## Audit

Run:

```powershell
python tools/audit_setup_router_v3.py --db data/trader.sqlite3 --trade-date YYYY-MM-DD
```

Outputs:

- `reports/setup_router_v3/<trade_date>/summary.json`
- `report.md`
- `type_counts.json`
- `transitions.json`
- `invalid_observations.json`
- `flip_analysis.json`
- `context_alignment.json`
