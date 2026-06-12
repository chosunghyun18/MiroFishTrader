# MiroFish 셋업 (업스트림 + Ollama + Zep 무료)

> 최종 업데이트: 2026-06-12
> 목적: 리포트 생산자(MiroFish)를 로컬에서 거의 무료로 띄워 `latest.json`을 만든다.

리포지토리: https://github.com/666ghj/MiroFish (AGPL-3.0, CAMEL-AI OASIS 기반)

---

## 1. 사전 요구사항

| 도구 | 버전 |
|------|------|
| Node.js | 18+ |
| Python | ≥3.11, ≤3.12 |
| uv | 최신 |
| Ollama | 실행 중 (우리 추출 레이어와 공유) |
| Zep 계정 | https://app.getzep.com (무료 티어) → `ZEP_API_KEY` 발급 |

---

## 2. 클론 & 환경설정

```bash
git clone https://github.com/666ghj/MiroFish
cd MiroFish
cp .env.example .env
```

`.env` 핵심 — **LLM을 DashScope 대신 로컬 Ollama로**:

```bash
# LLM: OpenAI SDK 호환이므로 Ollama로 우회 (무료)
LLM_API_KEY=ollama                      # 더미 값
LLM_BASE_URL=http://localhost:11434/v1  # Docker로 띄우면 http://host.docker.internal:11434/v1
LLM_MODEL_NAME=qwen2.5:7b               # 시뮬 품질 원하면 14b+, M2면 7b로 시작

# 메모리/그래프: Zep 무료 티어
ZEP_API_KEY=<app.getzep.com 에서 발급>
```

> ⚠️ Docker로 MiroFish를 띄우고 Ollama는 호스트(Mac)에서 돌리면,
> `LLM_BASE_URL`은 `http://host.docker.internal:11434/v1` 로 해야 컨테이너에서 호스트 Ollama에 접근된다.

---

## 3. 실행

```bash
# Docker (권장)
docker compose up -d
# 또는 소스 실행
npm run setup:all && npm run dev
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:5001

---

## 4. 리포트 생산 → 우리 파이프라인 연결

1. UI(:3000)에서 시드 업로드 → 5단계(Graph Build → Env Setup → Simulation → Report) 진행
2. 생성된 리포트를 우리 공유 폴더로 export:
   ```
   MiroFishTrader/shared/mirofish/out/latest.json
   ```
   - 수동: 백엔드 `GET /api/report/{report_id}` 응답(JSON)을 위 경로로 저장
   - 자동(예정): 헤드리스 러너가 5단계 API를 구동하고 결과를 위 경로에 저장 (작업 ④)

3. 이후 `python -m src.pipeline` 이 그 리포트를 읽어 신호 추출 → Slack 전송

---

## 5. 주의

- **컴퓨팅**: 시뮬레이션은 에이전트 수천 × 라운드만큼 LLM을 호출 → 로컬 Ollama(M2)에선 느림. 라운드 수를 낮춰(<40) 시작.
- **Zep 쿼터**: 무료 티어는 저volume 가정. 하루 1회면 충분할 가능성이 높으나 초과 시 유료/Neo4j 전환 검토.
- **API 검증 필요**: 자동화 러너 작성 전, 업스트림 백엔드의 실제 `/api/graph·simulation·report` 엔드포인트를 1회 확인.
