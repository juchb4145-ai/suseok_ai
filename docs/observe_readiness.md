# OBSERVE Readiness 운영 절차

Phase 2 OBSERVE를 시작하기 전에 condition profile, theme mapping, active 후보 coverage,
protected subscription 여유를 확인한다. 이 문서는 운영 준비용이며 `main.py` CLI 옵션을
추가하지 않는다.

## Theme Mapping 초안 생성

운영 테마 데이터는 검수 후 `data/theme_mappings.csv`로 저장하고 기존 import 함수로 DB에 반영한다.
자동 생성 파일은 초안이며 그대로 DB에 넣는 파일이 아니다.

### Kiwoom TR 기반 초안

```powershell
.\venv_32\Scripts\python.exe scripts\generate_theme_mappings.py --output data\theme_mappings_auto.csv --overwrite
```

Kiwoom 경로는 `OPT90001` 테마그룹별요청과 `OPT90002` 테마구성종목요청을 사용한다.

### Naver Finance 기반 초안

```powershell
.\venv_32\Scripts\python.exe scripts\generate_naver_theme_mappings.py --output data\theme_mappings_auto.csv --max-themes 80 --overwrite
```

기본값은 전일대비 상승률과 최근 3일 등락률 정렬 페이지를 모두 수집해 merge한다.
특정 기준만 사용하려면 `--ranking-source change_rate` 또는 `--ranking-source recent_3days`를 사용한다.
특정 테마만 좁히려면 `--include-keywords "반도체,로봇,전력"`을 사용한다.

Naver 초안의 전일대비 상승률과 최근 3일 등락률은 `memo`에 저장되는 검수 보조값이다.
이 값만으로 `enabled=1`을 자동 결정하지 않는다.

## theme_mappings.csv 컬럼

필수 컬럼:

- `code`: `A005930`은 `005930`으로 정규화되며 6자리 숫자여야 한다.
  스프레드시트 저장 과정에서 `005930`이 `5930`처럼 바뀐 경우 import가 6자리로 left-pad한다.
  가능하면 CSV 편집 시 code 컬럼은 텍스트 형식으로 유지한다.
- `name`
- `market`: `KOSPI` 또는 `KOSDAQ`.
- `theme_id`
- `theme_name`
- `strategy_profile`: `KOSDAQ_THEME_PROFILE`, `KOSPI_LEADER_PROFILE`, `SEMICONDUCTOR_SIGNAL_PROFILE`, `THEME_DISCOVERY_PROFILE`.
- `enabled`: `1/0`, `true/false`, `Y/N`.

선택 컬럼:

- `sub_theme`
- `is_large_cap`
- `is_leader_candidate`
- `base_priority`: 0부터 100.
- `is_signal_stock`
- `memo`

같은 종목이 여러 테마에 포함되는 것은 정상이다. 중복 제거와 DB unique 기준은 `(code, theme_id)`이다.
같은 `code`와 같은 `theme_id`만 중복으로 본다.

`enabled=0` row는 DB에 저장될 수 있지만 candidate enrich와 Gate 계산에서는 제외된다.
`market=UNKNOWN` row는 import 전에 `KOSPI` 또는 `KOSDAQ`으로 보정해야 한다.

## CSV Import 실행

```powershell
.\venv_32\Scripts\python.exe -c "from storage.db import TradingDatabase; from trading.strategy.themes import import_theme_mappings_csv; db=TradingDatabase('data/trader.sqlite3'); result=import_theme_mappings_csv(db, 'data/theme_mappings.csv'); print(result); db.close()"
```

`ThemeImportResult.errors`가 비어 있어야 정상이다. 오류가 있는 row는 저장하지 않고 `skipped`로 집계된다.

## Condition Profiles 확인

OBSERVE runtime 생성 시 필수 condition profile 3개 중 없는 항목만 seed한다.

- `코스닥_테마주_눌림`
- `코스피_대형주_주도`
- `주도테마_넓은후보`

확인 예시:

```powershell
.\venv_32\Scripts\python.exe -c "from storage.db import TradingDatabase; db=TradingDatabase('data/trader.sqlite3'); print([(p.condition_name, p.strategy_profile.value, p.purpose, p.enabled) for p in db.list_condition_profiles(enabled=None)]); db.close()"
```

기존 정상 row는 덮어쓰지 않는다. purpose 누락, 알 수 없는 purpose, 한글 깨짐 의심 이름은 readiness warning에 남긴다.

## OBSERVE 시작 전 확인

OBSERVE 탭 또는 설정 탭의 readiness summary에서 다음을 확인한다.

- condition profile 수와 unresolved 수.
- theme mapping 전체 수와 enabled 수.
- active 후보 수, mapped/unmapped 후보 수, coverage.
- protected subscription usage.
- startup warnings.

후보 테이블 수동 새로고침은 read-only 조회만 수행해야 하며 runtime cycle, candidate mutation,
review 생성, order 생성은 호출하지 않는다.

## Warning별 조치

- `THEME_MAPPING_EMPTY`: 자동 초안을 생성하고 검수 후 `theme_mappings.csv`로 저장한 뒤 import한다.
- `NO_THEME_MAPPING_FOR_ACTIVE_CANDIDATES`: active 후보 코드가 CSV에 있는지, `enabled=1`인지, code가 6자리인지 확인한다.
- `NO_THEME_MAPPING_FOR_CANDIDATE`: 해당 후보 코드의 enabled mapping을 추가하거나 disabled 상태를 확인한다.
- `CONDITION_PROFILE_UNRESOLVED`: 실제 조건식 목록과 condition profile 이름이 일치하는지 확인한다.
- `INDEX_DATA_INSUFFICIENT`: 지수 실시간 구독과 index data 수신 상태를 확인한다.
- `INDICATOR_DATA_INSUFFICIENT`: 해당 후보의 tick/candle 누적 상태를 확인한다.
- `BROAD_CANDIDATES_ONLY`: broad 후보만 있고 pullback/leader 후보 조건식이 없는 상태이므로 조건식 구성을 확인한다.

## theme_mappings=0 해결 절차

1. Kiwoom 또는 Naver 자동 생성기로 `data/theme_mappings_auto.csv` 초안을 만든다.
2. `enabled`, `market`, `strategy_profile`, priority, leader, signal 값을 검수한다.
3. 검수본을 `data/theme_mappings.csv`로 저장한다.
4. 기존 import one-liner로 DB에 반영한다.
5. OBSERVE를 재시작하고 readiness summary에서 coverage와 startup warnings를 확인한다.
