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
| `OPEN_ORDERS` | `opt10075` | `KOA_STUDIO_SCREENSHOT / HOLD` |
| `ACCOUNT_POSITIONS` | `opw00018` | `KOA_STUDIO_SCREENSHOT / HOLD` |
| `ACCOUNT_CASH` | `opw00001` | `KOA_STUDIO_SCREENSHOT / HOLD` |

KOA Studio screenshot으로 `opt10075`, `opw00018`, `opw00001`의 TR 입력 계약과 `opw00001` expanded output field list를 확인했지만, 실제 모의서버 capture fixture로 parser를 검증하기 전에는 PASS로 취급하지 않는다. `opw00018`과 `opw00001`은 KOA sample 기준으로 `비밀번호`를 공백으로 입력하고 `비밀번호입력매체구분=00`을 유지한다.

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
- `KOA_STUDIO_SCREENSHOT / HOLD`: KOA Studio field contract는 확인했지만 actual simulation payload fixture가 아직 없다.
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
