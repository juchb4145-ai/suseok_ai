# Context Risk Exit Confirmation Config

This PR exposes confirmation-cycle settings for DRY_RUN context-risk exits.
It does not enable LIVE orders, does not create Gateway `send_order` commands,
does not enable context risk exits by default, and does not change exit thresholds.

## Environment Variables

| Variable | Default | Range | Meaning |
| --- | ---: | ---: | --- |
| `TRADING_THEME_WEAK_CONFIRMATION_CYCLES` | `2` | `1..5` | Consecutive weak-theme cycles required before `THEME_WEAK_EXIT`. |
| `TRADING_LEADER_COLLAPSE_CONFIRMATION_CYCLES` | `1` | `1..5` | Confirmation cycles required before `LEADER_COLLAPSE_EXIT`. |
| `TRADING_INDEX_WEAK_CONFIRMATION_CYCLES` | `1` | `1..5` | Confirmation cycles required before `INDEX_WEAK_EXIT`. |
| `TRADING_BREADTH_COLLAPSE_CONFIRMATION_CYCLES` | `1` | `1..5` | Confirmation cycles required before `BREADTH_COLLAPSE_EXIT`. |

Invalid values fall back to defaults. Invalid means non-integer, below `1`, or
above `5`. Fallback diagnostics are recorded in exit details as
`config_source=env_invalid_fallback` and `config_fallback_reasons`.

`MARKET_RISK_OFF_EXIT` ignores confirmation-cycle settings and remains
immediate. It is treated as a portfolio protection signal in DRY_RUN.

## Exit Details

Context-risk exit details include:

- `required_confirmation_cycles`
- `observed_confirmation_cycles`
- `confirmation_passed`
- `config_source`
- `confirmation_config`
- `config_fallback_reasons`

If confirmation fails, `ExitDecisionEngine.last_details["context_risk"]` carries
the same diagnostic fields with `DATA_LIMITED_CONTEXT` or `LOW_CONFIDENCE_EXIT`.

## Operating Risk

Lower confirmation cycles can show earlier exits in DRY_RUN, but may overstate
protection quality during noisy theme rotations.

Higher confirmation cycles can reduce false exits, but may delay exits when a
leader collapses or index risk-off spreads quickly.

Do not tune these values from small samples. Review context coverage,
attribution confidence, false positive/false negative buckets, and per-exit
confidence distribution before changing operating defaults.
