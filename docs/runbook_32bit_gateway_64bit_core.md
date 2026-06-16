# Runbook: 32bit Gateway / 64bit Core

## 1. 64bit Core/API

Use a 64bit Python environment.

```powershell
python -m pip install -r requirements-64.txt
$env:TRADING_CORE_TOKEN = "change-me-local-token"
$env:TRADING_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000 --reload
```

For LIVE order enqueue testing, all of these must be explicitly set:

```powershell
$env:TRADING_MODE = "LIVE"
$env:TRADING_ALLOW_LIVE = "1"
$env:TRADING_MAX_ORDER_AMOUNT = "3000000"
$env:TRADING_MAX_DAILY_ORDERS_PER_CODE = "5"
```

Dashboard:

```text
http://127.0.0.1:8000/
```

The intraday operator URL is the single ThemeLab operator dashboard at `/`. `/themelab` remains a compatibility alias for older bookmarks. Core/Gateway/Runtime details are folded into the `시스템 상태` and `개발자 상세` tabs.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## 2. 32bit Kiwoom Gateway

Use 32bit Python 3.9.13 with Kiwoom OpenAPI+ installed and OCX registered.

```powershell
py -3.9-32 -m pip install -r requirements-32.txt
py -3.9-32 apps/kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token change-me-local-token
```

Optional Gateway rate-limit overrides:

```powershell
$env:GATEWAY_RATE_LIMIT_SEND_ORDER_SEC = "0.5"
$env:GATEWAY_RATE_LIMIT_TR_REQUEST_SEC = "1.0"
```

Mock Gateway smoke test:

```powershell
py -3.9-32 apps/kiwoom_gateway.py --mock --once --core-url http://127.0.0.1:8000 --token change-me-local-token
```

## 3. Legacy PyQt App

The old single-process app is deprecated but still available:

```powershell
py -3.9-32 apps/legacy_pyqt_app.py --mock
```

## 4. Safety Checklist Before LIVE

- `TRADING_MODE=LIVE`
- `TRADING_ALLOW_LIVE=1`
- Account is selected and shown in `/api/gateway/status`.
- Gateway heartbeat is fresh.
- Kiwoom login is true.
- Market session guard is open.
- Daily order count/loss limits are configured.
- Order command carries `command_id` or `idempotency_key`.
- `OrderGuard` and risk checks pass before `send_order` is queued.

PR-2 defaults keep real orders disabled unless LIVE is explicitly enabled.

## 5. Command Queue Inspection

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/gateway/commands/status
Invoke-RestMethod "http://127.0.0.1:8000/api/gateway/commands/history?limit=20"
```

The Gateway polling endpoint is separate and should be used only by the Gateway:

```text
GET /api/gateway/commands
```
