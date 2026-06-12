#!/usr/bin/env bash
# 서버 준비 상태 확인. 모두 준비되면 exit 0, 아니면 1.
OLLAMA_URL="http://localhost:11434"
MIROFISH_URL="http://localhost:5001"
MODEL="${LLM_MODEL_NAME:-qwen2.5:7b}"

ok=0
echo "서버 상태:"

if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  echo "  ✓ Ollama (:11434)"
  if curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | grep -q "$MODEL"; then
    echo "  ✓ 모델 $MODEL"
  else
    echo "  ✗ 모델 $MODEL 없음 → ollama pull $MODEL"; ok=1
  fi
else
  echo "  ✗ Ollama (:11434) 안 뜸"; ok=1
fi

if curl -sf "$MIROFISH_URL/api/simulation/history?limit=1" >/dev/null 2>&1; then
  echo "  ✓ MiroFish 백엔드 (:5001)"
else
  echo "  ✗ MiroFish 백엔드 (:5001) 안 뜸"; ok=1
fi

[ "$ok" -eq 0 ] && echo "→ 전부 준비됨" || echo "→ 준비 안 됨 (scripts/up.sh 실행)"
exit "$ok"
