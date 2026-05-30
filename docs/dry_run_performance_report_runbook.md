# DRY_RUN Performance Report Runbook

## Purpose

PR-7 adds an analysis layer for DRY_RUN order intent data. It does not enable LIVE orders and does not create Gateway `send_order` commands. The report answers: if the strategy had actually placed the DRY_RUN entry and exit orders, what kind of result would have appeared?

## Lifecycle Linking

The analyzer links `runtime_order_intents` into a trade lifecycle using this priority:

1. Same `virtual_position_id`
2. Same `virtual_order_id`, upgraded to the matching virtual position when one exists
3. Same `trade_review_id`
4. Same `candidate_id + code + trade_date`
5. Otherwise `orphan_entry` or `orphan_exit`

Each lifecycle can contain one or more entry/buy intents, multiple exit/sell intents, one virtual position, exit decisions, and one latest trade review.

## Joined Data

The report reads:

- `runtime_order_intents`: entry/buy, exit/sell, safety/live safety, idempotency, score metadata
- `trade_reviews`: final status, max return/drawdown windows, existing false-positive/false-negative flags
- `virtual_positions`: realized return, hold time, max return/drawdown
- `exit_decisions`: TAKE_PROFIT, SUPPORT_LOSS, TIME_EXIT, TRAILING_STOP context

The existing `trade_reviews` flags are preserved. DRY_RUN false signal fields are reported separately because an intent-level diagnosis can differ from a review-level diagnosis.

## False Positive

DRY_RUN false positive means an entry intent existed and passed decision safety, but the outcome was weak or harmful. Default thresholds are configurable.

- `LIVE_WOULD_PASS_BUT_SUPPORT_LOSS`
- `LIVE_WOULD_PASS_BUT_DRAWDOWN`
- `LIVE_WOULD_PASS_BUT_NEGATIVE_RETURN`
- `DRY_RUN_ACCEPTED_BUT_NO_EXIT_AND_DRAWDOWN`
- `ENTRY_ACCEPTED_BUT_TIME_EXIT_WEAK`
- `LATE_CHASE_FALSE_POSITIVE`

## False Negative / Opportunity Loss

DRY_RUN false negative means an order was rejected or absent, but later return metrics show a missed opportunity.

- `LIVE_REJECTED_BUT_RALLIED`
- `DRY_RUN_REJECTED_BUT_RALLIED`
- `GATE_BLOCKED_BUT_RALLIED`
- `EXPIRED_BUT_RALLIED`
- `NO_ENTRY_INTENT_BUT_RALLIED`
- `SAFETY_REJECT_REASON_OPPORTUNITY_LOSS`

`live_would_pass` and `live_would_reject` compare the DRY_RUN decision with the LIVE safety guard result. A rejected-live order that later rallied is useful for diagnosing operational blockers such as gateway offline, orderable=false, or account configuration.

## Data Quality

The report includes counts and samples for:

- Entry intent without trade review
- Entry intent without virtual position
- Exit intent without entry intent
- Exit intent without exit decision
- Trade review without DRY_RUN entry intent
- Missing price or quantity
- Missing live safety / decision safety
- Missing horizon metrics
- Stale open position

## API

- `GET /api/runtime/performance/dry-run`
- `POST /api/runtime/performance/dry-run/rebuild?trade_date=YYYY-MM-DD&persist=true`
- `GET /api/runtime/performance/dry-run/reports`
- `GET /api/runtime/performance/dry-run/reports/{report_id}`
- `GET /api/runtime/performance/dry-run/export?trade_date=YYYY-MM-DD&format=json|csv|md|all`
- `GET /api/runtime/performance/dry-run/false-signals?type=false_positive|false_negative|opportunity_loss|all`

`rebuild` and `export` require the local token because they create stored reports or files.

## Dashboard

PR-10 adds paginated dashboard drilldowns:

- **DRY_RUN Order Intents** for entry/buy and exit/sell intent browsing.
- **DRY_RUN Performance Cases** for lifecycle-level performance rows.
- **False Signals** for false positive, false negative, and opportunity-loss rows.

Use filters to narrow by `trade_date`, `code`, `theme_name`, `strategy_name`, `side`, or `order_phase`. Click a row to open the detail drawer with compact fields and raw JSON. Summary cards continue to update through `/ws/dashboard`, while table pages fetch REST APIs independently.

## Export Location

Generated files are written under:

```text
reports/dry_run_performance/<trade_date>/
```

Examples:

- `dry_run_performance_2026-05-30.json`
- `dry_run_performance_2026-05-30.csv`
- `dry_run_performance_2026-05-30.md`

## Recommendations

Recommendations are intentionally phrased as review prompts, not automatic parameter changes. Examples:

- LATE_CHASE false positives: review chase-risk penalty
- LOW_BREADTH false negatives: consider WATCH instead of hard block
- Rejected-live rallies: inspect SafetyGuard/Gateway availability
- High SUPPORT_LOSS count: revisit entry price and stop policy

## Safety

This is an analysis-only PR. It never enables LIVE automation, never calls Kiwoom, and never enqueues Gateway `send_order`.
