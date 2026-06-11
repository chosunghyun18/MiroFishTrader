"""mapper 단위 테스트."""
from __future__ import annotations

from src.mapper import TickerMap, map_signal
from src.models import EntitySentiment, ExtractedSignal

TMAP = TickerMap(
    themes={"반도체": ["SOXX", "SMH"], "ai": ["BOTZ"]},
    entities={"nvidia": "NVDA"},
)


def _signal(themes=None, entities=None):
    return ExtractedSignal(
        date="2026-06-11",
        source_report_id="r1",
        themes=themes or [],
        entities=entities or [],
    )


def test_theme_mapping_and_dedupe():
    sig = _signal(themes=["반도체", "AI"], entities=[EntitySentiment("NVIDIA", "positive")])
    result = map_signal(sig, TMAP)
    assert result.tickers == ["SOXX", "SMH", "BOTZ", "NVDA"]
    assert result.misses == []


def test_case_insensitive():
    sig = _signal(themes=["반도체"], entities=[EntitySentiment("nvidia")])
    result = map_signal(sig, TMAP)
    assert "SOXX" in result.tickers and "NVDA" in result.tickers


def test_misses_recorded():
    sig = _signal(themes=["우주항공"], entities=[EntitySentiment("Unknown")])
    result = map_signal(sig, TMAP)
    assert result.tickers == []
    assert result.misses == ["우주항공", "Unknown"]


def test_load_from_yaml(tmp_path):
    p = tmp_path / "tm.yaml"
    p.write_text("themes:\n  금리: [TLT]\nentities:\n  Apple: AAPL\n", encoding="utf-8")
    tm = TickerMap.load(p)
    assert tm.themes == {"금리": ["TLT"]}
    assert tm.entities == {"apple": "AAPL"}
