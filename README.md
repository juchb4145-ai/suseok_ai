# 키움 눌림목 반자동 매매

키움 OpenAPI+ ActiveX를 사용하는 PyQt5 기반 데스크톱 반자동 매매 프로그램입니다.

## 실행 환경

- Windows
- Kiwoom OpenAPI+ 설치 및 OCX 등록
- 32bit Python `3.9.13`
- 권장 패키지 설치:

```powershell
python -m pip install -r requirements.txt
```

## 실행

키움 OpenAPI 연결:

```powershell
python main.py
```

키움 없이 UI와 로직 테스트:

```powershell
python main.py --mock
```

## 주요 동작

- 앱 내부 `매수종목` 목록 관리
- 종목별 1차/2차/3차 목표매수가와 비중 설정
- 현재가가 목표가 N틱 이내 접근 시 지정가 매수
- 평균매수가 대비 기본 5% 수익 도달 시 보유수량의 기본 70% 지정가 매도
- 손절가는 자동매도가 아니라 알림으로 처리
- 실시간 시세 기반 감시로 TR 반복조회 최소화

## 주의

실거래 주문 잠금은 두지 않았습니다. 앱 상단의 접속 모드와 `주문 가능` 체크 상태를 반드시 확인한 뒤 사용하세요.
