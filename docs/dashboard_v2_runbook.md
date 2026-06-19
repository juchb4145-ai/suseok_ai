# Dashboard V2 Reboot Operations Console

Dashboard V2 is the default intraday console for Reboot V2 / Theme Core V3 observe runtime. It is an observe-only operations view: it does not expose order enable buttons, REAL/LIVE activation UI, direct Gateway order controls, or kill-switch reset controls.

## Default Cutover

Default routes:

- `/`: Dashboard V2
- `/legacy`: frozen legacy ThemeLab dashboard
- `/themelab`: compatibility alias for the legacy ThemeLab dashboard
- `/debug`: raw Core/system diagnostics

Default flags:

```text
TRADING_DASHBOARD_V2_ENABLED=true
TRADING_DASHBOARD_V2_AUTO_ROUTE=true
STRATEGY_RUNTIME_PROFILE=V2_OBSERVE
```

`STRATEGY_RUNTIME_PROFILE=LEGACY` is the explicit rollback/debug profile. Legacy panels stay off the default Dashboard V2 page.

## Main View

Dashboard V2 prioritizes:

- market regime and data freshness
- leading themes TOP5
- entry candidates in observe state
- position and exit risk summaries
- order-manager safety state
- pre-market Go/No-Go status
- top wait/block reasons

Hidden from the default page:

- hybrid score detail
- final grade detail
- threshold A/B detail
- shadow/promotion detail
- raw condition hit table
- raw gateway events
- raw command history
- raw JSON dump
- legacy ThemeLab internal diagnostics

Operators can use `/legacy`, `/debug`, or `GET /api/snapshot?detail=full` when old diagnostics are needed.

## API Schema

`GET /api/dashboard-v2/snapshot` returns:

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

`GET /api/snapshot?view=v2` returns the same Dashboard V2 schema. `GET /api/snapshot` keeps the existing schema and includes additive `dashboard_v2` when Dashboard V2 is enabled.

`GET /api/dashboard-v2/source-status` returns read-only source diagnostics for the active canonical snapshot, including namespace, view, generation, checksum prefix, source runtime cycle, DB path fingerprint, stale/fallback flags, and the active REST/WS contract.

## Read Model Operation

Dashboard V2 now reads `dashboard_read_models` first for:

- `GET /api/dashboard-v2/snapshot`
- `GET /api/snapshot?view=v2`
- `/ws/dashboard` Dashboard V2 payload

The read model is built from the latest in-memory runtime snapshot, gateway health, command summary, and lightweight runtime supervisor status. It should not trigger raw candidate/theme/order table scans on every dashboard request.

The canonical Reboot V2 view is `reboot_v2.main`. Legacy compatibility rows may remain in `dashboard_read_models`, but Reboot V2 does not write to or select from the legacy `main` row as its source of truth.

Read model metadata appears under `read_model`:

- `source=READ_MODEL`: persisted/in-memory read model was used.
- `source=FALLBACK_LIVE_BUILD`: read model was missing/corrupt and the legacy live builder was used.
- `generation`: monotonic generation per `view_name`.
- `snapshot_namespace`: canonical namespace. Reboot V2 uses `reboot_v2.main`.
- `snapshot_at`: source snapshot timestamp.
- `snapshot_age_sec`: current age of the snapshot.
- `stale=true`: the dashboard is showing the last known snapshot, not current live state.
- `fallback_used=true`: operator should treat the payload as degraded.

Stale policy:

- `READ_MODEL_STALE`: snapshot age exceeded the configured stale threshold, default 5 seconds.
- `RUNTIME_SNAPSHOT_STALE`: runtime source cycle is old.
- `ORDER_RECONCILE_REQUIRED`: order state requires reconcile or STOP_NEW_BUY handling.

When stale is true, the dashboard may still show the last normal data, but operators must read it as “last known data”. Do not treat green-looking section values as current if a stale banner is present.

Fallback policy:

- Missing/corrupt read model can use `FALLBACK_LIVE_BUILD`.
- Stale alone does not trigger live rebuild on request.
- Fallback failure should produce a degraded payload instead of an API 500 where possible.

Restart recovery:

- The latest persisted read model is restored after process restart.
- Recovered snapshots should be interpreted as stale until the next runtime snapshot is written.

Order safety fields:

- `STOP_NEW_BUY`: 신규 매수 금지. Reconcile/risk state를 먼저 확인한다.
- `REDUCE_ONLY`: 신규 매수 금지, 축소/청산 우선 모드.
- `RECONCILE_REQUIRED`: Kiwoom/order/balance state mismatch 가능성. 신규 주문보다 reconcile 확인이 우선이다.
- `observe_only=true`: OrderManager가 관찰 전용이다.
- `send_order_allowed=false`: Gateway `send_order` 생성이 차단된 상태다.

Operator response:

1. If `READ_MODEL_STALE`, check runtime status and Gateway heartbeat before trusting the view.
2. If `FALLBACK_LIVE_BUILD`, inspect read model writer errors and DB health.
3. If `ORDER_RECONCILE_REQUIRED` or `STOP_NEW_BUY`, inspect order reconcile logs before restarting runtime.
4. Do not use Dashboard V2 as an order activation surface. It has no order-enable, kill-switch reset, or direct send/cancel controls.

## Safety Policy

Dashboard V2 must not create or expose:

- order enable/disable controls
- LIVE/REAL order activation controls
- direct Gateway `send_order` / `cancel_order` controls
- kill-switch reset controls
- automatic threshold-apply controls
- UI language that implies observe candidates are confirmed trades

## Verification

```powershell
python -m pytest tests/test_dashboard_v2_snapshot.py -q
python -m pytest tests/test_dashboard_read_model_repository.py tests/test_dashboard_read_model_writer.py tests/test_dashboard_read_model_api.py -q
python -m pytest tests/test_themelab_web_dashboard.py -q
python -m pytest tests/test_core_runtime_api.py -q
```
