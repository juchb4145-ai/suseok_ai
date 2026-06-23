# Reboot V2 시장 컨텍스트 연속성

## 목적

Reboot V2에서는 시장 컨텍스트가 여러 하위 모듈로 전달됩니다.

- 테마 보드: 시장 요약 상태만 사용
- 전략 컨텍스트: 종목별 정책이 포함된 전체 시장 컨텍스트 사용
- 분리장세 상대강도 shadow: 관찰 전용 검증에 사용
- 대시보드: transport 진단 필드 표시

여기서 중요한 기준은 하나입니다.

**현재 거래일의 최신 시장 컨텍스트가 정상적으로 전달되고 있는가?**

이 질문과 **지금 주문 판단에 쓸 만큼 시장 판단 데이터가 준비됐는가?** 는 서로 다릅니다.

예전에는 현재 스냅샷이 전부 `DATA_WAIT`이면, 최신 데이터가 있어도 `UNAVAILABLE`로 취급했습니다. 이번 수정은 이 둘을 분리합니다.

## Transport와 Decision Ready의 차이

`transport_fresh`는 전달 상태입니다.

- 거래일이 오늘인지
- `calculated_at`이 있는지
- schema가 지원되는지
- 장중이면 너무 오래된 스냅샷이 아닌지
- 장마감 스냅샷이면 현재 snapshot으로 인정 가능한지

`decision_ready`는 주문 판단 준비 상태입니다.

예를 들어 현재 시장 컨텍스트가 전부 `DATA_WAIT`이면, 수정 후에는 이렇게 표시됩니다.

- `source=PIPELINE_VIEW`
- `transport_status=AVAILABLE`
- `transport_fresh=true`
- `decision_ready=false`
- `decision_data_status=FULL_DATA_WAIT`

즉, **데이터 전달은 정상**이지만 **주문 판단은 아직 대기**라는 뜻입니다.

이 상태에서는 기존처럼 주문/진입은 막힙니다. 주문 정책이나 threshold를 바꾼 것이 아닙니다.

## Fallback 순서

runtime은 시장 컨텍스트를 아래 순서로 찾습니다.

1. 현재 pipeline의 `MarketContextView`
2. DB fallback
3. 대시보드 summary fallback
4. 명시적인 unavailable view

이번 수정 후에는 **현재 pipeline view가 DATA_WAIT이라는 이유만으로 DB fallback을 타지 않습니다.**

DB fallback은 아래처럼 정말 pipeline view가 문제가 있을 때만 사용합니다.

- pipeline view가 없음
- 스냅샷이 오래됨
- 거래일 불일치
- 지원하지 않는 schema
- market regime pipeline section이 ERROR

## 대시보드에서 봐야 할 필드

`market_context_transport`에서 아래 필드를 보면 됩니다.

- `source`: 정상 장중이면 보통 `PIPELINE_VIEW`
- `transport_status`: 최신 컨텍스트 전달 상태
- `transport_fresh`: 전달 상태가 fresh인지
- `decision_ready`: 주문 판단에 쓸 만큼 준비됐는지
- `decision_data_status`: `READY`, `FULL_DATA_WAIT`, `PARTIAL_DATA_WAIT`, `MARKET_CLOSED`
- `fallback_reason`: fallback을 탔다면 이유
- `pipeline_view_present`: pipeline view가 있었는지
- `current_snapshot_authoritative`: 현재 snapshot을 authoritative로 인정했는지
- `market_context_id`
- `market_context_generation`
- `global_status`, `kospi_status`, `kosdaq_status`

운영 판단 기준은 이렇게 보면 됩니다.

- `transport_fresh=true`이고 `source=PIPELINE_VIEW`이면 전달은 정상입니다.
- `decision_ready=false`이면 주문 판단은 아직 대기입니다.
- `decision_data_status=FULL_DATA_WAIT`이면 전 시장 컨텍스트가 아직 DATA_WAIT인 상태입니다.
- `UNAVAILABLE`이 steady-state에서 반복되면 transport 문제가 남아 있는 것입니다.

## market_context_id / generation

`market_context_id`와 `market_context_generation`은 같은 시장 컨텍스트인지 추적하기 위한 ID입니다.

아래 값들을 기반으로 stable hash를 만듭니다.

- schema version
- trade date
- calculated timestamp
- global/KOSPI/KOSDAQ status
- composite market mode
- candidate policy count

이 값은 다음 경로로 전달됩니다.

- `MarketRegimeSnapshot`
- `MarketContextView`
- `StrategyContextSnapshot.source_timestamps`

덕분에 나중에 대시보드, DB, strategy context가 같은 시장 컨텍스트를 봤는지 비교할 수 있습니다.

## Intraday Discovery Recovery 중복 방지

intraday discovery recovery는 매 cycle마다 도는 작업이 아니어야 합니다.

정상 동작은 아래와 같습니다.

- 첫 eligible cycle: recovery 실행, `recovery_trigger=STARTUP`
- 같은 거래일 + 같은 gateway generation: 실행하지 않음, `recovery_trigger=ALREADY_RECOVERED`
- 거래일 변경: 다시 실행
- gateway generation 변경: 다시 실행

pipeline 내부에서도 같은 command/idempotency/natural key는 한 번만 복구합니다.

반복분은 저장을 다시 시도하지 않고 `duplicate_skipped_count`로 집계합니다.

DB 저장도 중복 저장을 예외로 터뜨리지 않고, 기존 batch로 수렴하게 했습니다.

## 2026-06-23 증거 요약

증거 파일 위치:

`reports/daily_market_validation/2026-06-23/followup/`

주요 파일:

- `unavailable_root_cause_timeline.csv`
- `market_context_continuity_before_after.csv`
- `market_context_continuity_replay_summary.json`
- `market_context_continuity_replay_summary.md`
- `recovery_error_correlation.csv`

2026-06-23 검증 결과는 이렇게 해석했습니다.

- 전체 `UNAVAILABLE`: 34건
- 재기동 직후 warmup 구간: 8건
- steady-state 오분류: 26건
- steady-state 원인: `CURRENT_DATA_WAIT_REJECTED_AS_UNAVAILABLE`

즉, 재시작은 일부 8건만 설명합니다.

나머지 26건은 프로세스가 이미 정상적으로 돌고 있는 상태에서, 현재 `DATA_WAIT` 시장 컨텍스트를 transport unavailable로 잘못 분류한 문제였습니다.

이번 수정 후 기대값:

- steady-state `UNAVAILABLE`: 26건에서 0건
- steady-state DB fallback: 26건에서 0건
- steady-state summary fallback: 26건에서 0건
- 현재 DATA_WAIT 스냅샷: `PIPELINE_VIEW` 유지
- 주문 판단: 여전히 `decision_ready=false`

## 검증 명령

```powershell
python -m py_compile trading/strategy/market_context_view.py trading/strategy/market_regime.py trading/strategy/reboot_v2_runtime.py trading/strategy/strategy_context.py trading/theme_engine/intraday_discovery.py storage/db.py
python -m pytest tests/test_reboot_v2_runtime_cutover.py tests/test_intraday_theme_discovery.py -q
python -m pytest tests/test_market_regime.py tests/test_strategy_context_v3.py -q
python -m pytest tests/test_market_relative_strength_shadow.py tests/test_market_relative_strength_outcomes.py tests/test_postmarket_dashboard_review.py -q
python -m pytest tests/test_setup_router_v3.py tests/test_context_dirty_publisher.py -q
```

현재 follow-up에서 선택 영향권 테스트는 `89 passed in 19.96s`였습니다.

전체 `pytest`는 303초 제한에서 timeout이 나서 전체 통과로 기록하지 않았습니다.
