# Dashboard V2 Reboot Operations Console

Dashboard V2는 Strategy Reboot V2 운영 흐름을 한 화면으로 요약하는 관찰 콘솔이다. 주문 실행 버튼, REAL/LIVE 활성화 UI, kill switch reset UI는 포함하지 않는다.

## Feature Flags

기본값은 기존 dashboard 유지다.

```text
TRADING_DASHBOARD_V2_ENABLED=false
TRADING_DASHBOARD_V2_AUTO_ROUTE=false
TRADING_DASHBOARD_V2_SNAPSHOT_CACHE_TTL_SEC=3
TRADING_DASHBOARD_V2_HEAVY_CACHE_TTL_SEC=30
STRATEGY_REBOOT_V2_DASHBOARD=0
```

V2 payload만 확인:

```powershell
$env:TRADING_DASHBOARD_V2_ENABLED="true"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000
Invoke-RestMethod "http://127.0.0.1:8000/api/dashboard-v2/snapshot"
Invoke-RestMethod "http://127.0.0.1:8000/api/snapshot?view=v2"
```

`/`에서 V2 summary를 기본 화면으로 보고 싶으면:

```powershell
$env:TRADING_DASHBOARD_V2_ENABLED="true"
$env:TRADING_DASHBOARD_V2_AUTO_ROUTE="true"
```

flag off 상태에서 `/`와 `/themelab`은 기존 ThemeLab dashboard를 유지한다. `/legacy`는 기존 ThemeLab dashboard 호환 경로, `/debug`는 Core dashboard 경로다.

## API Schema

`GET /api/dashboard-v2/snapshot`은 다음 section을 안정적으로 반환한다.

```text
v2_status
market_overview
leading_themes
entry_candidates
position_risk
exit_watch
order_manager
pre_market_check
wait_block_reasons
system_health
legacy_debug_link
safety_banners
```

`GET /api/snapshot?view=v2`도 같은 schema를 반환한다.

`GET /api/snapshot`은 기존 schema를 유지한다. `TRADING_DASHBOARD_V2_ENABLED=true`일 때만 additive field `dashboard_v2`를 포함하며, WebSocket `/ws/dashboard`도 같은 snapshot payload를 전달한다.

## Main View

첫 화면은 다음 5가지를 우선 표시한다.

- 시장국면
- 주도테마 TOP5
- 진입 준비 관찰 후보
- 보유 리스크 / 청산 판단
- 차단/대기/주문거부 사유 TOP

OrderManager 상태는 별도 카드로 표시한다.

- mode
- LIVE_SIM 허용 여부
- broker env
- account whitelist
- kill switch state
- open/rejected/pending cancel order count

## Safety Banners

상단 banner 조건:

- REAL broker detected
- LIVE_SIM flag off
- account whitelist missing
- kill switch active
- gateway heartbeat stale
- RISK_OFF
- order_manager disabled
- pre-market check NO_GO / MANUAL_REVIEW_REQUIRED / GO_OBSERVE / GO_LIVE_SIM_LIMITED

예시:

```text
실계좌 환경 감지: 모든 자동주문 차단
모의주문 비활성: 관찰 전용
RISK_OFF: 신규진입 차단, 보유 리스크 축소 우선
킬스위치 활성: 신규 매수 차단
```

## Pre-Market Check

Dashboard V2는 `pre_market_check` section을 첫 화면에 표시한다.

표시 항목:

- Go/No-Go 상태
- requested mode
- broker env
- account whitelist
- gateway heartbeat
- SQLite health
- kill switch
- pending reconcile
- data preload status
- recommended action

`NO_GO`이면 빨간 banner, `MANUAL_REVIEW_REQUIRED`이면 노란 banner, `GO_OBSERVE`이면 관찰 전용 가능, `GO_LIVE_SIM_LIMITED`이면 모의주문 제한 가능으로 표시한다. 이 section에는 주문 enable 버튼, kill switch reset 버튼, Gateway 주문 호출 버튼을 두지 않는다.

## Legacy And Debug

기본 화면에서 숨긴다.

- hybrid score detail
- final grade detail
- threshold A/B detail
- shadow/promotion detail
- raw condition hit table
- raw gateway events
- raw command history
- raw JSON dump
- legacy ThemeLab internal diagnostics

운영자가 필요할 때 `/legacy`, `/debug`, `detail=full` API로 확인한다.

## Forbidden UI

Dashboard V2에서는 만들지 않는다.

- 주문 enable/disable 버튼
- LIVE/REAL 주문 활성화 UI
- Gateway send_order/cancel_order 직접 호출 UI
- kill switch reset 버튼
- threshold 자동 변경 UI
- “매수 추천”, “매수 확정”, “수익 보장” 표현

## Verification

```powershell
python -m pytest tests/test_pre_market_check.py -q
python -m pytest tests/test_dashboard_v2_snapshot.py -q
python -m pytest tests/test_themelab_web_dashboard.py -q
python -m pytest tests/test_core_runtime_api.py -q
python -m pytest tests/test_order_manager_live_sim.py -q
python -m pytest tests/test_entry_engine.py -q
python -m pytest tests/test_exit_engine_reboot.py -q
python -m pytest -q
```
