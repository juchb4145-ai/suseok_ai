# ThemeLab TR Backfill OBSERVE Pilot

ThemeLab TR backfill is a data-quality and dashboard coverage aid. It must not be used as a buy signal or as a Hybrid Gate READY/READY_SMALL input.

## Default Safety

Keep the default disabled state:

```powershell
TRADING_THEME_BACKFILL_ENABLED=0
TRADING_THEME_BACKFILL_ALLOW_OPT10081=0
```

Do not enable this as a LIVE default. LIVE use requires a separate safety PR.

## OBSERVE Pilot Preset

Use these values only for an OBSERVE pilot:

```powershell
TRADING_MODE=OBSERVE
TRADING_THEME_BACKFILL_ENABLED=1
TRADING_THEME_BACKFILL_OBSERVE_ONLY=1
TRADING_THEME_BACKFILL_MAX_PER_CYCLE=1
TRADING_THEME_BACKFILL_MAX_PENDING=3
TRADING_THEME_BACKFILL_TTL_SEC=90
TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC=300
TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC=1800
TRADING_THEME_BACKFILL_ALLOW_OPT10081=0
TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION=1
```

`MAX_PER_CYCLE=2` is acceptable after confirming gateway command latency is stable.

## Watch During Pilot

Check `/api/themelab/snapshot` and the dashboard for:

- `theme_backfill_runtime.observe_pilot_active`
- `theme_backfill_runtime.paused_reason`
- `theme_backfill_runtime.queued_count`
- `theme_backfill_runtime.dispatched_count`
- `theme_backfill_runtime.success_count`
- `theme_backfill_runtime.failure_count`
- `theme_backfill_runtime.skipped_count`
- `theme_backfill_runtime.parser_miss_ratio`
- `theme_backfill_runtime.missing_price_count_before`
- `theme_backfill_runtime.missing_prev_close_count_before`
- `theme_backfill_runtime.tr_backfill_caused_ready_count`
- gateway queue depth and command latency

Current dashboard aggregation is based on the recent 500 gateway commands. It is not a durable long-term event store.

## Stop Criteria

Disable the pilot immediately if any of these occur:

- `tr_backfill_caused_ready_count > 0`
- order command latency worsens materially
- gateway heartbeat gap worsens materially
- `RATE_LIMITED`, `TR_TIMEOUT`, or `TR_REQUEST_FAILED` increases
- `parser_miss_ratio` is persistently high
- backfill dispatch occurs while READY/READY_SMALL or order commands are pending

## Fallback Order

Prev close is filled in this order:

1. existing runtime/cache value
2. `opt10001` base price
3. gateway-only `GetMasterLastPrice(code)`
4. `opt10081` only when explicitly enabled

TR_BACKFILL values are marked display/coverage only. They are not gate usable.
