# Support Data Quality

This PR expands support-missing diagnostics. It does not allow buy intents when
support is missing, does not promote `DATA_INSUFFICIENT` to `READY`, does not
change thresholds, and does not enable LIVE or Gateway orders.

## Support Missing Taxonomy

- `SUPPORT_STRUCTURALLY_MISSING`: support metadata exists, but no usable support candidate is available.
- `SUPPORT_DATA_MISSING`: support, VWAP, and minute-bar metadata are absent.
- `SUPPORT_NOT_READY`: a support candidate exists but readiness checks are not satisfied.
- `SUPPORT_STALE_VWAP`: VWAP exists but is stale.
- `SUPPORT_LOW_CONFIDENCE`: support exists, but bar quality is weak.
- `SUPPORT_SOURCE_UNAVAILABLE`: configured support source coverage is unavailable.

All of these remain diagnostic-only for entry. They must not create buy intents
unless a later PR explicitly changes strategy behavior.

## Support Sources

Coverage is tracked for:

- `recent_support_price`
- `support_price`
- `recent_swing_low`
- `opening_range`
- `prev_day_level`
- `vwap`
- `base_line_120`
- `envelope_mid`
- `day_mid`
- `ema20_5m`
- `manual_support`

## Minute Bar Quality

Minute-bar status is reported as:

- `VALID_RECENT_MINUTE_BARS`
- `LOW_RECENT_BAR_COUNT`
- `STALE_MINUTE_BARS`
- `MISSING_1M_BARS`
- `MISSING_3M_AGGREGATION`
- `MISSING_5M_AGGREGATION`
- `INSUFFICIENT_WARMUP_BARS`

Session-aware warmup policy:

- Early session: 3 or more recent 1m bars can be enough for diagnostics.
- Midday: 10 or more recent 1m bars is expected, with 3m/5m aggregation coverage.
- Late session: stale bar checks are stricter.

## Report Fields

`dry_run_performance` exposes a `Support/VWAP Coverage` section:

- `support_metadata_coverage_pct`
- `vwap_metadata_coverage_pct`
- `minute_bar_coverage_pct`
- `support_missing_count_by_reason`
- `support_source_distribution`
- `stale_vwap_count`
- `diagnostic_only_due_to_support_count`
- `diagnostic_only_later_rallied_count`
- `SUPPORT_STRUCTURALLY_MISSING_AND_RALLIED`
- `SUPPORT_DATA_MISSING_AND_RALLIED`
- `SUPPORT_NOT_READY_AND_RALLIED`

Later-rallied counts are false-negative diagnostics only. They are not automatic
evidence to relax support requirements.

## Dashboard Fields

Dashboard candidate summary exposes `support_coverage_summary` with:

- `sample_count`
- `support_metadata_coverage_pct`
- `vwap_metadata_coverage_pct`
- `minute_bar_coverage_pct`
- `stale_vwap_count`
- `support_missing_count_by_reason`
- `support_source_distribution`
- `minute_bar_quality_status_counts`

The dashboard intentionally shows summary values, not every raw support field.

## Known Limitations

- Minute-bar quality relies on metadata supplied by the runtime/gate path.
- Historical DRY_RUN rows without enriched support metadata remain low coverage.
- Rallied diagnostic counts can identify missed opportunities, but they do not
separate good first pullbacks from unsafe chase moves by themselves.
