# AGENTS.md

## 응답 규칙

- 어떤 답변을 하든 예상 꼬리 질문 3개를 함께 알려준다.
- 작업 중 진행 메시지에서는 꼬리 질문을 하지 않는다.

## 테스트 실행 정책

코드 변경 또는 PR 적용 후 테스트할 때는 전체 테스트를 무작정 실행하지 않는다.
빠른 피드백을 먼저 받고, 무거운 검증은 프로필과 shard로 나눠 실행한다.

기본 순서:

1. 변경한 파일과 직접 관련된 targeted test를 먼저 실행한다.
2. 그 다음 빠른 회귀 확인으로 quick 프로필을 실행한다.
3. timeout 위험이 있는 전체 검증은 full을 한 번에 돌리지 말고 shard로 나눠 실행한다.
4. slow/e2e 성격 검증은 필요할 때 `slow` 프로필로 별도 실행한다.

권장 명령:

```powershell
python tools/run_tests.py quick
python tools/run_tests.py slow
python tools/run_tests.py full --shard 1/4
python tools/run_tests.py full --shard 2/4
python tools/run_tests.py full --shard 3/4
python tools/run_tests.py full --shard 4/4
```

PR 적용 후 기본 검증:

- 작은 수정: 관련 targeted test + `python tools/run_tests.py quick`
- runtime/API/storage/dashboard 변경: 관련 targeted test + `python tools/run_tests.py quick` + 필요한 integration/slow 일부
- 릴리즈/머지 전 검증: `full`을 shard로 나눠 실행

테스트 결과를 보고할 때는 실행한 명령, 통과/실패 여부, 전체 테스트를 생략했다면 그 이유를 함께 남긴다.


## PR 운영 규칙

- 앞으로 모든 전략 PR에는 아래 항목이 있어야 합니다.

```text
Experiment ID:
Baseline version:
관측된 문제:
사용한 유효 거래일:
표본 수:
가설:
변경 변수 1개:
고정할 변수:
성공 기준:
실패 기준:
관찰 종료 조건:
롤백 방법:
주문 경로 영향:
```

추가 규칙은 다음과 같습니다.

- 동시에 활성화된 전략 실험은 1개
- 전략 변경 PR은 주당 최대 1개
- Entry와 Exit를 같은 PR에서 변경 금지
- 시장 Gate와 테마 Gate 동시 변경 금지
- 한 거래일 결과로 threshold 변경 금지
- 신규 Dashboard 패널보다 기존 패널 삭제 우선
- APPROVED_FOR_OBSERVE와 APPROVED_FOR_DRY_RUN 분리
- 자동 설정 적용은 계속 금지

현재 코드의 allow_auto_apply=False는 반드시 유지해야 합니다.