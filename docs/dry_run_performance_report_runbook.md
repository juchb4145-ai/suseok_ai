# DRY_RUN Performance Report Runbook

## Purpose

PR-7 adds an analysis layer for DRY_RUN order intent data. It does not enable LIVE orders and does not create Gateway `send_order` commands. The report answers: if the strategy had actually placed the DRY_RUN entry and exit orders, what kind of result would have appeared?

## Lifecycle Linking

The analyzer links `runtime_order_intents` into a trade lifecycle using this priority:

1. Same `virtual_position_id`
2. Same `virtual_order_id`, upgraded to the matching virtual position when one exists
3. Same `trade_review_id`
4. Same `candidate_id + code + trade_date`
5. Otherwise `orphan_entry` or `orphan_exit`

Each lifecycle can contain one or more entry/buy intents, multiple exit/sell intents, one virtual position, exit decisions, and one latest trade review.
The analyzer also joins matching `hybrid_gate_validation_events` by trade date/code and candidate instance metadata when available, so grouped performance can be reviewed by `hybrid_status`, `hybrid_position_tier`, reason codes, theme score bucket, membership score bucket, session bucket, and stock role.

## Joined Data

The report reads:

- `runtime_order_intents`: entry/buy, exit/sell, safety/live safety, idempotency, score metadata
- `trade_reviews`: final status, max return/drawdown windows, existing false-positive/false-negative flags
- `virtual_positions`: realized return, hold time, max return/drawdown
- `exit_decisions`: TAKE_PROFIT, SUPPORT_LOSS, TIME_EXIT, TRAILING_STOP context
- `hybrid_gate_validation_events`: hybrid status, position tier, reason codes, theme/membership scores, stock role
- `gateway_price_ticks`: delayed entry price, limit-hit, spread, and liquidity diagnostics when tick data is available

The existing `trade_reviews` flags are preserved. DRY_RUN false signal fields are reported separately because an intent-level diagnosis can differ from a review-level diagnosis.

## Cost, Slippage, And Delay

The report is still review-only, but it now computes cost-adjusted return fields:

- `net_return_pct`: primary scenario net return after buy/sell commission, sell tax, and entry/exit slippage
- `net_expectancy`: average primary net return for completed accepted entry lifecycles
- `cost_scenario_expectancy`: matrix for slippage `0,10,20,30` bp and entry delay `0,1,3,5` seconds by default
- `net_bad_ready_type`: READY lifecycle that becomes a bad-ready case after costs
- `net_opportunity_type`: WAIT/BLOCKED/OBSERVE or rejected lifecycle that still has positive net opportunity after costs

Delayed entry uses the first stored `gateway_price_ticks` row at or after `entry_created_at + delay_sec`. If tick data is missing, the delayed scenario is marked with no sample rather than inferred.

## Execution Realism

Each lifecycle includes:

- `limit_price_hit`
- `partial_fill_risk`
- `spread_risk`
- `liquidity_bucket`
- `entry_tick_age_sec`
- `gateway_command_latency_ms`

The summary reports hit rate, high partial-fill/spread risk rates, stale tick rate, latency risk rate, and liquidity buckets. Missing ticks are kept visible as data-quality limitations.

## Go/No-Go

The `summary.go_no_go` block is review-only and uses these criteria:

- at least 5 trade days
- at least 30 accepted entry lifecycles
- cost/slippage adjusted `net_expectancy > 0`
- `bad_ready_rate` within the configured limit
- cost-adjusted opportunity loss within the configured limit
- stale tick and gateway latency distortion within configured limits

Failing any criterion yields `NO_GO`; insufficient trade days or accepted entries also marks readiness as `INSUFFICIENT_DATA`.

## False Positive

DRY_RUN false positive means an entry intent existed and passed decision safety, but the outcome was weak or harmful. Default thresholds are configurable.

- `LIVE_WOULD_PASS_BUT_SUPPORT_LOSS`
- `LIVE_WOULD_PASS_BUT_DRAWDOWN`
- `LIVE_WOULD_PASS_BUT_NEGATIVE_RETURN`
- `DRY_RUN_ACCEPTED_BUT_NO_EXIT_AND_DRAWDOWN`
- `ENTRY_ACCEPTED_BUT_TIME_EXIT_WEAK`
- `LATE_CHASE_FALSE_POSITIVE`

## False Negative / Opportunity Loss

DRY_RUN false negative means an order was rejected or absent, but later return metrics show a missed opportunity.

- `LIVE_REJECTED_BUT_RALLIED`
- `DRY_RUN_REJECTED_BUT_RALLIED`
- `GATE_BLOCKED_BUT_RALLIED`
- `EXPIRED_BUT_RALLIED`
- `NO_ENTRY_INTENT_BUT_RALLIED`
- `SAFETY_REJECT_REASON_OPPORTUNITY_LOSS`

`live_would_pass` and `live_would_reject` compare the DRY_RUN decision with the LIVE safety guard result. A rejected-live order that later rallied is useful for diagnosing operational blockers such as gateway offline, orderable=false, or account configuration.

## Data Quality

The report includes counts and samples for:

- Entry intent without trade review
- Entry intent without virtual position
- Exit intent without entry intent
- Exit intent without exit decision
- Trade review without DRY_RUN entry intent
- Missing price or quantity
- Missing live safety / decision safety
- Missing horizon metrics
- Stale open position

## API

- `GET /api/runtime/performance/dry-run`
- `POST /api/runtime/performance/dry-run/rebuild?trade_date=YYYY-MM-DD&persist=true`
- `GET /api/runtime/performance/dry-run/reports`
- `GET /api/runtime/performance/dry-run/reports/{report_id}`
- `GET /api/runtime/performance/dry-run/export?trade_date=YYYY-MM-DD&format=json|csv|md|all`
- `GET /api/runtime/performance/dry-run/false-signals?type=false_positive|false_negative|opportunity_loss|all`

`rebuild` and `export` require the local token because they create stored reports or files.

## 대시보드

PR-10은 대시보드에 페이지네이션 기반 상세 탐색을 추가한다.

- **DRY_RUN 주문 의도 목록**: entry/buy와 exit/sell 의도 확인
- **DRY_RUN 성과 사례**: 라이프사이클 단위 성과 행 확인
- **오탐/미탐 신호**: false positive, false negative, opportunity loss 행 확인

`trade_date`, `code`, `theme_name`, `strategy_name`, `side`, `order_phase` 필터로 범위를 좁힌다. 행을 클릭하면 핵심 필드와 원본 JSON을 보여주는 상세 패널이 열린다. 요약 카드는 계속 `/ws/dashboard`로 갱신되고, 표 데이터는 각 REST API에서 독립적으로 가져온다.

## Export Location

Generated files are written under:

```text
reports/dry_run_performance/<trade_date>/
```

Examples:

- `dry_run_performance_2026-05-30.json`
- `dry_run_performance_2026-05-30.csv`
- `dry_run_performance_2026-05-30.md`

## Recommendations

Recommendations are intentionally phrased as review prompts, not automatic parameter changes. Examples:

- LATE_CHASE false positives: review chase-risk penalty
- LOW_BREADTH false negatives: consider WATCH instead of hard block
- Rejected-live rallies: inspect SafetyGuard/Gateway availability
- High SUPPORT_LOSS count: revisit entry price and stop policy

## Threshold A/B 제안으로 이어지는 흐름

PR-12는 이 성과 리포트의 lifecycle 데이터를 다시 사용해 게이트/리스크 기준 후보를 만든다.

- FP가 많은 `LATE_CHASE`, `CHASE_RISK`, `LATE_LAGGARD`는 차단/감점 강화 후보가 될 수 있다.
- 막았지만 상승한 `LOW_BREADTH`는 테마 점수가 충분할 때 WATCH 허용 후보가 될 수 있다.
- `theme_score`, `hybrid_score`, `gate_score` 구간별 성과 차이가 크면 최소 기준 조정 후보가 될 수 있다.
- `live_safety`로 막혔지만 상승한 사례는 안전장치 완화가 아니라 운영 상태 점검 후보로 분류한다.

자세한 사용법은 [DRY_RUN 기준 A/B 제안 Runbook](dry_run_threshold_ab_runbook.md)을 참고한다. A/B 제안은 실제 설정을 자동 변경하지 않는다.

## Safety

This is an analysis-only PR. It never enables LIVE automation, never calls Kiwoom, and never enqueues Gateway `send_order`.
