# MiroFishTrader — Memory Index

> 사전(dictionary) 형태의 프로젝트 지식 저장소.
> 새로운 컴포넌트, 의사결정, 용어 추가 시 반드시 이 파일을 먼저 업데이트할 것.

---

## Architecture (시스템 구조)

| 문서 | 설명 |
|------|------|
| [architecture/overview.md](architecture/overview.md) | 전체 시스템 컴포넌트 및 데이터 흐름 |
| [architecture/mirofish-integration.md](architecture/mirofish-integration.md) | MiroFish 인사이트 연동 설계 — 파일 기반 배치, 공유폴더 계약, 종목 매핑 |

---

## Progress (진행 이력)

| 문서 | 설명 |
|------|------|
| [progress/changelog.md](progress/changelog.md) | 날짜별 변경사항 및 의사결정 로그 |
| [progress/mvp-plan.md](progress/mvp-plan.md) | MVP v1 구현 계획 — 범위·파이프라인·작업목록·완료기준 |

---

## Concepts (도메인 지식)

| 문서 | 설명 |
|------|------|
| [concepts/etf-glossary.md](concepts/etf-glossary.md) | ETF·분석 지표 및 Polymarket(예측시장) 용어 정의 |

---

## 업데이트 규칙

- **새 모듈/파일 추가** → `architecture/overview.md` 의 컴포넌트 목록 갱신 후 이 INDEX에 링크 추가
- **설계 결정 변경** → `progress/changelog.md` 에 날짜 + 이유 기록
- **새 분석 지표 도입** → `concepts/etf-glossary.md` 에 정의 추가
- **이 INDEX는 프로젝트의 진입점** — 새 대화를 시작할 때 Claude는 이 파일을 먼저 읽을 것
