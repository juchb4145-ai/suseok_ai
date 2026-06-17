# DB Architecture Reboot

## 목적

이번 설계는 SQLite 단일 파일에 운영, 전략, 분석, 리포트 데이터가 모두 섞여 있는 현재 구조를 바로 PostgreSQL로 옮기기 위한 작업이 아니다. 목표는 DB의 책임을 먼저 분리하고, 이후 PostgreSQL + SQLite + Memory 혼합 구조로 안전하게 확장할 수 있는 경계를 고정하는 것이다.

핵심 원칙은 단순하다.

- 주문 실행 경로는 PostgreSQL에 동기 의존하지 않는다.
- 장중 매수/매도 판단은 DB 조회가 아니라 Memory Hot Store의 최신 상태를 기준으로 한다.
- SQLite는 단순 캐시가 아니라 당일 주문 안정성, 재시작 복구, 중복 방지를 위한 Local Operational Store다.
- PostgreSQL은 장기 보관, 분석, 복기, 리포트, 백테스트를 위한 Warehouse다.
- PostgreSQL 장애 중에도 Core, Gateway, OrderManager는 주문, 취소, 체결 반영, 리스크 관리를 계속 수행해야 한다.

## 현재 코드 관찰

현재 저장소 기준 확인 사항:

- `trading_app/dependencies.py`
  - `DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "trader.sqlite3"`를 기본 SQLite 경로로 사용한다.
  - `get_settings()`는 `TRADING_DB_PATH` 환경변수로 DB 경로를 오버라이드한다.
  - `open_database()`는 `TradingDatabase(str(get_settings().db_path))`를 직접 반환한다.
- `storage/db.py`
  - `TradingDatabase`는 `sqlite3.connect(self.path)`를 사용한다.
  - 연결 설정은 `PRAGMA busy_timeout = 5000`, `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`이다.
  - `_migrate()` 안에 운영 테이블, 테마/심볼 마스터, 후보/전략 판단, 리포트, 스냅샷, 리플레이, 드라이런/라이브시뮬 테이블이 함께 생성된다.
- `trading/broker/command_persistence.py`
  - `SQLiteCommandStore`는 같은 SQLite 파일을 대상으로 `TradingDatabase`를 부트스트랩한 뒤 별도 `sqlite3.connect(..., check_same_thread=False)` 연결을 연다.
  - `gateway_commands`, `gateway_command_events`, `gateway_command_dedupe_keys`를 command queue의 영속 저장소로 사용한다.
- `trading/broker/gateway_state.py`
  - 시작 시 `recover_from_store()`로 유효한 `QUEUED` 명령을 메모리 dispatch queue로 복구한다.
  - `enqueue_command()`는 command store에 먼저 저장하고 중복 키를 확인한 뒤 메모리 queue에 복원한다.
  - `dispatch_commands()`는 메모리 queue에서 꺼낸 명령을 `DISPATCHED`로 저장하고 event journal을 남긴다.
  - `ack_command()`는 Gateway ack 결과를 command store에 반영한다.

이 구조 덕분에 현재 SQLite는 이미 주문 안전성 경계의 일부다. 따라서 PostgreSQL 전환은 SQLite를 제거하는 방향이 아니라, SQLite의 운영 책임을 명확히 남기고 Warehouse 책임만 분리하는 방향이어야 한다.

## 목표 저장소 역할

| Layer | 역할 | 포함 데이터 | 포함하지 않는 것 |
| --- | --- | --- | --- |
| Memory Hot Store | 장중 판단의 기준 상태 | 최신 tick, 1m/3m/5m candle, VWAP, 체결강도, ThemeBoard, MarketRegime, Watchset, open position runtime view | 주문 복구의 유일 원장, 장기 분석 원장 |
| SQLite Local Operational Store | 당일 운영 원장과 재시작 복구 | command queue, dedupe/idempotency, order result, execution, open position, watchset/candidates/decisions today, latest theme rank snapshot, kill switch state, outbox | 전체 히스토리 분석, 백테스트 원천 저장소 |
| PostgreSQL Warehouse | 장기 보관, 분석, 복기, 백테스트 | symbol/theme master, theme membership history, raw TR, OHLCV/minute bars, theme rank history, orders/executions/positions all, dry-run/outcome/review/backtest data, strategy versions | 장중 주문 실행의 동기 의존성 |

## 장중 데이터 흐름

```text
Kiwoom/Gateway events
  -> Memory Hot Store update
     - latest tick
     - candles and VWAP
     - execution strength
     - ThemeBoard and MarketRegime
     - open position runtime view

Strategy/Risk/OrderManager
  -> reads Memory Hot Store
  -> writes operational decision/order intent to SQLite
  -> enqueues Gateway command after local persistence succeeds

Gateway ack/execution events
  -> writes SQLite operational tables first
  -> updates Memory Hot Store runtime view
  -> appends outbox/event journal rows

Async PostgreSQL writer
  -> reads SQLite outbox
  -> writes Warehouse tables
  -> marks outbox delivered or retries with backoff
```

실시간 판단 경로는 DB 조회를 피한다. Tick마다 DB insert를 수행하지 않고, 메모리 상태를 갱신한 뒤 샘플링된 tick, 분봉, 주문/체결, 리스크 상태 전환, 중요한 전략 판단 이벤트만 SQLite나 PostgreSQL 복제 대상으로 남긴다.

## 장 시작 전 Preload

장 시작 전 PostgreSQL에서 필요한 기준 데이터를 읽어 SQLite와 Memory로 preload한다.

- `symbol_master`
- `theme_master`
- `theme_membership_history`의 최신 유효 구간
- 전일 종가와 기준 가격
- `avg_turnover_20d`
- strategy settings와 strategy version

Preload 결과는 장중 Core가 PostgreSQL 없이도 판단을 계속할 수 있도록 Local Operational Store와 Memory Hot Store에 반영한다. Preload가 실패하면 장 시작 전 go/no-go 판단에서 차단하거나, 명시적으로 degraded mode를 선택해야 한다. 단, 장중 PostgreSQL 장애는 이미 preload된 상태와 SQLite operational 원장을 사용해 주문 경로를 막지 않는다.

## PostgreSQL 장애 원칙

PostgreSQL은 장중 주문 경로에서 동기 dependency가 아니다.

- PostgreSQL write 실패는 주문 enqueue, cancel, ack, execution 반영을 실패시키지 않는다.
- PostgreSQL writer는 SQLite outbox/event journal의 backlog로 장애를 흡수한다.
- Writer는 backoff, 재시도, idempotent upsert, dead-letter 상태를 가져야 한다.
- Core/Gateway/OrderManager는 Warehouse health가 `DOWN`이어도 SQLite operational write가 성공하면 계속 운영한다.
- Warehouse 장애는 dashboard/리포트/분석 지연으로 노출하고, 주문 안전성 장애로 승격하지 않는다.

SQLite 장애는 다르다. SQLite Local Operational Store는 주문 안전성 경계이므로 command 저장, dedupe 저장, kill switch 저장이 실패하면 새 주문 생성은 차단하는 것이 맞다.

## Tick 저장 정책

Tick 단위 이벤트는 다음 정책을 따른다.

- 매 tick은 Memory Hot Store에 반영한다.
- DB에는 모든 tick을 무조건 insert하지 않는다.
- 저장 후보는 샘플링 tick, 분봉 집계, 급등락/체결강도 급변 이벤트, 주문/체결 주변의 중요 tick, 리플레이에 필요한 제한된 버퍼다.
- `gateway_price_ticks` 같은 현재 SQLite 테이블은 장기적으로 sampled tick fact 또는 replay seed로 재분류하고, 전체 tick warehouse 적재는 별도 압축/파티셔닝 설계 후 결정한다.

## 복구 모델

재시작 복구는 SQLite를 기준으로 한다.

- `gateway_commands`: 유효한 `QUEUED` 명령만 메모리 dispatch queue로 복구한다.
- `DISPATCHED` 주문 명령은 자동 재전송하지 않는다.
- `gateway_command_dedupe_keys`: Core 재시작 뒤에도 같은 주문이 중복 생성되지 않게 한다.
- `order_results`, `executions`, `open_positions`: 주문 결과와 포지션 런타임 뷰를 복원한다.
- `watchset_today`, `candidates_today`, `decisions_today`, `latest_theme_rank_snapshot`: 장중 판단 컨텍스트를 재구성한다.
- kill switch state: 재시작 뒤에도 위험 차단 상태를 보존한다.

Memory Hot Store는 빠른 판단을 위한 상태이며, 장애 복구의 원장은 아니다. 재시작 시에는 SQLite operational state와 preloaded master/settings를 기반으로 재구성한다.

## 이번 PR의 경계

이번 PR에서는 다음을 하지 않는다.

- 실제 PostgreSQL 연결 생성
- PostgreSQL migration 실행
- 기존 `TradingDatabase` 호출 경로 변경
- 주문 enqueue/dispatch/ack 로직 변경
- Tick write path 변경

이번 PR은 문서, 테이블 배치 계획, 스토리지 인터페이스 스켈레톤을 통해 다음 구현 PR의 경계를 만든다.
