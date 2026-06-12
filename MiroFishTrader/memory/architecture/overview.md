# Architecture Overview

> 최종 업데이트: 2026-06-11
> 상태: 초기 설계 단계 (미구현)

---

## 시스템 목적

MiroFish 오픈소스로 도출한 투자 인사이트를 바탕으로 보고서를 생성하고, **매일 오전 Slack 또는 Gmail로 전달**.

**투자 인사이트 2가지 목표**
1. 신규 종목 발견
2. 대중의 추세 파악

**투자 대상 시장**: Polymarket(예측시장), ETF 시장

---

## 컴포넌트 (미확정, 설계 예정)

```
[MiroFish 인사이트]  ┐
[시장/매크로 데이터]  ┼→ [분석/신호 생성] → [리포트 빌더] → [전달: Slack / Gmail]
[Polymarket 데이터]  ┘         ↑
                          [캐시 레이어]
                          ↑
                    [매일 오전 스케줄러]
```

| 컴포넌트 | 역할 | 상태 | 파일 경로 |
|----------|------|------|-----------|
| MiroFish Insight Source | 멀티에이전트 시뮬레이션으로 대중 추세 인사이트 도출 (외부 오픈소스) | 미연동 | 외부 |
| Data Fetcher | Yahoo Finance / FRED / Polymarket 데이터 수집 | 미구현 | `src/fetcher.py` (예정) |
| Analyzer | 신규 종목 발견 + 추세 신호 생성 | 미구현 | `src/analyzer.py` (예정) |
| Report Builder | 인사이트 → 리포트 포맷팅 | 미구현 | `src/reporter.py` (예정) |
| Slack Sender | Webhook으로 전송 | 미구현 | `src/slack.py` (예정) |
| Gmail Sender | 이메일로 전송 | 미구현 | `src/gmail.py` (예정) |
| Scheduler | 매일 오전 정기 실행 | 미결정 | TBD |

---

## 데이터 소스 후보

| 소스 | 비용 | 용도 |
|------|------|------|
| MiroFish (업스트림 + Ollama + Zep무료) | 거의 무료 | 대중 여론·심리 추세 인사이트 (→ mirofish-setup.md) |
| Polymarket API | 무료 | 예측시장 확률/추세 |
| **Stooq** (1순위) | 무료·무가입 | ETF EOD 가격/등락 (CSV 한 줄) |
| Alpha Vantage / Finnhub (fallback) | 무료 키 | ETF 가격 JSON, 풍부한 fallback |
| FRED API | 무료 | 매크로 지표 (금리, CPI 등) |
| FinViz | 무료 (스크래핑) | ETF 스크리닝 / 신규 종목 발견 |

> ⚠️ Yahoo Finance(`yfinance`)는 비공식 스크래핑으로 rate-limit·IP 차단·데이터 누락이
> 잦아 일일 배치에 부적합 → **Stooq 1순위 + Alpha Vantage/Finnhub fallback**으로 결정.
> 국내(KRX) 필요 시 pykrx 또는 Stooq 한국 티커.

---

## 미결 사항

- [x] ~~MiroFish 인사이트 연동 방식~~ → 파일 기반 배치 (`mirofish-integration.md`)
- [x] ~~전달 채널~~ → **Slack 우선** (기존 Slack Webhook 재사용), Gmail은 추후
- [ ] 스케줄러: cron vs Airflow vs APScheduler 선택
- [ ] 캐시: 로컬 파일 vs Redis vs SQLite
- [ ] Polymarket 데이터 수집 방식 (공식 API vs 스크래핑)
