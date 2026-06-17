# Strategy Reboot V2 Opening Theme Burst

## 목표

`Opening Theme Burst + RT-TLS`는 장 시작 직후 `09:01~09:15`에 당일 주도 테마와 대장주 후보를 빠르게 압축하기 위한 OBSERVE 전용 알고리즘이다.

핵심 전제:

- 키움 조건검색 3개는 필수가 아니다.
- 조건검색 include는 optional booster이며, `READY` 또는 주문 intent를 만들 수 없다.
- 장초반 seed는 조건검색이 아니라 `opt10032 거래대금 상위` rolling 호출이 담당한다.
- Core는 `opt10032 seed + realtime snapshot + theme membership`만으로 theme rank를 계산할 수 있어야 한다.
- 이번 PR은 설계 문서와 skeleton 범위다. LIVE 주문/매수 주문 로직은 건드리지 않는다.
- 산출물은 `OBSERVE`로만 저장한다.

## 왜 09:05 단발이 아닌 Rolling Seed인가

09:05 1회 거래대금 상위만 보면 다음 문제가 생긴다.

- 09:03에 이미 급등했지만 09:05에 눌린 종목을 놓친다.
- 09:06~09:12 사이에 거래대금이 폭발하는 2차 테마를 놓친다.
- 단일 시점 rank noise가 크다.
- 상위 50개만 쓰면 테마 동조화 확인에 필요한 2~3번째 종목이 잘린다.

따라서 기본 seed schedule은 다음으로 둔다.

```text
09:03
09:06
09:09
09:12
09:15
```

각 호출은 처음에는 상위 100개를 받는다. Core는 다섯 번의 결과를 `union/dedupe`한 뒤 실시간 snapshot으로 재랭킹한다.

기본 정책:

- `TOP_N_PER_CALL = 100`
- `MAX_UNION_SIZE = 300`
- 같은 종목이 여러 번 잡히면 가장 큰 거래대금과 가장 좋은 seed rank를 유지한다.
- `seed_times`에 포착 시각들을 남겨 persistence score에 사용한다.
- ETF, ETN, 우선주, 스팩, 관리종목, 거래정지 종목은 seed 단계에서 제외한다.
- union 결과는 실시간 등록 대상 후보가 된다.

## 전체 흐름

```text
OpeningTurnoverSeedCollector
  -> BurstStockScorer
  -> ThemeCohesionScorer
  -> ThemeLeadershipRanker
  -> StockRoleClassifier
  -> WatchsetSelector
  -> EntryTimingEngine
```

이번 PR의 skeleton은 [trading/theme_engine/opening_burst.py](../trading/theme_engine/opening_burst.py)에 둔다. 이 모듈은 실제 TR 호출을 직접 수행하지 않는다. Gateway/TR runner가 전달한 `opt10032` 결과 row를 받아 순수 Core 계산만 수행한다.

## 1. OpeningTurnoverSeedCollector

책임:

- `opt10032` rolling 호출 결과 수집
- 거래대금 상위 N 종목 union/dedupe
- seed 시각 기록
- ETF/ETN/우선주/스팩/관리종목/거래정지 제외
- 실시간 등록 대상 추출

입력 예:

```text
stock_code
stock_name
rank
turnover_krw
change_rate_pct
collected_at
instrument_type
is_etf / is_etn / is_spac / is_preferred / is_suspended
```

출력:

```text
OpeningTurnoverSeed
  stock_code
  stock_name
  turnover_krw
  change_rate_pct
  seed_rank
  first_seen_at
  last_seen_at
  seed_times
```

## 2. BurstStockScorer

`BurstStockScorer`는 seed 종목을 실시간 snapshot과 결합해 장초반 burst 점수를 계산한다.

`stock_burst_score`:

```text
30% 거래대금 순위 점수
25% 거래대금 속도 점수
20% 등락률 등급 점수
10% 체결강도 점수
10% 1m/3m momentum 점수
 5% 호가/스프레드 안정성
- VI/상한가근접/추격위험 penalty
```

등락률 등급:

```text
ALIVE  = -1% 이상
STRONG = +3% 이상
LEADER = +5% 이상
BURST  = +7% 이상
```

중요한 해석:

- `+7%`는 매수 신호가 아니다.
- `+7%`는 단일 기준이 아니라 `BURST` 등급이다.
- 단일 종목이 `BURST`여도 같은 테마 내 동조화가 없으면 주도 테마로 인정하지 않는다.

거래량 급증 기준:

- 전일 동시간대 대비 거래량 200%는 사전 캐시가 있을 때만 사용한다.
- 기본 점수는 `거래대금 속도`와 `20일 평균 거래대금 대비 초반 거래대금 비율`을 사용한다.
- `prior_same_time_volume_ratio`가 있으면 보조 score로 반영한다.
- 사전 캐시가 없다는 이유만으로 DATA_FAIL 또는 BLOCKED로 가지 않는다.

VI/상한가/추격위험:

- `vi_active=True`는 `BLOCKED` 성격의 timing status로 남긴다.
- `upper_limit_gap_pct <= 3`은 상한가근접으로 본다.
- 고점 밀착, VWAP 과이격은 `WAIT` 또는 `OVERHEATED`로 남긴다.
- 이 상태들은 watchset/READY 분리 원칙상 매수 READY가 아니다.

## 3. ThemeCohesionScorer

테마 동조화는 장초반 알고리즘의 핵심이다. 단순 상승률 상위 종목을 사는 구조가 아니다.

계산 필드:

```text
theme_active_count
strong_count
leader_count
alive_ratio
strong_ratio
leader_ratio
theme_turnover_krw
weighted_return_pct
leader_concentration
data_quality_ratio
```

주도 테마 후보 조건:

- 일반 테마: 같은 테마 내 `STRONG` 또는 `LEADER/BURST` 후보가 3개 이상
- 소형 테마: 구성종목 수가 4개 이하이고 강한 동조 종목이 2개 이상

과열/VI/상한가근접 종목은 감시 대상에는 남길 수 있지만, 테마 동조화의 strong/leader count 근거에서는 제외한다.

단일 종목 `BURST` 처리:

- `+7%` 단일 종목만 있는 테마는 `LEADING_THEME` 또는 `SPREADING_THEME`가 될 수 없다.
- 기본 상태는 `WATCH_THEME`이며 reason은 `SINGLE_BURST_NOT_THEME`로 남긴다.

## 4. ThemeLeadershipRanker

장초반 전용 `theme_score`:

```text
theme_score =
  25% theme_turnover_rank_score
+ 20% strong_ratio_score
+ 20% leader_count_score
+ 15% weighted_return_score
+ 10% momentum_score
+ 10% persistence_score
- leader_only_penalty
- data_quality_penalty
```

컴포넌트:

- `theme_turnover_rank_score`
  - opt10032 seed와 realtime snapshot의 거래대금을 테마 단위로 합산한 rank score
- `strong_ratio_score`
  - 전체 테마 membership 대비 장초반 strong 후보 비율
- `leader_count_score`
  - `LEADER`/`BURST` 후보 수를 점수화
- `weighted_return_score`
  - 거래대금 가중 평균 등락률
- `momentum_score`
  - seed 종목들의 1m/3m momentum
- `persistence_score`
  - 09:03~09:15 rolling seed에 반복 등장한 정도
- `leader_only_penalty`
  - 대장주만 강하고 확산이 약한 구조에 대한 감점
- `data_quality_penalty`
  - seed match 부족, active ratio 부족에 대한 감점

상태:

- `LEADING_THEME`
- `SPREADING_THEME`
- `LEADER_ONLY_THEME`
- `WATCH_THEME`
- `WEAK_THEME`
- `DATA_WAIT`

`LEADING_THEME`와 `SPREADING_THEME`는 반드시 동조화 조건을 통과해야 한다.

## 5. StockRoleClassifier

역할은 상승률 최고가 아니라 `leader_score` 기준으로 결정한다.

`leader_score`:

```text
30% 테마 내 거래대금 순위
20% 테마 내 등락률 순위
15% 거래대금 속도
15% 체결강도
10% 1m/3m/5m 모멘텀
10% 가격 위치 안정성
- 추격위험 penalty
- VI/상한가근접 penalty
```

역할:

- `LEADER`
- `CO_LEADER`
- `FOLLOWER`
- `LATE_LAGGARD`
- `WEAK_MEMBER`
- `OVERHEATED`

운영 원칙:

- 가장 많이 오른 종목이 아니라 leader_score 1위가 `LEADER`다.
- 거래대금과 체결강도가 약한 `+9%` 종목보다 거래대금 속도와 체결강도가 압도적인 `+5%` 종목이 대장이 될 수 있다.
- VI/상한가근접/고점추격 종목은 `OVERHEATED`로 분류하고 watchset/READY 경로에서 제외하거나 WAIT/BLOCKED로 남긴다.

## 6. WatchsetSelector

기본 제한:

```text
TOP_THEME_COUNT = 5
MAX_STOCKS_PER_THEME = 3
MAX_TOTAL_WATCHSET = 20~30
```

선정 규칙:

- `LEADING_THEME`, `SPREADING_THEME` 중심으로 선정
- `LEADER_ONLY_THEME`는 `LEADER`, `CO_LEADER`만 허용
- `LEADER`, `CO_LEADER` 우선
- `FOLLOWER`는 시장국면 `EXPANSION`에서만 허용
- `LATE_LAGGARD`, `WEAK_MEMBER`, `OVERHEATED` 제외

watchset 편입은 관심 종목 압축이다. 매수 READY가 아니다.

## 7. EntryTimingEngine 분리

`Opening Theme Burst + RT-TLS`는 진입 타이밍 엔진이 아니다.

READY 가능 조건은 별도 `EntryTimingEngine`에서 다음 가격 위치가 확인될 때만 검토한다.

- `GOOD_PULLBACK`
- `PULLBACK_RECLAIM`
- `VWAP_RECLAIM`

금지:

- opt10032 seed 편입만으로 READY
- 조건검색 include만으로 READY
- `BURST` 등급만으로 READY
- watchset 편입만으로 주문 intent
- `OVERHEATED`, `VI_ACTIVE`, `UPPER_LIMIT_NEAR`에서 일반 READY

이번 PR skeleton의 모든 출력은 다음 기본값을 가진다.

```text
output_mode = OBSERVE
ready_allowed = False
order_intent_allowed = False
```

## 조건검색 Adapter 위치

조건검색 adapter는 deprecated가 아니다. 단 optional booster다.

허용:

- `condition_boost`
- `discovery_source=condition_search`
- universe 보강
- seed 외부의 관심 종목 발견

금지:

- 조건검색 include -> READY
- 조건검색 include -> order intent
- 조건식 3개 이름을 RT-TLS 핵심 상태로 고정
- 조건검색 결과만으로 주도 테마 인정

## Skeleton 파일

이번 PR의 code skeleton:

- [trading/theme_engine/opening_burst.py](../trading/theme_engine/opening_burst.py)
- [trading/theme_engine/leadership.py](../trading/theme_engine/leadership.py)
- [tests/test_opening_theme_burst_selection.py](../tests/test_opening_theme_burst_selection.py)

`opening_burst.py`의 주요 클래스:

- `OpeningTurnoverSeedCollector`
- `BurstStockScorer`
- `ThemeCohesionScorer`
- `OpeningThemeBurstRanker`
- `OpeningBurstWatchsetSelector`
- `OpeningThemeBurstEngine`

## 검증 기준 매핑

1. 조건식 profile 0개
   - `OpeningThemeBurstEngine.run(..., condition_boosts=None)`이 rank와 watchset OBSERVE 결과를 만든다.

2. `+7%` 단일 종목
   - `BURST` 등급은 부여하지만 `SINGLE_BURST_NOT_THEME`으로 남기고 주도 테마로 인정하지 않는다.

3. 테마 동조화
   - 일반 테마 strong/leader 3개 이상, 소형 테마 2개 이상만 `LEADING_THEME` 또는 `SPREADING_THEME` 후보가 된다.

4. 대장주 기준
   - `LEADER`는 등락률 최고가 아니라 `leader_score` 최고 종목이다.

5. `LEADER_ONLY_THEME`
   - `FOLLOWER`, `LATE_LAGGARD`는 watchset에 들어갈 수 없다.

6. `OVERHEATED`/VI/상한가근접
   - `timing_status=WAIT` 또는 `BLOCKED`로 남긴다.
   - `ready_allowed=False`, `order_intent_allowed=False`다.

7. Watchset과 READY 분리
   - watchset 결과는 `OBSERVE` 산출물이다.
   - 매수 READY는 `EntryTimingEngine`의 별도 가격 위치 확인 이후에만 가능하다.

## Dashboard 방향

대시보드는 다음을 표시한다.

- 09:03/09:06/09:09/09:12/09:15 seed 수집 상태
- seed union/dedupe 종목 수
- 주도 테마 TOP5
- 테마별 `strong_count`, `leader_count`, `theme_turnover_krw`
- 테마별 대장주/공동대장
- `SINGLE_BURST_NOT_THEME` 테마 수
- `LATE_LAGGARD` 제외 수
- `OVERHEATED`/VI/상한가근접 제외 수
- 조건검색 booster 유입 수

READY/주문 버튼처럼 보이는 표현은 넣지 않는다.

