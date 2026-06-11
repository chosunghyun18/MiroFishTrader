#!/usr/bin/env bash
# MiroFishTrader v1 셋업 — Python 의존성 + Ollama 설치 + 모델 pull.
# Mac/Linux에서 한 번 실행: bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/3] Python 의존성 설치"
python3 -m pip install -r requirements.txt

echo "[2/3] Ollama 설치 확인"
if ! command -v ollama >/dev/null 2>&1; then
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install ollama
      else
        echo "Homebrew 없음 → https://ollama.com/download 에서 설치 후 재실행하세요."
        exit 1
      fi
      ;;
    Linux)
      curl -fsSL https://ollama.com/install.sh | sh
      ;;
    *)
      echo "지원되지 않는 OS → https://ollama.com/download 참고."
      exit 1
      ;;
  esac
else
  echo "  ollama 이미 설치됨"
fi

echo "[3/3] 모델 pull"
MODEL="qwen2.5:7b"
if [ -f .env ]; then
  envmodel="$(grep -E '^LLM_MODEL_NAME=' .env | cut -d= -f2- || true)"
  [ -n "${envmodel:-}" ] && MODEL="$envmodel"
fi
echo "  pull: $MODEL"
ollama pull "$MODEL"

echo ""
echo "완료. 다음 단계:"
echo "  1) 'ollama serve' 가 실행 중인지 확인 (macOS 앱 실행 시 자동)"
echo "  2) python3 -m src.pipeline --dry-run   # 스모크 테스트"
