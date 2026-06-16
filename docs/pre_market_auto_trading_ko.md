# 장 시작 전 자동매매 실행 가이드

이 문서는 아침에 자동매매 프로그램을 켤 때 보는 한글 운영 가이드입니다. 기본 권장 순서는 `DRY_RUN`으로 먼저 켜고, 키움 모의투자 서버 연결이 확실할 때만 `LIVE_SIM`으로 전환하는 것입니다.

투자 판단을 대신하는 문서가 아닙니다. 이 문서는 프로그램 실행, 점검, 모의투자 운영 절차만 다룹니다.

## 먼저 구분하기

| 구분 | 실제 주문 여부 | 언제 사용하나 |
| --- | --- | --- |
| `OBSERVE` | 없음 | 시세, 조건식, 전략 판단만 관찰 |
| `DRY_RUN` | 없음 | 자동매매가 주문하려던 의도만 DB에 기록 |
| `LIVE_SIM` | 키움 모의투자 주문 | 키움 모의투자 서버에 주문을 실제로 전송 |
| `LIVE_REAL` | 실계좌 주문 | 현재 가이드에서는 사용 금지 |

주의할 점:

- `DRY_RUN`은 주문 의도 기록용입니다. Kiwoom `send_order`를 만들지 않습니다.
- `LIVE_SIM`은 가짜 기록이 아니라 키움 모의투자 계좌로 주문을 보냅니다.
- `--mock`은 키움 접속 없이 테스트 이벤트를 보내는 개발용입니다. 키움 모의투자와 다릅니다.
- 실계좌 자동주문을 켜기 위해 `TRADING_MODE=LIVE` 또는 `TRADING_ALLOW_LIVE=1`을 설정하지 않습니다.

## 장 시간 기준

프로젝트 설정은 KRX 기준으로 장전 시작을 `08:30`, 정규장을 `09:00~15:30`으로 봅니다. KRX 안내도 주식 정규장 거래시간을 `09:00~15:30`, 호가 접수 시간을 `08:30~15:30`으로 설명합니다.

휴장일, 임시 변경, 증권사 장애 공지는 프로그램이 대신 판단해 주지 못할 수 있습니다. 실행 전에 KRX 또는 증권사 공지를 한 번 확인합니다.

권장 준비 시간:

| 시간 | 할 일 |
| --- | --- |
| 08:20 | PC, 인터넷, 키움 접속 상태 확인 |
| 08:25 | 64비트 Core/API 실행 |
| 08:30 | 32비트 Kiwoom Gateway 실행 및 로그인 |
| 08:35 | 운영 점검 명령 실행 |
| 08:40 | 대시보드 확인 |
| 09:00 이후 | 장중 모니터링 |

## 공통 준비

PowerShell을 열고 프로젝트 폴더로 이동합니다.

```powershell
cd C:\Users\juchn\주식2
```

처음 실행하거나 패키지가 바뀐 뒤에는 의존성을 설치합니다.

```powershell
.\venv_64\Scripts\python.exe -m pip install -r requirements-64.txt
py -3.9-32 -m pip install -r requirements-32.txt
```

이 가이드의 예시는 토큰을 `local-dev-token`으로 씁니다. 다른 토큰을 쓰면 Core와 Gateway 양쪽에 같은 값을 넣어야 합니다.

## 권장 실행: DRY_RUN

`DRY_RUN`은 아침 자동매매 점검의 기본 모드입니다. 전략은 돌고 주문 의도는 기록되지만, 실제 키움 주문은 나가지 않습니다.

### 1. DRY_RUN 설정 저장

```powershell
.\venv_64\Scripts\python.exe tools\configure_runtime_order_mode.py --mode DRY_RUN
```

### 2. 64비트 Core/API 실행

PowerShell 창 1에서 실행합니다.

```powershell
cd C:\Users\juchn\주식2
$env:TRADING_CORE_TOKEN = "local-dev-token"
$env:TRADING_MODE = "OBSERVE"
$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "1"
$env:TRADING_RUNTIME_MODE = "DRY_RUN"
$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "1"
$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT = "300000"
.\venv_64\Scripts\python.exe apps\core_api.py --host 127.0.0.1 --port 8000 --token $env:TRADING_CORE_TOKEN --mode OBSERVE
```

확인 포인트:

- `TRADING_MODE`는 `OBSERVE`로 둡니다. Core의 실주문 스위치를 닫아두기 위해서입니다.
- `TRADING_RUNTIME_MODE`만 `DRY_RUN`으로 둡니다. 전략 런타임이 주문 의도를 기록하게 하는 스위치입니다.
- `TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT`는 종목당 가상 주문 금액입니다. 위 예시는 30만 원입니다.

### 3. 32비트 Kiwoom Gateway 실행

PowerShell 창 2에서 실행합니다.

```powershell
cd C:\Users\juchn\주식2
py -3.9-32 apps\kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token local-dev-token --transport rest --poll-wait-sec 1.0 --network-interval-sec 0.5
```

키움 로그인 창이 뜨면 로그인합니다. DRY_RUN이어도 실제 시세, 조건식, 체결 이벤트를 받으려면 Gateway가 정상 로그인되어 있어야 합니다.

### 4. 대시보드 열기

브라우저에서 엽니다.

```text
http://127.0.0.1:8000/
```

대시보드에서 먼저 볼 곳:

- 상단 요약 카드의 Core/Gateway/Runtime 상태
- `운영 점검 알림`
- `Runtime/DRY_RUN` 탭
- 최근 DRY_RUN 주문 의도
- Gateway 명령 이력

### 5. 운영 점검 명령

PowerShell 창 3에서 실행합니다.

```powershell
cd C:\Users\juchn\주식2
.\venv_64\Scripts\python.exe tools\ops_check.py --core-url http://127.0.0.1:8000 --token local-dev-token
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/status
```

문제가 있으면 먼저 봅니다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/ops/alerts
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/commands/status
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/commands/history?limit=20"
```

## 키움 없이 가볍게 테스트

키움 접속 전 구조만 확인할 때는 Mock Gateway를 씁니다. 이건 모의투자가 아니라 테스트 이벤트입니다.

Core는 위 DRY_RUN 방식으로 켜 둔 뒤 실행합니다.

```powershell
py -3.9-32 apps\kiwoom_gateway.py --mock --once --core-url http://127.0.0.1:8000 --token local-dev-token
```

## 모의투자형 실행: LIVE_SIM

`LIVE_SIM`은 키움 모의투자 서버에 주문을 보냅니다. 실계좌 주문은 아니지만, 주문 라이프사이클, 미체결 취소, 손절/익절 감시, 리컨실 같은 운영 흐름을 실제 주문처럼 다룹니다.

중요한 차이:

- `DRY_RUN`: DB에 주문 의도만 기록합니다. Gateway `send_order`가 만들어지지 않습니다.
- `LIVE_SIM`: 키움 모의투자 서버로 Gateway `send_order`를 보냅니다. 모의투자 계좌의 가상 돈으로 실제 매수/매도 요청이 들어갑니다.

아래 Core 실행 예시에는 `TRADING_RUNTIME_MODE=DRY_RUN`이라는 이름이 나오지만, 이것은 현재 코드의 안전 레일 이름입니다. 실제 동작은 DB에 저장된 `order_execution.mode=LIVE_SIM`과 `live_sim_enabled=true`가 결정합니다. 이 설정이 켜져 있으면 Runtime 주문 싱크가 `LiveSimRuntimeOrderSink`로 바뀌고, 모의투자 서버 확인을 통과한 주문은 Gateway 명령 이력에 `send_order`로 들어갑니다.

### LIVE_SIM 시작 전 체크

모두 만족할 때만 진행합니다.

- 키움이 모의투자 서버로 로그인되어 있다.
- Gateway heartbeat에서 계좌/서버 상태가 모의투자로 잡힌다.
- `broker_env`가 `SIMULATION`이다.
- `server_mode`가 `SIMULATION` 또는 `MOCK`이다.
- `account_mode`가 `SIMULATION`이다.
- `live_real_enabled`는 반드시 `false`다.
- 주문 한도는 작게 시작한다.
- 실계좌 번호 전체를 문서나 Git에 남기지 않는다.

LIVE_SIM 전용 Go/No-Go 점검은 Core가 켜진 뒤 다음 API로 확인합니다.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/preflight?include_details=true"
```

운영 이력에 snapshot을 남기려면 local token으로 rebuild를 실행합니다. 이 API는 읽기/저장 전용이며 주문을 만들거나 설정을 바꾸지 않습니다.

```powershell
Invoke-RestMethod `
  -Method Post `
  -Headers @{ "X-Local-Token" = $env:TRADING_CORE_TOKEN } `
  "http://127.0.0.1:8000/api/runtime/live-sim/preflight/rebuild?include_details=true"
```

판정 기준:

- `GO`: 시작 가능
- `GO_WITH_WARNINGS`: `tools/start_market_open_live_sim.ps1 -AllowLiveSimWithWarnings`를 명시했을 때만 진행
- `INSUFFICIENT_DATA`, `NO_GO`: 진행하지 않음
- `FAIL_CLOSED`: 어떤 플래그로도 우회하지 않음

대시보드 Overview의 `LIVE_SIM 실행 전 점검` 카드는 같은 preflight 결과를 보여줍니다. 이 카드는 상태 표시 전용이며 주문 실행 버튼이나 LIVE_SIM 활성화 버튼을 제공하지 않습니다.

### LIVE_SIM Canary 상태 확인

Preflight가 `GO`인 뒤에도 Hybrid READY Canary는 별도로 확인합니다.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/canary/summary"
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/live-sim/canary/decisions?limit=20"
```

처음에는 반드시 `live_sim_hybrid_ready_canary.order_enabled=false`로 두고 판단만 관찰합니다. 이 상태에서는 READY가 나오더라도 Canary decision만 남고 LIVE_SIM 주문은 생성되지 않습니다.

`order_enabled=true`는 충분한 DRY_RUN 성과 검증과 LIVE_SIM 운영 검증 이후에만 켭니다. 이 Canary는 `WATCH`, `PROVISIONAL`, `small_first_entry`를 주문 대상으로 삼지 않으며, `LIVE_REAL` 설정과도 무관합니다.

### 1. LIVE_SIM 설정 저장

Core를 켜기 전에 실행합니다. 이미 Core가 실행 중이면 설정 저장 후 Core를 재시작합니다.

```powershell
cd C:\Users\juchn\주식2
.\venv_64\Scripts\python.exe tools\configure_runtime_order_mode.py `
  --mode LIVE_SIM `
  --allowed-account "1234567890" `
  --max-orders-per-day 5 `
  --max-new-positions-per-day 3 `
  --max-order-amount-krw 300000 `
  --max-position-amount-krw 300000 `
  --max-total-exposure-krw 1000000
```

`--allowed-account`에는 키움 모의투자 계좌번호를 넣습니다. 계좌 제한을 비워 둘 수도 있지만, 아침 운영에서는 모의투자 계좌를 명시하는 쪽이 더 안전합니다.

불안하면 첫 실행은 kill switch를 켠 상태로 저장해서 Gateway/계좌 인식만 확인합니다.

```powershell
.\venv_64\Scripts\python.exe tools\configure_runtime_order_mode.py --mode LIVE_SIM --kill-switch-active
```

### 2. 권장 통합 실행 스크립트

설정 저장 후에는 통합 스크립트를 우선 사용합니다. 이 스크립트는 Core/Gateway를 준비하고, Runtime 시작 전에 LIVE_SIM preflight rebuild를 실행합니다.

```powershell
.\tools\start_market_open_live_sim.ps1
```

`GO_WITH_WARNINGS` 상태에서만 운영자가 명시적으로 계속 진행하려면 다음처럼 실행합니다.

```powershell
.\tools\start_market_open_live_sim.ps1 -AllowLiveSimWithWarnings
```

`FAIL_CLOSED`, REAL/UNKNOWN 계좌, `LIVE_REAL`, 허용 계좌 불일치, TR backfill READY 근거는 이 플래그로도 우회되지 않습니다.

아래 Core/Gateway 수동 실행 절차는 장애 대응이나 분리 점검용입니다.

### 3. Core/API 실행

LIVE_SIM도 Core의 실주문 스위치는 닫아 둡니다. 여기서 `TRADING_MODE=LIVE`를 쓰지 않습니다.

```powershell
cd C:\Users\juchn\주식2
$env:TRADING_CORE_TOKEN = "local-dev-token"
$env:TRADING_MODE = "OBSERVE"
$env:TRADING_RUNTIME_ENABLED = "1"
$env:TRADING_RUNTIME_AUTO_START = "1"
$env:TRADING_RUNTIME_MODE = "DRY_RUN"
$env:TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS = "1"
$env:TRADING_RUNTIME_DRY_RUN_POSITION_AMOUNT = "300000"
.\venv_64\Scripts\python.exe apps\core_api.py --host 127.0.0.1 --port 8000 --token $env:TRADING_CORE_TOKEN --mode OBSERVE
```

왜 Runtime이 `DRY_RUN`인가:

- 이 프로젝트의 `LIVE_SIM` 주문 싱크는 Runtime이 `DRY_RUN`이고 `TRADING_RUNTIME_ALLOW_DRY_RUN_ORDERS=1`일 때만 동작합니다.
- DB 설정의 `order_execution.mode=LIVE_SIM`이 들어가면 DRY_RUN 기록 대신 모의투자 주문 싱크가 선택되고, 조건을 통과한 주문은 Gateway `send_order`로 큐잉됩니다.
- `LIVE_REAL`은 별도로 막혀 있습니다.

### 4. Gateway 실행 및 모의투자 로그인

PowerShell 창 2에서 실행합니다.

```powershell
cd C:\Users\juchn\주식2
py -3.9-32 apps\kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token local-dev-token --transport rest --poll-wait-sec 1.0 --network-interval-sec 0.5
```

키움 로그인 창에서 모의투자 서버/모의투자 계좌로 로그인합니다. 실계좌 또는 알 수 없는 계좌 환경이면 `LIVE_SIM` 주문은 fail-closed로 막혀야 합니다.

### 5. LIVE_SIM 점검

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/status
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/status
Invoke-RestMethod http://127.0.0.1:8000/api/ops/alerts
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/commands/history?limit=20"
```

Gateway 상태에서 확인할 값:

- `kiwoom_logged_in`: `true`
- `orderable`: `true`
- heartbeat가 오래되지 않았는지
- heartbeat payload의 `broker_env`, `server_mode`, `account_mode`
- Gateway 명령 이력에 의도하지 않은 실계좌 주문 흔적이 없는지

## 장중 모니터링

장중에는 대시보드와 아래 명령을 번갈아 봅니다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/ops/alerts
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/orders/dry-run/summary
Invoke-RestMethod "http://127.0.0.1:8000/api/runtime/orders/dry-run?limit=20"
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/commands/status
```

즉시 멈추고 확인할 상황:

- Gateway heartbeat가 stale 또는 slow로 뜬다.
- Kiwoom 로그인 상태가 false다.
- Runtime `last_error`가 반복된다.
- Gateway 명령이 실패 또는 거부된다.
- LIVE_SIM인데 계좌/서버 모드가 `SIMULATION`으로 확인되지 않는다.

## 장 마감 후

DRY_RUN 성과 리포트를 재생성합니다.

```powershell
$headers = @{ "X-Local-Token" = "local-dev-token" }
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/runtime/performance/dry-run/rebuild?persist=true&export=true&format=all" -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/performance/dry-run/reports
```

전송 지연 리포트도 남깁니다.

```powershell
$headers = @{ "X-Local-Token" = "local-dev-token" }
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/gateway/transport/latency/rebuild?persist=true&export=true" -Headers $headers
```

저장 위치:

- DRY_RUN 성과: `reports/dry_run_performance/<거래일>/`
- 전송 지연: `reports/gateway_transport_latency/<거래일>/`

## LIVE_SIM Canary 체결/성과 확인

장중 LIVE_SIM Canary 주문이 발생한 뒤에는 대시보드의 `LIVE_SIM Canary 체결/성과` 패널을 확인합니다.

확인 항목:

- 오늘 Canary 주문 수
- 제출/접수/부분체결/완전체결/미체결/취소/청산/리컨실 필요 수
- 평균 `fill_ratio`
- 평균 `entry_slippage_bp`
- 평균 `net_return_pct`
- DRY_RUN 대비 LIVE_SIM 평균 차이
- `LIVE_WORSE`, `NO_FILL`, `PARTIAL_FILL`, `RECONCILE_REQUIRED`, orphan case 비율과 건수

행 상세에서는 order/fill/exit timeline, DRY_RUN vs LIVE_SIM 비교, raw metadata, linked IDs를 확인합니다. 이 패널에는 주문 실행 버튼이나 설정 변경 버튼이 없어야 합니다.

장후에는 LIVE_SIM Canary post-trade 리포트를 재생성하고 export합니다.

```powershell
$headers = @{ "X-Local-Token" = "local-dev-token" }
Invoke-RestMethod `
  -Method Post `
  "http://127.0.0.1:8000/api/runtime/live-sim/canary/performance/rebuild" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"persist":true,"export":"all"}'
Invoke-RestMethod http://127.0.0.1:8000/api/runtime/live-sim/canary/performance/reports
```

저장 위치:

- LIVE_SIM Canary 사후 분석: `reports/live_sim_canary/<거래일>/`

운영자 조치:

- `NO_FILL`: 주문번호와 미체결 원장을 확인하고, 반복되면 limit price policy 검토 후보로만 기록합니다.
- `PARTIAL_FILL`: 잔량 취소/리컨실 여부와 부분체결 대기 시간을 확인합니다.
- `RECONCILE_REQUIRED`: 신규 설정 변경 검토보다 먼저 broker snapshot과 `live_sim_positions` 원장을 맞춥니다.
- `ORPHAN_EXECUTION` 또는 `ORPHAN_ORDER_RESULT`: `gateway_command_id`, `order_intent_id`, `broker_order_id`, `candidate_instance_id` 순서로 연결 누락을 확인합니다.

## DRY_RUN으로 되돌리기

LIVE_SIM을 쓴 뒤에는 다음 날 실수 방지를 위해 DRY_RUN으로 되돌립니다. Core가 실행 중이면 저장 후 재시작합니다.

```powershell
cd C:\Users\juchn\주식2
.\venv_64\Scripts\python.exe tools\configure_runtime_order_mode.py --mode DRY_RUN
```

## 빠른 복구표

| 증상 | 먼저 할 일 |
| --- | --- |
| 대시보드가 안 열린다 | Core 창이 살아 있는지, 포트가 8000인지 확인 |
| Gateway heartbeat가 오래됐다 | Gateway 창, 키움 로그인, 인터넷 연결 확인 |
| Runtime이 안 돈다 | `/api/runtime/status`의 `enabled`, `running`, `last_error` 확인 |
| DRY_RUN 주문 의도가 없다 | 조건식/시세 수신, Runtime warning, gate 결과 확인 |
| LIVE_SIM 주문이 막힌다 | 계좌가 모의투자인지, heartbeat의 `SIMULATION` 값 확인 |
| 명령이 중복/거부된다 | `/api/gateway/commands/history?limit=20`에서 reason 확인 |

## 절대 하지 말 것

- 장 시작 전 테스트 목적으로 `TRADING_MODE=LIVE`를 켜지 않습니다.
- `TRADING_ALLOW_LIVE=1`을 넣지 않습니다.
- 실계좌 번호 전체를 문서, README, Git commit에 남기지 않습니다.
- 모의투자 서버 확인 없이 `LIVE_SIM`으로 시작하지 않습니다.
- 경고가 있는데 “일단 지켜보자”로 장을 시작하지 않습니다.
