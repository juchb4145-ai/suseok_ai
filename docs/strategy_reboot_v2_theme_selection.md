# Strategy Reboot V2 Theme Selection

## RT-TLS 원칙

RT-TLS는 `Real-Time Theme Leadership Selection`의 약자다. 기존 설계에서 전제했던 키움 조건검색 3개, 예를 들어 `ALIVE`, `STRONG`, `LEADER` 또는 생존/강세/주도 조건식은 더 이상 핵심 의사결정 축이 아니다.

새 기준은 다음과 같다.

- 조건검색은 선택적 보조 센서다.
- Core는 조건식 profile이 0개여도 테마 membership과 실시간 snapshot만으로 주도 테마와 대장주 후보를 계산해야 한다.
- 조건검색 include 이벤트는 `condition_boost`, `discovery_source`, Universe 보강에만 사용한다.
- 조건검색 include 이벤트는 `READY`, `READY_SMALL`, 주문 intent, Gateway order command로 직접 이어질 수 없다.
- RT-TLS 산출물은 이번 범위에서 `OBSERVE` 전용이다.
- 기존 hybrid gate, final grade, promotion, threshold A/B와 신규 RT-TLS 결과를 연결하지 않는다.
- 기존 조건식 adapter는 deprecated가 아니라 optional booster다.

이번 PR 범위는 문서와 모델/스켈레톤 고정이다. 대규모 주문 로직, 기존 runtime 교체, LIVE 주문 활성화, DB migration 강제 적용은 포함하지 않는다.

## 현재 Repo 기준점

현재 `trading/theme_engine/lab.py`에는 이미 조건식 이름 없이 snapshot 기반 분류가 가능한 구성요소가 있다.

- `ThemeLabConditionClassifier`
  - `current_price`, `prev_close`, `change_rate_pct`, `turnover_krw`만으로 `alive_hit`, `strong_hit`, `leader_hit`을 만든다.
  - 즉, 실제로는 키움 조건식 이름이 없어도 동작 가능한 threshold classifier다.
- `ThemeBreadthEngine`
  - 테마 구성종목과 `StockSnapshot`을 입력으로 받아 `alive_count`, `strong_count`, `leader_count`, ratios, `condition_score`, `theme_turnover`를 계산한다.
  - 현재 이름과 출력은 condition 중심이지만, 입력 구조는 이미 실시간 snapshot 중심으로 이동할 수 있다.
- `ThemeLabRanker`
  - `theme_status`, `condition_score`, `leader_count`, `strong_count`, `theme_turnover` 기준으로 랭킹한다.
  - RT-TLS에서는 이 단순 condition score rank를 `ThemeLeadershipRanker`로 대체한다.

따라서 리팩터링 방향은 기존 `ThemeLab`을 즉시 삭제하는 것이 아니라, `condition hit 압축 모델`에서 `real-time leadership 모델`로 책임을 분리하는 것이다.

## 전체 흐름

```text
ThemeUniverseBuilder
  -> RealtimeSnapshotBuilder
  -> ThemeLeadershipRanker
  -> ThemeStateClassifier
  -> StockRoleClassifier
  -> WatchsetSelector
  -> EntryTimingEngine
```

이번 스켈레톤은 `trading/theme_engine/leadership.py`에 둔다. 이 파일은 주문 판단을 하지 않고, watchset 후보와 랭킹을 `OBSERVE` 산출물로만 만든다.

## 1. ThemeUniverseBuilder

입력 소스:

- 내부 theme membership DB
- 네이버 테마 구성종목
- 키움 TR 기반 테마/업종/종목 보강
- 수동 보강
- 조건검색 include 이벤트

조건검색 include의 역할은 다음으로 제한한다.

- 새 종목 발견: `discovery_source=condition_search`
- 기존 종목 우선순위 소폭 상승: `condition_boost`
- membership evidence 보강
- 실시간 구독 universe 확장 후보

금지:

- include 이벤트만으로 `READY` 생성
- include 이벤트만으로 watchset 최종 편입 보장
- include 이벤트만으로 주문 intent 생성
- 조건식 이름을 `ALIVE`, `STRONG`, `LEADER`로 고정해서 핵심 테마 상태를 결정

## 2. RealtimeSnapshotBuilder

`RealtimeSnapshotBuilder`는 키움 실시간 FID와 Core candle 계산 결과를 종목별 `StockSnapshot` 또는 그 확장 metadata로 유지한다.

필수 필드:

```text
current_price
change_rate
turnover_krw
cum_volume
execution_strength
best_bid
best_ask
spread_ticks
day_high
day_low
open_price
prev_close
momentum_1m
momentum_3m
momentum_5m
vwap
pullback_from_high_pct
```

데이터 품질 원칙:

- TR backfill 가격은 실시간 가격 타이밍 판단의 단독 근거가 될 수 없다.
- quote freshness, FID 누락, minute bar warmup, VWAP warmup은 `data_quality_flags`로 남긴다.
- 데이터 부족은 매수 차단의 확정 사유가 아니라 `DATA_WAIT` 또는 `WATCH_THEME`로 남긴다.

## 3. ThemeLeadershipRanker

테마별 `theme_score` 공식:

```text
theme_score =
  25% turnover_rank_score
  20% breadth_score
  20% weighted_return_score
  15% leader_strength_score
  10% momentum_score
  10% persistence_score
  - concentration_penalty
  - data_quality_penalty
```

컴포넌트 정의:

- `turnover_rank_score`
  - 테마별 실시간 거래대금 합계를 전체 테마 내 rank score로 정규화한다.
  - 조건검색 hit 수는 여기에 포함하지 않는다.
- `breadth_score`
  - 상승 종목 비율과 강세 종목 비율을 합성한다.
  - 한 종목만 급등하는 테마는 높은 breadth를 받을 수 없다.
- `weighted_return_score`
  - membership score와 거래대금 가중치를 반영한 테마 수익률 점수다.
  - 단순 평균 수익률보다 대장주 거래대금과 핵심 구성종목 가중치를 더 반영한다.
- `leader_strength_score`
  - 테마 내 `LEADER`와 `CO_LEADER`의 stock score 최댓값 또는 대표값이다.
  - 대장주가 없으면 상위 stock score로 fallback하지만 reason에 남긴다.
- `momentum_score`
  - 1분, 3분, 5분 momentum을 정규화한다.
- `persistence_score`
  - 직전 rank/history가 있으면 유지력을 반영한다.
  - 초기 스켈레톤에서는 3분/5분 momentum과 양봉 유지 여부로 근사할 수 있다.
- `concentration_penalty`
  - 구성종목 수가 충분한 테마에서 TOP3 거래대금 집중도가 과도할 때 감점한다.
  - 소형 테마는 TOP3 집중도가 항상 높게 나오므로 최소 표본 조건을 둔다.
- `data_quality_penalty`
  - snapshot coverage, stale quote, missing turnover, warmup 상태를 반영한다.
  - 단, 데이터 부족 테마는 `WEAK_THEME`로 확정하지 않는다.

## 4. ThemeStateClassifier

전략 상태는 다음 5개를 기본으로 한다.

- `LEADING_THEME`
  - 거래대금, breadth, weighted return, leader strength가 모두 강한 주도 테마
- `SPREADING_THEME`
  - 강세가 확산 중이고 leader dominance가 과도하지 않은 테마
- `LEADER_ONLY_THEME`
  - 대장주는 강하지만 확산이 약하거나 거래대금 집중도가 높은 테마
- `WATCH_THEME`
  - 관찰 가치는 있지만 신규 진입 판단에는 아직 부족한 테마
- `WEAK_THEME`
  - 실시간 leadership이 붕괴했거나 거래대금/수익률/breadth가 모두 약한 테마

운영상 데이터 준비 상태로 `DATA_WAIT`를 추가 허용한다.

`DATA_WAIT`는 약한 테마가 아니다. 구성종목 snapshot coverage가 부족하거나 valid member가 부족할 때 사용하는 보류 상태다. 검증 기준상 데이터 부족 테마를 `WEAK_THEME`로 확정하면 실패로 본다.

## 5. StockRoleClassifier

테마 내 종목 역할은 6개로 분류한다.

- `LEADER`
  - 테마 내 가장 강한 대장주
- `CO_LEADER`
  - 대장주와 점수 또는 수익률 격차가 작은 공동대장
- `FOLLOWER`
  - 확산 구간에서만 관찰 가능한 후속 강세주
- `LATE_LAGGARD`
  - 테마 leader가 이미 크게 움직인 뒤 늦게 따라오는 후발주
- `WEAK_MEMBER`
  - 테마 membership은 있지만 실시간 leadership이 약한 구성종목
- `OVERHEATED`
  - 고점 추격, VWAP 과열, 당일 고점 밀착 등 진입 리스크가 과한 종목

종목별 `stock_score` 공식:

```text
stock_score =
  25% theme_membership_score
  25% turnover_rank_in_theme
  20% return_rank_in_theme
  15% execution_strength_score
  10% momentum_score
   5% liquidity_score
  + condition_boost
  - late_laggard_penalty
  - overheat_penalty
```

조건검색 include는 `condition_boost`로만 들어간다. 이 boost는 stock priority를 살짝 올릴 수 있지만 `LEADER`, watchset, READY, order intent를 단독으로 만들 수 없다.

## 6. WatchsetSelector

기본 제한:

```text
TOP_THEME_COUNT = 5
MAX_STOCKS_PER_THEME = 3
MAX_TOTAL_WATCHSET = 20~30
```

선정 규칙:

- `LEADING_THEME`, `SPREADING_THEME` 중심으로 watchset을 만든다.
- `LEADER_ONLY_THEME`는 허용하되 `LEADER`, `CO_LEADER`만 편입한다.
- `LEADER`, `CO_LEADER`를 우선 편입한다.
- `FOLLOWER`는 시장국면 `EXPANSION`에서만 허용한다.
- `LATE_LAGGARD`, `WEAK_MEMBER`, `OVERHEATED`는 항상 제외한다.
- 같은 종목이 여러 테마에 속하면 더 높은 theme rank와 stock score를 우선한다.
- watchset 편입은 주문 가능 상태가 아니다. 출력은 `OBSERVE`다.

## 7. EntryTimingEngine

RT-TLS의 watchset은 관심 종목 압축 결과일 뿐이다. 매수 `READY`는 별도 `EntryTimingEngine`에서 가격 위치가 확인될 때만 가능하다.

허용 가능한 가격 위치 예:

- `GOOD_PULLBACK`
- `PULLBACK_RECLAIM`
- `VWAP_RECLAIM`

금지:

- watchset 편입만으로 `READY`
- condition include만으로 `READY`
- theme rank 상승만으로 주문 intent
- `CHASE_HIGH`, `VWAP_OVEREXTENDED`, `FAILED_BREAKOUT`에서 일반 매수 READY

이번 PR에서는 `EntryTimingEngine` 연결을 하지 않는다. RT-TLS는 신규 알고리즘 OBSERVE 산출물로만 저장한다.

## 신규 모델

`ThemeLeadershipSnapshot`

- 테마 단위 scoring 결과
- `theme_score`, 상태, component score, penalties, data quality, leader/co-leader, 제외 count를 포함한다.

`ThemeLeadershipRank`

- dashboard와 downstream observe 저장용 랭킹 projection
- `snapshot` 원본을 포함하되 주문 판단 필드는 없다.

`StockLeadershipSnapshot`

- 테마 내 종목 단위 scoring 결과
- role, stock score, component score, condition boost, discovery source, data quality를 포함한다.
- `ready_allowed=False`, `order_intent_allowed=False`가 기본이다.

`WatchsetSelectionResult`

- 최종 watchset OBSERVE 결과
- selected/excluded, 제외된 후발주 수, 과열 제외 수, 조건검색 보조 유입 수를 포함한다.
- `ready_allowed=False`, `order_intent_allowed=False`가 기본이다.

## 기존 구조 리팩터링 방향

`ThemeLabConditionClassifier`

- 즉시 제거하지 않는다.
- 조건식 이름 없이 snapshot threshold hit를 만들 수 있는 호환 도구로 남긴다.
- RT-TLS의 핵심 theme score 계산 주체는 아니다.

`ThemeBreadthEngine`

- 기존 ThemeLab dashboard와 테스트 호환을 위해 유지한다.
- RT-TLS에서는 breadth를 더 넓은 component score 중 하나로 흡수한다.

`ThemeLabRanker`

- 기존 condition score ranker로 유지한다.
- RT-TLS 경로에서는 `ThemeLeadershipRanker`를 사용한다.

기존 조건식 adapter

- deprecated가 아니다.
- `ConditionBoost` 또는 `discovery_source=condition_search`를 만드는 optional booster다.
- adapter 출력이 `READY` 또는 주문 intent로 직접 변환되면 실패다.

## Dashboard 방향

대시보드는 다음 정보를 우선 표시한다.

- 주도 테마 TOP5
- 테마 상태: `LEADING_THEME`, `SPREADING_THEME`, `LEADER_ONLY_THEME`, `WATCH_THEME`, `DATA_WAIT`
- 테마별 대장주와 공동대장
- 테마별 `theme_score`와 주요 component score
- 제외된 `LATE_LAGGARD` 수
- 제외된 `OVERHEATED` 수
- 조건검색 보조 유입 수: `condition_boost_count`
- 데이터 부족 테마 수와 `DATA_WAIT` reason

표시하지 않거나 debug/legacy로 밀 항목:

- legacy hybrid final grade를 RT-TLS 핵심 판단처럼 노출
- 조건식 raw hit를 주도 테마의 주 근거처럼 노출
- READY/order intent처럼 보이는 버튼 또는 상태

## 검증 기준 매핑

1. 조건식 profile이 0개여도 theme rank 계산
   - `ThemeLeadershipRanker.rank(..., condition_boosts=None)`이 theme rank를 반환해야 한다.

2. 조건검색 include 이벤트는 booster/discovery로만 기록
   - `StockLeadershipSnapshot.condition_boost`
   - `StockLeadershipSnapshot.discovery_sources`
   - `ThemeLeadershipSnapshot.condition_boost_count`

3. 조건검색 include만으로 READY 또는 order intent 생성 금지
   - RT-TLS 모델의 기본값은 `ready_allowed=False`, `order_intent_allowed=False`다.
   - 이 값을 true로 만드는 코드는 이번 범위에 없다.

4. `LEADER_ONLY_THEME` watchset 제한
   - `LEADER`, `CO_LEADER` 외 role은 제외한다.

5. `LATE_LAGGARD`, `OVERHEATED` 제외
   - `WatchsetSelector`가 항상 제외한다.

6. 데이터 부족 테마 처리
   - snapshot coverage 부족 또는 valid member 부족은 `DATA_WAIT`다.
   - 이를 `WEAK_THEME`로 확정하지 않는다.

7. 대시보드 문서화
   - TOP5, 대장주/공동대장, 후발주 제외 수, 조건검색 보조 유입 수를 표시한다.

## 저장 정책

RT-TLS 결과는 신규 알고리즘 OBSERVE 산출물이다.

- 저장 대상: rank, theme snapshot, stock leadership snapshot, watchset selection result
- 저장 목적: dashboard, replay, calibration, 사후 리뷰
- 금지 연결: legacy hybrid/final grade, live order path, dry-run order path 자동 승격

다음 PR에서 저장소를 연결할 경우에도 `observe_only` 또는 `rt_tls_observe` namespace를 명확히 둔다.

