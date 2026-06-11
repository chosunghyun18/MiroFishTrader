"""추출 레이어 단위 테스트 (LLM 없이 FakeLLM으로 검증)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.extractor import build_prompt, extract_signal
from src.models import EntitySentiment, ExtractedSignal

SAMPLE = json.loads((Path(__file__).parent / "sample_report.json").read_text())


class FakeLLM:
    """고정 응답을 돌려주는 가짜 LLM. 마지막 프롬프트를 보관."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None

    def complete(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


CLEAN_JSON = json.dumps(
    {
        "trend_direction": "bullish",
        "confidence": 0.8,
        "themes": ["반도체", "AI"],
        "entities": [{"name": "NVIDIA", "sentiment": "positive"}],
        "summary": "AI 수요로 반도체 강세 전망",
    }
)


def test_extract_basic():
    sig = extract_signal(SAMPLE, FakeLLM(CLEAN_JSON), date="2026-06-11")
    assert isinstance(sig, ExtractedSignal)
    assert sig.trend_direction == "bullish"
    assert sig.confidence == 0.8
    assert sig.themes == ["반도체", "AI"]
    assert sig.entities == [EntitySentiment("NVIDIA", "positive")]
    assert sig.source_report_id == "rep_test_0001"
    assert sig.date == "2026-06-11"


def test_extract_strips_code_fence():
    fenced = f"```json\n{CLEAN_JSON}\n```"
    sig = extract_signal(SAMPLE, FakeLLM(fenced))
    assert sig.trend_direction == "bullish"


def test_extract_ignores_surrounding_prose():
    noisy = f"Here is the signal:\n{CLEAN_JSON}\nHope this helps!"
    sig = extract_signal(SAMPLE, FakeLLM(noisy))
    assert sig.confidence == 0.8


def test_invalid_trend_coerced_to_neutral():
    bad = json.dumps({"trend_direction": "MOON", "confidence": 0.5})
    sig = extract_signal(SAMPLE, FakeLLM(bad))
    assert sig.trend_direction == "neutral"


def test_confidence_clamped_and_coerced():
    over = json.dumps({"trend_direction": "bearish", "confidence": 9.9})
    assert extract_signal(SAMPLE, FakeLLM(over)).confidence == 1.0
    bad = json.dumps({"trend_direction": "bearish", "confidence": "nope"})
    assert extract_signal(SAMPLE, FakeLLM(bad)).confidence == 0.0


def test_entities_validated():
    raw = json.dumps(
        {
            "trend_direction": "neutral",
            "entities": [
                {"name": "Apple", "sentiment": "WEIRD"},  # 잘못된 심리 → neutral
                {"sentiment": "positive"},  # 이름 없음 → 제거
                {"name": "  Tesla  ", "sentiment": "negative"},  # 공백 트림
            ],
        }
    )
    sig = extract_signal(SAMPLE, FakeLLM(raw))
    assert sig.entities == [
        EntitySentiment("Apple", "neutral"),
        EntitySentiment("Tesla", "negative"),
    ]


def test_parse_failure_raises():
    with pytest.raises(ValueError):
        extract_signal(SAMPLE, FakeLLM("no json here at all"))


def test_prompt_uses_markdown_content():
    prompt = build_prompt(SAMPLE)
    assert "AI infrastructure spending" in prompt
    assert "JSON" in prompt


def test_prompt_falls_back_to_sections():
    report = {k: v for k, v in SAMPLE.items() if k != "markdown_content"}
    prompt = build_prompt(report)
    assert "Crowd Direction" in prompt
    assert "rate sensitivity" in prompt
