<div align="center">

# 🐟 MiroFishTrader

**여론·심리 시뮬레이션에서 투자 신호를 길어 올려, 매일 아침 Slack으로 배달합니다.**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](tests/)
[![LLM](https://img.shields.io/badge/LLM-Ollama%20(local)-000000?logo=ollama&logoColor=white)](https://ollama.com/)
[![Cost](https://img.shields.io/badge/cost-%240%20first-2EA043)](#-설계-원칙)
[![Status](https://img.shields.io/badge/status-v1%20MVP-blue)](#-현황)

</div>

---

## 📌 한눈에

[MiroFish](https://github.com/) 오픈소스(멀티에이전트 여론·심리 시뮬레이션)를 **외부 엔진**으로 활용해 투자 인사이트를 도출하고, 보고서를 만들어 **매일 오전 Slack으로 전달**하는 서비스입니다.

| 목표 | 내용 |
|------|------|
| 🔍 **신규 종목 발견** | 시뮬레이션에서 떠오르는 엔티티·테마 → 티커 후보 |
| 📊 **대중 추세 파악** | Polymarket 예측시장 + 군중 심리 방향성 |
| 🎯 **투자 대상** | Polymarket(예측시장), ETF 시장 |
| ⏱️ **투자 기간** | 1~2주 ~ 최대 2개월 이내 매도 |

> 📁 설계·의사결정 문서의 단일 소스(SSoT)는 Obsidian 볼트의 `Projects/work/MiroFishTrader/`(`memory/INDEX.md` 진입점)입니다.

---

## 🔄 파이프라인

```
 ┌─ 시드 생성 (seed) ───────────────────────────┐
 │   GDELT 뉴스 헤드라인  +  Polymarket 신호      │
 │   + 관심 섹터 워치리스트                        │
 │        → shared/mirofish/in/seed-YYYYMMDD.md   │
 └────────────────────┬──────────────────────────┘
                      ▼
        MiroFish 엔진 (외부 도구, 5단계 시뮬레이션)
                      ▼
        shared/mirofish/out/latest.json  ← 자유 산문 리포트
                      ▼
   ┌──────────────────────────────────────────────┐
   │  extractor  자유 산문 → 구조화 신호 (로컬 LLM)  │
   │  mapper     themes/entities → 티커             │
   │  polymarket themes → 예측시장 확률              │
   │  reporter   → Slack 페이로드                    │
   └──────────────────────┬───────────────────────┘
                      ▼
                 📨  Slack 전송
```

각 단계는 실패해도 **부분 리포트를 전송하도록 graceful degrade** 합니다. (LLM 미실행 → 중립 신호, 뉴스/마켓 조회 실패 → 빈 섹션)

---

## 🧩 MiroFish가 읽어오는 것

MiroFish는 **별개 외부 도구**입니다. MiroFishTrader는 이를 직접 제어하지 않고, 공유 폴더에 떨어진 **리포트 JSON만 읽어옵니다**. 내용은 구조화 신호가 아니라 **"미래 예측 보고서" 형태의 자유 산문**입니다.

| 읽어오는 필드 | 성격 | 활용 |
|--------------|------|------|
| `outline.summary` | 핵심 예측 한 문장 | 리포트 도입부 |
| `outline.sections[].content` | 자유 산문 2~5개 | 추출 레이어 입력 |
| `markdown_content` | 전체 리포트 마크다운 | 추출 레이어 입력 / 원문 첨부 |
| `simulation_requirement` | 주입한 시드 시나리오 | 어떤 시드였는지 추적 |

> ⚠️ **주의할 현실**
> - `sentiment_trend`·`insights`·`recommendations` 같은 **구조화 필드는 없습니다** — 모두 산문 안에 녹아 있음.
> - 섹션 제목은 실행마다 LLM이 새로 생성 → **특정 섹션명에 의존 불가**.
> - MiroFish는 엔티티/심리 중심이라 **종목 티커를 직접 주지 않습니다**.

그래서 추출 레이어(로컬 Ollama LLM)가 이 산문을 `trend_direction`·`confidence`·`themes`·`entities`·`summary` 구조화 신호 JSON으로 변환합니다. 데이터 계약은 [`mirofish-integration.md`](https://github.com/) 참조.

---

## ⚙️ 설치

```bash
cp .env.example .env   # 값 채우기 (특히 SLACK_WEBHOOK_URL)

# Python 의존성 + Ollama 설치 + 모델 pull 을 한 번에
bash scripts/setup.sh
```

<details>
<summary>수동 설치</summary>

```bash
pip install -r requirements.txt
# Ollama: https://ollama.com/download 설치 후
ollama pull qwen2.5:7b
```
</details>

### 환경변수 (`.env`)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | 추출 LLM (Ollama, OpenAI 호환) |
| `LLM_MODEL_NAME` | `qwen2.5:7b` | 추출 모델 |
| `MIROFISH_SHARED_DIR` | `./shared/mirofish` | 리포트 JSON 입출력 폴더 |
| `MIROFISH_API_URL` | `http://localhost:5001` | MiroFish 백엔드 API |
| `SLACK_WEBHOOK_URL` | _(필수)_ | 리포트 전송 대상 |

---

## 🚀 원할 때 리포트 받기 (한 명령)

서버 기동 → 준비 확인 → MiroFish 5단계 → Slack 전송까지 한 번에. 진행률이 로그로 표시됩니다.

```bash
bash scripts/report.sh                 # 전체 자동 (기본 max_rounds=10)
bash scripts/report.sh --max-rounds 5  # 더 빠르게 (가벼운 시뮬)
```

보조 스크립트:

```bash
bash scripts/check.sh   # 서버(Ollama/MiroFish/모델) 준비 상태만 확인
bash scripts/up.sh      # 서버만 기동 + 준비 대기
```

> ⏳ 시뮬레이션은 M2에서 수 분~십수 분 걸립니다. `report.sh` 실행 중 단계별 진행률(라운드/%)이 출력됩니다.

---

## 🛠️ 실행 (개별 단계)

```bash
python -m src.mirofish_runner --max-rounds 10   # MiroFish 5단계 → latest.json
python -m src.pipeline --dry-run                # 전송 없이 신호 확인
python -m src.pipeline                           # 실제 Slack 전송
```

추출에는 로컬 Ollama가 필요합니다(미실행 시 중립 신호로 degrade).

---

## ⏰ 매일 오전 자동 실행

<details>
<summary><b>cron (간단)</b></summary>

```bash
chmod +x scripts/run_daily.sh
crontab -e
# 매일 오전 8시
0 8 * * * /Users/jo/Desktop/work/MiroFishTrader/scripts/run_daily.sh >> /tmp/mirofishtrader.log 2>&1
```
</details>

<details>
<summary><b>launchd (macOS 네이티브, 권장)</b></summary>

`~/Library/LaunchAgents/com.mirofishtrader.daily.plist` 생성 후:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mirofishtrader.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/jo/Desktop/work/MiroFishTrader/scripts/run_daily.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/mirofishtrader.log</string>
  <key>StandardErrorPath</key><string>/tmp/mirofishtrader.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.mirofishtrader.daily.plist
```
</details>

---

## 🗂️ 코드 맵

```
src/
├── seed.py            시드 문서 생성 (뉴스 + Polymarket + 워치리스트)
├── sources/news.py    GDELT 2.0 뉴스 헤드라인 (무료·키 불필요)
├── mirofish_runner.py MiroFish 5단계 헤드리스 실행 → latest.json
├── mirofish_export.py MiroFish 리포트 export
├── extractor.py       자유 산문 → 구조화 신호 (LLM)
├── llm.py             Ollama 클라이언트
├── mapper.py          themes/entities → 티커
├── polymarket.py      예측시장 조회
├── reporter.py        Slack 페이로드 빌드
├── slack.py           Slack 전송
├── report_store.py    latest.json 로드/저장
├── pipeline.py        전체 오케스트레이션
├── config.py          .env 설정 로드
└── models.py          데이터 모델
```

---

## ✅ 테스트

```bash
python -m pytest tests/ -q
```

---

## 🎯 설계 원칙

- 💸 **비용 최소화 우선** — 로컬 Ollama, GDELT/Polymarket 등 무료 데이터 소스
- 🧱 **graceful degrade** — 어떤 단계가 실패해도 부분 리포트 전송
- 🧪 **DI + Protocol** — 외부 HTTP를 주입 가능하게 → 테스트 시 네트워크 불필요
- 📏 함수 50줄 이하 · 타입 어노테이션 필수 · 외부 호출부 에러 핸들링

---

## 📈 현황

**v1 MVP**

- ✅ 시드 생성(뉴스+Polymarket) / 추출 / 매핑 / Polymarket / 리포트 / Slack / 파이프라인 / 스케줄
- ⬜ **v2**: Yahoo/FRED 시장데이터, 시드 생성·MiroFish 배치 자동 트리거, Gmail 전달, 캐싱

<div align="center">

---

<sub>🐟 cost-first · local-first · degrade-gracefully</sub>

</div>
