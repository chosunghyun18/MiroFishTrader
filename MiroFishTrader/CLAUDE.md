# MiroFishTrader — Claude Project Config

## Project Overview

**MiroFishTrader**는 MiroFish 오픈소스를 활용해 투자 인사이트를 도출하고, 이를 토대로 보고서를 만들어 **매일 오전 Slack 또는 Gmail로 리포트를 전달**하는 서비스다.

- **투자 인사이트 (2가지 목표)**:
  1. 신규 종목 발견
  2. 대중의 추세 파악
- **투자 대상 시장**: Polymarket(예측시장), ETF 시장
- **인사이트 소스**: MiroFish 오픈소스(멀티에이전트 여론/심리 시뮬레이션) — 외부 도구로 활용
- **제약 및 방침**:
  - 비용 최소화가 핵심 제약 조건
  - 기술적 분석은 보조 수단 (최소한으로 활용)
  - 목표 투자 기간: 1~2주 ~ 최대 2개월 이내 매도

## Stack

- **언어**: Python (주)
- **리포트 전달**: Slack Webhook
- **데이터 소스**: 무료/저비용 우선 (Yahoo Finance, FRED, 등)
- **스케줄링**: cron 또는 Airflow (미결정)

## Documentation System

모든 프로젝트 문서는 `memory/` 디렉토리에 사전(dictionary) 형태로 관리.

```
memory/
├── INDEX.md              ← 마스터 인덱스 (항상 최신 유지)
├── architecture/         ← 시스템 구조, 컴포넌트 관계
├── progress/             ← 변경 이력, 의사결정 로그
└── concepts/             ← 도메인 용어 (ETF, 지표 등)
```

**규칙**:
- 새로운 컴포넌트/모듈 추가 시 → `memory/architecture/` 업데이트
- 의사결정 또는 방향 변경 시 → `memory/progress/changelog.md` 에 날짜와 함께 기록
- `memory/INDEX.md` 는 항상 최신 상태 유지

## Agents Available

| Agent | 용도 |
|---|---|
| `planner` | 기능 구현 계획 수립 |
| `architect` | 시스템 설계 및 트레이드오프 분석 |
| `code-reviewer` | 코드 품질 검토 |
| `python-reviewer` | Python 코드 전문 리뷰 |

## Cost Constraints

- API 호출은 캐싱 적극 활용
- 무료 데이터 소스 우선, 유료 API 사용 시 반드시 기록
- Claude API 호출 시 prompt caching 필수 적용

## Code Standards

- 함수 50줄 이하
- 타입 어노테이션 필수
- 외부 API 호출부 반드시 에러 핸들링
- 환경변수는 `.env` 파일로 관리 (절대 하드코딩 금지)
