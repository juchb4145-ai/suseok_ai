# LIVE_SIM Hybrid READY Canary

## 목적

LIVE_SIM Hybrid READY Canary는 Hybrid Gate의 observe-only 결과 중 일부 READY 신호만 키움 모의투자 주문 후보로 제한 승격하는 실험 경로다.

이 경로는 `LIVE_REAL`과 무관하다. 실계좌 주문 설정을 켜지 않으며, `TRADING_MODE=LIVE` 또는 `TRADING_ALLOW_LIVE=1`을 요구하지 않는다.

## 제외 대상

다음 신호는 Canary 주문 대상이 아니다.

- `WATCH`, `PROVISIONAL`, `WARMUP`, `MISSING_CORE`, `STALE`
- `small_first_entry`
- backfill tick 또는 `gate_usable=false` 근거
- 시장 risk-off, 추격 위험, late laggard, low breadth, entry risk 차단 신호

WATCH/PROVISIONAL은 아직 관찰과 검증 대상이다. 주문 후보로 승격하려면 별도 검증 PR과 운영 승인 절차가 필요하다.

## 기본 설정

`strategy_runtime_settings.live_sim_hybrid_ready_canary` 기본값은 반드시 안전하게 닫혀 있다.

```yaml
live_sim_hybrid_ready_canary:
  enabled: false
  order_enabled: false
  require_preflight_go: true
  allow_go_with_warnings: false
  require_dry_run_go_no_go: true
  min_trade_days: 5
  min_accepted_entry_lifecycles: 30
  min_net_expectancy_pct: 0.0
  max_bad_ready_rate: 0.25
  max_stale_tick_rate: 0.10
  max_latency_risk_rate: 0.10
  max_orders_per_day: 1
  max_orders_per_cycle: 1
  max_new_positions_per_day: 1
  max_position_amount_krw: 100000
  position_size_multiplier: 0.10
  allowed_hybrid_statuses: ["READY"]
  allowed_position_tiers: ["normal_first_entry"]
  allowed_theme_statuses: ["ACTIVE", "LEADING_THEME", "SPREADING_THEME"]
  allowed_stock_roles: ["LEADER", "CO_LEADER"]
  allowed_price_location_readiness: ["READY"]
  allowed_risk_levels: ["PASS"]
  require_latest_tick_ready: true
  require_support_ready: true
  require_vwap_or_recent_support_ready: true
  require_gate_usable_true: true
  block_if_backfill_source_only: true
  block_if_market_risk_off: true
  block_if_chase_risk: true
  block_if_late_laggard: true
  block_if_low_breadth: true
  block_if_leader_only_laggard: true
  block_if_entry_risk_temp_wait: true
  block_if_entry_risk_final_block: true
  block_if_load_guard_not_ok: true
  order_ttl_sec: 30
  limit_price_policy: "safe_limit"
  max_entry_slippage_bp: 10
  submit_first_leg_only: true
  reason_code: "LIVE_SIM_HYBRID_READY_CANARY"
```

`enabled=false`이면 판단 경로 자체가 비활성이다. `enabled=true`이고 `order_enabled=false`이면 판단만 기록하는 observe-only 모드다.

## 필수 조건

Canary 주문 후보가 되려면 모두 통과해야 한다.

- LIVE_SIM Preflight status가 `GO`
- DRY_RUN Performance `go_no_go.decision`이 `GO`
- Runtime Load Guard status가 `OK`
- Hybrid status가 `READY`
- Position tier가 `normal_first_entry`
- Theme status가 `ACTIVE`, `LEADING_THEME`, `SPREADING_THEME` 중 하나
- Stock role이 `LEADER` 또는 `CO_LEADER`
- Price location readiness가 `READY`
- 최신 tick, support, vwap 또는 recent support 준비 완료
- `gate_usable=true`
- 제한 수량이 1주 이상이고 지정가가 현재가 대비 slippage 제한 안에 있음

## 차단 조건

대표 차단 reason code:

- `PREFLIGHT_NOT_GO`, `PREFLIGHT_GO_WITH_WARNINGS_BLOCKED`, `PREFLIGHT_FAIL_CLOSED`
- `DRY_RUN_GO_NO_GO_NOT_GO`
- `HYBRID_STATUS_NOT_READY`, `HYBRID_POSITION_TIER_NOT_ALLOWED`
- `THEME_STATUS_NOT_ALLOWED`, `STOCK_ROLE_NOT_ALLOWED`
- `PRICE_LOCATION_NOT_READY`
- `LATEST_TICK_NOT_READY`, `SUPPORT_NOT_READY`, `VWAP_OR_RECENT_SUPPORT_NOT_READY`
- `GATE_USABLE_FALSE`, `TR_BACKFILL_ONLY_BLOCKED`
- `MARKET_RISK_OFF_BLOCKED`, `CHASE_RISK_BLOCKED`, `LATE_LAGGARD_BLOCKED`
- `LOW_BREADTH_BLOCKED`, `LEADER_ONLY_LAGGARD_BLOCKED`
- `ENTRY_RISK_TEMP_WAIT_BLOCKED`, `ENTRY_RISK_FINAL_BLOCKED`
- `LOAD_GUARD_NOT_OK`
- `MAX_ORDERS_PER_DAY_EXCEEDED`, `MAX_ORDERS_PER_CYCLE_EXCEEDED`
- `SAME_CODE_OPEN_ORDER_EXISTS`, `SAME_CODE_POSITION_EXISTS`
- `LIMIT_PRICE_INVALID`, `LIMIT_PRICE_SLIPPAGE_EXCEEDED`, `BLOCKED_QUANTITY_BELOW_MIN`

## 대시보드

Overview의 `LIVE_SIM Canary` 패널에서 확인한다.

- 설정 상태: disabled / observe-only / order-enabled
- 오늘 Preflight, Load Guard, DRY_RUN go/no-go
- eligible / blocked / submitted / filled 카운트
- 차단 사유 Top 10
- 최근 Canary 판단 목록
- 연결된 `order_intent_id`, `gateway_command_id`
- 원본 metadata JSON

대시보드는 주문 실행 버튼을 제공하지 않는다.

## API

```text
GET /api/runtime/live-sim/canary/summary
GET /api/runtime/live-sim/canary/decisions
GET /api/runtime/live-sim/canary/decisions/{decision_id}
POST /api/runtime/live-sim/canary/rebuild
```

`rebuild`는 local token이 필요하고 analysis-only다. 저장된 gate/runtime 데이터를 기준으로 판단을 재생성하지만 주문 intent나 Gateway `send_order`를 만들지 않는다.

## 롤백

즉시 롤백은 설정을 닫는 것이다.

```yaml
live_sim_hybrid_ready_canary:
  enabled: false
  order_enabled: false
```

더 강한 롤백이 필요하면 `order_execution.mode=DRY_RUN`, `live_sim_enabled=false`, kill switch active 상태로 되돌린다. 실계좌 관련 설정은 이 Canary와 별개이며 켜지 않는다.

## 안전 설명

Canary는 기존 `OrderEnqueueService.enqueue_live_sim_order`와 `LiveSimRuntimeOrderSink` 안전 경로를 재사용한다. 계좌 모드, LIVE_REAL 차단, 중복 주문, 미체결/포지션 차단, lifecycle/reconcile guard는 기존 LIVE_SIM 안전 장치가 다시 검증한다.
