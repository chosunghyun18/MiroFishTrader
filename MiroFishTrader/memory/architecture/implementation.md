# 구현 현황 (v1) — 실제 빌드된 시스템

> 최종 업데이트: 2026-06-12
> 상태: **v1 구현·라이브 검증 완료** (실데이터로 end-to-end Slack 전송 확인)

이 문서는 "설계"가 아니라 **실제로 동작하는 코드**를 설명한다.

---

## 한 줄 요약

`bash scripts/report.sh` 한 명령으로: 서버 기동 → 시드 생성 → MiroFish 5단계 시뮬레이션 →
리포트 추출·신호화 → 티커 매핑 + Polymarket → **Slack 한국어 리포트** 전송.

---

## 전체 흐름

```
scripts/report.sh
  └ up.sh        : Ollama + MiroFish(Docker) 기동 + 준비 대기
  └ run_daily.sh
      ├ mirofish_runner : 시드 생성 → MiroFish 5단계 → shared/out/latest.json
      │     seed.py → POST /api/graph/ontology/generate(시드 업로드)
      │            → /api/graph/build → /simulation/create → /prepare → /start
      │            → /api/report/generate → mirofish_export(봉투 벗겨 저장)
      └ pipeline        : latest.json → 추출 → 매핑 → Polymarket → Slack
            report_store → extractor → mapper → polymarket → reporter → slack
```

각 단계는 실패해도 가능한 한 부분 리포트를 전송 (graceful degrade).

---

## 모듈 맵 (`src/`)

| 모듈 | 역할 |
|------|------|
| `config.py` | `.env` 자동 로드 + `Settings` (LLM/공유폴더/MiroFish API/Slack/티커맵) |
| `seed.py` | Polymarket 금융 마켓 + 워치리스트 → MiroFish 시드 문서 + 예측 요구사항 |
| `mirofish_runner.py` | MiroFish 백엔드 5단계 헤드리스 구동 + 폴링(진행률) → latest.json |
| `mirofish_export.py` | `MiroFishClient`(get/post/upload) + 리포트 봉투 벗겨 저장 |
| `report_store.py` | `shared/out/latest.json` 로드 (없음/손상 graceful) |
| `extractor.py` | 리포트 산문 → 구조화 신호(JSON). 요약=한국어, 테마/엔티티=영어 강제 |
| `models.py` | `ExtractedSignal`/`EntitySentiment` + 방어적 검증 |
| `llm.py` | 로컬 Ollama OpenAI 호환 클라이언트 |
| `mapper.py` | `ticker_map.yaml` → themes/entities를 티커로 (실패는 로그) |
| `polymarket.py` | Gamma API 상위 거래량 마켓 → 키워드(단어경계) 필터 → Yes 확률 |
| `reporter.py` | 신호 + 티커 + Polymarket → Slack blocks 메시지 |
| `slack.py` | Incoming Webhook 전송 |
| `pipeline.py` | latest.json → 추출→매핑→Polymarket→Slack 오케스트레이션 |

스크립트(`scripts/`): `report.sh`(한 명령) · `up.sh`(기동) · `check.sh`(상태) ·
`run_daily.sh`(러너+파이프라인) · `setup.sh`(의존성+Ollama 설치).

테스트: `tests/` 43개 (외부 호출은 전부 Fake/mock으로 분리).

---

## 외부 의존

| 대상 | 위치 | 비고 |
|------|------|------|
| MiroFish 업스트림 | `~/Desktop/work/MiroFish` (Docker, :3000/:5001) | `.git` 제거·gitignore됨. LLM=Ollama, 메모리=Zep 무료 |
| Ollama | `:11434`, 모델 `qwen2.5:7b` | 추출 + MiroFish 시뮬레이션 LLM (무료) |
| Zep Cloud | 무료 티어 | MiroFish 기억 그래프 |
| Slack | Incoming Webhook | `.env`의 `SLACK_WEBHOOK_URL` |
| Polymarket | Gamma API (무가입) | 예측시장 확률 |

---

## 설정 (`.env`, gitignore됨)

```
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL_NAME=qwen2.5:7b
MIROFISH_SHARED_DIR=./shared/mirofish
MIROFISH_API_URL=http://localhost:5001
SLACK_WEBHOOK_URL=<webhook>
```

---

## 현재 한계 / 다음(v2)

- MiroFish 리포트가 **중국어**로 생성됨 → 추출 단계에서 한국어 요약으로 정규화(엔티티는 종종 중국어로 남아 매핑 miss, 단 테마가 티커를 커버해 무해).
- **Polymarket**: 테마가 섹터(반도체 등)뿐이면 매칭 마켓이 없을 수 있음 → 매크로 키워드 병행 검색 개선 후보.
- 시뮬레이션은 M2에서 느림(라운드 수로 조절).
- v2: Stooq 시장데이터(`fetcher.py`), Gmail 채널, 캐싱, 매일 오전 cron 자동화.
