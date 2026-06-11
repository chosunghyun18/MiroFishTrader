"""환경설정 로딩 (.env / 환경변수)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    llm_base_url: str
    llm_model: str
    mirofish_shared_dir: str
    slack_webhook_url: str
    ticker_map_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            llm_model=os.getenv("LLM_MODEL_NAME", "qwen2.5:14b"),
            mirofish_shared_dir=os.getenv("MIROFISH_SHARED_DIR", "./shared/mirofish"),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip(),
            ticker_map_path=os.getenv(
                "TICKER_MAP_PATH", str(_ROOT / "config" / "ticker_map.yaml")
            ),
        )
