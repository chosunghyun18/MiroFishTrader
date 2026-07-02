"""polymarket 단위 테스트 (HTTP 없이 FakeClient)."""
from __future__ import annotations

from src.polymarket import filter_raw_markets, find_markets, parse_market

RAW_MARKETS = [
    {
        "question": "Will the Fed cut rates in July?",
        "slug": "fed-cut-july",
        "outcomePrices": "[\"0.62\", \"0.38\"]",
        "volume24hr": 50000,
        "oneDayPriceChange": 0.03,
    },
    {
        "question": "Will a recession start in 2026?",
        "slug": "recession-2026",
        "outcomePrices": ["0.20", "0.80"],
        "volume24hr": 10000,
        "oneDayPriceChange": -0.01,
    },
    {
        "question": "New Rihanna album before GTA VI?",
        "slug": "rihanna-gta",
        "outcomePrices": "[\"0.52\", \"0.48\"]",
        "volume24hr": 1000,
    },
]


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def fetch_markets(self, limit=100):
        return self.markets


def test_parse_market_string_prices():
    m = parse_market(RAW_MARKETS[0])
    assert m.yes_probability == 0.62
    assert m.day_change == 0.03
    assert m.url == "https://polymarket.com/market/fed-cut-july"


def test_parse_market_list_prices():
    m = parse_market(RAW_MARKETS[1])
    assert m.yes_probability == 0.20


def test_find_markets_keyword_filter():
    res = find_markets(["fed", "recession"], FakeClient(RAW_MARKETS))
    questions = [m.question for m in res]
    assert "Will the Fed cut rates in July?" in questions
    assert "Will a recession start in 2026?" in questions
    assert all("Rihanna" not in q for q in questions)


def test_find_markets_empty_keywords():
    assert find_markets([], FakeClient(RAW_MARKETS)) == []


def test_find_markets_handles_fetch_error():
    class BrokenClient:
        def fetch_markets(self, limit=100):
            import requests

            raise requests.RequestException("boom")

    assert find_markets(["fed"], BrokenClient()) == []


def test_find_markets_max_results():
    res = find_markets(["will"], FakeClient(RAW_MARKETS), max_results=1)
    assert len(res) == 1


def test_filter_raw_markets_matches_find_markets():
    """filter_raw_markets(원시 리스트)가 find_markets(fetch+filter)와 동일 결과를 내야 함
    (fetch를 신호 추출과 병렬 실행하기 위해 분리한 헬퍼)."""
    res = filter_raw_markets(RAW_MARKETS, ["fed", "recession"])
    questions = [m.question for m in res]
    assert "Will the Fed cut rates in July?" in questions
    assert "Will a recession start in 2026?" in questions
    assert all("Rihanna" not in q for q in questions)


def test_filter_raw_markets_empty_keywords():
    assert filter_raw_markets(RAW_MARKETS, []) == []


def test_filter_raw_markets_max_results():
    res = filter_raw_markets(RAW_MARKETS, ["will"], max_results=1)
    assert len(res) == 1


def test_word_boundary_avoids_substring_false_match():
    markets = [
        {"question": "Will Spain win the World Cup?", "slug": "spain", "outcomePrices": ["0.2", "0.8"]},
        {"question": "Latest AI breakthrough by 2027?", "slug": "ai", "outcomePrices": ["0.5", "0.5"]},
    ]
    res = find_markets(["ai"], FakeClient(markets))
    questions = [m.question for m in res]
    assert "Latest AI breakthrough by 2027?" in questions  # 단어 AI 매칭
    assert "Will Spain win the World Cup?" not in questions  # Sp-ai-n 오매칭 방지
