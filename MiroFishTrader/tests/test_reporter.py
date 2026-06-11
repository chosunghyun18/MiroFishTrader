"""reporter 단위 테스트."""
from __future__ import annotations

from src.mapper import MappingResult
from src.models import EntitySentiment, ExtractedSignal
from src.polymarket import PolymarketMarket
from src.reporter import build_report_text, build_slack_payload

SIGNAL = ExtractedSignal(
    date="2026-06-11",
    source_report_id="r1",
    trend_direction="bullish",
    confidence=0.8,
    themes=["반도체", "AI"],
    entities=[EntitySentiment("NVIDIA", "positive")],
    summary="AI 수요로 반도체 강세",
)
MAPPING = MappingResult(tickers=["SOXX", "NVDA"], misses=[])
MARKETS = [
    PolymarketMarket("Fed cut in July?", 0.62, 50000, 0.03, "https://polymarket.com/market/fed"),
]


def test_report_text_contains_core():
    text = build_report_text(SIGNAL, MAPPING, MARKETS)
    assert "bullish" in text
    assert "80%" in text
    assert "SOXX" in text and "NVDA" in text
    assert "Fed cut in July?" in text
    assert "62%" in text


def test_empty_sections_graceful():
    empty_map = MappingResult()
    text = build_report_text(SIGNAL, empty_map, [])
    assert "매칭 없음" in text
    assert "관련 마켓 없음" in text


def test_slack_payload_shape():
    payload = build_slack_payload(SIGNAL, MAPPING, MARKETS)
    assert "text" in payload
    assert payload["blocks"][0]["type"] == "section"
    assert payload["blocks"][0]["text"]["type"] == "mrkdwn"
