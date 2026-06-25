# 테스트 실행 전략

테스트 수가 늘어나면서 전체 `pytest`를 매번 한 번에 실행하면 timeout이 쉽게 납니다.
이 저장소는 기본 전체 실행 의미는 유지하되, 빠른 피드백과 무거운 검증을 분리합니다.

## 프로필

```powershell
python tools/run_tests.py quick
python tools/run_tests.py unit
python tools/run_tests.py integration
python tools/run_tests.py slow
python tools/run_tests.py full
```

- `quick`: 기본 개발/PR 확인용. 빠른 unit smoke만 실행하고 integration, slow, e2e 성격 테스트는 제외합니다.
- `unit`: 빠른 단위 테스트만 실행합니다.
- `integration`: API, runtime, storage, gateway, dashboard 같은 경계를 건드리되 `slow`, `e2e`는 제외합니다.
- `slow`: wall-clock을 크게 잡아먹는 테스트와 e2e 성격 테스트를 실행합니다.
- `full`: 전체 테스트를 실행합니다.

pytest를 직접 호출해도 됩니다.

```powershell
python -m pytest --profile=quick
python -m pytest --profile=slow
python -m pytest
```

## 전체 테스트 샤딩

전체 테스트가 timeout에 걸릴 때는 같은 프로필을 여러 shard로 나눠 실행합니다.

```powershell
python tools/run_tests.py full --shard 1/4
python tools/run_tests.py full --shard 2/4
python tools/run_tests.py full --shard 3/4
python tools/run_tests.py full --shard 4/4
```

`--shard=N/M`은 선택된 테스트 목록을 deterministic하게 M개로 나누고 N번째 조각만 실행합니다.
CI에서는 각 shard를 별도 job으로 두면 전체 wall-clock timeout을 줄일 수 있습니다.

## 느린 테스트 추적

`pytest.ini`는 기본적으로 가장 느린 테스트 20개를 출력합니다.
더 넓게 확인하려면 아래처럼 실행합니다.

```powershell
python tools/run_tests.py quick --durations 50
python tools/run_tests.py slow --durations 50
```

새로 추가한 테스트가 시간이 많이 걸리거나 DB/API/CLI/report/replay 성격이면 `tests/conftest.py`의
`SLOW_TEST_FILES` 또는 `INTEGRATION_FILE_KEYWORDS`에 추가해서 quick 프로필에서 제외합니다.
또한 8KB 이상 테스트 파일은 기본적으로 slow로 분류합니다.
반대로 빠른 테스트가 slow에 잘못 들어갔다면 `FAST_TEST_FILES`에 추가하면 됩니다.
