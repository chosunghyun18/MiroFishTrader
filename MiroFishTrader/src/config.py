"""환경설정 로딩 (.env / 환경변수).

`python -m src.pipeline`을 직접 실행해도 동작하도록, 프로젝트 루트의 `.env`를
가볍게 읽어 환경변수로 주입한다 (이미 설정된 변수는 덮어쓰지 않음).
외부 의존성(python-dotenv) 없이 최소 구현.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """루트 `.env`를 읽어 os.environ에 주입 (기존 값 우선)."""
    env_path = path or (_ROOT / ".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass
class Settings:
    llm_base_url: str
    llm_model: str
    mirofish_shared_dir: str
    mirofish_api_url: str
    slack_webhook_url: str
    ticker_map_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            llm_model=os.getenv("LLM_MODEL_NAME", "qwen2.5:7b"),
            mirofish_shared_dir=os.getenv("MIROFISH_SHARED_DIR", "./shared/mirofish"),
            mirofish_api_url=os.getenv("MIROFISH_API_URL", "http://localhost:5001"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            ticker_map_path=os.getenv(
                "TICKER_MAP_PATH", str(_ROOT / "config" / "ticker_map.yaml")
            ),
        )
