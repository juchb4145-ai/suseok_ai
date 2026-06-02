# Runtime Reason Taxonomy

This taxonomy is a display and reporting adapter over existing `reason_codes`.
It does not remove or rename legacy reason codes. Runtime, dashboard, and
DRY_RUN reports keep the original codes and add a normalized `reason_status`
plus `reason_family`.

## WAIT

| reason_status | Typical legacy reasons | Meaning |
| --- | --- | --- |
| `WAIT_DATA` | `DATA_INSUFFICIENT`, `INPUT_MISSING`, `FILL_INPUT_INSUFFICIENT` | Required market, indicator, theme, or fill data is not reliable enough. |
| `WAIT_MARKET` | `INDEX_WEAK`, `MARKET_WAIT`, `MARKET_INDEX_TEMPORARY_CAP`, `RISK_OFF` | Market/index context is not supportive. |
| `WAIT_PULLBACK` | `WAIT_PULLBACK_CONFIRMATION`, `PULLBACK`, `SUPPORT_TOUCHED`, `DEEP_PULLBACK` | Pullback/reclaim setup is not confirmed yet. |
| `WAIT_BREADTH` | `LOW_BREADTH`, `MARKET_BREADTH_WEAK`, `BREADTH_SCOPE_LIMITED` | Theme or market breadth is too thin. |
| `WAIT_THEME_CONFIRMATION` | `THEME_STRENGTH_C`, `THEME_CONFIRM`, `WATCH_THEME` | Theme is watchable but not confirmed as tradable. |
| `WAIT_LEADER_CONFIRMATION` | `LEADERSHIP_WEAK`, `THEME_LEADER_COLLAPSE`, leader confirmation reasons | Leader structure is not strong enough. |

## BLOCKED

| reason_status | Typical legacy reasons | Meaning |
| --- | --- | --- |
| `BLOCK_THEME` | `THEME_WEAK`, `WEAK_THEME`, `THEME_SYNC_WEAK` | Theme itself is weak or broken. |
| `BLOCK_RISK` | hard risk block, market risk, leader risk | Risk is too high for entry. |
| `BLOCK_CHASE` | `CHASE_HIGH`, `HIGH_CHASE`, `CHASE_RISK`, `LATE_CHASE` | Entry would be a chase. |
| `BLOCK_LATE_LAGGARD` | `LATE_LAGGARD` | Stock is a late follower rather than leader/co-leader. |
| `BLOCK_DATA` | final data quality block | Data is insufficient and should not be retried as a buy candidate. |
| `BLOCK_LIQUIDITY` | `FILL_LIQUIDITY_WEAK`, `SPREAD_TOO_WIDE`, `LOW_TURNOVER` | Liquidity or spread is not acceptable. |

## OBSERVE

| reason_status | Typical legacy reasons | Meaning |
| --- | --- | --- |
| `OBSERVE_CHASE` | `CHASE_HIGH`, `VWAP_OVEREXTENDED`, chase/extension statuses | Interesting but too extended to buy. |
| `OBSERVE_BREAKOUT` | `BREAKOUT_CONTINUATION` | Breakout continuation is being watched, not bought as pullback. |
| `OBSERVE_READY_SMALL` | `READY_SMALL` | Small-size eligible/watch state, not a full READY entry. |
| `OBSERVE_LEADER_ONLY` | `LEADER_ONLY_THEME`, `WATCH_THEME` | Leader-only theme structure needs confirmation. |

## Compatibility

- Existing `reason_codes` remain the source of truth.
- New fields are additive: `reason_status`, `reason_family`, and
  `reason_summary`.
- Dashboard candidate rows and DRY_RUN performance reports show both legacy
  `reason_codes` and normalized taxonomy counts.
- `CHASE_HIGH` maps to `OBSERVE_CHASE` unless the candidate is already a final
  blocked item, in which case it maps to `BLOCK_CHASE`.
- `THEME_WEAK` maps to `BLOCK_THEME`.
- `DATA_INSUFFICIENT` maps to `WAIT_DATA` for recoverable wait states and
  `BLOCK_DATA` for final blocked states.

## Support Diagnostics

Support-related diagnostic-only reasons are split so reports can distinguish
strategy structure from data quality. These reasons do not permit buy intents.

| reason_code | Meaning |
| --- | --- |
| `SUPPORT_DATA_MISSING` | Required support inputs are missing, such as recent support, VWAP, and minute-bar coverage. |
| `SUPPORT_STRUCTURALLY_MISSING` | Data exists, but no usable support level is present for a pullback entry. |
| `SUPPORT_NOT_READY` | A support candidate exists but is not ready or not reliable enough yet. |

DRY_RUN reports aggregate support coverage for `recent_support_price`, `vwap`,
`minute_bar`, and `support_reclaimed` so threshold tuning does not confuse
missing data with a structurally invalid setup.
