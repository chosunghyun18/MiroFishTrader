# Changelog — 의사결정 및 변경 이력

> 날짜 내림차순. 코드 변경이 아닌 **결정과 이유**를 기록하는 곳.

---

## 2026-06-11

### 프로젝트 범위 명확화

- **결정**: 프로젝트 목적·범위를 구체화하여 문서 갱신
- **목적**: MiroFish 오픈소스로 투자 인사이트 도출 → 보고서 생성 → **매일 오전 Slack 또는 Gmail로 전달**
- **투자 인사이트 2가지**: (1) 신규 종목 발견 (2) 대중의 추세 파악
- **투자 대상 시장**: Polymarket(예측시장), ETF 시장
- **갱신 문서**: CLAUDE.md, architecture/overview.md, concepts/etf-glossary.md(Polymarket 용어 추가)

### MiroFish 버전 결정 — 업스트림 + Ollama + Zep 무료

- **결정**: 리포트 생산자로 **업스트림 `666ghj/MiroFish`** 채택. LLM은 로컬 Ollama로 우회(OpenAI SDK 호환), 메모리는 Zep Cloud 무료 티어
- **정정**: 업스트림이 "유료"라던 초기 평가 수정 — LLM을 Ollama로 바꾸면 API 요금 0, Zep 무료 티어가 저volume에 충분. 남는 비용은 시뮬 컴퓨팅뿐
- **Offline 포크 대신 업스트림 이유**: 유지보수됨 + Neo4j 불필요. 대가로 Zep 가입·데이터 일부 외부 전송
- **신규 문서**: `architecture/mirofish-setup.md` (클론·.env·Docker·리포트 export)
- **MiroFish 연동 작업 재정의 (①완료, ②~⑥ 진행)**:
  - ① 버전 선택 ✅
  - ② 업스트림 배포·실행 (Ollama+Zep, Docker)
  - ③ 시드 생성 로직
  - ④ 5단계 워크플로우 헤드리스 자동화 (API 러너)
  - ⑤ 리포트 export → `shared/out/latest.json`
  - ⑥ 컴퓨팅(제약, 라운드 수 조절)

### v1 라이브 검증 완료 + Polymarket 매칭 품질 수정

- **라이브 검증**: Mac(M2)에서 Ollama `qwen2.5:7b`로 end-to-end 성공 — 추세 bullish 80%, 테마 semiconductors/AI/rates, 티커 SOXX·SMH·NVDA, Slack 전송 완료
- **셋업 이슈 해결**: `.env` 파이썬 자동 로드 버그 수정, 모델 14b→7b(M2 VRAM 10.7GB 적합), Ollama 서버 기동 순서 정리
- **품질 버그 수정**: Polymarket 키워드가 부분 문자열 매칭이라 짧은 키워드 `ai`가 "Sp**ai**n"·"Str**ai**t"에 오매칭 → **단어 경계(\b) 매칭**으로 변경. 무관한 스포츠/정치 마켓 제거, Fed 금리 마켓 등 관련만 유지
- **사전 보강**: `chip`/`chips`/`chip sector` → SOXX/SMH 추가 (매핑 miss 로그 반영)
- **테스트**: 30개 통과 (단어경계 케이스 추가)

### 시장데이터 소스 결정 (v2 작업 예정)

- **결정**: 시장 데이터 fetcher 소스를 **Stooq(1순위) + Alpha Vantage/Finnhub(fallback)**로 확정. 매크로는 FRED
- **이유**: 용도가 "ETF 몇 개의 EOD 가격·등락을 하루 1회"라 무료·무가입·소량이면 충분. Stooq는 키·가입 없이 CSV로 EOD 제공(비용 0). Yahoo `yfinance`는 비공식 스크래핑으로 rate-limit·IP 차단·누락이 잦아 일일 배치 부적합
- **국내**: KRX 필요 시 pykrx 또는 Stooq 한국 티커
- **반영**: `architecture/overview.md` 데이터소스, `progress/mvp-plan.md` v2 항목
- **다음 작업**: `src/fetcher.py` — Stooq EOD 조회 + AlphaVantage/Finnhub fallback (v2)

### MVP v1 전체 구현 완료

- **구현**: report_store → mapper → polymarket → reporter → slack → pipeline → 스케줄 (7개 모두)
- **Polymarket**: Gamma API(`/markets?order=volume24hr`) 실제 검증. 상위 거래량 마켓을 키워드 필터. `outcomePrices[0]`=Yes확률, `oneDayPriceChange`=추세. 실패 시 빈 결과로 degrade
- **언어 정합성**: 추출 themes를 영어로 강제(소스 리포트·Polymarket이 영어). `ticker_map.yaml`은 영/한 별칭 병기
- **graceful degrade 검증**: Ollama 미실행 시 중립 신호로 fallback, 리포트 없으면 안내 메시지, Polymarket 실패해도 부분 리포트 전송 — 모두 크래시 없음
- **스케줄**: `scripts/run_daily.sh` + cron/launchd 가이드(README)
- **테스트**: 29개 전부 통과 (extractor 9, report_store 4, mapper 4, polymarket 6, reporter 3, pipeline 3)
- **남은 것(v2)**: Yahoo/FRED 시장데이터, 시드 생성·MiroFish 배치 트리거, Gmail, 캐싱

### MVP v1 범위 확정 + 작업 계획

- **결정**: "최소한으로 빠르게" 원칙으로 v1 범위 확정
- **v1 포함**: MiroFish 추출 신호(✅) + 티커 매핑 + **Polymarket 예측시장 확률** + Slack 전송 + 매일 오전 스케줄
- **v1 제외**: Yahoo/FRED 시장데이터, 시드 생성·MiroFish 배치 트리거, Gmail, 캐싱
- **작업 목록(7개)**: report_store → mapper → polymarket → reporter → slack → pipeline → 스케줄
- **신규 문서**: `progress/mvp-plan.md` (범위·파이프라인·완료기준)
- **주의**: Polymarket이 유일한 비자명 조각 — 구현 시 실제 API 1회 검증 필요

### 구현 착수 — 추출 레이어 (첫 코드)

- **결정**: 가장 자립적인 추출 레이어부터 구현 시작
- **추가 파일**:
  - `src/models.py` — `ExtractedSignal`/`EntitySentiment` + 방어적 `from_raw` 검증
  - `src/llm.py` — `OllamaClient` (OpenAI 호환), `SupportsComplete` 프로토콜로 DI
  - `src/extractor.py` — `build_prompt`/`extract_signal`, 코드펜스·잡텍스트 허용 JSON 파싱
  - `config/ticker_map.yaml`, `requirements.txt`, `.env.example`
  - `tests/test_extractor.py` — FakeLLM 기반 9개 테스트 (전부 통과)
- **검증**: `python -m pytest tests/` → 9 passed. LLM 없이 추출/검증 로직 커버 (코드펜스 제거, trend 강제, confidence 클램프, 엔티티 정제, 파싱 실패 처리)
- **다음**: 티커 매핑 모듈(`mapper.py`) → Analyzer → Slack sender → 일일 배치 오케스트레이션

### 추출 레이어·매핑·전달 채널 확정

- **추출 출력 스키마 (확정)**: 플랫 구조 `{date, source_report_id, trend_direction(bullish/bearish/neutral), confidence(0~1), themes[], entities[{name, sentiment}], summary}`
- **추출 LLM**: 로컬 Ollama 재사용 (MiroFish가 이미 띄우는 인스턴스 → 추가 비용 0)
- **티커 매핑**: 정적 사전 `config/ticker_map.yaml` (수동 시드, 외부 API 없이 시작, 점진 확장). 매칭 실패 키워드는 로그로 보강
- **전달 채널**: Slack 우선 — **기존 Slack Webhook 재사용**. Gmail은 추후
- **갱신 문서**: `mirofish-integration.md` 3.3/4장, `overview.md` 미결사항

### MiroFish 리포트 스키마 검증 — 추출 레이어 필요 확인

- **발견**: 백업 소스(`report_agent.py`)로 실제 출력 스키마 검증 결과, `sentiment_trend`·`insights`·`recommendations`·`interviews` 같은 **구조화 필드가 존재하지 않음**
- **실제 구조**: `{report_id, simulation_requirement, outline:{title, summary, sections:[{title, content}]}, markdown_content, ...}` — 섹션 제목은 매 실행마다 LLM 생성(2~5개), 신호는 전부 자유 산문
- **결정**: MiroFishTrader에 **추출 레이어** 추가 — `markdown_content`를 LLM으로 파싱해 `{trend_direction, mentioned_entities, themes, confidence}` 구조화 신호 생성. 출력 스키마는 MiroFishTrader가 통제
- **영향**: `architecture/mirofish-integration.md` 3.2/3.3 갱신, 일일 흐름에 추출 단계 추가

### MiroFish 인사이트 연동 방식 확정

- **결정**: MiroFish 인사이트 연동을 **파일 기반 배치**로 확정 (REST API 오케스트레이션 미채택)
- **이유**: MiroFish는 Neo4j+Ollama 필요한 무거운 로컬 스택 → 상시 구동 대신 일일 배치가 비용·구조 면에서 유리, 두 프로젝트 결합도 최소화
- **계약**: 공유 폴더(`shared/mirofish/`)의 리포트 JSON 파일을 MiroFishTrader가 소비. MiroFish의 Step4 `sentiment_trend`가 "대중 추세" 신호의 핵심 소스
- **신규 문서**: `architecture/mirofish-integration.md`

### MiroFish-Offline 프로젝트 제거

- **결정**: `MiroFish-Offline/` 디렉토리 및 관련 메모리 문서 전부 제거
- **배경**: MiroFish-Offline(멀티에이전트 시뮬레이션 엔진, Flask+Neo4j+Ollama+CAMEL-AI)은 ETF→Slack 리포트 서비스인 본 프로젝트와 **별개 프로젝트**로 확인됨. 한 폴더에 함께 있었을 뿐 의존 관계 없음
- **조치**:
  - `MiroFish-Offline/` 삭제 (git에서도 제거)
  - `memory/architecture/mirofish-offline.md` 삭제
  - `memory/INDEX.md`에서 해당 링크 제거
- **백업**: 삭제 직전 미커밋 변경분 포함 `MiroFish-Offline-backup-20260611-140042.tar.gz`로 보관 (복구 필요 시 사용)

---

## 2026-04-19

### 프로젝트 초기화

- **결정**: MiroFishTrader 프로젝트 시작
- **배경**: 적은 비용으로 ETF 분석 리포트를 Slack으로 제공하는 서비스 개발
- **주요 제약**:
  - 비용 최소화 (무료 데이터 소스 우선)
  - 기술적 분석 최소화 (보조 수단으로만 활용)
  - 투자 기간: 1~2주 ~ 최대 2개월

### Claude 세팅 구성

- **결정**: `everything-claude-code` 레포 참조하여 프로젝트 맞춤 Claude 환경 구성
- **포함된 에이전트**: planner, architect, code-reviewer, python-reviewer
- **제외된 에이전트**: TypeScript/Go/Java/Kotlin/Rust/C++ 리뷰어, frontend, e2e, build-fix (Python 전용 프로젝트)
- **이유**: 불필요한 에이전트 제거로 컨텍스트 오염 방지

### 문서화 시스템 구성

- **결정**: `memory/` 디렉토리를 사전 형태로 관리
- **구조**: INDEX.md (마스터) + architecture/ + progress/ + concepts/
- **이유**: 여러 대화에 걸쳐 프로젝트 컨텍스트 유지

---

<!-- 새 항목은 맨 위에 추가 (날짜 내림차순 유지) -->
