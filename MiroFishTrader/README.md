# MiroFishTrader

MiroFish 오픈소스로 투자 인사이트를 도출하고, 보고서를 만들어 **매일 오전 Slack으로 전달**하는 서비스.

- 투자 인사이트: (1) 신규 종목 발견 (2) 대중 추세 파악
- 투자 대상: Polymarket(예측시장), ETF 시장
- 비용 최소화 우선 (로컬 Ollama, 무료 데이터 소스)

설계 문서는 `memory/`(INDEX.md 진입점) 참조.

---

## 파이프라인

```
MiroFish 리포트(out/latest.json)
  → 추출(extractor): 자유 산문 → 구조화 신호
  → 매핑(mapper): themes/entities → 티커
  → Polymarket: themes → 예측시장 확률
  → 리포트(reporter) → Slack 전송
```

각 단계는 실패해도 부분 리포트를 전송하도록 graceful degrade 한다.

## 설치

```bash
cp .env.example .env   # 값 채우기 (특히 SLACK_WEBHOOK_URL)

# Python 의존성 + Ollama 설치 + 모델 pull 을 한 번에
bash scripts/setup.sh
```

수동으로 하려면:

```bash
pip install -r requirements.txt
# Ollama: https://ollama.com/download 설치 후
ollama pull qwen2.5:14b
```

## 실행

```bash
# 전송 없이 페이로드만 확인
python -m src.pipeline --dry-run

# 실제 Slack 전송
python -m src.pipeline
```

전제: MiroFish 배치가 `MIROFISH_SHARED_DIR/out/latest.json`을 미리 생성. 추출에는 로컬 Ollama가 필요하다(미실행 시 중립 신호로 degrade).

## 매일 오전 자동 실행

### cron (간단)

```bash
chmod +x scripts/run_daily.sh
crontab -e
# 매일 오전 8시
0 8 * * * /Users/jo/Desktop/work/MiroFishTrader/scripts/run_daily.sh >> /tmp/mirofishtrader.log 2>&1
```

### launchd (macOS 네이티브, 권장)

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

## 테스트

```bash
python -m pytest tests/ -q
```

## 현황 (v1 MVP)

- ✅ 추출 레이어 / 매핑 / Polymarket / 리포트 / Slack / 파이프라인 / 스케줄
- ⬜ v2: Yahoo/FRED 시장데이터, 시드 생성·MiroFish 배치 자동 트리거, Gmail, 캐싱
