# Reboot V2 MarketContextView Lightweight Transport

## Background

Reboot V2 needs split-market policy to reach StrategyContext without losing `candidate_policy_by_code`.
Passing the full MarketRegime snapshot to every downstream consumer fixed correctness, but it also made the Core runtime copy and serialize large payloads more often than needed.

The lightweight transport keeps correctness and separates consumer contracts:

- ThemeBoard receives a small `MarketContextSummary`.
- StrategyContext receives a read-only `MarketContextView`.
- Runtime/dashboard snapshots receive only a small `market_context_transport` diagnostic section.

## Transport Types

`MarketContextSummary` is the small ThemeBoard input. It contains:

- `trade_date`
- `calculated_at`
- `global_status`
- `kospi_status`
- `kosdaq_status`
- `composite_market_mode`
- `systemic_risk_off`
- session flags
- `reason_codes`
- `source`
- `schema_version`

It intentionally excludes:

- `candidate_policy_by_code`
- full `kospi_snapshot`
- full `kosdaq_snapshot`
- candidate code lists
- large diagnostics

`MarketContextView` is the StrategyContext input. It contains the summary, compact side views, and a read-only policy mapping. StrategyContext uses `policy_for(code)` for O(1) lookup and does not copy the full policy map per candidate.

## Source Priority

Reboot V2 resolves market context lazily in this order:

1. `PIPELINE_VIEW`: current MarketRegime pipeline `last_context_view`
2. `DB_FALLBACK`: latest MarketRegime DB snapshot
3. `DASHBOARD_SUMMARY_FALLBACK`: current runtime market summary
4. `UNAVAILABLE`: DATA_WAIT view

The fallback chain is lazy. When `PIPELINE_VIEW` is fresh and usable, DB snapshot lookup is not evaluated.

## Freshness

The setting `TRADING_MARKET_CONTEXT_VIEW_MAX_AGE_SEC` controls open-market freshness. Default: `30`.

A view is usable when:

- `trade_date` matches the current cycle date
- `calculated_at` exists
- required side/global statuses exist
- the current MarketRegime section is not `ERROR`
- if the market is open, age is less than or equal to the max age
- same-date `MARKET_CLOSED` snapshots are allowed

Next-day stale views are rejected.

## Single Serialization

For each real MarketRegime build:

1. typed `MarketRegimeSnapshot` is created
2. full snapshot is serialized once for DB storage
3. `MarketContextView` is built using typed objects and the serialized payload
4. dashboard payload reuses the serialized payload

`run_if_due()` interval skips reuse cached view and summary and do not call full snapshot serialization again.

## Legacy Adapter

Adapters support:

- typed MarketRegime snapshot
- full serialized MarketRegime mapping
- dashboard summary mapping
- older legacy mapping

If a dashboard summary has no policy map, StrategyContext falls back to side status policy calculation. UNKNOWN-side candidates remain DATA_WAIT and do not inherit global status.

## Diagnostics

Runtime snapshot includes only:

- `status`
- `source`
- `schema_version`
- `calculated_at`
- `age_sec`
- `trade_date_match`
- `usable`
- `policy_count`
- `build_ms`
- `full_snapshot_serialize_count`
- `db_fallback_count`
- `summary_fallback_count`
- `warning_codes`

StrategyContext summary can additionally report:

- `assembly_duration_ms`
- `assembled_count`
- `policy_lookup_count`
- `market_context_source`
- `market_context_policy_count`

## Benchmark

Run:

```powershell
python tools/benchmark_market_context_view.py
```

Outputs:

- `reports/performance/market_context_view/benchmark_market_context_view.json`
- `reports/performance/market_context_view/benchmark_market_context_view.md`

The benchmark compares the old full-payload copy path against the new view path for 50, 200, and 500 candidates.

## Functional Invariants

This PR does not change:

- MarketRegime thresholds
- relative-strength thresholds
- EntryDecision logic
- Candidate FSM
- position sizing
- exit policy
- portfolio limits
- order path flags

Split-market policy remains side-specific:

- healthy side in split market can be reduced
- weak/risk-off side waits or blocks
- systemic risk-off blocks all new entries
- UNKNOWN side remains DATA_WAIT

## Order Safety

The change is transport-only. It does not enable dry-run or live orders. OBSERVE/order safety flags remain controlled by existing runtime settings.
