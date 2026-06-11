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
