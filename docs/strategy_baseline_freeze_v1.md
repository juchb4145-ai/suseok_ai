# Strategy Baseline Freeze v1

## 목적

PR-0는 Reboot V2 / Theme Core V3 observe runtime의 기준 전략을 고정한다. 새 전략을 추가하거나 임계값을 조정하지 않고, 현재 설정 snapshot과 hash를 만들어 이후 성과 비교의 기준으로 사용한다.

## 현재 구조 감사 결과

| 항목 | 현재 구현 위치 | 기준선 포함 여부 | 비고 |
|---|---|---:|---|
| SetupRouter config | `trading/strategy/setup_router_v3.py` (`SetupRouterConfig`) | Y | LEADER_FIRST_PULLBACK, VWAP_RECLAIM, BREAKOUT_RETEST 임계값 allowlist |
| EntryEngine config | `trading/strategy/entry_engine.py` (`EntryEngineConfig`) | Y | Strategy Context V3 사용 여부와 legacy fallback 차단 포함 |
| MarketRegime config | `trading/strategy/market_regime.py` (`MarketRegimeConfig`) | Y | 지수/시장 breadth 임계값 포함 |
| Theme Core config | `trading/theme_engine/core_v3_runtime.py` (`ThemeCoreV3RuntimeConfig`) | Y | Theme Core V3 observe 설정 포함 |
| Data freshness config | `trading/strategy/market_data_service.py`, `trading/strategy/setup_data_readiness.py` | Y | tick freshness, readiness, candle readiness는 관련 config와 runtime settings로 포함 |
| Risk config | `trading/strategy/position_risk.py`, `trading/strategy/order_models.py`, `trading/strategy/runtime_settings.py` | Y | position risk, portfolio/order guard, live-sim limit 설정 포함 |
| Exit config | `trading/strategy/exit_engine_reboot.py`, `trading/strategy/runtime_settings.py` | Y | Reboot exit engine 및 exit policy threshold 포함 |

Runtime 시작 경로는 `trading_app/runtime_factory.py`의 `build_reboot_v2_runtime_bundle()`이며, 기본 실행 스크립트는 `tools/start_reboot_v2_observe.ps1`이다. Runtime snapshot은 `trading/strategy/reboot_v2_runtime.py`에서 생성되고 Dashboard V2는 `trading_app/dashboard_v2.py`와 `web/static/dashboard.js`가 표시한다. SQLite schema는 `storage/db.py`의 additive `CREATE TABLE IF NOT EXISTS` 방식이다.

## Champion / Challenger

- Champion: `LEADER_FIRST_PULLBACK`
- Challenger: `VWAP_RECLAIM`, `BREAKOUT_RETEST`
- Champion과 Challenger 모두 `OBSERVE_ONLY`이다.
- Challenger는 Candidate FSM, EntryDecision, opportunity rank, position size, EntryPlan, OrderIntent, Gateway command에 영향을 주지 않는다.

## 설정 Snapshot 대상

Baseline snapshot은 명시 allowlist만 포함한다.

- `SetupRouterConfig`
- `EntryEngineConfig`
- `MarketRegimeConfig`
- `ThemeCoreV3RuntimeConfig`
- `MarketDataServiceConfig`
- `PositionRiskConfig`
- `ExitEngineConfig`
- `OrderManagerConfig`
- `StrategyRuntimeConfig`
- runtime settings 중 data readiness, entry/risk/exit/order guard 관련 항목
- 전략 판단에 영향을 주는 feature flag allowlist

계좌, token, secret, password, DB path, 절대 경로, PID, request id, 사용자 식별 정보는 snapshot과 hash 입력에서 제거한다.

## Config Hash

Snapshot은 민감값 제거 후 key 정렬 canonical JSON으로 직렬화하고 SHA-256으로 `config_hash`를 만든다. 같은 설정이면 key 순서와 무관하게 같은 hash가 나온다.

설정 경로를 찾지 못하면 임의 기본값으로 채우지 않는다. 이 경우 `config_snapshot_completeness=PARTIAL`, `missing_config_paths`, warning code를 기록하고 OBSERVE runtime은 중단하지 않는다.

## Drift 판정

기존 `baseline_id + baseline_version` definition이 있으면 current config hash와 비교한다.

- `CLEAN`: 기존 definition hash와 current hash가 동일
- `DRIFT_DETECTED`: hash가 다르고 diff path가 존재
- `PARTIAL`: snapshot 일부 경로 누락
- `UNKNOWN`: definition/hash를 판정할 수 없음

Drift는 설정을 원상복구하지 않고 runtime을 재시작하지 않는다. Dashboard와 SQLite session audit에만 표시한다.

## Persistence

SQLite에 additive table 두 개를 둔다.

- `strategy_baseline_definitions`: `baseline_id + baseline_version` primary key. 같은 버전은 덮어쓰지 않는 immutable definition이다.
- `strategy_baseline_sessions`: runtime session 단위 audit row. 같은 `session_id`는 upsert되어 중복 생성되지 않는다.

Definition의 idempotency 기준은 `baseline_id + baseline_version`이다. Session의 idempotency 기준은 `baseline_id`, `baseline_version`, trade date, runtime start time, git sha, config hash로 만든 deterministic `session_id`이다.

## Runtime Snapshot / Dashboard

Runtime snapshot은 작은 `strategy_baseline` section을 포함한다.

- `status`, `baseline_id`, `version`
- `champion_setup`, `challenger_count`
- `config_hash_short`, `git_sha_short`
- `drift_status`, `config_snapshot_completeness`
- `order_intent_allowed=false`, `live_order_allowed=false`
- `checked_at`

Dashboard V2는 기존 시스템 상태 영역에 작은 행으로 표시한다. Drift 감지 시 safety banner에 “성과 표본 제외 검토 필요” 메시지를 보여준다. 주문 활성화, 설정 적용, drift 승인 버튼은 없다.

## Legacy 격리

Hybrid gate, final grade, shadow promotion, threshold A/B, legacy theme context fallback은 debug/legacy/analysis 전용이다. Reboot V2 baseline config hash에는 legacy decision 결과를 포함하지 않으며 `legacy_decision_usage_allowed=false`를 유지한다.

## 장전 확인 항목

- `tools/start_reboot_v2_observe.ps1`로 실행했는지 확인
- `STRATEGY_RUNTIME_PROFILE=THEME_CORE_V3`
- `TRADING_STRATEGY_BASELINE_ENABLED=true`
- `TRADING_STRATEGY_BASELINE_ALLOW_MUTATION=false`
- `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=0`
- `TRADING_RUNTIME_ALLOW_LIVE_ORDERS=0`
- Dashboard 기준선 상태가 `FROZEN / CLEAN`인지 확인

## 장중 설정 변경 금지

장중에는 strategy threshold, market gate, theme gate, data quality gate, position sizing, exit policy를 변경하지 않는다. 변경이 감지되면 runtime은 계속 OBSERVE로 동작하지만 해당 실행은 기준선 성과 표본에서 제외 검토한다.

## Rollback

Baseline 기능만 끄려면 다음 flag를 내린다.

```powershell
$env:TRADING_STRATEGY_BASELINE_ENABLED = "false"
```

Legacy runtime rollback/debug는 기존 방식대로 명시 profile을 사용한다.

```powershell
$env:STRATEGY_RUNTIME_PROFILE = "LEGACY"
```

## 이번 PR에서 변경하지 않은 전략 동작

- Setup 임계값
- MarketRegime 임계값
- Theme Core V3 점수/상태 계산
- Strategy Context V3 판단
- EntryEngine 판단
- Candidate FSM 상태 전이
- ExitEngine 판단
- Position sizing / portfolio limit
- OrderIntent / Gateway command 생성 경로
- StrategyChangeProposal 자동 적용 정책

## 다음 PR 연결점

다음 PR인 Trading Day Qualification + Funnel은 이 baseline section의 `drift_status`, `config_snapshot_completeness`, `git_sha`, `config_hash`를 사용해 “오늘 표본이 기준선 성과 표본으로 유효한가”를 판정할 수 있다.
