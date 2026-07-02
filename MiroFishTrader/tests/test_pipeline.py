"""pipeline 통합 테스트 (외부 의존 전부 Fake)."""
from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from src.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = json.loads((Path(__file__).parent / "sample_report.json").read_text())

CLEAN_JSON = json.dumps(
    {
        "trend_direction": "bullish",
        "confidence": 0.8,
        "themes": ["semiconductors", "rates"],
        "entities": [{"name": "NVIDIA", "sentiment": "positive"}],
        "summary": "강세",
    }
)


class FakeLLM:
    def __init__(self, response):
        self.response = response

    def complete(self, prompt):
        return self.response


class FakePM:
    def fetch_markets(self, limit=100):
        return [
            {
                "question": "Will the Fed cut rates?",
                "slug": "fed",
                "outcomePrices": ["0.6", "0.4"],
                "volume24hr": 100,
            }
        ]


def _settings(shared_dir):
    return Settings(
        llm_base_url="x",
        llm_model="x",
        mirofish_shared_dir=str(shared_dir),
        mirofish_api_url="http://localhost:5001",
        slack_webhook_url="",
        ticker_map_path=str(ROOT / "config" / "ticker_map.yaml"),
    )


def _seed_report(shared_dir):
    out = shared_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(SAMPLE), encoding="utf-8")


def test_pipeline_dry_run_full(tmp_path):
    _seed_report(tmp_path)
    payload = run_pipeline(
        _settings(tmp_path), llm=FakeLLM(CLEAN_JSON), pm_client=FakePM(), dry_run=True
    )
    text = payload["blocks"][0]["text"]["text"]
    assert "bullish" in text
    assert "SOXX" in text  # semiconductors → ticker_map.yaml
    assert "Will the Fed cut rates?" in text  # rates 테마 → Polymarket 매칭


def test_pipeline_no_report(tmp_path):
    payload = run_pipeline(
        _settings(tmp_path), llm=FakeLLM(CLEAN_JSON), pm_client=FakePM(), dry_run=True
    )
    assert "리포트가 없습니다" in payload["text"]


def test_pipeline_extract_failure_degrades(tmp_path):
    _seed_report(tmp_path)
    payload = run_pipeline(
        _settings(tmp_path), llm=FakeLLM("garbage no json"), pm_client=FakePM(), dry_run=True
    )
    # 추출 실패해도 크래시 없이 페이로드 생성 (중립)
    text = payload["blocks"][0]["text"]["text"]
    assert "neutral" in text


def test_pipeline_concurrent_extract_and_fetch_filters_by_theme(tmp_path):
    """_safe_extract(LLM)와 pm_client.fetch_markets가 동시에 실행되고,
    그 결과가 신호의 themes로 올바르게 필터링되어 payload에 반영되는지 검증."""
    _seed_report(tmp_path)

    class TrackingFakePM:
        """fetch_markets 호출 여부/횟수를 기록하는 가짜 클라이언트 (top-100, 테마 무관)."""

        def __init__(self):
            self.calls = 0

        def fetch_markets(self, limit=100):
            self.calls += 1
            return [
                {
                    "question": "Will the Fed cut rates?",
                    "slug": "fed",
                    "outcomePrices": ["0.6", "0.4"],
                    "volume24hr": 100,
                },
                {
                    "question": "New Rihanna album before GTA VI?",
                    "slug": "rihanna-gta",
                    "outcomePrices": ["0.5", "0.5"],
                    "volume24hr": 1,
                },
            ]

    pm = TrackingFakePM()
    payload = run_pipeline(
        _settings(tmp_path), llm=FakeLLM(CLEAN_JSON), pm_client=pm, dry_run=True
    )
    text = payload["blocks"][0]["text"]["text"]
    assert pm.calls == 1
    assert "Will the Fed cut rates?" in text  # "rates" 테마로 필터링되어 남음
    assert "Rihanna" not in text  # 테마와 무관해 필터링에서 제외됨


def test_pipeline_fetch_markets_error_still_returns_payload(tmp_path):
    """Polymarket fetch_markets가 예외를 던져도 markets는 빈 리스트로 degrade하고
    파이프라인은 부분 리포트를 정상적으로 반환해야 함."""
    _seed_report(tmp_path)

    class BrokenPM:
        def fetch_markets(self, limit=100):
            import requests

            raise requests.RequestException("boom")

    payload = run_pipeline(
        _settings(tmp_path), llm=FakeLLM(CLEAN_JSON), pm_client=BrokenPM(), dry_run=True
    )
    text = payload["blocks"][0]["text"]["text"]
    assert "bullish" in text
    assert "Fed" not in text
