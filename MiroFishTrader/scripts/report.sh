#!/usr/bin/env bash
# 원할 때 한 명령으로: 서버 기동·대기 → MiroFish 5단계 → Slack 리포트.
# 사용: bash scripts/report.sh [--max-rounds N]
set -euo pipefail
cd "$(dirname "$0")/.."

# .env 로드 (모델명 등)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

echo "═══ 1/2 서버 준비 ═══"
bash scripts/up.sh

echo ""
echo "═══ 2/2 리포트 생성 (진행률 로그 ↓) ═══"
exec bash scripts/run_daily.sh "$@"
