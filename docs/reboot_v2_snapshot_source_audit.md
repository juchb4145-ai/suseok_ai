# Reboot V2 Snapshot Source Audit

## 결론

Reboot V2의 실제 흔들림 원인은 단일 파일 경로 문제가 아니라 source 경로 혼합이었다. 코드상 확인된 핵심 충돌은 다음이다.

- `trading_app.dashboard_v2.build_dashboard_v2_snapshot()`가 같은 logical section을 top-level과 `runtime.*`에서 동시에 읽고 `_prefer_runtime_section()`의 `enabled/status/calculated_at` 휴리스틱으로 선택했다.
- `trading_app.api._broadcast_dashboard_snapshot_after()`는 `/ws/dashboard` 연결에 `_build_dashboard_snapshot_payload()` 기반 legacy/live-built wrapper를 push할 수 있었고, 같은 websocket loop의 read-model wrapper와 섞일 수 있었다.
- `web/static/dashboard.js`는 REST polling과 WS snapshot을 도착 순서대로 `render()`해 낮은 generation 또는 live fallback이 최신 read-model 화면을 덮을 수 있었다.
- `storage.dashboard_read_model.DashboardReadModelRepository`는 `UNIQUE(view_name)` row에 조건 없는 upsert를 수행해 낮은 generation 또는 낮은 runtime cycle writer가 최신 row를 덮을 수 있었다.
- `trading_app.dependencies.get_settings()`는 상대 `TRADING_DB_PATH`를 cwd 기준으로 해석할 수 있었다.

이번 변경 후 Reboot V2의 canonical contract는 다음이다.

- canonical namespace/view: `reboot_v2.main`
- canonical producer: `DashboardReadModelService.build_from_runtime()` -> `build_dashboard_v2_snapshot()`
- canonical storage row: `dashboard_read_models.view_name = 'reboot_v2.main'`
- canonical REST: `GET /api/dashboard-v2/snapshot`, `GET /api/snapshot?view=v2`
- canonical WS: `/ws/dashboard`의 `snapshot.dashboard_v2`
- browser acceptance: `generation`, `source_runtime_cycle_count`, `checksum`, `schema_version`, `snapshot_namespace` 기준 monotonic guard

## Snapshot Source Matrix

| logical_state | producer | producer_method | current_key_path | canonical_key_path | schema_version | namespace/view_name | persistence_db_path | persistence_table | process/pid role | runtime profile | API endpoint | WebSocket payload shape | frontend consumer | freshness timestamp | generation/watermark | fallback rule | current precedence | conflict possibility | proposed action | keep / adapt / freeze / remove |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| runtime profile | runtime supervisor | `runtime_supervisor.snapshot()` | `runtime.runtime_profile`, `base.runtime_profile` | `dashboard_v2.v2_status.runtime_profile` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | `resolve_trading_db_path()` | `dashboard_read_models` | Core API/read-model writer | `V2_OBSERVE`, `THEME_CORE_V3` | `/api/dashboard-v2/snapshot`, `/api/snapshot?view=v2` | `{type:"snapshot", snapshot:{dashboard_v2:{...}}}` | `renderDashboardV2()` | `read_model.snapshot_at` | `read_model.generation` | fallback marked `FALLBACK_LIVE_BUILD` | read-model first | legacy wrapper could previously overwrite | canonical metadata, browser guard | keep |
| reboot_v2_enabled | runtime supervisor | runtime payload | `runtime.reboot_v2_enabled` | `dashboard_v2.v2_status.reboot_v2_enabled` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same DB resolver | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | V2 status label | `snapshot_at` | generation | no stale-only fallback | read-model | low | keep read-model source | keep |
| market regime | runtime market regime stage | `market_regime_dashboard_section()` into runtime snapshot | `market_regime`, `runtime.market_regime` | `dashboard_v2.market_overview` from canonical runtime section | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | `market_overview` renderer | `read_model.source_runtime_cycle_at` | `source_runtime_cycle_count` | legacy fallback allowed only when missing/corrupt and marked | before: `_prefer_runtime_section()` | high | read-model build input no longer duplicates top-level/runtime sections | adapt |
| market session | runtime market data/session | runtime snapshot | `runtime.market_session_status`, `market_regime.market_session_status` | `dashboard_v2.v2_status.market_session_status` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | status grid | `snapshot_at` | generation | marked fallback only | read-model | medium | canonical read-model metadata | keep |
| ThemeBoard | theme runtime | `theme_board_dashboard_section()` | `theme_board`, `runtime.theme_board` | `dashboard_v2.leading_themes` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | `THEME_CORE_V3` | V2 REST | WS wrapper | `renderDashboardV2Themes()` | `source_runtime_cycle_at` | cycle count | fallback cannot overwrite newer browser state | before: top/runtime heuristic | high | canonical runtime-only input for read-model | adapt |
| entry candidates | entry engine | `entry_engine_dashboard_section()` | `entry_engine`, `runtime.entry_engine`, `candidates` | `dashboard_v2.entry_candidates` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | entry rows | `snapshot_at` | generation | fallback marked degraded | read-model | medium | browser monotonic acceptance | keep |
| Candidate FSM | runtime supervisor | `runtime.candidate_fsm` | `runtime.candidate_fsm` | `dashboard_v2.system_health.strategy.candidate_fsm_state_counts` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | system health debug | `snapshot_at` | generation | no behavior fallback | read-model | low | diagnostics only | keep |
| exit engine | exit runtime | `exit_engine_dashboard_section()` | `exit_engine`, `runtime.exit_engine`, `runtime.exit_engine_reboot` | `dashboard_v2.exit_watch` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | exit/position rows | `snapshot_at` | generation | marked fallback only | read-model | medium | canonical read-model source | keep |
| position risk | position risk stage | `position_risk_dashboard_section()` | `position_risk`, `runtime.position_risk` | `dashboard_v2.position_risk` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | position risk renderer | `snapshot_at` | generation | marked fallback only | read-model | medium | monotonic guard | keep |
| order manager | runtime order manager | `_order_manager_source()` | `order_manager`, `runtime.order_manager`, `runtime.order_manager_v2` | `dashboard_v2.order_manager` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | OBSERVE/LIVE_SIM guarded | V2 REST | WS wrapper | order safety panel | `source_runtime_cycle_at` | cycle count | fallback marked `LEGACY_LIVE_FALLBACK_ADAPTER` | before: `order_manager_v2` normalized then duplicated | high | source metadata plus stale write rejection | adapt |
| gateway heartbeat | gateway state | `gateway_state.snapshot()` | `gateway`, `system_health.gateway_heartbeat` | `dashboard_v2.system_health.gateway_heartbeat` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core API process | any | V2 REST | WS wrapper | health grid | `snapshot_at` | generation | fallback if read-model missing | read-model | low | source-status exposes active contracts | keep |
| pre-market result | pre-market service | `_build_pre_market_check_report_payload()` | `pre_market_check` | `dashboard_v2.pre_market_check` | `pre_market_check.v1` inside V2 | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | observe/dry-run | V2 REST | WS wrapper | pre-market panel | `snapshot_at` | generation | marked fallback only | read-model | low | keep top-level input only for this report | keep |
| read-model metadata | read-model service | `_metadata()` and `_with_current_metadata()` | `read_model` | `dashboard_v2.read_model` | `dashboard_v2.read_model.v1` | `reboot_v2.main` | `get_settings().db_path` resolved by `resolve_trading_db_path()` | `dashboard_read_models` | single Core writer, CAS guarded | V2 | V2 REST | WS wrapper | status message/source grid | `snapshot_at`, `published_at` | `generation`, `checksum`, `source_runtime_cycle_count` | missing/corrupt only | read-model first | high before change | namespace, metadata, stale write rejection | keep |
| safety banners | dashboard builder | `_safety_banners()` plus read-model stale banners | `safety_banners` | `dashboard_v2.safety_banners` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer/API | V2 | V2 REST | WS wrapper | banner list | `snapshot_at` | generation | fallback banner explicit | read-model | medium | duplicate/stale browser guard | keep |
| wait/block reasons | dashboard builder | `_wait_block_reasons()` | `wait_block_reasons` from multiple runtime sections | `dashboard_v2.wait_block_reasons` | `dashboard_v2.reboot_ops.v1` | `reboot_v2.main` | same | `dashboard_read_models` | Core writer | V2 | V2 REST | WS wrapper | reason list | `snapshot_at` | generation | fallback marked | read-model | medium | canonical runtime input | adapt |

## Ordering Rules

Browser and writer ordering do not use arrival time as truth.

1. Reject if schema is not `dashboard_v2.reboot_ops.v1`.
2. Reject if namespace is present and not `reboot_v2.main`.
3. Reject lower `generation`.
4. Reject lower `source_runtime_cycle_count`.
5. Treat same `generation` and same `checksum` as duplicate.
6. Reject same generation with a different checksum.
7. Reject older `snapshot_at` when generation and cycle are tied.
8. Persisted writes reject lower generation or lower runtime cycle before SQLite upsert.

## API And WS Contract

- `GET /api/dashboard-v2/snapshot`: direct Dashboard V2 payload.
- `GET /api/snapshot?view=v2`: same direct Dashboard V2 payload source.
- `GET /api/snapshot`: legacy/debug compatible payload. Reboot V2 browser polling no longer uses it as the V2 source.
- `/ws/dashboard`: wrapper payload with canonical V2 under `snapshot.dashboard_v2`.

The event-driven WS broadcast path now uses `_dashboard_snapshot_payload_for_ws_client_count()` so it cannot send `_build_dashboard_snapshot_payload()` legacy/live-built snapshots into the same V2 connection.

## Diagnostics

`GET /api/dashboard-v2/source-status` returns the current canonical namespace/view, schema, DB path fingerprint, writer identity, source epoch, generation, runtime cycle, checksum prefix, source kind, stale/fallback status, active REST/WS contract, and conflict counters.

## DB And Migration Notes

No destructive migration is required. Existing `main` rows are preserved. Reboot V2 writes to `view_name='reboot_v2.main'`, so legacy compatibility rows can remain read-only for old diagnostics.

Rollback is code-only:

1. Revert the service default `view_name` to the previous value.
2. Revert WS broadcast helper usage.
3. Leave the `reboot_v2.main` row in place or delete it manually after backup if old code cannot ignore it.

