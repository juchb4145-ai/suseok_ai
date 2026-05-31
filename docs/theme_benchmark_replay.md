# Dynamic Theme Benchmark Replay

Internal replay snapshots capture the Dynamic Theme Engine's own ranking output for a fixed trade date. They are written as JSON with the same comparison-facing fields expected from external benchmark data: ranked themes, Top stocks, active members, reason codes, and snapshot quality.

This gives us a stable internal baseline before comparing against Royalroader, Themelab, or other external theme snapshots. Without it, differences are hard to attribute: a mismatch could come from external source coverage, our membership resolver, replay tick coverage, or score calculation. The internal snapshot makes our side explicit.

## Fixture Replay

Run a fixture replay with a fresh SQLite database and write the benchmark JSON:

```bash
python -m trading.theme_engine.benchmark.replay \
  --fixture tests/fixtures/theme_engine/furiosa_ai.json \
  --db /tmp/theme_replay.sqlite3 \
  --trade-date 2026-05-29 \
  --out reports/theme_benchmark/internal_2026-05-29.json
```

The command:

1. creates a fresh replay database,
2. syncs fixture source themes through `FixtureThemeSource`,
3. builds current theme memberships,
4. scores `mock_ticks` through `DynamicThemeEngineRuntime.score_fixture_ticks()`,
5. exports `InternalThemeBenchmarkSnapshot` JSON.

## Snapshot Fields

Each exported theme includes ranking metrics (`rank`, `theme_score`, `weighted_return_pct`, `turnover`, `breadth`, `leader_code`) and comparison details from `ThemeActivitySnapshot.details`: `top_stocks`, active `members`, `reason_codes`, and `snapshot_quality`.

Use this internal file as the left-hand side of external benchmark comparison. Operational replay from the latest DB trade date is intentionally left for a follow-up PR.

## End-to-End Compare

If you already have an internal replay snapshot and an external benchmark snapshot, generate comparison reports in one command:

```bash
python -m trading.theme_engine.benchmark.compare \
  --internal reports/theme_benchmark/internal_2026-05-29.json \
  --external benchmarks/external/royalroader_2026-05-29.json \
  --out reports/theme_benchmark/
```

You can also replay the fixture and compare it with an external snapshot in the same run:

```bash
python -m trading.theme_engine.benchmark.compare \
  --fixture tests/fixtures/theme_engine/furiosa_ai.json \
  --db /tmp/theme_replay.sqlite3 \
  --trade-date 2026-05-29 \
  --external tests/fixtures/theme_benchmark/sample_royalroader.json \
  --out reports/theme_benchmark/
```

The command writes `theme_benchmark_YYYY-MM-DD.json`, `.csv`, and `.md` into the output directory. If `--external` is omitted or the file does not exist, comparison is marked `SKIPPED` with reason `EXTERNAL_BENCHMARK_NOT_PROVIDED`; fixture replay can still write the internal snapshot.
