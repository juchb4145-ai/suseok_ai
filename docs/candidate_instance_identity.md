# Candidate Instance Identity

This PR keeps the existing `candidates` table shape and `UNIQUE(trade_date, code)` behavior, but adds signal attribution identity into metadata so DRY_RUN lifecycle analysis does not merge separate same-day signals for the same stock.

## Identity Fields

- `candidate_instance_id`: stable signal-instance key for one candidate generation.
- `candidate_generation_seq`: generation number within the same `trade_date + code`.
- `decision_cycle_id`: runtime evaluation cycle key tying lifecycle, diagnostics, EntryPlan, and order intent together.

The generated `candidate_instance_id` uses:

- `trade_date`
- normalized `code`
- `source`
- `strategy_name`
- `theme_id`
- `candidate_instance_first_seen_at`
- `candidate_generation_seq`

Format:

```text
ci:{trade_date}:{code}:{candidate_generation_seq}:{digest}
```

## Generation Increase Rules

A new generation is created when an existing same-day candidate is detected again and one of these conditions applies:

- `theme_id` changed
- source changed
- strategy profile changed
- the previous detection is stale beyond the configured generation gap

The default stale re-detect gap is 90 minutes. The implementation is intentionally conservative: routine same-cycle refreshes keep the same generation.

## Propagation

The identity is carried through metadata/details for:

- candidate `metadata`
- candidate event payload
- gate decision details
- EntryPlan cancel condition
- runtime order intent metadata
- virtual order details
- virtual position details
- exit decision details
- trade review details

Virtual positions may still represent net code-level accounting. Signal attribution remains separated by `candidate_instance_id`, and aggregated positions preserve `candidate_instance_ids`.

## Lifecycle Attribution Priority

DRY_RUN performance linking prefers stronger exact links before weak fallback:

1. `virtual_position_id`
2. `virtual_order_id -> virtual_position_id`
3. `trade_review_id`
4. `candidate_instance_id`
5. `candidate_id`
6. `trade_date + code + time_window + source + strategy_name`
7. `trade_date + code`

Weak fallback is preserved for legacy/sparse rows, but it records `matched_by` and `link_confidence`.

## Weak Fallback Policy

- `weak_code_date_fallback` uses `link_confidence=LOW`.
- If multiple candidate instances exist for the same `trade_date + code`, code-date fallback is not merged.
- Ambiguous rows are classified as `AMBIGUOUS_CANDIDATE_LINK`.
- The performance report exposes attribution quality counts, including weak fallback and ambiguity.

## Known Limitations

- The candidates table still stores one row per `trade_date + code`; generation history is metadata/event based in this PR.
- Virtual position accounting can be code-netted, so a single position may contain multiple `candidate_instance_ids`.
- Old rows without identity fields can still require weak fallback. Those rows should not be used for threshold tuning without reviewing attribution quality.
- `decision_cycle_id` is a runtime-cycle attribution key, not an order execution or LIVE trading identifier.

This is a DRY_RUN analysis hardening change only. It does not enable LIVE orders and does not create Gateway `send_order` commands.
