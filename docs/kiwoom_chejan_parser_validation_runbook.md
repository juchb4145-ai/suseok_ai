# Kiwoom Chejan Parser Validation Runbook

이 문서는 Kiwoom OpenAPI+ `OnReceiveChejanData` payload를 canonical order lifecycle event로 검증하기 위한 절차다. 자동매매 시스템은 검증용 주문을 생성하지 않는다. 모의투자 주문 이벤트는 운영자가 HTS/MTS 등 별도 수동 경로로 발생시킨다.

## Safety Prerequisites

| item | required |
|---|---|
| broker env | `SIMULATION` |
| `TRADING_SEND_ORDER_ALLOWED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| `TRADING_KIWOOM_CHEJAN_RAW_CAPTURE_ENABLED` | operator-controlled |
| `TRADING_KIWOOM_CHEJAN_CAPTURE_SIMULATION_ONLY` | `true` |

REAL 계좌에서는 capture pilot을 실행하지 않는다. 계좌번호 원문, 비밀번호, 사용자 ID, Core/Gateway token은 fixture에 저장하지 않는다.

## Gateway OBSERVE 실행

1. Core와 Gateway를 OBSERVE 모드로 실행한다.
2. heartbeat에서 `broker_env=SIMULATION`인지 확인한다.
3. 주문 관련 flag가 모두 비활성인지 확인한다.
4. capture가 필요할 때만 다음을 켠다.

```powershell
$env:TRADING_KIWOOM_CHEJAN_RAW_CAPTURE_ENABLED="true"
$env:TRADING_KIWOOM_CHEJAN_CAPTURE_SIMULATION_ONLY="true"
$env:TRADING_KIWOOM_CHEJAN_CAPTURE_DIR="reports/kiwoom_chejan"
```

## Manual Simulation Event Collection

운영자가 수동으로 소량 모의투자 이벤트를 만든다.

- 주문접수
- 주문거절
- 부분체결
- 완전체결
- 미체결 취소접수
- 취소완료
- 잔고증가
- 보유수량 0

자동매매 시스템의 `send_order`는 계속 비활성이다.

## 2026-06-19 Actual Simulation Status

현재 모의서버 capture 기준으로 다음 case는 실제 Kiwoom Chejan payload에서 관측했다.

- 주문접수
- 부분체결
- 완전체결
- 미체결 취소접수
- 취소완료
- 잔고증가
- 보유수량 0

잔여 리스크:

- `order_rejected`는 아직 실제 Chejan payload로 관측하지 못했다.
- 보유하지 않은 종목 매도 시도는 주문창/서버 pre-check 단계에서 팝업으로 차단되어 `OnReceiveChejanData` 거절 이벤트가 발생하지 않을 수 있다.
- 과거 `reject_reason=0`을 거절로 오분류했던 row는 실제 거절 fixture로 인정하지 않는다.
- 따라서 PR10 실제 모의서버 parser validation은 `HOLD`이며, `order_rejected`는 `NOT_OBSERVED_IN_SIMULATION` 잔여 리스크로 유지한다.

이 잔여 리스크는 PR11 read-only TR reconcile pilot 진행을 막지는 않는다. 다만 LIVE_SIM canary review 전에는 실제 거절 Chejan fixture를 추가로 확보하거나, 운영자가 `order_rejected` 미관측 리스크를 명시 승인해야 한다.

## Fixture Validation

수집 후 redacted fixture 디렉터리를 만든다.

```text
tests/fixtures/kiwoom_chejan/
  manifest.json
  order_accepted.json
  order_rejected.json
  partial_fill.json
  full_fill.json
  cancel_accepted.json
  cancelled.json
  balance_increase.json
  balance_zero.json
  unknown_gubun.json
```

검증 실행:

```powershell
python tools\kiwoom_chejan_parser_validation.py --fixture-dir tests\fixtures\kiwoom_chejan --output-dir reports\kiwoom_chejan_validation
```

산출물:

- `validation.json`
- `summary.md`
- `field_coverage.json`
- `unknown_fids.json`
- `classification_matrix.json`
- `failures.json`

## PASS / HOLD / FAIL

`PASS`:

- `source=KIWOOM_SIMULATION`
- 필수 주문/체결/잔고 case coverage 충족
- critical required field 누락 없음
- event classification 전부 일치
- duplicate fill single-apply 검증
- account redaction 통과

`HOLD`:

- synthetic fixture만 존재
- 실제 simulation sample 부족
- cancel/reject/partial fill sample 부족
- FID 911/915 의미가 아직 fixture로 확정되지 않음
- unknown FID가 있지만 안전하게 DEGRADED 처리됨

`FAIL`:

- fill 중복 반영
- order accepted/fill 오분류
- balance delta를 full snapshot으로 처리
- account/password/user id 유출
- malformed critical event 정상 처리
- FID 920을 strategy tag로 재사용
- actual order command 생성

## Operational Notes

- `gubun=1`은 단일 종목 balance delta일 수 있으므로 전체 계좌 snapshot으로 취급하지 않는다.
- `FID 920`은 screen number이며 strategy tag가 아니다.
- `command_id`, `idempotency_key`, `managed_order_id`는 Chejan FID에서 복원하지 않는다.
- ambiguous correlation은 강제 matching하지 않고 `RECONCILE_REQUIRED`를 우선한다.
- invalid critical order Chejan은 raw payload를 보존하고 fail-closed로 처리한다.

## Before LIVE_SIM Review

1. PR9 quick/fault baseline 통과.
2. Actual Kiwoom simulation fixture validation `PASS`.
3. Parser validation subsection이 qualification report에 포함됨.
4. Event Log replay에서 duplicate execution apply 0.
5. order command count 0.
6. operator review 전까지 LIVE_SIM flag를 켜지 않음.
7. `order_rejected`가 실제 모의서버에서 미관측이면 `NOT_OBSERVED_IN_SIMULATION` 잔여 리스크를 review 문서에 남김.
