#!/usr/bin/env bash
# 서버(Ollama + MiroFish) 기동 후 준비될 때까지 대기.
set -euo pipefail

OLLAMA_URL="http://localhost:11434"
MIROFISH_URL="http://localhost:5001"
MIROFISH_DIR="${MIROFISH_DIR:-$HOME/Desktop/work/MiroFish}"
WAIT_SECS="${WAIT_SECS:-180}"

_up() { curl -sf "$1" >/dev/null 2>&1; }

# 1) Ollama
if ! _up "$OLLAMA_URL/api/tags"; then
  echo "▶ Ollama 시작..."
  open -a Ollama 2>/dev/null || nohup ollama serve >/tmp/ollama-mirofish.log 2>&1 &
fi

# 2) MiroFish (Docker)
if ! _up "$MIROFISH_URL/api/simulation/history?limit=1"; then
  echo "▶ MiroFish(Docker) 시작... ($MIROFISH_DIR)"
  ( cd "$MIROFISH_DIR" && docker compose up -d )
fi

# 3) 준비 대기
echo "준비 대기 중 (최대 ${WAIT_SECS}s)..."
deadline=$(( $(date +%s) + WAIT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if _up "$OLLAMA_URL/api/tags" && _up "$MIROFISH_URL/api/simulation/history?limit=1"; then
    echo "✓ 서버 준비 완료"
    exit 0
  fi
  sleep 3
done
echo "✗ 시간 초과 — 서버가 안 떴습니다. 'docker compose logs' 확인하세요." >&2
exit 1
