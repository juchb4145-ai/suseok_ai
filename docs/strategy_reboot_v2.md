# Strategy Reboot V2 Design

## 목적

Strategy Reboot V2는 기존 hybrid gate, final grade, shadow/promotion, threshold A/B, 복잡한 관측 로직을 더 고도화하지 않고, 조건검색 + TR + 실시간 시세를 분리된 판단 엔진으로 연결하는 새 전략 흐름이다.

이 문서는 1단계 PR의 설계 고정 문서다. 실행 로직의 대규모 삭제나 LIVE 주문 활성화는 범위에 포함하지 않는다.

핵심 원칙:

- 조건검색은 매수 신호가 아니라 후보 감지 센서다.
- TR은 매수 타이밍 판단용이 아니라 후보/시장/계좌 상태 보강용이다.
- 매수/매도 타이밍은 실시간 tick, 1m/3m/5m candle, VWAP, 체결강도, 거래대금, 호가 스프레드로 판단한다.
- 테마 분석, 시장국면, 매수판단, 매도판단, 주문/리스크 관리는 서로 다른 컴포넌트로 분리한다.
- LIVE 주문은 비활성 상태로 유지한다. 1단계는 OBSERVE/DRY_RUN 설계와 skeleton까지만 허용한다.
- 기존 코드 중 재사용 가능한 Gateway/CommandQueue/BrokerModel/TRRunner/MarketDataStore는 유지하되, 전략 판단부는 새 구조로 본다.

## 재사용 경계

재사용한다:

- 32bit Kiwoom Gateway / 64bit Core 분리 구조
- `BrokerPriceTick`, `BrokerConditionEvent`, `BrokerOrderRequest`, `BrokerTrRequest`, `BrokerTrResponse`
- `KiwoomClient`의 조건검색, 실시간, TR, 주문 wrapper
- `KiwoomTrRunner`
- Gateway command queue와 command ack 구조
- `MarketDataStore`의 tick/backfill merge 구조
- Chejan execution event 구조

새로 설계한다:

- 조건검색 후보 감지와 후보 FSM
- 후보 hydration 우선순위와 idempotency 정책
- 실시간 candle/VWAP/체결강도/거래대금/스프레드 기반 entry/exit 판단
- ThemeBoard, MarketRegimeEngine, EntryEngine, ExitEngine, RiskManager, OrderManager의 책임 경계
- 대시보드 요약 모델

명시적으로 새 구조에서 사용하지 않는다:

- hybrid gate를 최종 매수 승인 계층으로 사용하는 방식
- final grade를 주문 여부로 직접 연결하는 방식
- shadow/promotion 결과가 자동 주문 경로를 여는 방식
- threshold A/B가 실시간 주문 경로를 바꾸는 방식
- 조건검색 include 이벤트가 READY/order intent를 직접 만드는 방식

기존 구현은 즉시 삭제하지 않는다. Reboot V2 경로에서는 feature flag 또는 문서상 deprecated로 분리한다.

## 목표 데이터 흐름

```text
[Kiwoom 조건검색/실시간]
  -> ConditionSensor
  -> Candidate FSM
  -> CandidateHydrator(TR)
  -> RealtimeStore/CandleStore
  -> ThemeBoard
  -> MarketRegimeEngine
  -> EntryEngine
  -> ExitEngine
  -> RiskManager
  -> OrderManager
```

이 흐름에서 주문과 가까운 판단은 항상 오른쪽으로 갈수록 좁아진다. 조건검색과 TR은 후보와 상태를 보강할 뿐이며, 주문 intent는 EntryEngine/ExitEngine 결과를 RiskManager와 OrderManager가 통과시킨 뒤 OBSERVE/DRY_RUN 레코드로만 생성된다.

## 컴포넌트 책임

### ConditionSensor

조건검색 이벤트를 정규화하고 후보 감지 이벤트로만 변환한다.

입력:

- `BrokerConditionEvent`
- 조건식 이름/인덱스/목적
- include/remove 이벤트

출력:

- `ConditionHit`
- Candidate FSM 이벤트: `condition_detected`, `condition_seen_again`, `condition_removed`

금지:

- `READY`, `SETUP_READY`, `TIMING_READY` 직접 생성
- entry score/final grade 계산
- order intent 생성
- Gateway `send_order` command 생성

### Candidate FSM

후보의 생애주기를 관리한다. Candidate는 주문 의사결정 객체가 아니라 관측 대상이다.

상태:

- `DETECTED`: 조건검색 또는 수동/테마 watch에서 최초 감지됨
- `HYDRATING`: 신규 후보 보강 TR이 요청되었거나 응답 대기 중
- `WATCHING`: 실시간 tick/candle을 수집하며 setup을 기다림
- `SETUP_READY`: 데이터, 테마, 시장, 종목 역할 조건이 충족됨
- `TIMING_READY`: 가격 타이밍 조건까지 충족되어 주문 검토 가능
- `ORDER_PENDING`: DRY_RUN 주문 intent가 생성되어 체결/취소/만료를 기다림
- `OPEN`: DRY_RUN 포지션 또는 관측 포지션이 열림
- `EXITING`: exit intent가 생성되어 청산 처리 중
- `CLOSED`: 포지션/후보 관측이 종료됨
- `WAIT`: 회복 가능한 대기 상태
- `HARD_BLOCK`: 당일 또는 세션 내 회복 불가능한 차단 상태

대기/차단 reason은 상태와 분리해서 기록한다. 특히 데이터 부족은 `HARD_BLOCK`이 아니라 `WAIT` + `WAIT_DATA` reason으로 처리한다.

권장 reason taxonomy:

- `WAIT_DATA`: tick/candle/TR/테마 데이터 부족
- `WAIT_TR`: hydration command 대기 또는 rate limit 대기
- `WAIT_MARKET`: 시장국면이 아직 허용되지 않음
- `WAIT_THEME`: 테마 확산/주도주 확인 부족
- `WAIT_TIMING`: setup은 있으나 가격 타이밍 미충족
- `WAIT_RISK`: risk budget 또는 포지션 슬롯 대기
- `HARD_BLOCK_INVALID_CODE`: 종목코드 부적합
- `HARD_BLOCK_UNTRADABLE`: 거래정지/관리종목 등 매매 불가
- `HARD_BLOCK_SESSION`: 전략 허용 세션 밖에서 복구 불가
- `HARD_BLOCK_MANUAL`: 운영자 수동 차단

상태 전이 요약:

| From | To | 조건 |
| --- | --- | --- |
| none | `DETECTED` | 조건검색 include, 수동 등록, 테마 watch 등록 |
| `DETECTED` | `HYDRATING` | P1 hydration 필요 |
| `DETECTED` | `WATCHING` | 신규 TR 보강 없이 실시간 관측 가능 |
| `HYDRATING` | `WATCHING` | hydration 성공 또는 비필수 TR 대기 만료 |
| `HYDRATING` | `WAIT` | 필수 데이터 지연, `WAIT_TR`/`WAIT_DATA` |
| `WATCHING` | `SETUP_READY` | Data/Theme/Market/Role 단계 통과 |
| `WATCHING` | `WAIT` | 데이터/시장/테마/리스크 대기 |
| `SETUP_READY` | `TIMING_READY` | 가격 타이밍 통과 |
| `SETUP_READY` | `WATCHING` | setup 조건 훼손 |
| `TIMING_READY` | `ORDER_PENDING` | RiskManager 통과 후 DRY_RUN intent 생성 |
| `TIMING_READY` | `WAIT` | 주문 슬롯/리스크 예산 대기 |
| `ORDER_PENDING` | `OPEN` | DRY_RUN fill 또는 관측 fill 확정 |
| `ORDER_PENDING` | `WATCHING` | 미체결 취소/만료 후 재관측 가능 |
| `OPEN` | `EXITING` | ExitEngine trigger 발생 |
| `EXITING` | `CLOSED` | exit fill/close 확정 |
| any active | `HARD_BLOCK` | 회복 불가능 차단 |
| `WAIT` | previous active | 대기 reason 해소 |

`ConditionSensor`는 `DETECTED` 또는 기존 후보의 `last_seen_at`/hit counter 갱신까지만 수행한다. 조건검색 include는 `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING`으로 건너뛸 수 없다.

## ConditionHit 모델

`ConditionHit`은 조건검색 결과를 후보 감지 센서 데이터로 저장하는 모델이다.

필드:

```text
code: str
condition_name: str
condition_level: ALIVE | STRONG | LEADER
event_type: include | remove
first_seen_at: datetime
last_seen_at: datetime
hit_count: int
```

정규화 규칙:

- `code`는 `A` prefix 제거 후 6자리 숫자만 허용한다.
- `condition_level`은 조건식 purpose 또는 이름 매핑으로 결정한다.
- 같은 `trade_date + code + condition_name + condition_level`의 include는 hit_count를 증가시키고 `last_seen_at`만 갱신한다.
- remove 이벤트는 후보를 즉시 매도/차단하지 않는다. 해당 조건식 source만 제거하고, ThemeBoard와 ExitEngine이 별도로 테마 약화/주도주 붕괴를 판단한다.

조건 레벨 의미:

- `ALIVE`: 테마 생존/관심권 감지
- `STRONG`: 테마 강세/확산 감지
- `LEADER`: 테마 주도주 감지

## TR Hydration 정책

TR은 매수 타이밍 판단용이 아니다. TR은 후보/시장/계좌 상태를 보강하고 실시간 판단의 context를 제공한다.

우선순위:

| Priority | 대상 | 목적 | 예시 |
| --- | --- | --- | --- |
| P0 | 계좌/잔고/미체결 | 주문 가능 상태와 리스크 계산 | 예수금, 보유 수량, 미체결 주문 |
| P1 | 신규 후보 | 후보 기본정보와 거래 가능성 확인 | 종목명, 시장, 전일종가, 상하한가, 거래정지 여부 |
| P2 | 테마 구성 종목 | ThemeBoard breadth/leader 보강 | 테마 구성 종목 스냅샷, 전일 기준가, 거래대금 보강 |
| P3 | 장전/장후 보강 | 다음 세션 준비/리뷰 | 전일 고저, 일봉/분봉 backfill, 후처리 리뷰 |

스케줄링 원칙:

- P0는 항상 최우선이며 주문 intent 생성 전 최신성을 확인한다.
- P1은 `DETECTED -> HYDRATING` 전이에서 생성한다.
- P2는 ThemeBoard가 구성 종목 상태를 계산하기 위해 필요한 경우에만 생성한다.
- P3는 장중 entry/exit 판단을 막지 않으며 장전/장후 batch로 실행한다.
- TR rate limit 초과나 지연은 `WAIT_TR`로 기록한다.
- TR 실패가 매매 불가 정보처럼 확정적인 위험을 뜻하지 않으면 `HARD_BLOCK`이 아니라 `WAIT_DATA` 또는 `WAIT_TR`이다.

Idempotency key:

```text
tr:{trade_date}:{priority}:{tr_code}:{rq_name}:{normalized_inputs_hash}
```

예시:

```text
tr:2026-06-17:P1:opt10001:candidate_basic:code=005930
tr:2026-06-17:P0:opw00018:account_balance:account=12345678
tr:2026-06-17:P2:theme_members:theme=semiconductor:bucket=0935
```

규칙:

- `normalized_inputs_hash`는 정렬된 input key/value를 기준으로 만든다.
- 같은 active idempotency key가 `QUEUED`, `DISPATCHED`, `ACKED` 보존 구간에 있으면 중복 생성하지 않는다.
- P1 후보 hydration은 candidate id가 바뀌어도 같은 trade_date/code/TR 목적이면 중복 요청하지 않는다.
- 응답이 도착하면 CandidateHydrator가 Candidate context를 갱신하고, 가격 타이밍 판단은 RealtimeStore/CandleStore가 계속 담당한다.

## RealtimeStore/CandleStore

RealtimeStore는 tick의 최신성과 체결 품질을 관리한다. CandleStore는 1m/3m/5m candle, VWAP, 거래대금, 체결강도 추세, 스프레드 추세를 관리한다.

필수 입력:

- `BrokerPriceTick`
- 현재가
- 누적거래량
- 거래대금
- 체결강도
- 최우선 매수/매도호가
- 호가 스프레드
- 체결시각

Entry/ExitEngine이 참조하는 최소 지표:

- 최근 tick freshness
- 1m/3m/5m candle 방향과 변동성
- intraday VWAP 대비 위치
- 체결강도 절대값과 변화
- 거래대금 재가속
- 호가 스프레드 허용 범위
- 당일 고저와 눌림/돌파 위치

TR backfill price는 `metadata.price_source=TR_BACKFILL`로 표시하고, 실시간 가격 타이밍의 단독 근거로 쓰지 않는다.

## ThemeBoard 정책

ThemeBoard는 개별 종목 점수가 아니라 테마 단위의 확산, 주도주, breadth, leader health를 계산한다.

상태:

- `LEADING_THEME`: 주도주가 명확하고 테마 breadth와 거래대금이 동반 확산
- `SPREADING_THEME`: 강세 종목 수가 증가하고 거래대금이 확산되지만 leader dominance는 아직 중간
- `LEADER_ONLY_THEME`: leader는 강하지만 후속 확산이 약함
- `WATCH_THEME`: 관심권이나 entry에 필요한 강도/확산 부족
- `WEAK_THEME`: 주도주 약화, breadth 붕괴, 거래대금 둔화

주요 입력:

- ConditionHit의 `ALIVE`/`STRONG`/`LEADER`
- 구성 종목 실시간 tick/candle
- 테마별 거래대금 rank
- leader 후보의 VWAP/지지선 상태
- 상승 종목 수, 강세 종목 수, 신고가/고가권 종목 수

Entry에서 허용하는 기본 정책:

- `LEADING_THEME`: leader와 strong follower 모두 허용 가능
- `SPREADING_THEME`: leader 또는 확산 초입 strong follower 허용 가능
- `LEADER_ONLY_THEME`: leader만 허용, follower 신규 진입 금지
- `WATCH_THEME`: 신규 매수 금지, 관측만
- `WEAK_THEME`: 신규 매수 금지, 보유 포지션 exit 검토

## MarketRegime 정책

MarketRegimeEngine은 전체 시장과 지수/업종 breadth를 기준으로 신규 진입 허용도를 결정한다.

상태:

- `EXPANSION`: 지수/거래대금/breadth가 확장, 신규 진입 허용
- `SELECTIVE`: 일부 테마만 강함, 주도테마/주도주 중심으로 제한 허용
- `CHOPPY`: 방향성 낮고 변동성/휩쏘 큼, 신규 진입 제한
- `WEAK`: 지수와 breadth 약세, 신규 진입 원칙적 금지
- `RISK_OFF`: 시장 리스크 회피, 신규 매수 금지와 리스크 축소

기본 정책:

| MarketRegime | 신규 매수 | 미체결 주문 | 보유 포지션 |
| --- | --- | --- | --- |
| `EXPANSION` | 허용 | 유지 가능 | 정상 관리 |
| `SELECTIVE` | 주도테마/leader 중심 제한 허용 | 품질 낮은 주문 취소 검토 | 테마 약화 감시 강화 |
| `CHOPPY` | 원칙적 금지, 예외는 leader-only 소액 DRY_RUN | 공격적 주문 취소 | 시간/스프레드 리스크 축소 |
| `WEAK` | 금지 | 신규/추격성 미체결 취소 | 손절/시간청산 조건 강화 |
| `RISK_OFF` | 금지 | 모든 신규 매수 미체결 취소 | 포지션 리스크 축소, market risk exit 검토 |

`RISK_OFF`에서는 반드시 다음을 수행한다:

- 신규 매수 intent 생성 금지
- 기존 buy-side 미체결 취소 intent 생성
- 보유 포지션의 사이즈 축소 또는 exit 검토
- 대시보드와 이벤트 로그에 `MARKET_RISK_OFF` reason 기록

## EntryEngine 5단계 판단

EntryEngine은 후보를 `TIMING_READY`로 올릴 수 있는 유일한 전략 판단 계층이다. 단, 실제 주문 intent 생성은 RiskManager와 OrderManager 이후에만 가능하다.

판단 단계:

1. Data Ready
2. Theme Ready
3. Market Allowed
4. Stock Role Allowed
5. Price Timing Ready

### 1. Data Ready

필수 조건:

- 최근 tick 존재
- 1m candle 최소 개수 충족
- VWAP 계산 가능
- 체결강도/거래대금/스프레드 입력 최신
- P1 hydration이 성공했거나 비필수 데이터 대기 만료

실패 처리:

- 데이터 부족은 `WAIT` + `WAIT_DATA`
- TR 대기는 `WAIT` + `WAIT_TR`
- 거래 불가 확정만 `HARD_BLOCK_UNTRADABLE`

### 2. Theme Ready

허용 조건:

- ThemeBoard 상태가 `LEADING_THEME` 또는 `SPREADING_THEME`
- `LEADER_ONLY_THEME`는 leader 종목만 통과
- `WATCH_THEME`/`WEAK_THEME`는 신규 진입 금지

실패 처리:

- 확산 부족은 `WAIT_THEME`
- 약화 확정은 신규 진입 금지, 보유는 ExitEngine에서 처리

### 3. Market Allowed

허용 조건:

- `EXPANSION`: 기본 허용
- `SELECTIVE`: 주도테마/leader 중심 제한 허용
- `CHOPPY`: 기본 금지, 실험성 DRY_RUN도 별도 flag 필요
- `WEAK`, `RISK_OFF`: 신규 매수 금지

실패 처리:

- `WAIT_MARKET`
- `RISK_OFF`는 신규 entry 금지와 미체결 취소 정책을 동시에 발동

### 4. Stock Role Allowed

역할:

- `LEADER`: 주도주
- `STRONG_FOLLOWER`: 확산 테마의 강한 후발주
- `WATCH_MEMBER`: 관측만
- `WEAK_MEMBER`: 신규 금지

허용:

- `LEADING_THEME`: `LEADER`, `STRONG_FOLLOWER`
- `SPREADING_THEME`: `LEADER`, 초기 `STRONG_FOLLOWER`
- `LEADER_ONLY_THEME`: `LEADER`만

### 5. Price Timing Ready

실시간 가격 조건:

- 최근 tick freshness 충족
- 1m/3m/5m candle 구조가 entry setup과 일치
- 가격이 VWAP 또는 정의된 지지/돌파 기준과 일치
- 체결강도와 거래대금이 동반 개선
- 스프레드가 허용 범위 안
- 추격 매수 위험이 제한값 이하

통과 시:

- Candidate 상태를 `TIMING_READY`로 전이
- RiskManager에 주문 가능성 평가 요청

실패 시:

- `SETUP_READY` 또는 `WATCHING` 유지
- `WAIT_TIMING` reason 기록

## ExitEngine 판단

ExitEngine은 보유 포지션 또는 DRY_RUN 포지션에 대해 독립적으로 판단한다. 조건검색 remove 이벤트는 exit trigger가 아니다. remove는 ThemeBoard/leader health를 약화시키는 입력일 뿐이다.

Exit trigger:

- `TAKE_PROFIT`: 목표 수익 또는 분할 익절 조건 도달
- `SUPPORT_LOSS`: VWAP/분봉 지지/당일 기준선 이탈
- `TIME_EXIT`: 정해진 시간 내 진전 부족
- `TRAILING_STOP`: 고점 대비 반납률 초과
- `THEME_WEAK_EXIT`: ThemeBoard가 `WEAK_THEME`로 약화
- `LEADER_COLLAPSE_EXIT`: leader VWAP 이탈, 거래대금 급감, 급락
- `INDEX_WEAK_EXIT`: 관련 지수/업종 약화
- `MARKET_RISK_OFF_EXIT`: 시장국면 `RISK_OFF`
- `BREADTH_COLLAPSE_EXIT`: 테마 breadth 급감

Exit 우선순위:

1. `MARKET_RISK_OFF_EXIT`
2. `SUPPORT_LOSS`
3. `LEADER_COLLAPSE_EXIT`
4. `BREADTH_COLLAPSE_EXIT`
5. `THEME_WEAK_EXIT`
6. `INDEX_WEAK_EXIT`
7. `TRAILING_STOP`
8. `TAKE_PROFIT`
9. `TIME_EXIT`

RISK_OFF에서는 수익/손실 여부와 무관하게 리스크 축소가 우선이다.

## RiskManager 정책

RiskManager는 EntryEngine/ExitEngine의 판단을 주문 가능한 intent로 바꾸기 전에 계좌/포지션/시장 위험을 확인한다.

입력:

- P0 계좌/잔고/미체결 hydration
- MarketRegime
- 후보/테마/역할
- 포지션 슬롯
- 종목별/테마별 노출
- 당일 손실 한도
- Gateway health와 command queue 상태

신규 매수 차단:

- `RISK_OFF`
- P0 계좌 상태 최신성 부족
- 동일 종목 open/order pending 중복
- 테마 노출 한도 초과
- 종목 스프레드/유동성 위험 초과
- 당일 손실 한도 초과
- Gateway 불안정 또는 command ack 지연

데이터 부족은 기본적으로 `WAIT_RISK` 또는 `WAIT_DATA`다. 운영자가 지정한 kill switch, 거래불가, 리스크 한도 초과처럼 회복 불가능하거나 세션 내 복구가 불가능한 경우에만 `HARD_BLOCK`으로 전환한다.

## OrderManager 정책

1단계에서 OrderManager는 OBSERVE/DRY_RUN까지만 구현 대상으로 본다.

허용:

- DRY_RUN buy intent 기록
- DRY_RUN sell/exit intent 기록
- buy-side 미체결 취소 intent 기록
- command queue idempotency 설계
- order/execution/Chejan event correlation 설계

금지:

- Gateway `send_order` 생성
- LIVE 주문 활성화
- 조건검색 또는 TR 응답에서 직접 주문 intent 생성

주문 intent 생성 조건:

```text
Candidate TIMING_READY
  + EntryEngine pass
  + RiskManager pass
  + runtime mode DRY_RUN
  + dry-run feature flag enabled
  -> DRY_RUN order intent
```

LIVE 주문은 별도 안전 PR에서만 다룬다. 현재 Reboot V2에서는 `LIVE_DISABLED`를 명시적 불변 조건으로 둔다.

## 기존 hybrid/final grade 계층 처리

Reboot V2는 기존 hybrid/final grade/promotion 계층을 더 고도화하지 않는다.

정책:

- 기존 계층은 현행 테스트와 운영 리포트 보존을 위해 유지한다.
- 새 v2 runtime이 켜진 경우 기존 hybrid gate 결과는 주문 승인 입력으로 사용하지 않는다.
- final grade는 리뷰/분석용 legacy artifact로만 둔다.
- shadow/promotion은 LIVE 전환 근거가 아니라 별도 분석 리포트로만 둔다.
- threshold A/B는 새 EntryEngine threshold를 자동 변경하지 않는다.
- 기존 dashboard panel은 새 요약 dashboard로 점진 축소한다.

권장 flag:

```text
STRATEGY_REBOOT_V2_ENABLED=0
STRATEGY_REBOOT_V2_DRY_RUN=0
STRATEGY_REBOOT_V2_LIVE_DISABLED=1
STRATEGY_REBOOT_V2_USE_LEGACY_HYBRID=0
```

`STRATEGY_REBOOT_V2_LIVE_DISABLED=1`은 기본값이며 1단계에서 변경하지 않는다.

## 검증 기준 매핑

### 1. condition include 이벤트가 발생해도 주문 intent가 생성되지 않아야 한다

ConditionSensor는 `ConditionHit`과 Candidate `DETECTED`/hit counter만 생성한다. 주문 intent는 EntryEngine, RiskManager, OrderManager를 모두 통과해야 하며 condition include 경로에는 없다.

### 2. condition include -> Candidate DETECTED/HYDRATING 상태가 되어야 한다

신규 후보는 `DETECTED`로 시작한다. P1 보강이 필요하면 CandidateHydrator가 idempotent TR command를 만들고 상태를 `HYDRATING`으로 전환한다. P1이 불필요하거나 이미 최신이면 `WATCHING`으로 전환 가능하다.

### 3. TR hydration command가 idempotency key로 중복 없이 생성되는 설계를 제시해야 한다

TR command key는 `tr:{trade_date}:{priority}:{tr_code}:{rq_name}:{normalized_inputs_hash}`다. CommandQueue의 active dedupe 상태에 동일 key가 있으면 새 command를 만들지 않는다.

### 4. 데이터 부족은 HARD_BLOCK이 아니라 WAIT_DATA로 처리해야 한다

tick/candle/TR/테마 데이터 부족은 Candidate `WAIT` 상태와 `WAIT_DATA` reason으로 기록한다. 거래 불가 확정, kill switch, 수동 차단 등만 `HARD_BLOCK`이다.

### 5. 시장 RISK_OFF 정책

`RISK_OFF`에서는 신규 매수 금지, buy-side 미체결 취소, 보유 포지션 리스크 축소/exit 검토를 수행한다. 모든 이벤트는 `MARKET_RISK_OFF` 또는 `MARKET_RISK_OFF_EXIT` reason을 남긴다.

### 6. 대시보드 단순화 방향

최종 dashboard는 다음만 우선 노출한다:

- 현재 시장국면
- 주도테마 TOP5
- READY 후보
- 보유 리스크
- 차단/대기 사유 TOP

상세 hybrid score, final grade, threshold A/B panel은 v2 기본 화면에서 제거하거나 legacy tab으로 이동한다.

## Dashboard V2 방향

요약 화면:

- Market Regime: `EXPANSION`/`SELECTIVE`/`CHOPPY`/`WEAK`/`RISK_OFF`
- Leading Themes TOP5: theme status, leader, breadth, trade value rank
- Ready Candidates: `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING`
- Position Risk: open exposure, theme concentration, stop/exit triggers
- Wait/Block Reasons TOP: `WAIT_DATA`, `WAIT_THEME`, `WAIT_MARKET`, `WAIT_TIMING`, `WAIT_RISK`, hard block top reasons

제외/축소:

- 조건검색 raw hit table은 상세 drilldown으로 이동
- hybrid/final grade 상세는 legacy/debug tab으로 이동
- threshold A/B 결과는 운영 리포트로 분리
- 주문 버튼 또는 LIVE enable control은 제공하지 않는다

## 1단계 비범위

- 대규모 코드 삭제
- LIVE 주문 활성화
- 기존 strategy runtime 완전 교체
- 기존 DB migration 강제 적용
- 새 dashboard UI 완성
- entry/exit threshold calibration 자동화
- 실전 주문 promotion

1단계의 완료 기준은 새 전략 흐름, 상태, 모델, hydration/idempotency, risk-off, dashboard 축소 방향이 문서로 고정되고, 필요한 경우 최소 skeleton이 추가되는 것이다.
