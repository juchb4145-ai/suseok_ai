# Strategy Reboot V2 Migration Plan

## 원칙

Strategy Reboot V2 마이그레이션은 기존 자동매매 기능을 즉시 갈아엎는 작업이 아니다. 1단계는 설계 고정과 안전한 병렬 경로 확보가 목적이다.

원칙:

- LIVE 주문은 계속 비활성화한다.
- 기존 hybrid/final grade/promotion 로직은 삭제하지 않고 deprecated 경로로 남긴다.
- 새 v2 판단부는 기존 Gateway, broker model, command queue, TR runner, market data store를 재사용한다.
- v2 경로에서 조건검색 이벤트는 주문 intent를 만들 수 없다.
- v2 경로의 주문 관련 산출물은 OBSERVE/DRY_RUN intent까지만 허용한다.

## Feature Flags

권장 기본값:

```text
STRATEGY_REBOOT_V2_ENABLED=0
STRATEGY_REBOOT_V2_OBSERVE=1
STRATEGY_REBOOT_V2_DRY_RUN=0
STRATEGY_REBOOT_V2_LIVE_DISABLED=1
STRATEGY_REBOOT_V2_USE_LEGACY_HYBRID=0
STRATEGY_REBOOT_V2_DASHBOARD=0
```

의미:

- `STRATEGY_REBOOT_V2_ENABLED`: v2 runtime/router 활성화 여부
- `STRATEGY_REBOOT_V2_OBSERVE`: 후보 FSM과 board 계산만 수행
- `STRATEGY_REBOOT_V2_DRY_RUN`: OrderManager가 DRY_RUN intent를 기록할 수 있음
- `STRATEGY_REBOOT_V2_LIVE_DISABLED`: LIVE Gateway order command 생성 금지
- `STRATEGY_REBOOT_V2_USE_LEGACY_HYBRID`: v2에서 legacy hybrid 결과를 참조하는 예외 flag. 기본 금지
- `STRATEGY_REBOOT_V2_DASHBOARD`: dashboard v2 summary API/UI 활성화

`STRATEGY_REBOOT_V2_LIVE_DISABLED=1`은 1단계와 후속 observe/dry-run 단계에서 불변값으로 유지한다.

## Phase 1: 설계 고정

산출물:

- `docs/strategy_reboot_v2.md`
- `docs/strategy_reboot_v2_migration_plan.md`
- 필요한 경우 최소 enum/model skeleton

작업:

- 목표 아키텍처와 데이터 흐름 정의
- Candidate FSM 상태와 대기/차단 reason 정의
- `ConditionHit` 모델 정의
- TR hydration priority와 idempotency key 정의
- ThemeBoard/MarketRegime/EntryEngine/ExitEngine/RiskManager/OrderManager 책임 경계 정의
- 기존 hybrid/final grade/promotion 계층의 v2 비사용 정책 정의
- Dashboard v2 요약 방향 정의

검증:

- 문서에 condition include -> order intent 금지 경로가 명시되어야 한다.
- 문서에 condition include -> `DETECTED`/`HYDRATING` 경로가 명시되어야 한다.
- 문서에 TR idempotency key가 명시되어야 한다.
- 문서에 `WAIT_DATA`와 `HARD_BLOCK` 구분이 명시되어야 한다.
- 문서에 `RISK_OFF` 신규 매수 금지/미체결 취소/포지션 축소가 명시되어야 한다.

## Phase 2: Model Skeleton

목표:

- 실행 로직을 바꾸지 않고 v2 모델을 코드상에서 참조 가능하게 만든다.

권장 추가 위치:

- `trading/strategy/reboot_v2.py`

포함 항목:

- `CandidateV2State`
- `CandidateWaitReason`
- `ConditionLevel`
- `ConditionHit`
- `ThemeStatus`
- `MarketRegime`
- `EntryStep`
- `ExitTrigger`
- `HydrationPriority`

주의:

- 기존 `CandidateState`를 즉시 변경하지 않는다.
- 기존 DB schema migration은 이 단계에서 하지 않는다.
- 기존 runtime import 경로에 v2 skeleton을 강제로 연결하지 않는다.

검증:

- import smoke test가 통과한다.
- 기존 테스트가 v2 skeleton 추가로 깨지지 않는다.

## Phase 3: ConditionSensor 병렬 연결

목표:

- 기존 조건검색 이벤트를 v2 ConditionSensor에도 병렬 전달한다.
- 기존 CandidateCollector 동작은 유지한다.

작업:

- `BrokerConditionEvent` -> `ConditionHit` 변환기 추가
- condition level mapping 추가
- hit count와 first/last seen 갱신 저장소 추가
- 신규 후보를 `DETECTED`로만 생성하는 v2 Candidate FSM stub 추가

금지:

- condition include에서 `SETUP_READY`, `TIMING_READY`, order intent 생성
- legacy hybrid gate 호출
- Gateway order command 생성

검증:

- include 이벤트 후 order intent count 변화 없음
- include 이벤트 후 v2 candidate가 `DETECTED`
- P1 hydration 필요 후보는 `HYDRATING` 요청 후보로 표시

## Phase 4: CandidateHydrator/TR Queue

목표:

- P0/P1/P2/P3 hydration command를 command queue 정책에 맞춰 생성한다.

작업:

- hydration request planner 추가
- idempotency key builder 추가
- P0 계좌/잔고/미체결 freshness policy 추가
- P1 신규 후보 basic info policy 추가
- P2 테마 구성 종목 보강 policy 추가
- P3 장전/장후 보강 batch policy 추가

검증:

- 같은 후보/같은 TR 목적의 command 중복 생성 없음
- active dedupe 상태의 command가 있으면 새 command가 reject 또는 skip
- TR 지연/부족은 `WAIT_TR` 또는 `WAIT_DATA`
- TR 실패가 확정 차단 근거가 아니면 `HARD_BLOCK` 금지

## Phase 5: RealtimeStore/CandleStore 연결

목표:

- Entry/Exit 판단이 TR price가 아니라 실시간 tick/candle에서만 timing을 읽도록 한다.

작업:

- `BrokerPriceTick` -> RealtimeStore update 경로 확인
- 1m/3m/5m candle aggregation 경로 정리
- VWAP/체결강도/거래대금/스프레드 feature snapshot 정의
- TR backfill metadata와 realtime timing source 구분

검증:

- TR backfill price만 있는 후보는 `WAIT_DATA`
- tick freshness 미충족 후보는 `WAIT_DATA`
- candle 부족 후보는 `WAIT_DATA`
- realtime feature가 충분해야 `SETUP_READY` 검토 가능

## Phase 6: ThemeBoard와 MarketRegime

목표:

- 테마와 시장국면을 EntryEngine 외부에서 계산한다.

작업:

- ThemeBoard status 계산
- leader/follower role 계산
- MarketRegime status 계산
- `RISK_OFF` event와 policy hook 추가

검증:

- `WEAK_THEME` 신규 매수 금지
- `LEADER_ONLY_THEME` follower 신규 매수 금지
- `RISK_OFF` 신규 매수 금지
- `RISK_OFF` buy-side 미체결 취소 intent 생성
- `RISK_OFF` 보유 포지션 exit/risk reduction 검토 기록

## Phase 7: EntryEngine OBSERVE

목표:

- 주문 없이 5단계 판단 결과만 기록한다.

작업:

- Data Ready
- Theme Ready
- Market Allowed
- Stock Role Allowed
- Price Timing Ready
- 단계별 wait reason 기록

상태 전이:

- Data/Theme/Market/Role 통과: `SETUP_READY`
- Price Timing 통과: `TIMING_READY`
- 데이터 부족: `WAIT` + `WAIT_DATA`
- 시장/테마/타이밍 대기: `WAIT` + 해당 reason

검증:

- condition include만으로 `SETUP_READY` 불가
- TR hydration만으로 `TIMING_READY` 불가
- realtime timing source 없으면 `WAIT_DATA`
- legacy final grade를 읽지 않아도 판단 가능

## Phase 8: ExitEngine OBSERVE

목표:

- 보유/DRY_RUN 포지션에 대해 exit trigger를 기록한다.

작업:

- `TAKE_PROFIT`
- `SUPPORT_LOSS`
- `TIME_EXIT`
- `TRAILING_STOP`
- `THEME_WEAK_EXIT`
- `LEADER_COLLAPSE_EXIT`
- `INDEX_WEAK_EXIT`
- `MARKET_RISK_OFF_EXIT`
- `BREADTH_COLLAPSE_EXIT`

검증:

- condition remove만으로 exit trigger 생성 금지
- ThemeBoard/leader/market 입력으로 exit 판단
- `RISK_OFF`에서 exit/risk reduction 우선

## Phase 9: RiskManager와 OrderManager DRY_RUN

목표:

- v2 `TIMING_READY` 후보에 대해 DRY_RUN intent만 생성한다.

작업:

- P0 freshness check
- 포지션 슬롯/테마 노출/당일 손실 한도 check
- Gateway health/command ack delay check
- DRY_RUN buy/sell/cancel intent 기록
- LIVE command 생성 방지 guard 추가

검증:

- `STRATEGY_REBOOT_V2_DRY_RUN=0`이면 intent 미생성
- `STRATEGY_REBOOT_V2_LIVE_DISABLED=1`이면 Gateway `send_order` 생성 불가
- `RISK_OFF` 신규 buy intent 미생성
- 미체결 취소는 DRY_RUN cancel intent로만 기록

## Phase 10: Dashboard V2

목표:

- 운영 화면을 새 구조에 맞게 단순화한다.

표시 항목:

- 시장국면
- 주도테마 TOP5
- READY 후보
- 보유 리스크
- 차단/대기 사유 TOP

Legacy 이동:

- hybrid score 상세
- final grade 상세
- threshold A/B 상세
- shadow/promotion 상세
- raw condition hit table

검증:

- 첫 화면에서 운영자가 신규 매수 가능/금지 상태를 즉시 볼 수 있어야 한다.
- `RISK_OFF`일 때 신규 매수 금지와 risk reduction 상태가 명확해야 한다.
- READY 후보는 `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING`로 구분되어야 한다.

## Deprecated 처리

문서상 deprecated:

- hybrid gate as final entry approval
- final grade as direct order decision
- shadow promotion as auto-live enablement
- threshold A/B as runtime order threshold switch
- condition include as order signal

코드상 권장 처리:

- legacy module docstring에 v2 비사용 정책 추가
- v2 runtime에서는 legacy import를 하지 않도록 dependency inversion
- legacy dashboard panel은 debug/legacy namespace로 이동
- 기존 테스트는 유지하되 새 v2 테스트와 의미를 분리

## PR 2 구현 체크포인트

Phase 3-4의 첫 구현 단위는 Candidate Ingestion과 CandidateHydrator다. 이 단계는 observe 전용 병렬 경로이며 기존 order path를 변경하지 않는다.

완료된 계약:

- `CandidateSourceEvent`로 조건검색 include/remove, Opening Burst selected, 향후 manual watch, 향후 ThemeBoard 후보를 같은 형태로 표현한다.
- Candidate merge key는 `trade_date + code`이며, 같은 종목의 여러 source는 하나의 active Candidate에 병합한다.
- PR 2에서 사용하는 상태는 `DETECTED`, `HYDRATING`, `WATCHING`, `WAIT_DATA`, `REMOVED`, `EXPIRED`로 제한한다.
- 조건검색 include는 Candidate 생성과 hydration enqueue만 수행한다. `READY`, `EntryPlan`, DRY_RUN buy intent, LIVE order command를 만들지 않는다.
- Opening Burst는 selected 종목만 후보로 유입한다. excluded/observed 종목은 Candidate를 만들지 않는다.
- CandidateHydrator는 `purpose=candidate_hydration`, `response_mode=capture`, `tr_code=opt10001` command를 Gateway queue에 넣는다.
- P1 hydration idempotency key는 `candidate_hydration:{trade_date}:{code}:{tr_code}:{bucket}`다.
- 기본 throttle은 `max_per_cycle=5`, `max_pending=10`, `ttl_sec=90`이며 환경 변수로 조정한다.
- TR ack는 Candidate metadata와 MarketDataStore backfill만 갱신한다. TR-only 가격은 `gate_usable_for_entry=false`로 남긴다.
- 데이터 부족은 `WAIT_DATA`와 `WAIT_DATA_*` reason으로 기록한다. `theme_id` 부재는 `theme_unmapped` reason만 추가하며 단독 hard block이 아니다.
- SQLite에는 `candidate_source_events`, `candidate_hydration_requests`, `candidate_hydration_results`만 추가한다. PostgreSQL 의존성은 만들지 않는다.
- Dashboard snapshot은 `candidate_ingestion` 요약만 추가한다.

검증:

- condition include 후 order intent count가 증가하지 않는다.
- condition include 후 Candidate가 `DETECTED` 또는 `HYDRATING`에 있다.
- 같은 idempotency key의 hydration command는 중복 생성되지 않는다.
- hydration ack 데이터가 부족하면 `WAIT_DATA`가 된다.
- hydration ack TR backfill 가격만으로 entry gate를 통과하지 않는다.

## PR 4 구현 체크포인트

MarketRegimeEngine Runtime Integration은 Phase 6의 시장국면 계산을 observe-only로 먼저 고정하는 PR이다.

범위:

- `trading/strategy/market_regime.py`에 `MarketRegimeConfig`, `MarketSideSnapshot`, `MarketBreadthSnapshot`, `MarketRegimeSnapshot`, `CandidateMarketPolicy`, `MarketRegimeResult`를 추가한다.
- KOSPI/KOSDAQ을 분리 계산하고 global status는 양쪽 side status를 보수적으로 합성한다.
- index tick이 없거나 stale이면 side status는 `DATA_WAIT`이며 예외를 내지 않는다.
- 후보 universe breadth는 diagnostic-only이며 sample 부족 시 `LOW_TRUST_BREADTH`/`DATA_WAIT` reason을 남긴다.
- `RISK_OFF`는 후보 삭제가 아니라 `BLOCK_NEW_ENTRY` 정책 metadata merge로만 표현한다.
- `WEAK`은 `WAIT_MARKET`, `SELECTIVE`는 reduced size, `EXPANSION`은 normal size, `CHOPPY`는 wait 또는 reduced 관찰로 표현한다.
- Runtime cycle은 ThemeBoard 이후 MarketRegime을 실행한다.
- `market_regime_snapshots`, `market_side_snapshots`, `candidate_market_policies` SQLite 테이블을 추가한다.
- Dashboard에는 `market_regime` section과 candidate row의 `market_side`/`market_status`/`market_action` 필드를 추가한다.
- ThemeBoard overlay는 market distribution과 risk flag만 추가하고 ThemeBoard status 자체를 강제로 바꾸지 않는다.

금지:

- `READY`, `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING` 전이
- `EntryPlan`, DRY_RUN buy intent, LIVE order command 생성
- hybrid/final grade, threshold A/B, promotion 자동 변경
- PostgreSQL 의존성 추가
- RISK_OFF 후보 삭제

검증:

- index tick missing -> `DATA_WAIT`
- condition/candidate include -> order intent 미생성
- RISK_OFF -> `BLOCK_NEW_ENTRY`, 후보 유지
- WEAK -> `WAIT_MARKET`, 신규 진입 차단
- DATA_WAIT 후보는 `WAIT_DATA` 상태 유지
- `candidate_market_policies`는 `trade_date + calculated_at + code` 기준 idempotent
- dashboard `market_regime` section과 candidate market fields 노출

## PR 5 구현 체크포인트

EntryEngine Reboot V2 Runtime Integration은 `WATCHING` 후보를 5단계로 평가해 observe-only entry decision을 저장하는 PR이다.

범위:

- `trading/strategy/entry_engine.py`에 EntryEngine 모델과 런타임 파이프라인을 추가한다.
- 판단 단계는 Data Ready, Theme Ready, Market Allowed, Stock Role Allowed, Price Timing Ready로 고정한다.
- 최종 status는 `OBSERVE_READY`, `WAIT`, `HARD_BLOCK`, `DATA_WAIT`, `MARKET_WAIT`, `THEME_WAIT`, `PRICE_WAIT`로 제한한다.
- `entry_decisions`, `entry_decision_checks` SQLite 테이블을 추가한다.
- Runtime cycle은 MarketRegime 이후 EntryEngine을 실행한다.
- Dashboard에는 `entry_engine` section과 candidate row의 `entry_status`, `price_location`, `entry_reason_codes`, `entry_operator_message_ko`를 추가한다.
- Candidate state는 기본적으로 `WATCHING`을 유지하고 entry 결과는 metadata와 decision table에만 기록한다.

금지:

- LIVE order command, Gateway `send_order` command 생성
- 기존 hybrid gate/final grade/EntryPlanBuilder 직접 연결
- promotion/threshold 자동 변경
- RISK_OFF 후보 삭제
- condition include 또는 Opening Burst selected만으로 `OBSERVE_READY` 생성
- TR backfill 가격만으로 price timing ready 생성

검증:

- realtime tick 누락 또는 TR-only 가격은 `DATA_WAIT`
- `LEADING_THEME + LEADER + ALLOW_NORMAL + VWAP_RECLAIM`은 `OBSERVE_READY`
- `LEADER_ONLY_THEME + FOLLOWER`는 ready가 아님
- `RISK_OFF`는 `HARD_BLOCK` 또는 시장 대기
- `SELECTIVE + FOLLOWER`는 통과하지 않음
- `EXPANSION + FOLLOWER`는 조건부 observe-ready 가능
- `CHASE_HIGH`, `VWAP_OVEREXTENDED`, VI active, `OVERHEATED`는 ready가 아님
- `TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS=false`에서 dry-run intent가 생성되지 않음
- LIVE order command가 생성되지 않음

## PR 6 구현 체크포인트

ExitEngine + PositionRiskManager Reboot V2는 open position을 관찰 snapshot으로 정규화하고, observe-only exit decision과 포지션/포트폴리오 risk snapshot을 저장하는 PR이다.

범위:

- `trading/strategy/exit_engine_reboot.py`에 ExitEngine 모델, 판단 우선순위, DRY_RUN sell intent 산출, 런타임 파이프라인을 추가한다.
- `trading/strategy/position_risk.py`에 PositionRuntimeService, PositionRiskManager, portfolio risk 요약, 런타임 파이프라인을 추가한다.
- `position_runtime_snapshots`, `exit_decisions_reboot`, `dry_run_sell_intents`, `position_risk_snapshots`, `portfolio_risk_snapshots` SQLite 테이블을 추가한다.
- Runtime cycle은 EntryEngine 이후 ExitEngine, PositionRiskManager 순서로 실행한다.
- Dashboard에는 `exit_engine`, `position_risk` section을 추가한다.
- Candidate metadata에는 exit/risk 표시 필드만 병합하고 매수 lifecycle state는 변경하지 않는다.

금지:

- LIVE order command, Gateway `send_order`, `cancel_order`, `modify_order` command 생성
- 기존 hybrid gate/final grade/promotion/threshold 로직 연결
- stale/invalid price 기반 sell intent 생성
- open position 없는 sell intent 생성
- DRY_RUN sell intent를 실제 주문 queue로 전달
- PostgreSQL 연동

검증:

- virtual filled buy/open virtual position에서 runtime open position snapshot이 생성된다.
- 동일 candidate/code 중복 open position snapshot은 하나로 정규화된다.
- stop loss, fast stop loss, take profit, trailing stop, time exit 판단이 priority에 맞게 생성된다.
- theme weak, leader collapse, market RISK_OFF/WEAK 판단이 생성된다.
- stale/invalid data는 `DATA_WAIT` 또는 `STALE_DATA_RISK`가 되고 sell intent를 만들지 않는다.
- `TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS=false`에서는 sell intent가 생성되지 않는다.
- flag true에서는 idempotency key 기준으로 DRY_RUN sell intent가 중복 생성되지 않는다.
- position risk manager가 portfolio risk level, stop_new_entry recommendation, kill_switch recommendation을 계산한다.
- dashboard snapshot에 `exit_engine`, `position_risk` section이 포함된다.
- LIVE/Gateway order command는 생성되지 않는다.

## Rollback

v2는 feature flag로 분리되므로 rollback은 다음과 같다.

```text
STRATEGY_REBOOT_V2_ENABLED=0
STRATEGY_REBOOT_V2_DRY_RUN=0
STRATEGY_REBOOT_V2_DASHBOARD=0
```

rollback 후에도 기존 Gateway, command queue, runtime, dashboard는 기존 경로로 동작해야 한다.

## 1단계 완료 조건

- 설계 문서 2개가 추가되어 있다.
- v2 상태/모델/정책이 기존 hybrid 문서와 분리되어 있다.
- 조건검색 include가 주문 intent를 만들지 않는 경계가 명시되어 있다.
- TR hydration idempotency 설계가 명시되어 있다.
- `WAIT_DATA`와 `HARD_BLOCK` 구분이 명시되어 있다.
- `RISK_OFF` 정책이 신규 매수 금지, 미체결 취소, 포지션 축소까지 포함한다.
- Dashboard v2 축소 방향이 명시되어 있다.
- LIVE 주문 활성화는 포함하지 않는다.
