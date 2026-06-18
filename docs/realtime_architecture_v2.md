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

## PR 5 구현 상태: Dirty-code StrategyEvaluator

PR 5에서는 `MarketDataService`의 `DirtyCodeQueue`를 소비하는 `DirtyStrategyEvaluator`를 추가했다. 기존 EntryEngine 판단식은 바꾸지 않고, full scan `build()` API도 유지한다. 신규 경로는 dirty code로 좁혀진 후보만 `EntryEngine.evaluate_candidates()` / `EntryEngine.evaluate_codes()`로 평가하는 shadow 구조다.

기본 설정:

| env | default |
|---|---:|
| `TRADING_DIRTY_EVALUATOR_ENABLED` | `true` |
| `TRADING_DIRTY_EVALUATOR_SHADOW_MODE` | `true` |
| `TRADING_DIRTY_EVALUATOR_MAX_CODES_PER_CYCLE` | `50` |
| `TRADING_DIRTY_EVALUATOR_MAX_CANDIDATES_PER_CYCLE` | `100` |
| `TRADING_DIRTY_EVALUATOR_DEBOUNCE_MS` | `200` |
| `TRADING_DIRTY_EVALUATOR_FALLBACK_FULL_SCAN` | `true` |
| `TRADING_DIRTY_EVALUATOR_THEME_CADENCE_SEC` | `1` |
| `TRADING_DIRTY_EVALUATOR_MARKET_CADENCE_SEC` | `1` |
| `TRADING_DIRTY_EVALUATOR_SAVE_DECISIONS` | `true` |
| `TRADING_DIRTY_EVALUATOR_ORDER_INTENT_ENABLED` | `false` |

구조:

- `trading/strategy/dirty_strategy_evaluator.py`
- `DirtyStrategyEvaluatorConfig`
- `DirtyStrategyEvaluator`
- `DirtyStrategyEvaluatorRuntimePipeline`
- `DirtyStrategyEvaluatorResult`

Dirty code 소비 규칙:

- `PRICE_TICK`: 해당 code의 active candidate만 평가한다.
- `CANDLE_BOUNDARY`: 해당 code의 candle/VWAP/price timing 재평가로 합쳐진다.
- `DATA_QUALITY_CHANGED`: 해당 code의 `DATA_READY` 결과 재평가로 합쳐진다.
- `SPREAD_CHANGED`: 해당 code의 price/risk 관련 reason으로 합쳐진다.
- `MARKET_REGIME_CHANGED`: active candidate 전체를 대상으로 market 단계만 좁혀 재평가할 수 있도록 전체 후보 pool을 연다.
- `THEME_ROLE_CHANGED`, `ORDER_EVENT`, `POSITION_EVENT`: reason enum과 summary는 보존하되 실제 OrderManager 연계는 다음 PR로 미룬다.

Candidate debounce:

- candidate id 또는 code 기준으로 `last_evaluated_at`을 메모리에 기록한다.
- `TRADING_DIRTY_EVALUATOR_DEBOUNCE_MS` 이내 재평가는 skip/coalesce한다.
- skip 수는 `dirty_evaluator.debounced_count`에 노출한다.

EntryEngine partial evaluation:

- 기존 `EntryEngine.build()` full scan API는 유지한다.
- 신규 API:
  - `EntryEngine.evaluate_candidates(candidates, trade_date, now, save)`
  - `EntryEngine.evaluate_codes(codes, trade_date, now, save)`
- `save=False`는 shadow comparison용이며 candidate metadata를 갱신하지 않는다.
- 판단 순서와 reason 생성은 기존 EntryEngine 내부 로직을 그대로 사용한다.

Candidate FSM 연동:

- `DATA_READY` 실패: `blocking_stage=DATA`
- `THEME_READY` 실패: `blocking_stage=THEME`
- `MARKET_ALLOWED` 실패: `blocking_stage=MARKET`
- `ROLE_ALLOWED` 실패: `blocking_stage=ROLE`
- `PRICE_TIMING_READY` 실패: `v2_state=SETUP_READY`, `blocking_stage=PRICE`
- 모든 단계 통과: `v2_state=TIMING_READY`, `blocking_stage=NONE`, `reason_code=OBSERVE_READY_ORDER_DISABLED`
- fresh realtime tick이 없거나 `price_source=TR_BACKFILL`이면 `SETUP_READY/TIMING_READY`로 승격하지 않는다.
- transition journal은 상태/primary reason이 바뀔 때만 기록해 tick마다 과도하게 증가하지 않도록 한다.

RebootV2Runtime 연결:

- `dirty_evaluator` snapshot section을 추가했다.
- dirty evaluator가 enabled이면 기존 `entry_engine` full scan pipeline은 직접 실행하지 않고 `SHADOWED_BY_DIRTY_EVALUATOR`로 표시한다.
- dirty code가 없는 cycle에서는 EntryEngine full scan을 수행하지 않는다.
- dirty code가 있는 cycle에서만 shadow comparison을 위해 `save=False` full scan을 수행할 수 있다.
- ThemeBoard / MarketRegime cadence는 dirty evaluator config의 1초 기본값으로 조정된다.

Snapshot fields:

- `dirty_evaluator.status`
- `dirty_evaluator.enabled`
- `dirty_evaluator.shadow_mode`
- `dirty_evaluator.dirty_code_count`
- `dirty_evaluator.evaluated_code_count`
- `dirty_evaluator.evaluated_candidate_count`
- `dirty_evaluator.debounced_count`
- `dirty_evaluator.skipped_count`
- `dirty_evaluator.saved_decision_count`
- `dirty_evaluator.full_scan_fallback_used`
- `dirty_evaluator.last_evaluated_at`
- `dirty_evaluator.duration_ms`
- `dirty_evaluator.top_dirty_reasons`
- `dirty_evaluator.blocking_stage_counts`
- `dirty_evaluator.warnings`
- `dirty_evaluator.shadow_comparison`

주문 경로 제한:

- `DirtyStrategyEvaluator`는 `OrderIntent`를 만들지 않는다.
- `TRADING_DIRTY_EVALUATOR_ORDER_INTENT_ENABLED` 기본값은 `false`이며 factory에서도 `false`로 강제한다.
- `OBSERVE_READY`가 나와도 `TIMING_READY + OBSERVE_READY_ORDER_DISABLED`까지만 기록한다.
- `GatewayCommand`, `send_order`, `runtime_order_intents`, `entry_plans` 생성 경로는 변경하지 않았다.

다음 PR은 `OrderIntent -> RiskManager -> CommandQueue -> Ack/Fill/Reconcile` hardening이다. 그 전까지 dirty evaluator는 운영 판단 cadence와 read model 개선용 shadow evaluator로만 사용한다.

## PR 6 구현 상태: OrderIntent / Risk / OrderManager Hardening

PR 6에서는 EntryEngine/DirtyStrategyEvaluator 결과가 바로 `GatewayCommand`로 이어지지 않도록 주문 경계를 더 단단히 분리했다. 기본값은 계속 observe-only이며 실제 Kiwoom `send_order` command는 생성하지 않는다.

기본 설정:

| env | default |
|---|---:|
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| `TRADING_ORDER_INTENT_SHADOW_MODE` | `true` |
| `TRADING_ORDER_MANAGER_ENABLED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_CREATE_LOCAL_ORDER` | `false` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_MANAGER_REQUIRE_SIMULATION_BROKER` | `true` |
| `TRADING_ORDER_MANAGER_BLOCK_REAL_BROKER` | `true` |
| `TRADING_ORDER_MANAGER_MAX_DAILY_BUY_ORDERS` | `3` |
| `TRADING_ORDER_MANAGER_MAX_DAILY_SELL_ORDERS` | `10` |
| `TRADING_ORDER_MANAGER_MAX_DAILY_ORDERS_PER_CODE` | `1` |
| `TRADING_ORDER_MANAGER_MAX_OPEN_POSITIONS` | `3` |
| `TRADING_ORDER_MANAGER_MAX_THEME_EXPOSURE_COUNT` | `2` |
| `TRADING_ORDER_MANAGER_MAX_ORDER_AMOUNT` | `100000` |
| `TRADING_ORDER_MANAGER_STALE_TICK_SEC` | `10` |
| `TRADING_ORDER_MANAGER_ACK_TIMEOUT_SEC` | `30` |
| `TRADING_ORDER_MANAGER_RECONCILE_REQUIRED_BLOCKS_BUY` | `true` |
| `TRADING_SEND_ORDER_ALLOWED` | `false` |

OrderIntent 표준 구조:

- `trade_date`
- `source`
- `side`: `BUY`, `SELL`, `CANCEL_BUY`, `CANCEL_SELL`
- `code`, `name`
- `account`
- `quantity`, `price`, `hoga`
- `candidate_id`, `decision_id`, `position_id`
- `theme_id`, `theme_name`
- `reason`
- `idempotency_key`
- `status`
- `created_at`
- `details`

BUY idempotency key:

`buy:{trade_date}:{candidate_id}:{code}:{entry_generation}:{price_bucket}`

SELL/CANCEL은 모델과 handler skeleton만 유지한다. 실제 SELL/CANCEL gateway flow 확대는 다음 PR 범위다.

OrderIntent 생성 조건:

- `entry_status=OBSERVE_READY`
- `candidate_fsm.v2_state=TIMING_READY`
- `candidate_fsm.blocking_stage in {"", "NONE"}`
- `price_source != TR_BACKFILL`
- `latest_tick_fresh != false`
- `market_action in {ALLOW_NORMAL, ALLOW_REDUCED}`
- `stock_role in {LEADER, CO_LEADER}`
- `price_location in {GOOD_PULLBACK, PULLBACK_RECLAIM, VWAP_RECLAIM}`
- `limit_price_hint` 또는 현재가가 0보다 큼
- `TRADING_ORDER_INTENT_ENABLED=true`

RiskManager check:

- OrderManager enabled
- LIVE_SIM mode / live sim flag
- Kiwoom login / orderable / gateway heartbeat
- account configured / whitelist
- simulation broker required
- real broker block
- max daily buy/sell/order-per-code
- max open positions
- max theme exposure
- duplicate pending order / existing position
- reconcile required blocks buy
- max quantity / max amount
- stale decision / stale quote / stale tick
- spread
- VI / upper-limit near
- market RISK_OFF / block new entry
- portfolio stop-new-entry / kill-switch recommendation
- STOP_NEW_BUY / REDUCE_ONLY / KILL_SWITCH_ACTIVE state

ManagedOrder lifecycle:

- `INTENT_CREATED`
- `RISK_APPROVED`
- `RISK_REJECTED`
- `LOCAL_ORDER_CREATED`
- `COMMAND_BLOCKED_OBSERVE_ONLY`
- `COMMAND_QUEUED`
- `COMMAND_ACKED`
- `PARTIALLY_FILLED`
- `FILLED`
- `CANCEL_PENDING`
- `CANCELLED`
- `RECONCILE_REQUIRED`
- `FAILED`
- `EXPIRED`

이번 PR 기본 경로:

1. EntryDecision snapshot을 읽는다.
2. `TRADING_ORDER_INTENT_ENABLED=false`이면 아무 intent도 만들지 않는다.
3. flag가 켜진 경우 local `ManagedOrderIntent`를 저장한다.
4. RiskManager가 승인/거절을 기록한다.
5. 승인되고 `TRADING_ORDER_MANAGER_CREATE_LOCAL_ORDER=true`인 경우 local `ManagedOrder`를 저장한다.
6. observe-only 또는 `TRADING_SEND_ORDER_ALLOWED=false` 또는 gateway enqueue flag off이면 `COMMAND_BLOCKED_OBSERVE_ONLY`로 기록한다.
7. `send_order` GatewayCommand enqueue는 모든 guard가 통과한 경우에만 가능하다. 기본값에서는 항상 차단된다.

GatewayCommand enqueue guard:

- `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND=true`
- `TRADING_ORDER_MANAGER_OBSERVE_ONLY=false`
- `TRADING_SEND_ORDER_ALLOWED=true`
- mode is `LIVE_SIM`
- live sim flag enabled
- broker env is `SIMULATION`
- real broker not detected
- RiskDecision approved
- kill switch normal

Gateway event / reconcile skeleton:

- `apply_order_ack(event)`
- `apply_order_reject(event)`
- `apply_order_fill(event)`
- `apply_balance_snapshot(event)`
- `apply_order_status_snapshot(event)`
- `reconcile_open_orders()`
- `reconcile_positions()`

현재 구현은 command id, order no, code, side, idempotency key 기반 matching skeleton이다. ack timeout, unmatched fill, balance mismatch는 `RECONCILE_REQUIRED` 또는 `STOP_NEW_BUY`로 이어진다. 이번 PR에서는 자동 청산/취소 주문은 만들지 않는다.

Candidate FSM 연동:

- risk reject: `blocking_stage=RISK`, primary reason은 RiskManager reason
- observe-only command block: `blocking_stage=ORDER`, `reason_code=ORDER_MANAGER_OBSERVE_ONLY`
- reconcile required / unmatched broker event: STOP_NEW_BUY 상태 기록
- candidate legacy state를 `BLOCKED`로 바꾸지 않는다.

RebootV2Runtime snapshot:

- `order_manager_v2.status`
- `enabled`
- `observe_only`
- `intent_enabled`
- `local_order_enabled`
- `gateway_command_enqueue_enabled`
- `send_order_allowed`
- `risk_state`
- `kill_switch_state`
- `created_intent_count`
- `risk_approved_count`
- `risk_rejected_count`
- `local_order_created_count`
- `command_blocked_observe_only_count`
- `queued_command_count`
- `reconcile_required_count`
- `stop_new_buy`
- `reduce_only`
- `last_reject_reason`
- `warnings`

주의:

- 실제 Kiwoom `send_order` 활성화 PR이 아니다.
- `LIVE_SIM/LIVE_REAL` 활성화 PR이 아니다.
- 기본값에서는 intent도 만들지 않는다.
- 테스트에서만 flag를 켜 local intent/order lifecycle과 guard를 검증한다.

다음 PR은 Dashboard read model 분리 또는 controlled LIVE_SIM canary 사전 점검 중 하나를 선택한다. 운영 관점에서는 dashboard read model로 order/risk/reconcile 상태를 먼저 분리한 뒤 controlled canary를 진행하는 순서를 권장한다.

## PR 7 구현 상태: Dashboard Read Model

PR 7에서는 Dashboard V2 API와 WebSocket이 요청마다 runtime/raw DB aggregate를 직접 조립하지 않도록 `dashboard_read_models` 기반 read model을 추가했다. 이번 PR은 UI 재설계, 전략 변경, 주문 활성화 PR이 아니다.

DB schema:

| column | purpose |
|---|---|
| `id` | local row id |
| `view_name` | `main`, `system_health`, `developer_summary` 등 view key |
| `schema_version` | read model schema version |
| `trade_date` | KST trade date |
| `generation` | same view snapshot generation |
| `snapshot_json` | 완성된 Dashboard V2 JSON |
| `checksum` | dynamic timestamp를 제외한 content checksum |
| `status` | `OK`, `STALE`, `CORRUPT`, fallback 상태 |
| `snapshot_at` | snapshot 생성 시각 |
| `source_runtime_cycle_at` | 원천 runtime cycle 시각 |
| `source_runtime_cycle_count` | 원천 runtime cycle count |
| `source_event_watermark` | 추후 EventLog watermark |
| `stale_after_sec` | stale 판정 기준 |
| `build_duration_ms` | build duration |
| `created_at`, `updated_at`, `last_error` | persistence metadata |

Index / constraint:

- `UNIQUE(view_name)`
- `view_name, trade_date`
- `snapshot_at`
- `status`

저장 정책:

- 동일 `view_name` 한 줄만 atomic upsert한다.
- history row를 매초 누적하지 않는다.
- JSON 전체를 완성한 뒤 transaction 안에서 저장한다.
- checksum이 같으면 DB write를 skip한다.
- 저장 실패가 runtime cycle을 실패시키지 않으며 이전 정상 snapshot은 유지된다.
- corrupt JSON은 `CORRUPT`로 보고하고 fallback 대상으로만 취급한다.

구성요소:

- `storage.dashboard_read_model.DashboardReadModelRepository`
- `trading_app.dashboard_read_model.DashboardReadModelConfig`
- `DashboardReadModelService`
- `DashboardReadModelWriter`
- `DashboardReadModelRecord`
- `trading.runtime_ports.DashboardReadModelPort` 확장 계약

Feature flags:

| env | default |
|---|---:|
| `TRADING_DASHBOARD_READ_MODEL_ENABLED` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_API_ENABLED` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_PERSIST_ENABLED` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_WRITE_INTERVAL_SEC` | `1` |
| `TRADING_DASHBOARD_READ_MODEL_STALE_AFTER_SEC` | `5` |
| `TRADING_DASHBOARD_READ_MODEL_SKIP_UNCHANGED` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_FALLBACK_LIVE_BUILD` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_HISTORY_ENABLED` | `false` |
| `TRADING_DASHBOARD_READ_MODEL_SHADOW_COMPARE_ENABLED` | `true` |
| `TRADING_DASHBOARD_READ_MODEL_SHADOW_COMPARE_INTERVAL_SEC` | `30` |
| `TRADING_DASHBOARD_WS_PUSH_INTERVAL_SEC` | `1` |

Writer cadence / coalescing:

- FastAPI lifespan에서 1초 writer loop를 시작한다.
- Runtime cycle 성공 시 dirty mark 후 `write_if_due()`를 호출한다.
- Gateway event ingress도 dirty mark를 추가한다.
- `price_tick`은 dirty mark 대상에서 제외해 tick마다 snapshot을 쓰지 않는다.
- writer가 이미 실행 중이면 중복 실행을 skip한다.
- dirty가 아니면 rebuild하지 않는다.
- write interval 안의 여러 signal은 coalesce한다.
- writer callback/build/save 실패는 warning/metric으로만 남기며 runtime/order path를 중단시키지 않는다.

Read model source:

- `RuntimeSupervisor.snapshot()`의 in-memory latest runtime snapshot
- `RuntimeSupervisor.lightweight_status()`
- `gateway_state.snapshot()`
- `gateway_state.command_snapshot()`
- runtime snapshot 안의 `market_regime`, `theme_board`, `dirty_evaluator`, `candidate_fsm`, `entry_engine`, `exit_engine_reboot`, `position_risk`, `order_manager_v2`

금지한 source:

- API 요청마다 candidate raw scan
- API 요청마다 theme membership join
- API 요청마다 command history 전체 조회
- API 요청마다 BuyZero RCA / postmarket / hybrid validation / replay 재계산

API cutover:

- `GET /api/dashboard-v2/snapshot`: read model 우선
- `GET /api/snapshot?view=v2`: read model 우선
- `GET /api/snapshot`: 기존 schema 유지, legacy aggregate path 유지
- missing/corrupt일 때만 기존 live builder fallback을 허용한다.
- stale이라는 이유만으로 API 요청 시 live rebuild를 실행하지 않는다.
- fallback 결과는 `read_model.source=FALLBACK_LIVE_BUILD`, `fallback_used=true`로 표시한다.

WebSocket:

- `/ws/dashboard`는 latest read model을 사용한다.
- 최초 연결 시 latest snapshot을 전송한다.
- `generation/checksum/status` signature가 바뀔 때만 push한다.
- push cadence 기본 1초다.
- 연결별 raw DB aggregate 조립을 하지 않는다.
- slow/disconnected client 실패는 read model writer에 영향을 주지 않는다.

Stale / recovery:

- `READ_MODEL_STALE`: read model age > `stale_after_sec`
- `RUNTIME_SNAPSHOT_STALE`: runtime source age가 기준을 초과
- `ORDER_RECONCILE_REQUIRED`: order manager가 reconcile/STOP_NEW_BUY 상태
- stale snapshot은 마지막 정상 데이터를 반환하되 `read_model.stale=true`와 safety banner를 붙인다.
- 재기동 시 persisted latest snapshot을 복구하고 `recovered=true`, `stale=true`로 표시할 수 있다.

OrderManager V2 반영 필드:

- `status`
- `enabled`
- `observe_only`
- `intent_enabled`
- `local_order_enabled`
- `gateway_command_enqueue_enabled`
- `send_order_allowed`
- `risk_state`
- `kill_switch_state`
- `created_intent_count`
- `risk_approved_count`
- `risk_rejected_count`
- `local_order_created_count`
- `command_blocked_observe_only_count`
- `queued_command_count`
- `reconcile_required_count`
- `stop_new_buy`
- `reduce_only`
- `last_reject_reason`
- `warnings`

성능 / 관측 지표:

- `build_count`
- `write_count`
- `unchanged_skip_count`
- `coalesced_signal_count`
- `concurrent_write_skip_count`
- `build_duration_ms`
- `db_write_duration_ms`
- `read_duration_ms`
- `api_read_count`
- `fallback_count`
- `stale_read_count`
- `websocket_push_count`
- `websocket_push_skip_unchanged_count`
- `last_build_at`
- `last_write_at`
- `last_error`

Shadow comparison:

- `compare_dashboard_v2_snapshots()` helper를 추가했다.
- API 요청 경로에서는 legacy builder를 호출하지 않는다.
- 비교 cadence 기본값은 30초 flag로 남겨두었고, background metric wiring은 후속 PR에서 확대한다.

안전 확인:

- `send_order_allowed=false` 기본값은 변경하지 않았다.
- `TRADING_SEND_ORDER_ALLOWED=false` 기본값은 변경하지 않았다.
- `TRADING_ORDER_MANAGER_ENABLED=false`, `TRADING_ORDER_MANAGER_OBSERVE_ONLY=true` 기본값은 유지된다.
- Dashboard에 order enable / kill switch reset / direct send_order UI를 추가하지 않았다.
- EntryEngine, DirtyEvaluator, Candidate FSM 판단 의미는 변경하지 않았다.
- 실제 Gateway `send_order` command 생성 경로는 변경하지 않았다.

검증 명령:

```powershell
python -m pytest tests\test_dashboard_read_model_repository.py tests\test_dashboard_read_model_writer.py tests\test_dashboard_read_model_api.py tests\test_dashboard_v2_snapshot.py tests\test_runtime_supervisor.py tests\test_core_runtime_api.py
python -m pytest
```

다음 PR은 read model shadow comparison을 실제 background metric으로 확대하거나, Gateway order ack/fill/reconcile event consumer를 read model과 OrderManager lifecycle에 연결하는 작업을 권장한다.

## PR 8 구현 상태: Gateway Order Lifecycle Event Consumer & Replay

PR 8은 Event Log 위에 broker 주문 생명주기 consumer를 추가한다. 이 PR은 주문 생성, LIVE_SIM 주문 활성화, 실제 Kiwoom `send_order` 활성화 PR이 아니다. 이미 Gateway에서 도착한 주문 승인, 거절, 체결, 잔고/포지션 snapshot 이벤트를 보존한 뒤 idempotent하게 관측하고 복구하는 기반이다.

안전 기본값은 유지한다.

| setting | default |
|---|---:|
| `TRADING_SEND_ORDER_ALLOWED` | `false` |
| `TRADING_ORDER_MANAGER_ENABLED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| `TRADING_EVENT_REPLAY_PRICE_TICK_ENABLED` | `false` |
| `TRADING_EVENT_REPLAY_HEARTBEAT_ENABLED` | `false` |

### Canonical Event Mapping

| raw GatewayEvent | canonical event | note |
|---|---|---|
| `command_ack` + `command_type=send_order` + `order_no` | `ORDER_ACCEPTED` | broker order number required |
| `command_ack` + `command_type=cancel_order` + `order_no` | `ORDER_CANCEL_ACCEPTED` | cancel command accepted |
| `command_ack` without `order_no` | `COMMAND_ACK` | order command이면 broker accepted로 보지 않고 reconcile required |
| rejected/failed `command_ack` | `ORDER_REJECTED` or `COMMAND_FAILED` | order command이면 order rejected |
| `command_failed`, `command_timeout`, `command_expired` | `COMMAND_FAILED` | send_order 자동 재전송 금지 |
| `execution_event`, `execution`, `fill`, `order_fill` | `ORDER_PARTIALLY_FILLED` or `ORDER_FILLED` | `remaining_quantity` 기준 |
| `order_ack` | `ORDER_ACCEPTED` | future parser output |
| `order_reject` | `ORDER_REJECTED` | future parser output |
| `cancel_ack` | `ORDER_CANCEL_ACCEPTED` | future parser output |
| `order_cancel`, `order_cancelled` | `ORDER_CANCELLED` | future parser output |
| `order_status_snapshot` | `ORDER_STATUS_SNAPSHOT` | latest projection |
| `balance_snapshot` | `BALANCE_SNAPSHOT` | reconcile/projection input |
| `position_snapshot` | `POSITION_SNAPSHOT` | reconcile/projection input |
| unsupported non-order event | `IGNORED` | reason stored |
| `price_tick`, `heartbeat` | excluded | logging/replay disabled by default |

`command_ack`는 Gateway command 실행 결과다. `order_no`가 없으면 증권사 주문 접수로 해석하지 않는다. ack timeout 또는 missing `order_no` 때문에 동일 BUY command를 자동 재전송하지 않는다.

### Live 처리 순서

1. Gateway가 `GatewayEvent`를 보낸다.
2. `GatewayStateStore.record_event()`가 상태를 갱신하고 `gateway_event_log`에 append한다.
3. `RuntimeSupervisor.handle_gateway_event()`가 주문 lifecycle event를 전용 `order-event-consumer` executor로 전달한다.
4. `GatewayEventDispatcher.consume_live_event()`가 Event Log row를 찾아 claim한다.
5. `GatewayEventCodec`이 raw type을 canonical type으로 정규화한다.
6. `OrderLifecycleEventConsumer`가 `order_gateway_event_receipts`를 확인한다.
7. OrderManager generic API가 lifecycle update를 적용한다.
8. `broker_order_state` 또는 `broker_position_state` projection을 갱신한다.
9. receipt를 저장한다.
10. Event Log row를 `PROCESSED`, `IGNORED`, `RETRY_WAIT`, `DEAD_LETTER` 중 하나로 닫는다.
11. Dashboard read model에는 dirty mark만 전달한다. snapshot DB write는 기존 1초 coalescing writer가 담당한다.

Critical order event는 Event Log append 전에 OrderManager로 전달하지 않는다. Event Log append가 불가능하면 `EVENT_LOG_UNAVAILABLE`로 fail-closed 처리하고 신규 매수는 `STOP_NEW_BUY` 상태로 막아야 한다.

### Event Log Claim/Retry/Dead Letter

`gateway_event_log`에는 다음 processing column이 추가됐다.

| column | purpose |
|---|---|
| `processing_attempts` | claim attempt count |
| `claimed_at` | lease start |
| `claimed_by` | worker id |
| `next_retry_at` | lease expiry or retry eligibility |
| `handler_name` | consumer name |
| `handler_version` | consumer contract version |
| `last_attempt_at` | last processing attempt |
| `dead_lettered_at` | terminal failure time |
| `processing_result_json` | canonical result detail |

지원 상태:

- `PENDING`
- `PROCESSING`
- `PROCESSED`
- `RETRY_WAIT`
- `FAILED`
- `DEAD_LETTER`
- `IGNORED`

Claim 정렬은 `received_at ASC, id ASC`이며 batch size는 `TRADING_EVENT_REPLAY_BATCH_SIZE`로 제한한다. stale `PROCESSING` lease는 startup/periodic replay worker가 복구한다.

### Receipt와 Idempotency

`order_gateway_event_receipts`는 이미 적용된 broker event를 기록한다.

- `UNIQUE(source_event_id)`
- `UNIQUE(dedupe_key)`

체결 dedupe는 `account + order_no + execution_id`를 우선한다. 같은 체결이 다른 transport event_id로 다시 들어와도 filled quantity를 두 번 증가시키지 않는다. Order DB update와 receipt 저장 후 Core가 `mark_processed` 전에 죽으면, 재기동 replay는 receipt를 보고 `DUPLICATE_ALREADY_APPLIED`로 Event Log만 닫는다.

### Broker Projection

추가 projection:

- `broker_order_state`
- `broker_position_state`

projection은 latest state다. append-only 원장은 `gateway_event_log`이며, projection은 dashboard와 reconcile을 위한 broker truth cache다.

### Out-of-order 정책

- fill-before-ack: matching 가능하면 즉시 partial/full fill로 승격한다.
- late ack: FILLED/PARTIALLY_FILLED를 ACKED로 되돌리지 않는다.
- duplicate fill: receipt dedupe로 no-op 처리한다.
- cancel ack 후 late execution: 안전하게 matching되지 않으면 reconcile required.
- balance-before-fill: projection은 보존하고 mismatch는 fail-closed 대상이다.
- unmatched fill: raw event/receipt/result를 보존하고 `RECONCILE_REQUIRED + STOP_NEW_BUY`로 둔다.

### Matching 우선순위

1. `managed_order_id`
2. `command_id`
3. `idempotency_key`
4. `account + order_no`
5. `account + original_order_no`
6. `account + code + side + nearby sent_at`
7. 정확히 하나의 pending local order

현재 구현은 기존 `ManagedOrderReconciler`의 `order_no`, `command_id`, idempotency/code/side fallback을 사용한다. ambiguous heuristic은 자동 matching하지 않고 `RECONCILE_REQUIRED`를 우선한다.

### RuntimeSupervisor와 Dashboard

RuntimeSupervisor는 optional `order_event_consumer` hook을 갖는다. 주문 lifecycle event는 runtime cycle executor가 아니라 전용 `order-event-consumer` executor에서 처리한다. `command_ack`와 `command_failed`는 lifecycle 관측 후 기존 candidate hydration/command handler 경로도 계속 탄다.

Startup 순서:

1. Gateway/condition/core event worker 시작
2. stale Event Log claim 복구
3. critical pending order event replay
4. RuntimeSupervisor startup
5. periodic Event Log replay worker 시작
6. Dashboard read-model writer 시작

Dashboard read model은 `order_lifecycle` 섹션을 노출한다.

- `status`
- `consumer_enabled`
- `consumer_running`
- `order_lifecycle_ready`
- `pending_event_count`
- `retry_wait_count`
- `failed_count`
- `dead_letter_count`
- `oldest_pending_age_sec`
- `processed_count`
- `duplicate_applied_count`
- `unmatched_event_count`
- `reconcile_required_count`
- `last_event_type`
- `last_event_at`
- `last_processed_at`
- `last_error`
- `replay_status`
- `replay_duration_ms`

Safety banner:

- order lifecycle not ready
- order event dead letter
- unmatched order event

### 운영 Runbook

Backlog 확인:

1. Dashboard `order_lifecycle.pending_event_count`, `retry_wait_count`, `oldest_pending_age_sec`를 확인한다.
2. `gateway_event_log`에서 `PENDING`, `PROCESSING`, `RETRY_WAIT` row를 `received_at, id` 순으로 본다.
3. row를 삭제하지 않는다. replay와 idempotency 증거가 사라진다.

Dead Letter:

1. `payload_json`과 `processing_result_json`을 확인한다.
2. malformed payload, wrong account/order match, terminal state regression 중 무엇인지 분류한다.
3. broker order/position과 local state가 맞을 때까지 `STOP_NEW_BUY`를 유지한다.
4. 자동 삭제와 자동 send_order retry는 금지한다.

Unmatched fill:

1. account, code, side, order_no, command_id, idempotency_key, execution_id를 확인한다.
2. `broker_order_state`, `broker_position_state`, managed orders, Kiwoom 계좌 화면을 비교한다.
3. local order 누락 또는 ambiguous matching이면 수동 reconcile 전까지 신규 매수를 열지 않는다.

Balance mismatch:

1. `broker_position_state`와 local managed/live-sim position projection을 비교한다.
2. broker에 local이 모르는 실제 포지션이 있으면 `REDUCE_ONLY`를 검토한다.
3. 이 PR은 자동 매도 command를 생성하지 않는다.

Replay:

1. startup replay와 periodic replay가 기본 활성화되어 있다.
2. manual replay는 replay worker/repository API를 사용한다.
3. row를 수동 삭제하지 않는다. lost fill을 숨기고 receipt 기반 복구를 깨뜨릴 수 있다.

### 남은 작업

- 실제 Kiwoom Chejan 주문/체결/잔고 parser의 canonical field mapping 강화
- 다계좌 partition ordering
- reconcile용 TR request orchestration
- Candidate FSM lifecycle transition hook 확장
- REDUCE_ONLY escalation/operator workflow
- Event Log/receipt drilldown용 dashboard developer detail

## PR 9 구현 상태: OBSERVE Reliability Qualification Gate

PR 9는 Realtime Architecture V2를 LIVE 주문 활성화 전에 검증하기 위한 qualification framework다. 이 PR은 LIVE_SIM 주문 활성화, LIVE_REAL 주문 활성화, 전략 수익성 검증, 주문 command 생성 PR이 아니다.

안전 기본값은 유지한다.

| setting | required value |
|---|---|
| `TRADING_SEND_ORDER_ALLOWED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| `TRADING_RELIABILITY_TEST_MODE` | `true` for execution |
| broker/account mode | not `REAL` |

### Components

- `trading.reliability.models`: profile, scenario, report, SLO model.
- `trading.reliability.metrics`: bounded metric series, percentile summary, process/DB sampling.
- `trading.reliability.slo`: hard safety gate와 operational SLO 판정.
- `trading.reliability.guards`: REAL broker, production DB, order-enabled flag 실행 차단.
- `trading.reliability.workload`: deterministic synthetic GatewayEvent generator.
- `trading.reliability.replay`: isolated DB deterministic replay digest verifier.
- `trading.reliability.faults`: F01-F18 deterministic fault scenario controller.
- `trading.reliability.soak`: observe soak metric sampler.
- `trading.reliability.qualification`: profile runner와 PASS/HOLD/FAIL 판정.
- `tools/runtime_reliability_qualification.py`: local CLI entrypoint.

기존 `tools/websocket_real_pilot_soak.py`는 삭제하지 않는다. Qualification report의 transport subsection에서 참조하며, transport PASS가 전체 runtime PASS를 의미하지 않는다고 기록한다.

### Profiles

| profile | purpose | expected CI status |
|---|---|---|
| `quick-ci` | synthetic replay + mandatory fault subset | `HOLD` if long soak not run |
| `replay` | bundle or synthetic deterministic replay | `HOLD` until enough market sessions |
| `fault-suite` | F01-F18 deterministic faults | `HOLD` without long soak |
| `observe-soak` | runtime/core-url long observation | `PASS` only with enough duration and SLO pass |
| `full` | replay + fault suite + observe soak | `PASS` only after all mandatory evidence |

실행하지 않은 1시간/4시간 soak는 `NOT_RUN` 또는 `SAMPLE_INSUFFICIENT`로 남기며 PASS로 기록하지 않는다.

### Deterministic Replay

동일 입력을 격리된 두 SQLite DB에서 실행한 뒤 digest를 비교한다.

비교 대상:

- `gateway_event_log`
- `order_gateway_event_receipts`
- `managed_orders`
- `managed_order_intents`
- `broker_order_state`
- `broker_position_state`
- `order_kill_switch_state`
- `dashboard_read_models`

제외 대상:

- local auto id
- generated/created/updated/processed timestamps
- processing duration
- worker id
- runtime-specific transient fields

`--bundle`이 지정되면 `gateway_events.jsonl`, `gateway_events.json`, 기존 strategy replay `ticks.csv` 순서로 입력을 재사용한다. bundle이 없으면 deterministic synthetic GatewayEvent를 만든다.

### Fault Scenarios

F01-F18은 named deterministic scenario로 등록되어 있다.

- duplicate price tick은 latest snapshot regression과 full scan amplification을 확인한다.
- duplicate execution은 receipt dedupe와 filled quantity single-apply를 확인한다.
- fill-before-ack은 late ack가 FILLED/PARTIALLY_FILLED를 ACKED로 되돌리지 않는지 확인한다.
- crash-after-receipt는 Event Log `PROCESSED` 전 crash replay에서 중복 반영이 없는지 확인한다.
- stale claim은 lease 만료 후 다른 worker가 회수할 수 있는지 확인한다.
- append failure는 `STOP_NEW_BUY`와 lifecycle not ready를 확인한다.
- malformed event는 `RECONCILE_REQUIRED` 또는 dead-letter 계열 fail-closed를 확인한다.
- dead letter present는 lifecycle ready false를 확인한다.

Fault scenario 내부의 의도된 append failure/dead letter는 scenario 성공 조건이며, top-level hard gate의 예상 밖 운영 위반과 분리해 집계한다.

### Hard Safety Gate

하나라도 위반하면 FAIL이다.

- order command count = 0
- REAL broker access count = 0
- critical event lost count = 0
- duplicate execution applied count = 0
- terminal state regression count = 0
- negative remaining quantity count = 0
- overfill count = 0
- silent unmatched fill count = 0
- unresolved event consumer crash count = 0
- event log append failure인데 lifecycle ready true인 경우 = 0
- dead letter가 있는데 lifecycle ready true인 경우 = 0
- reconcile required인데 신규 매수 가능 상태인 경우 = 0
- runtime DB corruption count = 0

### Operational SLO

기본 threshold는 env로 조정 가능하다.

- `TRADING_RELIABILITY_RUNTIME_CYCLE_P95_MS=1500`
- `TRADING_RELIABILITY_DIRTY_EVALUATOR_P95_MS=500`
- `TRADING_RELIABILITY_ORDER_EVENT_P95_MS=250`
- `TRADING_RELIABILITY_DASHBOARD_READ_P95_MS=100`
- `TRADING_RELIABILITY_READ_MODEL_MAX_AGE_SEC=5`
- `TRADING_RELIABILITY_BACKLOG_MAX_AGE_SEC=5`
- `TRADING_RELIABILITY_REPLAY_DRAIN_MAX_SEC=10`
- `TRADING_RELIABILITY_MAX_CAPACITY_DROPS=0`
- `TRADING_RELIABILITY_MAX_RSS_GROWTH_MB=200`

CI unit test에서는 짧은 wall-clock threshold를 hard assertion으로 쓰지 않는다. 장시간 성능 SLO는 `observe-soak` 또는 `full` profile에서 판정한다.

### Report Artifacts

산출물:

- `reports/reliability/{run_id}/qualification.json`
- `reports/reliability/{run_id}/summary.md`
- `reports/reliability/{run_id}/metrics.json`
- `reports/reliability/{run_id}/scenario_results.json`
- `reports/reliability/{run_id}/failures.json`

Read-only API:

- `GET /api/runtime/reliability/latest`
- `GET /api/runtime/reliability/runs`
- `GET /api/runtime/reliability/runs/{run_id}`

Dashboard developer/system detail은 이 report를 읽어 latest status, recommendation, hard gate failure count, SLO count, event loss, duplicate apply, dead letter, peak backlog, runtime p95, event consumer p95, memory growth, report link를 표시할 수 있다. Main 운영 화면에는 qualification 실행 버튼이나 fault injection 버튼을 추가하지 않는다.

### CLI

```powershell
python tools\runtime_reliability_qualification.py --profile quick-ci --output-dir reports\reliability
python tools\runtime_reliability_qualification.py --profile replay --bundle reports\strategy_replay\bundles\bundle-2026-06-18 --repeat 2 --output-dir reports\reliability
python tools\runtime_reliability_qualification.py --profile observe-soak --duration-sec 3600 --core-url http://127.0.0.1:8000 --output-dir reports\reliability
python tools\runtime_reliability_qualification.py --profile full --duration-sec 14400 --seed 20260618 --output-dir reports\reliability
```

Exit code:

- `0`: PASS
- `2`: HOLD
- `1`: FAIL
- `3`: execution/configuration error

### Runbook

운영 절차는 `docs/runtime_reliability_qualification_runbook.md`에 둔다.

핵심 원칙:

- 운영 DB row를 수동 삭제하지 않는다.
- dead letter를 자동 삭제하지 않는다.
- STOP_NEW_BUY를 자동 reset하지 않는다.
- unmatched fill 상태에서 신규 매수를 허용하지 않는다.
- qualification recommendation은 설정을 자동 변경하지 않는다.
- LIVE_SIM flag는 수동 review 전까지 활성화하지 않는다.

## PR 10 구현 상태: Kiwoom Chejan Canonical Parser & Real Payload Validation

PR 10은 Kiwoom OpenAPI+ `OnReceiveChejanData`의 `gubun`과 FID payload를 broker-specific raw event로 보존하고, Core `GatewayEventCodec`이 PR8 order lifecycle canonical event로 안전하게 변환하도록 만드는 단계다. 이 PR도 LIVE_SIM/LIVE_REAL 주문 활성화 PR이 아니다.

### gubun Routing

| gubun | GatewayEvent type | handling |
|---|---|---|
| `0` | `kiwoom_order_chejan` | 주문접수, 주문거절, 부분체결, 완전체결, 취소 |
| `1` | `kiwoom_balance_chejan` | 국내주식 단일 종목 balance/position delta |
| `3` | `kiwoom_special_chejan` | diagnostic only, order lifecycle ignored |
| unknown | `kiwoom_special_chejan` | `UNSUPPORTED`, raw preserved, lifecycle ignored |

`gubun=1`은 계좌 전체 snapshot이 아니라 단일 종목 delta일 수 있으므로 `snapshot_scope=SINGLE_CODE_DELTA`, `full_account_snapshot=false`를 명시한다.

### FID Registry

현재 registry는 PR10 fixture와 Kiwoom Chejan contract validation을 위해 다음 FID를 명명한다.

- 주문/체결: `9201`, `9203`, `9001`, `912`, `913`, `302`, `900`, `901`, `902`, `903`, `904`, `905`, `906`, `907`, `908`, `909`, `910`, `911`, `914`, `915`, `919`, `920`
- 잔고: `9201`, `9001`, `302`, `10`, `930`, `931`, `932`, `933`, `945`, `946`, `951`, `27`, `28`, `307`, `8019`

실제 모의투자 fixture에서 값과 의미가 불일치하면 fixture와 공식 계약을 기준으로 registry를 수정한다.

### FID 920

`FID 920`은 `screen_no`로만 취급한다. strategy tag, candidate id, managed order id, command id, idempotency key를 Chejan FID에서 임의 복원하지 않는다.

호환 필드:

- `legacy_tag=""`
- `tag_source="UNAVAILABLE_FROM_CHEJAN"`

### Canonical Payload

주문 payload는 account, code, order_no, original_order_no, order_status, order_gubun, side, order_quantity, order_price, unfilled_quantity, cumulative_filled_quantity, execution_id, execution_price, execution_quantity, unit_execution_quantity, incremental_execution_quantity, reject_reason, screen_no, broker_event_key, parser_status를 포함한다.

잔고 payload는 account, code, position_quantity, orderable_quantity, average_buy_price, total_buy_amount, current_price, intraday_net_buy_quantity, best ask/bid, profit_rate, `snapshot_scope`, `full_account_snapshot`, broker_event_key, parser_status를 포함한다. 보유수량 0 이벤트도 position close delta로 보존한다.

### Event Classification

Core codec은 다음을 함께 보고 canonical type을 결정한다.

- `event_kind`
- `order_status`
- `reject_reason`
- `execution_id`
- `incremental_execution_quantity`
- `cumulative_filled_quantity`
- `unfilled_quantity`
- `original_order_no`

금지 규칙:

- `remaining_quantity=0`만으로 FILLED 분류 금지
- `execution_id` 또는 체결량 없는 이벤트를 fill로 분류 금지
- `command_ack`를 broker order accepted로 간주 금지
- reject reason이 있는데 accepted 분류 금지
- late ack로 FILLED/PARTIALLY_FILLED 상태 역행 금지

### broker_event_key / Dedupe

- 체결 우선 키: `account + order_no + execution_id`
- execution id 누락 fallback: `account + order_no + event_time + incremental_execution_quantity + execution_price + cumulative_filled_quantity`
- fallback 사용 시 `dedupe_confidence=LOW`, `EXECUTION_ID_MISSING`, `DEGRADED`
- 잔고 delta 키: `account + code + position_quantity + orderable_quantity + average_buy_price + raw checksum`

Event Log dedupe와 receipt idempotency는 `broker_event_key`를 우선 사용한다.

### Capture / Redaction

`trading.broker.chejan_capture`는 simulation-only raw capture writer를 제공한다.

기본 flag:

- `TRADING_KIWOOM_CHEJAN_RAW_CAPTURE_ENABLED=false`
- `TRADING_KIWOOM_CHEJAN_CAPTURE_SIMULATION_ONLY=true`
- `TRADING_KIWOOM_CHEJAN_CAPTURE_DIR=reports/kiwoom_chejan`
- `TRADING_KIWOOM_CHEJAN_CAPTURE_MAX_ROWS=10000`

계좌번호는 deterministic token으로 치환하고 password/user id/token 계열 key는 저장하지 않는다. REAL broker capture는 거부한다.

### Parser Validation

CLI:

```powershell
python tools\kiwoom_chejan_parser_validation.py --fixture-dir tests\fixtures\kiwoom_chejan --output-dir reports\kiwoom_chejan_validation
```

산출물:

- `validation.json`
- `summary.md`
- `field_coverage.json`
- `unknown_fids.json`
- `classification_matrix.json`
- `failures.json`

현재 repo fixture는 `source=SYNTHETIC`이므로 validation은 `HOLD`가 정상이다. 실제 모의서버 sanitized fixture가 `source=KIWOOM_SIMULATION`이고 필수 case coverage를 만족해야 `PASS`가 가능하다.

### Qualification 연결

PR9 qualification report에는 parser validation subsection이 추가된다.

recommendation 흐름:

- qualification 안전성 FAIL: `NOT_READY`
- synthetic parser fixture만 통과: `READY_FOR_KIWOOM_PARSER_VALIDATION`
- actual simulation fixture 통과: `READY_FOR_RECONCILE_TR_PILOT`
- reconcile TR pilot까지 통과: `READY_FOR_LIVE_SIM_CANARY_REVIEW`

qualification은 parser flag나 주문 flag를 자동 변경하지 않는다.

### 실제 주문 경로

유지되는 금지 사항:

- `TRADING_SEND_ORDER_ALLOWED=false`
- `TRADING_ORDER_MANAGER_ENABLED=false`
- `TRADING_ORDER_MANAGER_OBSERVE_ONLY=true`
- `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND=false`
- `TRADING_ORDER_INTENT_ENABLED=false`
- `TRADING_KIWOOM_CHEJAN_EMIT_LEGACY_EXECUTION_EVENT=false`
- 실제 `send_order`, `cancel_order`, `modify_order` command 생성 없음
