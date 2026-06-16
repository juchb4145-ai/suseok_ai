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

## Market-Open LIVE_SIM Warmup Preset

`tools/start_market_open_live_sim.ps1` is an OBSERVE/DRY_RUN startup path, so it enables a bounded ThemeLab warmup profile by default:

```powershell
TRADING_THEME_BACKFILL_ENABLED=1
TRADING_THEME_BACKFILL_OBSERVE_ONLY=1
TRADING_THEME_BACKFILL_MAX_PER_CYCLE=6
TRADING_THEME_BACKFILL_MAX_PENDING=10
TRADING_THEME_BACKFILL_TTL_SEC=60
TRADING_THEME_BACKFILL_OPT10001_BUCKET_SEC=60
TRADING_THEME_BACKFILL_OPT10081_BUCKET_SEC=1800
TRADING_THEME_BACKFILL_ALLOW_OPT10081=0
TRADING_THEME_BACKFILL_ALLOW_REGULAR_SESSION=1
TRADING_THEME_BACKFILL_MAX_THEMES=8
TRADING_THEME_BACKFILL_MAX_HITS_PER_THEME=8
TRADING_THEME_BACKFILL_CACHE_ENABLED=1
TRADING_THEME_BACKFILL_CACHE_TTL_SEC=21600
TRADING_THEME_BACKFILL_CACHE_LIMIT=500
```

The script does not wait for `/api/themelab/snapshot` by default. Use `-WaitThemeLabStartupSnapshot` only when startup diagnostics should include the fresh ThemeLab dashboard snapshot, and `-DisableThemeBackfillWarmup` when TR backfill traffic should remain fully off.

The warmup scope is intentionally bounded. It backfills the highest ranked degraded themes first, and within each theme prefers leader/strong/alive members before lower-signal members. This keeps startup TR traffic focused on the symbols most likely to affect WatchSet and operator judgment.

On restart, the runtime hydrates same-trade-date ACKed ThemeLab backfill results from the gateway command history before building ThemeLab snapshots. Cached TR_BACKFILL ticks remain `gate_usable=false`; they improve coverage and operator visibility, but they must not create READY/READY_SMALL by themselves.

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
- `theme_backfill_runtime.backfill_cache_applied_count`
- `theme_backfill_runtime.backfill_cache_stale_count`
- `theme_backfill_runtime.load_guard_status`
- `theme_backfill_runtime.paused_backfill`
- `theme_backfill_runtime.pause_reason_codes`
- `theme_backfill_runtime.load_guard.gateway_queue_depth`
- `theme_backfill_runtime.load_guard.order_command_pending_count`
- `theme_backfill_runtime.load_guard.backfill_pending_count`
- gateway queue depth and command latency

Current dashboard aggregation is based on the recent 500 gateway commands. It is not a durable long-term event store.

## Runtime Load Guard

Runtime Load Guard is evaluated before new ThemeLab backfill commands are enqueued. It protects order flow and Kiwoom TR capacity by pausing non-order backfill traffic when the runtime is already under pressure.

Guard statuses:

- `OK`: backfill may enqueue.
- `DEGRADED`: backfill is not hard-paused, but operator review is needed.
- `PAUSED`: new backfill dispatch is stopped.
- `FAIL_CLOSED`: hard safety violation; new backfill dispatch is stopped.

Primary pause reasons:

- `READY_OR_READY_SMALL_PRESENT`
- `ORDER_COMMAND_PENDING`
- `GATEWAY_HEARTBEAT_STALE`
- `COMMAND_LATENCY_HIGH`
- `RATE_LIMITED_RECENT`
- `TR_FAILURE_RECENT`
- `PARSER_MISS_RATIO_HIGH`
- `TR_BACKFILL_CAUSED_READY`

`TR_BACKFILL_CAUSED_READY` must be treated as a safety incident. Backfill data is display/coverage data only and must never be accepted as READY/READY_SMALL evidence.

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
