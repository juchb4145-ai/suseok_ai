# PR-2 Opportunity Benchmark Collector v1

## Repository Audit

| 데이터 | 현재 source | 저장 여부 | 품질 | PR-2 사용 방식 |
|---|---|---:|---|---|
| opt10032 rank | `opening_turnover_seed_rows.rank`, `intraday_theme_discovery_rows.rank` | 예 | parsed row 기준 | Benchmark membership의 canonical rank로 사용 |
| current price | Opening parsed row는 `current_price`, Intraday는 raw row 복구 | 부분 | Opening HIGH, Intraday raw recovered 가능 | Observation/anchor price에 additive normalization만 적용 |
| turnover | `turnover_krw`, `current_turnover_krw` | 예 | parsed row 기준 | rank 1~100 universe filter와 report 정렬에 사용 |
| change rate | `change_rate_pct` | 예 | parsed row 기준 | observation, episode summary에 보존 |
| volume | Opening parsed row는 `volume`, Intraday는 raw row 복구 | 부분 | source별 차등 | observation volume과 data quality에 보존 |
| realtime tick | `MarketDataStore.latest_tick()` | 메모리 | 구독 종목만 HIGH | anchor fallback, price observation fallback에만 사용 |
| 1m candle | `CandleBuilder.completed_candles(code, 1)` | 메모리 | 구독 종목만 OHLC | MFE/MAE high/low 계산에 사용 |
| candidate instance | `candidate_funnel_episode_latest` | 예 | PR-1 strict identity | `candidate_instance_id` primary link로 사용 |
| qualification | `trading_day_qualification_reports` | 예 | PR-1 report | batch/episode/report strict/diagnostic 분리 |

## Reused opt10032 Paths

- Opening: `trading/theme_engine/opening_runtime.py`
  - `OPT10032_FIELDS`
  - `parse_opt10032_seed_rows()`
  - `save_opening_turnover_seed_batch()`
  - `opening_turnover_seed_batches`, `opening_turnover_seed_rows`
- Intraday: `trading/theme_engine/intraday_discovery.py`
  - `IntradayDiscoveryRuntimePipeline`
  - `parse_intraday_discovery_rows()`
  - `save_intraday_theme_discovery_batch()`
  - `intraday_theme_discovery_batches`, `intraday_theme_discovery_rows`

PR-2 does not enqueue opt10032, does not call Candidate ingestion, and does not create realtime registrations.

## Contracts

- Batch: `opportunity_benchmark_batch.v1`
- Observation: `opportunity_benchmark_observation.v1`
- Episode: `opportunity_benchmark_episode.v1`
- Candidate link: `opportunity_benchmark_candidate_link.v1`
- Price observation: `opportunity_benchmark_price_observation.v1`
- Outcome: `opportunity_benchmark_outcome.v1`
- Report: `opportunity_benchmark_report.v1`

## Classification Boundary

This collector only reports benchmark existence, candidate capture, detection delay, raw path labels, label quality, and qualification. It intentionally does not emit `MISSED_OPPORTUNITY`, `OVERFILTERED`, gate relaxation, buy recommendations, or Champion promotion decisions.

## Rollback

Set `TRADING_OPPORTUNITY_BENCHMARK_ENABLED=false`. The additive SQLite tables can remain in place because no strategy/order path reads them as decision input.
