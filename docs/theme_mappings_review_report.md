# theme_mappings.csv 생성 검토 리포트

- 원본 파일: `theme_mappings_auto.csv`
- 생성 파일: `theme_mappings.csv`
- 총 행 수: 909
- 고유 종목 수: 647
- 고유 테마 수: 142
- enabled=1: 859
- enabled=0: 50

## 시장별 행 수
- KOSDAQ: 489
- KOSPI: 420

## 프로필별 행 수
- KOSDAQ_THEME_PROFILE: 489
- KOSPI_LEADER_PROFILE: 418
- SEMICONDUCTOR_SIGNAL_PROFILE: 2

## 비활성 처리한 테마
- 코스닥_라이징스타: 27행
- 코스닥_히든챔피언: 23행

## enabled 테마 상위 20개
- kiwoom_430 / 증권: 19행
- kiwoom_515 / 스마트폰_삼성전자관련주: 17행
- kiwoom_456 / 모바일솔루션: 15행
- kiwoom_470 / 게임_온라인: 15행
- kiwoom_141 / 2차전지_소재(양극화물질등): 14행
- kiwoom_245 / 그린카_하이브리드카/전기차: 13행
- kiwoom_270 / 교육: 13행
- kiwoom_500 / PCB(인쇄회로기판): 13행
- kiwoom_210 / 기계_건설기계: 12행
- kiwoom_211 / 기계_공작기계: 12행
- kiwoom_282 / 컨텐츠_영상: 12행
- kiwoom_454 / 전자결제: 12행
- kiwoom_810 / 스마트 그리드: 12행
- kiwoom_820 / 엔젤산업: 12행
- kiwoom_910 / 중국_내수소비 확대: 12행
- kiwoom_551 / 반도체_설계(fabless): 11행
- kiwoom_212 / 방위산업: 10행
- kiwoom_241 / 자동차_전장화 수혜: 10행
- kiwoom_313 / 배합사료: 10행
- kiwoom_330 / 화장품: 10행

## 적용 규칙
- `005930`, `000660`은 `SEMICONDUCTOR_SIGNAL_PROFILE`, `is_signal_stock=1`, `base_priority=100`으로 보정.
- `KOSPI` 종목은 기본 `KOSPI_LEADER_PROFILE`, `is_large_cap=1`, 최소 `base_priority=70`.
- `KOSDAQ` 종목은 기본 `KOSDAQ_THEME_PROFILE`, 최소 `base_priority=60`.
- `is_leader_candidate=1`인 종목은 최소 `base_priority=80`.
- `코스닥_라이징스타`, `코스닥_히든챔피언`은 실제 테마/업종이 아닌 분류성 목록으로 보고 `enabled=0`.
- 그 외 market이 정상인 테마 매핑은 OBSERVE coverage 확보를 위해 `enabled=1`.
