# Reboot V2 Pre-Market Operations Runbook

이 문서는 Strategy Reboot V2를 장 시작 전에 켜고, OBSERVE / DRY_RUN / LIVE_SIM_LIMITED 모드별로 Go/No-Go를 판단하는 운영 절차다. 이 Runbook과 점검 도구는 주문 전략을 추가하지 않으며, LIVE/REAL 주문 활성화 기능도 제공하지 않는다.

## 운영 모드

### OBSERVE

조건검색, TR hydration, 실시간 tick/candle, ThemeBoard, MarketRegime, Entry/Exit 판단 흐름을 관찰한다. 주문 intent와 Gateway `send_order` 경로는 꺼진 상태가 기본이다.

```powershell
$env:STRATEGY_REBOOT_V2_ENABLED="1"
$env:STRATEGY_REBOOT_V2_OBSERVE="1"
$env:STRATEGY_REBOOT_V2_DRY_RUN="0"
$env:STRATEGY_REBOOT_V2_LIVE_DISABLED="1"
$env:TRADING_ORDER_MANAGER_ENABLED="false"
$env:TRADING_ORDER_MANAGER_MODE="OBSERVE"
$env:TRADING_ALLOW_LIVE_SIM_ORDERS="false"
```

### DRY_RUN

EntryEngine/ExitEngine 판단 결과가 DRY_RUN intent로만 남는다. Gateway 주문 전송은 활성화하지 않는다.

```powershell
$env:STRATEGY_REBOOT_V2_ENABLED="1"
$env:STRATEGY_REBOOT_V2_OBSERVE="1"
$env:STRATEGY_REBOOT_V2_DRY_RUN="1"
$env:STRATEGY_REBOOT_V2_LIVE_DISABLED="1"
$env:TRADING_ENTRY_ALLOW_DRY_RUN_INTENTS="true"
$env:TRADING_EXIT_ALLOW_DRY_RUN_SELL_INTENTS="true"
$env:TRADING_ORDER_MANAGER_MODE="DRY_RUN"
$env:TRADING_ALLOW_LIVE_SIM_ORDERS="false"
```

### LIVE_SIM_LIMITED

모의계좌에서만 제한적으로 OrderManager LIVE_SIM 경로를 관찰한다. 실계좌 주문을 의미하지 않는다.

```powershell
$env:TRADING_ORDER_MANAGER_ENABLED="true"
$env:TRADING_ORDER_MANAGER_MODE="LIVE_SIM"
$env:TRADING_ALLOW_LIVE_SIM_ORDERS="true"
$env:TRADING_REQUIRE_SIMULATION_BROKER="true"
$env:TRADING_BLOCK_REAL_BROKER="true"
$env:TRADING_LIVE_SIM_ACCOUNT_WHITELIST="<모의계좌번호>"
$env:TRADING_LIVE_SIM_MAX_ORDER_QUANTITY="1"
$env:TRADING_LIVE_SIM_MAX_ORDER_AMOUNT="100000"
$env:TRADING_LIVE_SIM_CANCEL_UNFILLED_AFTER_SEC="45"
```

REAL broker/account를 허용하는 env profile은 만들지 않는다. `STRATEGY_REBOOT_V2_LIVE_DISABLED=1`은 REAL live 주문 차단 의미를 유지한다.

## 전날 장 마감 후

- Theme universe, theme membership, symbol master preload 상태를 확인한다.
- 전일 종가, 평균 거래대금, 주요 지수 watch code 구성을 확인한다.
- 미체결, 미관리 주문, reconcile 필요 건을 정리한다.
- Kill switch state가 `NORMAL`인지 확인한다.
- Dashboard V2와 API token 설정을 확인한다.

## 08:30~08:45 Core/Gateway 기동

1. Core API를 OBSERVE 기본값으로 기동한다.
2. 32bit Kiwoom Gateway를 기동한다.
3. `/health`, `/api/status`, `/api/gateway/status`를 확인한다.
4. Gateway heartbeat, Kiwoom login, orderable, broker env, account를 확인한다.

```powershell
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
python apps/kiwoom_gateway.py
```

## 08:45~08:55 장전 preload

- 조건검색식 로드와 실시간 등록 상태를 확인한다.
- ThemeBoard warmup, MarketRegime index watch, Opening Burst schedule을 확인한다.
- PostgreSQL 또는 외부 Warehouse preload 실패는 주문 경로의 동기 장애로 승격하지 않는다. 단, 장전 Go/No-Go에는 사유로 표시한다.
- SQLite operational store 장애는 No-Go다.

## 08:55~09:00 Go/No-Go 판단

```powershell
python tools/pre_market_check.py --core-url http://127.0.0.1:8000 --token $env:TRADING_CORE_TOKEN --mode observe
python tools/pre_market_check.py --core-url http://127.0.0.1:8000 --token $env:TRADING_CORE_TOKEN --mode dry-run
python tools/pre_market_check.py --core-url http://127.0.0.1:8000 --token $env:TRADING_CORE_TOKEN --mode live-sim --export reports/pre_market/%DATE%/check.json
```

Exit code:

- `0`: GO
- `1`: WARN 또는 `MANUAL_REVIEW_REQUIRED`
- `2`: `NO_GO`

API:

- `GET /api/ops/pre-market-check?mode=observe`
- `POST /api/ops/pre-market-check/rebuild?mode=live-sim` with `X-Local-Token`

`POST rebuild`도 주문 enable을 수행하지 않는다.

## Go/No-Go 규칙

`NO_GO`:

- REAL broker detected
- broker env unknown and live_sim requested
- account whitelist fail
- SQLite operational store unhealthy
- Gateway heartbeat stale
- Kiwoom login fail
- command queue unhealthy
- kill switch active
- unmanaged pending order reconcile required
- LIVE_SIM requested but cancel scheduler disabled
- max quantity/amount missing or too high

`GO_OBSERVE`:

- 주문 경로가 disabled이고, 치명적 차단 사유가 없으면 가능하다.
- broker unknown은 WARN으로 표시하되 OBSERVE는 가능하다.

`GO_DRY_RUN`:

- Core/Gateway/Data가 운영 가능하고 DRY_RUN intent flag가 켜져 있어야 한다.
- OrderManager LIVE_SIM은 켜지지 않아야 한다.

`GO_LIVE_SIM_LIMITED`:

- 필수 항목이 모두 PASS여야 한다.
- broker simulation confirmed
- account whitelist pass
- live_sim flags on
- quantity/amount/day limits pass
- cancel scheduler enabled
- kill switch normal

`MANUAL_REVIEW_REQUIRED`:

- 데이터 preload 일부 실패
- ThemeBoard/MarketRegime warmup 전
- Warehouse preload 실패 but operational local cache available

## 09:00~09:15 Opening Burst 관찰

- 조건검색 include는 후보 감지 센서이며 직접 주문 신호가 아니다.
- Candidate FSM, Hydrator, ThemeBoard, MarketRegime, EntryEngine의 연결만 본다.
- `RISK_OFF`에서는 신규 매수 금지, 미체결 취소, 보유 포지션 리스크 축소를 우선한다.

## LIVE_SIM 제한 운영 체크

- 모의계좌 whitelist가 통과해야 한다.
- 최대 주문 수량은 1주, 최대 주문 금액은 100,000원 이하로 시작한다.
- 미체결 취소 스케줄러가 켜져 있어야 한다.
- Dashboard V2의 장전 점검 section이 `GO_LIVE_SIM_LIMITED`인지 확인한다.
- 실계좌임이 감지되면 무조건 No-Go다.

## 장중 장애 대응

- Gateway heartbeat stale: 신규 주문 중지, Gateway 재기동 여부 확인
- command queue unhealthy: 신규 주문 중지, stale command 정리
- kill switch active: 신규 매수 중지, 보유 리스크 축소 우선
- warehouse 장애: 장중 주문 경로를 동기 장애로 승격하지 말고 preload/data quality 사유로 표시
- SQLite 장애: No-Go, 운영 중단

## Rollback

```powershell
$env:STRATEGY_REBOOT_V2_ENABLED="0"
$env:STRATEGY_REBOOT_V2_DRY_RUN="0"
$env:STRATEGY_REBOOT_V2_DASHBOARD="0"
$env:TRADING_ORDER_MANAGER_ENABLED="false"
$env:TRADING_ALLOW_LIVE_SIM_ORDERS="false"
```

Rollback 후 확인:

- Gateway command queue에 신규 `send_order`가 없다.
- pending LIVE_SIM order는 cancel/reconcile 상태를 확인한다.
- legacy dashboard route가 정상이다.

## 금지 사항

- 주문 enable/disable API 추가 금지
- Dashboard 주문 활성화 버튼 추가 금지
- REAL live 주문 flag 추가 금지
- Gateway `send_order` / `cancel_order` 직접 호출 금지
- kill switch reset 버튼 추가 금지
- threshold 자동 조정 금지
- 체크 실패를 무시하고 `GO_LIVE_SIM_LIMITED` 반환 금지
- SQLite 장애를 WARN으로 낮추지 말 것
- REAL broker 감지를 WARN으로 낮추지 말 것
