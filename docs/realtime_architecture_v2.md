# Realtime Architecture V2

이 문서는 `suseok_ai` 장중 자동매매 런타임을 성능/안정성 중심으로 재설계하기 위한 Runtime Boundary & Architecture Contract이다. 이번 PR은 실제 주문 경로를 바꾸지 않고, Gateway/Core/Order/Dashboard의 책임 경계와 다음 migration 단위를 고정한다.

## 설계 원칙

- 32bit Kiwoom Gateway는 Kiwoom I/O만 담당한다. 전략 판단, 대시보드 집계, 후보 스캔, 리스크 계산을 수행하지 않는다.
- 64bit Core는 append-only Event Log를 기준으로 GatewayEvent를 replay 가능한 단위로 처리한다.
- 장중 hot path는 `price_tick -> MarketDataService -> dirty_codes -> incremental evaluation`으로 흐른다. 전체 후보 full scan은 피한다.
- EntryEngine은 `OrderIntent`까지만 만든다. 실제 GatewayCommand 생성은 RiskManager를 통과한 OrderManager의 책임이다.
- Dashboard는 `dashboard_read_model`만 읽는다. 전략 계산, raw event 조립, command history 분석은 개발자 상세 화면으로 분리한다.
- LIVE_SIM/LIVE_REAL 활성화는 이 문서 범위가 아니다. `send_order_allowed=false`, `OrderManagerConfig.enabled=false`, `observe_only=true` 기본값을 유지한다.

## 현재 코드 점검 및 이동 기준

| 현재 구성요소 | 현재 책임 | 유지할 부분 | V2에서 이동/분리할 부분 |
|---|---|---|---|
| `trading.broker.gateway_state.GatewayStateStore` | Gateway 상태, 최근 이벤트, latest tick cache, command queue facade | heartbeat/orderability/status snapshot, command persistence 연결, command dedupe/dispatch API | latest tick cache와 event processing은 Event Log + MarketDataService로 이동. GatewayStateStore는 Gateway health/command outbox 중심으로 축소 |
| `trading.broker.command_queue.CommandQueue` | command TTL, priority, attempts, idempotency/dedupe | idempotency key, order command priority, TTL/max_attempts, duplicate rejection | append-only event log와 별도 유지. command lifecycle은 OrderManager reconcile 모델과 연결 |
| `trading_app.runtime_adapters.GatewayEventMarketDataBridge` | price_tick을 MarketDataStore/CandleBuilder에 반영 | Kiwoom payload normalization 경험, index/stock 구분, warning sink | Core event consumer로 이동. raw tick full DB write 금지, dirty_codes 생성, batch flush 책임 추가 |
| `trading_app.runtime_adapters.GatewayEventThemeRuntimeBridge` | price_tick을 theme runtime에 직접 전달 | theme adapter 변환 로직 | ThemeBoard는 MarketDataService snapshot 또는 dirty_codes 기반 1초 tick으로 실행 |
| `trading_app.runtime_adapters.GatewayCommandRealtimeClient` | realtime subscription GatewayCommand enqueue | command idempotency, stale dispatched recovery | subscription policy는 Core에 유지하되 GatewayCommandPort 뒤로 숨김 |
| `trading_app.runtime_adapters.GatewayCommandConditionAdapter` | condition load/send/stop command enqueue, include event handling | condition profile resolution, stale send_condition recovery | Gateway는 condition event만 emit. include는 Candidate FSM의 DISCOVERED/HYDRATING 입력으로 처리 |
| `trading.strategy.reboot_v2_runtime.RebootV2Runtime` | 주기형 runtime cycle orchestration | 안전 기본값: `order_path_enabled=false`, `send_order_allowed=false`, legacy entry path disabled | full cycle scan을 dirty-code incremental evaluator와 1초 board/regime tick으로 분리 |
| `trading.strategy.entry_engine.EntryEngine` | WATCHING 후보 full scan 후 EntryDecision 저장 | DATA/THEME/MARKET/ROLE/PRICE 단계별 PASS/WAIT/BLOCK 모델, TR price only 차단 | dirty candidate만 평가. `RISK_PRECHECK` 결과와 `next_required_action` 저장. 주문 생성 금지 유지 |
| `trading.strategy.order_manager.OrderManagerRuntimePipeline` | EntryDecision/ExitDecision에서 ManagedOrderIntent 생성, risk 통과 후 GatewayCommand enqueue | ManagedOrderIntent/ManagedOrder 모델, risk gate, idempotency, command queue 연동 | LIVE_SIM 전용 가정 제거. OrderIntent -> RiskManager -> GatewayCommand -> Ack/Fill/Reconcile 표준 흐름으로 승격 |
| `trading.strategy.order_risk.OrderRiskManager` | broker env/account/order count/position/spread/stale checks | 실전 운영 리스크 gate 대부분 | Risk state를 STOP_NEW_BUY/REDUCE_ONLY/KILL_SWITCH_ACTIVE로 Core health와 연결 |
| Dashboard API/read sections | runtime snapshot, entry/order/theme 등 조립 | 현재 운영 가시성 | 1초 `dashboard_read_model` snapshot만 읽도록 변경. 계산/집계는 Core snapshot writer로 이동 |

## Runtime Ports

새 계약은 [trading/runtime_ports.py](../trading/runtime_ports.py)에 정의한다.

- `EventLogPort`: 모든 `GatewayEvent`를 append-only로 저장하고 pending replay, processed/failed 마킹을 담당한다.
- `GatewayCommandPort`: Core가 GatewayCommand를 enqueue/dispatch할 때 바라보는 command outbox 경계다.
- `MarketDataServicePort`: `price_tick` 정규화, latest snapshot, candle ring buffer, dirty_codes, batch flush를 담당한다.
- `CandidateFsmPort`: condition include/TR/tick/order event를 후보 상태 전이로 반영한다.
- `StrategyEvaluatorPort`: dirty code 단위 incremental evaluation을 수행한다.
- `RiskManagerPort`: EntryDecision의 risk precheck와 OrderIntent 승인을 담당한다.
- `OrderManagerPort`: OrderIntent 생성, GatewayCommand enqueue, ack/fill/cancel/reconcile을 담당한다.
- `DashboardReadModelPort`: CoreEvent를 1초 read model snapshot으로 반영하고 Dashboard API에 제공한다.

`GatewayEvent`, `GatewayCommand`, `EntryDecision`, `ManagedOrderIntent`, `ManagedOrder`는 기존 모델을 재사용한다. V2 전용 envelope인 `MarketDataSnapshot`, `CandidateStateTransition`, `EntryDecisionEnvelope`, `OrderIntent`, `ManagedOrderEnvelope`는 책임 경계와 감사 추적 필드를 명확히 하기 위한 계약이다.

## 책임 경계

### 32bit Kiwoom Gateway Process

Gateway는 다음만 수행한다.

- Kiwoom login/session 유지
- condition load/send/stop과 include/remove event emit
- realtime registration과 price tick emit
- TR request/response emit
- send_order/cancel_order/modify_order 실행
- order ack/fill/balance event emit
- heartbeat/gateway_error emit

Gateway는 다음을 수행하지 않는다.

- 후보 승격/전략 판단
- ThemeBoard/MarketRegime 계산
- risk check
- dashboard 집계
- raw tick analytics 또는 candle build

### 64bit Core

Core는 GatewayEvent를 Event Log에 append한 뒤 consumer별로 처리한다.

1. Event Log append 및 dedupe
2. MarketDataService update와 dirty_codes 생성
3. Candidate FSM transition
4. dirty code debounce 후 StrategyEvaluator 실행
5. EntryDecision 저장 및 OrderIntent 생성
6. RiskManager 승인 후 OrderManager가 GatewayCommand enqueue
7. Ack/Fill/Balance/Reconcile 반영
8. Dashboard read model snapshot 갱신

### Order Boundary

EntryEngine은 주문을 만들지 않는다. EntryEngine은 `EntryDecisionEnvelope`에 단계별 결과와 `next_required_action`을 저장한다.

OrderManager만 `OrderIntent`를 만들 수 있고, RiskManager 승인 전에는 GatewayCommand를 만들 수 없다. 모든 GatewayCommand에는 `idempotency_key`가 있어야 한다.

### Dashboard Boundary

Dashboard API는 `dashboard_read_model`만 읽는다. 메인 화면은 다음 영역으로 제한한다.

- 시장국면
- 주도테마 TOP5
- 진입 후보
- 안 산 이유 TOP3
- 주문/리스크
- 데이터 품질
- 시스템 헬스

raw JSON, command history, legacy hybrid detail은 개발자 상세 화면으로 이동한다.

## Event Log 기반 복구 흐름

Event Log record 필수 필드:

- `event_id`
- `event_type`
- `dedupe_key`
- `received_at`
- `processed_at`
- `processing_status`
- `payload_json`
- `source`
- `command_id`
- `error`

정상 처리:

```text
GatewayEvent
  -> EventLog.append(status=PENDING, dedupe_key)
  -> Core consumer transaction
  -> CoreEvent emission
  -> EventLog.mark_processed(processed_at)
```

장애 후 복구:

```text
Core restart
  -> EventLog.pending_gateway_events(limit=N)
  -> event_id order replay
  -> idempotent MarketData/Candidate/Order handlers
  -> processed_at update
```

dedupe 기준:

- Gateway native event id가 있으면 `event_id`
- order/fill은 `account:order_no:execution_id`
- command ack는 `command_id:status`
- price_tick은 원칙적으로 append하되, MarketDataService에서 per-code timestamp와 sequence로 out-of-order drop
- heartbeat는 append하되 read model은 latest만 유지

복구 중 불일치 처리:

- command가 DISPATCHED 상태로 오래 남으면 stale dispatched로 expire/requeue 검토
- order ack는 있는데 fill/order_no가 없으면 `RECONCILE_REQUIRED`
- broker balance와 managed order가 불일치하면 `STOP_NEW_BUY`
- 포지션 감소만 허용해야 하면 `REDUCE_ONLY`

## MarketDataService와 Dirty Code

MarketDataService 책임:

- `price_tick` 정규화
- `latest_tick_by_code` in-memory cache
- 1m/3m/5m candle ring buffer
- VWAP, turnover, execution_strength, spread, day_high/day_low snapshot
- tick freshness/data quality 판단
- dirty_codes queue 생성
- 1~3초 batch flush

금지 사항:

- raw tick DB full write
- tick마다 전체 후보/테마/대시보드 계산
- TR backfill 가격을 realtime tick과 동일하게 취급

dirty code 생성 기준:

- fresh realtime price_tick 수신
- candle boundary update
- spread/data quality 변화
- theme role 또는 market regime snapshot 변화로 해당 code 재평가 필요
- order/fill/balance event로 position/order dependent evaluation 필요

평가 cadence:

- candidate별 debounce: 100~300ms
- ThemeBoard: 1초 단위
- MarketRegime: 1초 단위
- Dashboard read model: 1초 단위

## Candidate FSM

V2 상태:

```text
DISCOVERED
  -> HYDRATING
  -> WATCHING
  -> SETUP_READY
  -> TIMING_READY
  -> ORDER_INTENT_CREATED
  -> ORDER_PENDING
  -> POSITION_OPEN
  -> EXIT_PENDING
  -> CLOSED
```

`WAIT_DATA`, `WAIT_MARKET`, `WAIT_THEME`, `WAIT_PRICE`, `BLOCK_RISK`는 상태가 아니다. `blocking_stage`와 `reason_code`로 저장한다.

전이 규칙:

- condition include만으로 `ORDER_INTENT_CREATED` 금지
- condition include는 `DISCOVERED` 또는 기존 candidate의 `last_seen_at` 갱신만 가능
- TR backfill 가격만으로 `SETUP_READY`/`TIMING_READY` 금지
- fresh realtime tick이 없으면 `WATCHING` 이상 승격 금지
- hydration이 끝나도 realtime freshness가 없으면 `HYDRATING + WAIT_DATA`
- risk block은 candidate state를 오염시키지 않고 `blocking_stage=BLOCK_RISK`로 남김

## Strategy Evaluator

EntryEngine 판단 순서:

1. `DATA_READY`
2. `THEME_READY`
3. `MARKET_ALLOWED`
4. `ROLE_ALLOWED`
5. `PRICE_TIMING_READY`
6. `RISK_PRECHECK`

각 단계는 `PASS`, `WAIT`, `DATA_WAIT`, `BLOCK` 중 하나를 반환하고 다음 필드를 저장한다.

- `reason_codes`
- `next_required_action`
- `details`
- source event ids

`OBSERVE_READY`가 되더라도 EntryEngine은 직접 주문하지 않는다. OrderIntent 생성 가능 여부만 OrderManager가 읽을 수 있는 형태로 저장한다.

## Order Flow

표준 흐름:

```text
EntryDecisionEnvelope(OBSERVE_READY)
  -> OrderManager.create_intent()
  -> RiskManager.approve_intent()
  -> ManagedOrder(PENDING_LOCAL)
  -> GatewayCommand(send_order, idempotency_key)
  -> CommandQueue.enqueue()
  -> Gateway dispatch
  -> order_ack/order_fill/balance_snapshot
  -> OrderManager.apply_gateway_event()
  -> reconcile()
```

필수 risk checks:

- account mode
- simulation broker
- real broker block
- max daily orders
- max position count
- theme exposure
- stale tick
- spread
- kill switch
- RISK_OFF

장애 처리:

- duplicate `idempotency_key`: intent/order/command 재생성 금지
- ack timeout: `RECONCILE_REQUIRED`
- fill without local order: broker event를 우선 보존하고 managed order matching 시도
- balance mismatch: `STOP_NEW_BUY`
- 심각한 불일치: `REDUCE_ONLY` 또는 kill switch

## Migration Plan

1. Runtime ports + architecture contract
   - 이번 PR
   - 실제 order path 변경 없음
   - import smoke test만 추가

2. Event Log skeleton
   - `gateway_event_log` table 추가
   - GatewayStateStore의 recent event와 독립적으로 append-only 저장
   - pending replay API 추가

3. MarketDataService extraction
   - `GatewayEventMarketDataBridge`를 service consumer로 이동
   - latest snapshot, candle ring buffer, dirty_codes queue 추가
   - raw tick full write 금지와 batch flush 적용

4. Candidate FSM migration
   - 기존 `CandidateState`와 V2 state mapping 추가
   - WAIT_* 상태를 blocking_stage/reason_code로 이동
   - condition include/TR/tick 승격 규칙 적용

5. Dirty-code StrategyEvaluator
   - EntryEngine full scan 제거
   - candidate debounce 100~300ms
   - ThemeBoard/MarketRegime 1초 cadence 분리

6. OrderIntent/Risk/OrderManager hardening
   - EntryDecisionEnvelope -> OrderIntent 표준화
   - ack/fill/cancel/reconcile event handling 통합
   - STOP_NEW_BUY/REDUCE_ONLY state machine 연결

7. Dashboard read model
   - 1초 snapshot writer 추가
   - Dashboard API가 read model만 읽도록 변경
   - raw JSON/command history/legacy detail은 developer detail로 이동

8. Controlled activation
   - OBSERVE smoke
   - replay/backtest
   - LIVE_SIM canary
   - 별도 승인 후 live order guard 검토

## 이번 PR 검증 기준

- 실제 order path 변경 없음
- `send_order_allowed` 기본 false 유지
- `OrderManagerConfig.enabled=false`, `observe_only=true` 기본 유지
- LIVE_SIM/LIVE_REAL 활성화 없음
- `trading.runtime_ports` import 가능
- 기존 pytest가 깨지지 않음

## PR 2 구현 상태: Event Log Skeleton

PR 2에서는 Event Log를 shadow 구조로 추가한다. 기존 `GatewayStateStore.record_event()`의 in-memory 상태 갱신, command queue, StrategyRuntime/RebootV2Runtime, OrderManager, Dashboard 경로는 변경하지 않는다.

구현 범위:

- `gateway_event_log` SQLite table 추가
- `storage.event_log.EventLogRepository` 추가
- `trading.runtime_ports.EventLogAppendResult` 추가
- `GatewayStateStore(event_log_store=...)` 주입 시에만 shadow append
- append 실패 시 기존 runtime 중단 금지
- duplicate `dedupe_key`는 duplicate result로 반환하고 추가 row를 만들지 않음

`gateway_event_log` schema:

| field | 설명 |
|---|---|
| `id` | SQLite row id |
| `event_id` | GatewayEvent event id |
| `event_type` | GatewayEvent type |
| `dedupe_key` | replay/dedupe key, unique |
| `source` | event source |
| `command_id` | command 연계 id |
| `code` | 종목 코드가 있는 이벤트의 normalized code |
| `trade_date` | KST 기준 거래일 |
| `payload_json` | replay 가능한 GatewayEvent JSON |
| `received_at` | GatewayEvent timestamp 또는 append 시각 |
| `processed_at` | consumer 처리 완료 시각 |
| `processing_status` | `PENDING`, `PROCESSED`, `FAILED` |
| `error` | 처리/직렬화 실패 메시지 |
| `created_at` | append row 생성 시각 |

Index:

- unique `dedupe_key`
- `event_type`
- `processing_status`
- `received_at`
- `trade_date, code`
- `command_id`
- `event_id`

Repository API:

- `append_gateway_event(event, dedupe_key="")`
- `pending_gateway_events(limit=100, event_type=None)`
- `mark_processed(event_log_id_or_event_id, processed_at=...)`
- `mark_failed(event_log_id_or_event_id, error=...)`
- `find_by_dedupe_key(dedupe_key)`
- `event_log_snapshot()`

Feature flag 기본값:

| flag | default | 의미 |
|---|---:|---|
| `TRADING_EVENT_LOG_ENABLED` | `true` | Event Log shadow append 활성화 |
| `TRADING_EVENT_LOG_PRICE_TICK_ENABLED` | `false` | `price_tick` full logging 비활성 |
| `TRADING_EVENT_LOG_HEARTBEAT_ENABLED` | `false` | `heartbeat` full logging 비활성 |
| `TRADING_EVENT_LOG_MAX_PENDING_REPLAY` | `500` | pending replay 조회 상한 |

Dedupe 기준:

- `event_id`가 있으면 `event:{event_id}`를 우선 사용
- `command_ack`: `command_id + status`
- `order_ack/order_fill/execution/fill`: `account + order_no + execution_id`
- `condition_event/condition_include/condition_remove`: `condition_name + condition_index + code + timestamp bucket`
- `tr_response`: `command_id` 또는 `request_id`
- `price_tick`: 기본 logging disabled이면 append하지 않음
- `heartbeat`: 기본 logging disabled이면 append하지 않음

현재 Event Log는 replay 가능한 저장 기반만 제공한다. MarketDataService, Candidate FSM, StrategyEvaluator, OrderManager consumer 전환은 다음 PR 범위다. 특히 `price_tick` full logging은 장중 DB write 부하를 만들 수 있으므로 기본 비활성으로 유지하며, MarketDataService PR에서 1~3초 batch flush 및 dirty-code 정책과 함께 다시 다룬다.

## PR 3 구현 상태: MarketDataService Extraction

PR 3에서는 `GatewayEventMarketDataBridge` 내부의 price tick 정규화와 market data update 경계를 `MarketDataService`로 분리한다. 이 변경은 extraction/shadow 성격이며, EntryEngine incremental evaluation 전환이나 Candidate FSM migration은 포함하지 않는다.

구현 범위:

- `trading.strategy.market_data_service.MarketDataService` 추가
- `MarketDataSnapshot` 필드 확장
- `DirtyCodeQueue`와 `DirtyReason` 추가
- `GatewayEventMarketDataBridge`가 기존 public API를 유지한 채 `MarketDataService`로 위임
- 기존 `MarketDataStore`, `CandleBuilder`, `MarketIndexStore`, `RealtimeDataQualityTracker` 업데이트 경로 유지
- batch flush hook 추가. 기본값은 disabled이며 저장소가 없으면 no-op

`MarketDataService` 책임:

- `price_tick` payload 정규화
- `latest_snapshot_by_code` in-memory cache 관리
- 기존 `MarketDataStore` latest tick update
- 기존 `CandleBuilder` 1m candle update 및 3m/5m aggregation 준비
- VWAP, turnover, execution_strength, spread, day_high/day_low snapshot 관리
- tick freshness 및 data quality status 산출
- `source_event_id`를 snapshot metadata에 보존
- dirty code 생성
- batch flush hook 제공

`MarketDataSnapshot` 주요 필드:

- `code`, `name`
- `price`, `change_rate`, `trade_value`, `cum_volume`
- `execution_strength`, `best_ask`, `best_bid`, `spread_ticks`
- `day_high`, `day_low`, `open_price`, `vwap`
- `tick_timestamp`, `tick_age_sec`, `freshness_status`
- `data_quality_status`, `source_event_id`, `price_source`
- `updated_at`, `metadata`

Dirty code 생성 기준:

- fresh stock `price_tick` 처리 성공: `PRICE_TICK`
- 1m/3m/5m completed candle count 증가: `CANDLE_BOUNDARY`
- data quality status 변화: `DATA_QUALITY_CHANGED`
- spread tick 변화: `SPREAD_CHANGED`
- `ORDER_EVENT`, `POSITION_EVENT`, `THEME_ROLE_CHANGED`, `MARKET_REGIME_CHANGED`는 다음 PR consumer 연결을 위한 reason enum만 준비

Feature flag 기본값:

| flag | default | 의미 |
|---|---:|---|
| `TRADING_MARKET_DATA_SERVICE_ENABLED` | `true` | MarketDataService update 활성화 |
| `TRADING_MARKET_DATA_DIRTY_QUEUE_ENABLED` | `true` | dirty code queue 생성 활성화 |
| `TRADING_MARKET_DATA_BATCH_FLUSH_ENABLED` | `false` | batch flush write 비활성 |
| `TRADING_MARKET_DATA_MAX_TICK_AGE_SEC` | `10` | freshness 판단 기준 |
| `TRADING_MARKET_DATA_DIRTY_DEBOUNCE_MS` | `200` | dirty reason debounce |

Data quality 기준:

- latest tick 없음: consumer 관점에서 `MISSING_TICK`
- tick age 초과: `STALE_TICK`
- `price <= 0`: `MISSING_PRICE`
- `trade_value <= 0` 및 `cum_volume <= 0`: `TURNOVER_MISSING`
- `price_source=TR_BACKFILL`: `TR_BACKFILL_PRICE_ONLY`

현재 dirty codes는 생성만 한다. EntryEngine incremental evaluation에 연결하지 않는다. raw tick full DB write는 여전히 금지이며, batch flush는 기본 disabled다. 다음 PR은 Candidate FSM migration 또는 Dirty-code StrategyEvaluator 중 하나를 선택할 수 있으나, 운영 관점에서는 `Candidate FSM + blocking_stage`를 먼저 고정한 뒤 dirty-code evaluator를 붙이는 순서를 권장한다.

## PR 4 구현 상태: Candidate FSM Migration

PR 4에서는 기존 `CandidateState`를 즉시 제거하지 않고, V2 candidate FSM을 metadata와 transition journal로 병행 도입한다. 기존 StrategyRuntime/RebootV2Runtime/EntryEngine/OrderManager의 판단 및 주문 경로는 변경하지 않는다.

V2 상태:

- `DISCOVERED`
- `HYDRATING`
- `WATCHING`
- `SETUP_READY`
- `TIMING_READY`
- `ORDER_INTENT_CREATED`
- `ORDER_PENDING`
- `POSITION_OPEN`
- `EXIT_PENDING`
- `CLOSED`
- `REMOVED`
- `EXPIRED`

기존 `CandidateState` 매핑:

| 기존 상태 | V2 표현 |
|---|---|
| `DETECTED` | `DISCOVERED` |
| `HYDRATING` | `HYDRATING` |
| `WATCHING` | `WATCHING` |
| `WAIT_DATA` | `WATCHING + blocking_stage=DATA` |
| `READY` | `TIMING_READY` 호환 매핑. 신규 승격은 다음 EntryEngine PR까지 보류 |
| `BLOCKED` | `WATCHING + blocking_stage=RISK/PRICE/MARKET` |
| `EXPIRED` | `EXPIRED` |
| `REMOVED` | `REMOVED` |

`WAIT_DATA`, `WAIT_MARKET`, `WAIT_THEME`, `WAIT_PRICE`, `BLOCK_RISK`는 V2 상태가 아니다. V2에서는 `candidate.metadata["candidate_fsm"]` 아래의 다음 필드로 관리한다.

- `v2_state`
- `blocking_stage`
- `primary_reason_code`
- `reason_codes`
- `next_required_action`
- `source_event_ids`
- `last_transition_at`

`blocking_stage`:

- `NONE`
- `DATA`
- `THEME`
- `MARKET`
- `ROLE`
- `PRICE`
- `RISK`
- `ORDER`
- `SYSTEM`

대표 `reason_code`:

- `LATEST_TICK_MISSING`
- `LATEST_TICK_STALE`
- `TR_BACKFILL_PRICE_ONLY`
- `HYDRATION_PENDING`
- `THEME_NOT_READY`
- `MARKET_RISK_OFF`
- `ROLE_NOT_ALLOWED`
- `PRICE_TIMING_NOT_READY`
- `CHASE_RISK`
- `VWAP_OVEREXTENDED`
- `ORDER_RISK_BLOCKED`
- `GATEWAY_UNHEALTHY`

Transition 저장:

`candidate_state_transitions` table을 추가했다.

| field | 설명 |
|---|---|
| `id` | row id |
| `candidate_id` | candidate id |
| `trade_date` | 거래일 |
| `code` | 종목 코드 |
| `from_state`, `to_state` | V2 state transition |
| `blocking_stage` | 현재 차단 단계 |
| `reason_code` | primary reason |
| `reason_codes_json` | reason list |
| `next_required_action` | 다음 필요 조치 |
| `source_event_id` | 원천 event id |
| `source_event_type` | 원천 event type |
| `source_component` | transition 기록 component |
| `details_json` | 감사 payload |
| `occurred_at`, `created_at` | 발생/저장 시각 |

전이 규칙:

- condition include는 `DISCOVERED` 생성/갱신만 가능하다.
- condition include만으로 `SETUP_READY`, `TIMING_READY`, `OrderIntent`, `GatewayCommand`를 만들 수 없다.
- hydration request는 `DISCOVERED -> HYDRATING` transition을 기록한다.
- hydration result가 TR backfill 가격만 가진 경우 `SETUP_READY/TIMING_READY`로 승격하지 않고 `blocking_stage=DATA`, `reason_code=TR_BACKFILL_PRICE_ONLY` 또는 `LATEST_TICK_MISSING`을 기록한다.
- fresh realtime tick이 확인되면 `WATCHING`까지만 승격한다. `SETUP_READY/TIMING_READY`는 다음 EntryEngine PR에서 처리한다.
- stale/missing tick은 기존 state를 가능한 유지하고 `blocking_stage=DATA`와 `reason_code=LATEST_TICK_STALE/LATEST_TICK_MISSING`으로 표현한다.
- risk block은 candidate state를 `BLOCKED`로 오염시키지 않고 `blocking_stage=RISK`, `reason_code=ORDER_RISK_BLOCKED`로 표현한다.

RebootV2Runtime snapshot에는 최소 요약만 추가한다.

- `candidate_fsm.status`
- `candidate_fsm.state_counts`
- `candidate_fsm.blocking_stage_counts`
- `candidate_fsm.top_reason_codes`
- `candidate_fsm.transition_count`
- `candidate_fsm.last_transition_at`

현재 dirty-code StrategyEvaluator 연결은 아직 없다. 주문 경로와 EntryEngine 판단 결과도 변경하지 않는다. 다음 PR에서는 `Dirty-code StrategyEvaluator`를 붙이되, 이번 PR에서 만든 `v2_state/blocking_stage/reason_code`를 입력 조건으로 사용한다.
