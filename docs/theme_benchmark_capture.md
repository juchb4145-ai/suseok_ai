# Theme Benchmark Manual Capture

Manual capture is for benchmark snapshots only. A captured Royalroader or Themelab file is not a Dynamic Theme Engine source, is not synced into canonical themes, and must not be used directly for live buy decisions.

## Why CI Does Not Capture External Sites

External benchmark sites may require login, throttle requests, change markup, or block automated access. CI should stay deterministic and should not crawl third-party pages. The capture command is a local/manual utility: one explicit command, one HTTP request, one saved JSON file.

## Manual Capture

Example:

```bash
python -m trading.theme_engine.benchmark.capture \
  --source royalroader \
  --url https://theme.royalroader.co.kr/ \
  --trade-date 2026-05-29 \
  --out benchmarks/external/royalroader_2026-05-29.json
```

The command refuses to run when `CI` is set, uses a request timeout, does not poll, and aborts on 403, 429, login-required pages, or parse failure. A failure only stops the capture command; it does not affect the core engine.

The output is compatible with `load_external_theme_benchmark()`:

```python
from trading.theme_engine.benchmark.loader import load_external_theme_benchmark

external = load_external_theme_benchmark("benchmarks/external/royalroader_2026-05-29.json")
```

## Comparing With Internal Replay

Once an internal replay snapshot exists, compare and write reports with the benchmark utilities:

```python
import json

from trading.theme_engine.benchmark.comparator import compare_theme_benchmarks
from trading.theme_engine.benchmark.loader import load_external_theme_benchmark
from trading.theme_engine.benchmark.report import write_benchmark_reports

external = load_external_theme_benchmark("benchmarks/external/royalroader_2026-05-29.json")
internal = json.loads(open("reports/theme_benchmark/internal_2026-05-29.json", encoding="utf-8").read())

result = compare_theme_benchmarks(external, internal)
write_benchmark_reports(result, "reports/theme_benchmark", "2026-05-29")
```

Use the Markdown report for quality review: missing aliases, low Top5 overlap, low member overlap, and leader mismatches are observation signals, not trading gates.
