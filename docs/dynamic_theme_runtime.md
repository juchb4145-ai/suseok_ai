# Dynamic Theme Runtime

## Runtime Structure

PR-DYN-02 connects the dynamic theme engine to live-style data flow. The runtime
is still read-only for trading decisions: it scores themes, stores snapshots,
publishes WebSocket payloads, and records OBSERVE dry-run metadata without
changing order, sell, stop-loss, or final gate behavior.

Core modules:

- `KiwoomThemeSource`: reads Kiwoom theme catalogs and members through mockable
  OpenAPI wrappers.
- `ThemeSourceSyncService`: runs source adapters, records sync runs, and keeps
  going when one source fails.
- `ThemeUniverseBuilder`: builds active and trade-eligible realtime watch
  universes from canonical themes and current memberships.
- `KiwoomRealtimeThemeAdapter`: converts Kiwoom realtime data and existing
  `StrategyTick` objects to `StockSnapshot`.
- `RealTimeThemeRuntime`: receives snapshots, recalculates impacted themes,
  persists throttled DB snapshots, and publishes WS payloads.
- `ThemeWebSocketBroadcaster`: isolates WS failures from the runtime loop.

## Kiwoom Live Theme Sync

The Kiwoom adapter tries these APIs first:

1. `GetThemeGroupList`
2. `GetThemeGroupCode`
3. `GetMasterCodeName`

It also accepts mock/fallback methods such as `opt90001`, `opt90002`,
`request_theme_groups`, and `request_theme_members`. Parser functions are pure
and testable, so pytest does not need a 32-bit PyQt/OpenAPI runtime.

## Source Sync

Manual sync is done through `ThemeSourceSyncService`:

```python
repo = ThemeEngineRepository(db)
service = ThemeSourceSyncService(repo, [KiwoomThemeSource(client), FixtureThemeSource(path)])
service.sync_all_sources()
```

Each run is stored in `theme_source_sync_runs` with source, start/finish time,
status, counts, message, and details.

## Universe Build

`ThemeUniverseBuilder` includes memberships only when:

- `theme_membership_current.active = 1`
- canonical status is `WATCH` or `ACTIVE`
- membership score is above the configured threshold
- `trade_eligible=1` for the trade-eligible universe

The default max universe size is 500. When exceeded, stocks are prioritized by
theme status, trade eligibility, membership score, source count, and recent
activity score.

## Realtime Snapshots

`KiwoomRealtimeThemeAdapter` standardizes:

- stock code/name
- price, change rate, volume, turnover
- execution strength
- bid/ask
- session high/low
- 1m/3m/5m momentum
- turnover strength

If turnover is missing, it estimates `price * volume` and records
`TURNOVER_ESTIMATED` in metadata reason codes.

## Theme Score

```text
theme_score =
  0.30 * normalized_weighted_return
+ 0.25 * normalized_turnover_strength
+ 0.20 * breadth_score
+ 0.15 * leader_score
+ 0.10 * momentum_score
```

Reason codes include:

- `LEADER_ONLY_THEME`
- `LOW_BREADTH`
- `LOW_TURNOVER`
- `INSUFFICIENT_SNAPSHOT`
- `TOO_FEW_MEMBERS`
- `TURNOVER_ESTIMATED`

ACTIVE promotion is dry-run only in this PR. The scorer records
`active_promotion_dry_run` as `ACTIVE` or `WATCH`.

## WebSocket Payloads

Payload types:

- `theme_rank`
- `theme_detail`
- `stock_theme_state`
- `runtime_health`
- `heartbeat`
- `error`

HTTP endpoints:

- `GET /health`
- `GET /api/themes/rank?top_n=20`
- `GET /api/themes/{theme_id}`
- `GET /api/stocks/{stock_code}/themes`
- `GET /api/theme-runtime/health`

WS endpoint:

- `WS /ws/themes`

Example subscribe:

```json
{
  "action": "subscribe",
  "channels": ["theme_rank", "runtime_health"],
  "top_n": 20
}
```

## Demo

```powershell
python -m trading.theme_engine.demo_live_runtime --db data/theme_engine_demo.sqlite3 --fixture tests/fixtures/theme_engine/furiosa_ai.json --ticks tests/fixtures/theme_engine/furiosa_ticks.json
```

The demo prints source sync results, `theme_rank`, and `runtime_health`.

## UI Read-Only Fields

The dashboard displays:

- theme engine status
- ACTIVE theme count
- WATCH theme count
- active universe stock count
- last theme sync time
- last runtime tick time
- WS client count
- top theme name and score

## OBSERVE Dry-Run Metadata

Candidate enrichment records:

- `dynamic_theme_status`
- `active_theme_id`
- `active_theme_name`
- `active_theme_score`
- `active_theme_rank`
- `stock_rank_in_theme`
- `stock_membership_score`
- `theme_reason_codes`
- `theme_gate_dry_run_status`
- `theme_gate_dry_run_reason`

These fields are diagnostic only in this PR.

## Next PRs

PR-DYN-03: Dynamic Theme Gate A/B Test

- Compare dry-run theme gate with existing gate output.
- Store `old_result` and `new_result`.
- Compare performance by theme score thresholds.
- Decide whether to affect actual buy gates.

PR-DYN-04: External/News Theme Source Adapter

- Connect licensed Infostock/themelab/news API style adapters.
- Improve automatic handling of Kiwoom-missing themes such as FuriosaAI.

PR-DYN-05: Dashboard Theme Heatmap

- Add realtime theme ranking, leaders, breadth, and leader-only warnings to UI.
