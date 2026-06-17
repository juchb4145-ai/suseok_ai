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

## PR 2: Candidate Ingestion + CandidateHydrator 고정 사항

이 PR은 조건검색과 Opening Burst를 공통 후보 유입 모델로 묶고, 후보 보강 TR을 idempotent command로 큐잉하는 범위까지만 구현한다. `READY`, `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING`, `EntryPlan`, DRY_RUN buy intent, LIVE order command는 이 PR의 산출물이 아니다.

### CandidateSourceEvent

모든 후보 유입은 `CandidateSourceEvent`로 정규화한다.

```text
trade_date: str
code: str
name: str
source_type: condition_search | opening_burst | manual_watch | theme_board
source_id: str
source_rank: int
source_score: float
theme_id: str
theme_name: str
stock_role: str
reason_codes: list[str]
raw_payload: dict
detected_at: str
```

정규화 규칙:

- 조건검색 include는 `condition_search` source event를 만들고 Candidate를 `DETECTED`로 생성 또는 merge한다.
- 조건검색 remove는 해당 source만 비활성화한다. 활성 source가 모두 사라진 관찰 후보만 `REMOVED`가 될 수 있다.
- Opening Burst는 `selected` 종목만 `opening_burst` source event를 만든다. 제외/관찰 종목은 Candidate를 만들지 않는다.
- 수동 watch와 ThemeBoard 후보는 같은 모델을 쓰되, 별도 entry 판단을 직접 만들 수 없다.

Candidate merge key는 `trade_date + code`다. 같은 종목이 조건검색과 Opening Burst에 동시에 잡히면 하나의 active Candidate로 합쳐지고, source별 정보는 `metadata.candidate_ingestion.source_map`에 남긴다. 운영상 대표 source는 우선순위와 score/rank로 선택한다.

### PR 2 Candidate 상태

이 PR에서 활성화되는 상태는 다음으로 제한한다.

- `DETECTED`
- `HYDRATING`
- `WATCHING`
- `WAIT_DATA`
- `REMOVED`
- `EXPIRED`

데이터 부족은 `WAIT_DATA` 상태와 reason code로 표현한다. `WAIT_DATA`는 회복 가능한 대기이며 `HARD_BLOCK`이 아니다. 테마 미매핑은 `theme_unmapped` reason만 추가하고, 가격/변동률/source 최소 데이터가 충분하면 `WATCHING`으로 갈 수 있다.

### CandidateHydrator

Hydrator는 `DETECTED` 또는 보강이 필요한 `WAIT_DATA` 후보에 대해 P1 기본 보강 TR command를 생성한다.

기본값:

```text
tr_code=opt10001
rq_name=CandidateHydration_opt10001
bucket=basic
purpose=candidate_hydration
response_mode=capture
idempotency_key=candidate_hydration:{trade_date}:{code}:{tr_code}:{bucket}
max_per_cycle=5
max_pending=10
ttl_sec=90
```

환경 변수:

```text
TRADING_CANDIDATE_HYDRATION_MAX_PER_CYCLE
TRADING_CANDIDATE_HYDRATION_MAX_PENDING
TRADING_CANDIDATE_HYDRATION_TTL_SEC
```

보강 필드는 다음을 기본으로 한다. 영문 alias와 키움 한글 필드 alias는 모두 허용한다.

```text
stock_name
current_price
change_rate
volume
trade_value
open_price
day_high
day_low
prev_close
```

우선순위:

- `HIGH`: `opening_burst` + `LEADER`/`CO_LEADER`
- `MEDIUM`: `condition_search` + `theme_id` 있음
- `LOW`: 미매핑 source 또는 약한 source

CommandQueue에 같은 idempotency key가 active 상태로 존재하면 새 command를 만들지 않는다. 중복 요청은 hydration request record에는 `DUPLICATE`로 남기되, 후보는 보강 대기 상태를 유지한다.

### TR ack merge

`purpose=candidate_hydration` ack는 CandidateHydrator가 처리한다.

- parsed payload를 `Candidate.metadata.candidate_hydration.parsed`에 저장한다.
- `candidate_hydration_requests` 상태를 `ACKED`로 갱신하고 `candidate_hydration_results`에 raw/parsed payload를 남긴다.
- `MarketDataStore.apply_theme_backfill`로 TR backfill tick을 병합한다.
- TR-only 가격은 `price_source=TR_BACKFILL`로 표시하고 `gate_usable_for_entry=false`를 유지한다.
- 실시간 tick이 들어오면 실시간 가격이 entry timing source가 되며, TR 가격은 후보 보강 context로만 남는다.
- 최소 데이터가 충분하면 `WATCHING`, 부족하면 `WAIT_DATA`와 `WAIT_DATA_*` reason으로 남긴다.

### 저장소와 대시보드

최소 SQLite 테이블:

- `candidate_source_events`
- `candidate_hydration_requests`
- `candidate_hydration_results`

Dashboard snapshot에는 `candidate_ingestion` 섹션을 추가한다.

```text
detected_count
hydrating_count
watching_count
wait_data_count
source_counts
hydration_pending_count
hydration_error_count
top_wait_data_reasons
```

이 섹션은 최종 Dashboard V2의 전 단계다. 최종 화면은 시장국면, 주도테마 TOP5, READY 후보, 보유 리스크, 차단/대기 사유 TOP만 보는 방향을 유지한다.

## PR 3: ThemeBoard Runtime Unification

ThemeBoard는 `WATCHING`/`WAIT_DATA` 후보, 실시간 tick, TR backfill metadata, theme membership, Opening Burst source를 테마 단위로 압축하는 관찰/분류 계층이다. 매수 판단 엔진이 아니며 `READY`, `SETUP_READY`, `TIMING_READY`, `EntryPlan`, DRY_RUN buy intent, LIVE order command를 만들지 않는다.

위치:

- `trading/theme_engine/theme_board.py`
- runtime cycle hook: Opening Burst 반영 후 ThemeBoard 실행
- dashboard section: `theme_board`

기본 runtime flag:

```text
TRADING_THEME_BOARD_ENABLED=false
TRADING_THEME_BOARD_OBSERVE_ONLY=true
TRADING_THEME_BOARD_INTERVAL_SEC=5
```

### ThemeBoardSnapshot 계약

`ThemeBoardSnapshot`은 Runtime/API/Dashboard가 공유하는 단일 payload다.

```text
trade_date
calculated_at
board_status
theme_count
active_theme_count
watch_theme_count
data_wait_theme_count
top_themes
stocks
source_counts
data_quality_flags
reason_codes
output_mode=OBSERVE
ready_allowed=false
order_intent_allowed=false
```

## PR 4: MarketRegimeEngine Runtime Integration

MarketRegimeEngine은 지수 tick, 후보 universe breadth, ThemeBoard overlay를 이용해 KOSPI/KOSDAQ 시장국면을 분리 계산하는 관찰/정책 계층이다. 매수 타이밍 엔진이 아니며 `READY`, `SETUP_READY`, `TIMING_READY`, `ORDER_PENDING`, `EntryPlan`, DRY_RUN buy intent, LIVE order command를 만들지 않는다.

### Runtime 계약

- 기본 feature flag는 `TRADING_MARKET_REGIME_ENABLED=false`다.
- `TRADING_MARKET_REGIME_OBSERVE_ONLY=true`가 기본이며 산출물의 `output_mode=OBSERVE`, `ready_allowed=false`, `order_intent_allowed=false`를 고정한다.
- Runtime cycle에서는 Opening Burst, ThemeBoard 이후 MarketRegime을 실행한다.
- 데이터 부족은 `DATA_WAIT`/`WAIT_DATA`로 남기며 `HARD_BLOCK` 또는 후보 삭제로 승격하지 않는다.
- `REMOVED`/`EXPIRED` 후보는 정책 계산과 metadata merge 대상에서 제외한다.

### 설정값

```text
TRADING_MARKET_REGIME_ENABLED=false
TRADING_MARKET_REGIME_OBSERVE_ONLY=true
TRADING_MARKET_REGIME_INTERVAL_SEC=5
TRADING_MARKET_REGIME_KOSPI_CODE=001
TRADING_MARKET_REGIME_KOSDAQ_CODE=101
TRADING_MARKET_REGIME_WEAK_KOSPI_PCT=-0.8
TRADING_MARKET_REGIME_WEAK_KOSDAQ_PCT=-1.0
TRADING_MARKET_REGIME_RISK_OFF_KOSPI_PCT=-2.0
TRADING_MARKET_REGIME_RISK_OFF_KOSDAQ_PCT=-2.5
TRADING_MARKET_REGIME_BREADTH_EXPANSION_PCT=0.58
TRADING_MARKET_REGIME_BREADTH_WEAK_PCT=0.38
TRADING_MARKET_REGIME_BREADTH_RISK_OFF_PCT=0.28
TRADING_MARKET_REGIME_MIN_BREADTH_SAMPLE_KOSPI=80
TRADING_MARKET_REGIME_MIN_BREADTH_SAMPLE_KOSDAQ=120
TRADING_MARKET_REGIME_MAX_QUOTE_AGE_SEC=60
```

### 모델 계약

- `MarketRegimeStatus`: `EXPANSION`, `SELECTIVE`, `CHOPPY`, `WEAK`, `RISK_OFF`, `DATA_WAIT`, `MARKET_CLOSED`
- `MarketSide`: `KOSPI`, `KOSDAQ`, `UNKNOWN`
- `CandidateMarketAction`: `ALLOW_NORMAL`, `ALLOW_REDUCED`, `WAIT_MARKET`, `BLOCK_NEW_ENTRY`, `DATA_WAIT`, `MARKET_CLOSED`
- Models: `MarketRegimeConfig`, `MarketSideSnapshot`, `MarketBreadthSnapshot`, `MarketRegimeSnapshot`, `CandidateMarketPolicy`, `MarketRegimeResult`

`MarketSideSnapshot` 필드:

```text
side
status
index_code
index_name
index_price
index_return_pct
index_slope_1m_pct
index_slope_3m_pct
index_slope_5m_pct
index_slope_20m_pct
position_vs_vwap
position_vs_day_mid
low_break_recent
high_break_recent
breadth_pct
advancing_count
declining_count
flat_count
strong_count
weak_count
valid_quote_count
valid_quote_ratio
turnover_weighted_return_pct
risk_score
data_quality_flags
reason_codes
```

`MarketRegimeSnapshot` 필드:

```text
trade_date
calculated_at
global_status
kospi_status
kosdaq_status
kospi_snapshot
kosdaq_snapshot
candidate_policy_by_code
market_session_status
market_open
market_closed
risk_off_detected
weak_market_detected
data_wait_count
policy_summary
data_quality_flags
reason_codes
output_mode=OBSERVE
ready_allowed=false
order_intent_allowed=false
```

`CandidateMarketPolicy` 필드:

```text
code
market_side
market_side_source
market_status
global_market_status
market_action
position_size_multiplier_hint
block_new_entry
wait_reason
recheck_after_sec
reason_codes
```

### 정책 매핑

- `EXPANSION`: `ALLOW_NORMAL`, size multiplier `1.0`
- `SELECTIVE`: `ALLOW_REDUCED`, size multiplier `0.5~0.7`
- `CHOPPY`: `WAIT_MARKET` 또는 reduced 관찰, 기본 multiplier `0.25~0.5`
- `WEAK`: `WAIT_MARKET`, `block_new_entry=true`
- `RISK_OFF`: `BLOCK_NEW_ENTRY`, `block_new_entry=true`
- `DATA_WAIT`: `DATA_WAIT`
- `MARKET_CLOSED`: `MARKET_CLOSED`

`RISK_OFF`는 후보를 삭제하지 않는다. 신규 매수만 금지하고, 후속 RiskManager/OrderManager 단계에서 buy-side 미체결 취소와 보유 포지션 축소를 별도 intent로 다룬다.

### Candidate Metadata Merge

```text
market_side
market_side_source
market_regime_status
global_market_regime_status
market_action
market_position_size_multiplier_hint
market_block_new_entry
market_reason_codes
updated_by_market_regime_at
```

허용 영향은 `WATCHING` 유지, `WAIT_DATA` 유지, 시장 reason 추가뿐이다. `REMOVED`/`EXPIRED`는 제외한다. 금지 영향은 `READY` 계열 전이, `EntryPlan`, DRY_RUN buy intent, LIVE order command, hybrid/final grade 또는 threshold/promotion 자동 변경, RISK_OFF 후보 삭제다.

### Storage와 Dashboard

SQLite 테이블:

- `market_regime_snapshots`
- `market_side_snapshots`
- `candidate_market_policies`

Dashboard `market_regime` section:

```text
calculated_at
global_status
kospi_status
kosdaq_status
kospi_return_pct
kosdaq_return_pct
kospi_breadth_pct
kosdaq_breadth_pct
expansion/selective/choppy/weak/risk_off reason
candidate_policy_summary
block_new_entry_count
wait_market_count
data_wait_count
warnings
```

Candidate table은 가능한 경우 `market_side`, `market_status`, `market_action`, `market_reason_codes`를 표시한다. 최종 Dashboard V2 방향은 시장국면, 주도테마 TOP5, READY 후보, 보유 리스크, 차단/대기 사유 TOP만 보는 단순 화면이다.

### ThemeBoard Overlay

MarketRegime은 최신 ThemeBoard snapshot에 `market_side_distribution`, `market_status_distribution`, `dominant_market_side`, `market_risk_flag`를 overlay할 수 있다. Overlay는 ThemeBoard의 `LEADING_THEME`, `SPREADING_THEME`, `LEADER_ONLY_THEME`, `WATCH_THEME`, `WEAK_THEME` 자체를 강제로 바꾸지 않는다.

## PR 5: EntryEngine Reboot V2 Runtime Integration

EntryEngine은 `WATCHING` 후보에 대해 Reboot V2의 5단계 진입 판단을 observe-only로 산출한다. 기존 hybrid gate, final grade, EntryPlanBuilder, promotion, threshold A/B와 직접 연결하지 않는다. 조건검색 include, Opening Burst selected, TR hydration 가격만으로는 `OBSERVE_READY`가 될 수 없다.

### Runtime 계약

- 기본 feature flag는 `TRADING_ENTRY_ENGINE_ENABLED=false`다.
- `TRADING_ENTRY_ENGINE_OBSERVE_ONLY=true`가 기본이며 LIVE order command는 항상 금지한다.
- `TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS=false`가 기본이다.
- Runtime cycle 순서는 `Opening Burst -> ThemeBoard -> MarketRegime -> EntryEngine`이다.
- Candidate state는 기본적으로 `WATCHING`을 유지하고, readiness는 `entry_decisions` 테이블과 candidate metadata에만 기록한다.

### 5단계 판단

```text
1. Data Ready
2. Theme Ready
3. Market Allowed
4. Stock Role Allowed
5. Price Timing Ready
```

최종 `EntryDecisionStatus`:

- `OBSERVE_READY`
- `WAIT`
- `HARD_BLOCK`
- `DATA_WAIT`
- `MARKET_WAIT`
- `THEME_WAIT`
- `PRICE_WAIT`

Check status:

- `PASS`
- `WAIT`
- `BLOCK`
- `DATA_WAIT`

### 설정값

```text
TRADING_ENTRY_ENGINE_ENABLED=false
TRADING_ENTRY_ENGINE_OBSERVE_ONLY=true
TRADING_ENTRY_ENGINE_INTERVAL_SEC=5
TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS=false
TRADING_ENTRY_MAX_TICK_AGE_SEC=10
TRADING_ENTRY_MIN_1M_CANDLES=3
TRADING_ENTRY_REQUIRE_REALTIME_TICK=true
TRADING_ENTRY_REQUIRE_VWAP=false
TRADING_ENTRY_REQUIRE_TURNOVER=true
TRADING_ENTRY_GOOD_PULLBACK_MIN_PCT=0.7
TRADING_ENTRY_GOOD_PULLBACK_MAX_PCT=3.5
TRADING_ENTRY_MAX_VWAP_GAP_LEADER_PCT=5.0
TRADING_ENTRY_MAX_VWAP_GAP_CO_LEADER_PCT=4.0
TRADING_ENTRY_MAX_VWAP_GAP_FOLLOWER_PCT=3.0
TRADING_ENTRY_CHASE_HIGH_PULLBACK_MIN_PCT=0.3
TRADING_ENTRY_MAX_SPREAD_TICKS=3
TRADING_ENTRY_VI_COOLDOWN_SEC=180
TRADING_ENTRY_UPPER_LIMIT_MIN_GAP_PCT=3.0
```

### EntryDecision 필드

```text
trade_date
calculated_at
candidate_id
code
name
theme_id
theme_name
theme_status
stock_role
market_side
market_status
market_action
price_location
entry_status
data_ready_status
theme_ready_status
market_ready_status
role_ready_status
price_timing_status
current_price
reference_price
vwap
support_price
breakout_level
limit_price_hint
stop_loss_price_hint
take_profit_price_hint
position_size_multiplier_hint
ready_allowed
dry_run_intent_allowed
live_order_allowed=false
reason_codes
operator_message_ko
details
```

### 단계별 정책

Data Ready:

- recent realtime tick이 필수다.
- TR backfill 가격만 있으면 `DATA_WAIT`다.
- tick age 초과, current price 누락, 1m candle warmup 부족은 `DATA_WAIT`다.
- turnover/cum volume 누락은 `WAIT` 계열 reason으로 남긴다.

Theme Ready:

- `LEADING_THEME`, `SPREADING_THEME`은 통과 가능하다.
- `LEADER_ONLY_THEME`은 `LEADER`/`CO_LEADER`만 통과 가능하다.
- `WATCH_THEME`은 `THEME_WAIT`, `WEAK_THEME`은 `HARD_BLOCK`, `DATA_WAIT`은 `DATA_WAIT`이다.

Market Allowed:

- `ALLOW_NORMAL`은 통과한다.
- `ALLOW_REDUCED`는 통과하되 position size multiplier를 축소한다.
- `WAIT_MARKET`은 `MARKET_WAIT`이다.
- `BLOCK_NEW_ENTRY`는 `HARD_BLOCK`이다.
- `DATA_WAIT`은 `DATA_WAIT`, `MARKET_CLOSED`는 차단한다.

Stock Role Allowed:

- `LEADER`, `CO_LEADER`는 통과 가능하다.
- `FOLLOWER`는 `EXPANSION` 시장과 `LEADING_THEME`/`SPREADING_THEME`에서만 observe-ready 가능하다.
- `LATE_LAGGARD`, `WEAK_MEMBER`, `OVERHEATED`는 차단한다.

Price Timing:

- 정상 ready 위치는 `GOOD_PULLBACK`, `PULLBACK_RECLAIM`, `VWAP_RECLAIM`이다.
- `BREAKOUT_CONTINUATION`은 `EXPANSION + LEADER`에서만 observe-ready 가능하다.
- `CHASE_HIGH`, `VWAP_OVEREXTENDED`, `FAILED_BREAKOUT`, `DEEP_PULLBACK`, `UNKNOWN`, `DATA_WAIT`은 ready가 아니다.
- VI active, 상한가 근접, spread 과대는 진입을 차단하거나 대기시킨다.

### 저장소와 Dashboard

SQLite 테이블:

- `entry_decisions`
- `entry_decision_checks`

Candidate metadata merge:

```text
entry_status
entry_price_location
entry_reason_codes
entry_operator_message_ko
entry_ready_allowed
entry_dry_run_intent_allowed
entry_live_order_allowed=false
entry_limit_price_hint
entry_stop_loss_price_hint
entry_take_profit_price_hint
updated_by_entry_engine_at
```

Dashboard `entry_engine` section:

```text
calculated_at
evaluated_count
observe_ready_count
wait_count
hard_block_count
data_wait_count
market_wait_count
theme_wait_count
price_wait_count
dry_run_intent_allowed_count
top_ready_candidates
top_wait_reasons
top_block_reasons
warnings
```

금지:

- LIVE order command 생성
- Gateway `send_order` command 생성
- hybrid gate/final grade 연결
- promotion/threshold 자동 변경
- RISK_OFF 후보 삭제
- condition include만으로 `OBSERVE_READY` 생성
- TR 가격만으로 price timing ready 생성

`ThemeBoardThemeSnapshot`은 테마 단위 압축 결과다.

```text
theme_id
theme_name
theme_rank
theme_status
theme_score
active_candidate_count
watching_candidate_count
data_wait_count
alive_count
strong_count
leader_count
alive_ratio
strong_ratio
leader_ratio
breadth_ratio
weighted_return_pct
theme_turnover_krw
leader_concentration
leader_symbol
leader_name
co_leader_symbols
opening_burst_score
condition_boost_count
realtime_valid_count
realtime_valid_ratio
hydration_coverage_ratio
data_quality_flags
reason_codes
```

`ThemeBoardStockSnapshot`은 종목 단위 관찰 결과다.

```text
code
name
theme_id
theme_name
stock_role
stock_score
source_types
source_score
opening_burst_score
condition_boost
current_price
change_rate_pct
turnover_krw
execution_strength
momentum_1m
momentum_3m
momentum_5m
vwap
pullback_from_high_pct
spread_ticks
vi_active
upper_limit_gap_pct
entry_usable=false
data_quality_flags
reason_codes
```

### 상태와 역할 분류

Theme status:

- `DATA_WAIT`: active 후보가 없거나 realtime coverage가 낮음. 데이터 부족을 `WEAK_THEME`로 강등하지 않는다.
- `LEADING_THEME`: 점수, strong count, leader count, breadth가 충분하고 단일 leader 집중이 과도하지 않음.
- `SPREADING_THEME`: leader가 있고 여러 종목 동조가 확인되지만 `LEADING_THEME` 기준에는 아직 미달.
- `LEADER_ONLY_THEME`: 대장주는 강하지만 breadth가 낮거나 leader concentration이 높음. 단일 급등주는 이 상태로 남긴다.
- `WATCH_THEME`: 관심은 있으나 확산/강도가 부족한 관찰 상태.
- `WEAK_THEME`: 데이터는 충분하지만 점수와 후속 흐름이 낮음.

Stock role:

- `LEADER`: 상승률 최고가 아니라 leader score 1위. 거래대금, 체결강도, 모멘텀, source score를 함께 본다.
- `CO_LEADER`: leader score 2위 또는 공동대장 조건 충족.
- `FOLLOWER`: 테마 동조는 있으나 대장주 대비 약함.
- `LATE_LAGGARD`: 테마 확산 후 늦게 따라오며 거래대금/모멘텀이 약함.
- `OVERHEATED`: VI active, 상한가 근접, VWAP 과열, 고가 근접 과열. 항상 `entry_usable=false`.
- `WEAK_MEMBER`: 나머지 약한 구성원.

### 데이터 사용 원칙

- 조건검색 source는 `condition_boost` 또는 discovery source로만 반영한다.
- Opening Burst selected 후보는 `opening_burst_score`와 source type으로 반영한다.
- CandidateHydrator metadata는 이름, 전일가, 현재가, 거래대금, data quality 보강에만 쓴다.
- TR-only 가격은 ThemeBoard에 포함할 수 있지만 `TR_BACKFILL_PRICE_ONLY` flag와 `entry_usable=false`를 남긴다.
- REMOVED/EXPIRED 후보는 계산에서 제외한다.
- Candidate metadata에는 `theme_board_*` 필드만 병합하고 state는 READY 계열로 바꾸지 않는다.

Candidate metadata 병합 필드:

```text
theme_board_theme_id
theme_board_theme_name
theme_board_theme_rank
theme_board_theme_status
theme_board_theme_score
theme_board_stock_role
theme_board_stock_score
theme_board_reason_codes
entry_usable=false
updated_by_theme_board_at
```

### 저장과 Dashboard

SQLite 저장 테이블:

- `theme_board_snapshots`
- `theme_board_theme_snapshots`
- `theme_board_stock_snapshots`

Dashboard `theme_board` section:

```text
calculated_at
top_themes
theme_status_counts
top_leaders
data_wait_count
weak_theme_count
leader_only_count
source_counts
warnings
ready_allowed=false
order_intent_allowed=false
```

대시보드 표현은 “주도 테마”, “관찰 종목”, “대장주/공동대장”, “후발/과열 제외”로 제한한다. “매수 추천”, “매수 확정”, “자동매수 대상”은 금지한다.
## PR 6: ExitEngine + PositionRiskManager Reboot V2

PR 6은 진입 이후의 관찰 포지션을 추적하고, 실시간 가격/캔들/ThemeBoard/MarketRegime을 기반으로 매도 판단과 포지션 리스크 요약을 산출한다.

범위는 observe decision과 DRY_RUN sell intent 저장까지다. LIVE 주문, Gateway `send_order`, `cancel_order`, `modify_order` command는 생성하지 않는다. LIVE_SIM 실제 매도 주문과 broker reconciliation은 PR 7 OrderManager 범위다.

### Runtime 순서

```text
gateway events / realtime tick
-> candidate ingestion / hydration
-> Opening Burst
-> ThemeBoard
-> MarketRegime
-> EntryEngine
-> PositionRuntimeService
-> ExitEngine
-> PositionRiskManager
-> dashboard snapshot
```

기본 feature flag:

```text
TRADING_EXIT_ENGINE_ENABLED=false
TRADING_EXIT_ENGINE_OBSERVE_ONLY=true
TRADING_EXIT_ENGINE_INTERVAL_SEC=5
TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS=false
TRADING_POSITION_RISK_ENABLED=false
TRADING_POSITION_RISK_INTERVAL_SEC=5
```

### PositionRuntimeSnapshot

PositionRuntimeService는 기존 `virtual_positions`와 `live_sim_positions`의 open position을 읽어 Reboot V2 runtime snapshot으로 정규화한다.

중복 방지 기준:

- 같은 `candidate_id + code`의 open position은 한 번만 snapshot으로 생성한다.
- `source_type`은 `DRY_RUN`, `VIRTUAL`, `LIVE_SIM_OBSERVED`, `MANUAL` 중 하나로 명시한다.
- 실제 broker 잔고 reconcile은 하지 않는다.

주요 필드:

```text
trade_date
calculated_at
position_id
candidate_id
code
name
theme_id
theme_name
source_type
entry_price
quantity
remaining_quantity
avg_entry_price
opened_at
holding_minutes
current_price
current_return_pct
max_return_pct
max_drawdown_pct
highest_price_since_entry
lowest_price_since_entry
realized_return_pct
unrealized_return_pct
stop_loss_price
take_profit_price
trailing_stop_price
trailing_active
first_profit_taken
last_tick_at
data_quality_flags
risk_status
details
```

`risk_status`:

```text
OPEN
SCALE_OUT_PENDING
EXIT_PENDING
CLOSED
DATA_WAIT
STALE_DATA_RISK
```

데이터 부족 정책:

- latest tick 없음: `DATA_WAIT`
- tick age 초과: `STALE_DATA_RISK`
- current_price <= 0: `DATA_WAIT`
- candle 부족: support/VWAP 기반 exit는 보류한다.
- stale/invalid price로는 DRY_RUN sell intent를 생성하지 않는다.

### ExitDecision

`ExitDecisionStatus`:

```text
HOLD
SCALE_OUT
EXIT_NOW
WAIT_CONFIRMATION
DATA_WAIT
ALREADY_CLOSED
```

`ExitReason`:

```text
TAKE_PROFIT
STOP_LOSS
STOP_LOSS_FAST
SUPPORT_LOSS
VWAP_LOSS
TRAILING_STOP
TIME_EXIT
THEME_WEAK_EXIT
LEADER_COLLAPSE_EXIT
MARKET_WEAK_EXIT
MARKET_RISK_OFF_EXIT
BREADTH_COLLAPSE_EXIT
STALE_DATA_EXIT_GUARD
MANUAL_PROTECT
```

판단 우선순위:

1. Data integrity guard: stale/invalid price면 sell intent 금지
2. `STOP_LOSS` / `STOP_LOSS_FAST`
3. `MARKET_RISK_OFF_EXIT`
4. `SUPPORT_LOSS` / `VWAP_LOSS`
5. `TRAILING_STOP`
6. `THEME_WEAK_EXIT` / `LEADER_COLLAPSE_EXIT`
7. `TAKE_PROFIT`
8. `TIME_EXIT`
9. `HOLD`

stop과 take-profit이 같은 candle에서 동시에 가능하면 보수적으로 stop을 우선하고 `details.ambiguous_bar=true`를 남긴다.

### Exit Config

```text
TRADING_EXIT_STOP_LOSS_PCT=-2.0
TRADING_EXIT_FAST_STOP_LOSS_PCT=-1.2
TRADING_EXIT_FAST_STOP_LOSS_MINUTES=5
TRADING_EXIT_SUPPORT_BREAK_CONFIRM_CANDLES=2
TRADING_EXIT_VWAP_BREAK_CONFIRM_CANDLES=2
TRADING_EXIT_TAKE_PROFIT_1_PCT=5.0
TRADING_EXIT_TAKE_PROFIT_1_RATIO=0.5
TRADING_EXIT_TRAILING_ACTIVATE_PCT=3.0
TRADING_EXIT_TRAILING_GAP_PCT=1.2
TRADING_EXIT_MAX_HOLD_MINUTES=30
TRADING_EXIT_MIN_RETURN_AFTER_HOLD_PCT=0.0
TRADING_EXIT_FORCE_EXIT_BEFORE_CLOSE_MIN=10
TRADING_THEME_WEAK_CONFIRMATION_CYCLES=2
TRADING_LEADER_COLLAPSE_CONFIRMATION_CYCLES=1
```

### DRY_RUN Sell Intent

기본값은 disabled다.

```text
TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS=false
```

생성 조건:

- `ExitDecisionStatus`가 `SCALE_OUT` 또는 `EXIT_NOW`
- position source가 `DRY_RUN`, `VIRTUAL`, `LIVE_SIM_OBSERVED`
- `remaining_quantity > 0`
- data quality가 sell intent 가능한 상태
- 같은 idempotency key가 없음
- LIVE order가 아님

idempotency key:

```text
reboot_exit_dry_run:{trade_date}:{position_id}:{exit_reason}:{exit_bucket}
```

intent 필드:

```text
trade_date
position_id
candidate_id
code
side=sell
quantity
price_hint
exit_reason
exit_status
hoga_hint
idempotency_key
source=exit_engine_reboot_v2
live_order_allowed=false
gateway_command_created=false
details
```

### PositionRiskManager

PositionRiskManager는 포지션 단위와 포트폴리오 단위 위험을 요약한다.

포지션 단위:

```text
stop_loss_distance_pct
take_profit_distance_pct
trailing_distance_pct
theme_risk_level
market_risk_level
data_risk_level
risk_level
reason_codes
```

포트폴리오 단위:

```text
open_position_count
total_exposure
theme_exposure_by_theme
market_side_exposure
unrealized_pnl_pct
max_drawdown_pct
daily_realized_pnl_pct
risk_level
stop_new_entry_recommended
kill_switch_recommended
```

`risk_level`:

```text
NORMAL
CAUTION
REDUCE
STOP_NEW_ENTRY
KILL_SWITCH_RECOMMENDED
```

이번 PR의 kill switch는 recommendation만 저장한다. 실제 OrderManager 차단 강제 연결은 PR 7에서 처리한다.

### SQLite 저장소

추가 테이블:

```text
position_runtime_snapshots
exit_decisions_reboot
dry_run_sell_intents
position_risk_snapshots
portfolio_risk_snapshots
```

저장 기준:

- `position_runtime_snapshots`: `trade_date + calculated_at + position_id`
- `exit_decisions_reboot`: `trade_date + calculated_at + position_id`
- `dry_run_sell_intents`: `idempotency_key`
- `position_risk_snapshots`: `trade_date + calculated_at + position_id`
- `portfolio_risk_snapshots`: `trade_date + calculated_at`

### Dashboard

`exit_engine` section:

```text
calculated_at
open_position_count
hold_count
scale_out_count
exit_now_count
wait_confirmation_count
data_wait_count
dry_run_sell_intent_count
top_exit_reasons
warnings
live_order_allowed=false
```

`position_risk` section:

```text
portfolio_risk_level
open_position_count
theme_exposure
market_side_exposure
unrealized_pnl_pct
daily_realized_pnl_pct
stop_new_entry_recommended
kill_switch_recommended
top_position_risks
live_order_allowed=false
```

대시보드 표현은 "관찰상 매도 판단", "포지션 리스크 축소 필요", "손절/익절/시간청산 관찰", "테마 약화", "시장 위험"으로 제한한다. "실제 매도 주문 완료", "자동매매 확정", "LIVE 매도" 표현은 금지한다.

### 금지 사항

- LIVE order command 생성 금지
- Gateway `send_order` command 생성 금지
- `cancel_order`/`modify_order` command 생성 금지
- hybrid gate/final grade 연결 금지
- promotion 로직 연결 금지
- threshold 자동 변경 금지
- PostgreSQL 연동 금지
- stale/invalid price로 sell intent 생성 금지
- open position 없는 sell intent 생성 금지
- DRY_RUN sell intent를 실제 주문 queue로 넣는 행위 금지
