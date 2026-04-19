# Architecture Overview

> 최종 업데이트: 2026-04-19
> 상태: 초기 설계 단계 (미구현)

---

## 시스템 목적

ETF 및 시장 분석 리포트를 자동 생성하여 Slack으로 저비용 전달.

---

## 컴포넌트 (미확정, 설계 예정)

```
[데이터 수집]  →  [분석/신호 생성]  →  [리포트 포맷팅]  →  [Slack 전송]
     ↓
[캐시 레이어]
```

| 컴포넌트 | 역할 | 상태 | 파일 경로 |
|----------|------|------|-----------|
| Data Fetcher | Yahoo Finance / FRED 데이터 수집 | 미구현 | `src/fetcher.py` (예정) |
| Analyzer | ETF 지표 계산, 신호 생성 | 미구현 | `src/analyzer.py` (예정) |
| Report Builder | Slack 메시지 포맷팅 | 미구현 | `src/reporter.py` (예정) |
| Slack Sender | Webhook으로 전송 | 미구현 | `src/slack.py` (예정) |
| Scheduler | 정기 실행 | 미결정 | TBD |

---

## 데이터 소스 후보

| 소스 | 비용 | 용도 |
|------|------|------|
| Yahoo Finance (`yfinance`) | 무료 | ETF 가격, 거래량 |
| FRED API | 무료 | 매크로 지표 (금리, CPI 등) |
| FinViz | 무료 (스크래핑) | ETF 스크리닝 |

---

## 미결 사항

- [ ] 스케줄러: cron vs Airflow vs APScheduler 선택
- [ ] 캐시: 로컬 파일 vs Redis vs SQLite
- [ ] 리포트 주기: 일간 / 주간 / 이벤트 트리거
