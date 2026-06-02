# Candidate Generation Config

This PR exposes candidate generation split policy for DRY_RUN attribution.
It does not enable LIVE orders, does not create Gateway `send_order` commands,
and does not change buy/sell thresholds.

## Environment Variables

| Variable | Default | Meaning |
| --- | ---: | --- |
| `TRADING_CANDIDATE_STALE_REDETECT_MINUTES` | `90` | Same `trade_date + code` is treated as a new generation after this many minutes since the previous signal. |
| `TRADING_CANDIDATE_NEW_GENERATION_ON_THEME_CHANGE` | `1` | Create a new generation when `theme_id` or primary theme changes. |
| `TRADING_CANDIDATE_NEW_GENERATION_ON_SOURCE_CHANGE` | `1` | Create a new generation when signal source changes. |
| `TRADING_CANDIDATE_NEW_GENERATION_ON_STRATEGY_CHANGE` | `1` | Create a new generation when strategy/profile changes. |
| `TRADING_CANDIDATE_NEW_GENERATION_AFTER_POSITION_CLOSED` | `1` | Allow a new generation when a previous lifecycle is explicitly marked closed/re-detected. |
| `TRADING_CANDIDATE_GENERATION_MIN_GAP_MINUTES` | `20` | Guardrail that blocks too-frequent generation splits. |
| `TRADING_CANDIDATE_MAX_GENERATION_PER_CODE_PER_DAY` | `5` | Guardrail that blocks excessive same-day generations for one code. |

## Generation Reasons

- `initial_generation`: first observed candidate instance.
- `same_generation`: repeated detection remains in the current generation.
- `stale_re_detected`: stale re-detect threshold was exceeded.
- `theme_changed`: primary theme changed and the config allows a split.
- `source_changed`: source changed and the config allows a split.
- `strategy_changed`: strategy/profile changed and the config allows a split.
- `previous_lifecycle_closed`: prior lifecycle was closed and the config allows a split.
- `manual_reset`: metadata requested a manual generation reset.
- `session_reset`: metadata requested a session reset.
- `same_generation_min_gap_guardrail`: a split was requested but blocked by min gap.
- `same_generation_max_generation_guardrail`: a split was requested but blocked by max generation per day.

## Attribution Policy

`candidate_instance_id` remains the primary DRY_RUN signal attribution key.
`virtual_position` accounting can still be code-netted, but performance attribution
uses candidate generation metadata to distinguish morning and afternoon signals.

Each candidate metadata payload includes:

- `generation_reason`
- `previous_candidate_instance_id`
- `previous_seen_at`
- `minutes_since_previous_signal`
- `blocked_generation_reason`
- `excessive_generation_blocked`
- `candidate_generation_config`

## Report Summary

DRY_RUN performance Markdown and JSON expose:

- `multi_generation_code_count`
- `avg_generation_per_code`
- `max_generation_per_code`
- `stale_re_detect_count`
- `theme_change_generation_count`
- `source_change_generation_count`
- `strategy_change_generation_count`
- `previous_lifecycle_closed_generation_count`
- `excessive_generation_count`

Runtime snapshots also expose `candidate_generation_summary` for dashboard use.

## Operating Notes

장초반에는 테마와 대장주가 빠르게 바뀌므로 너무 낮은 stale 기준은 같은 초기 변동을 여러 신호로 쪼갤 수 있다.
기본 90분과 min gap 20분은 DRY_RUN attribution 오염을 줄이는 보수적 값이다.

장중에는 테마 전환이 실제로 의미 있을 수 있으므로 `theme_changed` split은 유용하다.
다만 같은 종목이 여러 테마에 동시에 걸리는 날에는 generation이 과도하게 늘 수 있어 max per day guardrail을 유지한다.

장후반에는 재감지 신호가 시간 손절/청산 리뷰와 섞일 수 있다.
threshold 변경 전에는 `excessive_generation_count`와 attribution confidence를 먼저 확인한다.

## Known Limitations

- Existing `candidates` still uses `UNIQUE(trade_date, code)`, so database row identity is not split.
- The generation identity is additive metadata; historical rows without metadata remain low-confidence for attribution.
- `previous_lifecycle_closed` requires upstream metadata to mark the lifecycle closure reason.
