# MVP v1 구현 계획

> 최종 업데이트: 2026-06-11
> 목표: **매일 오전 Slack으로 MiroFish 인사이트 + Polymarket 기반 투자 리포트 수신**
> 원칙: 최소한으로 빠르게. 동작하는 end-to-end 수직 슬라이스 우선.

---

## 범위

**포함 (v1)**
- MiroFish 리포트 → 추출 신호 (✅ 구현 완료)
- 티커 매핑 (정적 사전)
- Polymarket 예측시장 확률 (테마 매칭)
- Slack 전송 (기존 Webhook)
- 매일 오전 스케줄

**제외 (v2 이후)**
- 시장 데이터 fetcher — **Stooq(1순위, 무료·무가입) + Alpha Vantage/Finnhub fallback**, 매크로는 FRED. (Yahoo `yfinance`는 비공식·불안정으로 미채택)
- 시드 생성 + MiroFish 배치 자동 트리거 (리포트 파일은 이미 있다고 가정)
- Gmail, 캐싱, stale 정밀 처리, 티커 자동 리졸버

---

## 파이프라인

```
out/latest.json
  → [report_store] 읽기
  → [extractor] 구조화 신호 (✅)
  → [mapper] themes/entities → 티커
  → [polymarket] themes → 예측시장 확률
  → [reporter] Slack 메시지 조립
  → [slack] Webhook 전송
        ↑
  [pipeline] 전체 오케스트레이션 ← [cron] 매일 오전
```

---

## 작업 목록 (의존 순서)

| # | 모듈 | 역할 | 비고 |
|---|------|------|------|
| 1 | `src/report_store.py` | `MIROFISH_SHARED_DIR/out/latest.json` 로드, 없음/손상 graceful | 소 |
| 2 | `src/mapper.py` | `ticker_map.yaml` 로드 → 티커 매핑, 실패 키워드 로그 | 소 |
| 3 | `src/polymarket.py` | 테마 키워드로 마켓 검색 → 확률(Yes price) 상위 N | **API 확인 필요** |
| 4 | `src/reporter.py` | 신호+티커+Polymarket → Slack 메시지(blocks/markdown) | 소 |
| 5 | `src/slack.py` | Webhook POST, 에러 핸들링 | 소 |
| 6 | `src/pipeline.py` | 진입점: 위 단계 순차 실행 | 소 |
| 7 | 스케줄 | cron/launchd 매일 오전 1회 + 실행 셸 스크립트 | 설정 |

각 모듈은 단위 테스트 동반 (외부 호출은 Fake/mock으로 분리).

---

## Polymarket 메모

- 공개 API 사용 (Gamma API `gamma-api.polymarket.com/markets` 계열) — **구현 시 실제 엔드포인트·응답 형태 1회 검증 필수** (스키마가 변동될 수 있음).
- v1 최소: 테마 키워드로 활성 마켓 검색 → 각 마켓의 현재 Yes 확률 + 제목만 추출.
- 네트워크 미허용/실패 시 Polymarket 섹션은 생략하고 리포트는 계속 생성 (graceful degrade).

---

## 완료 기준 (Definition of Done)

- `python -m src.pipeline` 실행 시: 샘플 `latest.json`으로 Slack 채널에 리포트 1건 도착
- MiroFish 리포트 없거나 Polymarket 실패해도 크래시 없이 부분 리포트 전송
- 전체 단위 테스트 통과
- cron 등록으로 매일 오전 자동 실행
