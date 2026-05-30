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

Common Core environment variables:

- `TRADING_CORE_TOKEN`: local Gateway/Core token.
- `TRADING_DB_PATH`: SQLite path.
- `TRADING_MODE`: `OBSERVE`, `DRY_RUN`, or `LIVE`.
- `TRADING_ALLOW_LIVE`: must be `1` before LIVE orders can be queued.
- `TRADING_MAX_ORDER_AMOUNT`: per-order amount cap.
- `TRADING_MAX_DAILY_ORDERS_PER_CODE`: per-code command cap.
- `TRADING_ORDER_COMMAND_TTL_SEC`: order command expiry.
- `TRADING_ORDER_COMMAND_MAX_ATTEMPTS`: order command max attempts.

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
- `GET /api/gateway/commands` Gateway polling only
- `GET /api/gateway/commands/status`
- `GET /api/gateway/commands/history`
- `POST /api/gateway/commands/{command_id}/cancel`
- `POST /api/gateway/commands/prune`
- `POST /api/orders/enqueue`

Order enqueue example:

```powershell
$body = @{
  account = "1234567890"
  code = "005930"
  side = "buy"
  quantity = 1
  price = 70000
  order_type = 1
  hoga = "00"
  tag = "HYBRID_005930"
  strategy_name = "hybrid_gate"
  candidate_id = 123
  reason = "READY gate result"
  idempotency_key = "hybrid_gate:2026-05-30:005930:1"
} | ConvertTo-Json

Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/orders/enqueue -Body $body -ContentType "application/json"
```

## 32bit Kiwoom Gateway

Use 32bit Python 3.9.13 with Kiwoom OpenAPI+ installed and OCX registered.

```powershell
py -3.9-32 -m pip install -r requirements-32.txt
py -3.9-32 apps/kiwoom_gateway.py --core-url http://127.0.0.1:8000 --token change-me-local-token
```

Gateway rate-limit overrides:

- `GATEWAY_RATE_LIMIT_SEND_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_CANCEL_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_MODIFY_ORDER_SEC`
- `GATEWAY_RATE_LIMIT_TR_REQUEST_SEC`
- `GATEWAY_RATE_LIMIT_SEND_CONDITION_SEC`
- `GATEWAY_RATE_LIMIT_REGISTER_REALTIME_SEC`
- `GATEWAY_RATE_LIMIT_REMOVE_REALTIME_SEC`
- `GATEWAY_RATE_LIMIT_DEFAULT_SEC`

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

`trading/engine.py` direct `client.send_order` behavior is legacy-only. The 64bit Core must queue real orders through `/api/orders/enqueue`.

## Safety Defaults

- Default mode is `OBSERVE`.
- `LIVE` order enablement requires both `TRADING_MODE=LIVE` and `TRADING_ALLOW_LIVE=1`.
- Gateway/Core traffic requires a local token.
- Bind Core to `127.0.0.1` unless there is a reviewed deployment plan.
- Core must pass order/risk guards before queueing any real order command.
- Gateway polling does not mean success. Only `command_ack status=ACKED` marks a command successful.
- Duplicate `idempotency_key` or deterministic order dedupe keys are rejected while active or already ACKED.

More detail:

- [Architecture](docs/architecture_32bit_gateway_64bit_core.md)
- [Runbook](docs/runbook_32bit_gateway_64bit_core.md)
- [Gateway Command Queue Runbook](docs/gateway_command_queue_runbook.md)
