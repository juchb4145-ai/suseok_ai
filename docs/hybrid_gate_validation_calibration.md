# Hybrid Gate Validation & Calibration

## Why This Validates Hybrid Itself

The old static `theme_mappings.csv` path is retired, and the legacy theme
repository is not a fallback. The previous entry gates are now component inputs
inside the hybrid decision. For that reason PR-DYN-04 does not make old/new gate
comparison the main report. The source of truth for validation is the
`HybridGateDecision` itself:

- `READY`
- `WAIT`
- `BLOCKED`
- `OBSERVE`

The validation question is simple: did each hybrid status match the stock's
later intraday behavior?

## Validation Event

Each hybrid decision can be persisted as a `HybridValidationEvent` in
`hybrid_gate_validation_events`. The event stores the status, score, position
tier, reason codes, dynamic theme metrics, stock leadership metrics, entry
timing score, market score, and risk score.

`legacy_result` is not a required field. When present it is kept only inside
`details_json` for debugging.

## Outcome Labels

Outcome labeling links a validation event to later minute candles.

- `good_ready`: READY followed by at least the configured favorable return with
  controlled MAE.
- `bad_ready`: READY followed by poor upside and large adverse excursion.
- `good_block`: BLOCKED followed by weak upside or meaningful downside.
- `false_block`: BLOCKED followed by a strong upside move.
- `good_wait`: WAIT followed by a better pullback/rebound or risk-avoiding
  breakdown.
- `missed_wait`: WAIT followed by immediate breakout.
- `observe_to_ready_opportunity`: WATCH/OBSERVE later behaved like an
  opportunity.
- `insufficient`: not enough minute data; excluded from averages.

Thresholds are runtime settings, not hardcoded policy:

- `hybrid_validation.good_ready_return_threshold`
- `hybrid_validation.bad_ready_mae_threshold`
- `hybrid_validation.false_block_return_threshold`
- `hybrid_validation.wait_missed_return_threshold`
- `hybrid_validation.outcome_windows`

## Status Performance

`Hybrid Status Performance` groups READY, WAIT, BLOCKED, and OBSERVE by:

- count
- average max return at 5, 10, 25, and 60 minutes
- average 25-minute MAE
- 25-minute win rate
- good/bad label counts
- insufficient data count

Missing outcome data is never treated as zero.

## Reason Code Performance

`Hybrid Reason Code Performance` shows whether each rule is doing useful work.
Core reason codes include:

- `LOW_BREADTH`
- `LEADER_ONLY_THEME`
- `LEADER_ONLY_THEME_LAGGARD_BLOCK`
- `LATE_LAGGARD`
- `CHASE_RISK`
- `LOW_MEMBERSHIP_SCORE`
- `NO_ACTIVE_THEME`
- `THEME_CONTEXT_NOT_READY`
- `STRONG_ACTIVE_THEME`
- `WATCH_THEME_EARLY`
- `STRONG_THEME_ENTRY_NOT_READY`

Each row includes return, MAE, win rate, false block rate, bad ready rate,
sample stocks, and a plain recommendation.

## Score Bands

Theme score bands:

- `0_50`
- `50_65`
- `65_75`
- `75_85`
- `85_100`

Membership score bands:

- `0_0_55`
- `0_55_0_65`
- `0_65_0_80`
- `0_80_1_00`

These bands validate whether `hybrid_min_ready_score`, `min_membership_score`,
and the 0.65 trade-eligible boundary are too strict or too loose.

## WATCH Small Entry Shadow Test

WATCH themes remain observe-only by default. The validation report simulates:

- Policy A: WATCH remains OBSERVE only.
- Policy B: WATCH leader/co-leader with high entry timing and low chase risk can
  become a small-first-entry shadow candidate.
- Policy C: WATCH with positive `rank_delta_5m` and improving breadth can become
  a small-first-entry shadow candidate.

This is a shadow simulation only. It does not change live order behavior.

## WAIT Quality

`Hybrid WAIT Quality` checks whether WAIT avoided bad entries or missed a fast
move:

- better pullback then rebound
- immediate breakout
- breakdown/risk avoided
- average time to better entry
- `good_wait`
- `missed_wait`

This tells us whether entry timing thresholds are helping or holding back good
setups.

## Calibration Recommendations

The report emits a recommendation JSON such as:

```json
{
  "trade_date": "2026-05-30",
  "auto_apply": false,
  "recommendations": {
    "hybrid_min_ready_score": {
      "current": 75,
      "recommended": 72,
      "reason": "65_75 theme_score band showed positive 25m return with controlled MAE",
      "confidence": 0.62,
      "low_sample_size": false
    }
  }
}
```

Recommendations are never auto-applied. `hybrid_validation.calibration_auto_apply`
defaults to `false`, and this PR keeps it that way. The recommendation file is
for operator review after enough observe-only samples have accumulated.

## Report Outputs

Use `HybridValidationReportExporter` to generate:

- `reports/hybrid_gate_validation_YYYYMMDD.csv`
- `reports/hybrid_gate_validation_YYYYMMDD.json`
- `reports/hybrid_gate_validation_YYYYMMDD.md`
- `reports/hybrid_calibration_recommendations_YYYYMMDD.json`

The Markdown report contains:

1. Hybrid Gate Validation Summary
2. Hybrid Status Performance
3. Hybrid Reason Code Performance
4. Theme Score Band Performance
5. Membership Score Band Performance
6. WATCH Theme Small Entry Policy Review
7. WAIT Quality Review
8. High Score Failure Cases
9. False Block Candidates
10. Calibration Recommendations

## Next Live Procedure

The next live-step PR should use multiple trading days of validation events,
then review:

- READY win rate and bad-ready rate
- BLOCKED false-block rate
- WAIT missed-wait rate
- WATCH Policy B/C risk
- LOW_BREADTH and LEADER_ONLY_THEME after-outcomes
- membership threshold behavior for new or low-source-count themes

Only after that should a user-approved configuration change move any hybrid
result from observe-only into live gate behavior.
