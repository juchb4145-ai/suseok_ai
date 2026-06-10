# Hybrid Dynamic Theme Gate

## Purpose

`PR-DYN-03` keeps the dynamic theme engine as the theme source of truth and
combines it with the existing entry timing gates. The goal is not to let a
strong theme override a bad entry, and not to let a clean pullback promote a
weak or stale theme. The final live order path is unchanged by default because
the new hybrid result runs in `observe_only` mode.

The retired `theme_mappings.csv`, static `ThemeRepository`, and CSV import
fallback remain out of the runtime path.

## Hybrid Score

The hybrid score is weighted, not a simple average:

```text
hybrid_score =
  0.30 * dynamic_theme_score
+ 0.20 * stock_leadership_score
+ 0.25 * entry_timing_score
+ 0.15 * market_session_score
+ 0.10 * risk_liquidity_score
```

Default thresholds:

- `hybrid_min_ready_score = 75`
- `hybrid_min_small_entry_score = 65`
- `min_membership_score = 0.55`
- `min_theme_breadth = 0.35`
- `max_rank_in_theme_for_ready = 5`

## Hard Guard vs Soft Score

Hard guards cannot be compensated by score. A market hard block, stale theme,
low membership, weak relation, or late-laggard condition blocks the hybrid
decision even if the weighted score is high.

Soft score components still matter for ranking and readiness:

- dynamic theme strength, breadth, rank, and reason codes
- stock rank inside the theme and membership quality
- pullback/support/volume confirmation
- market session condition
- chase and liquidity risk

## Status

- `READY`: ACTIVE theme, eligible member, good leadership, and confirmed entry
  timing.
- `WAIT`: theme is strong but entry timing is not ready, entry is good but
  theme quality is weak, breadth is low, or chase risk is high.
- `BLOCKED`: hard guard, STALE theme, low membership, weak relation,
  non-eligible member, leader-only laggard, or late laggard.
- `OBSERVE`: CANDIDATE/WATCH theme or low confidence setup that should be
  tracked without changing live gate behavior.

## WATCH Themes

WATCH themes are not promoted to normal first entry by default. A WATCH theme
leader with a clean first pullback can produce `observe_only`; if
`watch_theme_allows_small_entry=true`, it can produce `small_first_entry` for a
future live experiment.

## WatchSet Hysteresis

ThemeLab keeps recently demoted WatchSet symbols for a short configurable
window to reduce Kiwoom realtime subscription churn. A symbol that was promoted
by CONDITION2/CONDITION3 and then falls back to an alive-only signal remains in
the WatchSet as `watchset_retained=true`, but its gate remains conservative
(`OBSERVE`/`WAIT`) unless it qualifies again.

Runtime knobs:

- `TRADING_THEME_LAB_WATCHSET_RETAIN_CYCLES` defaults to `2`.
- `TRADING_THEME_LAB_WATCHSET_RETAIN_MIN_CONDITION_LEVEL` defaults to `1`.

## Price Location Readiness

ThemeLab separates price-location status from readiness. The status still says
what the setup looks like (`GOOD_PULLBACK`, `CHASE_HIGH`, `UNKNOWN`, etc.), while
readiness explains whether the price-location inputs are reliable enough:

- `READY`: enough price, VWAP, support, momentum, and minute-bar context.
- `PROVISIONAL`: early-session context exists, but uses active/incomplete 1m
  support or very few completed bars.
- `WARMUP`: price exists, but minute bars, VWAP, support, or momentum are still
  warming up.
- `MISSING_CORE`: a core input such as current price, return pct, role, theme,
  market, or session high is missing.
- `STALE`: quote or minute-bar data is stale.

This is observability-first. `PROVISIONAL` does not automatically promote a
candidate to normal `READY`; it gives replay and reason-performance reports a
clean way to measure early-session opportunity loss before small-entry policy is
expanded.

## Outcome Tracking Subscriptions

ThemeLab uses a separate realtime source, `theme_lab_outcome_tracking`, for
symbols that were delayed by data/readiness reasons worth measuring later. This
source is not a buy signal. It keeps ticks flowing for a short TTL after a symbol
falls out of the active WatchSet so 장후 replay and gate-reason performance can
answer questions such as "did a WARMUP or PROVISIONAL candidate surge 5 or 15
minutes later?"

Tracked cases include price-location `WARMUP`/`PROVISIONAL`/`MISSING_CORE`,
explicit `PRICE_LOCATION_*` reasons, market-side wait reasons, side-breadth data
quality waits, and recently demoted WatchSet symbols.

Runtime knobs:

- `TRADING_THEME_LAB_OUTCOME_TRACKING_TTL_SEC` defaults to `1800`.
- `TRADING_THEME_LAB_OUTCOME_TRACKING_MAX_CODES` defaults to `40`.

The subscription priority is below `theme_lab_watchset` and
`theme_lab_bootstrap`, and above broad candidate/theme-universe discovery. It is
non-protected, so protected Kiwoom OpenAPI streams such as index, holdings, and
virtual order/position monitoring still win when the realtime code limit is
tight.

## PROVISIONAL Small Entry Shadow

Gate-reason outcome reports include a shadow-only small-entry policy for
`PROVISIONAL` price-location candidates. This does not submit orders and does not
change live eligibility. It asks whether a very small entry would have improved
opportunity capture for early-session candidates that were held back by
incomplete but usable price-location data.

Default shadow eligibility:

- `price_location_readiness = PROVISIONAL`
- `final_gate_status` is `WAIT` or `OBSERVE`
- `stock_role` is `LEADER` or `CO_LEADER`
- `condition_level >= 2`
- `risk_level` is `PASS` or `RISK_ADJUST`
- candidate market status is not `RISK_OFF`

The report records `shadow_small_entry_candidate`, 15-minute win/risk flags,
average 15-minute MFE/MAE, and a missed-opportunity reduction estimate. The
default simulated position multiplier is `0.25`.

The same report also includes `shadow_small_entry_ab`, an A/B calibrator that
compares conservative and broader PROVISIONAL small-entry filters:

- `LEADER` only vs `LEADER` + `CO_LEADER`
- `condition_level >= 3` vs `condition_level >= 2`
- `PASS` only vs `PASS` + `RISK_ADJUST`
- `HEALTHY` market only vs all non-`RISK_OFF` markets
- simulated position multipliers `0.10`, `0.15`, and `0.25`

Each scenario reports candidate count, labeled sample count, 15-minute win rate,
15-minute risk-case rate, average MFE/MAE, scaled return/risk by simulated
position size, missed-opportunity capture, and a recommendation label. Low
sample scenarios remain `INSUFFICIENT_SAMPLE`; they should not be promoted to
dry-run order candidates until enough trading days accumulate.

## LEADER_ONLY_THEME

`LEADER_ONLY_THEME` means the theme move is concentrated in one stock. The
hybrid gate blocks non-leader and late-laggard candidates by default because
they are usually chase-prone followers rather than real theme breadth.

## Position Tier

- `none`: no entry tier.
- `observe_only`: record the setup but do not use it for live entry.
- `small_first_entry`: future small pilot entry tier for WATCH leaders.
- `normal_first_entry`: normal READY tier for ACTIVE themes.
- `blocked`: blocked by hard rule or hybrid quality rule.

## Observe-Only Operation

Defaults:

```text
hybrid_gate_enabled = true
hybrid_gate_observe_only = true
```

With observe-only enabled, `GatePipeline` keeps the existing final decision and
adds hybrid fields to result details:

- `hybrid_status`
- `hybrid_score`
- `hybrid_position_tier`
- `hybrid_primary_reason`
- `hybrid_reason_codes`
- `dynamic_theme_id`
- `dynamic_theme_name`
- `dynamic_theme_status`
- `dynamic_theme_score`
- `dynamic_theme_rank`
- `theme_breadth`
- `leader_gap`
- `top3_concentration`
- `rank_in_theme`
- `membership_score`
- `entry_timing_score`
- `chase_risk`
- `hybrid_observe_only`

If `hybrid_gate_observe_only=false`, the pipeline can use the hybrid result as
the final gate output. This is intended for the next PR's A/B validation, not
for immediate live order changes.

## Report Interpretation

The review exporter adds `Hybrid Gate Summary` with:

- READY candidates missed by the existing live gate
- existing live READY candidates that hybrid would block
- top WAIT reasons
- leader-only blocked candidates
- WATCH small-entry candidates
- theme score buckets
- membership score buckets

These sections are for offline comparison and threshold tuning.

## Next PR Live Procedure

`PR-DYN-04` should compare observe-only hybrid output against current gate
results before enabling live behavior:

- persist `old_result` and `hybrid_result`
- compare returns by `hybrid_status` and `position_tier`
- test score thresholds by theme status
- measure `LEADER_ONLY_THEME`, `LOW_BREADTH`, and `LATE_LAGGARD` outcomes
- decide whether `small_first_entry` is allowed for WATCH themes
- only then wire hybrid output into live buy eligibility
