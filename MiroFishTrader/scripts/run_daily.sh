#!/usr/bin/env bash
# MiroFishTrader 일일 리포트 실행 (cron/launchd에서 호출).
set -euo pipefail

# 프로젝트 루트로 이동
cd "$(dirname "$0")/.."

# .env 로드 (있으면)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# 1) MiroFish 5단계 실행 → shared/out/latest.json (실패해도 계속: 직전 리포트로 degrade)
python3 -m src.mirofish_runner "$@" || echo "WARN: MiroFish 러너 실패 — 직전 latest.json으로 진행"

# 2) 추출 → 매핑 → Polymarket → Slack 전송
exec python3 -m src.pipeline
