"""로컬 Ollama LLM 클라이언트.

Ollama의 OpenAI 호환 엔드포인트(`/chat/completions`)를 사용한다.
MiroFish가 이미 띄우는 Ollama 인스턴스를 재사용하므로 추가 비용은 없다.
"""
from __future__ import annotations

from typing import Optional, Protocol

import requests


class LLMError(RuntimeError):
    """LLM 호출 실패."""


class SupportsComplete(Protocol):
    """추출 레이어가 의존하는 최소 인터페이스 (테스트 시 대체 가능)."""

    def complete(self, prompt: str) -> str: ...


class OllamaClient:
    """OpenAI 호환 채팅 엔드포인트를 호출하는 동기 클라이언트."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 120,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._session = session or requests.Session()

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        """프롬프트를 보내고 모델 응답 텍스트를 반환. 실패 시 LLMError."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, ValueError, IndexError) as exc:
            raise LLMError(f"Ollama 호출 실패: {exc}") from exc
