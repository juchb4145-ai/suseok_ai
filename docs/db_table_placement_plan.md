# DB Table Placement Plan

## 분류 기준

테이블 배치는 데이터의 사용 시점과 장애 허용 범위로 결정한다.

- Memory Hot Store: 장중 판단에 필요한 최신 파생 상태. 손실되면 SQLite/preload로 재구성할 수 있어야 한다.
- SQLite Local Operational Store: 오늘 주문과 복구에 필요한 원장. PostgreSQL 장애와 무관하게 동작해야 한다.
- PostgreSQL Warehouse: 장기 보관, 분석, 복기, 리포트, 백테스트 원장. 장중 주문 경로를 막으면 안 된다.

## Memory Hot Store

DB 테이블이 아니라 프로세스 메모리 상태로 유지한다.

| Runtime view | 설명 | 영속화 정책 |
| --- | --- | --- |
| latest tick | 종목별 최신 가격, 거래량, 호가, 체결강도 | 중요 이벤트 또는 샘플만 저장 |
| 1m/3m/5m candle | 장중 판단용 분봉 | 완성된 bar만 SQLite outbox 또는 Warehouse 대상 |
| VWAP | 종목/테마 판단용 rolling VWAP | candle/feature snapshot으로 저장 가능 |
| execution strength | 체결강도와 변화율 | 급변 이벤트 또는 feature snapshot만 저장 |
| ThemeBoard | 테마 순위, breadth, leader, concentration | 최신 snapshot은 SQLite, 히스토리는 Warehouse |
| MarketRegime | 장세, 지수, 위험 상태 | 최신 상태는 Memory/SQLite, 히스토리는 Warehouse |
| Watchset | 오늘 감시 종목과 readiness | SQLite `watchset_today` 후보 |
| Open position runtime view | 현재 수량, 평균단가, 미실현 손익, MFE/MAE | SQLite `open_positions`, 장기 히스토리는 Warehouse |

장중 Strategy, Risk, Entry, Exit 판단은 이 메모리 상태를 읽는다. DB 조회를 판단 루프의 필수 입력으로 만들지 않는다.

## SQLite Local Operational Store

현재 SQLite에 남겨야 하거나, 향후 SQLite operational schema로 분리해야 하는 테이블이다.

| 현재/미래 테이블 | 목표 역할 | 이유 |
| --- | --- | --- |
| `gateway_commands` | SQLite operational | command queue 원장. 재시작 복구와 stale dispatched 점검에 필요 |
| `gateway_command_events` | SQLite operational event journal | enqueue/dispatch/ack/fail/cancel/duplicate/rate-limit 타임라인 |
| `gateway_command_dedupe_keys` | SQLite operational | idempotency/dedupe ledger. Core 재시작 후 중복 주문 방지 |
| `order_results` | SQLite operational | Kiwoom `send_order` 결과의 당일 운영 원장 |
| `executions` | SQLite operational | 체결 이벤트 반영과 포지션 복구의 입력 |
| `watch_items` | SQLite operational, legacy | 현재 watch/open holding 상태. 향후 `watchset_today`와 `open_positions`로 분리 |
| `runtime_order_intents` | SQLite operational | 주문 intent와 dedupe/idempotency를 주문 경로에 남김 |
| `runtime_order_intent_events` | SQLite operational event journal | intent 상태 전이와 order sink 결과 |
| `candidates` | SQLite operational today | 오늘 후보 복구와 dashboard 표시. 장기 분석은 Warehouse 복제 |
| `candidate_events` | SQLite operational today | 오늘 후보 FSM 복구. 장기 이벤트 분석은 Warehouse 복제 |
| `gate_decisions` | SQLite operational today | 오늘 판단 trace와 재평가. 장기 분석은 Warehouse 복제 |
| `entry_plans` | SQLite operational today | 주문 intent 생성 근거와 복구 |
| `exit_decisions` | SQLite operational today | 청산 판단, 취소/매도 복구 |
| `indicator_snapshots` | SQLite sampled operational | 후보 판단 당시 feature snapshot. 전체 tick 저장 대체 |
| `strategy_decision_events` | SQLite operational today + outbox | 오늘 판단 이벤트. Warehouse `intraday_outcomes`/analysis로 복제 |
| `strategy_decision_outcomes` | SQLite operational today + outbox | 장중 outcome labeling 진행 상태. 장기 평가는 Warehouse |
| `theme_activity_snapshots` | SQLite latest snapshot + Warehouse history | 장중 최신 테마 순위 복구용 최신 N개는 SQLite, 전체 히스토리는 Warehouse |
| `theme_lab_flow_snapshots` | SQLite latest/debug window | 실시간 dashboard 최신 상태. 장기 리포트 원장은 Warehouse |
| `theme_lab_outcome_observations` | SQLite today + outbox | 당일 outcome 추적. 장기 분석은 Warehouse |
| `market_side_confirmation_state` | SQLite operational | 장중 market regime 확정 상태 복구 |
| `market_side_confirmation_transitions` | SQLite operational today + outbox | 장중 상태 전이 journal. 히스토리는 Warehouse |
| `dashboard_operator_events` | SQLite operational today | 장중 operator action center 상태 |
| `dashboard_operator_actions` | SQLite operational today | 장중 operator 승인/실행 상태 |
| `runtime_events` | SQLite operational today | runtime health와 장애 trace |
| `runtime_cycles` | SQLite operational today | 현재 runtime loop 관찰과 복구 보조 |
| `live_sim_orders` | SQLite operational | live-sim 주문 lifecycle과 command 연결 |
| `live_sim_order_events` | SQLite operational event journal | live-sim 주문 상태 전이 |
| `live_sim_cancel_orders` | SQLite operational | 취소 intent와 retry/ack 상태 |
| `live_sim_fill_events` | SQLite operational | 체결 반영과 포지션 업데이트 입력 |
| `live_sim_positions` | SQLite operational open position | open/partial position 복구. 장기 `positions_all`로 복제 |
| `live_sim_runtime_health` | SQLite operational | order sink/reconcile health |
| `live_sim_reconcile_events` | SQLite operational event journal | 체결/포지션 재조정 이력 |
| `live_sim_preflight_snapshots` | SQLite operational latest | 장중 go/no-go와 order sink guard |
| `live_sim_canary_decisions` | SQLite operational today + outbox | live-sim 주문 전 판단과 결과 연결 |
| `strategy_runtime_settings` | SQLite preload copy | 장 시작 전 Warehouse/settings에서 내려받는 로컬 복사본 |
| `condition_profiles` | SQLite preload copy | 조건식 runtime 설정. 장기 version은 Warehouse |
| `shadow_small_entry_ops_state` | SQLite operational | 운영 토글/상태 |
| `shadow_small_entry_ops_tokens` | SQLite operational | operator token/approval 상태 |
| `shadow_small_entry_ops_audit_log` | SQLite operational + outbox | 운영 변경 감사 로그. 장기 보관은 Warehouse |
| future `operational_outbox_events` | SQLite operational | PostgreSQL 비동기 복제 큐 |
| future `kill_switch_state` | SQLite operational | Core 재시작 후에도 위험 차단 상태 유지 |
| future `open_positions` | SQLite operational | 실주문/모의/라이브시뮬 공통 open position runtime 원장 |
| future `watchset_today` | SQLite operational | 장중 watchset 복구 |
| future `candidates_today` | SQLite operational | 현재 `candidates`의 당일 운영 명칭 분리 |
| future `entry_decisions_today` | SQLite operational | 매수 판단 복구와 outbox |
| future `exit_decisions_today` | SQLite operational | 매도/취소 판단 복구와 outbox |
| future `latest_theme_rank_snapshot` | SQLite operational | ThemeBoard 재시작 seed |

SQLite operational 테이블은 retention을 가져야 한다. 당일 운영과 단기 복구에 필요한 기간만 보관하고, 장기 보관은 outbox를 통해 PostgreSQL Warehouse로 넘긴 뒤 pruning한다.

## PostgreSQL Warehouse 후보

현재 SQLite에 있으나 장기적으로 Warehouse가 원장이어야 하는 테이블과 목표 모델이다.

| 현재 테이블 또는 데이터 | 목표 Warehouse 테이블 | 비고 |
| --- | --- | --- |
| `kiwoom_symbol_master` | `symbol_master` | 장 시작 전 SQLite/Memory preload |
| `canonical_themes` | `theme_master` | 테마 기준 정보 |
| `theme_aliases` | `theme_master`/`theme_alias_history` | alias 변경 이력 보존 |
| `source_theme_catalog` | `naver_theme_snapshots` 또는 source catalog history | 외부 소스 snapshot/매칭 근거 |
| `theme_member_evidence` | `theme_membership_history` | 구성 종목 근거와 유효 기간 |
| `theme_membership_current` | `theme_membership_history` latest view | 장 시작 전 최신 membership preload |
| `theme_source_sync_runs` | source sync audit history | 수집 품질 분석 |
| `dynamic_theme_clusters` | dynamic theme cluster history | 실시간 군집의 장기 성능 분석 |
| `theme_activity_snapshots` | `theme_rank_history` | 전체 ranking history |
| Naver theme raw data | `naver_theme_snapshots` | 현재 별도 source에서 수집되는 raw snapshot |
| Kiwoom TR raw payload | `kiwoom_tr_raw` | 재처리와 데이터 품질 검증 |
| OHLCV 일봉 | `daily_ohlcv` | 백테스트/전일 기준값 |
| 분봉 | `minute_bars` | 리플레이와 feature 재계산 |
| 장초반 거래대금 seed | `opening_turnover_seed_history` | theme/leader 선별 복기 |
| theme rank snapshots | `theme_rank_history` | ThemeBoard 성능 복기 |
| leader/member metrics | `stock_leadership_history` | 테마 내 대장주/추종주 분석 |
| `order_results`, `runtime_order_intents`, `live_sim_orders` | `orders_all` | SQLite operational 원장을 비동기 복제 |
| `executions`, `live_sim_fill_events` | `executions_all` | 체결 전체 이력 |
| `live_sim_positions`, future `open_positions` snapshots | `positions_all` | 포지션 lifecycle 전체 이력 |
| `dry_run_performance_reports` | `dry_run_results` | 전략 평가 결과 |
| `dry_run_performance_items` | `dry_run_results` detail | lifecycle 단위 평가 |
| `dry_run_threshold_ab_reports` | `dry_run_results`/`strategy_experiments` | threshold 실험 결과 |
| `dry_run_threshold_ab_candidates` | `strategy_experiments` detail | parameter candidate 평가 |
| `strategy_decision_events` | `intraday_outcomes` seed | 판단 이벤트 장기 분석 |
| `strategy_decision_outcomes` | `intraday_outcomes` | horizon별 결과 라벨 |
| `dashboard_postmarket_reviews` | `postmarket_reviews` | 장후 리뷰 원장 |
| `position_context_history` | `postmarket_reviews`/`position_context_history` | 청산/보유 판단 복기 |
| `hybrid_gate_validation_events` | `intraday_outcomes`/validation facts | gate calibration |
| `buy_zero_rca_traces` | diagnostic fact history | 원인 분석과 리포트 |
| `shadow_strategy_evaluations` | strategy experiment facts | shadow policy 성능 |
| `shadow_small_entry_pilot_runs` | strategy experiment runs | pilot run 이력 |
| `shadow_small_entry_pilot_events` | strategy experiment events | pilot event 이력 |
| `strategy_replay_runs` | `backtest_datasets`/`backtest_runs` | replay 실행 원장 |
| `strategy_replay_reports` | `backtest_datasets`/`backtest_reports` | replay 결과 |
| `strategy_change_proposals` | `strategy_versions`/change proposals | 운영 승인 상태 일부는 SQLite에도 유지 가능 |
| `strategy_change_evidence` | strategy change evidence history | 제안 근거 |
| `strategy_change_approvals` | strategy approval audit history | 승인 감사 로그 |
| `strategy_config_snapshots` | `strategy_versions` | strategy settings version 원장 |
| `gateway_transport_latency_samples` | transport metrics history | 운영 분석. 주문 경로 동기 의존 금지 |
| `gateway_transport_latency_reports` | transport report history | 리포트 보관 |
| `logs` | operational log archive | 필요 시 structured event로 재분류 |
| `dashboard_operator_events` | operator audit history | 당일 상태는 SQLite, 장기 감사는 Warehouse |
| `dashboard_operator_actions` | operator audit history | 당일 상태는 SQLite, 장기 감사는 Warehouse |

## Warehouse 신규 목표 테이블

사용자가 지정한 목표 Warehouse 테이블은 다음과 같이 유지한다.

- `symbol_master`
- `theme_master`
- `theme_membership_history`
- `naver_theme_snapshots`
- `kiwoom_tr_raw`
- `daily_ohlcv`
- `minute_bars`
- `opening_turnover_seed_history`
- `theme_rank_history`
- `stock_leadership_history`
- `orders_all`
- `executions_all`
- `positions_all`
- `dry_run_results`
- `intraday_outcomes`
- `postmarket_reviews`
- `backtest_datasets`
- `strategy_versions`

## Outbox 복제 규칙

SQLite operational write가 성공한 뒤 outbox/event journal에 Warehouse 복제 이벤트를 남긴다.

```text
SQLite transaction:
  write operational row
  write event journal row
  write outbox row

Async writer:
  read pending outbox rows
  transform to warehouse schema
  upsert/insert idempotently
  mark delivered
```

Outbox event key는 `command_id`, `order_intent_id`, `execution_id`, `position_id`, `decision_id`, `snapshot_id`처럼 재시도해도 같은 Warehouse row로 수렴하는 값을 사용한다.

## 보류 항목

다음은 실제 migration PR에서 더 세분화한다.

- PostgreSQL physical schema, partition key, index
- SQLite operational pruning policy
- outbox dead-letter 기준
- tick/minute bar 압축 정책
- `watch_items`, `virtual_positions`, `live_sim_positions`를 공통 `open_positions` 모델로 통합할지 여부
- Warehouse에서 preload할 strategy settings의 승인/버전 정책
