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

⚠️ **실제 스키마 검증 완료 (2026-06-11, 백업 소스 기준)**.
MiroFish 리포트는 구조화된 신호 필드가 **없고**, "미래 예측 보고서" 형태의 자유 산문이다.

실제 `report.to_dict()` 스키마:

```json
{
  "report_id": "...",
  "simulation_id": "...",
  "graph_id": "...",
  "simulation_requirement": "주입된 시나리오 변수(=시드 핵심)",
  "status": "completed",
  "outline": {
    "title": "리포트 제목",
    "summary": "핵심 예측 결과 한 문장",
    "sections": [ {"title": "섹션 제목", "content": "산문 본문"} ]
  },
  "markdown_content": "전체 리포트 마크다운",
  "created_at": "...", "completed_at": "...", "error": null
}
```

| 필드 | 실제 성격 | MiroFishTrader 활용 |
|------|----------|---------------------|
| `outline.summary` | 한 문장 요약 | 리포트 도입부 |
| `outline.sections[].content` | 자유 산문 (2~5개) | **추출 레이어 입력** |
| `markdown_content` | 전체 마크다운 | 추출 레이어 입력 / 원문 첨부 |
| `simulation_requirement` | 입력 시나리오 | 어떤 시드였는지 추적 |

**중요한 현실**
- `sentiment_trend`·`insights`·`recommendations`·`interviews` 같은 **구조화 필드는 존재하지 않음**.
  이 개념들은 모두 `sections[].content` 산문 안에 녹아 있음.
- **섹션 제목은 매 실행마다 LLM이 생성** (고정 아님) → 특정 섹션명에 의존 불가.
- MiroFish는 엔티티/심리 중심이라 **종목 티커를 직접 제공하지 않음**.

### 3.3 추출 레이어 (필수 추가 단계)

자유 산문 → 구조화 신호로 변환하는 단계가 MiroFishTrader 측에 필요하다.

```
report.markdown_content (+ sections)
  → LLM 추출 프롬프트 (로컬 Ollama = 무료, 비용 제약 부합)
  → { trend_direction, mentioned_entities[], themes[], confidence }
```

- 출력 구조는 MiroFishTrader가 직접 정의/통제 (MiroFish 출력에 의존하지 않음).
- 이 추출 JSON이 비로소 Analyzer의 "대중 추세" 신호 입력이 됨.

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
  4. 추출 레이어: markdown_content → 구조화 신호 JSON (LLM 추출)
  5. Analyzer: 추출 신호 + 시장데이터(Yahoo/FRED/Polymarket) 결합 → 신호 생성
  6. Report Builder → Slack / Gmail 전달
```

- 2번(MiroFish 실행)과 3~5번(MiroFishTrader)은 **시간 분리** 가능: MiroFish가 먼저 끝나도록 스케줄을 앞당기거나, report 파일 존재를 폴링.
- MiroFish 실행이 실패/누락돼도 MiroFishTrader는 직전 `latest.json`으로 degrade 동작.

---

## 6. 미결 사항

- [ ] 시드 토픽 선정 로직 (자동 트렌딩 vs 고정 워치리스트)
- [ ] MiroFish 배치를 누가 트리거하나 (별도 cron vs MiroFishTrader가 subprocess 호출)
- [x] ~~report JSON 실제 스키마 확정~~ → 검증 완료 (3.2 참조). 구조화 필드 없음, 자유 산문
- [ ] 추출 레이어 출력 스키마 확정 (trend_direction/entities/themes/confidence 등)
- [ ] 추출용 LLM 선택 (로컬 Ollama vs 저비용 API)
- [ ] 엔티티/테마 → 티커 매핑 사전 구축
- [ ] stale 기준 (몇 시간 지난 리포트까지 허용?)
