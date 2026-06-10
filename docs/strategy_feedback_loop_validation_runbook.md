# Strategy Feedback Loop Validation Runbook

## 목적

이 문서는 Intraday Decision Event Ledger, Intraday Outcome Labeler, Intraday Shadow Strategy Evaluator, Full Strategy Replay Runner, Strategy Change Proposal Board가 하나의 전략 개선 루프로 안전하게 연결되는지 검증하기 위한 운영 runbook이다.

검증 대상 흐름은 다음과 같다.

1. 장중 판단 이벤트 저장
2. horizon 이후 outcome label 생성
3. shadow policy와 baseline 판단 비교
4. 과거 장 replay와 replay report 생성
5. evidence 기반 change proposal 생성
6. 운영자 승인/보류/폐기 workflow 저장

이 runbook과 검증 스크립트는 전략 로직을 변경하지 않는다. 실제 주문, LIVE/LIVE_SIM 주문 제출, Kiwoom Gateway 명령 생성, runtime config 자동 적용을 수행하지 않는다.

## 검증 순서

1. 안전장치, DB schema, 기본 API health를 검증한다.
2. Intraday Decision Event Ledger를 검증한다.
3. Intraday Outcome Labeler를 검증한다.
4. Shadow Strategy Evaluator를 검증한다.
5. Replay Runner와 replay DB 격리를 검증한다.
6. Change Proposal Board와 guardrail을 검증한다.
7. 장중 OBSERVE/DRY_RUN 대시보드 흐름을 확인한다.
8. 장후 replay/proposal 재검증을 수행한다.
9. DRY_RUN/LIVE_SIM 전환 전 체크리스트를 다시 확인한다.

## 운영 DB 백업 및 검증 DB

운영 DB인 `data/trader.sqlite3`는 장중 runtime이 쓰고 있을 수 있다. 직접 쓰지 말고 검증용 복사본을 사용한다.

```powershell
Copy-Item data/trader.sqlite3 data/trader_validation.sqlite3 -Force
python tools/validate_strategy_feedback_loop.py `
  --db data/trader_validation.sqlite3 `
  --base-url http://127.0.0.1:8000 `
  --trade-date 2026-06-10 `
  --skip-replay `
  --output-dir reports/strategy_validation
```

장중에는 기본적으로 GET API와 read-only DB 검증만 사용한다. replay 실행은 장후에 별도 DB로 수행한다.

Replay DB는 반드시 운영 DB와 분리한다.

- 허용: `data/replay/replay_YYYY-MM-DD_xxx.sqlite3`
- 금지: `data/trader.sqlite3`
- 금지: `data/trader_validation.sqlite3`를 replay output으로 재사용

## 자동 검증 스크립트

기본 명령:

```powershell
python tools/validate_strategy_feedback_loop.py `
  --db data/trader_validation.sqlite3 `
  --base-url http://127.0.0.1:8000 `
  --trade-date 2026-06-10 `
  --output-dir reports/strategy_validation
```

장중 안전 모드:

```powershell
python tools/validate_strategy_feedback_loop.py `
  --db data/trader_validation.sqlite3 `
  --base-url http://127.0.0.1:8000 `
  --trade-date 2026-06-10 `
  --skip-replay
```

API 없이 DB만 검증:

```powershell
python tools/validate_strategy_feedback_loop.py `
  --db data/trader_validation.sqlite3 `
  --trade-date 2026-06-10 `
  --skip-api
```

출력:

- `reports/strategy_validation/{trade_date}_validation.json`
- `reports/strategy_validation/{trade_date}_validation.md`

상태:

- `PASS`: hard fail과 주요 warning 없음
- `WARN`: 누락 컴포넌트, 빈 데이터, API 미접속, report section 부족 등
- `FAIL`: 주문 side effect 위험, replay DB 오염, look-ahead bias, forbidden config patch 통과 등

## API 검증 예시

PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/status
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/readiness
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/decisions/summary?trade_date=2026-06-10"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/outcomes/intraday/summary?trade_date=2026-06-10"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/shadow-strategies/summary?trade_date=2026-06-10"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/replay/summary?trade_date=2026-06-10"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/change-proposals/summary?trade_date=2026-06-10"
```

## Replay-grade tick history 확인

`full_runtime` replay 품질을 높이기 위해 Core는 accepted `price_tick` 이벤트를 별도 background writer로 `gateway_price_ticks`에 저장한다. 이 저장 경로는 주문 생성, runtime gate 판단, Kiwoom command enqueue와 분리된 관찰용 경로다.

기본 설정:

- `TRADING_REPLAY_TICK_HISTORY_ENABLED=1`
- `TRADING_REPLAY_TICK_HISTORY_QUEUE_MAX_SIZE=5000`
- `TRADING_REPLAY_TICK_HISTORY_BATCH_SIZE=200`
- `TRADING_REPLAY_TICK_HISTORY_FLUSH_INTERVAL_SEC=1.0`
- `TRADING_REPLAY_TICK_HISTORY_MIN_INTERVAL_MS=500`

장중 부하가 커지면 `TRADING_REPLAY_TICK_HISTORY_MIN_INTERVAL_MS`를 1000 이상으로 올리거나 `TRADING_REPLAY_TICK_HISTORY_ENABLED=0`으로 끈다. 설정 변경 후에는 Core 서버 재시작이 필요하다.

API 확인:

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status).replay_tick_history
(Invoke-RestMethod http://127.0.0.1:8000/api/gateway/transport/status).replay_tick_history
```

DB 확인:

```sql
SELECT COUNT(*) AS tick_count,
       MIN(timestamp) AS first_tick,
       MAX(timestamp) AS last_tick,
       COUNT(DISTINCT code) AS code_count
FROM gateway_price_ticks
WHERE trade_date = '2026-06-10';

SELECT code, COUNT(*) AS tick_count, MIN(timestamp), MAX(timestamp)
FROM gateway_price_ticks
WHERE trade_date = '2026-06-10'
GROUP BY code
ORDER BY tick_count DESC
LIMIT 20;
```

주의:

- `timestamp`는 replay export의 장중 시간 필터와 맞도록 KST 세션 시간으로 저장된다.
- `received_at`은 Core 수신 시각을 보조로 남긴다.
- raw payload와 metadata의 `account`, `token`, `secret`, `password`, `credential`, `auth` 계열 키는 마스킹된다.
- queue가 가득 차면 tick 저장만 drop되고 runtime 판단은 대기하지 않는다. 이 경우 `dropped_count`, `last_error`를 확인한다.

POST endpoint는 local token이 필요하다. 운영 중에는 검증 목적으로 임의 호출하지 않는다.

```powershell
$headers = @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN }
Invoke-RestMethod -Method Post `
  "http://127.0.0.1:8000/api/runtime/outcomes/intraday/rebuild?trade_date=2026-06-10&persist=true" `
  -Headers $headers
```

Proposal 승인 API도 상태 저장만 해야 하며 실제 config apply를 수행하면 안 된다.

## SQL 검증 예시

중복 decision_id:

```sql
SELECT decision_id, COUNT(*) AS count
FROM strategy_decision_events
GROUP BY decision_id
HAVING COUNT(*) > 1;
```

중복 outcome:

```sql
SELECT decision_id, horizon_sec, COUNT(*) AS count
FROM strategy_decision_outcomes
GROUP BY decision_id, horizon_sec
HAVING COUNT(*) > 1;
```

중복 shadow evaluation:

```sql
SELECT decision_id, policy_id, COUNT(*) AS count
FROM shadow_strategy_evaluations
GROUP BY decision_id, policy_id
HAVING COUNT(*) > 1;
```

Outcome look-ahead bias:

```sql
SELECT outcome_id, decision_id, decision_at, evaluated_at, horizon_sec
FROM strategy_decision_outcomes
WHERE datetime(evaluated_at) < datetime(decision_at, '+' || horizon_sec || ' seconds');
```

Shadow rebuild 전후 side effect 비교:

```sql
SELECT 'runtime_order_intents' AS table_name, COUNT(*) AS count FROM runtime_order_intents
UNION ALL SELECT 'virtual_orders', COUNT(*) FROM virtual_orders
UNION ALL SELECT 'virtual_positions', COUNT(*) FROM virtual_positions
UNION ALL SELECT 'gateway_commands', COUNT(*) FROM gateway_commands;
```

Replay DB 격리:

```sql
SELECT replay_id, replay_db_path
FROM strategy_replay_runs
WHERE replay_db_path LIKE '%trader.sqlite3%';
```

Proposal forbidden patch:

```sql
SELECT proposal_id, recommendation_grade, guardrail_passed, candidate_config_patch_json
FROM strategy_change_proposals
WHERE candidate_config_patch_json LIKE '%live_order_enabled%'
   OR candidate_config_patch_json LIKE '%runtime_allow_live_orders%'
   OR candidate_config_patch_json LIKE '%trading_allow_live%'
   OR candidate_config_patch_json LIKE '%token%'
   OR candidate_config_patch_json LIKE '%secret%'
   OR candidate_config_patch_json LIKE '%password%'
   OR candidate_config_patch_json LIKE '%account%';
```

Proposal 승인 전후 config 미변경:

```sql
SELECT proposal_id, status, baseline_config_hash, candidate_config_hash, candidate_config_patch_json
FROM strategy_change_proposals
WHERE proposal_id = '<proposal_id>';
```

승인 전후 `status`와 `operator_note` 외의 config hash/patch가 바뀌면 실패다.

## Replay 검증 명령

장후 또는 검증 DB에서만 실행한다.

```powershell
python tools/strategy_replay.py export `
  --trade-date 2026-06-10 `
  --start-time 09:00:00 `
  --end-time 10:30:00 `
  --output-dir reports/strategy_replay/bundles

python tools/strategy_replay.py run `
  --bundle reports/strategy_replay/bundles/<bundle_dir> `
  --mode data_only `
  --export-report

python tools/strategy_replay.py run `
  --bundle reports/strategy_replay/bundles/<bundle_dir> `
  --mode decision_led `
  --export-report

python tools/strategy_replay.py run `
  --bundle reports/strategy_replay/bundles/<bundle_dir> `
  --mode full_runtime `
  --export-report
```

검증 기준:

- `data_only`: tick/candle/VWAP/momentum 생성 또는 partial data warning이 명확해야 한다.
- `decision_led`: decision event 기준 outcome/shadow가 재계산되어야 한다.
- `full_runtime`: 실제 주문/gateway command 없이 replay DB에만 결과를 생성해야 한다.
- 동일 bundle, 동일 config, 동일 policy로 반복 실행하면 summary가 동일해야 한다.
- 데이터 부족이면 `PARTIAL_REPLAY` 또는 `DATA_INSUFFICIENT`로 보수적으로 종료해야 한다.

## Dashboard 검증 체크리스트

확인 화면:

- Strategy Funnel
- Why Not Bought
- Intraday Outcomes
- Shadow Policy Ranking
- Replay Run / Replay Funnel
- Change Proposal Board
- Data Quality
- OrderGuard / Runtime Status

확인 질문:

- 후보가 들어왔는데 왜 매수하지 않았는가?
- 그 판단은 5분/10분 뒤 맞았는가?
- `RISK_OFF` 때문에 막힌 종목 중 실제로 오른 종목이 많은가?
- `LATE_CHASE` 차단은 기회손실인가 좋은 방어인가?
- `DATA_INSUFFICIENT` 때문에 좋은 종목을 놓치는가?
- `READY` 후 바로 밀리는 종목이 많은가?
- `HOLD/EXIT`이 수익을 반납시키는가?
- shadow 후보 전략이 baseline보다 좋아 보이는가?
- replay에서도 같은 결론이 반복되는가?
- proposal은 충분한 evidence와 guardrail을 갖는가?

## 합격 기준

- 전체 pytest가 통과한다.
- `live_order_enabled=false` 또는 안전 모드가 유지된다.
- decision event coverage가 정상이고 `decision_id` 중복이 없다.
- outcome label은 horizon 이후에만 생성된다.
- shadow evaluation은 `runtime_order_intents`, `virtual_orders`, `virtual_positions`, `gateway_commands`를 증가시키지 않는다.
- replay는 별도 DB에서만 실행된다.
- replay report에 funnel/outcome/shadow summary가 포함된다.
- proposal은 evidence를 갖고, forbidden patch는 guardrail에 의해 차단된다.
- proposal 승인 후 실제 runtime config는 변경되지 않는다.
- dashboard/API에서 funnel -> outcome -> shadow -> replay -> proposal 흐름을 확인할 수 있다.

## Hard Fail 조건

다음은 validation `overall_status=FAIL`로 처리한다.

- `live_order_enabled=true`인데 검증 모드가 OBSERVE/DRY_RUN 안전 모드가 아님
- shadow 검증 후 `runtime_order_intents`, `virtual_orders`, `virtual_positions`, `gateway_commands` count 증가
- `replay_db_path`가 운영 DB path와 동일
- replay 실행 후 운영 DB 주요 테이블 row count 증가
- proposal approve가 실제 runtime config를 변경
- proposal patch에 `live_order_enabled`, `runtime_allow_live_orders`, `trading_allow_live`, `account`, `token`, `password`, `secret`이 포함되었는데 guardrail이 막지 않음
- outcome label이 horizon 도달 전 생성됨
- API endpoint가 500 에러를 반환
- details/report JSON에 토큰, 계좌, 비밀번호, secret이 저장됨

## 실패 시 조치

`MISSING_COMPONENT`

- 실제 구현명과 migration 적용 여부를 확인한다.
- 중복 테이블을 만들지 말고 기존 구현명을 검증 스크립트에 맞춘다.

`UNSAFE_ORDER_SIDE_EFFECT`

- shadow/replay/proposal 작업을 중단하고 before/after row count를 비교한다.
- Gateway command queue에 주문 명령이 생겼는지 확인한다.

`REPLAY_DB_CONTAMINATION`

- replay output path가 `data/replay/*.sqlite3`인지 확인한다.
- 운영 DB의 replay metadata row가 생겼다면 원인을 추적한다.

`OUTCOME_LOOKAHEAD_BIAS`

- outcome labeler가 decision 시점 이후 horizon 데이터만 쓰는지 확인한다.
- 잘못 생성된 outcome은 rebuild 전에 별도 백업한다.

`PROPOSAL_AUTO_APPLY_RISK`

- proposal approval endpoint와 generator guardrail을 점검한다.
- 이번 PR 범위에서는 config apply가 없어야 한다.

`DATA_QUALITY_LOW`

- tick/candle/VWAP/theme snapshot coverage를 확인한다.
- 낮은 품질의 replay/proposal은 `STRONG_CANDIDATE`가 되면 안 된다.

`API_CONTRACT_BROKEN`

- Core API 로그를 확인한다.
- 데이터가 없어도 summary endpoint는 500 대신 empty summary를 반환해야 한다.

## PR 설명에 포함할 문구

- 이 PR은 전략 로직 변경이 아니라 PR-1~PR-5 검증 자동화와 운영 가이드 추가다.
- 실제 주문, LIVE, LIVE_SIM, Kiwoom Gateway 명령 생성 로직은 변경하지 않는다.
- shadow/replay/proposal은 안전 검증 대상이며 주문 side effect가 없어야 한다.
- validation script는 운영자가 장중/장후에 전략 개선 루프를 검증하기 위한 도구다.
- 검증 실패 조건과 후속 조치가 문서화되어 있다.
