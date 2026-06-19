# Kiwoom Reconcile TR Pilot Runbook

이 runbook은 Kiwoom OpenAPI+ read-only TR로 broker truth snapshot을 수집하고 local/Chejan projection과 비교하는 절차다. 이 절차는 주문 생성 절차가 아니다.

## Safety Gates

실행 전 다음 값이 유지되어야 한다.

| flag | required |
| --- | --- |
| `TRADING_SEND_ORDER_ALLOWED` | `false` |
| `TRADING_ORDER_MANAGER_ENABLED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| `TRADING_RECONCILE_TR_DISPATCH_ENABLED` | pilot 전까지 `false` |
| `TRADING_RECONCILE_TR_SIMULATION_ONLY` | `true` |

REAL broker에서 pilot을 실행하지 않는다. account password는 Core command, Event Log, DB, report에 저장하지 않는다.

## Credential Policy

Core command에는 다음만 포함한다.

- `account`
- `account_token`
- `credential_ref`
- `reconcile_run_id`
- `logical_source`

Gateway는 TR 실행 직전에만 `credential_ref`를 local provider로 해석한다. credential이 없으면 `CREDENTIAL_UNAVAILABLE`로 source를 실패 처리하고 기존 projection을 삭제하지 않는다.

## Logical Sources

현재 registry는 다음 source를 가진다.

| logical source | TR code | status |
| --- | --- | --- |
| `OPEN_ORDERS` | `opt10075` | `KIWOOM_SIM_RECONCILE_PASS / LIVE_PROMOTION_HOLD` |
| `ACCOUNT_POSITIONS` | `opw00018` | `KIWOOM_SIM_RECONCILE_PASS / LIVE_PROMOTION_HOLD` |
| `ACCOUNT_CASH` | `opw00001` | `KIWOOM_SIM_RECONCILE_PASS / LIVE_PROMOTION_HOLD` |

KOA Studio screenshot으로 `opt10075`, `opw00018`, `opw00001`의 TR 입력 계약과 `opw00001` expanded output field list를 확인했다. 2026-06-19에는 실제 모의서버에서 세 TR의 read-only capture/parser smoke와 자동 `broker_reconcile` startup pipeline을 모두 실행했다. PR11 read-only reconcile pilot 범위는 `PASS`로 판정한다. 다만 LIVE_SIM promotion은 PR10 Chejan `order_rejected` 실제 fixture 미확보와 장중 반복 샘플 부족 때문에 별도 `HOLD`로 유지한다. `opw00018`과 `opw00001`은 KOA sample 기준으로 `비밀번호`를 공백으로 입력하고 `비밀번호입력매체구분=00`을 유지한다.

## Current Verdict

PR11 판정:

- `scope`: Kiwoom simulation read-only reconcile pilot
- `status`: `PASS`
- `evidence`: KOA Studio field contract, actual simulation TR payload, startup broker reconcile staging/finalize
- `live_promotion_status`: `HOLD`
- `live_promotion_blockers`: PR10 `order_rejected` actual Chejan fixture 미확보, 장중 반복/장시간 sample 부족

이 `PASS`는 read-only broker truth snapshot과 reconcile pipeline 검증에 한정된다. 이 문서는 LIVE_SIM/LIVE_REAL 주문 활성화 승인 문서가 아니다.

## 2026-06-19 Actual Simulation Capture

보존된 산출물:

- `reports/reconcile_tr/pr11_tr_20260619_135842_raw.json`
- `reports/reconcile_tr/pr11_tr_20260619_135842_parsed_summary.json`

관측 결과:

- `opt10075`: 미체결 1건 관측. `005930` 삼성전자, 주문번호 `0120039`, 매수 1주, 주문가 347500, 미체결 1주.
- `opw00018`: 보유 1건 관측. `005930` 삼성전자, 보유수량 4, 주문가능수량 4, 평균단가 355000.
- `opw00001`: 예수금/주문가능금액 관측. 예수금 46614997, 주문가능금액 44841317.

해석:

- 실제 Kiwoom 모의서버 payload 기준 field alias와 parser smoke는 통과했다.
- 이 실행 중 Core/Gateway의 order-like command count는 0이어야 한다.
- 현재 결과는 manual capture/parse 검증이다. 자동 `broker_reconcile_runs`, staging tables, discrepancy comparator까지 통과한 것으로 기록하지 않는다.
- 다음 PR11 테스트는 Core/Gateway를 재시작해 최신 reconcile routing/security fix를 로드한 뒤, `broker_reconcile` service를 OBSERVE/read-only 조건에서 활성화하여 staging pipeline까지 확인한다.

## 2026-06-19 Startup Pilot Result

통합 OBSERVE 스크립트를 `-EnableReconcileTrPilot -EnableReconcileTrStartup -ReconcileTrIncludeCash`로 재시작해 자동 startup reconcile을 실행했다.

- run id: `reconcile_ae0e75b47da8421fa3e3069d30292498`
- trigger: `STARTUP`
- broker env: `SIMULATION`
- required sources: `OPEN_ORDERS`, `ACCOUNT_POSITIONS`, `ACCOUNT_CASH`
- completed sources: `OPEN_ORDERS`, `ACCOUNT_POSITIONS`, `ACCOUNT_CASH`
- status: `CLEAN`
- broker truth ready: `true`
- discrepancy count: `0`
- order-like command 생성: `0`

source 결과:

- `opt10075` / `OPEN_ORDERS`: `VALID_EMPTY`, complete, row_count 0.
- `opw00018` / `ACCOUNT_POSITIONS`: `VALID_EMPTY`, complete, row_count 0.
- `opw00001` / `ACCOUNT_CASH`: `PASS`, complete, 예수금 46614997, 주문가능금액 46591135, 출금가능금액 46209237.

이 결과는 실제 Kiwoom 모의서버 read-only TR이 Gateway command queue, Gateway capture, Core event routing, staging table, run finalize, Dashboard/API status까지 이어졌다는 증거다. 단, 이는 해당 시점의 snapshot이므로 장중 보유/미체결 상태가 바뀌면 manual capture 결과와 다를 수 있다.

반복 검증:

- run id: `reconcile_6cacff3d8f9946dbaaf5d460ae9e7be1`
- status: `CLEAN`
- broker truth ready: `true`
- completed sources: `OPEN_ORDERS`, `ACCOUNT_CASH`, `ACCOUNT_POSITIONS`
- discrepancy count: `0`
- `opt10075`: `PASS`, 미체결 1건. `005930`, 주문번호 `0136535`, 매수 1주, 주문가 353500, 미체결 1주.
- `opw00018`: `PASS`, 보유 1건. `005930`, 보유수량 1, 주문가능수량 1, 평균단가 358500.
- `opw00001`: `PASS`, 예수금 46614997, 주문가능금액 45876655, 출금가능금액 46209237.
- order-like command 생성: `0`

## Pilot Flow

1. Gateway가 모의투자 서버로 로그인됐는지 확인한다.
2. 안전 flag가 모두 false/observe-only인지 확인한다.
3. `TRADING_RECONCILE_TR_DISPATCH_ENABLED=true`는 별도 pilot 때만 켠다.
4. credential provider를 Gateway process local 환경에만 준비한다.
5. `OPEN_ORDERS`, `ACCOUNT_POSITIONS`, 필요 시 `ACCOUNT_CASH`를 read-only TR로 수집한다.
6. `broker_reconcile_source_results`와 staging snapshot을 확인한다.
7. `broker_reconcile_discrepancies`를 확인한다.
8. `order command count = 0`을 확인한다.
9. flag를 원복한다.

통합 OBSERVE 스크립트로 startup pilot을 실행할 때는 다음 옵션을 사용한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\start_reboot_v2_observe_all.ps1 `
  -StopExisting `
  -EnableChejanCapture `
  -EnableReconcileTrPilot `
  -EnableReconcileTrStartup `
  -ReconcileTrIncludeCash
```

이 옵션은 `tr_request` command만 생성한다. `send_order`, `cancel_order`, `modify_order`는 계속 비활성이다. 실행 후 `/api/runtime/reconcile/status`가 `NOT_CONFIGURED`가 아니라 `RUNNING`, `PARTIAL`, `CLEAN`, `RECONCILE_REQUIRED` 계열로 노출되는지 확인한다.

## Valid Empty

`OPEN_ORDERS` valid empty는 다음이 모두 참일 때만 인정한다.

- TR request success
- page capture complete
- parser contract 확인
- row count = 0
- parser status `VALID_EMPTY`

단순히 rows가 비었다는 이유로 미체결 0건으로 publish하지 않는다.

## Interpreting Results

- `CLEAN`: TR snapshot과 local/Chejan projection이 일치한다.
- `RECONCILE_REQUIRED`: 신규 매수 금지 상태를 유지한다.
- `REDUCE_ONLY`: 보유 포지션 불일치가 있어 축소 전용 검토가 필요하다.
- `CREDENTIAL_UNAVAILABLE`: snapshot을 비었다고 취급하지 않는다.
- `KIWOOM_SIM_RECONCILE_PASS / LIVE_PROMOTION_HOLD`: PR11 read-only reconcile은 통과했지만 LIVE_SIM 승격은 별도 검토가 필요하다.
- `KOA_STUDIO_SCREENSHOT / HOLD`: KOA Studio field contract만 있고 actual simulation payload fixture가 아직 없는 상태다.
- `SYNTHETIC_ONLY`: KOA/payload 검증 전이므로 qualification은 HOLD다.

## Never Do

- partial snapshot으로 기존 projection 삭제
- broker-only order 자동 adopt
- broker-only position 자동 adopt
- local-only order 자동 cancel
- STOP_NEW_BUY 자동 해제
- REDUCE_ONLY 자동 해제
- dead letter 자동 삭제
- REAL broker pilot
