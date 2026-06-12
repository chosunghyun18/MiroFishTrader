# Architecture Overview

> 최종 업데이트: 2026-06-12
> 상태: **v1 구현·라이브 검증 완료** — 실제 빌드 상세는 [implementation.md](implementation.md) 참조
> (이 문서는 상위 설계/데이터소스 관점. 실제 모듈·흐름은 implementation.md가 최신)

---

## 시스템 목적

MiroFish 오픈소스로 도출한 투자 인사이트를 바탕으로 보고서를 생성하고, **매일 오전 Slack 또는 Gmail로 전달**.

**투자 인사이트 2가지 목표**
1. 신규 종목 발견
2. 대중의 추세 파악

**투자 대상 시장**: Polymarket(예측시장), ETF 시장

---

## 컴포넌트 (v1 구현 상태)

```
[MiroFish 시뮬레이션] → latest.json → [추출 신호화] → [티커 매핑]   ┐
                                                    [Polymarket]  ┼→ [리포트] → [Slack]
```

| 컴포넌트 | 역할 | 상태 | 파일 |
|----------|------|------|------|
| MiroFish Insight Source | 멀티에이전트 시뮬레이션으로 추세 인사이트 도출 | ✅ 연동 | 외부 + `mirofish_runner.py` |
| Seed Generator | 시드 문서 생성 | ✅ 구현 | `src/seed.py` |
| Extractor | 리포트 산문 → 구조화 신호 | ✅ 구현 | `src/extractor.py` |
| Mapper | themes/entities → 티커 | ✅ 구현 | `src/mapper.py` |
| Polymarket | 예측시장 확률 | ✅ 구현 | `src/polymarket.py` |
| Report Builder | Slack 메시지 조립 | ✅ 구현 | `src/reporter.py` |
| Slack Sender | Webhook 전송 | ✅ 구현 | `src/slack.py` |
| Data Fetcher (시장데이터) | Stooq/AlphaVantage | ⬜ v2 | `src/fetcher.py` (예정) |
| Gmail Sender | 이메일 전송 | ⬜ v2 | (예정) |
| Scheduler | 매일 오전 cron | ⬜ 보류 | `scripts/run_daily.sh` 준비됨 |

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
