# Strategy Reboot V2 Theme Core V3

## 목적

`Theme Core V3 - Seed-to-Theme Expansion Engine`은 장초반 seed를 테마 단위로 확장해 주도 테마와 실시간 구독 대상을 고르는 OBSERVE 전용 파이프라인이다.

이번 재설계의 핵심은 다음과 같다.

- 조건검색 3개 고정 전제는 폐기한다.
- `opt10032`, 조건검색 include, 보유종목, 수동 관심종목은 모두 seed sensor다.
- Opening Burst는 최종 후보 생성기가 아니라 live seed provider다.
- ThemeBoard는 계산 엔진이 아니라 dashboard/API projection이다.
- Core는 `ThemeUniverse + LiveSeedSignal + realtime snapshot`만으로 theme state와 stock role을 계산해야 한다.
- 주문, READY, DRY_RUN, LIVE_SIM, `send_order`는 이번 PR 범위가 아니다.

## 책임 분리

```text
OpeningTurnoverSeedCollector / condition include / manual watch
  -> LiveSeedSignal
  -> ThemeCohortEngine
  -> ThemeStateMachine
  -> StockRoleEngine
  -> FocusedExpansionPlanner
  -> CandidateBridge
  -> ThemeBoardView
```

### Opening Burst

Opening Burst는 `09:03`, `09:06`, `09:09`, `09:12`, `09:15` rolling `opt10032` seed를 만든다.

역할:

- 거래대금 상위 100개를 회차별로 수집한다.
- union/dedupe 후 Core가 재랭킹할 seed universe를 만든다.
- seed 종목 중 일부만 실시간 등록 대상으로 제안한다.
- `selected_symbols`를 저장할 수 있지만 직접 Candidate를 생성하지 않는다.

금지:

- Opening Burst `selected_symbols -> Candidate`
- Opening Burst `selected_symbols -> READY`
- Opening Burst `selected_symbols -> order intent`

### ThemeBoard

ThemeBoard는 Core 산출물을 보여주는 view다.

표시 방향:

- 주도 테마 TOP5
- 테마별 LEADER / CO_LEADER
- 제외된 LATE_LAGGARD 수
- 제외된 OVERHEATED/VI/상한가근접 수
- 조건검색 보조 유입 수
- DATA_WAIT 이유
- focused realtime expansion 대상 수

ThemeBoard는 `entry_usable=false`, `ready_allowed=false`, `order_intent_allowed=false`를 유지한다.

## ThemeUniverse / Registry

`ThemeRegistry`는 내부 theme membership DB와 수동 입력을 theme 단위 스냅샷으로 정규화한다.

필드:

- `theme_id`
- `theme_name`
- `member_count`
- `tradable_member_count`
- `kospi_member_count`
- `kosdaq_member_count`
- `membership_quality`
- `reason_codes`

조건검색 결과는 universe 보강 근거가 될 수 있지만, theme membership 자체를 대체하지 않는다.

## LiveSeedSignal

`LiveSeedSignal`은 seed sensor의 공통 입력 모델이다.

주요 source:

- `opt10032`
- `condition_include`
- `manual_watch`
- `holding`
- `pending_order`
- `realtime_tick`
- `hydration`

조건검색 include는 다음으로만 남긴다.

- `source_types`
- `reason_codes`
- `condition_boost`
- `discovery_source`

조건검색 include 하나만으로 theme state, READY, 주문 intent를 만들면 실패다.

## ThemeCohortEngine

`ThemeCohortEngine`은 seed signal을 theme membership에 매핑한다.

산출 필드:

- `seed_member_count`
- `realtime_valid_count`
- `strong_count`
- `leader_count`
- `alive_ratio`
- `strong_ratio`
- `leader_ratio`
- `theme_turnover_krw`
- `weighted_return_pct`
- `breadth_ratio`
- `leader_concentration`
- `coverage_ratio`
- `signal_persistence_count`
- `cohesion_passed`
- `leader_only_candidate`
- `data_quality_reason`

동조화 기준:

- 일반 테마는 STRONG/LEADER 후보 3개 이상.
- 소형 테마는 구성 종목 4개 이하이고 강한 동조 종목 2개 이상.
- +7% 단일 BURST 종목은 `leader_only_candidate`일 수 있지만 `SPREADING_THEME`이나 `LEADING_THEME`가 아니다.
- VI, 상한가근접, 과열 종목은 strong/leader 근거에서 제외한다.

데이터가 부족하면 `WEAK_THEME`로 떨어뜨리지 않는다. `DATA_WAIT`, `SEED_WAIT`, `WATCH_THEME`로 남긴다.

## ThemeStateMachine

상태:

- `UNIVERSE_EMPTY`
- `SEED_WAIT`
- `DATA_WAIT`
- `WATCH_THEME`
- `EMERGING_THEME`
- `LEADER_ONLY_THEME`
- `SPREADING_THEME`
- `LEADING_THEME`
- `FADING_THEME`
- `WEAK_THEME`

핵심 규칙:

- `LEADING_THEME`는 최소 2회 cycle persistence가 필요하다.
- 첫 강한 동조는 `SPREADING_THEME`로 시작한다.
- data quality 부족은 `DATA_WAIT`로 남긴다.
- 단일 leader spike는 `LEADER_ONLY_THEME`로 제한한다.
- 직전 `LEADING_THEME`가 점수 급락, leader 이탈, 음수 weighted return을 보이면 `FADING_THEME`로 전이한다.

## StockRoleEngine

역할은 두 단계로 분리한다.

`raw_role`:

- `LEADER`
- `CO_LEADER`
- `FOLLOWER`
- `LATE_LAGGARD`
- `WEAK_MEMBER`
- `OVERHEATED`

`trade_role`:

- `LEADER_CONFIRMED`
- `CO_LEADER_CONFIRMED`
- `LEADER_CANDIDATE_DATA_WAIT`
- `FOLLOWER_ALLOWED`
- `FOLLOWER_BLOCKED_LEADER_ONLY`
- `LATE_LAGGARD_BLOCKED`
- `OVERHEATED_BLOCKED`
- `WEAK_MEMBER_BLOCKED`

운영 규칙:

- 상승률 최고가 아니라 role score와 theme state로 LEADER를 결정한다.
- `LEADER_ONLY_THEME`에서는 LEADER/CO_LEADER만 통과한다.
- FOLLOWER는 시장 국면이 `EXPANSION`이고 theme state가 `SPREADING_THEME` 또는 `LEADING_THEME`일 때만 허용한다.
- LATE_LAGGARD, OVERHEATED는 watchset/bridge 대상에서 제외한다.

## FocusedExpansionPlanner

모든 theme member를 실시간 구독하지 않는다.

허용 상태:

- `EMERGING_THEME`
- `LEADER_ONLY_THEME`
- `SPREADING_THEME`
- `LEADING_THEME`

기본 제한:

- `max_per_theme = 6`
- `max_total = 30`
- KOSDAQ risk가 `WEAK` 또는 `RISK_OFF`이면 KOSDAQ expansion을 줄인다.

Focused expansion은 실시간 등록 대상 선정이다. 매수 READY가 아니다.

## CandidateBridge

`CandidateBridge`는 Core 산출물을 CandidateSourceEvent로 변환하는 유일한 V3 bridge다.

허용 조건:

- theme state가 `SPREADING_THEME`, `LEADING_THEME`, `LEADER_ONLY_THEME` 중 하나.
- trade role이 `LEADER_CONFIRMED`, `CO_LEADER_CONFIRMED`, `FOLLOWER_ALLOWED` 중 하나.

출력 불변식:

- `output_mode = OBSERVE`
- `ready_allowed = false`
- `order_intent_allowed = false`
- raw payload에 `ready_allowed=false`, `order_intent_allowed=false`를 남긴다.

CandidateBridge는 CandidateSourceEvent를 만들 수 있지만 EntryEngine, OrderManager, Gateway `send_order`를 호출하지 않는다.

## 검증 기준

1. 조건식 profile이 0개여도 `ThemeUniverse + LiveSeedSignal + realtime snapshot`으로 theme rank/state가 계산된다.
2. 조건검색 include는 `condition_boost` 또는 `discovery_source`로만 기록된다.
3. 조건검색 include만으로 READY 또는 order intent가 생성되면 실패다.
4. Opening Burst `selected_symbols`만으로 Candidate가 생성되면 실패다.
5. +7% 단일 종목은 주도 테마가 아니다.
6. 같은 테마 내 strong/leader 3개 이상, 또는 소형 테마 2개 이상 동조할 때만 주도 테마 후보가 된다.
7. `LEADING_THEME`는 2 cycle persistence 없이 생성되지 않는다.
8. `LEADER_ONLY_THEME`에서는 FOLLOWER/LATE_LAGGARD가 watchset에 들어가면 실패다.
9. `LATE_LAGGARD`, `OVERHEATED`, VI, 상한가근접은 WAIT/BLOCKED로 남아야 한다.
10. Watchset 편입과 매수 READY는 분리되어야 한다.

## 이번 PR의 코드 범위

추가/변경:

- `trading/theme_engine/universe.py`: `ThemeRegistry`, `ThemeUniverseSnapshot`
- `trading/theme_engine/signals.py`: `LiveSeedSignal`
- `trading/theme_engine/cohort.py`: `ThemeCohortEngine`
- `trading/theme_engine/state_machine.py`: `ThemeStateMachine`
- `trading/theme_engine/roles.py`: `StockRoleEngine`
- `trading/theme_engine/expansion.py`: `FocusedExpansionPlanner`
- `trading/theme_engine/candidate_bridge.py`: `CandidateBridge`
- `trading/theme_engine/board_view.py`: `ThemeBoardView`
- `trading/theme_engine/opening_runtime.py`: Opening Burst direct candidate ingestion 차단

명시적으로 제외:

- EntryEngine 변경
- OrderManager 변경
- Gateway `send_order` 변경
- hybrid/final grade 연결
- LIVE 주문 또는 LIVE_SIM 주문 생성
