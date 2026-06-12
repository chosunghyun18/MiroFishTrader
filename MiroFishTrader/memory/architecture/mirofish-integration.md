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
- MiroFish는 무거운 스택 → 상시 구동 부담 회피, 매일 오전 1회 배치 성격과 부합
- 두 프로젝트의 결합도를 최소화 (MiroFish 내부 변경이 MiroFishTrader에 영향 없음)

### 1.1 MiroFish 생산자 (확정: 2026-06-12)

**업스트림 `666ghj/MiroFish` + 로컬 Ollama + Zep 무료 티어**를 사용한다.

| 구성 | 선택 | 비용 |
|------|------|------|
| 엔진 | 업스트림 MiroFish (유지보수됨) | 무료(AGPL) |
| LLM | `LLM_BASE_URL`을 로컬 Ollama로 변경 (OpenAI SDK 호환) | 무료 |
| 메모리/그래프 | Zep Cloud 무료 티어 | 무료(저volume) |

- DashScope(유료) 대신 Ollama로 우회 → API 요금 0. 남는 비용은 시뮬레이션 컴퓨팅뿐.
- Offline 포크(로컬 Neo4j) 대신 업스트림 선택: 유지보수됨 + Neo4j 불필요. 대가로 Zep 가입·데이터 일부 외부 전송 감수.
- 셋업 절차: → `architecture/mirofish-setup.md`

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

### 3.3 추출 레이어 (필수 추가 단계) — 확정

자유 산문 → 구조화 신호로 변환하는 단계가 MiroFishTrader 측에 필요하다.

```
report.markdown_content (+ sections)
  → 추출 LLM (로컬 Ollama, MiroFish가 이미 띄우는 인스턴스 재사용 = 추가 비용 0)
  → 아래 구조화 신호 JSON
```

**추출 출력 스키마 (확정, 플랫 구조)**

```json
{
  "date": "2026-06-11",
  "source_report_id": "...",
  "trend_direction": "bullish | bearish | neutral",
  "confidence": 0.0,
  "themes": ["반도체", "금리인하 기대"],
  "entities": [
    {"name": "NVIDIA", "sentiment": "positive | negative | neutral"}
  ],
  "summary": "한 줄 요약"
}
```

| 필드 | 타입 | 의미 |
|------|------|------|
| `trend_direction` | enum(3) | 대중 추세 방향 — 핵심 신호 |
| `confidence` | float 0~1 | 추세 확신도 |
| `themes` | string[] | 테마/토픽 (→ 티커 매핑 입력) |
| `entities` | {name, sentiment}[] | 언급 엔티티 + 개별 심리 |
| `summary` | string | 리포트 한 줄 요약 |

- 출력 구조는 MiroFishTrader가 직접 정의/통제 (MiroFish 출력에 의존하지 않음).
- LLM 추출 프롬프트는 위 스키마를 강제(JSON only)하도록 작성.
- 이 추출 JSON이 Analyzer의 "대중 추세" 신호 입력이 됨.

---

## 4. 종목 매핑 레이어

MiroFish 인사이트를 실제 거래 대상으로 연결하는 책임은 MiroFishTrader의 Analyzer에 있다.

| 인사이트 목표 | 담당 | MiroFish 역할 |
|--------------|------|--------------|
| 신규 종목 발견 | FinViz 스크리닝 등 | 발견된 종목/테마에 추세·심리 레이어 덧입힘 |
| 대중 추세 파악 | MiroFish `sentiment_trend` + Polymarket 확률 추이 | 주 소스 |

**매핑 방식 (확정): 정적 사전 파일**

```
config/ticker_map.yaml   # 수동 시드, 외부 API 없이 시작, 점진적 확장
```

```yaml
# theme/keyword → ETF 티커
themes:
  반도체:   [SOXX, SMH]
  금리:     [TLT, IEF]
  AI:       [BOTZ, AIQ]
  에너지:   [XLE]
# entity(회사명) → 티커 (선택, 직접 종목)
entities:
  NVIDIA:   NVDA
  Apple:    AAPL
```

- 매핑 흐름: 추출 `themes`/`entities` → `ticker_map.yaml` 조회 → ETF 티커(또는 직접 종목).
- **v1은 정적 사전만** (비용 0, 가장 단순). 매칭 실패 키워드는 로그로 남겨 사전 보강.
- 추후 확장: 회사명→티커 자동 리졸버(yfinance 검색 등)는 필요 시 도입.

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
- [x] ~~추출 레이어 출력 스키마 확정~~ → 확정 (3.3 참조)
- [x] ~~추출용 LLM 선택~~ → 로컬 Ollama 재사용 (비용 0)
- [x] ~~엔티티/테마 → 티커 매핑 방식~~ → 정적 사전 `config/ticker_map.yaml` (4장 참조)
- [ ] stale 기준 (몇 시간 지난 리포트까지 허용?)
