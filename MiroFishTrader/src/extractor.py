"""추출 레이어 — MiroFish 리포트(자유 산문) → 구조화 신호.

MiroFish 리포트에는 구조화된 신호 필드가 없고 자유 산문만 있으므로,
LLM으로 한 번 더 파싱해 `ExtractedSignal` 스키마로 강제한다.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, Optional

from .llm import SupportsComplete
from .models import ExtractedSignal

PROMPT_TEMPLATE = """\
You are a financial signal extractor. Read the MiroFish prediction report below \
and extract a structured market-sentiment signal.

Return ONLY a JSON object (no prose, no code fences) with EXACTLY this shape:
{{
  "trend_direction": "bullish | bearish | neutral",
  "confidence": 0.0,
  "themes": ["short theme keyword", "..."],
  "entities": [{{"name": "Entity", "sentiment": "positive | negative | neutral"}}],
  "summary": "one-line summary"
}}

Rules:
- trend_direction: overall crowd/market direction implied by the report.
- confidence: 0.0-1.0, how strongly the report supports that direction.
- themes: 1-6 concise sector/topic keywords, IN ENGLISH (e.g. "semiconductors", "rates").
- entities: named companies/assets mentioned, with per-entity sentiment.
- Output valid JSON only.

REPORT:
{report_text}
"""


def _sections_to_text(report: Dict[str, Any]) -> str:
    """outline.sections → 텍스트 (markdown_content 부재 시 폴백)."""
    outline = report.get("outline") or {}
    parts = []
    summary = outline.get("summary")
    if summary:
        parts.append(str(summary))
    for section in outline.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "")).strip()
        content = str(section.get("content", "")).strip()
        parts.append(f"{title}\n{content}".strip())
    return "\n\n".join(p for p in parts if p)


def build_prompt(report: Dict[str, Any]) -> str:
    """리포트에서 본문 텍스트를 골라 추출 프롬프트 생성."""
    text = str(report.get("markdown_content") or "").strip()
    if not text:
        text = _sections_to_text(report)
    return PROMPT_TEMPLATE.format(report_text=text)


def _parse_json(text: str) -> Dict[str, Any]:
    """LLM 응답에서 JSON 객체를 추출/파싱. 코드펜스·잡텍스트 허용."""
    cleaned = text.strip()
    # ```json ... ``` 펜스 제거
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    # 첫 '{' ~ 마지막 '}' 구간만 취함
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("응답에서 JSON 객체를 찾지 못함")
    obj = json.loads(cleaned[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON 최상위가 객체가 아님")
    return obj


def _today() -> str:
    return _dt.date.today().isoformat()


def extract_signal(
    report: Dict[str, Any],
    llm: SupportsComplete,
    *,
    date: Optional[str] = None,
) -> ExtractedSignal:
    """MiroFish 리포트 dict → 검증된 ExtractedSignal.

    Raises:
        ValueError: LLM 응답을 JSON으로 파싱하지 못한 경우.
        LLMError: LLM 호출 자체가 실패한 경우.
    """
    prompt = build_prompt(report)
    raw_text = llm.complete(prompt)
    raw = _parse_json(raw_text)
    return ExtractedSignal.from_raw(
        raw,
        date=date or _today(),
        source_report_id=str(report.get("report_id", "")),
    )
