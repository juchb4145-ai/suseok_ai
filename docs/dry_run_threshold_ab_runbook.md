# DRY_RUN 기준 A/B 제안 Runbook

## 목적

이 리포트는 DRY_RUN 성과 리포트에 쌓인 lifecycle 데이터를 다시 읽어 “게이트/리스크 기준을 이렇게 바꿔보면 어떨까”를 제안한다. 실제 전략 설정을 바꾸지 않고, 같은 표본에 기존 기준과 후보 기준을 사후 적용해 차이를 계산한다.

중요한 안전선:

- LIVE 자동주문을 켜지 않는다.
- Gateway `send_order` 명령을 만들지 않는다.
- `strategy_runtime_settings`를 자동 수정하지 않는다.
- 추천값은 OBSERVE/DRY_RUN 검증 후보로만 저장한다.

## 용어

- FP(False Positive, 오탐): 진입했지만 손실이 나거나 위험이 컸던 신호.
- FN(False Negative, 미탐): 막았지만 이후 상승한 신호.
- Opportunity Loss(기회손실): 안전장치나 게이트가 막았는데 뒤늦게 상승해 놓친 기회.
- Baseline(기존 기준): 현재 DRY_RUN 결과.
- Candidate(후보 기준): 점수 최소값 상향, 특정 사유 차단, 특정 사유 완화 같은 가상의 변경안.
- A/B 비교: 같은 lifecycle 표본에 기존 기준과 후보 기준을 각각 적용해 결과 차이를 보는 방식.

## 후보 종류

- 게이트 후보: `hybrid_score`, `gate_score`, `LOW_BREADTH` 같은 게이트 조건.
- 리스크 후보: `LATE_CHASE`, `CHASE_RISK`, `LATE_LAGGARD` 같은 추격매수 위험 사유.
- 테마 후보: `theme_score` 구간별 허용/차단 기준.
- 시간대 후보: `session_bucket`별 기준 강화 또는 관찰.
- 안전장치 후보: `live_safety`에서 막혔지만 이후 상승한 사례. 이 경우 자동 완화가 아니라 운영 상태 점검 후보로만 본다.

## 추천 등급

- 강한 후보: 표본이 충분하고 FP 감소가 크며 FN/기회손실 증가가 작다.
- 관찰 후보: 방향은 좋아 보이지만 표본 부족 또는 리스크가 있다.
- 위험 후보: FP는 줄지만 FN/기회손실이 늘 가능성이 크다.
- 데이터 부족: 표본 수가 부족해 판단을 보류한다.
- 적용 비추천: 후보 기준이 사후 지표를 악화시킨다.

## A/B 계산 방식

후보가 `block` 성격이면 해당 조건에 걸린 lifecycle을 후보 기준에서 차단했다고 가정한다. 후보가 `allow` 성격이면 기존에 막혔지만 상승한 lifecycle을 후보 기준에서 허용했다고 가정한다.

핵심 지표:

- `avoided_false_positive_count`: 후보 기준으로 줄일 수 있었던 오탐 수.
- `newly_created_false_negative_count`: 후보 기준 때문에 새로 놓칠 수 있는 좋은 신호 수.
- `opportunity_loss_delta`: 기회손실 증가/감소 추정.
- `win_rate_delta`: 후보 기준의 승률 변화.
- `avg_realized_return_delta`: 평균 실현수익률 변화.
- `expected_net_benefit_score`: 위 지표를 보수적으로 합친 참고 점수.

## API

조회:

```text
GET /api/runtime/threshold-ab/dry-run
```

주요 필터:

- `trade_date`
- `strategy_name`
- `code`
- `theme_name`
- `session_bucket`
- `category`
- `recommendation_grade`
- `parameter_name`
- `min_sample_count`
- `include_risky`
- `limit`
- `offset`

리포트 재생성/export:

```text
POST /api/runtime/threshold-ab/dry-run/rebuild?trade_date=YYYY-MM-DD&persist=true&export=true&format=all
```

저장 리포트:

```text
GET /api/runtime/threshold-ab/dry-run/reports
GET /api/runtime/threshold-ab/dry-run/reports/{report_id}
GET /api/runtime/threshold-ab/dry-run/candidates/{candidate_id}
```

`rebuild`와 `export`는 local token이 필요하다.

## Export 위치

```text
reports/dry_run_threshold_ab/<trade_date>/
```

파일:

- `dry_run_threshold_ab_<trade_date>.json`
- `dry_run_threshold_ab_<trade_date>.csv`
- `dry_run_threshold_ab_<trade_date>.md`

Markdown 리포트는 한글 중심으로 요약, 추천 Top 10, 후보 상세, 실제 적용 전 확인사항을 담는다.

## 대시보드 확인

대시보드에는 두 영역이 추가된다.

- **DRY_RUN 기준 제안**: 후보 수, 강한 후보/관찰 후보/위험 후보, 예상 FP 감소, 예상 FN 증가, 예상 기회손실 변화를 보여준다.
- **게이트/리스크 A/B 후보**: 후보별 기준명, 기존값, 후보값, 추천등급, 기대효과, 예상리스크를 페이지네이션 표로 확인한다.

행을 클릭하면 후보 상세, 영향받은 lifecycle 샘플, 추천 이유를 볼 수 있다. 화면에는 “실제 적용 아님”을 명시한다.

## 실제 적용 전 확인사항

1. 같은 후보가 여러 거래일에서 반복되는지 확인한다.
2. FP 감소보다 FN/기회손실 증가가 큰지 확인한다.
3. safety 관련 후보는 자동 완화하지 말고 Gateway/계좌/장상태 운영 문제인지 먼저 점검한다.
4. 적용은 별도 승인 PR에서 `strategy_runtime_settings` 변경, DRY_RUN 재검증, 롤백 계획까지 포함해 진행한다.
