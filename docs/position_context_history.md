# Position Theme/Market Context History

This PR adds DRY_RUN-only context history for open virtual positions. It improves context-risk exits by comparing entry and holding context against the current evaluation context instead of relying on the latest ThemeLab/index state alone.

## Snapshot Lifecycle

Context snapshots are stored in `position_context_history`.

- `ENTRY`: captured when a filled virtual order opens a virtual position.
- `HOLDING_EVAL`: captured during each open position evaluation cycle.
- `EXIT_EVAL`: captured immediately around exit evaluation for attribution and review.

The snapshot carries candidate, candidate instance, theme, leader, breadth, index, market, risk reason, and metadata fields. LIVE orders are not enabled and no Gateway `send_order` commands are created.

## Schema

Important fields:

- `position_id`, `candidate_id`, `candidate_instance_id`
- `code`, `trade_date`, `captured_at`, `capture_reason`
- `theme_id`, `theme_name`, `theme_score`, `theme_status`
- `leader_count`, `strong_count`, `breadth_status`
- `leader_code`, `leader_return_pct`, `leader_vwap_status`, `leader_support_broken`
- `index_market`, `index_status`, `index_return_pct`
- `market_status`, `market_risk_status`
- `risk_reason_codes_json`, `metadata_json`

## Exit Confidence

`ExitDecision.details` now includes before/current/delta fields:

- `theme_score_before/current/delta`
- `theme_status_before/current`
- `leader_count_before/current/delta`
- `strong_count_before/current/delta`
- `breadth_before/current`
- `index_status_before/current`
- `context_history_available`
- `context_history_count`
- `exit_confidence`

Context-risk decisions also preserve:

- `primary_exit_reason`
- `secondary_exit_reasons`
- `exit_reason_priority`
- `exit_reason_confidence`

## Hysteresis

- `MARKET_RISK_OFF_EXIT`: can trigger immediately because market-wide risk-off is a portfolio protection signal.
- `LEADER_COLLAPSE_EXIT`: can trigger immediately on hard leader VWAP/support break, otherwise requires at least prior context.
- `THEME_WEAK_EXIT`: requires two consecutive weak theme contexts.
- `INDEX_WEAK_EXIT`: can be partial or full depending on return and role; confidence improves with history.
- `BREADTH_COLLAPSE_EXIT`: prioritizes weaker roles such as late laggards.

When context is missing or too thin, context-risk-only forced exits are limited and marked through `DATA_LIMITED_CONTEXT` or `LOW_CONFIDENCE_EXIT` diagnostics. Existing hard stops such as support loss, trailing stop, time exit, and take profit continue to operate.

## Report Section

`dry_run_performance` now includes a `Position Context History` section.
It is diagnostic-only and does not change exit thresholds.

Reported fields:

- `positions_with_entry_context_count`
- `positions_with_holding_context_count`
- `positions_with_exit_context_count`
- `position_context_coverage_pct`
- `data_limited_context_count`
- `low_confidence_exit_count`
- `context_history_count_distribution`
- `context_risk_exit_confidence_distribution`
- `context_risk_exit_confidence_by_type`
- `theme_score_delta_distribution`
- `leader_count_delta_distribution`
- `index_status_deterioration_count`
- `market_risk_off_exit_count`

The confidence summary is split by context-risk exit type:

- `THEME_WEAK_EXIT`
- `LEADER_COLLAPSE_EXIT`
- `INDEX_WEAK_EXIT`
- `MARKET_RISK_OFF_EXIT`
- `BREADTH_COLLAPSE_EXIT`

`DATA_LIMITED_CONTEXT` and `LOW_CONFIDENCE_EXIT` remain report diagnostics.
They must not force exits by themselves when context history is thin.

## Retention

Raw context history can grow every runtime cycle, so it is pruned separately from
trade reviews, virtual positions, and dry-run performance reports.

Environment variables:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `TRADING_CONTEXT_HISTORY_PRUNE_ENABLED` | `1` | Enables raw context history pruning during runtime cycles. |
| `TRADING_CONTEXT_HISTORY_RETENTION_DAYS` | `20` | Keeps raw `position_context_history` rows newer than this cutoff. |
| `TRADING_CONTEXT_HISTORY_SUMMARY_RETENTION_DAYS` | `180` | Documents the intended retention horizon for prune summaries and reports. |
| `TRADING_CONTEXT_HISTORY_PRUNE_BATCH_SIZE` | `1000` | Maximum raw rows deleted per runtime cycle. |

Pruning targets only raw `position_context_history` rows. It does not delete
virtual positions, exit decisions, order intents, trade reviews, or persisted
dry-run performance reports.

Each prune run records:

- `pruned_context_history_rows`
- `retained_context_history_rows`
- `oldest_retained_context_at`
- `prune_error_count`

The latest prune summary is exposed in runtime snapshots and in the
`dry_run_performance` report under `context_history_prune`.

## Known Limitations

- Context history starts only after this PR is deployed; older positions may begin with limited history.
- Snapshot quality depends on ThemeLab payload, market index state, and leader metadata coverage.
- Virtual positions can still be netted by code/candidate, while attribution is tracked through `candidate_instance_id`.
- The table is additive and does not replace existing position accounting.
