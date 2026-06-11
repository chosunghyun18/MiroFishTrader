# MiroFish 인사이트 연동 설계

> 최종 업데이트: 2026-06-11
> 상태: 설계 확정 (미구현)
> 실행 모델: **파일 기반 배치** (REST API 미사용)

---

## 1. 개요

MiroFish 오픈소스(멀티에이전트 여론·심리 시뮬레이션 엔진)는 **별개 프로젝트/외부 도구**로 취급한다.
MiroFishTrader는 MiroFish를 직접 임포트하거나 API로 제어하지 않고, **공유 폴더에 떨어진 리포트 JSON 파일만 소비**한다.

```
[MiroFish 배치 실행]  →  공유폴더에 리포트 JSON  →  [MiroFishTrader가 읽기]
   (외부, 무겁게)         (느슨한 계약/경계)          (가볍게 소비)
```

**이 방식을 택한 이유**
- MiroFish는 Neo4j + Ollama가 필요한 무거운 로컬 스택 → 상시 구동 부담 회피
- 매일 오전 1회 배치 성격과 잘 맞음 (비용 최소화 제약 부합)
- 두 프로젝트의 결합도를 최소화 (MiroFish 내부 변경이 MiroFishTrader에 영향 없음)

---

## 2. 공유 폴더 계약

```
shared/mirofish/
├── in/
│   └── seed-YYYYMMDD.json      # MiroFishTrader가 작성 (그날 시뮬레이션 시드)
└── out/
    ├── report-YYYYMMDD.json    # MiroFish 배치가 작성 (Step 4 결과)
    └── latest.json             # 가장 최근 리포트 (심볼릭/복사)
```

- **경로는 환경변수로 관리**: `MIROFISH_SHARED_DIR` (`.env`)
- MiroFishTrader는 `out/latest.json`만 읽으면 됨. 파일 없음/오래됨(stale) 시 graceful 처리.

---

## 3. 데이터 계약

### 3.1 입력 — seed (MiroFishTrader → MiroFish)

그날 시뮬레이션할 주제. 후보 소스: 트렌딩 ETF 섹터, Polymarket 핫이슈, 최신 뉴스 헤드라인.

```json
{
  "date": "2026-06-11",
  "topics": [
    {"source": "polymarket", "title": "...", "ref": "market_id"},
    {"source": "etf",        "title": "반도체 섹터 모멘텀", "ref": "SOXX"},
    {"source": "news",       "title": "...", "ref": "url"}
  ]
}
```

### 3.2 출력 — report (MiroFish → MiroFishTrader)

MiroFish Step 4 리포트 JSON. 핵심 소비 필드:

| 필드 | 용도 |
|------|------|
| `summary` | 리포트 도입부 컨텍스트 |
| `insights` | 핵심 인사이트 목록 |
| `sentiment_trend` | **"대중 추세" 신호의 핵심 소스** (심리 추이) |
| `recommendations` | 참고용 추천 |
| `interviews` | (선택) 근거 보강 |

> MiroFish는 엔티티/심리 중심이라 **종목 티커를 직접 제공하지 않음**.

---

## 4. 종목 매핑 레이어

MiroFish 인사이트를 실제 거래 대상으로 연결하는 책임은 MiroFishTrader의 Analyzer에 있다.

| 인사이트 목표 | 담당 | MiroFish 역할 |
|--------------|------|--------------|
| 신규 종목 발견 | FinViz 스크리닝 등 | 발견된 종목/테마에 추세·심리 레이어 덧입힘 |
| 대중 추세 파악 | MiroFish `sentiment_trend` + Polymarket 확률 추이 | 주 소스 |

매핑 흐름: MiroFish 엔티티/테마 → (키워드/섹터 매칭) → ETF 티커 또는 Polymarket 마켓.

---

## 5. 일일 배치 흐름

```
매일 오전 스케줄러
  1. 시드 수집 → shared/mirofish/in/seed-YYYYMMDD.json 작성
  2. MiroFish 배치 트리거 (외부 실행: 시드 읽고 시뮬레이션 → out/report 작성)
  3. MiroFishTrader: out/latest.json 로드
  4. Analyzer: sentiment_trend + 시장데이터(Yahoo/FRED/Polymarket) 결합 → 신호 생성
  5. Report Builder → Slack / Gmail 전달
```

- 2번(MiroFish 실행)과 3~5번(MiroFishTrader)은 **시간 분리** 가능: MiroFish가 먼저 끝나도록 스케줄을 앞당기거나, report 파일 존재를 폴링.
- MiroFish 실행이 실패/누락돼도 MiroFishTrader는 직전 `latest.json`으로 degrade 동작.

---

## 6. 미결 사항

- [ ] 시드 토픽 선정 로직 (자동 트렌딩 vs 고정 워치리스트)
- [ ] MiroFish 배치를 누가 트리거하나 (별도 cron vs MiroFishTrader가 subprocess 호출)
- [ ] report JSON 실제 스키마 확정 (MiroFish 출력 샘플로 검증 필요)
- [ ] 엔티티/테마 → 티커 매핑 사전 구축
- [ ] stale 기준 (몇 시간 지난 리포트까지 허용?)
