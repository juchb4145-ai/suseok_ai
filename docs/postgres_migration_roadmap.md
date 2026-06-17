# PostgreSQL Migration Roadmap

## 목표

PostgreSQL 도입은 주문 경로를 더 복잡하게 만드는 작업이 아니라, 장기 분석과 복기를 안전하게 분리하는 작업이다. 마이그레이션은 항상 다음 불변식을 만족해야 한다.

- SQLite 기반 command queue, dedupe, execution, open position 복구는 계속 동작한다.
- PostgreSQL 장애는 주문 enqueue, cancel, ack, execution, risk 관리 실패로 전파되지 않는다.
- 장중 판단은 Memory Hot Store를 기준으로 한다.
- PostgreSQL write는 SQLite outbox를 통한 비동기 복제다.
- 장 시작 전 preload만 PostgreSQL read dependency가 될 수 있으며, preload 실패는 장전 go/no-go에서 판단한다.

## Phase 0: 설계 고정

이번 PR 범위다.

산출물:

- `docs/db_architecture_reboot.md`
- `docs/db_table_placement_plan.md`
- `docs/postgres_migration_roadmap.md`
- `storage/interfaces.py`
- `storage/operational_store.py`
- `storage/warehouse_store.py`
- `storage/outbox.py`
- `storage/postgres_writer.py`

검증:

- 기존 SQLite 테스트가 깨지지 않아야 한다.
- 새 스켈레톤은 PostgreSQL 드라이버 없이 import 가능해야 한다.
- 문서에 PostgreSQL 장애 시 주문 경로 비의존 원칙이 명시되어야 한다.

## Phase 1: Storage Boundary 도입

목표:

- 기존 `TradingDatabase` 직접 호출을 즉시 제거하지 않고, 신규 코드부터 `OperationalStore`/`WarehouseStore` 계약을 사용하게 한다.
- `SQLiteCommandStore`는 command persistence의 기준 구현으로 유지한다.
- `TradingDatabase`는 임시 monolith SQLite adapter로 취급하고, 내부 테이블을 역할별로 문서화된 이름에 맞춰 점진 분리한다.

작업:

- runtime factory에서 operational store와 warehouse writer를 주입할 수 있게 한다.
- PostgreSQL writer는 기본 disabled/deferred 상태로 둔다.
- SQLite operational write 실패와 Warehouse write 실패를 서로 다른 장애 등급으로 분리한다.

검증:

- command enqueue/dispatch/ack/recovery 테스트 통과
- order idempotency/dedupe 재시작 테스트 통과
- Warehouse disabled 상태에서도 주문 enqueue 가능

## Phase 2: SQLite Outbox/Event Journal

목표:

- SQLite 저장 성공 후 Warehouse 복제 이벤트를 로컬 outbox에 남긴다.
- Outbox는 주문 경로 transaction 뒤에 붙지만, PostgreSQL 네트워크 호출은 하지 않는다.

작업:

- `operational_outbox_events` 테이블 추가
- event topic과 idempotent key 표준화
- command/order/execution/position/decision/snapshot 이벤트 envelope 표준화
- retry count, next_attempt_at, delivered_at, dead_letter 상태 추가

검증:

- PostgreSQL writer disabled 상태에서 outbox backlog만 증가하고 주문은 성공
- writer exception 발생 시 operational transaction rollback 금지
- 같은 outbox event 재처리 시 Warehouse row가 중복되지 않는 key 설계 검증

## Phase 3: Preload Pipeline

목표:

- 장 시작 전 Warehouse에서 필요한 기준 데이터를 SQLite/Memory로 preload한다.
- 장중 판단은 preload 결과와 실시간 Memory Hot Store를 사용한다.

Preload 대상:

- symbol master
- theme master
- latest theme membership
- prev close
- avg turnover 20d
- strategy settings/version

작업:

- preload snapshot id와 생성 시각 기록
- preload 성공/실패를 preflight/go-no-go에 연결
- preload 결과를 SQLite operational copy와 Memory bootstrap state에 반영

검증:

- preload 실패 시 장전 차단 또는 명시적 degraded mode
- 장중 PostgreSQL 연결 종료 후에도 기존 preload 상태로 판단 루프 유지
- strategy settings version이 decision/order intent에 남음

## Phase 4: PostgreSQL Writer 활성화

목표:

- Outbox consumer가 Warehouse에 비동기 write를 수행한다.
- Writer 장애는 dashboard/ops alert로 노출하고 주문 경로를 막지 않는다.

작업:

- PostgreSQL connection pool과 health check
- batch write, idempotent upsert, retry/backoff
- topic별 transformer
- dead-letter queue와 replay tool
- writer lag/backlog metrics

검증:

- PostgreSQL down drill: 주문, 취소, ack, execution, open position update 성공
- PostgreSQL 복구 후 backlog drain
- writer 중복 실행 시 중복 row 없음
- Warehouse lag이 임계치를 넘으면 alert만 발생하고 order path는 계속 운영

## Phase 5: Warehouse Schema와 Backfill

목표:

- 장기 원장을 PostgreSQL로 옮기고 기존 SQLite 히스토리를 backfill한다.

작업:

- `symbol_master`, `theme_master`, `theme_membership_history`
- `naver_theme_snapshots`, `kiwoom_tr_raw`
- `daily_ohlcv`, `minute_bars`
- `theme_rank_history`, `stock_leadership_history`
- `orders_all`, `executions_all`, `positions_all`
- `dry_run_results`, `intraday_outcomes`, `postmarket_reviews`
- `backtest_datasets`, `strategy_versions`

검증:

- SQLite row count와 Warehouse count reconciliation
- 주요 idempotent key uniqueness 검증
- 날짜/종목/strategy version 단위 sampling 검증
- backfill 중에도 live operational SQLite 성능 영향 없음

## Phase 6: 분석/리포트 Read Path 전환

목표:

- 장기 분석, 장후 리뷰, dry-run 성능, replay/backtest 조회를 Warehouse read로 전환한다.
- 장중 operator dashboard의 현재 상태는 SQLite/Memory를 유지한다.

작업:

- report API별 read source 분류
- Warehouse read timeout과 fallback 정책
- 장중 dashboard current view와 장후 analytics view 분리

검증:

- Warehouse read 장애가 주문 API와 Gateway API를 막지 않음
- 장중 화면은 SQLite/Memory 기반으로 계속 표시
- 장후 리포트는 Warehouse lag 상태를 사용자에게 명확히 표시

## Phase 7: SQLite Pruning과 Schema 정리

목표:

- SQLite를 당일 운영과 단기 복구에 맞게 가볍게 유지한다.
- Warehouse에 복제된 히스토리는 retention 후 pruning한다.

작업:

- operational table별 retention 정의
- delivered outbox pruning
- sampled tick/window pruning
- legacy table rename 또는 view 제공
- migration runbook 작성

검증:

- pruning 후에도 Core 재시작 복구 가능
- dedupe retention 기간 내 중복 주문 방지 유지
- open position이 남아 있으면 관련 command/execution/position 원장 삭제 금지

## Cutover 원칙

Cutover는 read path부터 진행하고, write path는 SQLite operational first 원칙을 유지한다.

1. Warehouse schema 생성
2. SQLite outbox 생성
3. Writer disabled 상태로 outbox 누적 검증
4. Writer shadow mode로 Warehouse 적재
5. Reconciliation 통과 후 analytics read path 전환
6. Operational pruning 활성화

주문 실행 경로에 PostgreSQL synchronous write를 추가하는 단계는 없다.

## Rollback

Rollback은 Writer와 Warehouse read path를 끄는 방식으로 한다.

- `POSTGRES_WRITER_ENABLED=0`
- Analytics/report API는 SQLite fallback 또는 "warehouse unavailable" 상태 표시
- Outbox row는 보존하고 재활성화 후 drain
- SQLite command queue와 operational tables는 그대로 유지

PostgreSQL schema rollback이 필요해도 장중 주문 경로 rollback은 SQLite operational path를 유지하는 것으로 충분해야 한다.

## 장애 훈련 체크리스트

- PostgreSQL connection refused 상태에서 order enqueue 성공
- PostgreSQL timeout 상태에서 Gateway polling/dispatch 성공
- Writer process kill 후 outbox backlog 증가와 alert 확인
- Writer 재시작 후 backlog drain 확인
- Core 재시작 후 `QUEUED` command 복구 확인
- `DISPATCHED` order command가 자동 재전송되지 않는지 확인
- dedupe retention 내 동일 주문 재요청이 reject되는지 확인
- open position runtime view가 SQLite와 execution event로 복구되는지 확인

## 완료 기준

마이그레이션 완료는 PostgreSQL을 연결했다는 뜻이 아니라, 다음 상태를 만족한다는 뜻이다.

- 주문 안전성 원장은 SQLite operational store에 남아 있다.
- 장중 판단은 Memory Hot Store에서 수행된다.
- Warehouse는 장기 분석 원장으로 정상 적재된다.
- PostgreSQL 장애 중에도 Core/Gateway/OrderManager가 정상 운영된다.
- Warehouse lag과 장애는 ops alert와 dashboard 상태로 관측 가능하다.
