# Trading Day Qualification + Canonical Candidate Funnel v1

PR-1은 Reboot V2 / Theme Core V3 관측 인프라다. 전략 임계값, SetupRouter 판단, EntryEngine 판단, 주문 경로는 변경하지 않고, 거래일 표본 품질과 candidate episode funnel을 read-only로 집계한다.

## 저장소 감사 결과

| Funnel stage | canonical source | identity key | timestamp | confidence | fallback |
|---|---|---|---|---|---|
| SOURCE_DETECTED | `candidate_source_events`, `candidates.sources_json` | `trade_date + candidate_instance_id` | `detected_at`, `candidates.detected_at` | HIGH when candidate instance exists | LOW synthetic source episode |
| CANDIDATE_CREATED | `candidates` | `metadata.candidate_instance_id` | `detected_at` | HIGH | `LOW:<trade_date>:<code>:<candidate_id>` |
| ACTIVE_SOURCE_PRESENT | `candidate.metadata.candidate_state_contract`, `CandidateStateContractService.snapshot()` | `candidate_instance_id` | `last_seen_at` | HIGH | computed snapshot, no mutation |
| HYDRATION_COMPLETE | CandidateStateContract metadata/computed snapshot | `candidate_instance_id` | `last_seen_at` | HIGH | computed snapshot, no mutation |
| EVALUATION_ELIGIBLE | CandidateStateContract metadata/computed snapshot | `candidate_instance_id` | `last_seen_at` | HIGH | computed snapshot, no mutation |
| REALTIME_SUBSCRIPTION_ACTIVE | `setup_router_readiness_latest` | `candidate_instance_id` | `subscription_active_since`, `calculated_at` | HIGH | none |
| FRESH_REALTIME_READY | `setup_router_readiness_latest` | `candidate_instance_id` | `latest_tick_at`, `calculated_at` | HIGH | none |
| STRATEGY_CONTEXT_READY | `strategy_context_latest` | `candidate_id`, `code` | `calculated_at` | MEDIUM/HIGH | code join if candidate id absent |
| ENTRY_EVALUATED | `entry_decisions` | `candidate_id`, `code` | `calculated_at` | MEDIUM/HIGH | code join if candidate id absent |
| CHAMPION_FORMING | `setup_observations_latest_v2` | `candidate_instance_id` | `calculated_at` | HIGH | none |
| CHAMPION_MATCHED | `setup_observations_latest_v2` | `candidate_instance_id` | `calculated_at` | HIGH | none |
| CHAMPION_CONTEXT_ELIGIBLE | `setup_observations_latest_v2` | `candidate_instance_id` | `calculated_at` | HIGH | none |
| CHAMPION_VALID_OBSERVE | `setup_observations_latest_v2` | `candidate_instance_id` | `calculated_at` | HIGH | none |

주문 관련 13~17 stage는 현재 baseline이 `OBSERVE_ONLY`이므로 `applicable=false`, `expected_disabled=true`로 집계한다. 0건은 drop-off가 아니다.

## BuyZeroRCA와 신규 Funnel 차이

`trading_app/buy_zero_rca.py`는 Hybrid, Shadow Small Entry, legacy EntryPlan 흐름을 포함하는 debug/legacy 분석기다. PR-1의 `candidate_funnel.reboot_v2.v1`은 Reboot V2 canonical source만 사용하며, `candidate_instance_id` episode 단위로 count inflation을 방지한다.

## Schema

- `trading_day_qualification.v1`
- `candidate_funnel.reboot_v2.v1`
- `candidate_funnel_episode.v1`
- `no_trade_classification.v1`

## Qualification 판정

검사 영역은 `BASELINE_INTEGRITY`, `RUNTIME_INTEGRITY`, `MARKET_CONTEXT_INTEGRITY`, `REALTIME_DATA_INTEGRITY`, `CANDIDATE_ATTRIBUTION_INTEGRITY`, `SNAPSHOT_INTEGRITY`, `ORDER_SAFETY_INTEGRITY`, `FUNNEL_INTEGRITY`다.

`strict_sample_eligible=true`는 `FINAL + VALID`에서만 가능하다. `LIVE_PREVIEW`는 항상 strict sample이 아니다.

## Persistence

- `candidate_funnel_episode_latest`: `trade_date + candidate_instance_id` latest row. 동일 fingerprint는 write skip, `max_stage_ordinal`은 후퇴하지 않는다.
- `candidate_funnel_reports`: funnel report 저장. FINAL은 revision append.
- `trading_day_qualification_reports`: preview는 report id upsert, FINAL은 revision append.
- `ops_runtime_health_samples`: 30초 bucket quality evidence. raw 대형 snapshot은 저장하지 않는다.

## Runtime 연결

`RebootV2Runtime.cycle()`에서 OrderManager V2와 count attach가 끝난 뒤 `candidate_funnel`, `trading_day_qualification` section을 붙인다. 결과는 전략 pipeline 입력으로 전달하지 않는다.

## API / CLI / Export

읽기 API:

- `GET /api/ops/trading-day-qualification`
- `GET /api/ops/candidate-funnel`
- `GET /api/ops/candidate-funnel/episodes`
- `GET /api/ops/candidate-funnel/candidates/{candidate_instance_id}`

rebuild API는 local token이 필요하다.

- `POST /api/ops/trading-day-qualification/rebuild`
- `POST /api/ops/candidate-funnel/rebuild`

CLI:

```powershell
python tools/audit_trading_day_qualification.py --db data/trader.sqlite3 --trade-date YYYY-MM-DD --finalize --export
python tools/audit_candidate_funnel.py --db data/trader.sqlite3 --trade-date YYYY-MM-DD --strict-only --export
```

Export:

- `reports/trading_day_qualification/<trade_date>/report.json`
- `reports/trading_day_qualification/<trade_date>/report.md`
- `reports/trading_day_qualification/<trade_date>/checks.csv`
- `reports/candidate_funnel/<trade_date>/summary.json`
- `reports/candidate_funnel/<trade_date>/summary.md`
- `reports/candidate_funnel/<trade_date>/stages.csv`
- `reports/candidate_funnel/<trade_date>/episodes.csv`
- `reports/candidate_funnel/<trade_date>/invariant_violations.csv`

## No-trade Classification

현재 baseline은 OBSERVE_ONLY다. `CHAMPION_VALID_OBSERVE`가 존재하고 주문 count가 0이면 `EXPECTED_OBSERVE_ONLY`다. Opportunity Benchmark가 없으므로 `DISCOVERY_MISS`를 확정하지 않고, future outcome label이 없으므로 `OVERFILTERED`도 확정하지 않는다.

## Rollback

다음 flag를 끄면 기존 Reboot V2 OBSERVE 동작으로 돌아간다.

```powershell
$env:TRADING_DAY_QUALIFICATION_ENABLED = "false"
$env:TRADING_CANDIDATE_FUNNEL_ENABLED = "false"
```

## PR-2 연결

PR-2 Opportunity Benchmark Collector는 `trade_date`, `as_of`, `candidate_instance_id`, `code`, `first_seen_at`, `baseline_role`, Champion stage, strict attribution, session bucket, qualification status, strict sample eligibility를 재사용할 수 있다.
