# Changelog — 의사결정 및 변경 이력

> 날짜 내림차순. 코드 변경이 아닌 **결정과 이유**를 기록하는 곳.

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
