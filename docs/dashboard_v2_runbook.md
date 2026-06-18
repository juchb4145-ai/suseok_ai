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
python -m pytest tests/test_themelab_web_dashboard.py -q
python -m pytest tests/test_core_runtime_api.py -q
```
