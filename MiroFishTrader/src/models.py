"""추출 신호 데이터 모델.

MiroFish 리포트(자유 산문)에서 뽑아낸 구조화 신호를 표현한다.
모든 입력은 신뢰할 수 없는 LLM 출력이므로 `from_raw`에서 방어적으로 검증한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal

TrendDirection = Literal["bullish", "bearish", "neutral"]
Sentiment = Literal["positive", "negative", "neutral"]

_VALID_TREND = {"bullish", "bearish", "neutral"}
_VALID_SENTIMENT = {"positive", "negative", "neutral"}


def _clamp_unit(value: Any) -> float:
    """confidence를 [0.0, 1.0] float로 강제. 변환 실패 시 0.0."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, num))


def _as_list(value: Any) -> List[Any]:
    """리스트가 아니면 빈 리스트로 정규화."""
    return value if isinstance(value, list) else []


@dataclass
class EntitySentiment:
    """언급된 엔티티와 그에 대한 개별 심리."""

    name: str
    sentiment: Sentiment = "neutral"

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "EntitySentiment":
        name = str(raw.get("name", "")).strip()
        sentiment = str(raw.get("sentiment", "neutral")).lower().strip()
        if sentiment not in _VALID_SENTIMENT:
            sentiment = "neutral"
        return cls(name=name, sentiment=sentiment)  # type: ignore[arg-type]


@dataclass
class ExtractedSignal:
    """추출 레이어의 확정 출력 스키마 (플랫 구조)."""

    date: str
    source_report_id: str
    trend_direction: TrendDirection = "neutral"
    confidence: float = 0.0
    themes: List[str] = field(default_factory=list)
    entities: List[EntitySentiment] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_raw(
        cls,
        raw: Dict[str, Any],
        *,
        date: str,
        source_report_id: str,
    ) -> "ExtractedSignal":
        """LLM 원시 dict + 리포트 메타데이터 → 검증된 신호."""
        trend = str(raw.get("trend_direction", "neutral")).lower().strip()
        if trend not in _VALID_TREND:
            trend = "neutral"

        themes = [
            str(t).strip() for t in _as_list(raw.get("themes")) if str(t).strip()
        ]
        entities = [
            EntitySentiment.from_raw(e)
            for e in _as_list(raw.get("entities"))
            if isinstance(e, dict)
        ]
        entities = [e for e in entities if e.name]

        return cls(
            date=date,
            source_report_id=source_report_id,
            trend_direction=trend,  # type: ignore[arg-type]
            confidence=_clamp_unit(raw.get("confidence", 0.0)),
            themes=themes,
            entities=entities,
            summary=str(raw.get("summary", "")).strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "source_report_id": self.source_report_id,
            "trend_direction": self.trend_direction,
            "confidence": self.confidence,
            "themes": self.themes,
            "entities": [asdict(e) for e in self.entities],
            "summary": self.summary,
        }
