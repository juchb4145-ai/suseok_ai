# Naver Theme CSV Draft Generator

`scripts/generate_naver_theme_mappings.py`는 Naver Finance 테마 페이지를 읽어 검수용
`theme_mappings_auto.csv` 초안을 만든다. 이 스크립트는 DB import를 실행하지 않고,
OBSERVE runtime 또는 UI refresh에서도 자동 실행되지 않는다.

## 실행 예시

기본 실행은 전일대비 상승률과 최근 3일 등락률 순위를 모두 수집해 merge한다.

```powershell
.\venv_32\Scripts\python.exe .\scripts\generate_naver_theme_mappings.py `
  --output data\theme_mappings_auto.csv `
  --max-themes 80 `
  --request-delay-ms 1200 `
  --overwrite
```

특정 정렬 기준만 사용할 수도 있다.

```powershell
.\venv_32\Scripts\python.exe .\scripts\generate_naver_theme_mappings.py `
  --ranking-source recent_3days `
  --output data\theme_mappings_auto.csv `
  --overwrite
```

테마 키워드로 범위를 좁힐 수 있다.

```powershell
.\venv_32\Scripts\python.exe .\scripts\generate_naver_theme_mappings.py `
  --include-keywords "반도체,로봇,전력" `
  --max-themes 40 `
  --output data\theme_mappings_auto.csv `
  --overwrite
```

## 검수 포인트

- 기본 `enabled`는 `0`이다. 운영할 row만 검수 후 `1`로 바꾼다.
- `market=UNKNOWN` row는 항상 `enabled=0`이며, import 전에 `KOSPI` 또는 `KOSDAQ`으로 보정해야 한다.
- `strategy_profile`, `base_priority`, `is_leader_candidate`, `is_signal_stock`은 자동 추론값이므로 운영자가 최종 확인한다.
- 같은 종목이 여러 테마에 등장하는 것은 정상이다. 중복 제거 기준은 `(code, theme_id)`이다.
- `memo`에는 Naver 테마 번호, 전일대비 상승률, 최근 3일 등락률, rank, 생성 시각이 들어간다. 이 값은 검수 보조값이며 자동 enable 기준이 아니다.

## DB 반영 절차

1. `data/theme_mappings_auto.csv`를 검수한다.
2. 승인한 결과를 `data/theme_mappings.csv`로 저장한다.
3. 기존 import 함수를 실행한다.

```powershell
.\venv_32\Scripts\python.exe -c "from storage.db import TradingDatabase; from trading.strategy.themes import import_theme_mappings_csv; db=TradingDatabase('data/trader.sqlite3'); result=import_theme_mappings_csv(db, 'data/theme_mappings.csv'); print(result); db.close()"
```

4. OBSERVE 재시작 후 readiness summary에서 mapped/unmapped coverage를 확인한다.
