# Runbook: 32bit Gateway / 64bit Core

## 1. 64bit Core/API

Use a 64bit Python environment.

```powershell
python -m pip install -r requirements-64.txt
$env:TRADING_CORE_TOKEN = "change-me-local-token"
$env:TRADING_MODE = "OBSERVE"
python -m uvicorn trading_app.api:app --host 127.0.0.1 --port 8000 --reload
```

Dashboard:

```text
http://127.0.0.1:8000/
```

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

PR-1 defaults keep real orders disabled.
