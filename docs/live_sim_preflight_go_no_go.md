# LIVE_SIM Preflight Go/No-Go

`LIVE_SIM`은 실계좌 주문은 아니지만 키움 모의투자 서버로 실제 `send_order`를 전송한다. 그래서 시작 전에는 계좌 모드, 안전 설정, DRY_RUN 성과, Gateway/TR 부하, ThemeLab backfill 상태를 한 번에 확인하고, 통과하지 못하면 런타임 주문 싱크를 열지 않는다.

## Status

| status | 의미 | 시작 스크립트 동작 |
| --- | --- | --- |
| `GO` | 필수 안전 조건과 성과 조건 통과 | Runtime 시작 가능 |
| `GO_WITH_WARNINGS` | 필수 조건은 통과했지만 운영자 확인 경고 존재 | `-AllowLiveSimWithWarnings`가 있을 때만 진행 |
| `INSUFFICIENT_DATA` | DRY_RUN 성과 표본이 부족하거나 리포트가 없음 | 차단 |
| `NO_GO` | 성과, 리스크 설정, kill switch, Gateway 조건 중 차단 사유 존재 | 차단 |
| `FAIL_CLOSED` | REAL/UNKNOWN 계좌, LIVE_REAL 설정, backfill READY 근거 등 하드 위험 감지 | 항상 차단, override 불가 |

`FAIL_CLOSED`는 `-AllowLiveSimWithWarnings`로 우회하지 않는다. 특히 `BROKER_ACCOUNT_REAL_DETECTED`, `BROKER_ACCOUNT_UNKNOWN_FAIL_CLOSED`, `LIVE_REAL_EXECUTION_CONFIG_DETECTED`, `LIVE_REAL_ENABLED_TRUE`, `ALLOWED_ACCOUNT_MISMATCH`, `THEME_BACKFILL_CAUSED_READY_DETECTED`는 운영자가 수동으로 원인을 제거해야 한다.

## API

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/preflight?include_details=true"
```

현재 상태를 읽기 전용으로 계산한다. 주문을 만들지 않고 설정을 바꾸지 않는다.

```powershell
Invoke-RestMethod `
  -Method Post `
  -Headers @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN } `
  "http://127.0.0.1:8000/api/runtime/live-sim/preflight/rebuild?include_details=true"
```

preflight를 다시 계산하고 `live_sim_preflight_snapshots`에 저장한다. 이 역시 주문을 만들지 않고 설정을 바꾸지 않는다.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/preflight/history?limit=20&offset=0"
```

최근 저장된 snapshot 이력을 조회한다.

## Snapshot 주요 필드

- `status`: 최종 Go/No-Go 상태
- `blocking_reasons`, `warning_reasons`: 차단/경고 reason code
- `operator_message_ko`, `recommended_action_ko`: 운영자용 한국어 메시지
- `account_mode_summary`: 계좌/서버 모드 요약, 계좌번호는 마스킹
- `performance_summary`: DRY_RUN 성과 요약과 `go_no_go`
- `gateway_load_summary`: Gateway queue, 주문 pending, backfill pending, 최근 TR 실패/속도 제한
- `backfill_summary`: ThemeLab backfill 상태와 load guard
- `safety_summary`: LIVE_SIM/LIVE_REAL/kill switch/exit guard/reconcile/risk limit 상태

## 시작 스크립트

`tools/start_market_open_live_sim.ps1`는 Core와 Gateway를 준비한 뒤 Runtime 시작 전에 `/api/runtime/live-sim/preflight/rebuild`를 호출한다.

```powershell
.\tools\start_market_open_live_sim.ps1
```

`GO_WITH_WARNINGS` 상태를 운영자가 명시적으로 받아들이려면 다음 플래그가 필요하다.

```powershell
.\tools\start_market_open_live_sim.ps1 -AllowLiveSimWithWarnings
```

다음 상태는 절대 우회하지 않는다.

- `FAIL_CLOSED`
- REAL 계좌/서버 감지
- UNKNOWN 계좌 모드에서 fail-closed 설정
- `LIVE_REAL` 또는 `live_real_enabled=true`
- 허용 계좌번호 불일치
- TR backfill이 READY/READY_SMALL 근거가 된 흔적

스크립트 출력과 최종 JSON은 계좌번호를 마스킹한 preflight summary만 보여준다.

## Runtime Load Guard

ThemeLab TR backfill은 Runtime Load Guard를 통해 신규 dispatch가 멈춘다.

`load_guard_status`:

- `OK`: backfill dispatch 가능
- `DEGRADED`: 운영자 확인 필요, 보수적으로 관찰
- `PAUSED`: 신규 backfill dispatch 중단
- `FAIL_CLOSED`: hard safety 사유로 중단

대표 `pause_reason_codes`:

- `READY_OR_READY_SMALL_PRESENT`
- `ORDER_COMMAND_PENDING`
- `GATEWAY_HEARTBEAT_STALE`
- `COMMAND_LATENCY_HIGH`
- `RATE_LIMITED_RECENT`
- `TR_FAILURE_RECENT`
- `PARSER_MISS_RATIO_HIGH`
- `TR_BACKFILL_CAUSED_READY`

Backfill 값은 dashboard coverage와 운영자 판단 보조용이다. `gate_usable=false`이며 READY/READY_SMALL 근거로 사용하면 안 된다.
