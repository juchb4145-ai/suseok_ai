# Dynamic Theme Engine

## Source Policy

The theme universe source is Naver Finance only:

```text
https://finance.naver.com/sise/theme.naver?field=change_rate&ordering=desc
```

Naver is used only for theme names, detail links, stock membership, and optional
membership reason text. Naver change rate, volume, turnover, listed leader
stocks, and page ranking are not persisted as scoring inputs and are not used
for intraday buy decisions.

Kiwoom theme catalog APIs, static CSV mappings, fixture production sources, and
dynamic cluster source creation are retired from the runtime path.

## Architecture

- `NaverThemeUniverseSource`: crawls Naver theme list/detail pages and emits
  `ThemeSourcePayload` plus `ThemeMemberEvidence`.
- `ThemeSourceSyncService`: runs the Naver source and supports replace sync
  that purges retired source evidence before rebuilding memberships.
- `ThemeCanonicalResolver`: resolves Naver theme names into canonical themes.
- `ThemeMembershipBuilder`: builds `theme_membership_current`.
- `KiwoomRealtimeThemeAdapter`: converts Kiwoom realtime price ticks into
  `StockSnapshot`.
- `RealTimeThemeRuntime`: combines membership with Kiwoom realtime snapshots,
  ranks themes, and stores activity snapshots.
- `GatePipeline`: reads `DynamicThemeContextProvider` and records hybrid
  READY/WAIT/BLOCKED/OBSERVE decisions in OBSERVE/DRY_RUN validation fields.

## Sync

Manual CLI sync:

```powershell
python -m trading.theme_engine.sync_naver_universe --db data/trader.sqlite3
```

Manual API sync:

```text
POST /api/themes/sync/naver?replace=true&max_pages=20
```

The API requires the local gateway token. `replace=true` is the default and
removes old Naver evidence plus retired `kiwoom`, `fixture`, and internal source
evidence before rebuilding the current universe.

## Realtime Scoring

Theme strength, breadth, leader, co-leader, follower, and late-laggard
classification are derived from Kiwoom realtime price ticks only. The Naver
source never supplies intraday score inputs.

## Demo

```powershell
python -m trading.theme_engine.demo `
  --db data/theme_engine_demo.sqlite3 `
  --list-html tests/fixtures/naver_theme/list.html `
  --detail-dir tests/fixtures/naver_theme `
  --ticks tests/fixtures/theme_engine/furiosa_ticks.json
```
