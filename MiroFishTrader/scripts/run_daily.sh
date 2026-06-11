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

exec python3 -m src.pipeline "$@"
