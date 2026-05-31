# Dynamic Theme Runtime

## Runtime Structure

The runtime has two distinct inputs:

- Naver Finance theme list/detail pages create the theme universe and
  membership.
- Kiwoom realtime price ticks calculate intraday theme activity and leadership.

The runtime remains read-only for live trading. Hybrid gate output is recorded
for OBSERVE/DRY_RUN validation unless a later, explicit safety change enables a
different policy.

## Source Sync

```python
repo = ThemeEngineRepository(db)
source = NaverThemeUniverseSource(max_pages=20)
service = ThemeSourceSyncService(repo, [source])
service.sync_source("naver_theme_universe", replace=True, purge_sources=RETIRED_THEME_SOURCE_NAMES)
```

Naver numeric columns are intentionally ignored. Only theme names, detail page
ids, member stock codes/names, and optional membership reasons are ingested.

## Realtime Flow

```text
Naver universe -> theme_membership_current
Kiwoom price_tick -> MarketDataStore
Kiwoom price_tick -> RealTimeThemeRuntime
RealTimeThemeRuntime -> theme_activity_snapshots
GatePipeline -> hybrid READY/WAIT/BLOCKED/OBSERVE validation
```

`StrategyRuntime` also registers the active theme universe as a non-protected
`theme_universe` realtime subscription source. Protected index, holding, order,
and virtual activity subscriptions remain higher priority.

## Theme Score

`ThemeScoringEngine` uses only `StockSnapshot` fields derived from Kiwoom
realtime ticks:

```text
theme_score =
  0.30 * normalized_weighted_return
+ 0.25 * normalized_turnover_strength
+ 0.20 * breadth_score
+ 0.15 * leader_score
+ 0.10 * momentum_score
```

Reason codes include low breadth, leader-only concentration, low turnover,
insufficient snapshots, and estimated turnover warnings.

## Demo

```powershell
python -m trading.theme_engine.demo_live_runtime `
  --db data/theme_engine_demo.sqlite3 `
  --list-html tests/fixtures/naver_theme/list.html `
  --detail-dir tests/fixtures/naver_theme `
  --ticks tests/fixtures/theme_engine/furiosa_ticks.json
```
