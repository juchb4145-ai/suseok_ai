# Kiwoom Trading System

This repository is moving from a 32bit single-process PyQt/Kiwoom app to a split architecture:

- **32bit Kiwoom Gateway**: Kiwoom OpenAPI+ ActiveX/QAxWidget only.
- **64bit Core/API/Web Dashboard**: strategy runtime, candidates, themes, reviews, risk checks, DB, API, and web UI.

The old PyQt desktop app is still available as a deprecated legacy entrypoint.

## 64bit Core/API

Use a 64bit Python environment. The Core requirements intentionally do not include PyQt5.

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

Core APIs:

- `GET /health`
- `GET /api/status`
- `GET /api/gateway/status`
- `GET /api/candidates`
- `GET /api/themes`
- `GET /api/orders`
- `GET /api/reviews`
- `GET /api/snapshot`
- `WS /ws/dashboard`
- `POST /api/gateway/events`
- `GET /api/gateway/commands`

## 32bit Kiwoom Gateway

Use 32bit Python 3.9.13 with Kiwoom OpenAPI+ installed and OCX registered.

```powershell
py -3.9-32 -m pip install -r requirements-32.txt
py -3.9-32 apps/kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token change-me-local-token
```

Mock Gateway smoke test:

```powershell
py -3.9-32 apps/kiwoom_gateway.py --mock --once --core-url http://127.0.0.1:8000 --token change-me-local-token
```

## Legacy PyQt App

Deprecated compatibility path:

```powershell
py -3.9-32 apps/legacy_pyqt_app.py --mock
```

`main.py` remains for compatibility, but new strategy/API/dashboard development should target `trading_app` and `apps/kiwoom_gateway.py`.

## Safety Defaults

- Default mode is `OBSERVE`.
- `LIVE` order enablement requires both `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Gateway/Core traffic requires a local token.
- Bind Core to `127.0.0.1` unless there is a reviewed deployment plan.
- Core must pass order/risk guards before queueing any real order command.

More detail:

- [Architecture](docs/architecture_32bit_gateway_64bit_core.md)
- [Runbook](docs/runbook_32bit_gateway_64bit_core.md)
