# Dynamic Theme Engine

## Why CSV mappings were retired

`theme_mappings.csv` was a static review artifact. It could not discover new
intraday narratives, could not explain why a stock belonged to a theme, and
made stale manual mappings look authoritative. The dynamic engine replaces it
with source evidence, canonical theme resolution, membership scoring, activity
scoring, and explicit readiness states.

## Why Kiwoom alone is not enough

Kiwoom theme groups are useful but incomplete. New themes often appear first in
licensed external datasets, news/event streams, or intraday co-movement before
they exist in Kiwoom groups. FuriosaAI is the reference case: it can be absent
from Kiwoom while external/news sources and live clustering still identify
related stocks.

## Architecture

- `KiwoomThemeSource`: adapter shell for `GetThemeGroupList`,
  `GetThemeGroupCode`, `GetMasterCodeName`, `opt90001`, and `opt90002`.
- `ExternalThemeSourceBase`: interface for licensed sources such as Infostock,
  themelab-style feeds, or approved news APIs.
- News/Event source adapters: future adapters emit the same
  `ThemeSourcePayload` and `ThemeMemberEvidence` objects.
- `DynamicThemeClusterDetector`: detects intraday co-movement when no source
  theme exists yet.
- `ThemeCanonicalResolver`: merges source names and aliases into
  `canonical_themes`.
- `ThemeEvidenceService`: persists source member evidence.
- `ThemeMembershipBuilder`: builds `theme_membership_current` with confidence,
  source count, relation type, and freshness weighting.
- `ThemeScoringEngine`: writes `theme_activity_snapshots`, leader diagnostics,
  breadth, turnover strength, and leader-only risk.
- WebSocket API: exposes rank, detail, stock state, heartbeat, and error
  payloads without requiring FastAPI for core tests.
- GatePipeline integration: uses `DynamicThemeContextProvider`; no static
  fallback is allowed.

## Theme states

- `CANDIDATE`: created from a source or cluster but not yet trusted.
- `WATCH`: enough evidence to monitor and score.
- `ACTIVE`: strong enough for strategy priority, subject to trade eligibility.
- `STALE`: old or no longer active.

## Trading integration

Only `ACTIVE` or `WATCH` contexts with `trade_eligible=True` should improve a
candidate. `LEADER_ONLY_THEME` is recorded when breadth is weak and a single
leader explains most of the move. `NO_ACTIVE_THEME` and
`THEME_CONTEXT_NOT_READY` block or wait without falling back to legacy mappings.
Actual order execution, stop loss, and sell logic are unchanged.

## Demo

```powershell
python -m trading.theme_engine.demo --db data/theme_engine_demo.sqlite3 --fixture tests/fixtures/theme_engine/furiosa_ai.json
```

The demo creates a fresh DB, syncs fixture source themes, resolves aliases,
stores evidence, builds current membership, scores mock ticks, and prints a
`theme_rank` JSON payload.

## WebSocket payload

```json
{
  "type": "theme_rank",
  "top_n": 20,
  "themes": [
    {
      "rank": 1,
      "theme_id": "furiosa_ai",
      "theme_name": "퓨리오사AI",
      "theme_score": 84.5,
      "status": "ACTIVE",
      "trade_eligible": true
    }
  ]
}
```

## Next PR

- Harden live Kiwoom `opt90001`/`opt90002` sync.
- Connect licensed external source adapters.
- Add news keyword/event source adapters.
- Build UI theme ranking and detail views.
- Run GatePipeline weight A/B tests with dynamic theme scores.
