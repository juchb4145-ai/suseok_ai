# LIVE_SIM OrderManager Runbook

이 문서는 Strategy Reboot V2의 PR7 범위인 제한적 OrderManager 운용 절차다. 이 경로는 `LIVE_SIM` 전용이며 REAL broker/account 주문을 지원하지 않는다.

## 기본 원칙

- 기본값은 전부 차단이다.
- `OBSERVE`와 `DRY_RUN`에서는 Gateway 주문 command를 만들지 않는다.
- `LIVE_SIM`에서도 시뮬레이션 브로커 확인, 계좌 가드, 킬스위치, 주문 한도를 모두 통과해야 command를 만든다.
- real broker, production server, real account로 판정되면 즉시 `REAL_BROKER_BLOCKED`로 거절한다.
- `TRADING_ALLOW_REAL_LIVE_ORDERS` 같은 REAL 활성화 플래그는 만들지 않는다.
- 체결과 포지션 반영의 source of truth는 Chejan `execution_event`다. `command_ack`만으로 filled 처리하지 않는다.

## 필수 플래그

기본값:

```text
TRADING_ORDER_MANAGER_ENABLED=false
TRADING_ORDER_MANAGER_MODE=OBSERVE
TRADING_ALLOW_LIVE_SIM_ORDERS=false
TRADING_REQUIRE_SIMULATION_BROKER=true
TRADING_BLOCK_REAL_BROKER=true
TRADING_LIVE_SIM_ACCOUNT_WHITELIST=
TRADING_LIVE_SIM_MAX_ORDER_QUANTITY=1
TRADING_LIVE_SIM_MAX_ORDER_AMOUNT=100000
TRADING_LIVE_SIM_MAX_DAILY_BUY_ORDERS=3
TRADING_LIVE_SIM_MAX_DAILY_SELL_ORDERS=10
TRADING_LIVE_SIM_MAX_DAILY_ORDERS_PER_CODE=1
TRADING_LIVE_SIM_MAX_OPEN_POSITIONS=3
TRADING_LIVE_SIM_MAX_THEME_EXPOSURE_COUNT=2
TRADING_LIVE_SIM_CANCEL_UNFILLED_AFTER_SEC=45
TRADING_LIVE_SIM_ORDER_HOGA=00
TRADING_LIVE_SIM_USE_LIMIT_PRICE=true
TRADING_LIVE_SIM_ALLOW_MARKET_ORDER=false
TRADING_LIVE_SIM_DAILY_LOSS_LIMIT_PCT=-2.0
TRADING_LIVE_SIM_DAILY_LOSS_LIMIT_KRW=50000
TRADING_LIVE_SIM_CONSECUTIVE_LOSS_LIMIT=2
TRADING_LIVE_SIM_KILL_SWITCH_ENABLED=true
TRADING_ORDER_MANAGER_OBSERVE_ONLY=true
```

제한적 LIVE_SIM을 실제로 켤 때 필요한 최소 조합:

```text
TRADING_ORDER_MANAGER_ENABLED=true
TRADING_ORDER_MANAGER_MODE=LIVE_SIM
TRADING_ALLOW_LIVE_SIM_ORDERS=true
TRADING_ORDER_MANAGER_OBSERVE_ONLY=false
TRADING_REQUIRE_SIMULATION_BROKER=true
TRADING_BLOCK_REAL_BROKER=true
```

계좌 whitelist는 운영 환경에서 명시 권장이다. 비어 있으면 코드상 whitelist 제한은 적용하지 않지만, broker/account/orderable 가드는 유지된다.

## Broker Guard

OrderManager는 다음 조건을 모두 확인한다.

- `kiwoom_logged_in=true`
- `orderable=true`
- account 존재
- whitelist가 있으면 account 포함
- heartbeat 정상
- broker env가 `SIMULATION`, `MOCK`, `LIVE_SIM`, `SIM`, `PAPER`, `DEMO`, `server_gubun=1` 계열
- `REAL`, `PROD`, `PRODUCTION`, `LIVE_REAL`, `server_gubun=0` 계열은 차단
- command queue가 비정상적으로 밀려 있으면 차단

대표 reject reason:

```text
BROKER_NOT_LOGGED_IN
BROKER_NOT_ORDERABLE
ACCOUNT_NOT_CONFIGURED
ACCOUNT_NOT_WHITELISTED
BROKER_ENV_UNKNOWN
REAL_BROKER_BLOCKED
GATEWAY_HEARTBEAT_STALE
COMMAND_QUEUE_UNHEALTHY
LIVE_SIM_FLAG_DISABLED
ORDER_MANAGER_OBSERVE_ONLY
```

## Entry Buy Path

EntryEngine 결과만 사용한다. hybrid gate, final grade, shadow/promotion, threshold A/B 결과는 OrderManager 입력이 아니다.

BUY intent 조건:

- latest `entry_decisions.entry_status=OBSERVE_READY`
- `dry_run_intent_allowed=true` 또는 동등한 live-sim intent 허용 플래그
- mode `LIVE_SIM`
- broker simulation guard 통과
- market action `ALLOW_NORMAL` 또는 `ALLOW_REDUCED`
- stock role `LEADER` 또는 `CO_LEADER`
- price location `GOOD_PULLBACK`, `PULLBACK_RECLAIM`, `VWAP_RECLAIM`
- `OVERHEATED` 아님
- candidate state는 `WATCHING/READY/SETUP_READY/TIMING_READY` 계열
- PositionRisk stop-new-entry 미권고
- kill switch가 신규 BUY를 막지 않음
- 동일 코드 open position 또는 pending order 없음
- 일간 code/order/theme/position 한도 통과
- 수량 기본 1주, 최대 수량/금액 한도 통과
- limit price 우선: `limit_price_hint`, best ask/current price 순서

Idempotency key:

```text
reboot_live_sim_buy:{trade_date}:{candidate_id}:{code}:{entry_decision_id_or_cycle_bucket}
```

## Exit Sell Path

ExitEngine Reboot 결과만 사용한다.

SELL intent 조건:

- latest `exit_decisions_reboot.exit_status`가 `SCALE_OUT` 또는 `EXIT_NOW`
- position remaining quantity > 0
- source가 `LIVE_SIM`, `LIVE_SIM_OBSERVED`, `DRY_RUN_TO_LIVE_SIM` 계열
- broker simulation guard 통과
- stale/invalid price 아님
- duplicate sell intent 없음
- kill switch는 SELL/CANCEL을 기본 허용
- `SCALE_OUT`은 decision quantity, `EXIT_NOW`는 remaining quantity

Idempotency key:

```text
reboot_live_sim_sell:{trade_date}:{position_id}:{exit_reason}:{exit_bucket}
```

## Persistence And Command Queue

순서는 고정이다.

```text
ManagedOrderIntent 저장
-> OrderRiskDecision 저장
-> risk PASS면 ManagedOrder 저장
-> GatewayCommand(send_order/cancel_order) 생성
-> command queue enqueue
```

Risk reject, observe-only, dry-run mode, real broker block에서는 Gateway command를 만들지 않는다.

SQLite 테이블:

```text
managed_order_intents
managed_orders
managed_order_events
order_risk_decisions
order_kill_switch_state
```

PostgreSQL 의존성은 없다.

## Ack, Chejan, Reconcile

`command_ack` 처리:

- ACKED: `ACKED_BY_GATEWAY`
- REJECTED/FAILED/result_code != 0: `REJECTED_BY_GATEWAY`
- order_no가 있으면 저장
- command_id/idempotency로 managed order 매칭
- ack만으로 fill 처리하지 않음

`execution_event` 처리:

- order_no 우선 매칭
- fallback: command_id, idempotency, code/side
- full fill: `FILLED`
- partial fill: `PARTIALLY_FILLED`
- buy fill: LIVE_SIM position open/update
- sell fill: LIVE_SIM position reduce/close
- unmatched execution: `RECONCILE_REQUIRED`

## Unfilled Cancel

대상:

- `ACKED_BY_GATEWAY` 또는 `PARTIALLY_FILLED`
- remaining quantity > 0
- age > `TRADING_LIVE_SIM_CANCEL_UNFILLED_AFTER_SEC`
- original order_no 존재
- cancel 중복 없음
- broker simulation guard 통과

Cancel idempotency:

```text
reboot_live_sim_cancel:{trade_date}:{order_id}:{original_order_no}
```

## Kill Switch

상태:

```text
NORMAL
STOP_NEW_BUY
REDUCE_ONLY
KILL_SWITCH_ACTIVE
```

트리거:

- daily realized loss pct/krw limit
- consecutive loss limit
- position risk `kill_switch_recommended`
- position risk `stop_new_entry_recommended`
- manual switch
- reject/queue/heartbeat storm은 후속 PR에서 더 세분화 가능

정책:

- BUY는 kill switch 상태에 따라 차단
- SELL/CANCEL은 위험 축소 경로로 기본 허용
- 상태는 SQLite에 저장해 재시작 후에도 유지한다.

## Dashboard

최종 dashboard 방향:

- market regime
- leading theme TOP5
- READY candidates
- position risk
- order manager status
- top wait/block reasons

OrderManager section 필드:

```text
mode
enabled
live_sim_orders_allowed
broker_env
account
account_whitelisted
risk_state
kill_switch_state
today_buy_order_count
today_sell_order_count
open_order_count
pending_cancel_count
rejected_order_count
last_order_at
last_reject_reason
warnings
managed_orders
```

금지 표현:

```text
real account order
real trading active
profit guaranteed
```

## 검증 명령

```bash
python -m pytest tests/test_order_manager_live_sim.py -q
python -m pytest tests/test_unfilled_cancel_scheduler.py -q
python -m pytest tests/test_chejan_reconcile.py -q
python -m pytest tests/test_core_runtime_api.py -q
python -m pytest -q
```
