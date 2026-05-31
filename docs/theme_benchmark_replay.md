# Dynamic Theme Benchmark Replay

Internal replay snapshots capture the engine's own ranking output for a fixed
trade date. Replay uses local Naver HTML fixtures for universe membership and a
separate Kiwoom-style tick JSON file for realtime scoring.

## Naver Fixture Replay

```bash
python -m trading.theme_engine.benchmark.replay \
  --list-html tests/fixtures/naver_theme/list.html \
  --detail-dir tests/fixtures/naver_theme \
  --ticks tests/fixtures/theme_engine/furiosa_ticks.json \
  --db /tmp/theme_replay.sqlite3 \
  --trade-date 2026-05-29 \
  --out reports/theme_benchmark/internal_2026-05-29.json
```

The command creates a fresh replay database, syncs Naver theme membership,
scores tick snapshots, and exports `InternalThemeBenchmarkSnapshot` JSON.

## End-to-End Compare

```bash
python -m trading.theme_engine.benchmark.compare \
  --list-html tests/fixtures/naver_theme/list.html \
  --detail-dir tests/fixtures/naver_theme \
  --ticks tests/fixtures/theme_engine/furiosa_ticks.json \
  --db /tmp/theme_replay.sqlite3 \
  --trade-date 2026-05-29 \
  --external tests/fixtures/theme_benchmark/sample_royalroader.json \
  --out reports/theme_benchmark/
```

You can also compare an existing internal snapshot with `--internal`. Reports
are written as JSON, CSV, and Markdown.
