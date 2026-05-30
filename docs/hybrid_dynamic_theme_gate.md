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
