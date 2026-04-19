# MiroFish-Offline — 아키텍처 문서

> 최종 업데이트: 2026-04-19

## 프로젝트 개요

**MiroFish-Offline**은 다중 에이전트 스웜 인텔리전스 엔진으로, 여론·시장 심리·사회 역학을 **완전 로컬** 환경에서 시뮬레이션한다. 기존 MiroFish(중국어 UI, 클라우드 의존)의 포크로, 클라우드 API 없이 동작하며 UI는 전부 영어로 번역됨.

### 핵심 특징
- 클라우드 의존 없음 (Zep Cloud, DashScope 제거)
- 완전 로컬 LLM (Ollama)
- 지식 그래프 (Neo4j Community)
- CAMEL-AI OASIS 기반 멀티에이전트 시뮬레이션
- AGPL-3.0 라이선스

### 주요 활용 사례
- **트레이딩 시그널**: 시뮬레이션된 시장 심리 관찰
- **PR 위기 테스트**: 발표 전 여론 반응 예측
- **정책 영향 분석**: 초안 규제의 사회적 반응 시뮬레이션

---

## 디렉토리 구조

```
MiroFish-Offline/
├── backend/                  # Python Flask API 서버
│   ├── run.py                # 진입점
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py       # Flask 앱 팩토리
│       ├── config.py         # .env 파싱, 설정 관리
│       ├── api/              # Blueprint 엔드포인트
│       │   ├── graph.py      # Step 1: 그래프 빌드
│       │   ├── simulation.py # Step 2-3: 시뮬레이션
│       │   └── report.py     # Step 4-5: 리포트/채팅
│       ├── models/
│       │   ├── project.py    # 프로젝트 상태 관리
│       │   └── task.py       # 비동기 태스크 추적
│       ├── services/         # 비즈니스 로직
│       │   ├── graph_builder.py
│       │   ├── text_processor.py
│       │   ├── entity_reader.py
│       │   ├── ontology_generator.py
│       │   ├── oasis_profile_generator.py
│       │   ├── simulation_config_generator.py
│       │   ├── simulation_manager.py
│       │   ├── simulation_runner.py
│       │   ├── simulation_ipc.py
│       │   ├── graph_memory_updater.py
│       │   ├── report_agent.py
│       │   └── graph_tools.py
│       ├── storage/          # 그래프 DB 추상화
│       │   ├── graph_storage.py      # 인터페이스
│       │   ├── neo4j_storage.py      # Neo4j 구현체
│       │   ├── neo4j_schema.py
│       │   ├── ner_extractor.py
│       │   ├── embedding_service.py
│       │   └── search_service.py
│       └── utils/
│           ├── file_parser.py  # PDF/MD/TXT 파싱
│           ├── llm_client.py   # OpenAI-compatible wrapper
│           ├── logger.py
│           └── retry.py
│
├── frontend/                 # Vue 3 SPA (포트 3000)
│   └── src/
│       ├── views/            # 페이지 컴포넌트
│       └── components/       # Step1~5 + GraphPanel
│
├── scripts/                  # 독립 실행 스크립트
│   ├── run_parallel_simulation.py
│   └── run_twitter_simulation.py
│
├── docker-compose.yml        # Neo4j + Ollama + Flask 오케스트레이션
├── Dockerfile
└── .env.example
```

---

## 기술 스택

| 컴포넌트 | 기술 | 포트 |
|---------|------|------|
| Backend API | Flask 3.0+ | 5001 |
| Graph DB | Neo4j 5.15 Community | 7687 |
| LLM | Ollama (qwen2.5 등) | 11434 |
| 시뮬레이션 | CAMEL-AI OASIS 0.2.5 | — |
| Frontend | Vue 3 + Vite | 3000 |
| 시각화 | D3.js 7.9 | — |

---

## 전체 아키텍처

```
┌─────────────────────────────────────────┐
│           Vue 3 Frontend (3000)          │
└──────────────────┬──────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────┐
│       Flask API Server (5001)            │
│  /api/graph  /api/simulation  /api/report│
│  ─────────────────────────────────────  │
│  Service Layer (business logic)          │
│  ─────────────────────────────────────  │
│  GraphStorage interface → Neo4jStorage   │
└──────────────┬────────────┬─────────────┘
               │            │
        ┌──────▼────┐  ┌────▼──────┐
        │  Neo4j    │  │  Ollama   │
        │  (7687)   │  │  (11434)  │
        │  지식그래프│  │  LLM/임베딩│
        └───────────┘  └───────────┘
```

---

## 5단계 워크플로우

### Step 1: Graph Build (그래프 빌드)
```
문서 업로드 (PDF/MD/TXT)
    → TextProcessor: 청크 분리 (500 토큰, 50 오버랩)
    → OntologyGenerator: LLM으로 엔티티 타입 10개 + 관계 타입 생성
    → GraphBuilder: 배치 처리 → NERExtractor로 엔티티/관계 추출
    → Neo4jStorage: 노드/엣지 저장 + 임베딩 (768d)
결과: graph_id, 노드 수, 엣지 수
```

### Step 2: Environment Setup (에이전트 페르소나 생성)
```
그래프에서 엔티티 목록 조회
    → 사용자가 시뮬레이션할 엔티티 선택
    → OasisProfileGenerator: 페르소나 생성
      (personality_traits, opinion_bias, reaction_speed, influence_level)
    → SimulationConfigGenerator: LLM으로 시뮬레이션 파라미터 생성
      (num_rounds, platform_type, temperature, focus_topics)
결과: OasisAgentProfile 목록, SimulationParameters
```

### Step 3: Simulation (시뮬레이션 실행)
```
SimulationRunner → 서브프로세스로 CAMEL-AI OASIS 실행
    → 각 라운드: 에이전트 행동 결정 (CREATE_POST, LIKE, REPLY, DO_NOTHING 등)
    → SimulationIPCClient: 소켓으로 실시간 상태 전달
    → GraphMemoryUpdater: 라운드마다 그래프 업데이트
결과: simulation_id, action_log, 통계
```

### Step 4: Report Generation (리포트 생성)
```
ReportAgent (ReACT 패턴)
    → LLM 사고: 필요한 정보 파악
    → 툴 호출: SearchService, InsightForge, Panorama, interviews
    → Neo4j 하이브리드 검색: 0.7 × 벡터 + 0.3 × BM25
    → LLM 반성: 이해 보완 → 반복
    → 최종 리포트 생성 (JSON + 마크다운)
결과: report_id, 요약/인사이트/인터뷰/심리 추이/추천
```

### Step 5: Agent Interaction (에이전트 채팅)
```
사용자 선택 에이전트와 채팅
    → 그래프에서 에이전트 메모리 로드
    → LLM: 페르소나 + 컨텍스트 기반 응답 생성
    → 채팅 이력 Neo4j 저장
```

---

## API 엔드포인트 요약

### /api/graph
| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | /project/{id}/file | 파일 업로드 |
| POST | /project/{id}/analyze | 온톨로지 생성 |
| POST | /project/{id}/build | 그래프 빌드 (비동기) |
| GET | /task/{task_id} | 태스크 상태 폴링 |
| GET | /graph/{id}/data | 노드/엣지 조회 |

### /api/simulation
| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | /entities/{graph_id} | 엔티티 목록 |
| POST | /profile/{graph_id} | 에이전트 프로필 생성 |
| POST | /config/{graph_id} | 시뮬레이션 설정 생성 |
| POST | /{sim_id}/start | 시뮬레이션 시작 |
| GET | /{sim_id}/status | 상태 폴링 |

### /api/report
| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | /generate | 리포트 생성 시작 |
| GET | /{report_id} | 리포트 조회 |
| POST | /{report_id}/interact | 에이전트 채팅 |

---

## 검색 시스템

**하이브리드 검색** (Neo4j + Ollama):
```
최종 점수 = 0.7 × 벡터 유사도 + 0.3 × BM25 키워드 점수

1. 쿼리 텍스트 → nomic-embed-text (768d 임베딩)
2. Neo4j 벡터 인덱스 검색
3. Neo4j 풀텍스트 BM25 검색
4. 결합 후 재랭킹
```

---

## 지식 그래프 스키마

**노드 종류:**
- `Entity` (기본) + 온톨로지 파생 레이블 (Person, Company 등)
- `Graph` / `Ontology` (메타데이터)

**엣지 종류:**
- 사용자 정의 관계 (REPORTED_BY, INFLUENCES 등)
- 시스템 엣지 (HAS_ONTOLOGY, CONTAINS_ENTITY, MEMORY_OF)

**인덱스:**
- Vector Index (768d, nomic-embed-text)
- Fulltext Index (BM25)
- UUID 유니크 제약

---

## 설정 (.env)

```bash
# LLM
LLM_API_KEY=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL_NAME=qwen2.5:32b        # 또는 qwen2.5:14b (경량)

# Neo4j
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofish

# Embeddings
EMBEDDING_MODEL=nomic-embed-text

# CAMEL-AI (OPENAI_* 변수를 읽음)
OPENAI_API_KEY=ollama
OPENAI_API_BASE_URL=http://localhost:11434/v1

# Flask
FLASK_PORT=5001

# Simulation
OASIS_DEFAULT_MAX_ROUNDS=10
```

---

## 실행 방법

### Docker (권장)
```bash
cd MiroFish-Offline
cp .env.example .env
docker compose up -d
docker exec mirofish-ollama ollama pull qwen2.5:32b
docker exec mirofish-ollama ollama pull nomic-embed-text
# → http://localhost:3000
```

### 수동 실행
```bash
# 1. Neo4j Docker 시작
# 2. ollama serve & ollama pull qwen2.5:32b
# 3. cd backend && pip install -r requirements.txt && python run.py
# 4. cd frontend && npm install && npm run dev
```

---

## 하드웨어 요구사항

| 등급 | RAM | GPU VRAM | 모델 |
|------|-----|----------|------|
| 최소 | 8 GB | CPU only | qwen2.5:3b |
| 표준 | 32 GB | 12-16 GB | qwen2.5:14b |
| 고성능 | 64 GB | 24+ GB | qwen2.5:32b |

---

## 주요 제약 및 현황 (v0.2.0)

- Python < 3.12 필수 (CAMEL-AI 호환성)
- 시뮬레이션 상태는 인메모리 (재시작 시 소실)
- 하이브리드 검색 가중치 하드코딩 (0.7/0.3)
- 단일 사용자만 지원

---

## 마이그레이션 이력 (Zep Cloud → Neo4j)

| 구성요소 | 원본 (클라우드) | 현재 (로컬) |
|---------|--------------|------------|
| 그래프/메모리 | Zep Cloud | Neo4j 5.15 Community |
| LLM | DashScope (Alibaba) | Ollama qwen2.5 |
| 임베딩 | Zep Cloud | nomic-embed-text |
