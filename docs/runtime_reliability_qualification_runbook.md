# Runtime Reliability Qualification Runbook

이 문서는 Realtime Architecture V2를 LIVE 주문 활성화 전에 OBSERVE 모드에서 검증하기 위한 운영 절차다. Qualification은 전략 수익성 검증이 아니며, 실제 `send_order`, `cancel_order`, `modify_order` command를 만들면 안 된다.

## Safety Prerequisites

실행 전에 다음 값을 확인한다.

| item | required |
|---|---|
| `TRADING_RELIABILITY_TEST_MODE` | `true` |
| `TRADING_SEND_ORDER_ALLOWED` | `false` |
| `TRADING_ORDER_MANAGER_OBSERVE_ONLY` | `true` |
| `TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND` | `false` |
| `TRADING_ORDER_INTENT_ENABLED` | `false` |
| broker/account mode | not `REAL` |
| DB path | qualification/test/report workspace only |

REAL broker 또는 운영 DB에서는 fault suite를 실행하지 않는다. Guard가 거부하면 설정을 완화하지 말고 DB path, broker mode, 주문 flag를 먼저 확인한다.

## Quick CI

```powershell
$env:TRADING_RELIABILITY_TEST_MODE="true"
$env:TRADING_SEND_ORDER_ALLOWED="false"
$env:TRADING_ORDER_MANAGER_OBSERVE_ONLY="true"
$env:TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND="false"
$env:TRADING_ORDER_INTENT_ENABLED="false"
python tools\runtime_reliability_qualification.py --profile quick-ci --output-dir reports\reliability
```

Quick CI는 deterministic replay와 mandatory fault subset을 짧게 실행한다. 1시간 soak를 실행하지 않으므로 정상 결과는 `HOLD`일 수 있다. 이 상태를 장시간 성능 PASS로 해석하지 않는다.

## Replay

```powershell
python tools\runtime_reliability_qualification.py ^
  --profile replay ^
  --bundle reports\strategy_replay\bundles\bundle-2026-06-18 ^
  --repeat 2 ^
  --output-dir reports\reliability
```

`gateway_events.jsonl` 또는 `gateway_events.json`이 있으면 그대로 사용한다. 없으면 기존 strategy replay bundle의 `ticks.csv`를 `price_tick` GatewayEvent로 변환해 deterministic digest를 비교한다.

통과 기준:

- 같은 입력을 두 번 실행했을 때 digest가 같아야 한다.
- 동적 필드(`created_at`, auto id, worker id, duration)는 비교에서 제외된다.
- Event Log replay 후 critical pending event가 남지 않아야 한다.

## Fault Suite

```powershell
python tools\runtime_reliability_qualification.py --profile fault-suite --output-dir reports\reliability
```

Fault suite는 F01부터 F18까지 deterministic scenario를 순차 실행한다. fault는 production code에 sleep/exception을 직접 심지 않고, qualification 전용 DB와 명시적 scenario context에서만 재현한다.

중요 scenario:

- `F02_DUPLICATE_EXECUTION`: 같은 `execution_id`가 여러 번 들어와도 체결 수량은 한 번만 반영된다.
- `F03_FILL_BEFORE_ACK`: fill 후 늦은 ack가 FILLED/PARTIALLY_FILLED를 ACKED로 되돌리지 않는다.
- `F05_CRASH_AFTER_RECEIPT`: receipt 저장 후 `mark_processed` 전 crash를 replay해도 중복 반영하지 않는다.
- `F08_EVENT_LOG_APPEND_FAILURE`: critical event append 실패 시 `STOP_NEW_BUY`와 `order_lifecycle_ready=false`가 된다.
- `F18_DEAD_LETTER_PRESENT`: critical dead letter가 있으면 lifecycle ready가 false다.

## Observe Soak

```powershell
python tools\runtime_reliability_qualification.py ^
  --profile observe-soak ^
  --duration-sec 3600 ^
  --core-url http://127.0.0.1:8000 ^
  --output-dir reports\reliability
```

기본 1시간 이상을 권장한다. 짧은 duration은 `SAMPLE_INSUFFICIENT` 또는 `HOLD`로 남긴다.

수집 지표:

- runtime cycle p50/p95/p99/max
- dirty evaluator duration
- event consumer/replay duration
- Event Log pending/retry/dead-letter/oldest age
- Dashboard read/build duration and read model age
- process RSS, thread count, SQLite/WAL size

기존 `tools/websocket_real_pilot_soak.py`는 transport subsection으로 참고한다. WebSocket transport PASS는 전체 runtime PASS를 의미하지 않는다.

## Full Qualification

```powershell
python tools\runtime_reliability_qualification.py ^
  --profile full ^
  --duration-sec 14400 ^
  --seed 20260618 ^
  --output-dir reports\reliability
```

Full profile은 replay, fault suite, observe soak를 결합한다. 4시간을 실행하지 않았다면 report는 그 사실을 그대로 기록해야 한다.

## Report

산출물은 다음 위치에 생성된다.

```text
reports/reliability/{run_id}/qualification.json
reports/reliability/{run_id}/summary.md
reports/reliability/{run_id}/metrics.json
reports/reliability/{run_id}/scenario_results.json
reports/reliability/{run_id}/failures.json
```

API 조회:

- `GET /api/runtime/reliability/latest`
- `GET /api/runtime/reliability/runs`
- `GET /api/runtime/reliability/runs/{run_id}`

API는 read-only다. fault injection 실행 API 또는 Dashboard 실행 버튼은 만들지 않는다.

## PASS / HOLD / FAIL

`PASS`:

- mandatory scenario가 모두 실행됐다.
- hard safety gate 위반이 없다.
- operational SLO가 통과했다.
- sample 수가 충분하다.
- critical pending/retry/dead-letter가 없다.
- lifecycle ready가 true다.

`HOLD`:

- 안전 무결성 위반은 없다.
- 장시간 soak가 미실행이거나 sample 수가 부족하다.
- 실제 장중 데이터가 부족하다.
- parser/reconcile 검증이 synthetic 수준이다.

`FAIL`:

- 실제 order command가 생성됐다.
- 이벤트 유실, 중복 체결 반영, terminal state regression이 발생했다.
- critical dead letter가 예상 밖으로 남았다.
- lifecycle ready가 잘못 true가 됐다.
- DB corruption, deadlock, 복구 불가능 backlog가 발생했다.

## Operator Response

Dead letter:

1. `qualification.json`의 failure와 `gateway_event_log.processing_result_json`을 확인한다.
2. 원본 event row를 삭제하지 않는다.
3. broker order/position projection과 local managed order를 대조한다.
4. `STOP_NEW_BUY` 해제 전 unmatched fill, balance mismatch, critical backlog가 0인지 확인한다.

Unmatched fill:

1. `account`, `code`, `side`, `order_no`, `command_id`, `execution_id`를 확인한다.
2. 자동 매칭이 애매하면 `RECONCILE_REQUIRED`를 유지한다.
3. 동일 BUY command를 자동 재전송하지 않는다.

Balance mismatch:

1. `broker_position_state`와 local position projection을 비교한다.
2. mismatch가 크거나 반복되면 `REDUCE_ONLY`를 검토한다.
3. 이번 단계에서는 자동 매도 command를 만들지 않는다.

## Next Stage Entry Criteria

- 최소 3개 거래일 replay deterministic PASS
- 정상장, 약세장, 고변동 세션 포함
- mandatory fault suite 전부 PASS
- 1시간 이상 observe soak PASS
- 가능하면 장중 전체 세션 observe PASS
- critical event loss 0
- duplicate execution apply 0
- terminal state regression 0
- unexpected dead letter 0
- order command count 0
- lifecycle ready 오판 0

자료가 부족하면 PASS 대신 HOLD로 둔다.
