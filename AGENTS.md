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
